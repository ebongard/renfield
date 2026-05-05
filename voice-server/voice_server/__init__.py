"""Renfield voice-server.

GPU-resident voice-tier microservice: faster-whisper STT (streaming),
Piper TTS (sentence-streaming), ECAPA-TDNN speaker embeddings.
Stateless — no DB, no Redis. Frontend talks directly via /ws/voice.

See docs/VOICE_PIPELINE_DESIGN.md § "Phase B" for the full architecture.
"""

__version__ = "0.1.0"
