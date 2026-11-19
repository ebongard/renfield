"""The test process must not inherit a real Paperless or reach the HF hub (#1577).

The suite runs inside the dev stack's backend container. With its
PAPERLESS_API_URL/_TOKEN inherited, upload tests polled a real Paperless for a
made-up task id until the 30 s deadline, and folder-ingest tests searched its
documents. ``tests/backend/conftest.py`` removes both before ``settings`` is
built; this pins that.
"""

import os

import pytest

pytestmark = pytest.mark.unit


def test_no_paperless_endpoint_reaches_a_test():
    from utils.config import settings

    # Names only: an `assert ... in os.environ` would print the whole
    # environment on failure, DATABASE_URL with its password included.
    present = sorted({"PAPERLESS_API_URL", "PAPERLESS_API_TOKEN"} & set(os.environ))
    assert present == []
    # `settings` read the environment once, at import -- after the removal.
    assert not settings.paperless_api_url


def test_the_hugging_face_hub_is_offline():
    offline = os.environ.get("HF_HUB_OFFLINE")
    assert offline == "1"
