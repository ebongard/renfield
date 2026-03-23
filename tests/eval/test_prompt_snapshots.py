"""
Prompt Snapshot Tests — Detects accidental or unreviewed prompt changes.

Hashes all prompts from YAML files (per file x key x language) and compares
against a stored snapshot. Any change to a prompt will fail this test until
the snapshot is explicitly updated.

Update snapshots:
    python3 -m pytest tests/eval/test_prompt_snapshots.py --update-snapshots

This is NOT a correctness test — it's a change-detection mechanism.
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

EVAL_DIR = Path(__file__).parent
SNAPSHOT_PATH = EVAL_DIR / "prompt_snapshots.json"

SUPPORTED_LANGUAGES = ["de", "en"]
# Keys that are config, not prompts — skip hashing
CONFIG_KEYS = {"llm_options", "llm_options_retry", "llm_options_summary",
               "rag_llm_options", "contradiction_llm_options"}


def _collect_prompt_hashes() -> dict[str, str]:
    """Hash all prompts from prompt_manager, keyed as 'file.key.lang'."""
    from services.prompt_manager import prompt_manager

    hashes: dict[str, str] = {}

    for file_name in sorted(prompt_manager.list_files()):
        all_data = prompt_manager.get_all(file_name)
        if not all_data:
            continue

        for key in sorted(prompt_manager.list_keys(file_name)):
            if key in CONFIG_KEYS:
                continue

            if key in SUPPORTED_LANGUAGES:
                # This is a language key — hash each prompt inside it
                lang_data = all_data.get(key, {})
                if isinstance(lang_data, dict):
                    for prompt_name in sorted(lang_data.keys()):
                        value = lang_data[prompt_name]
                        if isinstance(value, str):
                            hash_key = f"{file_name}.{prompt_name}.{key}"
                            hashes[hash_key] = hashlib.sha256(value.encode()).hexdigest()[:16]
            else:
                # Root-level key — try getting it for each language
                for lang in SUPPORTED_LANGUAGES:
                    try:
                        value = prompt_manager.get(file_name, key, lang=lang)
                        if isinstance(value, str) and value:
                            hash_key = f"{file_name}.{key}.{lang}"
                            hashes[hash_key] = hashlib.sha256(value.encode()).hexdigest()[:16]
                    except Exception:
                        pass

    return hashes


def _load_snapshot() -> dict[str, str]:
    """Load stored prompt hashes from JSON file."""
    if not SNAPSHOT_PATH.exists():
        return {}
    with open(SNAPSHOT_PATH) as f:
        return json.load(f)


def _save_snapshot(hashes: dict[str, str]) -> None:
    """Save prompt hashes to JSON file."""
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(hashes, f, indent=2, sort_keys=True)
        f.write("\n")


@pytest.mark.eval
def test_prompt_snapshots(request):
    """Verify that no prompts have changed since the last snapshot."""
    update_mode = request.config.getoption("--update-snapshots", default=False)
    current = _collect_prompt_hashes()

    if update_mode or not SNAPSHOT_PATH.exists():
        _save_snapshot(current)
        if not SNAPSHOT_PATH.exists():
            pytest.skip("Snapshot file generated — run again to verify")
        return

    stored = _load_snapshot()

    # Find changes
    changed: list[str] = []
    added: list[str] = []
    removed: list[str] = []

    for key, hash_val in current.items():
        if key not in stored:
            added.append(key)
        elif stored[key] != hash_val:
            changed.append(key)

    for key in stored:
        if key not in current:
            removed.append(key)

    if changed or added or removed:
        msg_parts = []
        if changed:
            msg_parts.append(f"CHANGED ({len(changed)}): {', '.join(changed)}")
        if added:
            msg_parts.append(f"ADDED ({len(added)}): {', '.join(added)}")
        if removed:
            msg_parts.append(f"REMOVED ({len(removed)}): {', '.join(removed)}")
        msg = "Prompt changes detected! " + " | ".join(msg_parts)
        msg += "\n\nRun with --update-snapshots to accept these changes."
        pytest.fail(msg)


@pytest.mark.eval
def test_prompt_snapshot_coverage():
    """Verify that the snapshot covers a reasonable number of prompts."""
    current = _collect_prompt_hashes()
    assert len(current) >= 20, (
        f"Only {len(current)} prompt hashes found — expected at least 20. "
        f"Are all prompt YAML files loaded?"
    )
