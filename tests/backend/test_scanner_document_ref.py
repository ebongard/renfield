"""Der Knopf zum Dokument haengt an der Fertigmeldung — und der TEXT bleibt gleich.

Anlass (2026-10-27): „wenn ich scanne, dann moechte ich danach ein Link zu dem
Paperless Document in der Antwort haben, nicht nur die Nummer."

🛑 Die zwei Tests, die hier mehr tragen als die anderen:

- **Die Negativkontrolle auf den unveraenderten Meldungstext.**
  ``render_completion_message`` ist eine Vertrauensgrenze: ihr Text wird in
  spaeteren Agentenzuegen WIEDER EINGESPIELT. Haette jemand den Link in den Satz
  geschrieben, waere das ein Kanal fuer Freitext in den naechsten Prompt. Ohne
  diesen Test weicht die Grenze beim naechsten Umbau unbemerkt auf.

- **Die Statusmenge.** ``scanner_jobs`` fuehrt jetzt VIER Mengen ueber dieselben
  Zustaende. Die zweite war einmal nicht hochgezogen und vergass zwei Zustaende
  (2026-10-04, ein echter `unrouted`-Auftrag meldete `error_code=<fehlt>`). Der
  Strukturtest haelt fest, dass der Anhang ueber die geteilte Menge verzweigt
  und nicht ueber ein zweites, abgeschriebenes Literal.
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
    # Die Selbstkennung gehoert zum Fall: `_DONE` scannt nach `household`, und
    # erst mit gesetzter `instance_id` ist das NACHWEISLICH die eigene Instanz.
    # Ohne sie greift die Fail-closed-Regel unten — siehe
    # `test_WITHOUT_a_self_identity_every_named_target_is_remote`.
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    arts = meta.get("artifacts")
    assert arts, f"kein Artefakt angehaengt: {meta}"
    assert len(arts) == 1, arts
    assert arts[0]["kind"] == "document_ref"
    assert arts[0]["data"] == {"documentId": 515}


def test_the_message_TEXT_is_byte_for_byte_unchanged():
    """🛑 Die Vertrauensgrenze. Der Link lebt in den Metadaten, NIE im Satz."""
    text = render_completion_message("done", "Rechnung", _DONE, "de")
    assert "http" not in text, f"eine URL im Meldungstext:\n{text}"
    assert "In Paperless" not in text, text
    # Und der Anhang veraendert ihn auch nicht nachtraeglich.
    meta = _meta()
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    assert render_completion_message("done", "Rechnung", _DONE, "de") == text


def test_the_scanner_job_key_survives_the_merge():
    """`merge_artifacts_into_metadata` darf die bestehenden Schluessel nicht fassen."""
    meta = _meta()
    meta["action_success"] = True
    attach_document_ref(meta, "done", _DONE, "scan-20261027-120000-abcdef12")
    assert meta["scanner_job"]["job_id"] == "scan-20261027-120000-abcdef12"
    assert meta["action_success"] is True


@pytest.mark.parametrize("status", ["unrouted", "interrupted", "failed", "gibtesnicht"])
def test_only_states_with_a_document_get_a_button(status):
    """🛑 `unrouted` ist der wichtigste Fall: der Scan lief, aber das Ziel steht
    aus — es gibt kein abgelegtes Dokument zu oeffnen."""
    meta = _meta()
    attach_document_ref(meta, status, _DONE, "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, f"{status} bekam einen Knopf: {meta}"


@pytest.mark.parametrize("doc_id", [None, "515", True, 0, -1, 3.0])
def test_an_untrusted_document_id_is_refused(doc_id):
    """Das Ereignis ist unvertraut. `True` ist in Python ein `int` — ohne den
    ausdruecklichen Ausschluss waere es eine gueltige Kennung."""
    meta = _meta()
    attach_document_ref(meta, "done", {"renfield_document_id": doc_id},
                        "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, f"{doc_id!r} wurde akzeptiert: {meta}"


def test_a_split_scan_gets_no_button():
    """🛑 `result["documents"]` ist `list[Any]` aus dem Ereignis. Lieber kein
    Knopf als geratene Kennungen aus Freitext."""
    meta = _meta()
    attach_document_ref(meta, "done", {"documents": [{"id": 1}, {"id": 2}]},
                        "scan-20261027-120000-abcdef12")
    assert "artifacts" not in meta, meta


def test_a_second_delivery_replaces_instead_of_duplicating():
    """Die Artefakt-Kennung ist stabil je Auftrag; `merge` verschluesselt nach `id`."""
    meta = _meta()
    job = "scan-20261027-120000-abcdef12"
    attach_document_ref(meta, "done", _DONE, job)
    attach_document_ref(meta, "done", _DONE, job)
    assert len(meta["artifacts"]) == 1, meta["artifacts"]


def test_the_attachment_branches_over_the_SHARED_state_set():
    """🛑 Strukturtest gegen die Drift, die diese Datei schon einmal hatte.

    Ein abgeschriebenes `status == "done"` waere heute richtig und beim naechsten
    neuen Zustand still falsch — genau der Fehler von 2026-10-04.
    """
    quelle = inspect.getsource(attach_document_ref)
    assert "_STATES_WITH_DOCUMENT" in quelle, (
        "der Anhang verzweigt nicht ueber die geteilte Menge:\n" + quelle)
    assert '== "done"' not in quelle, (
        "ein zweites Literal neben der Menge — genau die Drift:\n" + quelle)
    assert "unrouted" not in _STATES_WITH_DOCUMENT, (
        "`unrouted` hat kein abgelegtes Dokument")


# --- Instanzgrenze -----------------------------------------------------------
#
# 🛑 Der Fehler, den der Eigentümer beim Lesen fand: der Scanner schickt die
# Fertigmeldung an die Instanz, die GEFRAGT hat — das Dokument kann aber in
# einer anderen liegen. Die Kennung gehört dann jener anderen Instanz, und weil
# beide eigene Zählfolgen führen, gibt es sie hier auch, für ein anderes
# Dokument. Ein Knopf darauf führt nicht ins Leere, sondern ins FALSCHE.


def _mit_ziel(ziel):
    return {**_DONE, "target": ziel}


def test_a_scan_into_THIS_instance_stays_linkable(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("household"), "scan-20261027-120000-abcdef12")
    assert "instance" not in meta["artifacts"][0]["data"], (
        "ein lokales Dokument braucht keine Instanzangabe — sonst fragt die "
        "Komponente gar nicht erst nach")


def test_a_scan_into_ANOTHER_instance_is_marked_remote(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("xidra"), "scan-20261027-120000-abcdef12")
    assert meta["artifacts"][0]["data"]["instance"] == "xidra", (
        "ohne Markierung loeste die Komponente xidras Kennung LOKAL auf und "
        "traefe ein fremdes Dokument derselben Nummer")


def test_the_comparison_ignores_case_and_spacing(monkeypatch):
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "  Household ", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("HOUSEHOLD"), "scan-20261027-120000-abcdef12")
    assert "instance" not in meta["artifacts"][0]["data"]


def test_WITHOUT_a_self_identity_every_named_target_is_remote(monkeypatch):
    """🛑 Fail-closed. Eine unkonfigurierte Instanz verlinkt nie falsch — sie
    verlinkt gar nicht.

    Der Mechanismus ist die Ungleichheit selbst, kein eigener Zweig: ein
    nichtleeres Ziel ist ungleich der leeren Selbstkennung. Wer hier einen
    zweiten Riegel einbaut, baut einen wirkungslosen — dieser Test bestünde
    mit und ohne ihn.
    """
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "", raising=False)
    meta = _meta()
    attach_document_ref(meta, "done", _mit_ziel("household"), "scan-20261027-120000-abcdef12")
    assert meta["artifacts"][0]["data"]["instance"] == "household", (
        "ohne Selbstkennung muss JEDES benannte Ziel als fremd gelten")


def test_an_unnamed_target_stays_local(monkeypatch):
    """Ohne Ziel im Ereignis gibt es nichts zu vergleichen — `n=1` kurzschliesst
    die ganze Routing-Schicht, dort ist der Scan immer hier."""
    from utils.config import settings
    monkeypatch.setattr(settings, "instance_id", "household", raising=False)
    for ziel in (None, "", "   "):
        meta = _meta()
        attach_document_ref(meta, "done", {**_DONE, "target": ziel},
                            "scan-20261027-120000-abcdef12")
        assert "instance" not in meta["artifacts"][0]["data"], f"target={ziel!r}"

