"""``pc20261126b_merge_heads`` joins the two heads #1551 stage 2 B and #1240
left behind (both written against pc20261124_kg_edge_tier_cap). The general
one-head check lives in ``test_pc20261119_fk_rules_to_model_migration.py``;
this pins WHICH two revisions are joined and that the merge changes nothing."""

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]


def _load():
    import services.database as _db

    path = (Path(_db.__file__).resolve().parents[1] / "alembic" / "versions"
            / "pc20261126b_merge_heads.py")
    spec = importlib.util.spec_from_file_location("pc20261126b_merge_heads_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, path.parent.parent


def test_joins_exactly_the_two_heads_and_is_the_only_head():
    from alembic.script import ScriptDirectory

    mig, alembic_dir = _load()
    assert set(mig.down_revision) == {"pc20261125_presence_calibration", "pc20261126_kg_contact_points"}
    assert len(mig.revision) <= 32, "alembic_version.version_num is VARCHAR(32)"
    assert ScriptDirectory(str(alembic_dir)).get_heads() == [mig.revision]


def test_carries_no_schema_change():
    mig, _ = _load()
    assert mig.upgrade() is None and mig.downgrade() is None
