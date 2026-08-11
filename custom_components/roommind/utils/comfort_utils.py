"""External comfort setpoint source (a climate entity) for RoomMind.

A room may point ``comfort_heat_entity`` at a climate entity — typically a wall
display such as the Aqara W100, which exposes its dial as ``climate.<name>``.
Its heating setpoint is then mirrored into the room's ``comfort_heat`` so the
physical dial changes the comfort temperature. Rooms without the field set are
untouched and keep using the value stored from the panel.
"""

from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, State

from .temp_utils import celsius_to_ha_temp, ha_temp_to_celsius

_LOGGER = logging.getLogger(__name__)

COMFORT_ENTITY_FIELD = "comfort_heat_entity"

# Same bounds as the panel input and the RoomMind override climate entity.
# Setpoints outside this range are treated as noise (e.g. a device reporting 0
# while off) and ignored so the stored comfort temperature survives.
COMFORT_MIN = 5.0
COMFORT_MAX = 35.0

_UNUSABLE_STATES = ("unavailable", "unknown")


def get_comfort_entity(room: dict) -> str:
    """Return the room's comfort source entity id, or '' when not configured."""
    entity_id = room.get(COMFORT_ENTITY_FIELD) or ""
    return str(entity_id)


def setpoint_signature(state: State | None) -> tuple:
    """Return the parts of *state* that decide the comfort setpoint.

    Used to tell a real dial change from unrelated attribute churn (current
    temperature, battery, …) on the same entity.
    """
    if state is None:
        return ()
    return (
        state.state in _UNUSABLE_STATES,
        state.attributes.get(ATTR_TEMPERATURE),
        state.attributes.get(ATTR_TARGET_TEMP_LOW),
    )


def read_comfort_heat_setpoint(hass: HomeAssistant, room: dict) -> float | None:
    """Return the heating setpoint (°C) of the room's comfort source entity.

    Returns None when no entity is configured, the entity is missing or
    unavailable, or it reports no plausible setpoint — every one of which means
    "keep the stored comfort temperature".
    """
    entity_id = get_comfort_entity(room)
    if not entity_id:
        return None

    state = hass.states.get(entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return None

    # A range-capable entity (heat_cool) exposes the heating end as
    # target_temp_low; single-setpoint entities use temperature.
    raw = state.attributes.get(ATTR_TEMPERATURE)
    if raw is None:
        raw = state.attributes.get(ATTR_TARGET_TEMP_LOW)
    if raw is None:
        return None

    try:
        value = ha_temp_to_celsius(hass, float(raw), entity_id=entity_id)
    except (TypeError, ValueError):
        return None

    if not COMFORT_MIN <= value <= COMFORT_MAX:
        _LOGGER.debug(
            "Comfort source '%s' reported %.1f°C, outside %.0f–%.0f°C — ignoring",
            entity_id,
            value,
            COMFORT_MIN,
            COMFORT_MAX,
        )
        return None

    # Rounded so a converted (°F) setpoint lands on a stable value instead of
    # drifting the store by fractions on every cycle.
    return round(value, 2)


def build_set_temperature_data(hass: HomeAssistant, entity_id: str, value_c: float) -> dict | None:
    """Build climate.set_temperature service data pushing *value_c* (°C) to *entity_id*.

    Returns None when the entity cannot take a setpoint right now. Range-capable
    entities get target_temp_low/high so their cooling end is left as it is.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in _UNUSABLE_STATES:
        return None

    value = round(celsius_to_ha_temp(hass, value_c), 1)

    if state.attributes.get(ATTR_TEMPERATURE) is None and state.attributes.get(ATTR_TARGET_TEMP_LOW) is not None:
        high = state.attributes.get(ATTR_TARGET_TEMP_HIGH)
        data = {"entity_id": entity_id, ATTR_TARGET_TEMP_LOW: value}
        if high is not None:
            data[ATTR_TARGET_TEMP_HIGH] = max(float(high), value)
        return data

    return {"entity_id": entity_id, ATTR_TEMPERATURE: value}
