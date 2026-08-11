"""DataUpdateCoordinator for RoomMind."""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from functools import partial
from typing import Any

from homeassistant.components.persistent_notification import async_create as async_create_notification
from homeassistant.components.persistent_notification import async_dismiss as async_dismiss_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    AC_COOLING_BOOST_TARGET,
    AC_HEATING_BOOST_TARGET,
    CLIMATE_MODE_COOL_ONLY,
    CLIMATE_MODE_HEAT_ONLY,
    DEFAULT_COMFORT_COOL,
    DEFAULT_COMFORT_HEAT,
    DEFAULT_ECO_COOL,
    DEFAULT_ECO_HEAT,
    DEFAULT_OUTDOOR_HEATING_MAX,
    DOMAIN,
    HEATING_BOOST_TARGET,
    HISTORY_ROTATE_CYCLES,
    HISTORY_WRITE_CYCLES,
    MAX_PREDICTION_DELTA,
    MAX_SENSOR_STALENESS,
    MAX_TARGET_TEMP,
    MIN_TARGET_TEMP,
    MODE_COOLING,
    MODE_HEATING,
    MODE_IDLE,
    OUTDOOR_UNAVAILABLE_NOTIFICATION_ID,
    OUTDOOR_UNAVAILABLE_NOTIFY_CYCLES,
    SCHEDULE_STATE_ON,
    THERMAL_SAVE_CYCLES,
    UPDATE_INTERVAL,
    TargetTemps,
    build_override_live,
    is_override_active,
    is_override_suppressed,
    make_roommind_context,
)
from .control.mpc_controller import (
    DEFAULT_OUTDOOR_TEMP_FALLBACK,
    MPCController,
    check_acs_can_heat,
    get_can_heat_cool,
    is_mpc_active,
)
from .control.solar import compute_q_solar_norm
from .control.thermal_model import RoomModelManager
from .managers.ac_coil_dry_manager import AcCoilDryManager, CoilDryRoomResult
from .managers.compressor_group_manager import (
    CompressorGroupConfig,
    CompressorGroupManager,
    CompressorGroupState,
    resolve_master_action,
)
from .managers.cover_orchestrator import CoverOrchestrator, CoverResult
from .managers.ekf_training_manager import EkfTrainingManager
from .managers.heat_source_orchestrator import HeatSourcePlan, evaluate_heat_sources
from .managers.mold_manager import MoldManager
from .managers.residual_heat_tracker import ResidualHeatTracker
from .managers.valve_manager import ValveManager
from .managers.weather_manager import WeatherManager
from .managers.window_manager import WindowManager
from .utils.comfort_utils import (
    get_comfort_entity,
    read_comfort_heat_setpoint,
    setpoint_signature,
)
from .utils.device_utils import (
    build_rooms_devices_map,
    get_ac_eids,
    get_all_entity_ids,
    get_direct_setpoint_eids,
    get_trv_eids,
    room_contributes_to_group,
)
from .utils.history_store import HistoryStore
from .utils.schedule_utils import resolve_schedule_index
from .utils.sensor_utils import read_sensor_value
from .utils.temp_utils import celsius_delta_to_ha, ha_temp_to_celsius, ha_temp_unit_str

_LOGGER = logging.getLogger(__name__)


def _get_area_name(hass: HomeAssistant, area_id: str) -> str:
    """Get human-readable area name from area registry."""
    try:
        area_reg = ar.async_get(hass)
        area = area_reg.async_get_area(area_id)
        return area.name if area else area_id
    except Exception:  # noqa: BLE001
        return area_id


# Per-room entity unique_id suffixes (uid = ``{DOMAIN}_{area_id}{suffix}``).
# All suffixes end differently, so a given uid maps to exactly one
# (area_id, suffix) pair — enabling unambiguous exact matching.
ROOM_ENTITY_SUFFIXES = (
    "_target_temp",
    "_mode",
    "_override",
    "_climate_control",
    "_cover_auto",
    "_cover_paused",
)
# Suffixes only valid when the room has covers configured.
COVER_ENTITY_SUFFIXES = ("_cover_auto", "_cover_paused")


def _match_room_entity(parts: str, rooms: dict) -> tuple[str, str] | None:
    """Match ``parts`` (a uid without the ``{DOMAIN}_`` prefix) to an owning room.

    Returns ``(area_id, suffix)`` when ``parts`` equals ``{area_id}{suffix}`` for
    an existing room and a known per-room suffix, else ``None``.

    Uses exact matching rather than prefix ``startswith`` so a shorter area_id
    (e.g. ``bedroom``) never falsely claims a longer room's entity
    (``bedroom_2_l_override``). (#340)
    """
    for area_id in rooms:
        for suffix in ROOM_ENTITY_SUFFIXES:
            if parts == f"{area_id}{suffix}":
                return area_id, suffix
    return None


class RoomMindCoordinator(DataUpdateCoordinator):
    """Central coordinator for RoomMind room data and state."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.entry = entry
        self.rooms: dict = {}
        self.outdoor_temp: float | None = None
        self.outdoor_temp_effective: float | None = None
        self.outdoor_temp_source: str = "none"
        self.outdoor_humidity: float | None = None
        self._outdoor_unavailable_cycles: int = 0
        self._outdoor_warning_sent: bool = False
        self._window_manager = WindowManager()
        self._previous_modes: dict[str, str] = {}
        self._model_manager: RoomModelManager = RoomModelManager()
        self._model_loaded = False
        self._thermal_save_count: int = 0
        self._history_store: HistoryStore | None = None
        self._history_write_count: int = 0
        self._history_rotate_count: int = 0
        self._pending_predictions: dict[str, float] = {}
        self._prediction_forecasts: dict[str, list[dict]] = {}
        self._weather_manager = WeatherManager(hass)
        self._current_q_solar: float = 0.0
        # Valve protection (anti-seize)
        self._valve_manager = ValveManager(hass)
        # Mold risk tracking
        self._mold_manager = MoldManager(hass)
        # Residual heat tracking (heating → idle transition)
        self._residual_tracker = ResidualHeatTracker()
        # EKF training accumulation
        self._ekf_training = EkfTrainingManager(self._model_manager)
        # Cover/blind automatic control
        from .managers.cover_manager import CoverManager

        self._cover_manager = CoverManager()
        self._cover_orchestrator = CoverOrchestrator(hass, self._cover_manager, self._model_manager)
        # Compressor group management (min-run / min-off protection)
        self._compressor_manager = CompressorGroupManager()
        # AC evaporator drying (anti-odour): bounded fan run after cooling
        self._coil_dry_manager = AcCoilDryManager(hass)
        # Heat source orchestration state (per room)
        self._heat_source_states: dict[str, str] = {}
        # Track which rooms already have entity platform entities registered
        self._entity_areas: set[str] = set()
        # Min-run enforcement: timestamp when current non-idle mode started
        self._mode_on_since: dict[str, float] = {}
        # Sensor dropout fallback: last valid temperature per room
        self._last_valid_temps: dict[str, tuple[float, float]] = {}  # {area_id: (celsius, monotonic_ts)}
        # Startup guard: rooms with at least one valid temperature reading since
        # coordinator start. Full Control rooms send no device commands until then
        # (prevents the off/on bounce after HA restart while sensors are unavailable).
        # Capped at MAX_SENSOR_STALENESS after start so a never-reporting sensor
        # falls back to the same safety shutdown as a mid-operation dropout.
        self._had_valid_temp: set[str] = set()
        self._startup_ts: float = time.monotonic()
        self._startup_guard_warned: set[str] = set()
        # Out-of-range schedule block temps already warned about (#395).
        # Keyed by (area_id, field, raw value) so the coordinator's 30s cycle
        # does not flood the log with the same typo.
        self._block_temp_warned: set[tuple[str, str, str]] = set()
        self._switch_entity_areas: set[str] = set()
        self._climate_control_switch_areas: set[str] = set()
        self._binary_sensor_entity_areas: set[str] = set()
        self._climate_entity_areas: set[str] = set()
        # External comfort setpoint sources (climate entities acting as a dial)
        self._comfort_source_entities: set[str] = set()
        self._comfort_source_unsub: CALLBACK_TYPE | None = None
        entry.async_on_unload(self._cancel_comfort_source_listener)
        # Per-entity cache of schedule blocks; fallback when schedule.get_schedule fails (#308)
        self._schedule_blocks_cache: dict[str, dict] = {}
        # Entity platform callbacks, set by platform async_setup_entry
        self.async_add_entities: Any = None
        self.async_add_switch_entities: Any = None
        self.async_add_climate_entities: Any = None
        self.async_add_binary_sensor_entities: Any = None

    async def _async_update_data(self) -> dict:
        """Fetch and compute state for all rooms.

        This is the central loop that:
        1. Reads current temperatures from sensor entities
        2. Evaluates active schedule for each room
        3. Determines heating/cooling action per room
        4. Applies climate control commands
        5. Returns state dict consumed by sensor entities
        """
        store = self.hass.data[DOMAIN]["store"]
        rooms = store.get_rooms()

        # Mirror external comfort setpoint dials into the rooms of this cycle
        await self._async_sync_comfort_sources(rooms)

        # Read outdoor sensors from global settings
        settings = store.get_settings()
        outdoor_sensor_id = settings.get("outdoor_temp_sensor")
        raw_outdoor = read_sensor_value(self.hass, outdoor_sensor_id, "global", "outdoor temperature")
        self.outdoor_temp = (
            ha_temp_to_celsius(self.hass, raw_outdoor, entity_id=outdoor_sensor_id) if raw_outdoor is not None else None
        )
        self.outdoor_humidity = read_sensor_value(
            self.hass, settings.get("outdoor_humidity_sensor"), "global", "outdoor humidity"
        )

        # Effective outdoor temperature: sensor → weather entity → none.
        # The EKF must not train with a degenerate fallback (e.g. room temp);
        # see _async_process_room where this gates EKF updates.
        self.outdoor_temp_effective, self.outdoor_temp_source = self._resolve_outdoor_temp(settings)
        self._update_outdoor_unavailable_notification(settings)

        # Load compressor groups from settings (every cycle, cheap)
        self._compressor_manager.load_groups(settings.get("compressor_groups", []))

        # Load thermal model and valve actuation data from store (once)
        if not self._model_loaded:
            thermal_data = store.get_thermal_data()
            if thermal_data:
                self._model_manager = RoomModelManager.from_dict(thermal_data)
                self._ekf_training._model_manager = self._model_manager
                self._cover_orchestrator._model_manager = self._model_manager
            self._valve_manager.load_actuation_data(settings.get("valve_last_actuation", {}))
            self._coil_dry_manager.load_state(settings.get("coil_dry_state", {}))
            self._model_loaded = True

        # Initialize history store (once)
        if self._history_store is None:
            self._history_store = HistoryStore(self.hass.config.path(".storage/roommind_history"))

        room_states: dict[str, dict] = {}

        # Read weather forecast once for all rooms
        outdoor_forecast = await self._weather_manager.async_read_forecast(settings)

        # Update cover orchestrator with cloud forecast for solar trajectory prediction
        self._cover_orchestrator.set_cloud_series(WeatherManager.extract_cloud_series(outdoor_forecast))

        # Compute solar irradiance once per cycle
        cloud_coverage = None
        weather_entity = settings.get("weather_entity")
        if weather_entity:
            ws = self.hass.states.get(weather_entity)
            if ws:
                cloud_coverage = ws.attributes.get("cloud_coverage")
        self._current_q_solar = compute_q_solar_norm(
            self.hass.config.latitude,
            self.hass.config.longitude,
            time.time(),
            cloud_coverage,
        )

        for area_id, room in rooms.items():
            try:
                room_state = await self._async_process_room(room, settings, outdoor_forecast)
                room_states[area_id] = room_state
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Room '%s': processing failed, skipping", area_id)

        # Control master devices based on aggregate room demand
        await self._async_control_master_devices(room_states, rooms, settings)

        # Record to history store (throttled)
        learning_disabled = set(settings.get("learning_disabled_rooms", []))
        self._history_write_count += 1
        if self._history_write_count >= HISTORY_WRITE_CYCLES and self._history_store:
            self._history_write_count = 0
            for area_id, rs in room_states.items():
                if area_id in learning_disabled:
                    continue
                current_temp = rs.get("current_temp")
                mode = rs.get("mode", MODE_IDLE)
                target_temp = rs.get("target_temp")
                # Use the prediction made *last* write cycle for the
                # current timestamp — this compares "what the model
                # predicted would happen" vs "what actually happened".
                predicted = self._pending_predictions.pop(area_id, None)
                try:
                    await self.hass.async_add_executor_job(
                        self._history_store.record,
                        area_id,
                        {
                            "room_temp": rs.get("current_temp_raw", current_temp),
                            "outdoor_temp": self.outdoor_temp_effective,
                            "target_temp": target_temp,
                            "mode": mode,
                            "predicted_temp": predicted,
                            "window_open": rs.get("window_open", False),
                            "heating_power": rs.get("heating_power", 0),
                            "solar_irradiance": round(self._current_q_solar, 3),
                            "blind_position": rs.get("blind_position"),
                            "cover_reason": rs.get("cover_reason", ""),
                            "device_setpoint": rs.get("device_setpoint"),
                            "occupancy": rs.get("q_occupancy", 0.0) > 0,
                        },
                    )
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("History record failed for '%s'", area_id)
                # Compute prediction for the *next* write cycle (~3 min ahead)
                room_config = rooms.get(area_id, {})
                if (
                    current_temp is not None
                    and self.outdoor_temp_effective is not None
                    and not room_config.get("is_outdoor", False)
                ):
                    try:
                        is_window_open = rs.get("window_open", False)
                        if is_window_open:
                            raw_pred = self._model_manager.predict_window_open(
                                area_id,
                                current_temp,
                                self.outdoor_temp_effective,
                                3.0,
                            )
                        else:
                            model = self._model_manager.get_model(area_id)
                            hp = rs.get("heating_power", 100) / 100.0
                            Q = (
                                hp * model.Q_heat
                                if mode == "heating"
                                else (-hp * model.Q_cool if mode == "cooling" else 0.0)
                            )
                            raw_pred = model.predict(
                                current_temp,
                                self.outdoor_temp_effective,
                                Q,
                                3.0,
                                q_solar=self._current_q_solar * rs.get("shading_factor", 1.0),
                            )
                        # Sanity clamp: prevent unrealistic jumps in one prediction step
                        clamped = max(
                            current_temp - MAX_PREDICTION_DELTA, min(current_temp + MAX_PREDICTION_DELTA, raw_pred)
                        )
                        self._pending_predictions[area_id] = round(clamped, 2)
                    except Exception:  # noqa: BLE001
                        pass

        # Save thermal data periodically
        self._thermal_save_count += 1
        if self._thermal_save_count >= THERMAL_SAVE_CYCLES:
            self._thermal_save_count = 0
            await store.async_save_thermal_data(self._model_manager.to_dict())

        # Rotate history periodically
        self._history_rotate_count += 1
        if self._history_rotate_count >= HISTORY_ROTATE_CYCLES and self._history_store:
            self._history_rotate_count = 0
            for area_id in rooms:
                try:
                    await self.hass.async_add_executor_job(self._history_store.rotate, area_id)
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("History rotation failed for '%s'", area_id)

        # Valve protection: finish active cycles (runs every tick, cheap).
        # Pass a {eid: devices[]} map so idle_action is respected when the
        # cycle closes (idle_action="low" TRVs stay awake instead of being
        # hard-turned-off).
        await self._valve_manager.async_finish_cycles(build_rooms_devices_map(rooms))

        # Valve protection: check for stale valves (throttled)
        if self._valve_manager.should_run_cycle_check():
            await self._valve_manager.async_check_and_cycle(rooms, settings)

        # Persist valve actuation timestamps (piggyback on thermal save cycle)
        if self._valve_manager.actuation_dirty and self._thermal_save_count == 0:
            await store.async_save_settings({"valve_last_actuation": self._valve_manager.get_actuation_data()})
            self._valve_manager.actuation_dirty = False

        # Coil dry: drop state for devices no longer configured anywhere
        self._coil_dry_manager.prune(set(build_rooms_devices_map(rooms)))

        # Persist coil dry state on change — a 20 min run is far shorter than
        # the 15 min thermal save cycle, so this must not piggyback on it.
        if self._coil_dry_manager.state_dirty:
            await store.async_save_settings({"coil_dry_state": self._coil_dry_manager.get_state()})
            self._coil_dry_manager.state_dirty = False

        self.rooms = room_states
        return {"rooms": room_states}

    def _read_room_sensors(
        self,
        room: dict,
        area_id: str,
    ) -> tuple[float | None, float | None, float | None, bool]:
        """Read temperature and humidity sensors for a room.

        Returns (current_temp, current_temp_raw, current_humidity, has_external_sensor).
        """
        temp_sensor_id = room.get("temperature_sensor")
        has_external_sensor = bool(temp_sensor_id)

        raw_temp = read_sensor_value(self.hass, temp_sensor_id, area_id, "temperature")
        current_temp = (
            ha_temp_to_celsius(self.hass, raw_temp, entity_id=temp_sensor_id) if raw_temp is not None else None
        )

        # Fallback: read current_temperature from first thermostat/AC if no external sensor
        if current_temp is None and not has_external_sensor:
            raw_dev = self._read_device_temp(room)
            current_temp = ha_temp_to_celsius(self.hass, raw_dev) if raw_dev is not None else None

        # --- Sensor dropout fallback: use cached temp if fresh enough ---
        current_temp_raw = current_temp  # preserve original for EKF/history

        if current_temp is not None:
            self._last_valid_temps[area_id] = (current_temp, time.monotonic())
            self._had_valid_temp.add(area_id)
        elif area_id in self._last_valid_temps:
            cached_temp, cached_ts = self._last_valid_temps[area_id]
            if time.monotonic() - cached_ts < MAX_SENSOR_STALENESS:
                current_temp = cached_temp
                _LOGGER.debug(
                    "Room '%s': sensor unavailable, using cached temp %.1f°C (age %.0fs)",
                    area_id,
                    cached_temp,
                    time.monotonic() - cached_ts,
                )
            else:
                del self._last_valid_temps[area_id]

        current_humidity = read_sensor_value(self.hass, room.get("humidity_sensor"), area_id, "humidity")

        return current_temp, current_temp_raw, current_humidity, has_external_sensor

    def _resolve_outdoor_temp(self, settings: dict) -> tuple[float | None, str]:
        """Return (temp, source) for the current cycle.

        Source is one of:
          - "sensor": primary outdoor_temp_sensor delivered a value
          - "weather": weather_entity attribute "temperature" delivered a value
          - "none": neither source available

        ``self.outdoor_temp`` remains the raw sensor reading for diagnostics;
        the result of this method is stored in ``self.outdoor_temp_effective``
        and is the canonical value all consumers (EKF, MPC, cover, heat-source,
        analytics, mold) use. EKF training is gated on a non-None effective
        temperature so the filter never trains with a degenerate fallback
        (e.g. room temp), which would cause the alpha state to drift to the
        upper bound — see #301.
        """
        if self.outdoor_temp is not None:
            return self.outdoor_temp, "sensor"

        weather_eid = settings.get("weather_entity") or ""
        if weather_eid:
            ws = self.hass.states.get(weather_eid)
            if ws is not None and ws.state not in ("unavailable", "unknown"):
                temp_attr = ws.attributes.get("temperature")
                if isinstance(temp_attr, (int, float)) and not isinstance(temp_attr, bool):
                    converted = ha_temp_to_celsius(self.hass, float(temp_attr), entity_id=weather_eid)
                    if converted is not None:
                        return converted, "weather"

        return None, "none"

    def _update_outdoor_unavailable_notification(self, settings: dict) -> None:
        """Track consecutive cycles without a valid outdoor temperature.

        After OUTDOOR_UNAVAILABLE_NOTIFY_CYCLES (default 60 ≈ 30 min) raise a
        single HA persistent notification informing the user that EKF training
        is paused. The notification clears as soon as a valid outdoor source
        returns. Suppressed entirely when the user disables it via the
        ``outdoor_unavailable_notify`` global setting.
        """
        if self.outdoor_temp_effective is not None:
            self._outdoor_unavailable_cycles = 0
            if self._outdoor_warning_sent:
                self._outdoor_warning_sent = False
                async_dismiss_notification(self.hass, OUTDOOR_UNAVAILABLE_NOTIFICATION_ID)
            return

        self._outdoor_unavailable_cycles += 1

        if self._outdoor_warning_sent:
            return
        if self._outdoor_unavailable_cycles < OUTDOOR_UNAVAILABLE_NOTIFY_CYCLES:
            return
        if not settings.get("outdoor_unavailable_notify", True):
            return

        sensor_id = settings.get("outdoor_temp_sensor") or "(not configured)"
        weather_eid = settings.get("weather_entity") or "(not configured)"
        message = (
            "RoomMind cannot read an outdoor temperature. Thermal model "
            "learning is paused for all rooms until a valid source returns.\n\n"
            f"• Outdoor sensor: `{sensor_id}`\n"
            f"• Weather entity: `{weather_eid}`\n\n"
            "Check that the sensor is online or configure a weather entity in "
            "Settings → Outdoor."
        )
        _LOGGER.warning(
            "Outdoor temperature unavailable for %d cycles — EKF learning paused",
            self._outdoor_unavailable_cycles,
        )
        async_create_notification(
            self.hass,
            message,
            title="RoomMind: outdoor temperature unavailable",
            notification_id=OUTDOOR_UNAVAILABLE_NOTIFICATION_ID,
        )
        self._outdoor_warning_sent = True

    async def _evaluate_mold_risk(
        self,
        area_id: str,
        current_temp: float | None,
        current_humidity: float | None,
        settings: dict,
    ) -> tuple[str, float | None, bool, float]:
        """Evaluate mold risk for a room.

        Returns (mold_risk_level, mold_surface_rh, mold_prevention_active, mold_prevention_delta).
        """
        mold = await self._mold_manager.evaluate(
            area_id,
            _get_area_name(self.hass, area_id),
            current_temp,
            current_humidity,
            self.outdoor_temp_effective,
            settings,
            celsius_delta_to_ha_fn=lambda d: celsius_delta_to_ha(self.hass, d),  # type: ignore[misc]
            ha_temp_unit_str_fn=lambda: ha_temp_unit_str(self.hass),  # type: ignore[misc]
        )
        return mold.risk_level, mold.surface_rh, mold.prevention_active, mold.prevention_delta

    async def _async_process_room(self, room: dict, settings: dict, outdoor_forecast: list[dict]) -> dict:
        """Process a single room: read sensor, evaluate schedule, apply control."""
        area_id = room.get("area_id", "unknown")

        current_temp, current_temp_raw, current_humidity, has_external_sensor = self._read_room_sensors(room, area_id)

        # --- Outdoor room: skip all control logic ---
        if room.get("is_outdoor", False):
            return {
                "area_id": area_id,
                "current_temp": current_temp,
                "current_temp_raw": current_temp_raw,
                "current_humidity": current_humidity,
                "target_temp": None,
                "heat_target": None,
                "cool_target": None,
                "mode": MODE_IDLE,
                "heating_power": 0,
                "device_setpoint": None,
                "window_open": False,
                "override_active": False,
                "override_type": None,
                "override_heat": None,
                "override_cool": None,
                "override_until": None,
                "override_suppressed": False,
                "active_schedule_index": -1,
                "confidence": None,
                "mpc_active": False,
                "presence_away": False,
                "force_off": False,
                "mold_risk_level": "ok",
                "mold_surface_rh": None,
                "mold_prevention_active": False,
                "mold_prevention_delta": 0,
                "shading_factor": 1.0,
                "n_observations": 0,
                "blind_position": None,
                "cover_auto_paused": False,
                "cover_forced_reason": "",
                "active_cover_schedule_index": -1,
                "q_occupancy": 0.0,
                "active_heat_sources": None,
                "compressor_protection_active": False,
                "compressor_protection_reason": None,
                "coil_dry_active": False,
                "coil_dry_phase": None,
                "coil_dry_until": None,
                "coil_dry_entities": [],
                "schedule_temp_warnings": [],
            }

        # --- Mold risk calculation ---
        (
            mold_risk_level,
            mold_surface_rh,
            mold_prevention_active_room,
            mold_prevention_temp_delta,
        ) = await self._evaluate_mold_risk(area_id, current_temp, current_humidity, settings)

        # Load schedule blocks once — used for both target temp resolution and MPC lookahead.
        from .utils.schedule_utils import (
            find_rejected_block_temps,
            get_active_schedule_entity,
            make_target_resolver,
            read_schedule_blocks,
        )

        schedule_entity_id = get_active_schedule_entity(self.hass, room)
        schedule_blocks = (
            await read_schedule_blocks(self.hass, schedule_entity_id, cache=self._schedule_blocks_cache)
            if schedule_entity_id
            else None
        )

        # Scan the whole week for unusable block temps so the panel can flag a
        # typo in a block that is not running right now (#395). Only the active
        # schedule is loaded, so a typo in a deselected schedule surfaces once
        # that schedule is picked.
        schedule_temp_warnings = find_rejected_block_temps(schedule_blocks, partial(ha_temp_to_celsius, self.hass))

        # Determine dual heat/cool target temperatures
        # Returns TargetTemps(heat, cool). None values mean "force off".
        targets = self._resolve_target_temps(room, settings, schedule_blocks, schedule_entity_id)

        # Apply mold prevention temperature delta (heating target only).
        # Safety: mold prevention overrides "off" to prevent structural damage.
        force_off = targets.heat is None and targets.cool is None
        if mold_prevention_active_room and mold_prevention_temp_delta > 0:
            if force_off:
                eco_heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
                eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)
                targets = TargetTemps(
                    heat=eco_heat + mold_prevention_temp_delta,
                    cool=eco_cool,
                )
                force_off = False
            elif targets.heat is not None:
                targets = TargetTemps(
                    heat=targets.heat + mold_prevention_temp_delta,
                    cool=targets.cool,
                )
        presence_away = not room.get("ignore_presence", False) and self._is_presence_away(room, settings)
        target_resolver = make_target_resolver(
            schedule_blocks,
            room,
            settings,
            hass=self.hass,
            presence_away=presence_away,
            mold_prevention_delta=mold_prevention_temp_delta,
        )

        # --- Compute residual heat from previous cycle state ---
        system_type = room.get("heating_system_type", "")
        q_residual = self._residual_tracker.get_q_residual(
            area_id,
            system_type,
            self._previous_modes.get(area_id, MODE_IDLE),
        )

        # Read current cover positions for shading factor
        cover_eids: list[str] = room.get("covers", [])
        cover_pos_result = self._cover_orchestrator.read_positions(area_id, room)
        shading_factor = cover_pos_result.shading_factor

        # Read occupancy sensors for thermal model (OR logic: any sensor "on" → occupied)
        q_occupancy = 0.0
        for occ_eid in room.get("occupancy_sensors", []):
            occ_state = self.hass.states.get(occ_eid)
            if occ_state and occ_state.state == "on":
                q_occupancy = 1.0
                break
            # unavailable/unknown/off → skip (conservative: no occupancy heat)

        # Determine and apply mode with MPC controller
        controller = MPCController(
            self.hass,
            room,
            model_manager=self._model_manager,
            outdoor_temp=self.outdoor_temp_effective,
            outdoor_forecast=outdoor_forecast,
            settings=settings,
            previous_mode=self._previous_modes.get(area_id, MODE_IDLE),
            mode_on_since=self._mode_on_since.get(area_id),
            has_external_sensor=has_external_sensor,
            target_resolver=target_resolver,
            q_solar=self._current_q_solar,
            latitude=self.hass.config.latitude,
            longitude=self.hass.config.longitude,
            cloud_series=WeatherManager.extract_cloud_series(outdoor_forecast),
            q_residual=q_residual,
            heating_system_type=system_type,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
        )
        mode, power_fraction = await controller.async_evaluate(current_temp, targets)

        # Compute effective single target_temp for display/history (mode + climate_mode aware)
        climate_mode = room.get("climate_mode", "auto")
        if climate_mode == CLIMATE_MODE_COOL_ONLY:
            target_temp = targets.cool
        elif climate_mode == CLIMATE_MODE_HEAT_ONLY:
            target_temp = targets.heat
        else:  # auto
            if mode == MODE_HEATING and targets.heat is not None:
                target_temp = targets.heat
            elif mode == MODE_COOLING and targets.cool is not None:
                target_temp = targets.cool
            else:
                target_temp = targets.heat if targets.heat is not None else targets.cool

        # Force idle when target resolved to "off" (presence away or schedule off)
        if force_off:
            mode = MODE_IDLE
            power_fraction = 0.0

        # Store MPC prediction forecast for analytics
        if controller.last_plan and len(controller.last_plan.temperatures) > 1:
            plan = controller.last_plan
            now_ts = time.time()
            dt_s = plan.dt_minutes * 60
            self._prediction_forecasts[area_id] = [
                {"ts": round(now_ts + i * dt_s, 1), "temp": round(t, 2)} for i, t in enumerate(plan.temperatures)
            ]
        else:
            self._prediction_forecasts.pop(area_id, None)

        # Pause climate control when any window/door is open (with configurable delays)
        raw_open = self._is_window_open(room)
        window_open = self._window_manager.update(
            area_id,
            raw_open,
            room.get("window_open_delay", 0),
            room.get("window_close_delay", 0),
        )
        if window_open:
            mode = MODE_IDLE
            power_fraction = 0.0

        climate_active = settings.get("climate_control_active", True) and room.get("climate_control_enabled", True)
        # Startup guard: Full Control room without any temperature reading yet —
        # leave devices in their current state instead of idling them.
        waiting_for_data = has_external_sensor and self._waiting_for_first_reading(area_id)
        if (
            climate_active
            and has_external_sensor
            and not waiting_for_data
            and area_id not in self._had_valid_temp
            and area_id not in self._startup_guard_warned
        ):
            self._startup_guard_warned.add(area_id)
            _LOGGER.warning(
                "Room '%s': no temperature reading within %ds after startup, "
                "resuming control (devices stay off until the sensor reports)",
                area_id,
                MAX_SENSOR_STALENESS,
            )

        # Read device temperature limits for dynamic boost targets
        trv_max_temps: list[float] = []
        for eid in get_trv_eids(room.get("devices", [])):
            st = self.hass.states.get(eid)
            if st and st.attributes.get("max_temp") is not None:
                trv_max_temps.append(ha_temp_to_celsius(self.hass, st.attributes["max_temp"]))
        device_max_temp = min(trv_max_temps) if trv_max_temps else None

        ac_min_temps: list[float] = []
        ac_max_temps: list[float] = []
        for eid in get_ac_eids(room.get("devices", [])):
            st = self.hass.states.get(eid)
            if st:
                if st.attributes.get("min_temp") is not None:
                    ac_min_temps.append(ha_temp_to_celsius(self.hass, st.attributes["min_temp"]))
                if st.attributes.get("max_temp") is not None:
                    ac_max_temps.append(ha_temp_to_celsius(self.hass, st.attributes["max_temp"]))
        device_min_temp = max(ac_min_temps) if ac_min_temps else None
        ac_device_max_temp = min(ac_max_temps) if ac_max_temps else None

        # Exclude TRVs currently being valve-protection-cycled from normal control
        cycling_eids = {
            eid for eid in get_trv_eids(room.get("devices", [])) if self._valve_manager.is_entity_cycling(eid)
        }

        # Heat source orchestration: smart routing for rooms with both TRVs and ACs
        heat_source_plan = None
        if (
            room.get("heat_source_orchestration", False)
            and mode == MODE_HEATING
            and has_external_sensor
            and get_trv_eids(room.get("devices", []))
            and get_ac_eids(room.get("devices", []))
        ):
            heat_source_plan = evaluate_heat_sources(
                room_config=room,
                mode=mode,
                power_fraction=power_fraction,
                current_temp=current_temp,
                target_temp=targets.heat,
                outdoor_temp=self.outdoor_temp_effective,
                previous_active_sources=self._heat_source_states.get(area_id, "none"),
                hass=self.hass,
            )
            if heat_source_plan is not None:
                self._heat_source_states[area_id] = heat_source_plan.active_sources
            else:
                # Orchestrator returned None (e.g. missing current/target temp).
                # The non-orchestrated async_apply path commands all devices,
                # so clear stale state to prevent the master-demand filter
                # from acting on a previous orchestration decision.
                self._heat_source_states.pop(area_id, None)
        else:
            # Orchestration not active for this room — remove stale state
            # so re-enabling starts fresh.
            self._heat_source_states.pop(area_id, None)

        # Compressor group constraints
        all_device_eids = get_all_entity_ids(room.get("devices", []))
        compressor_forced_on: set[str] = set()
        compressor_forced_off: set[str] = set()
        compressor_protection_reason: str | None = None

        if all_device_eids and climate_active and not window_open and not force_off:
            for eid in all_device_eids:
                if self._compressor_manager.get_group_for_entity(eid) is None:
                    continue
                if mode != MODE_IDLE:
                    if not self._compressor_manager.check_can_activate(eid):
                        compressor_forced_off.add(eid)
                    else:
                        enforced = self._compressor_manager.get_enforced_action(eid)
                        if enforced is not None and enforced != "idle":
                            if (mode == MODE_HEATING and enforced == "cool") or (
                                mode == MODE_COOLING and enforced == "heat"
                            ):
                                compressor_forced_off.add(eid)
                else:
                    if self._compressor_manager.check_must_stay_active(eid):
                        compressor_forced_on.add(eid)

            # Exposed for the UI (#compressor-protection-visibility). Must be
            # captured here, before the "fully blocked" branch below clears
            # compressor_forced_off — otherwise a room idled entirely by the
            # shared-compressor guard would silently look like a normal idle.
            if compressor_forced_off:
                compressor_protection_reason = "min_off"
            elif compressor_forced_on:
                compressor_protection_reason = "min_run"

            # In cooling mode only ACs can cool, so TRVs must not prevent the IDLE
            # transition when all cooling-capable devices are blocked.  In heating
            # mode both TRVs and ACs can contribute, so the full device set applies.
            _mode_relevant_eids = (
                set(get_ac_eids(room.get("devices", []))) if mode == MODE_COOLING else set(all_device_eids)
            )
            if compressor_forced_off and _mode_relevant_eids and compressor_forced_off >= _mode_relevant_eids:
                mode = MODE_IDLE
                power_fraction = 0.0
                compressor_forced_off.clear()

        # --- Residual heat transition tracking ---
        # After compressor constraints may have changed mode to IDLE.
        if climate_active and system_type:
            self._residual_tracker.update(
                area_id,
                mode,
                power_fraction,
                self._previous_modes.get(area_id, MODE_IDLE),
                q_residual=q_residual,
            )

        # AC evaporator drying: bounded fan run after cooling.  Runs even under
        # window pause and force_off — the coordinator already bypasses the
        # compressor timers on those paths, and that is exactly when the coil
        # would otherwise sit wet for hours.  Deliberately before the
        # climate_active / waiting_for_data chain and not inside its else
        # branch: the manager must also run when nothing may be commanded, so
        # it can end phases and hold the pending fan restore.
        coil_dry = await self._coil_dry_manager.async_process_room(
            area_id=area_id,
            room=room,
            settings=settings,
            mode=mode,
            commandable=climate_active and not waiting_for_data,
            compressor_forced_on=compressor_forced_on,
            compressor_forced_off=compressor_forced_off,
            exclude_eids=cycling_eids,
            force_off=force_off,
            can_activate=self._compressor_manager.check_can_activate,
        )

        if not climate_active:
            # Climate control disabled — do NOT send commands.
            mode = MODE_IDLE
            power_fraction = 0.0
        elif waiting_for_data:
            mode = MODE_IDLE
            power_fraction = 0.0
            _LOGGER.debug(
                "Room '%s': no temperature reading since startup, skipping device control",
                area_id,
            )
        else:
            try:
                await controller.async_apply(
                    mode,
                    targets,
                    power_fraction=power_fraction,
                    current_temp=current_temp,
                    exclude_eids=cycling_eids | coil_dry.controlled_eids,
                    heating_boost_target=device_max_temp,
                    ac_heating_boost_target=ac_device_max_temp,
                    cooling_boost_target=device_min_temp,
                    heat_source_plan=heat_source_plan,
                    compressor_forced_on=compressor_forced_on or None,
                    compressor_forced_off=compressor_forced_off or None,
                    force_off=force_off,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Room '%s': climate service call failed",
                    area_id,
                    exc_info=True,
                )
            # Update compressor group member states (always, even after failed apply)
            for eid in all_device_eids:
                if self._compressor_manager.get_group_for_entity(eid) is None:
                    continue
                if eid in cycling_eids:
                    continue
                if eid in compressor_forced_off:
                    self._compressor_manager.update_member(eid, False)
                elif eid in compressor_forced_on:
                    # Verify device is actually running before tracking as active.
                    # If user manually turned it off, respect that.
                    dev_state = self.hass.states.get(eid)
                    actually_on = dev_state is not None and dev_state.state not in (
                        "off",
                        "unavailable",
                        "unknown",
                    )
                    self._compressor_manager.update_member(eid, actually_on)
                elif eid in coil_dry.compressor_active_eids:
                    # coil_dry_mode="dry" really runs the compressor
                    self._compressor_manager.update_member(eid, True)
                elif mode != MODE_IDLE:
                    self._compressor_manager.update_member(eid, True)
                else:
                    self._compressor_manager.update_member(eid, False)

        # --- Cover/blind automatic control ---
        has_override = is_override_active(room)
        cover_result = await self._cover_orchestrator.async_process(
            area_id=area_id,
            room=room,
            targets=targets,
            mode=mode,
            current_temp=current_temp,
            outdoor_temp=self.outdoor_temp_effective,
            q_solar=self._current_q_solar,
            predicted_peak_temp=controller.predicted_peak_temp,
            has_override=has_override,
        )

        # Track valve actuation during normal heating (skip excluded entities)
        if mode == MODE_HEATING:
            excluded = set(room.get("valve_protection_exclude", []))
            heating_eids = [eid for eid in get_trv_eids(room.get("devices", [])) if eid not in excluded]
            self._valve_manager.record_heating(heating_eids)

        mpc_active = False
        if has_external_sensor:
            try:
                _ch, _cc = get_can_heat_cool(
                    room,
                    self.outdoor_temp_effective,
                    acs_can_heat=check_acs_can_heat(self.hass, room),
                    override_active=is_override_active(room),
                )
                _T_out = (
                    self.outdoor_temp_effective
                    if self.outdoor_temp_effective is not None
                    else DEFAULT_OUTDOOR_TEMP_FALLBACK
                )
                mpc_active = is_mpc_active(
                    self._model_manager,
                    area_id,
                    _ch,
                    _cc,
                    current_temp or 20.0,
                    _T_out,
                )
            except Exception:  # noqa: BLE001
                mpc_active = False

        display_mode, display_pf = await self._observe_and_train(
            area_id=area_id,
            room=room,
            settings=settings,
            current_temp_raw=current_temp_raw,
            mode=mode,
            power_fraction=power_fraction,
            window_open=window_open,
            raw_open=raw_open,
            q_residual=q_residual,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
            has_external_sensor=has_external_sensor,
            heat_source_plan=heat_source_plan,
            climate_active=climate_active,
            coil_dry_skip_training=coil_dry.skip_ekf_training,
        )

        return self._build_room_state_dict(
            area_id=area_id,
            room=room,
            settings=settings,
            current_temp=current_temp,
            current_temp_raw=current_temp_raw,
            current_humidity=current_humidity,
            target_temp=target_temp,
            targets=targets,
            schedule_temp_warnings=schedule_temp_warnings,
            display_mode=display_mode,
            display_pf=display_pf,
            heat_source_plan=heat_source_plan,
            device_max_temp=device_max_temp,
            ac_device_max_temp=ac_device_max_temp,
            device_min_temp=device_min_temp,
            has_external_sensor=has_external_sensor,
            window_open=window_open,
            presence_away=presence_away,
            force_off=force_off,
            mode=mode,
            power_fraction=power_fraction,
            mold_risk_level=mold_risk_level,
            mold_surface_rh=mold_surface_rh,
            mold_prevention_active_room=mold_prevention_active_room,
            mold_prevention_temp_delta=mold_prevention_temp_delta,
            shading_factor=shading_factor,
            q_occupancy=q_occupancy,
            cover_eids=cover_eids,
            cover_result=cover_result,
            mpc_active=mpc_active,
            compressor_protection_reason=compressor_protection_reason,
            coil_dry=coil_dry,
        )

    async def _observe_and_train(
        self,
        *,
        area_id: str,
        room: dict,
        settings: dict,
        current_temp_raw: float | None,
        mode: str,
        power_fraction: float,
        window_open: bool,
        raw_open: bool,
        q_residual: float,
        shading_factor: float | None,
        q_occupancy: float,
        has_external_sensor: bool,
        heat_source_plan: Any | None,
        climate_active: bool,
        coil_dry_skip_training: bool = False,
    ) -> tuple[str, float]:
        """Observe device state, train EKF, compute display mode.

        Returns (display_mode, display_pf).
        """
        # observed_mode/observed_pf: only populated when climate control is off
        observed_mode: str | None = None
        observed_pf = 0.0

        if not climate_active:
            # Climate control disabled (learn-only) — observe device state
            # for training and display.
            observed_mode, observed_pf = self._observe_device_action(room)
            if observed_mode is None and self._devices_lack_hvac_action(room):
                # No hvac_action on any device — fall back to temp-vs-setpoint
                # inference for approximate training (better than skipping).
                # Don't infer for other None reasons (conflicts, unavailable).
                inferred = self._infer_device_mode(room)
                observed_mode = inferred
                observed_pf = 1.0 if inferred != MODE_IDLE else 0.0
            if observed_mode is not None and observed_mode != MODE_IDLE:
                _LOGGER.debug(
                    "Room '%s': device self-regulating (%s), using for training",
                    area_id,
                    observed_mode,
                )

        # For Managed Mode rooms, observe actual device state for display + training.
        # The controller's mode is "intent" (device told to heat), but the device
        # self-regulates and may be idle at setpoint.  See #69.
        managed_display_mode: str | None = None
        managed_display_pf = 0.0
        if climate_active and not has_external_sensor:
            obs_mode, obs_pf = self._observe_device_action(room)
            if obs_mode is not None:
                managed_display_mode = obs_mode
                managed_display_pf = obs_pf
            else:
                managed_display_mode = self._infer_device_mode(room)
                managed_display_pf = 1.0 if managed_display_mode != MODE_IDLE else 0.0

        # Determine mode for EKF training: use observed device state when
        # RoomMind doesn't directly control the device (see #36, #69).
        if climate_active:
            if has_external_sensor:
                # Full Control: controller's commanded mode is truth
                ekf_mode: str | None = mode
                ekf_pf = power_fraction
                # When heat source orchestration is active, adjust ekf_pf to
                # reflect the actual power delivered (not all devices may be
                # heating).  Use the mean of per-device power_fractions so the
                # EKF learns an accurate aggregated beta_h.
                if heat_source_plan is not None and heat_source_plan.commands:
                    ekf_pf = sum(c.power_fraction for c in heat_source_plan.commands) / len(heat_source_plan.commands)
            else:
                # Managed Mode: device self-regulates, use observed/inferred
                # state to avoid training "always heating" (#69).
                ekf_mode = managed_display_mode
                ekf_pf = managed_display_pf
        else:
            ekf_mode = observed_mode  # may be None → skip training
            ekf_pf = observed_pf

        # --- Observation-based corrections on the training mode (#150, #241) ---
        # Ghost-heating guard: in Full Control the controller's commanded mode
        # can diverge from what the device actually does.  Near target with
        # setpoint_mode="direct" a device's internal hysteresis can block
        # firing even while RoomMind commands heating/cooling.  Without this
        # guard the EKF receives a heating/cooling label for a period where no
        # energy actually entered the room, which drives alpha toward its
        # upper bound via cross-covariance with a negative innovation.
        # We only override to idle when all active devices unambiguously
        # report idle/off — heating/cooling observations keep the commanded
        # power_fraction so MPC throttling (e.g. pf=0.3) is preserved.
        q_residual_training = q_residual
        if climate_active and has_external_sensor and ekf_mode in (MODE_HEATING, MODE_COOLING):
            obs_mode, _ = self._observe_device_action(room)
            if obs_mode == MODE_IDLE:
                _LOGGER.debug(
                    "Room '%s': ghost-heating guard — commanded %s but devices idle, training as idle",
                    area_id,
                    ekf_mode,
                )
                ekf_mode = MODE_IDLE
                ekf_pf = 0.0
                q_residual_training = 0.0

        # Zero-power normalization: heat source orchestration may yield
        # mean(pf)=0 while the commanded mode is still heating/cooling.
        # Without this the predict step inflates Q_BETA_H through a zero
        # Jacobian (F[0][2]=pf=0) — variance grows without an observable
        # signal and destabilises the alpha↔beta coupling.  Downgrade to idle
        # for a consistent training batch.
        if ekf_mode in (MODE_HEATING, MODE_COOLING) and ekf_pf == 0.0:
            ekf_mode = MODE_IDLE
            q_residual_training = 0.0

        # Update thermal model with observation (EKF online learning).
        # The filter must NOT train with a degenerate outdoor fallback (e.g.
        # using room temp when the sensor is unavailable): F[0][1] collapses
        # toward 0, alpha drifts under process noise and eventually pegs at
        # the upper bound (see #301).  Skip the update — and flush any
        # accumulated batch — when no real outdoor source is available.
        # Same flush for a coil dry run in "dry" mode: the compressor is really
        # cooling, so training that window as idle would drift alpha (spec 9.3).
        learning_disabled = settings.get("learning_disabled_rooms", [])
        learning_active = area_id not in learning_disabled
        if (
            learning_active
            and not coil_dry_skip_training
            and current_temp_raw is not None
            and self.outdoor_temp_effective is not None
        ):
            can_heat, can_cool = get_can_heat_cool(room, acs_can_heat=check_acs_can_heat(self.hass, room))
            self._ekf_training.process(
                area_id=area_id,
                current_temp=current_temp_raw,
                T_outdoor=self.outdoor_temp_effective,
                ekf_mode=ekf_mode,
                ekf_pf=ekf_pf,
                window_open=window_open,
                raw_open=raw_open,
                q_residual=q_residual_training,
                shading_factor=shading_factor if shading_factor is not None else 0.0,
                q_solar=self._current_q_solar,
                can_heat=can_heat,
                can_cool=can_cool,
                dt_minutes=UPDATE_INTERVAL / 60.0,
                q_occupancy=q_occupancy,
            )
        else:
            self._ekf_training.clear(area_id)

        # Update mode-start tracking for min-run enforcement in the next cycle
        _prev_mode = self._previous_modes.get(area_id, MODE_IDLE)
        if mode != MODE_IDLE and _prev_mode != mode:
            self._mode_on_since[area_id] = time.time()
        elif mode == MODE_IDLE:
            self._mode_on_since.pop(area_id, None)
        self._previous_modes[area_id] = mode

        # Compute display mode: show actual device state when RoomMind doesn't
        # directly control the device, without affecting internal tracking
        # (residual heat, valve actuation, _previous_modes).  See #36, #69.
        if climate_active:
            if has_external_sensor:
                # Full Control: controller's mode is authoritative
                display_mode = mode
                display_pf = power_fraction
            else:
                # Managed Mode: show observed/inferred device state (#69)
                display_mode = managed_display_mode if managed_display_mode is not None else mode
                display_pf = managed_display_pf if managed_display_mode is not None else power_fraction
        else:
            if observed_mode is not None and observed_mode != MODE_IDLE:
                display_mode = observed_mode
                display_pf = observed_pf
            elif observed_mode is None:
                display_mode = self._infer_device_mode(room)
                display_pf = 1.0 if display_mode != MODE_IDLE else 0.0
            else:
                display_mode = MODE_IDLE
                display_pf = 0.0

        return display_mode, display_pf

    def _build_room_state_dict(
        self,
        *,
        area_id: str,
        room: dict,
        settings: dict,
        current_temp: float | None,
        current_temp_raw: float | None,
        current_humidity: float | None,
        target_temp: float | None,
        targets: TargetTemps,
        schedule_temp_warnings: list[dict[str, Any]],
        display_mode: str,
        display_pf: float,
        heat_source_plan: HeatSourcePlan | None,
        device_max_temp: float | None,
        ac_device_max_temp: float | None,
        device_min_temp: float | None,
        has_external_sensor: bool,
        window_open: bool,
        presence_away: bool,
        force_off: bool,
        mode: str,
        power_fraction: float,
        mold_risk_level: str | None,
        mold_surface_rh: float | None,
        mold_prevention_active_room: bool,
        mold_prevention_temp_delta: float,
        shading_factor: float | None,
        q_occupancy: float,
        cover_eids: list[str],
        cover_result: CoverResult,
        mpc_active: bool,
        compressor_protection_reason: str | None = None,
        coil_dry: CoilDryRoomResult | None = None,
    ) -> dict:
        """Build the final room state dictionary."""
        _room_devices = room.get("devices", [])
        _direct_eids = get_direct_setpoint_eids(_room_devices)
        # Directness is evaluated only over the devices actually driven in the
        # current mode: cooling only ever commands ACs (TRVs are turned off),
        # heating can command both. Including mode-irrelevant devices in the
        # check previously made a room with a direct AC + a proportional TRV
        # display the TRV's proportional boost value for the AC too, even
        # though async_apply() (see mpc_controller) already sends the correct
        # direct target to the AC. (#device_setpoint mixed-mode display bug)
        _mode_relevant_eids: set[str] = (
            set(get_ac_eids(_room_devices)) if mode == MODE_COOLING else set(get_all_entity_ids(_room_devices))
        )
        _all_direct = bool(_mode_relevant_eids) and _mode_relevant_eids <= _direct_eids

        return {
            "area_id": area_id,
            "current_temp": current_temp,
            "current_temp_raw": current_temp_raw,
            "current_humidity": current_humidity,
            "target_temp": target_temp,
            "heat_target": targets.heat,
            "cool_target": targets.cool,
            "schedule_temp_warnings": schedule_temp_warnings,
            "mode": display_mode,
            "commanded_mode": mode,
            "heating_power": round(display_pf * 100) if display_mode != MODE_IDLE else 0,
            "device_setpoint": self._compute_device_setpoint_orchestrated(
                heat_source_plan,
                current_temp,
                target_temp,
                device_max_temp,
                ac_device_max_temp,
                direct_eids=_direct_eids,
            )
            if heat_source_plan is not None
            else self._compute_device_setpoint(
                mode,
                power_fraction,
                current_temp,
                target_temp,
                has_external_sensor,
                device_max_temp=device_max_temp,
                device_min_temp=device_min_temp,
                has_thermostats=bool(get_trv_eids(_room_devices)),
                has_acs=bool(get_ac_eids(_room_devices)),
                all_direct=_all_direct,
            ),
            "window_open": window_open,
            **build_override_live(
                room,
                suppressed=is_override_suppressed(room, settings, presence_away),
            ),
            "active_schedule_index": self._get_active_schedule_index(room),
            "confidence": self._model_manager.get_confidence(area_id),
            "mpc_active": mpc_active,
            "presence_away": presence_away,
            "force_off": force_off,
            "mold_risk_level": mold_risk_level,
            "mold_surface_rh": (round(mold_surface_rh, 1) if mold_surface_rh is not None else None),
            "mold_prevention_active": mold_prevention_active_room,
            "mold_prevention_delta": mold_prevention_temp_delta,
            "shading_factor": shading_factor,
            "q_occupancy": q_occupancy,
            "n_observations": self._model_manager.get_n_observations(area_id),
            "blind_position": (self._cover_orchestrator.get_current_position(area_id) if cover_eids else None),
            "cover_auto_paused": (self._cover_orchestrator.is_user_override_active(area_id) if cover_eids else False),
            "cover_override_until": (self._cover_orchestrator.get_user_override_until(area_id) if cover_eids else None),
            "cover_reason": (cover_result.decision.reason if cover_eids else ""),
            "cover_forced_reason": (cover_result.forced_reason if cover_eids else ""),
            "active_cover_schedule_index": (cover_result.active_cover_schedule_index if cover_eids else -1),
            "active_heat_sources": self._heat_source_states.get(area_id),
            "compressor_protection_active": compressor_protection_reason is not None,
            "compressor_protection_reason": compressor_protection_reason,
            "coil_dry_active": bool(coil_dry and coil_dry.active),
            "coil_dry_phase": coil_dry.phase if coil_dry else None,
            "coil_dry_until": coil_dry.until if coil_dry else None,
            "coil_dry_entities": sorted(coil_dry.controlled_eids) if coil_dry else [],
        }

    @staticmethod
    def _compute_device_setpoint_orchestrated(
        heat_source_plan: HeatSourcePlan,
        current_temp: float | None,
        target_temp: float | None,
        device_max_temp: float | None,
        ac_device_max_temp: float | None,
        direct_eids: set[str] | None = None,
    ) -> float | None:
        """Compute device setpoint from the orchestrated heat source plan."""
        if current_temp is None or target_temp is None:
            return None
        # Find the most representative active command
        active_cmds = [c for c in heat_source_plan.commands if c.active]
        if not active_cmds:
            return None
        # Pick the first active command (primary preferred, then secondary)
        cmd = active_cmds[0]
        if direct_eids and cmd.entity_id in direct_eids:
            return target_temp
        if cmd.device_type == "thermostat":
            boost = device_max_temp if device_max_temp is not None else HEATING_BOOST_TARGET
        else:
            boost = ac_device_max_temp if ac_device_max_temp is not None else AC_HEATING_BOOST_TARGET
        sp = round(current_temp + cmd.power_fraction * (boost - current_temp), 1)
        sp = max(target_temp, sp)
        sp = min(boost, sp)
        return sp

    @staticmethod
    def _compute_device_setpoint(
        mode: str,
        power_fraction: float,
        current_temp: float | None,
        target_temp: float | None,
        has_external_sensor: bool,
        device_max_temp: float | None = None,
        device_min_temp: float | None = None,
        has_thermostats: bool = True,
        has_acs: bool = False,
        all_direct: bool = False,
    ) -> float | None:
        """Compute the device setpoint for UI display (Full Control only)."""
        if not has_external_sensor or current_temp is None or target_temp is None:
            return None
        if all_direct:
            return target_temp

        if mode == MODE_HEATING:
            default_boost = HEATING_BOOST_TARGET if has_thermostats else AC_HEATING_BOOST_TARGET
            boost = device_max_temp if device_max_temp is not None else default_boost
            if not has_thermostats and not has_acs:
                return None
            sp = round(current_temp + power_fraction * (boost - current_temp), 1)
            sp = max(target_temp, sp)
            sp = min(boost, sp)
            return sp

        if mode == MODE_COOLING and has_acs:
            boost = device_min_temp if device_min_temp is not None else AC_COOLING_BOOST_TARGET
            sp = round(current_temp - power_fraction * (current_temp - boost), 1)
            sp = max(boost, sp)
            sp = min(target_temp, sp)
            return sp

        return None

    def _read_device_temp(self, room: dict) -> float | None:
        """Read current_temperature from the first thermostat or AC entity."""
        for entity_id in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(entity_id)
            if state and state.attributes.get("current_temperature") is not None:
                try:
                    return float(state.attributes["current_temperature"])
                except (ValueError, TypeError):
                    continue
        return None

    def _observe_device_action(self, room: dict) -> tuple[str | None, float]:
        """Observe actual hvac_action from climate devices for model training.

        When climate control is disabled, devices may still self-regulate.
        This method reads the actual device state so the EKF receives
        correct mode information instead of blindly assuming idle.

        Returns (observed_mode, power_fraction):
          - ("heating", 1.0) / ("cooling", 1.0) / ("idle", 0.0) when conclusive
          - (None, 0.0) when state is unobservable (caller should skip training)
        """
        dominated: str | None = None

        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown"):
                continue

            # Device explicitly off → conclusively idle
            if state.state == "off":
                if dominated is None:
                    dominated = "idle"
                continue

            # Device in an active hvac_mode → need hvac_action to determine firing
            action = state.attributes.get("hvac_action")
            if action is None:
                # No hvac_action attribute → can't tell if firing → unobservable
                return (None, 0.0)

            if action in ("heating", "preheating"):
                if dominated == "cooling":
                    return (None, 0.0)  # conflicting → skip
                dominated = "heating"
            elif action == "cooling":
                if dominated == "heating":
                    return (None, 0.0)  # conflicting → skip
                dominated = "cooling"
            elif action in ("idle", "off"):
                if dominated is None:
                    dominated = "idle"
            else:
                # drying, fan, etc. — unknown thermal effect → skip
                return (None, 0.0)

        if dominated is None:
            return (None, 0.0)  # no devices or all unavailable

        pf = 1.0 if dominated in ("heating", "cooling") else 0.0
        return (dominated, pf)

    def _devices_lack_hvac_action(self, room: dict) -> bool:
        """Return True if at least one active device lacks hvac_action.

        Used to distinguish 'missing attribute' from other reasons
        _observe_device_action returns None (conflicts, unavailable, etc.).
        """
        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown", "off"):
                continue
            if state.attributes.get("hvac_action") is None:
                return True
        return False

    def _infer_device_mode(self, room: dict) -> str:
        """Infer heating/cooling from hvac_mode when hvac_action is unavailable.

        Compares current_temperature to the device setpoint to avoid showing
        'Heating' when the thermostat is in heat mode but already at target.
        Used for display and as a fallback for EKF training when hvac_action
        is missing (Managed Mode and learn-only mode).  See #69, #332.

        Handles single-setpoint modes ('heat', 'cool') and range modes
        ('heat_cool', 'auto') — the latter via ``target_temp_low`` /
        ``target_temp_high``.  A range mode without explicit low/high
        setpoints is ambiguous, so it stays idle (conservative, see #332).
        """
        for eid in get_all_entity_ids(room.get("devices", [])):
            state = self.hass.states.get(eid)
            if state is None or state.state in ("unavailable", "unknown", "off"):
                continue
            current = state.attributes.get("current_temperature")
            setpoint = state.attributes.get("temperature")
            if state.state == "heat":
                if current is not None and setpoint is not None and current >= setpoint:
                    continue  # at or above setpoint — not actively heating
                return MODE_HEATING
            if state.state == "cool":
                if current is not None and setpoint is not None and current <= setpoint:
                    continue  # at or below setpoint — not actively cooling
                return MODE_COOLING
            if state.state in ("heat_cool", "auto"):
                # Range mode: the device fires only once the room temp leaves
                # the [low, high] dead-band.  Above high → cooling, below low →
                # heating, within the band (or setpoints missing) → idle.
                low = state.attributes.get("target_temp_low")
                high = state.attributes.get("target_temp_high")
                if current is not None and high is not None and current > high:
                    return MODE_COOLING
                if current is not None and low is not None and current < low:
                    return MODE_HEATING
                continue
        return MODE_IDLE

    def _is_window_open(self, room: dict) -> bool:
        """Return True if any configured window/door sensor reports 'on' (open)."""
        for entity_id in room.get("window_sensors", []):
            state = self.hass.states.get(entity_id)
            if state and state.state == "on":
                return True
        return False

    def _is_presence_away(self, room: dict, settings: dict) -> bool:
        """Return True if presence detection says all relevant persons are away."""
        from .utils.presence_utils import is_presence_away

        return is_presence_away(self.hass, room, settings)  # all tracked persons are away

    def _get_active_schedule_index(self, room: dict) -> int:
        """Return the index of the active schedule in room['schedules'].

        Returns -1 if there are no schedules.
        """

        return resolve_schedule_index(self.hass, room)

    def _warn_block_temp_rejected(self, area_id: str, field: str, raw: Any) -> None:
        """Log a rejected schedule block temperature once per distinct value.

        The coordinator re-resolves every 30s, so an unthrottled warning would
        add ~2880 identical lines per day for a single typo.
        """
        key = (area_id, field, str(raw))
        if key in self._block_temp_warned:
            return
        self._block_temp_warned.add(key)
        _LOGGER.warning(
            "Room '%s': schedule block %s=%r is outside the plausible range "
            "%.1f-%.1f C and was ignored - falling back to the comfort target. "
            "Check the schedule helper for a typo.",
            area_id,
            field,
            raw,
            MIN_TARGET_TEMP,
            MAX_TARGET_TEMP,
        )

    def _resolve_target_temps(
        self,
        room: dict,
        settings: dict,
        schedule_blocks: dict | None = None,
        schedule_entity_id: str | None = None,
    ) -> TargetTemps:
        """Resolve dual heat/cool target temperatures.

        Priority: override > vacation > presence away > schedule block temp > comfort/eco.
        Returns TargetTemps(heat, cool). None values mean "force off".

        When ``presence_clears_override`` is enabled in settings and the room is
        currently presence-away (and not ``ignore_presence``), the override is
        held in the store but skipped here so the room follows the presence-away
        branch instead.
        """
        from .utils.schedule_utils import find_active_block, sanitize_block_temp

        # 1. Override — split heat/cool dead-band (suppressed when presence-away clears it)
        override_heat = room.get("override_heat")
        override_cool = room.get("override_cool")
        override_until = room.get("override_until")
        if override_heat is not None or override_cool is not None:
            if override_until is None or time.time() < override_until:
                presence_away_now = not room.get("ignore_presence", False) and self._is_presence_away(room, settings)
                if not (presence_away_now and bool(settings.get("presence_clears_override", False))):
                    return TargetTemps(heat=override_heat, cool=override_cool)
            else:
                # Timed override has expired — auto-clear
                area_id = room.get("area_id", "unknown")
                store = self.hass.data[DOMAIN]["store"]
                self.hass.async_create_task(
                    store.async_update_room(
                        area_id,
                        {
                            "override_heat": None,
                            "override_cool": None,
                            "override_until": None,
                            "override_type": None,
                        },
                    )
                )

        # 2. Vacation — heat setback, cooling stays at eco_cool
        vacation_until = settings.get("vacation_until")
        if vacation_until is not None:
            if time.time() < vacation_until:
                vacation_temp = settings.get("vacation_temp")
                if vacation_temp is not None:
                    t = float(vacation_temp)
                    eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)
                    return TargetTemps(heat=t, cool=max(t, eco_cool))
            else:
                self.hass.async_create_task(
                    self.hass.data[DOMAIN]["store"].async_save_settings(
                        {
                            "vacation_until": None,
                        }
                    )
                )

        # 2.5 Presence-based eco or off (skip if room ignores presence)
        if not room.get("ignore_presence", False) and self._is_presence_away(room, settings):
            if settings.get("presence_away_action", "eco") == "off":
                return TargetTemps(heat=None, cool=None)
            return TargetTemps(
                heat=room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT)),
                cool=room.get("eco_cool", DEFAULT_ECO_COOL),
            )

        # 3. Schedule / comfort / eco
        comfort_heat = room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT))
        comfort_cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL)
        eco_heat = room.get("eco_heat", room.get("eco_temp", DEFAULT_ECO_HEAT))
        eco_cool = room.get("eco_cool", DEFAULT_ECO_COOL)

        # schedule_entity_id is pre-resolved by the caller (_async_process_room) to avoid
        # a second resolve_schedule_index() call that could diverge if selector state changes.
        if not schedule_entity_id:
            return TargetTemps(heat=comfort_heat, cool=comfort_cool)

        state = self.hass.states.get(schedule_entity_id)
        state_unavailable = state is None or state.state in ("unavailable", "unknown")

        if state_unavailable:
            # #308 follow-up: when the schedule entity briefly flickers to
            # unavailable/unknown, derive on/off from cached blocks instead of
            # jumping to comfort_heat. Without cached blocks we have no signal,
            # so comfort_heat remains the last-resort fallback.
            if schedule_blocks is None:
                return TargetTemps(heat=comfort_heat, cool=comfort_cool)
            if find_active_block(schedule_blocks, time.time()) is None:
                if settings.get("schedule_off_action", "eco") == "off":
                    return TargetTemps(heat=None, cool=None)
                return TargetTemps(heat=eco_heat, cool=eco_cool)
            # Block is active right now: fall through to block resolution below.

        if state_unavailable or state.state == SCHEDULE_STATE_ON:
            if schedule_blocks is not None:
                # Read all temperature fields from block data.
                # HA does not expose custom data keys (heat_temperature, cool_temperature)
                # as entity state attributes, so schedule.get_schedule is required.
                block_data = find_active_block(schedule_blocks, time.time()) or {}
                heat_temp = block_data.get("heat_temperature")
                cool_temp = block_data.get("cool_temperature")
                block_temp = block_data.get("temperature")
            else:
                # Fallback when schedule.get_schedule is unavailable (non-schedule.* entity
                # or service failure). Works for temperature; heat/cool split will not resolve.
                heat_temp = state.attributes.get("heat_temperature")
                cool_temp = state.attributes.get("cool_temperature")
                block_temp = state.attributes.get("temperature")

            # Block values are unvalidated user YAML: a typo (110 instead of 11)
            # would be heated against for hours, so implausible values are
            # dropped in favour of the comfort fallback (#395).
            converter = partial(ha_temp_to_celsius, self.hass)
            area_id = room.get("area_id", "unknown")

            def _read(raw: Any, field: str) -> float | None:
                if raw is None:
                    return None
                val = sanitize_block_temp(raw, converter)
                if val is None:
                    self._warn_block_temp_rejected(area_id, field, raw)
                return val

            if heat_temp is not None or cool_temp is not None:
                h = _read(heat_temp, "heat_temperature")
                c = _read(cool_temp, "cool_temperature")
                return TargetTemps(
                    heat=h if h is not None else comfort_heat,
                    cool=c if c is not None else comfort_cool,
                )
            # Distinct name from the vacation branch's `t`, which is a plain float.
            single = _read(block_temp, "temperature")
            if single is not None:
                return TargetTemps(heat=single, cool=single)  # single-point
            return TargetTemps(heat=comfort_heat, cool=comfort_cool)

        # Schedule is "off" -> eco or off
        if settings.get("schedule_off_action", "eco") == "off":
            return TargetTemps(heat=None, cool=None)
        return TargetTemps(heat=eco_heat, cool=eco_cool)

    # ------------------------------------------------------------------
    # External comfort setpoint sources
    # ------------------------------------------------------------------

    async def _async_sync_comfort_sources(self, rooms: dict[str, dict]) -> None:
        """Mirror each room's comfort source entity into its comfort_heat.

        A room may point ``comfort_heat_entity`` at a climate entity (e.g. an
        Aqara W100 wall display) to use its dial as the comfort temperature.
        Rooms without one — and sources that are missing, unavailable or report
        an implausible setpoint — are left alone, so comfort stays exactly what
        the panel stored. *rooms* is updated in place so the rest of this cycle
        already runs on the new setpoint.
        """
        store = self.hass.data[DOMAIN]["store"]
        tracked: set[str] = set()

        for area_id, room in rooms.items():
            entity_id = get_comfort_entity(room)
            if not entity_id:
                continue
            tracked.add(entity_id)

            value = read_comfort_heat_setpoint(self.hass, room)
            if value is None:
                continue

            current = room.get("comfort_heat", room.get("comfort_temp", DEFAULT_COMFORT_HEAT))
            try:
                if abs(float(current) - value) < 0.05:
                    continue
            except (TypeError, ValueError):
                pass

            updates = {"comfort_heat": value, "comfort_temp": value}
            # Keep the dead-band intact: a dial turned above the cooling target
            # would otherwise ask for heating and cooling at the same time.
            cool = room.get("comfort_cool", DEFAULT_COMFORT_COOL)
            if isinstance(cool, (int, float)) and cool < value:
                updates["comfort_cool"] = value

            room.update(updates)
            try:
                await store.async_update_room(area_id, dict(updates))
            except KeyError:
                continue
            _LOGGER.debug(
                "Room '%s': comfort_heat set to %.1f°C from source '%s'",
                area_id,
                value,
                entity_id,
            )

        self._async_track_comfort_sources(tracked)

    @callback
    def _async_track_comfort_sources(self, entities: set[str]) -> None:
        """(Re)subscribe to the configured comfort source entities."""
        if entities == self._comfort_source_entities:
            return
        self._cancel_comfort_source_listener()
        if entities:
            self._comfort_source_unsub = async_track_state_change_event(
                self.hass,
                sorted(entities),
                self._async_comfort_source_changed,
            )
            self._comfort_source_entities = entities

    @callback
    def _cancel_comfort_source_listener(self) -> None:
        """Drop the comfort source subscription (also called on entry unload)."""
        if self._comfort_source_unsub is not None:
            self._comfort_source_unsub()
            self._comfort_source_unsub = None
        self._comfort_source_entities = set()

    @callback
    def _async_comfort_source_changed(self, event: Event[EventStateChangedData]) -> None:
        """Refresh as soon as a comfort dial is turned, instead of next cycle."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        if setpoint_signature(event.data.get("old_state")) == setpoint_signature(new_state):
            return
        self.hass.async_create_task(self.async_request_refresh())

    async def async_room_added(self, room: dict) -> None:
        """Create entity platform entities for a newly added/updated room and refresh data."""
        area_id = room["area_id"]
        has_covers = bool(room.get("covers"))

        if area_id not in self._entity_areas and hasattr(self, "async_add_entities") and self.async_add_entities:
            from .sensor import _create_room_entities

            entities = _create_room_entities(self, area_id)
            self.async_add_entities(entities)
            self._entity_areas.add(area_id)

        # Climate entities (override control): always create
        if (
            area_id not in self._climate_entity_areas
            and hasattr(self, "async_add_climate_entities")
            and self.async_add_climate_entities
        ):
            from .climate import _create_room_climates

            self.async_add_climate_entities(_create_room_climates(self, area_id))
            self._climate_entity_areas.add(area_id)

        if (
            area_id not in self._climate_control_switch_areas
            and hasattr(self, "async_add_switch_entities")
            and self.async_add_switch_entities
        ):
            from .switch import RoomMindClimateControlSwitch

            self.async_add_switch_entities([RoomMindClimateControlSwitch(self, area_id)])
            self._climate_control_switch_areas.add(area_id)

        # Cover entities: only create when covers are configured.
        # Not removed on save — cleanup_orphaned_entities() handles that at startup
        # so brief config changes don't break user automations.
        if has_covers:
            if (
                area_id not in self._switch_entity_areas
                and hasattr(self, "async_add_switch_entities")
                and self.async_add_switch_entities
            ):
                from .switch import _create_room_switches

                self.async_add_switch_entities(_create_room_switches(self, area_id))
                self._switch_entity_areas.add(area_id)
            if (
                area_id not in self._binary_sensor_entity_areas
                and hasattr(self, "async_add_binary_sensor_entities")
                and self.async_add_binary_sensor_entities
            ):
                from .binary_sensor import _create_room_binary_sensors

                self.async_add_binary_sensor_entities(_create_room_binary_sensors(self, area_id))
                self._binary_sensor_entity_areas.add(area_id)

        await self.async_request_refresh()

    async def async_room_removed(self, area_id: str) -> None:
        """Remove sensor entities for a deleted room and refresh data."""
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.hass)

        # Remove entities owned by exactly this area. Exact-match the known
        # per-room suffixes so a shorter area_id (e.g. `bedroom`) does not also
        # remove a longer room's entities (`bedroom_2_l_*`). (#340)
        owned_uids = {f"{DOMAIN}_{area_id}{suffix}" for suffix in ROOM_ENTITY_SUFFIXES}
        entries_to_remove = [
            entity_entry.entity_id
            for entity_entry in registry.entities.values()
            if isinstance(entity_entry.unique_id, str) and entity_entry.unique_id in owned_uids
        ]

        for entity_id in entries_to_remove:
            registry.async_remove(entity_id)

        # Clean up in-memory state
        self._window_manager.remove_room(area_id)
        self._previous_modes.pop(area_id, None)
        self._last_valid_temps.pop(area_id, None)
        self._had_valid_temp.discard(area_id)
        self._startup_guard_warned.discard(area_id)
        self._block_temp_warned = {k for k in self._block_temp_warned if k[0] != area_id}
        self._ekf_training.remove_room(area_id)
        self._pending_predictions.pop(area_id, None)
        self._residual_tracker.remove_room(area_id)
        self._cover_orchestrator.remove_room(area_id)
        self._entity_areas.discard(area_id)
        self._mode_on_since.pop(area_id, None)
        self._switch_entity_areas.discard(area_id)
        self._climate_control_switch_areas.discard(area_id)
        self._binary_sensor_entity_areas.discard(area_id)
        self._climate_entity_areas.discard(area_id)
        self._model_manager.remove_room(area_id)
        self._heat_source_states.pop(area_id, None)
        self._coil_dry_manager.remove_room(area_id)
        if self._history_store:
            await self.hass.async_add_executor_job(self._history_store.remove_room, area_id)

        await self.async_request_refresh()

    def cleanup_orphaned_entities(self) -> None:
        """Remove entities that no longer match any registered entity type.

        Called at startup to clean up entities from removed features.
        """
        from homeassistant.helpers import entity_registry as er

        store = self.hass.data[DOMAIN]["store"]
        rooms = store.get_rooms()
        registry = er.async_get(self.hass)

        # Global entities (not per-room) that should never be cleaned up
        global_uids = {f"{DOMAIN}_vacation"}

        to_remove: list[str] = []
        for entity_entry in registry.entities.values():
            uid = entity_entry.unique_id
            if not isinstance(uid, str) or not uid.startswith(f"{DOMAIN}_"):
                continue
            if uid in global_uids:
                continue

            # Match uid to an owning room via exact suffix matching (#340).
            parts = uid.removeprefix(f"{DOMAIN}_")
            match = _match_room_entity(parts, rooms)

            if match is None:
                # No room owns this uid — removed room or removed feature.
                to_remove.append(entity_entry.entity_id)
                continue

            area_id, suffix = match
            if suffix in COVER_ENTITY_SUFFIXES and not rooms[area_id].get("covers"):
                # Cover entity for a room without covers configured — orphaned.
                to_remove.append(entity_entry.entity_id)

        for eid in to_remove:
            _LOGGER.info("Removing orphaned entity: %s", eid)
            registry.async_remove(eid)

    # ------------------------------------------------------------------
    # Public thermal API
    # ------------------------------------------------------------------

    def reset_thermal_room(self, area_id: str) -> None:
        """Reset thermal model, EKF state, and residual tracking for one room."""
        self._model_manager.remove_room(area_id)
        self._ekf_training.last_temps.pop(area_id, None)
        self._residual_tracker.clear_room(area_id)

    def reset_thermal_all(self) -> list[str]:
        """Reset all thermal models. Returns list of affected room IDs."""
        room_ids = self._model_manager.get_room_ids()
        self._model_manager = RoomModelManager()
        self._ekf_training.set_model_manager(self._model_manager)
        self._cover_orchestrator.set_model_manager(self._model_manager)
        self._ekf_training.last_temps.clear()
        self._residual_tracker.clear_all()
        return room_ids

    def boost_learning(self, area_id: str) -> int:
        """Boost EKF covariance for a room. Returns n_observations."""
        return self._model_manager.boost_learning(area_id)

    def clear_cover_override(self, area_id: str) -> None:
        """Clear a user cover override so automatic cover control resumes."""
        self._cover_orchestrator.clear_user_override(area_id)

    @property
    def history_store(self) -> HistoryStore | None:
        """Access to history store for cleanup operations."""
        return self._history_store

    # ------------------------------------------------------------------
    # Master device control
    # ------------------------------------------------------------------

    def _collect_member_room_modes(
        self,
        members: list[str],
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
        settings: dict,
    ) -> list[str]:
        """Collect room modes for rooms containing group member devices.

        When heat-source orchestration is active for a room, the room is
        only counted if the orchestration decision includes this group's
        device types.  Prevents a boiler master from activating when only
        the AC (secondary) is heating, and vice versa. (#168)
        """
        if not settings.get("climate_control_active", True):
            return []
        member_set = set(members)
        modes: list[str] = []
        for area_id, room in rooms_config.items():
            if not room.get("climate_control_enabled", True):
                continue
            if room.get("is_outdoor", False):
                continue
            device_eids = {d.get("entity_id", "") for d in room.get("devices", [])}
            if not (device_eids & member_set):
                continue
            rs = room_states.get(area_id)
            if not rs:
                continue
            commanded = rs.get("commanded_mode", rs.get("mode", MODE_IDLE))

            # Orchestration filter (heating only): skip this room when its
            # active heat sources don't include this group's device types.
            if (
                commanded == MODE_HEATING
                and room.get("heat_source_orchestration", False)
                and not room_contributes_to_group(
                    room.get("devices", []),
                    member_set,
                    rs.get("active_heat_sources"),
                )
            ):
                continue

            modes.append(commanded)
        return modes

    def _waiting_for_first_reading(self, area_id: str) -> bool:
        """True while a room has had no valid reading since startup, within the grace period."""
        return area_id not in self._had_valid_temp and time.monotonic() - self._startup_ts < MAX_SENSOR_STALENESS

    def _any_member_room_waiting(self, members: list[str], rooms_config: dict[str, dict]) -> bool:
        """Return True when a Full Control member room has no temperature reading yet."""
        member_set = set(members)
        for area_id, room in rooms_config.items():
            if not room.get("temperature_sensor") or not self._waiting_for_first_reading(area_id):
                continue
            if room.get("is_outdoor", False) or not room.get("climate_control_enabled", True):
                continue
            device_eids = {d.get("entity_id", "") for d in room.get("devices", [])}
            if device_eids & member_set:
                return True
        return False

    def _resolve_master_hvac_mode(self, master_entity: str, action: str) -> str | None:
        """Map action to supported hvac_mode for master entity. Returns None if unsupported."""
        state = self.hass.states.get(master_entity)
        if state is None or state.state in ("unavailable", "unknown"):
            return None
        supported = state.attributes.get("hvac_modes", [])
        if action == "idle":
            if "off" in supported:
                return "off"
            _LOGGER.warning(
                "Master '%s': 'off' not supported, cannot turn idle (available: %s)",
                master_entity,
                supported,
            )
            return None
        if action in supported:
            return action
        if "heat_cool" in supported:
            return "heat_cool"
        if "auto" in supported:
            return "auto"
        _LOGGER.warning(
            "Master '%s': mode '%s' not supported (available: %s)",
            master_entity,
            action,
            supported,
        )
        return None

    async def _async_wake_member_zone(
        self,
        group: CompressorGroupConfig,
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
    ) -> None:
        """Pre-activate a member zone for ducted multi-zone systems.

        Ducted systems (e.g. AirTouch) require at least one active zone
        before the outdoor unit can start.  When all zones are off, set
        one to fan_only (always available) to enable outdoor unit startup.
        """
        for eid in group.members:
            state = self.hass.states.get(eid)
            if state is not None and state.state not in ("off", "unavailable", "unknown"):
                return

        member_set = set(group.members)
        wake_eid: str | None = None

        for area_id, room in rooms_config.items():
            if not room.get("climate_control_enabled", True):
                continue
            if room.get("is_outdoor", False):
                continue
            rs = room_states.get(area_id)
            if not rs:
                continue
            commanded = rs.get("commanded_mode", rs.get("mode", MODE_IDLE))
            if commanded == MODE_IDLE:
                continue
            for dev in room.get("devices", []):
                eid = dev.get("entity_id", "")
                if eid not in member_set:
                    continue
                zone_state = self.hass.states.get(eid)
                if zone_state and "fan_only" in (zone_state.attributes.get("hvac_modes") or []):
                    wake_eid = eid
                    break
            if wake_eid:
                break

        if not wake_eid:
            for eid in group.members:
                zone_state = self.hass.states.get(eid)
                if zone_state and "fan_only" in (zone_state.attributes.get("hvac_modes") or []):
                    wake_eid = eid
                    break

        if not wake_eid:
            _LOGGER.debug(
                "Group '%s': no zone supports fan_only for pre-activation",
                group.name,
            )
            return

        try:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": wake_eid, "hvac_mode": "fan_only"},
                blocking=True,
                context=make_roommind_context(),
            )
            self._compressor_manager.update_member(wake_eid, True)
            _LOGGER.debug(
                "Master '%s' (group '%s'): pre-activated zone '%s' (fan_only)",
                group.master_entity,
                group.name,
                wake_eid,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Master '%s' (group '%s'): failed to pre-activate zone '%s'",
                group.master_entity,
                group.name,
                wake_eid,
                exc_info=True,
            )

    async def _async_control_master_devices(
        self,
        room_states: dict[str, dict],
        rooms_config: dict[str, dict],
        settings: dict,
    ) -> None:
        """Control master devices based on aggregate demand from member rooms.

        Groups with master_entity get climate commands + optional script.
        Groups with only action_script (no master_entity) get script-only mode.
        """
        if not settings.get("climate_control_active", True):
            return
        for gid, group in self._compressor_manager.get_groups().items():
            if not group.master_entity and not group.action_script and not group.enforce_uniform_mode:
                continue
            try:
                has_master = bool(group.master_entity)

                # 1. Check master entity availability (only when configured)
                master_state = None
                if has_master:
                    master_state = self.hass.states.get(group.master_entity)
                    if master_state is None or master_state.state in (
                        "unavailable",
                        "unknown",
                    ):
                        _LOGGER.warning(
                            "Master '%s' (group '%s'): entity unavailable, skipping",
                            group.master_entity,
                            group.name,
                        )
                        continue

                # 2. Collect member room modes
                modes = self._collect_member_room_modes(
                    group.members,
                    room_states,
                    rooms_config,
                    settings,
                )

                # 3. Resolve desired action
                new_action = resolve_master_action(
                    modes,
                    group.conflict_resolution,
                    self.outdoor_temp_effective,
                    settings.get("outdoor_heating_max", DEFAULT_OUTDOOR_HEATING_MAX),
                )

                # Startup guard: don't idle the master while a member room is
                # still waiting for its first temperature reading.
                if new_action == "idle" and self._any_member_room_waiting(group.members, rooms_config):
                    continue

                # 4. Get previous state for transition detection
                state = self._compressor_manager.get_state(gid)
                prev_action = state.master_action if state else None

                # 5. Control master climate entity (when configured)
                if has_master:
                    # Min-run/min-off guard: prevent master short-cycling
                    if not self._compressor_manager.check_master_can_switch(gid, new_action):
                        continue

                    resolved_mode = self._resolve_master_hvac_mode(group.master_entity, new_action)

                    # Skip when mode is unsupported
                    if resolved_mode is None:
                        if new_action != "idle":
                            _LOGGER.warning(
                                "Master '%s' (group '%s'): cannot resolve mode for action '%s', skipping",
                                group.master_entity,
                                group.name,
                                new_action,
                            )
                        continue

                    # Redundancy check — compare resolved mode with actual entity state
                    if master_state is not None and master_state.state == resolved_mode:
                        self._compressor_manager.set_master_action(gid, new_action)
                        # Still call script if action changed
                        if new_action != prev_action and group.action_script:
                            await self._call_action_script(group, state, new_action)
                        continue

                    # Pre-activate a zone for ducted multi-zone systems where
                    # the outdoor unit requires at least one active zone (#135).
                    if new_action != "idle" and (prev_action is None or prev_action == "idle"):
                        await self._async_wake_member_zone(group, room_states, rooms_config)

                    # Send climate command
                    try:
                        await self.hass.services.async_call(
                            "climate",
                            "set_hvac_mode",
                            {
                                "entity_id": group.master_entity,
                                "hvac_mode": resolved_mode,
                            },
                            blocking=True,
                            context=make_roommind_context(),
                        )
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning(
                            "Master '%s' (group '%s'): failed to set hvac_mode '%s'",
                            group.master_entity,
                            group.name,
                            resolved_mode,
                            exc_info=True,
                        )
                        continue  # don't update state on failed command

                # 6. Call action script on transition
                if new_action != prev_action and group.action_script:
                    await self._call_action_script(group, state, new_action)

                # 7. Update state + log transition
                if new_action != prev_action:
                    label = group.master_entity or group.action_script or f"group:{group.id}"
                    _LOGGER.info(
                        "Master '%s' (group '%s'): %s -> %s",
                        label,
                        group.name,
                        prev_action,
                        new_action,
                    )
                self._compressor_manager.set_master_action(gid, new_action)

            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Master device control failed for group '%s'",
                    group.name,
                    exc_info=True,
                )

    async def _call_action_script(
        self,
        group: CompressorGroupConfig,
        state: CompressorGroupState | None,
        new_action: str,
    ) -> None:
        """Call the group's action script with transition variables."""
        script_state = self.hass.states.get(group.action_script)
        if script_state is None:
            _LOGGER.warning(
                "Master group '%s': action script '%s' not found",
                group.name,
                group.action_script,
            )
            return
        try:
            await self.hass.services.async_call(
                "script",
                "turn_on",
                {
                    "entity_id": group.action_script,
                    "variables": {
                        "action": new_action,
                        "master_entity": group.master_entity,
                        "members": group.members,
                        "active_members": [eid for eid in group.members if state and eid in state.active_members],
                    },
                },
                blocking=False,
                context=make_roommind_context(),
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Master group '%s': action script '%s' failed",
                group.name,
                group.action_script,
                exc_info=True,
            )
