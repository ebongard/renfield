# Satellite Hardware Configuration

This directory contains device tree overlays and scripts for configuring Raspberry Pi hardware used by Renfield satellites.

## ReSpeaker 2-Mics Pi HAT - MCLK Fix

### Problem

On kernel 6.x (particularly 6.12.x and later), the ReSpeaker 2-Mics Pi HAT fails to capture audio with the error:

```
wm8960 1-001a: No MCLK configured
ASoC: error at snd_soc_dai_hw_params on wm8960-hifi: -22
```

### Root Cause

The WM8960 codec requires a master clock (MCLK) signal at 12.288 MHz. The standard overlays (`seeed-2mic-voicecard`, `wm8960-soundcard`) use a `fixed-clock` device tree node:

```dts
wm8960_mclk: wm8960_mclk {
    compatible = "fixed-clock";
    clock-frequency = <12288000>;
};
```

This `fixed-clock` is **metadata only** - it tells the driver "there is a clock at this frequency" but doesn't actually generate any signal. On older kernels, the WM8960 driver didn't verify the clock existed, but on kernel 6.x, the driver is stricter and requires a real clock source.

### Solution

Our custom overlays use the **real BCM2835 GPCLK0** hardware clock instead of a fake fixed-clock:

```dts
wm8960: wm8960@1a {
    clocks = <&clocks 38>;  /* BCM2835_CLOCK_GP0 */
    clock-names = "mclk";
    assigned-clocks = <&clocks 38>;
    assigned-clock-rates = <12288000>;
    pinctrl-names = "default";
    pinctrl-0 = <&wm8960_gpclk_pin>;
};
```

This configures:
1. GPIO4 in ALT0 mode (GPCLK0 function)
2. The BCM2835 clock manager to output 12.288 MHz
3. The WM8960 driver to consume this clock, which triggers `clk_prepare_enable()` and starts the actual clock output

## Files

| File | Description |
|------|-------------|
| `seeed-2mic-gpclk-overlay.dts` | Full overlay with regulators and complete routing |
| `seeed-2mic-gpclk-simple-overlay.dts` | Simplified overlay (try if full version fails) |
| `install-overlay.sh` | Installation script |

## Installation

### Automatic (Recommended)

```bash
# Copy files to satellite
rsync -av src/satellite/hardware/ satellite-hostname:/tmp/hardware/

# SSH to satellite and run installer
ssh satellite-hostname
cd /tmp/hardware
sudo ./install-overlay.sh

# Or for simplified version:
sudo ./install-overlay.sh simple
```

### Manual

```bash
# On the satellite:

# 1. Install device tree compiler
sudo apt install device-tree-compiler

# 2. Compile the overlay
dtc -@ -I dts -O dtb -o seeed-2mic-gpclk.dtbo seeed-2mic-gpclk-overlay.dts

# 3. Copy to boot partition
sudo cp seeed-2mic-gpclk.dtbo /boot/firmware/overlays/

# 4. Edit /boot/firmware/config.txt
#    Remove any existing audio overlays:
#    - dtoverlay=seeed-2mic-voicecard
#    - dtoverlay=wm8960-soundcard
#
#    Add:
#    dtparam=i2s=on
#    dtparam=i2c_arm=on
#    dtoverlay=seeed-2mic-gpclk

# 5. Reboot
sudo reboot
```

## Verification

After installation and reboot:

```bash
# Check if sound card is detected
aplay -l
# Should show: card X: seeed2micvoicec [seeed-2mic-voicecard], ...

# Check for MCLK errors in dmesg
dmesg | grep -i mclk
# Should be empty (no errors)

# Check WM8960 codec
dmesg | grep -i wm8960
# Should show successful probe without errors

# Test recording
arecord -D plughw:0,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav
aplay test.wav
```

## Troubleshooting

### "failed to get clock" error

If you see errors about failing to get the clock, the `clocks` phandle might not be resolving correctly. Try:

1. Check that `i2c_arm=on` is in config.txt
2. Use the simplified overlay: `sudo ./install-overlay.sh simple`
3. Check kernel log: `dmesg | grep -E 'clk|wm8960|asoc'`

### Overlay compilation fails

Ensure you have the device tree compiler with overlay support:
```bash
sudo apt install device-tree-compiler
dtc --version  # Should be 1.6.0 or later
```

The `-@` flag is required for overlay compilation.

### No sound card detected

1. Check I2C is working: `i2cdetect -y 1` (should show device at 0x1a)
2. Check overlay is loaded: `dtoverlay -l`
3. Verify config.txt changes took effect: `cat /boot/firmware/config.txt`

## Technical Details

### BCM2835 Clock IDs

The clock manager provides several general-purpose clocks:

| ID | Name | GPIO | Function |
|----|------|------|----------|
| 38 | BCM2835_CLOCK_GP0 | GPIO4 | GPCLK0 (ALT0) |
| 39 | BCM2835_CLOCK_GP1 | GPIO5 | GPCLK1 (ALT0) |
| 40 | BCM2835_CLOCK_GP2 | GPIO6 | GPCLK2 (ALT0) |

We use GPCLK0 on GPIO4 because it's conveniently located next to the I2S pins.

### Clock Rate Calculation

For audio at 48kHz sample rate with 256x oversampling:
- MCLK = 48000 × 256 = 12,288,000 Hz (12.288 MHz)

The BCM2835 clock manager can derive this from the 19.2 MHz crystal oscillator with minimal jitter.

## References

- [Raspberry Pi Device Tree Overlays](https://github.com/raspberrypi/linux/tree/rpi-6.12.y/arch/arm/boot/dts/overlays)
- [BCM2835 Clock Driver](https://github.com/raspberrypi/linux/blob/rpi-6.12.y/drivers/clk/bcm/clk-bcm2835.c)
- [WM8960 Driver](https://github.com/torvalds/linux/blob/master/sound/soc/codecs/wm8960.c)
- [Raspberry Pi Forums - GPCLK Discussion](https://forums.raspberrypi.com/viewtopic.php?t=136988)
