# GPU-Fallback auf Host D (kalt)

**Stand:** 2026-11-23 (#1636). Proxmox-Host D (V100 32 GB + RTX 4060 Ti 16 GB) kann die
Produktions-GPU-Schicht beider Instanzen übernehmen, wenn eine Karte ausfällt. **Kalt** heißt:
nichts schaltet von selbst. Host D wird regelmäßig ausgeschaltet; umgeschaltet wird von Hand mit
`bin/gpu-failover.sh`, und zurück ebenso. Bestand und Rollen: `docs/GPU_TOPOLOGY.md` §1/§2/§6.

## Was wohin geht

| Teil | Ausfall von | Schlüssel in `renfield-env` | Ziel auf Host D (`gpu-ct`, 192.168.1.137) |
|---|---|---|---|
| `llm` | 5090 (`cuda.local:8081`): Chat, Agent, Gedächtnis, Vision | `LLM_OPENAI_BASE_URL`, `OLLAMA_VISION_URL` | `llama-server` auf der V100, `:8081/v1` |
| `ollama` | 5070 Ti (Cluster-Ollama): Intent, Router, Embeddings, Chat-Fallback | `OLLAMA_URL`, `OLLAMA_EMBED_URL`, `AGENT_ROUTER_URL`, `LLM_OPENAI_EMBED_BASE_URL` | Ollama 0.30.8 auf der 4060 Ti, `:11434` |
| `voice` | 5060 Ti (voice-server): STT, TTS, Sprecher, Meetings | `VOICE_SERVER_URL` | voice-server auf der 4060 Ti, `:8091` (anonym) |

Nur diese Schlüssel ändert das Skript, sonst nichts. Ein Schlüssel, der in git leer ist, bleibt
leer: xidra hat Voice aus, und der Fallback schaltet es dort nicht ein.

## Ablauf

1. **Host D einschalten.** gpu-ct startet zuerst, die Testinstanz danach. `llama-server` braucht
   nach einem Kaltstart **etwa 1:50 min**, bis das Modell geladen ist (vorher HTTP 503).
2. **Prüfen:** `bin/gpu-failover.sh status`. Das Skript fragt die Endpunkte **aus dem
   Backend-Pod** jeder Instanz ab, also auf demselben Weg, den das Backend nimmt. Alle
   Host-D-Zeilen müssen `OK` zeigen.
3. **Umschalten, nur den ausgefallenen Teil:**
   `bin/gpu-failover.sh to-hostd llm` (oder `ollama`, `voice`, `all`; `--instance household|xidra|both`,
   Vorgabe `both`; `--dry-run` zeigt nur an). Ist ein Endpunkt auf Host D nicht gesund, ändert
   das Skript **nichts**. Sonst setzt es die Schlüssel, markiert die ConfigMap
   (`renfield.io/gpu-failover`) und startet Backend und Worker neu. Der Init-Container wartet
   dabei bis zu 30 s auf `cuda.local` (`WAIT_CUDA_MAX_SECONDS`) und läuft dann weiter.
4. **Zurück, sobald die Produktion wieder gesund ist:** `bin/gpu-failover.sh to-prod <teil>`. Das
   Skript setzt die Werte **aus git** zurück und verweigert das, solange der Produktionsendpunkt
   nicht antwortet (`--force` überstimmt). Danach muss `status` „GPU keys = git" zeigen; die
   Markierung verschwindet erst, wenn kein Schlüssel mehr abweicht.

**Warum ein Live-Patch hier erlaubt ist:** Sonst gilt „nie `renfield-env` live patchen"
(`.claude/rules/deploy.md`). Der Fallback ist die eine Ausnahme, weil er ein vorübergehender
Zustand ist, und `to-prod` beendet ihn auf den Stand in git. Ein Deploy während des Fallbacks
(`kubectl apply` der ConfigMap) schaltet stillschweigend zurück auf die ausgefallene Karte.
Deshalb während des Fallbacks nicht deployen, oder danach `to-hostd` erneut ausführen.

## Grenzen

- **Langsamer:** Die V100 dekodiert mit etwa 0,4× der 5090 (§4.6). Sie drosselt bei 83 °C
  thermisch; das ist eine Frage des Luftstroms, ein Power-Limit hilft nicht.
- **Browser-Voice bleibt beim Voice-Fallback weg.** Das Skript schaltet nur den Weg über das
  Backend (Satelliten, App). Die IngressRoutes für `wss://…/ws/voice`
  (`private_k8s/voice-server/41-/42-*`) zeigen weiter auf den Cluster-Dienst.
- **Nicht abgedeckt:** Reva (`ollama.test.local`, `voice.test.local`) und der Twin
  (`TWIN_OLLAMA_URL=cuda.local:11434`) haben eigene Konfigurationen.
- **Ollama-Version:** Host D ist auf 0.30.8 gepinnt (geteilter Modellspeicher nur lesend, §6),
  der Cluster läuft mit `:latest` (2026-11-23: 0.35.1). Neue Modelle zieht nur der Cluster-Pod;
  was dort nicht im Speicher liegt, fehlt auch auf Host D.
- **Feste Adresse:** Die Ziele sind IP-Adressen (Pods lösen kein mDNS auf). gpu-ct
  (192.168.1.137) und die Testinstanz (192.168.1.51) brauchen deshalb eine feste
  DHCP-Zuweisung auf der OPNsense. Ohne sie zeigt ein Fallback nach einem neuen Lease ins Leere,
  und das Skript bricht dann am Probe-Schritt ab.

## Bausteine

| Datei | Was |
|---|---|
| `bin/gpu-failover.sh` | das Umschalten (`status`, `to-hostd`, `to-prod`); Tests: `tests/backend/test_gpu_failover.py` |
| `deploy/hostd/llama-server.service` | V100, Modell auf der lokalen SSD, persistenter PTX-JIT-Cache |
| `deploy/hostd/ollama.service` | 4060 Ti, 0.30.8, Cluster-Modellspeicher nur lesend |
| `deploy/hostd/voice-server.service` | 4060 Ti, Produktions-Digest, lokaler beschreibbarer HF-Cache |
| `deploy/hostd/renfield-test/` | die Testinstanz (LXC 101), README mit Installation und Update |

Bilder: Harbor-Robot `robot$renfield+hostd-pull` (nur Pull, Projekt `renfield`). Der Token liegt
nur in der Docker-Konfiguration der beiden Container. Die Registry ist dort auf das LAN gepinnt
(`private_k8s/docs/harbor-registry-internal-pin.md`).

## Übungen

| Datum | Instanz | Teil | Ergebnis |
|---|---|---|---|
| — | — | — | noch keine; die erste ist auf xidra geplant, danach der Haushalt |
