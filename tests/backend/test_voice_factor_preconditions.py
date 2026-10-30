"""The hurdle is raised exactly when it can also be cleared.

🛑 THE FINDING THIS FILE STANDS AGAINST
=======================================
`/auth/login` raised the hurdle as soon as `VOICE_AUTH_ENABLED` was on.
`/auth/voice` demanded FOUR further things to clear it. Every gap between the
two sets was a lockout -- there is deliberately no fallback to the password
alone. Five variants of the same defect were found; there is one counter-check
here for each.
"""
from unittest.mock import MagicMock

import pytest


def _user(**kw):
    u = MagicMock()
    u.id = kw.get("id", 1)
    u.speaker_id = kw.get("speaker_id", 7)
    u.voice_second_factor_enabled = kw.get("consent", True)
    return u


class TestPathBlocker:
    """The instance's switches -- both of them, not only one."""

    @pytest.mark.unit
    def test_the_voice_path_being_off_blocks(self, monkeypatch):
        from services.voice_factor_preconditions import PATH_OFF, voice_path_blocker
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", False)
        assert voice_path_blocker() == PATH_OFF

    @pytest.mark.unit
    def test_recognition_being_off_blocks_too(self, monkeypatch):
        """🛑 The second guard the login page NEVER queried.

        `/auth/voice` refuses categorically when speaker recognition is off.
        Whoever switches it off -- an obvious privacy action -- thereby locked
        out every account with a consent, the password path included.
        """
        from services.voice_factor_preconditions import RECOGNITION_OFF, voice_path_blocker
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        assert voice_path_blocker() == RECOGNITION_OFF

    @pytest.mark.unit
    def test_both_on_is_open(self, monkeypatch):
        from services.voice_factor_preconditions import voice_path_blocker
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
        assert voice_path_blocker() is None


class TestProfileBlocker:
    """THIS person's door."""

    @pytest.mark.database
    async def test_no_profile_blocks(self, db_session):
        from services.voice_factor_preconditions import NO_PROFILE, voice_profile_blocker

        assert await voice_profile_blocker(db_session, _user(speaker_id=None)) == NO_PROFILE

    @pytest.mark.database
    async def test_a_profile_without_embeddings_blocks(self, db_session, test_speaker):
        from services.voice_factor_preconditions import NO_EMBEDDINGS, voice_profile_blocker

        got = await voice_profile_blocker(db_session, _user(speaker_id=test_speaker.id))
        assert got == NO_EMBEDDINGS

    @pytest.mark.database
    async def test_a_usable_profile_does_not_block(self, db_session, test_speaker):
        from models.database import SpeakerEmbedding
        from services.voice_factor_preconditions import voice_profile_blocker

        db_session.add(SpeakerEmbedding(speaker_id=test_speaker.id, embedding="AAAA"))
        await db_session.commit()
        assert await voice_profile_blocker(db_session, _user(speaker_id=test_speaker.id)) is None


class TestTheHurdleRestsInsteadOfLockingOut:
    """🛑 The core: six routes could remove the profile after the consent
    (`DELETE /users/{id}/link-speaker`, `DELETE /speakers/{id}` via
    `ON DELETE SET NULL`, deleting the last embedding, re-enrolling). After
    that `/auth/login` withheld the tokens and `/auth/voice` refused
    fail-closed: the account was shut.

    Now the hurdle RESTS. The consent stays stored -- it is the record of a
    declaration by the person (Art. 9 GDPR), not a switch the system may flip
    silently -- and takes hold again by itself as soon as the profile is back.
    """

    @pytest.mark.database
    async def test_consent_without_a_profile_lets_the_password_path_through(
        self, db_session, monkeypatch
    ):
        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
        assert await second_factor_applies(db_session, _user(speaker_id=None)) is False

    @pytest.mark.database
    async def test_recognition_off_lets_the_password_path_through(
        self, db_session, test_speaker, monkeypatch
    ):
        from models.database import SpeakerEmbedding
        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        db_session.add(SpeakerEmbedding(speaker_id=test_speaker.id, embedding="AAAA"))
        await db_session.commit()
        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", False)
        assert await second_factor_applies(
            db_session, _user(speaker_id=test_speaker.id)
        ) is False

    @pytest.mark.database
    async def test_a_resting_hurdle_is_logged_as_a_warning(
        self, db_session, monkeypatch, caplog
    ):
        """A resting hurdle is a protection that is NOT in effect right now.
        Silently, that would be a lowering nobody notices."""
        import logging

        from loguru import logger as loguru_logger

        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)

        seen: list[str] = []
        sink = loguru_logger.add(lambda m: seen.append(m), level="WARNING")
        try:
            await second_factor_applies(db_session, _user(speaker_id=None))
        finally:
            loguru_logger.remove(sink)
        assert any("RESTS" in m for m in seen), seen
        assert logging is not None  # (imported only to make the intent clear)

    @pytest.mark.database
    async def test_the_path_being_off_is_quiet(self, db_session, monkeypatch):
        """`VOICE_AUTH_ENABLED=false` is the documented normal state of both
        instances -- a warning on EVERY login would be noise that the real
        deviations drown in."""
        from loguru import logger as loguru_logger

        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", False)
        seen: list[str] = []
        sink = loguru_logger.add(lambda m: seen.append(m), level="WARNING")
        try:
            assert await second_factor_applies(db_session, _user()) is False
        finally:
            loguru_logger.remove(sink)
        assert not [m for m in seen if "RESTS" in m]

    @pytest.mark.database
    async def test_without_consent_nothing_applies(self, db_session, monkeypatch):
        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        assert await second_factor_applies(db_session, _user(consent=False)) is False

    @pytest.mark.database
    async def test_everything_in_place_raises_the_hurdle(
        self, db_session, test_speaker, monkeypatch
    ):
        """The counter-check: otherwise the five tests above would only prove
        that the function always says `False`."""
        from models.database import SpeakerEmbedding
        from services.voice_factor_preconditions import second_factor_applies
        from utils.config import settings

        db_session.add(SpeakerEmbedding(speaker_id=test_speaker.id, embedding="AAAA"))
        await db_session.commit()
        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "speaker_recognition_enabled", True)
        assert await second_factor_applies(
            db_session, _user(speaker_id=test_speaker.id)
        ) is True
