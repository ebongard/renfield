# Runbook: den Haushalt auf auth-on schalten

Kommandofolge für den Cutover der Haushalts-Instanz (`renfield`) von
`AUTH_ENABLED=false` auf `true`. Entwurf und Begründungen:
`docs/design/household-auth-on-cutover.md` — dieser Runbook wiederholt sie nicht,
er führt aus.

**Der Schalter ist klein, der Datenbesitz ist das Projekt.** Unter auth-off
schrieb der Haushalt alles auf einen Rückfall-Eigentümer mit Stufe 0 und wertete
die Zugriffsfilter nie aus. Wer nur den Schalter umlegt, nimmt allen außer dem
Admin die Sicht, trennt die Satelliten und schaltet das Wanddisplay ab.

**Streng nacheinander** (D-10): P1 → P2 → P3. Jede Phase hat einen Haltepunkt,
an dem Du entscheidest. Die Haltepunkte sind der Zweck dieses Dokuments; ein
Skript, das durchläuft, nähme sie weg.

**Diese Anleitung ist kein Skript und soll keines werden.** Sie wird gelesen und
Zeile für Zeile ausgeführt, mit Blick auf die Ausgabe.

---

## 0. Vorbedingungen

Der P0-Bau ist vollständig und gemergt (#1305–#1318). Ohne ihn nicht anfangen.

```bash
kubectl config use-context renfield-private
kubectl -n renfield get deploy backend -o jsonpath='{.spec.template.spec.containers[0].image}'; echo
kubectl -n renfield get cm renfield-env -o jsonpath='{.data.AUTH_ENABLED}'; echo   # erwartet: false
```

Sicherung, bevor irgendetwas geschrieben wird — die Datenänderungen aus P2 sind
teilweise **nicht** rücknehmbar (§10):

```bash
kubectl -n renfield get cronjob                       # pg_dump-Job vorhanden?
kubectl -n renfield create job --from=cronjob/<pg-dump-cronjob> cutover-backup
kubectl -n renfield wait --for=condition=complete job/cutover-backup --timeout=30m
```

Erst weitermachen, wenn die Sicherung **fertig** ist und Du weißt, wo sie liegt.

---

## 1. Vorflug

Sechs Prüfungen. Alle sechs müssen stimmen, bevor P1 beginnt.

### 1.0 Die GERÄTESEITE ist ausgerollt und NACHGEWIESEN — vor allem anderen

Die teuerste Lehre des Haushalt-Cutovers (2026-09-23): der Schalter fiel, und
alle laufenden Satelliten waren binnen Sekunden stumm — der Weg zu ihnen führt
über dieselbe WS-Verbindung, die dann fehlt (OTA), es bleibt Ansible gegen
fragile Pi Zeros. Die Serverhälfte (`SATELLITE_PSK_HANDSHAKE_ENABLED`) nützt
nichts, solange das Gerät keinen `Authorization`-Kopf sendet.

Der Nachweis hat ZWEI Hälften, und keine genügt allein.

**Hälfte A — kann das Gerät den Kopf überhaupt senden?** Auf JEDEM Satelliten,
noch unter auth-off:

```bash
ssh <sat> 'cd /opt/renfield-satellite && venv/bin/python -c "
import asyncio, ssl, websockets
from renfield_satellite.config import load_config
from renfield_satellite.network.websocket_client import _HEADERS_KWARG
c = load_config(\"config/satellite.yaml\")
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
tok = c.server.auth_token or f\"sat.{c.satellite.id}.{c.server.enrollment_token}\"
async def m():
    kw = {_HEADERS_KWARG: {\"Authorization\": \"Bearer \"+tok}, \"ssl\": ctx}
    async with websockets.connect(c.server.url, **kw):
        print(\"HANDSHAKE OK\", _HEADERS_KWARG)
asyncio.run(m())"'
```

Den Kwarg-Namen NIE hart hinschreiben — `_HEADERS_KWARG` wählt ihn aus der
Signatur der installierten `websockets`, und genau die Annahme „der Name ist
doch bekannt" war einer der drei Fehler vom 2026-09-23. Eine Probe mit festem
Namen meldet auf einem Gerät mit websockets ≤ 13 einen `TypeError` und
verurteilt ein Gerät, das mit dem ausgelieferten Code einwandfrei verbindet.

**Hälfte B — ist der PSK auf dem Gerät auch der, den die Datenbank kennt?**
Hälfte A beweist das NICHT: solange `AUTH_ENABLED=false` ist, liest der Server
den Kopf gar nicht (`websocket_auth.py`, `auth_skipped` vor Strategie S). Ein
Gerät mit rotiertem, widerrufenem oder gar keinem PSK sagt fröhlich
`HANDSHAKE OK` und ist nach dem Umlegen trotzdem stumm.

Der Beweis dafür liegt schon vor — wenn die Einschreibung ERZWINGEND ist,
hat jede erfolgreiche Anmeldung genau diesen PSK gegen genau diesen
bcrypt-Hash geprüft:

```bash
kubectl -n renfield get cm renfield-env -o jsonpath='{.data.SATELLITE_ENROLLMENT_ENABLED}'; echo   # muss "true" sein
kubectl -n renfield exec renfield-pg-r1-1 -c postgres -- psql -U postgres -d renfield -c \
  "SELECT satellite_id, is_enabled, revoked_at IS NULL AS aktiv, last_authenticated_at,
          now() - last_authenticated_at AS alter FROM satellites ORDER BY last_authenticated_at DESC NULLS LAST;"
```

Erwartet je Satellit, der leben soll: `is_enabled`, `aktiv`, und ein `alter`
von Minuten — nicht Tagen. **Ein alter Zeitstempel heißt nicht „ruhig", sondern
„dieses Gerät ist schon jetzt offline".** Im Haushalt zeigte genau diese
Abfrage, dass zwei der sechs Satelliten seit Tagen bzw. Wochen weg waren, lange
vor dem Cutover — eine Ausfallliste ohne Zeitstempel ist eine Vermutung.

Erst wenn A und B für JEDEN Satelliten stimmen, geht es weiter.

### 1.1 Die NEUN Schlüssel sind vorbereitet, aber noch nicht gesetzt

Sie werden in P3 **gemeinsam** gesetzt (§7). Jetzt nur ansehen:

| Schlüssel | heute | Cutover |
|---|---|---|
| `AUTH_ENABLED` | `false` | `true` |
| `RENFIELD_ENV` | `development` | `production` |
| `ALLOW_REGISTRATION` | `false` | `false` — **gesetzt lassen**, sonst bricht der Start |
| `CORS_ORIGINS` | `*` | `https://renfield.local` |
| `TRUSTED_PROXIES` | `""` | Traefik-Pod-CIDR |
| `API_RATE_LIMIT_STORAGE_URI` | `memory://` | `redis://redis:6379` |
| `MEMORY_SUBSUME_TO_KG` | (unset) | `false` — sonst startet kein Pod (1.2) |
| `SATELLITE_PSK_HANDSHAKE_ENABLED` | `false` | `true` — **ohne ihn sind die Satelliten tot** |
| `SATELLITE_DEVICE_ACCOUNT` | (unset) | Name des Gerätekontos aus 1.4 |
| `VOICE_BROWSER_CLIENT_ID` | (unset) | die Registry-Zeile der Instanz (Browser-Sprache) |

Der Entwurf (§7) nennt sechs; es sind neun. Die drei letzten fehlten dort und
wurden im Haushalt-Cutover einzeln nachgesetzt.

`AUTH_COOKIE_ENABLED` bleibt **aus** und folgt als P4 nach
`docs/runbooks/cookie-auth-flag-flip-xidra.md` (D-8).

### 1.2 `MEMORY_SUBSUME_TO_KG` muss auf `false` — VOR dem Flag-Wechsel

Ein Startwächter verweigert `MEMORY_SUBSUME_TO_KG=true` zusammen mit
`AUTH_ENABLED=true` (§8.2). Wird das vergessen, startet in P3 **kein
Backend-Pod mehr**, und der Rückweg ist der ConfigMap-Rückbau.

```bash
kubectl -n renfield get cm renfield-env -o jsonpath='{.data.MEMORY_SUBSUME_TO_KG}'; echo
```

Steht dort `true`, gehört die Umstellung auf `"false"` in **denselben Commit und
denselben `apply`** wie die sechs Schlüssel in P3. Nach dem Cutover schaltest Du
Subsume bewusst wieder ein und entfernst den Wächter — er ist datiert, keine
Invariante.

### 1.3 Die Mitgliederliste steht

Das Backfill rät sie nie. Wer Familie ist, wer Gast und welches Konto der
Satellit ist, sind Tatsachen über einen Haushalt, nicht über eine Datenbank.

```bash
kubectl -n renfield exec deploy/backend -- python -c "
import asyncio
from sqlalchemy import text
from services.database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for r in (await db.execute(text(
            'SELECT id, username, is_device_account, speaker_id FROM users ORDER BY id'
        ))).all():
            print(r)
asyncio.run(main())"
```

Notiere: `--admin`, `--family` (der Admin **gehört dazu**), `--guests`,
`--device-account`. Die Liste wird in P2 unverändert benutzt.

### 1.4 Das Gerätekonto ANLEGEN — es existiert noch nicht

```bash
kubectl -n renfield get cm renfield-env -o jsonpath='{.data.SATELLITE_DEVICE_ACCOUNT}'; echo
kubectl -n renfield exec deploy/backend -c backend -- python -c "
import asyncio
from sqlalchemy import text
from services.database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        print((await db.execute(text(
            'SELECT COUNT(*) FROM users WHERE is_device_account'
        ))).scalar_one())
asyncio.run(main())"
```

**Stand 2026-09-23: der Schlüssel ist LEER und kein Nutzer trägt das Kennzeichen.**
Das ist kein Randfall, sondern eine Vorbedingung, ohne die ein gebautes Feature
nicht wirkt: ein Raumverlauf GEHÖRT dem Gerätekonto (§8.1). Ohne eines gibt es
keinen Eigentümer, und weil eine eigentümerlose Zeile auf Stufe 2 niemanden
erreicht, fällt die Stufe auf 0 zurück — die geteilten Raumverläufe aus P0 Nr. 6
funktionieren dann schlicht nicht. Still, ohne Fehlermeldung.

Also VOR P2:

1. Einen Nutzer „Haushalt" anlegen (Admin-Oberfläche), Rolle mit dem
   Anonym-Rechtesatz, **kein** persönliches Konto.
2. `users.is_device_account = true` setzen. Das KENNZEICHEN ist der Vertrag, nie
   der Name — es sperrt Erinnerungs-Extraktion und Präsenzbuchung (D-4b).
3. `SATELLITE_DEVICE_ACCOUNT` in `k8s/configmap.yaml` auf diesen Namen setzen —
   zusammen mit den sechs Schlüsseln in P3, nicht früher.
4. Die Konto-ID notieren: sie ist `--device-account` im Backfill (P2), und ohne
   sie legt das Backfill die Mitgliedschaften des Gerätekontos nicht an.

Ein konfiguriertes, aber nicht auflösbares Gerätekonto **verweigert** jeden
anonymen Satellitenzug — laut, nicht still. Das ist die gewollte Richtung; ein
gar nicht konfiguriertes ist die stille.

### 1.5 `SECRET_KEY` je DEPLOYMENT prüfen, nicht je Instanz

Der Startwächter (`fail_closed_on_insecure_jwt_key`) prüft den Schlüssel bei
`Settings()` — also in JEDEM Pod, der `config.py` importiert, nicht nur im
Backend. Im Haushalt stoppte der `document-worker` in der Cutover-Nacht genau
daran: das Repo-Manifest trug den Schlüssel, live fehlte er.

Die Drift war STUNDEN vorher gefunden und beschrieben, und der
Manifest-Kommentar sagte die Bedingung ausdrücklich — „DRIFT IS EXPECTED HERE
**on the auth-off household**". Ein „erwarteter" Zustand ist bis zu einem
DATUM erwartet: was nur gilt, solange das Flag aus ist, wird mit dem Umlegen
fällig.

Maßgeblich ist nicht „fährt das Backend-Image", sondern „importiert
`config.py`". Der `ami-embedding-eval-job` etwa fährt dasselbe Image, startet
aber ein eigenständiges Skript ohne `Settings()` — er braucht den Schlüssel
nicht. Der `alembic-upgrade-job` dagegen löst ihn über `alembic/env.py` aus und
trägt ihn zu Recht.

```bash
# Für JEDES Deployment/Job, das die Anwendung hochfährt:
for d in backend document-worker meeting-worker pdf-split-worker; do
  printf '%-20s ' "$d"
  kubectl -n renfield get deploy $d -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="SECRET_KEY")].valueFrom.secretKeyRef.name}'; echo
done
# und die Drift-Prüfung einmal in diesem Licht lesen: jedes
# "Repo bewusst voraus" wird mit dem Umlegen fällig.
bin/k8s-drift-check.sh
```

---

## 2. P1 — Konten und Sprecher-Verknüpfungen

Noch unter auth-off. Alles hier ist harmlos und jederzeit rücknehmbar (§10).

1. Für jedes Haushaltsmitglied ein Konto anlegen (Admin-Oberfläche), Rolle
   vergeben.
2. Jedes Konto mit seinem Sprecher verknüpfen (`users.speaker_id`), soweit ein
   Sprecher erkannt wird. Im Haushalt war bisher **einer von fünf** verknüpft —
   ohne Verknüpfung gibt es für diese Person keine Spracherkennung mit Identität,
   also keine Erinnerungen aus Sprachzügen und keine Präsenzbuchung.
3. Das Kiosk-Konto anlegen (Rolle mit `kiosk.view`), falls das Wanddisplay läuft.

**Haltepunkt.** Prüfen, dass jedes Mitglied sich anmelden kann, **bevor** der
Schalter fällt. Ein Konto, das erst nach P3 auffällt, sperrt eine Person aus
einem System aus, das gerade eben noch offen war.

---

## 3. P2 — Backfill

Noch unter auth-off. Die Änderungen sind in diesem Moment **inert** — Stufen und
Mitgliedschaften werden nicht ausgewertet, solange der Schalter aus ist. Das ist
der Sinn: die Daten liegen richtig, bevor der Schalter fällt.

### 3.1 Probelauf

**Das Skript liegt NICHT im Image.** Der Build-Kontext des Backends ist
`src/backend`; `bin/` ist nicht darin. Die Backfill-Skripte werden einzeln in den
Pod kopiert — so sind sie gebaut (ihr Kopfkommentar sagt es), und `python
bin/…` im Pod scheitert mit `No such file or directory`.

```bash
POD=$(kubectl -n renfield get pods --no-headers -o custom-columns=":metadata.name" \
        | grep '^backend-' | head -1)
kubectl -n renfield cp bin/backfill_household_tiers.py $POD:/tmp/bf.py -c backend

kubectl -n renfield exec $POD -c backend -- python /tmp/bf.py \
  --dry-run --admin 1 --family 1,2,3,4,5 --device-account <ID aus 1.4>
```

Die Ausgabe zeigt je Klasse `geändert von geprüft`. Lies sie ganz. Erwartet:

Gemessen am 2026-09-23 (Probelauf, ohne Gerätekonto): **5021 Änderungen** —
kg 4744, Unterhaltungen 208 (45 über Sprecher-Verknüpfung), Null-Relationen 38,
KB-Eigentümer 6, Kopplungs-Rest 1, Mitgliedschaften 20, Erfassungsvorgabe 4;
`admin-atome` 0 von 7525. Weicht Dein Probelauf stark davon ab, ist etwas anders
als erwartet — nachsehen, bevor Du freigibst. **Mit** Gerätekonto steigen die
Mitgliedschaften von 20 auf 26 (die sechs Zeilen des Gerätekontos).

* **kg** bewegt als einzige Klasse wirklich etwas (Stufe 0 → 2).
* **admin-atome** meldet `0` — Dokumente, Fakten und Erinnerungen stehen schon
  auf Stufe 0 und ihr Ziel ist Stufe 0. Die `0` heißt „nichts nötig", nicht
  „vergessen".
* **handgesetzt (unberührt)** nennt die Zahl der Stufen, die jemand bewusst
  gesetzt hat. Sie werden nicht angefasst.
* Meldet der Probelauf **handgesetzte Kanten-Stufen**, die die Kaskade neu
  rechnet: das ist unvermeidbar und verengt nur (`LEAST` der Endpunkte). Zur
  Kenntnis nehmen, nicht abbrechen.

**Haltepunkt: Freigabe.** Die Zahlen ansehen und entscheiden.

### 3.2 Schreiblauf

```bash
kubectl -n renfield exec $POD -c backend -- python /tmp/bf.py \
  --commit --admin 1 --family 1,2,3,4,5 --device-account <ID aus 1.4> \
  --log /tmp/cutover-backfill.json
```

**Die Zählwerte müssen dieselben sein wie im Probelauf** (§11 Nr. 2). Weichen sie
ab, hat sich der Bestand zwischen den Läufen verändert: **nicht weitermachen**,
neu prüfen.

### 3.3 Das Protokoll sichern

```bash
kubectl -n renfield cp $POD:/tmp/cutover-backfill.json ./cutover-backfill.json -c backend
```

**Das ist der einzige Rückweg.** `--revert` verweigert ohne dieses Protokoll, und
zwar mit Absicht: „Admin-Knoten auf Stufe 2" ist genau die Form der handgesetzten
Stufen, die der Hinweg schützt — ein Rückweg, der rät, zerstört sie. Ohne die
Datei gibt es kein Zurück für die Stufen und die Mitgliedschaften.

### 3.4 Die zwei Richtungen prüfen

Eine Mitgliedschaft hat eine **Richtung**: `circle_owner_id` ist der Eigentümer
der Zeile, `member_user_id` der Frager. Für den Raumverlauf braucht es beide.

```bash
kubectl -n renfield exec deploy/backend -- python -c "
import asyncio
from sqlalchemy import text
from services.database import AsyncSessionLocal
async def main():
    async with AsyncSessionLocal() as db:
        for r in (await db.execute(text(
            \"SELECT circle_owner_id, member_user_id, value FROM circle_memberships \"
            \"WHERE dimension = 'tier' ORDER BY circle_owner_id, member_user_id\"
        ))).all():
            print(r)
asyncio.run(main())"
```

Erwartet: jedes Familienmitglied bei jedem anderen (Stufe 2); das Gerätekonto im
Kreis des **Admins** (D-2e: das Gerät liest); **und jedes Familienmitglied im
Kreis des Gerätekontos** — ohne diese Richtung erreicht der Küchenverlauf
niemanden außer dem Admin. Gäste stehen in keiner Zeile (D-2d). Die alte
Kopplungszeile `(1,1,tier,…)` ist **weg**.

---

## 4. P3 — der Schalter

Config ist git, nie `kubectl patch`.

```bash
# 1) k8s/configmap.yaml bearbeiten: die SECHS Schlüssel aus 1.1
#    PLUS MEMORY_SUBSUME_TO_KG: "false"  (1.2 — sonst startet kein Pod)
git add k8s/configmap.yaml && git commit   # ein Commit, alle Schlüssel zusammen
kubectl -n renfield apply -f k8s/configmap.yaml
kubectl -n renfield rollout restart deploy/backend deploy/document-worker \
        deploy/meeting-worker deploy/pdf-split-worker
kubectl -n renfield rollout status deploy/backend --timeout=600s
```

**Startet der Pod nicht, lies die Logs, bevor Du irgendetwas änderst.** Die
Startwächter brechen absichtlich laut ab und sagen jeweils, was fehlt:

```bash
kubectl -n renfield logs deploy/backend --tail=50 | grep -iE "inconsistent|MEMORY_SUBSUME|ALLOW_REGISTRATION|SECRET_KEY"
```

---

## 5. Abnahme (§11)

Nach dem Rollout, in dieser Reihenfolge:

1. **Anmeldung** jedes Mitglieds; erzwungene Passwortrotation läuft durch.
2. **Wissen:** jedes Mitglied sieht den Wissensgraphen (Stufe 2), **nicht** die
   Dokumente und Erinnerungen des Admins (Stufe 0).
3. **Satellit, unerkannte Stimme:** Licht ja; Kamera nein, `call_service` nein,
   Scan nein, Kalender lesen nein, Fernseher nein (D-4a-2) — jeweils als
   **gesprochene Ablehnung**, nicht als freie Antwort des Modells. Keine
   Erinnerung geschrieben, keine Präsenz gebucht.
4. **Satellit, erkannter Sprecher:** ersetzt das Gerätekonto; die Erinnerung des
   Zuges gehört ihm.
5. **Geteilter Raumverlauf:** A fragt, B hakt nach — **ein** Verlauf. Beide sehen
   ihn in ihrer Liste. Löschen nur mit `chat.all`.
6. **Kiosk:** Kiosk-Konto öffnet `/kiosk`; Admin-Routen 403.
7. **Browser-E2E** (`smoke-tester`), Rückweg geprobt.

---

## 6. Rückweg (§10)

```bash
# k8s/configmap.yaml: AUTH_ENABLED zurück auf "false"
kubectl -n renfield apply -f k8s/configmap.yaml
kubectl -n renfield rollout restart deploy/backend
```

Das stellt **jeden Lesepfad byte-identisch** wieder her; die Stufen und
Mitgliedschaften sind dann wieder inert. `RENFIELD_ENV` kann auf `production`
bleiben.

Die Stufen zusätzlich zurücknehmen — nur mit dem Protokoll aus 3.3:

```bash
kubectl -n renfield cp bin/backfill_household_tiers.py $POD:/tmp/bf.py -c backend
kubectl -n renfield cp ./cutover-backfill.json $POD:/tmp/cutover-backfill.json -c backend
kubectl -n renfield exec $POD -c backend -- python /tmp/bf.py \
  --commit --revert --admin 1 --family 1,2,3,4,5 --device-account <ID aus 1.4> \
  --log /tmp/cutover-backfill.json
```

**Was nicht rückbaubar ist,** und wovon Du wissen sollst, bevor Du umschaltest:

* Eigentümerlose Zeilen, die einen Eigentümer bekommen haben — nichts hält fest,
  dass sie keinen hatten. Betrifft Unterhaltungen, Wissensbasen und Relationen
  ohne Eigentümer. Der Rückweg sagt das an, statt es vorzutäuschen.
* Erinnerungen, die zwischen Umschalten und Rückbau beim richtigen Eigentümer
  entstanden sind. Sie bleiben ihm zugeordnet und werden unter auth-off wieder
  für alle sichtbar — der heutige Zustand, kein Leck.

---

## 7. Danach

* `MEMORY_SUBSUME_TO_KG` bewusst wieder einschalten und den Wächter
  `assert_subsume_is_single_user` entfernen (§8.2).
* P4: Cookie-Modus nach `docs/runbooks/cookie-auth-flag-flip-xidra.md` (D-8).
* `docs/CIRCLES.md` und `.claude/rules/circles.md` auf den neuen Stand bringen.
