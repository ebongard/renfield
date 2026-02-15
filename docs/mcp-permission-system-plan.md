# Plan: MCP Permission System (Hybrid: Konvention + YAML)

## Context

Renfield hat aktuell **keine Permission-Kontrolle für MCP-Tools**. Jeder authentifizierte User kann jedes Tool jedes verbundenen MCP-Servers aufrufen. Die Core-Permissions (kb, ha, cam, etc.) sind als Python Enum hardcoded — das skaliert nicht für MCP-Integrationen, da jeder neue Server eine Code-Änderung + Deploy erfordert.

**Auslöser:** Kalender-Integration benötigt granulare Permissions (read vs. manage), und das gleiche Muster gilt für alle zukünftigen MCP-Server.

## Aktueller Stand

| Aspekt | Status |
|--------|--------|
| Permission-Definition | 31 hardcoded Enum-Werte in `permissions.py` |
| Permission-Speicherung | JSON-Array pro Rolle in DB |
| Permission-Prüfung | `require_permission()` Decorator für REST-Routes |
| MCP-Tool-Zugriff | **KEINE Prüfung** — alle User können alle Tools nutzen |
| MCP-Server-Config | Statisches YAML, keine Permission-Felder |
| Alte Plugin-Permissions | `allowed_plugins` Column wurde entfernt, kein Ersatz |

## Lösung: Hybrid A+B

### Konvention (Default)

Jeder MCP-Server `<name>` in `mcp_servers.yaml` erzeugt automatisch die Permission `mcp.<name>`. Ohne weitere Konfiguration prüft `execute_tool()` nur diese eine Permission.

**Beispiel:** Server `weather` → Permission `mcp.weather` → User braucht `mcp.weather` in seiner Rolle.

### YAML-Override (Granularität)

Für Server die feinere Kontrolle brauchen (read vs. manage), können in `mcp_servers.yaml` explizite Permissions und Tool-Mappings definiert werden:

```yaml
- name: calendar
  # ... bestehende Felder (url, transport, enabled, prompt_tools, examples) ...
  permissions:                          # Optional: Verfügbare Permission-Stufen
    - mcp.calendar.read                 # Events abfragen
    - mcp.calendar.manage               # Events erstellen/bearbeiten/löschen
  tool_permissions:                     # Optional: Per-Tool Mapping
    list_events: mcp.calendar.read
    get_next_event: mcp.calendar.read
    check_availability: mcp.calendar.read
    create_event: mcp.calendar.manage
```

### Prüflogik in `execute_tool()` (Reihenfolge)

```
Tool-Aufruf mit user_id:
  │
  ├─ AUTH_ENABLED=false oder kein user_id → erlauben (backwards-kompatibel)
  │
  ├─ tool_permissions definiert + Tool hat Mapping
  │   → Spezifische Permission prüfen (z.B. mcp.calendar.read)
  │
  ├─ permissions definiert (ohne tool_permissions für dieses Tool)
  │   → Prüfen ob User mindestens eine der permissions hat
  │
  └─ Nichts definiert in YAML
      → Konvention: mcp.<server_name> prüfen
```

### Wildcard-Support

- `mcp.*` — Zugang zu allen MCP-Servern (für Admin-Rolle)
- `mcp.calendar.*` — Zugang zu allen Calendar-Permissions (impliziert read + manage)

## Änderungen

### Geänderte Dateien

| Datei | Änderung |
|-------|---------|
| `config/mcp_servers.yaml` | Schema erweitern: `permissions` (list), `tool_permissions` (dict), beide optional |
| `src/backend/services/mcp_client.py` | `execute_tool()`: Permission-Check vor Tool-Ausführung; `user_id` Parameter hinzufügen |
| `src/backend/models/permissions.py` | `has_permission()`: dynamische `mcp.*` Strings akzeptieren (nicht nur Enum); Wildcard-Matching für `mcp.*` |
| `src/backend/models/permissions.py` | `get_all_permissions()`: MCP-Permissions aus verbundenen Servern einschließen (für Rollen-Editor) |
| `src/backend/api/routes/roles.py` | Validierung: `mcp.*` Permissions neben Enum-Werten akzeptieren |
| `src/backend/services/action_executor.py` | `user_id` an `mcp_manager.execute_tool()` durchreichen |

### Default-Rollen Update

| Rolle | MCP-Permissions |
|-------|----------------|
| Admin | `mcp.*` (Wildcard — Zugang zu allen MCP-Servern) |
| Familie | Explizite Liste pro Server (z.B. `mcp.calendar.manage`, `mcp.weather`, `mcp.homeassistant`) |
| Gast | Nur read-only Server (z.B. `mcp.weather`) |

### Frontend-Anpassungen

- **Rollen-Editor:** MCP-Permissions als eigene Sektion anzeigen (dynamisch aus verbundenen Servern)
- **Permissions-Liste:** `GET /api/auth/permissions` erweitern um MCP-Permissions aus laufenden Servern

## Abhängigkeiten

- **Voraussetzung für:** Kalender-Integration Phase 4d, und alle zukünftigen MCP-Integrationen mit granularen Permissions
- **Benötigt:** User-ID Propagation (Kalender Phase 3) — `execute_tool()` muss `user_id` kennen
- **Abwärtskompatibel:** Ohne `user_id` oder `AUTH_ENABLED=false` → alle Tools erlaubt (wie bisher)

## Verifizierung

1. `make test-backend` — alle Tests grün
2. Server ohne YAML-Permissions: User mit `mcp.weather` → kann Weather-Tools nutzen; User ohne → 403
3. Server mit `tool_permissions`: User mit `mcp.calendar.read` → kann `list_events` aber nicht `create_event`
4. Admin mit `mcp.*` → kann alle Tools aller Server nutzen
5. `AUTH_ENABLED=false` → alle Tools wie bisher erlaubt (kein Breaking Change)
6. Rollen-Editor zeigt MCP-Permissions dynamisch an