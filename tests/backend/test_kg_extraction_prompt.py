"""
Regression tests for the KG extraction prompt hardening clauses.

The 2026-05-26 incident in production produced 11 garbage relations from two
adjacent chat sessions: self-loops (Anna ist_verheiratet_mit Anna),
training-data hallucinations (Hans Filbinger), and predicate/object-type
mismatches (Anna heißt_auch Kleinenbroich — Kleinenbroich is a place, not a
name). The prompt was tightened with four ground rules + a worked example.

These tests lock the clauses in. They do not exercise the LLM — they assert
that the rendered prompt contains the guidance the model needs to see.
"""
import pytest


class TestKGExtractionPromptHardening:
    """The extraction prompt must carry the anti-hallucination rules."""

    @pytest.fixture(autouse=True)
    def reload_prompts(self):
        from services.prompt_manager import prompt_manager
        prompt_manager.reload()

    @pytest.mark.unit
    def test_german_prompt_carries_ground_rules(self):
        from services.prompt_manager import prompt_manager

        rendered = prompt_manager.get(
            "knowledge_graph", "extraction_prompt", lang="de",
            user_message="Eduard ist verheiratet mit Jutta.",
            assistant_response="Notiert.",
        )

        assert "GRUNDREGELN" in rendered
        # Rule 1 — verbatim grounding
        assert "woertlich im Dialog" in rendered
        # Rule 2 — no self-loops
        assert "Self-Loop" in rendered or "Subject und Object dieselbe Entitaet" in rendered
        # Rule 3 — no world-knowledge hallucination
        assert "Weltwissen" in rendered
        # Rule 4 — pronoun discipline
        assert "Pronomen" in rendered
        # Predicate/object-type contract
        assert "Praedikat-Objekt-Typ" in rendered
        # Worked rejection example mentions the specific failure shapes
        assert "Hans Filbinger" in rendered  # hallucination example
        # Original template hooks still present
        assert "Eduard ist verheiratet mit Jutta." in rendered
        assert "Notiert." in rendered

    @pytest.mark.unit
    def test_english_prompt_carries_ground_rules(self):
        from services.prompt_manager import prompt_manager

        rendered = prompt_manager.get(
            "knowledge_graph", "extraction_prompt", lang="en",
            user_message="Eduard is married to Jutta.",
            assistant_response="Noted.",
        )

        assert "GROUND RULES" in rendered
        # Rule 1
        assert "verbatim in the dialog" in rendered
        # Rule 2
        assert "self-loop" in rendered or "subject and object are the same" in rendered
        # Rule 3
        assert "world knowledge" in rendered
        # Rule 4
        assert "Pronouns" in rendered or "pronouns" in rendered
        # Type contract
        assert "Predicate/object-type" in rendered or "predicate/object" in rendered.lower()
        # Worked example
        assert "Hans Filbinger" in rendered
        # Template hooks
        assert "Eduard is married to Jutta." in rendered
        assert "Noted." in rendered

    @pytest.mark.unit
    def test_system_prompts_reject_invention(self):
        """The system message in both languages must forbid fact invention."""
        from services.prompt_manager import prompt_manager

        de_system = prompt_manager.get(
            "knowledge_graph", "extraction_system", lang="de"
        )
        en_system = prompt_manager.get(
            "knowledge_graph", "extraction_system", lang="en"
        )

        assert "erfindest niemals" in de_system
        assert "never invent" in en_system
