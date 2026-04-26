"""
RAG Evaluation Service — LLM-as-Judge quality metrics.

Evaluates RAG pipeline quality across three dimensions:
- Context Relevance: are retrieved chunks relevant to the query?
- Faithfulness: does the answer stick to the retrieved context?
- Answer Quality: does the answer address the question?

Usage:
    eval_svc = RAGEvalService(db_session)
    results = await eval_svc.evaluate(test_cases)
"""
import asyncio
import logging
import yaml
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from services.rag_service import RAGService
from utils.config import settings
from utils.llm_client import get_default_client

logger = logging.getLogger(__name__)

SCORING_PROMPT_DE = """Bewerte auf einer Skala von 0 bis 10.
Antworte NUR mit einer Zahl (0-10), nichts anderes.

{criterion}

{content}

Bewertung (0-10):"""

CRITERIA = {
    "relevance": "Wie relevant ist der folgende Kontext für die Beantwortung der Frage?\n\nFrage: {query}\n\nKontext:\n{context}",
    "faithfulness": "Basiert die folgende Antwort ausschließlich auf dem gegebenen Kontext? 10 = komplett kontextbasiert, 0 = frei erfunden.\n\nKontext:\n{context}\n\nAntwort:\n{answer}",
    "answer_quality": "Beantwortet die folgende Antwort die gestellte Frage vollständig und korrekt?\n\nFrage: {query}\n\nAntwort:\n{answer}",
}


class RAGEvalService:
    """Evaluates RAG quality using LLM-as-Judge scoring."""

    def __init__(self, db: AsyncSession):
        self.db = db
        self.rag = RAGService(db)

    async def evaluate(self, test_cases: list[dict[str, Any]]) -> dict[str, Any]:
        """Run evaluation on a list of test cases.

        Each test case: {"query": str, "expected_source": str|None, "expected_answer_contains": list[str]|None}

        Returns: {"scores": {...}, "per_case": [...], "summary": str}
        """
        results = []
        for i, tc in enumerate(test_cases):
            logger.info(f"RAG Eval: case {i + 1}/{len(test_cases)}: {tc['query'][:50]}")
            case_result = await self._evaluate_single(tc)
            results.append(case_result)

        # Aggregate scores
        all_relevance = [r["scores"]["relevance"] for r in results if r["scores"]["relevance"] is not None]
        all_faithfulness = [r["scores"]["faithfulness"] for r in results if r["scores"]["faithfulness"] is not None]
        all_quality = [r["scores"]["answer_quality"] for r in results if r["scores"]["answer_quality"] is not None]
        all_source = [r["scores"]["source_accuracy"] for r in results if r["scores"]["source_accuracy"] is not None]

        summary = {
            "timestamp": datetime.now(UTC).isoformat(),
            "total_cases": len(test_cases),
            "scores": {
                "context_relevance": round(sum(all_relevance) / len(all_relevance), 2) if all_relevance else None,
                "faithfulness": round(sum(all_faithfulness) / len(all_faithfulness), 2) if all_faithfulness else None,
                "answer_quality": round(sum(all_quality) / len(all_quality), 2) if all_quality else None,
                "source_accuracy": round(sum(all_source) / len(all_source), 2) if all_source else None,
            },
            "per_case": results,
        }

        logger.info(
            f"RAG Eval complete: relevance={summary['scores']['context_relevance']}, "
            f"faithfulness={summary['scores']['faithfulness']}, "
            f"quality={summary['scores']['answer_quality']}, "
            f"source_accuracy={summary['scores']['source_accuracy']}"
        )
        return summary

    async def _evaluate_single(self, test_case: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a single query through the full RAG pipeline."""
        query = test_case["query"]
        expected_source = test_case.get("expected_source")
        expected_contains = test_case.get("expected_answer_contains", [])

        # Step 1: RAG search
        search_results = await self.rag.search(query, top_k=5)
        context = self.rag.format_context_from_results(search_results)

        # Step 2: Generate answer
        answer = await self._generate_answer(query, context)

        # Step 3: Score with LLM-as-judge
        relevance = await self._score("relevance", query=query, context=context)
        faithfulness = await self._score("faithfulness", context=context, answer=answer)
        quality = await self._score("answer_quality", query=query, answer=answer)

        # Step 4: Check source accuracy
        source_accuracy = None
        if expected_source and search_results:
            source_hit = any(expected_source in r["document"]["filename"] for r in search_results)
            source_accuracy = 10.0 if source_hit else 0.0

        # Step 5: Check expected content
        contains_hits = sum(1 for kw in expected_contains if kw.lower() in answer.lower()) if expected_contains else None
        contains_score = (contains_hits / len(expected_contains) * 10) if expected_contains else None

        return {
            "query": query,
            "answer": answer[:500],
            "retrieved_sources": [r["document"]["filename"] for r in search_results],
            "scores": {
                "relevance": relevance,
                "faithfulness": faithfulness,
                "answer_quality": quality,
                "source_accuracy": source_accuracy,
                "contains_score": contains_score,
            },
        }

    async def _generate_answer(self, query: str, context: str) -> str:
        """Generate an answer using the RAG prompt + context."""
        try:
            client = get_default_client()
            prompt = f"Kontext:\n{context}\n\nFrage: {query}\n\nAntwort:"
            response = await asyncio.wait_for(
                client.generate(
                    model=settings.ollama_chat_model,
                    prompt=prompt,
                    options={"temperature": 0.3, "num_predict": 500},
                ),
                timeout=settings.rag_eval_answer_timeout,
            )
            return response.response.strip()
        except Exception as e:
            logger.error(f"Answer generation failed: {e}")
            return f"[ERROR: {e}]"

    async def _score(self, criterion: str, **kwargs) -> float | None:
        """Score a dimension using LLM-as-judge. Returns 0-10 or None on failure."""
        try:
            template = CRITERIA[criterion]
            content = template.format(**kwargs)
            prompt = SCORING_PROMPT_DE.format(criterion="", content=content)

            client = get_default_client()
            response = await asyncio.wait_for(
                client.generate(
                    model=settings.ollama_chat_model,
                    prompt=prompt,
                    options={"temperature": 0.0, "num_predict": 5},
                ),
                timeout=settings.rag_eval_score_timeout,
            )
            # Extract number from response
            text = response.response.strip()
            for token in text.split():
                try:
                    score = float(token)
                    return min(max(score, 0), 10)
                except ValueError:
                    continue
            return None
        except Exception as e:
            logger.warning(f"Scoring failed for {criterion}: {e}")
            return None

    @staticmethod
    def load_test_cases(path: str | None = None) -> list[dict[str, Any]]:
        """Load test cases from YAML file."""
        if path is None:
            path = "data/rag-eval/test_cases.yaml"
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Test cases file not found: {path}")
        with open(p) as f:
            data = yaml.safe_load(f)
        return data.get("test_cases", [])
