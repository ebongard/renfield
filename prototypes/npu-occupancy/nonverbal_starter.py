"""Non-verbal starter — hand-gesture (Head A) + facial-expression (Head B) — prototype.

IMPLEMENTS the buildable near-term slice of the EXISTING, spike-validated design:
`docs/design/non-verbal-communication.md` (Phase 3a). This is NOT a new design — read
that doc first; the decisions (fail-closed actuation, advisory-only affect, nothing
persisted, gated bounded-window capture, BLE-single-occupant attribution) are binding.

DIFFERENT inference stack from occupancy/object recognition: this is the MediaPipe
LANDMARK pipeline, which the T-SILICON-PROBE spike found runs on the A733's **A76 CPU**
(~3-5 fps), NOT the NPU — MediaPipe Tasks' Delegate is CPU/GPU only; an NPU port is a
separate deferred Verisilicon TFLite→NBG conversion (the very rail the occupancy
prototype demonstrates, but out of scope for Phase 3a). So: CPU here, on purpose.

  Head A — STATIC command gestures (ships now, zero training): stock MediaPipe
           GestureRecognizer → {Open_Palm=palm-stop, Thumb_Up=confirm, Thumb_Down=reject,
           finger-count 1-2 = pick option N}. MOTION gestures (wave/swipe/volume) are
           Phase 3b (a Jester-trained temporal model, landmark-only) — not here.
  Head B — FACIAL EXPRESSION / affect (deferred; scaffolded advisory-only): MediaPipe
           FaceLandmarker blendshapes → a bounded affect enum. NEVER actuates; shapes
           tone/verbosity only, fail-open. Gated behind Head A + a read-quality eval.

Actuation is fail-closed and reuses the device-widget gate VERBATIM (Decision 6): a
recognized command → a `device_action`-style frame → `HA_CONTROL`-gated in chat_handler →
server re-validated → `_HANDLERS`-only internal tool. A gesture grants nothing the user
lacks; unidentified / not-single-occupant → read-only.
"""
from __future__ import annotations

from dataclasses import dataclass

# Static gesture → Renfield intent (config-driven in prod; Phase-3a subset from the doc).
STATIC_GESTURE_INTENTS: dict[str, str] = {
    "Open_Palm": "cancel_pending",       # palm-stop → stop action / clear pending confirm
    "Thumb_Up": "confirm_pending",       # → confirm the staged device_action / Paperless card
    "Thumb_Down": "reject_pending",      # → cancel the staged confirm
    # finger-count handled separately (pick option N) via landmark finger counting
}

# Head B: MediaPipe FaceLandmarker blendshape → bounded affect (Decision: bounded enums,
# no free-form, discarded each window). Advisory only; fail-open on low confidence.
_AFFECT_ORDER = ("frustrated", "confused", "pleased", "neutral")


@dataclass
class NonVerbalConfig:
    gesture_model: str          # gesture_recognizer.task (MediaPipe bundle)
    face_model: str             # face_landmarker.task (blendshapes on)
    conf: float = 0.60          # per-gesture confidence floor (Decision D7)
    debounce_frames: int = 3    # N-frame debounce before a command fires (D7)


class NonVerbalReader:
    """Bounded-window reader: fed frames while a capture window is open (gesture-gated per
    T2 — trigger → window → sleep), emits at most one debounced command + an advisory
    affect read. Holds no history across windows (Decision 7: nothing persisted)."""

    def __init__(self, cfg: NonVerbalConfig) -> None:
        from mediapipe.tasks import python as mp
        from mediapipe.tasks.python import vision
        self._vision = vision
        base = mp.BaseOptions
        self._greco = vision.GestureRecognizer.create_from_options(
            vision.GestureRecognizerOptions(base_options=base(model_asset_path=cfg.gesture_model),
                                            running_mode=vision.RunningMode.VIDEO, num_hands=2))
        self._face = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(base_options=base(model_asset_path=cfg.face_model),
                                         output_face_blendshapes=True,
                                         running_mode=vision.RunningMode.VIDEO))
        self._cfg = cfg
        self._streak: dict[str, int] = {}

    # ── Head A ────────────────────────────────────────────────────────────────
    def gesture(self, mp_image, ts_ms: int) -> str | None:
        """→ a debounced intent when a static gesture is held for N frames, else None.
        Returns the INTENT string; the caller routes it through the fail-closed gate —
        this function never actuates."""
        res = self._greco.recognize_for_video(mp_image, ts_ms)
        top = res.gestures[0][0] if res.gestures and res.gestures[0] else None
        if not top or top.category_name == "None" or top.score < self._cfg.conf:
            self._streak.clear()
            return None
        name = top.category_name
        self._streak = {name: self._streak.get(name, 0) + 1}     # only the current gesture streaks
        if self._streak[name] < self._cfg.debounce_frames:
            return None
        self._streak.clear()                                     # one fire per hold (cooldown)
        return STATIC_GESTURE_INTENTS.get(name)

    # ── Head B (advisory; deferred) ─────────────────────────────────────────────
    def affect(self, mp_image, ts_ms: int) -> tuple[str, float] | None:
        """→ (affect, confidence) or None. Maps ARKit-style blendshapes to the doc's
        bounded affect enum. ADVISORY: never actuates, only shapes {nonverbal_context}."""
        res = self._face.detect_for_video(mp_image, ts_ms)
        if not res.face_blendshapes:
            return None
        bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
        smile = max(bs.get("mouthSmileLeft", 0), bs.get("mouthSmileRight", 0))
        brow_down = max(bs.get("browDownLeft", 0), bs.get("browDownRight", 0))
        brow_up = bs.get("browInnerUp", 0)
        scores = {
            "frustrated": brow_down,
            "confused": brow_up * (1 - smile),
            "pleased": smile,
            "neutral": 0.35,                                     # prior; wins on weak signals
        }
        affect = max(_AFFECT_ORDER, key=lambda a: scores[a])
        conf = scores[affect]
        return (affect, conf) if conf >= 0.5 else ("neutral", conf)

    def close(self) -> None:
        self._greco.close(); self._face.close()


# ── Streaming / privacy (mirror non-verbal-communication.md §"Streaming protocol") ──────
#
# WS mirrors capture_snapshot/bt_scan_request; a SEPARATE gesture WS that MUST inherit the
# satellite enrollment-PSK + fleet-state machine (Decision D6 — else it reopens the H1
# "LAN device claims a satellite_id", now streaming video).
#
#   register       += {gesture_capable: bool, gesture_tier: "ondevice"|"video"}
#   backend→sat     : gesture_stream_start / gesture_stream_stop   (gate the window)
#   sat→backend     : landmark_frame {ts, hands[], pose[], face_blendshapes[]}  ← Tier-1
#                     COORDINATES/SCORES ONLY — raw video NEVER leaves the room (privacy spine)
#   result→agent    : recognized intent → device_action-style frame through the EXISTING
#                     HA_CONTROL fail-closed gate;  affect → {nonverbal_context} prompt line
#
# Binding invariants from the doc (do not soften in the productization PR):
#   * Nothing persisted (Decision 7): no raw video, no landmark history, no affect log.
#   * LED "vision active" tell while the window is open (LEDController.set_pattern).
#   * Attribution fail-closed (T-ATTRIB-SPIKE): actuate only when is_user_alone_in_room()
#     identifies a single BLE-tracked occupant; multi-person / unidentified → read-only.
#   * Safe-action allowlist = REVERSIBLE actions only; no irreversible action via gesture
#     without a voice/tap confirm (D7).
#   * kinderbad needs its OWN opt-in flag (Decision 8) — never inherits the camera flag.
