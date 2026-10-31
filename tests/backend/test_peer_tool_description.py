"""The peer tool's description -- measured, not guessed.

🛑 What was measured is what ships, **byte for byte**. That is why the measured
version stands here as a literal: if somebody changes the text in the code this
test goes red and demands a new measurement with `bin/measure-preselect.py`,
rather than letting an unmeasured version go live silently. Four review rounds
on the hint text without a measurement cost this project eight measurement
rounds on 2026-10-27.

Measured on 2026-10-29 against the REAL prompt of the live turn (signature
`aa62fdb7`, evidenced through the signature as an oracle, not reimplemented):

    old version (the person only)   27/39   69 %
    this version                    36/39   92 %

The old one named only the PERSON. "frag <jemand>s Renfield" hit 3/3 with it --
the model recognises the pattern, not the name (even "frag Mamas renfield" hit,
although Mama is not the display name). 0/3 was the score for every question
naming the INSTANCE or asking for the PLURAL; those three are now 3/3.

Still open (0/3, with three different document phrasings as well): "Welche
Dokumente sind in der x-idra instanz heute angelegt worden?" -- for that the
INSTANCE NAME would have to be in the description, and it is not available.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.agent_service import _PRESELECT_DESC_CHARS, preselect_tool_line
from services.peer_mcp_registry import _build_query_brain_tool

pytestmark = pytest.mark.unit

#: The version that measured 36/39 -- byte for byte, with `evdb` as the display name.
GEMESSEN = (
    "Ask evdb's Renfield — another paired INSTANCE — for information its "
    "people chose to share with you through circles. Use for „frag <jemand>s "
    "Renfield“, „was liegt in <Instanz>“, „die andere Instanz“ "
    "and „in allen Renfields suchen“. NOT for scanning paper: in "
    "„scanne nach <Ziel>“ <Ziel> is a filing target, not an instance."
)


def _peer(name: str = "evdb") -> MagicMock:
    p = MagicMock()
    p.id, p.remote_display_name = 3, name
    return p


def test_the_shipped_description_is_the_MEASURED_one_byte_for_byte():
    """🛑 The guard against an unmeasured change. If this test goes red it is
    not a formatting slip: the text was changed without measuring again."""
    assert _build_query_brain_tool(_peer()).description == GEMESSEN


def test_it_names_the_INSTANCE_not_only_the_person():
    """The gap that cost 0/3: three kinds of question found no anchor."""
    d = _build_query_brain_tool(_peer()).description
    assert "INSTANCE" in d
    assert "andere Instanz" in d
    assert "allen Renfields" in d


def test_it_keeps_the_pattern_that_ALREADY_worked():
    """🛑 Negative control against a regression: "frag <jemand>s Renfield" hit
    3/3 with the old version already. A new text that loses that would be
    barely visible in the total and very visible in use."""
    d = _build_query_brain_tool(_peer()).description
    assert "Renfield" in d
    assert "frag <jemand>s Renfield" in d


def test_the_scanner_boundary_is_in_the_text():
    """🛑 "scanne nach x-idra" names a FILING TARGET, not an instance, and has
    to stay with the scanner (measured 3/3). The sentence that carries that
    stands at the END -- see the cap test below."""
    d = _build_query_brain_tool(_peer()).description
    assert "NOT for scanning paper" in d
    assert "filing target, not an instance" in d


class TestTheCapCannotEatTheBoundary:
    """🛑 The description is capped at `_PRESELECT_DESC_CHARS`, and the
    scanner demarcation sentence stands at the end. A long display name pushes
    exactly that over the edge -- and then "scanne nach x-idra" becomes a peer
    request again. The name comes from the FAR SIDE (`offer.display_name`), so
    it is not under our own control.
    """

    @pytest.mark.parametrize("name", ["evdb", "x" * 200, "Ü" * 300, ""])
    def test_the_description_stays_under_the_cap(self, name):
        d = _build_query_brain_tool(_peer(name)).description
        assert len(d) <= _PRESELECT_DESC_CHARS, (
            f"{len(d)} characters for a {len(name)}-character name — the cap "
            "cuts off the scanner sentence")

    @pytest.mark.parametrize("name", ["evdb", "x" * 200, "Ü" * 300])
    def test_the_boundary_survives_the_line_build(self, name):
        """Do not check the description alone but the LINE the prompt really
        carries -- built with the production function, not reimplemented."""
        werkzeug = _build_query_brain_tool(_peer(name))
        zeile = preselect_tool_line(werkzeug.namespaced_name, werkzeug)
        assert "filing target, not an instance" in zeile

    def test_a_long_name_is_SHORTENED_not_the_sentence(self):
        """Negative control on the cap: it takes hold on the NAME, not on the
        sentence. Without this check there would be no way to see whether the
        test above is green because the name was shortened or because the
        sentence disappeared."""
        d = _build_query_brain_tool(_peer("x" * 200)).description
        assert "x" * 200 not in d
        assert d.startswith("Ask " + "x" * 40 + "'s Renfield")
