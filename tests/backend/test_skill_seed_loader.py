"""Unit tests for SkillSeedLoader.

Focus on the file parser (pure Python — no DB, no LLM). The DB-side
``load_all_seeds`` integration is covered by test_skill_service.
"""

import textwrap
from pathlib import Path


def _write(path: Path, content: str) -> Path:
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return path


class TestParseSeedFile:
    def test_happy_path(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "x.md", """
            ---
            title: Album abspielen
            triggers:
              - spiel das Album X
              - leg X auf
            tools:
              - mcp.media.search_media
              - internal.play_album_on_dlna
            ---
            - Schritt 1
            - Schritt 2
        """)
        parsed = _parse_seed_file(md)
        assert parsed is not None
        assert parsed["title"] == "Album abspielen"
        assert parsed["trigger_examples"] == ["spiel das Album X", "leg X auf"]
        assert parsed["tool_sequence"] == [
            "mcp.media.search_media",
            "internal.play_album_on_dlna",
        ]
        assert "Schritt 1" in parsed["body_md"]
        assert "Schritt 2" in parsed["body_md"]

    def test_strips_quotes_from_list_items(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "x.md", """
            ---
            title: x
            triggers:
              - "spiel das album X"
              - 'leg X auf'
            tools:
              - mcp.x
            ---
            body line
        """)
        parsed = _parse_seed_file(md)
        assert parsed is not None
        assert "spiel das album X" in parsed["trigger_examples"]
        assert "leg X auf" in parsed["trigger_examples"]

    def test_missing_front_matter_returns_none(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "broken.md", """
            - just body
            - no front matter
        """)
        assert _parse_seed_file(md) is None

    def test_missing_title_returns_none(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "no-title.md", """
            ---
            triggers:
              - x
            ---
            body
        """)
        assert _parse_seed_file(md) is None

    def test_missing_triggers_returns_none(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "no-triggers.md", """
            ---
            title: x
            ---
            body
        """)
        assert _parse_seed_file(md) is None

    def test_missing_body_returns_none(self, tmp_path):
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "no-body.md", """
            ---
            title: x
            triggers:
              - y
            ---
        """)
        assert _parse_seed_file(md) is None

    def test_tools_optional(self, tmp_path):
        """tools: block is optional — some skills are pure knowledge,
        no MCP calls."""
        from services.skill_seed_loader import _parse_seed_file
        md = _write(tmp_path / "no-tools.md", """
            ---
            title: just a heuristic
            triggers:
              - "remind me to be polite"
            ---
            - speak in full sentences
        """)
        parsed = _parse_seed_file(md)
        assert parsed is not None
        assert parsed["tool_sequence"] == []

    def test_crlf_line_endings_normalized(self, tmp_path):
        """A Windows-checkout seed file with CRLF endings parses cleanly
        and the title/body contain no stray \\r characters."""
        from services.skill_seed_loader import _parse_seed_file
        md = tmp_path / "crlf.md"
        # Bypass _write's dedent + plain newlines and emit literal CRLF.
        md.write_bytes(
            b"---\r\n"
            b"title: CRLF Test\r\n"
            b"triggers:\r\n"
            b"  - hello windows\r\n"
            b"tools:\r\n"
            b"  - mcp.x\r\n"
            b"---\r\n"
            b"- step one\r\n"
            b"- step two\r\n"
        )
        parsed = _parse_seed_file(md)
        assert parsed is not None
        assert parsed["title"] == "CRLF Test"
        assert "\r" not in parsed["title"]
        assert "\r" not in parsed["body_md"]
        assert "hello windows" in parsed["trigger_examples"]
        assert all("\r" not in t for t in parsed["trigger_examples"])


class TestBundledSeedsValid:
    """Sanity check: every .md in src/backend/seed_skills/ parses cleanly."""

    def test_all_bundled_seeds_parse(self):
        from services.skill_seed_loader import _parse_seed_file
        backend_root = Path(__file__).resolve().parents[2] / "src" / "backend"
        seed_dir = backend_root / "seed_skills"
        if not seed_dir.is_dir():
            return  # No seeds in this checkout — skip.

        md_files = list(seed_dir.glob("*.md"))
        assert len(md_files) >= 1, "Expected at least one bundled seed skill"
        for md in md_files:
            parsed = _parse_seed_file(md)
            assert parsed is not None, f"Bundled seed {md.name} failed to parse"
            assert parsed["title"], f"{md.name} missing title"
            assert parsed["trigger_examples"], f"{md.name} missing triggers"
            assert parsed["body_md"].strip(), f"{md.name} missing body"
