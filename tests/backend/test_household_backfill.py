"""Re-homing the household before the cutover (§4, P0 Nr. 8).

The script runs ONCE, against the only copy of the household's data, while auth
is still off. Every class is therefore tested for three things: the dry-run
counts what the write run writes (§11 Abnahme), the write run is idempotent, and
nothing touches a tier somebody set by hand.

The hand-set rule deserves its own sentence because it has no flag behind it:
the auth-off fallback wrote tier 0 and nothing else, so a NON-ZERO tier is the
fingerprint of a deliberate choice. 56 such rows exist. Overwriting one would be
the script silently overruling its operator.

Real Postgres throughout — every class is SQL, and `update_tier` cascades.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    Atom,
    Circle,
    Conversation,
    KGEntity,
    KGRelation,
    KnowledgeBase,
    Speaker,
)
from services.household_backfill import (
    MemberList,
    _membership_pairs,
    run_backfill,
)
from tests.backend.dbrows import ensure_role, ensure_user

pytestmark = [pytest.mark.backend, pytest.mark.database]


@pytest.fixture
async def household(db_session: AsyncSession):
    """Admin (1), two more family members (2, 3), a guest (4), a device (5)."""
    await ensure_role(db_session)
    admin = await ensure_user(db_session, 1, username="admin")
    await ensure_user(db_session, 2, username="zweite")
    await ensure_user(db_session, 3, username="dritte")
    await ensure_user(db_session, 4, username="gast")
    device = await ensure_user(db_session, 5, username="haushalt")
    device.is_device_account = True
    await db_session.commit()
    return MemberList(admin_id=admin.id, family=[1, 2, 3], guests=[4],
                      device_account_id=5)


async def _kg_atom(db: AsyncSession, owner: int, tier: int, seq: int) -> Atom:
    """A knowledge-graph node atom with its source entity, at `tier`."""
    ent = KGEntity(user_id=owner, name=f"Entity {seq}", entity_type="person",
                   circle_tier=tier)
    db.add(ent)
    await db.flush()
    atom = Atom(atom_id=f"kg-{seq:08d}", atom_type="kg_node",
                source_table="kg_entities", source_id=str(ent.id),
                owner_user_id=owner, policy={"tier": tier})
    db.add(atom)
    await db.flush()
    ent.atom_id = atom.atom_id
    await db.flush()
    return atom


async def _tier_of(db: AsyncSession, atom_id: str) -> int:
    return int((await db.execute(
        text("SELECT (policy ->> 'tier')::int FROM atoms WHERE atom_id = :a"),
        {"a": atom_id},
    )).scalar_one())


class TestTheGraphBecomesHouseholdKnowledge:
    async def test_fallback_tier_atoms_move_to_two(self, db_session, household):
        atom = await _kg_atom(db_session, household.admin_id, 0, 1)
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["kg"], dry_run=False)

        assert res.changed == 1
        assert await _tier_of(db_session, atom.atom_id) == 2

    async def test_a_hand_set_tier_is_never_overwritten(self, db_session, household):
        """No flag marks these — a non-zero tier IS the mark. 56 of them exist,
        and overruling one would be the script overriding its operator."""
        handset = await _kg_atom(db_session, household.admin_id, 4, 2)
        fallback = await _kg_atom(db_session, household.admin_id, 0, 3)
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["kg"], dry_run=False)

        assert res.changed == 1
        assert await _tier_of(db_session, handset.atom_id) == 4
        assert await _tier_of(db_session, fallback.atom_id) == 2

    async def test_the_cascade_carries_the_source_row(self, db_session, household):
        """`update_tier` owns the cascade — never a direct UPDATE, or the
        denormalized column and `atoms.policy` drift apart."""
        atom = await _kg_atom(db_session, household.admin_id, 0, 4)
        await db_session.commit()

        await run_backfill(db_session, household, only=["kg"], dry_run=False)

        tier = (await db_session.execute(
            text("SELECT circle_tier FROM kg_entities WHERE atom_id = :a"),
            {"a": atom.atom_id},
        )).scalar_one()
        assert tier == 2

    async def test_the_dry_run_counts_what_the_write_run_writes(
        self, db_session, household
    ):
        """§11 Abnahme: the two numbers must agree, or the corpus moved between
        the runs and the operator must stop."""
        for i in range(3):
            await _kg_atom(db_session, household.admin_id, 0, 10 + i)
        await _kg_atom(db_session, household.admin_id, 2, 20)
        await db_session.commit()

        [planned] = await run_backfill(db_session, household, only=["kg"], dry_run=True)
        [done] = await run_backfill(db_session, household, only=["kg"], dry_run=False)

        assert planned.changed == done.changed == 3

    async def test_a_second_run_changes_nothing(self, db_session, household):
        await _kg_atom(db_session, household.admin_id, 0, 30)
        await db_session.commit()

        await run_backfill(db_session, household, only=["kg"], dry_run=False)
        [again] = await run_backfill(db_session, household, only=["kg"], dry_run=False)

        assert again.changed == 0

    async def test_revert_puts_the_graph_back(self, db_session, household):
        atom = await _kg_atom(db_session, household.admin_id, 0, 40)
        await db_session.commit()

        await run_backfill(db_session, household, only=["kg"], dry_run=False)
        await run_backfill(db_session, household, only=["kg"], dry_run=False, revert=True)

        assert await _tier_of(db_session, atom.atom_id) == 0

    async def test_another_users_atoms_are_untouched(self, db_session, household):
        mine = await _kg_atom(db_session, household.admin_id, 0, 50)
        theirs = await _kg_atom(db_session, 2, 0, 51)
        await db_session.commit()

        await run_backfill(db_session, household, only=["kg"], dry_run=False)

        assert await _tier_of(db_session, mine.atom_id) == 2
        assert await _tier_of(db_session, theirs.atom_id) == 0


class TestTheOwnerlessRows:
    async def test_null_relations_get_the_admin_and_the_graphs_tier(
        self, db_session, household
    ):
        """Unreachable in EVERY branch once auth is on: the owner branch compares
        against NULL and the membership branch keys `circle_owner_id` on it."""
        a = KGEntity(user_id=1, name="A", entity_type="person")
        b = KGEntity(user_id=1, name="B", entity_type="place")
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(KGRelation(user_id=None, subject_id=a.id, predicate="p",
                                  object_id=b.id, confidence=0.9))
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["null-relationen"],
                                   dry_run=False)

        assert res.changed == 1
        row = (await db_session.execute(
            text("SELECT user_id, circle_tier FROM kg_relations")
        )).first()
        assert row == (1, 2)

    async def test_ownerless_knowledge_bases_get_the_admin(self, db_session, household):
        db_session.add(KnowledgeBase(name="Alt-KB", is_active=True, owner_id=None))
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["kb-eigentümer"],
                                   dry_run=False)

        assert res.changed == 1
        owner = (await db_session.execute(
            text("SELECT owner_id FROM knowledge_bases")
        )).scalar_one()
        assert owner == 1

    async def test_a_conversation_follows_its_speaker(self, db_session, household):
        """D-3: the ones with a linked speaker go to THAT user, not the admin."""
        speaker = Speaker(id=1, name="zweite")
        db_session.add(speaker)
        await db_session.flush()
        user2 = await ensure_user(db_session, 2)
        user2.speaker_id = speaker.id
        db_session.add(Conversation(session_id="mit-sprecher", speaker_id=speaker.id))
        db_session.add(Conversation(session_id="ohne-sprecher"))
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["unterhaltungen"],
                                   dry_run=False)

        assert res.changed == 2
        owners = dict((await db_session.execute(
            text("SELECT session_id, user_id FROM conversations")
        )).all())
        assert owners == {"mit-sprecher": 2, "ohne-sprecher": 1}

    async def test_every_adopted_conversation_gets_its_atom(
        self, db_session, household
    ):
        """The invariant `ensure_atom` keeps everywhere else: a row that gets an
        owner gets its atoms entry, or no per-person grant can ever reach it."""
        db_session.add(Conversation(session_id="alt"))
        await db_session.commit()

        await run_backfill(db_session, household, only=["unterhaltungen"], dry_run=False)

        atom_id = (await db_session.execute(
            text("SELECT atom_id FROM conversations WHERE session_id = 'alt'")
        )).scalar_one()
        assert atom_id is not None

    async def test_an_owned_conversation_is_left_alone(self, db_session, household):
        db_session.add(Conversation(session_id="gehoert-schon", user_id=3))
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["unterhaltungen"],
                                   dry_run=False)

        assert res.changed == 0


class TestTheMemberships:
    async def test_family_reaches_family(self, db_session, household):
        pairs = _membership_pairs(household)
        assert (1, 2, 2) in pairs and (2, 1, 2) in pairs and (2, 3, 2) in pairs
        assert not any(p[0] == p[1] for p in pairs), "nobody is a member of themselves"

    async def test_a_guest_gets_none(self, db_session, household):
        """D-2d: guests are in the house, not in the circles."""
        pairs = _membership_pairs(household)
        assert all(4 not in (owner, member) for owner, member, _ in pairs)

    async def test_the_device_reads_only_the_admins_circle(self, db_session, household):
        """D-2e: an anonymous room turn reads the household graph and what the
        admin holds at tier 2 — NOT the other members' tier-2 memories."""
        pairs = _membership_pairs(household)
        reading = [(o, m) for o, m, _ in pairs if m == household.device_account_id]
        assert reading == [(household.admin_id, household.device_account_id)]

    async def test_every_member_reaches_INTO_the_devices_circle(
        self, db_session, household
    ):
        """The correction to §4.3. A room history is OWNED by the device account
        and the filter keys `circle_owner_id` on the ROW's owner — without this
        direction the kitchen thread reaches nobody but the admin, and P0 Nr. 6
        has no effect in service. §4.3 predates §8.1."""
        pairs = _membership_pairs(household)
        into_device = {m for o, m, _ in pairs if o == household.device_account_id}
        assert into_device == {1, 2, 3}

    async def test_the_rows_are_written_and_idempotent(self, db_session, household):
        [first] = await run_backfill(db_session, household, only=["mitgliedschaften"],
                                     dry_run=False)
        [second] = await run_backfill(db_session, household, only=["mitgliedschaften"],
                                      dry_run=False)

        assert first.changed == len(_membership_pairs(household))
        assert second.changed == 0

    async def test_an_existing_membership_is_never_lowered(self, db_session, household):
        """Somebody may have raised a tier by hand; the backfill is a floor, not
        a rewrite."""
        await db_session.execute(
            text(
                "INSERT INTO circle_memberships "
                "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
                "VALUES (1, 2, 'tier', '4', 1, NOW())"
            )
        )
        await db_session.commit()

        await run_backfill(db_session, household, only=["mitgliedschaften"], dry_run=False)

        value = (await db_session.execute(
            text(
                "SELECT value FROM circle_memberships "
                " WHERE circle_owner_id = 1 AND member_user_id = 2 AND dimension = 'tier'"
            )
        )).scalar_one()
        assert str(value).strip('"') == "4"

    async def test_revert_removes_what_it_created(self, db_session, household):
        await run_backfill(db_session, household, only=["mitgliedschaften"], dry_run=False)
        [res] = await run_backfill(db_session, household, only=["mitgliedschaften"],
                                   dry_run=False, revert=True)

        assert res.changed == len(_membership_pairs(household))
        left = (await db_session.execute(
            text("SELECT COUNT(*) FROM circle_memberships WHERE dimension = 'tier'")
        )).scalar_one()
        assert left == 0


class TestThePairingRemnant:
    async def test_the_stale_self_membership_goes(self, db_session, household):
        """`pairing_service` wrote `(1,1,tier,2)` with a PEER's remote user id,
        which collides with the local admin's own id in a single-user household.
        Under auth-on it would make the admin a member of their own circles."""
        await db_session.execute(
            text(
                "INSERT INTO circle_memberships "
                "(circle_owner_id, member_user_id, dimension, value, granted_by, granted_at) "
                "VALUES (1, 1, 'tier', '2', 1, NOW())"
            )
        )
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["kopplungs-rest"],
                                   dry_run=False)

        assert res.changed == 1
        left = (await db_session.execute(
            text("SELECT COUNT(*) FROM circle_memberships")
        )).scalar_one()
        assert left == 0


class TestTheCapturePolicy:
    async def test_family_captures_at_household_guests_at_self(
        self, db_session, household
    ):
        """D-2c. What a NEW atom is written at: a family member's turns become
        household knowledge, a guest's stay private."""
        await run_backfill(db_session, household, only=["erfassungsvorgabe"],
                           dry_run=False)

        rows = dict((await db_session.execute(
            text("SELECT owner_user_id, default_capture_policy ->> 'tier' FROM circles")
        )).all())
        assert rows == {1: "2", 2: "2", 3: "2", 4: "0"}

    async def test_an_existing_policy_is_somebodys_decision(self, db_session, household):
        db_session.add(Circle(owner_user_id=2, dimension_config={"tier": {}},
                              default_capture_policy={"tier": 4}))
        await db_session.commit()

        await run_backfill(db_session, household, only=["erfassungsvorgabe"],
                           dry_run=False)

        value = (await db_session.execute(
            text("SELECT default_capture_policy ->> 'tier' FROM circles "
                 " WHERE owner_user_id = 2")
        )).scalar_one()
        assert value == "4"


class TestTheReportOnlyClass:
    async def test_admin_atoms_are_counted_never_moved(self, db_session, household):
        """Documents, facts and memories are ALREADY at tier 0 and their target
        is tier 0. What changes for them at the cutover is that the filter starts
        being evaluated — not anything this script writes. Counted anyway, so a
        dry-run shows the whole posture instead of only the deltas."""
        doc_atom = Atom(atom_id="doc-1", atom_type="kb_document",
                        source_table="documents", source_id="1",
                        owner_user_id=1, policy={"tier": 0})
        db_session.add(doc_atom)
        await db_session.commit()

        [res] = await run_backfill(db_session, household, only=["admin-atome"],
                                   dry_run=False)

        assert res.examined == 1
        assert res.changed == 0
        assert await _tier_of(db_session, "doc-1") == 0


class TestTheRunner:
    async def test_an_unknown_class_is_refused_by_name(self, db_session, household):
        with pytest.raises(ValueError, match="Unbekannte Klasse"):
            await run_backfill(db_session, household, only=["gibtsnicht"])

    async def test_the_default_runs_every_class(self, db_session, household):
        from services.household_backfill import CLASSES

        results = await run_backfill(db_session, household, dry_run=True)
        assert [r.name for r in results] == list(CLASSES)
