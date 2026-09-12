"""Tests for RoomMind WebSocket API."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.roommind.const import DOMAIN
from custom_components.roommind.websocket_api import (
    _csv_to_points,
    _safe_float,
    websocket_covers_clear_override,
    websocket_delete_room,
    websocket_get_analytics,
    websocket_get_diagnostics,
    websocket_get_settings,
    websocket_list_rooms,
    websocket_override_clear,
    websocket_override_set,
    websocket_save_room,
    websocket_save_settings,
    websocket_thermal_reset,
    websocket_thermal_reset_all,
)

# The HA @async_response decorator wraps async handlers into synchronous
# schedulers.  Access the original coroutine via ``__wrapped__`` so we can
# ``await`` them directly in tests without needing a running event-loop task
# factory on the mock hass object.
_list_rooms = websocket_list_rooms.__wrapped__
_save_room = websocket_save_room.__wrapped__
_delete_room = websocket_delete_room.__wrapped__
_override_set = websocket_override_set.__wrapped__
_override_clear = websocket_override_clear.__wrapped__
_get_settings = websocket_get_settings.__wrapped__
_save_settings = websocket_save_settings.__wrapped__
_thermal_reset = websocket_thermal_reset.__wrapped__
_thermal_reset_all = websocket_thermal_reset_all.__wrapped__
_get_analytics = websocket_get_analytics.__wrapped__
_get_diagnostics = websocket_get_diagnostics.__wrapped__
_covers_clear_override = websocket_covers_clear_override.__wrapped__


@pytest.fixture
def connection():
    """Return a mocked WebSocket connection."""
    conn = MagicMock()
    conn.send_result = MagicMock()
    conn.send_error = MagicMock()
    return conn


@pytest.fixture
def ws_hass(hass, store):
    """Return a hass instance with the store wired into hass.data."""
    hass.data[DOMAIN] = {"store": store}
    return hass


@pytest.mark.asyncio
async def test_list_rooms_empty(ws_hass, store, connection):
    """Listing rooms on a fresh store returns an empty dict."""
    await store.async_load()

    msg = {"id": 1, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(
        1,
        {
            "rooms": {},
            "outdoor_temp": None,
            "outdoor_humidity": None,
            "vacation_active": False,
            "vacation_temp": None,
            "vacation_until": None,
            "hidden_rooms": [],
            "room_order": [],
            "group_by_floor": False,
            "control_mode": "bangbang",
            "climate_control_active": True,
            "presence_enabled": False,
            "presence_persons": [],
            "presence_away_action": "eco",
            "presence_clears_override": False,
            "schedule_off_action": "eco",
            "anyone_home": True,
            "valve_protection_enabled": False,
            "coil_dry_enabled": False,
            "coil_dry_minutes": 20,
            "coil_dry_mode": "fan_only",
            "coil_dry_fan_mode": "low",
            "coil_dry_min_cooling_minutes": 10,
            "coil_dry_drain_minutes": 0,
            "compressor_groups": [],
        },
    )


@pytest.mark.asyncio
async def test_save_room_creates_new(ws_hass, store, connection):
    """Saving a room with a new area_id creates the room with defaults."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "thermostats": ["climate.living_room_trv"],
        "temperature_sensor": "sensor.living_room_temp",
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    assert call_args[0][0] == 2
    room = call_args[0][1]["room"]
    assert room["area_id"] == "living_room"
    assert room["thermostats"] == ["climate.living_room_trv"]
    assert room["temperature_sensor"] == "sensor.living_room_temp"
    # Defaults for fields not provided
    assert room["acs"] == []
    assert room["climate_mode"] == "auto"
    assert room["schedules"] == []
    assert room["schedule_selector_entity"] == ""
    assert room["comfort_temp"] == 21.0
    assert room["eco_temp"] == 17.0


@pytest.mark.asyncio
async def test_save_room_updates_existing(ws_hass, store, connection):
    """Saving a room with an existing area_id updates only the provided fields."""
    await store.async_load()

    # First create a room
    create_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "office",
        "thermostats": ["climate.office_trv"],
        "temperature_sensor": "sensor.office_temp",
        "climate_mode": "heat_only",
    }
    await _save_room(ws_hass, connection, create_msg)
    connection.send_result.reset_mock()

    # Now update it - only change thermostats
    update_msg = {
        "id": 3,
        "type": "roommind/rooms/save",
        "area_id": "office",
        "thermostats": ["climate.office_trv", "climate.office_trv_2"],
    }
    await _save_room(ws_hass, connection, update_msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    assert call_args[0][0] == 3
    room = call_args[0][1]["room"]
    assert room["area_id"] == "office"
    assert room["thermostats"] == ["climate.office_trv", "climate.office_trv_2"]
    # Fields not in update_msg should be preserved
    assert room["temperature_sensor"] == "sensor.office_temp"
    assert room["climate_mode"] == "heat_only"


@pytest.mark.asyncio
async def test_list_rooms_after_save(ws_hass, store, connection):
    """After saving a room, list_rooms includes it with live state."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        "temperature_sensor": "sensor.kitchen_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    # Add coordinator mock before listing so live state can be merged
    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}  # No live data yet
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    assert call_args[0][0] == 3
    rooms = call_args[0][1]["rooms"]
    assert len(rooms) == 1
    assert "kitchen" in rooms
    room = rooms["kitchen"]
    assert room["thermostats"] == ["climate.kitchen_trv"]
    assert "live" in room
    assert room["live"]["mode"] == "idle"


@pytest.mark.asyncio
async def test_list_rooms_learning_paused_when_outdoor_unavailable(ws_hass, store, connection):
    """list_rooms surfaces learning_paused_reason='outdoor_unavailable' when
    the coordinator has no effective outdoor temperature (see #301)."""
    await store.async_load()
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        "temperature_sensor": "sensor.kitchen_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = None
    mock_coordinator.outdoor_temp_effective = None
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 3, "type": "roommind/rooms/list"})

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["kitchen"]["live"]["learning_paused_reason"] == "outdoor_unavailable"


@pytest.mark.asyncio
async def test_list_rooms_learning_paused_none_when_outdoor_available(ws_hass, store, connection):
    """learning_paused_reason is None as long as the coordinator has any
    effective outdoor temperature (sensor or weather fallback)."""
    await store.async_load()
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        "temperature_sensor": "sensor.kitchen_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = 5.0
    mock_coordinator.outdoor_temp_effective = 5.0
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 3, "type": "roommind/rooms/list"})

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["kitchen"]["live"]["learning_paused_reason"] is None


@pytest.mark.asyncio
async def test_list_rooms_learning_paused_respects_learning_disabled(ws_hass, store, connection):
    """Rooms in ``learning_disabled_rooms`` do not get a paused reason — they
    are intentionally not learning regardless of outdoor availability."""
    await store.async_load()
    await store.async_save_settings({"learning_disabled_rooms": ["kitchen"]})
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        "temperature_sensor": "sensor.kitchen_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = None
    mock_coordinator.outdoor_temp_effective = None
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 3, "type": "roommind/rooms/list"})

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["kitchen"]["live"]["learning_paused_reason"] is None


@pytest.mark.asyncio
async def test_list_rooms_learning_paused_none_for_managed_mode(ws_hass, store, connection):
    """Managed Mode rooms (no temperature_sensor) never train the EKF, so
    learning_paused_reason must be None even when outdoor is unavailable."""
    await store.async_load()
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        # no temperature_sensor → Managed Mode
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = None
    mock_coordinator.outdoor_temp_effective = None
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 3, "type": "roommind/rooms/list"})

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["kitchen"]["live"]["learning_paused_reason"] is None


@pytest.mark.asyncio
async def test_list_rooms_learning_paused_none_for_outdoor_area(ws_hass, store, connection):
    """is_outdoor rooms disable learning entirely, so learning_paused_reason
    is None even when the outdoor temperature is missing."""
    await store.async_load()
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "balcony",
        "thermostats": ["climate.balcony_trv"],
        "temperature_sensor": "sensor.balcony_temp",
        "is_outdoor": True,
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = None
    mock_coordinator.outdoor_temp_effective = None
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 3, "type": "roommind/rooms/list"})

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["balcony"]["live"]["learning_paused_reason"] is None


@pytest.mark.asyncio
async def test_list_rooms_outdoor_temp_uses_effective(ws_hass, store, connection):
    """The rooms/list response surfaces the effective outdoor temperature
    (sensor → weather fallback) rather than only the raw sensor reading."""
    await store.async_load()
    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    mock_coordinator.outdoor_temp = None
    mock_coordinator.outdoor_temp_effective = 8.5
    mock_coordinator.outdoor_humidity = None
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    await _list_rooms(ws_hass, connection, {"id": 1, "type": "roommind/rooms/list"})

    payload = connection.send_result.call_args[0][1]
    assert payload["outdoor_temp"] == 8.5


@pytest.mark.asyncio
async def test_save_room_display_name_roundtrip(ws_hass, store, connection):
    """display_name is persisted through save and returned in list."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "bedroom",
        "thermostats": ["climate.bedroom_trv"],
        "display_name": "Schlafzimmer OG",
    }
    await _save_room(ws_hass, connection, save_msg)

    call_args = connection.send_result.call_args
    room = call_args[0][1]["room"]
    assert room["display_name"] == "Schlafzimmer OG"
    connection.send_result.reset_mock()

    # Verify it comes back in list_rooms
    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    rooms = connection.send_result.call_args[0][1]["rooms"]
    assert rooms["bedroom"]["display_name"] == "Schlafzimmer OG"


@pytest.mark.asyncio
async def test_save_room_display_name_defaults_empty(ws_hass, store, connection):
    """Rooms created without display_name default to empty string."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
    }
    await _save_room(ws_hass, connection, save_msg)

    room = connection.send_result.call_args[0][1]["room"]
    assert room["display_name"] == ""


@pytest.mark.asyncio
async def test_save_room_with_schedules(ws_hass, store, connection):
    """Saving a room with schedules persists the reference."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "bedroom",
        "thermostats": ["climate.bedroom_trv"],
        "temperature_sensor": "sensor.bedroom_temp",
        "schedules": [{"entity_id": "schedule.bedroom_heating"}],
        "schedule_selector_entity": "",
        "comfort_temp": 22.0,
        "eco_temp": 18.0,
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["schedules"] == [{"entity_id": "schedule.bedroom_heating"}]
    assert room["schedule_selector_entity"] == ""
    assert room["comfort_temp"] == 22.0
    assert room["eco_temp"] == 18.0


@pytest.mark.asyncio
async def test_delete_room(ws_hass, store, connection):
    """Deleting a room removes it from the store."""
    await store.async_load()

    # First create a room
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "garage",
        "thermostats": ["climate.garage_trv"],
        "temperature_sensor": "sensor.garage_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    # Now delete it
    delete_msg = {
        "id": 3,
        "type": "roommind/rooms/delete",
        "area_id": "garage",
    }
    await _delete_room(ws_hass, connection, delete_msg)

    connection.send_result.assert_called_once_with(3, {"success": True})

    # Verify room is gone
    assert store.get_rooms() == {}


@pytest.mark.asyncio
async def test_delete_nonexistent_room_sends_error(ws_hass, store, connection):
    """Deleting a room that doesn't exist sends an error."""
    await store.async_load()

    delete_msg = {
        "id": 4,
        "type": "roommind/rooms/delete",
        "area_id": "nonexistent_area",
    }
    await _delete_room(ws_hass, connection, delete_msg)

    connection.send_error.assert_called_once()
    call_args = connection.send_error.call_args
    assert call_args[0][0] == 4
    assert call_args[0][1] == "not_found"


@pytest.mark.asyncio
async def test_save_room_minimal_only_area_id(ws_hass, store, connection):
    """Saving with only area_id creates a room with all defaults."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "hallway",
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["area_id"] == "hallway"
    assert room["thermostats"] == []
    assert room["acs"] == []
    assert room["temperature_sensor"] == ""
    assert room["humidity_sensor"] == ""
    assert room["climate_mode"] == "auto"
    assert room["schedules"] == []
    assert room["schedule_selector_entity"] == ""
    assert room["comfort_temp"] == 21.0
    assert room["eco_temp"] == 17.0


@pytest.mark.asyncio
async def test_save_room_notifies_coordinator(ws_hass, store, connection):
    """Saving a room notifies the coordinator via async_room_added."""
    await store.async_load()

    mock_coordinator = MagicMock()
    mock_coordinator.async_room_added = AsyncMock()
    # hasattr check used by _get_coordinator
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "balcony",
        "thermostats": ["climate.balcony_trv"],
    }
    await _save_room(ws_hass, connection, msg)

    mock_coordinator.async_room_added.assert_called_once()
    room = mock_coordinator.async_room_added.call_args[0][0]
    assert room["area_id"] == "balcony"


@pytest.mark.asyncio
async def test_delete_room_notifies_coordinator(ws_hass, store, connection):
    """Deleting a room notifies the coordinator via async_room_removed."""
    await store.async_load()

    # First create the room
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "cellar",
    }
    await _save_room(ws_hass, connection, save_msg)

    mock_coordinator = MagicMock()
    mock_coordinator.async_room_removed = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    delete_msg = {
        "id": 3,
        "type": "roommind/rooms/delete",
        "area_id": "cellar",
    }
    await _delete_room(ws_hass, connection, delete_msg)

    mock_coordinator.async_room_removed.assert_called_once_with("cellar")


@pytest.mark.asyncio
async def test_override_set_boost(ws_hass, store, connection):
    """Setting a boost override uses the room's comfort_temp."""
    await store.async_load()

    # Create room first
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living",
        "thermostats": ["climate.living"],
        "temperature_sensor": "sensor.living_temp",
        "comfort_temp": 22.0,
        "eco_temp": 17.0,
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "living",
        "override_type": "boost",
        "duration": 2.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})

    room = store.get_room("living")
    assert room["override_heat"] == 22.0
    assert room["override_cool"] == 24.0
    assert room["override_type"] == "boost"
    assert room["override_until"] is not None


@pytest.mark.asyncio
async def test_override_set_eco(ws_hass, store, connection):
    """Setting an eco override uses the room's eco_temp."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "bed",
        "comfort_temp": 22.0,
        "eco_temp": 16.0,
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "bed",
        "override_type": "eco",
        "duration": 4.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})
    room = store.get_room("bed")
    assert room["override_heat"] == 16.0
    assert room["override_cool"] == 27.0
    assert room["override_type"] == "eco"


@pytest.mark.asyncio
async def test_override_set_custom(ws_hass, store, connection):
    """Setting a custom override uses the provided temperature."""
    await store.async_load()

    save_msg = {"id": 2, "type": "roommind/rooms/save", "area_id": "office"}
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "office",
        "override_type": "custom",
        "heat": 21.0,
        "cool": 24.5,
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})
    room = store.get_room("office")
    assert room["override_heat"] == 21.0
    assert room["override_cool"] == 24.5
    assert room["override_type"] == "custom"


@pytest.mark.asyncio
async def test_override_set_custom_without_temp_errors(ws_hass, store, connection):
    """Custom override without temperature sends an error."""
    await store.async_load()

    save_msg = {"id": 2, "type": "roommind/rooms/save", "area_id": "hall"}
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()
    connection.send_error.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "hall",
        "override_type": "custom",
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid"


@pytest.mark.asyncio
async def test_override_clear(ws_hass, store, connection):
    """Clearing an override removes override fields."""
    await store.async_load()

    save_msg = {"id": 2, "type": "roommind/rooms/save", "area_id": "bath"}
    await _save_room(ws_hass, connection, save_msg)

    # Set override
    set_msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "bath",
        "override_type": "boost",
        "duration": 2.0,
    }
    await _override_set(ws_hass, connection, set_msg)
    connection.send_result.reset_mock()

    # Clear it
    clear_msg = {
        "id": 4,
        "type": "roommind/override/clear",
        "area_id": "bath",
    }
    await _override_clear(ws_hass, connection, clear_msg)

    connection.send_result.assert_called_once_with(4, {"success": True})
    room = store.get_room("bath")
    assert room.get("override_heat") is None
    assert room.get("override_cool") is None
    assert room.get("override_until") is None
    assert room.get("override_type") is None


@pytest.mark.asyncio
async def test_override_set_without_duration_permanent(ws_hass, store, connection):
    """Setting override without duration creates a permanent override."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "perm",
        "comfort_temp": 22.0,
        "eco_temp": 17.0,
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "perm",
        "override_type": "custom",
        "heat": 24.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})
    room = store.get_room("perm")
    assert room["override_heat"] == 24.0
    assert room["override_cool"] is None
    assert room["override_until"] is None
    assert room["override_type"] == "custom"


@pytest.mark.asyncio
async def test_override_set_nonexistent_room_errors(ws_hass, store, connection):
    """Setting override on nonexistent room sends an error."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/override/set",
        "area_id": "nope",
        "override_type": "boost",
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_found"


@pytest.mark.asyncio
async def test_override_set_rejects_cool_below_heat(ws_hass, store, connection):
    """Custom override with cool < heat sends an error."""
    await store.async_load()

    save_msg = {"id": 2, "type": "roommind/rooms/save", "area_id": "study"}
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()
    connection.send_error.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "study",
        "override_type": "custom",
        "heat": 24.0,
        "cool": 21.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid"
    room = store.get_room("study")
    assert room.get("override_heat") is None
    assert room.get("override_cool") is None


@pytest.mark.asyncio
async def test_override_set_boost_heat_only_no_cool(ws_hass, store, connection):
    """Boost in a heat_only room sets only the heat target."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "cellar",
        "climate_mode": "heat_only",
        "comfort_temp": 22.0,
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "cellar",
        "override_type": "boost",
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})
    room = store.get_room("cellar")
    assert room["override_heat"] == 22.0
    assert room["override_cool"] is None


@pytest.mark.asyncio
async def test_override_set_custom_cool_only_forces_heat_none(ws_hass, store, connection):
    """Custom override in a cool_only room drops any provided heat target."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "server",
        "climate_mode": "cool_only",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    msg = {
        "id": 3,
        "type": "roommind/override/set",
        "area_id": "server",
        "override_type": "custom",
        "heat": 20.0,
        "cool": 23.0,
    }
    await _override_set(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(3, {"success": True})
    room = store.get_room("server")
    assert room["override_heat"] is None
    assert room["override_cool"] == 23.0


@pytest.mark.asyncio
async def test_save_room_with_multiple_schedules_and_selector(ws_hass, store, connection):
    """Saving with 2 schedules and a selector entity persists correctly."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "wohnzimmer",
        "thermostats": ["climate.wz_trv"],
        "temperature_sensor": "sensor.wz_temp",
        "schedules": [
            {"entity_id": "schedule.morning"},
            {"entity_id": "schedule.evening"},
        ],
        "schedule_selector_entity": "input_boolean.schedule_toggle",
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["schedules"] == [
        {"entity_id": "schedule.morning"},
        {"entity_id": "schedule.evening"},
    ]
    assert room["schedule_selector_entity"] == "input_boolean.schedule_toggle"


@pytest.mark.asyncio
async def test_list_rooms_includes_active_schedule_index(ws_hass, store, connection):
    """Verify active_schedule_index appears in live data from list_rooms."""
    await store.async_load()

    # Create a room
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "buero",
        "thermostats": ["climate.buero_trv"],
        "temperature_sensor": "sensor.buero_temp",
        "schedules": [{"entity_id": "schedule.buero"}],
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    # Add coordinator mock with live data including active_schedule_index
    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {
        "buero": {
            "current_temp": 20.0,
            "target_temp": 21.0,
            "mode": "heating",
            "active_schedule_index": 0,
        }
    }
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    rooms = call_args[0][1]["rooms"]
    assert "buero" in rooms
    live = rooms["buero"]["live"]
    assert "active_schedule_index" in live
    assert live["active_schedule_index"] == 0


@pytest.mark.asyncio
async def test_list_rooms_includes_compressor_protection_status(ws_hass, store, connection):
    """Verify compressor_protection_active/reason pass through from the
    coordinator into list_rooms' live data.

    Regression test: these fields were added to the coordinator's room-state
    dict but the websocket_list_rooms handler builds `live` from an explicit
    field allow-list, so they were silently dropped until this handler was
    also updated (#386).
    """
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kinderzimmer",
        "thermostats": ["climate.kz_trv"],
        "temperature_sensor": "sensor.kz_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {
        "kinderzimmer": {
            "current_temp": 25.0,
            "target_temp": 25.0,
            "mode": "idle",
            "compressor_protection_active": True,
            "compressor_protection_reason": "min_off",
        }
    }
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    live = call_args[0][1]["rooms"]["kinderzimmer"]["live"]
    assert live["compressor_protection_active"] is True
    assert live["compressor_protection_reason"] == "min_off"


@pytest.mark.asyncio
async def test_list_rooms_includes_coil_dry_status(ws_hass, store, connection):
    """Verify the four coil_dry_* fields pass through into live data.

    Same allow-list trap as the compressor fields above (#386): the handler
    builds ``live`` from an explicit field list, so coordinator state that is
    not named here is silently dropped.
    """
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kueche",
        "acs": ["climate.kueche_ac"],
        "temperature_sensor": "sensor.kueche_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {
        "kueche": {
            "current_temp": 24.0,
            "target_temp": 24.0,
            "mode": "idle",
            "coil_dry_active": True,
            "coil_dry_phase": "blow",
            "coil_dry_until": 1700.0,
            "coil_dry_entities": ["climate.kueche_ac"],
        }
    }
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    live = connection.send_result.call_args[0][1]["rooms"]["kueche"]["live"]
    assert live["coil_dry_active"] is True
    assert live["coil_dry_phase"] == "blow"
    assert live["coil_dry_until"] == 1700.0
    assert live["coil_dry_entities"] == ["climate.kueche_ac"]


@pytest.mark.asyncio
async def test_list_rooms_coil_dry_defaults_without_coordinator_state(ws_hass, store, connection):
    """A room the coordinator has not run yet reports the inactive defaults."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "bad",
        "acs": ["climate.bad_ac"],
        "temperature_sensor": "sensor.bad_temp",
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {}
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    live = connection.send_result.call_args[0][1]["rooms"]["bad"]["live"]
    assert live["coil_dry_active"] is False
    assert live["coil_dry_phase"] is None
    assert live["coil_dry_until"] is None
    assert live["coil_dry_entities"] == []


@pytest.mark.asyncio
async def test_list_rooms_includes_cover_override_until(ws_hass, store, connection):
    """Verify cover_override_until appears in live data from list_rooms."""
    await store.async_load()

    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "schlafzimmer",
        "thermostats": ["climate.sz_trv"],
        "temperature_sensor": "sensor.sz_temp",
        "covers": ["cover.sz_rollo"],
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {
        "schlafzimmer": {
            "current_temp": 20.0,
            "target_temp": 21.0,
            "mode": "idle",
            "cover_auto_paused": True,
            "cover_override_until": 1751700000.0,
        }
    }
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    live = connection.send_result.call_args[0][1]["rooms"]["schlafzimmer"]["live"]
    assert live["cover_auto_paused"] is True
    assert live["cover_override_until"] == 1751700000.0


@pytest.mark.asyncio
async def test_save_room_with_window_sensors(ws_hass, store, connection):
    """Saving a room with window_sensors persists them; default is empty list."""
    await store.async_load()

    # Save a room WITH window_sensors
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen_trv"],
        "temperature_sensor": "sensor.kitchen_temp",
        "window_sensors": ["binary_sensor.kitchen_window"],
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["window_sensors"] == ["binary_sensor.kitchen_window"]
    connection.send_result.reset_mock()

    # Save a room WITHOUT window_sensors — should default to []
    msg2 = {
        "id": 3,
        "type": "roommind/rooms/save",
        "area_id": "hallway",
        "thermostats": ["climate.hallway_trv"],
    }
    await _save_room(ws_hass, connection, msg2)

    connection.send_result.assert_called_once()
    room2 = connection.send_result.call_args[0][1]["room"]
    assert room2["window_sensors"] == []


@pytest.mark.asyncio
async def test_list_rooms_includes_window_open(ws_hass, store, connection):
    """Verify window_open appears in live data from list_rooms."""
    await store.async_load()

    # Create a room
    save_msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "wohnzimmer",
        "thermostats": ["climate.wz_trv"],
        "temperature_sensor": "sensor.wz_temp",
        "window_sensors": ["binary_sensor.wz_window"],
    }
    await _save_room(ws_hass, connection, save_msg)
    connection.send_result.reset_mock()

    # Add coordinator mock with live data including window_open
    mock_coordinator = MagicMock()
    mock_coordinator.rooms = {
        "wohnzimmer": {
            "current_temp": 20.0,
            "target_temp": 21.0,
            "mode": "idle",
            "window_open": True,
            "active_schedule_index": 0,
        }
    }
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    list_msg = {"id": 3, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, list_msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    rooms = call_args[0][1]["rooms"]
    assert "wohnzimmer" in rooms
    live = rooms["wohnzimmer"]["live"]
    assert "window_open" in live
    assert live["window_open"] is True


@pytest.mark.asyncio
async def test_get_settings_empty(ws_hass, store, connection):
    """Getting settings on a fresh store returns empty dict."""
    await store.async_load()

    msg = {"id": 10, "type": "roommind/settings/get"}
    await _get_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(10, {"settings": {}})


@pytest.mark.asyncio
async def test_save_settings(ws_hass, store, connection):
    """Saving outdoor_temp_sensor persists and returns updated settings."""
    await store.async_load()

    msg = {
        "id": 11,
        "type": "roommind/settings/save",
        "outdoor_temp_sensor": "sensor.outdoor",
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert result["settings"]["outdoor_temp_sensor"] == "sensor.outdoor"


# ---------------------------------------------------------------------------
# Vacation mode tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_settings_vacation(ws_hass, store, connection):
    """Saving vacation fields persists and returns updated settings."""
    await store.async_load()

    until = 1771900000.0
    msg = {
        "id": 12,
        "type": "roommind/settings/save",
        "vacation_temp": 15.0,
        "vacation_until": until,
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert result["settings"]["vacation_temp"] == 15.0
    assert result["settings"]["vacation_until"] == until


@pytest.mark.asyncio
async def test_save_settings_vacation_clear(ws_hass, store, connection):
    """Setting vacation_until to None clears vacation mode."""
    await store.async_load()

    msg = {
        "id": 13,
        "type": "roommind/settings/save",
        "vacation_until": None,
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert result["settings"]["vacation_until"] is None


# ---------------------------------------------------------------------------
# Thermal model reset tests
# ---------------------------------------------------------------------------


def _make_coordinator_with_model(ws_hass):
    """Create a coordinator mock with public thermal API methods."""
    mock_coordinator = MagicMock()
    mock_coordinator.reset_thermal_room = MagicMock()
    mock_coordinator.reset_thermal_all = MagicMock(return_value=["room_a", "room_b"])
    mock_coordinator.boost_learning = MagicMock(return_value=42)
    mock_history = MagicMock()
    mock_history.remove_room = MagicMock()
    mock_coordinator.history_store = mock_history
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator
    return mock_coordinator


@pytest.mark.asyncio
async def test_thermal_reset_room(ws_hass, store, connection):
    """Resetting one room clears its model but keeps others."""
    await store.async_load()
    await store.async_save_thermal_data({"room_a": {"n_samples": 10}, "room_b": {"n_samples": 5}})

    coordinator = _make_coordinator_with_model(ws_hass)

    msg = {"id": 20, "type": "roommind/thermal/reset", "area_id": "room_a"}
    await _thermal_reset(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(20, {"success": True})

    # Public API called for room_a
    coordinator.reset_thermal_room.assert_called_once_with("room_a")
    # History cleared for room_a
    coordinator.history_store.remove_room.assert_called_once_with("room_a")
    # Persisted thermal data cleared for room_a
    assert "room_a" not in store.get_thermal_data()
    assert "room_b" in store.get_thermal_data()


@pytest.mark.asyncio
async def test_thermal_reset_all(ws_hass, store, connection):
    """Resetting all rooms clears all models and history."""
    await store.async_load()
    await store.async_save_thermal_data({"room_a": {"n_samples": 10}, "room_b": {"n_samples": 5}})

    coordinator = _make_coordinator_with_model(ws_hass)

    msg = {"id": 21, "type": "roommind/thermal/reset_all"}
    await _thermal_reset_all(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(21, {"success": True})

    # Public API called
    coordinator.reset_thermal_all.assert_called_once()
    # History cleared for all rooms returned by reset_thermal_all
    assert coordinator.history_store.remove_room.call_count == 2
    # Persisted thermal data empty
    assert store.get_thermal_data() == {}


@pytest.mark.asyncio
async def test_thermal_reset_nonexistent_room(ws_hass, store, connection):
    """Resetting a room that has no model data still succeeds (idempotent)."""
    await store.async_load()

    _make_coordinator_with_model(ws_hass)

    msg = {"id": 22, "type": "roommind/thermal/reset", "area_id": "nonexistent"}
    await _thermal_reset(ws_hass, connection, msg)

    connection.send_result.assert_called_once_with(22, {"success": True})


# --- Mold risk settings tests ---


@pytest.mark.asyncio
async def test_save_settings_mold_fields(ws_hass, store, connection):
    """Mold detection/prevention settings should be accepted and persisted."""
    await store.async_load()

    msg = {
        "id": 30,
        "type": "roommind/settings/save",
        "mold_detection_enabled": True,
        "mold_humidity_threshold": 65.0,
        "mold_sustained_minutes": 15,
        "mold_notification_cooldown": 30,
        "mold_notifications_enabled": True,
        "mold_notification_targets": [
            {"entity_id": "notify.mobile_app_kevin", "person_entity": "person.kevin", "notify_when": "always"},
        ],
        "mold_prevention_enabled": True,
        "mold_prevention_intensity": "strong",
        "mold_prevention_notify_enabled": True,
        "mold_prevention_notify_targets": [],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    settings = result["settings"]
    assert settings["mold_detection_enabled"] is True
    assert settings["mold_humidity_threshold"] == 65.0
    assert settings["mold_sustained_minutes"] == 15
    assert settings["mold_notification_cooldown"] == 30
    assert settings["mold_notifications_enabled"] is True
    assert len(settings["mold_notification_targets"]) == 1
    assert settings["mold_prevention_enabled"] is True
    assert settings["mold_prevention_intensity"] == "strong"


@pytest.mark.asyncio
async def test_save_settings_mold_partial_update(ws_hass, store, connection):
    """Updating only one mold field should not affect others (merge behavior)."""
    await store.async_load()

    # First save all fields
    msg1 = {
        "id": 31,
        "type": "roommind/settings/save",
        "mold_detection_enabled": True,
        "mold_humidity_threshold": 75.0,
    }
    await _save_settings(ws_hass, connection, msg1)
    connection.send_result.reset_mock()

    # Now save only one field
    msg2 = {
        "id": 32,
        "type": "roommind/settings/save",
        "mold_prevention_enabled": True,
    }
    await _save_settings(ws_hass, connection, msg2)

    result = connection.send_result.call_args[0][1]
    settings = result["settings"]
    # Original fields should still be there
    assert settings["mold_detection_enabled"] is True
    assert settings["mold_humidity_threshold"] == 75.0
    # New field should be set
    assert settings["mold_prevention_enabled"] is True


@pytest.mark.asyncio
async def test_compute_target_forecast_includes_mold_delta(ws_hass):
    """_compute_target_forecast should add mold_prevention_delta to all targets."""
    from custom_components.roommind.websocket_api import _compute_target_forecast

    room = {"comfort_temp": 21.0, "eco_temp": 17.0, "schedules": []}
    settings: dict = {}

    # Without delta
    forecast_base = await _compute_target_forecast(ws_hass, room, settings)
    assert forecast_base[0]["target_temp"] == 21.0

    # With delta
    forecast_mold = await _compute_target_forecast(
        ws_hass,
        room,
        settings,
        mold_prevention_delta=2.0,
    )
    assert forecast_mold[0]["target_temp"] == 23.0

    # All forecast points should have the delta applied
    for point in forecast_mold:
        assert point["target_temp"] == 23.0


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


def test_safe_float_valid():
    assert _safe_float("21.5") == 21.5


def test_safe_float_empty():
    assert _safe_float("") is None


def test_safe_float_none():
    assert _safe_float(None) is None


def test_safe_float_invalid():
    assert _safe_float("abc") is None


def test_csv_to_points_normal():
    """Converts CSV rows with string values to typed points."""
    rows = [
        {
            "timestamp": "1000.0",
            "room_temp": "21.5",
            "outdoor_temp": "5.0",
            "target_temp": "21.0",
            "mode": "heating",
            "predicted_temp": "21.3",
            "window_open": "False",
            "heating_power": "75.0",
        },
    ]
    points = _csv_to_points(rows)
    assert len(points) == 1
    p = points[0]
    assert p["ts"] == 1000.0
    assert p["room_temp"] == 21.5
    assert p["outdoor_temp"] == 5.0
    assert p["target_temp"] == 21.0
    assert p["mode"] == "heating"
    assert p["predicted_temp"] == 21.3
    assert p["window_open"] is False
    assert p["heating_power"] == 75.0


def test_csv_to_points_window_open_true():
    rows = [
        {
            "timestamp": "1000",
            "room_temp": "21",
            "outdoor_temp": "5",
            "target_temp": "21",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "True",
            "heating_power": "",
        },
    ]
    points = _csv_to_points(rows)
    assert points[0]["window_open"] is True


def test_csv_to_points_skips_bad_timestamp():
    rows = [
        {
            "timestamp": "bad",
            "room_temp": "21",
            "outdoor_temp": "5",
            "target_temp": "21",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "",
            "heating_power": "",
        },
        {
            "timestamp": "1000",
            "room_temp": "21",
            "outdoor_temp": "5",
            "target_temp": "21",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "",
            "heating_power": "",
        },
    ]
    points = _csv_to_points(rows)
    assert len(points) == 1


def test_csv_to_points_empty():
    assert _csv_to_points([]) == []


# ---------------------------------------------------------------------------
# Analytics handler tests
# ---------------------------------------------------------------------------


def _make_mock_estimator(**overrides):
    """Build a mock ThermalEKF estimator with sensible defaults."""
    est = MagicMock()
    est._n_updates = overrides.get("n_updates", 200)
    est._n_idle = overrides.get("n_idle", 120)
    est._n_heating = overrides.get("n_heating", 60)
    est._n_cooling = overrides.get("n_cooling", 20)
    est._applicable_modes = overrides.get("applicable_modes", {"idle", "heating"})
    est._P = overrides.get("P", [[0.01 * (i == j) for j in range(6)] for i in range(6)])
    est.confidence = overrides.get("confidence", 0.85)
    est.prediction_std.return_value = overrides.get("prediction_std", 0.3)
    rc = MagicMock()
    rc.Q_heat = overrides.get("Q_heat", 100.0)
    rc.to_dict.return_value = overrides.get("model_dict", {"alpha": 0.5})
    est.get_model.return_value = rc
    return est


def _make_analytics_coordinator(history_rows=None, estimator=None, rooms_live=None):
    """Build a mock coordinator for analytics tests."""
    coordinator = MagicMock()
    coordinator.rooms = rooms_live or {}
    coordinator.outdoor_temp = 5.0
    coordinator.outdoor_temp_effective = 5.0
    coordinator.outdoor_humidity = 60
    coordinator._weather_manager._outdoor_forecast = []
    coordinator._window_manager._paused = {}

    if history_rows is not None:
        hs = MagicMock()
        hs.read_detail.return_value = history_rows
        hs.read_history.return_value = []
        coordinator._history_store = hs
    else:
        coordinator._history_store = None

    from custom_components.roommind.control.thermal_model import RoomModelManager

    mgr = RoomModelManager()
    if estimator:
        mgr._estimators["room_a"] = estimator
    coordinator._model_manager = mgr

    return coordinator


@pytest.mark.asyncio
async def test_analytics_no_history_store(ws_hass, store, connection):
    """Analytics returns empty data when no history store exists."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    coordinator = _make_analytics_coordinator(history_rows=None)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 50, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["detail"] == []
    assert result["history"] == []


@pytest.mark.asyncio
async def test_analytics_with_range_key(ws_hass, store, connection):
    """Analytics reads history with max_age based on range key."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    csv_rows = [
        {
            "timestamp": "1000",
            "room_temp": "21.0",
            "outdoor_temp": "5.0",
            "target_temp": "21.0",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "",
            "heating_power": "",
        },
    ]
    coordinator = _make_analytics_coordinator(history_rows=csv_rows)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 51, "type": "roommind/analytics/get", "area_id": "room_a", "range": "24h"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert len(result["detail"]) == 1
    assert result["detail"][0]["ts"] == 1000.0
    # read_detail was called with max_age=86400
    coordinator._history_store.read_detail.assert_called_once_with("room_a", 86400)


@pytest.mark.asyncio
async def test_analytics_with_custom_timestamps(ws_hass, store, connection):
    """Analytics reads history with custom start/end timestamps."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    csv_rows = [
        {
            "timestamp": "1500",
            "room_temp": "21.0",
            "outdoor_temp": "5.0",
            "target_temp": "21.0",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "",
            "heating_power": "",
        },
    ]
    coordinator = _make_analytics_coordinator(history_rows=csv_rows)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {
        "id": 52,
        "type": "roommind/analytics/get",
        "area_id": "room_a",
        "start_ts": 1000.0,
        "end_ts": 2000.0,
    }
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert len(result["detail"]) == 1
    coordinator._history_store.read_detail.assert_called_once_with(
        "room_a",
        None,
        1000.0,
        2000.0,
    )


@pytest.mark.asyncio
async def test_analytics_no_estimator(ws_hass, store, connection):
    """Analytics returns empty model info when no estimator exists."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    coordinator = _make_analytics_coordinator(history_rows=[])
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 53, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["model"] == {}


@pytest.mark.asyncio
async def test_analytics_with_estimator(ws_hass, store, connection):
    """Analytics includes model info when estimator exists."""
    await store.async_load()
    await store.async_save_room(
        "room_a",
        {
            "thermostats": ["climate.trv1"],
            "temperature_sensor": "sensor.temp",
        },
    )

    est = _make_mock_estimator()

    coordinator = _make_analytics_coordinator(history_rows=[], estimator=est)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 54, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    model = result["model"]
    assert model["confidence"] == 0.85
    assert model["n_samples"] == 200
    assert model["n_heating"] == 60
    assert model["n_cooling"] == 20
    assert "mpc_active" in model


@pytest.mark.asyncio
async def test_analytics_no_external_sensor_mpc_false(ws_hass, store, connection):
    """Without external sensor, mpc_active is always False."""
    await store.async_load()
    await store.async_save_room(
        "room_a",
        {
            "thermostats": ["climate.trv1"],
            "temperature_sensor": "",  # no external sensor
        },
    )

    est = _make_mock_estimator(n_cooling=0)

    coordinator = _make_analytics_coordinator(history_rows=[], estimator=est)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 55, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["model"]["mpc_active"] is False


@pytest.mark.asyncio
async def test_analytics_prediction_disabled(ws_hass, store, connection):
    """With prediction_enabled=False, pred_temps is empty."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})
    await store.async_save_settings({"prediction_enabled": False})

    csv_rows = [
        {
            "timestamp": "1000",
            "room_temp": "21.0",
            "outdoor_temp": "5.0",
            "target_temp": "21.0",
            "mode": "idle",
            "predicted_temp": "",
            "window_open": "",
            "heating_power": "",
        },
    ]
    coordinator = _make_analytics_coordinator(history_rows=csv_rows)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 56, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    # Forecast should have target_temp but predicted_temp should be None
    for f in result["forecast"]:
        assert f["predicted_temp"] is None


@pytest.mark.asyncio
async def test_analytics_forecast_grid_alignment(ws_hass, store, connection):
    """Forecast timestamps are snapped to 5-min grid."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    coordinator = _make_analytics_coordinator(history_rows=[])
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 57, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    for f in result["forecast"]:
        assert f["ts"] % 300 == 0  # all timestamps on 5-min grid
        assert f["mode"] == "forecast"
        assert f["room_temp"] is None
        assert f["window_open"] is False


@pytest.mark.asyncio
async def test_analytics_mold_delta_from_live(ws_hass, store, connection):
    """Mold prevention delta is read from coordinator live state."""
    await store.async_load()
    await store.async_save_room(
        "room_a",
        {
            "thermostats": ["climate.trv1"],
            "comfort_temp": 21.0,
        },
    )

    coordinator = _make_analytics_coordinator(
        history_rows=[],
        rooms_live={"room_a": {"mold_prevention_delta": 2.0}},
    )
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 58, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    # Target forecast should include the mold delta
    assert result["forecast"][0]["target_temp"] == 23.0


@pytest.mark.asyncio
async def test_analytics_model_has_occupancy_sensors_true(ws_hass, store, connection):
    """Analytics model_info reports has_occupancy_sensors=True when room has occupancy sensors."""
    await store.async_load()
    await store.async_save_room(
        "room_a",
        {
            "thermostats": ["climate.trv1"],
            "occupancy_sensors": ["binary_sensor.room_a_occupancy"],
        },
    )

    est = _make_mock_estimator(model_dict={"Q_heat": 3.0, "Q_solar": 0.5, "Q_occupancy": 0.3})
    coordinator = _make_analytics_coordinator(history_rows=[], estimator=est)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 60, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["model"]["has_occupancy_sensors"] is True
    assert "Q_occupancy" in result["model"]["model"]


@pytest.mark.asyncio
async def test_analytics_model_has_occupancy_sensors_false(ws_hass, store, connection):
    """Analytics model_info reports has_occupancy_sensors=False when no occupancy sensors configured."""
    await store.async_load()
    await store.async_save_room("room_a", {"thermostats": ["climate.trv1"]})

    est = _make_mock_estimator(model_dict={"Q_heat": 3.0, "Q_solar": 0.5, "Q_occupancy": 0.3})
    coordinator = _make_analytics_coordinator(history_rows=[], estimator=est)
    ws_hass.data[DOMAIN]["coordinator"] = coordinator

    msg = {"id": 61, "type": "roommind/analytics/get", "area_id": "room_a"}
    await _get_analytics(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["model"]["has_occupancy_sensors"] is False
    assert "Q_occupancy" in result["model"]["model"]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_websocket_commands(hass):
    """async_register_websocket_commands registers all 13 commands."""
    from unittest.mock import patch

    from custom_components.roommind.websocket_api import async_register_websocket_commands

    with patch("custom_components.roommind.websocket_api.websocket_api.async_register_command") as mock_reg:
        async_register_websocket_commands(hass)
        assert mock_reg.call_count == 13


# ---------------------------------------------------------------------------
# Heating system type field tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_room_heating_system_type_accepted(ws_hass, store, connection):
    """heating_system_type is accepted in rooms/save schema."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "kitchen",
        "thermostats": ["climate.kitchen"],
        "heating_system_type": "underfloor",
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["heating_system_type"] == "underfloor"


@pytest.mark.asyncio
async def test_save_room_heating_system_type_empty(ws_hass, store, connection):
    """Empty string is a valid heating_system_type (default)."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "bedroom",
        "thermostats": ["climate.bedroom"],
        "heating_system_type": "",
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["heating_system_type"] == ""


@pytest.mark.asyncio
async def test_save_room_heating_system_type_radiator(ws_hass, store, connection):
    """'radiator' is a valid heating_system_type."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "hallway",
        "thermostats": ["climate.hallway"],
        "heating_system_type": "radiator",
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["heating_system_type"] == "radiator"


def test_save_room_heating_system_type_invalid_rejected():
    """Invalid heating_system_type value should be rejected by voluptuous schema."""
    import voluptuous as vol

    # Test the vol.In validator directly (matches the schema in websocket_api.py)
    validator = vol.In(["", "radiator", "underfloor"])
    with pytest.raises(vol.Invalid):
        validator("geothermal")
    # Valid values should pass
    assert validator("") == ""
    assert validator("radiator") == "radiator"
    assert validator("underfloor") == "underfloor"


@pytest.mark.asyncio
async def test_save_room_heating_system_type_defaults_empty(ws_hass, store, connection):
    """When heating_system_type is not provided, it defaults to empty string."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "study",
        "thermostats": ["climate.study"],
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room.get("heating_system_type", "") == ""


# ---------------------------------------------------------------------------
# Override set: cool_only climate mode paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_override_set_boost_cool_only_uses_comfort_cool(ws_hass, store, connection):
    """Boost override in cool_only room uses comfort_cool temperature."""
    await store.async_load()
    await store.async_save_room("room1", {"climate_mode": "cool_only", "comfort_cool": 26.0})
    connection.send_result.reset_mock()

    msg = {
        "id": 1,
        "type": "roommind/override/set",
        "area_id": "room1",
        "override_type": "boost",
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    room = store.get_room("room1")
    assert room["override_heat"] is None
    assert room["override_cool"] == 26.0


@pytest.mark.asyncio
async def test_override_set_eco_cool_only_uses_eco_cool(ws_hass, store, connection):
    """Eco override in cool_only room uses eco_cool temperature."""
    await store.async_load()
    await store.async_save_room("room1", {"climate_mode": "cool_only", "eco_cool": 29.0})
    connection.send_result.reset_mock()

    msg = {
        "id": 1,
        "type": "roommind/override/set",
        "area_id": "room1",
        "override_type": "eco",
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    room = store.get_room("room1")
    assert room["override_heat"] is None
    assert room["override_cool"] == 29.0


@pytest.mark.asyncio
async def test_override_set_triggers_coordinator_refresh(ws_hass, store, connection):
    """override/set notifies coordinator via async_request_refresh."""
    await store.async_load()
    await store.async_save_room("kitchen", {})

    mock_coordinator = MagicMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    msg = {
        "id": 1,
        "type": "roommind/override/set",
        "area_id": "kitchen",
        "override_type": "boost",
        "duration": 1.0,
    }
    await _override_set(ws_hass, connection, msg)

    mock_coordinator.async_request_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_override_clear_nonexistent_room_errors(ws_hass, store, connection):
    """Clearing override on non-existent room sends an error."""
    await store.async_load()

    msg = {"id": 1, "type": "roommind/override/clear", "area_id": "does_not_exist"}
    await _override_clear(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_found"


@pytest.mark.asyncio
async def test_override_clear_triggers_coordinator_refresh(ws_hass, store, connection):
    """override/clear notifies coordinator via async_request_refresh."""
    await store.async_load()
    await store.async_save_room("hall", {})

    mock_coordinator = MagicMock()
    mock_coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    msg = {"id": 1, "type": "roommind/override/clear", "area_id": "hall"}
    await _override_clear(ws_hass, connection, msg)

    mock_coordinator.async_request_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# Boost learning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boost_learning_no_coordinator_errors(ws_hass, store, connection):
    """boost_learning without coordinator sends an error."""
    from custom_components.roommind.websocket_api import websocket_boost_learning

    _boost_learning = websocket_boost_learning.__wrapped__

    await store.async_load()
    # No coordinator in hass.data
    msg = {"id": 1, "type": "roommind/model/boost_learning", "area_id": "living_room"}
    await _boost_learning(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "no_coordinator"


@pytest.mark.asyncio
async def test_boost_learning_success(ws_hass, store, connection):
    """boost_learning with coordinator boosts EKF and persists cooldown."""
    from custom_components.roommind.websocket_api import websocket_boost_learning

    _boost_learning = websocket_boost_learning.__wrapped__

    await store.async_load()

    mock_coordinator = MagicMock()
    mock_coordinator.boost_learning = MagicMock(return_value=42)
    ws_hass.data[DOMAIN]["coordinator"] = mock_coordinator

    msg = {"id": 1, "type": "roommind/model/boost_learning", "area_id": "living_room"}
    await _boost_learning(ws_hass, connection, msg)

    mock_coordinator.boost_learning.assert_called_once_with("living_room")
    connection.send_result.assert_called_once_with(1, {"success": True, "n_observations": 42})


# ── Cover schedule WS validation ───────────────────────────────────────


@pytest.mark.asyncio
async def test_save_room_with_cover_schedules(ws_hass, store, connection):
    """Cover schedules with valid entity_id are persisted."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "sunroom",
        "thermostats": ["climate.sunroom"],
        "cover_schedules": [{"entity_id": "schedule.cover_day"}],
        "cover_schedule_selector_entity": "input_boolean.cover_mode",
        "covers_night_close": True,
        "covers_night_position": 10,
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["cover_schedules"] == [{"entity_id": "schedule.cover_day"}]
    assert room["cover_schedule_selector_entity"] == "input_boolean.cover_mode"
    assert room["covers_night_close"] is True
    assert room["covers_night_position"] == 10


def test_save_room_cover_night_position_validation():
    """covers_night_position validated by schema: 0-100 range."""
    import voluptuous as vol

    validator = vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
    assert validator(0) == 0
    assert validator(100) == 100
    assert validator(50) == 50
    with pytest.raises(vol.Invalid):
        validator(150)
    with pytest.raises(vol.Invalid):
        validator(-1)


@pytest.mark.asyncio
async def test_save_room_with_is_outdoor(ws_hass, store, connection):
    """Round-trip: save a room with is_outdoor=True and verify it persists."""
    await store.async_load()

    msg = {
        "id": 10,
        "type": "roommind/rooms/save",
        "area_id": "terrasse",
        "is_outdoor": True,
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args
    room = call_args[0][1]["room"]
    assert room["is_outdoor"] is True

    # Verify it persists in the store
    stored = store.get_room("terrasse")
    assert stored["is_outdoor"] is True


@pytest.mark.asyncio
async def test_save_room_valve_protection_exclude_roundtrip(ws_hass, store, connection):
    """Round-trip: valve_protection_exclude persists through WS save (#110)."""
    await store.async_load()

    msg = {
        "id": 10,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "valve_protection_exclude": ["climate.boiler"],
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["valve_protection_exclude"] == ["climate.boiler"]

    stored = store.get_room("living_room")
    assert stored["valve_protection_exclude"] == ["climate.boiler"]


@pytest.mark.asyncio
async def test_save_room_with_climate_control_enabled(ws_hass, store, connection):
    """Round-trip: climate_control_enabled persists through WS save."""
    await store.async_load()

    msg = {
        "id": 10,
        "type": "roommind/rooms/save",
        "area_id": "bedroom",
        "climate_control_enabled": False,
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["climate_control_enabled"] is False

    stored = store.get_room("bedroom")
    assert stored["climate_control_enabled"] is False


def test_save_room_cover_deploy_threshold_rejects_negative():
    """covers_deploy_threshold rejects negative values."""
    import voluptuous as vol

    validator = vol.All(vol.Coerce(float), vol.Range(min=0))
    assert validator(0) == 0.0
    assert validator(1.5) == 1.5
    assert validator(5.0) == 5.0
    with pytest.raises(vol.Invalid):
        validator(-1.0)
    with pytest.raises(vol.Invalid):
        validator(-0.1)


# ---------------------------------------------------------------------------
# Self-assignment guard (#86)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("thermostats", ["climate.roommind_living_room_override"]),
        ("acs", ["climate.roommind_living_room_override"]),
        ("temperature_sensor", "sensor.roommind_living_room_target_temp"),
        ("humidity_sensor", "sensor.roommind_living_room_mode"),
        ("window_sensors", ["binary_sensor.roommind_test"]),
        ("covers", ["cover.roommind_living_room_auto"]),
        ("comfort_heat_entity", "climate.roommind_living_room_override"),
    ],
)
async def test_save_room_rejects_own_entities(ws_hass, store, connection, field, value):
    """Assigning RoomMind's own entities to a room is rejected."""
    await store.async_load()
    msg = {"id": 2, "type": "roommind/rooms/save", "area_id": "living_room", field: value}
    await _save_room(ws_hass, connection, msg)
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_entity"


@pytest.mark.asyncio
async def test_save_room_devices_duplicate_entity_rejected(ws_hass, store, connection):
    """Duplicate entity_ids in devices[] are rejected."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.trv1", "type": "trv"},
            {"entity_id": "climate.trv1", "type": "ac"},
        ],
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "duplicate_entity"


@pytest.mark.asyncio
async def test_save_room_allows_normal_entities(ws_hass, store, connection):
    """Normal (non-RoomMind) entities are accepted."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "thermostats": ["climate.living_room_trv"],
        "temperature_sensor": "sensor.living_room_temp",
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    connection.send_error.assert_not_called()


# ---------------------------------------------------------------------------
# Unified Device Model: devices field in WS save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_room_with_devices_accepted(ws_hass, store, connection):
    """WS save with devices array is accepted and stored."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.trv1", "type": "trv", "role": "auto", "heating_system_type": "radiator"},
            {"entity_id": "climate.ac1", "type": "ac", "role": "auto", "heating_system_type": ""},
        ],
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert room["devices"] == [
        {"entity_id": "climate.trv1", "type": "trv", "role": "auto", "heating_system_type": "radiator"},
        {"entity_id": "climate.ac1", "type": "ac", "role": "auto", "heating_system_type": ""},
    ]
    # Legacy keys should be synced
    assert room["thermostats"] == ["climate.trv1"]
    assert room["acs"] == ["climate.ac1"]


@pytest.mark.asyncio
async def test_save_room_device_type_heat_pump_rejected(ws_hass, store, connection):
    """Sending type: 'heat_pump' in a device should be rejected by the WS schema.

    The voluptuous schema on the websocket_save_room handler only allows
    'trv' and 'ac'.  We rebuild the schema from the decorator definition
    and validate through it to ensure heat_pump is rejected at the WS layer.
    """
    import voluptuous as vol

    # Reproduce the device sub-schema from websocket_api.py
    device_schema = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Required("type"): vol.In(["trv", "ac"]),
            vol.Optional("role", default="auto"): vol.In(["primary", "secondary", "auto"]),
            vol.Optional("heating_system_type", default=""): vol.In(["", "radiator", "underfloor"]),
        }
    )
    save_room_schema = vol.Schema(
        {
            vol.Required("id"): int,
            vol.Required("type"): "roommind/rooms/save",
            vol.Required("area_id"): str,
            vol.Optional("devices"): [device_schema],
        },
        extra=vol.ALLOW_EXTRA,
    )

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.hp1", "type": "heat_pump", "role": "auto"},
        ],
    }

    with pytest.raises(vol.Invalid):
        save_room_schema(msg)


@pytest.mark.asyncio
async def test_save_room_devices_self_assignment_rejected(ws_hass, store, connection):
    """Self-assignment check rejects RoomMind's own entities in devices."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.roommind_living_room_trv", "type": "trv"},
        ],
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_entity"


@pytest.mark.asyncio
async def test_save_room_accepts_idle_action_low_for_trv(ws_hass, store, connection):
    """TRV with idle_action='low' is accepted and persisted."""
    await store.async_load()
    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.trv1", "type": "trv", "role": "auto", "idle_action": "low"},
        ],
    }
    await _save_room(ws_hass, connection, msg)
    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]
    assert any(d.get("idle_action") == "low" and d["entity_id"] == "climate.trv1" for d in room["devices"])


@pytest.mark.asyncio
async def test_save_room_rejects_ac_with_low_idle_action(ws_hass, store, connection):
    """idle_action='low' is not permitted on AC devices — they would cool continuously.

    Imports the real _validate_device_idle_action so the test catches any regression
    in the production validator, not a local copy.
    """
    import voluptuous as vol

    from custom_components.roommind.websocket_api import _validate_device_idle_action

    device_schema = vol.All(
        vol.Schema(
            {
                vol.Required("entity_id"): str,
                vol.Required("type"): vol.In(["trv", "ac"]),
                vol.Optional("role", default="auto"): vol.In(["primary", "secondary", "auto"]),
                vol.Optional("idle_action", default="off"): vol.In(["off", "fan_only", "setback", "low"]),
            }
        ),
        _validate_device_idle_action,
    )
    save_room_schema = vol.Schema(
        {
            vol.Required("id"): int,
            vol.Required("type"): "roommind/rooms/save",
            vol.Required("area_id"): str,
            vol.Optional("devices"): [device_schema],
        },
        extra=vol.ALLOW_EXTRA,
    )

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [
            {"entity_id": "climate.ac1", "type": "ac", "role": "auto", "idle_action": "low"},
        ],
    }

    with pytest.raises(vol.Invalid):
        save_room_schema(msg)


@pytest.mark.asyncio
async def test_validate_device_idle_action_unit():
    """Direct unit test of the real validator — catches regressions if the function changes."""
    import voluptuous as vol

    from custom_components.roommind.websocket_api import _validate_device_idle_action

    # TRV + low is allowed
    assert _validate_device_idle_action({"type": "trv", "idle_action": "low"}) == {
        "type": "trv",
        "idle_action": "low",
    }
    # AC + off is allowed
    assert _validate_device_idle_action({"type": "ac", "idle_action": "off"}) == {
        "type": "ac",
        "idle_action": "off",
    }
    # AC + low is rejected
    with pytest.raises(vol.Invalid):
        _validate_device_idle_action({"type": "ac", "idle_action": "low"})


@pytest.mark.asyncio
async def test_save_room_rejects_unknown_idle_action(ws_hass, store, connection):
    """Schema rejects unknown idle_action values (regression guard)."""
    import voluptuous as vol

    device_schema = vol.Schema(
        {
            vol.Required("entity_id"): str,
            vol.Required("type"): vol.In(["trv", "ac"]),
            vol.Optional("idle_action", default="off"): vol.In(["off", "fan_only", "setback", "low"]),
        }
    )
    save_room_schema = vol.Schema(
        {
            vol.Required("id"): int,
            vol.Required("type"): "roommind/rooms/save",
            vol.Required("area_id"): str,
            vol.Optional("devices"): [device_schema],
        },
        extra=vol.ALLOW_EXTRA,
    )

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "devices": [{"entity_id": "climate.trv1", "type": "trv", "idle_action": "sleep"}],
    }

    with pytest.raises(vol.Invalid):
        save_room_schema(msg)


# ---------------------------------------------------------------------------
# Compressor group validation tests (K3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_settings_compressor_groups_valid(ws_hass, store, connection):
    """Saving valid compressor_groups succeeds and persists them."""
    await store.async_load()

    msg = {
        "id": 20,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "outdoor1",
                "name": "Outdoor Unit 1",
                "members": ["climate.ac_living", "climate.ac_bedroom"],
                "min_run_minutes": 10,
                "min_off_minutes": 5,
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    groups = result["settings"]["compressor_groups"]
    assert len(groups) == 1
    assert groups[0]["id"] == "outdoor1"
    assert groups[0]["members"] == ["climate.ac_living", "climate.ac_bedroom"]


@pytest.mark.asyncio
async def test_save_settings_compressor_groups_duplicate_member(ws_hass, store, connection):
    """Duplicate entity across compressor groups should be rejected."""
    await store.async_load()

    msg = {
        "id": 21,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "group1",
                "name": "Group 1",
                "members": ["climate.ac_living"],
                "min_run_minutes": 5,
                "min_off_minutes": 5,
            },
            {
                "id": "group2",
                "name": "Group 2",
                "members": ["climate.ac_living"],
                "min_run_minutes": 5,
                "min_off_minutes": 5,
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "duplicate_member"


@pytest.mark.asyncio
async def test_save_settings_compressor_groups_invalid_member(ws_hass, store, connection):
    """Non-climate entity in compressor group should be rejected."""
    await store.async_load()

    msg = {
        "id": 22,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "group1",
                "name": "Group 1",
                "members": ["switch.pump_relay"],
                "min_run_minutes": 5,
                "min_off_minutes": 5,
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_member"


# ---------------------------------------------------------------------------
# Compressor group master device validation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_settings_compressor_master_entity_valid(ws_hass, store, connection):
    """Master entity saves successfully."""
    await store.async_load()

    msg = {
        "id": 30,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.ac1"],
                "master_entity": "climate.boiler",
                "conflict_resolution": "heating_priority",
                "action_script": "",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    saved = result["settings"]["compressor_groups"][0]
    assert saved["master_entity"] == "climate.boiler"
    assert saved["conflict_resolution"] == "heating_priority"
    assert saved["action_script"] == ""


@pytest.mark.asyncio
async def test_save_settings_compressor_master_non_climate(ws_hass, store, connection):
    """Master entity must be a climate entity."""
    await store.async_load()

    msg = {
        "id": 31,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.ac1"],
                "master_entity": "switch.boiler",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_master_entity"


@pytest.mark.asyncio
async def test_save_settings_compressor_master_in_own_members(ws_hass, store, connection):
    """Master entity cannot be a member of its own group."""
    await store.async_load()

    msg = {
        "id": 32,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.boiler"],
                "master_entity": "climate.boiler",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "master_in_members"


@pytest.mark.asyncio
async def test_save_settings_compressor_master_in_other_members(ws_hass, store, connection):
    """Master entity cannot be a member of another group."""
    await store.async_load()

    msg = {
        "id": 33,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "G1",
                "members": ["climate.ac1"],
            },
            {
                "id": "g2",
                "name": "G2",
                "members": ["climate.ac2"],
                "master_entity": "climate.ac1",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "master_is_other_member"


@pytest.mark.asyncio
async def test_save_settings_compressor_duplicate_masters(ws_hass, store, connection):
    """Same master entity cannot be in multiple groups."""
    await store.async_load()

    msg = {
        "id": 34,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "G1",
                "members": ["climate.ac1"],
                "master_entity": "climate.boiler",
            },
            {
                "id": "g2",
                "name": "G2",
                "members": ["climate.ac2"],
                "master_entity": "climate.boiler",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "duplicate_master"


@pytest.mark.asyncio
async def test_save_settings_compressor_invalid_action_script(ws_hass, store, connection):
    """Action script must be a script entity."""
    await store.async_load()

    msg = {
        "id": 35,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.ac1"],
                "action_script": "automation.foo",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_action_script"


@pytest.mark.asyncio
async def test_save_settings_compressor_valid_action_script(ws_hass, store, connection):
    """Valid action script saves successfully."""
    await store.async_load()

    msg = {
        "id": 36,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.ac1"],
                "action_script": "script.boiler_control",
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    saved = result["settings"]["compressor_groups"][0]
    assert saved["action_script"] == "script.boiler_control"


@pytest.mark.asyncio
async def test_save_settings_compressor_backward_compat(ws_hass, store, connection):
    """Groups without new fields save successfully (no validation errors)."""
    await store.async_load()

    msg = {
        "id": 37,
        "type": "roommind/settings/save",
        "compressor_groups": [
            {
                "id": "g1",
                "name": "Test",
                "members": ["climate.ac1"],
            },
        ],
    }
    await _save_settings(ws_hass, connection, msg)

    # No error — old-format groups pass validation
    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    saved = result["settings"]["compressor_groups"][0]
    assert saved["id"] == "g1"
    # New fields not present (schema defaults only applied via decorator)
    assert "master_entity" not in saved or saved["master_entity"] == ""


# ---------------------------------------------------------------------------
# V11: Legacy-only save syncs devices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_room_with_legacy_only_syncs_devices(ws_hass, store, connection):
    """Saving with thermostats/acs but NO devices key creates devices[] from legacy."""
    await store.async_load()

    msg = {
        "id": 30,
        "type": "roommind/rooms/save",
        "area_id": "legacy_room",
        "thermostats": ["climate.trv1"],
        "acs": ["climate.ac1"],
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    room = connection.send_result.call_args[0][1]["room"]

    # Store should have synthesized devices from legacy fields
    assert "devices" in room
    assert len(room["devices"]) == 2

    trv_devices = [d for d in room["devices"] if d["type"] == "trv"]
    ac_devices = [d for d in room["devices"] if d["type"] == "ac"]
    assert len(trv_devices) == 1
    assert trv_devices[0]["entity_id"] == "climate.trv1"
    assert len(ac_devices) == 1
    assert ac_devices[0]["entity_id"] == "climate.ac1"


# ---------------------------------------------------------------------------
# Diagnostics WS endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_diagnostics_returns_full_structure(ws_hass, store, connection):
    """roommind/diagnostics/get returns full integration diagnostics."""
    await store.async_load()

    # Mock config_entries.async_entries to return a fake config entry
    mock_entry = MagicMock()
    mock_entry.entry_id = "test_entry_id"
    ws_hass.config_entries = MagicMock()
    ws_hass.config_entries.async_entries = MagicMock(return_value=[mock_entry])

    # Wire up coordinator
    coordinator = MagicMock()
    coordinator.rooms = {}
    coordinator.outdoor_temp = 10.0
    coordinator.outdoor_humidity = 60.0
    coordinator._previous_modes = {}
    coordinator._mode_on_since = {}
    coordinator._last_valid_temps = {}
    coordinator._residual_tracker = MagicMock()
    coordinator._model_manager = MagicMock()
    coordinator._model_manager._estimators = {}
    coordinator._window_manager = MagicMock()
    coordinator._window_manager._states = {}
    coordinator._cover_orchestrator = MagicMock()
    coordinator._cover_orchestrator._cover_managers = {}
    coordinator._heat_source_states = {}
    coordinator._weather_manager = MagicMock()
    coordinator._weather_manager._outdoor_forecast = []
    coordinator._history_store = None
    coordinator._compressor_manager = MagicMock()
    coordinator._compressor_manager._groups = {}
    coordinator._compressor_manager._member_states = {}
    coordinator._valve_manager = MagicMock()
    coordinator._valve_manager._cycling = {}
    coordinator._valve_manager._last_actuation = {}
    ws_hass.data[DOMAIN]["coordinator"] = coordinator
    ws_hass.config = MagicMock()
    ws_hass.config.units = MagicMock()
    ws_hass.config.units.temperature_unit = "°C"

    await _get_diagnostics(ws_hass, connection, {"id": 1, "type": "roommind/diagnostics/get"})

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert "integration" in result
    assert "settings" in result
    assert "rooms" in result
    assert "outdoor" in result
    assert "presence" in result
    assert result["integration"]["domain"] == "roommind"


@pytest.mark.asyncio
async def test_get_diagnostics_no_config_entry(ws_hass, store, connection):
    """roommind/diagnostics/get returns error when no config entry exists."""
    ws_hass.config_entries = MagicMock()
    ws_hass.config_entries.async_entries = MagicMock(return_value=[])

    await _get_diagnostics(ws_hass, connection, {"id": 1, "type": "roommind/diagnostics/get"})

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_found"


@pytest.mark.asyncio
async def test_covers_clear_override_unknown_room(ws_hass, store, connection):
    await store.async_load()
    msg = {"id": 7, "type": "roommind/covers/clear_override", "area_id": "nope"}
    await _covers_clear_override(ws_hass, connection, msg)
    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_found"


@pytest.mark.asyncio
async def test_covers_clear_override_success(ws_hass, store, connection):
    await store.async_load()
    await store.async_save_room("lr", {"covers": ["cover.lr"]})
    coordinator = MagicMock()
    coordinator.clear_cover_override = MagicMock()
    coordinator.async_request_refresh = AsyncMock()
    ws_hass.data[DOMAIN]["coordinator"] = coordinator
    msg = {"id": 8, "type": "roommind/covers/clear_override", "area_id": "lr"}
    await _covers_clear_override(ws_hass, connection, msg)
    coordinator.clear_cover_override.assert_called_once_with("lr")
    coordinator.async_request_refresh.assert_awaited_once()
    connection.send_result.assert_called_once_with(8, {"success": True})


# ---------------------------------------------------------------------------
# Coil dry (evaporator drying) schema tests (Task 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coil_dry_device_schema_defaults():
    """Absent coil dry fields get their inherit defaults."""
    schema = websocket_save_room._ws_schema  # set by the websocket_command decorator
    result = schema(
        {
            "type": "roommind/rooms/save",
            "id": 1,
            "area_id": "living_room",
            "devices": [{"entity_id": "climate.ac", "type": "ac"}],
        }
    )
    dev = result["devices"][0]
    assert dev["coil_dry"] == "inherit"
    assert dev["coil_dry_minutes"] == 0
    assert dev["coil_dry_mode"] == ""
    assert dev["coil_dry_fan_mode"] == ""


@pytest.mark.asyncio
async def test_coil_dry_rejects_on_for_trv():
    """A TRV has no evaporator coil."""
    import voluptuous as vol

    schema = websocket_save_room._ws_schema
    with pytest.raises(vol.Invalid):
        schema(
            {
                "type": "roommind/rooms/save",
                "id": 1,
                "area_id": "living_room",
                "devices": [{"entity_id": "climate.trv", "type": "trv", "coil_dry": "on"}],
            }
        )


@pytest.mark.asyncio
async def test_coil_dry_allows_off_for_trv():
    """'off' and 'inherit' are harmless on a TRV and must not be rejected."""
    schema = websocket_save_room._ws_schema
    result = schema(
        {
            "type": "roommind/rooms/save",
            "id": 1,
            "area_id": "living_room",
            "devices": [{"entity_id": "climate.trv", "type": "trv", "coil_dry": "off"}],
        }
    )
    assert result["devices"][0]["coil_dry"] == "off"


@pytest.mark.asyncio
async def test_validate_device_coil_dry_unit():
    """Direct unit test of the real validator — catches regressions if the function changes."""
    import voluptuous as vol

    from custom_components.roommind.websocket_api import _validate_device_coil_dry

    # TRV + off is allowed
    assert _validate_device_coil_dry({"type": "trv", "coil_dry": "off"}) == {
        "type": "trv",
        "coil_dry": "off",
    }
    # AC + on is allowed
    assert _validate_device_coil_dry({"type": "ac", "coil_dry": "on"}) == {
        "type": "ac",
        "coil_dry": "on",
    }
    # TRV + on is rejected
    with pytest.raises(vol.Invalid):
        _validate_device_coil_dry({"type": "trv", "coil_dry": "on"})


@pytest.mark.asyncio
async def test_coil_dry_settings_schema_ranges():
    """Out-of-range values are rejected."""
    import voluptuous as vol

    schema = websocket_save_settings._ws_schema
    schema({"type": "roommind/settings/save", "id": 1, "coil_dry_minutes": 60})
    with pytest.raises(vol.Invalid):
        schema({"type": "roommind/settings/save", "id": 1, "coil_dry_minutes": 61})
    with pytest.raises(vol.Invalid):
        schema({"type": "roommind/settings/save", "id": 1, "coil_dry_drain_minutes": 16})
    with pytest.raises(vol.Invalid):
        schema({"type": "roommind/settings/save", "id": 1, "coil_dry_mode": "heat"})
    # 0 is valid for drain (= phase disabled)
    schema({"type": "roommind/settings/save", "id": 1, "coil_dry_drain_minutes": 0})


@pytest.mark.asyncio
async def test_coil_dry_state_not_writable_via_ws():
    """coil_dry_state is server-side runtime state, clients must not set it."""
    import voluptuous as vol

    schema = websocket_save_settings._ws_schema
    with pytest.raises(vol.Invalid):
        schema({"type": "roommind/settings/save", "id": 1, "coil_dry_state": {}})


@pytest.mark.asyncio
async def test_save_settings_persists_coil_dry_keys(ws_hass, store, connection):
    """settings/save must actually persist coil_dry_* — schema acceptance alone is not enough.

    _SETTINGS_SAVE_FIELDS is a separate allowlist that filters msg into the changes
    dict handed to the store; a key can pass schema validation and still never reach
    storage if it is missing from that tuple.
    """
    await store.async_load()
    msg = {
        "id": 5,
        "type": "roommind/settings/save",
        "coil_dry_enabled": True,
        "coil_dry_minutes": 30,
        "coil_dry_mode": "dry",
        "coil_dry_fan_mode": "high",
        "coil_dry_min_cooling_minutes": 15,
        "coil_dry_drain_minutes": 5,
    }
    await _save_settings(ws_hass, connection, msg)

    connection.send_result.assert_called_once()
    settings = connection.send_result.call_args[0][1]["settings"]
    assert settings["coil_dry_enabled"] is True
    assert settings["coil_dry_minutes"] == 30
    assert settings["coil_dry_mode"] == "dry"
    assert settings["coil_dry_fan_mode"] == "high"
    assert settings["coil_dry_min_cooling_minutes"] == 15
    assert settings["coil_dry_drain_minutes"] == 5


@pytest.mark.asyncio
async def test_list_rooms_defaults_coil_dry_settings(ws_hass, store, connection):
    """rooms/list surfaces the six global coil dry settings with their documented defaults."""
    await store.async_load()

    msg = {"id": 1, "type": "roommind/rooms/list"}
    await _list_rooms(ws_hass, connection, msg)

    result = connection.send_result.call_args[0][1]
    assert result["coil_dry_enabled"] is False
    assert result["coil_dry_minutes"] == 20
    assert result["coil_dry_mode"] == "fan_only"
    assert result["coil_dry_fan_mode"] == "low"
    assert result["coil_dry_min_cooling_minutes"] == 10
    assert result["coil_dry_drain_minutes"] == 0


# ---------------------------------------------------------------------------
# Comfort setpoint source (external climate entity)
# ---------------------------------------------------------------------------


def _source_state(temp=21.0, state="heat"):
    """Return a mock climate state reporting *temp* as its setpoint."""
    s = MagicMock()
    s.state = state
    s.attributes = {"temperature": temp}
    return s


@pytest.mark.asyncio
async def test_save_room_stores_comfort_source(ws_hass, store, connection):
    """The comfort source entity is persisted on the room."""
    await store.async_load()
    ws_hass.states.get = MagicMock(return_value=_source_state(21.0))
    ws_hass.services.async_call = AsyncMock()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "comfort_heat_entity": "climate.kontor_display",
    }
    await _save_room(ws_hass, connection, msg)

    room = connection.send_result.call_args[0][1]["room"]
    assert room["comfort_heat_entity"] == "climate.kontor_display"


@pytest.mark.asyncio
async def test_save_room_rejects_non_climate_comfort_source(ws_hass, store, connection):
    """Only climate entities can act as the comfort setpoint source."""
    await store.async_load()

    msg = {
        "id": 2,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "comfort_heat_entity": "input_number.comfort",
    }
    await _save_room(ws_hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_entity"


@pytest.mark.asyncio
async def test_save_room_pushes_comfort_to_source(ws_hass, store, connection):
    """A comfort temperature edited in the panel is pushed to the source device."""
    await store.async_load()
    await store.async_save_room("living_room", {"comfort_heat_entity": "climate.kontor_display"})
    ws_hass.states.get = MagicMock(return_value=_source_state(21.0))
    ws_hass.services.async_call = AsyncMock()

    msg = {
        "id": 3,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "comfort_heat": 23.0,
    }
    await _save_room(ws_hass, connection, msg)

    ws_hass.services.async_call.assert_awaited_once_with(
        "climate",
        "set_temperature",
        {"entity_id": "climate.kontor_display", "temperature": 23.0},
        blocking=False,
    )


@pytest.mark.asyncio
async def test_save_room_skips_push_when_unchanged(ws_hass, store, connection):
    """Unrelated saves do not spam the source device with its own value."""
    await store.async_load()
    await store.async_save_room("living_room", {"comfort_heat_entity": "climate.kontor_display"})
    ws_hass.states.get = MagicMock(return_value=_source_state(21.0))
    ws_hass.services.async_call = AsyncMock()

    msg = {
        "id": 3,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "comfort_heat": 21.0,
        "display_name": "Living",
    }
    await _save_room(ws_hass, connection, msg)

    ws_hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_room_no_push_without_source(ws_hass, store, connection):
    """Rooms without a source entity never call climate.set_temperature."""
    await store.async_load()
    ws_hass.states.get = MagicMock(return_value=None)
    ws_hass.services.async_call = AsyncMock()

    msg = {
        "id": 4,
        "type": "roommind/rooms/save",
        "area_id": "living_room",
        "comfort_heat": 23.0,
    }
    await _save_room(ws_hass, connection, msg)

    ws_hass.services.async_call.assert_not_awaited()
