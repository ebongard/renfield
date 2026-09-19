# TTS-Klangprofil pro Ausgabegerät (Entzerrung für HiFi-Ausgaben)

Auslöser 2026-09-19/20: Sprachantwort über den HiFiBerry klingt „flach und dumpf".
Gemessen an einer echten Antwort im TTS-Cache: Piper `de_DE-thorsten-high`, 22,05 kHz mono,
**79 % der Energie in 80–300 Hz, nur 2 % über 1 kHz**, kein Clipping. Der Pfad reicht die
WAV unbearbeitet an DLNA durch. Satelliten-Lautsprecher geben den Bass nicht wieder — dort
fällt es nicht auf, auf einer Anlage schon.

Hörvergleich über den HiFiBerry (12 Proben): Nutzer wählt **Probe 2** (Thorsten + sanfte
Entzerrung) und Probe 5 (Kerstin). Entscheidungen des Nutzers:
- D1 Richtung: **Thorsten bleibt, Entzerrung** (kein Stimmwechsel).
- D2 Steuerung: **pro Ausgabegerät** (wie `tts_volume`), nicht global.
- D3 Entzerrung am HiFiBerry selbst: **ausgeschlossen** (färbt auch Musik).
- D4 Kokoro: für Deutsch keine Option (67 Stimmen, keine deutsche; `lang_code` d/de abgelehnt).

## Entwurfsentscheidung: benanntes Profil, keine freien Parameter

Spalte `room_output_devices.tts_eq_profile` (String, NULL = aus). Ein Profil ist ein NAME
(`hifi_speech`), die Filterwerte stehen im Code. Begründung: die Werte sind gehört und
gemessen (Probe 2), ein Regler-Satz pro Gerät wäre eine UI ohne Messgrundlage. Ein zweites
Profil ist später ein Dict-Eintrag + ein i18n-String, keine Migration.

`hifi_speech` = Hochpass 120 Hz (Butterworth 2. Ordnung) + High-Shelf +7 dB @ 2,5 kHz,
danach Spitzen-Normalisierung auf −1 dBFS. Exakt die Kette von Probe 2.

## Backend
- [x] B1 Migration `pc20260920_output_tts_eq`: `tts_eq_profile VARCHAR(32) NULL`,
      `down_revision = pc20260912_taskalert` (LIVE geprüft: einziger Kopf). Rein additiv.
- [x] B2 Modell `RoomOutputDevice.tts_eq_profile`; Schemas Create/Update/Response mit
      Validierung gegen die bekannten Profile (unbekannt → 422, nicht still ignorieren).
- [x] B3 `ha_glue/services/tts_equalizer.py`: reine Funktion `apply_profile(wav, profile)`.
      Fail-safe: jeder Fehler → Original-Bytes + ein Log (eine kaputte Entzerrung darf nie
      die Antwort kosten). Abtastrate, Kanalzahl, Länge bleiben erhalten.
- [x] B4 `audio_output_service`: Profil vor dem Cachen anwenden — DLNA- UND
      HA-Media-Player-Pfad, in `asyncio.to_thread` (Filter ist CPU-Arbeit).
      Satelliten-Wiedergabe (`send_tts_audio`) bleibt unberührt.
- [x] B5 Routen + `output_routing_service` reichen das Feld durch (add/update/list).
- [x] B6 `scipy` explizit in `requirements.txt` (heute nur transitiv vorhanden).

## Frontend
- [x] F1 `roomOutputs.ts`: Feld `tts_eq_profile`.
- [x] F2 `RoomOutputSettings.tsx`: Auswahl „Klangprofil" (Keins / HiFi-Sprache) neben der
      TTS-Lautstärke; Anzeige am Gerät. DESIGN.md-Token, `dark:`-Varianten, i18n de + en.

## Tests
- [x] T1 Equalizer: Energieanteil > 1 kHz steigt, < 300 Hz sinkt (an synthetischem Signal
      gemessen, nicht nur „läuft durch"); Format erhalten; kaputtes WAV → Original;
      unbekanntes Profil / None → Original byte-identisch.
- [x] T2 Schema-Validierung (422), Routen-Roundtrip, Modell-Default NULL.
- [x] T3 `audio_output_service`: Profil wird angewendet, wenn gesetzt — und NICHT, wenn NULL.
- [x] T4 Vitest: Auswahl rendert, sendet das Feld, zeigt den gespeicherten Wert.

## Docs
- [x] `docs/OUTPUT_ROUTING.md`, `CLAUDE.md` (Audio Output Routing). `docs/FEATURES.md` erwähnt die TTS-Lautstärke nicht — dort nichts nachzuziehen.
- [x] `docs/voice-pipeline-plan.md`: die Annahme „Kokoro — bessere deutsche Prosodie" ist
      falsch (keine deutsche Stimme) — berichtigen.

## Verifikation
- Backend-Tests auf .159, Vitest + typecheck lokal.
- Migration gegen echtes Postgres (`renfield_test`).
- Nach Deploy: Profil am HiFiBerry setzen, Sprachantwort auslösen, gecachte Datei messen
  (Energie > 1 kHz muss gegenüber heute 2 % deutlich steigen), Nutzer hört gegen.

## Nicht in diesem Vorhaben
- Stimm-Klonen (eigener Versuch: aktuelle Modell-/Lizenzrecherche, Probe mit
  Referenzaufnahme; Einwilligung + xidra-Lizenzfrage vorab).
- Schlichtung zwischen zwei Satelliten, die denselben Satz hören.

## Review (2026-09-20)
Umgesetzt auf `feat/output-tts-eq-profile`, LOKAL, nicht gepusht, nicht deployt.
- Backend (`17354d7a`): wie geplant. Abweichung: PATCH braucht eine Unterscheidung
  „nicht gesendet" vs. „null" — sonst liesse sich das Profil nie wieder ausschalten
  (`model_fields_set` + `_UNSET`). Leerer String (HTML-<select>) gilt als aus.
- Frontend (`ca75edb8`, per Subagent): zusaetzlich ein Bearbeiten-Knopf je Audio-Ausgabe —
  die Komponente hatte KEIN Bearbeiten-Formular, das Profil waere sonst nur beim Anlegen
  setzbar gewesen, und der HiFiBerry existiert schon.
- Tests: 128 Backend-Tests gruen (.159), 12 Komponententests + typecheck + eslint gruen.
  Migration gegen echtes Postgres: Upgrade → Bestandszeile NULL → Downgrade → Upgrade.
- Offen: /review, Push + PR (Freigabe), Deploy MIT `--migrate` (Backend + Frontend),
  danach Profil am HiFiBerry setzen, gecachte Antwort messen, Nutzer hoert gegen.
- Nicht geprueft: UI im Browser (kommt mit dem Post-Deploy-E2E).

