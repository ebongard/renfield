---
title: Album auf DLNA-Renderer abspielen
triggers:
  - "spiel das Album X im Wohnzimmer"
  - "leg X im Wohnzimmer auf"
  - "play album X in the living room"
  - "Album X auflegen"
tools:
  - mcp.media.search_media
  - internal.play_album_on_dlna
---
- Suche das Album mit `mcp.media.search_media` (type="MusicAlbum", query=Albumname [+ ggf. Kuenstler]). Notiere die `album_id` aus dem ersten passenden Treffer.
- Rufe `internal.play_album_on_dlna(album_id=<id>, renderer_name=<Raum>)` auf. Das Tool holt automatisch alle Tracks und startet die Wiedergabe — NIEMALS `mcp.dlna.play_tracks` direkt aufrufen.
- Bei `success=true` gib SOFORT `final_answer` ("Spielt jetzt in <Raum>"). NIEMALS dasselbe Play-Tool erneut aufrufen.
- Bei `success=false` gib direkt `final_answer` mit der Fehlermeldung — beschreibe NICHT die Suchergebnisse und gib KEINE URLs aus.
