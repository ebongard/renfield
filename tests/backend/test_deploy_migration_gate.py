"""``bin/deploy-production.sh`` — das Vorflug-Tor, das den Alembic-Kopf IM
GEBAUTEN BILD gegen die laufende Datenbank stellt (#1392).

Am 2026-10-11 rollte ein Deploy ohne ``--migrate`` aus, weil der PR keine
Migration enthielt; das Bild wurde aber aus ``main`` gebaut, wo
``pc20260929_kg_validity`` lag. Das ORM waehlt jede Spalte unbedingt → ~35 min
Produktionsschaden, und ``/health/ready`` meldete durchgehend ``healthy``.

Hier wird nicht geprueft, DASS es ein Tor gibt, sondern dass es GREIFT: jeder
Fall laeuft das echte Skript ohne ``--dry-run`` gegen Attrappen fuer
``ssh``/``kubectl``/``rsync`` und prueft ZWEI Dinge — den Rueckgabewert UND dass
``kubectl set image`` nie gefallen ist. Ein Tor, dessen Greifen nicht gezeigt
wurde, ist kein Tor.

Die isolierte Kopie auf der Baubox braucht dafuer ``bin/`` UND ``k8s/`` (der
``--migrate``-Schritt liest ``k8s/alembic-upgrade-job.yaml``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "deploy-production.sh"
# Der --migrate-Zweig liest dieses Manifest. Fehlt es in der isolierten Kopie,
# scheitert das Skript mit Code 2 und der Test behauptete sonst, das Tor habe
# nicht durchgelassen — ein Befund am falschen Objekt. Hier bricht es stattdessen
# mit der wahren Ursache ab.
_MIGRATION_JOB = _REPO / "k8s" / "alembic-upgrade-job.yaml"

# Die Attrappe fuer die Baubox. Antwortet auf die beiden Abfragen des Tores und
# schluckt alles andere (docker build/push, rsync-Vorbereitung, Aufraeumen).
_SSH_STUB = r"""#!/bin/bash
args="$*"
case "$args" in
  *"alembic heads"*)
    if [[ "${STUB_IMG_FAIL:-0}" == 1 ]]; then
      echo "docker: error during connect"
      exit 125
    fi
    printf '%s\n' "${STUB_IMG_HEADS}"; exit 0 ;;
  *"image inspect"*) exit "${STUB_IMG_PRESENT_EXIT:-0}" ;;
  # Die Richtungs-Sonde fragt BEIDE Revisionen in EINEM Aufruf und gibt die
  # Rueckgabewerte als Text zurueck. `STUB_PROBE_SILENT=1` spielt den Fall, in
  # dem ssh/docker gar nicht antworten — genau der Fail-open-Pfad aus dem
  # Review vom 2026-10-11.
  *"alembic show"*)
    if [[ "${STUB_PROBE_SILENT:-0}" == 1 ]]; then
      echo "ssh: connect to host ... port 22: Operation timed out" >&2
      exit 255
    fi
    echo "head=${STUB_SHOW_HEAD_EXIT:-0}"
    echo "db=${STUB_SHOW_EXIT:-0}"
    exit 0 ;;
esac
exit 0
"""

# Die Attrappe fuer kubectl protokolliert JEDEN Aufruf — daran haengt der Beweis,
# dass der Abbruch VOR dem ersten `set image` liegt.
_KUBECTL_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
args="$*"
case "$args" in
  *"alembic current"*)
    if [[ "${STUB_DB_FAIL:-0}" == 1 ]]; then
      echo 'Error from server (NotFound): deployments.apps "backend" not found'
      exit 1
    fi
    printf '%s\n' "${STUB_DB_OUT}"; exit 0 ;;
  *"get cm renfield-env"*)
    # Der Plugin-Riegel vor dem Push liest PLUGIN_MODULES; eine leere Antwort
    # laesst seine Pipeline unter `pipefail` scheitern. Eigenheit der Attrappe.
    printf 'stubpkg'; exit 0 ;;
  *version*) echo '{}'; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def deploy(tmp_path):
    """Gibt einen Aufrufer zurueck, der das echte Skript gegen Attrappen laeuft."""
    if shutil.which("bash") is None:  # pragma: no cover - Umgebungsschutz
        pytest.skip("bash nicht vorhanden")
    assert _SCRIPT.is_file(), f"unvollstaendige Kopie: {_SCRIPT} fehlt (bin/ mitrsyncen)"
    assert _MIGRATION_JOB.is_file(), (
        f"unvollstaendige Kopie: {_MIGRATION_JOB} fehlt (k8s/ mitrsyncen) — "
        "ohne das Manifest scheitert der --migrate-Zweig aus einem anderen Grund"
    )

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    for name, body in (("ssh", _SSH_STUB), ("kubectl", _KUBECTL_STUB), ("rsync", "#!/bin/bash\nexit 0\n")):
        path = stub_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "kubectl.log"
    log.write_text("")

    def _run(*args, backend_tag="stub-tag", **stub_env):
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["RENFIELD_REGISTRY"] = "stub.example/renfield"
        # Verhindert, dass der Drift-Schritt echte Manifeste anfasst.
        env["RENFIELD_MANIFESTS_DIR"] = str(tmp_path / "no-manifests")
        env["STUB_LOG"] = str(log)
        env.update({k: str(v) for k, v in stub_env.items()})
        if backend_tag is None:  # frontend-only: es gibt kein neues Backend-Bild
            flags = ["--frontend-tag", "v0.0.0-stub", "--skip-backend"]
        else:
            flags = ["--backend-tag", backend_tag, "--skip-frontend"]
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *flags, *args],
            capture_output=True, text=True, env=env, timeout=120,
        )
        rolled_out = "set image" in log.read_text()
        return proc, rolled_out

    return _run


def test_image_ahead_of_db_without_migrate_aborts_before_rollout(deploy):
    """Der Schadensfall vom 2026-10-11: Bild VOR der DB, kein --migrate."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.\npc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 1
    assert not rolled_out, "das Tor muss VOR dem ersten `set image` abbrechen"
    # Beide Revisionen im Klartext, sonst taugt die Meldung nicht.
    assert "pc20260929_kg_validity" in proc.stderr
    assert "pc20260927_voice2fa" in proc.stderr
    assert "--migrate" in proc.stderr


def test_image_ahead_of_db_with_migrate_proceeds(deploy):
    """Mit --migrate ist die Entscheidung getroffen — der Rollout darf laufen."""
    proc, rolled_out = deploy(
        "--migrate",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert rolled_out


def test_multiple_heads_in_image_abort_even_with_migrate(deploy):
    """Gegabelte Kette: auf der Rollout-Seite ebenso ein Abbruch."""
    proc, rolled_out = deploy(
        "--migrate",
        STUB_IMG_HEADS="aaa111_erste (head)\nbbb222_zweite (head)",
        STUB_DB_OUT="aaa111_erste",
    )
    assert proc.returncode == 1
    assert not rolled_out
    assert "aaa111_erste" in proc.stderr and "bbb222_zweite" in proc.stderr


def test_unreadable_image_head_aborts(deploy):
    """Ein Tor, das bei Unklarheit durchlaesst, ist genau der Fehler aus #1392."""
    proc, rolled_out = deploy(STUB_IMG_FAIL=1, STUB_DB_OUT="pc20260929_kg_validity")
    assert proc.returncode == 1
    assert not rolled_out


def test_unreadable_db_revision_aborts(deploy):
    proc, rolled_out = deploy(STUB_IMG_HEADS="pc20260929_kg_validity (head)", STUB_DB_FAIL=1)
    assert proc.returncode == 1
    assert not rolled_out


def test_empty_alembic_version_without_migrate_aborts(deploy):
    """Leere `alembic_version`: ohne --migrate ist die Lage unklar."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
    )
    assert proc.returncode == 1
    assert not rolled_out


def test_explicit_bypass_proceeds(deploy):
    """Der Notausgang existiert, muss aber getippt werden."""
    proc, rolled_out = deploy(
        "--skip-migration-gate",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "NICHT gegen" in proc.stderr  # der Verzicht wird protokolliert


def test_db_ahead_of_image_warns_but_proceeds(deploy):
    """Rueckfall auf ein aelteres Bild: erlaubt, aber nicht verschwiegen."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260927_voice2fa (head)",
        STUB_DB_OUT="pc20260929_kg_validity",
        STUB_SHOW_EXIT=1,  # das Bild kennt die DB-Revision NICHT
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "RUECKFALL" in proc.stderr


def test_heads_equal_proceeds_with_one_log_line(deploy):
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260929_kg_validity (head)",
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "keine Migration ausstehend" in proc.stdout


def test_skip_backend_skips_the_gate(deploy):
    """Ohne neues Backend-Bild gibt es keinen Kopf zu vergleichen."""
    proc, _ = deploy(backend_tag=None)
    assert proc.returncode == 0
    assert "Vorflug-Tor" not in proc.stdout


def test_dry_run_shows_the_verdict_but_never_aborts(deploy):
    """Der Probelauf ist die einzige Moeglichkeit, das Tor vorher zu proben —
    er muss es also AUSFUEHREN und darf trotzdem nicht scheitern."""
    proc, rolled_out = deploy(
        "--dry-run",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert not rolled_out, "ein Probelauf rollt nichts aus"
    assert "TOR WUERDE ABBRECHEN" in proc.stdout
    assert "pc20260927_voice2fa" in proc.stdout


# ---------------------------------------------------------------------------
# Die Richtungs-Sonde darf bei EIGENEM Scheitern nicht oeffnen
# (Review-Befund vom 2026-10-11)
# ---------------------------------------------------------------------------
#
# 🛑 Der einzige Fail-open-Pfad, den das Tor hatte — und er sass im
# gefaehrlichen Zweig. Die erste Fassung fragte nur `alembic show <db-rev>`
# und las JEDEN Rueckgabewert != 0 als „Revision unbekannt" → Rueckfall →
# WARNUNG → Rollout. Gemessen: Datenbank zurueck + Sonde mit Code 255 ergab
# rc=0 und vier `kubectl set image` — exakt der Ausfall, gegen den dieses Tor
# gebaut ist, nur mit einer Warnzeile davor.
#
# Der alte Test schrieb die Luecke als ABSICHT fest: `STUB_SHOW_EXIT=1` mit
# rc=0 plus Rollout trennte „unbekannte Revision" nicht von „Sonde kaputt".


def test_a_silent_probe_aborts(deploy):
    """ssh/docker antworten nicht → die Richtung ist UNBEKANNT, nicht
    „Rueckfall". Im Zweifel wird nicht ausgerollt."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="neu_kopf (head)",
        STUB_DB_OUT="alt_rev",
        STUB_PROBE_SILENT=1,
    )
    assert proc.returncode == 1, "eine stumme Sonde darf nicht durchlassen"
    assert not rolled_out, "der Abbruch muss VOR dem ersten `set image` liegen"
    assert "UNBEKANNT" in proc.stderr


def test_a_probe_that_fails_on_its_own_head_aborts(deploy):
    """Das Bild MUSS seinen eigenen Kopf kennen. Tut es das nicht, ist die
    Sonde kaputt, und aus dem Fehlschlag fuer die DB-Revision ist kein Schluss
    zu ziehen."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="neu_kopf (head)",
        STUB_DB_OUT="alt_rev",
        STUB_SHOW_HEAD_EXIT=1,
        STUB_SHOW_EXIT=1,
    )
    assert proc.returncode == 1, "eine kaputte Sonde darf nicht als Rueckfall gelten"
    assert not rolled_out
    assert "KAPUTT" in proc.stderr


def test_a_genuine_rollback_still_proceeds(deploy):
    """🛑 Negativkontrolle, und die wichtigere Haelfte: ein ECHTER Rueckfall —
    das Bild kennt seinen eigenen Kopf, aber die DB-Revision nicht — muss
    weiterhin durchlassen. Sonst blockiert das Tor jeden Rueckfall auf ein
    aelteres Bild, wird umgangen und ist wirkungslos."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="alt_kopf (head)",
        STUB_DB_OUT="neu_rev",
        STUB_SHOW_HEAD_EXIT=0,
        STUB_SHOW_EXIT=1,
    )
    assert proc.returncode == 0, "ein echter Rueckfall muss durchlassen"
    assert rolled_out, "und ausrollen"
    assert "RUECKFALL" in proc.stderr


def test_a_missing_image_in_dry_run_is_not_an_abort(deploy):
    """Im --dry-run ist eine noch nicht gebaute Marke der NORMALFALL (Bau und
    Push werden dort nur gedruckt). Das Tor meldete dafuer einen Abbruch, den
    es im echten Lauf nie gaebe — und `docker run` haette dafuer 4,08 GB aus
    der Registry gezogen."""
    proc, _ = deploy("--dry-run", STUB_IMG_PRESENT_EXIT=1)
    assert proc.returncode == 0
    assert "TOR WUERDE ABBRECHEN" not in proc.stdout, (
        f"kein falscher Abbruch im Probelauf, war: {proc.stdout}"
    )
    assert "NICHT probebar" in proc.stdout


def test_a_missing_image_in_a_real_run_aborts(deploy):
    """Im echten Lauf hat Schritt 2 das Bild gerade gebaut. Fehlt es dann, ist
    Bau oder Push schiefgegangen — und dann wird nicht ausgerollt."""
    proc, rolled_out = deploy(STUB_IMG_PRESENT_EXIT=1)
    assert proc.returncode == 1
    assert not rolled_out
