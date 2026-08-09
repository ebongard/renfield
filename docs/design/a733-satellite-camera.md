# A733 (Orange Pi Zero 3W) satellite camera — hardware bring-up reference

**Status: investigated + schematic-verified 2026-08-09. Not yet built.** This is the
authoritative camera reference for the Allwinner **A733** satellite (the Esszimmer node,
`orangepizero3w` / 192.168.1.82). It supersedes earlier, wrong conclusions in this repo
that said "CSI is a dead end, use RTSP" — the schematic shows the opposite.

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

## Bring-up steps

1. Order the 15→22-pin Pi-Zero camera cable; connect the IMX219 module to **CAM1**.
2. Write + install the DT overlay (below); enable it in `/boot/orangepiEnv.txt`
   (`overlays=...`), reboot.
3. Verify `/dev/video0` appears and `v4l2-ctl --list-devices` / a test capture works.
4. Mount `/dev/video0` + `/dev/media0` into the Esszimmer pod (`k8s/satellite-esszimmer.yaml`,
   alongside the existing `/dev/snd` mounts).
5. Wire it to the occupancy/gesture prototypes (`prototypes/npu-occupancy/`) — the
   detector + WS contract are camera-agnostic; only the frame source changes.

### DT overlay — draft (confirmed pins; a few bring-up TODOs)

```dts
/* sun60i-a733-orangepi-zero3w-cam-imx219.dts — CAM1 / CSI0, IMX219 2-lane.
 * Pins verified from OPi ZERO 3W V1.2 schematic sheet 13. */
&vind0            { status = "okay"; };
&csi0             { status = "okay"; };            /* MCSIA / CAM1 */
&twi11            { status = "okay"; };            /* sensor i2c (pins 20/21) */

&sensor0 {
    device_type       = "sensor0";
    sensor0_mname     = "imx219_mipi";             /* in-tree MIPI driver (=m) */
    sensor0_twi_id    = <11>;                      /* TWI11 */
    sensor0_twi_addr  = <0x10>;                    /* IMX219 default */
    sensor0_mclk_id   = <0>;                       /* TODO verify vs csi_mclk group */
    /* PE6 = pin17, PE5 = pin18 — assign reset/pwdn per driver expectation + polarity: */
    sensor0_reset     = <&pio 'E' 6 ...>;          /* TODO confirm reset vs pwdn + active level */
    sensor0_pwdn      = <&pio 'E' 5 ...>;
    sensor0_mipi_id   = <0>;
    sensor0_lane      = <2>;                        /* IMX219 = 2 lanes */
    status            = "okay";
};
```

TODOs to resolve on first bring-up (hardware-in-the-loop): exact `mclk` source (Pi modules
self-clock — the SoC mclk may be unused), and the reset/pwdn GPIO polarity. Everything else
(bus = TWI11, GPIOs = PE5/PE6, CSI0, 2-lane, 0x10) is schematic-confirmed.

## Verification / provenance

- Board identity, kernel config, DTB, i2c buses, absent `/dev/video`: read live on the
  board over SSH (`root@192.168.1.82`), 2026-08-09.
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
