"""Canonical nav routes for the Renfield frontend.

Single source of truth for every area's URL path + human label + a
quick-check selector that proves the page actually rendered (not a
generic error/loading shell). The selectors are deliberately tolerant:
we want "smoke" coverage that fails on "page blank" or "wrong page
loaded", not on minor DOM shuffles.

When adding a new page:
  1. Add a matching Area entry below.
  2. Drop a `test_<area>.py` in tests/e2e/areas/ using the same key.

Navigation labels come from src/frontend/src/i18n/locales/de.json —
the main consumer is the German UI.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Area:
    key: str                 # short stable id (matches test filename suffix)
    path: str                # URL path under BASE_URL
    label: str               # human label in nav (for heading / reference)
    ready_selector: str      # CSS/text selector that proves the page loaded
    admin: bool = False      # gated behind Admin nav group


AREAS: tuple[Area, ...] = (
    # === Primary nav ===
    Area("chat",                "/",                 "Chat",
         ready_selector="textarea[placeholder*='Nachricht'], textarea"),
    Area("knowledge",           "/knowledge",        "Wissen",
         ready_selector="h1, h2"),
    Area("brain",               "/brain",            "Zweites Gehirn",
         ready_selector="h1, h2"),
    Area("brain_review",        "/brain/review",     "Überprüfung",
         ready_selector="h1, h2"),
    Area("federation_audit",    "/brain/audit",      "Föderations-Verlauf",
         ready_selector="h1, h2"),
    Area("memory",              "/memory",           "Erinnerungen",
         ready_selector="h1, h2"),
    Area("knowledge_graph",     "/knowledge-graph",  "Wissensgraph",
         ready_selector="h1, h2, svg"),
    Area("tasks",               "/tasks",            "Aufgaben",
         ready_selector="h1, h2"),
    Area("camera",              "/camera",           "Kameras",
         ready_selector="h1, h2"),
    Area("rooms",               "/rooms",            "Räume",
         ready_selector="h1, h2"),
    Area("speakers",            "/speakers",         "Sprecher",
         ready_selector="h1, h2"),
    Area("smart_home",          "/homeassistant",    "Smart Home",
         ready_selector="h1, h2"),
    Area("settings_circles",    "/settings/circles", "Kreise",
         ready_selector="h1, h2"),

    # === Admin-gated ===
    Area("admin_integrations",  "/admin/integrations",   "Integrationen",
         ready_selector="h1, h2", admin=True),
    Area("admin_intents",       "/admin/intents",        "Intents",
         ready_selector="h1, h2", admin=True),
    Area("admin_routing",       "/admin/routing",        "Routing Dashboard",
         ready_selector="h1, h2", admin=True),
    Area("admin_users",         "/admin/users",          "Benutzer",
         ready_selector="h1, h2", admin=True),
    Area("admin_roles",         "/admin/roles",          "Rollen",
         ready_selector="h1, h2", admin=True),
    Area("admin_satellites",    "/admin/satellites",     "Satellites",
         ready_selector="h1, h2", admin=True),
    Area("admin_presence",      "/admin/presence",       "Anwesenheit",
         ready_selector="h1, h2", admin=True),
    Area("admin_paperless_audit", "/admin/paperless-audit", "Paperless Audit",
         ready_selector="h1, h2", admin=True),
    Area("admin_maintenance",   "/admin/maintenance",    "Wartung",
         ready_selector="h1, h2", admin=True),
    Area("admin_settings",      "/admin/settings",       "Einstellungen",
         ready_selector="h1, h2", admin=True),
)


AREAS_BY_KEY: dict[str, Area] = {a.key: a for a in AREAS}


def get(key: str) -> Area:
    """Look up an Area by key — raises KeyError with a helpful message."""
    try:
        return AREAS_BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"Unknown area {key!r}. Known areas: {sorted(AREAS_BY_KEY)}"
        ) from None
