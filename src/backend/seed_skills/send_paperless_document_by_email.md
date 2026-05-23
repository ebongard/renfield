---
title: Paperless-Dokument per E-Mail senden
triggers:
  - "schick mir die Rechnung von X"
  - "sende mir das Dokument Y"
  - "email me the invoice"
  - "schick die per Mail"
tools:
  - mcp.paperless.search_documents
  - mcp.paperless.download_document
  - mcp.email.send_email
---
- Wenn die Dokument-ID noch nicht aus dem Konversationskontext bekannt ist, finde sie mit `mcp.paperless.search_documents`. Erfinde NIEMALS IDs.
- Lade das Dokument mit `mcp.paperless.download_document(document_id=<id>)` herunter. Die geladenen Bytes werden automatisch fuer den naechsten send_email Call vorgehalten.
- Sende mit `mcp.email.send_email(to=<adresse>, subject=<titel>, body=<kurz>)`. Uebergib KEINE `attachments` — sie werden automatisch aus dem Download-Schritt angehaengt.
- Bestaetige in `final_answer` an wen die Mail rausging.
