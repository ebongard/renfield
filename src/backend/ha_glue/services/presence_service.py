"""
Presence Service — BLE-based room-level presence detection.

In-memory state management for tracking which users are in which rooms,
based on BLE scan reports from satellites. Aggregates RSSI from multiple
satellites for robust room assignment with hysteresis to prevent room flicker.
"""

import time
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.config import settings
from ha_glue.utils.config import ha_glue_settings


# Security (review H1): IRKs permanently de-anonymize a resident's rotating BLE
# address. Only push them to allowlisted satellites. Track which satellites we've
# already warned about (when the allowlist is empty) so the warning fires once.
_irk_ungated_warned: set[str] = set()


def irk_push_allowed(satellite_id: str) -> bool:
    """Whether ``satellite_id`` may receive per-person BLE IRKs.

    Driven by ``settings.satellite_irk_allowlist`` (comma-separated). Non-empty
    → allow only listed ids. Empty → ungated (legacy) but log a one-shot warning
    per satellite so the exposure is visible and operators can lock it down.
    """
    raw = (settings.satellite_irk_allowlist or "").strip()
    if raw:
        allow = {s.strip() for s in raw.split(",") if s.strip()}
        return satellite_id in allow
    if satellite_id not in _irk_ungated_warned:
        _irk_ungated_warned.add(satellite_id)
        logger.warning(
            f"⚠️ IRK push to satellite '{satellite_id}' is UNGATED "
            f"(SATELLITE_IRK_ALLOWLIST empty) — any device registering as a "
            f"satellite receives household IRKs (location-tracking keys). Set "
            f"SATELLITE_IRK_ALLOWLIST to the known satellite ids to close this."
        )
    return True


@dataclass
class DeviceSighting:
    """A single BLE scan result from a satellite."""
    satellite_id: str
    room_id: int | None
    rssi: int
    timestamp: float


@dataclass
class UserPresence:
    """Current presence state of a user."""
    user_id: int
    room_id: int | None = None
    room_name: str | None = None
    satellite_id: str | None = None
    confidence: float = 0.0
    last_seen: float = 0.0
    consecutive_room_count: int = 0  # scans the user has held the CURRENT room
    # Hysteresis candidate: the room currently challenging current.room_id and how
    # many CONSECUTIVE scans it has won. A switch only happens when this reaches
    # the threshold; it resets whenever the current room wins again — so a single
    # stray sighting from an adjacent satellite can't flip the room.
    pending_room_id: int | None = None
    pending_room_count: int = 0
    # #10 asymmetric RSSI filter: room_id → smoothed RSSI (fast attack / slow
    # release). Drives margin-based room selection instead of the raw-mean +
    # scan-count scheme. Empty when the filter is disabled.
    room_rssi_filtered: dict[int, float] = field(default_factory=dict)


class PresenceService:
    """
    In-memory presence tracking service.

    Processes BLE scan reports from satellites and maintains a map of
    user_id → current room. Uses "strongest RSSI wins" with hysteresis
    to avoid room flicker.
    """

    def __init__(self):
        self._mac_to_user: dict[str, int] = {}          # MAC → user_id cache
        self._mac_to_method: dict[str, str] = {}         # MAC → detection_method cache
        self._presence: dict[int, UserPresence] = {}     # user_id → presence
        self._sightings: dict[str, list[DeviceSighting]] = {}  # MAC → recent sightings
        self._hysteresis_threshold: int = ha_glue_settings.presence_hysteresis_scans
        self._stale_timeout: float = float(ha_glue_settings.presence_stale_timeout)
        self._rssi_threshold: int = ha_glue_settings.presence_rssi_threshold
        # #10 asymmetric RSSI filter + margin hysteresis
        self._filter_enabled: bool = bool(ha_glue_settings.presence_rssi_filter_enabled)
        self._filter_alpha_up: float = float(ha_glue_settings.presence_rssi_filter_alpha_up)
        self._filter_alpha_down: float = float(ha_glue_settings.presence_rssi_filter_alpha_down)
        self._filter_fresh_seconds: float = float(ha_glue_settings.presence_filter_fresh_seconds)
        self._switch_enter_margin_db: float = float(ha_glue_settings.presence_switch_enter_margin_db)
        self._room_names: dict[int, str] = {}            # room_id → name cache
        self._user_names: dict[int, str] = {}            # user_id → username
        self._user_first_names: dict[int, str] = {}      # user_id → first_name
        self._user_last_names: dict[int, str] = {}       # user_id → last_name
        self._pending_events: list[tuple[str, dict]] = []  # (event_name, kwargs)
        # Per-person IRK store (for resolving rotating RPAs on non-bonded
        # satellites). label → user_id, and label → decrypted IRK hex.
        self._irk_label_to_user: dict[str, int] = {}
        self._irks_hex: dict[str, str] = {}

    async def load_device_registry(self, db: AsyncSession):
        """Load UserBleDevice table into MAC → user_id cache."""
        from models.database import User, UserBleDevice

        result = await db.execute(
            select(UserBleDevice).where(UserBleDevice.is_enabled == True)  # noqa: E712
        )
        devices = result.scalars().all()

        self._mac_to_user = {
            d.mac_address.upper(): d.user_id for d in devices
        }
        self._mac_to_method = {
            d.mac_address.upper(): (d.detection_method or "ble") for d in devices
        }

        # Cache user names for frontend display and chat lookup
        user_result = await db.execute(select(User))
        users = user_result.scalars().all()
        self._user_names = {u.id: u.username for u in users}
        self._user_first_names = {u.id: u.first_name for u in users if u.first_name}
        self._user_last_names = {u.id: u.last_name for u in users if u.last_name}

        logger.info(f"Presence: loaded {len(self._mac_to_user)} devices "
                     f"(BLE: {sum(1 for m in self._mac_to_method.values() if m == 'ble')}, "
                     f"Classic BT: {sum(1 for m in self._mac_to_method.values() if m == 'classic_bt')})")

        await self._load_irks(db)

    async def _load_irks(self, db: AsyncSession):
        """Load UserBleIrk into the label → user_id and label → IRK-hex caches,
        decrypting each IRK. A row that fails to decrypt is skipped (logged)."""
        from models.database import UserBleIrk
        from services.secret_encryption import decrypt_secret

        result = await db.execute(
            select(UserBleIrk).where(UserBleIrk.is_enabled == True)  # noqa: E712
        )
        rows = result.scalars().all()
        label_to_user: dict[str, int] = {}
        irks_hex: dict[str, str] = {}
        for row in rows:
            try:
                irks_hex[row.label] = decrypt_secret(row.irk_encrypted)
            except Exception:
                logger.warning(f"Presence: could not decrypt IRK for label '{row.label}' (skipped)")
                continue
            label_to_user[row.label] = row.user_id
        self._irk_label_to_user = label_to_user
        self._irks_hex = irks_hex
        if rows and not irks_hex:
            # All IRKs failed to decrypt — almost certainly a SECRET_KEY change.
            # Surface a single aggregate signal, not just scattered per-row warns.
            logger.error(
                f"Presence: {len(rows)} BLE IRK(s) present but 0 decryptable — "
                "SECRET_KEY may have rotated; phone presence will not resolve."
            )
        logger.info(f"Presence: loaded {len(irks_hex)} BLE IRK(s)")

    def set_room_name(self, room_id: int, name: str):
        """Cache a room name for display."""
        self._room_names[room_id] = name

    def get_user_name(self, user_id: int) -> str | None:
        """Get cached username for a user_id."""
        return self._user_names.get(user_id)

    def get_display_name(self, user_id: int) -> str:
        """Get best display name: first_name > username."""
        return self._user_first_names.get(user_id) or self._user_names.get(user_id, f"User {user_id}")

    def find_user_by_name(self, name: str) -> int | None:
        """
        Find a user_id by name (case-insensitive).

        Searches in order: username, first_name, last_name.
        Returns user_id or None.
        """
        name_lower = name.strip().lower()
        if not name_lower:
            return None

        # Check usernames
        for uid, uname in self._user_names.items():
            if uname.lower() == name_lower:
                return uid

        # Check first names
        for uid, fname in self._user_first_names.items():
            if fname.lower() == name_lower:
                return uid

        # Check last names
        for uid, lname in self._user_last_names.items():
            if lname.lower() == name_lower:
                return uid

        return None

    async def process_ble_report(
        self,
        satellite_id: str,
        room_id: int | None,
        devices: list[dict],
        room_name: str | None = None,
    ):
        """
        Process a BLE scan report from a satellite.

        Args:
            satellite_id: ID of the reporting satellite
            room_id: Room where the satellite is located
            devices: List of {mac, rssi} dicts
            room_name: Optional room name for display
        """
        if room_name and room_id:
            self._room_names[room_id] = room_name

        now = time.time()

        for device in devices:
            rssi = device.get("rssi", -100)

            # An IRK-resolved reading carries a stable `identity` (the rotating
            # `mac` would never match a whitelist); key it by the identity so a
            # phone is tracked across RPA rotation. Otherwise key by MAC.
            identity = device.get("identity")
            if identity and identity in getattr(self, "_irk_label_to_user", {}):
                key = "irk:" + identity
            else:
                key = device.get("mac", "").upper()
                if self._user_for_key(key) is None:
                    continue  # not a known device

            sighting = DeviceSighting(
                satellite_id=satellite_id,
                room_id=room_id,
                rssi=rssi,
                timestamp=now,
            )

            # Keep only recent sightings (last 2 minutes)
            if key not in self._sightings:
                self._sightings[key] = []
            self._sightings[key] = [
                s for s in self._sightings[key]
                if now - s.timestamp < self._stale_timeout
            ]
            self._sightings[key].append(sighting)

            self._assign_room(key)

        # Clean up stale presence
        self._cleanup_stale(now)

        # Fire collected presence events
        await self._fire_pending_events()

    def _user_for_key(self, key: str) -> int | None:
        """Resolve a sighting key (a MAC, or an "irk:<label>" identity) to a
        user_id. Keeps IRK identities out of the MAC caches used for the
        satellite known-MAC push."""
        if key.startswith("irk:"):
            return getattr(self, "_irk_label_to_user", {}).get(key[4:])
        return self._mac_to_user.get(key)

    @staticmethod
    def _select_room_legacy(room_rssi: dict[int, list[int]]) -> tuple[int | None, list[int]]:
        """Legacy selection: per-room mean RSSI + 5 dB per extra satellite, argmax."""
        best_room_id, best_score, best_rssi_values = None, float("-inf"), []
        for room_id, vals in room_rssi.items():
            score = sum(vals) / len(vals) + 5 * (len(vals) - 1)
            if score > best_score:
                best_score, best_room_id, best_rssi_values = score, room_id, vals
        return best_room_id, best_rssi_values

    def _select_room_filtered(
        self,
        current: "UserPresence",
        room_raw_mean: dict[int, float],
        room_last_ts: dict[int, float],
        room_rssi: dict[int, list[int]],
    ) -> tuple[int | None, list[int]]:
        """#10 selection: asymmetric-EWMA-smoothed per-room RSSI, argmax.

        Fast attack (a room STRENGTHENING → snappy entry) / slow release (a room
        WEAKENING → damps departures + strays). A room not heard within
        ``filter_fresh_seconds`` decays toward the RSSI floor (so a room the user
        left fades). A first-ever sighting of a room starts at the FLOOR — never
        at full strength — so a single strong stray can't win on one scan."""
        floor = float(self._rssi_threshold)
        now = max(room_last_ts.values()) if room_last_ts else 0.0
        filt = current.room_rssi_filtered
        for room, raw in room_raw_mean.items():
            fresh = (now - room_last_ts.get(room, 0.0)) <= self._filter_fresh_seconds
            prev = filt.get(room, floor)  # new room seeds at the floor → damped
            if not fresh:
                filt[room] = self._filter_alpha_down * floor + (1 - self._filter_alpha_down) * prev
            else:
                alpha = self._filter_alpha_up if raw >= prev else self._filter_alpha_down
                filt[room] = alpha * raw + (1 - alpha) * prev
        # Drop rooms that have aged out of the sighting window entirely.
        for room in list(filt.keys()):
            if room not in room_raw_mean:
                del filt[room]
        if not filt:
            return None, []
        best_room_id = max(filt, key=lambda r: filt[r])
        return best_room_id, room_rssi.get(best_room_id, [])

    def _should_switch_filtered(self, current: "UserPresence", best_room_id: int) -> bool:
        """Switch only when the challenger's FILTERED value beats the current
        room by the enter margin — or the current room has aged out entirely
        (user left it). Replaces the N-consecutive-scan count."""
        if current.room_id is None:
            return True
        filt = current.room_rssi_filtered
        if current.room_id not in filt:
            return True  # current room no longer heard at all → follow the signal
        return (filt.get(best_room_id, float("-inf")) - filt[current.room_id]
                >= self._switch_enter_margin_db)

    def _assign_room(self, mac: str):
        """Assign a user to a room based on multi-satellite RSSI aggregation with hysteresis."""
        user_id = self._user_for_key(mac)
        if user_id is None:
            return

        sightings = self._sightings.get(mac, [])
        if not sightings:
            return

        # Filter by RSSI threshold
        valid = [s for s in sightings if s.rssi >= self._rssi_threshold]
        if not valid:
            return

        # Group by room — keep latest sighting per (room, satellite) pair
        latest_per_key: dict[tuple[int, str], DeviceSighting] = {}
        for s in valid:
            if s.room_id is None:
                continue
            key = (s.room_id, s.satellite_id)
            if key not in latest_per_key or s.timestamp > latest_per_key[key].timestamp:
                latest_per_key[key] = s

        # Collect RSSI values per room
        room_rssi: dict[int, list[int]] = {}
        for (room_id, _), s in latest_per_key.items():
            room_rssi.setdefault(room_id, []).append(s.rssi)

        if not room_rssi:
            return

        # --- Room selection --------------------------------------------------
        # Per-room raw mean + the room's freshest sighting time (the filter uses
        # the latter to decay a room the current satellite stops hearing).
        room_raw_mean: dict[int, float] = {
            r: sum(v) / len(v) for r, v in room_rssi.items()
        }
        room_last_ts: dict[int, float] = {}
        for (r, _sat), s in latest_per_key.items():
            if s.timestamp > room_last_ts.get(r, 0.0):
                room_last_ts[r] = s.timestamp

        current = self._presence.get(user_id)
        if current is None:
            current = UserPresence(user_id=user_id)
            self._presence[user_id] = current

        if self._filter_enabled:
            best_room_id, best_rssi_values = self._select_room_filtered(
                current, room_raw_mean, room_last_ts, room_rssi
            )
        else:
            best_room_id, best_rssi_values = self._select_room_legacy(room_rssi)
        if best_room_id is None or not best_rssi_values:
            return

        # Strongest satellite + freshest timestamp in the winning room
        best_satellite_id = None
        best_sat_rssi = float("-inf")
        best_timestamp = 0.0
        for (room_id, sat_id), s in latest_per_key.items():
            if room_id == best_room_id:
                if s.rssi > best_sat_rssi:
                    best_sat_rssi = s.rssi
                    best_satellite_id = sat_id
                if s.timestamp > best_timestamp:
                    best_timestamp = s.timestamp

        current.last_seen = best_timestamp

        # Confidence: RSSI component (70%) + satellite count component (30%)
        mean_rssi = sum(best_rssi_values) / len(best_rssi_values)
        rssi_conf = max(0.0, min(1.0, (mean_rssi + 90) / 60.0))
        sat_factor = min(1.0, len(best_rssi_values) / 3.0)
        confidence = rssi_conf * 0.7 + sat_factor * 0.3
        current.confidence = confidence

        # --- Same room? reinforce and stop -----------------------------------
        if best_room_id == current.room_id:
            current.consecutive_room_count += 1
            current.pending_room_id = None
            current.pending_room_count = 0
            current.satellite_id = best_satellite_id
            return

        # --- Switch decision -------------------------------------------------
        if self._filter_enabled:
            # Margin hysteresis: the challenger's FILTERED value must beat the
            # current room by the enter margin (a single stray is damped below it).
            should_switch = self._should_switch_filtered(current, best_room_id)
        else:
            # Legacy: N CONSECUTIVE scans of the same candidate (a stray resets it).
            if current.pending_room_id == best_room_id:
                current.pending_room_count += 1
            else:
                current.pending_room_id = best_room_id
                current.pending_room_count = 1
            should_switch = (current.room_id is None
                             or current.pending_room_count >= self._hysteresis_threshold)

        if not should_switch:
            return

        # --- Execute the room change + events --------------------------------
        old_room_id = current.room_id
        old_room_name = current.room_name
        was_first = old_room_id is None and len(self._presence) == 1

        current.room_id = best_room_id
        current.room_name = self._room_names.get(best_room_id) if best_room_id else None
        current.satellite_id = best_satellite_id
        current.consecutive_room_count = 1
        current.pending_room_id = None
        current.pending_room_count = 0
        new_room = current.room_name or current.room_id
        logger.debug(f"Presence: user {user_id} moved {old_room_name or old_room_id} → {new_room}")

        # Fire leave event for old room
        if old_room_id is not None and old_room_id != best_room_id:
            self._pending_events.append(("presence_leave_room", {
                "user_id": user_id,
                "user_name": self.get_user_name(user_id),
                "room_id": old_room_id,
                "room_name": old_room_name,
                "source": "ble",
            }))
            # Check if old room is now empty
            if not self.get_room_occupants(old_room_id):
                self._pending_events.append(("presence_last_left", {
                    "room_id": old_room_id,
                    "room_name": old_room_name,
                }))

        # Fire enter event for new room
        if best_room_id is not None:
            self._pending_events.append(("presence_enter_room", {
                "user_id": user_id,
                "user_name": self.get_user_name(user_id),
                "room_id": best_room_id,
                "room_name": self._room_names.get(best_room_id),
                "confidence": confidence,
                "source": "ble",
                "satellite_id": best_satellite_id,
            }))
            if was_first:
                self._pending_events.append(("presence_first_arrived", {
                    "user_id": user_id,
                    "user_name": self.get_user_name(user_id),
                    "room_id": best_room_id,
                    "room_name": self._room_names.get(best_room_id),
                    "source": "ble",
                }))

    def _cleanup_stale(self, now: float):
        """Mark users as absent if not seen recently."""
        stale_users = []
        for user_id, presence in self._presence.items():
            if now - presence.last_seen > self._stale_timeout:
                stale_users.append(user_id)

        for user_id in stale_users:
            old = self._presence.pop(user_id)
            logger.debug(f"Presence: user {user_id} marked absent (was in {old.room_name or old.room_id})")
            if old.room_id is not None:
                self._pending_events.append(("presence_leave_room", {
                    "user_id": user_id,
                    "user_name": self.get_user_name(user_id),
                    "room_id": old.room_id,
                    "room_name": old.room_name,
                    "source": "ble",
                }))
                if not self.get_room_occupants(old.room_id):
                    self._pending_events.append(("presence_last_left", {
                        "room_id": old.room_id,
                        "room_name": old.room_name,
                    }))

    async def _fire_pending_events(self):
        """Fire all collected presence events via the hook system."""
        from utils.hooks import run_hooks

        events = self._pending_events[:]
        self._pending_events.clear()
        for event_name, kwargs in events:
            await run_hooks(event_name, **kwargs)

        # Push ONE content-free presence_changed delta to the kiosk hub — but
        # only when a room-occupant set actually changed this pass. _pending_events
        # is populated exclusively by the enter/leave/last-left/first-arrived
        # branches (already de-bounced by the room-assignment hysteresis), so an
        # empty list means a bare RSSI tick with no membership change → no push.
        # This is the §6 presence-chatter guard: we ride the existing transition
        # signal, never the raw sensor cadence.
        if events:
            await self._broadcast_presence_changed()

    async def _broadcast_presence_changed(self):
        """Fire-and-forget a content-free rooms→occupant-count delta. A hub
        failure must never disrupt presence tracking."""
        try:
            from api.websocket.kiosk_handler import (
                broadcast_kiosk_event,
                build_presence_payload,
            )

            payload = build_presence_payload(self)
            await broadcast_kiosk_event({"type": "presence_changed", **payload})
        except Exception as e:
            logger.debug(f"kiosk presence_changed broadcast failed: {e}")

    async def register_voice_presence(
        self,
        user_id: int,
        room_id: int,
        room_name: str | None = None,
        confidence: float = 1.0,
        satellite_id: str | None = None,
    ):
        """
        Register presence from voice interaction (speaker recognition or auth).

        Voice/auth = certain presence, so this bypasses BLE hysteresis.
        A single call is enough to move the user to the new room.
        """
        now = time.time()

        if room_name and room_id:
            self._room_names[room_id] = room_name

        current = self._presence.get(user_id)

        if current is not None and current.room_id == room_id:
            # Same room — just refresh last_seen, no hooks
            current.last_seen = now
            current.confidence = confidence
            return

        # Different room or first appearance — bypass hysteresis
        if current is None:
            current = UserPresence(user_id=user_id)
            self._presence[user_id] = current

        old_room_id = current.room_id
        old_room_name = current.room_name

        # Check if house was empty before this user (first_arrived detection)
        was_first = old_room_id is None and len(self._presence) == 1

        # Fire leave event for old room
        if old_room_id is not None and old_room_id != room_id:
            self._pending_events.append(("presence_leave_room", {
                "user_id": user_id,
                "user_name": self.get_user_name(user_id),
                "room_id": old_room_id,
                "room_name": old_room_name,
                "source": "voice",
            }))
            # Check if old room is now empty (user hasn't moved yet, so exclude them)
            other_occupants = [
                p for p in self._presence.values()
                if p.room_id == old_room_id and p.user_id != user_id
            ]
            if not other_occupants:
                self._pending_events.append(("presence_last_left", {
                    "room_id": old_room_id,
                    "room_name": old_room_name,
                }))

        # Update presence state
        current.room_id = room_id
        current.room_name = room_name or self._room_names.get(room_id)
        current.confidence = confidence
        current.last_seen = now
        current.consecutive_room_count = 1

        # Fire enter event for new room
        self._pending_events.append(("presence_enter_room", {
            "user_id": user_id,
            "user_name": self.get_user_name(user_id),
            "room_id": room_id,
            "room_name": current.room_name,
            "confidence": confidence,
            "source": "voice",
            "satellite_id": satellite_id,
        }))
        if was_first:
            self._pending_events.append(("presence_first_arrived", {
                "user_id": user_id,
                "user_name": self.get_user_name(user_id),
                "room_id": room_id,
                "room_name": current.room_name,
                "source": "voice",
            }))

        logger.debug(f"Presence: voice/auth — user {user_id} → {current.room_name or room_id}")

        await self._fire_pending_events()

    def get_room_occupants(self, room_id: int) -> list[UserPresence]:
        """Get all users currently in a room."""
        return [
            p for p in self._presence.values()
            if p.room_id == room_id
        ]

    def get_user_presence(self, user_id: int) -> UserPresence | None:
        """Get presence info for a specific user."""
        return self._presence.get(user_id)

    def get_all_presence(self) -> dict[int, UserPresence]:
        """Get all current presence data."""
        return dict(self._presence)

    def is_user_alone_in_room(self, user_id: int) -> bool | None:
        """
        Check if user is the only person in their room.

        Returns:
            True if alone, False if others present, None if user not tracked.
        """
        presence = self._presence.get(user_id)
        if presence is None or presence.room_id is None:
            return None

        occupants = self.get_room_occupants(presence.room_id)
        return len(occupants) == 1

    def get_known_macs(self) -> set[str]:
        """Get all known MAC addresses for pushing to satellites."""
        return set(self._mac_to_user.keys())

    def get_ble_macs(self) -> set[str]:
        """Get MAC addresses of BLE devices only."""
        return {mac for mac, method in self._mac_to_method.items() if method == "ble"}

    def get_classic_bt_macs(self) -> set[str]:
        """Get MAC addresses of Classic BT devices only."""
        return {mac for mac, method in self._mac_to_method.items() if method == "classic_bt"}

    def get_ble_irks(self) -> list[dict]:
        """Per-person IRKs to push to satellites: [{'name': label, 'irk': hex}].
        IRK hex is decrypted in memory; only ever leaves over the WS link."""
        return [{"name": label, "irk": irk} for label, irk in getattr(self, "_irks_hex", {}).items()]

    def irks_for_satellite(
        self, satellite_id: str, is_enrolled_authenticated: bool = False
    ) -> list[dict]:
        """IRK list to push to ``satellite_id`` (security H1).

        When per-satellite enrollment is enabled, the IRK push keys on whether
        THIS connection presented a valid enrollment PSK — the allowlist
        stop-gap is bypassed entirely (a verified satellite is a stronger
        signal than a config string). When enrollment is disabled, fall back to
        the legacy ``SATELLITE_IRK_ALLOWLIST`` gate.

        Returns the IRKs only when permitted; an empty list otherwise (so the
        caller's ``if irks:`` guard naturally skips the send).
        """
        if settings.satellite_enrollment_enabled:
            return self.get_ble_irks() if is_enrolled_authenticated else []
        if not irk_push_allowed(satellite_id):
            return []
        return self.get_ble_irks()

    async def push_macs_to_satellites(self):
        """Push current known MACs + IRKs to all connected satellites."""
        from ha_glue.services.satellite_manager import get_satellite_manager

        manager = get_satellite_manager()
        ble_macs = list(self.get_ble_macs())
        classic_macs = list(self.get_classic_bt_macs())
        all_irks = self.get_ble_irks()

        if not ble_macs and not classic_macs and not all_irks:
            return

        for sat_id, sat_info in manager.satellites.items():
            try:
                if ble_macs:
                    await sat_info.websocket.send_json({
                        "type": "ble_known_devices",
                        "devices": ble_macs,
                    })
                if classic_macs:
                    await sat_info.websocket.send_json({
                        "type": "classic_bt_known_devices",
                        "devices": classic_macs,
                    })
                # IRKs are gated per satellite (H1): enrollment-auth when
                # enrollment is on, else the legacy allowlist. Mirrors
                # irks_for_satellite so the two push paths agree.
                if settings.satellite_enrollment_enabled:
                    irks = all_irks if getattr(sat_info, "authenticated", False) else []
                else:
                    irks = all_irks if irk_push_allowed(sat_id) else []
                if irks:
                    await sat_info.websocket.send_json({
                        "type": "ble_known_irks",
                        "irks": irks,
                    })
                logger.debug(f"Pushed {len(ble_macs)} BLE + {len(classic_macs)} Classic BT MACs "
                             f"+ {len(irks)} IRK(s) to {sat_id}")
            except Exception as e:
                logger.warning(f"Failed to push MACs/IRKs to {sat_id}: {e}")

    async def add_device(
        self,
        user_id: int,
        mac: str,
        name: str,
        device_type: str,
        db: AsyncSession,
        detection_method: str = "ble",
    ):
        """Add a BLE/Classic BT device to the registry and DB."""
        from models.database import UserBleDevice

        mac = mac.upper()
        device = UserBleDevice(
            user_id=user_id,
            mac_address=mac,
            device_name=name,
            device_type=device_type,
            detection_method=detection_method,
        )
        db.add(device)
        await db.commit()
        await db.refresh(device)

        # Update caches
        self._mac_to_user[mac] = user_id
        self._mac_to_method[mac] = detection_method
        logger.info(f"Presence: registered {detection_method} device {mac} for user {user_id}")

        # Push updated MACs to all connected satellites
        await self.push_macs_to_satellites()

        return device

    async def update_device(
        self,
        device_id: int,
        detection_method: str,
        db: AsyncSession,
        mac_address: str | None = None,
    ):
        """Update a device's detection method and/or MAC address."""
        from models.database import UserBleDevice

        result = await db.execute(
            select(UserBleDevice).where(UserBleDevice.id == device_id)
        )
        device = result.scalar_one_or_none()
        if not device:
            return None

        old_mac = device.mac_address.upper()
        device.detection_method = detection_method

        if mac_address:
            new_mac = mac_address.upper()
            device.mac_address = new_mac
            # Update MAC caches: remove old, add new
            self._mac_to_user.pop(old_mac, None)
            self._mac_to_method.pop(old_mac, None)
            self._mac_to_user[new_mac] = device.user_id
            self._mac_to_method[new_mac] = detection_method
            logger.info(f"Presence: updated device {old_mac} -> {new_mac} ({detection_method})")
        else:
            self._mac_to_method[old_mac] = detection_method
            logger.info(f"Presence: updated {old_mac} to {detection_method}")

        await db.commit()
        await db.refresh(device)

        # Push updated MACs to all connected satellites
        await self.push_macs_to_satellites()

        return device

    async def remove_device(self, device_id: int, db: AsyncSession):
        """Remove a BLE device from the registry and DB."""
        from models.database import UserBleDevice

        result = await db.execute(
            select(UserBleDevice).where(UserBleDevice.id == device_id)
        )
        device = result.scalar_one_or_none()
        if device:
            mac = device.mac_address.upper()
            self._mac_to_user.pop(mac, None)
            self._mac_to_method.pop(mac, None)
            self._sightings.pop(mac, None)
            await db.delete(device)
            await db.commit()
            logger.info(f"Presence: removed BLE device {mac}")

            # Push updated MACs to all connected satellites
            await self.push_macs_to_satellites()

            return True
        return False


# Singleton instance
_presence_service: PresenceService | None = None


def get_presence_service() -> PresenceService:
    """Get the singleton PresenceService instance."""
    global _presence_service
    if _presence_service is None:
        _presence_service = PresenceService()
    return _presence_service
