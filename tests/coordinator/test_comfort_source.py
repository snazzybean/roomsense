"""Tests for mirroring an external climate entity into a room's comfort temp."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    SAMPLE_ROOM,
    _create_coordinator,
    _make_store_mock,
    make_mock_states_get,
)

AREA = "living_room_abc12345"
SOURCE = "climate.kontor_display"

ROOM_WITH_SOURCE = {**SAMPLE_ROOM, "comfort_heat_entity": SOURCE}


def _states(setpoint=22.5, state="heat"):
    """States mock where the comfort source reports *setpoint*."""
    return make_mock_states_get(extra={SOURCE: (state, {"temperature": setpoint})})


async def _sync(coordinator, rooms):
    """Run the comfort sync with the state listener stubbed out."""
    with patch("custom_components.roommind.coordinator.async_track_state_change_event") as track:
        await coordinator._async_sync_comfort_sources(rooms)
    return track


class TestComfortSourceSync:
    """Tests for _async_sync_comfort_sources."""

    @pytest.mark.asyncio
    async def test_setpoint_becomes_comfort_heat(self, hass, mock_config_entry):
        """The source entity's setpoint replaces the stored comfort temp."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(22.5))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(ROOM_WITH_SOURCE)}
        await _sync(coordinator, rooms)

        assert rooms[AREA]["comfort_heat"] == 22.5
        assert rooms[AREA]["comfort_temp"] == 22.5
        store.async_update_room.assert_awaited_once_with(AREA, {"comfort_heat": 22.5, "comfort_temp": 22.5})

    @pytest.mark.asyncio
    async def test_dial_above_cooling_target_raises_it(self, hass, mock_config_entry):
        """A dial above comfort_cool pushes the cooling target up, keeping the dead-band."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(26.0))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(ROOM_WITH_SOURCE)}  # comfort_cool is 24.0
        await _sync(coordinator, rooms)

        assert rooms[AREA]["comfort_heat"] == 26.0
        assert rooms[AREA]["comfort_cool"] == 26.0
        store.async_update_room.assert_awaited_once_with(
            AREA, {"comfort_heat": 26.0, "comfort_temp": 26.0, "comfort_cool": 26.0}
        )

    @pytest.mark.asyncio
    async def test_cooling_target_kept_when_above_dial(self, hass, mock_config_entry):
        """A dial below comfort_cool leaves the cooling target alone."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(22.0))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(ROOM_WITH_SOURCE)}
        await _sync(coordinator, rooms)

        assert rooms[AREA]["comfort_cool"] == 24.0
        store.async_update_room.assert_awaited_once_with(AREA, {"comfort_heat": 22.0, "comfort_temp": 22.0})

    @pytest.mark.asyncio
    async def test_no_source_entity_is_untouched(self, hass, mock_config_entry):
        """Without a source entity the stored comfort temp is used as before."""
        store = _make_store_mock({AREA: dict(SAMPLE_ROOM)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(22.5))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(SAMPLE_ROOM)}
        await _sync(coordinator, rooms)

        assert rooms[AREA]["comfort_heat"] == 21.0
        store.async_update_room.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unavailable_source_keeps_stored_comfort(self, hass, mock_config_entry):
        """An unavailable dial leaves the last known comfort temp in place."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=make_mock_states_get(extra={SOURCE: ("unavailable", {})}))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(ROOM_WITH_SOURCE)}
        await _sync(coordinator, rooms)

        assert rooms[AREA]["comfort_heat"] == 21.0
        store.async_update_room.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unchanged_setpoint_does_not_write(self, hass, mock_config_entry):
        """A dial that already matches the stored value causes no store write."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(21.0))

        coordinator = _create_coordinator(hass, mock_config_entry)
        rooms = {AREA: dict(ROOM_WITH_SOURCE)}
        await _sync(coordinator, rooms)

        store.async_update_room.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_source_entity_is_tracked(self, hass, mock_config_entry):
        """Configured source entities get a state listener for instant response."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(22.5))

        coordinator = _create_coordinator(hass, mock_config_entry)
        track = await _sync(coordinator, {AREA: dict(ROOM_WITH_SOURCE)})

        track.assert_called_once()
        assert track.call_args[0][1] == [SOURCE]

    @pytest.mark.asyncio
    async def test_no_listener_without_sources(self, hass, mock_config_entry):
        """Rooms without a source entity register no listener."""
        store = _make_store_mock({AREA: dict(SAMPLE_ROOM)})
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=make_mock_states_get())

        coordinator = _create_coordinator(hass, mock_config_entry)
        track = await _sync(coordinator, {AREA: dict(SAMPLE_ROOM)})

        track.assert_not_called()

    @pytest.mark.asyncio
    async def test_target_temp_follows_source(self, hass, mock_config_entry):
        """A full cycle heats to the dial's setpoint (schedule on, no block temp)."""
        store = _make_store_mock({AREA: dict(ROOM_WITH_SOURCE)})
        store.async_update_room = AsyncMock()
        hass.data = {"roommind": {"store": store}}
        hass.states.get = MagicMock(side_effect=_states(23.0))
        hass.services.async_call = AsyncMock()

        coordinator = _create_coordinator(hass, mock_config_entry)
        with patch("custom_components.roommind.coordinator.async_track_state_change_event"):
            data = await coordinator._async_update_data()

        assert data["rooms"][AREA]["target_temp"] == 23.0


class TestComfortSourceListener:
    """Tests for the state-change listener on comfort source entities."""

    def _coordinator(self, hass, mock_config_entry):
        hass.data = {"roommind": {"store": _make_store_mock()}}
        return _create_coordinator(hass, mock_config_entry)

    def _event(self, old_temp, new_temp):
        event = MagicMock()
        old = MagicMock(state="heat", attributes={"temperature": old_temp}) if old_temp is not None else None
        new = MagicMock(state="heat", attributes={"temperature": new_temp}) if new_temp is not None else None
        event.data = {"old_state": old, "new_state": new}
        return event

    def test_dial_change_triggers_refresh(self, hass, mock_config_entry):
        """Turning the dial refreshes instead of waiting for the next cycle."""
        coordinator = self._coordinator(hass, mock_config_entry)
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        coordinator._async_comfort_source_changed(self._event(21.0, 22.0))

        hass.async_create_task.assert_called_once()

    def test_unrelated_attribute_change_is_ignored(self, hass, mock_config_entry):
        """Attribute churn without a setpoint change does not refresh."""
        coordinator = self._coordinator(hass, mock_config_entry)
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        coordinator._async_comfort_source_changed(self._event(21.0, 21.0))

        hass.async_create_task.assert_not_called()

    def test_removed_entity_is_ignored(self, hass, mock_config_entry):
        """A removed source entity does not refresh."""
        coordinator = self._coordinator(hass, mock_config_entry)
        hass.async_create_task = MagicMock(side_effect=lambda coro: coro.close())

        coordinator._async_comfort_source_changed(self._event(21.0, None))

        hass.async_create_task.assert_not_called()

    def test_listener_resubscribes_on_change(self, hass, mock_config_entry):
        """Changing the tracked set cancels the old subscription."""
        coordinator = self._coordinator(hass, mock_config_entry)
        unsub = MagicMock()

        with patch(
            "custom_components.roommind.coordinator.async_track_state_change_event",
            return_value=unsub,
        ) as track:
            coordinator._async_track_comfort_sources({SOURCE})
            coordinator._async_track_comfort_sources({SOURCE})  # unchanged -> no resubscribe
            assert track.call_count == 1
            coordinator._async_track_comfort_sources({"climate.other"})

        unsub.assert_called_once()

    def test_cancel_clears_subscription(self, hass, mock_config_entry):
        """Entry unload drops the subscription."""
        coordinator = self._coordinator(hass, mock_config_entry)
        unsub = MagicMock()

        with patch(
            "custom_components.roommind.coordinator.async_track_state_change_event",
            return_value=unsub,
        ):
            coordinator._async_track_comfort_sources({SOURCE})

        coordinator._cancel_comfort_source_listener()

        unsub.assert_called_once()
        assert coordinator._comfort_source_entities == set()
