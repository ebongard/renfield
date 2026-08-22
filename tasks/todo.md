# H1/H6-Security-Rollout + P0-Fleet — chore/h1-h6-security-rollout

Aus der priorisierten Offene-Punkte-Liste ("make it so"), Stand 2026-08-22 abends.

## Befund (Korrekturen zur Doku)
- Esszimmer LÄUFT (Node Ready, Pod 12d, authentifiziert) — P0-Annahme überholt
- H1 weiter als dokumentiert: ENROLLMENT_ENABLED=true (PERMISSIVE), 5/6 Sats enrolled+authenticated
- NEU-P0: 4 Pis seit Wochen OFFLINE (arbeitszimmer .72 seit 19.07., fitnessraum .225 seit 13.07., kinderbad .206 seit 20.07., wohnzimmer .193 seit 06.08.) — kein SSH, physisch aus/gestrandet

## Erledigt (dieser Branch / diese Session)
- [x] sat-benszimmer enrolled (PSK via Admin-API), Token in gitignored host_var
- [x] H6: Ed25519-Keypair generiert (~/.renfield/ota_release_key, nur Workstation), Release v1.4.6 signiert+verifiziert, RELEASE_MANIFEST.json+.sig committet
- [x] group_vars: satellite_release_pubkeys mit Key #1; require_signature bleibt false (verify-if-present) bis Fleet-Provisionierung
- [x] ConfigMap: SATELLITE_ENROLLMENT_AUTOFLIP_ENABLED=true (Latch feuert erst, wenn benszimmer mit Token authentifiziert)

## Offen — braucht Freigabe/Physik
- [ ] benszimmer-Pi (192.168.1.176) provisionieren (--tags config → Service-RESTART; Pi-SD-Risiko → Freigabe!)
- [ ] 4 offline Pis: Netzteil/Steckdose prüfen (USER vor Ort); nach Wiedereinschalten authentifizieren sie mit vorhandenen Tokens
- [ ] Fleet-Provisionierung satellite_release_pubkeys auf alle Pis (gleiche Restart-Runde), DANACH require_signature-Flip (satellit + backend SATELLITE_OTA_REQUIRE_SIGNATURE)
- [ ] Speaker-Enrollment: 3 Mitglieder via "Sprecher einlernen" (USER spricht), dann purge_unknown_speakers --commit, dann Threshold-Kalibrierung + Phase-3-Flip
- [ ] Login/User-Mgmt-Audit (nächstes großes Paket)
