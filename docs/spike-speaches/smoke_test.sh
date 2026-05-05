#!/usr/bin/env bash
# Speaches smoke-test runner. Executes inside backend pod (curl + ffmpeg available).
#
# Usage from local shell:
#   kubectl --context=renfield-private -n renfield cp docs/spike-speaches/smoke_test.sh backend-POD:/tmp/smoke.sh
#   kubectl --context=renfield-private -n renfield exec backend-POD -- bash /tmp/smoke.sh

set -euo pipefail

BASE="http://speaches.renfield.svc.cluster.local:8000"
OUT=/tmp/speaches-smoke
mkdir -p "$OUT"

echo "=== /health ==="
curl -fsS "$BASE/health" && echo

echo
echo "=== /v1/models (catalog) ==="
curl -fsS "$BASE/v1/models" | python3 -m json.tool | head -30

echo
echo "=== STT roundtrip ==="
# Generate a 3s German test sentence via espeak-ng if available, else use a known wav
if command -v espeak-ng >/dev/null 2>&1; then
  espeak-ng -v de "Heute ist ein schöner Tag, wir testen die Spracherkennung." -w "$OUT/in.wav" 2>/dev/null
else
  # Fallback: 3s sine tone (not useful for STT accuracy but validates pipe)
  python3 -c "import wave,struct,math; w=wave.open('$OUT/in.wav','wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); [w.writeframes(struct.pack('<h',int(16000*math.sin(2*math.pi*440*i/16000)))) for i in range(48000)]; w.close()"
fi

ls -la "$OUT/in.wav"

# Trigger model preload via warm request
T0=$(date +%s.%N)
RESP=$(curl -fsS "$BASE/v1/audio/transcriptions" \
  -F "file=@$OUT/in.wav" \
  -F "model=Systran/faster-whisper-small" \
  -F "language=de" \
  -F "response_format=json" || echo '{"error":"failed"}')
T1=$(date +%s.%N)
echo "STT response: $RESP"
echo "STT latency: $(echo "$T1 - $T0" | bc)s"

echo
echo "=== TTS roundtrip ==="
T0=$(date +%s.%N)
curl -fsS "$BASE/v1/audio/speech" \
  -H "Content-Type: application/json" \
  -d '{"input":"Guten Tag, dies ist ein Test der Sprachausgabe.","model":"speaches-ai/Kokoro-82M-v1.0-ONNX","voice":"af_heart","response_format":"wav"}' \
  --output "$OUT/out.wav" || echo "TTS failed"
T1=$(date +%s.%N)
ls -la "$OUT/out.wav"
echo "TTS latency: $(echo "$T1 - $T0" | bc)s"

echo
echo "=== DONE — smoke test artifacts in $OUT ==="
