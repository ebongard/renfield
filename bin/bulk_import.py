#!/usr/bin/env python3
"""
Renfield Bulk Import — klassifiziert Dokumente nach Dateiname oder Adressat
und lädt sie in die richtige Wissensdatenbank hoch.

Verwendung:
    python bin/bulk_import.py --dir ~/Downloads/Docs
    python bin/bulk_import.py --dir ~/Downloads/Docs --dry-run
    python bin/bulk_import.py --dir ~/Downloads/Docs --config config/import_rules.yaml
    python bin/bulk_import.py --dir ~/Downloads/Docs --base-url https://renfield.local
    python bin/bulk_import.py --dir ~/Downloads/Docs --verify-ssl   # erzwingt SSL-Verifikation

Klassifizierungsreihenfolge:
    1. Dateiname: enthält PRIVAT / XIDRA / SSV / VP / VuP  →  KB-Name aus Regeln
    2. Adressat:  Text der 1. Seite (PDF) auf bekannte Muster prüfen
    3. Fallback:  unclassified — wird separat gemeldet (kein Upload)

Duplikate (HTTP 409) werden als "bereits importiert" gewertet, nicht als Fehler.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    sys.exit("httpx nicht installiert. Bitte: pip install httpx")

# ---------------------------------------------------------------------------
# Standardkonfiguration (kann per --config überschrieben werden)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    # Groß-/Kleinschreibung wird ignoriert; Reihenfolge bestimmt Priorität.
    "filename_patterns": {
        "PRIVAT": "privat",
        "XIDRA": "xidra",
        "SSV": "ssv",
        "VuP": "VuP",
        "VP": "VuP",
    },
    # Regex gegen den extrahierten Text der 1. Seite (nur PDF).
    "addressee_patterns": {
        r"Eduard[.\s]+van\s+den\s+Bongard": "privat",
        r"Jutta[.\s]+van\s+den\s+Bongard": "privat",
        r"XIDRA": "xidra",
        r"SSV.{0,30}Kleinenbroich": "ssv",
        r"Schiess-Sport": "ssv",
    },
    # KB-Name für nicht klassifizierte Dateien (None = überspringen, nur melden).
    "fallback_kb": None,
    # PDFs größer als dieser Wert (in KB) bekommen automatisch force_ocr=true.
    "ocr_size_threshold_kb": 500,
}

SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md",
    ".html", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg",
}

# ---------------------------------------------------------------------------
# Datenklassen
# ---------------------------------------------------------------------------


@dataclass
class ImportResult:
    path: Path
    kb_name: Optional[str]
    classification_reason: str
    status: str = "pending"   # pending | uploaded | duplicate | skipped | failed
    document_id: Optional[int] = None
    chunk_count: int = 0
    error: Optional[str] = None
    force_ocr: bool = False


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


def load_config(config_path: Optional[Path]) -> dict:
    if config_path is None:
        return DEFAULT_CONFIG

    try:
        import yaml  # optional dependency
        with open(config_path) as f:
            user_cfg = yaml.safe_load(f)
    except ImportError:
        with open(config_path) as f:
            user_cfg = json.load(f)

    cfg = DEFAULT_CONFIG.copy()
    cfg.update(user_cfg or {})
    return cfg


def resolve_local_hostname(url: str) -> str:
    """Löst .local-Hostnamen via mDNS auf, falls Python's socket das nicht kann.

    macOS löst .local über Bonjour auf, aber Python's socket.getaddrinfo()
    unterstützt das häufig nicht. Fallback: ping -c1 → IP extrahieren.
    """
    from urllib.parse import urlparse, urlunparse
    import socket

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname or not hostname.endswith(".local"):
        return url

    # Prüfen ob Python es selbst auflösen kann
    try:
        socket.getaddrinfo(hostname, parsed.port or 443)
        return url  # klappt, nichts zu tun
    except socket.gaierror:
        pass

    # Fallback: ping -c1 um die IP zu bekommen
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-t", "2", hostname],
            capture_output=True, text=True, timeout=5,
        )
        # Zeile: "PING renfield.local (192.168.1.159): ..."
        match = re.search(r"\((\d+\.\d+\.\d+\.\d+)\)", result.stdout)
        if match:
            ip = match.group(1)
            # Host im URL ersetzen, aber SNI/Host-Header bleibt korrekt
            new_netloc = parsed.netloc.replace(hostname, ip)
            resolved = urlunparse(parsed._replace(netloc=new_netloc))
            print(f"   ℹ️  {hostname} → {ip} (mDNS-Fallback)")
            return resolved
    except Exception:
        pass

    return url  # Auflösung fehlgeschlagen, Original-URL zurückgeben


def fetch_kb_map(base_url: str, token: Optional[str], verify_ssl: bool = True) -> dict[str, int]:
    """Lädt alle Knowledge Bases und gibt name→id zurück."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=base_url, timeout=10, verify=verify_ssl) as client:
        r = client.get("/api/knowledge/bases", headers=headers)
        r.raise_for_status()
    return {kb["name"]: kb["id"] for kb in r.json()}


def extract_first_page_text(path: Path) -> str:
    """Versucht, Text der 1. Seite eines PDFs zu extrahieren (best-effort)."""
    if path.suffix.lower() != ".pdf":
        return ""

    # 1. pypdf (keine System-Abhängigkeit)
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        if reader.pages:
            return reader.pages[0].extract_text() or ""
    except Exception:
        pass

    # 2. pdftotext via subprocess
    try:
        result = subprocess.run(
            ["pdftotext", "-l", "1", str(path), "-"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass

    return ""


def classify(path: Path, cfg: dict) -> tuple[Optional[str], str]:
    """Gibt (kb_name, grund) zurück. kb_name=None → unklassifiziert."""
    name_upper = path.stem.upper()

    # 1. Dateiname
    for pattern, kb_name in cfg["filename_patterns"].items():
        if pattern.upper() in name_upper:
            return kb_name, f"Dateiname enthält '{pattern}'"

    # 2. Adressat (1. Seite)
    first_page = extract_first_page_text(path)
    if first_page:
        for regex, kb_name in cfg["addressee_patterns"].items():
            if re.search(regex, first_page, re.IGNORECASE):
                return kb_name, f"Adressat-Regex '{regex}'"

    # 3. Fallback
    fallback = cfg.get("fallback_kb")
    if fallback:
        return fallback, "Fallback-KB"

    return None, "Kein Treffer"


def should_force_ocr(path: Path, cfg: dict) -> bool:
    threshold = cfg.get("ocr_size_threshold_kb", 500) * 1024
    return path.suffix.lower() == ".pdf" and threshold > 0 and path.stat().st_size > threshold


def upload_file(
    path: Path,
    kb_id: int,
    force_ocr: bool,
    base_url: str,
    token: Optional[str],
    verify_ssl: bool = True,
) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {"knowledge_base_id": kb_id, "force_ocr": str(force_ocr).lower()}

    with open(path, "rb") as f:
        files = {"file": (path.name, f, "application/octet-stream")}
        with httpx.Client(base_url=base_url, timeout=300, verify=verify_ssl) as client:
            r = client.post(
                "/api/knowledge/upload",
                params=params,
                files=files,
                headers=headers,
            )

    if r.status_code == 409:
        return {"_duplicate": True, **r.json()}
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Haupt-Ablauf
# ---------------------------------------------------------------------------


def scan_directory(directory: Path, recursive: bool) -> list[Path]:
    if recursive:
        files = [p for p in directory.rglob("*") if p.is_file()]
    else:
        files = [p for p in directory.iterdir() if p.is_file()]
    return sorted(
        [f for f in files if f.suffix.lower() in SUPPORTED_EXTENSIONS],
        key=lambda p: p.name,
    )


def print_plan(results: list[ImportResult], kb_map: dict[str, int]) -> None:
    print(f"\n{'='*72}")
    print(f"  RENFIELD BULK IMPORT — DRY RUN")
    print(f"{'='*72}")
    unclassified = [r for r in results if r.kb_name is None]
    classified = [r for r in results if r.kb_name is not None]

    print(f"\n✅ Klassifiziert ({len(classified)} Dateien):\n")
    for r in classified:
        kb_id = kb_map.get(r.kb_name, "?")
        ocr = " [force_ocr]" if r.force_ocr else ""
        print(f"  {r.path.name:<50}  →  {r.kb_name} (ID={kb_id}){ocr}")
        print(f"    Grund: {r.classification_reason}")

    if unclassified:
        print(f"\n⚠️  Nicht klassifiziert ({len(unclassified)} Dateien — werden übersprungen):\n")
        for r in unclassified:
            print(f"  {r.path.name}")
    print()


def run_import(
    results: list[ImportResult],
    kb_map: dict[str, int],
    base_url: str,
    token: Optional[str],
    delay: float,
    verify_ssl: bool = True,
) -> None:
    to_upload = [r for r in results if r.kb_name is not None]
    skipped = [r for r in results if r.kb_name is None]

    for r in skipped:
        r.status = "skipped"

    total = len(to_upload)
    for i, r in enumerate(to_upload, 1):
        kb_id = kb_map.get(r.kb_name)
        if kb_id is None:
            r.status = "failed"
            r.error = f"KB '{r.kb_name}' nicht gefunden (verfügbar: {list(kb_map)})"
            print(f"[{i}/{total}] ❌ FEHLER {r.path.name}: {r.error}")
            continue

        size_mb = r.path.stat().st_size / 1024 / 1024
        ocr_flag = " [OCR]" if r.force_ocr else ""
        print(f"[{i}/{total}] ⬆  {r.path.name} ({size_mb:.1f}MB){ocr_flag} → {r.kb_name}", end=" ", flush=True)

        try:
            data = upload_file(r.path, kb_id, r.force_ocr, base_url, token, verify_ssl)
            if data.get("_duplicate"):
                r.status = "duplicate"
                existing = data.get("detail", {})
                if isinstance(existing, dict):
                    existing = existing.get("existing_document", {})
                print(f"⚠️  bereits vorhanden (ID={existing.get('id', '?')})")
            elif data.get("status") == "failed":
                r.status = "failed"
                r.error = data.get("error_message", "Unbekannter Fehler")
                print(f"❌ Verarbeitung fehlgeschlagen: {r.error}")
            else:
                r.status = "uploaded"
                r.document_id = data.get("id")
                r.chunk_count = data.get("chunk_count", 0)
                print(f"✅ ID={r.document_id}, {r.chunk_count} Chunks")
        except httpx.HTTPStatusError as e:
            r.status = "failed"
            r.error = f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            print(f"❌ {r.error}")
        except Exception as e:
            r.status = "failed"
            r.error = str(e)
            print(f"❌ {r.error}")

        if i < total and delay > 0:
            time.sleep(delay)


def print_summary(results: list[ImportResult]) -> None:
    counts = {s: sum(1 for r in results if r.status == s)
              for s in ("uploaded", "duplicate", "skipped", "failed")}
    print(f"\n{'='*72}")
    print(f"  ZUSAMMENFASSUNG")
    print(f"{'='*72}")
    print(f"  ✅ Hochgeladen:       {counts['uploaded']}")
    print(f"  ⚠️  Bereits vorhanden: {counts['duplicate']}")
    print(f"  ⏭  Übersprungen:      {counts['skipped']}")
    print(f"  ❌ Fehlgeschlagen:    {counts['failed']}")
    print(f"  Gesamt:              {len(results)}")
    print()

    if counts["failed"] > 0:
        print("Fehlgeschlagene Dateien:")
        for r in results:
            if r.status == "failed":
                print(f"  {r.path.name}: {r.error}")
        print()


def write_report(results: list[ImportResult], directory: Path) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = directory / f"bulk_import_{ts}.json"
    data = [
        {
            "file": str(r.path),
            "kb": r.kb_name,
            "reason": r.classification_reason,
            "status": r.status,
            "document_id": r.document_id,
            "chunk_count": r.chunk_count,
            "force_ocr": r.force_ocr,
            "error": r.error,
        }
        for r in results
    ]
    report_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return report_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Renfield Bulk Import — lädt Dokumente in die richtige Wissensdatenbank.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dir", required=True, type=Path, help="Verzeichnis mit Dokumenten")
    parser.add_argument("--base-url", default="https://renfield.local", help="Renfield Backend URL")
    parser.add_argument("--token", default=None, help="JWT Bearer Token (wenn AUTH_ENABLED=true)")
    parser.add_argument("--config", type=Path, default=None, help="Pfad zur YAML/JSON-Konfigurationsdatei")
    parser.add_argument("--dry-run", action="store_true", help="Nur anzeigen, nichts hochladen")
    parser.add_argument("--recursive", action="store_true", help="Unterverzeichnisse einschließen")
    parser.add_argument("--delay", type=float, default=1.0, help="Pause zwischen Uploads in Sekunden (default: 1.0)")
    parser.add_argument("--no-report", action="store_true", help="Keinen JSON-Bericht schreiben")
    parser.add_argument("--verify-ssl", action="store_true", help="SSL-Zertifikat verifizieren (default: aus für self-signed)")
    args = parser.parse_args()

    if not args.dir.is_dir():
        sys.exit(f"Verzeichnis nicht gefunden: {args.dir}")

    cfg = load_config(args.config)
    verify_ssl = args.verify_ssl

    # Self-signed-Zertifikat-Warnung unterdrücken
    if not verify_ssl:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    # .local-Hostname auflösen (mDNS-Fallback für macOS + Python)
    base_url = resolve_local_hostname(args.base_url)

    # KB-Map vom Server laden
    print(f"🔗 Verbinde mit {base_url} …")
    try:
        kb_map = fetch_kb_map(base_url, args.token, verify_ssl)
    except Exception as e:
        sys.exit(f"❌ Kann Knowledge Bases nicht laden: {e}")
    print(f"   Gefundene KBs: {', '.join(f'{n} (ID={i})' for n, i in kb_map.items())}\n")

    # Dateien einlesen und klassifizieren
    files = scan_directory(args.dir, args.recursive)
    if not files:
        print("Keine unterstützten Dateien gefunden.")
        return

    print(f"🔍 {len(files)} Dateien gefunden — klassifiziere …\n")
    results: list[ImportResult] = []
    for path in files:
        kb_name, reason = classify(path, cfg)
        force_ocr = should_force_ocr(path, cfg)
        results.append(ImportResult(
            path=path,
            kb_name=kb_name,
            classification_reason=reason,
            force_ocr=force_ocr,
        ))

    if args.dry_run:
        print_plan(results, kb_map)
        return

    # Import durchführen
    run_import(results, kb_map, base_url, args.token, args.delay, verify_ssl)
    print_summary(results)

    if not args.no_report:
        report = write_report(results, args.dir)
        print(f"📄 Bericht gespeichert: {report}")


if __name__ == "__main__":
    main()
