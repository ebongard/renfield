"""Encryption at rest with key rotation (BL-0357).

SECRET_KEY_PREVIOUS lets stored Fernet tokens (BLE IRKs) stay decryptable across
a SECRET_KEY rotation: encrypt with the current key, decrypt with any listed key,
re-encrypt (rotate) under the current one so the old key can be dropped.
"""
import pytest
from pydantic import SecretStr

import services.secret_encryption as se
from services.secret_encryption import (
    InvalidToken,
    decrypt_secret,
    encrypt_secret,
    is_current_key,
    reset_key_cache,
    rotate_secret,
)

OLD = "old-secret-key-0123456789-abcdef-0123456789"
NEW = "new-secret-key-9876543210-fedcba-9876543210"
IRK = "3a66fe43118690229991659536ef9a4b"


@pytest.fixture
def keys(monkeypatch):
    """Set (current, previous) keys and reset the derived-key cache; restores after."""
    def _set(current: str, previous: str = ""):
        monkeypatch.setattr(se.settings, "secret_key", SecretStr(current), raising=False)
        monkeypatch.setattr(se.settings, "secret_key_previous", SecretStr(previous), raising=False)
        reset_key_cache()

    yield _set
    reset_key_cache()


def _token_under(secret: str) -> str:
    """A token as the OLD deployment would have written it (plain Fernet)."""
    return se._fernet_for(secret).encrypt(IRK.encode()).decode()


class TestRoundTrip:
    @pytest.mark.unit
    def test_encrypt_decrypt(self, keys):
        keys(NEW)
        tok = encrypt_secret(IRK)
        assert tok != IRK
        assert decrypt_secret(tok) == IRK
        assert is_current_key(tok) is True

    @pytest.mark.unit
    def test_wrong_key_without_previous_is_invalid(self, keys):
        keys(NEW)
        old_tok = _token_under(OLD)
        with pytest.raises(InvalidToken):
            decrypt_secret(old_tok)
        with pytest.raises(InvalidToken):
            is_current_key(old_tok)


class TestPreviousKeys:
    @pytest.mark.unit
    async def test_previous_key_decrypts_old_tokens(self, keys):
        keys(NEW, previous=OLD)
        old_tok = _token_under(OLD)
        assert decrypt_secret(old_tok) == IRK
        assert is_current_key(old_tok) is False

    @pytest.mark.unit
    def test_encryption_always_uses_the_current_key(self, keys):
        keys(NEW, previous=OLD)
        tok = encrypt_secret(IRK)
        # The old key alone cannot read it — the new key is the writer.
        with pytest.raises(InvalidToken):
            se._fernet_for(OLD).decrypt(tok.encode())
        assert se._fernet_for(NEW).decrypt(tok.encode()).decode() == IRK

    @pytest.mark.unit
    def test_rotate_reencrypts_under_current_and_is_idempotent(self, keys):
        keys(NEW, previous=OLD)
        old_tok = _token_under(OLD)
        new_tok = rotate_secret(old_tok)
        assert new_tok != old_tok
        assert is_current_key(new_tok) is True
        assert decrypt_secret(new_tok) == IRK
        assert rotate_secret(new_tok) == new_tok  # already current → unchanged
        # After the previous key is dropped the rotated token still works.
        keys(NEW)
        assert decrypt_secret(new_tok) == IRK

    @pytest.mark.unit
    def test_several_previous_keys_newest_first(self, keys):
        older = "older-secret-key-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        keys(NEW, previous=f"{OLD}, {older}")
        assert decrypt_secret(_token_under(OLD)) == IRK
        assert decrypt_secret(_token_under(older)) == IRK

    @pytest.mark.unit
    def test_previous_key_with_trailing_newline_still_matches(self, keys):
        # A former SECRET_KEY provisioned --from-file carried its newline INTO the
        # derived key; listing it must match byte-exactly, and the stripped form
        # must match too when the operator pastes it cleanly.
        keys(NEW, previous=f"{OLD}\n")
        assert decrypt_secret(_token_under(f"{OLD}\n")) == IRK   # verbatim match
        assert decrypt_secret(_token_under(OLD)) == IRK          # stripped match

    @pytest.mark.unit
    def test_current_key_listed_as_previous_is_harmless(self, keys):
        keys(NEW, previous=NEW)
        tok = encrypt_secret(IRK)
        assert decrypt_secret(tok) == IRK
        assert is_current_key(tok) is True

    @pytest.mark.unit
    def test_unknown_key_stays_invalid_even_with_previous(self, keys):
        keys(NEW, previous=OLD)
        with pytest.raises(InvalidToken):
            rotate_secret(_token_under("some-third-key-never-configured-xxxxxxxxxxxx"))


class TestRotateStoredIrks:
    """The ops path: ha_glue.services.secret_rotation.rotate_irks over user_ble_irks."""

    async def _seed(self, db_session, user_id: int, tokens: list[tuple[str, str]]):
        from ha_glue.models.database import UserBleIrk
        rows = [UserBleIrk(user_id=user_id, label=label, irk_encrypted=tok) for label, tok in tokens]
        db_session.add_all(rows)
        await db_session.commit()
        return rows

    @pytest.mark.database
    async def test_dry_run_reports_but_writes_nothing(self, db_session, test_user, keys):
        from ha_glue.services.secret_rotation import rotate_irks
        keys(NEW, previous=OLD)
        rows = await self._seed(db_session, test_user.id, [("phone-a", _token_under(OLD)), ("phone-b", encrypt_secret(IRK))])
        before = [r.irk_encrypted for r in rows]

        report = await rotate_irks(db_session, commit=False)

        assert report.as_dict() == {"total": 2, "already_current": 1, "rotated": 1, "undecryptable": 0, "committed": False}
        await db_session.refresh(rows[0])
        await db_session.refresh(rows[1])
        assert [r.irk_encrypted for r in rows] == before

    @pytest.mark.database
    async def test_commit_rewrites_old_tokens_only(self, db_session, test_user, keys):
        from ha_glue.services.secret_rotation import rotate_irks
        keys(NEW, previous=OLD)
        current_tok = encrypt_secret(IRK)
        rows = await self._seed(db_session, test_user.id, [("phone-a", _token_under(OLD)), ("phone-b", current_tok)])

        report = await rotate_irks(db_session, commit=True)

        assert report.rotated == 1 and report.already_current == 1 and report.committed is True
        await db_session.refresh(rows[0])
        await db_session.refresh(rows[1])
        assert rows[1].irk_encrypted == current_tok            # untouched
        assert rows[0].irk_encrypted != _token_under(OLD)      # rewritten
        keys(NEW)                                              # previous key dropped
        assert decrypt_secret(rows[0].irk_encrypted) == IRK    # still readable

    @pytest.mark.database
    async def test_undecryptable_rows_are_counted_and_left_alone(self, db_session, test_user, keys):
        from ha_glue.services.secret_rotation import rotate_irks
        keys(NEW, previous=OLD)
        stray = _token_under("a-key-nobody-configured-zzzzzzzzzzzzzzzzzzzzzz")
        rows = await self._seed(db_session, test_user.id, [("phone-x", stray)])

        report = await rotate_irks(db_session, commit=True)

        assert report.undecryptable == 1 and report.rotated == 0
        await db_session.refresh(rows[0])
        assert rows[0].irk_encrypted == stray
