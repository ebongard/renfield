"""stdlib `logging` reaches the one loguru sink (#1565).

19 backend modules log through stdlib `logging`. Without a handler on the root
logger their WARNING lines went to stderr through `logging.lastResort`, past
the sink `install_log_sink` configures. `_route_stdlib_logging_into_loguru`
puts one bridge on the root logger; the root LEVEL is left alone.
"""

import logging

import pytest
from loguru import logger as loguru_logger

import utils.log_safety as log_safety

pytestmark = pytest.mark.unit


@pytest.fixture
def bridged():
    """The bridge on the root logger and a list sink on loguru; both removed after."""
    root = logging.getLogger()
    before = list(root.handlers)
    level_before = root.level
    lines: list[str] = []
    sink_id = loguru_logger.add(lambda m: lines.append(str(m)), level="DEBUG", colorize=False,
                                format="{level}|{name}|{message}")
    log_safety._route_stdlib_logging_into_loguru()
    try:
        yield lines
    finally:
        loguru_logger.remove(sink_id)
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        root.setLevel(level_before)


def test_a_stdlib_warning_reaches_the_loguru_sink_formatted_by_stdlib(bridged):
    logging.getLogger("some_backend_module").warning("scan failed for user %d: %s", 7, "x")
    assert any(line.startswith("WARNING|") and "scan failed for user 7: x" in line for line in bridged), bridged


def test_the_line_is_attributed_to_the_caller_not_to_the_bridge(bridged):
    logging.getLogger("some_backend_module").warning("where am I")
    (line,) = [x for x in bridged if "where am I" in x]
    assert f"|{__name__}|" in line, line


def test_braces_in_a_stdlib_message_are_not_loguru_placeholders(bridged):
    logging.getLogger("some_backend_module").warning("payload {not_a_field} %s", "ok")
    assert any("payload {not_a_field} ok" in line for line in bridged), bridged


def test_the_root_level_is_untouched_so_library_info_stays_silent(bridged):
    # httpx logs every request at INFO; routing the root must not start printing it.
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    logging.getLogger("httpx").info("HTTP Request: GET http://example.test")
    assert not any("HTTP Request" in line for line in bridged)


def test_installing_twice_leaves_one_bridge(bridged):
    log_safety._route_stdlib_logging_into_loguru()
    bridges = [h for h in logging.getLogger().handlers if isinstance(h, log_safety._StdlibToLoguru)]
    assert len(bridges) == 1


def test_a_stdlib_exception_keeps_its_traceback(bridged):
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("some_backend_module").exception("handler caught it")
    text = "\n".join(bridged)
    assert "handler caught it" in text
    assert "RuntimeError" in text


def test_install_log_sink_installs_the_bridge():
    """The entry points call `install_log_sink`; the bridge must come with it."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(log_safety.__file__).read_text(encoding="utf-8"))
    (install,) = [n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "install_log_sink"]
    called = {getattr(c.func, "id", None) for c in ast.walk(install) if isinstance(c, ast.Call)}
    assert "_route_stdlib_logging_into_loguru" in called


class TestLoggedContent:
    """`logged_content` (#1565): length by default, the text only with LOG_CONTENT."""

    def test_off_by_default_shows_only_the_length(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "log_content", False)
        assert log_safety.logged_content("Zebrafink Sonderling") == "<20 chars>"

    def test_on_shows_the_text_cut_short_and_quoted(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "log_content", True)
        assert log_safety.logged_content("kurz") == "'kurz'"
        assert log_safety.logged_content("x" * 10, limit=4) == "'xxxx…'"

    def test_none_and_non_strings(self, monkeypatch):
        from utils.config import settings

        monkeypatch.setattr(settings, "log_content", False)
        assert log_safety.logged_content(None) == "<none>"
        assert log_safety.logged_content(12345) == "<5 chars>"

    def test_the_setting_defaults_to_off(self):
        from utils.config import Settings

        assert Settings.model_fields["log_content"].default is False


def test_an_in_process_alembic_run_leaves_the_apps_logging_alone():
    """#1565 review: on a fresh install the app runs `alembic upgrade` in its own
    process; `env.py`'s `fileConfig` would replace the root handlers (the bridge)
    and disable every existing logger. The app passes `configure_logger=False`
    and `env.py` honours it -- both ends, read off the source."""
    import pathlib

    backend = pathlib.Path(log_safety.__file__).resolve().parents[1]
    env = (backend / "alembic" / "env.py").read_text(encoding="utf-8")
    db = (backend / "services" / "database.py").read_text(encoding="utf-8")
    assert 'config.attributes.get("configure_logger", True)' in env
    assert 'cfg.attributes["configure_logger"] = False' in db
