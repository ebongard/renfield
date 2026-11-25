# Telefonie (SIP / Asterisk)

Der vollständige Plan (Asterisk-Telefonanlage, Netz/Firewall, Phasen 0–7) und das Telephony-Gateway liegen in
einem privaten Repo, weil sie Rufnummern und Infrastrukturdetails enthalten.

## Was davon in Renfield liegt

| Phase | Renfield-Teil | Stand |
|---|---|---|
| 2 – Anruf erkennen | `services/telephony_client.py`: eine **ausgehende** WebSocket-Verbindung zum Gateway, Ereignisse per Push → `NotificationService` | #1650 |
| 3 – Fax | PDF über denselben Kanal abholen → Paperless (Business-Instanz) | offen |
| 4 – Renfield ruft an | `internal.*`-Tool `place_call` über den Kanal, Ziele nur aus einer Allowlist | offen (vgl. #1627) |
| 5 – Renfield nimmt an | Audio über den Kanal → STT → Agent mit eigener Rolle `telephony_guest` → TTS | offen |
| 7 – Satelliten als Freisprechstelle | Vollduplex-Modus `call_audio` (zunächst nur XVF3800 mit Hardware-AEC) | offen |

## Invarianten

- **Richtung:** Renfield verbindet sich zum Gateway. Das Gateway (DMZ) öffnet nie eine Verbindung ins LAN.
- **Trust Boundary:** Das Gateway liefert nur Nummern. Kontakte, Namen, Sichtbarkeit und Empfänger bestimmt Renfield.
- **Anrufer sind nicht authentifiziert** (CallerID fälschbar): Ein Anrufer bekommt nie die Rechte eines Haushaltsmitglieds.
- **Instanztrennung:** Jede Instanz hat ein eigenes Token und sieht nur ihre eigenen Rufnummern.

Konfiguration: `docs/ENVIRONMENT_VARIABLES.md` → „Telefonie“.
