"""The preselection's length limit -- per tool, with a cap on the sum.

🛑 The occasion: the limit was GLOBAL (480), and its consequence had never been
measured on the tools without a hint channel. Measured on 2026-10-29 against
the REAL prompt (signature `aa62fdb7`, evidenced through the signature, not
reimplemented), two case collections, 3 repetitions each:

    everything at 480            peer 92 %   scanner 91 %
    these four in full (+589)    peer 92 %   scanner 95 %   <- both
    all nine in full (+2640)     peer 85 %   scanner 95 %

Nine of our own descriptions were cut off MID-SENTENCE at 480, 2640 characters
in total -- among them guards ("it never deletes anything"), demarcations
against neighbouring tools, German trigger examples and a permission statement
(`system_health`: "Nur-lesend. Admin-Berechtigung.").

🛑 Lifting it entirely is wrong regardless: it broke the negative control
"suche nach Rechnungen" (3/3 -> 0/3). An ordinary LOCAL search turned into a
federated request -- the free text would have left the instance. At an equal
score (181 against 180) the CONSEQUENCE decides: crossing a boundary without
cause is worse than missing a tool.

🛑 A higher GLOBAL number is wrong too: 706 would be needed, which would add
1719 characters instead of 589 and would still cut five of the nine off
mid-sentence.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_service import (
    _PRESELECT_DESC_BUDGET,
    _PRESELECT_DESC_CHARS,
    preselect_tool_line,
)

pytestmark = pytest.mark.unit

#: The measured edge. +589 brings back four load-bearing endings and holds
#: BOTH collections; +829 (one step more) added 0 points; +2640 cost 7.
MEASURED_EDGE = 589


def test_the_budget_sum_stays_at_the_MEASURED_edge():
    """🛑 The guarantee belongs on the SUM, not on individual tools: what did
    the damage was the total length (+2640), not a single text. And not on the
    prompt length: that grows when a new tool arrives with the default too, and
    that is harmless.

    Whoever raises a budget pushes against this number -- so the raise becomes
    a visible decision against a measurement, not a silent increase. If it is
    to rise, a new measurement belongs with it (`bin/measure-preselect.py`
    against BOTH case collections), not merely a larger number here.
    """
    total = sum(b - _PRESELECT_DESC_CHARS for b in _PRESELECT_DESC_BUDGET.values())
    assert total <= MEASURED_EDGE, (
        f"budgets add up to +{total} characters, measured and proven is "
        f"+{MEASURED_EDGE}. At +2640 the peer collection fell to 85 % and a "
        "local search turned into a federated query. Measure first, then raise.")


def test_no_budget_is_BELOW_the_default():
    """A budget below 480 would be a tightening through the back door -- the
    mapping exists for MORE room, not for less."""
    for name, b in _PRESELECT_DESC_BUDGET.items():
        assert b >= _PRESELECT_DESC_CHARS, f"{name}: {b} < {_PRESELECT_DESC_CHARS}"


def test_the_line_builder_USES_the_budget():
    """Behaviourally, not structurally: the line has to carry the full text."""
    name = "internal.find_duplicate_documents"
    budget = _PRESELECT_DESC_BUDGET[name]
    w = MagicMock()
    w.description, w.hint = "x" * budget, ""
    assert len(preselect_tool_line(name, w)) == len(f"- {name}: ") + budget


def test_a_tool_WITHOUT_a_budget_still_gets_the_default():
    """🛑 Negative control. Without it the test above only checked that some
    text gets through -- not that the default APPLIES to everything else. Were
    it lifted by accident, 2640 characters would stand in the prompt again."""
    w = MagicMock()
    w.description, w.hint = "y" * 2000, ""
    line = preselect_tool_line("internal.gibt_es_nicht", w)
    assert len(line) == len("- internal.gibt_es_nicht: ") + _PRESELECT_DESC_CHARS


def test_the_hint_is_STILL_exempt():
    """The operator's steering text was never capped and stays that way --
    otherwise this change would have trimmed `scan_document` in passing, whose
    hint carries 1760 characters."""
    w = MagicMock()
    w.description, w.hint = "z" * 1000, "H" * 900
    line = preselect_tool_line("mcp.scanner.scan_document", w)
    assert "H" * 900 in line
    assert line.count("z") == _PRESELECT_DESC_CHARS


class TestTheBudgetedToolsAreActuallyComplete:
    """🛑 A budget smaller than its text keeps truncating -- only this time
    with the appearance of being settled. So check against the REAL
    descriptions, not against the numbers alone.
    """

    @pytest.fixture(scope="class")
    def registry(self):
        from services.agent_tools import AgentToolRegistry

        return AgentToolRegistry(_init_only=True)

    def test_every_budgeted_tool_exists(self, registry):
        """A budget for a renamed or removed tool is dead weight and hides
        the fact that the real text is being capped again."""
        missing = [n for n in _PRESELECT_DESC_BUDGET if n not in registry._tools]
        assert not missing, f"budget without a tool: {missing}"

    def test_no_budgeted_tool_is_truncated(self, registry):
        """The purpose of the mapping: these four are to stand COMPLETE."""
        too_short = []
        for name, budget in _PRESELECT_DESC_BUDGET.items():
            w = registry._tools.get(name)
            if w is None:
                continue
            d = str(getattr(w, "description", "") or "")
            h = str(getattr(w, "hint", "") or "")
            if h and d.endswith(h):
                d = d[: len(d) - len(h)].rstrip()
            if len(d) > budget:
                too_short.append((name, len(d), budget))
        assert not too_short, (
            "budget smaller than the text — it keeps being truncated, only "
            f"no longer visibly: {too_short}. Shorten the text or (after a "
            "measurement) raise the budget and `MEASURED_EDGE` together.")
