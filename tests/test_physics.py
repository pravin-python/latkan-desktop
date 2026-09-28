"""Tests for the pure-Python rope simulation (no Qt needed)."""

from __future__ import annotations

import math

import pytest

from latkan.physics import MIN_LENGTH, PhysicsParams, Rope, Simulation

DT = PhysicsParams().dt


def segment_lengths(rope: Rope) -> list[float]:
    pts = rope.points
    return [math.dist(a, b) for a, b in zip(pts, pts[1:])]


def max_stretch(rope: Rope) -> float:
    """Largest relative over-length of any segment (a thread may go slack, never stretch)."""
    return max(d / rope.segment_length - 1.0 for d in segment_lengths(rope))


def max_deviation(rope: Rope) -> float:
    """Largest relative deviation of any segment from its rest length, either way."""
    return max(abs(d / rope.segment_length - 1.0) for d in segment_lengths(rope))


def run_until_settled(rope: Rope, max_seconds: float = 60.0) -> float:
    """Step until settled; return the simulated seconds it took (fails the test on timeout)."""
    for i in range(1, int(max_seconds / DT) + 1):
        rope.step()
        if rope.settled():
            return i * DT
    pytest.fail(f"rope did not settle within {max_seconds} s: {rope!r}, speed={rope.speed():.2f}")


def velocity_of_end(rope: Rope) -> tuple[float, float]:
    """Finite-difference end velocity over one step (px/s)."""
    x0, y0 = rope.end()
    rope.step()
    x1, y1 = rope.end()
    return (x1 - x0) / DT, (y1 - y0) / DT


def all_finite(rope: Rope) -> bool:
    return all(math.isfinite(c) for p in rope.points for c in p)


# --------------------------------------------------------------------------- params


def test_params_defaults_match_contract():
    p = PhysicsParams()
    assert (p.gravity, p.damping, p.segments, p.iterations) == (1800.0, 0.993, 12, 20)
    assert (p.end_inv_mass, p.dt, p.max_substeps) == (0.25, 1 / 120, 8)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"segments": 0},
        {"iterations": 0},
        {"dt": 0.0},
        {"dt": float("nan")},
        {"damping": 0.0},
        {"damping": 1.5},
        {"end_inv_mass": 0.0},
        {"max_substeps": 0},
        {"gravity": float("inf")},
    ],
)
def test_params_reject_invalid_values(kwargs):
    with pytest.raises(ValueError):
        PhysicsParams(**kwargs)


# --------------------------------------------------------------------------- construction


def test_initial_rope_is_straight_with_exact_length():
    rope = Rope(300.0, 0.0, 180.0)
    pts = rope.points
    assert len(pts) == PhysicsParams().segments + 1
    assert pts[0] == (300.0, 0.0)
    assert rope.end() == pytest.approx((300.0, 180.0))
    assert segment_lengths(rope) == pytest.approx([180.0 / 12] * 12)
    assert rope.speed() == 0.0
    assert not rope.is_grabbed


def test_initial_offset_skews_line_but_keeps_length():
    rope = Rope(300.0, 0.0, 200.0, initial_offset=60.0)
    ex, ey = rope.end()
    assert ex == pytest.approx(360.0)
    assert math.hypot(ex - 300.0, ey) == pytest.approx(200.0)
    assert segment_lengths(rope) == pytest.approx([200.0 / 12] * 12)
    # an absurd offset is clamped instead of producing an impossible geometry
    wide = Rope(0.0, 0.0, 100.0, initial_offset=1e6)
    assert math.hypot(*wide.end()) == pytest.approx(100.0)
    assert all_finite(wide)


def test_constructor_rejects_non_finite_values():
    with pytest.raises(ValueError):
        Rope(float("nan"), 0.0, 100.0)
    with pytest.raises(ValueError):
        Rope(0.0, 0.0, float("inf"))


def test_points_is_a_snapshot():
    rope = Rope(0.0, 0.0, 100.0)
    snapshot = rope.points
    snapshot[3] = (999.0, 999.0)
    assert rope.points[3] != (999.0, 999.0)


# --------------------------------------------------------------------------- settling & constraints


@pytest.mark.parametrize("length", [40.0, 150.0, 600.0, 1500.0])
def test_rope_settles_straight_below_anchor(length):
    rope = Rope(500.0, 0.0, length, initial_offset=0.4 * length)
    run_until_settled(rope)
    ex, ey = rope.end()
    assert ex == pytest.approx(500.0, abs=1.5)
    assert ey == pytest.approx(length, rel=0.02)
    assert abs(rope.end_angle()) < 0.02
    for x, _ in rope.points:
        assert x == pytest.approx(500.0, abs=1.5)


@pytest.mark.parametrize("length", [40.0, 150.0, 600.0, 1500.0])
def test_segment_lengths_preserved_within_two_percent(length):
    rope = Rope(200.0, 0.0, length, initial_offset=0.4 * length)
    worst = 0.0
    for _ in range(int(3.0 / DT)):  # while swinging
        rope.step()
        worst = max(worst, max_stretch(rope))
    assert worst < 0.02
    run_until_settled(rope)
    assert max_deviation(rope) < 0.02  # and at rest every segment is taut at its length


def test_hard_nudge_does_not_rubber_band():
    rope = Rope(200.0, 0.0, 150.0)
    rope.nudge(2500.0)
    worst = 0.0
    for _ in range(int(2.0 / DT)):
        rope.step()
        worst = max(worst, max_stretch(rope))
    assert worst < 0.05
    assert all_finite(rope)


def test_settled_is_not_fooled_by_swing_turning_point():
    rope = Rope(500.0, 0.0, 150.0, initial_offset=40.0)
    # Force a turning point: at t=0 the rope is displaced and at rest (speed 0) - not settled.
    assert rope.speed() == 0.0
    assert not rope.settled()
    for _ in range(int(1.0 / DT)):
        rope.step()
        if rope.speed() < 1.5:
            assert not rope.settled(), "a displaced rope must not count as settled"


def test_stable_across_supported_lengths_under_hard_input():
    for length in (40.0, 1500.0):
        rope = Rope(0.0, 0.0, length)
        rope.nudge(3000.0, -2000.0)
        for _ in range(int(5.0 / DT)):
            rope.apply_force(5000.0, 0.0)
            rope.step()
        assert all_finite(rope)
        assert max(segment_lengths(rope)) < rope.segment_length * 1.25
        run_until_settled(rope)


def test_rope_pushed_straight_up_goes_slack_and_settles():
    # Exactly vertical input on a never-swung rope: rigid links could balance folded forever.
    rope = Rope(960.0, 0.0, 150.0)
    rope.grab(960.0, 150.0)
    for y in range(150, 40, -5):
        rope.move_grab(960.0, float(y))
        rope.step()
        rope.step()
    ex, ey = rope.end()
    assert (ex, ey) == pytest.approx((960.0, 45.0))
    assert min(segment_lengths(rope)) < rope.segment_length * 0.9  # slack, not rigid
    rope.release()
    run_until_settled(rope, max_seconds=10.0)
    assert rope.end() == pytest.approx((960.0, 150.0), abs=1.0)


def test_vertical_toss_goes_slack_and_settles():
    rope = Rope(0.0, 0.0, 150.0)
    rope.nudge(0.0, -800.0)
    highest = rope.end()[1]
    for _ in range(30):
        rope.step()
        highest = min(highest, rope.end()[1])
    assert highest < 140.0  # the charm flew up on a slack thread
    run_until_settled(rope, max_seconds=10.0)
    assert rope.end() == pytest.approx((0.0, 150.0), abs=1.0)


def test_zero_length_segments_do_not_produce_nan():
    rope = Rope(100.0, 50.0, 120.0)
    # Collapse every node onto the anchor - a degenerate state no public call produces directly.
    for arr, value in ((rope._x, 100.0), (rope._px, 100.0), (rope._y, 50.0), (rope._py, 50.0)):
        arr[:] = [value] * len(arr)
    rope.step()
    assert all_finite(rope)
    run_until_settled(rope)
    assert rope.end() == pytest.approx((100.0, 170.0), abs=2.5)


def test_non_finite_state_recovers_by_resetting():
    rope = Rope(100.0, 0.0, 120.0)
    rope._x[5] = float("nan")
    rope.step()
    assert all_finite(rope)
    assert rope.end() == pytest.approx((100.0, 120.0))


# --------------------------------------------------------------------------- grab / release


def test_grab_clamps_target_to_rope_length_and_follows():
    rope = Rope(100.0, 0.0, 150.0)
    rope.grab(100.0, 1000.0)  # far below: clamped to the rope length
    assert rope.is_grabbed
    rope.step()
    assert rope.end() == pytest.approx((100.0, 150.0))

    rope.move_grab(400.0, 400.0)  # clamped along the direction from the anchor
    rope.step()
    ex, ey = rope.end()
    assert math.hypot(ex - 100.0, ey) == pytest.approx(150.0)
    assert (ex - 100.0) / ey == pytest.approx(300.0 / 400.0)  # same direction as the target

    rope.move_grab(180.0, 60.0)  # within reach: pinned exactly
    for _ in range(3):
        rope.step()
        assert rope.end() == pytest.approx((180.0, 60.0))
    assert not rope.settled()  # grabbed ropes are never settled


def test_move_grab_without_grab_is_ignored():
    rope = Rope(0.0, 0.0, 100.0)
    rope.move_grab(50.0, 50.0)
    rope.step()
    assert not rope.is_grabbed
    assert rope.end()[0] == pytest.approx(0.0)


def test_release_after_drag_keeps_momentum():
    rope = Rope(500.0, 0.0, 200.0)
    rope.grab(500.0, 150.0)
    for _ in range(30):
        rope.step()
    # Drag right at 360 px/s with 60 Hz input while physics runs at 120 Hz.
    x = 500.0
    for i in range(40):
        if i % 2 == 0:
            x += 6.0
            rope.move_grab(x, 150.0)
        rope.step()
    rope.release()
    assert not rope.is_grabbed
    vx, _ = velocity_of_end(rope)
    assert vx > 150.0  # keeps a good share of the 360 px/s drag, in the drag direction
    x_before = rope.end()[0]
    for _ in range(10):
        rope.step()
    assert rope.end()[0] > x_before + 5.0


def test_release_after_stationary_hold_does_not_fling():
    rope = Rope(500.0, 0.0, 200.0)
    rope.grab(560.0, 150.0)
    for _ in range(120):
        rope.step()
    rope.release()
    vx, vy = velocity_of_end(rope)
    assert math.hypot(vx, vy) < 200.0  # only gravity starts it moving, no stored fling


def total_length(rope: Rope) -> float:
    return sum(segment_lengths(rope))


def node_velocities(rope: Rope) -> list[float]:
    """Every node's Verlet velocity (px/s), flattened as ``[vx0, vy0, vx1, vy1, ...]``."""
    result = []
    for x, y, px, py in zip(rope._x, rope._y, rope._px, rope._py):
        result += [(x - px) / DT, (y - py) / DT]
    return result


def circle_grab(rope: Rope, speed: float, steps: int):
    """Drag the end round a circle below the anchor at ``speed`` px/s, one target per step."""
    ax, length = rope.anchor_x, rope.length
    cx, cy, radius = ax, rope.anchor_y + 0.55 * length, 0.4 * length
    omega = speed / radius
    rope.grab(cx + radius, cy)
    for k in range(steps):
        rope.move_grab(cx + radius * math.cos(omega * k * DT), cy + radius * math.sin(omega * k * DT))
        rope.step()
        yield k


def test_fast_drag_does_not_stretch_the_rope():
    # Both ends are pinned while grabbed; without inextensibility passes the inner nodes lagged
    # behind so far that this drag stretched the rope by ~17 % (single segments by ~50 %).
    rope = Rope(500.0, 0.0, 170.0)
    worst = 0.0
    for k in circle_grab(rope, 3000.0, int(2.0 / DT)):
        if k > 30:
            worst = max(worst, total_length(rope) / rope.length - 1.0)
    assert worst < 0.04
    assert all_finite(rope)


def test_release_after_fast_drag_does_not_jerk_toward_the_anchor():
    worst_jerk = 0.0
    for release_after in range(60, 200, 7):  # different phases of the circle
        rope = Rope(500.0, 0.0, 110.0)
        for _ in circle_grab(rope, 3000.0, release_after):
            pass
        velocities = node_velocities(rope)
        rope.release()
        # release() removes the leftover stretch without changing any node's velocity ...
        assert total_length(rope) <= rope.length * (1.0 + 1e-9)
        assert node_velocities(rope) == pytest.approx(velocities, abs=1e-6)
        # ... so the first free step adds no pull toward the anchor beyond what the rope going
        # taut takes (that stretch used to snap back as up to ~650 px/s toward the anchor here).
        ex, ey = rope.end()
        ux, uy = ex - rope.anchor_x, ey - rope.anchor_y
        norm = math.hypot(ux, uy)
        inward_before = -(velocities[-2] * ux + velocities[-1] * uy) / norm
        vx, vy = velocity_of_end(rope)
        inward_after = -(vx * ux + vy * uy) / norm
        worst_jerk = max(worst_jerk, inward_after - max(inward_before, 0.0))
    assert worst_jerk < 100.0


def test_grab_ignores_non_finite_positions():
    rope = Rope(0.0, 0.0, 100.0)
    rope.grab(float("nan"), 0.0)
    assert not rope.is_grabbed
    rope.grab(10.0, 50.0)
    rope.move_grab(float("inf"), 0.0)
    rope.step()
    assert rope.end() == pytest.approx((10.0, 50.0))


# --------------------------------------------------------------------------- nudge / force


def test_nudge_adds_velocity_scaled_from_anchor_to_end():
    rope = Rope(0.0, 0.0, 120.0)
    rope.nudge(240.0)
    assert rope.speed() == pytest.approx(240.0)  # end node gets the full velocity
    n = len(rope.points) - 1
    for i in range(n + 1):
        vx = (rope._x[i] - rope._px[i]) / DT
        assert vx == pytest.approx(240.0 * i / n)


def test_nudge_produces_motion_then_damps_to_settled():
    rope = Rope(0.0, 0.0, 150.0)
    assert rope.settled()
    rope.nudge(600.0)
    assert not rope.settled()
    peak = 0.0
    for _ in range(int(0.3 / DT)):
        rope.step()
        peak = max(peak, abs(rope.end()[0]))
    assert peak > 10.0
    seconds = run_until_settled(rope)
    assert seconds < 30.0
    assert rope.speed() < 1.5
    assert rope.end()[0] == pytest.approx(0.0, abs=1.0)


def test_nudge_ignores_non_finite():
    rope = Rope(0.0, 0.0, 100.0)
    rope.nudge(float("nan"))
    rope.nudge(float("inf"), 1.0)
    assert rope.speed() == 0.0


def test_end_angle_sign_convention():
    right = Rope(0.0, 0.0, 100.0, initial_offset=30.0)
    left = Rope(0.0, 0.0, 100.0, initial_offset=-30.0)
    straight = Rope(0.0, 0.0, 100.0)
    assert right.end_angle() > 0.0
    assert left.end_angle() < 0.0
    assert right.end_angle() == pytest.approx(-left.end_angle())
    assert straight.end_angle() == pytest.approx(0.0)
    assert right.end_angle() == pytest.approx(math.asin(30.0 / 100.0))


def test_apply_force_lasts_for_one_step_only():
    once, twice = Rope(0.0, 0.0, 150.0), Rope(0.0, 0.0, 150.0)
    once.apply_force(3000.0, 0.0)
    twice.apply_force(3000.0, 0.0)
    once.step()
    twice.step()
    assert once.points == twice.points
    assert once.end()[0] > 0.0  # pushed toward +x
    twice.apply_force(3000.0, 0.0)
    once.step()
    twice.step()
    assert twice.end()[0] > once.end()[0]


def test_apply_force_ignores_non_finite():
    rope = Rope(0.0, 0.0, 100.0)
    rope.apply_force(float("nan"), 0.0)
    rope.step()
    assert all_finite(rope)


# --------------------------------------------------------------------------- anchor & length


def test_anchor_move_is_honoured_on_next_step_and_swings():
    rope = Rope(100.0, 0.0, 150.0)
    rope.anchor_x = 130.0
    rope.step()
    assert rope.points[0] == (130.0, 0.0)
    assert rope.speed() > 1.5
    run_until_settled(rope)
    assert rope.end()[0] == pytest.approx(130.0, abs=1.0)


def test_large_anchor_jump_translates_instead_of_exploding():
    rope = Rope(100.0, 0.0, 150.0)
    rope.anchor_x = 2100.0
    rope.step()
    assert rope.points[0] == (2100.0, 0.0)
    assert rope.end()[0] == pytest.approx(2100.0, abs=0.5)
    assert rope.speed() < 50.0


def test_relocate_moves_whole_rope_keeping_velocity():
    rope = Rope(100.0, 0.0, 150.0)
    rope.nudge(300.0)
    speed = rope.speed()
    rope.relocate(400.0, 20.0)
    assert (rope.anchor_x, rope.anchor_y) == (400.0, 20.0)
    assert rope.points[0] == (400.0, 20.0)
    assert rope.end() == pytest.approx((400.0, 170.0))
    assert rope.speed() == pytest.approx(speed)


def test_length_setter_clamps_and_rescales():
    rope = Rope(0.0, 0.0, 150.0)
    rope.length = 5.0
    assert rope.length == MIN_LENGTH
    assert rope.segment_length == pytest.approx(MIN_LENGTH / 12)

    rope.length = 300.0
    assert rope.length == 300.0
    assert rope.segment_length == pytest.approx(25.0)
    # the shape is rescaled immediately, so the rope is consistent before any step
    assert segment_lengths(rope) == pytest.approx([25.0] * 12)
    assert rope.end() == pytest.approx((0.0, 300.0))
    run_until_settled(rope)
    assert rope.end()[1] == pytest.approx(300.0, rel=0.02)

    with pytest.raises(ValueError):
        rope.length = float("nan")


def test_shortening_rope_does_not_launch_it():
    rope = Rope(0.0, 0.0, 600.0)
    rope.length = 100.0
    rope.step()
    assert rope.speed() < 100.0
    assert rope.end()[1] == pytest.approx(100.0, rel=0.02)


# --------------------------------------------------------------------------- simulation


class CountingRope(Rope):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.steps = 0

    def step(self) -> None:
        self.steps += 1
        super().step()


def test_advance_runs_fixed_steps_and_accumulates_remainder():
    sim = Simulation()
    rope = CountingRope(0.0, 0.0, 100.0, params=sim.params)
    sim.add(rope)
    assert sim.advance(1 / 60) == 2
    assert sim.advance(0.5 * DT) == 0
    assert sim.advance(0.5 * DT) == 1  # the two halves add up to one step
    assert rope.steps == 3
    total = sum(sim.advance(0.016) for _ in range(60))  # 0.96 s of ~62.5 fps frames
    assert total == round(0.96 / DT)


def test_advance_respects_max_substeps_and_drops_leftover():
    params = PhysicsParams(max_substeps=4)
    sim = Simulation(params)
    rope = CountingRope(0.0, 0.0, 100.0, params=params)
    sim.add(rope)
    assert sim.advance(1.0) == 4  # a one second stall only runs the cap
    assert sim.advance(0.0) == 0  # ...and the rest of that second is gone
    assert rope.steps == 4


def test_advance_ignores_bad_elapsed_values():
    sim = Simulation()
    sim.add(Rope(0.0, 0.0, 100.0))
    assert sim.advance(-1.0) == 0
    assert sim.advance(float("nan")) == 0
    assert sim.advance(float("inf")) == 0
    assert sim.advance("soon") == 0  # type: ignore[arg-type]
    assert sim.advance(DT) == 1


def test_reset_clock_clears_accumulated_time():
    kept = Simulation()
    assert kept.advance(0.9 * DT) == 0
    assert kept.advance(0.2 * DT) == 1  # 1.1 dt accumulated

    reset = Simulation()
    assert reset.advance(0.9 * DT) == 0
    reset.reset_clock()
    assert reset.advance(0.2 * DT) == 0  # only 0.2 dt since the reset


def test_simulation_add_remove_and_settled():
    sim = Simulation()
    assert sim.settled()
    a = Rope(0.0, 0.0, 100.0, params=sim.params)
    b = Rope(200.0, 0.0, 100.0, params=sim.params, initial_offset=30.0)
    sim.add(a)
    sim.add(a)  # adding twice is a no-op
    sim.add(b)
    assert sim.ropes == [a, b]
    assert not sim.settled()  # b starts displaced
    for _ in range(int(60 / DT / 8)):
        sim.advance(8 * DT)
        if sim.settled():
            break
    assert sim.settled()
    ropes_ref = sim.ropes
    sim.remove(a)
    sim.remove(a)  # removing twice is a no-op
    assert sim.ropes == [b]
    assert ropes_ref is sim.ropes  # list identity is stable
    b.grab(250.0, 50.0)
    assert not sim.settled()
