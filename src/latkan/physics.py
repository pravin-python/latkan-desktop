"""Pure-Python Verlet rope simulation.

This module deliberately has no Qt (or any third-party) dependency so it can be unit tested
headless and reused anywhere.

Model
-----
A :class:`Rope` is a chain of ``segments + 1`` point masses. Node 0 is pinned to the anchor,
the last node carries the charm. Every fixed step:

1. **Verlet integration** of all free nodes: ``x' = x + (x - x_prev) * damping + a * dt^2``.
   Velocity is never stored explicitly - it is encoded by the previous position, which is what
   makes :meth:`Rope.release` keep momentum and :meth:`Rope.nudge` a simple shift of ``x_prev``.
2. **Distance constraints** (position based, Gauss-Seidel, alternating sweep direction) keep every
   segment from exceeding its rest length - like a real thread the rope resists stretching but
   goes slack under compression. Corrections are split by inverse mass: the anchor and a grabbed
   end have inverse mass 0, the charm end is heavier than a rope node (``end_inv_mass``).
3. For a free (not grabbed) rope, a final follow-the-leader pass from the anchor removes any
   remaining stretch, so the rope never rubber-bands. A grabbed rope has both ends pinned, so it
   gets alternating follow-the-leader passes from the grabbed end and from the anchor instead
   (moving only the inner nodes), and :meth:`Rope.release` removes what little stretch is left
   without changing any node's velocity.

Coordinates are logical pixels with +y pointing down. Time is in seconds.
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["MIN_LENGTH", "PhysicsParams", "Rope", "Simulation"]

MIN_LENGTH = 10.0
"""Shortest rope the simulation accepts (px); the ``length`` setter clamps to this."""

# Hard cap on any node's speed (px/s). Physically plausible motion is far below this; it only
# guards against runaway inputs (absurd nudges/forces) turning into a numeric explosion.
_MAX_SPEED = 12_000.0
# While grabbed, the end's Verlet velocity is an exponential moving average of the target's
# motion (per fixed step). Mouse events arrive at a different rate than physics steps; without
# smoothing a release right after a step without cursor motion would drop all momentum.
_GRAB_SMOOTHING = 0.35
_MAX_GRAB_SPEED = 6_000.0
# Alternating inextensibility passes per step while grabbed (even: the last one runs from the
# grabbed end, so the small residual stretch sits at the anchor instead of at the charm).
# Without them a 3000 px/s drag stretched the rope by 15-25 %; with 6 it stays at a few %.
_GRAB_INEXTENSIBLE_PASSES = 6
# An anchor that jumps farther than max(rope length, this) in a single step is treated as a
# relocation (screen/geometry change): the whole rope is translated instead of being yanked.
_TELEPORT_MIN_DISTANCE = 250.0
# Tolerance for floating point drift when converting accumulated time into fixed steps.
_TIME_EPS = 1e-9


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


@dataclass
class PhysicsParams:
    """Tunable simulation constants shared by a :class:`Simulation` and its ropes.

    ``segments`` and ``end_inv_mass`` are read when a rope is created; the other fields are read
    on every step, so changing them affects existing ropes immediately.
    """

    gravity: float = 1800.0  # px/s^2, +y is down
    damping: float = 0.993  # per fixed step velocity retention
    segments: int = 12
    iterations: int = 20  # constraint passes per step
    end_inv_mass: float = 0.25  # charm end is 4x heavier than a rope node
    dt: float = 1 / 120  # fixed timestep (s)
    max_substeps: int = 8  # cap per advance() to avoid spiral of death

    def __post_init__(self) -> None:
        if not _finite(self.gravity):
            raise ValueError(f"gravity must be finite, got {self.gravity!r}")
        if not (math.isfinite(self.damping) and 0.0 < self.damping <= 1.0):
            raise ValueError(f"damping must be in (0, 1], got {self.damping!r}")
        if int(self.segments) != self.segments or self.segments < 1:
            raise ValueError(f"segments must be a positive integer, got {self.segments!r}")
        if int(self.iterations) != self.iterations or self.iterations < 1:
            raise ValueError(f"iterations must be a positive integer, got {self.iterations!r}")
        if not (math.isfinite(self.end_inv_mass) and self.end_inv_mass > 0.0):
            raise ValueError(f"end_inv_mass must be > 0, got {self.end_inv_mass!r}")
        if not (math.isfinite(self.dt) and self.dt > 0.0):
            raise ValueError(f"dt must be > 0, got {self.dt!r}")
        if int(self.max_substeps) != self.max_substeps or self.max_substeps < 1:
            raise ValueError(f"max_substeps must be a positive integer, got {self.max_substeps!r}")
        self.segments = int(self.segments)
        self.iterations = int(self.iterations)
        self.max_substeps = int(self.max_substeps)


class Rope:
    """A single rope pinned at ``(anchor_x, anchor_y)`` with a charm on its free end."""

    def __init__(
        self,
        anchor_x: float,
        anchor_y: float,
        length: float,
        params: PhysicsParams | None = None,
        initial_offset: float = 0.0,
    ) -> None:
        """Create a rope hanging in a straight line from the anchor.

        ``initial_offset`` is the horizontal offset (px) of the end node relative to the point
        directly below the anchor, so a freshly added rope starts tilted and swings in. It is
        clamped to 95 % of the rope length; the initial line always has exactly ``length``.
        """
        self.params: PhysicsParams = params if params is not None else PhysicsParams()
        ax, ay, ln, offset = float(anchor_x), float(anchor_y), float(length), float(initial_offset)
        if not _finite(ax, ay, ln, offset):
            raise ValueError("Rope anchor, length and initial_offset must be finite numbers")

        self.anchor_x: float = ax
        self.anchor_y: float = ay
        self._n: int = self.params.segments
        self._length: float = max(MIN_LENGTH, ln)
        self._seg: float = self._length / self._n

        self._grabbed = False
        self._target_x = 0.0
        self._target_y = 0.0
        self._grab_vx = 0.0
        self._grab_vy = 0.0
        self._force_x = 0.0
        self._force_y = 0.0

        count = self._n + 1
        self._x: list[float] = [0.0] * count
        self._y: list[float] = [0.0] * count
        self._px: list[float] = [0.0] * count
        self._py: list[float] = [0.0] * count

        # Per-constraint correction shares (w_a / (w_a + w_b), w_b / (w_a + w_b)), precomputed for
        # the free and the grabbed configuration so the inner solver loop does no divisions by mass.
        inv = [0.0] + [1.0] * (self._n - 1) + [float(self.params.end_inv_mass)]
        self._shares_free = self._constraint_shares(inv)
        inv[-1] = 0.0
        self._shares_grabbed = self._constraint_shares(inv)
        self._forward = tuple(range(self._n))
        self._backward = tuple(reversed(self._forward))
        self._inner = tuple(range(1, self._n))  # every node but the anchor and the end
        self._inner_back = tuple(reversed(self._inner))

        self._hang(offset)

    # ------------------------------------------------------------------ properties

    @property
    def length(self) -> float:
        """Rest length of the whole rope (px)."""
        return self._length

    @length.setter
    def length(self, value: float) -> None:
        new = float(value)
        if not math.isfinite(new):
            raise ValueError(f"rope length must be finite, got {value!r}")
        new = max(MIN_LENGTH, new)
        if new == self._length:
            return
        # Scale the current shape (and previous positions, i.e. preserve angular velocity) about
        # the pinned node so the rope is immediately consistent with the new rest length instead
        # of being snapped by the solver, which would inject a huge implicit velocity.
        factor = new / self._length
        ox, oy = self._x[0], self._y[0]
        for arr, origin in ((self._x, ox), (self._y, oy), (self._px, ox), (self._py, oy)):
            for i in range(1, len(arr)):
                arr[i] = origin + (arr[i] - origin) * factor
        self._length = new
        self._seg = new / self._n

    @property
    def segment_length(self) -> float:
        """Rest length of one segment (px)."""
        return self._seg

    @property
    def points(self) -> list[tuple[float, float]]:
        """Snapshot of the ``segments + 1`` node positions, anchor first."""
        return list(zip(self._x, self._y, strict=True))

    @property
    def is_grabbed(self) -> bool:
        return self._grabbed

    # ------------------------------------------------------------------ simulation

    def step(self) -> None:
        """Advance this rope by exactly one fixed ``params.dt`` step."""
        params = self.params
        dt = params.dt
        n = self._n
        xs, ys, pxs, pys = self._x, self._y, self._px, self._py

        ax, ay = self.anchor_x, self.anchor_y
        if not _finite(ax, ay):
            logger.warning("Ignoring non-finite rope anchor (%r, %r)", ax, ay)
            ax, ay = xs[0], ys[0]
            self.anchor_x, self.anchor_y = ax, ay
        jump_x, jump_y = ax - xs[0], ay - ys[0]
        if math.hypot(jump_x, jump_y) > max(self._length, _TELEPORT_MIN_DISTANCE):
            self._translate(jump_x, jump_y)

        # Pinned anchor: prev keeps the old position so speed() reflects anchor drags.
        pxs[0], pys[0] = xs[0], ys[0]
        xs[0], ys[0] = ax, ay

        dt2 = dt * dt
        acc_x = self._force_x * dt2
        acc_y = (params.gravity + self._force_y) * dt2
        self._force_x = self._force_y = 0.0
        damping = params.damping
        vmax = _MAX_SPEED * dt
        vmax_sq = vmax * vmax

        grabbed = self._grabbed
        last_free = n - 1 if grabbed else n
        for i in range(1, last_free + 1):
            x, y = xs[i], ys[i]
            vx = (x - pxs[i]) * damping
            vy = (y - pys[i]) * damping
            v_sq = vx * vx + vy * vy
            if v_sq > vmax_sq:
                scale = vmax / math.sqrt(v_sq)
                vx *= scale
                vy *= scale
            pxs[i], pys[i] = x, y
            xs[i] = x + vx + acc_x
            ys[i] = y + vy + acc_y

        if grabbed:
            self._drive_grabbed_end(ax, ay, dt)
            share_a, share_b = self._shares_grabbed
        else:
            share_a, share_b = self._shares_free

        # Unilateral constraints: a thread resists stretching only. Bilateral (rigid rod) links
        # would let a rope pushed straight up balance folded in an unstable equilibrium forever
        # (never settling); they also never divide by a zero length, as seg > 0.
        seg = self._seg
        seg_sq = seg * seg
        sqrt = math.sqrt
        forward, backward = self._forward, self._backward
        for it in range(params.iterations):
            for i in forward if not it & 1 else backward:
                j = i + 1
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                d_sq = dx * dx + dy * dy
                if d_sq <= seg_sq:
                    continue
                d = sqrt(d_sq)
                k = (d - seg) / d
                ka = k * share_a[i]
                kb = k * share_b[i]
                xs[i] += dx * ka
                ys[i] += dy * ka
                xs[j] -= dx * kb
                ys[j] -= dy * kb

        if not grabbed:
            # Follow-the-leader pass: a fixed number of Gauss-Seidel passes leaves a residual
            # stretch (several % on short ropes, far more under a hard nudge - a visible rubber
            # band). Walking down from the pinned anchor and pulling each over-stretched node
            # back onto its segment length makes the free rope inextensible. Positions only: the
            # implicit velocity this adds is what cancels gravity at rest, so ropes still settle.
            for i in forward:
                j = i + 1
                dx = xs[j] - xs[i]
                dy = ys[j] - ys[i]
                d = sqrt(dx * dx + dy * dy)
                if d > seg:
                    f = seg / d
                    xs[j] = xs[i] + dx * f
                    ys[j] = ys[i] + dy * f
        else:
            # Both ends are pinned, so the pass above (which moves the end) cannot run, and the
            # Gauss-Seidel passes alone let the inner nodes lag far behind a fast drag: the rope
            # visibly stretched and, on release, snapped back and bent the fling toward the
            # anchor. Alternate follow-the-leader passes from the anchor (each inner node pulled
            # onto its segment length toward the node above) and from the grabbed end (toward the
            # node below). Unilateral like the constraints, so a rope pushed together stays slack.
            inner, inner_back = self._inner, self._inner_back
            for p in range(_GRAB_INEXTENSIBLE_PASSES):
                step_to = -1 if not p & 1 else 1  # leader: the node above, then the node below
                for i in inner if not p & 1 else inner_back:
                    h = i + step_to
                    dx = xs[i] - xs[h]
                    dy = ys[i] - ys[h]
                    d_sq = dx * dx + dy * dy
                    if d_sq > seg_sq:
                        f = seg / sqrt(d_sq)
                        xs[i] = xs[h] + dx * f
                        ys[i] = ys[h] + dy * f

        if not math.isfinite(sum(xs) + sum(ys) + sum(pxs) + sum(pys)):
            logger.warning("Rope state became non-finite; resetting it to hang at rest")
            self._grabbed = False
            self._hang(0.0)

    def grab(self, x: float, y: float) -> None:
        """Pin the end node to ``(x, y)``, clamped to at most ``length`` from the anchor."""
        if not _finite(x, y):
            logger.warning("Ignoring grab at non-finite position (%r, %r)", x, y)
            return
        self._grabbed = True
        self._target_x, self._target_y = float(x), float(y)
        self._grab_vx = self._grab_vy = 0.0

    def move_grab(self, x: float, y: float) -> None:
        """Move the grab target; ignored when the rope is not grabbed."""
        if not self._grabbed:
            return
        if not _finite(x, y):
            logger.warning("Ignoring grab move to non-finite position (%r, %r)", x, y)
            return
        self._target_x, self._target_y = float(x), float(y)

    def release(self) -> None:
        """Let go of the end. Its velocity (encoded in the Verlet previous position) is kept.

        Any stretch the drag left behind is removed right here by one follow-the-leader pass from
        the anchor that moves each node's previous position along with it, so it changes no
        node's velocity. (Left to the next free step, that pass would turn the stretch into a
        jerk of the charm toward the anchor and bend the fling.)
        """
        if not self._grabbed:
            return
        self._grabbed = False
        xs, ys, pxs, pys = self._x, self._y, self._px, self._py
        seg = self._seg
        for i in self._forward:
            j = i + 1
            dx = xs[j] - xs[i]
            dy = ys[j] - ys[i]
            d = math.sqrt(dx * dx + dy * dy)
            if d > seg:
                f = seg / d
                nx, ny = xs[i] + dx * f, ys[i] + dy * f
                pxs[j] += nx - xs[j]
                pys[j] += ny - ys[j]
                xs[j], ys[j] = nx, ny

    def nudge(self, vx: float, vy: float = 0.0) -> None:
        """Add velocity (px/s) to every free node, scaled linearly from 0 at the anchor to 1 at the end."""
        if not _finite(vx, vy):
            logger.warning("Ignoring non-finite nudge (%r, %r)", vx, vy)
            return
        dt = self.params.dt
        n = self._n
        last_free = n - 1 if self._grabbed else n
        for i in range(1, last_free + 1):
            f = i / n * dt
            self._px[i] -= vx * f
            self._py[i] -= vy * f

    def apply_force(self, fx: float, fy: float) -> None:
        """Add an extra acceleration (px/s^2) to all free nodes for the next step only."""
        if not _finite(fx, fy):
            logger.warning("Ignoring non-finite force (%r, %r)", fx, fy)
            return
        self._force_x += fx
        self._force_y += fy

    def relocate(self, anchor_x: float, anchor_y: float) -> None:
        """Move the anchor and translate the whole rope with it, keeping shape and velocity.

        Use this for re-anchoring (e.g. a screen geometry change). Assigning ``anchor_x`` /
        ``anchor_y`` directly instead makes the rope follow physically (it swings).
        """
        if not _finite(anchor_x, anchor_y):
            logger.warning("Ignoring relocate to non-finite anchor (%r, %r)", anchor_x, anchor_y)
            return
        self._translate(float(anchor_x) - self._x[0], float(anchor_y) - self._y[0])
        self.anchor_x, self.anchor_y = float(anchor_x), float(anchor_y)

    # ------------------------------------------------------------------ queries

    def end(self) -> tuple[float, float]:
        """Position of the charm end node."""
        return self._x[-1], self._y[-1]

    def end_angle(self) -> float:
        """Angle of the last segment in radians: 0 hanging straight down, + when the end is to +x."""
        n = self._n
        return math.atan2(self._x[n] - self._x[n - 1], self._y[n] - self._y[n - 1])

    def speed(self) -> float:
        """Fastest node speed over the last step, in px/s (0 when at rest)."""
        best = 0.0
        for x, y, px, py in zip(self._x, self._y, self._px, self._py, strict=True):
            dx = x - px
            dy = y - py
            d_sq = dx * dx + dy * dy
            if d_sq > best:
                best = d_sq
        return math.sqrt(best) / self.params.dt

    def settled(self, threshold: float = 1.5) -> bool:
        """True when the rope is at rest: not grabbed and ``speed() < threshold``.

        Speed alone would also be (almost) zero at the turning point of every swing, which would
        freeze a tilted charm when the caller stops animating. So the rope must additionally hang
        close to its equilibrium (straight down along gravity). The allowed sideways deviation is
        ``threshold / omega`` with ``omega = sqrt(|g| / length)`` - the amplitude of a swing whose
        peak speed is ``threshold`` - which keeps the criterion consistent for short and long ropes.
        """
        if self._grabbed or self.speed() >= threshold:
            return False
        gravity = self.params.gravity
        if gravity == 0.0:
            return True
        tolerance = threshold * math.sqrt(self._length / abs(gravity))
        xs = self._x
        pivot = xs[0]
        for x in xs:
            if abs(x - pivot) > tolerance:
                return False
        # A rope momentarily at rest while folded up (e.g. at the top of a vertical toss).
        drop = (self._y[-1] - self._y[0]) * (1.0 if gravity > 0.0 else -1.0)
        return drop >= 0.99 * self._length - tolerance

    def __repr__(self) -> str:
        ex, ey = self.end()
        return (
            f"Rope(anchor=({self.anchor_x:.1f}, {self.anchor_y:.1f}), length={self._length:.1f}, "
            f"end=({ex:.1f}, {ey:.1f}), grabbed={self._grabbed})"
        )

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _constraint_shares(inv: list[float]) -> tuple[tuple[float, ...], tuple[float, ...]]:
        share_a: list[float] = []
        share_b: list[float] = []
        for wa, wb in itertools.pairwise(inv):
            total = wa + wb
            share_a.append(wa / total if total > 0.0 else 0.0)
            share_b.append(wb / total if total > 0.0 else 0.0)
        return tuple(share_a), tuple(share_b)

    def _hang(self, offset: float) -> None:
        """Place all nodes on a straight line of exactly ``length`` from the anchor, at rest."""
        n = self._n
        length = self._length
        limit = 0.95 * length
        dx = max(-limit, min(limit, offset))
        dy = math.sqrt(max(0.0, length * length - dx * dx))
        for i in range(n + 1):
            t = i / n
            x = self.anchor_x + dx * t
            y = self.anchor_y + dy * t
            self._x[i] = self._px[i] = x
            self._y[i] = self._py[i] = y
        self._force_x = self._force_y = 0.0

    def _translate(self, dx: float, dy: float) -> None:
        for i in range(self._n + 1):
            self._x[i] += dx
            self._px[i] += dx
            self._y[i] += dy
            self._py[i] += dy
        if self._grabbed:
            self._target_x += dx
            self._target_y += dy

    def _drive_grabbed_end(self, ax: float, ay: float, dt: float) -> None:
        n = self._n
        tx, ty = self._target_x, self._target_y
        dist = math.hypot(tx - ax, ty - ay)
        if dist > self._length:
            scale = self._length / dist
            tx = ax + (tx - ax) * scale
            ty = ay + (ty - ay) * scale

        inst_vx = (tx - self._x[n]) / dt
        inst_vy = (ty - self._y[n]) / dt
        gvx = self._grab_vx + (inst_vx - self._grab_vx) * _GRAB_SMOOTHING
        gvy = self._grab_vy + (inst_vy - self._grab_vy) * _GRAB_SMOOTHING
        g_speed = math.hypot(gvx, gvy)
        if g_speed > _MAX_GRAB_SPEED:
            gvx *= _MAX_GRAB_SPEED / g_speed
            gvy *= _MAX_GRAB_SPEED / g_speed
        self._grab_vx, self._grab_vy = gvx, gvy

        # Exact position, smoothed Verlet velocity: release() then keeps a sensible momentum.
        self._x[n], self._y[n] = tx, ty
        self._px[n] = tx - gvx * dt
        self._py[n] = ty - gvy * dt


class Simulation:
    """A set of ropes advanced together with a fixed-timestep accumulator."""

    def __init__(self, params: PhysicsParams | None = None) -> None:
        self.params: PhysicsParams = params if params is not None else PhysicsParams()
        self.ropes: list[Rope] = []
        self._accumulator = 0.0

    def add(self, rope: Rope) -> None:
        """Add ``rope`` (no-op if it is already part of the simulation)."""
        if not any(r is rope for r in self.ropes):
            self.ropes.append(rope)

    def remove(self, rope: Rope) -> None:
        """Remove ``rope`` (no-op if it is not part of the simulation)."""
        self.ropes[:] = [r for r in self.ropes if r is not rope]

    def advance(self, elapsed: float) -> int:
        """Accumulate ``elapsed`` real seconds and run the due fixed steps; return how many ran.

        At most ``params.max_substeps`` steps run per call; time beyond that is dropped so a
        stall (sleep, debugger, busy system) never turns into a spiral of death.
        """
        try:
            seconds = float(elapsed)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid elapsed time %r", elapsed)
            return 0
        if not math.isfinite(seconds) or seconds < 0.0:
            logger.debug("Ignoring non-positive/non-finite elapsed time %r", elapsed)
            seconds = 0.0

        dt = self.params.dt
        max_steps = self.params.max_substeps
        self._accumulator += seconds
        steps = int((self._accumulator + _TIME_EPS) / dt)
        if steps > max_steps:
            logger.debug(
                "Physics fell behind by %.3f s; dropping time beyond %d substeps",
                self._accumulator - max_steps * dt,
                max_steps,
            )
            steps = max_steps
            self._accumulator = 0.0
        else:
            self._accumulator = max(0.0, self._accumulator - steps * dt)

        for _ in range(steps):
            for rope in tuple(self.ropes):
                rope.step()
        return steps

    def settled(self) -> bool:
        """True when every rope is settled (vacuously true with no ropes)."""
        return all(rope.settled() for rope in self.ropes)

    def reset_clock(self) -> None:
        """Forget accumulated time (call when resuming from idle)."""
        self._accumulator = 0.0
