---
paths:
  - "src/backend/services/agent_service.py"
  - "src/backend/prompts/agent.yaml"
---
# Agent loop

Loaded only when `agent_service.py` or `prompts/agent.yaml` is read.

## Stale-error marker — the two halves must stay together
- A failed tool turn is persisted with `action_success: False` in the message metadata.
- The `conv_context` builder in `services/agent_service.py` prepends `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` to those
  assistant messages when it re-injects history into the next agent turn.
- The `conv_context_template` in `prompts/agent.yaml` carries the matching hint (in both the de and the en variant): marker
  lines are HISTORICAL, not current state — so a repeated user request RE-RUNS the tool instead of echoing the old
  error.
- Never rename the marker, drop the hint, or change the `action_success is False` test on one side only: without the
  hint the LLM treats the old failure as the present state and answers from it without calling the tool.
