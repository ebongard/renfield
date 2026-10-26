"""Der SSRF-Riegel des n8n-MCP gegen die Pflicht, n8n im LAN zu halten.

🛑 Am 2026-10-26 gemessen, Anlass war ein Alarm der Funktionssonde
(„MCP-Dienst n8n ist eingeschränkt", urgency=critical). Die Kette:

- n8n war erreichbar: ohne Schlüssel HTTP 401 in 25 ms, Startseite 200.
- Der Zugangsschlüssel war gültig: mit Schlüssel HTTP 200.
- Und trotzdem scheiterte jeder Werkzeugaufruf. `n8n-mcp@2.94.3` prüft die
  API-BASISADRESSE durch seinen SSRF-Riegel
  (`dist/services/n8n-api-client.js:200` ruft
  `SSRFProtection.validateWebhookUrl(this.baseUrl)` — trotz des Namens also
  nicht nur im Webhook-Pfad) und verweigert private Adressen.
- n8n MUSS hier aber im LAN stehen: die öffentliche Adresse zeigte auf einen
  fremden Hoster, darum wurde sie mit #1235 auf eine LAN-IP gesetzt.

Beides schließt sich aus. Der stdio-Server verbindet dabei sauber mit 13
Werkzeugen — benutzen kann sie niemand. Die Sonde meldete das korrekt; sie hat
damit zum zweiten Mal etwas Echtes gefangen (das erste Mal am 2026-09-13).

Darunter liegt der Mechanismus, der es lautlos machte: das Bild installierte
`n8n-mcp@latest`, also entschied jeder Bildbau neu, welcher Code mit n8n
spricht.
"""
import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.backend]

WURZEL = Path(__file__).resolve().parents[2]


def _globale_installationszeilen() -> list[tuple[str, str]]:
    """Die echten `npm install -g`-Zeilen ALLER Backend-Bilddateien.

    Rückgabe: `(dateiname, zeile)`, damit eine Fehlermeldung sagt WO.

    🛑 Zwei eigene Fehlmessungen stecken in dieser Funktion, beide am
    2026-10-26 im Review gefunden:

    1. Die erste Fassung griff über das ganze Dockerfile und sammelte einen
       KOMMENTAR ein, der `npm install -g <localdir>` nur erklärt (der
       mitgelieferte SearXNG-Fork). Der rote Test war meine Messung, nicht das
       Dockerfile. Darum jetzt nur Anweisungszeilen.
    2. Die zweite Fassung las NUR `Dockerfile` — und `Dockerfile.gpu` trug
       `n8n-mcp@latest` unverändert weiter, obwohl es ein lebender Bauweg ist
       (`docker-compose.yml`, `docker-compose.prod.yml`,
       `.github/workflows/release.yml`). Der Test war grün, während der Ausfall
       in jedem GPU-Bild reproduzierbar blieb: die benannte Instanz repariert,
       das Geschwister stehen gelassen. Darum `glob("Dockerfile*")`.

    Fortsetzungszeilen werden zusammengezogen, sonst entgeht ein
    `RUN npm install -g` mit Zeilenumbruch und dem Paket auf der nächsten
    Zeile der Prüfung.
    """
    treffer: list[tuple[str, str]] = []
    for datei in sorted((WURZEL / "src" / "backend").glob("Dockerfile*")):
        if not datei.is_file():
            continue
        # Fortsetzungen zu einer logischen Anweisung verbinden.
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
    """Ohne den Schalter ist die n8n-Anbindung tot, nicht bloß eingeschränkt."""
    env = _server("n8n").get("env") or {}
    assert env.get("WEBHOOK_SECURITY_MODE") == "permissive", (
        "`WEBHOOK_SECURITY_MODE: permissive` fehlt in der n8n-Stanza — der "
        "SSRF-Riegel von n8n-mcp weist dann jeden Aufruf an die LAN-Adresse "
        "ab, waehrend der Server weiter als „verbunden\" gilt. Der Ausfall "
        "ist lautlos: nur die Funktionssonde sieht ihn.")


def test_the_probe_that_catches_it_still_exists():
    """🛑 Der Schalter und seine Kontrolle gehören zusammen.

    Entdeckt wurde der Ausfall nur, weil n8n eine Funktionssonde hat. Fällt die
    weg, kehrt der lautlose Modus zurück — der Server meldete ja „verbunden".
    """
    sonde = _server("n8n").get("health_probe") or {}
    assert sonde.get("tool") == "n8n_list_workflows", (
        "die n8n-Funktionssonde ist weg oder ruft ein anderes Werkzeug — dann "
        "faellt ein Totalausfall der Anbindung wieder niemandem auf")


def _npx_server_pakete() -> list[tuple[str, str]]:
    """`(servername, paket)` fuer JEDEN stdio-Server, der per npx startet.

    🛑 Die Herleitung geht von der KONFIGURATION aus, nicht vom Dockerfile.
    Die erste Fassung las nur `npm install -g`-Zeilen und beglaubigte damit die
    Klasse auf der Staerke der einen reparierten Instanz: `mcp-newsapi` stand
    gar nicht im Bild, lief aber in Produktion (`NEWS_ENABLED: "true"`), und
    der Test war gruen. Von der Konfiguration her gedacht faellt ein NEU
    hinzugefuegter npx-Server von selbst durch, statt an einer
    Dockerfile-Pruefung vorbeizurutschen. Im Review zu #1412 gefunden.
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
    """`{dateiname: {paketname: version}}` aus allen Backend-Bilddateien."""
    je_datei: dict[str, dict[str, str]] = {}
    for datei, zeile in _globale_installationszeilen():
        for token in re.findall(r"(?<=\s)([@\w][\w@/.-]*@[\w.+-]+)", " " + zeile):
            name, _, version = token.rpartition("@")
            je_datei.setdefault(datei, {})[name] = version
    return je_datei


def test_every_npx_mcp_server_is_pinned_in_every_backend_image():
    """🛑 Ohne globale Installation laedt `npx -y <paket>` beim POD-START aus
    dem Netz und fuehrt den Code aus — der Startzeitpunkt entscheidet dann,
    welcher Code neben der Datenbank, den Modellservern und den beiden
    MCP-Diensten laeuft, die absichtlich Zugangsdaten halten.

    Am 2026-10-26 im laufenden Pod gemessen: `mcp-newsapi` war NICHT im Bild,
    stattdessen lag unter `$HOME/.npm/_npx/<hash>` ein Cache mit
    `{"dependencies": {"mcp-newsapi": "^1.0.0"}}` — eine Caret-Spanne, zur
    Laufzeit aufgeloest, bei `NEWS_ENABLED: "true"`.
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
    """🛑 Die KLASSE, nicht nur n8n-mcp.

    `npx -y <paket>` löst auf die globale Installation im Bild auf (im
    laufenden Pod nachgesehen: `node /usr/bin/n8n-mcp`, kein npx-Cache). Diese
    Zeilen entscheiden also, welcher Code läuft. Mit `@latest` entscheidet das
    der Zeitpunkt des Bildbaus, und eine Verhaltensänderung kommt ohne eine
    einzige Zeile Diff herein — genau so geschah es.
    """
    offen = [f"{d}: {z}" for d, z in _globale_installationszeilen()
             if "@latest" in z]
    assert not offen, (
        "ein MCP-Paket haengt an `@latest` — jeder Bildbau kann dann ein "
        "anderes Verhalten einziehen, ohne dass der Diff etwas zeigt:\n  "
        + "\n  ".join(offen))


def test_every_globally_installed_mcp_package_names_an_exact_version():
    """Und die Gegenrichtung: eine Angabe wie `^2.9` waere ebenso offen.

    🛑 Geprueft wird das SCHWEBEN, nicht die SemVer-Form. Die erste Fassung
    verlangte genau `\\d+.\\d+.\\d+` und haette damit eine absichtliche
    Festlegung auf eine Vorabversion (`2.94.3-rc.1`, etwa um einen noch nicht
    veroeffentlichten Fix zu holen) aus dem falschen Grund abgelehnt. Eine
    Vorabversion ist unveraenderlich; `latest`, ein dist-tag, `^`, `~` und `x`
    sind es nicht. Im Review zu #1412 gefunden.
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
