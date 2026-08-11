"""Room persistence layer for RoomMind."""

from __future__ import annotations

import copy
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    DEFAULT_HEAT_SOURCE_AC_MIN_OUTDOOR,
    DEFAULT_HEAT_SOURCE_OUTDOOR_THRESHOLD,
    DEFAULT_HEAT_SOURCE_PRIMARY_DELTA,
    DOMAIN,
)
from .utils.device_utils import (
    devices_to_legacy,
    ensure_room_has_devices,
    get_room_heating_system_type,
    legacy_to_devices,
    migrate_heat_pump_devices,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = DOMAIN


def _migrate_room_temps(room: dict) -> dict:
    """Migrate legacy single comfort/eco temps to split heat/cool fields."""
    if "comfort_heat" not in room:
        room["comfort_heat"] = room.get("comfort_temp", DEFAULT_COMFORT_HEAT)
    if "comfort_cool" not in room:
        room["comfort_cool"] = DEFAULT_COMFORT_COOL
    if "eco_heat" not in room:
        room["eco_heat"] = room.get("eco_temp", DEFAULT_ECO_HEAT)
    if "eco_cool" not in room:
        room["eco_cool"] = DEFAULT_ECO_COOL
    return room


def _migrate_override_fields(room: dict) -> dict:
    """Migrate legacy single override_temp to split override_heat/override_cool."""
    if "override_temp" not in room:
        return room
    legacy = room.pop("override_temp")
    if legacy is None or room.get("override_heat") is not None or room.get("override_cool") is not None:
        return room
    climate_mode = room.get("climate_mode", "auto")
    legacy = float(legacy)
    if climate_mode == "cool_only":
        room["override_heat"] = None
        room["override_cool"] = legacy
    elif climate_mode == "heat_only":
        room["override_heat"] = legacy
        room["override_cool"] = None
    else:
        room["override_heat"] = legacy
        room["override_cool"] = max(legacy, room.get("comfort_cool", DEFAULT_COMFORT_COOL))
    return room


def _migrate_room(room: dict) -> dict:
    """Apply all read-time migrations (safety net)."""
    _migrate_room_temps(room)
    _migrate_override_fields(room)
    migrate_heat_pump_devices(room.get("devices", []))
    ensure_room_has_devices(room)
    return room


_ORPHAN_SETTINGS_KEYS = ("heating_threshold", "cooling_threshold")


class RoomMindStore:
    """Manage room configuration storage for RoomMind."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._store: Store[dict] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: dict[str, dict] = {}
        self._settings: dict = {}
        self._thermal_data: dict = {}

    async def async_load(self) -> None:
        """Load room data from the HA store."""
        stored = await self._store.async_load()
        if stored and "rooms" in stored:
            self._data = stored["rooms"]
        else:
            self._data = {}

        self._settings = stored.get("settings", {}) if stored else {}
        self._thermal_data = stored.get("thermal_data", {}) if stored else {}

        # One-time migrations (combined into single pass + single save)
        device_migrated = 0
        hp_migrated = 0
        override_migrated = 0
        for room in self._data.values():
            if "devices" not in room:
                ensure_room_has_devices(room)
                device_migrated += 1
            if migrate_heat_pump_devices(room.get("devices", [])):
                t, a = devices_to_legacy(room["devices"])
                room["thermostats"] = t
                room["acs"] = a
                hp_migrated += 1
            if "override_temp" in room:
                _migrate_room_temps(room)
                _migrate_override_fields(room)
                override_migrated += 1
        orphan_settings_removed = [k for k in _ORPHAN_SETTINGS_KEYS if self._settings.pop(k, None) is not None]
        if device_migrated or hp_migrated or override_migrated or orphan_settings_removed:
            await self._async_save()
        if device_migrated:
            _LOGGER.info("Migrated %d room(s) to unified device model", device_migrated)
        if hp_migrated:
            _LOGGER.info("Migrated %d room(s) from heat_pump to ac device type", hp_migrated)
        if override_migrated:
            _LOGGER.info("Migrated %d room(s) to split override heat/cool", override_migrated)
        if orphan_settings_removed:
            _LOGGER.info("Removed orphan setting(s): %s", ", ".join(orphan_settings_removed))

    async def _async_save(self) -> None:
        """Persist current room data to the HA store."""
        await self._store.async_save(
            {"rooms": self._data, "settings": self._settings, "thermal_data": self._thermal_data}
        )

    def get_rooms(self) -> dict[str, dict]:
        """Return a deep copy of all rooms (with migration applied)."""
        rooms = copy.deepcopy(dict(self._data))
        for room in rooms.values():
            _migrate_room(room)
        return rooms

    def get_room(self, area_id: str) -> dict | None:
        """Return a deep copy of a single room by area ID, or None if not found."""
        room = self._data.get(area_id)
        if room is None:
            return None
        result = copy.deepcopy(room)
        _migrate_room(result)
        return result

    def get_settings(self) -> dict:
        """Return a deep copy of global settings."""
        return copy.deepcopy(dict(self._settings))

    async def async_save_settings(self, changes: dict) -> dict:
        """Merge changes into global settings and persist."""
        self._settings.update(changes)
        await self._async_save()
        return dict(self._settings)

    def get_thermal_data(self) -> dict:
        """Return a deep copy of thermal learning data."""
        return copy.deepcopy(dict(self._thermal_data))

    async def async_save_thermal_data(self, data: dict) -> None:
        """Replace thermal learning data and persist."""
        self._thermal_data = data
        await self._async_save()

    async def async_clear_thermal_data_room(self, area_id: str) -> None:
        """Clear thermal learning data for a single room."""
        self._thermal_data.pop(area_id, None)
        await self._async_save()

    async def async_clear_all_thermal_data(self) -> None:
        """Clear all thermal learning data."""
        self._thermal_data = {}
        await self._async_save()

    @staticmethod
    def _sync_devices(room: dict, config: dict) -> None:
        """Bidirectional sync between devices[] and legacy thermostats/acs fields.

        Used for the UPDATE path only. Presence check (not truthiness) on
        ``"devices" in config`` so that sending ``devices=[]`` still triggers
        the devices→legacy sync.
        """
        if "devices" in config:
            # New frontend: devices is source of truth -> regenerate legacy.
            # Also derive heating_system_type from devices (ignore any sent value).
            t, a = devices_to_legacy(room["devices"])
            room["thermostats"] = t
            room["acs"] = a
            room["heating_system_type"] = get_room_heating_system_type(room["devices"])
        elif "thermostats" in config or "acs" in config:
            # Old frontend: legacy is source of truth -> regenerate devices
            room["devices"] = legacy_to_devices(
                room.get("thermostats", []),
                room.get("acs", []),
                room.get("heating_system_type", ""),
            )

    def _merge_room(self, area_id: str, config: dict) -> dict:
        """Merge config changes into an existing room."""
        existing = self._data[area_id]
        for key, value in config.items():
            if key != "area_id":
                existing[key] = value
        # Sync legacy fields from split fields for backward compat
        if "comfort_heat" in config:
            existing["comfort_temp"] = config["comfort_heat"]
        if "eco_heat" in config:
            existing["eco_temp"] = config["eco_heat"]
        # Reverse-sync: legacy callers sending only comfort_temp/eco_temp
        if "comfort_temp" in config and "comfort_heat" not in config:
            existing["comfort_heat"] = config["comfort_temp"]
        if "eco_temp" in config and "eco_heat" not in config:
            existing["eco_heat"] = config["eco_temp"]
        # Directional device sync
        self._sync_devices(existing, config)
        return existing

    def _create_room(self, area_id: str, config: dict) -> dict:
        """Create a new room with defaults and device sync."""
        # Derive split fields from legacy if needed
        comfort_heat = config.get("comfort_heat", config.get("comfort_temp", DEFAULT_COMFORT_HEAT))
        eco_heat = config.get("eco_heat", config.get("eco_temp", DEFAULT_ECO_HEAT))
        room = {
            "area_id": area_id,
            "devices": config.get("devices", []),
            "temperature_sensor": config.get("temperature_sensor", ""),
            "humidity_sensor": config.get("humidity_sensor", ""),
            "occupancy_sensors": config.get("occupancy_sensors", []),
            "climate_mode": config.get("climate_mode", "auto"),
            "schedules": config.get("schedules", []),
            "schedule_selector_entity": config.get("schedule_selector_entity", ""),
            "window_sensors": config.get("window_sensors", []),
            "window_open_delay": config.get("window_open_delay", 0),
            "window_close_delay": config.get("window_close_delay", 0),
            "comfort_temp": comfort_heat,
            "eco_temp": eco_heat,
            "comfort_heat": comfort_heat,
            "comfort_cool": config.get("comfort_cool", DEFAULT_COMFORT_COOL),
            "comfort_heat_entity": config.get("comfort_heat_entity", ""),
            "eco_heat": eco_heat,
            "eco_cool": config.get("eco_cool", DEFAULT_ECO_COOL),
            "presence_persons": config.get("presence_persons", []),
            "display_name": config.get("display_name", ""),
            "heating_system_type": config.get("heating_system_type", ""),
            "covers": config.get("covers", []),
            "covers_auto_enabled": config.get("covers_auto_enabled", False),
            "covers_deploy_threshold": config.get("covers_deploy_threshold", 1.5),
            "covers_min_position": config.get("covers_min_position", 0),
            "covers_outdoor_min_temp": config.get("covers_outdoor_min_temp", None),
            "covers_override_minutes": config.get("covers_override_minutes", 60),
            "cover_schedules": config.get("cover_schedules", []),
            "cover_schedule_selector_entity": config.get("cover_schedule_selector_entity", ""),
            "cover_orientations": config.get("cover_orientations", {}),
            "covers_night_close": config.get("covers_night_close", False),
            "covers_night_close_elevation": config.get("covers_night_close_elevation", 0),
            "covers_night_close_offset_minutes": config.get("covers_night_close_offset_minutes", 0),
            "covers_night_position": config.get("covers_night_position", 0),
            "covers_snap_deploy": config.get("covers_snap_deploy", False),
            "cover_min_positions": config.get("cover_min_positions", {}),
            "ignore_presence": config.get("ignore_presence", False),
            "is_outdoor": config.get("is_outdoor", False),
            "valve_protection_exclude": config.get("valve_protection_exclude", []),
            "heat_source_orchestration": config.get("heat_source_orchestration", False),
            "heat_source_primary_delta": config.get("heat_source_primary_delta", DEFAULT_HEAT_SOURCE_PRIMARY_DELTA),
            "heat_source_outdoor_threshold": config.get(
                "heat_source_outdoor_threshold", DEFAULT_HEAT_SOURCE_OUTDOOR_THRESHOLD
            ),
            "heat_source_ac_min_outdoor": config.get("heat_source_ac_min_outdoor", DEFAULT_HEAT_SOURCE_AC_MIN_OUTDOOR),
            "climate_control_enabled": config.get("climate_control_enabled", True),
        }
        # Directional device sync for new rooms (truthiness check, not just presence)
        if "devices" in config and config["devices"]:
            t, a = devices_to_legacy(room["devices"])
            room["thermostats"] = t
            room["acs"] = a
            room["heating_system_type"] = get_room_heating_system_type(room["devices"])
        elif "thermostats" in config or "acs" in config:
            room["thermostats"] = config.get("thermostats", [])
            room["acs"] = config.get("acs", [])
            room["devices"] = legacy_to_devices(
                room["thermostats"],
                room["acs"],
                room.get("heating_system_type", ""),
            )
        # Ensure legacy keys always exist (for backward compat)
        room.setdefault("thermostats", [])
        room.setdefault("acs", [])
        return room

    async def async_save_room(self, area_id: str, config: dict) -> dict:
        """Create or update room configuration for an area."""
        if area_id in self._data:
            room = self._merge_room(area_id, config)
        else:
            room = self._create_room(area_id, config)
            self._data[area_id] = room
        await self._async_save()
        return room

    async def async_update_room(self, area_id: str, changes: dict) -> dict:
        """Merge changes into an existing room. Raises KeyError if not found.

        Note: Does NOT perform device sync (devices <-> thermostats/acs).
        Use async_save_room() for changes involving device fields.
        """
        if area_id not in self._data:
            raise KeyError(f"Room '{area_id}' not found")

        # Prevent overriding the area_id
        changes.pop("area_id", None)

        self._data[area_id].update(changes)
        await self._async_save()
        return self._data[area_id]

    async def async_delete_room(self, area_id: str) -> None:
        """Delete a room. Raises KeyError if not found."""
        if area_id not in self._data:
            raise KeyError(f"Room '{area_id}' not found")

        del self._data[area_id]
        await self._async_save()
