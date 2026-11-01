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
import ast
import inspect
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
    """Without `PAPERLESS_PUBLIC_URL` the API URL is the link base -- the
    behaviour before the split, unchanged."""
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_public_url", None, raising=False)
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


# ---------------------------------------------------------------------------
# PAPERLESS_PUBLIC_URL: the browser-facing link base, split from the API URL
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("public,api,expected", [
    # Set -> the public URL wins over the (container/LAN) API URL.
    ("https://paperless.example.org", "http://paperless:8000",
     "https://paperless.example.org/documents/42/details"),
    # Trailing slash on the public URL must not produce `//documents/`.
    ("https://paperless.example.org/", "http://paperless:8000",
     "https://paperless.example.org/documents/42/details"),
    # A path prefix (reverse proxy) is kept.
    ("https://example.org/paperless/", "http://paperless:8000",
     "https://example.org/paperless/documents/42/details"),
    # Unset or empty -> fall back to the API URL.
    (None, "http://paperless:8000", "http://paperless:8000/documents/42/details"),
    ("", "http://paperless:8000/", "http://paperless:8000/documents/42/details"),
    # Whitespace-only counts as unset, not as a base of "   ".
    ("   ", "http://paperless:8000", "http://paperless:8000/documents/42/details"),
    # Public set, API unset -> still a link (the link does not need the API).
    ("https://paperless.example.org", None,
     "https://paperless.example.org/documents/42/details"),
    # Both unset -> no link.
    (None, None, None),
    ("", "", None),
])
def test_the_public_url_is_the_link_base(monkeypatch, public, api, expected):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_public_url", public, raising=False)
    monkeypatch.setattr(settings, "paperless_api_url", api, raising=False)
    assert paperless_document_url(42) == expected


def test_the_public_url_reaches_the_response_model(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "paperless_public_url", "https://paperless.example.org",
                        raising=False)
    monkeypatch.setattr(settings, "paperless_api_url", "http://paperless:8000", raising=False)
    response = DocumentResponse(**_doc_to_response_kwargs(
        _doc(paperless_document_id=42, paperless_state="done")))
    assert response.paperless_url == "https://paperless.example.org/documents/42/details"


def test_the_env_var_is_read_into_the_setting(monkeypatch):
    """`PAPERLESS_PUBLIC_URL` really is the env name of the field."""
    from utils.config import Settings
    monkeypatch.setenv("PAPERLESS_PUBLIC_URL", "https://paperless.example.org")
    monkeypatch.setenv("PAPERLESS_API_URL", "http://paperless:8000")
    s = Settings()
    assert s.paperless_public_url == "https://paperless.example.org"
    assert s.paperless_api_url == "http://paperless:8000"


def test_both_unset_by_default(monkeypatch):
    """Unset env -> both None: the readers' `or ""` treats that as 'not
    configured' (no link, no checksum lookup)."""
    from utils.config import Settings
    monkeypatch.delenv("PAPERLESS_PUBLIC_URL", raising=False)
    monkeypatch.delenv("PAPERLESS_API_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.paperless_public_url is None
    assert s.paperless_api_url is None


def _duplicate_fields(class_source: str) -> list[str]:
    """Fields annotated more than once in a class BODY -- measured on the
    AST, so no docstring or comment can satisfy or trip it."""
    tree = ast.parse(class_source)
    class_def = next(n for n in tree.body if isinstance(n, ast.ClassDef))
    names = [
        n.target.id for n in class_def.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    ]
    return sorted({n for n in names if names.count(n) > 1})


def test_settings_declares_no_field_twice():
    """🛑 Pydantic lets a second declaration of the same field silently win
    over the first -- that is how `paperless_api_url: str = ""` was dead
    code behind `paperless_api_url: str | None = None` (2026-10-31).
    Covers EVERY field, not only the one that was found."""
    from utils.config import Settings
    assert _duplicate_fields(inspect.getsource(Settings)) == [], (
        "fields declared more than once in Settings")


def test_NEGATIVE_CONTROL_the_duplicate_check_sees_a_duplicate():
    """Proves the AST walk above catches a real duplicate (the old shape)."""
    source = (
        "class S:\n"
        "    paperless_api_url: str = ''\n"
        "    other: int = 0\n"
        "    paperless_api_url: str | None = None\n"
    )
    assert _duplicate_fields(source) == ["paperless_api_url"]
