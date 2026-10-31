"""`paperless_url` really reaches the frontend -- through the response model.

🛑 The reason these tests do NOT call the serializer but build a
``DocumentResponse``: Pydantic drops unknown keys SILENTLY when building a
model. A test on ``_doc_to_response_kwargs`` alone would have stayed green
while the field is never delivered and nothing reports it. Found as finding C1
in the review of this plan, before it was built -- the plan named only the
serializer.

The negative control for that stands below: it proves that this test really
does catch the defect.
"""
from types import SimpleNamespace

import pytest

from api.routes.knowledge import _doc_to_response_kwargs, paperless_document_url
from api.routes.knowledge_schemas import DocumentResponse

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _doc(**over):
    fields = dict(
        id=7, filename="rechnung.pdf", title=None, generated_title=None,
        file_type="pdf", file_size=1234, status="completed", error_message=None,
        chunk_count=3, page_count=2, knowledge_base_id=1, created_at=None,
        processed_at=None, document_date=None, paperless_document_id=None,
        paperless_state=None, circle_tier=0, atom_id=None,
    )
    fields.update(over)
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("base,doc_id,expected", [
    ("http://paperless.local:8000", 42, "http://paperless.local:8000/documents/42/details"),
    # A trailing slash must not produce `//documents/`.
    ("http://paperless.local:8000/", 42, "http://paperless.local:8000/documents/42/details"),
    ("http://paperless.local:8000", None, None),
    ("", 42, None),          # no base configured -> no link
    ("http://p", True, None),  # `bool` is an `int` and still not an id
])
def test_the_url_is_composed_in_one_place(monkeypatch, base, doc_id, expected):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", base, raising=False)
    assert paperless_document_url(doc_id) == expected


def test_the_field_SURVIVES_the_response_model(monkeypatch):
    """🛑 The actual test. Through `DocumentResponse`, not on the serializer."""
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    kwargs = _doc_to_response_kwargs(_doc(paperless_document_id=42, paperless_state="done"))
    response = DocumentResponse(**kwargs)
    assert response.paperless_url == "http://paperless.local:8000/documents/42/details", (
        "the field did not survive the response model — exactly the silent "
        "failure this test guards against")
    assert response.in_paperless is True


def test_a_duplicate_has_no_url_but_counts_as_filed(monkeypatch):
    """The case that hits roughly every sixth scan: in Paperless, but without
    an id of its own. That is how the button recognises the duplicate."""
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    response = DocumentResponse(**_doc_to_response_kwargs(
        _doc(paperless_document_id=None, paperless_state="done")))
    assert response.paperless_url is None
    assert response.in_paperless is True


def test_still_ingesting_is_neither_filed_nor_linked(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless.local:8000",
                        raising=False)
    response = DocumentResponse(**_doc_to_response_kwargs(
        _doc(status="processing", paperless_document_id=None, paperless_state=None)))
    assert response.paperless_url is None
    assert response.in_paperless is False


def test_NEGATIVE_CONTROL_the_model_really_drops_unknown_keys():
    """🛑 Proves that the test above catches something.

    Without this evidence `test_the_field_SURVIVES_the_response_model` could
    also be green if Pydantic passed unknown keys through -- and the claim
    "without a declaration it falls away silently" would be unevidenced.
    """
    response = DocumentResponse(**{
        **_doc_to_response_kwargs(_doc()),
        "gibtesnicht": "wert",
    })
    assert not hasattr(response, "gibtesnicht"), (
        "Pydantic passes unknown keys through — then the reason for the "
        "declared field no longer holds and C1 has to be re-assessed")
