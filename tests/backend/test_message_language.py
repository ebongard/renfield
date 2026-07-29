"""Per-turn chat language detection (web-chat answers in the user's language).

The web agent used to run every turn under ``ollama.default_lang``, so an
English question got the German system prompt and a German answer. These tests
lock the heuristic: German chars/stopwords → "de", a clear English signal →
"en", otherwise the configured default (so a German-default deployment never
regresses on an ambiguous message).
"""
import pytest

from api.websocket.chat_handler import detect_message_language

pytestmark = pytest.mark.backend


@pytest.mark.parametrize("text", [
    "Show me all active releases",
    "Which freeze windows are active in January 2027?",
    "Are there any dependency conflicts across the programme?",
    "What depends on Major Release R27.4? Show me the applications and Jira tickets.",
    "I want to deploy OrderMgmt to production on 10 January — when is the earliest safe date?",
    "Where can we improve freeze governance? Back it up with KPIs.",
    "Are there any approved freeze exceptions?",
])
def test_english_questions_detect_en(text):
    # default is German; a confident English signal must still flip to en.
    assert detect_message_language(text, "de") == "en"


@pytest.mark.parametrize("text", [
    "Zeige mir alle aktiven Releases",
    "Welche Freeze-Fenster sind im Januar 2027 aktiv?",
    "Gibt es Abhängigkeitskonflikte im Programm?",
    "Wo haben wir bei der Freeze-Governance Verbesserungspotenzial?",
    "Ich möchte OrderMgmt nach Prod deployen",  # umlaut ö
    "Was hängt alles am Major Release R27.4?",   # umlaut ä
])
def test_german_questions_detect_de(text):
    assert detect_message_language(text, "de") == "de"


def test_ambiguous_falls_back_to_default():
    # No German or English signal → keep the deployment default (no regression).
    assert detect_message_language("R27.4?", "de") == "de"
    assert detect_message_language("R27.4?", "en") == "en"
    assert detect_message_language("", "de") == "de"
