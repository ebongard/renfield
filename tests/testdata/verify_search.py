#!/usr/bin/env python3
"""
Verification tests for uploaded test documents.
Tests search API to confirm all documents are findable via RAG.

Usage:
    python3 tests/testdata/verify_search.py
    RENFIELD_URL=https://192.168.1.159 python3 tests/testdata/verify_search.py
"""

import os
import sys
import json
import requests
import warnings

warnings.filterwarnings("ignore")

BASE_URL = os.environ.get("RENFIELD_URL", "https://renfield.local")

# (query, kb_id_or_None, expected_filenames_substring, expected_content_keywords)
# Note: PDFs use latin-1 (fpdf2), so Umlaute become ue/oe/ae/ss in PDF content.
# Keywords should use both forms or only ASCII-safe terms.
SEARCH_TESTS = [
    # === ARBEIT (KB 1) ===
    (
        "Arbeitsvertrag TechNova Gehalt",
        1,
        ["arbeitsvertrag_technova"],
        ["72.000", "TechNova", "Software Developer"],
    ),
    (
        "Projekt Aurora Cloudify",
        1,
        ["projekt_aurora"],
        ["Aurora", "Cloudify"],
    ),
    (
        "Thomas Krüger Servermigration",
        1,
        ["krueger_servermigration"],
        ["Servermigration"],
    ),
    (
        "Beförderung Senior Developer",
        1,
        ["meier_befoerderung"],
        ["Senior Developer"],
    ),
    (
        "Reisekosten München Berlin",
        1,
        ["reisekostenabrechnung"],
        ["475", "271"],
    ),
    # === PRIVAT (KB 2) ===
    (
        "Mietvertrag Am Stirkenbend",
        2,
        ["mietvertrag_stirkenbend"],
        ["Stirkenbend", "950"],
    ),
    (
        "Stromrechnung Stadtwerke 2024",
        2,
        ["stadtwerke_strom"],
        ["Stadtwerke", "245"],
    ),
    (
        "DEVK Versicherung KFZ",
        2,
        ["versicherung_devk_kfz"],
        ["DEVK", "Golf"],
    ),
    (
        "Dr. Klaus Weber Arzt",
        2,
        ["arztbrief_weber"],
        ["Weber"],
    ),
    (
        "Maria Musterfrau Mallorca Reise",
        2,
        ["reisebuchung_mallorca"],
        ["Mallorca", "Maria Musterfrau"],
    ),
    (
        "Steuerbescheid Erstattung 2023",
        2,
        ["steuerbescheid"],
        ["1.245", "Erstattung"],
    ),
    (
        "VW Golf Autohaus Müller Inspektion",
        2,
        ["kfz_"],
        ["Golf"],
    ),
    # === VEREINSARBEIT (KB 3) ===
    (
        "TV Angermund Satzung Verein",
        3,
        ["satzung_tv_angermund"],
        ["Angermund", "1904"],
    ),
    (
        "Michael Schröder Vorsitzender Jahreshauptversammlung",
        3,
        ["protokoll_jhv"],
        ["Vorsitzender"],
    ),
    (
        "Lisa Kern Finanzbericht Kassenbericht",
        3,
        ["finanzbericht"],
        ["Kern", "Einnahmen"],
    ),
    (
        "Sponsoring Sparkasse Düsseldorf Vertrag",
        3,
        ["sponsorenvertrag_sparkasse"],
        ["Sparkasse", "5.000"],
    ),
    (
        "Sommerfest 2024 Planung Abrechnung",
        3,
        ["sommerfest"],
        ["Sommerfest", "2024"],
    ),
    # === CROSS-KB SEARCHES (all KBs) ===
    (
        "Düsseldorf 2024",
        None,
        [],  # many docs expected
        [],  # just check we get results
    ),
    (
        "Rechnung 2024",
        None,
        [],  # multiple invoices
        ["Rechnung"],
    ),
    (
        "Versicherung Police",
        None,
        ["versicherung"],
        ["Versicherung"],
    ),
]


def search(query, kb_id=None, top_k=10):
    """Search via API and return results."""
    payload = {"query": query, "top_k": top_k}
    if kb_id:
        payload["knowledge_base_id"] = kb_id
    resp = requests.post(
        f"{BASE_URL}/api/knowledge/search",
        json=payload,
        verify=False,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def normalize(text):
    """Normalize text for comparison (handle Umlaut replacements in PDFs)."""
    return (
        text.lower()
        .replace("ü", "ue")
        .replace("ö", "oe")
        .replace("ä", "ae")
        .replace("ß", "ss")
        .replace("€", "eur")
    )


def run_tests():
    """Run all search verification tests."""
    passed = 0
    failed = 0
    errors = []

    for i, (query, kb_id, expected_files, expected_keywords) in enumerate(
        SEARCH_TESTS, 1
    ):
        kb_name = {1: "Arbeit", 2: "Privat", 3: "Vereinsarbeit", None: "Alle"}[kb_id]
        print(f"[{i:2d}/{len(SEARCH_TESTS)}] {kb_name:14s} | {query[:50]:50s} ", end="")

        try:
            data = search(query, kb_id)
            results = data.get("results", [])
            count = data.get("count", len(results))

            if count == 0:
                print("FAIL (0 results)")
                errors.append(f"  Test {i}: '{query}' returned 0 results")
                failed += 1
                continue

            # Check expected filenames
            all_filenames = [
                r.get("document", {}).get("filename", "").lower() for r in results
            ]
            file_ok = True
            if expected_files:
                for ef in expected_files:
                    if not any(ef.lower() in fn for fn in all_filenames):
                        file_ok = False
                        errors.append(
                            f"  Test {i}: '{ef}' not in results: {all_filenames[:5]}"
                        )

            # Check expected keywords in content (with Umlaut normalization)
            all_content = " ".join(
                r.get("chunk", {}).get("content", "") for r in results
            )
            all_text = all_content + " " + " ".join(
                r.get("document", {}).get("filename", "") for r in results
            )
            all_text_norm = normalize(all_text)

            kw_ok = True
            for kw in expected_keywords:
                kw_norm = normalize(kw)
                if kw_norm not in all_text_norm and kw.lower() not in all_text.lower():
                    kw_ok = False
                    errors.append(
                        f"  Test {i}: keyword '{kw}' not found in results"
                    )

            if file_ok and kw_ok:
                top_sim = results[0].get("similarity", 0) if results else 0
                print(f"OK   ({count} results, top: {top_sim:.0%})")
                passed += 1
            else:
                print(f"FAIL (content mismatch, {count} results)")
                failed += 1

        except Exception as e:
            print(f"ERROR: {e}")
            errors.append(f"  Test {i}: Exception: {e}")
            failed += 1

    # Summary
    print(f"\n{'=' * 70}")
    print(f"Results: {passed} passed, {failed} failed out of {len(SEARCH_TESTS)}")

    if errors:
        print(f"\nFailure details:")
        for e in errors:
            print(e)

    # KB stats
    try:
        resp = requests.get(
            f"{BASE_URL}/api/knowledge/bases", verify=False, timeout=10
        )
        kbs = resp.json()
        total_docs = 0
        print(f"\nKnowledge Base Stats:")
        for kb in kbs:
            doc_count = kb.get("document_count", 0)
            total_docs += doc_count
            print(f"  {kb['name']}: {doc_count} documents")
        print(f"  Total: {total_docs} documents")
    except Exception:
        pass

    return failed == 0


if __name__ == "__main__":
    ok = run_tests()
    sys.exit(0 if ok else 1)
