"""Tests for the `/health` endpoint.

Reva-compat: `/health` must expose `prompt_hashes` (12-char SHA-256 prefixes
per loaded prompt YAML) so deploys can be verified to actually have changed
the prompts. Used by Reva's audit trail.
"""
import sys
from unittest.mock import MagicMock, patch

# Pre-mock optional native deps
_missing_stubs = [
    "asyncpg", "faster_whisper", "speechbrain",
    "speechbrain.inference", "speechbrain.inference.speaker",
    "openwakeword", "openwakeword.model",
    "piper", "piper.voice",
]
for _mod in _missing_stubs:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest


@pytest.mark.unit
class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_includes_prompt_hashes(self):
        """The `/health` handler returns prompt_hashes from PromptManager."""
        from main import health_check

        fake_pm = MagicMock()
        fake_pm.prompt_hashes = {"agent": "abc123def456", "intent": "deadbeefcafe"}

        with patch("services.prompt_manager.prompt_manager", fake_pm):
            result = await health_check()

        assert result["status"] == "ok"
        assert result["prompt_hashes"] == {"agent": "abc123def456", "intent": "deadbeefcafe"}

    @pytest.mark.asyncio
    async def test_health_tolerates_prompt_manager_failure(self):
        """If PromptManager raises during access, /health still returns 'ok'.

        The mutating-the-MagicMock-class trick (`type(mock).prop = property(...)`)
        leaks across tests because unittest.mock.patch does NOT restore class-
        level descriptor additions. Subclass `MagicMock` instead so the
        descriptor is scoped to one instance type and is GC'd with the test.
        """
        from main import health_check

        class _BrokenPM(MagicMock):
            @property
            def prompt_hashes(self):
                raise RuntimeError("PM not ready")

        with patch("services.prompt_manager.prompt_manager", _BrokenPM()):
            result = await health_check()

        assert result["status"] == "ok"
        assert result["prompt_hashes"] == {}
