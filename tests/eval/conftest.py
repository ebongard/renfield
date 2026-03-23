"""Shared fixtures for evaluation tests."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure backend modules are importable
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Stub ollama before any backend imports that might need it
if "ollama" not in sys.modules:
    sys.modules["ollama"] = MagicMock()

EVAL_DIR = Path(__file__).parent
GOLDEN_DATASET_PATH = EVAL_DIR / "golden_dataset.json"
PROMPT_SNAPSHOTS_PATH = EVAL_DIR / "prompt_snapshots.json"


@pytest.fixture
def golden_data():
    """Load the golden dataset."""
    with open(GOLDEN_DATASET_PATH) as f:
        return json.load(f)


def pytest_addoption(parser):
    """Add --update-snapshots CLI flag for prompt snapshot tests."""
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Regenerate prompt snapshot file",
    )
