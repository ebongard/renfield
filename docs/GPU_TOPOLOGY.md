# GPU-Bestand und Beschaffungsentscheidungen

**Stand:** 2026-09-18, gemessen auf den Nodes (`nvidia-smi`, `lspci`, `kubectl`), im
laufenden `voice-server`-Pod und über die Proxmox-API — nicht aus älteren Dokumenten
übernommen. Ergänzt am selben Tag, nachdem eine Tesla V100 32 GB gekauft wurde (§6).

Dieses Dokument hält drei Dinge fest: **welche GPUs es gibt und was darauf läuft**,
**welche Karten überhaupt in Frage kommen**, und **wo der Engpass heute tatsächlich
sitzt** — er sitzt nicht dort, wo man ihn vermutet.

## 1. Bestand

| Host | GPU | VRAM | Treiber | Compute Capability | k8s-Zuteilung |
|---|---|---|---|---|---|
| k8s-gpu-1 (192.168.1.180) | **1×** RTX 5070 Ti | 16303 MiB | 570.211.01 | 12.0 | **DRA-Claim** `ollama-gpu-5070ti` |
| k8s-gpu-2 (192.168.1.148) | **keine** | – | – | – | 0 — CPU-Node |
| k8s-gpu-3 (192.168.1.254) | RTX 4060 Ti | 16380 MiB | **595.84** | 8.9 | `nvidia.com/gpu: 1` (voice-server, ns `voice`) |
| cuda.local (192.168.1.227) | RTX 5090 | 32607 MiB | 575.64.03 | 12.0 | kein k8s-Node |

**Virtualisierung (Proxmox, über die API gelesen):**

| k8s-Node | VM | Proxmox-Host | Durchgereicht |
|---|---|---|---|
| k8s-gpu-1 | 201 | **pve4** (Ryzen 9 3950X, 135 GB RAM) | `0000:0b:00` = GB203, RTX 5070 Ti |
| k8s-gpu-2 | 202 | pve4 | **kein `hostpci`** — bestätigt ohne GPU |
| k8s-gpu-3 | 109 | **promox01** (i5-12600H, **Mobil-CPU**, 101 GB RAM) | `0000:01:00` = AD106, RTX 4060 Ti |

`promox01` läuft auf einer Mobil-CPU. Eine 250-W-Karte in Dual-Slot-Bauweise mit passiver
Kühlung ist dort praktisch ausgeschlossen — unabhängig von jeder Softwarefrage.
**Freier Steckplatz und Netzteilreserve sind über die API nicht ermittelbar** und brauchen
eine Sichtprüfung am Gerät.

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
  und nomic-embed-text, zusammen etwa 10,1 von 16,3 GB, geteilt mit Reva.
  `qwen3-vl:8b` lief früher hier und brauchte gemessen **11,9 GB** — es verdrängte alle
  anderen Modelle und wurde deshalb auf die 5090 verlegt.
- **k8s-gpu-3 (4060 Ti)** — voice-server: faster-whisper `medium` mit `int8_float16`,
  Sprechererkennung (ECAPA) über onnxruntime, Piper-TTS; pyannote-Diarisierung nur bei
  `MEETING_ENABLED`.
- **Ohne GPU:** Backend (`torch==2.6.0+cpu`), document-worker, pdf-split-worker und
  meeting-worker — die rufen nur nach außen. Auch die Zweitinstanz `renfield-xidra` hat
  **keinen einzigen** GPU-Request; sie nutzt dieselben geteilten Dienste (§5).

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

**Grenze B — das Treiberband.** Blackwell verlangt mindestens Treiber 570, Volta wird
längstens vom **580er-Zweig** getragen. Das gemeinsame Band ist also **570–580**, und es
ist belegt statt hergeleitet: Die `supportedchips`-Liste von **580.65.06** führt
`Tesla V100-PCIE-32GB`, `RTX 5090` und `RTX 5070 Ti` **gleichzeitig**; die
Data-Center-Release-Notes zu R580 nennen Volta und Blackwell zusammen. R580 ist dort
**Long Term Support**, EOL **Juni 2028**, maximal CUDA 13.

- [Phoronix: 580 ist der letzte Zweig für Maxwell/Pascal/Volta](https://www.phoronix.com/news/NVIDIA-580-Linux-Driver-Last-HW)
- [NVIDIA-KB: Support-Plan für Maxwell, Pascal, Volta](https://nvidia.custhelp.com/app/answers/detail/a_id/5706/)

Zwei Unschärfen: Im Deprecation-Schedule wird Volta über TITAN V und Quadro GV100 belegt,
nicht über die Tesla V100 selbst. Und dass **R595 Volta nicht mehr führt**, steht nirgends
wörtlich — es folgt nur aus „580 ist der letzte Zweig". `k8s-gpu-3` läuft auf 595.84 und
läge damit darüber.

**Wichtig: Beim Durchreichen gilt das Band pro VM, nicht pro Host.** Der Proxmox-Host
bindet eine durchgereichte Karte an `vfio-pci` und braucht selbst keinen NVIDIA-Treiber;
den lädt jede VM für sich. Eine V100 in einer **eigenen** VM (Treiber 580) und die
5070 Ti in VM 201 (Treiber 570) schränken einander also **nicht** ein. Zur Fessel wird das
Band nur, wenn beide Karten in **dieselbe** VM gereicht werden. Voraussetzung ist in
beiden Fällen, dass die IOMMU-Gruppen die Karten sauber trennen.

**Prüfreihenfolge vor jedem GPU-Kauf:** (1) Steht die Compute Capability in der
`arch_list` oben? (2) Liegt der nötige Treiber im selben Band wie der der anderen Karten
**derselben VM**? (3) Passt die Karte physisch in den Proxmox-Host (Slot, Strom,
Luftstrom)? (4) Erst danach über VRAM und Bandbreite reden.

## 4. Papierwerte der beteiligten Karten

| | **V100 PCIe 32 GB** | **RTX 5070 Ti** | **RTX 4060 Ti 16G** | **RTX 5090** |
|---|---|---|---|---|
| VRAM / Typ | 32 GB **HBM2** | 16 GB GDDR7 | 16 GB GDDR6 | 32 GB GDDR7 |
| Bandbreite | **900 GB/s** | 896 GB/s | 288 GB/s | **1792 GB/s** |
| fp16 Tensor, dense | **112 TFLOPS** | 87,9 | 44,1 | **209,5** |
| INT8 | **~56 TOPS, nur DP4A** | 351,5 TOPS Tensor | 176,5 TOPS Tensor | 838 TOPS Tensor |
| bf16 / tf32 / fp8 | **nein / nein / nein** | ja / ja / ja | ja / ja / ja | ja / ja / ja |
| PCIe | Gen3 x16 (15,75 GB/s) | Gen5 x16 (63,0) | Gen4 x8 (15,75) | Gen5 x16 (63,0) |
| TDP / Jahr | 250 W / 2018 | 300 W / 2025 | 165 W / 2023 | 575 W / 2025 |

**Konvention — sonst vergleicht man Falsches:** Alle fp16-Werte sind **dense, mit
fp32-Akkumulation, ohne Sparsity**. Mit fp16-Akkumulation verdoppeln sich die
GeForce-Zahlen; daher kursieren für die 5070 Ti „175,8" und für die 5090 „419" als
angeblich dichte Werte. Eine Quelle, die INT8 „dense" höher angibt als „sparse", ist
in sich widersprüchlich und unbrauchbar. Die V100-INT8-Zahl ist eine Drittquellen-Rechnung
— **NVIDIA spezifiziert für die V100 gar kein INT8**; die 4060-Ti-Werte stammen ebenfalls
aus Drittquellen.

**Reihung nach Papierwerten, keine Messung:**

- **Token-Ausgabe** (speichergebunden, nach Bandbreite): 5090 ≫ **V100 ≈ 5070 Ti** ≫
  4060 Ti. Die V100 liegt mit +0,4 % gleichauf mit der 5070 Ti, 3,1× über der 4060 Ti,
  bei der Hälfte der 5090 — mit doppeltem VRAM der 5070 Ti.
- **Prompt-Verarbeitung** (rechengebunden, nach dense fp16): 5090 > **V100** > 5070 Ti >
  4060 Ti. Die V100 liegt nominell 27 % über der 5070 Ti.

Zwei Dinge, die die Zahlen nicht zeigen: Der V100 fehlen bf16, tf32 und fp8 vollständig,
und llama.cpp ist auf `sm_70` zwar lauffähig (Flash-Attention über
`volta_mma_available`), aber nicht ausoptimiert — ein offener PR holt allein durch eine
passende SM70-Konfiguration 20 bis 28 % heraus. Der reale Durchsatz liegt daher unter dem
Papierverhältnis.

**INT8 auf Volta:** Voltas Tensor-Kerne der ersten Generation rechnen ausschließlich
fp16 mit fp32-Akkumulation; INT8-Tensor-Kerne kamen erst mit Turing (`IMMA` ist sm_75+).
ctranslate2 führt `int8_float16` ab CC ≥ 7.0 als unterstützt — es fällt also **nicht** auf
fp32 zurück, der INT8-Anteil läuft aber über **DP4A auf den CUDA-Kernen**. Auf einer V100
ist der fp16-Tensor-Pfad (112 TFLOPS) rechnerisch **schneller** als der INT8-Pfad
(~56 TOPS); der INT8-Vorteil bleibt dort allein der halbierte Speicherbedarf. Wer Whisper
je auf eine V100 legt, sollte `float16` gegen `int8_float16` **messen**, statt INT8
reflexhaft für schneller zu halten.

### 4.1 Welche Serving-Software auf `sm_70` überhaupt noch läuft

Geprüft am 2026-09-18 gegen die Build-Konfigurationen der Projekte, nicht gegen Foren:

| Software | Volta / `sm_70` | Beleg |
|---|---|---|
| **llama.cpp / Ollama** | **ja** | Ollamas Preset `llama_cuda_v12_linux` enthält `70`; Flash-Attention über `volta_mma_available` |
| **ctranslate2** (Whisper) | ja | `CUDA_ARCH_LIST="Common"` enthält 7.0 ab CUDA ≥ 9 |
| **onnxruntime-gpu** (ECAPA) | ja | Release-Pipeline baut mit `70-real` |
| **vLLM** | **nein**, seit v0.11.1 | `CUDA_SUPPORTED_ARCHS` fiel von `7.0;7.2;7.5;…` auf `7.5;…`; Doku: „compute capability 7.5 or higher" |
| **SGLang** | **nein**, nie | Alle `gencode`-Stufen beginnen bei `compute_80`; FlashInfer verlangt sm75+ |
| **PyTorch cu128 / cu13** | **nein** | `#removing sm_50-sm_70`; CUDA 13 hat sm_50/60/70 gestrichen |

**Beide modernen Serving-Stacks fallen also aus**, und zwar doppelt: über ihre eigenen
Arch-Listen **und** über den Torch-Unterbau, den sie nicht selbst bauen, sondern vom
offiziellen Wheel beziehen. Auch der frühere Notausgang ist zu — vLLM hat die V0-Engine
samt `xformers`-Backend entfernt, FlashAttention-2 verlangt ohnehin Ampere, und ein
geduldeter Alt-Pfad für Volta existiert nicht mehr. Die vLLM-Tabelle, die für Volta noch
GPTQ und GGUF als unterstützt führt, beschreibt den Zustand von damals: Ohne `sm_70`-Kernel
im Wheel ist sie gegenstandslos.

**Folge für die Modellwahl:** Auf der V100 ist **llama.cpp der Weg**, und dort sind
**quantisierte GGUF-Modelle richtig** — sie nutzen dieselben fp16-Tensor-Kerne (die
Gewichte werden zur Laufzeit entpackt) und sparen zugleich Bandbreite, die bei der
Token-Ausgabe der eigentliche Engpass ist. Gewichte in fp16 zu speichern brächte **keine
zusätzliche Tensor-Kern-Leistung**, sondern nur doppelten Speicherverkehr. Mehrmandanten-
Betrieb braucht dafür kein vLLM: llama.cpp bedient bereits mehrere Slots (auf `cuda.local`
laufen vier).

## 5. Der eigentliche Engpass: das geteilte LLM-Tier

Nicht das VRAM der 16-GB-Karten ist knapp, sondern der KV-Cache auf `cuda.local`:

- llama.cpp läuft dort mit **`n_slots = 4`, `n_ctx_slot = 262144`, `kv_unified = true`** —
  ein **gemeinsamer** KV-Pool, aus dem sich alle Slots bedienen, kein festes Kontingent
  je Slot.
- Frei sind rund **6,1 GiB**; Modell ≈ 21,3 GiB, mmproj ≈ 0,9 GiB, KV + Compute ≈ 4,2 GiB.
- Im Log stehen **2267 Fälle von `failed to find free space in the KV cache`**, dazu
  überschrittene Kontexte und fehlgeschlagene Slot-Restores.
- **Es gibt keine Mandantentrennung:** Haushalt und `renfield-xidra` zeigen auf dieselbe
  URL, ohne Quote, Priorität oder Slot-Reservierung. Die einzige Mandantenkennung im
  GPU-Tier ist die Client-Registry des voice-servers, und die authentifiziert nur.
- Ollama: `OLLAMA_NUM_PARALLEL=1`, `MAX_LOADED_MODELS=4`, `KEEP_ALIVE=-1`, 5,8 von
  16,3 GiB frei — dort ist ein weiterer Mandant eine Warteschlange, kein Speicherproblem.

**Folge:** Das Tier ist mit **zwei** Mandanten bereits am Anschlag. Eine dritte Instanz
(`club`/`ssv` ist beschlossen) trifft nicht auf freie Kapazität. Mehr VRAM allein hilft
nicht — die Last muss auf einen **zweiten Endpunkt** verteilt werden, und das ist eine
Frage von `LLM_OPENAI_BASE_URL`, nicht von Code.

## 6. Entscheidungsnotiz: Tesla V100 32 GB (Volta, sm_70)

**Stand 2026-09-18: Die Karte wurde gekauft.** Die ursprüngliche Empfehlung lautete
„kein Kauf für den Cluster"; nach der Messung der Papierwerte und der Klärung des
Treiberbands ist die Karte besser als zunächst angenommen — sie liegt bei der Bandbreite
gleichauf mit der 5070 Ti und bei dichter fp16-Rechenleistung darüber (§4).

**Befunde gegen die zwei Grenzen:**

- **Grenze A verletzt, aber nur für einen Pfad.** `sm_70` fehlt in der `arch_list`
  (im cu128-Build entfernt: `#removing sm_50-sm_70`). Betroffen ist allein die
  **pyannote-Diarisierung** der Meetings. Whisper (ctranslate2) und ECAPA (onnxruntime)
  liefen, ebenso **Ollama**: dessen CUDA-12-Preset enthält `70`, der CUDA-13-Preset nicht.
- **Grenze B einhaltbar**, sofern die Karte eine eigene VM bekommt (§3).
- **Versteckte Fallen:** `nvidia-cudnn-cu12>=9.1` ist nach oben offen und **cuDNN 9.11 hat
  sm_70 gestrichen**; `k8s/ollama.yaml` nutzt **`ollama/ollama:latest` ungepinnt** — fällt
  dort der CUDA-12-Zweig weg, stirbt die Karte beim nächsten Pod-Neustart ohne Vorwarnung.

**Bewertung je Einsatzzweck (überarbeitet):**

| Einsatz | Urteil | Grund |
|---|---|---|
| **Eigene VM auf `pve4` als zweiter `llama-server`** | **empfohlen** | Löst den echten Engpass (§5): zweiter Endpunkt für die dritte Instanz; 32 GB tragen dasselbe Modell mit ~9 GiB für KV statt 4,2; Treiberband bleibt auf diese VM beschränkt |
| Zusätzlich in `k8s-gpu-1` (neben der 5070 Ti) | möglich | 16 + 32 GB, VLM/OCR kämen zurück in den Cluster — aber beide Karten in einer VM heißt Treiber 580 für beide |
| Ersatz der 5070 Ti | nein | Tauscht bf16/tf32/fp8 gegen Speicher und lässt eine moderne Karte ungenutzt |
| Ersatz der 4060 Ti (`k8s-gpu-3`) | nein | `promox01` ist eine Mobil-CPU-Plattform (§1), dazu Treiber 595 und Verlust der Diarisierung |
| Ersatz der 5090 | nein | Halbe Bandbreite, kein bf16/fp8 |

**Bedingungen, falls die Karte in Betrieb geht:**

1. Treiber der betreffenden VM auf **R580** festnageln (LTS, EOL Juni 2028, max CUDA 13).
2. `ollama/ollama` auf eine **feste Version** pinnen, sobald die Karte Ollama bedient.
3. `nvidia-cudnn-cu12<9.11` deckeln, falls der Voice-Pfad je dorthin soll.
4. Strom: **CPU-8-Pin (EPS)**, nicht PCIe-8-Pin — ein Grafikkartenkabel passt mechanisch,
   ist aber falsch belegt.
5. **Above-4G-Decoding** im BIOS aktivieren, sonst mappt BAR1 auf `0M @ 0x0` und die Karte
   initialisiert beim Durchreichen nicht (nur Community-Beleg, keine NVIDIA-Primärquelle;
   Resizable BAR selbst ist nicht nötig).
6. IOMMU-Gruppen prüfen, damit V100 und 5070 Ti an getrennte VMs gehen können.

**Offen, nur physisch zu klären:** freier Dual-Slot-Platz und Netzteilreserve in `pve4`,
Luftstrom für die passive Kühlung (die Tabelle im NVIDIA-Product-Brief ließ sich nicht
extrahieren), IOMMU-Gruppierung.

**Weiter offen:** ob llama.cpp auf `sm_70` mit Flash-Attention die erwartete Leistung
bringt (lauffähig ja, ausoptimiert nein); das onnxruntime-Wheel ist aus der Build-Pipeline
belegt, nicht per `cuobjdump`; sämtliche Leistungsangaben in §4 sind Papierwerte, auf
dieser Hardware nicht gemessen.
