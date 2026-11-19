#!/bin/sh
# A/B of two llama-server images with the production arguments, on port 8082,
# while the production unit is stopped (GPU exclusive). Usage: llm-ab.sh <new-image>
# OLD = the image the INSTALLED unit runs, so the baseline is always production.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
OLD=$(grep -oE 'ghcr\.io/ggml-org/llama\.cpp@sha256:[0-9a-f]{64}' /etc/systemd/system/llama-server.service | head -1)
[ -n "$OLD" ] || { echo "no image digest in /etc/systemd/system/llama-server.service"; exit 1; }
NEW=$1
if docker ps --format '{{.Names}}' | grep -qx llama-server; then
  echo "llama-server still running - stop the unit first"; exit 1
fi
for IMG in "$OLD" "$NEW"; do
  echo "== $IMG $(date '+%H:%M:%S')"
  docker run -d --rm --name llama-ab --network host --gpus all \
    -v /mnt/llm/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf:/models/qwen3.6-35b-a3b.gguf:ro \
    -v /mnt/llm/mmproj-F16.gguf:/models/qwen3.6-35b-a3b-mmproj.gguf:ro \
    "$IMG" --model /models/qwen3.6-35b-a3b.gguf --mmproj /models/qwen3.6-35b-a3b-mmproj.gguf \
    --ctx-size 262144 --n-gpu-layers 99 --cache-type-k q8_0 --cache-type-v q8_0 --flash-attn on \
    --reasoning off --temp 0.6 --top-p 0.95 --top-k 20 --jinja --metrics --alias qwen3.6 \
    --host 127.0.0.1 --port 8082 >/dev/null
  i=0; until curl -fsS localhost:8082/health 2>/dev/null | grep -q ok; do
    i=$((i+1)); [ $i -gt 60 ] && { echo "not healthy after 300 s"; docker logs --tail 20 llama-ab; break; }; sleep 5; done
  curl -s localhost:8082/props | python3 -c "import sys,json;d=json.load(sys.stdin);print('build', d.get('build_info'), 'slots', d.get('total_slots'))"
  python3 "$HERE/llm-check.py" http://localhost:8082
  docker stop llama-ab >/dev/null
  sleep 3
done
echo AB_DONE
