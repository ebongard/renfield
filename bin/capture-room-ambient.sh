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
#   2. records RAW multi-channel audio — the exact stream the detector reads,
#      yielding the mic first where the mic is exclusive ("Who holds the mic")
#   3. runs a sanity gate on the capture (DC offset / clipping / dead mic)
#   4. stores it under data/wakeword-ambient/<satellite>/ with a JSON sidecar
#
# Raw multi-channel is deliberate. The detector-side mono (beamform / channel
# select / downmix) is DERIVED later by
# src/satellite/wakeword-training/scripts/derive_detector_mono.py. Recording a
# pre-downmixed mono throws away the ability to reproduce what the detector
# actually hears, and on a beamforming satellite that mono is the wrong signal.
#
# Who holds the mic — this depends on hat_type, read from the inventory:
#   * HAT mics (2mic, 2mic-v2, 4mic, whisplay): their asoundrc defines a dsnoop
#     capture PCM, so arecord and the running satellite service share the mic.
#     The capture is concurrent and the service keeps running.
#   * XVF3800 over USB: asoundrc-xvf3800-usb.j2 deliberately omits dmix/dsnoop
#     (those allocate SysV IPC objects that deadlock PortAudio once a root
#     process has created them), so capture is EXCLUSIVE: arecord and the
#     service both want hw:Array and the second opener loses the device. The
#     capture therefore stops renfield-satellite for its duration and starts it
#     again right after — never silently: it needs --allow-service-stop or an
#     interactive confirmation, and the restart is guaranteed by a trap
#     (EXIT/INT/TERM), because a satellite left deaf by an aborted capture
#     would be worse than a failed capture.
#
# Usage:
#   bin/capture-room-ambient.sh satellite-kinderbad
#   bin/capture-room-ambient.sh satellite-kinderbad --minutes 45 --label evening-use
#   bin/capture-room-ambient.sh satellite-fitnessraum --minutes 45 --allow-service-stop
#
# Flags:
#   --minutes N           capture length in minutes (default 15)
#   --label NAME          label for the filename + sidecar (default "ambient")
#   --out DIR             output root (default data/wakeword-ambient)
#   --allow-service-stop  authorise stopping renfield-satellite for the capture.
#                         Needed on an exclusive (XVF3800/USB) mic; without it
#                         the script asks when it has a terminal and refuses
#                         otherwise. The room is DEAF while the service is down.
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
ALLOW_SERVICE_STOP=0
# provision.yml installs this unit, and runs these hosts with `become: true`
# non-interactively — passwordless sudo is a property of a provisioned
# satellite, so stopping/starting it needs no --ask-become-pass here.
SERVICE_UNIT="renfield-satellite"

usage() {
    # Print the whole header comment, whatever its length — a hardcoded line
    # range silently truncates the moment the header grows.
    sed -n '3,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^#\{1,\} \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --minutes) MINUTES="$2"; shift 2 ;;
        --label)   LABEL="$2"; shift 2 ;;
        --out)     OUT_ROOT="$2"; shift 2 ;;
        --allow-service-stop) ALLOW_SERVICE_STOP=1; shift ;;
        -h|--help) usage 0 ;;
        -*)        echo "unknown flag: $1" >&2; usage 1 ;;
        *)         HOST="$1"; shift ;;
    esac
done

[[ -n "$HOST" ]] || { echo "error: no satellite host given" >&2; usage 1; }
[[ -f "$INVENTORY" ]] || { echo "error: inventory not found: $INVENTORY" >&2; exit 1; }
[[ -f "$INSPECT" ]] || { echo "error: helper not found: $INSPECT" >&2; exit 1; }
# HOST and LABEL both land in filenames and remote paths — keep them safe
# fragments (they reach the inventory helper as argv, never inlined into it).
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

# systemctl needs root; everything else runs as the login user.
ansible_root_sh() {
    ansible "$HOST" -i "$INVENTORY" -b -m shell -a "$1" 2>&1
}

# --- Who holds the mic: read hat_type, never guess it -----------------------
# One inventory lookup serves both the capture mode and the scp target.
HOST_FACTS="$(ansible-inventory -i "$INVENTORY" --host "$HOST" 2>/dev/null)" || HOST_FACTS=""
[[ -n "$HOST_FACTS" ]] || {
    echo "error: $HOST has no entry in $INVENTORY" >&2
    echo "       Note: satellite-esszimmer runs as a k8s POD (k8s/satellite-esszimmer.yaml)," >&2
    echo "       not as an Ansible host — this script cannot serve it. Capture there with" >&2
    echo "       arecord inside the pod." >&2
    exit 1
}
FACTS_PARSED="$(python3 -c '
import json, sys
h = json.load(sys.stdin)
addr = h.get("ansible_host") or sys.argv[1]
user = h.get("ansible_user")
print(h.get("hat_type") or "-", f"{user}@{addr}" if user else addr)
' "$HOST" <<<"$HOST_FACTS")" || { echo "error: could not read $HOST from the inventory" >&2; exit 1; }
read -r HAT_TYPE SSH_TARGET <<<"$FACTS_PARSED"
[[ -n "$SSH_TARGET" ]] || { echo "error: could not resolve an ssh target for $HOST" >&2; exit 1; }
[[ "$HAT_TYPE" != "-" ]] || {
    echo "error: no hat_type for $HOST in $INVENTORY — without it the script cannot tell" >&2
    echo "       whether the mic is shared (dsnoop) or exclusive. Set hat_type on the host." >&2
    exit 1
}

# Which asoundrc profile shares the mic: the HAT templates all define a dsnoop
# capture PCM, asoundrc-xvf3800-usb.j2 deliberately defines none.
case "$HAT_TYPE" in
    2mic|2mic-v2|4mic|whisplay) CAPTURE_MODE="shared" ;;
    *usb*)                      CAPTURE_MODE="exclusive" ;;
    *)                          CAPTURE_MODE="unknown" ;;
esac

if [[ "$CAPTURE_MODE" == "unknown" ]]; then
    if (( ALLOW_SERVICE_STOP )); then
        # Stopping the service is always sufficient for the capture, and the
        # operator has authorised it — so an unrecognised profile takes the
        # exclusive path instead of assuming a dsnoop PCM that may not exist.
        echo "note: unknown hat_type '$HAT_TYPE' — treating the mic as exclusive"
        CAPTURE_MODE="exclusive"
    else
        echo "error: unknown hat_type '$HAT_TYPE' for $HOST — cannot tell whether its ALSA" >&2
        echo "       profile shares the mic via dsnoop. Add it to the case table in" >&2
        echo "       bin/capture-room-ambient.sh, or re-run with --allow-service-stop to" >&2
        echo "       capture with $SERVICE_UNIT stopped." >&2
        exit 1
    fi
fi

SERVICE_STOPPED=0

start_service() {
    local state
    ansible_root_sh "systemctl start $SERVICE_UNIT" >/dev/null || return 1
    # ONE short query, never a journalctl follow: tailing a Pi Zero's log over
    # SSH knocked the device off the net twice (.claude/rules/satellites.md).
    state="$(ansible_root_sh "systemctl is-active $SERVICE_UNIT")" || true
    grep -qE '^[[:space:]]*active[[:space:]]*$' <<<"$state"
}

resume_service_if_stopped() {
    (( SERVICE_STOPPED )) || return 0
    SERVICE_STOPPED=0
    echo "==> Starting $SERVICE_UNIT on $HOST again"
    if start_service; then
        echo "    $SERVICE_UNIT is active"
        return 0
    fi
    echo "!!! CRITICAL: $SERVICE_UNIT is not confirmed active on $HOST — the room is DEAF." >&2
    echo "    Start it by hand:" >&2
    echo "    ansible $HOST -i $INVENTORY -b -m shell -a 'systemctl start $SERVICE_UNIT'" >&2
    return 1
}

# A satellite left mute by an aborted capture is worse than a failed capture, so
# the restart hangs off EXIT (which covers `set -e` aborts and every `exit`
# below) and the signal handlers route into it by exiting.
on_exit() {
    local rc=$?
    trap - EXIT
    resume_service_if_stopped || { (( rc )) || rc=1; }
    exit "$rc"
}
on_signal() {
    echo >&2
    echo "interrupted — putting $HOST back" >&2
    exit 130
}
trap on_exit EXIT
trap on_signal INT TERM

stop_service() {
    echo "==> Stopping $SERVICE_UNIT on $HOST — the room is DEAF until the capture ends"
    # Marked BEFORE the call: a stop that fails halfway must still be followed
    # by a start attempt, and starting a running service is a no-op.
    SERVICE_STOPPED=1
    ansible_root_sh "systemctl stop $SERVICE_UNIT" >/dev/null \
        || { echo "error: could not stop $SERVICE_UNIT on $HOST" >&2; exit 1; }
}

confirm_service_stop() {
    (( ALLOW_SERVICE_STOP )) && return 0
    if [[ ! -t 0 ]]; then
        echo "error: hat_type=$HAT_TYPE on $HOST captures EXCLUSIVELY (no dsnoop PCM), so the" >&2
        echo "       capture needs $SERVICE_UNIT stopped for ~${MINUTES} min. That is an" >&2
        echo "       intervention on a Pi Zero and the room is deaf meanwhile, so it is never" >&2
        echo "       done silently: re-run with --allow-service-stop to authorise it." >&2
        exit 1
    fi
    echo
    echo "$HOST (hat_type=$HAT_TYPE) captures EXCLUSIVELY: arecord and $SERVICE_UNIT"
    echo "cannot share the mic. The service must be stopped for ~${MINUTES} min — the room"
    echo "hears nothing until the capture ends, then the service is started again."
    read -r -p "Stop $SERVICE_UNIT on $HOST now? [yes/N] " answer
    [[ "$answer" == "yes" ]] || { echo "aborted — nothing was stopped" >&2; exit 1; }
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
echo "    hat_type=$HAT_TYPE mic=$CAPTURE_MODE"
echo "    gains: $(python3 -c '
import json, sys
for k, v in json.load(sys.stdin).get("mixer", {}).items():
    print("           %-22s %s" % (k, v))
' <<<"$PROBE" | sed '1s/^ *//')"
if [[ "$CAPTURE_MODE" == "exclusive" ]] \
   && [[ "$(python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("mixer") or {}))' <<<"$PROBE")" == "0" ]]; then
    # The XVF3800 holds its capture gain in chip flash, not in ALSA, so the
    # sidecar's gain provenance is empty here — and a negative captured at a
    # different gain than the deployed one is worthless (commissioning doc, §2).
    echo "    WARN  no ALSA capture controls on $HOST — an XVF3800 keeps its gain"
    echo "          (PP_AGCDESIREDLEVEL) in chip flash, so the sidecar records none."
    echo "          Note the value from host_vars/$HOST.yml (xvf3800_tuning) with this"
    echo "          capture; a gain change after the fact invalidates it."
fi

if [[ "$CAPTURE_MODE" == "exclusive" ]]; then
    confirm_service_stop
    stop_service
    echo "==> Capturing ${MINUTES} min of raw ambient — $SERVICE_UNIT is STOPPED (hat_type=$HAT_TYPE has no dsnoop PCM)"
else
    echo "==> Capturing ${MINUTES} min of raw ambient off the shared dsnoop PCM — the service keeps running"
fi

CAPTURE_RC=0
CAPTURE_OUT="$(ansible_sh "arecord -D '$DEVICE' -f S16_LE -r $RATE -c $CHANNELS -d $(( MINUTES * 60 )) $REMOTE_DIR/${BASENAME}.wav")" \
    || CAPTURE_RC=$?

# Hand the mic back the moment the capture is done — the transfer below takes
# minutes on a multi-hundred-MB file and the room must not stay deaf for it.
resume_service_if_stopped || exit 1

if (( CAPTURE_RC != 0 )); then
    if grep -qiE 'resource busy|device busy|EBUSY' <<<"$CAPTURE_OUT"; then
        echo "error: the capture device '$DEVICE' on $HOST is BUSY — another process holds it" >&2
        echo "       exclusively, so arecord never got the mic." >&2
        if [[ "$CAPTURE_MODE" == "exclusive" ]]; then
            echo "       $SERVICE_UNIT was stopped for this capture, so the holder is something" >&2
            echo "       else (a leftover arecord, a second capture run, an open session). Find it:" >&2
            echo "       ansible $HOST -i $INVENTORY -b -m shell -a 'fuser -v /dev/snd/*'" >&2
        else
            echo "       hat_type=$HAT_TYPE was taken to share the mic via dsnoop, but this device" >&2
            echo "       does not. Re-run with --allow-service-stop to capture with $SERVICE_UNIT" >&2
            echo "       stopped, and correct the case table in bin/capture-room-ambient.sh." >&2
        fi
    else
        echo "error: capture failed on $HOST (arecord exited $CAPTURE_RC)" >&2
        tail -3 <<<"$CAPTURE_OUT" | sed 's/^/       arecord: /' >&2
    fi
    exit 1
fi

echo "==> Sanity gate"
GATE_JSON="$(run_inspect "gate $REMOTE_DIR/${BASENAME}.wav" | extract_json)"
[[ -n "$GATE_JSON" ]] || { echo "error: sanity gate produced no report" >&2; exit 1; }

mkdir -p "$DEST_DIR"
echo "==> Transferring"
# NOT `ansible -m fetch`: it base64-encodes the payload over the control
# connection and stalls on a multi-hundred-MB capture (a 45-min stereo capture
# is ~173 MB). scp streams it. SSH_TARGET came from the inventory lookup above,
# so this keeps working when the host is not in ~/.ssh/config.
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
python3 - "$GATE_JSON" "$PROBE" "$DEST_DIR/${BASENAME}.json" "$HOST" "$LABEL" "$STAMP" \
         "$HAT_TYPE" "$CAPTURE_MODE" <<'PY'
import json, sys

gate, probe = json.loads(sys.argv[1]), json.loads(sys.argv[2])
sidecar = dict(gate)
sidecar.update({
    "satellite": sys.argv[4], "label": sys.argv[5], "captured_at": sys.argv[6],
    # Which mic regime produced this capture: "shared" = taken off the dsnoop
    # PCM with the service running, "exclusive" = service stopped for it.
    "hat_type": sys.argv[7], "mic_capture_mode": sys.argv[8],
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
