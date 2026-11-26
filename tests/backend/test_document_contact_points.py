"""Contact points from documents (#1240 stage 4): the issuer's own numbers.

Pure extraction rules first (keyword gate, issuer block, recipient block, line
ends), then real Postgres for the attachment (exactly one entity of the owner),
idempotency, the tier, and the read-side guards (a shared number names nobody, a
document value ranks after a manual one). Fictitious numbers only -- this
repository is public.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from models.database import ATOM_TYPE_DOCUMENT_FACT, Atom, DocumentFact
from services.contact_points import ContactPointService
from services.document_contact_points import (
    ISSUER_WINDOW,
    find_issuer_numbers,
    sync_document_contact_points,
)
from tests.backend.test_kg_contact_points import (
    _doc,
    _entity,
    _make_user,
    _next,
)

ISSUER = "Beispiel Stadtwerke GmbH"
LETTERHEAD = (
    "Beispiel Stadtwerke GmbH\nHauptstrasse 1\n12345 Beispielstadt\n"
    "Telefon: 030 1234567\nTelefax 030 1234568\n"
)
PHONE_E164 = "+49301234567"
FAX_E164 = "+49301234568"


def _kinds(found):
    return {(n.kind, n.normalized) for n in found}


class TestExtraction:
    def test_letterhead_phone_and_fax(self):
        found, dropped = find_issuer_numbers(LETTERHEAD + "Sehr geehrte Frau ...", [ISSUER])
        assert _kinds(found) == {("phone", PHONE_E164), ("fax", FAX_E164)}
        assert dropped == 0

    def test_mobile_keyword_and_detected_mobile(self):
        found, _ = find_issuer_numbers(
            f"{ISSUER}\nMobil 0170 1234567\nTel. 0171 1234567", [ISSUER])
        assert _kinds(found) == {("mobile", "+491701234567"), ("mobile", "+491711234567")}

    def test_no_keyword_no_number(self):
        found, _ = find_issuer_numbers(f"{ISSUER}\nKundennummer 030 1234567", [ISSUER])
        assert found == []

    def test_far_from_issuer_is_body_text(self):
        body = f"{ISSUER}\nHauptstrasse 1\n" + ("Text " * (ISSUER_WINDOW // 4)) + "\nTel 030 7654321"
        found, _ = find_issuer_numbers(body, [ISSUER])
        assert found == []

    def test_collection_agency_number_is_not_the_clients(self):
        # The agency writes for its client; the client's name sits in the body,
        # the agency's number sits by the agency's name.
        doc = (
            "Beispiel Inkasso GmbH\nTel 030 1112223\n"
            + ("Forderung " * 40)
            + "\nIhre Forderung gegenueber der Muster AG, Telefon 030 9998887"
        )
        assert _kinds(find_issuer_numbers(doc, ["Beispiel Inkasso GmbH"])[0]) == {("phone", "+49301112223")}
        assert find_issuer_numbers(doc, ["Muster AG"])[0][0].normalized == "+49309998887"

    def test_recipient_block_is_dropped(self):
        doc = f"{ISSUER} Tel 030 1234567\nHerrn Beispiel, Tel 030 5556667"
        found, dropped = find_issuer_numbers(doc, [ISSUER], ["Herrn Beispiel Tel 030 5556667"])
        assert _kinds(found) == {("phone", PHONE_E164)}
        assert dropped == 1

    def test_number_stops_at_the_line_end(self):
        found, _ = find_issuer_numbers(f"{ISSUER}\nTel 030 1234567\n12345 Beispielstadt", [ISSUER])
        assert [n.value for n in found] == ["030 1234567"]

    def test_issuer_without_legal_form_and_spacing(self):
        found, _ = find_issuer_numbers("Beispiel   Stadtwerke\nT: +49 30 1234567", [ISSUER])
        assert _kinds(found) == {("phone", PHONE_E164)}

    def test_single_letter_needs_a_separator(self):
        found, _ = find_issuer_numbers(f"{ISSUER}\nKlasse M 2030 12345", [ISSUER])
        assert found == []

    def test_parenthesized_area_code_and_word_label(self):
        found, _ = find_issuer_numbers(
            f"{ISSUER}\nTel. (030) 1234567\nFax (Zentrale): 030 1234568", [ISSUER])
        assert _kinds(found) == {("phone", PHONE_E164), ("fax", FAX_E164)}

    def test_number_keyword_forms(self):
        found, _ = find_issuer_numbers(
            f"{ISSUER}\nTel.-Nr. 030 1234567\nFaxnummer: 030 1234568", [ISSUER])
        assert _kinds(found) == {("phone", PHONE_E164), ("fax", FAX_E164)}

    def test_a_column_gap_ends_the_number(self):
        found, _ = find_issuer_numbers(f"{ISSUER}\nTel 030 1234567   01.02.2024", [ISSUER])
        assert [n.value for n in found] == ["030 1234567"]

    def test_issuer_matches_whole_words_only(self):
        # "Bau GmbH" without its legal form is "Bau" -- it must not match "Baumarkt".
        found, _ = find_issuer_numbers("Baumarkt Beispiel\nTel 030 1234567", ["Bau GmbH"])
        assert found == []

    def test_long_blank_run_after_a_keyword_is_fast(self):
        import time

        t0 = time.perf_counter()
        find_issuer_numbers(f"{ISSUER}\n" + ("Tel" + " " * 2000 + "x\n") * 20, [ISSUER])
        assert time.perf_counter() - t0 < 0.5

    def test_no_issuer_no_numbers(self):
        assert find_issuer_numbers(LETTERHEAD, []) == ([], 0)


async def _fact(db, owner, doc, kind: str, value: str, excerpt: str | None = None) -> None:
    atom_id = f"00000000-0000-0000-1242-{_next():012d}"
    db.add(Atom(atom_id=atom_id, atom_type=ATOM_TYPE_DOCUMENT_FACT, source_table="document_facts",
                source_id=f"t{_next()}", owner_user_id=owner.id, policy={"tier": doc.circle_tier}))
    await db.flush()
    db.add(DocumentFact(document_id=doc.id, category="party", kind=kind, value=value,
                        excerpt=excerpt, atom_id=atom_id, circle_tier=doc.circle_tier, source="llm"))
    await db.flush()


async def _doc_values(db, entity_id: int):
    return (await db.execute(text(
        "SELECT kind, normalized, source_type, document_id, source_tier, circle_tier "
        "FROM kg_entity_contact_points WHERE entity_id = :e AND valid_to IS NULL ORDER BY kind"
    ), {"e": entity_id})).all()


@pytest.fixture
def enabled(monkeypatch):
    from utils.config import settings

    monkeypatch.setattr(settings, "contact_points_enabled", True)
    monkeypatch.setattr(settings, "contact_points_default_region", "DE")
    monkeypatch.setattr(settings, "auth_enabled", True)
    return settings


@pytest.mark.database
class TestSync:
    async def test_attaches_to_the_one_matching_organization(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        doc = await _doc(db, owner, tier=1)
        await _fact(db, owner, doc, "aussteller", ISSUER)

        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert (res.candidates, res.stored) == (2, 2)
        rows = await _doc_values(db, org.id)
        assert {(r.kind, r.normalized) for r in rows} == {("phone", PHONE_E164), ("fax", FAX_E164)}
        # Tier = LEAST(entity 2, document 1); the document is the recorded source.
        assert {(r.source_type, r.document_id, r.source_tier, r.circle_tier) for r in rows} == {
            ("document", doc.id, 1, 1)}

        # Idempotent: a second run replaces, it does not add.
        await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert len(await _doc_values(db, org.id)) == 2

    async def test_alias_matches(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, "Stadtwerke Beispiel", tier=2, etype="organization")
        org.surface_forms = [ISSUER]
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        await db.flush()
        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert res.stored == 2

    async def test_no_or_two_entities_store_nothing(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        other = await _make_user(db, "oth")
        await _entity(db, other, ISSUER, tier=2, etype="organization")  # someone else's
        await _entity(db, owner, ISSUER, tier=2, etype="place")         # wrong type
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert (res.stored, res.no_entity) == (0, 2)

        await _entity(db, owner, ISSUER, tier=2, etype="organization")
        await _entity(db, owner, ISSUER, tier=1, etype="organization")
        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert (res.stored, res.ambiguous_entity) == (0, 2)

    async def test_manual_value_is_left_alone_and_ranks_first(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        svc = ContactPointService(db)
        await svc.add(org, kind="phone", value="030 1234567", actor_id=owner.id)
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert (res.stored, res.unchanged) == (1, 1)  # the fax is new, the phone was there
        hits = await svc.find_for_name(ISSUER, owner.id)
        assert [h.point.source_type for h in hits if h.point.kind == "phone"] == ["manual"]

    async def test_shared_number_names_nobody(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        a = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        b = await _entity(db, owner, "Beispiel Wasser AG", tier=2, etype="organization")
        for name in (ISSUER, "Beispiel Wasser AG"):
            doc = await _doc(db, owner, tier=2)
            await _fact(db, owner, doc, "aussteller", name)
            # The same advisor's line printed under both issuers.
            await sync_document_contact_points(db, doc.id, f"{name}\nTel 030 4443332", commit=False)
        svc = ContactPointService(db)
        assert await svc.lookup_number("030 4443332", owner.id) == []
        assert await svc.list_for_entity(a.id, owner.id) == []
        assert await svc.list_for_entity(b.id, owner.id) == []

    async def test_other_owners_same_number_does_not_hide(self, pg_db_session, enabled):
        db = pg_db_session
        owner, other = await _make_user(db, "own"), await _make_user(db, "oth")
        for user in (owner, other):
            await _entity(db, user, ISSUER, tier=2, etype="organization")
            doc = await _doc(db, user, tier=2)
            await _fact(db, user, doc, "aussteller", ISSUER)
            await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        hits = await ContactPointService(db).lookup_number("030 1234567", owner.id)
        assert len(hits) == 1

    async def test_a_later_more_open_document_does_not_lift_the_tier(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=3, etype="organization")
        private = await _doc(db, owner, tier=0)
        await _fact(db, owner, private, "aussteller", ISSUER)
        await sync_document_contact_points(db, private.id, LETTERHEAD, commit=False)
        shared = await _doc(db, owner, tier=2)
        await _fact(db, owner, shared, "aussteller", ISSUER)
        res = await sync_document_contact_points(db, shared.id, LETTERHEAD, commit=False)
        assert (res.stored, res.unchanged) == (0, 2)
        rows = await _doc_values(db, org.id)
        assert {(r.document_id, r.source_tier, r.circle_tier) for r in rows} == {(private.id, 0, 0)}

    async def test_owner_edits_survive_a_resync(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        svc = ContactPointService(db)
        monkey_commit = db.commit
        db.commit = db.flush  # stay inside the test transaction
        try:
            points = {p.kind: p for p in await svc.list_for_entity(org.id, owner.id)}
            await svc.update(points["phone"], source_tier=0)          # made private
            await svc.delete(points["fax"])                           # removed as wrong
            res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        finally:
            db.commit = monkey_commit
        assert (res.stored, res.unchanged, res.rejected) == (0, 1, 1)
        rows = await _doc_values(db, org.id)
        assert [(r.kind, r.source_tier, r.circle_tier) for r in rows] == [("phone", 0, 0)]

    async def test_a_number_gone_from_the_text_is_removed(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        res = await sync_document_contact_points(
            db, doc.id, f"{ISSUER}\nTelefon: 030 1234567", commit=False)
        assert res.removed == 1
        assert {r.kind for r in await _doc_values(db, org.id)} == {"phone"}

    async def test_recipient_fact_drops_the_number(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        await _entity(db, owner, ISSUER, tier=2, etype="organization")
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        await _fact(db, owner, doc, "empfaenger", "Herrn Beispiel", "Herrn Beispiel Tel 030 1234567")
        res = await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        assert (res.stored, res.in_recipient_block) == (1, 1)

    async def test_deleting_the_document_takes_its_values(self, pg_db_session, enabled):
        db = pg_db_session
        owner = await _make_user(db, "own")
        org = await _entity(db, owner, ISSUER, tier=2, etype="organization")
        doc = await _doc(db, owner, tier=2)
        await _fact(db, owner, doc, "aussteller", ISSUER)
        await sync_document_contact_points(db, doc.id, LETTERHEAD, commit=False)
        await db.execute(text("DELETE FROM document_facts WHERE document_id = :d"), {"d": doc.id})
        await db.execute(text("DELETE FROM documents WHERE id = :d"), {"d": doc.id})
        assert await _doc_values(db, org.id) == []


class TestFlag:
    async def test_hook_is_dark_by_default(self, monkeypatch):
        from services import document_contact_points as mod
        from utils.config import settings

        assert type(settings).model_fields["contact_points_from_documents_enabled"].default is False
        monkeypatch.setattr(settings, "contact_points_enabled", True)
        monkeypatch.setattr(settings, "contact_points_from_documents_enabled", False)

        called = []
        monkeypatch.setattr(mod, "sync_document_contact_points",
                            lambda *a, **k: called.append(a))
        await mod.contact_points_post_extraction(1, LETTERHEAD)
        assert called == []
