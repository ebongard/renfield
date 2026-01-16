# GitHub Deployment Anleitung

Diese Anleitung zeigt dir, wie du Renfield in dein GitHub Repository hochlädst.

## 🎯 Drei Methoden

### Methode 1: Deploy Script (Empfohlen) ⚡

**Am einfachsten und schnellsten!**

1. **Repository herunterladen**
   - Lade den kompletten `renfield` Ordner herunter

2. **In Terminal/CMD öffnen**
   ```bash
   cd /pfad/zu/renfield
   ```

3. **Script ausführbar machen**
   ```bash
   chmod +x deploy.sh
   ```

4. **Deployen**
   ```bash
   ./deploy.sh
   ```

5. **Bei Aufforderung authentifizieren**
   - Wenn GitHub nach Credentials fragt:
   - Username: `ebongard`
   - Password: Dein **Personal Access Token** (nicht Passwort!)

**Fertig!** 🎉

---

### Methode 2: GitHub Desktop (Grafisch) 🖱️

**Für visuelle Nutzer ohne Terminal-Kenntnisse**

1. **GitHub Desktop installieren**
   - Download: https://desktop.github.com/

2. **Repository erstellen**
   - File → Add Local Repository
   - Wähle den `renfield` Ordner
   - Wenn "nicht gefunden": Create Repository

3. **Remote hinzufügen**
   - Repository → Repository Settings
   - Primary remote repository: `https://github.com/ebongard/renfield`

4. **Committen**
   - Links alle Dateien markieren
   - Commit message eingeben
   - "Commit to main"

5. **Pushen**
   - "Push origin" Button oben

**Fertig!** 🎉

---

### Methode 3: Manuell mit Git (Fortgeschritten) 💻

**Für Entwickler die volle Kontrolle möchten**

1. **Terminal öffnen**
   ```bash
   cd /pfad/zu/renfield
   ```

2. **Git initialisieren (falls nötig)**
   ```bash
   git init
   ```

3. **Remote hinzufügen**
   ```bash
   git remote add origin https://github.com/ebongard/renfield.git
   ```

4. **Dateien hinzufügen**
   ```bash
   git add .
   ```

5. **Committen**
   ```bash
   git commit -m "Initial commit: Complete Renfield AI Assistant"
   ```

6. **Branch setzen**
   ```bash
   git branch -M main
   ```

7. **Pushen**
   ```bash
   git push -u origin main
   ```

**Fertig!** 🎉

---

## 🔑 GitHub Personal Access Token erstellen

Falls du noch keinen Token hast:

1. Gehe zu GitHub.com → Settings
2. Developer settings → Personal access tokens → Tokens (classic)
3. "Generate new token (classic)"
4. Name: `renfield-deploy`
5. Scopes auswählen:
   - ✅ `repo` (full control)
6. Generate token
7. **Token kopieren** (nur einmal sichtbar!)
8. Verwende Token als Passwort beim Git Push

---

## 📁 Was wird hochgeladen?

### Backend
```
backend/
├── Dockerfile
├── requirements.txt
├── main.py
├── api/routes/
├── services/
├── integrations/
├── models/
└── utils/
```

### Frontend
```
frontend/
├── Dockerfile
├── package.json
├── src/
│   ├── components/
│   ├── pages/
│   └── utils/
└── public/
```

### Konfiguration
```
├── docker-compose.yml
├── .env.example
├── .gitignore
└── config/
```

### Dokumentation
```
├── README.md
├── INSTALLATION.md
├── FEATURES.md
├── PROJECT_OVERVIEW.md
├── QUICKSTART.md
└── GITHUB_DEPLOYMENT.md
```

### Scripts
```
├── start.sh
├── update.sh
└── deploy.sh
```

---

## ⚠️ Wichtig: .env Datei

Die `.env` Datei mit deinen echten Credentials wird **NICHT** hochgeladen!

Im Repository ist nur `.env.example` - das ist gut so! 🔒

---

## 🔍 Verifizierung

Nach dem Push:

1. Gehe zu: https://github.com/ebongard/renfield
2. Du solltest alle Dateien sehen
3. README.md wird automatisch angezeigt

---

## 🐛 Troubleshooting

### "Permission denied"
```bash
chmod +x deploy.sh
```

### "Remote already exists"
```bash
git remote remove origin
git remote add origin https://github.com/ebongard/renfield.git
```

### "Authentication failed"
- Verwende **Personal Access Token** statt Passwort
- Token hat `repo` Berechtigung?

### "Repository not found"
- Existiert https://github.com/ebongard/renfield schon?
- Falls ja: `git pull origin main --allow-unrelated-histories`
- Falls nein: Erstelle Repository auf GitHub zuerst

### "Refusing to merge unrelated histories"
```bash
git pull origin main --allow-unrelated-histories
git push -u origin main
```

---

## 📝 Repository auf GitHub erstellen

Falls das Repository noch nicht existiert:

1. Gehe zu: https://github.com/new
2. Repository name: `renfield`
3. Description: `Vollständig offline-fähiger KI-Assistent für Smart Home`
4. Public oder Private wählen
5. **NICHT** initialisieren mit README/License (wir haben schon alles)
6. Create repository
7. Dann deploy.sh ausführen

---

## 🎨 GitHub Repository Features

Nach dem Upload kannst du aktivieren:

### GitHub Pages (für Dokumentation)
- Settings → Pages
- Source: Deploy from branch `main`
- Folder: `/docs` (wenn gewünscht)

### Issues & Projects
- Settings → Features
- ✅ Issues aktivieren
- ✅ Projects aktivieren

### GitHub Actions (CI/CD)
- Workflow für Docker Build
- Automatische Tests
- Release-Automation

---

## 🚀 Nächste Schritte nach Upload

1. **README Badge hinzufügen**
   - Docker Pulls
   - License Badge
   - Build Status

2. **Topics hinzufügen**
   - Tags: `ai`, `smart-home`, `home-assistant`, `offline`, `llm`

3. **License wählen**
   - Empfehlung: MIT License

4. **Contributors Guide**
   - CONTRIBUTING.md erstellen

5. **Release erstellen**
   - v1.0.0 Release Tag

---

## ✅ Checkliste vor dem Push

- [ ] .env.example vorhanden (ohne echte Credentials)
- [ ] .gitignore konfiguriert
- [ ] README.md vollständig
- [ ] Alle Scripts ausführbar (chmod +x)
- [ ] Keine sensitiven Daten im Code
- [ ] Docker Compose tested
- [ ] Dokumentation vollständig

---

## 📞 Hilfe

Bei Problemen:
- GitHub Docs: https://docs.github.com/
- Git Docs: https://git-scm.com/doc

---

**Viel Erfolg beim Deployen!** 🎉

Nach erfolgreichem Push ist dein Projekt öffentlich verfügbar (oder privat, je nach Einstellung) und andere können es nutzen oder dazu beitragen!
