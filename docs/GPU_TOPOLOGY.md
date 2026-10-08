# GPU-Bestand und Beschaffungsentscheidungen

**Stand:** 2026-09-18, gemessen auf den Nodes (`nvidia-smi`, `lspci`, `kubectl`), im
laufenden `voice-server`-Pod und über die Proxmox-API — nicht aus älteren Dokumenten
übernommen. Ergänzt am selben Tag, nachdem eine Tesla V100 32 GB gekauft wurde (§6).

**Nachtrag 2026-10-07:** Die Tesla V100 ist in Betrieb — im Proxmox-Host `pveold`,
nicht im Cluster — und selbst vermessen (§4.3, §4.4, §6). Nachgemessen auf allen fünf
Proxmox-Hosts (`lspci`, VM-/LXC-Konfiguration) und in den VMs (`nvidia-smi`).

**Nachtrag 2026-10-06:** In `k8s-gpu-3` steckt nicht mehr die RTX 4060 Ti, sondern die
**RTX 5060 Ti** — die Karte, die am 2026-09-01 in `k8s-gpu-1` mit Xid 79 ausgefallen und
dort ausgebaut worden war. Die 4060 Ti ist **nicht mehr im Bestand**. Gemessen per
`nvidia-smi` im voice-server-Pod und `lspci`/`qm config` auf `promox01`. §1–§3 und die
Bewertung in §6 sind angepasst; die 4060-Ti-Spalten in §4/§4.2 bleiben als Vergleichswerte
stehen.

Dieses Dokument hält drei Dinge fest: **welche GPUs es gibt und was darauf läuft**,
**welche Karten überhaupt in Frage kommen**, und **wo der Engpass heute tatsächlich
sitzt** — er sitzt nicht dort, wo man ihn vermutet.

## 1. Bestand

| Host | GPU | VRAM | Treiber | Compute Capability | k8s-Zuteilung |
|---|---|---|---|---|---|
| k8s-gpu-1 (192.168.1.180) | **1×** RTX 5070 Ti | 16303 MiB | 570.211.01 | 12.0 | **DRA-Claim** `ollama-gpu-5070ti` |
| k8s-gpu-2 (192.168.1.148) | **keine** | – | – | – | 0 — CPU-Node |
| k8s-gpu-3 (192.168.1.254) | RTX 5060 Ti | 16311 MiB | **595.91.07** | 12.0 | `nvidia.com/gpu: 1` (voice-server, ns `voice`) |
| cuda.local (192.168.1.15) | RTX 5090 | 32607 MiB | 575.64.03 | 12.0 | kein k8s-Node |
| gpu-ct (192.168.1.94, DHCP) | **Tesla V100 PCIe 32 GB** | 32768 MiB | **580.178.04** | 7.0 | kein k8s-Node |

**Virtualisierung (Proxmox, über die API gelesen):**

| k8s-Node | VM | Proxmox-Host | Durchgereicht |
|---|---|---|---|
| k8s-gpu-1 | 201 | **pve4** (Ryzen 9 3950X, 135 GB RAM) | `0000:0b:00` = GB203, RTX 5070 Ti |
| k8s-gpu-2 | 202 | pve4 | **kein `hostpci`** — bestätigt ohne GPU |
| k8s-gpu-3 | 109 | **promox01** (i5-12600H, **Mobil-CPU**, 101 GB RAM) | `0000:01:00` = GB206 (`10de:2d04`), RTX 5060 Ti |
| cuda.local | 110 | **promox** (i5-14600K, 188 GiB RAM) | `0000:01:00` = GB202, RTX 5090 (Mapping `gpu-rtx5090`) |
| gpu-ct | LXC 100 | **pveold** (i7-4770K, 31 GiB RAM) | **kein Passthrough** — der Host lädt den Treiber, der Container bekommt die Gerätedateien `/dev/nvidia*` (§6) |

Der fünfte Host `storage` trägt nur eine GT 730 für die Konsole.

`promox01` läuft auf einer Mobil-CPU. Eine 250-W-Karte in Dual-Slot-Bauweise mit passiver
Kühlung ist dort praktisch ausgeschlossen — unabhängig von jeder Softwarefrage.
**Freier Steckplatz und Netzteilreserve sind über die API nicht ermittelbar** und brauchen
eine Sichtprüfung am Gerät.

**Es gibt derzeit keinen freien GPU-Platz im Cluster.** Die zweite Karte in `k8s-gpu-1`
(RTX 5060 Ti) ist dort ausgefallen — **Xid 79** — und wurde ausgebaut. Seither läuft die
Zuteilung auf `k8s-gpu-1` über einen **DRA-Claim** statt über das Device-Plugin, weil das
Plugin nach dem Ausfall nichts mehr zuteilen konnte. Die 5060 Ti selbst arbeitet seither in
`k8s-gpu-3` (siehe Nachtrag oben).

Das PCI-Mapping auf `promox01` heißt noch **`gpu-rtx4060ti`**, zeigt aber auf
`10de:2d04` = GB206/5060 Ti. Der Name ist nur ein Etikett; wer nach ihm sucht, findet die
falsche Karte.

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
- **k8s-gpu-3 (5060 Ti)** — voice-server: faster-whisper `medium` mit `int8_float16`,
  Sprechererkennung (ECAPA) über onnxruntime, Piper-TTS; pyannote-Diarisierung nur bei
  `MEETING_ENABLED`.
- **pveold / gpu-ct (V100)** — **Testbetrieb**, keine Instanz zeigt dorthin. Eingerichtet
  am 2026-10-07 sind drei Dienste, von denen jeweils nur einer auf die Karte passt:
  ein `llama-server` mit demselben Image, Modell und denselben Argumenten wie auf
  `cuda.local` (Docker-Container `llama-server`, Port 8081), Strata mit
  `Qwen3.8-Flash-Next` Q2_0 (Docker-Container `strata`, Port 8080, §4.4) und Ollama 0.30.8
  (systemd, Port 11434). **Der Host wurde am Abend des 2026-10-07 heruntergefahren.** Nach
  dem Einschalten startet `gpu-ct` von selbst, die beiden Docker-Container aber nicht
  (`llama-server` wurde gestoppt, `strata` hat keine Restart-Policy): `docker start strata`
  oder `docker start llama-server` im Container. Stratas Thinking-Abschaltung ist nur zur
  Laufzeit gesetzt (`POST /settings`) und muss nach einem Start erneut gesetzt werden.
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

Warum es heute passt: 5060 Ti, 5070 Ti und 5090 treffen `sm_120` direkt. (Die frühere
4060 Ti, `sm_89`, wurde vom `sm_86`-Cubin bedient.)

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
wörtlich — es folgt nur aus „580 ist der letzte Zweig". `k8s-gpu-3` läuft auf 595.91.07 und
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

| | **V100 PCIe 32 GB** | **RTX 5070 Ti** | **RTX 4060 Ti 16G** (nicht mehr im Bestand) | **RTX 5090** |
|---|---|---|---|---|
| VRAM / Typ | 32 GB **HBM2** | 16 GB GDDR7 | 16 GB GDDR6 | 32 GB GDDR7 |
| Bandbreite | **900 GB/s** | 896 GB/s | 288 GB/s | **1792 GB/s** |
| fp16 Tensor, dense | **112 TFLOPS** | 87,9 | 44,1 | **209,5** |
| INT8 | **~56 TOPS, nur DP4A** | 351,5 TOPS Tensor | 176,5 TOPS Tensor | 838 TOPS Tensor |
| bf16 / tf32 / fp8 | **nein / nein / nein** | ja / ja / ja | ja / ja / ja | ja / ja / ja |
| PCIe | Gen3 x16 (15,75 GB/s) | Gen5 x16 (63,0) | Gen4 x8 (15,75) | Gen5 x16 (63,0) |
| TDP / Jahr | 250 W / 2018 | 300 W / 2025 | 165 W / 2023 | 575 W / 2025 |

Für die **RTX 5060 Ti** führt `docs/private/LLM_MODEL_GUIDE.md` 448 GB/s Bandbreite;
weitere Papierwerte sind hier nicht nachgetragen.

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

### 4.2 Gemessene Werte — und wo sie den Papierwerten widersprechen

Recherchiert am 2026-09-18. Alle Zahlen in diesem Abschnitt sind **Fremdmessungen aus dem
Netz**, nichts davon auf unserer Hardware gemessen. Die eigene Messung der V100 steht in §4.3.

**`llama-bench`, gleiches Modell `llama 7B Q4_0`**, alle aus derselben Sammelstelle
(llama.cpp-Diskussion #15013, **Community-Einreichungen**, keine offiziellen Zahlen):

| GPU | pp512 (Prompt) | tg128 (Ausgabe) |
|---|---:|---:|
| **V100-SXM2 32 GB** | **3043** | **129–135** |
| RTX 4060 Ti 8 GB | 3395 | 64 |
| RTX 3090 | 5175 | 158 |
| RTX 4090 | 14771 | 189 |
| RTX 5090 | 14142–16195 | 277–287 |

- **Token-Ausgabe: Papierwert bestätigt.** Die V100 liegt beim 2,0-fachen der 4060 Ti und
  bei 47 % der 5090 — exakt das Verhältnis der Speicherbandbreiten (§4).
- **Prompt-Verarbeitung: Papierwert WIDERLEGT.** Nach dichter fp16-Leistung müsste die
  V100 klar vor der 4060 Ti liegen (112 gegen 44 TFLOPS). Gemessen liegt sie **darunter**
  (3043 gegen 3395) und bei nur **rund einem Fünftel** der 5090. Wahrscheinliche Ursache
  ist der nicht ausoptimierte Volta-Pfad in llama.cpp; belegt ist aber nur die Messung,
  nicht die Ursache. **Für diese Phase sind die Papierwerte in §4 unbrauchbar.**

**Unser eigenes Modell, Blogbeitrag mit Eigenmessung** (V100-SXM2 32 GB, selbst gebautes
llama.cpp für `sm_70`, CUDA 12.9, **Treiber 580.159.03** — bestätigt das Treiberband aus
§3 in der Praxis):

| Modell | Ausgabe | Prompt-Verarbeitung |
|---|---:|---:|
| `qwen3.6` MoE 35B-A3B, 4 Bit, 16k Kontext | **98,8 Token/s** | **352 Token/s** |
| dichtes 27B Q4_K_M | 32,9 Token/s | — |

Das dichte 27B ist von einer zweiten, unabhängigen Quelle mit 32,17 Token/s bestätigt.
Kleinmodelle auf 2× V100 (Community-Repo, Ollama Q4_K_M): `llama3.2:3b` 157 Token/s,
`qwen3:1.7b` 166, `gemma3:4b` 119 — also die Modellklasse, die heute auf `k8s-gpu-1` läuft.

**Was daraus für den Betrieb folgt.** Bei 352 Token/s Prompt-Verarbeitung dauert ein
Prompt von 4000 Token rund **11 Sekunden**, einer von 16 000 Token rund **47 Sekunden**.
`cuda.local` ist auf **262 144** Token Kontext konfiguriert. Eine V100-Instanz braucht
daher ein **deutlich kleineres Kontextfenster** und Prompt-Zwischenspeicherung; lange
Kontexte bleiben auf der 5090. Die Ausgabegeschwindigkeit ist dagegen unkritisch.

**Beim Lesen zu beachten:** Alle V100-Werte stammen von der **SXM2-Variante**, die höher
taktet als unsere PCIe-Karte (ein Blogautor beziffert den Abstand mit ~10 %). Die Builds
unterscheiden sich zwischen den Einreichungen. Die MoE-Messung ist eine Einzelquelle mit
selbst gebautem Programm.

**Nicht gefunden, trotz gezielter Suche:** Durchsatz heutiger Einbettungsmodelle auf V100
(es gibt nur SBERT-Zahlen von 2019, nicht übertragbar) und ein Vergleich
`float16` gegen `int8_float16` für Whisper auf derselben V100. Beides müsste selbst
gemessen werden.

**Zwei Kernel-Sorten, die leicht verwechselt werden:** llama.cpp hat **eigene**
Flash-Attention-Kernel, die Volta bedienen (`volta_mma_available` in `fattn.cu`) — daher
sind die „FA an"-Zeilen oben gültig und sogar schneller. Die separate Bibliothek
**FlashAttention-2** von Dao-AILab verlangt Ampere und ist das, woran vLLM und SGLang
hängen. Kein Widerspruch, zwei verschiedene Dinge.

### 4.3 Eigene Messung der V100 PCIe (2026-10-07)

Gemessen auf `pveold` im Container `gpu-ct`: `llama-server` aus demselben Image wie auf
`cuda.local` (per Digest gepinnt, Build b10423, CUDA 12.8), dasselbe Modell
`Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` samt mmproj vom NFS, dieselben Argumente
(`--ctx-size 262144`, KV `q8_0`, `--flash-attn on`, 4 Slots), Treiber 580.178.04. Der
Lastlauf stellt den aus `docs/private/LLM_INFRASTRUCTURE_ANALYSIS.md` §1.4 nach
(Einzelstrom, `temperature=0`); die 5090-Spalte ist von dort übernommen (2026-09-03).
Der lange Prompt hatte hier 14.765 Token statt 15.640.

| Messung | RTX 5090 | **V100 PCIe** |
|---|---:|---:|
| Ausgabe (Decode) | 192–234 Token/s | **80–85 Token/s** |
| Prompt-Verarbeitung, langer Prompt kalt | 8.272 Token/s | **645 Token/s** |
| Langer Prompt kalt, Gesamtzeit | 2,06 s | **23,15 s** |
| Langer Prompt, Prefix-Cache-Treffer | 0,14 s | 0,22 s |

| Parallele Streams (je 128 Token) | 5090 gesamt | V100 gesamt | 5090 je Stream | V100 je Stream |
|---|---:|---:|---:|---:|
| 1 | 174,6 | 69,6 | 233,2 | 80,0 |
| 2 | 267,4 | 102,1 | 166,2 | 61,6 |
| 4 | 332,6 | 137,3 | 114,2 | 43,0 |
| 8 | 312,6 | 137,7 | 118,9 | 45,7 |

- **Ausgabe:** Die 5090 ist 2,4- bis 2,9-mal schneller — mehr als das Bandbreitenverhältnis
  von 2,0 (§4). Die Fremdmessung aus §4.2 (98,8 Token/s, SXM2, 16k Kontext) liegt darüber.
- **Prompt-Verarbeitung:** Die 5090 ist rund **13-mal** schneller; nach dichter fp16-Leistung
  wären es knapp 1,9. Der Befund aus §4.2 gilt also auch auf unserer Karte, der Absolutwert
  ist mit 645 Token/s aber fast doppelt so hoch wie die 352 Token/s der Fremdmessung.
- **Das volle 262k-Fenster passt** in die 32 GB (26,1 GB belegt). Der Engpass ist nicht der
  Speicher, sondern die Zeit für kalte lange Prompts; der Prefix-Cache gleicht das aus.
- **Kaltstart:** Das Laden des Modells über NFS dauert rund 7,5 Minuten (449 s, zweimal
  gemessen), und die erste Anfrage nach jedem Laden brauchte reproduzierbar 66 s bis zum
  ersten Token. Die Ursache der 66 s ist nicht geklärt.
- Im selben Container gemessen: `torch 2.14.1+cu126` führt `sm_70` in der `arch_list` und
  rechnet auf der Karte; Ollama 0.30.8 lädt `qwen3.6:35b` vollständig auf die GPU
  (77,5 Token/s bei 32k Kontext) und nutzt dafür den `cuda_v12`-Zweig — der `cuda_v13`-Zweig
  überspringt die Karte (`compute capability not in compiled architectures`).

**V100 gegen RTX 5070 Ti, gleiches Modell (2026-10-07).** `qwen3:8b` (derselbe Blob,
ID `500a1f067a9f`), beide Seiten Ollama **0.35.1**, Kontext 4096, `think=false`,
`temperature=0`, Einzelstrom, Median aus drei Läufen, Zeiten vom Server gemeldet. Die
5070 Ti ist das Cluster-Ollama auf `k8s-gpu-1` (Modell war bereits geladen, die Karte
teilt sich mit den Einbettungsmodellen); auf der V100 lief Ollama allein auf der Karte.

| Messung | RTX 5070 Ti | **V100 PCIe** | Faktor |
|---|---:|---:|---:|
| Ausgabe, kurze Antwort | 139,9 Token/s | 104,6 Token/s | 1,34 |
| Ausgabe, 256 Token | 130,1 Token/s | 101,2 Token/s | 1,29 |
| Prompt-Verarbeitung (3,26k Token, kalt) | 6.763 Token/s | 2.586 Token/s | 2,6 |
| Langer Prompt kalt, Gesamtzeit | 0,55 s | 1,34 s | 2,4 |
| Langer Prompt, Cache-Treffer | 0,08 s | 0,09 s | — |

- **Ausgabe: Papierwert widerlegt.** Nach Bandbreite wären beide gleichauf (900 gegen
  896 GB/s, §4); gemessen ist die 5070 Ti rund 1,3-mal schneller.
- **Prompt-Verarbeitung: Papierwert widerlegt.** Nach dichter fp16-Leistung läge die V100
  27 % vorn; gemessen ist die 5070 Ti 2,6-mal schneller.
- `qwen3:14b` auf der V100 (gleiche Einstellungen): **56,5 Token/s** Ausgabe bei 256 Token
  (Läufe 52,5–61,5), **1.411 Token/s** Prompt-Verarbeitung. Auf der 5070 Ti ist dieses
  Modell nicht nachgemessen — es zu laden hätte dort die Einbettungsmodelle verdrängt.

**Weiterhin nicht vergleichbar gemessen** ist die 5060 Ti: Für sie gibt es nur die
Circa-Angabe aus `docs/private/LLM_MODEL_GUIDE.md` mit `qwen3:14b` über Ollama
(~30–40 Token/s; für die 5070 Ti stehen dort ~40–60), kein Lastlauf.

### 4.4 Strata auf der V100 (2026-10-07)

[Strata](https://github.com/Niko1221/Strata) (MIT, auf llama.cpp/ggml aufgebaut) betreibt
**ausschließlich** `Qwen3.8-Flash-Next` (125 Mrd. Parameter, MoE, rund 6 Mrd. aktiv) und
bringt für Volta eigene Kernel mit — unter anderem eine Prompt-Attention auf den
fp16-Tensor-Kernen (`mma.m8n8k4`). Unser `qwen3.6-35b-a3b` lädt es nicht; der Vergleich
unten stellt also **zwei verschiedene Modelle** auf derselben Karte gegenüber.

Aufbau: v0.1.40.3 in `gpu-ct`, Engine von Strata selbst mit CUDA 12.8 für `sm_70`
kompiliert (experimenteller Pfad, eine fertige Engine gibt es für Volta unter Linux nicht),
Größe **Q2_0** im Low-RAM-Modus (die Karte hält rund 79 % der Experten, 9,1 GiB liegen
gesperrt im RAM), zunächst Kontext 32.768, KV `int8`, Thinking an. Der Container brauchte dafür
28 GB RAM und rund 102 GB Platte. Gleicher Lastlauf wie in §4.3, drei Läufe, Zeiten vom
Server gemeldet.

| Messung | llama.cpp, `qwen3.6-35b-a3b` Q4 | **Strata, `Qwen3.8-Flash-Next` Q2_0** |
|---|---:|---:|
| Prompt-Verarbeitung, langer Prompt kalt | 645 Token/s (14.765 Token) | **1.558–1.569 Token/s** (14.270 Token) |
| Langer Prompt kalt, Gesamtzeit | 23,15 s | **9,3–9,4 s** |
| Langer Prompt, Cache-Treffer | 0,22 s | 0,28–0,42 s |
| Ausgabe, kurze Antworten | 80–85 Token/s | 72–93 Token/s |
| Ausgabe, 256 Token | rund 80 Token/s | 70–77 Token/s |
| Start bis bereit | rund 7,5 min (Modell über NFS) | 92 s (Modell auf lokaler SSD) |
| Kontextfenster | 262.144 | 32.768 |

- **Die Prompt-Schwäche der V100 ist großteils Software.** Mit Volta-eigenen Kerneln liest
  dieselbe Karte ein doppelt so großes aktives Modell rund 2,4-mal so schnell ein.
- Die Ausgabe ist etwa gleich schnell; Strata nutzt dafür spekulatives Dekodieren (MTP).
- `bench.py` vom NFS: drei von drei Tool-Aufrufen gültig, deutsche Aufgaben sinnvoll
  beantwortet. Die **Antwortqualität** des 2-Bit-Modells gegen das heutige Modell ist
  **nicht bewertet**.
- Die Karte ist damit voll (32,0 von 32,8 GB); `llama-server` und Strata passen nicht
  gleichzeitig darauf. Strata arbeitet standardmäßig eine Anfrage nach der anderen ab
  (`--parallel` ist ein Schalter, nicht gemessen).
- `--ulimit memlock=-1` aus Stratas Docker-Anleitung ist im unprivilegierten LXC nicht
  erlaubt und wurde weggelassen; die Seitensperre meldete das Log dennoch als aktiv.

**Mit 262.144 Token Kontext** (wie auf `cuda.local`; Setup neu mit `--context 262144`,
Thinking serverseitig aus über `POST /settings`). Der KV-Cache bleibt im VRAM, dafür hält
die Karte weniger Experten: 17.450 statt 20.050 Cache-Plätze, 12,4 statt 9,1 GiB im RAM.

| Messung | Kontext 32k | **Kontext 262k** |
|---|---:|---:|
| Prompt-Verarbeitung, 14,3k Token kalt | 1.558–1.569 Token/s | 1.559 Token/s |
| Ausgabe, 256 Token | 70–77 Token/s | 59 Token/s (ein Lauf) |
| **Prompt mit 101.548 Token, kalt** | — | **1.391 Token/s, 78 s gesamt** |
| Derselbe Prompt, Cache-Treffer | — | 2,5 s |

Die Frage zum 101k-Prompt (zwei Werte aus Abschnitt 1234 von 1900) wurde **halb richtig**
beantwortet: Warnungen richtig, Tag falsch (27 statt 3). Ein Einzelversuch, kein
Qualitätsurteil.

**Reva-Fragen** (`reva/bench`, 19 Unterhaltungen × 3 Züge mit echten Tool-Ergebnissen,
Prompts 249–2.813 Token, ein Strom) gegen die Basislinie `5090-qwen36-quality`:

| | RTX 5090, `qwen3.6` | **V100, Strata Q2_0** |
|---|---:|---:|
| Fehler | 0 von 57 | 0 von 57 |
| Erwartete Entitäten in der Antwort | 27 von 28 | 28 von 28 |
| Zeit bis zum ersten Token, p50 / p95 | 0,16 s / 0,38 s | 0,71 s / 1,79 s |
| Dauer je Zug, p50 / p95 | 1,76 s / 2,79 s | 5,28 s / 7,60 s |
| Durchsatz | 194 Token/s | 63 Token/s |
| Gesamtdauer | 103 s | 291 s |

Der Tool-Probe (`get_release`) bestand. Mit eingeschaltetem Thinking dauerte derselbe Lauf
418 s (Zug p50 7,8 s).

**Renfield-Fragen** (`tests/eval/golden_dataset.json`, 37 Anfragen) als vereinfachter
Routing-Test mit einem **eigens geschriebenen** Router-Prompt — nicht Renfields echter
Router, daher nur im Vergleich der beiden Modelle aussagekräftig:

| | RTX 5090, `qwen3.6` | **V100, Strata Q2_0** |
|---|---:|---:|
| Richtige Rolle | 24 von 32 | 25 von 32 |
| Abgelehnte Angriffsversuche | 5 von 5 | 5 von 5 |
| Latenz, Median | 0,11 s | 0,63 s |

Die Rohdaten der Reva-Läufe liegen in `reva/bench/results/v100-strata-q2-*` (dort
gitignored): `…-nothink-*` ist der Lauf der Tabelle, `…-quality-*` der mit Thinking.

Fazit: Das 262k-Fenster läuft auf der V100, und bei diesen Fragen antwortet das
2-Bit-Modell nicht schlechter als das heutige — aber rund dreimal so langsam wie die 5090.

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

**Stand 2026-10-07: Die Karte ist in Betrieb** — nicht wie unten empfohlen in einer VM auf
`pve4`, sondern im Proxmox-Host `pveold` (i7-4770K, Gigabyte Z87X-UD5H, PVE 9.2.21,
Kernel 7.0.14). Auf diesem Host ist keine IOMMU aktiv (alle Geräte ohne IOMMU-Gruppe),
VM-Passthrough scheidet dort also aus: Der Treiber **580.178.04** (proprietäre Module,
DKMS) läuft auf dem Host, der unprivilegierte LXC-Container `gpu-ct` (ID 100) bekommt die
Gerätedateien und denselben Treiber als Userspace. Darin laufen Docker mit dem
NVIDIA-Container-Toolkit (`no-cgroups = true`, LXC-Feature `keyctl`) und der `llama-server`.
Messwerte in §4.3. Von den Bedingungen unten sind damit erledigt:

- **1 (R580):** 580.178.04 baut und lädt auf Kernel 7.0.14.
- **2 (Ollama pinnen):** auf 0.30.8 festgelegt, dieselbe Version wie auf `cuda.local`.
  Ollama 0.40.0 wollte im geteilten Modellspeicher ein `manifests-v2` anlegen (Migration);
  der Speicher ist in `gpu-ct` deshalb **nur lesend** eingebunden.
- **5 (Above-4G):** Der Fehler ist selbst beobachtet — `BAR1 is 0M @ 0x0`, die Firmware bot
  nur Adressraum unter 4 GB. Gelöst nicht im BIOS, sondern mit dem Kernel-Parameter
  `pci=realloc,nocrs` in `/etc/default/grub`; ohne ihn initialisiert die Karte nicht.
- **7 (llama.cpp-Image):** per Digest auf das CUDA-12.8-Image von `cuda.local` gepinnt;
  die Karte läuft mit dem fertigen Image.
- **8 (Kontextfenster):** bewusst **nicht** verkleinert — 262k passt, kostet aber bei
  kalten langen Prompts Zeit (§4.3).

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
| Ersatz der 5060 Ti (`k8s-gpu-3`) | nein | `promox01` ist eine Mobil-CPU-Plattform (§1), dazu Treiber 595 und Verlust der Diarisierung |
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
7. **llama.cpp-Image auf einen CUDA-12-Tag pinnen.** Der CI-Workflow setzt keine
   Architekturliste, deshalb greift der Standardzweig und dieser enthält `70-virtual` —
   also **PTX statt Cubin**: Eine V100 läuft mit dem fertigen Image, der Treiber übersetzt
   die Kernel beim ersten Start. Die **`cuda13`-Tags enthalten für Volta gar nichts**
   (Versionsabfrage `< 13`), ein Wechsel dorthin legt die Karte ohne Build-Fehler still.
   Ein Eigenbau ist nicht zwingend, verschafft aber ein echtes `70-real`-Cubin statt der
   Übersetzung zur Laufzeit. *(Aus der Build-Definition abgeleitet; die publizierten
   Image-Schichten wurden nicht auf enthaltene Cubins untersucht.)*
8. **Kontextfenster klein halten** und Prompt-Zwischenspeicherung nutzen (§4.2).

**Offen, nur physisch zu klären:** freier Dual-Slot-Platz und Netzteilreserve in `pve4`,
Luftstrom für die passive Kühlung (die Tabelle im NVIDIA-Product-Brief ließ sich nicht
extrahieren), IOMMU-Gruppierung.

**Weiter offen:** das onnxruntime-Wheel ist aus der Build-Pipeline belegt, nicht per
`cuobjdump` geprüft; Einbettungsdurchsatz und Whisper `float16` gegen `int8_float16` auf
Volta sind nirgends gemessen (§4.2). **bf16 läuft auf Volta nur emuliert und liefert NaN**
— jeder Pfad, der bf16 verlangt, muss auf fp16 gestellt werden.
