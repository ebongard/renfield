"""Renfield voice-server.

GPU-resident voice-tier microservice: faster-whisper STT (streaming),
Piper TTS (sentence-streaming), ECAPA-TDNN speaker embeddings.
Stateless — no DB, no Redis. Frontend talks directly via /ws/voice.

See docs/VOICE_PIPELINE_DESIGN.md § "Phase B" for the full architecture.
"""

# Kept in sync with the image tag by bin/release-voice-server.sh (extraction
# plan T4). 0.3.0 = registry auth mode; 0.3.1 = X-Verify-Secret header (T11);
# 0.3.2 = anon_default_client (household migration T31).
__version__ = "0.3.6"
