"""MPC Optimizer using Dynamic Programming for RoomMind."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..const import HEATING_SYSTEM_PROFILES, MIN_POWER_FRACTION, MODE_COOLING, MODE_HEATING, MODE_IDLE
from .residual_heat import compute_residual_heat
from .thermal_model import RCModel

# Base lookahead for per-block decision cost evaluation. Radiator / unknown
# systems stay at this value; systems with long thermal time constants scale up.
LOOKAHEAD_BASE_BLOCKS = 6
# How many tau time-constants to include in the lookahead beyond min_run. Set to
# 1.0 so UFH lookahead (24 blocks = 120 min) matches the outer horizon minimum,
# avoiding silent clamping. See issue #131.
LOOKAHEAD_HORIZON_SCALE = 1.0
# Cooling pre-conditioning horizon. Cool-capable rooms extend the lookahead to at
# least this many blocks (18 = 90 min) so the optimizer can see an upcoming
# comfort-window setpoint drop and start cooling early to be at target when the
# window opens — the cooling analogue of UFH pre-heating. Bounded to cap steady-
# state AC aggressiveness. No afterglow synthesis is needed for cooling: unlike a
# UFH slab, an AC has negligible stored-emission afterglow, so the RC model's
# post-run warm-back (block_Q=0) already models decay correctly.
LOOKAHEAD_COOLING_BLOCKS = 18


@dataclass
class MPCPlan:
    """Result of MPC optimization — a planned sequence of actions."""

    actions: list[str]
    temperatures: list[float]  # len = len(actions) + 1 (includes initial)
    dt_minutes: float = 5.0
    power_fractions: list[float] = field(default_factory=list)
    # Per-system decision lookahead used when building this plan. Exposed so
    # the controller's safety guard can align its horizon with the optimizer's.
    lookahead_blocks: int = LOOKAHEAD_BASE_BLOCKS

    def get_current_action(self) -> str:
        """Return the action for the current (first) time block."""
        if not self.actions:
            return MODE_IDLE
        return self.actions[0]

    def get_current_power_fraction(self) -> float:
        """Power fraction for the current block. Backward-compatible."""
        if not self.power_fractions:
            return 1.0 if self.actions and self.actions[0] != MODE_IDLE else 0.0
        return self.power_fractions[0]


@dataclass
class MPCOptimizer:
    """Dynamic Programming optimizer for heating/cooling control.

    Plans the optimal on/off schedule over a prediction horizon
    to minimize a weighted sum of temperature deviation and energy use.
    """

    model: RCModel
    can_heat: bool = True
    can_cool: bool = True
    w_comfort: float = 10.0
    w_energy: float = 1.0
    min_run_blocks: int = 2  # minimum 2 blocks (10 min) per run
    outdoor_cooling_min: float = 16.0
    outdoor_heating_max: float = 22.0
    temp_min: float = 5.0  # frost protection
    temp_max: float = 30.0  # overheat protection
    override_active: bool = False
    heating_system_type: str = ""  # key into HEATING_SYSTEM_PROFILES; "" = unknown
    approach_rate: float = 1.0  # fraction of remaining gap closed per block; 1.0 = legacy deadbeat

    def __post_init__(self) -> None:
        # Set before optimize() runs so callers / patched optimize() still expose
        # a sensible default. optimize() refreshes these from dt_minutes per call.
        self._lookahead_blocks = LOOKAHEAD_BASE_BLOCKS
        # Heating-only horizon, kept separate from the combined lookahead so the
        # afterglow synthesis gate cannot be tripped by the cooling extension.
        self._heating_lookahead_blocks = LOOKAHEAD_BASE_BLOCKS

    def optimize(
        self,
        T_room: float,
        T_outdoor_series: list[float],
        heat_target_series: list[float],
        cool_target_series: list[float] | None = None,
        dt_minutes: float = 5.0,
        *,
        solar_series: list[float] | None = None,
        residual_series: list[float] | None = None,
        occupancy_series: list[float] | None = None,
    ) -> MPCPlan:
        """Find optimal action sequence over the planning horizon.

        Uses forward simulation with greedy optimization per block,
        considering minimum run time constraints.

        Accepts dual target series (heat_target_series + cool_target_series)
        for dead-band-aware optimization. If cool_target_series is None,
        it defaults to heat_target_series (single-target behavior).
        """
        if cool_target_series is None:
            cool_target_series = list(heat_target_series)

        # Clamp inverted targets: cool must be >= heat
        cool_target_series = [max(h, c) for h, c in zip(heat_target_series, cool_target_series, strict=False)]

        # Per-system decision lookahead, taken as the max of two needs:
        #  - slow heating systems (UFH, tau=90min) scale up so the cost function
        #    can value pre-heating via the synthesized post-heating afterglow;
        #    radiator / "" stay at LOOKAHEAD_BASE_BLOCKS.
        #  - cool-capable rooms extend to LOOKAHEAD_COOLING_BLOCKS so the cost
        #    function can see an upcoming comfort-window setpoint drop and pre-cool
        #    (the cooling analogue of pre-heating). Cooling needs no afterglow
        #    synthesis — the RC model already captures post-run warm-back.
        # Hybrid UFH+AC rooms get both (max): winter pre-heating is preserved and
        # summer pre-cooling is enabled.
        #
        # The heating horizon is tracked separately from the combined lookahead:
        # the afterglow synthesis gate keys off the heating horizon alone, so a
        # cooling-driven extension can never switch synthesis on for a system whose
        # own tau does not warrant it (e.g. radiator + AC).
        heating_lookahead = LOOKAHEAD_BASE_BLOCKS
        profile = HEATING_SYSTEM_PROFILES.get(self.heating_system_type) if self.heating_system_type else None
        if profile and dt_minutes > 0:
            tau_blocks = math.ceil(profile["tau_minutes"] / dt_minutes)
            heating_lookahead = max(
                heating_lookahead,
                self.min_run_blocks + math.ceil(LOOKAHEAD_HORIZON_SCALE * tau_blocks),
            )
        lookahead = heating_lookahead
        if self.can_cool and dt_minutes > 0:
            lookahead = max(lookahead, LOOKAHEAD_COOLING_BLOCKS)
        self._heating_lookahead_blocks = heating_lookahead
        self._lookahead_blocks = lookahead

        n_blocks = min(len(T_outdoor_series), len(heat_target_series), len(cool_target_series))
        if n_blocks == 0 or not math.isfinite(T_room):
            return MPCPlan(actions=[], temperatures=[T_room], dt_minutes=dt_minutes)

        q_solar = solar_series or [0.0] * n_blocks
        q_residual = residual_series or [0.0] * n_blocks
        q_occupancy = occupancy_series or [0.0] * n_blocks

        actions: list[str] = []
        temperatures: list[float] = [T_room]
        power_fractions: list[float] = []
        current_temp = T_room
        current_mode = MODE_IDLE
        blocks_in_mode = 0

        for i in range(n_blocks):
            T_out = T_outdoor_series[i]
            heat_tgt = heat_target_series[i]
            cool_tgt = cool_target_series[i]
            qs = q_solar[i] if i < len(q_solar) else 0.0
            qr = q_residual[i] if i < len(q_residual) else 0.0
            qo = q_occupancy[i] if i < len(q_occupancy) else 0.0

            # Determine available actions this block
            available = [MODE_IDLE]
            if self.can_heat and not self._is_outdoor_gated(MODE_HEATING, T_out):
                available.append(MODE_HEATING)
            if self.can_cool and not self._is_outdoor_gated(MODE_COOLING, T_out):
                available.append(MODE_COOLING)

            # If in a run and below min_run_blocks, must continue
            if current_mode != MODE_IDLE and blocks_in_mode < self.min_run_blocks:
                if current_mode in available:
                    best_action = current_mode
                else:
                    best_action = MODE_IDLE  # forced off by constraint
            else:
                # Evaluate each action: look ahead to find best
                best_action = MODE_IDLE
                best_cost = float("inf")
                future_solar = q_solar[i:] if q_solar else None
                future_residual = q_residual[i:] if q_residual else None
                future_occupancy = q_occupancy[i:] if q_occupancy else None
                for action in available:
                    cost = self._evaluate_action(
                        action,
                        current_temp,
                        T_out,
                        heat_tgt,
                        cool_tgt,
                        T_outdoor_series[i:],
                        heat_target_series[i:],
                        cool_target_series[i:],
                        dt_minutes,
                        future_solar=future_solar,
                        future_residual=future_residual,
                        future_occupancy=future_occupancy,
                    )
                    if cost < best_cost:
                        best_cost = cost
                        best_action = action

            # Compute proportional power fraction for this block
            # Use heat target for heating power, cool target for cooling power
            pf_target = heat_tgt if best_action == MODE_HEATING else cool_tgt
            pf, _ = self.compute_optimal_power(
                current_temp,
                T_out,
                pf_target,
                dt_minutes,
                q_solar=qs,
                q_residual=qr,
                q_occupancy=qo,
            )
            if best_action == MODE_IDLE:
                pf = 0.0
            elif best_action != MODE_IDLE and pf == 0.0:
                pf = 1.0  # min_run_blocks enforcement: keep full power

            # Apply action with proportional Q for accurate forward prediction
            if best_action == MODE_HEATING:
                Q = pf * self.model.Q_heat
            elif best_action == MODE_COOLING:
                Q = -(pf * self.model.Q_cool)
            else:
                Q = 0.0
            next_temp = self.model.predict(
                current_temp,
                T_out,
                Q,
                dt_minutes,
                q_solar=qs,
                q_residual=qr if Q == 0.0 else 0.0,
                q_occupancy=qo,
            )
            next_temp = max(self.temp_min, min(next_temp, self.temp_max))

            actions.append(best_action)
            temperatures.append(round(next_temp, 2))
            power_fractions.append(round(pf, 3))

            # Track run length
            if best_action == current_mode:
                blocks_in_mode += 1
            else:
                current_mode = best_action
                blocks_in_mode = 1

            current_temp = next_temp

        return MPCPlan(
            actions=actions,
            temperatures=temperatures,
            dt_minutes=dt_minutes,
            power_fractions=power_fractions,
            lookahead_blocks=self._lookahead_blocks,
        )

    def _evaluate_action(
        self,
        action: str,
        T_room: float,
        T_outdoor: float,
        heat_target: float,
        cool_target: float,
        future_T_outdoor: list[float],
        future_heat_targets: list[float],
        future_cool_targets: list[float],
        dt_minutes: float,
        *,
        future_solar: list[float] | None = None,
        future_residual: list[float] | None = None,
        future_occupancy: list[float] | None = None,
    ) -> float:
        """Evaluate the cost of taking an action, looking a few steps ahead.

        Per-system lookahead (self._lookahead_blocks) extends the window for
        slow heating systems and for cool-capable rooms. For UFH, the HEATING
        hypothesis synthesizes its own post-heating residual afterglow so the
        cost function values the sustained-comfort benefit of pre-heating.
        Cooling uses the extended lookahead to see an upcoming comfort-window
        setpoint drop and pre-cool, but needs no afterglow synthesis (an AC has
        negligible stored-emission afterglow; the RC model already captures
        post-run warm-back). Radiator / "" rooms with no cooling stay at
        LOOKAHEAD_BASE_BLOCKS with synthesis gated off — byte-identical to
        pre-fix behaviour.

        Synthesis is gated on self._heating_lookahead_blocks, not on the combined
        lookahead, so adding cooling to a room never enables synthesis on its
        heating hypothesis: a radiator + AC room keeps the same heating cost as an
        unprofiled room at the same horizon.
        """
        lookahead = min(self._lookahead_blocks, len(future_T_outdoor))
        Q = self._action_to_Q(action)
        total_cost = 0.0
        T = T_room
        solar = future_solar or []
        residual = future_residual or []
        occupancy = future_occupancy or []
        synthesis_enabled = (
            action == MODE_HEATING
            and self._heating_lookahead_blocks > LOOKAHEAD_BASE_BLOCKS
            and self.min_run_blocks > 0
            and bool(self.heating_system_type)
        )
        heating_duration_minutes = self.min_run_blocks * dt_minutes

        for j in range(lookahead):
            qs = solar[j] if j < len(solar) else 0.0
            qo = occupancy[j] if j < len(occupancy) else 0.0
            # Simulate HVAC for min_run_blocks (not just 1 block) to correctly
            # value sustained heating/cooling over the lookahead horizon.
            block_Q = Q if j < self.min_run_blocks else 0.0
            # Residual for the idle / post-run blocks. For the HEATING
            # hypothesis on a gated slow system, synthesize the afterglow this
            # hypothetical run would generate; otherwise fall back to the
            # controller-provided current-state decay.
            if synthesis_enabled and j >= self.min_run_blocks:
                elapsed = (j - self.min_run_blocks) * dt_minutes
                qr = compute_residual_heat(
                    elapsed,
                    self.heating_system_type,
                    last_power_fraction=1.0,
                    heating_duration_minutes=heating_duration_minutes,
                )
            else:
                qr = residual[j] if j < len(residual) else 0.0
            T = self.model.predict(
                T,
                future_T_outdoor[j],
                block_Q,
                dt_minutes,
                q_solar=qs,
                q_residual=qr if block_Q == 0.0 else 0.0,
                q_occupancy=qo,
            )
            # Clamp temperature in lookahead to prevent cost explosion
            # from implausible model predictions
            T = max(self.temp_min, min(self.temp_max, T))
            h_tgt = future_heat_targets[j] if j < len(future_heat_targets) else heat_target
            c_tgt = future_cool_targets[j] if j < len(future_cool_targets) else cool_target
            # Dead-band-aware comfort cost: zero inside the band
            if T < h_tgt:
                total_cost += self.w_comfort * (T - h_tgt) ** 2
            elif T > c_tgt:
                total_cost += self.w_comfort * (T - c_tgt) ** 2
            # else: inside dead band, no comfort cost
            # Energy cost: proportional to HVAC power for min_run blocks
            if j < self.min_run_blocks and action != MODE_IDLE:
                total_cost += self.w_energy * abs(Q) / 1000.0

        return total_cost

    def _action_to_Q(self, action: str) -> float:
        if action == MODE_HEATING:
            return self.model.Q_heat
        if action == MODE_COOLING:
            return -self.model.Q_cool
        return 0.0

    def _is_outdoor_gated(self, mode: str, T_outdoor: float) -> bool:
        if self.override_active:
            return False
        if mode == MODE_COOLING and T_outdoor < self.outdoor_cooling_min:
            return True
        if mode == MODE_HEATING and T_outdoor > self.outdoor_heating_max:
            return True
        return False

    def compute_optimal_power(
        self,
        T_room: float,
        T_outdoor: float,
        target: float,
        dt_minutes: float,
        *,
        q_solar: float = 0.0,
        q_residual: float = 0.0,
        q_occupancy: float = 0.0,
    ) -> tuple[float, str]:
        """Analytical closed-form optimal heating/cooling power.

        Returns (power_fraction in [0,1], mode).
        """
        if not math.isfinite(T_room) or not math.isfinite(target):
            return 0.0, MODE_IDLE

        dt_h = dt_minutes / 60.0
        alpha = self.model.U
        if alpha < 0.01:
            beta = alpha * dt_h  # Euler approx for tiny alpha
        else:
            beta = 1.0 - math.exp(-alpha * dt_h)

        if beta < 1e-9:
            return 0.0, MODE_IDLE

        # Drift temperature: where the room would go with no HVAC
        T_drift = T_room + beta * (T_outdoor - T_room)
        # Add predicted solar gain to drift
        if alpha > 0.01:
            T_drift += beta * self.model.Q_solar * q_solar / alpha
        else:
            T_drift += self.model.Q_solar * q_solar * dt_h
        # Add residual heat from thermal mass to drift
        if q_residual > 0:
            if alpha > 0.01:
                T_drift += beta * self.model.Q_heat * q_residual / alpha
            else:
                T_drift += self.model.Q_heat * q_residual * dt_h
        # Add predicted occupancy gain to drift
        if q_occupancy > 0:
            if alpha > 0.01:
                T_drift += beta * self.model.Q_occupancy * q_occupancy / alpha
            else:
                T_drift += self.model.Q_occupancy * q_occupancy * dt_h

        # Reference sub-target: close `approach_rate` of the remaining gap this block
        # instead of the whole gap (deadbeat). approach_rate=1.0 => T_ref == target =>
        # identical to the legacy formula. Smaller values widen the proportional band.
        # Holding power is preserved: at steady state (T_room == target) T_ref == target
        # for any approach_rate, so Q_required reduces to the holding power.
        T_ref = T_room + self.approach_rate * (target - T_room)
        Q_required = (T_ref - T_drift) * alpha / beta

        # Energy penalty: bias toward less power based on comfort/energy weights
        energy_bias = (self.w_energy / max(self.w_comfort, 0.01)) * alpha / beta * 0.1
        if Q_required > 0:
            Q_required = max(0.0, Q_required - energy_bias)
        elif Q_required < 0:
            Q_required = min(0.0, Q_required + energy_bias)

        if Q_required > 0 and self.can_heat and not self._is_outdoor_gated(MODE_HEATING, T_outdoor):
            frac = min(Q_required / max(self.model.Q_heat, 0.01), 1.0)
            return max(frac, MIN_POWER_FRACTION), MODE_HEATING
        elif Q_required < 0 and self.can_cool and not self._is_outdoor_gated(MODE_COOLING, T_outdoor):
            frac = min(abs(Q_required) / max(self.model.Q_cool, 0.01), 1.0)
            return max(frac, MIN_POWER_FRACTION), MODE_COOLING
        else:
            return 0.0, MODE_IDLE
