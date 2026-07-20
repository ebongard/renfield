"""Browser E2E for Notes (Phase 4B) — authenticated, real backend.

Runs against an auth-on target (e.g. xidra) using credentials from the
environment ONLY — never hard-coded or on a URL. Skips cleanly on the auth-off
household (no creds set). Invoke:

    E2E_USERNAME=evdb \
    E2E_PASSWORD="$(kubectl -n renfield-xidra get secret renfield-secrets \
        -o jsonpath='{.data.default-admin-password}' | base64 -d)" \
    E2E_BASE_URL=https://x-ren.local \
    ./bin/run-e2e.sh tests/e2e/areas/test_notes.py

Exercises the full round trip through the real UI + backend + DB: the nav entry,
create, the [[link]] backlink panel, and inline-confirm delete. Uses unique
titles + tears its own notes down so it is safe to re-run against a live
instance. The `[[link]]` + backlink assertions are the load-bearing bit — they
prove the KG-substrate link path works end to end for a real logged-in user.
"""
import pytest

from tests.e2e.conftest import BASE_URL

pytestmark = pytest.mark.usefixtures("require_e2e_auth")

# Unique, self-identifying titles so a re-run never collides with real data and
# a stray leftover is obvious. Kept short (title max 255).
_SFX = "e2e-notes-verify"
_ALPHA = f"Alpha {_SFX}"
_BETA = f"Beta {_SFX}"


def _create_note(page, title: str, body: str) -> None:
    """Fill the create form and submit; wait for the card to appear."""
    page.get_by_placeholder("Titel").fill(title)
    page.get_by_placeholder("Inhalt (Markdown)").fill(body)
    page.get_by_role("button", name="Notiz anlegen").click()
    # The new card renders its title as an <h3>.
    page.get_by_role("heading", name=title).wait_for(timeout=15_000)


def _delete_note(page, title: str) -> None:
    """Inline-confirm delete of the card whose heading is `title`."""
    card = page.locator(".card").filter(has=page.get_by_role("heading", name=title))
    card.get_by_role("button", name="Löschen", exact=True).click()
    card.get_by_role("button", name="Löschen bestätigen").click()
    page.get_by_role("heading", name=title).wait_for(state="detached", timeout=15_000)


def test_notes_nav_visible(authenticated_page):
    """The Notizen nav entry renders for an authenticated user (feature on)."""
    authenticated_page.goto(f"{BASE_URL}/notes", wait_until="networkidle", timeout=20_000)
    # PageHeader title confirms the route rendered (not a login redirect).
    authenticated_page.get_by_role("heading", name="Notizen").first.wait_for(timeout=15_000)
    assert authenticated_page.get_by_role("link", name="Notizen").first.is_visible()


def test_notes_create_link_backlink_delete(authenticated_page):
    """Create two notes, link Alpha -> [[Beta]], verify the backlink on Beta,
    then delete both. Full KG-substrate link round trip through the real UI."""
    page = authenticated_page
    page.goto(f"{BASE_URL}/notes", wait_until="networkidle", timeout=20_000)
    page.get_by_role("heading", name="Notizen").first.wait_for(timeout=15_000)

    # Defensive cleanup of any leftover from a previous aborted run.
    for title in (_ALPHA, _BETA):
        if page.get_by_role("heading", name=title).count():
            _delete_note(page, title)

    _create_note(page, _BETA, "Ziel-Notiz")
    _create_note(page, _ALPHA, f"siehe [[{_BETA}]] für Details")

    # Alpha's card shows an outgoing link chip to Beta.
    alpha_card = page.locator(".card").filter(has=page.get_by_role("heading", name=_ALPHA))
    outgoing = alpha_card.get_by_text("Verlinkt:", exact=False)
    outgoing.wait_for(timeout=10_000)
    assert alpha_card.get_by_text(_BETA, exact=True).count() >= 1

    # Beta's card shows a backlink from Alpha (the KG note_link edge resolved).
    beta_card = page.locator(".card").filter(has=page.get_by_role("heading", name=_BETA))
    beta_card.get_by_text("Verlinkt von:", exact=False).wait_for(timeout=10_000)
    assert beta_card.get_by_text(_ALPHA, exact=True).count() >= 1

    # Tear down (also proves inline-confirm delete works).
    _delete_note(page, _ALPHA)
    _delete_note(page, _BETA)
