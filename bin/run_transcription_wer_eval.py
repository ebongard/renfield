#!/usr/bin/env python3
"""§2 — meeting-transcription WER eval against AMI reference transcripts.

Complements the speaker-separation gate (``run_embedding_separation_eval.py``):
that one asks "is a speaker's voiceprint matchable across meetings?"; THIS one
asks "how accurate is the transcribed TEXT?" — Word Error Rate of the ECAPA/
whisper STT the meeting pipeline uses, measured against AMI's human word-level
manual transcripts (the corpusResources meetings.xml + words/*.xml, extracted
into the manifest's ``words`` field by ``bin/build_ami_manifest.py --ami-*``).

Isolates ASR quality from diarization by using ground-truth speaker SEGMENTS:
per (recording, speaker) it pools that speaker's own reference segments (capped),
STTs them in whisper-sized chunks via voice-server ``POST /api/voice/stt``, and
scores the hypothesis against the reference words that fall inside those same
segments. Standard normalization (lowercase, strip punctuation) on BOTH sides.

CAVEATS (state them, don't hide them): AMI Mix-Headset is a MIXED mic, so
overlapping cross-talk bleeds into a target speaker's segments — WER here is a
realistic-but-pessimistic upper bound vs a per-speaker headset (IHM) eval. AMI
is spontaneous, disfluent, multi-party speech; even strong systems sit ~20-30%
WER on it. Treat the number as a regression baseline, not a PASS/FAIL cliff.

The voice-server defaults to the household language (de) and will hallucinate
German on English audio — ``--language`` (default ``en``) forces it for AMI.

Run IN-CLUSTER (the anon STT service has no auth):
    python bin/run_transcription_wer_eval.py \
        --manifest /app/data/uploads/ami/manifest-wer.json \
        --voice-url http://voice-server-anon.voice:8081 \
        --language en [--max-seconds-per-speaker 90] [--chunk-seconds 25] [--json]

    python bin/run_transcription_wer_eval.py --self-test   # pure-logic, no IO
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

_SR = 16000
_APOS_RE = re.compile(r"['’]", re.UNICODE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


# --- WER math (pure) ----------------------------------------------------------

def normalize(text):
    """lowercase; drop apostrophes so contractions stay one token ("don't"=="dont");
    replace remaining punctuation with space; split. Applied identically to ref
    and hyp so the comparison is fair."""
    text = _APOS_RE.sub("", text.lower())
    return _PUNCT_RE.sub(" ", text).split()


def wer_counts(ref_tokens, hyp_tokens):
    """Levenshtein over word lists → (edits, substitutions, deletions,
    insertions, n_ref). WER = edits / n_ref."""
    n, m = len(ref_tokens), len(hyp_tokens)
    # dp[i][j] = edit distance ref[:i] vs hyp[:j]; backtrack for S/D/I split.
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref_tokens[i - 1] == hyp_tokens[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + cost)
    # backtrack
    i, j, S, D, I = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (
                0 if ref_tokens[i - 1] == hyp_tokens[j - 1] else 1):
            if ref_tokens[i - 1] != hyp_tokens[j - 1]:
                S += 1
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            D += 1
            i -= 1
        else:
            I += 1
            j -= 1
    return {"edits": S + D + I, "sub": S, "del": D, "ins": I, "n_ref": n}


def words_in_segments(words, segments):
    """Reference words whose MIDPOINT falls inside any taken segment, in order."""
    spans = sorted((s["start_s"], s["end_s"]) for s in segments)
    out = []
    for w in sorted(words, key=lambda x: x["start_s"]):
        mid = (w["start_s"] + w["end_s"]) / 2
        if any(a <= mid <= b for a, b in spans):
            out.append(w["word"])
    return out


def chunk_segments(segments, chunk_seconds, max_seconds):
    """Greedily group consecutive segments into <=chunk_seconds chunks, stopping
    once total taken audio reaches max_seconds. Returns (chunks, taken_segments)
    where a chunk is a list of (start,end). A lone segment > chunk_seconds is its
    own (capped) chunk."""
    chunks, taken, cur, cur_dur, total = [], [], [], 0.0, 0.0
    for seg in sorted(segments, key=lambda s: s["start_s"]):
        if total >= max_seconds:
            break
        a, b = seg["start_s"], seg["end_s"]
        dur = b - a
        if cur and cur_dur + dur > chunk_seconds:
            chunks.append(cur)
            cur, cur_dur = [], 0.0
        cur.append((a, b))
        cur_dur += dur
        taken.append({"start_s": a, "end_s": b})
        total += dur
    if cur:
        chunks.append(cur)
    return chunks, taken


# --- audio + voice-server (IO) ------------------------------------------------

def _load_recording(audio_path):
    import numpy as np
    import soundfile as sf
    data, sr = sf.read(audio_path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != _SR:
        import librosa
        data = librosa.resample(data, orig_sr=sr, target_sr=_SR)
    return data


def _chunk_pcm(data, chunk):
    import numpy as np
    parts = [data[max(0, int(a * _SR)):max(0, int(b * _SR))] for a, b in chunk]
    parts = [p for p in parts if p.size]
    return np.concatenate(parts) if parts else None


def _stt_text(pcm, voice_url, language=None):
    import io
    import numpy as np
    import requests
    import soundfile as sf
    buf = io.BytesIO()
    sf.write(buf, pcm.astype(np.float32), _SR, format="WAV", subtype="PCM_16")
    buf.seek(0)
    # The voice-server defaults to the household language (de). AMI is English, so
    # force the language — else whisper mis-detects/hallucinates German on the
    # English audio and WER is meaningless (a language mismatch, not ASR quality).
    resp = requests.post(f"{voice_url.rstrip('/')}/api/voice/stt",
                         files={"audio": ("clip.wav", buf, "audio/wav")},
                         data={"language": language} if language else None, timeout=180)
    resp.raise_for_status()
    return resp.json().get("text", "") or ""


def evaluate(manifest, voice_url, chunk_seconds, max_seconds, log, language=None):
    agg = {"edits": 0, "sub": 0, "del": 0, "ins": 0, "n_ref": 0}
    per_speaker = []
    for rec in manifest:
        words = rec.get("words") or []
        if not words:
            log(f"  ! {rec['recording']}: no reference words — skipped")
            continue
        by_speaker_segs = defaultdict(list)
        for s in rec["segments"]:
            by_speaker_segs[s["speaker"]].append(s)
        by_speaker_words = defaultdict(list)
        for w in words:
            by_speaker_words[w["speaker"]].append(w)
        data = None
        for speaker, segs in sorted(by_speaker_segs.items()):
            ref_words = by_speaker_words.get(speaker)
            if not ref_words:
                continue
            chunks, taken = chunk_segments(segs, chunk_seconds, max_seconds)
            ref_tokens = normalize(" ".join(words_in_segments(ref_words, taken)))
            if not ref_tokens:
                continue
            if data is None:
                data = _load_recording(rec["audio"])
            hyp_parts = []
            for chunk in chunks:
                pcm = _chunk_pcm(data, chunk)
                if pcm is None or pcm.size < _SR // 2:  # <0.5s → skip
                    continue
                try:
                    hyp_parts.append(_stt_text(pcm, voice_url, language))
                except Exception as e:  # one bad chunk must not sink the eval
                    log(f"  ! {rec['recording']}/{speaker}: STT failed: {e}")
            hyp_tokens = normalize(" ".join(hyp_parts))
            c = wer_counts(ref_tokens, hyp_tokens)
            for k in agg:
                agg[k] += c[k]
            wer = c["edits"] / c["n_ref"] if c["n_ref"] else float("nan")
            per_speaker.append({"recording": rec["recording"], "speaker": speaker,
                                "wer": round(wer, 4), "n_ref": c["n_ref"]})
            log(f"  {rec['recording']}/{speaker}: WER {wer:.3f} "
                f"({c['n_ref']} ref words, {len(chunks)} chunks)")
    overall = agg["edits"] / agg["n_ref"] if agg["n_ref"] else float("nan")
    return {
        "overall_wer": round(overall, 4),
        "total_ref_words": agg["n_ref"],
        "substitutions": agg["sub"], "deletions": agg["del"], "insertions": agg["ins"],
        "speakers_scored": len(per_speaker),
        "per_speaker": sorted(per_speaker, key=lambda x: x["wer"]),
    }


def _self_test():
    assert normalize("Don't, okay!") == ["dont", "okay"]
    c = wer_counts(["the", "cat", "sat"], ["the", "cat", "sat"])
    assert c == {"edits": 0, "sub": 0, "del": 0, "ins": 0, "n_ref": 3}, c
    c = wer_counts(["the", "cat", "sat"], ["the", "dog", "sat", "down"])
    assert c["n_ref"] == 3 and c["sub"] == 1 and c["ins"] == 1 and c["edits"] == 2, c
    c = wer_counts(["a", "b", "c", "d"], ["a", "c"])
    assert c["del"] == 2 and c["edits"] == 2 and c["n_ref"] == 4, c
    words = [{"speaker": "X", "word": "hi", "start_s": 1.0, "end_s": 1.2},
             {"speaker": "X", "word": "bye", "start_s": 9.0, "end_s": 9.2}]
    inseg = words_in_segments(words, [{"start_s": 0.5, "end_s": 2.0}])
    assert inseg == ["hi"], inseg  # "bye" at 9.1 is outside the segment
    chunks, taken = chunk_segments(
        [{"start_s": 0, "end_s": 10}, {"start_s": 10, "end_s": 20},
         {"start_s": 20, "end_s": 30}], chunk_seconds=15, max_seconds=25)
    assert len(taken) == 3 and sum(len(c) for c in chunks) == 3, (chunks, taken)
    # 0-10 (10s) | 10-20 would exceed 15 → new chunk; cap 25s stops before 20-30's full add
    print("self-test OK")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", help="manifest built with --ami-words-dir (has `words`)")
    ap.add_argument("--voice-url", default="http://voice-server-anon.voice:8081")
    ap.add_argument("--max-seconds-per-speaker", type=float, default=90.0)
    ap.add_argument("--chunk-seconds", type=float, default=25.0)
    ap.add_argument("--language", default="en",
                    help="force STT language (AMI is English; the voice-server defaults to the household de)")
    ap.add_argument("--max-wer", type=float, default=0.40,
                    help="informational soft threshold (AMI is hard; not a hard gate)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        _self_test()
        return 0
    if not args.manifest:
        ap.error("--manifest is required")

    def log(msg):
        if not args.json:
            print(msg, file=sys.stderr)

    with open(args.manifest) as f:
        manifest = json.load(f)
    log(f"scoring transcription WER across {len(manifest)} recordings "
        f"via {args.voice_url} ...")
    result = evaluate(manifest, args.voice_url, args.chunk_seconds,
                      args.max_seconds_per_speaker, log, args.language)
    result["verdict"] = ("OK" if result["overall_wer"] <= args.max_wer else "HIGH")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("\n=== §2 transcription WER eval ===")
        for k in ("overall_wer", "total_ref_words", "substitutions", "deletions",
                  "insertions", "speakers_scored", "verdict"):
            print(f"  {k}: {result[k]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
