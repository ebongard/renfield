# GPU-Bestand und Beschaffungsentscheidungen

**Stand:** 2026-09-18, gemessen auf den Nodes (`nvidia-smi`, `lspci`, `kubectl`) und im
laufenden `voice-server`-Pod — nicht aus älteren Dokumenten übernommen.

Dieses Dokument hält zwei Dinge fest: **welche GPUs es gibt und was darauf läuft**, und
**welche Karten überhaupt in Frage kommen**, wenn eine dazukommen soll. Der zweite Teil
existiert, weil die Antwort darauf nicht am Speicherausbau hängt, sondern an zwei
Kompatibilitätsgrenzen, die man vor dem Kauf prüfen muss.

## 1. Bestand

| Host | GPU | VRAM | Treiber | Compute Capability | k8s-Zuteilung |
|---|---|---|---|---|---|
| k8s-gpu-1 (192.168.1.180) | **1×** RTX 5070 Ti | 16303 MiB | 570.211.01 | 12.0 | **DRA-Claim** `ollama-gpu-5070ti` |
| k8s-gpu-2 (192.168.1.148) | **keine** | – | – | – | 0 — CPU-Node |
| k8s-gpu-3 (192.168.1.254) | RTX 4060 Ti | 16380 MiB | **595.84** | 8.9 | `nvidia.com/gpu: 1` (voice-server, ns `voice`) |
| cuda.local (192.168.1.227) | RTX 5090 | 32607 MiB | 575.64.03 | 12.0 | kein k8s-Node |

**Es gibt derzeit keinen freien GPU-Platz im Cluster.** Die zweite Karte in `k8s-gpu-1`
(RTX 5060 Ti) ist ausgefallen — **Xid 79** — und physisch nicht mehr vorhanden. Seither
läuft die Zuteilung dort über einen **DRA-Claim** statt über das Device-Plugin, weil
das Plugin nach dem Ausfall nichts mehr zuteilen konnte.

`k8s-gpu-2` trägt den Namen zu Unrecht: Die VM hat keinen GPU-Passthrough, `lspci` zeigt
nur eine QEMU-VGA. Die installierten NVIDIA-Pakete ändern daran nichts.

**Veraltete Manifeste:** `k8s/llama-server.yaml` fordert **2 GPUs** auf `k8s-gpu-1` an
(`replicas: 0`) und passt nicht mehr zur Ein-Karten-Bestückung. `k8s/speaches.yaml`
ebenso (`replicas: 0`).

## 2. Was worauf läuft

- **cuda.local (5090)** — das Text-Hauptmodell `qwen3.6-35b-a3b` über llama.cpp
  (`--ctx-size 262144`, KV-Cache `q8_0`, `--flash-attn on`), rund 26,5 von 32 GB belegt.
  **Zusätzlich der VLM-/OCR-Pfad**: `OLLAMA_VISION_URL` und `OLLAMA_VISION_MODEL=qwen3.6`
  zeigen hierher, also laufen auch OCR-Nachbearbeitung und PDF-Split hier.
- **k8s-gpu-1 (5070 Ti)** — Cluster-Ollama mit llama3.2:3b, qwen3:8b, qwen3-embedding:4b
  und nomic-embed-text, zusammen etwa 12,5 von 16,3 GB, geteilt mit Reva.
  `qwen3-vl:8b` lief früher hier und brauchte gemessen **11,9 GB** — es verdrängte alle
  anderen Modelle und wurde deshalb auf die 5090 verlegt.
- **k8s-gpu-3 (4060 Ti)** — voice-server: faster-whisper `medium` mit `int8_float16`,
  Sprechererkennung (ECAPA) über onnxruntime, Piper-TTS; pyannote-Diarisierung nur bei
  `MEETING_ENABLED`.
- **Ohne GPU:** Backend (`torch==2.6.0+cpu`), document-worker, pdf-split-worker und
  meeting-worker — die rufen nur nach außen.

## 3. Die zwei Grenzen, die eine Karte ausschließen können

**Grenze A — die `arch_list` des ausgelieferten PyTorch.** Im laufenden voice-server-Pod
gemessen:

```
torch 2.7.1+cu128
arch_list ['sm_75','sm_80','sm_86','sm_90','sm_100','sm_120','compute_120']
```

Eine Karte, deren Compute Capability hier fehlt, läuft **nur**, wenn ein passendes PTX
vorhanden ist — und PTX ist ausschließlich aufwärtskompatibel. `compute_120` hilft
älteren Karten nicht. Der Fehler zeigt sich nicht beim Import, sondern erst beim ersten
Kernel: `no kernel image is available for execution on the device`.

Warum es heute passt: `sm_89` (4060 Ti) wird vom `sm_86`-Cubin bedient, die 5090 trifft
`sm_120` direkt.

**Grenze B — das Treiberband.** Blackwell verlangt mindestens Treiber 570. Für
Maxwell, Pascal und Volta ist der **580er-Zweig der letzte** mit Feature-Updates,
danach bis Oktober 2028 nur noch Sicherheitspatches. Wer eine dieser alten
Architekturen mit einer Blackwell-Karte **im selben Host** betreibt, sitzt dauerhaft im
schmalen Band **570–580** fest.

- [Phoronix: 580 ist der letzte Zweig für Maxwell/Pascal/Volta](https://www.phoronix.com/news/NVIDIA-580-Linux-Driver-Last-HW)
- [NVIDIA-KB: Support-Plan für Maxwell, Pascal, Volta](https://nvidia.custhelp.com/app/answers/detail/a_id/5706/)

Belegt ist damit der Linux-GeForce/Quadro-Zweig. Ob der **Data-Center-Zweig** dieselbe
Grenze zieht, ist **nicht verifiziert** — vor dem Kauf einer Tesla-Karte zu prüfen.

**Prüfreihenfolge vor jedem GPU-Kauf:** (1) Steht die Compute Capability in der
`arch_list` oben? (2) Liegt der nötige Treiber im selben Band wie der der anderen Karten
dieses Hosts? (3) Erst danach über VRAM und Bandbreite reden.

## 4. Entscheidungsnotiz: Tesla V100 32 GB (Volta, sm_70) — 2026-09-18

**Frage:** Bringt eine gebrauchte V100 mit 32 GB etwas?

**Befund gegen die zwei Grenzen:**

- **Grenze A verletzt.** `sm_70` fehlt in der `arch_list`, PTX hilft nicht. Volta wurde
  im cu128-Build entfernt (`#removing sm_50-sm_70` im
  [Build-Skript v2.7.1](https://raw.githubusercontent.com/pytorch/pytorch/v2.7.1/.ci/manywheel/build_cuda.sh),
  [dev-discuss](https://dev-discuss.pytorch.org/t/dropping-volta-support-from-cuda-12-8-binaries-for-release-2-11/3290)).
  Betroffen ist genau **ein** Konsument, dieser aber vollständig: die
  **pyannote-Diarisierung** der Meeting-Protokolle.
- **Nicht betroffen:** Whisper über ctranslate2 (`Common` enthält 7.0; das konfigurierte
  `int8_float16` verlangt CC ≥ 7.0, die V100 liegt exakt auf der Untergrenze) und ECAPA
  über onnxruntime (mit `70-real` gebaut).
- **Versteckte Falle:** `nvidia-cudnn-cu12>=9.1` ist nach oben offen. **cuDNN 9.11 hat
  sm_70 gestrichen** — im Image liegt heute 9.7.1.26, der nächste Rebuild würde cuDNN
  zum zweiten Blocker machen.
- **Grenze B je Host:** `k8s-gpu-1` (570.211.01) läge im Band, `k8s-gpu-3` (595.84)
  **darüber** — dort wäre ein Downgrade nötig, das die produktiv genutzte 4060 Ti
  mitträfe.

**Bewertung je Einsatzzweck:**

| Einsatz | Urteil | Grund |
|---|---|---|
| Ersatz für die 5090 | nein | Kein bf16, kein FP8, zwei Generationen Rückstand bei gleichem VRAM |
| Zweite Karte in `k8s-gpu-1` | eher nein | Bindet den Node dauerhaft an das Band 570–580 |
| VLM/OCR zurück in den Cluster | stärkster Grund dafür | `qwen3-vl:8b` brauchte 11,9 GB und musste weichen; 32 GB lösen das |
| Für den voice-server (`k8s-gpu-3`) | nein | Treiber-Downgrade **und** eigener cu126-Torch-Build nötig |
| Eigenständiger Rechner wie `cuda.local` | vertretbar | Der Volta-Treiber bleibt isoliert, kein Blackwell-Node wird eingeschnürt |

**Entscheidung:** Kein Kauf für den Cluster. Sinnvoll nur, wenn 32 GB am Stück zwingend
gebraucht werden **und** die Karte eine eigene Maschine bekommt. Für zusätzliches VRAM im
Cluster ist eine Karte **innerhalb** der `arch_list` die ruhigere Wahl — etwa eine
gebrauchte RTX 3090 mit 24 GB (`sm_86`): kein Rebuild, kein Treiberband, kein Support-Ende
2028.

**Offen geblieben:** der Data-Center-Treiberzweig (siehe §3); ob llama.cpp
Flash-Attention auf `sm_70` unterstützt; das onnxruntime-Wheel ist aus der Build-Pipeline
belegt, nicht per `cuobjdump` geprüft; eine V100 stand zum Testen nicht zur Verfügung —
die Torch-Folge ist aus der gemessenen `arch_list` abgeleitet, nicht auf Volta
nachgestellt.
