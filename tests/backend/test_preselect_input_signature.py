"""The preselection has to log WHAT it was offered -- not only how much.

🛑 The occasion, measured on 2026-10-26. To the same message ("scanne nach
xidra") the preselection gave two different results across nine turns: seven
times the scanner tools, twice the document search. The log said `3/33` both
times. So the decisive question could NOT be answered: had the input changed,
or was the model wavering?

Three misdiagnoses rested on that gap:

1. "The temperature is rolling dice." Re-measured: live `temperature: 0`, and
   ten identical requests gave ten identical answers. Disproved.
2. "The position in the list decides." Re-measured with the complete hint
   text: 14 of 15 positions pick the right tool. The first measurement, which
   said the opposite, used a HINT that I had myself cut off mid-sentence -- an
   artefact of my test data, not a property of the system.
3. Built on that, a new prompt ("position means nothing, go through the
   list"). Measured against the real model: 7 of 15 instead of 14 of 15 -- the
   repair would have been a REGRESSION. Not rolled out.

What was missing in all three attempts was the same thing: the input. This
fingerprint supplies it in eight characters.
"""
import pytest

from services.agent_service import preselect_input_signature

pytestmark = [pytest.mark.unit, pytest.mark.backend]

#: Exactly as `_preselect_tools` assembles it: one line per tool, with the
#: name, the trimmed description and the untruncated hint.
LISTE = "\n".join([
    "- mcp.scanner.scan_document: Startet einen NEUEN Scan. „scanne nach <X>“ "
    "bedeutet beim PAPIERSCANNER IMMER, dass <X> das ZIEL ist.",
    "- mcp.paperless.search_documents: Search archived documents by text.",
    "- internal.knowledge_search: Search the knowledge base.",
])


def _prompt(lang: str, tool_list: str, message: str = "scanne nach xidra") -> str:
    """The prompt as PRODUCTION builds it -- through the REAL prompt_manager.

    🛑 No reimplementation of the template. If this test did that, the language
    test would check my own string instead of the shipped `de` and `en`
    template, and the coverage of the message would be asserted instead of
    measured.
    """
    from services.prompt_manager import prompt_manager

    fertig = prompt_manager.get(
        "agent", "tool_preselect_prompt", lang=lang,
        message=message, tool_list=tool_list,
    )
    assert fertig, f"no `tool_preselect_prompt` template for lang={lang!r}"
    return fertig


def test_the_same_input_gives_the_same_signature():
    """Otherwise a comparison between two turns would be worthless."""
    assert preselect_input_signature(_prompt("de", LISTE)) == \
        preselect_input_signature(_prompt("de", str(LISTE)))


def test_a_changed_DESCRIPTION_changes_the_signature():
    """🛑 The defect of the FIRST version: it hashed only the names.

    What goes into the prompt is `tool_list`, so the name PLUS the trimmed
    description PLUS the untruncated hint. A changed hint -- through a ConfigMap
    patch, say, as happened on 2026-10-26 -- rearranged the prompt and left the
    signature untouched.
    """
    geaendert = LISTE.replace("das ZIEL ist", "ein Suchbegriff ist")
    assert geaendert != LISTE, "the test is meant to really change the hint text"
    # The same names, different text:
    assert [z.split(":")[0] for z in geaendert.splitlines()] == \
           [z.split(":")[0] for z in LISTE.splitlines()]
    assert preselect_input_signature(_prompt("de", geaendert)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "the signature ignores the description text — then a changed "
        "hint stays invisible although it rearranges the prompt")


def test_a_MESSAGE_that_differs_only_after_60_characters_changes_the_signature():
    """🛑 The defect of the SECOND version: it hashed `lang` + `tool_list`.

    The MESSAGE was missing -- and the log line shows only `message[:60]`. Two
    turns that agree in the first 60 characters and diverge after would thereby
    have shown an identical message AND an identical signature across two
    DIFFERENT prompts. Exactly the statement the fingerprint is supposed to
    carry would have been false. Dictated voice turns regularly exceed 60
    characters.

    Found in the review of #1413, before it was merged.
    """
    vorsilbe = "scanne bitte die Seiten aus dem Einzug ein und lege sie dann ab"
    assert len(vorsilbe) > 60, "the prefix must exceed 60 characters"
    a = _prompt("de", LISTE, message=f"{vorsilbe} in der Instanz xidra")
    b = _prompt("de", LISTE, message=f"{vorsilbe} in der Instanz haushalt")
    assert preselect_input_signature(a) != preselect_input_signature(b), (
        "the signature ignores the message — then it claims equality "
        "across two different prompts, and because of `message[:60]` "
        "the line shows the same beginning")


def test_a_different_LANGUAGE_changes_the_signature():
    """`tool_preselect_prompt` exists in de AND en -- two different
    templates. A signature that passes over that asserts equality across two
    different prompts."""
    assert preselect_input_signature(_prompt("en", LISTE)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "the signature ignores the language")


def test_a_different_ORDER_changes_the_signature():
    """🛑 `get_tool_names()` returns `list(self._tools.keys())`, so the
    INSERTION ORDER of a dict -- and that changes when tools are registered
    anew (`refresh_interval: 300` on the MCP servers).

    A signature that captures only the SET would not have seen exactly the case
    it was built for.
    """
    zeilen = LISTE.splitlines()
    gedreht = "\n".join(reversed(zeilen))
    assert sorted(gedreht.splitlines()) == sorted(zeilen), \
        "the test is meant to change only the order"
    assert preselect_input_signature(_prompt("de", gedreht)) != \
        preselect_input_signature(_prompt("de", LISTE)), (
        "the signature ignores the order — then a re-registration "
        "stays invisible, and exactly that was under suspicion on "
        "2026-10-26")


def test_an_added_or_removed_tool_changes_the_signature():
    """A server that has dropped out or been added has to show up."""
    zeilen = LISTE.splitlines()
    assert preselect_input_signature(_prompt("de", "\n".join(zeilen[:-1]))) != \
        preselect_input_signature(_prompt("de", LISTE))
    assert preselect_input_signature(_prompt("de", LISTE + "\n- mcp.email.send: Send mail.")) != \
        preselect_input_signature(_prompt("de", LISTE))


def test_the_signature_fits_into_a_log_line():
    """It stands next to the message on one line -- short and readable as a
    word.

    🛑 Checked on its FORM, not on a number: `len == 8` would only pin down the
    current choice without saying anything about fitness. What is checked is
    what the line needs.
    """
    sig = preselect_input_signature(_prompt("de", LISTE))
    assert sig.isalnum(), f"not readable as a word: {sig!r}"
    assert 6 <= len(sig) <= 16, f"unusable for a log line: {sig!r}"


def test_an_empty_list_does_not_raise():
    """The preselection does not start at all at <= 6 tools, but the function
    still must not raise -- a logging path that can crash takes the diagnosis
    down with it when it matters."""
    assert preselect_input_signature(_prompt("de", ""))


def test_two_tools_that_differ_only_in_a_namespace_are_distinguished():
    """No truncation to the short name: `mcp.a.x` and `mcp.b.x` are different
    tools."""
    assert preselect_input_signature(_prompt("de", "- mcp.scanner.scan_document: X")) != \
        preselect_input_signature(_prompt("de", "- mcp.paperless.scan_document: X"))
