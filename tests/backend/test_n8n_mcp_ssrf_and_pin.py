"""The n8n MCP's SSRF guard against the requirement to keep n8n on the LAN.

🛑 Measured on 2026-10-26; the occasion was an alert from the functional probe
("MCP-Dienst n8n ist eingeschränkt", urgency=critical). The chain:

- n8n was reachable: without a key HTTP 401 in 25 ms, start page 200.
- The access key was valid: with the key HTTP 200.
- And every tool call failed regardless. `n8n-mcp@2.94.3` checks the API BASE
  ADDRESS through its SSRF guard (`dist/services/n8n-api-client.js:200` calls
  `SSRFProtection.validateWebhookUrl(this.baseUrl)` -- so despite the name not
  only on the webhook path) and refuses private addresses.
- But n8n MUST sit on the LAN here: the public address pointed at somebody
  else's hoster, which is why #1235 set it to a LAN IP.

The two are mutually exclusive. The stdio server connects cleanly with 13
tools while this is the case -- nobody can use them. The probe reported it
correctly; that is the second real thing it has caught (the first was on
2026-09-13).

Underneath lies the mechanism that made it silent: the image installed
`n8n-mcp@latest`, so every image build decided anew which code talks to n8n.
"""
import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.backend]

ROOT = Path(__file__).resolve().parents[2]


def _global_install_lines() -> list[tuple[str, str]]:
    """The real `npm install -g` lines of ALL backend image files.

    Returns: `(file name, line)`, so an error message can say WHERE.

    🛑 Two mismeasurements of my own are embedded in this function, both found
    in the review on 2026-10-26:

    1. The first version reached over the whole Dockerfile and picked up a
       COMMENT that merely explains `npm install -g <localdir>` (the bundled
       SearXNG fork). The red test was my measurement, not the Dockerfile.
       Hence only instruction lines now.
    2. The second version read ONLY `Dockerfile` -- and `Dockerfile.gpu`
       carried `n8n-mcp@latest` on unchanged, although it is a live build path
       (`docker-compose.yml`, `docker-compose.prod.yml`,
       `.github/workflows/release.yml`). The test was green while the outage
       stayed reproducible in every GPU image: the named instance fixed, the
       sibling left standing. Hence `glob("Dockerfile*")`.

    Continuation lines are joined, otherwise a `RUN npm install -g` with a line
    break and the package on the next line escapes the check.
    """
    hits: list[tuple[str, str]] = []
    for dockerfile in sorted((ROOT / "src" / "backend").glob("Dockerfile*")):
        if not dockerfile.is_file():
            continue
        # Join continuations into one logical statement.
        text = dockerfile.read_text().replace("\\\n", " ")
        for line in text.splitlines():
            if "npm install -g" in line and not line.lstrip().startswith("#"):
                hits.append((dockerfile.name, " ".join(line.split())))
    return hits


def _server(name: str) -> dict:
    entries = yaml.safe_load(
        (ROOT / "config" / "mcp_servers.yaml").read_text())["servers"]
    return next(e for e in entries if e["name"] == name)


def test_n8n_mcp_is_allowed_to_reach_a_private_address():
    """Without the switch the n8n connection is dead, not merely limited."""
    env = _server("n8n").get("env") or {}
    assert env.get("WEBHOOK_SECURITY_MODE") == "permissive", (
        "`WEBHOOK_SECURITY_MODE: permissive` is missing from the n8n stanza — "
        "n8n-mcp's SSRF guard then rejects every call to the LAN address "
        "while the server still counts as \"connected\". The outage is "
        "silent: only the functional probe sees it.")


def test_the_probe_that_catches_it_still_exists():
    """🛑 The switch and its check belong together.

    The outage was only discovered because n8n has a functional probe. If that
    goes away, the silent mode comes back -- the server was reporting
    "connected", after all.
    """
    probe = _server("n8n").get("health_probe") or {}
    assert probe.get("tool") == "n8n_list_workflows", (
        "the n8n functional probe is gone or calls a different tool — then "
        "a total outage of the connection again goes unnoticed by anyone")


def _npx_server_packages() -> list[tuple[str, str]]:
    """`(server name, package)` for EVERY stdio server that starts via npx.

    🛑 The derivation starts from the CONFIGURATION, not from the Dockerfile.
    The first version read only `npm install -g` lines and thereby certified
    the class on the strength of the one instance that had been fixed:
    `mcp-newsapi` was not in the image at all but ran in production
    (`NEWS_ENABLED: "true"`), and the test was green. Thought through from the
    configuration, a NEWLY added npx server fails by itself instead of slipping
    past a Dockerfile check. Found in the review of #1412.
    """
    entries = yaml.safe_load(
        (ROOT / "config" / "mcp_servers.yaml").read_text())["servers"]
    hits = []
    for e in entries:
        if e.get("command") != "npx":
            continue
        args = [a for a in (e.get("args") or []) if not a.startswith("-")]
        assert args, f"npx server `{e['name']}` names no package in args"
        hits.append((e["name"], args[0]))
    return hits


def _installed_packages() -> dict[str, dict[str, str]]:
    """`{file name: {package name: version}}` from all backend image files."""
    per_file: dict[str, dict[str, str]] = {}
    for dockerfile, line in _global_install_lines():
        for token in re.findall(r"(?<=\s)([@\w][\w@/.-]*@[\w.+-]+)", " " + line):
            name, _, version = token.rpartition("@")
            per_file.setdefault(dockerfile, {})[name] = version
    return per_file


def test_every_npx_mcp_server_is_pinned_in_every_backend_image():
    """🛑 Without a global installation, `npx -y <package>` downloads from the
    network AT POD START and executes the code -- the moment of startup then
    decides which code runs next to the database, the model servers and the two
    MCP services that deliberately hold credentials.

    Measured in the running pod on 2026-10-26: `mcp-newsapi` was NOT in the
    image; instead a cache sat under `$HOME/.npm/_npx/<hash>` with
    `{"dependencies": {"mcp-newsapi": "^1.0.0"}}` -- a caret range, resolved at
    runtime, with `NEWS_ENABLED: "true"`.
    """
    expected = _npx_server_packages()
    assert expected, "no npx server left in the configuration — adjust the test"
    installed = _installed_packages()
    assert installed, "no global npm install in the image files"

    missing = []
    for dockerfile, packages in sorted(installed.items()):
        for server, package in expected:
            if package not in packages:
                missing.append(f"{dockerfile}: `{package}` (server `{server}`) missing")
    assert not missing, (
        "an MCP server started via npx is not pinned in the image — "
        "the pod then downloads it from the network on start:\n  "
        + "\n  ".join(missing))


def test_no_mcp_package_in_the_image_floats_on_latest():
    """🛑 The CLASS, not only n8n-mcp.

    `npx -y <package>` resolves to the global installation in the image
    (checked in the running pod: `node /usr/bin/n8n-mcp`, no npx cache). So
    these lines decide which code runs. With `@latest` the moment of the image
    build decides that, and a change in behaviour comes in without a single
    line of diff -- which is exactly how it happened.
    """
    floating = [f"{d}: {z}" for d, z in _global_install_lines()
             if "@latest" in z]
    assert not floating, (
        "an MCP package hangs on `@latest` — every image build can then pull in "
        "different behaviour without the diff showing anything:\n  "
        + "\n  ".join(floating))


def test_every_globally_installed_mcp_package_names_an_exact_version():
    """And the other direction: a spec like `^2.9` would be just as open.

    🛑 What is checked is the FLOATING, not the SemVer shape. The first version
    demanded exactly `\\d+.\\d+.\\d+` and would thereby have rejected a
    deliberate pin to a prerelease (`2.94.3-rc.1`, to pick up a fix that is not
    published yet, say) for the wrong reason. A prerelease is immutable;
    `latest`, a dist-tag, `^`, `~` and `x` are not. Found in the review of
    #1412.
    """
    lines = _global_install_lines()
    assert lines, "no global npm install left in the image — adjust the test"
    packages = [(d, m) for d, z in lines
              for m in re.findall(r"npm install -g\s+(\S+)", z)]
    for dockerfile, package in packages:
        assert "@" in package, f"{dockerfile}: `{package}` names no version"
        version = package.rsplit("@", 1)[1]
        assert re.match(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", version), (
            f"{dockerfile}: `{package}` floats — `latest`, a dist-tag, `^x.y`, "
            "`~x.y` or `x` let the image build decide which code talks "
            "to the service")
