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

WURZEL = Path(__file__).resolve().parents[2]


def _globale_installationszeilen() -> list[tuple[str, str]]:
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
    treffer: list[tuple[str, str]] = []
    for datei in sorted((WURZEL / "src" / "backend").glob("Dockerfile*")):
        if not datei.is_file():
            continue
        # Join continuations into one logical statement.
        text = datei.read_text().replace("\\\n", " ")
        for zeile in text.splitlines():
            if "npm install -g" in zeile and not zeile.lstrip().startswith("#"):
                treffer.append((datei.name, " ".join(zeile.split())))
    return treffer


def _server(name: str) -> dict:
    eintraege = yaml.safe_load(
        (WURZEL / "config" / "mcp_servers.yaml").read_text())["servers"]
    return next(e for e in eintraege if e["name"] == name)


def test_n8n_mcp_is_allowed_to_reach_a_private_address():
    """Without the switch the n8n connection is dead, not merely limited."""
    env = _server("n8n").get("env") or {}
    assert env.get("WEBHOOK_SECURITY_MODE") == "permissive", (
        "`WEBHOOK_SECURITY_MODE: permissive` fehlt in der n8n-Stanza — der "
        "SSRF-Riegel von n8n-mcp weist dann jeden Aufruf an die LAN-Adresse "
        "ab, waehrend der Server weiter als „verbunden\" gilt. Der Ausfall "
        "ist lautlos: nur die Funktionssonde sieht ihn.")


def test_the_probe_that_catches_it_still_exists():
    """🛑 The switch and its check belong together.

    The outage was only discovered because n8n has a functional probe. If that
    goes away, the silent mode comes back -- the server was reporting
    "connected", after all.
    """
    sonde = _server("n8n").get("health_probe") or {}
    assert sonde.get("tool") == "n8n_list_workflows", (
        "die n8n-Funktionssonde ist weg oder ruft ein anderes Werkzeug — dann "
        "faellt ein Totalausfall der Anbindung wieder niemandem auf")


def _npx_server_pakete() -> list[tuple[str, str]]:
    """`(server name, package)` for EVERY stdio server that starts via npx.

    🛑 The derivation starts from the CONFIGURATION, not from the Dockerfile.
    The first version read only `npm install -g` lines and thereby certified
    the class on the strength of the one instance that had been fixed:
    `mcp-newsapi` was not in the image at all but ran in production
    (`NEWS_ENABLED: "true"`), and the test was green. Thought through from the
    configuration, a NEWLY added npx server fails by itself instead of slipping
    past a Dockerfile check. Found in the review of #1412.
    """
    eintraege = yaml.safe_load(
        (WURZEL / "config" / "mcp_servers.yaml").read_text())["servers"]
    treffer = []
    for e in eintraege:
        if e.get("command") != "npx":
            continue
        args = [a for a in (e.get("args") or []) if not a.startswith("-")]
        assert args, f"npx-Server `{e['name']}` nennt kein Paket in args"
        treffer.append((e["name"], args[0]))
    return treffer


def _installierte_pakete() -> dict[str, dict[str, str]]:
    """`{file name: {package name: version}}` from all backend image files."""
    je_datei: dict[str, dict[str, str]] = {}
    for datei, zeile in _globale_installationszeilen():
        for token in re.findall(r"(?<=\s)([@\w][\w@/.-]*@[\w.+-]+)", " " + zeile):
            name, _, version = token.rpartition("@")
            je_datei.setdefault(datei, {})[name] = version
    return je_datei


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
    erwartet = _npx_server_pakete()
    assert erwartet, "kein npx-Server mehr in der Konfiguration — Test anpassen"
    installiert = _installierte_pakete()
    assert installiert, "keine globale npm-Installation in den Bilddateien"

    fehlend = []
    for datei, pakete in sorted(installiert.items()):
        for server, paket in erwartet:
            if paket not in pakete:
                fehlend.append(f"{datei}: `{paket}` (Server `{server}`) fehlt")
    assert not fehlend, (
        "ein per npx gestarteter MCP-Server ist nicht im Bild festgenagelt — "
        "dann laedt der Pod ihn beim Start aus dem Netz:\n  "
        + "\n  ".join(fehlend))


def test_no_mcp_package_in_the_image_floats_on_latest():
    """🛑 The CLASS, not only n8n-mcp.

    `npx -y <package>` resolves to the global installation in the image
    (checked in the running pod: `node /usr/bin/n8n-mcp`, no npx cache). So
    these lines decide which code runs. With `@latest` the moment of the image
    build decides that, and a change in behaviour comes in without a single
    line of diff -- which is exactly how it happened.
    """
    offen = [f"{d}: {z}" for d, z in _globale_installationszeilen()
             if "@latest" in z]
    assert not offen, (
        "ein MCP-Paket haengt an `@latest` — jeder Bildbau kann dann ein "
        "anderes Verhalten einziehen, ohne dass der Diff etwas zeigt:\n  "
        + "\n  ".join(offen))


def test_every_globally_installed_mcp_package_names_an_exact_version():
    """And the other direction: a spec like `^2.9` would be just as open.

    🛑 What is checked is the FLOATING, not the SemVer shape. The first version
    demanded exactly `\\d+.\\d+.\\d+` and would thereby have rejected a
    deliberate pin to a prerelease (`2.94.3-rc.1`, to pick up a fix that is not
    published yet, say) for the wrong reason. A prerelease is immutable;
    `latest`, a dist-tag, `^`, `~` and `x` are not. Found in the review of
    #1412.
    """
    zeilen = _globale_installationszeilen()
    assert zeilen, "keine globale npm-Installation mehr im Bild — Test anpassen"
    pakete = [(d, m) for d, z in zeilen
              for m in re.findall(r"npm install -g\s+(\S+)", z)]
    for datei, paket in pakete:
        assert "@" in paket, f"{datei}: `{paket}` nennt keine Version"
        version = paket.rsplit("@", 1)[1]
        assert re.match(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$", version), (
            f"{datei}: `{paket}` schwebt — `latest`, ein dist-tag, `^x.y`, "
            "`~x.y` oder `x` lassen den Bildbau entscheiden, welcher Code mit "
            "dem Dienst spricht")
