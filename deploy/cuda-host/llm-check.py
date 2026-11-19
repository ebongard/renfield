"""Functional + decode-speed check of a llama-server (OpenAI API).
Usage: python3 llm-check.py http://localhost:8081
Checks: plain chat (no <think>, no reasoning_content), a tool call with parsed
arguments, and decode speed over 256 forced tokens (server timings), 3 runs."""
import json
import sys
import urllib.request

BASE = sys.argv[1].rstrip("/")


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=300))


ok = True
r = post("/v1/chat/completions", {"model": "qwen3.6", "messages": [{"role": "user", "content": "Antworte mit genau einem Wort: Was ist die Hauptstadt von Frankreich?"}], "max_tokens": 32})
m = r["choices"][0]["message"]
content = m.get("content") or ""
plain_ok = "paris" in content.lower() and "<think>" not in content and not m.get("reasoning_content")
print(f"plain_chat ok={plain_ok} content_len={len(content)} reasoning_len={len(m.get('reasoning_content') or '')}")
ok &= plain_ok

tools = [{"type": "function", "function": {"name": "get_weather", "description": "Aktuelles Wetter fuer eine Stadt",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
r = post("/v1/chat/completions", {"model": "qwen3.6", "messages": [{"role": "user", "content": "Wie ist das Wetter gerade in Hamburg?"}], "tools": tools, "max_tokens": 256})
calls = r["choices"][0]["message"].get("tool_calls") or []
args = json.loads(calls[0]["function"]["arguments"]) if calls else {}
tool_ok = len(calls) == 1 and calls[0]["function"]["name"] == "get_weather" and "hamburg" in str(args.get("city", "")).lower()
print(f"tool_call ok={tool_ok} n_calls={len(calls)} finish={r['choices'][0]['finish_reason']}")
ok &= tool_ok

speeds = []
for _ in range(3):
    r = post("/completion", {"prompt": "Erzaehle eine lange Geschichte ueber einen Leuchtturm.", "n_predict": 256, "ignore_eos": True, "temperature": 0.6, "cache_prompt": False})
    speeds.append(r["timings"]["predicted_per_second"])
print("decode_tok_s " + " ".join(f"{s:.1f}" for s in speeds) + f" mean={sum(speeds)/len(speeds):.1f}")
print("RESULT", "PASS" if ok else "FAIL")
