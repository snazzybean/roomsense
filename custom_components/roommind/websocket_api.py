"""WebSocket API for RoomMind room CRUD operations."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import (
    CLIMATE_MODES,
    CONFLICT_RESOLUTIONS,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_COMPRESSOR_MIN_OFF_MINUTES,
    DEFAULT_COMPRESSOR_MIN_RUN_MINUTES,
    DEFAULT_CONFLICT_RESOLUTION,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    DOMAIN,
    OVERRIDE_TYPES,
    build_override_live,
    is_override_suppressed,
)
from .services.analytics_service import (
    _compute_target_forecast,  # noqa: F401 - re-exported for tests
    _csv_to_points,  # noqa: F401 - re-exported for tests
    _safe_float,  # noqa: F401 - re-exported for tests
    build_analytics_data,
)
from .utils.device_utils import (
    COIL_DRY_INHERIT,
    COIL_DRY_MODE_FAN_ONLY,
    COIL_DRY_MODES,
    COIL_DRY_ON,
    COIL_DRY_OVERRIDES,
    DEFAULT_COIL_DRY_DRAIN_MINUTES,
    DEFAULT_COIL_DRY_FAN_MODE,
    DEFAULT_COIL_DRY_MIN_COOLING_MINUTES,
    DEFAULT_COIL_DRY_MINUTES,
)

_LOGGER = logging.getLogger(__name__)


def _validate_device_idle_action(device: dict) -> dict:
    """Enforce type-specific idle_action constraints.

    idle_action="low" lowers the setpoint to min_temp while keeping the
    device in its active hvac_mode. For ACs this would cool continuously
    toward the minimum, which is never the intent. Restrict "low" to TRVs.
    """
    if device.get("type") == "ac" and device.get("idle_action") == "low":
        raise vol.Invalid("idle_action='low' is only supported for TRVs (type='trv')")
    return device


def _validate_device_coil_dry(device: dict) -> dict:
    """Enforce that coil dry is only enabled on ACs.

    A TRV has no evaporator coil, so "on" is meaningless there. "inherit" and
    "off" stay allowed for both types so a uniform device dict is always valid.
    """
    if device.get("type") == "trv" and device.get("coil_dry") == COIL_DRY_ON:
        raise vol.Invalid("coil_dry='on' is only supported for ACs (type='ac')")
    return device


if TYPE_CHECKING:
    from homeassistant.components.websocket_api import ActiveConnection

    from .coordinator import RoomMindCoordinator


def _get_coordinator(hass: HomeAssistant) -> RoomMindCoordinator | None:
    """Return the RoomMindCoordinator from hass.data, or None."""
    coordinator: RoomMindCoordinator | None = hass.data.get(DOMAIN, {}).get("coordinator")
    return coordinator


_ROOM_SAVE_FIELDS = (
    "thermostats",
    "acs",
    "devices",
    "temperature_sensor",
    "humidity_sensor",
    "occupancy_sensors",
    "climate_mode",
    "schedules",
    "schedule_selector_entity",
    "window_sensors",
    "window_open_delay",
    "window_close_delay",
    "comfort_temp",
    "eco_temp",
    "comfort_heat",
    "comfort_cool",
    "comfort_heat_entity",
    "eco_heat",
    "eco_cool",
    "presence_persons",
    "display_name",
    "heating_system_type",
    "covers",
    "covers_auto_enabled",
    "covers_deploy_threshold",
    "covers_min_position",
    "covers_outdoor_min_temp",
    "covers_override_minutes",
    "cover_schedules",
    "cover_schedule_selector_entity",
    "cover_orientations",
    "covers_night_close",
    "covers_night_close_elevation",
    "covers_night_close_offset_minutes",
    "covers_night_position",
    "covers_snap_deploy",
    "cover_min_positions",
    "ignore_presence",
    "is_outdoor",
    "heat_source_orchestration",
    "heat_source_primary_delta",
    "heat_source_outdoor_threshold",
    "heat_source_ac_min_outdoor",
    "valve_protection_exclude",
    "climate_control_enabled",
)

_SETTINGS_SAVE_FIELDS = (
    "outdoor_temp_sensor",
    "outdoor_humidity_sensor",
    "outdoor_cooling_min",
    "outdoor_heating_max",
    "control_mode",
    "comfort_weight",
    "weather_entity",
    "outdoor_unavailable_notify",
    "climate_control_active",
    "learning_disabled_rooms",
    "hidden_rooms",
    "vacation_temp",
    "vacation_until",
    "prediction_enabled",
    "presence_enabled",
    "presence_persons",
    "presence_away_action",
    "presence_clears_override",
    "schedule_off_action",
    "valve_protection_enabled",
    "valve_protection_interval_days",
    "coil_dry_enabled",
    "coil_dry_minutes",
    "coil_dry_mode",
    "coil_dry_fan_mode",
    "coil_dry_min_cooling_minutes",
    "coil_dry_drain_minutes",
    "mold_detection_enabled",
    "mold_humidity_threshold",
    "mold_sustained_minutes",
    "mold_notification_cooldown",
    "mold_notifications_enabled",
    "mold_notification_targets",
    "mold_prevention_enabled",
    "mold_prevention_intensity",
    "mold_prevention_notify_enabled",
    "mold_prevention_notify_targets",
    "room_order",
    "group_by_floor",
    "compressor_groups",
)


# _safe_float, _csv_to_points and _compute_target_forecast are imported from
# .services.analytics_service (see imports above) and re-exported so that
# existing callers (incl. tests) keep working.


def _compute_anyone_home(hass: HomeAssistant, settings: dict) -> bool:
    """Return True if at least one tracked person is home (or fail-safe)."""
    from .utils.presence_utils import is_presence_away

    return not is_presence_away(hass, {}, settings)  # all away


def _validate_no_own_entities(config: dict, own_prefix: str) -> str | None:
    """Check that no RoomMind-owned entities are assigned. Returns error message or None."""
    for field in ("thermostats", "acs", "window_sensors", "covers", "occupancy_sensors"):
        for eid in config.get(field, []):
            if eid.split(".", 1)[-1].startswith(own_prefix):
                return f"Cannot assign RoomMind's own entity '{eid}' to a room"
    for device in config.get("devices", []):
        eid = device.get("entity_id", "")
        if eid.split(".", 1)[-1].startswith(own_prefix):
            return f"Cannot assign RoomMind's own entity '{eid}' to a room"
    for field in ("temperature_sensor", "humidity_sensor", "comfort_heat_entity"):
        eid = config.get(field, "")
        if eid and eid.split(".", 1)[-1].startswith(own_prefix):
            return f"Cannot assign RoomMind's own entity '{eid}' to a room"
    return None


def _validate_comfort_entity(config: dict) -> str | None:
    """Check that the comfort source entity is a climate entity. Returns error or None."""
    eid = config.get("comfort_heat_entity", "")
    if eid and not eid.startswith("climate."):
        return f"Comfort setpoint source '{eid}' must be a climate entity"
    return None


async def _async_push_comfort_to_source(hass: HomeAssistant, room: dict, config: dict) -> None:
    """Push a panel-edited comfort_heat to the room's comfort source entity.

    Keeps a wall display (e.g. Aqara W100) showing the same setpoint the panel
    holds. No-op when the room has no source entity, when the value already
    matches, or when the entity cannot take a setpoint right now.
    """
    from .utils.comfort_utils import (
        build_set_temperature_data,
        get_comfort_entity,
        read_comfort_heat_setpoint,
    )

    entity_id = get_comfort_entity(room)
    if not entity_id or "comfort_heat" not in config:
        return

    try:
        target = float(config["comfort_heat"])
    except (TypeError, ValueError):
        return

    current = read_comfort_heat_setpoint(hass, room)
    if current is not None and abs(current - target) < 0.05:
        return

    data = build_set_temperature_data(hass, entity_id, target)
    if data is None:
        return

    try:
        await hass.services.async_call("climate", "set_temperature", data, blocking=False)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Failed to push comfort setpoint to '%s'", entity_id)


def _validate_no_duplicate_devices(config: dict) -> str | None:
    """Check for duplicate entity_ids in devices[]. Returns error message or None."""
    device_eids = [d["entity_id"] for d in config.get("devices", [])]
    if len(device_eids) != len(set(device_eids)):
        return "devices[] contains duplicate entity_ids"
    return None


# ---------------------------------------------------------------------------
# List rooms
# ---------------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "roommind/rooms/list"})
@websocket_api.async_response
async def websocket_list_rooms(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Return all rooms with current state."""
    store = hass.data[DOMAIN]["store"]
    rooms = store.get_rooms()

    # Merge live state from coordinator
    coordinator = _get_coordinator(hass)
    # If coordinator has no data yet but rooms exist, trigger an immediate refresh
    if coordinator and rooms and not coordinator.rooms:
        await coordinator.async_request_refresh()
    live_states = coordinator.rooms if coordinator else {}

    # Outdoor temperature availability gates EKF training (#301).  Surface the
    # paused state per room so the frontend can show an explanatory badge.
    outdoor_available = coordinator is not None and coordinator.outdoor_temp_effective is not None
    settings = store.get_settings()
    learning_disabled = set(settings.get("learning_disabled_rooms", []))

    # Build response: config + live state per room
    # Override fields are computed from the store (always up-to-date) rather
    # than from the coordinator (which refreshes on a 10s cycle).
    result = {}
    for area_id, room_config in rooms.items():
        room_data = dict(room_config)
        live = live_states.get(area_id, {})
        # Managed Mode rooms (no temperature_sensor) never train the EKF, so
        # surfacing "learning paused" would be misleading.
        has_external_sensor = bool(room_config.get("temperature_sensor"))
        if (
            not outdoor_available
            and has_external_sensor
            and area_id not in learning_disabled
            and not room_config.get("is_outdoor", False)
        ):
            learning_paused_reason: str | None = "outdoor_unavailable"
        else:
            learning_paused_reason = None

        room_data["live"] = {
            "current_temp": live.get("current_temp"),
            "current_humidity": live.get("current_humidity"),
            "target_temp": live.get("target_temp"),
            "heat_target": live.get("heat_target"),
            "cool_target": live.get("cool_target"),
            "mode": live.get("mode", "idle"),
            "heating_power": live.get("heating_power", 0),
            "device_setpoint": live.get("device_setpoint"),
            "window_open": live.get("window_open", False),
            **build_override_live(
                room_config,
                suppressed=is_override_suppressed(room_config, settings, live.get("presence_away", False)),
            ),
            "active_schedule_index": live.get("active_schedule_index", -1),
            "confidence": live.get("confidence"),
            "mpc_active": live.get("mpc_active", False),
            "presence_away": live.get("presence_away", False),
            "mold_risk_level": live.get("mold_risk_level", "ok"),
            "mold_surface_rh": live.get("mold_surface_rh"),
            "mold_prevention_active": live.get("mold_prevention_active", False),
            "mold_prevention_delta": live.get("mold_prevention_delta", 0),
            "n_observations": live.get("n_observations", 0),
            "blind_position": live.get("blind_position"),
            "cover_auto_paused": live.get("cover_auto_paused", False),
            "cover_override_until": live.get("cover_override_until"),
            "cover_forced_reason": live.get("cover_forced_reason", ""),
            "active_cover_schedule_index": live.get("active_cover_schedule_index", -1),
            "active_heat_sources": live.get("active_heat_sources"),
            "learning_paused_reason": learning_paused_reason,
            "compressor_protection_active": live.get("compressor_protection_active", False),
            "compressor_protection_reason": live.get("compressor_protection_reason"),
            "coil_dry_active": live.get("coil_dry_active", False),
            "coil_dry_phase": live.get("coil_dry_phase"),
            "coil_dry_until": live.get("coil_dry_until"),
            "coil_dry_entities": live.get("coil_dry_entities", []),
            "schedule_temp_warnings": live.get("schedule_temp_warnings", []),
        }
        result[area_id] = room_data

    # Vacation state from settings
    vacation_until = settings.get("vacation_until")
    vacation_active = bool(vacation_until and time.time() < vacation_until)

    connection.send_result(
        msg["id"],
        {
            "rooms": result,
            "outdoor_temp": coordinator.outdoor_temp_effective if coordinator else None,
            "outdoor_humidity": coordinator.outdoor_humidity if coordinator else None,
            "vacation_active": vacation_active,
            "vacation_temp": settings.get("vacation_temp") if vacation_active else None,
            "vacation_until": vacation_until if vacation_active else None,
            "hidden_rooms": settings.get("hidden_rooms", []),
            "room_order": settings.get("room_order", []),
            "group_by_floor": settings.get("group_by_floor", False),
            "control_mode": settings.get("control_mode", "bangbang"),
            "climate_control_active": settings.get("climate_control_active", True),
            "presence_enabled": settings.get("presence_enabled", False),
            "presence_persons": settings.get("presence_persons", []),
            "presence_away_action": settings.get("presence_away_action", "eco"),
            "presence_clears_override": settings.get("presence_clears_override", False),
            "schedule_off_action": settings.get("schedule_off_action", "eco"),
            "anyone_home": _compute_anyone_home(hass, settings),
            "valve_protection_enabled": settings.get("valve_protection_enabled", False),
            "coil_dry_enabled": settings.get("coil_dry_enabled", False),
            "coil_dry_minutes": settings.get("coil_dry_minutes", DEFAULT_COIL_DRY_MINUTES),
            "coil_dry_mode": settings.get("coil_dry_mode", COIL_DRY_MODE_FAN_ONLY),
            "coil_dry_fan_mode": settings.get("coil_dry_fan_mode", DEFAULT_COIL_DRY_FAN_MODE),
            "coil_dry_min_cooling_minutes": settings.get(
                "coil_dry_min_cooling_minutes", DEFAULT_COIL_DRY_MIN_COOLING_MINUTES
            ),
            "coil_dry_drain_minutes": settings.get("coil_dry_drain_minutes", DEFAULT_COIL_DRY_DRAIN_MINUTES),
            "compressor_groups": settings.get("compressor_groups", []),
        },
    )


# ---------------------------------------------------------------------------
# Save room (upsert: create or update)
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/rooms/save",
        vol.Required("area_id"): str,
        vol.Optional("thermostats"): [str],
        vol.Optional("acs"): [str],
        vol.Optional("devices"): [
            vol.All(
                {
                    vol.Required("entity_id"): str,
                    vol.Required("type"): vol.In(["trv", "ac"]),
                    vol.Optional("role", default="auto"): vol.In(["primary", "secondary", "auto"]),
                    vol.Optional("heating_system_type", default=""): vol.In(["", "radiator", "underfloor"]),
                    vol.Optional("idle_action", default="off"): vol.In(["off", "fan_only", "setback", "low"]),
                    vol.Optional("idle_fan_mode", default="low"): str,
                    vol.Optional("setpoint_mode", default="proportional"): vol.In(["proportional", "direct"]),
                    vol.Optional("coil_dry", default=COIL_DRY_INHERIT): vol.In(COIL_DRY_OVERRIDES),
                    vol.Optional("coil_dry_minutes", default=0): vol.All(vol.Coerce(int), vol.Range(min=0, max=60)),
                    vol.Optional("coil_dry_mode", default=""): vol.In(["", *COIL_DRY_MODES]),
                    vol.Optional("coil_dry_fan_mode", default=""): str,
                },
                _validate_device_idle_action,
                _validate_device_coil_dry,
            )
        ],
        vol.Optional("temperature_sensor"): str,
        vol.Optional("humidity_sensor"): str,
        vol.Optional("occupancy_sensors"): [str],
        vol.Optional("climate_mode"): vol.In(CLIMATE_MODES),
        vol.Optional("schedules"): [{vol.Required("entity_id"): str}],
        vol.Optional("schedule_selector_entity"): str,
        vol.Optional("window_sensors"): [str],
        vol.Optional("window_open_delay"): vol.Coerce(int),
        vol.Optional("window_close_delay"): vol.Coerce(int),
        vol.Optional("comfort_temp"): vol.Coerce(float),
        vol.Optional("eco_temp"): vol.Coerce(float),
        vol.Optional("comfort_heat"): vol.Coerce(float),
        vol.Optional("comfort_cool"): vol.Coerce(float),
        vol.Optional("comfort_heat_entity"): str,
        vol.Optional("eco_heat"): vol.Coerce(float),
        vol.Optional("eco_cool"): vol.Coerce(float),
        vol.Optional("presence_persons"): [str],
        vol.Optional("display_name"): str,
        vol.Optional("heating_system_type"): vol.In(["", "radiator", "underfloor"]),
        vol.Optional("covers"): [str],
        vol.Optional("covers_auto_enabled"): bool,
        vol.Optional("covers_deploy_threshold"): vol.All(vol.Coerce(float), vol.Range(min=0)),
        vol.Optional("covers_min_position"): vol.All(vol.Coerce(int), vol.Range(min=0, max=99)),
        vol.Optional("covers_outdoor_min_temp"): vol.Any(None, vol.All(vol.Coerce(float), vol.Range(min=0, max=35))),
        vol.Optional("covers_override_minutes"): vol.All(vol.Coerce(int), vol.Range(min=0, max=480)),
        vol.Optional("cover_schedules"): [
            {
                vol.Required("entity_id"): str,
                vol.Optional("mode", default="force"): vol.In(["force", "gate"]),
            }
        ],
        vol.Optional("cover_schedule_selector_entity"): str,
        vol.Optional("cover_orientations"): {str: vol.All(vol.Coerce(int), vol.Range(min=0, max=359))},
        vol.Optional("covers_night_close"): bool,
        vol.Optional("covers_night_close_elevation"): vol.All(vol.Coerce(float), vol.Range(min=-18, max=10)),
        vol.Optional("covers_night_close_offset_minutes"): vol.All(vol.Coerce(int), vol.Range(min=-120, max=120)),
        vol.Optional("covers_night_position"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("covers_snap_deploy"): bool,
        vol.Optional("cover_min_positions"): {str: vol.All(vol.Coerce(int), vol.Range(min=0, max=99))},
        vol.Optional("ignore_presence"): bool,
        vol.Optional("is_outdoor"): bool,
        vol.Optional("valve_protection_exclude"): [str],
        vol.Optional("heat_source_orchestration"): bool,
        vol.Optional("heat_source_primary_delta"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=5.0)),
        vol.Optional("heat_source_outdoor_threshold"): vol.All(vol.Coerce(float), vol.Range(min=-20, max=25)),
        vol.Optional("heat_source_ac_min_outdoor"): vol.All(vol.Coerce(float), vol.Range(min=-30, max=5)),
        vol.Optional("climate_control_enabled"): bool,
    }
)
@websocket_api.async_response
async def websocket_save_room(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Create or update a room configuration."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]

    # Build config dict from optional fields present in the message
    config: dict = {}
    for key in _ROOM_SAVE_FIELDS:
        if key in msg:
            config[key] = msg[key]

    # Reject RoomMind's own entities to prevent self-assignment (#86)
    own_prefix = f"{DOMAIN}_"
    err = _validate_no_own_entities(config, own_prefix)
    if err:
        connection.send_error(msg["id"], "invalid_entity", err)
        return
    # Reject duplicate entity_ids in devices[]
    err = _validate_no_duplicate_devices(config)
    if err:
        connection.send_error(msg["id"], "duplicate_entity", err)
        return
    # Comfort setpoint source must be a climate entity
    err = _validate_comfort_entity(config)
    if err:
        connection.send_error(msg["id"], "invalid_entity", err)
        return

    if ("thermostats" in config or "acs" in config) and "devices" not in config:
        _LOGGER.warning(
            "Room save for '%s' uses legacy thermostats/acs fields without devices. "
            "This is deprecated and will be removed in a future version.",
            area_id,
        )

    room = await store.async_save_room(area_id, config)

    # Mirror a panel-edited comfort temperature onto the source entity, so the
    # coordinator's next sync does not pull the old device value back in.
    await _async_push_comfort_to_source(hass, room, config)

    # Notify coordinator to create/update sensor entities for the room
    coordinator = _get_coordinator(hass)
    if coordinator:
        await coordinator.async_room_added(room)

    connection.send_result(msg["id"], {"room": room})


# ---------------------------------------------------------------------------
# Delete room
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/rooms/delete",
        vol.Required("area_id"): str,
    }
)
@websocket_api.async_response
async def websocket_delete_room(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Delete a room."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]

    try:
        await store.async_delete_room(area_id)
    except KeyError:
        connection.send_error(msg["id"], "not_found", f"Room '{area_id}' not found")
        return

    # Notify coordinator to remove sensor entities for the deleted room
    coordinator = _get_coordinator(hass)
    if coordinator:
        await coordinator.async_room_removed(area_id)

    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Set override
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/override/set",
        vol.Required("area_id"): str,
        vol.Required("override_type"): vol.In(OVERRIDE_TYPES),
        vol.Optional("heat"): vol.Coerce(float),
        vol.Optional("cool"): vol.Coerce(float),
        vol.Optional("duration"): vol.Coerce(float),  # hours (omit or 0 for permanent)
    }
)
@websocket_api.async_response
async def websocket_override_set(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Set a temporary override for a room."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]
    override_type = msg["override_type"]
    duration_hours = msg.get("duration")

    room = store.get_room(area_id)
    if room is None:
        connection.send_error(msg["id"], "not_found", f"Room '{area_id}' not found")
        return

    climate_mode = room.get("climate_mode", "auto")

    if override_type == "boost":
        heat = room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT))
        cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL)
    elif override_type == "eco":
        heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
        cool = room.get("eco_cool", DEFAULT_ECO_COOL)
    else:  # custom
        heat = msg.get("heat")
        cool = msg.get("cool")
        if heat is None and cool is None:
            connection.send_error(msg["id"], "invalid", "Custom override requires heat and/or cool")
            return

    if climate_mode == "heat_only":
        cool = None
    elif climate_mode == "cool_only":
        heat = None

    if heat is not None and cool is not None and cool < heat:
        connection.send_error(msg["id"], "invalid", "Cooling target must be >= heating target")
        return

    override_until = (time.time() + duration_hours * 3600) if duration_hours else None

    await store.async_update_room(
        area_id,
        {
            "override_heat": heat,
            "override_cool": cool,
            "override_until": override_until,
            "override_type": override_type,
        },
    )

    coordinator = _get_coordinator(hass)
    if coordinator:
        await coordinator.async_request_refresh()

    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Clear override
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/override/clear",
        vol.Required("area_id"): str,
    }
)
@websocket_api.async_response
async def websocket_override_clear(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Clear an active override for a room."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]

    room = store.get_room(area_id)
    if room is None:
        connection.send_error(msg["id"], "not_found", f"Room '{area_id}' not found")
        return

    await store.async_update_room(
        area_id,
        {
            "override_heat": None,
            "override_cool": None,
            "override_until": None,
            "override_type": None,
        },
    )

    coordinator = _get_coordinator(hass)
    if coordinator:
        await coordinator.async_request_refresh()

    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Get settings
# ---------------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "roommind/settings/get"})
@websocket_api.async_response
async def websocket_get_settings(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Return global settings."""
    store = hass.data[DOMAIN]["store"]
    connection.send_result(msg["id"], {"settings": store.get_settings()})


# ---------------------------------------------------------------------------
# Save settings
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/settings/save",
        vol.Optional("outdoor_temp_sensor"): str,
        vol.Optional("outdoor_humidity_sensor"): str,
        vol.Optional("outdoor_cooling_min"): vol.Coerce(float),
        vol.Optional("outdoor_heating_max"): vol.Coerce(float),
        vol.Optional("control_mode"): vol.In(["mpc", "bangbang"]),
        vol.Optional("comfort_weight"): vol.Coerce(float),
        vol.Optional("weather_entity"): str,
        vol.Optional("outdoor_unavailable_notify"): bool,
        vol.Optional("climate_control_active"): bool,
        vol.Optional("learning_disabled_rooms"): [str],
        vol.Optional("hidden_rooms"): [str],
        vol.Optional("prediction_enabled"): bool,
        vol.Optional("vacation_temp"): vol.Coerce(float),
        vol.Optional("vacation_until"): vol.Any(vol.Coerce(float), None),
        vol.Optional("presence_enabled"): bool,
        vol.Optional("presence_persons"): [str],
        vol.Optional("presence_away_action"): vol.In(["eco", "off"]),
        vol.Optional("presence_clears_override"): bool,
        vol.Optional("schedule_off_action"): vol.In(["eco", "off"]),
        vol.Optional("valve_protection_enabled"): bool,
        vol.Optional("valve_protection_interval_days"): vol.All(vol.Coerce(int), vol.Range(min=1, max=90)),
        vol.Optional("coil_dry_enabled"): bool,
        vol.Optional("coil_dry_minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        vol.Optional("coil_dry_mode"): vol.In(COIL_DRY_MODES),
        vol.Optional("coil_dry_fan_mode"): str,
        vol.Optional("coil_dry_min_cooling_minutes"): vol.All(vol.Coerce(int), vol.Range(min=1, max=240)),
        vol.Optional("coil_dry_drain_minutes"): vol.All(vol.Coerce(int), vol.Range(min=0, max=15)),
        vol.Optional("mold_detection_enabled"): bool,
        vol.Optional("mold_humidity_threshold"): vol.All(vol.Coerce(float), vol.Range(min=50, max=90)),
        vol.Optional("mold_sustained_minutes"): vol.All(vol.Coerce(int), vol.Range(min=5, max=120)),
        vol.Optional("mold_notification_cooldown"): vol.All(vol.Coerce(int), vol.Range(min=10, max=1440)),
        vol.Optional("mold_notifications_enabled"): bool,
        vol.Optional("mold_notification_targets"): [
            {
                vol.Required("entity_id"): str,
                vol.Optional("person_entity", default=""): str,
                vol.Optional("notify_when", default="always"): vol.In(["always", "home_only"]),
            }
        ],
        vol.Optional("mold_prevention_enabled"): bool,
        vol.Optional("mold_prevention_intensity"): vol.In(["light", "medium", "strong"]),
        vol.Optional("mold_prevention_notify_enabled"): bool,
        vol.Optional("mold_prevention_notify_targets"): [
            {
                vol.Required("entity_id"): str,
                vol.Optional("person_entity", default=""): str,
                vol.Optional("notify_when", default="always"): vol.In(["always", "home_only"]),
            }
        ],
        vol.Optional("room_order"): [str],
        vol.Optional("group_by_floor"): bool,
        vol.Optional("compressor_groups"): [
            {
                vol.Required("id"): str,
                vol.Required("name"): str,
                vol.Required("members"): vol.All([str], vol.Length(min=1)),
                vol.Optional("min_run_minutes", default=DEFAULT_COMPRESSOR_MIN_RUN_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
                vol.Optional("min_off_minutes", default=DEFAULT_COMPRESSOR_MIN_OFF_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=30)
                ),
                vol.Optional("master_entity", default=""): str,
                vol.Optional("conflict_resolution", default=DEFAULT_CONFLICT_RESOLUTION): vol.In(CONFLICT_RESOLUTIONS),
                vol.Optional("action_script", default=""): str,
                vol.Optional("enforce_uniform_mode", default=False): bool,
            }
        ],
    }
)
@websocket_api.async_response
async def websocket_save_settings(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Save global settings (partial merge)."""
    store = hass.data[DOMAIN]["store"]
    changes: dict = {}
    for key in _SETTINGS_SAVE_FIELDS:
        if key in msg:
            changes[key] = msg[key]

    # Validate compressor groups
    groups = changes.get("compressor_groups")
    if groups:
        group_ids = [g.get("id", "") for g in groups]
        if len(group_ids) != len(set(group_ids)):
            connection.send_error(
                msg["id"],
                "duplicate_group_id",
                "Compressor group IDs must be unique",
            )
            return
        all_members: list[str] = []
        for g in groups:
            for eid in g.get("members", []):
                if not eid.startswith("climate."):
                    connection.send_error(
                        msg["id"],
                        "invalid_member",
                        f"Compressor group member '{eid}' is not a climate entity",
                    )
                    return
            all_members.extend(g.get("members", []))
        if len(all_members) != len(set(all_members)):
            connection.send_error(
                msg["id"],
                "duplicate_member",
                "A climate entity cannot be in multiple compressor groups",
            )
            return

        # Validate master_entity and action_script fields
        all_masters: list[str] = []
        for g in groups:
            master = g.get("master_entity", "")
            if master:
                if not master.startswith("climate."):
                    connection.send_error(
                        msg["id"],
                        "invalid_master_entity",
                        f"Master entity '{master}' must be a climate entity",
                    )
                    return
                if master in g.get("members", []):
                    connection.send_error(
                        msg["id"],
                        "master_in_members",
                        f"Master entity '{master}' cannot also be a group member",
                    )
                    return
                if master in all_members:
                    connection.send_error(
                        msg["id"],
                        "master_is_other_member",
                        f"Master entity '{master}' is a member of another group",
                    )
                    return
                all_masters.append(master)
            script = g.get("action_script", "")
            if script and not script.startswith("script."):
                connection.send_error(
                    msg["id"],
                    "invalid_action_script",
                    f"Action script '{script}' must be a script entity",
                )
                return
        if len(all_masters) != len(set(all_masters)):
            connection.send_error(
                msg["id"],
                "duplicate_master",
                "A master entity cannot be assigned to multiple groups",
            )
            return

    settings = await store.async_save_settings(changes)
    connection.send_result(msg["id"], {"settings": settings})


# ---------------------------------------------------------------------------
# Target temperature forecast (for analytics chart)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Get analytics data
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/analytics/get",
        vol.Required("area_id"): str,
        vol.Optional("range"): vol.In(["12h", "24h", "2d", "7d", "14d", "30d", "90d"]),
        vol.Optional("start_ts"): vol.Coerce(float),
        vol.Optional("end_ts"): vol.Coerce(float),
    }
)
@websocket_api.async_response
async def websocket_get_analytics(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Return analytics data for a room."""
    store = hass.data[DOMAIN]["store"]
    coordinator = _get_coordinator(hass)
    result = await build_analytics_data(
        hass,
        msg["area_id"],
        msg.get("range", "12h"),
        store,
        coordinator,
        custom_start=msg.get("start_ts"),
        custom_end=msg.get("end_ts"),
    )
    connection.send_result(msg["id"], result)


# ---------------------------------------------------------------------------
# Reset thermal model (per room)
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/thermal/reset",
        vol.Required("area_id"): str,
    }
)
@websocket_api.async_response
async def websocket_thermal_reset(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Reset thermal model and history for a single room."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]
    coordinator = _get_coordinator(hass)

    # Clear learned model and residual heat tracking
    if coordinator:
        coordinator.reset_thermal_room(area_id)

    # Clear persisted thermal data
    await store.async_clear_thermal_data_room(area_id)

    # Clear history CSV files
    if coordinator and coordinator.history_store:
        await hass.async_add_executor_job(coordinator.history_store.remove_room, area_id)

    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Reset thermal model (all rooms)
# ---------------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "roommind/thermal/reset_all"})
@websocket_api.async_response
async def websocket_thermal_reset_all(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Reset thermal model and history for all rooms."""
    store = hass.data[DOMAIN]["store"]
    coordinator = _get_coordinator(hass)

    # Clear all learned models — replace entire manager for clean state
    room_ids: list[str] = []
    if coordinator:
        room_ids = coordinator.reset_thermal_all()

    # Clear persisted thermal data
    await store.async_clear_all_thermal_data()

    # Clear history CSV files for all rooms
    if coordinator and coordinator.history_store:
        for area_id in room_ids:
            await hass.async_add_executor_job(coordinator.history_store.remove_room, area_id)

    connection.send_result(msg["id"], {"success": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/model/boost_learning",
        vol.Required("area_id"): str,
    }
)
@websocket_api.async_response
async def websocket_boost_learning(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Boost EKF covariance for a room to accelerate re-learning."""
    store = hass.data[DOMAIN]["store"]
    coordinator = _get_coordinator(hass)
    area_id = msg["area_id"]

    if not coordinator:
        connection.send_error(msg["id"], "no_coordinator", "Coordinator not ready")
        return

    n_obs = coordinator.boost_learning(area_id)

    # Persist cooldown anchor in settings
    settings = store.get_settings()
    boost_applied = dict(settings.get("boost_applied_at", {}))
    boost_applied[area_id] = n_obs
    await store.async_save_settings({"boost_applied_at": boost_applied})

    connection.send_result(msg["id"], {"success": True, "n_observations": n_obs})


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "roommind/diagnostics/get"})
@websocket_api.async_response
async def websocket_get_diagnostics(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    """Return full integration diagnostics via WebSocket."""
    from .diagnostics import async_get_config_entry_diagnostics

    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_found", "No config entry found")
        return
    result = await async_get_config_entry_diagnostics(hass, entries[0])
    connection.send_result(msg["id"], result)


# ---------------------------------------------------------------------------
# Clear cover user override
# ---------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "roommind/covers/clear_override",
        vol.Required("area_id"): str,
    }
)
@websocket_api.async_response
async def websocket_covers_clear_override(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict,
) -> None:
    """Clear a user cover override so automatic cover control resumes."""
    store = hass.data[DOMAIN]["store"]
    area_id = msg["area_id"]
    if store.get_room(area_id) is None:
        connection.send_error(msg["id"], "not_found", f"Room '{area_id}' not found")
        return

    coordinator = _get_coordinator(hass)
    if coordinator:
        coordinator.clear_cover_override(area_id)
        await coordinator.async_request_refresh()

    connection.send_result(msg["id"], {"success": True})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Register all RoomMind WebSocket commands."""
    websocket_api.async_register_command(hass, websocket_list_rooms)
    websocket_api.async_register_command(hass, websocket_save_room)
    websocket_api.async_register_command(hass, websocket_delete_room)
    websocket_api.async_register_command(hass, websocket_override_set)
    websocket_api.async_register_command(hass, websocket_override_clear)
    websocket_api.async_register_command(hass, websocket_get_settings)
    websocket_api.async_register_command(hass, websocket_save_settings)
    websocket_api.async_register_command(hass, websocket_get_analytics)
    websocket_api.async_register_command(hass, websocket_thermal_reset)
    websocket_api.async_register_command(hass, websocket_thermal_reset_all)
    websocket_api.async_register_command(hass, websocket_boost_learning)
    websocket_api.async_register_command(hass, websocket_get_diagnostics)
    websocket_api.async_register_command(hass, websocket_covers_clear_override)
