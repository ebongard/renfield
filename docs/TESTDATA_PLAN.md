# Plan: 100 Testdokumente generieren, hochladen & verifizieren

## Context

Die Produktion wurde komplett zurückgesetzt (alle Daten gelöscht). Jetzt brauchen wir einen realistischen Testdatenbestand mit 100 Dokumenten in allen unterstützten Formaten, der die Bereiche Arbeit/Privat/Vereinsarbeit abdeckt und Verbindungen zwischen Personen und Organisationen enthält. Nach dem Upload werden gezielte Such-Tests durchgeführt.

## Schritt 1: Dependencies installieren

```bash
pip install fpdf2 python-docx openpyxl python-pptx --break-system-packages
```

## Schritt 2: Knowledge Bases anlegen (via API)

3 KBs erstellen via `POST https://renfield.local/api/knowledge/bases`:
- **Arbeit** (~35 Dokumente)
- **Privat** (~35 Dokumente)
- **Vereinsarbeit** (~30 Dokumente)

## Schritt 3: Dokumente generieren & hochladen

Generierungs-Script: `/tmp/generate_testdata.py`

### Formate (100 Dokumente total)

> **Hinweis: TXT wird NICHT unterstützt.** Der Docling-basierte Document Processor akzeptiert kein `.txt`-Format.
> Alle ursprünglich als TXT geplanten Dateien wurden als **Markdown (.md)** hochgeladen.
> Unterstützte Formate: PDF, DOCX, PPTX, HTML, MD, CSV, XLSX, ASCIIDOC, XML, AUDIO, VTT, LATEX.

| Format | Geplant | Tatsächlich | Bibliothek |
|--------|---------|-------------|------------|
| PDF    | 20      | 20          | fpdf2      |
| DOCX   | 20      | 20          | python-docx |
| ~~TXT~~| ~~15~~  | 0           | ~~built-in~~ (nicht unterstützt) |
| MD     | 15      | **31** (15 + 16 konvertierte TXT) | built-in |
| HTML   | 15      | 15          | built-in   |
| XLSX   | 10      | 10          | openpyxl   |
| PPTX   | 5       | 4           | python-pptx |

### Personen- und Organisationsnetzwerk

**Arbeit:**
- **TechNova GmbH** — Arbeitgeber (Musterstr. 42, 40210 Düsseldorf)
- **Dr. Sandra Meier** — Abteilungsleiterin, direkte Vorgesetzte
- **Thomas Krüger** — Kollege, Projektpartner
- **Petra Hoffmann** — HR-Abteilung
- **Cloudify Solutions AG** — Externer Dienstleister (München)
- **Digital Dynamics GmbH** — Partnerfirma (Berlin)
- **Prof. Dr. Anna Richter** — Fortbildungs-Dozentin

**Privat:**
- **Maria Musterfrau** — Partnerin (gleiche Adresse)
- **Dr. Klaus Weber** — Hausarzt (Praxis Am Markt 7, Düsseldorf)
- **Stadtwerke Düsseldorf** — Strom/Gas
- **DEVK Versicherung** — Haftpflicht, KFZ
- **Vodafone** — Internet/Telefon
- **Finanzamt Düsseldorf-Nord** — Steuer
- **Autohaus Müller** — KFZ-Werkstatt

**Verein:**
- **TV Angermund 04 e.V.** — Sportverein
- **Michael Schröder** — 1. Vorsitzender
- **Lisa Kern** — Schatzmeisterin
- **Sparkasse Düsseldorf** — Vereinskonto
- **Sportamt Düsseldorf** — Förderung

**User-Adresse:** Am Stirkenbend 20, 40489 Düsseldorf

### Dokumente nach Bereich

#### Arbeit (35 Dokumente)

| # | Dateiname | Format | Inhalt |
|---|-----------|--------|--------|
| 1 | arbeitsvertrag_technova.pdf | PDF | Arbeitsvertrag mit TechNova GmbH, Gehalt 72.000€, Beginn 01.03.2021, unterschrieben von Dr. Sandra Meier |
| 2 | gehaltsabrechnung_2024_01.pdf | PDF | Gehaltsabrechnung Januar 2024, Brutto 6.000€ |
| 3 | gehaltsabrechnung_2024_06.pdf | PDF | Gehaltsabrechnung Juni 2024, inkl. Urlaubsgeld |
| 4 | gehaltsabrechnung_2024_12.pdf | PDF | Gehaltsabrechnung Dezember 2024, inkl. Weihnachtsgeld |
| 5 | projekt_aurora_kickoff.docx | DOCX | Kickoff-Protokoll Projekt Aurora, Teilnehmer: Dr. Meier, Krüger, Cloudify Solutions AG |
| 6 | projekt_aurora_statusbericht_q3.docx | DOCX | Q3 Statusbericht, Budget 450.000€, Timeline, Risiken |
| 7 | projekt_aurora_abschlussbericht.pdf | PDF | Abschlussbericht mit Ergebnissen, Lessons Learned |
| 8 | email_krueger_servermigration.md | MD | Email-Thread mit Thomas Krüger über Servermigration am 15.05.2024 |
| 9 | email_meier_befoerderung.md | MD | Email von Dr. Sandra Meier bzgl. Beförderung zum Senior Developer |
| 10 | email_hoffmann_urlaubsantrag.md | MD | Email an Petra Hoffmann, Urlaubsantrag 05.-19.08.2024 |
| 11 | besprechung_2024_03_15.md | MD | Protokoll Team-Meeting, Themen: Sprint Review, neue Anforderungen Digital Dynamics |
| 12 | besprechung_2024_07_22.md | MD | Protokoll Projekt-Meeting mit Cloudify Solutions AG |
| 13 | performance_review_2024.docx | DOCX | Jahresgespräch 2024, Bewertung durch Dr. Sandra Meier, Zielvereinbarung 2025 |
| 14 | fortbildung_kubernetes_zertifikat.pdf | PDF | Kubernetes-Zertifikat, Dozentin: Prof. Dr. Anna Richter, bestanden 28.09.2024 |
| 15 | fortbildung_cloud_architecture.html | HTML | Schulungsunterlagen Cloud Architecture, TechNova Academy |
| 16 | rechnung_cloudify_hosting.pdf | PDF | Rechnung von Cloudify Solutions AG, 12.500€ netto, Hosting Q2/2024 |
| 17 | rechnung_digital_dynamics_api.pdf | PDF | Rechnung Digital Dynamics GmbH, API-Lizenz 8.900€/Jahr |
| 18 | angebot_cloudify_migration.docx | DOCX | Migrationsangebot Cloudify, 85.000€, Zeitraum 6 Monate |
| 19 | reisekostenabrechnung_muenchen.xlsx | XLSX | Reisekosten München (Cloudify-Besuch), Hotel, Bahn, Verpflegung |
| 20 | reisekostenabrechnung_berlin.xlsx | XLSX | Reisekosten Berlin (Digital Dynamics Workshop) |
| 21 | praesentation_techstrategie_2025.pptx | PPTX | Technologiestrategie 2025, vorgestellt von Dr. Sandra Meier |
| 22 | onboarding_checkliste.html | HTML | Onboarding-Checkliste für neue Mitarbeiter, IT-Setup |
| 23 | it_sicherheitsrichtlinie.md | MD | IT-Sicherheitsrichtlinie TechNova GmbH |
| 24 | vertrag_digital_dynamics_kooperation.pdf | PDF | Kooperationsvertrag mit Digital Dynamics GmbH |
| 25 | email_krueger_code_review.md | MD | Email über Code-Review Findings im Aurora-Projekt |
| 26 | meeting_notes_retro_q4.md | MD | Retrospektive Q4 2024, Action Items |
| 27 | stellenbeschreibung_senior_dev.docx | DOCX | Stellenbeschreibung Senior Developer bei TechNova |
| 28 | zeiterfassung_2024_q3.xlsx | XLSX | Arbeitszeiterfassung Juli-September 2024 |
| 29 | homeoffice_vereinbarung.pdf | PDF | Homeoffice-Vereinbarung, 3 Tage/Woche |
| 30 | inventarliste_hardware.xlsx | XLSX | Hardware-Inventar: Laptop, Monitore, Dockingstation |
| 31 | email_hoffmann_krankmeldung.md | MD | Krankmeldung 12.-14.11.2024 an HR |
| 32 | schulungsplan_2025.html | HTML | Schulungsplan 2025, Budget pro Mitarbeiter 3.000€ |
| 33 | protokoll_betriebsversammlung.docx | DOCX | Betriebsversammlung 15.12.2024, Jahresrückblick |
| 34 | nda_cloudify_solutions.pdf | PDF | NDA mit Cloudify Solutions AG |
| 35 | dienstreiseantrag_hamburg.md | MD | Dienstreiseantrag nach Hamburg, Kundentermin 03.02.2025 |

#### Privat (35 Dokumente)

| # | Dateiname | Format | Inhalt |
|---|-----------|--------|--------|
| 36 | mietvertrag_stirkenbend.pdf | PDF | Mietvertrag Am Stirkenbend 20, 40489 Düsseldorf, 950€ kalt, Vermieter: Immo Düsseldorf GmbH |
| 37 | nebenkostenabrechnung_2023.pdf | PDF | Nebenkostenabrechnung 2023, Nachzahlung 187,50€ |
| 38 | rechnung_stadtwerke_strom_2024_q1.pdf | PDF | Stromrechnung Stadtwerke Düsseldorf, 245,80€, Q1/2024 |
| 39 | rechnung_stadtwerke_gas_2024.pdf | PDF | Gasrechnung Jahresabrechnung 2024, 1.890€ |
| 40 | rechnung_vodafone_jan2024.pdf | PDF | Vodafone Internet+Telefon, 49,99€/Monat |
| 41 | versicherung_devk_haftpflicht.docx | DOCX | DEVK Haftpflichtversicherung, Police Nr. HP-2021-445566 |
| 42 | versicherung_devk_kfz.docx | DOCX | DEVK KFZ-Versicherung, VW Golf, Kennzeichen D-MB 2024, Police Nr. KFZ-2023-778899 |
| 43 | versicherung_devk_hausrat.docx | DOCX | DEVK Hausratversicherung Am Stirkenbend 20, Versicherungssumme 45.000€ |
| 44 | arztbrief_weber_checkup.pdf | PDF | Arztbrief Dr. Klaus Weber, Check-up 08.04.2024, alles unauffällig |
| 45 | arztbrief_weber_grippaler_infekt.md | MD | Befund grippaler Infekt November 2024, AU 3 Tage |
| 46 | rezept_weber_antibiotika.md | MD | Rezept Amoxicillin 500mg, 3x täglich, Dr. Weber |
| 47 | steuerbescheid_2023.pdf | PDF | Einkommensteuerbescheid 2023, Erstattung 1.245€, Finanzamt Düsseldorf-Nord |
| 48 | steuererklaerung_2023_belege.xlsx | XLSX | Belegliste Steuererklärung 2023, Werbungskosten, Sonderausgaben |
| 49 | kfz_kaufvertrag_golf.pdf | PDF | Kaufvertrag VW Golf VII, 15.500€, Autohaus Müller |
| 50 | kfz_inspektion_2024.md | MD | Inspektionsbericht Autohaus Müller, 18.06.2024, nächster TÜV 06/2026 |
| 51 | kfz_tuev_bericht_2024.pdf | PDF | TÜV-Bericht, bestanden ohne Mängel, 18.06.2024 |
| 52 | reisebuchung_mallorca_2024.html | HTML | Reisebuchung Mallorca 01.-15.08.2024, Hotel Sol y Mar, mit Maria Musterfrau |
| 53 | reisebuchung_wien_2024.html | HTML | Städtereise Wien 28.-31.10.2024, Hotel Sacher, mit Maria Musterfrau |
| 54 | email_maria_einkaufsliste.md | MD | Einkaufsliste von Maria Musterfrau für Geburtstagsfeier |
| 55 | email_maria_handwerker.md | MD | Email-Thread mit Maria über Handwerker-Termin Badezimmer |
| 56 | rechnung_ikea_moebel.md | MD | IKEA Rechnung, Bücherregal BILLY + Schreibtisch MALM, 487€ |
| 57 | rechnung_mediamarkt_laptop.md | MD | MediaMarkt Rechnung, MacBook Air M3, 1.299€ |
| 58 | kontoauszug_sparkasse_2024_01.xlsx | XLSX | Kontoauszug Januar 2024, Sparkasse Düsseldorf |
| 59 | meldebescheinigung_duesseldorf.pdf | PDF | Meldebescheinigung Am Stirkenbend 20, seit 15.02.2020 |
| 60 | haushaltsbuch_2024.xlsx | XLSX | Haushaltsbuch 2024, monatliche Ausgaben, Kategorien |
| 61 | garantieschein_waschmaschine.html | HTML | Garantie Bosch Waschmaschine, Kauf 03.03.2023, 5 Jahre |
| 62 | handyvertrag_telekom.docx | DOCX | Telekom Handyvertrag, iPhone 15, 49,95€/Monat |
| 63 | rezept_kontaktlinsen.md | MD | Kontaktlinsenrezept, Fielmann Düsseldorf, -2,5 / -2,75 |
| 64 | email_vermieter_reparatur.md | MD | Email an Immo Düsseldorf GmbH bzgl. Heizungsreparatur |
| 65 | vollmacht_maria_musterfrau.docx | DOCX | Gegenseitige Vorsorgevollmacht mit Maria Musterfrau (synthetic) |
| 66 | zahnarzt_rechnung_2024.pdf | PDF | Zahnarztrechnung Dr. Petersen, Prophylaxe, 98€ |
| 67 | rundfunkbeitrag_2024.md | MD | Rundfunkbeitrag 2024, Beitragsnummer 123-456-789-0 |
| 68 | email_nachbar_paketannahme.md | MD | Email von Nachbar Schmidt wegen Paketannahme |
| 69 | grundsteuer_bescheid_2024.pdf | PDF | Grundsteuerbescheid 2024, 185€/Jahr |
| 70 | strom_anbieterwechsel.html | HTML | Bestätigung Anbieterwechsel zu NaturStrom ab 01.01.2025 |

#### Vereinsarbeit (30 Dokumente)

| # | Dateiname | Format | Inhalt |
|---|-----------|--------|--------|
| 71 | satzung_tv_angermund.pdf | PDF | Vereinssatzung TV Angermund 04 e.V., Gründung 1904 |
| 72 | protokoll_jhv_2024.docx | DOCX | Jahreshauptversammlung 2024, Vorstand: Schröder, Kern, neue Beitragsordnung |
| 73 | protokoll_vorstandssitzung_2024_03.md | MD | Vorstandssitzung März 2024, Planung Sommerfest |
| 74 | protokoll_vorstandssitzung_2024_09.md | MD | Vorstandssitzung September 2024, Hallenzeiten Winter |
| 75 | protokoll_vorstandssitzung_2024_12.md | MD | Vorstandssitzung Dezember 2024, Jahresplanung 2025 |
| 76 | mitgliederliste_2024.xlsx | XLSX | Mitgliederliste 285 Mitglieder, Name, Abteilung, Beitritt |
| 77 | finanzbericht_2024.pdf | PDF | Kassenbericht 2024 von Lisa Kern, Einnahmen 42.500€, Ausgaben 38.200€ |
| 78 | haushaltsplan_2025.xlsx | XLSX | Haushaltsplan 2025, Positionen, Budget |
| 79 | sponsorenvertrag_sparkasse.pdf | PDF | Sponsoringvertrag Sparkasse Düsseldorf, 5.000€/Jahr |
| 80 | sponsorenvertrag_autohaus_mueller.docx | DOCX | Sponsoringvertrag Autohaus Müller, 2.000€/Jahr + Trikotwerbung |
| 81 | foerderantrag_sportamt.docx | DOCX | Förderantrag an Sportamt Düsseldorf, Jugendarbeit, 8.000€ |
| 82 | foerderbescheid_sportamt_2024.pdf | PDF | Förderbescheid Sportamt, bewilligt 6.500€ |
| 83 | email_schroeder_hallenzeiten.md | MD | Email von Michael Schröder wegen neuer Hallenzeiten |
| 84 | email_kern_beitraege.md | MD | Email von Lisa Kern, 12 Mitglieder mit Beitragsrückstand |
| 85 | email_sportamt_foerderung.md | MD | Email vom Sportamt bzgl. Verwendungsnachweis |
| 86 | sommerfest_2024_planung.html | HTML | Sommerfest 12.07.2024, Programm, Helferplanung |
| 87 | sommerfest_2024_abrechnung.xlsx | XLSX | Abrechnung Sommerfest, Einnahmen 3.200€, Ausgaben 2.800€ |
| 88 | trainingsplan_jugend_2024.docx | DOCX | Trainingsplan Jugendabteilung, Montag+Donnerstag, Trainer: Jens Becker |
| 89 | vereinsregister_auszug.pdf | PDF | Vereinsregisterauszug VR 4711, Amtsgericht Düsseldorf |
| 90 | datenschutzerklaerung_verein.html | HTML | DSGVO-Datenschutzerklärung des Vereins |
| 91 | turnierausschreibung_stadtmeisterschaft.md | MD | Stadtmeisterschaft Düsseldorf 2024, Anmeldung, Reglement |
| 92 | ergebnisse_stadtmeisterschaft_2024.md | MD | Ergebnisse: TV Angermund 3. Platz, beste Einzelleistung: Markus Lehmann |
| 93 | versicherung_verein_sportvers.docx | DOCX | Sportversicherung über Landessportbund NRW |
| 94 | mietvertrag_turnhalle.pdf | PDF | Hallennutzungsvertrag mit Stadt Düsseldorf, Halle Angermund |
| 95 | jubilaeum_120_jahre_festschrift.html | HTML | Festschrift 120 Jahre TV Angermund, Geschichte, Ehrungen |
| 96 | pressetext_stadtmeisterschaft.md | MD | Pressemitteilung TV Angermund bei Stadtmeisterschaft |
| 97 | email_schroeder_vereinsausflug.md | MD | Vereinsausflug 2025 planen, Vorschlag Wanderung Eifel |
| 98 | kooperationsvertrag_schule.docx | DOCX | Kooperation mit Carl-Benz-Realschule, Sport-AG |
| 99 | uebungsleiter_vertrag_becker.pdf | PDF | Übungsleiterpauschale Jens Becker, 250€/Monat |
| 100 | abteilungsleiter_treffen_2024.md | MD | Treffen aller Abteilungsleiter, Hallen-/Platzverteilung 2025 |

### Suchqueries & erwartete Ergebnisse

| # | Bereich | Query | Erwartete Treffer |
|---|---------|-------|-------------------|
| 1 | Arbeit | "Arbeitsvertrag TechNova Gehalt" | arbeitsvertrag_technova.pdf (72.000€) |
| 2 | Arbeit | "Projekt Aurora Cloudify" | kickoff, statusbericht, abschlussbericht |
| 3 | Arbeit | "Thomas Krüger Servermigration" | email_krueger_servermigration.md |
| 4 | Arbeit | "Beförderung Senior Developer" | email_meier_befoerderung.md, stellenbeschreibung |
| 5 | Arbeit | "Reisekosten München Berlin" | reisekostenabrechnung_muenchen.xlsx, _berlin.xlsx |
| 6 | Privat | "Mietvertrag Am Stirkenbend" | mietvertrag_stirkenbend.pdf |
| 7 | Privat | "Stromrechnung Stadtwerke 2024" | rechnung_stadtwerke_strom_2024_q1.pdf |
| 8 | Privat | "DEVK Versicherung KFZ" | versicherung_devk_kfz.docx (D-MB 2024) |
| 9 | Privat | "Dr. Klaus Weber Arzt" | arztbrief_weber_checkup.pdf, _grippaler_infekt.md |
| 10 | Privat | "Maria Musterfrau Mallorca" | reisebuchung_mallorca_2024.html |
| 11 | Privat | "Steuerbescheid Erstattung" | steuerbescheid_2023.pdf (1.245€) |
| 12 | Privat | "VW Golf Autohaus Müller" | kfz_kaufvertrag_golf.pdf, kfz_inspektion_2024.md |
| 13 | Verein | "TV Angermund Satzung" | satzung_tv_angermund.pdf |
| 14 | Verein | "Michael Schröder Vorsitzender" | protokoll_jhv_2024.docx, emails |
| 15 | Verein | "Lisa Kern Finanzbericht" | finanzbericht_2024.pdf, email_kern_beitraege.md |
| 16 | Verein | "Sponsoring Sparkasse" | sponsorenvertrag_sparkasse.pdf (5.000€) |
| 17 | Verein | "Sommerfest 2024 Abrechnung" | sommerfest_2024_planung.html, _abrechnung.xlsx |
| 18 | Cross | "Düsseldorf 2024" | Diverse Dokumente aus allen 3 KBs |
| 19 | Cross | "Rechnung 2024" | Strom, Gas, Vodafone, Cloudify, Digital Dynamics |
| 20 | Cross | "Versicherung" | DEVK Policen + Sportversicherung Verein |
