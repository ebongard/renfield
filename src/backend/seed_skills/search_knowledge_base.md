---
title: Im persoenlichen Wissen suchen
triggers:
  - "weiss ich noch was zu X"
  - "was hatten wir letztens zu Y besprochen"
  - "find my notes about X"
  - "was steht in meinen Dokumenten zu Z"
tools:
  - internal.knowledge_search
---
- Rufe `internal.knowledge_search(query=<frage>)` mit der natuerlichsprachigen Frage als Query auf — das Tool macht semantische Suche ueber RAG, KG und Memory in einem Aufruf.
- Wenn Treffer vorhanden sind, fasse sie kurz und ABSTRAHIEREND zusammen (keine wortwoertlichen Zitate, keine Quell-IDs im final_answer).
- Wenn keine Treffer vorhanden sind, sag das ehrlich und biete an, eine neue Notiz / ein neues Memory anzulegen.
- Folge-Anfragen ("und was war Detail Z?") nutzen die bereits geladenen Treffer aus dem Konversationskontext — KEIN neuer Suchaufruf noetig.
