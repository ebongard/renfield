#!/bin/sh
# Prefill (pp512) and decode (tg64) at growing context depth, two images one
# after the other, with the production model and KV settings. llama-bench is only
# in the FULL images (full-cuda*), not in server-cuda. Needs the GPU exclusively:
# stop the unit first. About 1-2 min per image on the 5090.
# Usage: llm-longctx.sh <old-full-image> <new-full-image>
set -u
[ $# -eq 2 ] || { echo "usage: $0 <old-full-image> <new-full-image>"; exit 2; }
if docker ps --format '{{.Names}}' | grep -qx llama-server; then
  echo "llama-server still running - stop the unit first"; exit 1
fi
for IMG in "$1" "$2"; do
  echo "== $IMG $(date +%H:%M:%S)"
  docker run --rm --gpus all --entrypoint /app/llama-bench \
    -v /mnt/llm/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf:/models/m.gguf:ro "$IMG" \
    -m /models/m.gguf -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 \
    -p 512 -n 64 -d 0,8192,32768,65536,131072 -r 2 -o md 2>&1 | grep -E '^\|'
done
echo BENCH_DONE
