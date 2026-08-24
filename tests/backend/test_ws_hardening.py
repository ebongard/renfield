"""WebSocket hardening (login audit #1116, Tier-2 Batch B).

#6 — inbound WS frame size cap. The prod uvicorn launch
(``k8s/backend.yaml`` args + the Dockerfile CMD) passes
``--ws-max-size 1000000`` so an oversized frame is dropped at the protocol
layer before any /ws handler parses it; ``main.py``'s direct uvicorn.run uses
``settings.ws_max_message_size``. All legitimate inbound frames are small
(chat = text + attachment IDs; attachments upload via REST; audio streams in
~KB chunks), so 1 MB is generous.
"""
from utils.config import settings


def test_ws_max_message_size_value():
    """Guard the value the launch args hardcode: the k8s/Dockerfile
    ``--ws-max-size`` is a literal 1000000 (CLI args can't read the config), so
    if this config changes the launch args (k8s/backend.yaml + Dockerfile) must
    be updated in lockstep. main.py's uvicorn.run reads the config directly."""
    assert settings.ws_max_message_size == 1_000_000
