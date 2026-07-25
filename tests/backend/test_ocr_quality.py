"""Unit tests for the shared OCR-quality heuristics (utils/ocr_quality.py).

Covers both consumers: the ingest pipeline's binary ``is_text_garbled`` gate
and the Paperless audit's 1..5 ``score_ocr_quality``. The headline case is the
false-positive regression: well-formatted invoices (column padding, dotted
leaders) must score 5, not be docked for "Repeated characters" — that bug
falsely flagged 15/26 documents in the real audit corpus.
"""
import pytest

from utils.ocr_quality import is_text_garbled, score_ocr_quality

pytestmark = pytest.mark.unit


class TestIsTextGarbled:
    def test_normal_prose_not_garbled(self):
        text = "Dies ist ein ganz normaler deutscher Text mit vielen Leerzeichen darin."
        assert is_text_garbled(text) is False

    def test_no_space_mojibake_is_garbled(self):
        # ~260 chars, zero spaces => well under the 3% threshold.
        assert is_text_garbled("UmschauMarktplatzWiesbaden" * 10) is True

    def test_short_text_never_garbled(self):
        # Under the 50-char floor we don't judge.
        assert is_text_garbled("NoSpacesHere") is False

    def test_empty_not_garbled(self):
        assert is_text_garbled("") is False


class TestScoreOcrQuality:
    def test_clean_text_scores_five(self):
        text = (
            "Sehr geehrte Damen und Herren,\n\n"
            "hiermit senden wir Ihnen die Rechnung.\n\n"
            "Mit freundlichen Gruessen"
        )
        score, issues = score_ocr_quality(text)
        assert score == 5
        assert issues == "OK"

    def test_empty_scores_one(self):
        score, issues = score_ocr_quality("")
        assert score == 1
        assert "minimal" in issues.lower() or "no" in issues.lower()

    def test_minimal_scores_one(self):
        score, _ = score_ocr_quality("abc")
        assert score == 1

    def test_space_aligned_invoice_scores_five(self):
        """A column-aligned invoice (runs of >=6 spaces) is clean — score 5.

        This is the corpus shape that the old ``(.)\\1{5,}`` rule wrongly
        docked: aligned columns contain long space runs, but spaces count as
        text so neither the repeated-char nor the special-char rule fires.
        """
        text = (
            "Position      Menge      Einzelpreis      Gesamt\n"
            "Webhosting          1          12,00 EUR          12,00 EUR\n"
            "Domain .de          1           5,00 EUR           5,00 EUR\n"
            "Summe netto                                       17,00 EUR\n"
        )
        score, issues = score_ocr_quality(text)
        assert score == 5, issues
        assert issues == "OK"

    @pytest.mark.parametrize(
        "text",
        [
            # Column-aligned invoice line items: runs of >=6 spaces.
            "Position      Menge      Einzelpreis      Gesamt\n"
            "Webhosting          1          12,00 EUR          12,00 EUR\n",
            # Table-of-contents dotted leaders embedded in real prose.
            "Das Inhaltsverzeichnis dieses Dokuments listet alle Abschnitte.\n"
            "Einleitung und Vorwort des Autors ............................ 1\n"
            "Der ausfuehrliche Hauptteil mit allen Details .............. 5\n",
            # Form with underscore fill-in fields among normal labels.
            "Bitte tragen Sie hier Ihre vollstaendigen Daten leserlich ein.\n"
            "Name und Vorname der antragstellenden Person: ____________________\n"
            "Vollstaendige Anschrift inklusive Postleitzahl: ________________\n",
            # Section rule of equals signs surrounded by content.
            "Rechnung Nummer 2301 vom dritten Juni zweitausendsechsundzwanzig\n"
            "==================================\n"
            "Der faellige Betrag belaeuft sich auf achtzehn Euro insgesamt.\n",
        ],
    )
    def test_ordinary_formatting_not_flagged_as_repeated(self, text):
        """The false-positive regression: punctuation/whitespace runs are
        normal formatting and must NOT be scored as 'Repeated characters'.

        (Orthogonal rules like high-special-char ratio may still apply to
        punctuation-dense fragments — this test only asserts the repeated-char
        rule no longer false-positives on formatting runs.)
        """
        _, issues = score_ocr_quality(text)
        assert "repeated" not in issues.lower(), issues

    def test_repeated_char_run_not_flagged(self):
        """There is no 'Repeated characters' rule: a long same-char run is not
        a reliable OCR-quality signal (measured 0 true positives on the real
        corpus — only redaction masks 'XXXX' and zero-padding '0000'). Such a
        run in otherwise-clean text scores 5; genuine garbling is caught by the
        space/special-char/fragmentation signals instead."""
        text = "Normal readable text here, and then llllllllll appears mid-sentence."
        score, issues = score_ocr_quality(text)
        assert "repeated" not in issues.lower()
        assert score == 5

    def test_garbled_no_spaces_flagged(self):
        text = "abcdefghijklmnopqrstuvwxyz" * 10
        score, issues = score_ocr_quality(text)
        assert score < 5
        assert "spaces" in issues.lower() or "garbled" in issues.lower()

    def test_high_special_char_ratio_flagged(self):
        text = "!!@@##$$%%^^&&**(()){{}}||\\//~~``" * 5
        score, issues = score_ocr_quality(text)
        assert score < 5
        assert "special" in issues.lower()

    def test_fragmented_short_lines_flagged(self):
        text = "\n".join(["ab cd ef"] * 20)
        score, issues = score_ocr_quality(text)
        assert score < 5
        assert "fragmented" in issues.lower() or "short" in issues.lower()

    def test_score_never_below_one(self):
        score, _ = score_ocr_quality("!@#$%^" * 50)
        assert score >= 1


class TestCharLevelGarbleSignal:
    """The rotated/poor-scan case: real-looking spacing but wrong letters
    (internal punctuation, all-consonant runs) — which the space / special-char /
    fragmentation signals all miss. Measured on a real garbled receipt: ratio 0.46."""

    def test_char_garble_with_normal_spacing_scores_low(self):
        garbled = (
            "Rechnung KIJ Betrag Bez:-ihl unq Maa KNr lUGB Datum i;5.Lei "
            "Nummer fff torC:a Summe Hqrq:nt Konto Aut:r Beleg Nmm:r Karte Nrq"
        )
        score, issues = score_ocr_quality(garbled)
        assert score <= 2, f"expected <=2, got {score} ({issues})"
        assert "garbled" in issues.lower()

    def test_clean_german_prose_scores_five(self):
        clean = (
            "Sehr geehrte Damen und Herren, anbei erhalten Sie die Rechnung fuer "
            "den Monat Mai. Der Gesamtbetrag betraegt 149,90 Euro und ist innerhalb "
            "von vierzehn Tagen zu begleichen. Mit freundlichen Gruessen."
        )
        score, issues = score_ocr_quality(clean)
        assert score == 5
        assert issues == "OK"

    def test_garbled_token_ratio_helper(self):
        from utils.ocr_quality import _garbled_token_ratio

        assert _garbled_token_ratio("KIJ Bez:-ihl lUGB i;5.Lei fff Aut:r Nrq KNr") >= 0.4
        assert _garbled_token_ratio(
            "normaler deutscher Text mit vielen echten Woertern hier drin"
        ) == 0.0

    def test_clean_receipt_tokens_not_over_flagged(self):
        # A clean receipt has numbers/codes but real words — must stay OK, not be
        # docked as garbled (false-positive guard for the 0.40 severe threshold).
        receipt = (
            "Rechnung Nummer 92088 Betrag 13,50 EUR Bezahlung MasterCard Datum "
            "30.01.2026 Kunde Sparkasse Wiesbaden Gesamt netto 11,34 Steuer 19 Prozent"
        )
        score, _ = score_ocr_quality(receipt)
        assert score >= 4
