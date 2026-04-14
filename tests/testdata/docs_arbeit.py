"""Arbeit documents (35 docs, KB: Arbeit)."""

DOCS = [
    # 1
    ("arbeitsvertrag_technova.pdf", "Arbeitsvertrag TechNova GmbH",
     """Arbeitsvertrag zwischen TechNova GmbH, Musterstraße 42, 40210 Düsseldorf, vertreten durch die Geschäftsführung, und Erik van den Berg, Am Stirkenbend 20, 40489 Düsseldorf.

Beginn des Arbeitsverhältnisses: 01.03.2021. Position: Software Developer in der Abteilung Softwareentwicklung unter der Leitung von Dr. Sandra Meier. Das jährliche Bruttogehalt beträgt 72.000 Euro, zahlbar in 12 gleichen Monatsraten. Es gelten 30 Urlaubstage pro Kalenderjahr.

Probezeit: 6 Monate. Kündigungsfrist: 3 Monate zum Quartalsende. Arbeitsort: Düsseldorf, mit Option auf Homeoffice nach Vereinbarung. Unterschrieben am 15.02.2021 von Dr. Sandra Meier (Abteilungsleiterin) und Erik van den Berg."""),

    # 2
    ("gehaltsabrechnung_2024_01.pdf", "Gehaltsabrechnung Januar 2024",
     """TechNova GmbH - Gehaltsabrechnung
Mitarbeiter: Erik van den Berg, Personalnummer: TN-2021-0342
Abrechnungsmonat: Januar 2024

Bruttogehalt: 6.000,00 Euro
Kirchensteuer: 42,30 Euro
Solidaritätszuschlag: 0,00 Euro
Lohnsteuer: 1.245,00 Euro
Rentenversicherung: 558,00 Euro
Krankenversicherung: 489,00 Euro
Pflegeversicherung: 101,40 Euro
Arbeitslosenversicherung: 78,00 Euro

Nettogehalt: 3.486,30 Euro
Überweisung an: Sparkasse Düsseldorf, IBAN DE89 3005 0110 0012 3456 78

Abteilung: Softwareentwicklung, Vorgesetzte: Dr. Sandra Meier"""),

    # 3
    ("gehaltsabrechnung_2024_06.pdf", "Gehaltsabrechnung Juni 2024",
     """TechNova GmbH - Gehaltsabrechnung
Mitarbeiter: Erik van den Berg, Personalnummer: TN-2021-0342
Abrechnungsmonat: Juni 2024

Bruttogehalt: 6.000,00 Euro
Urlaubsgeld: 3.000,00 Euro (50% des Monatsgehalts)
Brutto gesamt: 9.000,00 Euro

Lohnsteuer: 2.145,00 Euro
Sozialabgaben gesamt: 1.836,00 Euro
Nettogehalt: 5.019,00 Euro

Resturlaub: 18 Tage von 30 Tagen"""),

    # 4
    ("gehaltsabrechnung_2024_12.pdf", "Gehaltsabrechnung Dezember 2024",
     """TechNova GmbH - Gehaltsabrechnung
Mitarbeiter: Erik van den Berg, Personalnummer: TN-2021-0342
Abrechnungsmonat: Dezember 2024

Bruttogehalt: 6.000,00 Euro
Weihnachtsgeld: 6.000,00 Euro (100% des Monatsgehalts)
Brutto gesamt: 12.000,00 Euro

Lohnsteuer: 3.450,00 Euro
Sozialabgaben gesamt: 2.448,00 Euro
Nettogehalt: 6.102,00 Euro

Jahresbrutto 2024: 81.000,00 Euro (inkl. Urlaubs- und Weihnachtsgeld)"""),

    # 5
    ("projekt_aurora_kickoff.docx", "Projekt Aurora - Kickoff-Protokoll",
     """Projekt Aurora - Kickoff-Meeting
Datum: 15.01.2024, 10:00-12:00 Uhr
Ort: TechNova GmbH, Konferenzraum 3, Düsseldorf

Teilnehmer: Dr. Sandra Meier (Projektleiterin), Erik van den Berg (Lead Developer), Thomas Krüger (Backend-Entwickler), Vertreter Cloudify Solutions AG (München): Stefan Brandl, Anna Hofmann

Projektziel: Migration der Legacy-Infrastruktur auf Cloud-native Architektur mit Kubernetes. Partnerschaft mit Cloudify Solutions AG für Hosting und Beratung. Budget: 450.000 Euro. Zeitraum: Q1-Q3 2024.

Meilensteine: Phase 1 (Q1) - Assessment und Planung. Phase 2 (Q2) - Migration der Core-Services. Phase 3 (Q3) - Testing, Monitoring, Go-Live. Nächstes Meeting: 29.01.2024."""),

    # 6
    ("projekt_aurora_statusbericht_q3.docx", "Projekt Aurora - Statusbericht Q3 2024",
     """Projekt Aurora - Statusbericht Q3 2024
Berichtszeitraum: Juli bis September 2024
Erstellt von: Erik van den Berg

Status: Im Plan (Ampel: Grün)
Budget: 387.000 Euro von 450.000 Euro verbraucht (86%)
Verbleibend: 63.000 Euro

Fortschritt: 14 von 18 Microservices erfolgreich migriert. Kubernetes-Cluster bei Cloudify Solutions AG produktiv. CI/CD-Pipeline vollständig automatisiert. Performance-Tests zeigen 40% bessere Response-Zeiten.

Risiken: Abhängigkeit von Cloudify-Expertise für spezielle Konfigurationen. Thomas Krüger fällt im Oktober teilweise wegen Fortbildung aus.

Nächste Schritte: Verbleibende 4 Services migrieren. Last- und Stresstests durchführen. Go-Live am 15.11.2024 geplant."""),

    # 7
    ("projekt_aurora_abschlussbericht.pdf", "Projekt Aurora - Abschlussbericht",
     """Projekt Aurora - Abschlussbericht
Projektzeitraum: Januar 2024 bis November 2024
Projektleitung: Dr. Sandra Meier

Zusammenfassung: Das Projekt Aurora wurde erfolgreich abgeschlossen. Alle 18 Microservices wurden auf die Kubernetes-Infrastruktur bei Cloudify Solutions AG migriert. Das Go-Live erfolgte am 18.11.2024, zwei Wochen nach dem ursprünglichen Plan.

Endbudget: 438.000 Euro von 450.000 Euro (97% Ausschöpfung). Kernteam: Dr. Sandra Meier, Erik van den Berg, Thomas Krüger, externe Berater von Cloudify Solutions AG.

Lessons Learned: Frühzeitige Einbindung des Ops-Teams war entscheidend. Container-Sizing erforderte mehrere Iterationen. Dokumentation der Legacy-Systeme war unzureichend.

Empfehlung: Wartungsvertrag mit Cloudify Solutions AG für 24/7-Support."""),

    # 8
    ("email_krueger_servermigration.txt", "Email Thomas Krüger Servermigration",
     """Von: Thomas Krüger <t.krueger@technova.de>
An: Erik van den Berg <e.vandenberg@technova.de>
Datum: 15.05.2024, 14:22
Betreff: Re: Servermigration am Wochenende

Hallo Erik,

ich habe die Migration des Auth-Servers auf den neuen Kubernetes-Pod vorbereitet. Das Deployment-Script liegt im GitLab-Repo unter /deploy/auth-service/. Ich schlage vor, die Migration am Samstag, 18.05.2024 ab 06:00 Uhr durchzuführen, wenn die Nutzung am geringsten ist.

Checkliste:
1. Backup der PostgreSQL-Datenbank
2. DNS-Umstellung vorbereiten
3. Rollback-Plan testen
4. Monitoring-Alerts anpassen

Kannst du die Netzwerk-Konfiguration nochmal prüfen? Die Firewall-Rules müssen für den neuen Pod angepasst werden.

Grüße, Thomas"""),

    # 9
    ("email_meier_befoerderung.txt", "Email Dr. Sandra Meier Beförderung",
     """Von: Dr. Sandra Meier <s.meier@technova.de>
An: Erik van den Berg <e.vandenberg@technova.de>
Datum: 02.09.2024, 09:15
Betreff: Beförderung zum Senior Developer

Lieber Erik,

ich freue mich, dir mitteilen zu können, dass die Geschäftsführung deiner Beförderung zum Senior Developer zum 01.10.2024 zugestimmt hat. Deine hervorragende Arbeit am Projekt Aurora und dein Engagement für die Teamführung haben dies verdient.

Mit der Beförderung verbunden:
- Neuer Titel: Senior Software Developer
- Gehaltsanpassung: 78.000 Euro brutto p.a. (ab Oktober)
- Zusätzliche Verantwortung: Mentoring der Junior-Entwickler
- Teilnahme am Architecture Board

Bitte unterschreibe den beigefügten Nachtrag zum Arbeitsvertrag.

Herzliche Grüße,
Dr. Sandra Meier
Abteilungsleiterin Softwareentwicklung"""),

    # 10
    ("email_hoffmann_urlaubsantrag.txt", "Email Petra Hoffmann Urlaubsantrag",
     """Von: Erik van den Berg <e.vandenberg@technova.de>
An: Petra Hoffmann <p.hoffmann@technova.de>
CC: Dr. Sandra Meier <s.meier@technova.de>
Datum: 12.06.2024, 08:30
Betreff: Urlaubsantrag 05.-19.08.2024

Liebe Frau Hoffmann,

hiermit beantrage ich Erholungsurlaub für den Zeitraum 05.08.2024 bis 19.08.2024 (10 Arbeitstage). In dieser Zeit bin ich mit meiner Partnerin Maria Musterfrau auf Mallorca. Im Notfall bin ich per Email erreichbar, Vertretung übernimmt Thomas Krüger.

Resturlaub aktuell: 22 Tage. Nach dem beantragten Urlaub: 12 Tage.

Frau Dr. Meier hat dem Urlaub bereits mündlich zugestimmt.

Mit freundlichen Grüßen,
Erik van den Berg"""),

    # 11
    ("besprechung_2024_03_15.md", "Protokoll Team-Meeting 15.03.2024",
     """## Team-Meeting Softwareentwicklung
**Datum:** 15.03.2024, 14:00-15:30 Uhr
**Teilnehmer:** Dr. Sandra Meier, Erik van den Berg, Thomas Krüger, Julia Berger, Markus Wolf

### Sprint Review
- Feature Login-Redesign: Abgeschlossen, deployed in v2.4.1
- API-Rate-Limiting: In Review, Thomas prüft Performance-Impact
- Dashboard-Widgets: 3 von 5 Stories fertig

### Neue Anforderungen Digital Dynamics GmbH
- Integration der Digital Dynamics API für Partnerdaten-Austausch
- Deadline: Ende Q2 2024
- Erik übernimmt die technische Analyse
- Digital Dynamics stellt Sandbox-Umgebung bis 25.03.

### Action Items
- Erik: API-Spezifikation von Digital Dynamics analysieren (bis 22.03.)
- Thomas: Rate-Limiting-PR mergen (bis 18.03.)
- Sandra: Budget für zusätzliche Testserver freigeben"""),

    # 12
    ("besprechung_2024_07_22.md", "Protokoll Cloudify-Meeting 22.07.2024",
     """## Projekt-Meeting mit Cloudify Solutions AG
**Datum:** 22.07.2024, 10:00-11:30 Uhr (Video-Call)
**Teilnehmer TechNova:** Dr. Sandra Meier, Erik van den Berg
**Teilnehmer Cloudify:** Stefan Brandl, Anna Hofmann (Cloudify Solutions AG, München)

### Agenda
1. Status Migration Phase 2
2. Performance-Optimierung
3. Monitoring-Setup

### Ergebnisse
- 10 von 18 Services migriert, im Plan
- Cloudify schlägt Istio als Service-Mesh vor (Aufpreis 15.000 Euro)
- Prometheus + Grafana Stack wird von Cloudify bereitgestellt
- SLA-Vereinbarung: 99,9% Uptime, Reaktionszeit < 1h bei Priority 1

### Offene Punkte
- Entscheidung zu Istio bis 01.08. (Erik prüft Alternativen)
- Cloudify liefert Kostenaufstellung für dediziertes Monitoring"""),

    # 13
    ("performance_review_2024.docx", "Jahresgespräch 2024",
     """Performance Review 2024 - Erik van den Berg
Bewertet durch: Dr. Sandra Meier, Abteilungsleiterin Softwareentwicklung
Datum: 28.11.2024

Gesamtbewertung: Hervorragend (5 von 5)

Stärken:
- Technische Exzellenz bei Projekt Aurora (Kubernetes-Migration)
- Proaktive Kommunikation mit externen Partnern (Cloudify, Digital Dynamics)
- Mentoring von zwei Junior-Entwicklern
- Eigeninitiative bei der Einführung von Code-Review-Prozessen

Entwicklungsbereiche:
- Delegation von Aufgaben verbessern
- Präsentationsfähigkeiten für Management-Ebene ausbauen

Zielvereinbarung 2025:
1. Übernahme der technischen Leitung für Projekt Phoenix
2. Kubernetes-Zertifizierung (CKA) bis Q2
3. Mindestens 2 Tech-Talks intern halten
4. Einarbeitung in ML/AI-Grundlagen für neue Produktlinie"""),

    # 14
    ("fortbildung_kubernetes_zertifikat.pdf", "Kubernetes-Zertifikat CKA",
     """Certified Kubernetes Administrator (CKA)
The Linux Foundation

Hiermit wird bestätigt, dass Erik van den Berg die Prüfung zum Certified Kubernetes Administrator erfolgreich bestanden hat.

Prüfungsdatum: 28.09.2024
Ergebnis: Bestanden (Score: 87/100)
Zertifikatsnummer: CKA-2024-DE-004721
Gültig bis: 28.09.2027

Schulung durchgeführt von: Prof. Dr. Anna Richter, Cloud Academy Düsseldorf.
Schulungszeitraum: 02.09.2024 bis 27.09.2024 (4 Wochen, berufsbegleitend).
Arbeitgeber: TechNova GmbH, Düsseldorf."""),

    # 15
    ("fortbildung_cloud_architecture.html", "Schulungsunterlagen Cloud Architecture",
     """TechNova Academy - Cloud Architecture Fundamentals
Kursleiter: Prof. Dr. Anna Richter
Teilnehmer: Erik van den Berg

Modul 1: Cloud-Native Design Principles
- 12-Factor App Methodology
- Microservices vs. Monolith Entscheidungsmatrix
- Container-Orchestrierung mit Kubernetes

Modul 2: Infrastructure as Code
- Terraform Grundlagen und Best Practices
- GitOps-Workflows mit ArgoCD
- Secrets Management mit HashiCorp Vault

Modul 3: Observability
- Distributed Tracing mit Jaeger
- Logging-Strategien für Microservices
- Alerting-Konzepte und Incident Response

Abschlussbewertung: Sehr gut. Empfehlung für CKA-Zertifizierung ausgesprochen."""),

    # 16
    ("rechnung_cloudify_hosting.pdf", "Rechnung Cloudify Hosting Q2/2024",
     """Cloudify Solutions AG
Leopoldstraße 120, 80802 München

Rechnung Nr. CS-2024-0847
Rechnungsdatum: 01.07.2024
Kunde: TechNova GmbH, Musterstraße 42, 40210 Düsseldorf

Leistungszeitraum: 01.04.2024 bis 30.06.2024

Pos 1: Kubernetes Managed Cluster (Production) - 8.500,00 Euro
Pos 2: Kubernetes Managed Cluster (Staging) - 2.500,00 Euro
Pos 3: S3-kompatiblen Object Storage (500 GB) - 750,00 Euro
Pos 4: Dedicated Load Balancer - 750,00 Euro

Nettobetrag: 12.500,00 Euro
USt. 19%: 2.375,00 Euro
Bruttobetrag: 14.875,00 Euro

Zahlungsziel: 30 Tage netto. Bankverbindung: Commerzbank München, IBAN DE12 7004 0048 0123 4567 89"""),

    # 17
    ("rechnung_digital_dynamics_api.pdf", "Rechnung Digital Dynamics API-Lizenz",
     """Digital Dynamics GmbH
Friedrichstraße 191, 10117 Berlin

Rechnung Nr. DD-2024-1203
Rechnungsdatum: 15.03.2024
Kunde: TechNova GmbH, Musterstraße 42, 40210 Düsseldorf

Gegenstand: API-Lizenz Enterprise (Jahreslizenz)
Lizenzzeitraum: 01.04.2024 bis 31.03.2025
Lizenzumfang: Unlimited API Calls, SLA 99,95%, Premium Support

Nettobetrag: 8.900,00 Euro
USt. 19%: 1.691,00 Euro
Bruttobetrag: 10.591,00 Euro

Ansprechpartner: Markus Weber, Key Account Manager, m.weber@digitaldynamics.de
Zahlungsziel: 14 Tage netto"""),

    # 18
    ("angebot_cloudify_migration.docx", "Angebot Cloudify Migration",
     """Cloudify Solutions AG - Migrationsangebot

An: TechNova GmbH, z.Hd. Dr. Sandra Meier
Von: Stefan Brandl, Senior Cloud Architect
Datum: 05.12.2023
Angebotsnummer: CS-ANG-2023-0156

Projektumfang: Vollständige Migration der TechNova Legacy-Infrastruktur auf Kubernetes-basierte Cloud-native Architektur.

Paket 1 - Assessment und Planung: 15.000 Euro
Paket 2 - Migration Core Services (12 Microservices): 45.000 Euro
Paket 3 - Migration Support Services (6 Microservices): 15.000 Euro
Paket 4 - Testing und Optimization: 10.000 Euro

Gesamtangebot: 85.000 Euro netto
Zeitraum: 6 Monate ab Projektstart

Optionaler Wartungsvertrag: 2.500 Euro/Monat für 24/7-Support und Monitoring.
Gültig bis: 31.01.2024."""),

    # 19
    ("reisekostenabrechnung_muenchen.xlsx", "Reisekosten München Cloudify",
     """Reisekostenabrechnung|TechNova GmbH
Mitarbeiter|Erik van den Berg
Reiseziel|München (Cloudify Solutions AG)
Reisezeitraum|12.03.2024 bis 14.03.2024
Anlass|Projekt Aurora Kickoff vor Ort

Position|Betrag
ICE Düsseldorf-München (Hin)|89,90 Euro
ICE München-Düsseldorf (Rück)|89,90 Euro
Hotel Motel One München (2 Nächte)|178,00 Euro
Verpflegungspauschale (2 Tage)|56,00 Euro
Taxi Hbf-Cloudify|32,50 Euro
Taxi Cloudify-Hbf|28,70 Euro

Gesamt|475,00 Euro
Genehmigt durch|Dr. Sandra Meier"""),

    # 20
    ("reisekostenabrechnung_berlin.xlsx", "Reisekosten Berlin Digital Dynamics",
     """Reisekostenabrechnung|TechNova GmbH
Mitarbeiter|Erik van den Berg
Reiseziel|Berlin (Digital Dynamics GmbH Workshop)
Reisezeitraum|22.04.2024 bis 23.04.2024
Anlass|API-Integration Workshop

Position|Betrag
ICE Düsseldorf-Berlin (Hin)|69,90 Euro
ICE Berlin-Düsseldorf (Rück)|69,90 Euro
Hotel Premier Inn Berlin (1 Nacht)|95,00 Euro
Verpflegungspauschale (1 Tag)|28,00 Euro
BVG Tageskarte|8,80 Euro

Gesamt|271,60 Euro
Genehmigt durch|Dr. Sandra Meier"""),

    # 21
    ("praesentation_techstrategie_2025.pptx", "Technologiestrategie 2025",
     """Technologiestrategie 2025 - TechNova GmbH
Vorgestellt von: Dr. Sandra Meier, Abteilungsleiterin Softwareentwicklung
Datum: 05.12.2024

Rückblick 2024:
- Projekt Aurora erfolgreich: Cloud-Migration abgeschlossen
- API-Partnerschaft mit Digital Dynamics GmbH etabliert
- Team gewachsen: 12 auf 15 Entwickler

Strategie 2025:
- Projekt Phoenix: KI-gestützte Produktempfehlungen
- Investition in MLOps-Infrastruktur (Budget: 200.000 Euro)
- Partnerschaften mit 3 weiteren API-Providern geplant
- Ausbau der Kubernetes-Expertise im Team (CKA für 5 Mitarbeiter)

Technologie-Radar:
- Adopt: Kubernetes, GitOps, Terraform
- Trial: Rust für Performance-kritische Services, WebAssembly
- Assess: Large Language Models für interne Tools"""),

    # 22
    ("onboarding_checkliste.html", "Onboarding-Checkliste neue Mitarbeiter",
     """TechNova GmbH - Onboarding-Checkliste IT
Erstellt von: Erik van den Berg, Senior Developer
Letzte Aktualisierung: 15.10.2024

Tag 1 - IT-Setup:
- Laptop abholen bei IT-Service (Raum 2.14)
- VPN-Zugang einrichten (OpenVPN)
- GitLab-Account anlegen
- Slack-Workspace beitreten (technova-dev)
- Email-Konto konfigurieren

Tag 1-3 - Entwicklungsumgebung:
- Docker Desktop installieren
- IDE-Setup (VS Code mit Standardprofil)
- Lokale Kubernetes-Umgebung (Minikube)
- Zugang zu Staging-Cluster bei Cloudify Solutions AG

Woche 1 - Einarbeitung:
- Code-Konventionen lesen (Wiki/Standards)
- Architecture Decision Records durchgehen
- Buddy-Programm mit erfahrenem Entwickler
- Erster eigener PR bis Freitag"""),

    # 23
    ("it_sicherheitsrichtlinie.md", "IT-Sicherheitsrichtlinie TechNova",
     """## IT-Sicherheitsrichtlinie TechNova GmbH
**Version:** 3.1 | **Stand:** 01.01.2024 | **Freigabe:** Geschäftsführung

### 1. Passwörter
- Mindestlänge 12 Zeichen, Komplexitätsanforderungen
- Passwort-Manager verpflichtend (1Password Enterprise)
- MFA für alle externen Zugänge

### 2. Datenklassifizierung
- Öffentlich / Intern / Vertraulich / Streng Vertraulich
- Kundendaten sind mindestens "Vertraulich"
- Quellcode ist "Intern"

### 3. Homeoffice
- VPN-Pflicht für alle Zugriffe auf interne Systeme
- Bildschirmsperre nach 5 Minuten Inaktivität
- Keine Firmendaten auf privaten Geräten

### 4. Incident Response
- Sicherheitsvorfälle sofort an it-security@technova.de
- Kein eigenständiges Bereinigen ohne Rücksprache
- Vierteljährliche Phishing-Simulationen

Verantwortlich: IT-Sicherheitsbeauftragter Markus Wolf"""),

    # 24
    ("vertrag_digital_dynamics_kooperation.pdf", "Kooperationsvertrag Digital Dynamics",
     """Kooperationsvertrag

zwischen TechNova GmbH, Musterstraße 42, 40210 Düsseldorf (nachfolgend "TechNova")
und Digital Dynamics GmbH, Friedrichstraße 191, 10117 Berlin (nachfolgend "Digital Dynamics")

Gegenstand: Strategische Partnerschaft für den bidirektionalen Datenaustausch über die Digital Dynamics Enterprise API. TechNova erhält Zugang zur API-Plattform, Digital Dynamics erhält anonymisierte Nutzungsstatistiken.

Vertragsbeginn: 01.04.2024
Vertragslaufzeit: 24 Monate mit automatischer Verlängerung
Lizenzgebühr: 8.900 Euro/Jahr (siehe separate Rechnung)

Ansprechpartner TechNova: Erik van den Berg (technisch), Dr. Sandra Meier (kaufmännisch)
Ansprechpartner Digital Dynamics: Markus Weber (Key Account)

Geheimhaltung: Beide Parteien verpflichten sich zur Geheimhaltung aller ausgetauschten Daten gemäß beigefügtem NDA.

Unterschrieben am 15.03.2024 in Düsseldorf."""),

    # 25
    ("email_krueger_code_review.txt", "Email Thomas Krüger Code-Review",
     """Von: Thomas Krüger <t.krueger@technova.de>
An: Erik van den Berg <e.vandenberg@technova.de>
Datum: 03.07.2024, 16:45
Betreff: Code-Review Findings Aurora MR #247

Hey Erik,

ich habe deinen Merge Request für den Payment-Service reviewt. Insgesamt sieht das super aus, aber ein paar Punkte:

1. Die Error-Handling-Middleware fängt nicht alle Timeout-Exceptions ab (Zeile 142-155)
2. Die DB-Connection-Pool-Größe ist auf 5 gesetzt - bei dem erwarteten Traffic sollten wir mindestens 20 nehmen
3. Der Health-Check-Endpoint gibt aktuell immer 200 zurück, auch wenn die DB nicht erreichbar ist

Außerdem: Die Unit-Tests für den Retry-Mechanismus fehlen noch. Kannst du die noch nachreichen?

Approven kann ich nach den Fixes sofort.

Grüße, Thomas"""),

    # 26
    ("meeting_notes_retro_q4.md", "Retrospektive Q4 2024",
     """## Sprint-Retrospektive Q4 2024
**Datum:** 20.12.2024
**Moderator:** Erik van den Berg
**Team:** Dr. Sandra Meier, Thomas Krüger, Julia Berger, Markus Wolf, Sarah Klein

### Was lief gut?
- Projekt Aurora erfolgreich live gegangen
- Teamzusammenhalt während der heißen Migrationsphase
- Code-Review-Kultur hat sich deutlich verbessert
- Neue CI/CD-Pipeline spart 40 Minuten pro Deployment

### Was können wir verbessern?
- Dokumentation hinkt dem Code hinterher
- Stand-ups dauern zu lange (15 Min statt 30)
- Onboarding neuer Mitarbeiter sollte strukturierter sein

### Action Items
- Erik: Onboarding-Checkliste überarbeiten (bis 15.01.2025)
- Thomas: ADR-Template einführen (bis 10.01.2025)
- Sandra: Stand-up-Format ändern (ab sofort)"""),

    # 27
    ("stellenbeschreibung_senior_dev.docx", "Stellenbeschreibung Senior Developer",
     """TechNova GmbH - Stellenbeschreibung

Position: Senior Software Developer (m/w/d)
Abteilung: Softwareentwicklung
Vorgesetzte: Dr. Sandra Meier, Abteilungsleiterin
Standort: Düsseldorf (Hybrid: 3 Tage Office, 2 Tage Homeoffice)

Aufgaben:
- Technische Leitung von Entwicklungsprojekten
- Architekturentscheidungen und Code-Reviews
- Mentoring von Junior- und Mid-Level-Entwicklern
- Zusammenarbeit mit externen Partnern (Cloudify Solutions AG, Digital Dynamics GmbH)
- Teilnahme am Architecture Board

Anforderungen:
- Mindestens 5 Jahre Berufserfahrung in Softwareentwicklung
- Sehr gute Kenntnisse in Python und/oder Java
- Erfahrung mit Cloud-Technologien (Kubernetes, AWS/GCP)
- Zertifizierung (CKA, AWS Solutions Architect) von Vorteil

Vergütung: 72.000 - 84.000 Euro brutto p.a., 30 Urlaubstage, Urlaubs- und Weihnachtsgeld"""),

    # 28
    ("zeiterfassung_2024_q3.xlsx", "Zeiterfassung Q3 2024",
     """Arbeitszeiterfassung|Erik van den Berg|Q3 2024
Monat|Soll-Stunden|Ist-Stunden|Überstunden|Urlaub
Juli 2024|176|182|6|0 Tage
August 2024|176|80|0|10 Tage Urlaub (Mallorca)
September 2024|168|175|7|0 Tage

Quartal gesamt|520|437|13|10 Tage
Überstundenkonto|Stand 30.09.2024|27 Stunden
Resturlaub|Stand 30.09.2024|12 Tage

Genehmigt durch|Dr. Sandra Meier|01.10.2024"""),

    # 29
    ("homeoffice_vereinbarung.pdf", "Homeoffice-Vereinbarung",
     """Vereinbarung über mobiles Arbeiten (Homeoffice)

zwischen TechNova GmbH, Musterstraße 42, 40210 Düsseldorf und Erik van den Berg, Am Stirkenbend 20, 40489 Düsseldorf

Regelung: Der Arbeitnehmer arbeitet an bis zu 3 Tagen pro Woche von seinem häuslichen Arbeitsplatz. An mindestens 2 Tagen pro Woche ist Anwesenheit am Standort Düsseldorf erforderlich.

Ausstattung: Der Arbeitgeber stellt bereit: Laptop (Dell XPS 15), externen Monitor (27 Zoll), Dockingstation, Headset. Der Arbeitnehmer stellt einen geeigneten Arbeitsplatz sicher.

Erreichbarkeit: Kernarbeitszeit 09:00-15:00 Uhr, Erreichbarkeit über Slack und Teams.

Datenschutz: Es gelten die Regelungen der IT-Sicherheitsrichtlinie. VPN-Pflicht für alle Zugriffe.

Gültig ab: 01.04.2021, kündbar mit 4 Wochen zum Monatsende. Unterschrieben von Petra Hoffmann (HR) und Erik van den Berg."""),

    # 30
    ("inventarliste_hardware.xlsx", "Hardware-Inventar Erik van den Berg",
     """IT-Inventarliste|TechNova GmbH|Erik van den Berg
Inventarnummer|Gerät|Modell|Seriennummer|Standort
INV-2021-0891|Laptop|Dell XPS 15 9520|DL-SN-4721983|Homeoffice
INV-2021-0892|Monitor|Dell U2722D 27 Zoll|DL-MN-8834521|Homeoffice
INV-2021-0893|Dockingstation|Dell WD19TBS|DL-DS-2234891|Homeoffice
INV-2023-0445|Monitor|Dell U2722D 27 Zoll|DL-MN-9912345|Büro Raum 3.07
INV-2024-0112|Headset|Jabra Evolve2 85|JB-HS-5567234|Homeoffice

Letzte Inventur|15.01.2024
Verantwortlich|IT-Service TechNova"""),

    # 31
    ("email_hoffmann_krankmeldung.txt", "Email Petra Hoffmann Krankmeldung",
     """Von: Erik van den Berg <e.vandenberg@technova.de>
An: Petra Hoffmann <p.hoffmann@technova.de>
CC: Dr. Sandra Meier <s.meier@technova.de>
Datum: 12.11.2024, 07:45
Betreff: Krankmeldung 12.-14.11.2024

Liebe Frau Hoffmann,

leider muss ich mich für heute und voraussichtlich bis einschließlich Donnerstag, 14.11.2024, krank melden. Ich habe einen grippalen Infekt und war gestern bei Dr. Klaus Weber (Hausarzt). Die AU-Bescheinigung reiche ich nach.

Meine laufenden Aufgaben hat Thomas Krüger übernommen. Dringende Anfragen bitte an ihn weiterleiten.

Mit freundlichen Grüßen,
Erik van den Berg"""),

    # 32
    ("schulungsplan_2025.html", "Schulungsplan 2025 TechNova",
     """TechNova GmbH - Schulungsplan 2025
Abteilung Softwareentwicklung, erstellt von Dr. Sandra Meier

Budget pro Mitarbeiter: 3.000 Euro, Gesamtbudget Abteilung: 45.000 Euro (15 Mitarbeiter)

Q1 2025:
- Kubernetes Advanced (3 Tage): Erik van den Berg, Thomas Krüger
- Python Performance Optimization (2 Tage): Julia Berger, Sarah Klein
- Anbieter: Cloud Academy Düsseldorf (Prof. Dr. Anna Richter)

Q2 2025:
- CKA-Zertifizierung: Thomas Krüger, Markus Wolf
- Security Fundamentals OWASP (2 Tage): gesamtes Team
- Anbieter: Extern (wird noch ausgeschrieben)

Q3 2025:
- ML/AI Grundlagen für Entwickler (5 Tage): Erik van den Berg, 2 weitere
- Anbieter: TechNova Academy intern

Q4 2025:
- Konferenzbesuche: KubeCon Europe, PyCon DE
- Freie Wahl: Jeder Mitarbeiter 1 Schulung nach Interesse"""),

    # 33
    ("protokoll_betriebsversammlung.docx", "Betriebsversammlung 15.12.2024",
     """Protokoll der Betriebsversammlung TechNova GmbH
Datum: 15.12.2024, 15:00-17:00 Uhr
Ort: Kantine, Musterstraße 42, Düsseldorf
Anwesend: 87 von 120 Mitarbeitern

Tagesordnung:
1. Jahresrückblick 2024 (Geschäftsführung)
2. Finanzbericht (CFO Markus Hahn): Umsatz 18,5 Mio Euro (+12%), EBIT 2,1 Mio Euro
3. Personalentwicklung: 15 Neueinstellungen, Fluktuation 8%
4. Projekt Aurora Erfolg: Vorgestellt durch Erik van den Berg
5. Ausblick 2025: Projekt Phoenix, neue Geschäftsfelder KI

Beschlüsse:
- Inflationsausgleichsprämie 1.500 Euro für alle Mitarbeiter
- Einführung 4-Tage-Woche Pilotprojekt ab Q2 2025
- Neuer Pausenraum wird bis März 2025 eingerichtet

Nächste Betriebsversammlung: Juni 2025"""),

    # 34
    ("nda_cloudify_solutions.pdf", "NDA Cloudify Solutions AG",
     """Geheimhaltungsvereinbarung (NDA)

zwischen TechNova GmbH, Musterstraße 42, 40210 Düsseldorf und Cloudify Solutions AG, Leopoldstraße 120, 80802 München

Die Parteien vereinbaren, alle im Rahmen der Zusammenarbeit (Projekt Aurora) ausgetauschten vertraulichen Informationen geheim zu halten.

Vertrauliche Informationen umfassen: Quellcode, Architekturdiagramme, Kundendaten, Geschäftszahlen, technische Spezifikationen, Zugangsdaten.

Ausnahmen: Öffentlich bekannte Informationen, eigenständig entwickelte Informationen, behördlich angeordnete Offenlegung.

Laufzeit: 36 Monate ab Unterzeichnung (01.01.2024). Vertragsstrafe bei Verstoß: 50.000 Euro.

Unterschrieben am 20.12.2023.
TechNova: Dr. Sandra Meier, Abteilungsleiterin
Cloudify: Stefan Brandl, Senior Cloud Architect"""),

    # 35
    ("dienstreiseantrag_hamburg.md", "Dienstreiseantrag Hamburg",
     """## Dienstreiseantrag
**Antragsteller:** Erik van den Berg
**Datum des Antrags:** 15.01.2025

### Reisedetails
- **Ziel:** Hamburg, Kundentermin bei NordTech Solutions
- **Zeitraum:** 03.02.2025 bis 04.02.2025 (2 Tage)
- **Anlass:** Evaluierung einer möglichen API-Partnerschaft für Projekt Phoenix

### Geschätzte Kosten
- Bahnfahrt (ICE Hin/Rück): 160,00 Euro
- Hotel (1 Nacht): 120,00 Euro
- Verpflegungspauschale: 28,00 Euro
- ÖPNV vor Ort: 15,00 Euro
- **Gesamt: ca. 323,00 Euro**

### Genehmigung
- Vorgesetzte: Dr. Sandra Meier
- Status: Genehmigt am 17.01.2025"""),
]
