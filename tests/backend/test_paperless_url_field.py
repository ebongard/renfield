"""`paperless_url` kommt wirklich beim Frontend an — durch das Response-Modell.

🛑 Der Grund, warum diese Tests NICHT den Serialisierer aufrufen, sondern
``DocumentResponse`` bauen: Pydantic verwirft unbekannte Schluessel beim Bauen
eines Modells STILL. Ein Test auf ``_doc_to_response_kwargs`` allein waere gruen
geblieben, waehrend das Feld nie ausgeliefert wird und nichts es meldet. Im
Review zu diesem Plan als Befund C1 gefunden, bevor er gebaut wurde — der Plan
nannte nur den Serialisierer.

Die Negativkontrolle dazu steht unten: sie beweist, dass dieser Test den Fehler
auch wirklich faengt.
"""
from types import SimpleNamespace

import pytest

from api.routes.knowledge import _doc_to_response_kwargs, paperless_document_url
from api.routes.knowledge_schemas import DocumentResponse

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _doc(**over):
    felder = dict(
        id=7, filename="rechnung.pdf", title=None, generated_title=None,
        file_type="pdf", file_size=1234, status="completed", error_message=None,
        chunk_count=3, page_count=2, knowledge_base_id=1, created_at=None,
        processed_at=None, document_date=None, paperless_document_id=None,
        paperless_state=None, circle_tier=0, atom_id=None,
    )
    felder.update(over)
    return SimpleNamespace(**felder)


@pytest.mark.parametrize("base,doc_id,erwartet", [
    ("http://paperless.local:8000", 42, "http://paperless.local:8000/documents/42/details"),
    # Abschliessender Schraegstrich darf kein `//documents/` erzeugen.
    ("http://paperless.local:8000/", 42, "http://paperless.local:8000/documents/42/details"),
    ("http://paperless.local:8000", None, None),
    ("", 42, None),          # keine Basis konfiguriert → kein Link
    ("http://p", True, None),  # `bool` ist ein `int` und doch keine Kennung
])
def test_the_url_is_composed_in_one_place(monkeypatch, base, doc_id, erwartet):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", base, raising=False)
    assert paperless_document_url(doc_id) == erwartet


def test_the_field_SURVIVES_the_response_model(monkeypatch):
    """🛑 Der eigentliche Test. Durch `DocumentResponse`, nicht am Serialisierer."""
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    kwargs = _doc_to_response_kwargs(_doc(paperless_document_id=42, paperless_state="done"))
    antwort = DocumentResponse(**kwargs)
    assert antwort.paperless_url == "http://paperless.local:8000/documents/42/details", (
        "das Feld hat das Response-Modell nicht ueberlebt — genau der stille "
        "Fehlschlag, gegen den dieser Test steht")
    assert antwort.in_paperless is True


def test_a_duplicate_has_no_url_but_counts_as_filed(monkeypatch):
    """Der Fall, der rund jeden sechsten Scan trifft: in Paperless, aber ohne
    eigene Kennung. Daran erkennt der Knopf die Dublette."""
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    antwort = DocumentResponse(**_doc_to_response_kwargs(
        _doc(paperless_document_id=None, paperless_state="done")))
    assert antwort.paperless_url is None
    assert antwort.in_paperless is True


def test_still_ingesting_is_neither_filed_nor_linked(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    antwort = DocumentResponse(**_doc_to_response_kwargs(
        _doc(status="processing", paperless_document_id=None, paperless_state=None)))
    assert antwort.paperless_url is None
    assert antwort.in_paperless is False


def test_NEGATIVE_CONTROL_the_model_really_drops_unknown_keys():
    """🛑 Beweist, dass der Test oben etwas faengt.

    Ohne diesen Nachweis koennte `test_the_field_SURVIVES_the_response_model`
    auch dann gruen sein, wenn Pydantic unbekannte Schluessel durchreichte — und
    die Behauptung „ohne Deklaration faellt es still weg" waere unbelegt.
    """
    antwort = DocumentResponse(**{
        **_doc_to_response_kwargs(_doc()),
        "gibtesnicht": "wert",
    })
    assert not hasattr(antwort, "gibtesnicht"), (
        "Pydantic reicht unbekannte Schluessel durch — dann ist die Begruendung "
        "fuer das deklarierte Feld hinfaellig und C1 muss neu bewertet werden")
