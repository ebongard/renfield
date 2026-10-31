"""The button to the document hangs off the completion notice -- and the TEXT stays the same.

The occasion (2026-10-27): "wenn ich scanne, dann moechte ich danach ein Link
zu dem Paperless Document in der Antwort haben, nicht nur die Nummer."

🛑 The two tests that carry more here than the others:

- **The negative control on the unchanged notice text.**
  ``render_completion_message`` is a trust boundary: its text is REPLAYED into
  later agent turns. Had anybody written the link into the sentence, that would
  be a channel for free text into the next prompt. Without this test the
  boundary softens unnoticed at the next rework.

- **The status set.** ``scanner_jobs`` now keeps FOUR sets over the same
  states. The second one was once not brought along and forgot two states
  (2026-10-04, a real `unrouted` job reported `error_code=<fehlt>`). The
  structural test holds that the attachment branches over the shared set and
  not over a second, copied literal.
"""
import inspect

import pytest

from services.scanner_jobs import (
    _STATES_WITH_DOCUMENT,
    attach_document_ref,
    render_completion_message,
)

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_DONE = {"renfield_document_id": 515, "pages": 3, "target": "household"}


def _meta() -> dict:
    return {"scanner_job": {"job_id": "scan-20261027-120000-abcdef12", "status": "done"}}


def test_a_finished_scan_carries_a_document_ref_artifact(monkeypatch):
    # The self identity is part of the case: `_DONE` scans to `household`,
    # and only with `instance_id` set is that PROVABLY this instance. Without
    # it the fail-closed rule below applies -- see
    # `test_WITHOUT_a_self_identity_every_named_target_is_remote`.
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    arts = meta.get("artifacts")
    assert arts, f"no artifact attached: {meta}"
    assert len(arts) == 1, arts
    assert arts[0]["kind"] == "document_ref"
    assert arts[0]["data"] == {"documentId": 515}


def test_the_message_TEXT_is_byte_for_byte_unchanged():
    """🛑 The trust boundary. The link lives in the metadata, NEVER in the sentence."""
    text = render_completion_message("done", "Rechnung", _DONE, "de")
    assert "http" not in text, f"a URL in the message text:\n{text}"
    assert "In Paperless" not in text, text
    # And the attachment does not change it after the fact either.
    meta = _meta()
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    assert render_completion_message("done", "Rechnung", _DONE, "de") == text


def test_the_scanner_job_key_survives_the_merge():
    """`merge_artifacts_into_metadata` must not touch the existing keys."""
    meta = _meta()
    meta["action_success"] = True
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    assert meta["scanner_job"]["job_id"] == "scan-20261027-120000-abcdef12"
    assert meta["action_success"] is True


@pytest.mark.parametrize("status", ["unrouted", "interrupted", "failed", "gibtesnicht"])
def test_only_states_with_a_document_get_a_button(status):
    """🛑 `unrouted` is the most important case: the scan ran, but the target
    is still pending -- there is no filed document to open."""
    meta = _meta()
    attach_document_ref(meta, status, _DONE, "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, f"{status} got a button: {meta}"


@pytest.mark.parametrize("doc_id", [None, "515", True, 0, -1, 3.0])
def test_an_untrusted_document_id_is_refused(doc_id):
    """The event is untrusted. In Python `True` is an `int` -- without the
    explicit exclusion it would be a valid id."""
    meta = _meta()
    attach_document_ref(meta, "done", {"renfield_document_id": doc_id},
                        "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, f"{doc_id!r} was accepted: {meta}"


def test_a_split_scan_gets_no_button():
    """🛑 `result["documents"]` is `list[Any]` from the event. Better no button
    than ids guessed out of free text."""
    meta = _meta()
    attach_document_ref(meta, "done", {"documents": [{"id": 1}, {"id": 2}]},
                        "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, meta


def test_a_second_delivery_replaces_instead_of_duplicating():
    """The artifact id is stable per job; `merge` keys on `id`."""
    meta = _meta()
    job = "scan-20261027-120000-abcdef12"
    attach_document_ref(meta, "done", _DONE, job)
    attach_document_ref(meta, "done", _DONE, job)
    assert len(meta["artifacts"]) == 1, meta["artifacts"]


def test_the_attachment_branches_over_the_SHARED_state_set():
    """🛑 Structural test against the drift this file has already had once.

    A copied `status == "done"` would be right today and silently wrong at the
    next new state -- exactly the defect of 2026-10-04.
    """
    quelle = inspect.getsource(attach_document_ref)
    assert "_STATES_WITH_DOCUMENT" in quelle, (
        "the attachment does not branch on the shared set:\n" + quelle)
    assert '== "done"' not in quelle, (
        "a second literal next to the set — exactly the drift:\n" + quelle)
    assert "unrouted" not in _STATES_WITH_DOCUMENT, (
        "`unrouted` has no filed document")


# --- Instance boundary -------------------------------------------------------
#
# 🛑 The defect the owner found while reading: the scanner sends the completion
# notice to the instance that ASKED -- but the document can live in a different
# one. The id then belongs to that other instance, and because both keep their
# own sequences it exists here too, for a different document. A button on it
# does not lead nowhere but somewhere WRONG.


def _mit_ziel(ziel):
    return {**_DONE, "target": ziel}


def test_a_scan_into_THIS_instance_stays_linkable(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("household"), "scan-20261027-120000-abcdef12")
    assert "instance" not in meta["artifacts"][0]["data"], (
        "a local document needs no instance — otherwise the "
        "component does not even ask")


def test_a_scan_into_ANOTHER_instance_is_marked_remote(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("xidra"), "scan-20261027-120000-abcdef12")
    assert meta["artifacts"][0]["data"]["instance"] == "xidra", (
        "without the marker the component would resolve xidra's id LOCALLY and "
        "hit a foreign document with the same number")


def test_the_comparison_ignores_case_and_spacing(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "  Household ", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("HOUSEHOLD"), "scan-20261027-120000-abcdef12")
    assert "instance" not in meta["artifacts"][0]["data"]


def test_WITHOUT_a_self_identity_every_named_target_is_remote(monkeypatch):
    """🛑 Fail-closed. An unconfigured instance never links wrongly -- it does
    not link at all.

    The mechanism is the inequality itself, not a branch of its own: a
    non-empty target is unequal to the empty self identity. Add a second guard
    here and you add an ineffective one -- this test would pass with and
    without it.
    """
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("household"), "scan-20261027-120000-abcdef12")
    assert meta["artifacts"][0]["data"]["instance"] == "household", (
        "without a self id EVERY named target must count as foreign")


def test_an_unnamed_target_stays_local(monkeypatch):
    """With no target in the event there is nothing to compare -- `n=1`
    short-circuits the whole routing layer, and there the scan is always
    here."""
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    for ziel in (None, "", "   "):
        meta = _meta()
        attach_document_ref(meta, "done", {**_DONE, "target": ziel},
                            "scan-20261027-120000-abcdef12")
        assert "instance" not in meta["artifacts"][0]["data"], f"target={ziel!r}"

