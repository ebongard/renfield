# Konversations-API Dokumentation

Erweiterte API-Endpoints für Konversations-Management und Persistenz.

## Übersicht

Die Konversations-API bietet vollständige Persistenz für Chat-Verläufe mit PostgreSQL als Backend. Alle Nachrichten werden automatisch gespeichert und können später abgerufen werden.

## Neue API-Endpoints

### 1. Liste aller Konversationen

**Endpoint:** `GET /api/chat/conversations`

Ruft eine Liste aller gespeicherten Konversationen ab.

**Parameter:**
- `limit` (optional, default: 50) - Maximale Anzahl Ergebnisse
- `offset` (optional, default: 0) - Offset für Pagination

**Beispiel:**
```bash
curl http://localhost:8000/api/chat/conversations?limit=10&offset=0
```

**Response:**
```json
{
  "conversations": [
    {
      "session_id": "session-1234567890-abc",
      "created_at": "2024-01-15T10:30:00",
      "updated_at": "2024-01-15T11:45:00",
      "message_count": 15,
      "preview": "Wie ist das Wetter heute?"
    }
  ],
  "limit": 10,
  "offset": 0,
  "count": 1
}
```

---

### 2. Konversations-Zusammenfassung

**Endpoint:** `GET /api/chat/conversation/{session_id}/summary`

Ruft eine Zusammenfassung einer spezifischen Konversation ab.

**Beispiel:**
```bash
curl http://localhost:8000/api/chat/conversation/session-1234567890-abc/summary
```

**Response:**
```json
{
  "session_id": "session-1234567890-abc",
  "created_at": "2024-01-15T10:30:00",
  "updated_at": "2024-01-15T11:45:00",
  "message_count": 15,
  "first_message": "Wie ist das Wetter heute?",
  "last_message": "Danke für die Hilfe!"
}
```

---

### 3. Chat-Historie (bereits existiert, erweitert)

**Endpoint:** `GET /api/chat/history/{session_id}`

Ruft die komplette Chat-Historie einer Konversation ab.

**Parameter:**
- `limit` (optional, default: 50) - Maximale Anzahl Nachrichten

**Beispiel:**
```bash
curl http://localhost:8000/api/chat/history/session-1234567890-abc?limit=20
```

**Response:**
```json
{
  "messages": [
    {
      "role": "user",
      "content": "Wie ist das Wetter?",
      "timestamp": "2024-01-15T10:30:00",
      "metadata": {
        "intent": {
          "intent": "general.conversation",
          "confidence": 0.95
        }
      }
    },
    {
      "role": "assistant",
      "content": "Das aktuelle Wetter ist sonnig mit 20°C.",
      "timestamp": "2024-01-15T10:30:05",
      "metadata": null
    }
  ]
}
```

---

### 4. Konversationen durchsuchen

**Endpoint:** `GET /api/chat/search`

Sucht in allen Konversationen nach einem bestimmten Text.

**Parameter:**
- `q` (erforderlich) - Suchbegriff (mindestens 2 Zeichen)
- `limit` (optional, default: 20) - Maximale Anzahl Ergebnisse

**Beispiel:**
```bash
curl "http://localhost:8000/api/chat/search?q=wetter&limit=10"
```

**Response:**
```json
{
  "query": "wetter",
  "results": [
    {
      "session_id": "session-1234567890-abc",
      "created_at": "2024-01-15T10:30:00",
      "updated_at": "2024-01-15T11:45:00",
      "matching_messages": [
        {
          "role": "user",
          "content": "Wie ist das Wetter heute?",
          "timestamp": "2024-01-15T10:30:00"
        }
      ]
    }
  ],
  "count": 1
}
```

---

### 5. Konversations-Statistiken

**Endpoint:** `GET /api/chat/stats`

Ruft globale Statistiken über alle Konversationen ab.

**Beispiel:**
```bash
curl http://localhost:8000/api/chat/stats
```

**Response:**
```json
{
  "total_conversations": 127,
  "total_messages": 1543,
  "avg_messages_per_conversation": 12.15,
  "messages_last_24h": 89,
  "latest_activity": "2024-01-15T11:45:00"
}
```

---

### 6. Session löschen

**Endpoint:** `DELETE /api/chat/session/{session_id}`

Löscht eine spezifische Chat-Session und alle zugehörigen Nachrichten.

**Beispiel:**
```bash
curl -X DELETE http://localhost:8000/api/chat/session/session-1234567890-abc
```

**Response:**
```json
{
  "success": true
}
```

---

### 7. Alte Konversationen aufräumen

**Endpoint:** `DELETE /api/chat/conversations/cleanup`

Löscht automatisch alle Konversationen, die älter als X Tage sind.

**Parameter:**
- `days` (optional, default: 30) - Alter in Tagen

**Beispiel:**
```bash
curl -X DELETE "http://localhost:8000/api/chat/conversations/cleanup?days=60"
```

**Response:**
```json
{
  "success": true,
  "deleted_count": 23,
  "cutoff_days": 60
}
```

---

## OllamaService - Neue Methoden

Der `OllamaService` wurde erweitert mit folgenden Methoden:

### `load_conversation_context(session_id, db, max_messages=20)`
Lädt den Konversationskontext aus der Datenbank.

**Parameter:**
- `session_id`: String - Session-ID der Konversation
- `db`: AsyncSession - Datenbank-Session
- `max_messages`: int - Maximale Anzahl zu ladender Nachrichten

**Returns:** `List[Dict]` - Liste von Nachrichten im Format `{"role": "user/assistant", "content": "..."}`

**Beispiel:**
```python
from services.ollama_service import OllamaService
from services.database import get_db

async def example():
    ollama = OllamaService()
    async with get_db() as db:
        context = await ollama.load_conversation_context(
            session_id="session-123",
            db=db,
            max_messages=10
        )
        print(f"Geladen: {len(context)} Nachrichten")
```

---

### `save_message(session_id, role, content, db, metadata=None)`
Speichert eine einzelne Nachricht in der Datenbank.

**Parameter:**
- `session_id`: String - Session-ID
- `role`: String - "user" oder "assistant"
- `content`: String - Nachrichteninhalt
- `db`: AsyncSession - Datenbank-Session
- `metadata`: Dict (optional) - Zusätzliche Metadaten (z.B. Intent-Info)

**Returns:** `Message` - Das gespeicherte Message-Objekt

---

### `get_conversation_summary(session_id, db)`
Ruft eine Zusammenfassung einer Konversation ab.

**Returns:** `Dict` oder `None`

---

### `delete_conversation(session_id, db)`
Löscht eine komplette Konversation.

**Returns:** `bool` - True wenn erfolgreich

---

### `get_all_conversations(db, limit=50, offset=0)`
Ruft Liste aller Konversationen ab.

**Returns:** `List[Dict]` - Liste von Konversations-Zusammenfassungen

---

### `search_conversations(query, db, limit=20)`
Sucht in Konversationen nach Text.

**Returns:** `List[Dict]` - Liste von Suchergebnissen

---

## Datenbank-Schema

### Conversation Model
```python
class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    session_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")
```

### Message Model
```python
class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"))
    role = Column(String)  # 'user' oder 'assistant'
    content = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)
    message_metadata = Column(JSON, nullable=True)

    conversation = relationship("Conversation", back_populates="messages")
```

---

## Verwendungsbeispiele

### Python (Backend-Integration)

```python
from services.ollama_service import OllamaService
from services.database import get_db

async def chat_with_history():
    ollama = OllamaService()
    session_id = "my-session-123"

    async with get_db() as db:
        # Lade vorherigen Kontext
        context = await ollama.load_conversation_context(session_id, db)

        # Neue Nachricht
        user_message = "Wie ist das Wetter?"

        # Speichere User-Nachricht
        await ollama.save_message(session_id, "user", user_message, db)

        # Generiere Antwort mit Kontext
        response = await ollama.chat(user_message, context)

        # Speichere Assistant-Antwort
        await ollama.save_message(session_id, "assistant", response, db)

        print(f"Antwort: {response}")
```

### JavaScript/React (Frontend)

```javascript
// Liste aller Konversationen abrufen
const conversations = await apiClient.get('/api/chat/conversations');
console.log(conversations.data);

// Spezifische Historie laden
const history = await apiClient.get(`/api/chat/history/${sessionId}`);
console.log(history.data.messages);

// Suche durchführen
const results = await apiClient.get('/api/chat/search', {
  params: { q: 'wetter' }
});
console.log(results.data.results);

// Statistiken abrufen
const stats = await apiClient.get('/api/chat/stats');
console.log(`Gesamt: ${stats.data.total_conversations} Konversationen`);
```

---

## Performance-Optimierungen

### Indizes
Die Datenbank nutzt folgende Indizes für schnelle Abfragen:
- `session_id` (unique index auf Conversation)
- `conversation_id` (foreign key index auf Message)
- `timestamp` (für zeitbasierte Sortierung)

### Pagination
Alle Listen-Endpoints unterstützen `limit` und `offset` Parameter für effiziente Pagination bei großen Datenmengen.

### Kaskadierendes Löschen
Beim Löschen einer Conversation werden automatisch alle zugehörigen Messages gelöscht (`cascade="all, delete-orphan"`).

---

## Wartung

### Regelmäßiges Cleanup
Es wird empfohlen, den Cleanup-Endpoint regelmäßig (z.B. täglich via Cron) aufzurufen:

```bash
# Täglich um 3 Uhr morgens
0 3 * * * curl -X DELETE "http://localhost:8000/api/chat/conversations/cleanup?days=90"
```

### Backup
Erstelle regelmäßige Backups der PostgreSQL-Datenbank:

```bash
docker exec renfield-postgres pg_dump -U renfield renfield > backup-$(date +%Y%m%d).sql
```

---

## Troubleshooting

### Problem: "Konversation nicht gefunden"
- Überprüfe, ob die `session_id` korrekt ist
- Stelle sicher, dass die Datenbank-Verbindung funktioniert

### Problem: Langsame Abfragen bei vielen Konversationen
- Nutze Pagination mit `limit` und `offset`
- Führe regelmäßig Cleanup durch
- Erstelle zusätzliche Indizes falls nötig

### Problem: "Database connection error"
- Überprüfe PostgreSQL-Status: `docker ps | grep postgres`
- Check Logs: `docker logs renfield-postgres`
- Überprüfe Connection-String in `.env`

---

## Nächste Schritte

Mögliche Erweiterungen:
- **Tags/Labels**: Konversationen mit Tags versehen
- **Export**: Export zu JSON/CSV
- **Analytics**: Detaillierte Nutzungsstatistiken
- **Context Window Management**: Intelligentes Kürzen zu langer Kontexte
- **Zusammenfassungen**: Automatische Zusammenfassungen langer Konversationen
- **Multi-User Support**: User-IDs für Konversationen
