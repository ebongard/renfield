#!/usr/bin/env bash
#
# Scan a duplex stack from the ScanSnap into a searchable PDF.
#
# Phase 0 of docs/design/scanner-ingest.md: restores scanning on macOS Tahoe
# after ScanSnap Manager stopped working, WITHOUT committing to any of the
# routing architecture. Output lands in a staging folder; moving it into the
# right instance's injection folder stays a manual step for now.
#
# WHY THIS EXISTS: ScanSnap Manager 7.2.50 is x86_64-only, EOL for the S1500,
# and on Tahoe its device-watcher half still claims the USB device EXCLUSIVELY
# while its scanning half no longer works — bricking itself and every
# alternative. It must stay out of Login Items or SANE dies again with
# "USBDeviceOpen: another process has device opened for exclusive access".
#
# The S1500 uses the SANE `fujitsu` backend (SCSI over USB). There is NO .nal
# firmware to extract — that is only the S300/S1100/S1300 family.
#
# Usage:  bin/scan.sh [name]
#   SCAN_OUT=~/Scans/inbox   staging folder (point at your injection folder)
#   SCAN_DPI=300  SCAN_MODE=Color  SCAN_SOURCE='ADF Duplex'  SCAN_LANG=deu+eng

set -euo pipefail

export PATH="/opt/homebrew/bin:${PATH}"

SCAN_OUT="${SCAN_OUT:-${HOME}/Scans/inbox}"
SCAN_DPI="${SCAN_DPI:-300}"
SCAN_MODE="${SCAN_MODE:-Color}"
SCAN_SOURCE="${SCAN_SOURCE:-ADF Duplex}"
SCAN_LANG="${SCAN_LANG:-deu+eng}"
# SANE's default scan area is US LETTER (279.364mm) — 17.7mm SHORTER than A4.
# Left at the default, the bottom of every A4 page is silently cut off, taking
# the footer with it (bank details, Steuernummer, totals, signature lines) —
# exactly the Schicht-A fact material. Setting --page-height also raises the
# -y ceiling, and -y then defaults to the new maximum, so -y needs no flag.
# For mixed paper sizes consider --ald (scanner detects the lower edge).
SCAN_PAGE_HEIGHT="${SCAN_PAGE_HEIGHT:-297}"   # A4; 279.364 = US Letter
# Crop-to-content is OFF by default: it gives every page a DIFFERENT size, which
# makes the resulting PDF look ragged. Uniform A4 pages read better.
SCAN_CROP="${SCAN_CROP:-no}"
# Scanner-side image enhancement. ScanSnap Manager applied these silently, which
# is part of why its output looked punchier; raw SANE output is deliberately
# flat. Defaults are the scanner's neutral values (no-ops) — tune against a real
# comparison rather than guessing. emphasis: negative smooths, positive sharpens.
# Scanner-side software deskew is OFF by default. Measured on a real duplex
# sheet: the front came out straight and the BACK was rotated ~34 degrees by
# swdeskew itself. It estimates the angle from content, and a sparse back side
# with fold lines and a barcode strip fools it badly. A straight feed needs no
# deskew; a crooked feed skews BOTH sides, so a one-sided tilt is always this bug.
SCAN_DESKEW="${SCAN_DESKEW:-no}"
# Neutralise the scanner's colour cast. Raw S1500 output is strongly blue: the
# paper peak measured R226 G236 B253 on white paper (B-R = +26 across the page).
# Paper is neutral by definition, so each channel's paper peak is rescaled to a
# common near-white. Measured result: B-R +26.0 -> +2.6. This is the correction
# ScanSnap Manager applied silently.
SCAN_WHITE_BALANCE="${SCAN_WHITE_BALANCE:-yes}"
# Show-through: duplex scanning lights the sheet from both sides, so the reverse
# side's ink is faintly visible. Measured on a real page it lives in the 200-224
# band (7.08% of pixels), well clear of real ink (0-79). Mapping a level just
# BELOW the paper peak to pure white erases it. This is the fraction of the
# paper peak that becomes white: 1.0 = white balance only, lower = more
# aggressive. 0.90 measured: show-through 7.08% -> 1.93%, ink 5.59% -> 5.30%
# (that loss is antialiasing crisping, not lost text). Table rules, light logos
# and barcodes survive at 0.90; go lower only against a real comparison.
SCAN_WHITE_CLIP="${SCAN_WHITE_CLIP:-0.90}"
SCAN_BRIGHTNESS="${SCAN_BRIGHTNESS:-0}"   # -127..127
SCAN_CONTRAST="${SCAN_CONTRAST:-0}"       # -127..127
SCAN_EMPHASIS="${SCAN_EMPHASIS:-0}"       # -128..127
# Blank-page drop: discard pages below this % of dark pixels. 0 disables.
SCAN_SKIP_BLANK="${SCAN_SKIP_BLANK:-2}"

name="${1:-scan}"
stamp="$(date +%Y%m%d-%H%M%S)"
base="${name//[^A-Za-z0-9._-]/_}-${stamp}"

for tool in scanimage img2pdf ocrmypdf; do
    command -v "$tool" >/dev/null || {
        echo "error: $tool not found. brew install sane-backends img2pdf ocrmypdf tesseract-lang" >&2
        exit 1
    }
done

if pgrep -qf 'ScanSnap Manager.app/Contents/MacOS'; then
    echo "error: ScanSnap Manager is running and holds the scanner exclusively." >&2
    echo "       Quit it and remove it from System Settings > General > Login Items." >&2
    exit 1
fi

device="$(scanimage -f '%d%n' 2>/dev/null | grep -i fujitsu | head -1 || true)"
[ -n "$device" ] || { echo "error: no Fujitsu scanner found (scanimage -L)" >&2; exit 1; }

# Pre-flight the destination BEFORE scanning. Doing this afterwards means an
# unwritable SCAN_OUT aborts under `set -e`, the EXIT trap wipes the work dir,
# and a whole multi-page stack is lost to a mkdir that could have been checked
# in advance.
mkdir -p "$SCAN_OUT" 2>/dev/null || {
    echo "error: cannot create output folder: $SCAN_OUT" >&2; exit 1; }
[ -w "$SCAN_OUT" ] || { echo "error: output folder not writable: $SCAN_OUT" >&2; exit 1; }

work="$(mktemp -d "${TMPDIR:-/tmp}/scan.XXXXXX")"
trap 'rm -rf "$work"' EXIT

echo "Scanning from '${device}' (${SCAN_SOURCE}, ${SCAN_MODE}, ${SCAN_DPI}dpi, page height ${SCAN_PAGE_HEIGHT}mm)..."

# --batch writes one file per ADF page. "out of documents" is the NORMAL
# terminator once the feeder empties, so it must not fail the run; an empty
# feeder at the START is a real error, caught by the page count below.
set +e
scanimage -d "$device" \
    --source "$SCAN_SOURCE" --mode "$SCAN_MODE" --resolution "$SCAN_DPI" \
    --page-height "$SCAN_PAGE_HEIGHT" \
    --brightness "$SCAN_BRIGHTNESS" --contrast "$SCAN_CONTRAST" \
    --emphasis "$SCAN_EMPHASIS" \
    --swdeskew="$SCAN_DESKEW" --swcrop="$SCAN_CROP" --swskip "$SCAN_SKIP_BLANK" \
    --format=png --batch="${work}/p%04d.png" >"${work}/scan.log" 2>&1
set -e
grep -v 'out of documents' "${work}/scan.log" >&2 || true

# A finished stack ENDS with "out of documents", so that is not a fault, and
# "rounded value of ..." is just option quantisation (it appears on EVERY run —
# treating any scanimage: line as an error would fault every scan). Anything
# else scanimage reports means the stack did NOT feed completely: a jam, a
# double feed, an opened cover, a USB fault. A silently short document is worse
# than a loud failure when it is about to be filed into an archive, so refuse.
scan_fault="$(grep '^scanimage:' "${work}/scan.log" \
    | grep -Ev 'out of documents|rounded value' || true)"
if [ -n "$scan_fault" ]; then
    partial=$(find "$work" -name 'p*.png' | wc -l | tr -d ' ')
    echo "error: the scanner faulted mid-stack — refusing to file a partial document." >&2
    echo "$scan_fault" | sed 's/^/  /' >&2
    echo "  ${partial} page(s) were already scanned and are KEPT at:" >&2
    echo "    ${work}" >&2
    echo "  Clear the feeder and re-scan the whole stack." >&2
    trap - EXIT   # keep the pages rather than wiping a partly-fed stack
    exit 1
fi

pages=$(find "$work" -name 'p*.png' | wc -l | tr -d ' ')
[ "$pages" -gt 0 ] || { echo "error: no pages scanned — is the feeder loaded?" >&2; exit 1; }
echo "Scanned ${pages} page(s)."

if [ "$SCAN_WHITE_BALANCE" = "yes" ]; then
    echo "Neutralising colour cast and clearing show-through..."
    SCAN_WORK="$work" SCAN_DPI="$SCAN_DPI" SCAN_WHITE_CLIP="$SCAN_WHITE_CLIP" python3 - <<'WB'
import os, glob
from PIL import Image

work = os.environ["SCAN_WORK"]
dpi = float(os.environ["SCAN_DPI"])
clip = float(os.environ["SCAN_WHITE_CLIP"])

for f in sorted(glob.glob(os.path.join(work, "p*.png"))):
    im = Image.open(f).convert("RGB")
    chans = im.split()
    # The dominant peak above mid-grey is the paper; ink is a minority of pixels.
    modes = [max(range(128, 256), key=lambda v, h=c.histogram(): h[v]) for c in chans]
    # Guard: a genuinely dark page (a photo, an inverted print) has no paper peak
    # to anchor on. Rescaling it would blow out the image, so leave it alone.
    if min(modes) < 150:
        continue
    # One LUT per channel does BOTH jobs: dividing by each channel's own paper
    # peak neutralises the cast, and the clip factor puts a level just below that
    # peak at pure white, which erases show-through.
    out = Image.merge("RGB", [
        c.point([min(255, int(v * 255.0 / (m * clip))) for v in range(256)])
        for c, m in zip(chans, modes)
    ])
    # PIL does NOT carry the source pHYs across, and img2pdf sizes the PDF page
    # from image DPI — losing it built a 26-inch page at 96 dpi. Set it explicitly.
    out.save(f, dpi=(dpi, dpi))
WB
fi

img2pdf --output "${work}/${base}.pdf" "${work}"/p*.png

# QUALITY-CRITICAL FLAGS. Getting these wrong is what made the first version
# visibly worse than the vendor software:
#
#   --optimize 0  ocrmypdf's DEFAULT (--optimize 1) transcodes large scans to
#                 LOSSY JPEG. A real 2421x3299 RGB page came back as jpeg at
#                 3.2% of raw. This must stay 0 — a document scan is an archival
#                 master, not a web image.
#   no --deskew   the scanner already deskewed on raw sensor data (--swdeskew),
#                 which is the better place for it. Doing it again here is a
#                 SECOND resampling pass: softer text, no benefit.
#   no --rotate-pages  its orientation detection needs a page's worth of text
#                 and fails quietly otherwise, so it can rotate a page wrongly.
#   --skip-text   never re-OCR a page that already carries text.
if ! ocrmypdf -l "$SCAN_LANG" --skip-text --optimize 0 --quiet \
        "${work}/${base}.pdf" "${SCAN_OUT}/${base}.pdf" 2>"${work}/ocr.err"; then
    # Do NOT discard the reason. A bad SCAN_LANG, a missing tessdata language or
    # an unreadable page all land here, and "OCR failed" alone is undebuggable.
    echo "warning: OCR failed — writing the un-OCR'd PDF instead. Reason:" >&2
    sed 's/^/  /' "${work}/ocr.err" >&2 || true
    cp "${work}/${base}.pdf" "${SCAN_OUT}/${base}.pdf"
fi

echo "→ ${SCAN_OUT}/${base}.pdf (${pages} page(s))"
