"""Tests for the external comfort setpoint source (climate entity)."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.const import UnitOfTemperature

from custom_components.roommind.utils.comfort_utils import (
    build_set_temperature_data,
    get_comfort_entity,
    read_comfort_heat_setpoint,
    setpoint_signature,
)

ENTITY = "climate.kontor_display"


def _make_hass(state=None, unit=UnitOfTemperature.CELSIUS) -> MagicMock:
    """Return a mock hass whose states.get returns *state* for any entity."""
    hass = MagicMock()
    hass.config.units.temperature_unit = unit
    hass.states.get = MagicMock(return_value=state)
    return hass


def _make_state(state_str="heat", **attrs) -> MagicMock:
    s = MagicMock()
    s.state = state_str
    s.attributes = attrs
    return s


class TestGetComfortEntity:
    """Tests for get_comfort_entity."""

    def test_missing_field(self):
        """A room without the field has no source."""
        assert get_comfort_entity({}) == ""

    def test_none_value(self):
        """A stored None is treated as not configured."""
        assert get_comfort_entity({"comfort_heat_entity": None}) == ""

    def test_configured(self):
        """The configured entity id is returned."""
        assert get_comfort_entity({"comfort_heat_entity": ENTITY}) == ENTITY


class TestReadComfortHeatSetpoint:
    """Tests for read_comfort_heat_setpoint."""

    def test_no_entity_configured(self):
        """Rooms without a source entity return None (use stored comfort)."""
        hass = _make_hass(_make_state(temperature=22.0))
        assert read_comfort_heat_setpoint(hass, {}) is None

    def test_reads_temperature_attribute(self):
        """A single-setpoint climate entity provides its temperature."""
        hass = _make_hass(_make_state(temperature=22.5, current_temperature=20.1))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) == 22.5

    def test_falls_back_to_target_temp_low(self):
        """A range-capable entity provides its heating end."""
        hass = _make_hass(_make_state(target_temp_low=20.0, target_temp_high=25.0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) == 20.0

    def test_temperature_wins_over_range(self):
        """When both are present, the single setpoint is used."""
        hass = _make_hass(_make_state(temperature=22.0, target_temp_low=20.0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) == 22.0

    def test_missing_entity(self):
        """A missing entity keeps the stored comfort temperature."""
        hass = _make_hass(None)
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) is None

    def test_unavailable_entity(self):
        """An unavailable entity keeps the stored comfort temperature."""
        hass = _make_hass(_make_state("unavailable", temperature=22.0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) is None

    def test_no_setpoint_attribute(self):
        """An entity without any setpoint attribute yields None."""
        hass = _make_hass(_make_state(current_temperature=21.0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) is None

    def test_implausible_value_ignored(self):
        """A 0°C setpoint (e.g. device off) does not become the comfort temp."""
        hass = _make_hass(_make_state(temperature=0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) is None

    def test_non_numeric_value_ignored(self):
        """Garbage attribute values are ignored."""
        hass = _make_hass(_make_state(temperature="warm"))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) is None

    def test_off_entity_still_provides_setpoint(self):
        """An entity in hvac off still reports a usable dial value."""
        hass = _make_hass(_make_state("off", temperature=21.0))
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) == 21.0

    def test_fahrenheit_converted_to_celsius(self):
        """Setpoints are converted to the Celsius the store keeps."""
        hass = _make_hass(_make_state(temperature=72.0), unit=UnitOfTemperature.FAHRENHEIT)
        assert read_comfort_heat_setpoint(hass, {"comfort_heat_entity": ENTITY}) == 22.22


class TestBuildSetTemperatureData:
    """Tests for build_set_temperature_data."""

    def test_single_setpoint_entity(self):
        """Single-setpoint entities get a plain temperature."""
        hass = _make_hass(_make_state(temperature=21.0))
        assert build_set_temperature_data(hass, ENTITY, 22.5) == {
            "entity_id": ENTITY,
            "temperature": 22.5,
        }

    def test_range_entity_keeps_cooling_end(self):
        """Range entities get target_temp_low, keeping their cooling end."""
        hass = _make_hass(_make_state(target_temp_low=20.0, target_temp_high=25.0))
        assert build_set_temperature_data(hass, ENTITY, 21.0) == {
            "entity_id": ENTITY,
            "target_temp_low": 21.0,
            "target_temp_high": 25.0,
        }

    def test_range_entity_cooling_end_pushed_up(self):
        """A heat setpoint above the cooling end pushes the cooling end up."""
        data = build_set_temperature_data(
            _make_hass(_make_state(target_temp_low=20.0, target_temp_high=21.0)), ENTITY, 23.0
        )
        assert data == {"entity_id": ENTITY, "target_temp_low": 23.0, "target_temp_high": 23.0}

    def test_unavailable_entity(self):
        """No service data is built for an unavailable entity."""
        hass = _make_hass(_make_state("unavailable", temperature=21.0))
        assert build_set_temperature_data(hass, ENTITY, 22.0) is None

    def test_missing_entity(self):
        """No service data is built for a missing entity."""
        assert build_set_temperature_data(_make_hass(None), ENTITY, 22.0) is None

    def test_fahrenheit_converted_from_celsius(self):
        """The Celsius value is converted to the HA unit before sending."""
        hass = _make_hass(_make_state(temperature=70.0), unit=UnitOfTemperature.FAHRENHEIT)
        assert build_set_temperature_data(hass, ENTITY, 22.0) == {
            "entity_id": ENTITY,
            "temperature": 71.6,
        }


class TestSetpointSignature:
    """Tests for setpoint_signature."""

    def test_none_state(self):
        """A missing state has an empty signature."""
        assert setpoint_signature(None) == ()

    def test_unrelated_attribute_change(self):
        """Current temperature churn does not look like a dial change."""
        before = _make_state(temperature=21.0, current_temperature=20.0)
        after = _make_state(temperature=21.0, current_temperature=20.4)
        assert setpoint_signature(before) == setpoint_signature(after)

    def test_setpoint_change(self):
        """Turning the dial changes the signature."""
        before = _make_state(temperature=21.0)
        after = _make_state(temperature=22.0)
        assert setpoint_signature(before) != setpoint_signature(after)

    def test_availability_change(self):
        """Becoming unavailable changes the signature."""
        before = _make_state(temperature=21.0)
        after = _make_state("unavailable", temperature=21.0)
        assert setpoint_signature(before) != setpoint_signature(after)
