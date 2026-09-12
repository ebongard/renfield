# renfield-pg Ausfall — Befund & Wiederherstellungsplan (2026-09-12)

## Befund (gemessen, nicht vermutet)

**Fehlerkette, beide Instanzen identisch:**
1. `2026-09-11 06:20 UTC` — die Longhorn-Replica von `renfield-pg-3` (Daten + WAL,
   beide Namespaces) auf **k8s-gpu-1** fällt aus → Volumes `faulted`.
   `longhorn-pg` hat `numberOfReplicas: 1` → kein Ersatz vorhanden.
2. Longhorn kann keine neue Replica anlegen: die Disks auf gpu-1/gpu-2 stehen auf
   `Schedulable: False` (25-%-Mindestfreiraum unterschritten; VGs zu 100 % belegt).
   → `renfield-pg-3` bleibt tot, seither ~290 Restarts.
3. Der HA-Replication-Slot `_cnpg_renfield_pg_3` auf dem Primary hält damit WAL
   dauerhaft fest. `max_slot_wal_keep_size` ist **nicht gesetzt** = unbegrenzt.
4. `archive_timeout: 5min` erzwingt einen WAL-Switch alle 5 min → ~4,6 GB WAL/Tag,
   auch im Leerlauf. Das 2-GiB-WAL-Volume ist damit in ~10 h voll.
5. `2026-09-11 16:16/16:17 UTC` — beide Primaries: 
   `PANIC: could not write to file "pg_wal/xlogtemp": No space left on device`,
   `restart_after_crash=off` → Cluster-Status `Not enough disk space`.

**Zeitachse:** Vollausfall seit ~29 h (nicht 8 Tage). Die Pods sind 9 Tage alt —
das ist das letzte Rollout, nicht der Ausfallbeginn.

**Datenlage:** Die Datenvolumes von pg-1 und pg-2 sind in beiden Namespaces
`attached / healthy`. Letztes vollständiges Backup in Garage-S3: 2026-09-11 02:30,
`completed`, WAL-Archivierung lief bis zum PANIC. Datenverlustrisiko: nahe null.
Nur pg-3 ist verloren (Einzel-Replica, kein Ersatz) — wird neu aufgebaut.

**Betroffen:** Haushalt (ns `renfield`) und xidra (ns `renfield-xidra`) vollständig
— beide Backends zeigen `ConnectionRefused` gegen `renfield-pg-rw`.

## Plan

### Phase 0 — Kapazität (Voraussetzung für alles Weitere)
- [ ] Ungenutzte Container-Images auf gpu-1/gpu-2 entfernen (`crictl rmi --prune`),
      verwaiste Longhorn-Replicas gelöschter Volumes aufräumen
- [ ] Ziel: Longhorn-Disks gpu-1/gpu-2 wieder `Schedulable: True`
- [ ] Vorsicht: Harbor-WAN ist auf ~41 Mbit/s gedeckelt — nur wirklich
      unreferenzierte Images entfernen

### Phase 1 — WAL-Luft schaffen + Ursache beheben (Git, nicht kubectl patch)
- [ ] `k8s/cnpg/20-cluster-renfield-pg.yaml` + xidra-Pendant:
      `walStorage.size: 2Gi → 8Gi`
- [ ] `max_slot_wal_keep_size: 2GB` ergänzen — ein verwaister Slot wird künftig
      invalidiert, statt den Primary zu töten
- [ ] `kubectl apply` → CNPG vergrößert die PVCs

### Phase 2 — Cluster hochfahren (erst Haushalt, dann xidra)
- [ ] Tote pg-Pods löschen → Neustart auf vergrößertem WAL-Volume
- [ ] Crash-Recovery + Checkpoint beobachten
- [ ] ACHTUNG `synchronous: {number: 1, dataDurability: required}`: solange nur
      EINE Instanz läuft, blockieren Schreibzugriffe. Erst weitermachen, wenn
      pg-1 UND pg-2 stehen.
- [ ] Verifizieren: `pg_replication_slots`, WAL-Verzeichnisgröße, Archivierung

### Phase 3 — pg-3 neu aufbauen
- [ ] Faulted PVCs von pg-3 löschen → CNPG bootstrappt die Instanz neu
- [ ] Warten auf 3/3

### Phase 4 — Anwendung
- [ ] Backend + Worker in beiden Namespaces neu starten
- [ ] Browser-E2E beide Instanzen (Pflicht), PWA-Service-Worker vorher entladen

### Phase 5 — Vorbeugung (eigener PR)
- [ ] Alarmierung: CNPG-Cluster nicht `Ready` / WAL-Volume-Füllstand.
      Der eigentliche Mangel ist, dass 29 h Ausfall unbemerkt blieben.
- [ ] Knotenkapazität: alle drei VGs sind zu 100 % belegt — dauerhafte Lösung
      liegt auf der Proxmox-Ebene (Disk vergrößern)
- [ ] `archive_timeout: 5min` bewerten: erzeugt 4,6 GB WAL/Tag im Leerlauf


---

# Abschluss 2026-09-12 14:05 UTC

## Ursache (korrigiert gegenüber der Erstannahme)

Nicht die zu kleinen Platten, sondern **`unattended-upgrades`**:

```
11.09. 06:17:44  k8s-gpu-2  systemd: Stopping/Starting iscsid.service
11.09. 06:19:39  k8s-gpu-1  systemd: Stopping/Starting iscsid.service
11.09. 06:20:15  Longhorn:  Replicas von renfield-pg-3 faulted (beide Namespaces)
11.09. 16:16:19  Postgres:  PANIC "No space left on device" (Haushalt)
11.09. 16:17:30  Postgres:  dito (xidra)
```

`apt-daily-upgrade` startet den iSCSI-Initiator neu, über den Longhorn JEDES
Volume anbindet. Die Sessions reißen; `numberOfReplicas: 1` lässt keine zweite
Kopie zu → pg-3 tot in beiden Instanzen → dessen HA-Replikationsslot hält WAL
fest (`max_slot_wal_keep_size` war ungesetzt = unbegrenzt) → bei ~4,6 GB WAL/Tag
(erzwungen durch `archive_timeout: 5min`) lief das 2-GiB-WAL-Volume in ~10 h voll
→ Primary PANIC. Die kleinen WAL-Volumes waren der **Verstärker**, nicht die
Ursache. Ein nächtlicher Paket-Job hat zwei Produktionsdatenbanken abgeschaltet.

Vollausfall: 11.09. 16:16 bis 12.09. 13:51 UTC, rund **21,5 Stunden** (nicht
8 Tage — das war das Alter der Pods seit dem letzten Rollout).

## Ergebnis

Beide Instanzen laufen wieder, wiederhergestellt aus den Garage-S3-Sicherungen:

| | Haushalt | xidra |
|---|---|---|
| Cluster | `renfield-pg-r1` 3/3 gesund | `renfield-pg-r1` 3/3 gesund |
| Wiederherstellungspunkt | 11.09. 16:15:56 | 11.09. 16:17:07 |
| Absturz war | 16:16:19 | 16:17:30 |
| **Lücke** | **23 Sekunden** | **23 Sekunden** |
| Umfang | 380 Dok. / 2847 Chunks / 2677 Nachr. | 546 Dok. / 4368 Chunks |
| Alembic | `pc20260908b_cred_sphere` | `pc20260908b_cred_sphere` |

Browser-E2E Haushalt: Seite lädt, Verlauf rehydriert, WebSocket verbunden,
neuer Chat-Zug geschrieben (2 Nachrichten um 14:00:56) und auf dem Standby
angekommen, null Anwendungsfehler in der Konsole. xidra: Anmeldeseite und
Backend sauber — der angemeldete Durchlauf bleibt beim Benutzer (auth-on).

## Erledigt

- [x] Ursache gefunden und **abgestellt**: `k8s/node-iscsi-update-guard.sh` auf
      allen drei Knoten — `open-iscsi` auf hold + aus unattended-upgrades
      ausgenommen (gpu-3 hatte es am 12.09. 06:57 ebenfalls erwischt)
- [x] `max_slot_wal_keep_size: 2GB` in beiden Cluster-Manifesten — ein verwaister
      Slot wird künftig invalidiert, statt den Primary zu töten. Live verifiziert.
- [x] WAL-Volumes 2 GiB → 8 GiB
- [x] Knoten-Platten vergrößert: gpu-1 80→140 GB, gpu-2 80→140 GB,
      gpu-3 100→160 GB. Longhorn-Spielraum von ~6 GiB auf 75-82 GiB je Knoten.
- [x] Aufgeräumt: 4 defekte pg-3-Volumes, speaches-Cache (seit 130 Tagen auf 0/0),
      alte Container-Images
- [x] `k8s/cnpg/90-recovery-cluster.yaml` + `91-netpol-recovery.yaml` — das
      Wiederherstellungsverfahren als Manifest mit Runbook, nicht als Handgriff
- [x] Anwendung + pg-dump-CronJob + ScheduledBackups auf `renfield-pg-r1-rw`
      umgestellt (`set env`, nicht `apply` — Live-Deployments tragen gepinnte Tags)
- [x] Frische Basissicherung beider Cluster, ContinuousArchiving=True

## Was schiefging (für die Lessons)

Ich habe die Engine-Ressource des WAL-Volumes gelöscht, um die veraltete
iscsid-PID loszuwerden. Das erzeugte eine Verklemmung in Longhorns Steuerebene,
aus der ich mit sechs Versuchen nicht herauskam. Richtig wäre gewesen, das
Volume erst vollständig abzulösen ODER direkt den geprobten
Wiederherstellungspfad zu nehmen. Die Wiederherstellung dauerte am Ende
vier Minuten.

## Offen

- [ ] **Alarmierung** — 21,5 Stunden Ausfall blieben unbemerkt. Das ist der
      wichtigste offene Punkt, wichtiger als alles technisch Reparierte:
      CNPG-Cluster nicht `Ready`, WAL-Füllstand, Backend down.
- [ ] Alte `renfield-pg`-Cluster + verwaiste Longhorn-Volumes löschen (nach Soak)
- [ ] `numberOfReplicas: 1` auf `longhorn-pg` überdenken — machte den
      iscsid-Neustart überhaupt erst tödlich
- [ ] `kubeadm`/`kubectl`/`kubelet` nur auf gpu-3 auf hold, nicht auf gpu-1/gpu-2
- [ ] `archive_timeout: 5min` erzeugt 4,6 GB WAL/Tag im Leerlauf — prüfen
- [ ] Paperless unter 192.168.1.162 antwortet mit HTTP 500 (separates Problem,
      fiel beim Neustart des geplanten Dedupe-Tasks auf)
- [ ] Clusternamen tragen jetzt das `-r1`-Suffix — irgendwann zurückbenennen

---

# Alarme und Funktionsprüfungen (Haushalt + xidra)

Erhoben am 2026-09-12 aus zwei Ausfällen derselben Sitzung. Die
infrastrukturnahen Punkte (Paperless-Container auf `192.168.1.162`, CNPG,
Longhorn) liegen bewusst **nicht** hier, sondern in
`private_k8s/tasks/todo-integrationen-2026-09-12.md`. Hier steht nur, was
Renfield selbst betrifft — und das gilt für beide Instanzen gleichermaßen.

## Der Befund

Es fehlt keine Alarmierung. **Sie ist da, sie ist eingeschaltet, und sie hat
geschwiegen.**

| Ausfall | Dauer | `MCP_HEALTH_MONITOR_ENABLED` | `PROACTIVE_ENABLED` | Alarm |
|---|---|---|---|---|
| Paperless Haushalt tot | 3 d 10 h | `true` | `true` | **keiner** |
| `renfield-pg` beide Instanzen tot | 21,5 h | `true` | `true` | **keiner** |

Beide Male stand die Statusanzeige auf Grün. Bemerkt wurden beide Ausfälle von
einem Menschen, der zufällig hinsah.

## A1 — Funktionssonde statt Verbindungsprüfung

**Die Ursache des Schweigens.** `mcp_health_monitor.monitor_tick`
(`services/mcp_health_monitor.py:203`) wertet ausschließlich
`MCPManager.get_status()` aus (Zeile 231). Das prüft, ob die Verbindung zum
MCP-**Prozess** steht — nicht, ob der Dienst dahinter arbeitet. Bei
`stdio`-Servern läuft dieser Prozess **im Backend-Image selbst**, meldet also
zuverlässig „verbunden", ganz gleich wie es dem Ziel geht.

Gemessen am 12.09.: `/api/mcp/status` meldete für den Haushalt 13 von 13
Servern `healthy`. Paperless war seit drei Tagen tot, n8n aus dem Cluster gar
nicht erreichbar. Beide grün.

- [ ] **A1a** — Pro Server eine billige, lesende Funktionssonde definieren,
      konfigurierbar in `mcp_servers.yaml` (ein Werkzeugname plus erwartete
      Mindestantwort). Kandidaten aus der Prüfung vom 12.09.:
      | Server | Sonde | Was sie gefangen hätte |
      |---|---|---|
      | `paperless` | `/api/statistics/` bzw. ein lesendes Tool | den 3-Tage-Ausfall am ersten Tag |
      | `search` | eine Suche, Trefferzahl > 0 | die CAPTCHA-Fälle, bei denen SearXNG 0 Treffer liefert und trotzdem grün meldet |
      | `homeassistant` | `get_states`, Entitätenzahl > 0 | einen toten HA-Token |
      | `n8n` | Workflows auflisten | dass der Name auf eine öffentliche IP zeigt |
- [ ] **A1b** — Verdikt in `_server_health` einspeisen, damit Kiosk und
      `internal.system_health` es mittragen. Der Alarmweg existiert bereits
      (`NotificationService.process_webhook` an `active_admin_ids`,
      `mcp_health_monitor.py:62-84`) — es fehlt nur der Anlass.
- [ ] **A1c** — Eigenes Flag, zunächst dunkel; Sonden müssen lesend, billig und
      einzeln abschaltbar sein. Eine Sonde, die selbst Last erzeugt, ist
      schlimmer als keine.

## A2 — Alarm bei wiederholt scheiternden geplanten Aufgaben

Der Task „Paperless-Duplikate aufräumen" ist **50 Mal in Folge** gescheitert,
alle fünf Minuten, über anderthalb Tage. Die Lauf-Historie hat jeden einzelnen
Fehlschlag sauber protokolliert (`ScheduledTaskRun`,
`models/database.py:1484`; `last_status`, Zeile 1471). Niemand hat es erfahren.

- [ ] **A2a** — Nach N aufeinanderfolgenden `error`-Läufen genau **eine**
      deduplizierte Benachrichtigung an den Eigentümer-Admin, nicht eine pro
      Lauf. Muster dafür steht im Fristen-Notifier (`obligation_acknowledgements`
      als Ledger gegen Wiederholung).
- [ ] **A2b** — Dabei mitprüfen, ob ein Fünf-Minuten-Takt für einen
      Archiv-Dedupe die richtige Frequenz ist. Er hat 50 Fehlläufe in
      anderthalb Tagen erzeugt — das ist auch Protokollrauschen.

## A3 — Wer merkt, dass Renfield selbst weg ist?

Der 21,5-Stunden-Ausfall ist der unangenehme Fall: beide Backends lagen in
`CrashLoopBackOff` beziehungsweise meldeten `ConnectionRefused` gegen die
Datenbank. **Ein System, das selbst steht, kann sich nicht selbst melden.**
Dieser Punkt ist in Renfield allein nicht lösbar.

- [ ] **A3a** — Naheliegender Weg, weil er nichts Neues braucht: die beiden
      Instanzen sind vollständig unabhängige Deployments in getrennten
      Namespaces. Jede prüft periodisch den `/health` der anderen und meldet
      Ausbleiben über ihren eigenen, bereits vorhandenen Proaktiv-Kanal.
      Gegenseitige Beobachtung, kein zusätzlicher Dienst.
- [ ] **A3b** — Alternative oder Ergänzung auf Clusterebene (Deployment nicht
      verfügbar, CNPG nicht `Ready`): gehört dann nach `private_k8s`, nicht
      hierher.
- [ ] **A3c** — Ehrlich bleiben, was A3a **nicht** kann: fallen beide
      gleichzeitig aus — wie am 11.09., als derselbe `iscsid`-Neustart beide
      Datenbanken erschlug — schweigt auch die gegenseitige Prüfung. Dagegen
      hilft nur ein Beobachter außerhalb des Clusters.

## Abgrenzung

Nicht hier, sondern in `private_k8s`: der fehlende Healthcheck am
Paperless-Container auf `192.168.1.162` (drei Tage `restart: unless-stopped`
auf einem Container, der ausschließlich HTTP 500 auslieferte), Alarme auf
CNPG-Cluster-Zustand und WAL-Füllstand, sowie Longhorn-Replica-Ausfälle.
