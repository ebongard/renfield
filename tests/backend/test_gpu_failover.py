"""``bin/gpu-failover.sh`` -- the cold failover of the production GPU tier to Host D (#1636).

Every case runs the REAL script against a ``kubectl`` stub that keeps the live
ConfigMaps of both instances in a JSON file, and checks what the script did to
them. What has to hold:
- to-hostd moves exactly the keys of the part, to Host D, and restarts backend + workers;
- an unhealthy Host D endpoint changes NOTHING (the probe gate bites);
- a key that is empty in git (voice on xidra) is never switched on;
- to-prod puts the values from GIT back, so live == git afterwards, and the mark goes;
- to-prod refuses while the production endpoint is down, unless --force;
- the parser reads the real k8s/configmap.yaml (a wrong regex would restore garbage).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "gpu-failover.sh"
HOSTD = "10.9.9.9"

_KUBECTL_STUB = r"""#!/usr/bin/env python3
import json, os, sys
state_path, log_path = os.environ["STUB_STATE"], os.environ["STUB_LOG"]
state = json.load(open(state_path))
a = sys.argv[1:]
open(log_path, "a").write(" ".join(a) + "\n")
if a[:2] == ["--context", a[1]]:
    a = a[2:]
ns = a[a.index("-n") + 1] if "-n" in a else None
cm = state.setdefault(ns, {"data": {}, "annotations": {}})
def save(): json.dump(state, open(state_path, "w"))
if "exec" in a:
    sys.stdin.read()
    urls = a[a.index("-") + 1:]
    bad = os.environ.get("STUB_PROBE_BAD", "")
    rc = 0
    for u in urls:
        ok = not (bad and bad in u)
        print(("OK   " if ok else "FAIL ") + u)
        rc |= 0 if ok else 1
    sys.exit(rc)
if "get" in a and "configmap" in a:
    jp = [x for x in a if x.startswith("jsonpath=")][0][len("jsonpath="):]
    if jp.startswith("{.data."):
        print(cm["data"].get(jp[len("{.data."):-1], ""), end="")
    else:
        print(next(iter(cm["annotations"].values()), ""), end="")
    sys.exit(0)
if "get" in a and "deploy" in a:
    sys.exit(0 if a[a.index("deploy") + 1] in os.environ.get("STUB_DEPLOYS", "").split() else 1)
if "patch" in a:
    cm["data"].update(json.loads(a[a.index("-p") + 1])["data"]); save(); sys.exit(0)
if "annotate" in a:
    kv = a[-1]
    if kv.endswith("-"):
        cm["annotations"].pop(kv[:-1], None)
    else:
        k, v = kv.split("=", 1); cm["annotations"][k] = v
    save(); sys.exit(0)
sys.exit(0)  # rollout restart / status
"""

_PROD = {
    "LLM_OPENAI_BASE_URL": "http://cuda.local:8081/v1",
    "OLLAMA_VISION_URL": "http://cuda.local:8081/v1",
    "OLLAMA_URL": "http://ollama:11434",
    "OLLAMA_EMBED_URL": "http://ollama:11434",
    "AGENT_ROUTER_URL": "http://ollama:11434",
    "LLM_OPENAI_EMBED_BASE_URL": "http://ollama:11434/v1",
    "VOICE_SERVER_URL": "http://voice-server-anon.voice:8081",
}


def _configmap_yaml(values: dict[str, str]) -> str:
    lines = ["apiVersion: v1", "kind: ConfigMap", "metadata:", "  name: renfield-env", "data:"]
    lines += [f'  {k}: "{v}"  # comment' for k, v in values.items()]
    return "\n".join(lines) + "\n"


@pytest.fixture
def env(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    (repo / "k8s").mkdir()
    shutil.copy(_SCRIPT, repo / "bin" / "gpu-failover.sh")
    (repo / "k8s" / "configmap.yaml").write_text(_configmap_yaml(_PROD))
    xren = tmp_path / "x-ren"
    (xren / "k8s").mkdir(parents=True)
    xidra = dict(_PROD, OLLAMA_URL="http://ollama.renfield.svc.cluster.local:11434", VOICE_SERVER_URL="")
    (xren / "k8s" / "configmap.yaml").write_text(_configmap_yaml(xidra))
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "kubectl").write_text(_KUBECTL_STUB)
    (stubs / "kubectl").chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "renfield": {"data": dict(_PROD), "annotations": {}},
        "renfield-xidra": {"data": dict(xidra), "annotations": {}},
    }))
    log = tmp_path / "kubectl.log"
    log.write_text("")
    e = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", STUB_STATE=str(state), STUB_LOG=str(log),
             STUB_DEPLOYS="backend document-worker", HOSTD_GPU_HOST=HOSTD, X_REN_DIR=str(xren))

    class Env:
        def run(self, *args, **extra):
            return subprocess.run(["bash", str(repo / "bin" / "gpu-failover.sh"), *args],
                                  env=dict(e, **extra), capture_output=True, text=True, timeout=60)

        def live(self, ns):
            return json.loads(state.read_text())[ns]

        def calls(self):
            return log.read_text()

    return Env()


def test_to_hostd_moves_exactly_the_llm_keys_and_restarts(env):
    r = env.run("to-hostd", "llm", "--instance", "household")
    assert r.returncode == 0, r.stdout + r.stderr
    data = env.live("renfield")["data"]
    assert data["LLM_OPENAI_BASE_URL"] == f"http://{HOSTD}:8081/v1"
    assert data["OLLAMA_VISION_URL"] == f"http://{HOSTD}:8081/v1"
    assert data["OLLAMA_URL"] == _PROD["OLLAMA_URL"]            # other parts untouched
    assert data["VOICE_SERVER_URL"] == _PROD["VOICE_SERVER_URL"]
    assert env.live("renfield-xidra")["data"]["LLM_OPENAI_BASE_URL"] == _PROD["LLM_OPENAI_BASE_URL"]
    assert "LLM_OPENAI_BASE_URL" in next(iter(env.live("renfield")["annotations"].values()))
    calls = env.calls()
    assert "rollout restart deploy/backend deploy/document-worker" in calls
    assert "pdf-split-worker" not in calls.split("rollout restart", 1)[1].split("\n")[0]   # absent deploys skipped


def test_unhealthy_hostd_changes_nothing(env):
    r = env.run("to-hostd", "all", STUB_PROBE_BAD=f"{HOSTD}:11434")
    assert r.returncode == 1
    assert env.live("renfield")["data"] == _PROD
    assert "patch" not in env.calls() and "rollout" not in env.calls()


def test_voice_stays_off_where_git_has_it_off(env):
    r = env.run("to-hostd", "voice", "--instance", "both")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.live("renfield")["data"]["VOICE_SERVER_URL"] == f"http://{HOSTD}:8091"
    assert env.live("renfield-xidra")["data"]["VOICE_SERVER_URL"] == ""
    assert "-n renfield-xidra patch" not in env.calls()


def test_to_prod_restores_git_and_clears_the_mark(env):
    assert env.run("to-hostd", "all", "--instance", "household").returncode == 0
    r = env.run("to-prod", "all", "--instance", "household")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.live("renfield")["data"] == _PROD
    assert env.live("renfield")["annotations"] == {}


def test_partial_restore_keeps_the_mark_naming_what_is_left(env):
    assert env.run("to-hostd", "all", "--instance", "household").returncode == 0
    assert env.run("to-prod", "llm", "--instance", "household").returncode == 0
    mark = next(iter(env.live("renfield")["annotations"].values()))
    assert "OLLAMA_URL" in mark and "LLM_OPENAI_BASE_URL" not in mark


def test_to_prod_refuses_while_production_is_down_unless_forced(env):
    assert env.run("to-hostd", "llm", "--instance", "household").returncode == 0
    r = env.run("to-prod", "llm", "--instance", "household", STUB_PROBE_BAD="cuda.local")
    assert r.returncode == 1
    assert env.live("renfield")["data"]["LLM_OPENAI_BASE_URL"] == f"http://{HOSTD}:8081/v1"
    r = env.run("to-prod", "llm", "--instance", "household", "--force", STUB_PROBE_BAD="cuda.local")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.live("renfield")["data"]["LLM_OPENAI_BASE_URL"] == _PROD["LLM_OPENAI_BASE_URL"]


def test_dry_run_changes_nothing(env):
    r = env.run("to-hostd", "all", "--dry-run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert env.live("renfield")["data"] == _PROD
    assert "patch" not in env.calls() and "rollout restart" not in env.calls()


def test_parser_reads_the_real_household_configmap():
    """to-prod restores what this parser reads: it must read the committed file right."""
    out = subprocess.run(
        ["bash", "-c", f'eval "$(sed -n "/^git_value()/,/^}}/p" {_SCRIPT})"; '  # not source <(…): bash 3.2
                       f'for k in LLM_OPENAI_BASE_URL OLLAMA_URL VOICE_SERVER_URL; do '
                       f'printf "%s=%s\\n" "$k" "$(git_value {_REPO}/k8s/configmap.yaml $k)"; done'],
        capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    got = dict(line.split("=", 1) for line in out.stdout.splitlines())
    assert got["LLM_OPENAI_BASE_URL"] == "http://cuda.local:8081/v1"
    assert got["OLLAMA_URL"] == "http://ollama:11434"
    assert got["VOICE_SERVER_URL"] == "http://voice-server-anon.voice:8081"
