#!/usr/bin/env python3
"""§2 Track A — build the embedding-separation eval manifest from RTTM ground truth.

The go/no-go eval (``bin/run_embedding_separation_eval.py``) consumes a manifest
JSON of ``{recording, audio, segments:[{speaker,start_s,end_s}]}``. This script
turns the standard NIST **RTTM** ground truth (what the AMI Meeting Corpus and
every diarization benchmark ship — e.g. pyannote's ``AMI-diarization-setup``)
into that manifest, so no hand-labeling is needed for the authoritative gate.

Pure stdlib — runs on the operator workstation (NOT in-cluster). The split is
deliberate: build the manifest here from downloaded RTTMs, stage the manifest +
audio onto the shared PVC, then run the eval Job in-cluster where the ECAPA
voice-server resolves (``k8s/ami-embedding-eval-job.yaml``).

RTTM line (whitespace-delimited, NIST):
    SPEAKER <recording> <chnl> <start> <dur> <NA> <NA> <speaker> <NA> <NA>
    fields: [0]=SPEAKER [1]=recording [3]=start_s [4]=duration_s [7]=speaker

CRITICAL — the eval keys on GLOBAL speaker ids (stable ACROSS recordings): a
person's centroid from recording A must be comparable to their centroid from
recording B. AMI participant codes (``MEE068``/``FEE005``) already are global.
If your RTTMs carry meeting-LOCAL labels (``A``/``B``/``C``/``D``, which collide
across meetings), pass ``--speaker-map`` to remap them to global ids — otherwise
the eval silently treats two different people as "the same speaker" and its
verdict is a lie. This script WARNS when no speaker recurs across recordings
(the tell-tale of un-mapped local labels), so you catch it before burning GPU.

Usage:
    # AMI Mix-Headset audio in ./ami/audio, RTTMs in ./ami/rttm
    python bin/build_ami_manifest.py \
        --rttm ami/rttm \
        --audio-root /app/data/uploads/ami/audio \
        --audio-pattern '{recording}.Mix-Headset.wav' \
        --out ami/manifest.json

    python bin/build_ami_manifest.py --self-test   # pure-logic check, no IO
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys


def parse_rttm(text):
    """Yield (recording, speaker, start_s, end_s) from RTTM ``SPEAKER`` lines.

    Robust to blank lines, comments (``;;``), and non-SPEAKER rows; tolerant of
    extra trailing columns. Raises ValueError on a malformed SPEAKER row so a
    corrupt annotation fails loud rather than silently dropping ground truth.
    """
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(";;"):
            continue
        f = line.split()
        if f[0].upper() != "SPEAKER":
            continue
        if len(f) < 8:
            raise ValueError(f"RTTM line {lineno}: SPEAKER row has <8 fields: {raw!r}")
        recording, speaker = f[1], f[7]
        try:
            start = float(f[3])
            dur = float(f[4])
        except ValueError as e:
            raise ValueError(f"RTTM line {lineno}: bad start/dur: {raw!r}") from e
        if dur <= 0:
            continue  # zero/negative-duration annotation — nothing to embed
        yield recording, speaker, start, start + dur


def _remap(speaker, recording, speaker_map):
    """Apply a local->global remap. Supports per-recording nesting
    ``{recording: {local: global}}`` OR a flat ``{local: global}`` fallback."""
    if not speaker_map:
        return speaker
    per_rec = speaker_map.get(recording)
    if isinstance(per_rec, dict) and speaker in per_rec:
        return per_rec[speaker]
    return speaker_map.get(speaker, speaker) if not isinstance(per_rec, dict) else speaker


def build_manifest(rttm_texts, audio_root, audio_pattern, speaker_map, min_seg_seconds):
    """rttm_texts: {source_name: rttm_text}. Returns (manifest_list, summary)."""
    # recording -> speaker -> list[(start, end)]
    by_rec = {}
    for text in rttm_texts.values():
        for recording, speaker, start, end in parse_rttm(text):
            if end - start < min_seg_seconds:
                continue
            speaker = _remap(speaker, recording, speaker_map)
            by_rec.setdefault(recording, {}).setdefault(speaker, []).append((start, end))

    manifest = []
    for recording in sorted(by_rec):
        segments = [
            {"speaker": spk, "start_s": round(s, 3), "end_s": round(e, 3)}
            for spk, spans in sorted(by_rec[recording].items())
            for (s, e) in sorted(spans)
        ]
        manifest.append({
            "recording": recording,
            "audio": os.path.join(audio_root, audio_pattern.format(recording=recording)),
            "segments": segments,
        })

    # recurrence summary — how many people appear in >=2 recordings (the eval's
    # intra-person cross-recording pairs come only from those).
    speaker_recordings = {}
    for recording, spk_map in by_rec.items():
        for spk in spk_map:
            speaker_recordings.setdefault(spk, set()).add(recording)
    multi = {s: recs for s, recs in speaker_recordings.items() if len(recs) >= 2}
    intra_pairs = sum(len(recs) * (len(recs) - 1) // 2 for recs in multi.values())
    summary = {
        "recordings": len(by_rec),
        "speakers": len(speaker_recordings),
        "speakers_multi_recording": len(multi),
        "potential_intra_cross_recording_pairs": intra_pairs,
        "total_segments": sum(len(r["segments"]) for r in manifest),
    }
    return manifest, summary


def _self_test():
    rttm = (
        ";; comment\n"
        "SPEAKER ES2002a 1 12.400 2.700 <NA> <NA> MEE068 <NA> <NA>\n"
        "SPEAKER ES2002a 1 20.000 5.000 <NA> <NA> FEE005 <NA> <NA>\n"
        "SPEAKER ES2002b 1 3.000 4.000 <NA> <NA> MEE068 <NA> <NA>\n"
        "SPEAKER ES2002b 1 9.000 0.000 <NA> <NA> FEE005 <NA> <NA>\n"  # zero-dur dropped
        "GARBAGE row that is not a speaker line\n"
    )
    manifest, summary = build_manifest(
        {"t": rttm}, audio_root="/data", audio_pattern="{recording}.wav",
        speaker_map=None, min_seg_seconds=0.0)
    assert summary["recordings"] == 2, summary
    assert summary["speakers"] == 2, summary
    assert summary["speakers_multi_recording"] == 1, summary  # MEE068 in a+b
    assert summary["potential_intra_cross_recording_pairs"] == 1, summary
    assert summary["total_segments"] == 3, summary  # zero-dur FEE005/b dropped
    es2002a = next(r for r in manifest if r["recording"] == "ES2002a")
    assert es2002a["audio"] == "/data/ES2002a.wav", es2002a
    seg = next(s for s in es2002a["segments"] if s["speaker"] == "MEE068")
    assert seg == {"speaker": "MEE068", "start_s": 12.4, "end_s": 15.1}, seg
    # remap (local -> global)
    local_rttm = "SPEAKER M1 1 0.0 2.0 <NA> <NA> A <NA> <NA>\n"
    _, s2 = build_manifest(
        {"t": local_rttm + "SPEAKER M2 1 0.0 2.0 <NA> <NA> A <NA> <NA>\n"},
        audio_root="/d", audio_pattern="{recording}.wav",
        speaker_map={"M1": {"A": "PERSON_X"}, "M2": {"A": "PERSON_Y"}},
        min_seg_seconds=0.0)
    assert s2["speakers"] == 2, s2  # remapped apart, no false recurrence
    assert s2["speakers_multi_recording"] == 0, s2
    # malformed row fails loud
    try:
        list(parse_rttm("SPEAKER only three fields\n"))
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError on short SPEAKER row")
    print("self-test OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rttm", nargs="*", default=[],
                    help="RTTM file(s) and/or directories (dirs globbed for *.rttm)")
    ap.add_argument("--audio-root", default="/app/data/uploads/ami/audio",
                    help="dir the manifest's audio paths are rooted at (as seen IN the eval Job)")
    ap.add_argument("--audio-pattern", default="{recording}.Mix-Headset.wav",
                    help="filename template; {recording} = the RTTM file-id")
    ap.add_argument("--speaker-map", help="JSON local->global remap (see docstring)")
    ap.add_argument("--min-seg-seconds", type=float, default=0.0,
                    help="drop ground-truth segments shorter than this")
    ap.add_argument("--out", help="write manifest JSON here (else stdout)")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        _self_test()
        return 0

    if not args.rttm:
        ap.error("--rttm is required (one or more RTTM files or directories)")

    paths = []
    for p in args.rttm:
        if os.path.isdir(p):
            paths.extend(sorted(glob.glob(os.path.join(p, "*.rttm"))))
        else:
            paths.append(p)
    if not paths:
        ap.error("no RTTM files found under the given --rttm paths")

    rttm_texts = {}
    for p in paths:
        with open(p) as f:
            rttm_texts[p] = f.read()

    speaker_map = None
    if args.speaker_map:
        with open(args.speaker_map) as f:
            speaker_map = json.load(f)

    manifest, summary = build_manifest(
        rttm_texts, args.audio_root, args.audio_pattern, speaker_map, args.min_seg_seconds)

    print("=== AMI manifest summary ===", file=sys.stderr)
    for k, v in summary.items():
        print(f"  {k}: {v}", file=sys.stderr)
    if summary["speakers_multi_recording"] == 0:
        print(
            "\n  WARNING: NO speaker appears in >=2 recordings — the eval will be "
            "INSUFFICIENT.\n  If your RTTMs use meeting-LOCAL speaker labels "
            "(A/B/C/D), pass --speaker-map to\n  remap them to global participant "
            "ids (see docstring).", file=sys.stderr)
    elif summary["potential_intra_cross_recording_pairs"] < 20:
        print(
            f"\n  NOTE: only {summary['potential_intra_cross_recording_pairs']} "
            "cross-recording same-speaker pairs — the eval default --min-pairs is "
            "20.\n  Add more recordings sharing participants, or lower --min-pairs "
            "(weaker verdict).", file=sys.stderr)

    out = json.dumps(manifest, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(out + "\n")
        print(f"\n  wrote {len(manifest)} recordings -> {args.out}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
