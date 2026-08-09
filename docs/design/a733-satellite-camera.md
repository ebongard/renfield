# A733 (Orange Pi Zero 3W) satellite camera — hardware bring-up reference

**Status: BROUGHT UP + WORKING 2026-08-09.** `/dev/video0` is live on the Esszimmer node
(`orangepizero3w` / 192.168.1.82) with an IMX219 (Camera Module 2) on CAM1. This is the
authoritative camera reference for the Allwinner **A733** satellite. It supersedes earlier,
wrong conclusions in this repo that said "CSI is a dead end, use RTSP" — the schematic (and
now a working camera) show the opposite.

## TL;DR

- The board's camera connector is a **Raspberry-Pi-standard 22-pin 0.5 mm MIPI CSI FPC**
  (footprint literally `fpc22-20-sm-RPI-TOP-h2`). A Pi camera is electrically compatible.
- The **IMX219 driver is already built** into the running kernel (`CONFIG_SENSOR_IMX219=m`,
  MIPI CSI-2). **No kernel/driver work.**
- What's needed: **(1)** a **15-pin↔22-pin Raspberry Pi Zero/Pi 5 camera cable** (~€5) —
  this is the *only* hardware missing; **(2)** one **device-tree overlay** to enable CSI0 +
  add the `imx219` sensor node (all pin values known from the schematic, below).
- **The no-boot** an operator hit was simply a **15-pin camera forced into the 22-pin
  socket** without the adapter cable — a mechanical/pin mismatch, not a board fault.

## Board (verified on-device via SSH)

| Fact | Value | Source |
|---|---|---|
| Board | `Orange Pi Zero 3W`, `BOARDFAMILY=sun60iw2`, `BRANCH=legacy` | `/etc/orangepi-release` |
| SoC | Allwinner **A733** (`sun60i-a733`), arm64, 8 cores, ~12 GB RAM | `/proc/device-tree/model`, node caps |
| Kernel | 6.6.98 `sun60iw2` (vendor BSP, no mainline) | `uname -r` |
| DTB | `sun60i-a733-orangepi-zero3w.dtb` | `/boot/dtb/allwinner/` |

## The camera connector (verified from the official schematic)

Source: **"OPI ZERO 3W V1_2 Schematic Diagram.pdf"** (Shenzhen Xunlong), sheet **13 —
`13_CAM_MIPI CSI`**. There are **two** connectors: `CAM1` (CSI0) and `CAM2` (CSI1).

- Connector part: **`KH-FPC0.5-H2.0SMT-22P-QCHF`** — 22-pin, 0.5 mm pitch.
- Footprint: **`fpc22-20-sm-RPI-TOP-h2`** → the **Raspberry Pi 22-pin CSI standard**
  (same connector as Pi 5 / Pi Zero / CM4), contacts on top.

### `CAM1` = MIPI **CSI0** (MCSIA) — the primary camera port

| Pins | Signal | Notes |
|---|---|---|
| 2–3 | MCSIA-D0N / D0P | data lane 0 |
| 5–6 | MCSIA-D1N / D1P | data lane 1 (IMX219 = 2 lanes → uses 0+1) |
| 8–9 | MCSIA-CKN / CKP | MIPI clock |
| 11–12 | MCSIA-D2N / D2P | lane 2 (4-lane capable) |
| 14–15 | MCSIA-D3N / D3P | lane 3 |
| 17 | **PE6** | control GPIO (reset / pwdn) |
| 18 | **PE5** | control GPIO (reset / pwdn) |
| 20 | **TWI11_SCK** | sensor i2c clock → **i2c = TWI11** (`twi@251B000`) |
| 21 | **TWI11_SDA** | sensor i2c data |
| 22 | VCC_3V3_CSI | 3V3 power |
| 1,4,7,10,13,16,19 | GND | diff-pair returns |
| 23, 24 | GND | shield tabs |

### `CAM2` = MIPI **CSI1** (MCSIB) — second port

Same 22-pin RPI connector; **i2c = TWI9**, control GPIOs **PE9 / PE10**, MCSIB 4-lane.
Use CAM1 unless a second camera is needed.

**Note on clock:** the RPI 22-pin standard does not route a discrete sensor MCLK — Pi
camera modules carry their own oscillator. Confirm mclk handling during bring-up (the
sunxi-vin `imx219` node may still expect a `csi_mclk` pinctrl; the board defines
`csi_mclk0/1/2` groups).

## Driver status (verified in the running kernel)

```
CONFIG_AW_VIDEO_SUNXI_VIN=m     # Allwinner VIN (video-input) framework
CONFIG_VIN_IOMMU=y
CONFIG_SENSOR_IMX219=m          # ← MIPI CSI-2, 2-lane — matches the connector. USE THIS.
CONFIG_SENSOR_OV13850=m         # MIPI, also available (13 MP)
# CONFIG_SENSOR_OV5640 is not set # DVP/parallel driver — WRONG interface, do not use
```

- **IMX219** is the correct sensor: its in-tree `sunxi-vin` driver is `V4L2_MBUS_CSI2_DPHY`
  (MIPI) and already compiled. Zero driver work.
- **OV5640** is a trap: its in-tree driver is `V4L2_MBUS_PARALLEL` (DVP), and the only
  buyable OV5640 module (Orange Pi OP1300) is a parallel camera for old H3 boards — wrong
  interface for this MIPI connector. Do not use OV5640.

## Device-tree status

- `vind@5800800` (the CSI/ISP controller) is present in the Zero 3W DTB but
  `status = "disabled"`; no active sensor node; the BSP ships **no camera overlay**
  (confirmed: `/proc/device-tree` has no csi/vin nodes, `/dev/video*` absent).
- The **Orange Pi 4 Pro** DTS (same A733 SoC) carries a complete commented-out camera
  scaffold (`vind0`/`csi`/`sensor`/`vinc`) — adapt that as the overlay template.
- So the entire remaining task is **one DT overlay** using the pins verified above.

## Pin compatibility with Raspberry Pi cameras (verified)

The `fpc22-20-sm-RPI-TOP-h2` footprint is not just mechanically Pi-shaped — the **signal
assignment matches the Raspberry Pi 22-pin standard** on the pins that matter, so a genuine
Pi camera + the right cable maps correctly:

| | SCL | SDA | 3V3 |
|---|---|---|---|
| Raspberry Pi 22-pin standard | pin 20 | pin 21 | pin 22 |
| Orange Pi Zero 3W CAM1 (schematic) | pin 20 (TWI11_SCK) | pin 21 (TWI11_SDA) | pin 22 (VCC_3V3_CSI) |

So the connector is genuinely Pi-compatible; an **IMX219** Pi camera is the right part.

## The cable

- **The one part to order:** a **Raspberry Pi "Standard - Mini" camera cable, 15-pin
  (camera) ↔ 22-pin (host), 0.5 mm** (aka the Pi Zero / Pi 5 camera cable). ~€5,
  berrybase.de / Amazon.de / Adafruit #5818. A native-22-pin camera needs a 22↔22 cable.

## Troubleshooting: the board won't boot with the camera connected

Two distinct causes, both **mechanical, not a board fault** — the board boots fine the
moment the camera is unplugged. **Always power off before (dis)connecting the ribbon; a
mis-seated CSI ribbon shorts 3V3→GND and can damage the board or camera if left powered.**

1. **Wrong connector width** (the first incident): a **15-pin** Pi camera pushed onto the
   **22-pin** socket without the 15→22 Standard-Mini cable → pins don't align → short → no
   boot. Fix: use the Standard-Mini cable above.

2. **Flipped FPC ribbon** (the second incident, with a correct Module 2 + cable): an FPC
   ribbon has **contacts on one side only**. Inserted the wrong way round at **one** end,
   the connector is **mirrored** — pin 22 (**3V3**) lands on pin 1 (**GND**) → **dead short
   → no boot.** This is the classic FPC mistake and matches "boots without the camera, dies
   with it" exactly. Fix, with **power off**:
   - Unplug the camera; confirm the board boots bare (it will).
   - Re-seat the ribbon so the **exposed metal contacts** face each connector's contact side
     at **both** ends (the **blue stiffener** is the orientation reference; flipping one end
     is what shorts it). Fully insert; close the latch/flap.
   - Power on. If it boots but `/dev/video0` is absent, that's expected — the sensor still
     needs the **DT overlay** (above); the board *booting* means the cable is now correct.
   - Still no boot after fixing orientation with a known-good Standard-Mini cable → suspect a
     damaged cable, or board damage from an earlier forced insertion (test the board bare,
     then swap the cable).

**Symptom on the cluster side:** the node goes `NotReady` + no ping (it's a hardware-pinned
k8s node; a dead board can't self-heal). Unplug the camera and power-cycle to recover.

## Camera options (all IMX219 → driver already built)

| Camera | FoV / features | Use | Cable |
|---|---|---|---|
| **Pi Camera Module v2** (existing, arbeitszimmer) | ~62°, no IR | cheap first bring-up test | 15→22 |
| **Waveshare IMX219-160IR** | 160°, IR night vision | the real occupancy/gesture camera | 15→22 |

(Occupancy wants the wide/IR 160°; the plain Pi Cam v2 is fine to prove the pipeline. The
board has **two** CSI ports if both a wide and a narrower camera are ever wanted.)

### Cameras that will NOT work (driver mismatch, even though they plug in)

| Camera | Sensor | Why not |
|---|---|---|
| **Raspberry Pi Camera Module 3** | **IMX708** | No IMX708 driver in the A733 `sunxi-vin` set (only IMX219/OV13850 are built). Plugs in via a Standard-Mini cable, but never enumerates. |
| Orange Pi OP1300 / generic OV5640 modules | OV5640 | In-tree OV5640 driver is DVP/parallel; this connector is MIPI. Wrong interface. |
| Pi HQ Camera | IMX477 | No IMX477 driver in the A733 set. |

**Cable vs camera:** the connector is the Pi-standard 22-pin "Mini". The **"Raspberry Pi
Camera Cable Standard - Mini"** (15-pin↔22-pin) is the *correct* cable — pair it with an
**IMX219** camera. A cable being right does not make an unsupported *sensor* work: Camera
Module 3 (IMX708) fails on the sensor driver, not the cable.

## Bring-up steps (as actually performed — DONE ✅)

1. ✅ IMX219 (Camera Module 2) on **CAM1** via a Standard-Mini (15↔22) cable, contacts
   oriented correctly (a flipped ribbon shorts the board — see Troubleshooting).
2. ✅ Compile + install the overlay: `.dtbo` → `/boot/dtb/allwinner/overlay/sun60i-a733-cam-imx219.dtbo`;
   add `overlays=cam-imx219` to `/boot/orangepiEnv.txt` (the board uses
   `overlay_prefix=sun60i-a733`, so the entry is the suffix `cam-imx219`); reboot. The
   overlay applies (`/proc/device-tree/.../vind@5800800/sensor@5812000/sensor0_mname` = `imx219`).
3. ✅ **Load the sunxi-vin modules — REQUIRED, easy to miss.** `CONFIG_AW_VIDEO_SUNXI_VIN`
   and the sensor are **`=m` and do NOT autoload**, so after the overlay you still get no
   `/dev/video0` until: `modprobe vin_io imx219 vin_v4l2`. Made persistent with
   `/etc/modules-load.d/renfield-camera.conf` containing, in order:
   ```
   vin_io
   imx219
   vin_v4l2
   ```
   Result: `dmesg` → `[imx219]find the sony IMX219`, and `/dev/video0` + `/dev/media0`
   appear (`v4l2-ctl -d /dev/video0 --list-formats-ext` enumerates RGB565/RGB888/NV12/YUV422/
   GREY — the ISP output). **All overlay values were correct first try** (csi_sel/mipi_sel=0,
   mclk_id=0, PE6/PE5 reset/pwdn) — no tunable iteration was needed. (A harmless
   `imx219_2 ... No such device` logs from a second base-DTB sensor slot with no camera.)
4. ✅ Mount `/dev/video0` + `/dev/media0` into the Esszimmer pod
   (`k8s/satellite-esszimmer.yaml`, alongside `/dev/snd`; `type: CharDevice`). Verified
   visible in-container.
5. ⏳ **Frame capture — needs the Allwinner GStreamer plugin (see below).** The detector
   + WS contract are camera-agnostic; only the frame source needs this dependency.

## Frame capture — WORKING via the Allwinner ISP userspace (verified 2026-08-09)

**The sensor is detected and `/dev/video0` exists, but you cannot capture with plain V4L2 /
OpenCV** — the device is a multiplanar media-controller whose ISP/scaler pipeline
(sensor → mipi.0 → csi.0 → tdm_rx.0 → isp.0 → scaler.0 → vin_cap.0) produces nothing unless
**Allwinner's ISP userspace is running**. `cv2.VideoCapture` / `v4l2-ctl --stream-mmap` →
`VIDIOC_REQBUFS Invalid argument`, `vin_pipeline_try_format failed`,
`scaler get_selection error`.

**Red herring:** the board's `/usr/local/bin/test_camera.sh` uses `gst-launch-1.0 v4l2src …
en-awisp=1`, but `en-awisp` exists in **no binary** (grep of the whole desktop rootfs found it
only in that script). It's not a real property. Don't chase GStreamer.

**What actually works: the AW ISP userspace** — `libAWIspApi.so` + `libisp.so` + `libisp_ini.so`
(pkg `libawispapi-isp-602`, extracted from the OPi Zero 3W **desktop** image; tuning is baked
into `libisp_ini.so`, no `/vendor/camera/isp_tuning` needed). Start the ISP
(`CreateAWIspApi → ispApiInit → ispGetIspId(0) → ispStart`), then do the sunxi V4L2 mplane grab
(`S_INPUT`, `VIDIOC_SET_SENSOR_ISP_CFG {0,0}`, `S_PARM`, `S_FMT` `V4L2_PIX_FMT_YUV420M` @ 640×480,
`REQBUFS`/`QUERYBUF`+mmap/`QBUF`, `STREAMON`, `DQBUF`). The ISP does debayer + 3A.

Shipped tool: **`prototypes/npu-occupancy/isp_capture/renfield_isp_capture.c`** — grabs ONE frame
(skips ~8 frames for 3A warmup) and writes raw **I420**. Built + run on the board:
```
gcc -O2 -o renfield_isp_capture renfield_isp_capture.c -I/opt/awisp -L/opt/awisp/lib -lAWIspApi -Wl,-rpath,/opt/awisp/lib
LD_LIBRARY_PATH=/opt/awisp/lib ./renfield_isp_capture /dev/video0 640 480 /tmp/frame.i420 8
```
→ `/tmp/frame.i420` = **460800 bytes (640×480 I420)**, a real image (Y mean≈113, stddev≈22).
`V4L2Camera` (satellite_occupancy.py) calls this tool → I420 → BGR (numpy, no cv2); degrades to
"capture disabled" if the tool/libs are absent.

**Deploy dependency (remaining):** bake the tool + the AW ISP libs into the satellite image:
```
/opt/awisp/renfield_isp_capture                                   # gcc-built from the .c
/opt/awisp/lib/{libAWIspApi.so, libisp.so, libisp_ini.so}        # from the OPi desktop image
```
The `.c` is in-repo; the three AW `.so`s are Allwinner proprietary binaries (extracted from the
desktop rootfs) — stage them into the satellite image build context, do NOT commit to git.

## Thermal note (observed during bring-up)

The board runs **warm: ~72 °C** on the CPU cores at idle-ish load (1.3), driven by the
baseline satellite ML workload (wakeword + BLE + audio) + k8s overhead — **not** the camera
(idle vin draws ~nothing). It **has a PWM fan, already at max** (`pwm-fan cur=4/max=4`), and
is not throttling (cores at 1794 MHz, no dmesg thermal warnings), skin only 38 °C. There is
little cooling headroom left, so **continuous** vision inference would push it toward the
throttle point — another reason the non-verbal design uses *gated, bounded-window* capture
rather than a persistent stream.

### DT overlay

**Written + compile-verified:**
[`prototypes/npu-occupancy/dts/sun60i-a733-orangepi-zero3w-cam-imx219.dts`](../../prototypes/npu-occupancy/dts/sun60i-a733-orangepi-zero3w-cam-imx219.dts).
It adds the missing `sensor0` (IMX219) + `vinc00` (capture) nodes under the already-enabled
`&vind0`, using the exact sunxi-vin binding from the Orange Pi 4 Pro DTS (same A733 SoC) and
our confirmed values (`sensor0_mname="imx219"`, `twi_cci_id=11`, `twi_addr=0x20`,
reset/pwdn on PE6/PE5, CSI0). It **compiles** on the board (`cpp | dtc -@` → 1442-byte
`.dtbo`; only cosmetic `unit_address_vs_reg` warnings, same as the 4 Pro DTS).

Build/install (from the overlay's header): `armbian-add-overlay <file>.dts`, or manual
`cpp | dtc -@` → copy `.dtbo` to `/boot/dtb/allwinner/overlay/` → add
`overlays=sun60i-a733-orangepi-zero3w-cam-imx219` to `/boot/orangepiEnv.txt` → reboot →
expect `/dev/video0`.

**On-target tunables** (validate with `dmesg | grep -iE "vin|csi|imx219"`; the sensor
already ACKs on i2c, so if it doesn't enumerate it's one of these): the MCSIA→PHY mux
(`vinc0_csi_sel`/`vinc0_mipi_sel` = 0, try 1/2), `sensor0_mclk_id` (Pi modules self-clock,
likely inert), and the PE6/PE5 reset-vs-pwdn assignment + polarity.

## Verification / provenance

- Board identity, kernel config, DTB, i2c buses, absent `/dev/video`: read live on the
  board over SSH (`root@192.168.1.82`), 2026-08-09.
- **IMX219 sensor live-confirmed:** with a Camera Module 2 + correctly-oriented Standard-Mini
  cable, `i2cdetect -y 11` shows the sensor answering at **0x10** on **TWI11** — the exact
  bus/address the schematic predicted and the overlay hard-codes. (The prior no-boot was a
  flipped FPC; once re-seated, the board boots and the sensor ACKs.)
- Connector part, footprint, and CAM1/CAM2 pinout: the official schematic PDF, sheet 13.
- Driver bus types (IMX219 MIPI vs OV5640 DVP): the A733 vendor kernel source
  (`orangepi-xunlong/linux-orangepi`, `orange-pi-6.6-sun60iw2`).

## Corrections to earlier docs (same investigation)

Earlier edits in this repo reached wrong conclusions on this and are corrected to point
here: the prototype `prototypes/npu-occupancy/README.md` camera section, the 2026-08-09
addendum in `docs/design/non-verbal-communication.md`, and the note in
`docs/SATELLITE_CAMERA.md`. The sequence of wrong turns (Waveshare 15-pin "top pick" →
"24-pin, incompatible" → "CSI dead end, use RTSP" → **schematic: Pi-standard 22-pin, IMX219
works with a €5 cable**) is a case study in *verify against the actual hardware/schematic,
not extrapolation from lookalike boards.*
