# Frontend Fixes

## Problem: Empty Reply from Frontend

### Symptom
```bash
curl http://localhost:3000
# Empty reply from server
```

### Mögliche Ursachen

1. **Fehlende postcss.config.js** ✅ BEHOBEN
   - Tailwind CSS benötigt PostCSS
   - Ohne diese Datei crashed der Vite Build

2. **Node Module Installation**
   - Dependencies werden beim ersten Start installiert
   - Kann 2-5 Minuten dauern

3. **Port-Binding Problem**
   - Container läuft, aber Port ist nicht gemappt
   - Oder andere App nutzt Port 3000

## ✅ Behobene Probleme

### 1. postcss.config.js hinzugefügt
```javascript
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
```

### 2. API URL Fix
- HomePage.jsx nutzt jetzt explizite API URL
- Verhindert CORS-Probleme

### 3. .gitignore für Frontend
- Verhindert dass node_modules committed werden

## 🔍 Debug-Schritte

### 1. Container-Status prüfen
```bash
docker-compose ps
# frontend sollte "Up" sein
```

### 2. Frontend-Logs prüfen
```bash
docker-compose logs -f frontend
```

**Erwartete Ausgabe:**
```
VITE v5.x.x ready in XXX ms
➜  Local:   http://localhost:3000/
➜  Network: use --host to expose
```

### 3. In Container schauen
```bash
docker-compose exec frontend sh
ls -la
# Sollte node_modules/ haben
```

### 4. Debug-Script nutzen
```bash
chmod +x debug.sh
./debug.sh
```

## 🛠️ Lösungen

### Lösung 1: Frontend neu bauen
```bash
docker-compose down
docker-compose build --no-cache frontend
docker-compose up -d
```

### Lösung 2: Node Modules neu installieren
```bash
docker-compose exec frontend npm install
docker-compose restart frontend
```

### Lösung 3: Port ändern (falls 3000 belegt)
```yaml
# docker-compose.yml
frontend:
  ports:
    - "3001:3000"  # Nutze 3001 statt 3000
```

### Lösung 4: Alle Container neu starten
```bash
docker-compose restart
```

## 📊 Typische Fehler in Logs

### Fehler: "Cannot find module"
```bash
# Lösung:
docker-compose exec frontend npm install
docker-compose restart frontend
```

### Fehler: "EADDRINUSE :::3000"
```bash
# Port ist belegt
# Lösung: Anderen Port nutzen oder blockierende App stoppen
lsof -ti:3000 | xargs kill -9  # macOS/Linux
```

### Fehler: "postcss plugin not found"
```bash
# Lösung: postcss.config.js war fehlend (jetzt behoben)
```

## ✅ Erfolgreicher Start

Nach erfolgreichem Start solltest du sehen:

**Container Status:**
```
renfield-frontend   Up   0.0.0.0:3000->3000/tcp
```

**Logs:**
```
VITE v5.0.11 ready in 234 ms
➜  Local:   http://localhost:3000/
```

**curl Test:**
```bash
curl http://localhost:3000
# Sollte HTML zurückgeben
```

**Browser:**
```
http://localhost:3000
# Sollte die Renfield UI zeigen
```

## 🚀 Wenn alles funktioniert

Du solltest sehen:
- ✅ Renfield Logo
- ✅ Navigation (Home, Chat, Aufgaben, etc.)
- ✅ System Status (Online/Offline)
- ✅ Feature-Cards

## 📞 Immer noch Probleme?

1. **Vollständiger Neustart:**
```bash
docker-compose down -v  # ⚠️ Löscht Volumes!
docker-compose up --build
```

2. **Logs komplett anzeigen:**
```bash
docker-compose logs frontend > frontend.log
# Dann frontend.log prüfen
```

3. **Manuell in Container:**
```bash
docker-compose exec frontend sh
node --version  # Sollte v20.x sein
npm --version   # Sollte 10.x sein
ls node_modules # Sollte viele Packages zeigen
```

## 💡 Tipp

Der erste Start dauert länger (npm install), nachfolgende Starts sind schneller wegen Docker Layer Caching.

**Erste Start:** 3-5 Minuten
**Nachfolgende Starts:** 10-30 Sekunden

---

**Alle Frontend-Probleme sollten jetzt behoben sein!** 🎉
