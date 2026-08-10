# A733 (Orange Pi Zero 3W) satellite SPI display — bring-up reference

**Status: software port DONE, hardware not yet attached (2026-08-10).** The satellite's
`ST7789Display` driver is now Orange-Pi-capable; attaching a small SPI TFT needs the
physical wiring + the pin/overlay values filled in and an on-board test. Mirrors the camera
bring-up (`a733-satellite-camera.md`): the *hardware* fits (Pi-compatible header), the
*driver/GPIO layer* is where the Pi ecosystem stops.

## Why it's mostly reuse

- The board's 40-pin header is **Pi-pin-function-compatible** (schematic sheet 18): SPI0 on
  pins 19/21/23/24/26, I2C on 3/5, plenty of GPIO, 3V3/5V/GND on the Pi positions. A Pi SPI
  display HAT mates mechanically and the signals land on the right pins.
- The satellite already ships an **`ST7789Display`** SPI driver (Whisplay HAT). It's now
  **config-driven** (`gpio_backend`, `spi_bus/device/speed`, `dc/rst/bl` pins, `gpiochip`).

## The one real gap: GPIO backend (gpiozero → libgpiod)

gpiozero is Raspberry-Pi / **BCM-numbered** and can't drive Allwinner pins. So `display.py`
gained a **`sunxi` backend**: DC/RST/BL via **`python-periphery`** (libgpiod) by `(gpiochip,
line-offset)`, exposing the same `.on()/.off()/.value` surface — the rest of the driver is
unchanged. `spidev` + `python-periphery` are in the satellite image (Dockerfile). Backlight
is **on/off** on sunxi (no dimming — a PWM pin + overlay would be needed for that).

## Attaching a display — steps

1. **Pick a display:** a small SPI TFT — ST7789 240×280 (same as Whisplay, zero-code) or
   ILI9341 320×240 (needs an ILI9341 init sequence variant). 4-wire SPI + DC + RST + BL.
2. **Wire to the 40-pin header:** SCLK→23, MOSI→19, CS0→24, plus 3V3→1 (or 5V→2 per panel),
   GND→any GND. Choose GPIO header pins for **DC**, **RST**, **BL** (e.g. 22/13/12) — note
   which Allwinner pin each is (from schematic sheet 18: e.g. pin 22 = a `PD0/PE0` line).
3. **Enable SPI (device-tree overlay):** the BSP ships `spi1-cs0-spidev` / `spi2-cs0-spidev`
   overlays. Add the matching one to `overlays=` in `/boot/orangepiEnv.txt`, reboot, confirm
   `/dev/spidevX.Y` appears. Set `spi_bus`/`spi_device` to X/Y.
4. **Find the GPIO line offsets:** on the board, `gpiodetect` + `gpioinfo` — the main pinctrl
   is one `gpiochipN`. A line offset ≈ `bank_index*32 + pin` (PA=0,PB=1,…PE=4,…), e.g. `PE2`
   = 4*32+2 = 130 — **verify against `gpioinfo` line names on the actual board.** Put the
   offsets in `dc_pin`/`rst_pin`/`bl_pin` and the chip in `gpiochip`.
5. **Mount into the pod** (`k8s/satellite-esszimmer.yaml`, like the camera): hostPath
   `/dev/spidevX.Y` (CharDevice) + `/dev/gpiochipN` (CharDevice).
6. **Config** — the Esszimmer ConfigMap `display:` section:
   ```yaml
   display:
     enabled: true
     gpio_backend: sunxi
     width: 240
     height: 280
     spi_bus: 1            # the /dev/spidevX.Y from step 3
     spi_device: 0
     dc_pin: 130          # gpiochip line offsets from step 4
     rst_pin: 131
     bl_pin: 132
     gpiochip: "/dev/gpiochip0"
   ```
   (Bare-metal Pi satellites keep `gpio_backend: rpi` + BCM pins — byte-identical default.)
7. **Rebuild the satellite image (v9→v10)** with the updated code, `ctr` import, apply,
   restart — then verify the render on the panel.

## Host provisioning (reproducible)

The spidev overlay + gpio access are host-side — fold into `provision-camera.yml`'s pattern
(a `display`/`spidev` task branch) or add a small `provision-display.yml`, driven off
`display_enabled` + `display_gpio_backend` (already in the Ansible template + group_vars
model). The `python-periphery`/`spidev` runtime is in the image, not the host.

## What's done vs pending

- ✅ Driver config-driven + sunxi (libgpiod) GPIO backend; `spidev`+`periphery` in the image;
  config fields + parse + Ansible template. `gpio_backend` defaults to `rpi` (Pi unaffected).
- ⏳ Physical display + wiring + the exact `/dev/spidevX.Y` + gpiochip line offsets + overlay,
  the ConfigMap `display:` block, and an on-board render test. ILI9341 needs its init variant.
