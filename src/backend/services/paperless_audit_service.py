"""
Paperless Document Audit Service.

Audits Paperless-NGX documents using local LLMs via MCPManager.
Identifies missing/incorrect metadata and OCR quality issues.
Supports configurable fix modes: review, auto_threshold, auto_all.

Pattern: Like NotificationPollerService — takes MCPManager in constructor,
calls MCP tools for all Paperless interactions.
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from difflib import SequenceMatcher
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
        fix_mode = fix_mode or settings.paperless_audit_fix_mode
        confidence_threshold = confidence_threshold or settings.paperless_audit_confidence_threshold
        run_id = str(uuid4())

        # State is set by run_audit_background() when called via API.
        # Set here too for direct calls (e.g. tests).
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
                        if fix_mode == "auto_all" or (
                            fix_mode == "auto_threshold" and (result.confidence or 0) >= confidence_threshold
                        ):
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
        """Start audit as background task. Returns immediately."""
        if self._running:
            return "already_running"

        self._running = True
        self._cancel_requested = False
        self._progress = {"current": 0, "total": 0, "current_doc_id": None}

        async def _run():
            try:
                await self.run_audit(**kwargs)
            except Exception as e:
                logger.error(f"Background audit failed: {e}")

        self._task = asyncio.create_task(_run(), name="paperless-audit")
        return "started"

    async def _fetch_all_doc_ids(self) -> list[int]:
        """Fetch all document IDs from Paperless via MCP.

        Uses date-based pagination since MCP responses are truncated
        to ~10KB (mcp_max_response_size). Each page yields ~20 results
        with snippets, so we walk backwards by created date.
        """
        all_ids: list[int] = []
        created_before: str | None = None
        seen_ids: set[int] = set()

        for page in range(200):  # Safety limit: max 200 pages
            params: dict = {"max_results": 100, "ordering": "-created"}
            if created_before:
                params["created_before"] = created_before

            result = await self._mcp.execute_tool(
                "mcp.paperless.search_documents", params
            )
            if not result.get("success"):
                logger.warning(f"Failed to fetch documents page {page}: {result.get('message')}")
                break

            parsed = self._parse_mcp_result(result)
            if not parsed:
                break

            results = parsed.get("results", [])
            if not results:
                break

            new_count = 0
            oldest_date = None
            for doc in results:
                doc_id = doc.get("id")
                if doc_id and doc_id not in seen_ids:
                    all_ids.append(doc_id)
                    seen_ids.add(doc_id)
                    new_count += 1
                if doc.get("created"):
                    oldest_date = doc["created"]

            # Check if we got all documents
            total_matching = parsed.get("summary", {}).get("total_matching", 0)
            if len(all_ids) >= total_matching:
                break

            # No new documents found — we've exhausted this date range
            if new_count == 0:
                break

            # Move pagination cursor to day before oldest result
            if oldest_date:
                created_before = oldest_date
            else:
                break

        logger.info(f"Fetched {len(all_ids)} document IDs in {page + 1} page(s)")
        return all_ids

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
        """Fetch available document types, correspondents, tags, storage paths,
        and custom field definitions from Paperless.
        """
        result = await self._mcp.execute_tool(
            "mcp.paperless.search_documents", {"max_results": 200}
        )
        parsed = self._parse_mcp_result(result)
        if not parsed:
            return {"types": [], "correspondents": [], "tags": [], "storage_paths": [], "custom_fields": []}

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

        # Fetch storage paths via MCP
        storage_paths = []
        try:
            sp_result = await self._mcp.execute_tool("mcp.paperless.list_storage_paths", {})
            sp_parsed = self._parse_mcp_result(sp_result)
            if sp_parsed:
                storage_paths = [p["path"] for p in sp_parsed.get("paths", [])]
        except Exception as e:
            logger.warning(f"Failed to fetch storage paths: {e}")

        # Fetch custom field definitions via MCP
        custom_fields = []
        try:
            cf_result = await self._mcp.execute_tool("mcp.paperless.list_custom_fields", {})
            cf_parsed = self._parse_mcp_result(cf_result)
            if cf_parsed:
                custom_fields = cf_parsed.get("fields", [])
        except Exception as e:
            logger.warning(f"Failed to fetch custom fields: {e}")

        return {
            "types": sorted(types),
            "correspondents": sorted(correspondents),
            "tags": sorted(tags),
            "storage_paths": storage_paths,
            "custom_fields": custom_fields,
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

        # 2. Heuristic checks (no LLM needed)
        ocr_quality, ocr_issues = self._check_ocr_quality(content)
        missing_fields = self._check_missing_fields(doc)
        content_completeness, completeness_issues = self._check_content_completeness(
            content, doc.get("page_count")
        )
        content_hash = hashlib.md5(content[:1000].encode()).hexdigest() if content else None

        # 3. Call LLM for analysis
        analysis = await self._llm_analyze(doc, available_metadata)

        # 4. Determine changes_needed (extended logic)
        changes_needed = analysis.get("changes_needed", False) if analysis else False
        if analysis and not changes_needed:
            # Also mark as changed if LLM suggested new date/storage_path/custom_fields
            current_date = doc.get("created")
            if analysis.get("suggested_date") and analysis["suggested_date"] != current_date:
                changes_needed = True
            if analysis.get("suggested_storage_path") and analysis["suggested_storage_path"] != doc.get("storage_path"):
                changes_needed = True
            if analysis.get("suggested_custom_fields"):
                changes_needed = True

        # 5. Store in DB
        async with self._db_factory() as db:
            # Check if already exists (upsert)
            existing = await db.execute(
                select(PaperlessAuditResult).where(
                    PaperlessAuditResult.paperless_doc_id == doc_id
                )
            )
            audit_result = existing.scalar_one_or_none()

            field_values = dict(
                current_title=doc.get("title"),
                current_correspondent=doc.get("correspondent"),
                current_document_type=doc.get("document_type"),
                current_tags=doc.get("tags"),
                current_date=doc.get("created"),
                current_storage_path=doc.get("storage_path"),
                current_custom_fields=doc.get("custom_fields"),
                suggested_title=analysis.get("suggested_title") if analysis else None,
                suggested_correspondent=analysis.get("suggested_correspondent") if analysis else None,
                suggested_document_type=analysis.get("suggested_document_type") if analysis else None,
                suggested_tags=analysis.get("suggested_tags") if analysis else None,
                suggested_date=analysis.get("suggested_date") if analysis else None,
                suggested_storage_path=analysis.get("suggested_storage_path") if analysis else None,
                suggested_custom_fields=analysis.get("suggested_custom_fields") if analysis else None,
                detected_language=analysis.get("detected_language") if analysis else None,
                ocr_quality=ocr_quality,
                ocr_issues=ocr_issues,
                missing_fields=missing_fields or None,
                content_completeness=content_completeness,
                completeness_issues=completeness_issues,
                content_hash=content_hash,
                confidence=analysis.get("confidence", 0.0) if analysis else None,
                changes_needed=changes_needed,
                reasoning=analysis.get("reasoning") if analysis else None,
                status="pending",
                audited_at=datetime.now(UTC).replace(tzinfo=None),
                audit_run_id=run_id,
            )

            if audit_result:
                for key, value in field_values.items():
                    setattr(audit_result, key, value)
            else:
                audit_result = PaperlessAuditResult(
                    paperless_doc_id=doc_id,
                    **field_values,
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

        # Build custom fields schema string
        custom_fields = available_metadata.get("custom_fields", [])
        if custom_fields:
            cf_schema = json.dumps(custom_fields, ensure_ascii=False)
        else:
            cf_schema = "(keine benutzerdefinierten Felder definiert)"

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
                current_date=doc.get("created") or "—",
                storage_path=doc.get("storage_path") or "—",
                available_types=", ".join(available_metadata.get("types", [])) or "—",
                available_correspondents=", ".join(available_metadata.get("correspondents", [])) or "—",
                available_tags=", ".join(available_metadata.get("tags", [])) or "—",
                available_storage_paths=", ".join(available_metadata.get("storage_paths", [])) or "—",
                custom_fields_schema=cf_schema,
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
        if result.suggested_date and result.suggested_date != result.current_date:
            params["created_date"] = result.suggested_date
            has_changes = True
        if result.suggested_storage_path and result.suggested_storage_path != result.current_storage_path:
            params["storage_path"] = result.suggested_storage_path
            has_changes = True
        if result.suggested_custom_fields:
            params["custom_fields"] = result.suggested_custom_fields
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
        missing_field: str | None = None,
        detected_language: str | None = None,
        completeness_max: int | None = None,
        duplicate_group_id: str | None = None,
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
            if missing_field:
                # JSON array contains the field name
                query = query.where(
                    PaperlessAuditResult.missing_fields.isnot(None),
                    func.json_array_length(PaperlessAuditResult.missing_fields) > 0,
                )
            if detected_language:
                query = query.where(PaperlessAuditResult.detected_language == detected_language)
            if completeness_max is not None:
                query = query.where(PaperlessAuditResult.content_completeness <= completeness_max)
            if duplicate_group_id:
                query = query.where(PaperlessAuditResult.duplicate_group_id == duplicate_group_id)

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

            # Missing metadata count
            missing_metadata_count = (await db.execute(
                select(func.count()).select_from(PaperlessAuditResult).where(
                    PaperlessAuditResult.missing_fields.isnot(None),
                )
            )).scalar() or 0

            # Language distribution
            lang_rows = (await db.execute(
                select(
                    PaperlessAuditResult.detected_language,
                    func.count().label("cnt"),
                ).where(
                    PaperlessAuditResult.detected_language.isnot(None)
                ).group_by(PaperlessAuditResult.detected_language)
            )).all()
            language_distribution = {row[0]: row[1] for row in lang_rows}

            # Content completeness distribution
            completeness_dist = {}
            for score in range(1, 6):
                count = (await db.execute(
                    select(func.count()).select_from(PaperlessAuditResult).where(
                        PaperlessAuditResult.content_completeness == score
                    )
                )).scalar() or 0
                completeness_dist[str(score)] = count

            # Duplicate groups count
            duplicate_groups = (await db.execute(
                select(func.count(func.distinct(PaperlessAuditResult.duplicate_group_id))).where(
                    PaperlessAuditResult.duplicate_group_id.isnot(None)
                )
            )).scalar() or 0

            return {
                "total_audited": total,
                "changes_needed": changes_needed,
                "applied": applied,
                "skipped": skipped,
                "pending": pending,
                "failed": failed,
                "avg_confidence": round(avg_confidence, 3) if avg_confidence else None,
                "ocr_quality_distribution": ocr_dist,
                "missing_metadata_count": missing_metadata_count,
                "language_distribution": language_distribution,
                "completeness_distribution": completeness_dist,
                "duplicate_groups": duplicate_groups,
            }

    async def run_duplicate_detection(self) -> dict:
        """Post-audit pass: detect duplicate documents by content hash grouping.

        Groups documents by (correspondent, document_type, date), then compares
        content hashes within groups. Sets duplicate_group_id on matches.
        """
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.content_hash.isnot(None)
            )
            results = (await db.execute(stmt)).scalars().all()

        if not results:
            return {"groups_found": 0, "documents_flagged": 0}

        # Group by (correspondent, document_type, date)
        groups: dict[tuple, list] = {}
        for r in results:
            key = (
                r.current_correspondent or "",
                r.current_document_type or "",
                r.current_date or "",
            )
            groups.setdefault(key, []).append(r)

        groups_found = 0
        documents_flagged = 0

        async with self._db_factory() as db:
            for _key, group in groups.items():
                if len(group) < 2:
                    continue

                # Compare content hashes within the group
                hash_clusters: dict[str, list] = {}
                for r in group:
                    hash_clusters.setdefault(r.content_hash, []).append(r)

                for _content_hash, cluster in hash_clusters.items():
                    if len(cluster) < 2:
                        continue

                    group_id = str(uuid4())[:8]
                    groups_found += 1

                    for r in cluster:
                        stmt = select(PaperlessAuditResult).where(
                            PaperlessAuditResult.id == r.id
                        )
                        db_result = (await db.execute(stmt)).scalar_one_or_none()
                        if db_result:
                            db_result.duplicate_group_id = group_id
                            db_result.duplicate_score = 1.0  # Exact hash match
                            documents_flagged += 1

                # Also check for near-duplicates via SequenceMatcher within group
                for i in range(len(group)):
                    for j in range(i + 1, len(group)):
                        a, b = group[i], group[j]
                        # Skip if already in a duplicate group
                        if a.duplicate_group_id and b.duplicate_group_id:
                            continue
                        if a.content_hash == b.content_hash:
                            continue  # Already handled above

                        # Compare titles as a lightweight proxy
                        title_a = (a.current_title or "").lower()
                        title_b = (b.current_title or "").lower()
                        if title_a and title_b:
                            ratio = SequenceMatcher(None, title_a, title_b).ratio()
                            if ratio >= 0.8:
                                group_id = a.duplicate_group_id or str(uuid4())[:8]
                                groups_found += 1

                                for r in [a, b]:
                                    stmt = select(PaperlessAuditResult).where(
                                        PaperlessAuditResult.id == r.id
                                    )
                                    db_result = (await db.execute(stmt)).scalar_one_or_none()
                                    if db_result and not db_result.duplicate_group_id:
                                        db_result.duplicate_group_id = group_id
                                        db_result.duplicate_score = ratio
                                        documents_flagged += 1

            await db.commit()

        return {"groups_found": groups_found, "documents_flagged": documents_flagged}

    async def run_correspondent_normalization(self, threshold: float = 0.82) -> dict:
        """Post-audit pass: find similar correspondent names that may be duplicates.

        Returns clusters for display only — does not write to DB.
        Pattern from kg_cleanup_service.py.
        """
        available = await self._fetch_available_metadata()
        correspondents = available.get("correspondents", [])
        if not correspondents:
            return {"clusters": [], "total_correspondents": 0}

        # Normalize names for comparison
        _strip_suffixes = re.compile(
            r'\s*(GmbH|AG|SE|e\.V\.|Ltd\.?|Inc\.?|Corp\.?|Co\.?\s*KG|UG|OHG|KGaA)\s*$',
            re.IGNORECASE,
        )

        def normalize(name: str) -> str:
            n = _strip_suffixes.sub("", name).strip()
            return n.lower()

        # Pairwise comparison
        clusters: list[dict] = []
        used = set()

        for i in range(len(correspondents)):
            if i in used:
                continue
            norm_i = normalize(correspondents[i])
            cluster_variants = []

            for j in range(i + 1, len(correspondents)):
                if j in used:
                    continue
                norm_j = normalize(correspondents[j])
                ratio = SequenceMatcher(None, norm_i, norm_j).ratio()
                if ratio >= threshold:
                    cluster_variants.append({
                        "name": correspondents[j],
                        "similarity": round(ratio, 3),
                    })
                    used.add(j)

            if cluster_variants:
                used.add(i)
                clusters.append({
                    "canonical": correspondents[i],
                    "variants": cluster_variants,
                })

        return {
            "clusters": clusters,
            "total_correspondents": len(correspondents),
        }

    async def get_duplicate_groups(self) -> list[dict]:
        """Get all duplicate groups with their documents."""
        from models.database import PaperlessAuditResult

        async with self._db_factory() as db:
            stmt = select(PaperlessAuditResult).where(
                PaperlessAuditResult.duplicate_group_id.isnot(None)
            ).order_by(PaperlessAuditResult.duplicate_group_id)
            results = (await db.execute(stmt)).scalars().all()

        groups: dict[str, list] = {}
        for r in results:
            groups.setdefault(r.duplicate_group_id, []).append(self._result_to_dict(r))

        return [
            {"group_id": gid, "documents": docs}
            for gid, docs in groups.items()
        ]

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
    def _check_missing_fields(doc: dict) -> list[str]:
        """Check which metadata fields are missing on the document."""
        missing = []
        if not doc.get("correspondent"):
            missing.append("correspondent")
        if not doc.get("document_type"):
            missing.append("document_type")
        if not doc.get("tags"):
            missing.append("tags")
        if not doc.get("storage_path"):
            missing.append("storage_path")
        return missing

    @staticmethod
    def _check_content_completeness(content: str, page_count: int | None) -> tuple[int, str]:
        """Rate content completeness 1-5 based on heuristics.

        Checks chars-per-page ratio, page marker gaps, and content length.
        """
        if not content or len(content.strip()) < 10:
            return 1, "No/minimal content"

        issues = []

        # Check chars per page
        if page_count and page_count > 0:
            chars_per_page = len(content) / page_count
            if chars_per_page < 100:
                issues.append(f"Very low content per page ({int(chars_per_page)} chars/page)")
            elif chars_per_page < 300:
                issues.append(f"Low content per page ({int(chars_per_page)} chars/page)")

        # Check for very short content on multi-page docs
        if page_count and page_count > 1 and len(content) < 50:
            issues.append("Likely incomplete extraction")

        # Check for page marker gaps (e.g. "Seite 1" then "Seite 3" — missing page 2)
        page_markers = re.findall(r'(?:Seite|Page|S\.)\s*(\d+)', content, re.IGNORECASE)
        if len(page_markers) >= 2:
            pages = sorted(set(int(p) for p in page_markers))
            for i in range(1, len(pages)):
                if pages[i] - pages[i - 1] > 1:
                    issues.append(f"Missing pages between {pages[i - 1]} and {pages[i]}")
                    break

        score = max(1, 5 - len(issues))
        return score, "; ".join(issues) or "OK"

    @staticmethod
    def _parse_mcp_result(result: dict) -> dict | None:
        """Parse the inner JSON from MCP tool response.

        Handles truncated responses from MCPManager's _truncate_response(),
        which appends a text suffix like '[... Showing N of M results]'
        after the JSON, causing 'Extra data' errors.
        """
        message = result.get("message", "")
        if not message:
            return None

        try:
            return json.loads(message)
        except json.JSONDecodeError as e:
            # Truncated response: parse up to the error position
            if e.pos and e.pos > 1:
                try:
                    return json.loads(message[:e.pos])
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.warning(f"_parse_mcp_result: JSON parse failed: {e}")
            return None
        except TypeError:
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
            "current_date": r.current_date,
            "current_storage_path": r.current_storage_path,
            "current_custom_fields": r.current_custom_fields,
            "suggested_title": r.suggested_title,
            "suggested_correspondent": r.suggested_correspondent,
            "suggested_document_type": r.suggested_document_type,
            "suggested_tags": r.suggested_tags,
            "suggested_date": r.suggested_date,
            "suggested_storage_path": r.suggested_storage_path,
            "suggested_custom_fields": r.suggested_custom_fields,
            "detected_language": r.detected_language,
            "missing_fields": r.missing_fields,
            "duplicate_group_id": r.duplicate_group_id,
            "duplicate_score": r.duplicate_score,
            "content_completeness": r.content_completeness,
            "completeness_issues": r.completeness_issues,
            "content_hash": r.content_hash,
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
