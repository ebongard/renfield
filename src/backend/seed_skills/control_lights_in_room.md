---
title: Licht in einem Raum steuern
triggers:
  - "mach das Licht im Wohnzimmer an"
  - "Licht aus im Schlafzimmer"
  - "dimme das Wohnzimmer auf 30 Prozent"
  - "turn on the lights in the kitchen"
tools:
  - mcp.ha.get_entities
  - mcp.ha.turn_on
  - mcp.ha.turn_off
---
- Wenn die exakte `entity_id` der Lampe(n) nicht aus dem Konversationskontext bekannt ist, hole die Liste der Lichter im Zielraum mit `mcp.ha.get_entities(domain="light", area=<raum>)`. Erfinde KEINE entity_ids.
- Fuer "an"/"on" rufe `mcp.ha.turn_on(entity_id=<id>, brightness_pct=<wert optional>)` auf — fuer "aus"/"off" entsprechend `mcp.ha.turn_off`.
- Wenn mehrere Lampen den Raum bedienen: rufe die Tool-Calls PARALLEL auf (Action-Array), nicht sequentiell.
- Bestaetige in `final_answer` knapp ("Wohnzimmer ist an"). KEINE technischen Details zu entity_ids im final_answer.
