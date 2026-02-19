"""
Paperless Document Audit Service.

Audits Paperless-NGX documents using local LLMs via MCPManager.
Identifies missing/incorrect metadata and OCR quality issues.
Supports configurable fix modes: review, auto_threshold, auto_all.

Pattern: Like NotificationPollerService — takes MCPManager in constructor,
calls MCP tools for all Paperless interactions.
"""

import asyncio
import json
import logging
import re
from datetime import datetime, UTC
from uuid import uuid4

from sqlalchemy import func, select

from utils.config import settings

logger = logging.getLogger(__name__)

# Content length limit for LLM analysis (avoid huge prompts)
_MAX_CONTENT_LENGTH = 3000


class PaperlessAuditService:
    """Audit Paperless documents using local LLMs via MCPManager."""

    def __init__(self, mcp_manager, db_factory):
        self._mcp = mcp_manager
        self._db_factory = db_factory  # AsyncSessionLocal
        self._task: asyncio.Task | None = None
        self._running = False
        self._progress = {"current": 0, "total": 0, "current_doc_id": None}
        self._cancel_requested = False

    async def start(self):
        """Start only if Paperless MCP server is available."""
        if not self._mcp.has_server("paperless"):
            logger.info("Paperless MCP not configured — audit service disabled")
            return

        # Verify connectivity with a simple search
        try:
            test = await self._mcp.execute_tool(
                "mcp.paperless.search_documents", {"max_results": 1}
            )
            if not test.get("success"):
                logger.warning("Paperless MCP not reachable — audit service disabled")
                return
        except Exception as e:
            logger.warning(f"Paperless MCP connectivity check failed: {e}")
            return

        logger.info("Paperless Audit Service started")

    async def stop(self):
        """Cancel running audit task."""
        self._cancel_requested = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._running = False
        logger.info("Paperless Audit Service stopped")

    def get_status(self) -> dict:
        """Return current audit status."""
        return {
            "running": self._running,
            "progress": self._progress["current"],
            "total": self._progress["total"],
            "current_doc_id": self._progress["current_doc_id"],
        }

    async def run_audit(
        self,
        mode: str = "new_only",
        fix_mode: str | None = None,
        confidence_threshold: float | None = None,
        document_ids: list[int] | None = None,
    ) -> dict:
        """Run audit. Called from admin API.

        Args:
            mode: "new_only" (skip already audited) or "full" (re-audit all)
            fix_mode: "review" | "auto_threshold" | "auto_all"
            confidence_threshold: Min confidence for auto_threshold mode
            document_ids: Specific doc IDs to audit (None = all)

        Returns:
            {"run_id": str, "total": int, "processed": int, "changes_found": int}
        """
        if self._running:
            return {"error": "Audit already running"}

        fix_mode = fix_mode or settings.paperless_audit_fix_mode
        confidence_threshold = confidence_threshold or settings.paperless_audit_confidence_threshold
        run_id = str(uuid4())

        self._running = True
        self._cancel_requested = False
        self._progress = {"current": 0, "total": 0, "current_doc_id": None}

        try:
            # 1. Get document list via MCP
            if document_ids:
                doc_ids = document_ids
            else:
                doc_ids = await self._fetch_all_doc_ids()

            # 2. Filter already audited (mode=new_only)
            if mode == "new_only" and not document_ids:
                doc_ids = await self._filter_unaudited(doc_ids)

            total = len(doc_ids)
            self._progress["total"] = total
            processed = 0
            changes_found = 0

            logger.info(f"Audit started: run_id={run_id} mode={mode} fix_mode={fix_mode} docs={total}")

            # 3. Fetch available metadata for LLM context
            available_metadata = await self._fetch_available_metadata()

            # 4. Process each document
            for i, doc_id in enumerate(doc_ids):
                if self._cancel_requested:
                    logger.info(f"Audit cancelled at {i}/{total}")
                    break

                self._progress["current"] = i + 1
                self._progress["current_doc_id"] = doc_id

                try:
                    result = await self._analyze_document(doc_id, run_id, available_metadata)
                    processed += 1

                    if result and result.changes_needed:
                        changes_found += 1

                        # Apply fix based on mode
                        if fix_mode == "auto_all":
                            await self._apply_fix(result)
                        elif fix_mode == "auto_threshold" and (result.confidence or 0) >= confidence_threshold:
                            await self._apply_fix(result)
                        # else: stays "pending" for review

                    logger.info(
                        f"Audit [{i + 1}/{total}] doc={doc_id} "
                        f"changes={result.changes_needed if result else 'error'} "
                        f"confidence={result.confidence if result else 'N/A'}"
                    )
                except Exception as e:
                    logger.error(f"Audit failed for doc {doc_id}: {e}")

                # Throttle
                await asyncio.sleep(settings.paperless_audit_batch_delay)

            return {
                "run_id": run_id,
                "total": total,
                "processed": processed,
                "changes_found": changes_found,
            }
        finally:
            self._running = False
            self._progress["current_doc_id"] = None

    async def run_audit_background(self, **kwargs) -> str:
        """Start audit as background task. Returns run_id."""
        run_id = str(uuid4())

        async def _run():
            await self.run_audit(**kwargs)

        self._task = asyncio.create_task(_run(), name=f"paperless-audit-{run_id}")
        return run_id

    async def _fetch_all_doc_ids(self) -> list[int]:
        """Fetch all document IDs from Paperless via MCP."""
        result = await self._mcp.execute_tool(
            "mcp.paperless.search_documents", {"max_results": 500}
        )
        if not result.get("success"):
            logger.warning(f"Failed to fetch documents: {result.get('message')}")
            return []

        parsed = self._parse_mcp_result(result)
        if not parsed:
            return []

        results = parsed.get("results", [])
        return [doc["id"] for doc in results if "id" in doc]

    async def _filter_unaudited(self, doc_ids: list[int]) -> list[int]:
        """Filter out already-audited document IDs."""
        if not doc_ids:
            return []

        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult.paperless_doc_id).where(
                PaperlessAuditResult.paperless_doc_id.in_(doc_ids)
            )
            result = await db.execute(stmt)
            audited_ids = set(result.scalars().all())

        return [did for did in doc_ids if did not in audited_ids]

    async def _fetch_available_metadata(self) -> dict:
        """Fetch available document types, correspondents, tags from Paperless.

        Uses search results to extract unique values (no dedicated API needed via MCP).
        """
        result = await self._mcp.execute_tool(
            "mcp.paperless.search_documents", {"max_results": 200}
        )
        parsed = self._parse_mcp_result(result)
        if not parsed:
            return {"types": [], "correspondents": [], "tags": []}

        results = parsed.get("results", [])
        types = set()
        correspondents = set()
        tags = set()

        for doc in results:
            if doc.get("document_type"):
                types.add(doc["document_type"])
            if doc.get("correspondent"):
                correspondents.add(doc["correspondent"])
            for tag in doc.get("tags") or []:
                tags.add(tag)

        # Also check summary for top correspondents/types
        summary = parsed.get("summary", {})
        for item in summary.get("top_correspondents", []):
            correspondents.add(item["name"])
        for item in summary.get("top_document_types", []):
            types.add(item["name"])

        return {
            "types": sorted(types),
            "correspondents": sorted(correspondents),
            "tags": sorted(tags),
        }

    async def _analyze_document(self, doc_id: int, run_id: str, available_metadata: dict):
        """Analyze a single document via MCP + LLM."""
        from models.database import PaperlessAuditResult

        # 1. Fetch document via MCP
        result = await self._mcp.execute_tool(
            "mcp.paperless.get_document", {"document_id": doc_id}
        )
        if not result.get("success"):
            logger.warning(f"Failed to fetch doc {doc_id}: {result.get('message')}")
            return None

        doc = self._parse_mcp_result(result)
        if not doc:
            return None

        content = doc.get("content", "") or ""

        # 2. Heuristic OCR quality check (no LLM needed)
        ocr_quality, ocr_issues = self._check_ocr_quality(content)

        # 3. Call LLM for analysis
        analysis = await self._llm_analyze(doc, available_metadata)

        # 4. Store in DB
        async with self._db_factory() as db:
            # Check if already exists (upsert)
            existing = await db.execute(
                select(PaperlessAuditResult).where(
                    PaperlessAuditResult.paperless_doc_id == doc_id
                )
            )
            audit_result = existing.scalar_one_or_none()

            if audit_result:
                # Update existing
                audit_result.current_title = doc.get("title")
                audit_result.current_correspondent = doc.get("correspondent")
                audit_result.current_document_type = doc.get("document_type")
                audit_result.current_tags = doc.get("tags")
                audit_result.suggested_title = analysis.get("suggested_title") if analysis else None
                audit_result.suggested_correspondent = analysis.get("suggested_correspondent") if analysis else None
                audit_result.suggested_document_type = analysis.get("suggested_document_type") if analysis else None
                audit_result.suggested_tags = analysis.get("suggested_tags") if analysis else None
                audit_result.ocr_quality = ocr_quality
                audit_result.ocr_issues = ocr_issues
                audit_result.confidence = analysis.get("confidence", 0.0) if analysis else None
                audit_result.changes_needed = analysis.get("changes_needed", False) if analysis else False
                audit_result.reasoning = analysis.get("reasoning") if analysis else None
                audit_result.status = "pending"
                audit_result.audited_at = datetime.now(UTC).replace(tzinfo=None)
                audit_result.audit_run_id = run_id
            else:
                audit_result = PaperlessAuditResult(
                    paperless_doc_id=doc_id,
                    current_title=doc.get("title"),
                    current_correspondent=doc.get("correspondent"),
                    current_document_type=doc.get("document_type"),
                    current_tags=doc.get("tags"),
                    suggested_title=analysis.get("suggested_title") if analysis else None,
                    suggested_correspondent=analysis.get("suggested_correspondent") if analysis else None,
                    suggested_document_type=analysis.get("suggested_document_type") if analysis else None,
                    suggested_tags=analysis.get("suggested_tags") if analysis else None,
                    ocr_quality=ocr_quality,
                    ocr_issues=ocr_issues,
                    confidence=analysis.get("confidence", 0.0) if analysis else None,
                    changes_needed=analysis.get("changes_needed", False) if analysis else False,
                    reasoning=analysis.get("reasoning") if analysis else None,
                    audit_run_id=run_id,
                )
                db.add(audit_result)

            await db.commit()
            await db.refresh(audit_result)
            return audit_result

    async def _llm_analyze(self, doc: dict, available_metadata: dict) -> dict | None:
        """Run LLM analysis on a document."""
        from services.prompt_manager import prompt_manager
        from utils.llm_client import (
            extract_response_content,
            get_classification_chat_kwargs,
            get_default_client,
        )

        model = settings.paperless_audit_model or settings.ollama_model
        content = (doc.get("content") or "")[:_MAX_CONTENT_LENGTH]

        try:
            lang = settings.default_language
            system_prompt = prompt_manager.get(
                "paperless_audit", "system", lang=lang,
            )
            user_prompt = prompt_manager.get(
                "paperless_audit", "analyze_document", lang=lang,
                title=doc.get("title") or "—",
                correspondent=doc.get("correspondent") or "—",
                document_type=doc.get("document_type") or "—",
                tags=", ".join(doc.get("tags") or []) or "—",
                available_types=", ".join(available_metadata.get("types", [])) or "—",
                available_correspondents=", ".join(available_metadata.get("correspondents", [])) or "—",
                available_tags=", ".join(available_metadata.get("tags", [])) or "—",
                content=content or "(kein OCR-Text vorhanden)",
            )
            llm_options = prompt_manager.get_config("paperless_audit", "llm_options") or {}

            client = get_default_client()
            response = await client.chat(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                options=llm_options,
                **get_classification_chat_kwargs(model),
            )
            raw_text = extract_response_content(response)
        except Exception as e:
            logger.error(f"LLM analysis failed: {e}")
            return None

        if not raw_text:
            logger.warning("LLM returned empty response")
            return None

        # Parse JSON from response
        return self._parse_llm_response(raw_text)

    @staticmethod
    def _parse_llm_response(raw_text: str) -> dict | None:
        """Parse JSON from LLM response, handling markdown code blocks."""
        text = raw_text.strip()

        # Remove markdown code blocks
        if "```" in text:
            match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if match:
                text = match.group(1)

        # Find JSON object
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace >= 0 and last_brace > first_brace:
            text = text[first_brace:last_brace + 1]

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM JSON response: {e}")
            return None

    async def _apply_fix(self, result) -> bool:
        """Apply suggested fix via MCP update_document tool."""
        params = {"document_id": result.paperless_doc_id}
        has_changes = False

        if result.suggested_title and result.suggested_title != result.current_title:
            params["title"] = result.suggested_title
            has_changes = True
        if result.suggested_correspondent and result.suggested_correspondent != result.current_correspondent:
            params["correspondent"] = result.suggested_correspondent
            has_changes = True
        if result.suggested_document_type and result.suggested_document_type != result.current_document_type:
            params["document_type"] = result.suggested_document_type
            has_changes = True
        if result.suggested_tags and result.suggested_tags != result.current_tags:
            params["tags"] = result.suggested_tags
            has_changes = True

        if not has_changes:
            return True

        mcp_result = await self._mcp.execute_tool("mcp.paperless.update_document", params)

        async with self._db_factory() as db:
            from models.database import PaperlessAuditResult

            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.id == result.id
            )
            db_result = (await db.execute(stmt)).scalar_one_or_none()
            if db_result:
                if mcp_result.get("success"):
                    db_result.status = "applied"
                    db_result.applied_at = datetime.now(UTC).replace(tzinfo=None)
                else:
                    db_result.status = "failed"
                    logger.error(f"MCP update failed for doc {result.paperless_doc_id}: {mcp_result.get('message')}")
                await db.commit()

        return mcp_result.get("success", False)

    async def apply_results(self, result_ids: list[int]) -> dict:
        """Apply fixes for specific result IDs. Called from admin API."""
        from models.database import PaperlessAuditResult

        applied = 0
        failed = 0

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.id.in_(result_ids),
                PaperlessAuditResult.status == "pending",
                PaperlessAuditResult.changes_needed.is_(True),
            )
            results = (await db.execute(stmt)).scalars().all()

        for result in results:
            success = await self._apply_fix(result)
            if success:
                applied += 1
            else:
                failed += 1

        return {"applied": applied, "failed": failed}

    async def skip_results(self, result_ids: list[int]) -> dict:
        """Mark results as skipped. Called from admin API."""
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.id.in_(result_ids),
                PaperlessAuditResult.status == "pending",
            )
            results = (await db.execute(stmt)).scalars().all()
            count = 0
            for result in results:
                result.status = "skipped"
                count += 1
            await db.commit()

        return {"skipped": count}

    async def reprocess_documents(self, result_ids: list[int]) -> dict:
        """Trigger re-OCR for specific results via MCP."""
        from models.database import PaperlessAuditResult

        triggered = 0
        failed = 0

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.id.in_(result_ids),
            )
            results = (await db.execute(stmt)).scalars().all()

        for result in results:
            mcp_result = await self._mcp.execute_tool(
                "mcp.paperless.reprocess_document",
                {"document_id": result.paperless_doc_id},
            )
            if mcp_result.get("success"):
                triggered += 1
            else:
                failed += 1

        return {"triggered": triggered, "failed": failed}

    async def get_results(
        self,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        changes_needed: bool | None = None,
        ocr_quality_max: int | None = None,
    ) -> dict:
        """Get paginated audit results."""
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            query = select(PaperlessAuditResult)

            if status:
                query = query.where(PaperlessAuditResult.status == status)
            if changes_needed is not None:
                query = query.where(PaperlessAuditResult.changes_needed.is_(changes_needed))
            if ocr_quality_max is not None:
                query = query.where(PaperlessAuditResult.ocr_quality <= ocr_quality_max)

            # Count total
            count_query = select(func.count()).select_from(query.subquery())
            total = (await db.execute(count_query)).scalar() or 0

            # Paginate
            query = query.order_by(PaperlessAuditResult.audited_at.desc())
            query = query.offset((page - 1) * per_page).limit(per_page)
            results = (await db.execute(query)).scalars().all()

            return {
                "results": [self._result_to_dict(r) for r in results],
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": (total + per_page - 1) // per_page,
            }

    async def get_result_by_id(self, result_id: int) -> dict | None:
        """Get a single audit result."""
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(PaperlessAuditResult.id == result_id)
            result = (await db.execute(stmt)).scalar_one_or_none()
            if not result:
                return None
            return self._result_to_dict(result)

    async def get_stats(self) -> dict:
        """Get aggregated audit statistics."""
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            total = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult)
            )).scalar() or 0

            changes_needed = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.changes_needed.is_(True)
                )
            )).scalar() or 0

            applied = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.status == "applied"
                )
            )).scalar() or 0

            skipped = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.status == "skipped"
                )
            )).scalar() or 0

            pending = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.status == "pending"
                )
            )).scalar() or 0

            failed = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.status == "failed"
                )
            )).scalar() or 0

            avg_confidence = (await db.execute(
                select(func.avg(PaperlessAuditResult.confidence)).where(
                    PaperlessAuditResult.confidence.isnot(None)
                )
            )).scalar()

            # OCR quality distribution
            ocr_dist = {}
            for score in range(1, 6):
                count = (await db.execute(
                    select(func.count()).select_from(PaperlessAuditResult).where(
                        PaperlessAuditResult.ocr_quality == score
                    )
                )).scalar() or 0
                ocr_dist[str(score)] = count

            return {
                "total_audited": total,
                "changes_needed": changes_needed,
                "applied": applied,
                "skipped": skipped,
                "pending": pending,
                "failed": failed,
                "avg_confidence": round(avg_confidence, 3) if avg_confidence else None,
                "ocr_quality_distribution": ocr_dist,
            }

    @staticmethod
    def _check_ocr_quality(content: str) -> tuple[int, str]:
        """Rate OCR quality 1-5 based on heuristics."""
        if not content or len(content.strip()) < 20:
            return 1, "No/minimal OCR text"

        issues = []

        # Check space ratio (garbled text has very few spaces)
        space_ratio = content.count(" ") / len(content)
        if space_ratio < 0.03:
            issues.append("Very few spaces (garbled)")

        # Check for repeated characters
        if re.search(r'(.)\1{5,}', content):
            issues.append("Repeated characters")

        # Check alphanumeric ratio
        alnum = sum(c.isalnum() or c.isspace() for c in content)
        if alnum / len(content) < 0.6:
            issues.append("High special char ratio")

        # Check for very short lines (fragmented OCR)
        lines = [line for line in content.split('\n') if line.strip()]
        if lines:
            avg_line_len = sum(len(line) for line in lines) / len(lines)
            if avg_line_len < 10 and len(lines) > 5:
                issues.append("Fragmented text (very short lines)")

        score = max(1, 5 - len(issues))
        return score, "; ".join(issues) or "OK"

    @staticmethod
    def _parse_mcp_result(result: dict) -> dict | None:
        """Parse the inner JSON from MCP tool response."""
        message = result.get("message", "")
        if not message:
            return None

        try:
            return json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _result_to_dict(r) -> dict:
        """Convert a PaperlessAuditResult to dict for API response."""
        return {
            "id": r.id,
            "paperless_doc_id": r.paperless_doc_id,
            "current_title": r.current_title,
            "current_correspondent": r.current_correspondent,
            "current_document_type": r.current_document_type,
            "current_tags": r.current_tags,
            "suggested_title": r.suggested_title,
            "suggested_correspondent": r.suggested_correspondent,
            "suggested_document_type": r.suggested_document_type,
            "suggested_tags": r.suggested_tags,
            "ocr_quality": r.ocr_quality,
            "ocr_issues": r.ocr_issues,
            "confidence": r.confidence,
            "changes_needed": r.changes_needed,
            "reasoning": r.reasoning,
            "status": r.status,
            "audited_at": r.audited_at.isoformat() if r.audited_at else None,
            "applied_at": r.applied_at.isoformat() if r.applied_at else None,
            "audit_run_id": r.audit_run_id,
        }
