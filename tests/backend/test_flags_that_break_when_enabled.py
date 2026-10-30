"""Three flags that did not survive being switched on.

All three come from `tasks/backlog/TRIAGE.md` (the 2026-09-20 triage). They
share the one property that let them survive this long in the first place: the
defect sits behind a SWITCHED-OFF flag, so no test and no production run hits
it. Only a test that flips the flag itself sees it. That is why every test here
flips the flag.

🛑 The triage's fourth finding -- that `MEMORY_EXTRACTION_V2_AUTHORITATIVE`
returns ahead of the subsume gate -- is OUT OF DATE and needs no test here:
#1313 built the gate into the v2 path on 2026-09-23
(`conversation_memory_service.py:1596-1600`, the same `_should_subsume_fact`).
`test_memory_extraction_v2.py` covers it.
"""
from __future__ import annotations

import numpy as np
import pytest

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# 1. VOICE_AUTH_ENABLED -- signature and return type
# ---------------------------------------------------------------------------
class TestVoiceAuthCallShape:
    """The CONTRACT the route once failed against -- and which still holds.

    `identify_speaker` lives on: the resolver and `/api/speakers/identify` need
    the 1:N assignment. The LOGIN has not used it since the rework into an
    additional factor. These contract checks stay, because a change to the
    signature has to carry the two remaining callers with it.

    Historically: the route called `identify_speaker(audio_bytes)` -- ONE value
    where two mandatory arguments stand -- and read the result as a `dict`
    although a `tuple` comes back. `VOICE_AUTH_ENABLED=true` was therefore a
    `TypeError` on every login attempt.
    """

    def test_identify_speaker_needs_two_arguments(self):
        # The contract the route violated. If it changes, the route has to
        # move with it -- which is why it stands here as an assertion.
        import inspect

        from services.speaker_service import SpeakerService

        sig = inspect.signature(SpeakerService.identify_speaker)
        required = [
            n for n, p in sig.parameters.items()
            if n != "self" and p.default is inspect.Parameter.empty
        ]
        assert required == ["query_embedding", "known_speakers"]

    def test_identify_speaker_returns_a_tuple_not_a_dict(self):
        from services.speaker_service import SpeakerService

        svc = SpeakerService()
        q = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        known = [(7, "Anna", np.array([1.0, 0.0, 0.0], dtype=np.float32))]
        out = svc.identify_speaker(q, known)
        assert isinstance(out, tuple), "die Route las ein dict — das gab es nie"
        assert out[0] == 7 and out[1] == "Anna"

    def test_the_route_does_not_call_the_enrolling_resolver(self):
        """🛑 A LOGIN ATTEMPT must never create a profile.

        `resolve_speaker_from_embedding` writes an unknown speaker down as
        "Unbekannter Sprecher #N" and reinforces existing profiles. On a login
        path that would mean: every failed attempt creates a voiceprint
        (biometric data without consent), and a stranger could pull a profile
        onto their own voice step by step. The test reads the source, because
        the absence of a call cannot be checked any other way.
        """
        from api.routes import auth

        body = _function_body(auth.voice_authenticate)
        assert "resolve_speaker_from_embedding" not in body, (
            "die Sprachanmeldung darf nur LESEN, nicht einschreiben"
        )
        # `build_known_speaker_centroids` stood here until the rework into an
        # additional factor. Since 2026-09-27 the route verifies 1:1 against
        # ONE profile and builds no centroids at all -- that is what
        # `TestTheRouteIsNoLongerAnIdentifier` checks.

    def test_the_route_sets_cookies_and_reports_the_password_gate(self):
        """The other four login paths do both -- this one did neither.

        Without `_set_auth_cookies` a successful voice login set up no session
        at all (since #1125 the user interface works off the HttpOnly cookie).
        And without `must_change_password` an account under forced rotation
        would get past it by voice.
        """
        import inspect

        from api.routes import auth

        src = inspect.getsource(auth.voice_authenticate)
        assert "_set_auth_cookies(" in src
        assert "must_change_password=user.must_change_password" in src
        assert "must_change_password" in auth.VoiceAuthResponse.model_fields

    def test_both_sides_of_the_hurdle_ask_the_same_checker(self):
        """🛑 The finding this stands against: `/auth/login` raised the hurdle
        on ONE condition and `/auth/voice` cleared it on FIVE. Every gap
        between them was a lockout, because there is no fallback to the
        password alone.

        The check therefore lives in `services/voice_factor_preconditions`, and
        BOTH sides ask there. This is deliberately a structural check: the
        behaviour is in `TestTheHurdleRestsInsteadOfLockingOut`
        (`test_voice_factor_preconditions.py`) and in
        `test_the_route_refuses_while_recognition_is_off` further down -- the
        point here is that the two sides do not drift apart again by somebody
        writing their own condition alongside.
        """
        import inspect

        from api.routes import auth

        login_src = inspect.getsource(auth.login)
        voice_src = inspect.getsource(auth.voice_authenticate)
        assert "second_factor_applies" in login_src
        assert "voice_path_blocker" in voice_src
        # And no longer by hand alongside it:
        assert "settings.voice_auth_enabled" not in login_src, (
            "die Anmeldeseite prueft wieder selbst — genau so ist der Fehler entstanden"
        )


# ---------------------------------------------------------------------------
# 3. /api/speakers/identify -- the same yardstick as recognition
# ---------------------------------------------------------------------------
class TestOneCentroidCalculation:
    """The diagnostic endpoint answered from a different model than
    recognition did. Three deviations, each one on its own enough for a
    different answer.
    """

    def test_normalisation_follows_either_switch(self, monkeypatch):
        # 🛑 The core. The route checked only
        # `speaker_quality_gating_enabled`. In the household `controlled` is on
        # and `gating` is off -- so recognition normalized and the route did
        # not.
        from services import speaker_resolver as sr

        monkeypatch.setattr("utils.config.settings.speaker_quality_gating_enabled", False)
        monkeypatch.setattr("utils.config.settings.speaker_controlled_enrollment_enabled", True)
        gating, controlled, quality_active = sr.known_speaker_flags()
        assert gating is False
        assert controlled is True
        assert quality_active is True, "controlled allein muss die Normalisierung einschalten"

        monkeypatch.setattr("utils.config.settings.speaker_controlled_enrollment_enabled", False)
        assert sr.known_speaker_flags()[2] is False

    def test_the_route_uses_the_shared_builder(self):
        import inspect

        from api.routes import speakers

        src = inspect.getsource(speakers.get_speaker_embeddings_averaged)
        assert "build_known_speaker_centroids" in src
        assert "np.linalg.norm" not in src, "keine zweite Kopie der Rechnung"

    def test_controlled_mode_compares_only_against_enrolled_profiles(self):
        from services.speaker_resolver import build_known_speaker_centroids
        known, _with_emb = build_known_speaker_centroids(
            [_speaker(1, "Anna", enrolled=True), _speaker(2, "Gast", enrolled=False)],
            controlled=True, quality_active=True,
        )
        assert [k[1] for k in known] == ["Anna"]
        # Without the switch both are included -- otherwise the route would
        # have quietly become narrower ahead of phase 3.
        known_all, _ = build_known_speaker_centroids(
            [_speaker(1, "Anna", enrolled=True), _speaker(2, "Gast", enrolled=False)],
            controlled=False, quality_active=True,
        )
        assert sorted(k[1] for k in known_all) == ["Anna", "Gast"]

    def test_only_the_ten_most_recent_embeddings_count(self):
        # The route averaged ALL of them. A profile with 40 old and 3 new
        # embeddings therefore yields a different centroid than recognition.
        from services.speaker_resolver import (
            MAX_EMBEDDINGS_PER_SPEAKER,
            build_known_speaker_centroids,
        )
        # 12 embeddings: the 10 most recent are ones, the 2 oldest zeros.
        sp = _speaker(1, "Anna", enrolled=True, values=[1.0] * 10 + [0.0] * 2)
        known, _ = build_known_speaker_centroids(
            [sp], controlled=True, quality_active=False
        )
        assert MAX_EMBEDDINGS_PER_SPEAKER == 10
        assert known[0][2].tolist() == [1.0], "die zwei aeltesten duerfen nicht mitzaehlen"

    def test_normalisation_stops_a_loud_sample_from_dominating(self):
        # Raw ECAPA norms vary ~250-410; without normalization the loud
        # recording pulls the centroid towards itself.
        from services.speaker_resolver import build_known_speaker_centroids
        sp = _speaker(1, "Anna", enrolled=True, values=[1.0, 100.0])
        raw, _ = build_known_speaker_centroids([sp], controlled=False, quality_active=False)
        norm, _ = build_known_speaker_centroids([sp], controlled=False, quality_active=True)
        assert raw[0][2].tolist() == [50.5]
        assert norm[0][2].tolist() == [1.0]


# --- helpers ----------------------------------------------------------------
def _function_body(fn) -> str:
    """The source WITHOUT the docstring.

    Otherwise a test for "this call does not appear here" fires on the
    reasoning that names exactly that call -- which happened once, on
    2026-09-27.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(fn).lstrip())
    node = tree.body[0]
    body = node.body[1:] if ast.get_docstring(node) is not None else node.body
    return "\n".join(ast.unparse(n) for n in body)


class _Emb:
    def __init__(self, value: float, created_at):
        from services.speaker_service import SpeakerService
        self.embedding = SpeakerService.embedding_to_base64(
            np.array([value], dtype=np.float32)
        )
        self.created_at = created_at


class _Speaker:
    def __init__(self, sid, name, enrolled, embeddings):
        self.id, self.name, self.enrolled, self.embeddings = sid, name, enrolled, embeddings


def _speaker(sid, name, *, enrolled, values=(1.0,)):
    from datetime import datetime, timedelta
    base = datetime(2026, 1, 1)
    # The FIRST value is the most recent, so `values` reads as "newest first".
    return _Speaker(sid, name, enrolled,
                    [_Emb(v, base - timedelta(days=i)) for i, v in enumerate(values)])


# ---------------------------------------------------------------------------
# 1b. The SECOND factor -- behaviour, not source text
# ---------------------------------------------------------------------------
@pytest.mark.database
class TestVoiceSecondFactor:
    """The route after the 2026-09-27 rework: the voice confirms, it does not
    open.

    🛑 This class RUNS the two routes instead of reading their source. The first
    version of this file checked `inspect.getsource` text matches; that would
    have survived any of the bridges as long as the right strings stand in the
    body. What is checked here is results: cookies set, tokens issued, the
    ABSENCE of tokens, and that there is no path without the voice.
    """

    @pytest.fixture(autouse=True)
    def _no_rate_limit(self, monkeypatch):
        """Rate limiter off, because these tests call the handler DIRECTLY.

        The same pattern as `test_auth.py:280` ("its wrapper guards everything
        behind `if self.enabled`"): a direct call should not hang on a counter
        that other tests fill, and slowapi reaches for
        `request.app.state.limiter` (`extension.py:83`), which a hand-built
        `Request` does not have.

        🛑 A correction to myself: this first said the bypass fixed the four
        failures in the full suite. That was WRONG -- it did not fix them, the
        cause was the name binding in `_own_session`. A comment that claims a
        disproven cause costs the next reader the same journey a second time.
        The bypass stays because it is right on its own merits; what it costs
        is recovered by `TestTheRateLimitIsStillDeclared`.
        """
        from services.api_rate_limiter import limiter

        monkeypatch.setattr(limiter, "enabled", False)

    @staticmethod
    def _upload(data: bytes = b"RIFFfake", name: str = "turn.wav"):
        class _Up:
            filename = name

            async def read(self):
                return data

        return _Up()

    @staticmethod
    def _request():
        """A REAL starlette request: the rate limiter checks the type."""
        from starlette.requests import Request

        return Request({
            "type": "http", "method": "POST", "path": "/api/auth/voice",
            "raw_path": b"/api/auth/voice", "root_path": "", "scheme": "http",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 12345),
            "server": ("test", 80), "app": None,
        })

    async def _user(self, db, *, second_factor=True, with_speaker=True,
                    must_change=False, active=True):
        import numpy as np

        from models.database import Role, Speaker, SpeakerEmbedding, User
        from services.speaker_service import SpeakerService

        role = Role(name=f"V2F{id(self) % 100000}", description="", permissions=["chat.own"])
        db.add(role)
        await db.flush()
        sid = None
        if with_speaker:
            sp = Speaker(name="Anna", enrolled=True)
            db.add(sp)
            await db.flush()
            db.add(SpeakerEmbedding(
                speaker_id=sp.id,
                embedding=SpeakerService.embedding_to_base64(
                    np.array([1.0, 0.0], dtype=np.float32)),
            ))
            sid = sp.id
        user = User(username=f"anna{id(self) % 100000}", password_hash="x",
                    role_id=role.id, is_active=active, token_epoch=0,
                    speaker_id=sid, must_change_password=must_change,
                    voice_second_factor_enabled=second_factor)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    def _stub_voice(monkeypatch, *, verified, score, duration_s=3.0, embedding=(1.0, 0.0)):
        """Replace only the ML half. The rest of the route runs for real.

        🛑 Since the review the embedding comes from the VOICE SERVER, not from
        `SpeakerService`. The old path could never succeed in production:
        `speaker_inprocess_embeddings_enabled` is off on both instances, and
        even open it would be the wrong vector space. So `stt` is stubbed here
        -- and on the module the route imports FROM, not at the source:
        `from ... import stt` binds the name at import time.
        """
        from services import voice_server_client as vsc

        async def _stt(_audio, **_kw):
            return {"speaker_embedding": list(embedding), "audio_duration_s": duration_s}

        monkeypatch.setattr(vsc, "stt", _stt)

        from services import speaker_service as ss

        class _Svc:
            def verify_speaker(self, _q, _claimed):
                return (verified, score)

            @staticmethod
            def embedding_from_base64(enc):
                return ss.SpeakerService.embedding_from_base64(enc)

        monkeypatch.setattr(ss, "get_speaker_service", lambda: _Svc())

    @staticmethod
    def _stub_ticket(monkeypatch, *, user_id):
        """Redis-free ticket. The store has its own tests."""
        from services import voice_second_factor_store as store

        async def _consume(_t, _ip):
            return store.PendingVoiceFactor(user_id=user_id, client_ip=None)

        monkeypatch.setattr(store, "consume_ticket", _consume)

    @staticmethod
    def _on(monkeypatch, **over):
        from utils.config import settings

        base = {"voice_auth_enabled": True, "speaker_recognition_enabled": True,
                "auth_cookie_enabled": True, "voice_auth_min_confidence": 0.7}
        base.update(over)
        for k, v in base.items():
            monkeypatch.setattr(settings, k, v)

    # --- the preconditions, behaviourally ---------------------------------

    async def test_the_route_refuses_while_recognition_is_off(self, db_session, monkeypatch):
        """🛑 The second guard the login page never used to ask about.

        A voiceprint is biometric data (Art. 9 GDPR): with recognition off,
        none is computed for the login either. The test checks BOTH -- the
        refusal, and that no audio leaves.
        """
        from fastapi import Response

        from api.routes import auth as auth_routes
        from services import voice_server_client as vsc

        user = await self._user(db_session)
        self._on(monkeypatch, speaker_recognition_enabled=False)
        self._stub_ticket(monkeypatch, user_id=user.id)

        sent = []

        async def _stt(_audio, **_kw):
            sent.append(1)
            return {"speaker_embedding": [1.0, 0.0], "audio_duration_s": 3.0}

        monkeypatch.setattr(vsc, "stt", _stt)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        assert out.success is False
        assert not out.access_token
        assert not sent, "bei abgeschalteter Erkennung darf kein Audio den Prozess verlassen"

    async def test_a_sample_below_the_server_measured_minimum_is_refused(
        self, db_session, monkeypatch
    ):
        """🛑 H3, now closed. The 1.5 s in the form is guidance for the
        person, not a security check -- an attacker posts straight to the API.
        The only duration to be trusted here is the one the voice-server
        measures itself.
        """
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.99, duration_s=0.3)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        assert out.success is False, "eine 0,3-s-Probe darf nicht anmelden, auch nicht mit 0.99"
        assert not out.access_token

    async def test_a_voice_server_outage_is_not_counted_as_a_failed_attempt(
        self, db_session, monkeypatch
    ):
        """An operational error is not a failed attempt by the person --
        otherwise a voice-server outage locks people out of their account on
        top of everything else."""
        from fastapi import Response

        from api.routes import auth as auth_routes
        from services import voice_server_client as vsc

        user = await self._user(db_session)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)

        async def _boom(_audio, **_kw):
            raise vsc.VoiceServerError("voice-server down")

        monkeypatch.setattr(vsc, "stt", _boom)

        failures = []
        import utils.metrics as metrics

        monkeypatch.setattr(metrics, "record_login_failure", lambda r: failures.append(r))

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        assert out.success is False
        assert not out.access_token
        assert "voice_second_factor" not in failures

    # --- the good case ---------------------------------------------------

    async def test_both_factors_pass_then_tokens_and_cookies(self, db_session, monkeypatch):
        from fastapi import Response

        from api.routes import auth as auth_routes
        from utils.config import settings

        user = await self._user(db_session)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.92)

        resp = Response()
        out = await auth_routes.voice_authenticate(
            self._request(), resp, ticket="t", audio_file=self._upload(), db=db_session)

        assert out.success is True
        assert out.access_token and out.refresh_token
        cookies = resp.headers.getlist("set-cookie")
        assert settings.auth_cookie_name in {c.split("=", 1)[0] for c in cookies}
        assert any("httponly" in c.lower() for c in cookies)

    async def test_it_reports_the_password_gate(self, db_session, monkeypatch):
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session, must_change=True)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.92)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        assert out.success is True
        assert out.must_change_password is True

    # --- the paths that MUST NOT EXIST ----------------------------------

    _FORBIDDEN = ("access_token", "refresh_token")

    def _assert_opaque(self, out, resp=None):
        assert out.success is False
        assert out.message == "Voice authentication failed", (
            "der Text verraet, WELCHE Bedingung fehlschlug"
        )
        for f in self._FORBIDDEN:
            assert getattr(out, f) is None, f"{f} auf einem Fehlschlag gesetzt"
        # 🛑 And the fields that carried the oracle no longer exist in the model.
        for gone in ("speaker_id", "speaker_name", "confidence", "user_id", "username"):
            assert not hasattr(out, gone), (
                f"{gone} ist zurueck im Antwortmodell — das war das Orakel"
            )
        if resp is not None:
            assert resp.headers.getlist("set-cookie") == []

    async def test_no_ticket_means_no_check_at_all(self, db_session, monkeypatch):
        """🛑 The core of the additional factor: without a passed first
        factor you do not even reach the voice check. That puts the old
        oracle's name enumeration out of reach."""
        from fastapi import Response

        from api.routes import auth as auth_routes
        from services import voice_second_factor_store as store

        await self._user(db_session)
        self._on(monkeypatch)

        async def _no(_t, _ip):
            return None

        monkeypatch.setattr(store, "consume_ticket", _no)
        # 🛑 The probe now sits on the VOICE SERVER, not on `SpeakerService`
        # -- that is where the embedding is computed. It makes the test
        # sharper: it proves the recording does not even leave the house
        # before the ticket is valid.
        extracted = []

        from services import voice_server_client as vsc

        async def _stt(_audio, **_kw):
            extracted.append(1)
            return {"speaker_embedding": [1.0, 0.0], "audio_duration_s": 3.0}

        monkeypatch.setattr(vsc, "stt", _stt)

        resp = Response()
        out = await auth_routes.voice_authenticate(
            self._request(), resp, ticket="bogus", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out, resp)
        assert not extracted, "ohne gueltiges Ticket darf kein Audio an den voice-server gehen"

    async def test_a_wrong_voice_is_refused_and_mints_nothing(self, db_session, monkeypatch):
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=False, score=0.31)

        resp = Response()
        out = await auth_routes.voice_authenticate(
            self._request(), resp, ticket="t", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out, resp)

    async def test_a_verified_but_weak_score_is_refused(self, db_session, monkeypatch):
        """The route's own threshold applies on top of `verify_speaker`."""
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session)
        self._on(monkeypatch, voice_auth_min_confidence=0.9)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.75)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out)

    async def test_consent_revoked_between_factors_is_refused(self, db_session, monkeypatch):
        """The ticket is not taken on trust: the consent may have been
        withdrawn between the first and the second factor."""
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session, second_factor=False)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.99)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out)

    async def test_a_deactivated_account_is_refused(self, db_session, monkeypatch):
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session, active=False)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.99)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out)

    async def test_consent_without_a_profile_fails_closed(self, db_session, monkeypatch):
        """Consent without a linked profile: nothing to check against. Do
        not let it through."""
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session, with_speaker=False)
        self._on(monkeypatch)
        self._stub_ticket(monkeypatch, user_id=user.id)
        self._stub_voice(monkeypatch, verified=True, score=0.99)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        self._assert_opaque(out)

    async def test_recognition_off_computes_no_embedding(self, db_session, monkeypatch):
        """An ECAPA voiceprint is biometric data (Art. 9 GDPR)."""
        from fastapi import Response

        from api.routes import auth as auth_routes

        user = await self._user(db_session)
        self._on(monkeypatch, speaker_recognition_enabled=False)
        self._stub_ticket(monkeypatch, user_id=user.id)
        # 🛑 The probe now sits on the VOICE SERVER, not on `SpeakerService`
        # -- that is where the embedding is computed. It makes the test
        # sharper: it proves the recording does not even leave the house
        # before the ticket is valid.
        extracted = []

        from services import voice_server_client as vsc

        async def _stt(_audio, **_kw):
            extracted.append(1)
            return {"speaker_embedding": [1.0, 0.0], "audio_duration_s": 3.0}

        monkeypatch.setattr(vsc, "stt", _stt)

        out = await auth_routes.voice_authenticate(
            self._request(), Response(), ticket="t", audio_file=self._upload(), db=db_session)
        assert out.success is False
        assert not extracted


class TestTheRouteIsNoLongerAnIdentifier:
    """🛑 The guard against sliding back.

    Three findings of the 2026-09-27 review disappear NOT through a check but
    through the shape: the route no longer identifies, it verifies. If it falls
    back to 1:N, the name oracle and the coin toss are back immediately. That
    is why these three assertions check the route body -- in addition to the
    behavioural tests above, not in their place.
    """

    pytestmark = [pytest.mark.unit]

    def test_it_verifies_instead_of_identifying(self):
        from api.routes import auth

        body = _function_body(auth.voice_authenticate)
        assert "verify_speaker" in body, "1:1-Pruefung fehlt"
        assert "identify_speaker" not in body, "zurueck auf Argmax ueber alle Profile"
        assert "build_known_speaker_centroids" not in body, (
            "die Route baut wieder alle Schwerpunkte — das ist 1:N"
        )

    def test_it_requires_a_ticket(self):
        import inspect

        from api.routes import auth

        sig = inspect.signature(auth.voice_authenticate)
        assert "ticket" in sig.parameters, (
            "ohne Ticket waere die Stimme wieder der ERSTE Faktor"
        )

    def test_the_response_model_carries_no_identity(self):
        from api.routes.auth import VoiceAuthResponse

        for gone in ("speaker_id", "speaker_name", "confidence", "user_id", "username"):
            assert gone not in VoiceAuthResponse.model_fields, (
                f"{gone} ist zurueck — genau diese Felder waren das Orakel"
            )

    def test_login_can_withhold_tokens(self):
        """The login path MUST be able to withhold tokens, otherwise the
        second factor is decoration."""
        from api.routes.auth import TokenResponse

        assert TokenResponse.model_fields["access_token"].default is None
        assert "second_factor_ticket" in TokenResponse.model_fields


class TestTheMarginGateIsShared:
    """`match_known_speaker` stays -- for recognition and
    `/api/speakers/identify`.

    After the rework the login does NOT use it any more (it verifies 1:1), but
    the two 1:N paths do, and there the missing runner-up margin was the
    coin-toss finding.
    """

    pytestmark = [pytest.mark.unit]

    @staticmethod
    def _two_close_profiles():
        import numpy as np

        q = np.array([1.0, 0.0], dtype=np.float32)
        a = np.array([1.0, 0.05], dtype=np.float32)
        b = np.array([1.0, 0.10], dtype=np.float32)
        return q, [(1, "Anna", a), (2, "Admin", b)]

    def test_a_coin_flip_is_refused_under_controlled(self, monkeypatch):
        from services.speaker_resolver import match_known_speaker

        monkeypatch.setattr("utils.config.settings.speaker_recognition_threshold", 0.25)
        monkeypatch.setattr("utils.config.settings.speaker_match_min_margin", 0.1)
        q, known = self._two_close_profiles()

        mid, best, runner = match_known_speaker(q, known, controlled=True)
        assert best >= 0.25, "Voraussetzung: beide ueber der Schwelle"
        assert (best - runner) < 0.1, "Voraussetzung: zu nah beieinander"
        assert mid is None, "ein Muenzwurf darf NICHT zuordnen"

    def test_without_controlled_it_is_plain_argmax(self, monkeypatch):
        from services.speaker_resolver import match_known_speaker

        monkeypatch.setattr("utils.config.settings.speaker_recognition_threshold", 0.25)
        monkeypatch.setattr("utils.config.settings.speaker_match_min_margin", 0.1)
        q, known = self._two_close_profiles()

        mid, _b, _r = match_known_speaker(q, known, controlled=False)
        assert mid is not None, "ohne kontrollierte Erkennung gilt die Marge nicht"


@pytest.mark.database
class TestLoginWithholdsTokensForSecondFactor:
    """🛑 The core of the additional factor, and the place where it would
    fail.

    A second factor that comes after the first has already granted access is
    not one. For an account with `voice_second_factor_enabled`, `/auth/login`
    must therefore issue NO tokens and NO cookies, only a ticket. Remove that
    branch and the voice is decoration -- and nobody notices, because the login
    then simply feels normal.
    """

    @pytest.fixture(autouse=True)
    def _no_rate_limit(self, monkeypatch):
        """Rate limiter off, because these tests call the handler DIRECTLY.

        The same pattern as `test_auth.py:280` ("its wrapper guards everything
        behind `if self.enabled`"): a direct call should not hang on a counter
        that other tests fill, and slowapi reaches for
        `request.app.state.limiter` (`extension.py:83`), which a hand-built
        `Request` does not have.

        🛑 A correction to myself: this first said the bypass fixed the four
        failures in the full suite. That was WRONG -- it did not fix them, the
        cause was the name binding in `_own_session`. A comment that claims a
        disproven cause costs the next reader the same journey a second time.
        The bypass stays because it is right on its own merits; what it costs
        is recovered by `TestTheRateLimitIsStillDeclared`.
        """
        from services.api_rate_limiter import limiter

        monkeypatch.setattr(limiter, "enabled", False)

    @pytest.fixture(autouse=True)
    def _own_session(self, monkeypatch, db_session):
        """🛑 Redirect BOTH names -- and that is the actual finding.

        `DBProvider` opens its OWN session (the providers' contract carries no
        `db`), and `auth/providers/db.py:18` gets it via
        `from services.database import AsyncSessionLocal` -- a name BOUND AT
        MODULE LEVEL. A `monkeypatch.setattr` on `services.database` does NOT
        reach that binding once the module has been imported.

        Four tests hung on exactly this, and the failure mode was insidious:

        * **On its own**, `auth.providers.db` is not yet imported when the
          fixture runs. The patch lands first, the later import COPIES the
          patched value -- the tests were green.
        * **In the suite**, an earlier test loaded the module long ago. It
          holds the real factory, the provider looks for the test user in the
          wrong database, does not find them, and the login ends in
          `bad credentials` -- which looks like a product defect.

        So both bindings. And not only the one that currently hurts:
        `services.database` stays patched, because other paths look it up at
        runtime.
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        import auth.providers.db as db_provider
        import services.database as db_mod

        factory = async_sessionmaker(
            db_session.bind, class_=AsyncSession, expire_on_commit=False,
        )
        monkeypatch.setattr(db_mod, "AsyncSessionLocal", factory)
        monkeypatch.setattr(db_provider, "AsyncSessionLocal", factory)

    @staticmethod
    def _request():
        from starlette.requests import Request

        return Request({
            "type": "http", "method": "POST", "path": "/api/auth/login",
            "raw_path": b"/api/auth/login", "root_path": "", "scheme": "http",
            "query_string": b"", "headers": [], "client": ("127.0.0.1", 12345),
            "server": ("test", 80), "app": None,
        })

    @staticmethod
    def _form(username="anna2fa", password="pw"):
        class _F:
            pass

        f = _F()
        f.username = username
        f.password = password
        return f

    async def _user(self, db, *, second_factor: bool):
        """🛑 UNIQUE names per run, and that is not a style point.

        The test database is real, shared Postgres, and the login lockout lives
        in real Redis. With fixed names a run carries the previous run's state
        with it: failed attempts of my own locked `anna2fa`, and after that ALL
        tests in this class failed with a 401 -- a failure picture that looks
        like a product defect and is not one. The same trap as in
        `test_pc20260423_migration.py`; solved there by the same means.
        """
        import uuid

        from models.database import Role, Speaker, SpeakerEmbedding, User
        from services.auth_service import get_password_hash

        tag = uuid.uuid4().hex[:8]
        role = Role(name=f"L2F-{tag}", description="", permissions=["chat.own"])
        db.add(role)
        await db.flush()

        # 🛑 WITH a profile and an embedding, and that is not incidental.
        #
        # This class used to prove "the login withholds the tokens" on a user
        # WITHOUT a speaker profile -- that is, on one who could never have
        # redeemed the second factor. Under the old code that was exactly the
        # lockout, and the test celebrated it as a success. Now that the hurdle
        # only stands when it can also fall, the proof requires a user who
        # would genuinely get through.
        speaker = Speaker(name=f"stimme-{tag}")
        db.add(speaker)
        await db.flush()
        db.add(SpeakerEmbedding(speaker_id=speaker.id, embedding="AAAA"))

        user = User(username=f"anna-{tag}",
                    password_hash=get_password_hash("pw"), role_id=role.id,
                    is_active=True, token_epoch=0,
                    speaker_id=speaker.id,
                    voice_second_factor_enabled=second_factor)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user

    @staticmethod
    def _stub_ticket(monkeypatch, *, ticket="TICKET"):
        from services import voice_second_factor_store as store

        async def _issue(_uid, _ip):
            return ticket

        monkeypatch.setattr(store, "issue_ticket", _issue)

    async def test_it_returns_a_ticket_and_no_tokens(self, db_session, monkeypatch):
        from fastapi import Response

        from api.routes import auth as auth_routes
        from utils.config import settings

        user = await self._user(db_session, second_factor=True)
        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "auth_cookie_enabled", True)
        self._stub_ticket(monkeypatch)

        resp = Response()
        out = await auth_routes.login(
            self._request(), form_data=self._form(user.username), db=db_session,
            response=resp)

        assert out.second_factor == "voice"
        assert out.second_factor_ticket == "TICKET"
        assert out.access_token is None, "der erste Faktor hat Token ausgegeben"
        assert out.refresh_token is None
        assert resp.headers.getlist("set-cookie") == [], (
            "der erste Faktor hat eine Sitzung eingerichtet — dann sperrt die "
            "Stimme nichts mehr"
        )

    async def test_a_normal_account_is_untouched(self, db_session, monkeypatch):
        """The flag-off path has to stay byte-identical."""
        from fastapi import Response

        from api.routes import auth as auth_routes
        from utils.config import settings

        user = await self._user(db_session, second_factor=False)
        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "auth_cookie_enabled", True)

        resp = Response()
        out = await auth_routes.login(
            self._request(), form_data=self._form(user.username), db=db_session,
            response=resp)
        assert out.access_token and out.refresh_token
        assert out.second_factor is None
        assert resp.headers.getlist("set-cookie") != []

    async def test_with_the_voice_route_off_the_hurdle_rests(self, db_session, monkeypatch):
        """🛑 Otherwise the account would be locked out: the hurdle only
        applies where there is also a door. With `VOICE_AUTH_ENABLED` off
        nobody could redeem the ticket -- so the password path carries on
        normally and the consent lies dormant."""
        from fastapi import Response

        from api.routes import auth as auth_routes
        from utils.config import settings

        user = await self._user(db_session, second_factor=True)
        monkeypatch.setattr(settings, "voice_auth_enabled", False)
        monkeypatch.setattr(settings, "auth_cookie_enabled", True)

        out = await auth_routes.login(
            self._request(), form_data=self._form(user.username), db=db_session,
            response=Response())
        assert out.access_token, "ohne Sprachweg muss der Passwortpfad durchlassen"
        assert out.second_factor is None

    async def test_no_ticket_means_no_login_at_all(self, db_session, monkeypatch):
        """🛑 Fail-closed. A Redis outage must not quietly remove the second
        factor -- it has to refuse the login."""
        from fastapi import HTTPException, Response

        from api.routes import auth as auth_routes
        from services import voice_second_factor_store as store
        from utils.config import settings

        user = await self._user(db_session, second_factor=True)
        monkeypatch.setattr(settings, "voice_auth_enabled", True)

        async def _none(_uid, _ip):
            return None

        monkeypatch.setattr(store, "issue_ticket", _none)

        with pytest.raises(HTTPException) as ei:
            await auth_routes.login(
                self._request(), form_data=self._form(user.username),
                db=db_session, response=Response())
        assert ei.value.status_code == 503, (
            "ohne Ticket darf es weder Token noch eine stille Umgehung geben"
        )


    async def test_without_a_usable_profile_the_hurdle_rests(self, db_session, monkeypatch):
        """🛑 The counter-control to the whole class.

        Six routes can remove the speaker profile after the consent. Previously
        the login still withheld the tokens afterwards and `/auth/voice`
        refused fail-closed: the account was shut without anybody having done
        anything wrong. Now the hurdle lies dormant, the password path stays
        open, and the consent stays STORED -- it is the record of a declaration
        by the person, not a switch the system may quietly flip.
        """
        from fastapi import Response

        from api.routes import auth as auth_routes
        from utils.config import settings

        user = await self._user(db_session, second_factor=True)
        user.speaker_id = None
        await db_session.commit()

        monkeypatch.setattr(settings, "voice_auth_enabled", True)
        monkeypatch.setattr(settings, "auth_cookie_enabled", False)

        out = await auth_routes.login(
            self._request(), form_data=self._form(user.username), db=db_session,
            response=Response())

        assert out.access_token, "ohne einloesbaren Faktor darf das Passwort wieder genuegen"
        assert out.second_factor is None
        await db_session.refresh(user)
        assert user.voice_second_factor_enabled is True, "die Einwilligung bleibt stehen"


class TestTheRateLimitIsStillDeclared:
    """🛑 What the bypass costs is recovered here.

    `TestVoiceSecondFactor` and `TestLoginWithholdsTokensForSecondFactor` switch
    the rate limiter off, because they call the handlers DIRECTLY (otherwise
    slowapi reaches for `request.app.state.limiter`, which a hand-built request
    does not have). But that also means no test checks any more that the limit
    is still ATTACHED to the routes -- and both are reachable without signing
    in.

    Without this guard somebody could remove the decorator and all tests would
    stay green. That is exactly the gap a workaround typically tears open
    without anybody noticing.
    """

    pytestmark = [pytest.mark.unit]

    @pytest.mark.parametrize("route", ["login", "voice_authenticate"])
    def test_the_auth_routes_carry_the_rate_limiter(self, route):
        from api.routes import auth

        fn = getattr(auth, route)
        # slowapi wraps the handler with `functools.wraps`; the chain is
        # reachable through `__wrapped__`. If it is empty, the decorator is
        # gone.
        assert hasattr(fn, "__wrapped__"), (
            f"`{route}` traegt keinen Dekorator mehr — die Ratenbegrenzung auf "
            f"einer unangemeldet erreichbaren Anmelderoute ist weg"
        )

    def test_the_auth_limit_is_stricter_than_the_default(self):
        """The login routes run on `api_rate_limit_auth`, not on the general
        value -- otherwise the stricter limit would be mere decoration."""
        from utils.config import settings

        def _per_minute(spec: str) -> float:
            n, _, unit = spec.partition("/")
            factor = {"second": 60.0, "minute": 1.0, "hour": 1 / 60, "day": 1 / 1440}
            return float(n) * factor[unit.strip().rstrip("s") or "minute"]

        assert _per_minute(settings.api_rate_limit_auth) <= _per_minute(
            settings.api_rate_limit_default
        ), "die Anmeldegrenze ist nicht strenger als die allgemeine"
