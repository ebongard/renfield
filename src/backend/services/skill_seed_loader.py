"""
SkillSeedLoader — boot-time loader for src/backend/seed_skills/*.md.

Seed skills are system-owned, public-tier procedural skills shipped in
the git repo. They give the agent a baseline of Renfield-specific
procedures before any auto-extraction has happened.

File format — YAML front-matter + markdown body, parsed loosely (we
don't pull in PyYAML for this; the front-matter is a fixed-schema
key:value block delimited by ``---`` lines):

    ---
    title: Album auf DLNA-Renderer abspielen
    triggers:
      - "spiel das Album X im Wohnzimmer"
      - "Album X im Wohnzimmer"
    tools:
      - mcp.media.search_media
      - internal.play_album_on_dlna
    ---
    - Schritt 1: ...
    - Schritt 2: ...

Idempotent: SkillService.load_seed dedupes by title within source="seed",
so re-running the loader at every boot is safe.
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from services.skill_service import SkillService
from utils.config import settings


_FRONT_MATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL
)


def _parse_seed_file(path: Path) -> dict | None:
    """Parse a single .md seed file. Returns dict with title/triggers/tools/body
    or None on malformed input."""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"⚠️ Seed skill {path.name}: read failed: {e}")
        return None

    # Normalize line endings so a CRLF checkout (Windows contributor with
    # git's autocrlf=true) doesn't make the regex see stray \r characters
    # that pollute the body and break the title-dedup in load_seed.
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    m = _FRONT_MATTER_RE.match(raw)
    if not m:
        logger.warning(f"⚠️ Seed skill {path.name}: missing YAML-ish front-matter")
        return None

    fm_text, body = m.group(1), m.group(2).strip()

    # Hand-rolled tiny parser — only the three keys we know about.
    title: str | None = None
    triggers: list[str] = []
    tools: list[str] = []

    current_list: list[str] | None = None
    for line in fm_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("  - ") and current_list is not None:
            current_list.append(line.strip()[2:].strip().strip('"').strip("'"))
            continue
        if line.startswith("- ") and current_list is not None:
            current_list.append(line.strip()[2:].strip().strip('"').strip("'"))
            continue
        # Reset list-mode on a new key
        current_list = None
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip()
        if key == "title":
            title = val.strip().strip('"').strip("'")
        elif key == "triggers":
            current_list = triggers
            if val:  # inline single-trigger form
                triggers.append(val.strip('"').strip("'"))
        elif key == "tools":
            current_list = tools
            if val:
                tools.append(val.strip('"').strip("'"))

    if not (title and body and triggers):
        logger.warning(
            f"⚠️ Seed skill {path.name}: missing required title/triggers/body"
        )
        return None

    return {
        "title": title,
        "trigger_examples": triggers,
        "tool_sequence": tools,
        "body_md": body,
    }


async def load_all_seeds(db: AsyncSession) -> int:
    """Load every .md file in settings.skill_seed_directory.

    Returns the count of NEWLY loaded seeds (existing seeds with the same
    title are skipped by SkillService.load_seed).
    """
    if not settings.skill_seed_load_on_boot:
        return 0
    if not settings.skills_enabled:
        return 0

    # The seed directory is relative to the backend root (src/backend).
    backend_root = Path(__file__).resolve().parents[1]
    seed_dir = backend_root / settings.skill_seed_directory
    if not seed_dir.is_dir():
        logger.debug(f"🌱 Seed skill directory {seed_dir} not present, skipping")
        return 0

    service = SkillService(db)
    loaded = 0
    for md_path in sorted(seed_dir.glob("*.md")):
        parsed = _parse_seed_file(md_path)
        if parsed is None:
            continue
        result = await service.load_seed(
            title=parsed["title"],
            body_md=parsed["body_md"],
            trigger_examples=parsed["trigger_examples"],
            tool_sequence=parsed["tool_sequence"],
        )
        if result is not None:
            loaded += 1

    if loaded > 0:
        logger.info(f"🌱 Loaded {loaded} new seed skill(s) from {seed_dir}")
    return loaded
