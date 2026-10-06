---
paths:
  - "src/backend/services/agent_service.py"
  - "src/backend/prompts/agent.yaml"
  - "src/backend/api/websocket/chat_handler.py"
---
# Agent loop

Loaded only when `agent_service.py`, `prompts/agent.yaml` or the chat handler is read.

## Stale-error marker — the two halves must stay together
- A failed tool turn is persisted with `action_success: False` in the message metadata.
- The `conv_context` builder in `services/agent_service.py` prepends `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` to those
  assistant messages when it re-injects history into the next agent turn.
- The `conv_context_template` in `prompts/agent.yaml` carries the matching hint (in both the de and the en variant): marker
  lines are HISTORICAL, not current state — so a repeated user request RE-RUNS the tool instead of echoing the old
  error.
- Never rename the marker, drop the hint, or change the `action_success is False` test on one side only: without the
  hint the LLM treats the old failure as the present state and answers from it without calling the tool.

## …and the CONDITION BEFORE them is the third half (#1367)
Both halves can be intact and the marker still never fire, because nothing ever writes `False`. Measured 2026-10-04:
- **`success` means "the outcome", never "the call came back".** `mcp_client._detect_inner_error` reads the TOP level
  of the payload: an explicit `success`/`ok` boolean DECIDES, and only without one do `error` (str) and a truthy
  `error_code` count. Our own servers answer `{"ok": false, …}` with neither `success` nor `error` — that shape was a
  silent success for months. Never recurse into nested payloads: a status tool answering `{"ok": true, "result":
  {"ok": false}}` reports a PAST failure, and the call did succeed.
- **The agent path needs its own carrier.** A failed step has no `data`, so it never reaches `agent_tool_results`;
  fixing only the detector moves `action_success` from `True` to `None`, which is just as unmarked. `chat_handler`
  therefore collects EVERY `tool_result` step as `agent_tool_outcomes` `(tool, success)` and
  `_failed_agent_actions` decides per tool name.
- **An action's outcome is its LAST attempt with that tool.** A rejected parameter the agent immediately retries is a
  recovery, not a failed turn; an unretried failure stays a failure even when a different tool succeeded afterwards
  (the measured shape: `route_scan` fails, a read tool then succeeds while the agent explains why).
- `action_success` has four consumers — this marker, `turn_extraction` (no memory from a failed turn), the kiosk role
  activity, and the follow-up chips. A fifth, the proactive intent-feedback frame, is deliberately fenced to the
  ranked-intent path (`not agent_used`): the agent path has no recognised intent to confirm.
