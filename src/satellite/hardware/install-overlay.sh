#!/bin/bash
#
# Install custom GPCLK overlay for ReSpeaker 2-Mics Pi HAT
#
# This script compiles and installs the custom device tree overlay
# that fixes the "No MCLK configured" error on kernel 6.x.
#
# Usage: sudo ./install-overlay.sh [simple]
#   - No argument: Install full overlay (seeed-2mic-gpclk)
#   - "simple": Install simplified overlay (seeed-2mic-gpclk-simple)

set -e

# Configuration
OVERLAY_DIR="/boot/firmware/overlays"
CONFIG_FILE="/boot/firmware/config.txt"

# Determine which overlay to install
if [ "$1" = "simple" ]; then
    OVERLAY_NAME="seeed-2mic-gpclk-simple"
    DTS_FILE="seeed-2mic-gpclk-simple-overlay.dts"
else
    OVERLAY_NAME="seeed-2mic-gpclk"
    DTS_FILE="seeed-2mic-gpclk-overlay.dts"
fi

DTBO_FILE="${OVERLAY_NAME}.dtbo"

echo "==========================================="
echo "ReSpeaker 2-Mics GPCLK Overlay Installer"
echo "==========================================="
echo ""
echo "Overlay: $OVERLAY_NAME"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "Error: This script must be run as root (sudo)"
    exit 1
fi

# Check if DTS file exists
SCRIPT_DIR="$(dirname "$(realpath "$0")")"
if [ ! -f "$SCRIPT_DIR/$DTS_FILE" ]; then
    echo "Error: DTS file not found: $SCRIPT_DIR/$DTS_FILE"
    exit 1
fi

# Check for device tree compiler
if ! command -v dtc &> /dev/null; then
    echo "Installing device-tree-compiler..."
    apt-get update && apt-get install -y device-tree-compiler
fi

# Compile the overlay
echo "Compiling overlay..."
dtc -@ -I dts -O dtb -o "/tmp/$DTBO_FILE" "$SCRIPT_DIR/$DTS_FILE"

if [ $? -ne 0 ]; then
    echo "Error: Failed to compile overlay"
    exit 1
fi

echo "Overlay compiled successfully"

# Copy to boot partition
echo "Copying to $OVERLAY_DIR..."
cp "/tmp/$DTBO_FILE" "$OVERLAY_DIR/"
chmod 644 "$OVERLAY_DIR/$DTBO_FILE"

echo "Overlay installed to $OVERLAY_DIR/$DTBO_FILE"

# Remove old overlays from config.txt
echo "Updating $CONFIG_FILE..."

# Create backup
cp "$CONFIG_FILE" "${CONFIG_FILE}.bak.$(date +%Y%m%d%H%M%S)"

# Remove any existing seeed/wm8960 overlays
sed -i '/dtoverlay=seeed-2mic-voicecard/d' "$CONFIG_FILE"
sed -i '/dtoverlay=wm8960-soundcard/d' "$CONFIG_FILE"
sed -i '/dtoverlay=seeed-2mic-gpclk/d' "$CONFIG_FILE"
sed -i '/dtoverlay=seeed-2mic-gpclk-simple/d' "$CONFIG_FILE"

# Ensure I2S is enabled
if ! grep -q "^dtparam=i2s=on" "$CONFIG_FILE"; then
    echo "dtparam=i2s=on" >> "$CONFIG_FILE"
    echo "Added: dtparam=i2s=on"
fi

# Ensure I2C is enabled
if ! grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE"; then
    echo "dtparam=i2c_arm=on" >> "$CONFIG_FILE"
    echo "Added: dtparam=i2c_arm=on"
fi

# Add the new overlay
echo "dtoverlay=$OVERLAY_NAME" >> "$CONFIG_FILE"
echo "Added: dtoverlay=$OVERLAY_NAME"

echo ""
echo "==========================================="
echo "Installation complete!"
echo "==========================================="
echo ""
echo "Config.txt has been updated with:"
grep -E "^dtparam=i2s|^dtparam=i2c|^dtoverlay=$OVERLAY_NAME" "$CONFIG_FILE"
echo ""
echo "A backup was created at: ${CONFIG_FILE}.bak.*"
echo ""
echo "To complete installation, reboot the system:"
echo "  sudo reboot"
echo ""

# Ask to reboot
read -p "Reboot now? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Rebooting..."
    reboot
fi
