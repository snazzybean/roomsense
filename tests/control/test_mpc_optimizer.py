"""Tests for the MPC optimizer: MPCOptimizer, MPCPlan."""

from __future__ import annotations

import math

import pytest

from custom_components.roommind.const import MIN_POWER_FRACTION, MODE_COOLING, MODE_HEATING
from custom_components.roommind.control.mpc_optimizer import (
    LOOKAHEAD_BASE_BLOCKS,
    LOOKAHEAD_COOLING_BLOCKS,
    MPCOptimizer,
    MPCPlan,
)
from custom_components.roommind.control.thermal_model import RCModel


def test_optimizer_idle_at_target():
    """When at target with no heat loss, plan should be all idle."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    # Outdoor == room temp → no heat loss → idle is cheapest
    plan = opt.optimize(
        T_room=21.0,
        T_outdoor_series=[21.0] * 12,
        heat_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    assert plan.actions[0] == "idle"


def test_optimizer_heats_when_cold():
    """When below target, plan should start heating."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=17.0,
        T_outdoor_series=[5.0] * 24,
        heat_target_series=[21.0] * 24,
        dt_minutes=5,
    )
    assert plan.actions[0] == "heating"


def test_optimizer_cools_when_hot():
    """When above target, plan should start cooling."""
    # Use moderate Q_cool so one block doesn't overshoot wildly
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    opt = MPCOptimizer(model, can_heat=False, can_cool=True)
    plan = opt.optimize(
        T_room=27.0,
        T_outdoor_series=[30.0] * 24,
        heat_target_series=[23.0] * 24,
        dt_minutes=5,
    )
    assert plan.actions[0] == "cooling"


def test_optimizer_preheats():
    """Should start heating BEFORE target time to reach temp on time."""
    # Large thermal mass so pre-heating actually raises temperature that persists
    model = RCModel(C=200.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    # Target is eco (17°C) for first 6 blocks, then comfort (21°C)
    targets = [17.0] * 6 + [21.0] * 18
    plan = opt.optimize(
        T_room=17.0,
        T_outdoor_series=[17.0] * 24,
        heat_target_series=targets,
        dt_minutes=5,
    )
    # Should start heating before block 6 (pre-heating)
    first_heat = next(i for i, a in enumerate(plan.actions) if a == "heating")
    assert first_heat < 6  # starts before target changes


def test_optimizer_stops_near_target():
    """Should not overshoot far past target; proportional power keeps temp controlled."""
    # Realistic thermal mass: time constant = C/U = 200/50 = 4 hours
    model = RCModel(C=200.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=18.0,
        T_outdoor_series=[5.0] * 72,
        heat_target_series=[21.0] * 72,
        dt_minutes=5,
    )
    assert plan.actions[0] == "heating", "Should heat when below target"
    # With proportional control, power fraction decreases as temp approaches target
    # Final temperature should be near target, not wildly overshooting
    assert plan.temperatures[-1] < 22.0
    # Power fractions should decrease over time as room warms up
    assert plan.power_fractions[0] > plan.power_fractions[-1] or plan.actions[-1] == "idle"


def test_optimizer_min_run_time():
    """Should not create runs shorter than min_run_blocks."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model, min_run_blocks=2)
    plan = opt.optimize(
        T_room=20.5,
        T_outdoor_series=[5.0] * 12,
        heat_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    # Any heating run should be at least 2 blocks
    in_run = False
    run_length = 0
    for a in plan.actions:
        if a == "heating":
            run_length += 1
            in_run = True
        elif in_run:
            assert run_length >= 2
            run_length = 0
            in_run = False


def test_optimizer_outdoor_gating():
    """Cooling blocked when outdoor below threshold."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model, can_heat=False, can_cool=True, outdoor_cooling_min=16.0)
    plan = opt.optimize(
        T_room=25.0,
        T_outdoor_series=[10.0] * 12,  # below 16°C
        heat_target_series=[22.0] * 12,
        dt_minutes=5,
    )
    assert all(a == "idle" for a in plan.actions)


def test_optimizer_outdoor_gating_bypassed_by_override():
    """Cooling allowed when outdoor below threshold but override active."""
    model = RCModel(C=1.0, U=0.1, Q_heat=5.0, Q_cool=5.0)
    opt = MPCOptimizer(model, can_heat=False, can_cool=True, outdoor_cooling_min=16.0, override_active=True)
    plan = opt.optimize(
        T_room=25.0,
        T_outdoor_series=[10.0] * 12,
        heat_target_series=[22.0] * 12,
        dt_minutes=5,
    )
    assert any(a == "cooling" for a in plan.actions)


def test_plan_get_current_action():
    """MPCPlan returns correct action for current time."""
    plan = MPCPlan(
        actions=["heating", "heating", "idle", "idle"],
        temperatures=[18.0, 19.0, 20.5, 21.0, 21.0],
        dt_minutes=5,
    )
    assert plan.get_current_action() == "heating"


def test_plan_empty():
    """Empty plan returns idle."""
    plan = MPCPlan(actions=[], temperatures=[20.0], dt_minutes=5)
    assert plan.get_current_action() == "idle"


# ---------------------------------------------------------------------------
# Proportional control tests
# ---------------------------------------------------------------------------


def test_compute_optimal_power_cold_room():
    """Cold room needs high power fraction."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(
        T_room=15.0,
        T_outdoor=5.0,
        target=21.0,
        dt_minutes=5.0,
    )
    assert mode == "heating"
    assert pf >= 0.5  # large error → high power


def test_compute_optimal_power_at_target():
    """At target with matching outdoor → near-zero or idle."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(
        T_room=21.0,
        T_outdoor=21.0,
        target=21.0,
        dt_minutes=5.0,
    )
    assert mode == "idle"
    assert pf == 0.0


def test_power_fractions_in_plan():
    """Optimize returns power_fractions list matching actions length."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=17.0,
        T_outdoor_series=[5.0] * 12,
        heat_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    assert len(plan.power_fractions) == len(plan.actions)
    # Heating blocks should have positive power fractions
    for i, a in enumerate(plan.actions):
        if a == "heating":
            assert plan.power_fractions[i] > 0.0
        elif a == "idle":
            assert plan.power_fractions[i] == 0.0


def test_get_current_power_fraction_fallback():
    """Empty power_fractions → backward-compatible defaults."""
    plan_heat = MPCPlan(actions=["heating", "idle"], temperatures=[18.0, 19.0, 19.5], dt_minutes=5)
    assert plan_heat.get_current_power_fraction() == 1.0  # active → 1.0

    plan_idle = MPCPlan(actions=["idle"], temperatures=[21.0, 21.0], dt_minutes=5)
    assert plan_idle.get_current_power_fraction() == 0.0  # idle → 0.0

    plan_empty = MPCPlan(actions=[], temperatures=[21.0], dt_minutes=5)
    assert plan_empty.get_current_power_fraction() == 0.0  # no actions → 0.0


def test_power_fraction_clamped():
    """Power fraction should always be in [MIN_POWER_FRACTION, 1.0] when active."""
    from custom_components.roommind.const import MIN_POWER_FRACTION

    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=17.0,
        T_outdoor_series=[5.0] * 12,
        heat_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    for i, a in enumerate(plan.actions):
        pf = plan.power_fractions[i]
        if a != "idle":
            assert pf >= MIN_POWER_FRACTION
            assert pf <= 1.0


# ---------------------------------------------------------------------------
# Residual heat tests
# ---------------------------------------------------------------------------


def test_optimizer_residual_reduces_heating():
    """With residual heat, optimizer should heat less or stop sooner."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    n = 12
    # Without residual
    plan_no_res = opt.optimize(
        T_room=19.5,
        T_outdoor_series=[10.0] * n,
        heat_target_series=[21.0] * n,
        dt_minutes=5,
    )
    # With significant residual heat
    plan_res = opt.optimize(
        T_room=19.5,
        T_outdoor_series=[10.0] * n,
        heat_target_series=[21.0] * n,
        dt_minutes=5,
        residual_series=[0.5, 0.4, 0.3, 0.2, 0.15, 0.1, 0.05, 0.0, 0.0, 0.0, 0.0, 0.0],
    )
    # With residual, temperatures during idle blocks should be higher
    heating_blocks_no = sum(1 for a in plan_no_res.actions if a == "heating")
    heating_blocks_res = sum(1 for a in plan_res.actions if a == "heating")
    # Residual heat should reduce need for active heating
    assert heating_blocks_res <= heating_blocks_no


def test_compute_optimal_power_with_residual():
    """Residual heat in drift should reduce required heating power."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf_no_res, mode_no_res = opt.compute_optimal_power(
        19.5,
        10.0,
        21.0,
        5.0,
        q_residual=0.0,
    )
    pf_res, mode_res = opt.compute_optimal_power(
        19.5,
        10.0,
        21.0,
        5.0,
        q_residual=0.5,
    )
    # With residual, either lower power or idle
    if mode_res == "heating":
        assert pf_res <= pf_no_res
    else:
        assert mode_res == "idle"  # residual is enough


def test_optimizer_min_run_blocks_from_profile():
    """min_run_blocks is respected in the plan."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model, min_run_blocks=6)  # underfloor-like
    plan = opt.optimize(
        T_room=17.0,
        T_outdoor_series=[5.0] * 12,
        heat_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    # If heating starts, it should run for at least 6 blocks
    if plan.actions[0] == "heating":
        heating_count = 0
        for a in plan.actions:
            if a == "heating":
                heating_count += 1
            else:
                break
        assert heating_count >= 6


# ---------------------------------------------------------------------------
# Dead-band tests (split heat/cool targets)
# ---------------------------------------------------------------------------


def test_optimizer_deadband_idle():
    """Inside the dead band (between heat and cool targets) should be idle."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=22.5,
        T_outdoor_series=[22.5] * 12,
        heat_target_series=[21.0] * 12,
        cool_target_series=[24.0] * 12,
        dt_minutes=5,
    )
    assert plan.actions[0] == "idle"


def test_optimizer_heats_below_heat_target():
    """Below heat target with dead band should heat."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=19.0,
        T_outdoor_series=[5.0] * 12,
        heat_target_series=[21.0] * 12,
        cool_target_series=[24.0] * 12,
        dt_minutes=5,
    )
    assert plan.actions[0] == "heating"


def test_optimizer_cools_above_cool_target():
    """Above cool target with dead band should cool."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    opt = MPCOptimizer(model, can_heat=False, can_cool=True)
    plan = opt.optimize(
        T_room=25.0,
        T_outdoor_series=[30.0] * 12,
        heat_target_series=[21.0] * 12,
        cool_target_series=[24.0] * 12,
        dt_minutes=5,
    )
    assert plan.actions[0] == "cooling"


def test_optimizer_inverted_targets_clamped():
    """Inverted heat > cool targets should be clamped to equal."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    # heat=25 > cool=21 is inverted; after clamping cool becomes 25
    # Room at 23 < 25 → should heat
    plan = opt.optimize(
        T_room=23.0,
        T_outdoor_series=[20.0] * 12,
        heat_target_series=[25.0] * 12,
        cool_target_series=[21.0] * 12,
        dt_minutes=5,
    )
    assert plan.actions[0] == "heating"


# ---------------------------------------------------------------------------
# Coverage: uncovered lines
# ---------------------------------------------------------------------------


def test_optimize_empty_series_returns_empty_plan():
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=21.0,
        T_outdoor_series=[],
        heat_target_series=[],
        dt_minutes=5,
    )
    assert plan.actions == []
    assert plan.temperatures == [21.0]


def test_optimize_nan_t_room_returns_empty_plan():
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    plan = opt.optimize(
        T_room=float("nan"),
        T_outdoor_series=[10.0] * 6,
        heat_target_series=[21.0] * 6,
        dt_minutes=5,
    )
    assert plan.actions == []


def test_min_run_forced_idle_when_mode_gated():
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    opt = MPCOptimizer(model, can_heat=True, can_cool=True, min_run_blocks=4, outdoor_cooling_min=20.0)
    outdoor = [25.0] * 2 + [10.0] * 10
    targets_heat = [30.0] * 12
    targets_cool = [18.0] * 12
    plan = opt.optimize(
        T_room=27.0,
        T_outdoor_series=outdoor,
        heat_target_series=targets_heat,
        cool_target_series=targets_cool,
        dt_minutes=5,
    )
    cooling_started = False
    for i, a in enumerate(plan.actions):
        if a == "cooling":
            cooling_started = True
        if cooling_started and a == "idle" and i < 4:
            break
    assert len(plan.actions) == 12


def test_compute_optimal_power_nan_inputs():
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(float("nan"), 10.0, 21.0, 5.0)
    assert pf == 0.0
    assert mode == "idle"
    pf2, mode2 = opt.compute_optimal_power(21.0, 10.0, float("inf"), 5.0)
    assert pf2 == 0.0
    assert mode2 == "idle"


def test_compute_optimal_power_tiny_alpha():
    model = RCModel(C=2.0, U=0.005, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0)
    assert mode in ("heating", "idle")


def test_compute_optimal_power_zero_alpha():
    model = RCModel(C=2.0, U=0.0, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0)
    assert pf == 0.0
    assert mode == "idle"


def test_compute_optimal_power_solar_with_tiny_alpha():
    model = RCModel(C=2.0, U=0.005, Q_heat=1000.0, Q_cool=1500.0, Q_solar=100.0)
    opt = MPCOptimizer(model)
    pf, mode = opt.compute_optimal_power(20.0, 10.0, 21.0, 5.0, q_solar=0.8)
    assert mode in ("heating", "idle")


def test_compute_optimal_power_residual_with_tiny_alpha():
    model = RCModel(C=2.0, U=0.005, Q_heat=1000.0, Q_cool=1500.0)
    opt = MPCOptimizer(model)
    pf_no, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_residual=0.0)
    pf_res, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_residual=0.5)
    assert pf_res <= pf_no or pf_res == pf_no


def test_compute_optimal_power_occupancy_with_tiny_alpha():
    model = RCModel(C=2.0, U=0.005, Q_heat=1000.0, Q_cool=1500.0, Q_occupancy=200.0)
    opt = MPCOptimizer(model)
    pf_no, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_occupancy=0.0)
    pf_occ, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_occupancy=1.0)
    assert pf_occ <= pf_no or pf_occ == pf_no


def test_compute_optimal_power_occupancy_with_normal_alpha():
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0, Q_occupancy=200.0)
    opt = MPCOptimizer(model)
    pf_no, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_occupancy=0.0)
    pf_occ, _ = opt.compute_optimal_power(19.0, 10.0, 21.0, 5.0, q_occupancy=1.0)
    assert pf_occ <= pf_no


def test_get_current_power_fraction_with_fractions():
    plan = MPCPlan(
        actions=["heating", "idle"],
        temperatures=[18.0, 19.0, 19.5],
        dt_minutes=5,
        power_fractions=[0.75, 0.0],
    )
    assert plan.get_current_power_fraction() == 0.75


def test_min_run_forced_idle_outdoor_gate_mid_run():
    model = RCModel(C=200.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    opt = MPCOptimizer(
        model,
        can_heat=False,
        can_cool=True,
        min_run_blocks=6,
        outdoor_cooling_min=20.0,
    )
    outdoor = [25.0] + [10.0] * 11
    plan = opt.optimize(
        T_room=28.0,
        T_outdoor_series=outdoor,
        heat_target_series=[22.0] * 12,
        cool_target_series=[22.0] * 12,
        dt_minutes=5,
    )
    if len(plan.actions) > 1 and plan.actions[0] == "cooling":
        assert plan.actions[1] == "idle"


# ---------------------------------------------------------------------------
# Issue #131: UFH proactive pre-heating
# ---------------------------------------------------------------------------


def _ufh_optimizer(**overrides):
    """Construct an optimizer with UFH heat-only profile settings.

    Default can_cool=False matches pure UFH rooms (primary fix target).
    Override can_cool=True to exercise the hybrid UFH+AC code path.
    """
    model = overrides.pop("model", RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0))
    defaults = {
        "can_cool": False,
        "w_comfort": 7.0,
        "w_energy": 3.0,
        "min_run_blocks": 6,
        "heating_system_type": "underfloor",
    }
    defaults.update(overrides)
    return MPCOptimizer(model=model, **defaults)


def test_ufh_proactive_preheat_at_target():
    """UFH room at target with cold outdoor should pre-heat, not wait for drop."""
    opt = _ufh_optimizer()
    n = 30
    plan = opt.optimize(
        T_room=20.0,
        T_outdoor_series=[5.0] * n,
        heat_target_series=[20.0] * n,
        cool_target_series=[20.0] * n,
        dt_minutes=5,
    )
    assert plan.actions[0] == "heating", f"UFH at target should pre-heat, got {plan.actions[0]}"


def test_ufh_afterglow_visible_in_cost():
    """Synthesis reduces HEATING cost vs non-synthesized path at equal lookahead.

    Isolates the synthesis effect by forcing both optimizers to the same
    lookahead. Only the heating_system_type-based synthesis gate differs.
    """
    model = RCModel(C=200.0, U=50.0, Q_heat=300.0, Q_cool=1500.0)
    shared = {
        "T_room": 20.0,
        "T_outdoor": 5.0,
        "heat_target": 20.0,
        "cool_target": 20.0,
        "future_T_outdoor": [5.0] * 30,
        "future_heat_targets": [20.0] * 30,
        "future_cool_targets": [20.0] * 30,
        "dt_minutes": 5.0,
    }

    opt_ufh = MPCOptimizer(
        model=model,
        can_cool=False,
        w_comfort=7.0,
        w_energy=3.0,
        min_run_blocks=6,
        heating_system_type="underfloor",
    )
    opt_empty = MPCOptimizer(
        model=model,
        can_cool=False,
        w_comfort=7.0,
        w_energy=3.0,
        min_run_blocks=6,
        heating_system_type="",
    )
    # Force equal lookahead — isolate synthesis from horizon-size effect. The
    # heating horizon must be set too: the synthesis gate keys off it, not off the
    # combined lookahead.
    opt_ufh._lookahead_blocks = 24
    opt_empty._lookahead_blocks = 24
    opt_ufh._heating_lookahead_blocks = 24
    opt_empty._heating_lookahead_blocks = 24

    cost_ufh = opt_ufh._evaluate_action("heating", **shared)
    cost_empty = opt_empty._evaluate_action("heating", **shared)

    assert cost_ufh < cost_empty, (
        f"UFH heating cost {cost_ufh} should be lower than empty-type {cost_empty} "
        "due to afterglow synthesis reducing post-run comfort penalty"
    )


def test_radiator_plan_matches_empty_plan():
    """Radiator and empty-type plans must be byte-identical (no radiator regression)."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    kwargs = {
        "T_room": 19.5,
        "T_outdoor_series": [5.0] * 12,
        "heat_target_series": [21.0] * 12,
        "dt_minutes": 5,
    }
    plan_rad = MPCOptimizer(model=model, heating_system_type="radiator").optimize(**kwargs)
    plan_empty = MPCOptimizer(model=model, heating_system_type="").optimize(**kwargs)
    assert plan_rad.actions == plan_empty.actions
    assert plan_rad.temperatures == plan_empty.temperatures
    assert plan_rad.power_fractions == plan_empty.power_fractions


def test_unknown_system_no_regression():
    """Empty heating_system_type contributes no tau extension of its own.

    A pure-heating room (can_cool=False) therefore keeps the base lookahead and
    synthesis off — byte-identical to pre-fix behaviour. A cool-capable room with
    no heating profile still extends to LOOKAHEAD_COOLING_BLOCKS: the lookahead is
    the max of the heating tau horizon and the cooling horizon.
    """
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    kwargs = {
        "T_room": 19.5,
        "T_outdoor_series": [5.0] * 12,
        "heat_target_series": [21.0] * 12,
        "dt_minutes": 5,
    }
    plan_heat_only = MPCOptimizer(model=model, heating_system_type="", can_cool=False).optimize(**kwargs)
    assert plan_heat_only.lookahead_blocks == LOOKAHEAD_BASE_BLOCKS
    plan_cool_capable = MPCOptimizer(model=model, heating_system_type="", can_cool=True).optimize(**kwargs)
    assert plan_cool_capable.lookahead_blocks == LOOKAHEAD_COOLING_BLOCKS


def test_cooling_lookahead_no_afterglow_synthesis():
    """COOLING uses the provided future_residual; never synthesizes afterglow.

    Exercised on an extended-lookahead UFH setup (the UFH tau horizon supplies
    the extension here; a cool-capable room would extend anyway). With
    lookahead=24 and min_run=6, the 18 post-run blocks see block_Q=0 and let
    residual through. Proves residual drives COOLING cost, not synthesis.
    """
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    opt = _ufh_optimizer(model=model)  # can_cool=False by helper default
    opt.optimize(
        T_room=25.0,
        T_outdoor_series=[28.0] * 30,
        heat_target_series=[22.0] * 30,
        cool_target_series=[22.0] * 30,
        dt_minutes=5,
    )
    shared = {
        "T_room": 25.0,
        "T_outdoor": 28.0,
        "heat_target": 22.0,
        "cool_target": 22.0,
        "future_T_outdoor": [28.0] * 30,
        "future_heat_targets": [22.0] * 30,
        "future_cool_targets": [22.0] * 30,
        "dt_minutes": 5.0,
    }
    cost_with_residual = opt._evaluate_action("cooling", **shared, future_residual=[0.5] * 30)
    cost_without_residual = opt._evaluate_action("cooling", **shared, future_residual=[0.0] * 30)
    assert cost_with_residual != cost_without_residual


def test_lookahead_clamps_to_horizon():
    """Short outer horizon clamps lookahead without errors."""
    opt = _ufh_optimizer()
    plan = opt.optimize(
        T_room=20.0,
        T_outdoor_series=[5.0] * 8,
        heat_target_series=[20.0] * 8,
        cool_target_series=[20.0] * 8,
        dt_minutes=5,
    )
    assert len(plan.actions) == 8  # no crash, plan produced


def test_lookahead_blocks_attribute_exposed():
    """Plan exposes lookahead_blocks for guard integration; covers all setups."""
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=1500.0)
    # Expected lookahead = max(base 6, heating tau horizon, cooling horizon 18).
    # Heating tau horizon = min_run_blocks + ceil(tau_minutes / dt_minutes), only
    # for a known profile: radiator tau=10min -> 2+2=4 (below base), underfloor
    # tau=90min -> 6+18=24. The cooling horizon applies whenever can_cool.
    cases = [
        # (heating_system_type, can_cool, min_run_blocks, expected_lookahead)
        ("", False, 2, 6),  # no profile, no cooling -> base
        ("", True, 2, 18),  # cooling horizon alone extends it
        ("radiator", False, 2, 6),  # tau horizon 4 < base
        ("radiator", True, 2, 18),  # cooling horizon dominates
        ("underfloor", False, 6, 24),  # UFH tau horizon
        ("underfloor", True, 6, 24),  # hybrid UFH+AC: max(24, 18) = 24
    ]
    for hst, can_cool, mrb, exp_blocks in cases:
        opt = MPCOptimizer(
            model=model,
            can_cool=can_cool,
            min_run_blocks=mrb,
            heating_system_type=hst,
        )
        plan = opt.optimize(
            T_room=20.0,
            T_outdoor_series=[10.0] * 30,
            heat_target_series=[20.0] * 30,
            dt_minutes=5,
        )
        assert plan.lookahead_blocks == exp_blocks, (
            f"hst={hst!r} can_cool={can_cool}: expected {exp_blocks}, got {plan.lookahead_blocks}"
        )


def test_hybrid_ufh_ac_gets_max_of_both_horizons():
    """Hybrid UFH+AC rooms take the max of the heating tau and cooling horizons.

    Pre-cooling used to be sacrificed to protect UFH pre-heating: cool-capable
    rooms were pinned to the base lookahead. The lookahead is now the max of both
    needs, so a hybrid room keeps the full UFH horizon (winter pre-heating) while
    also clearing the cooling horizon (summer pre-cooling).
    """
    model = RCModel(C=2.0, U=50.0, Q_heat=1000.0, Q_cool=200.0)
    kwargs = {
        "T_room": 25.0,
        "T_outdoor_series": [28.0] * 24,
        "heat_target_series": [21.0] * 24,
        "cool_target_series": [23.0] * 24,
        "dt_minutes": 5,
    }
    hybrid_kwargs = {
        "model": model,
        "can_heat": True,
        "min_run_blocks": 6,
        "heating_system_type": "underfloor",
    }
    plan_hybrid = MPCOptimizer(**hybrid_kwargs, can_cool=True).optimize(**kwargs)
    # Pure UFH reference: the heating tau horizon on its own.
    plan_ufh_heat_only = MPCOptimizer(**hybrid_kwargs, can_cool=False).optimize(**kwargs)
    # Cool-capable room with no heating profile: the cooling horizon on its own.
    plan_cool_only = MPCOptimizer(
        model=model,
        can_heat=True,
        can_cool=True,
        min_run_blocks=6,
        heating_system_type="",
    ).optimize(**kwargs)

    assert plan_ufh_heat_only.lookahead_blocks == 24
    assert plan_cool_only.lookahead_blocks == LOOKAHEAD_COOLING_BLOCKS
    # max(24, 18) = 24: UFH pre-heating horizon preserved, cooling horizon cleared.
    assert plan_hybrid.lookahead_blocks == max(plan_ufh_heat_only.lookahead_blocks, LOOKAHEAD_COOLING_BLOCKS)
    assert plan_hybrid.lookahead_blocks >= LOOKAHEAD_COOLING_BLOCKS
    # The hybrid room is no longer pinned to the base lookahead — that pinning is
    # exactly what kept the AC from seeing an upcoming comfort window.
    assert plan_hybrid.lookahead_blocks > LOOKAHEAD_BASE_BLOCKS


def test_cooling_extension_does_not_enable_heating_synthesis():
    """The cooling horizon must never switch afterglow synthesis on.

    Synthesis is gated on the heating horizon alone, not on the combined lookahead.
    A radiator room (tau=10min, so its own horizon stays below base) that gains an
    AC gets a longer combined lookahead, but its HEATING hypothesis must still cost
    exactly what an unprofiled room costs at the same horizon. Only a system whose
    own tau warrants synthesis (UFH) stays eligible for it.
    """
    model = RCModel(C=200.0, U=50.0, Q_heat=300.0, Q_cool=1500.0)
    plan_kwargs = {
        "T_room": 20.0,
        "T_outdoor_series": [5.0] * 30,
        "heat_target_series": [20.0] * 30,
        "cool_target_series": [20.0] * 30,
        "dt_minutes": 5,
    }
    cost_kwargs = {
        "T_room": 20.0,
        "T_outdoor": 5.0,
        "heat_target": 20.0,
        "cool_target": 20.0,
        "future_T_outdoor": [5.0] * 30,
        "future_heat_targets": [20.0] * 30,
        "future_cool_targets": [20.0] * 30,
        "dt_minutes": 5.0,
    }

    def cool_capable(heating_system_type):
        opt = MPCOptimizer(
            model=model,
            can_heat=True,
            can_cool=True,
            min_run_blocks=2,
            heating_system_type=heating_system_type,
        )
        opt.optimize(**plan_kwargs)
        return opt, opt._evaluate_action(MODE_HEATING, **cost_kwargs)

    opt_radiator, cost_radiator = cool_capable("radiator")
    opt_empty, cost_empty = cool_capable("")
    opt_ufh, _ = cool_capable("underfloor")

    # All three are cool-capable, so all three clear the cooling horizon.
    assert opt_radiator._lookahead_blocks == LOOKAHEAD_COOLING_BLOCKS
    assert opt_empty._lookahead_blocks == LOOKAHEAD_COOLING_BLOCKS
    # Radiator's own tau horizon (2 + 2 = 4) stays below base, so it is not
    # synthesis-eligible even though its combined lookahead grew to 18.
    assert opt_radiator._heating_lookahead_blocks == LOOKAHEAD_BASE_BLOCKS
    assert opt_empty._heating_lookahead_blocks == LOOKAHEAD_BASE_BLOCKS
    assert cost_radiator == cost_empty, (
        f"radiator+AC heating cost {cost_radiator} must equal unprofiled+AC "
        f"{cost_empty}: the cooling extension must not enable afterglow synthesis"
    )
    # UFH's own tau horizon (2 + 18 = 20) clears base, so it stays eligible.
    assert opt_ufh._heating_lookahead_blocks > LOOKAHEAD_BASE_BLOCKS


# ---------------------------------------------------------------------------
# approach_rate tests
# ---------------------------------------------------------------------------


def test_approach_rate_one_matches_legacy_deadbeat_formula():
    """approach_rate=1.0 must reproduce the original one-block formula exactly."""
    model = RCModel(C=1.0, U=0.3, Q_heat=4.0, Q_cool=6.0)
    opt = MPCOptimizer(model=model, approach_rate=1.0)
    T_room, T_out, target, dt = 19.0, 5.0, 21.0, 5.0
    pf, mode = opt.compute_optimal_power(T_room, T_out, target, dt)

    dt_h = dt / 60.0
    alpha = model.U
    beta = 1.0 - math.exp(-alpha * dt_h)
    T_drift = T_room + beta * (T_out - T_room)
    energy_bias = (opt.w_energy / max(opt.w_comfort, 0.01)) * alpha / beta * 0.1
    q_req = max(0.0, (target - T_drift) * alpha / beta - energy_bias)
    expected = max(min(q_req / max(model.Q_heat, 0.01), 1.0), MIN_POWER_FRACTION)

    assert mode == MODE_HEATING
    assert pf == pytest.approx(expected)


def test_approach_rate_below_one_widens_heating_band():
    """Gentler approach_rate lowers pf for the same gap (less saturation)."""
    model = RCModel(C=1.0, U=0.3, Q_heat=20.0, Q_cool=6.0)
    args = (19.0, 5.0, 21.0, 5.0)
    pf_full, _ = MPCOptimizer(model=model, approach_rate=1.0).compute_optimal_power(*args)
    pf_gentle, _ = MPCOptimizer(model=model, approach_rate=0.3).compute_optimal_power(*args)
    assert pf_full == 1.0
    assert pf_gentle < pf_full
    assert pf_gentle >= MIN_POWER_FRACTION


def test_holding_power_independent_of_approach_rate():
    """At target, only holding power is needed; approach_rate must not change it."""
    model = RCModel(C=1.0, U=0.3, Q_heat=20.0, Q_cool=6.0)
    args = (21.0, 5.0, 21.0, 5.0)
    pf_full, _ = MPCOptimizer(model=model, approach_rate=1.0).compute_optimal_power(*args)
    pf_gentle, _ = MPCOptimizer(model=model, approach_rate=0.2).compute_optimal_power(*args)
    assert pf_full == pytest.approx(pf_gentle)


def test_approach_rate_widens_cooling_band_symmetrically():
    """Cooling path is gentled the same way as heating."""
    model = RCModel(C=1.0, U=0.3, Q_heat=4.0, Q_cool=20.0)
    args = (26.0, 30.0, 23.0, 5.0)
    pf_full, m1 = MPCOptimizer(model=model, approach_rate=1.0).compute_optimal_power(*args)
    pf_gentle, m2 = MPCOptimizer(model=model, approach_rate=0.3).compute_optimal_power(*args)
    assert m1 == MODE_COOLING and m2 == MODE_COOLING
    assert pf_gentle < pf_full
    assert pf_gentle >= MIN_POWER_FRACTION
