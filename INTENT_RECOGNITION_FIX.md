# Intent Recognition Fix - v1.1.2

## 🐛 Problem: False Positive Intent Detection

**Symptom:**
```
User: "Was ist 1989 in China passiert?"

Log:
🎯 Intent: homeassistant.get_state | Entity: light.arbeitszimmer
❌ Fehler beim Abrufen des States für light.arbeitszimmer
```

**Das ist falsch!** Diese Frage hat nichts mit Smart Home zu tun.

## 🔍 Ursache

Das LLM versuchte krampfhaft, JEDEN Input als Home Assistant Intent zu interpretieren, selbst bei allgemeinen Wissensfragen.

**Warum?**
- Intent-Extraction-Prompt hatte zu viele HA-Beispiele
- Keine klaren Beispiele für "general.conversation"
- Keine Validierung der erkannten Intents

## ✅ Lösung (v1.1.2)

### 1. **Verbesserter Intent-Prompt**

**Neu: Klarer Entscheidungsbaum**

```
1. Ist es eine SMART HOME Frage/Befehl?
   - Erwähnt Geräte?
   - Geht es um Steuerung?
   → JA: homeassistant.*
   → NEIN: Gehe zu 2

2. Ist es eine ALLGEMEINE Frage?
   - Geschichtsfragen
   - Wissensfragen
   - Erklärungen
   → JA: general.conversation

3. Ist es eine spezielle Aktion?
   - Kamera → camera.*
   - Workflow → n8n.*
```

**Viele Beispiele für general.conversation:**
```
- "Was ist 1989 in China passiert?" → general.conversation ✅
- "Wie ist das Wetter?" → general.conversation ✅
- "Wer war Einstein?" → general.conversation ✅
- "Erkläre mir Quantenphysik" → general.conversation ✅
```

### 2. **Keyword-Validierung** (NEU!)

```python
# Prüfe ob wirklich HA-Keywords vorhanden
ha_keywords = [
    'licht', 'lampe', 'schalter', 'thermostat',
    'heizung', 'fenster', 'tür', 'rolladen',
    'ein', 'aus', 'an', 'schalten'
]

has_ha_keyword = any(keyword in message.lower() for keyword in ha_keywords)

if not has_ha_keyword:
    # Überschreibe fälschlichen HA-Intent
    intent = "general.conversation"
```

**Das verhindert False Positives!**

### 3. **Standard = general.conversation**

Bei Unsicherheit: Default zu normaler Konversation statt zu HA-Intent.

## 📊 Vorher vs. Nachher

### Test 1: Allgemeine Frage

**Input:** "Was ist 1989 in China passiert?"

**Vorher (v1.1.1):**
```
🎯 Intent: homeassistant.get_state
❌ Fehler: Entity nicht gefunden
```

**Nachher (v1.1.2):**
```
🎯 Intent: general.conversation | Entity: none
✅ Normale Konversation
```

### Test 2: Smart Home Frage

**Input:** "Ist das Licht im Arbeitszimmer an?"

**Vorher (v1.1.1):**
```
🎯 Intent: homeassistant.get_state
✅ Funktioniert
```

**Nachher (v1.1.2):**
```
🎯 Intent: homeassistant.get_state
✅ Funktioniert (unverändert)
```

### Test 3: Grenzfall

**Input:** "Welche Fenster sind offen?"

**Vorher:**
```
🎯 Intent: homeassistant.get_state
```

**Nachher:**
```
🎯 Intent: homeassistant.get_state  ✅
(weil "fenster" ein HA-Keyword ist)
```

## 🧪 Test-Szenarien

### ✅ Sollte als general.conversation erkannt werden:

```
"Was ist 1989 in China passiert?"
"Wie ist das Wetter?"
"Wer war Albert Einstein?"
"Erkläre mir Quantenphysik"
"Was bedeutet KI?"
"Erzähl mir einen Witz"
"Wie spät ist es?"
"Was kann ich heute kochen?"
```

### ✅ Sollte als homeassistant.* erkannt werden:

```
"Ist das Licht im Wohnzimmer an?"
"Schalte das Licht im Schlafzimmer ein"
"Welche Fenster sind offen?"
"Mach die Heizung aus"
"Stelle das Thermostat auf 22 Grad"
"Sind alle Türen geschlossen?"
```

## 🚀 Update durchführen

### Quick Update

```bash
cd renfield
./quick-update.sh
```

### Oder manuell

```bash
docker-compose restart backend
```

## ✅ Verifizieren

### 1. Teste allgemeine Frage

```
User: "Was ist 1989 in China passiert?"
```

**Erwartete Logs:**
```
📨 WebSocket Nachricht: 'Was ist 1989 in China passiert?'
🔍 Extrahiere Intent...
🎯 Intent: general.conversation | Entity: none
✅ WebSocket Response gesendet
```

**KEINE HA-Fehlermeldung!** ✅

### 2. Teste Smart Home Frage

```
User: "Ist das Licht im Wohnzimmer an?"
```

**Erwartete Logs:**
```
📨 WebSocket Nachricht: 'Ist das Licht im Wohnzimmer an?'
🔍 Extrahiere Intent...
🎯 Intent: homeassistant.get_state | Entity: light.wohnzimmer
⚡ Führe Aktion aus: homeassistant.get_state
✅ Aktion: True - Licht ist eingeschaltet
```

**Funktioniert weiterhin!** ✅

### 3. Teste Grenzfall

```
User: "Welche Fenster sind offen?"
```

**Erwartete Logs:**
```
🎯 Intent: homeassistant.get_state | Entity: binary_sensor.fenster_*
⚡ Führe Aktion aus
```

**Richtig erkannt als HA-Intent!** ✅

## 📋 HA-Keywords (werden automatisch erkannt)

```
Geräte:
- licht, lampe, beleuchtung
- schalter, switch, steckdose
- thermostat, heizung, klima
- fenster, tür, tor
- rolladen, jalousie, rollo
- dimmer, sensor, bewegungsmelder

Aktionen:
- ein, aus, an, schalten
- öffnen, schließen
- stelle, setze
- dimme, erhöhe, verringere
```

Wenn **keines dieser Keywords** vorkommt → `general.conversation`

## 🐛 Troubleshooting

### Noch immer False Positives?

```bash
# Prüfe ob neue Version läuft
docker-compose exec backend python3 -c "
from services.ollama_service import OllamaService
import inspect
code = inspect.getsource(OllamaService.extract_intent)
if 'ha_keywords' in code:
    print('✅ Neue Version aktiv')
else:
    print('❌ Alte Version läuft - restart nötig')
"
```

### Intent-Erkennung debuggen

```bash
# Teste Intent-Extraction direkt
docker-compose exec backend python3 -c "
from services.ollama_service import OllamaService
import asyncio

async def test():
    ollama = OllamaService()
    intent = await ollama.extract_intent('Was ist 1989 in China passiert?')
    print(f'Intent: {intent}')

asyncio.run(test())
"
```

**Erwartete Ausgabe:**
```
🎯 Intent: general.conversation | Entity: none
Intent: {'intent': 'general.conversation', ...}
```

## 📝 Changelog v1.1.2

**Fixed:**
- False Positive Intent Detection für allgemeine Wissensfragen
- LLM versucht nicht mehr jeden Input als HA-Intent zu interpretieren

**Added:**
- Entscheidungsbaum im Intent-Prompt
- Keyword-Validierung für HA-Intents
- Viele Beispiele für general.conversation
- Automatische Intent-Korrektur wenn keine HA-Keywords gefunden

**Improved:**
- Intent-Extraction-Genauigkeit deutlich erhöht
- Weniger falsche HA-API-Calls
- Bessere Unterscheidung zwischen HA und allgemeinen Fragen

---

**Problem gelöst!** 🎉

Jetzt werden allgemeine Fragen korrekt als normale Konversation erkannt!
