#!/usr/bin/env bash
#
# Capture a satellite's REAL room ambient as wakeword hard-negative material.
#
# MANDATORY commissioning step for every new satellite installation.
# Procedure + rationale: docs/SATELLITE_ACOUSTIC_COMMISSIONING.md
#
# Why mandatory: the synthetic false-positive metric lies. renfield_de v1
# measured ~16 FP/hr on synthetic speech and then false-fired ~500/hr in real
# rooms. Every room has its own noise signature (tiles, fans, running water,
# HVAC, a TV) and a wakeword model only rejects what it was trained to reject.
# A satellite commissioned without its room in the negative set is a
# false-positive storm waiting for the room to be used.
#
# What it does:
#   1. reads the satellite's LIVE audio config + capture gains (provenance)
#   2. records RAW multi-channel audio off the shared dsnoop PCM — the exact
#      stream the detector reads; no service stop needed on HAT mics
#   3. runs a sanity gate on the capture (DC offset / clipping / dead mic)
#   4. stores it under data/wakeword-ambient/<satellite>/ with a JSON sidecar
#
# Raw multi-channel is deliberate. The detector-side mono (beamform / channel
# select / downmix) is DERIVED later by
# src/satellite/wakeword-training/scripts/derive_detector_mono.py. Recording a
# pre-downmixed mono throws away the ability to reproduce what the detector
# actually hears, and on a beamforming satellite that mono is the wrong signal.
#
# Usage:
#   bin/capture-room-ambient.sh satellite-kinderbad
#   bin/capture-room-ambient.sh satellite-kinderbad --minutes 45 --label evening-use
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INVENTORY="$REPO_ROOT/src/satellite/provisioning/inventory.yml"
INSPECT="$REPO_ROOT/bin/_ambient_inspect.py"
OUT_ROOT="$REPO_ROOT/data/wakeword-ambient"
VENV_PY="/opt/renfield-satellite/venv/bin/python"
REMOTE_DIR="/tmp/renfield-ambient"

HOST=""
MINUTES=15
LABEL="ambient"

usage() {
    sed -n '3,30p' "${BASH_SOURCE[0]}" | sed 's/^#\{1,\} \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minutes) MINUTES="$2"; shift 2 ;;
        --label)   LABEL="$2"; shift 2 ;;
        --out)     OUT_ROOT="$2"; shift 2 ;;
        -h|--help) usage 0 ;;
        -*)        echo "unknown flag: $1" >&2; usage 1 ;;
        *)         HOST="$1"; shift ;;
    esac
done

[[ -n "$HOST" ]] || { echo "error: no satellite host given" >&2; usage 1; }
[[ -f "$INVENTORY" ]] || { echo "error: inventory not found: $INVENTORY" >&2; exit 1; }
[[ -f "$INSPECT" ]] || { echo "error: helper not found: $INSPECT" >&2; exit 1; }
# HOST and LABEL both land in filenames, remote paths, and an embedded Python
# string literal (see the ansible-inventory call below) — keep them safe fragments.
[[ "$HOST" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "error: host must match [A-Za-z0-9._-]" >&2; exit 1; }
[[ "$LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "error: --label must match [A-Za-z0-9._-]" >&2; exit 1; }
[[ "$MINUTES" =~ ^[0-9]+$ ]] && (( MINUTES > 0 )) || { echo "error: --minutes must be a positive integer" >&2; exit 1; }

STAMP="$(date +%Y%m%d-%H%M)"
BASENAME="${HOST}-${LABEL}-${STAMP}"
DEST_DIR="$OUT_ROOT/$HOST"
INSPECT_B64="$(base64 < "$INSPECT" | tr -d '\n')"

ansible_sh() {
    ansible "$HOST" -i "$INVENTORY" -m shell -a "$1" 2>&1
}

# Ship the helper base64-encoded so no quoting layer can mangle it.
run_inspect() {
    ansible_sh "echo $INSPECT_B64 | base64 -d > $REMOTE_DIR/_inspect.py && $VENV_PY $REMOTE_DIR/_inspect.py $1"
}

extract_json() {
    sed -n 's/^AMBIENT_JSON://p' | tail -1
}

echo "==> Probing live audio config on $HOST"
ansible_sh "mkdir -p $REMOTE_DIR" >/dev/null
PROBE="$(run_inspect probe | extract_json)"
[[ -n "$PROBE" ]] || { echo "error: probe returned nothing — is $HOST reachable?" >&2; exit 1; }

# `read` succeeds on empty input, so a failing command substitution here does
# NOT trip `set -e`. Without the explicit check below, a helper error (e.g.
# satellite.yaml unreadable) left every field empty and the run died later as a
# misleading "capture failed".
PARSED="$(
    python3 -c '
import json, sys
p = json.load(sys.stdin)
if "error" in p:
    sys.exit(p["error"])
print(p["device"], p["channels"], p["sample_rate"], p["beamforming"])
' <<<"$PROBE"
)" || { echo "error: could not read the audio config from $HOST" >&2; exit 1; }
read -r DEVICE CHANNELS RATE BEAMFORMING <<<"$PARSED"
[[ -n "$DEVICE" && "$CHANNELS" =~ ^[0-9]+$ && "$RATE" =~ ^[0-9]+$ ]] \
    || { echo "error: incomplete audio config from $HOST: '$PARSED'" >&2; exit 1; }

echo "    device=$DEVICE channels=$CHANNELS rate=$RATE beamforming=$BEAMFORMING"
echo "    gains: $(python3 -c '
import json, sys
for k, v in json.load(sys.stdin).get("mixer", {}).items():
    print("           %-22s %s" % (k, v))
' <<<"$PROBE" | sed '1s/^ *//')"

echo "==> Capturing ${MINUTES} min of raw ambient — the service keeps running"
ansible_sh "arecord -D '$DEVICE' -f S16_LE -r $RATE -c $CHANNELS -d $(( MINUTES * 60 )) $REMOTE_DIR/${BASENAME}.wav" >/dev/null \
    || { echo "error: capture failed on $HOST" >&2; exit 1; }

echo "==> Sanity gate"
GATE_JSON="$(run_inspect "gate $REMOTE_DIR/${BASENAME}.wav" | extract_json)"
[[ -n "$GATE_JSON" ]] || { echo "error: sanity gate produced no report" >&2; exit 1; }

mkdir -p "$DEST_DIR"
echo "==> Transferring"
# NOT `ansible -m fetch`: it base64-encodes the payload over the control
# connection and stalls on a multi-hundred-MB capture (a 45-min stereo capture
# is ~173 MB). scp streams it. Resolve the address from the inventory so this
# keeps working when the host is not in ~/.ssh/config.
SSH_TARGET="$(ansible-inventory -i "$INVENTORY" --host "$HOST" 2>/dev/null | python3 -c '
import json, sys
h = json.load(sys.stdin)
user = h.get("ansible_user")
addr = h.get("ansible_host") or "'"$HOST"'"
print(f"{user}@{addr}" if user else addr)
')"
scp -q "$SSH_TARGET:$REMOTE_DIR/${BASENAME}.wav" "$DEST_DIR/${BASENAME}.wav" \
    || { echo "error: transfer failed from $SSH_TARGET" >&2; exit 1; }

# A silently truncated capture is worse than no capture: it trains the model on
# material that does not match the room. Verify before deleting the source.
REMOTE_SUM="$(ansible_sh "sha256sum $REMOTE_DIR/${BASENAME}.wav" | grep -oE '^[0-9a-f]{64}' | head -1)"
LOCAL_SUM="$(shasum -a 256 "$DEST_DIR/${BASENAME}.wav" | cut -d' ' -f1)"
if [[ -z "$REMOTE_SUM" || "$REMOTE_SUM" != "$LOCAL_SUM" ]]; then
    echo "error: checksum mismatch — transfer is incomplete, source kept on $HOST" >&2
    echo "       remote=${REMOTE_SUM:-<none>} local=$LOCAL_SUM" >&2
    exit 1
fi

ansible_sh "rm -f $REMOTE_DIR/${BASENAME}.wav $REMOTE_DIR/_inspect.py" >/dev/null || true

set +e
python3 - "$GATE_JSON" "$PROBE" "$DEST_DIR/${BASENAME}.json" "$HOST" "$LABEL" "$STAMP" <<'PY'
import json, sys

gate, probe = json.loads(sys.argv[1]), json.loads(sys.argv[2])
sidecar = dict(gate)
sidecar.update({
    "satellite": sys.argv[4], "label": sys.argv[5], "captured_at": sys.argv[6],
    "audio_config": {k: probe[k] for k in
                     ("device", "channels", "sample_rate", "beamforming",
                      "combine", "select_channel", "mic_spacing") if k in probe},
})
with open(sys.argv[3], "w") as fh:
    json.dump(sidecar, fh, indent=2, sort_keys=True)
    fh.write("\n")

print("    duration %.0fs  channels %d" % (gate["duration_seconds"], gate["channels"]))
fail, warn = [], []
for c in gate["per_channel"]:
    print("    ch%d  DC %+8.1f  RMS %7.1f (%6.1f dBFS)  peak %7.1f  crest %-5s clipped %.4f%%"
          % (c["channel"], c["dc_offset"], c["rms"], c["rms_dbfs"], c["peak"],
             c["crest"], c["clipped_fraction"] * 100))
    # A DC offset dominating the signal means the capture measures the codec's
    # bias, not the room. That is what made Kinderbad report audio_rms 1812
    # while the actual room sat at -55 dBFS. Unusable as a negative.
    if c["rms"] > 0 and abs(c["dc_offset"]) > 0.3 * c["rms"]:
        fail.append("ch%d: DC offset %.1f dominates RMS %.1f — enable the ADC high-pass "
                    "filter before capturing" % (c["channel"], c["dc_offset"], c["rms"]))
    if c["clipped_fraction"] > 0.001:
        fail.append("ch%d: %.2f%% of samples clipped — capture gain too high"
                    % (c["channel"], c["clipped_fraction"] * 100))
    if c["rms"] < 1.0:
        fail.append("ch%d: RMS %.1f — mic is dead or muted" % (c["channel"], c["rms"]))
    elif c["rms_dbfs"] < -70:
        warn.append("ch%d: very quiet (%.1f dBFS) — confirm the room saw real use "
                    "during the capture" % (c["channel"], c["rms_dbfs"]))

print()
for w in warn:
    print("    WARN  " + w)
for f in fail:
    print("    FAIL  " + f)
sys.exit(1 if fail else 0)
PY
GATE_RC=$?
set -e

if [[ $GATE_RC -ne 0 ]]; then
    # Keep the file as evidence, but move it out of the glob that the next step
    # (derive_detector_mono.py <dir>/*.wav) uses — otherwise "do not train on
    # this capture" is a sentence in the output, not a property of the corpus.
    mv -f "$DEST_DIR/${BASENAME}.wav" "$DEST_DIR/${BASENAME}.REJECTED.wav.bak"
    [[ -f "$DEST_DIR/${BASENAME}.json" ]] \
        && mv -f "$DEST_DIR/${BASENAME}.json" "$DEST_DIR/${BASENAME}.REJECTED.json"
    echo
    echo "REJECTED — quarantined as ${BASENAME}.REJECTED.wav.bak (not picked up by"
    echo "the corpus glob). Fix the cause, then re-capture."
    exit 1
fi

echo
echo "OK  $DEST_DIR/${BASENAME}.wav"
echo "    Next: derive the detector-side mono, then fold it into the hard-negative set."
echo "    See docs/SATELLITE_ACOUSTIC_COMMISSIONING.md"
