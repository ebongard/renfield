# Presence Analytics: Heatmap & Predictions

## Context

Presence detection tracks room occupancy in real-time via BLE, voice, and web auth — but all data is **in-memory only** and lost on restart. No historical data is persisted. There's no way to analyze occupancy patterns or predict future presence.

**Goal:** Persist presence events to PostgreSQL, provide a room x hour heatmap, and frequency-based predictions ("Montags um 14:00 ist Erik meist im Büro").

## Architecture

```
Hook events (enter/leave) already fire via utils/hooks.py
  → NEW: Analytics hook handler persists to presence_events table
  → SQL aggregation for heatmap (GROUP BY room, hour)
  → SQL aggregation for predictions (GROUP BY room, dow, hour)
  → 3 REST endpoints → Recharts + CSS heatmap on new "Analytics" tab
```

Event volume is low (~20-50 enter/leave events/day), so raw event storage + SQL `GROUP BY` is sufficient — no rollup tables needed.

---

## Files to Modify/Create

### 1. `src/backend/models/database.py` — Add `PresenceEvent` model

After `UserBleDevice` (line ~975):

```python
class PresenceEvent(Base):
    __tablename__ = "presence_events"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    event_type = Column(String(20), nullable=False)  # "enter" | "leave"
    source = Column(String(20), default="ble")        # "ble" | "voice" | "web"
    confidence = Column(Float, nullable=True)
    created_at = Column(DateTime, default=_utcnow, index=True)
    __table_args__ = (
        Index('ix_presence_events_analytics', 'user_id', 'room_id', 'created_at'),
    )
```

### 2. Alembic Migration

Auto-generate: `alembic revision --autogenerate -m "add presence_events table"`

### 3. `src/backend/services/presence_service.py` — Add `source` to hook kwargs

Currently BLE and voice events fire identical kwargs. Add `"source": "ble"` to BLE path (lines 235, 250) and `"source": "voice"` to voice path (lines 337, 362) so analytics can distinguish detection methods. ~6 lines changed.

### 4. `src/backend/services/presence_analytics.py` — NEW

**Hook handlers** (fire-and-forget, own `AsyncSessionLocal` session):
- `_on_enter_room(**kwargs)` → `PresenceEvent(event_type="enter")`
- `_on_leave_room(**kwargs)` → `PresenceEvent(event_type="leave")`
- `register_presence_analytics_hooks()` → registers both via `register_hook()`

**PresenceAnalyticsService(db: AsyncSession)**:

| Method | Query | Returns |
|--------|-------|---------|
| `get_heatmap(days, user_id?)` | `GROUP BY room_id, EXTRACT(HOUR FROM created_at)` on enter events | `[{room_id, room_name, hour, count}]` |
| `get_predictions(user_id, days)` | `GROUP BY room_id, EXTRACT(DOW), EXTRACT(HOUR)` + `COUNT(DISTINCT DATE)` / total_weeks | `[{room_id, room_name, day_of_week, hour, probability}]` |
| `get_daily_summary(days)` | `GROUP BY DATE(created_at)`, count enter/leave | `[{date, enter_count, leave_count}]` |
| `cleanup_old_events(retention_days)` | `DELETE WHERE created_at < cutoff` | count deleted |

### 5. `src/backend/utils/config.py` — Add setting

```python
presence_analytics_retention_days: int = 90
```

### 6. `src/backend/api/lifecycle.py` — Register hooks + cleanup

After existing webhook registration (line 465):
```python
if settings.presence_enabled:
    from services.presence_analytics import register_presence_analytics_hooks
    register_presence_analytics_hooks()
```

Add `_schedule_presence_event_cleanup()` — daily loop, follows `_schedule_memory_cleanup` pattern.

### 7. `src/backend/api/routes/presence.py` — 3 new endpoints

| Endpoint | Params | Response |
|----------|--------|----------|
| `GET /api/presence/analytics/heatmap` | `days=30`, `user_id?` | `list[HeatmapCell]` |
| `GET /api/presence/analytics/predictions` | `user_id` (required), `days=60` | `list[PredictionEntry]` |
| `GET /api/presence/analytics/daily` | `days=7` | `list[DailySummary]` |

### 8. `src/frontend/package.json` — Install Recharts

`npm install recharts` — for the predictions bar chart. Heatmap uses pure CSS/HTML table (simpler, better dark mode).

### 9. `src/frontend/src/pages/PresencePage.jsx` — Add tabs

Add `activeTab` state (`'live'` | `'analytics'`). Tab bar below header. Current content = "Live" tab. New `<AnalyticsTab>` component for analytics.

### 10. `src/frontend/src/components/presence/` — 3 new components

**`PresenceHeatmap.jsx`** — Room x hour HTML table grid
- Color-coded cells: gray → light blue → dark blue
- Dark mode via Tailwind, optional user filter, tooltips

**`PresencePredictions.jsx`** — Recharts BarChart
- Day-of-week selector pills (Mo-So)
- Bar chart: hour on X-axis, probability (%) on Y-axis
- Legend table for rooms with >= 30% probability

**`AnalyticsTab.jsx`** — Composition
- User dropdown + time range selector (7/30/60/90 days)
- `<PresenceHeatmap>` always shown, `<PresencePredictions>` when user selected
- Empty state when no data yet

### 11. `src/frontend/src/i18n/locales/{en,de}.json`

~20 new keys: `tabLive`, `tabAnalytics`, `heatmapTitle`, `predictionsTitle`, `allUsers`, `timeRange`, `days7`/`30`/`60`/`90`, `selectUserForPrediction`, `noAnalyticsData`, day names (`daySun`-`daySat`), etc.

### 12. `tests/backend/test_presence_analytics.py` — ~13 tests

- Hook handlers create events with correct fields
- Heatmap groups by room + hour correctly
- Heatmap user_id filter works
- Predictions calculate probability correctly
- Low-probability entries (< 10%) excluded
- Cleanup deletes old events, keeps recent
- Daily summary counts correctly
- Empty DB returns empty results
- Route tests for all 3 endpoints

---

## Implementation Order

| Step | What | Parallel? |
|------|------|-----------|
| 1 | DB model + migration + config setting | Yes (with 5) |
| 2 | `presence_service.py` — add `source` to kwargs | Yes (with 1) |
| 3 | `presence_analytics.py` service | After 1 |
| 4 | API endpoints | After 3 |
| 5 | `npm install recharts` + i18n keys | Yes (with 1) |
| 6 | Frontend components (3 files) | After 5 |
| 7 | PresencePage tabs | After 6 |
| 8 | Lifecycle hooks + cleanup scheduler | After 3 |
| 9 | Backend tests | After 4 |
| 10 | Deploy + Playwright screenshot test | After all |

## Verification

- [ ] Migration: `alembic upgrade head` creates `presence_events` table
- [ ] `python3 -m pytest tests/backend/test_presence_analytics.py -v` — all pass
- [ ] Existing 46+ presence tests still pass
- [ ] `curl /api/presence/analytics/heatmap` returns `[]` (no data yet)
- [ ] Trigger presence change → verify row in `presence_events` table
- [ ] Frontend: Analytics tab renders heatmap + predictions (empty state)
- [ ] Dark mode + responsive (Desktop/Tablet/Mobile)
- [ ] Production deploy + Playwright screenshot