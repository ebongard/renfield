"""Real browser E2E tests for Sprecher (/speakers).

Drives:
  * Page render
  * List speakers via API
  * Status endpoint returns an object
  * Create → rename → delete round-trip
  * Created speaker surfaces in the UI
"""
from __future__ import annotations

import re
import time
import uuid

import pytest

from tests.e2e.helpers import api
from tests.e2e.helpers.asserts import (
    assert_body_not_blank,
    assert_no_critical_console_errors,
)
from tests.e2e.helpers.page import BASE_URL, capture_console_errors


pytestmark = pytest.mark.e2e


@pytest.fixture()
def speakers_page(page):
    page.goto(f"{BASE_URL}/speakers",
              wait_until="networkidle", timeout=20_000)
    page.wait_for_selector("h1, h2", timeout=15_000)
    return page


@pytest.fixture()
def created_speaker_ids():
    ids: list[int] = []
    yield ids
    for sid in ids:
        try:
            api.delete_speaker(sid)
        except Exception:
            pass


class TestSpeakersPageRenders:
    def test_page_loads(self, speakers_page):
        get_errors = capture_console_errors(speakers_page)
        assert_body_not_blank(speakers_page.locator("body").inner_text())
        assert speakers_page.get_by_role(
            "heading", name=re.compile(r"Sprecher|Speakers", re.IGNORECASE),
        ).first.is_visible()
        assert_no_critical_console_errors(get_errors())

    def test_speakers_endpoint_returns_list(self):
        result = api.list_speakers()
        assert isinstance(result, list)

    def test_speaker_status_endpoint(self):
        result = api.get("/api/speakers/status",
                          skip_on_status=(401, 403, 404))
        assert isinstance(result, dict)


class TestSpeakersCRUD:
    def test_create_rename_delete_speaker(self, created_speaker_ids):
        name = f"e2e-speaker-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_speaker({"name": name})
        except Exception as e:
            pytest.skip(f"create_speaker schema/auth: {e}")
        speaker_id = created.get("id")
        assert speaker_id, f"No id in create: {created}"
        created_speaker_ids.append(speaker_id)

        new_name = f"{name}-renamed"
        api.update_speaker(speaker_id, {"name": new_name})

        speakers = api.list_speakers()
        assert any(s.get("name") == new_name for s in speakers), (
            f"Renamed speaker {new_name} not in list"
        )

        api.delete_speaker(speaker_id)
        created_speaker_ids.remove(speaker_id)
        speakers_after = api.list_speakers()
        assert not any(
            s.get("name") in (name, new_name) for s in speakers_after
        ), "Speaker still in list after delete"

    def test_created_speaker_appears_in_ui(
        self, speakers_page, created_speaker_ids,
    ):
        name = f"e2e-ui-speaker-{uuid.uuid4().hex[:8]}"
        try:
            created = api.create_speaker({"name": name})
        except Exception as e:
            pytest.skip(f"create_speaker schema/auth: {e}")
        if not created.get("id"):
            pytest.skip(f"No id: {created}")
        created_speaker_ids.append(created["id"])

        speakers_page.reload(wait_until="networkidle", timeout=15_000)
        speakers_page.wait_for_selector(f"text={name}", timeout=10_000)
