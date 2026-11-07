"""Damped least-squares IK and smooth trajectories for the Panda ``tcp`` site.

``ArmIK`` solves position + orientation IK on the 7 arm joints with joint-limit
clamping, a null-space pull towards a natural posture, and optional excluded
joints (their Jacobian columns are zeroed, so they hold their current angle).

``execute_cartesian`` and ``execute_joint`` turn a goal into a stream of
position-actuator commands: minimum-jerk timing, a straight Cartesian line
(slerp for orientation) with IK at every waypoint, velocity feed-forward so the
servos track without lag, then a short settle. Physics is advanced through a
caller-supplied ``step`` callback, so the simulator can apply faults, record
frames and evaluate triggers between steps.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import mujoco
import numpy as np

from .scene import ARM_ACTUATORS, ARM_JOINTS, TOP_DOWN_QUAT

# Natural top-down working posture used as the null-space attractor: elbow up,
# no swivel (joints 1, 3, 5 at zero). Joint 7 is not pulled (weight 0) because
# its value is fixed by the requested yaw.
REST_Q = (0.0, 0.2, 0.0, -2.3, 0.0, 2.5, 0.785)
REST_WEIGHTS = (1.0, 0.3, 1.0, 0.3, 1.0, 0.3, 0.0)

StepFn = Callable[[int], None]  # advance physics by n mj_steps


# ---------------------------------------------------------------------------
# Quaternion helpers (MuJoCo order w, x, y, z)
# ---------------------------------------------------------------------------

def quat_normalize(q: Sequence[float]) -> np.ndarray:
    """Return ``q`` as a unit quaternion (raises on a zero quaternion)."""
    q = np.asarray(q, dtype=float)
    n = np.linalg.norm(q)
    if not np.isfinite(n) or n < 1e-9:
        raise ValueError("quaternion has zero length")
    return q / n


def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Hamilton product ``a * b``."""
    out = np.zeros(4)
    mujoco.mju_mulQuat(out, np.asarray(a, float), np.asarray(b, float))
    return out


def quat_conj(q: np.ndarray) -> np.ndarray:
    """Conjugate (inverse of a unit quaternion)."""
    return np.array([q[0], -q[1], -q[2], -q[3]])


def quat_error(target: np.ndarray, current: np.ndarray) -> np.ndarray:
    """World-frame rotation vector that rotates ``current`` onto ``target``."""
    qe = quat_mul(target, quat_conj(current))
    if qe[0] < 0:
        qe = -qe
    v = qe[1:]
    s = np.linalg.norm(v)
    if s < 1e-12:
        return np.zeros(3)
    return 2.0 * np.arctan2(s, qe[0]) * v / s


def quat_slerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    """Spherical interpolation along the shortest arc."""
    a, b = quat_normalize(a), quat_normalize(b)
    dot = float(np.dot(a, b))
    if dot < 0:
        b, dot = -b, -dot
    if dot > 0.9995:
        return quat_normalize(a + t * (b - a))
    theta = np.arccos(np.clip(dot, -1.0, 1.0))
    return (np.sin((1 - t) * theta) * a + np.sin(t * theta) * b) / np.sin(theta)


def min_jerk(s: float) -> float:
    """Minimum-jerk time scaling: 0 -> 1 with zero velocity and acceleration at both ends."""
    s = min(max(s, 0.0), 1.0)
    return s * s * s * (10.0 + s * (-15.0 + 6.0 * s))


def min_jerk_rate(s: float) -> float:
    """Derivative of ``min_jerk`` with respect to ``s``."""
    s = min(max(s, 0.0), 1.0)
    return 30.0 * s * s * (1.0 - s) * (1.0 - s)


# ---------------------------------------------------------------------------
# IK
# ---------------------------------------------------------------------------

@dataclass
class IKConfig:
    """Tuning knobs for ``ArmIK``."""

    damping: float = 0.03  # DLS lambda (in the weighted task space)
    rot_weight: float = 0.4  # metres of error per radian of orientation error
    max_step: float = 0.15  # rad, per-iteration joint step limit (2-norm)
    nullspace_gain: float = 0.02  # pull towards the rest posture per iteration
    limit_margin: float = 0.02  # rad kept away from the joint limits
    pos_tol: float = 5e-5
    rot_tol: float = 1e-3


@dataclass
class IKResult:
    """Outcome of an IK solve."""

    q: np.ndarray
    pos_error: float
    rot_error: float
    iterations: int


class ArmIK:
    """Damped least-squares IK for site ``tcp`` on the 7 Panda arm joints.

    Uses its own ``MjData`` scratch copy, so solving never disturbs the simulation.
    """

    def __init__(self, model: mujoco.MjModel, site: str = "tcp",
                 rest_q: Sequence[float] = REST_Q, config: IKConfig | None = None):
        self.model = model
        self.data = mujoco.MjData(model)
        self.cfg = config or IKConfig()
        self.site_id = model.site(site).id
        jids = [model.joint(n).id for n in ARM_JOINTS]
        self.qadr = np.array([model.jnt_qposadr[j] for j in jids])
        self.dadr = np.array([model.jnt_dofadr[j] for j in jids])
        rng = np.array([model.jnt_range[j] for j in jids])
        self.lower = rng[:, 0] + self.cfg.limit_margin
        self.upper = rng[:, 1] - self.cfg.limit_margin
        self.rest_q = np.asarray(rest_q, dtype=float)
        self.rest_w = np.asarray(REST_WEIGHTS, dtype=float)
        self._jacp = np.zeros((3, model.nv))
        self._jacr = np.zeros((3, model.nv))

    def fk(self, q: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
        """Site position and quaternion for arm configuration ``q``."""
        self.data.qpos[self.qadr] = q
        mujoco.mj_kinematics(self.model, self.data)
        pos = self.data.site_xpos[self.site_id].copy()
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, self.data.site_xmat[self.site_id])
        return pos, quat

    def _jacobian(self) -> np.ndarray:
        mujoco.mj_comPos(self.model, self.data)
        mujoco.mj_jacSite(self.model, self.data, self._jacp, self._jacr, self.site_id)
        return np.vstack([self._jacp[:, self.dadr], self.cfg.rot_weight * self._jacr[:, self.dadr]])

    def solve(self, pos: Sequence[float], quat: Sequence[float] | None, q_init: Sequence[float],
              excluded: Sequence[int] = (), max_iters: int = 100,
              nullspace: bool = True) -> IKResult:
        """Solve for a joint vector reaching ``pos``/``quat``, starting from ``q_init``.

        Excluded joints keep their ``q_init`` value. ``quat=None`` means
        ``TOP_DOWN_QUAT``. Returns the best iterate even if it did not converge.
        """
        cfg = self.cfg
        target_p = np.asarray(pos, dtype=float)
        target_q = quat_normalize(TOP_DOWN_QUAT if quat is None else quat)
        q = np.clip(np.asarray(q_init, dtype=float).copy(), self.lower, self.upper)
        excl = np.zeros(7, dtype=bool)
        for j in excluded:
            excl[int(j)] = True
        q[excl] = np.asarray(q_init, dtype=float)[excl]  # held exactly, even outside the margin
        free = ~excl
        lam2 = cfg.damping ** 2
        best = None
        it = 0
        for it in range(1, max_iters + 1):
            cur_p, cur_q = self.fk(q)
            e_pos = target_p - cur_p
            e_rot = quat_error(target_q, cur_q)
            perr, rerr = float(np.linalg.norm(e_pos)), float(np.linalg.norm(e_rot))
            score = perr + cfg.rot_weight * rerr
            if best is None or score < best[0]:
                best = (score, q.copy(), perr, rerr)
            if perr < cfg.pos_tol and rerr < cfg.rot_tol:
                break
            jac = self._jacobian()
            jac[:, excl] = 0.0
            err = np.concatenate([e_pos, cfg.rot_weight * e_rot])
            jjt = jac @ jac.T + lam2 * np.eye(6)
            pinv = jac.T @ np.linalg.solve(jjt, np.eye(6))
            dq = pinv @ err
            if nullspace and cfg.nullspace_gain > 0:
                null = np.eye(7) - pinv @ jac
                dq += null @ (cfg.nullspace_gain * self.rest_w * (self.rest_q - q))
            dq[excl] = 0.0
            n = np.linalg.norm(dq)
            if n > cfg.max_step:
                dq *= cfg.max_step / n
            q[free] = np.clip(q + dq, self.lower, self.upper)[free]
        _, q_best, perr, rerr = best
        return IKResult(q=q_best, pos_error=perr, rot_error=rerr, iterations=it)


# ---------------------------------------------------------------------------
# Trajectory execution
# ---------------------------------------------------------------------------

@dataclass
class MoveReport:
    """What a trajectory execution did (filled by ``execute_*``)."""

    q_cmd_final: np.ndarray
    ik_pos_error: float = 0.0
    ik_rot_error: float = 0.0
    max_abs_vel: np.ndarray = field(default_factory=lambda: np.zeros(7))
    steps: int = 0
    duration_s: float = 0.0  # effective trajectory duration (may exceed the request)
    flipped: bool = False  # goal turned 180 deg about the tool axis (equivalent grasp)


class ArmDriver:
    """Drives the arm's position actuators along smooth trajectories."""

    WAYPOINT_DT = 0.01  # s between IK waypoints (5 physics steps at 2 ms)
    SETTLE_MAX_S = 0.5
    SETTLE_MIN_S = 0.12
    SETTLE_VEL = 0.01  # rad/s
    # Speed limits. A minimum-jerk profile peaks at 1.875x its average speed, so a
    # requested duration that would exceed these peaks is stretched.
    MAX_TCP_SPEED = 0.5  # m/s
    MAX_TCP_ROT_SPEED = 1.5  # rad/s
    MAX_JOINT_SPEED = 1.5  # rad/s, joint-space moves
    MAX_JOINT_STEP_SPEED = 2.0  # rad/s, hard cap on commanded joint speed per waypoint
    FLIP_ANGLE = 2.1  # rad: beyond this, use the symmetric grasp orientation instead
    CATCH_UP_MAX_S = 2.0
    MIN_JERK_PEAK = 1.875

    def __init__(self, model: mujoco.MjModel, data: mujoco.MjData, ik: ArmIK):
        self.model = model
        self.data = data
        self.ik = ik
        self.act_ids = np.array([model.actuator(n).id for n in ARM_ACTUATORS])
        bias = model.actuator_biasprm[self.act_ids]
        # position servo: force = kp * (ctrl - q) - kd * qdot  ->  feed-forward kd/kp * qdot_des
        self.ff = np.where(bias[:, 1] != 0, bias[:, 2] / np.where(bias[:, 1] != 0, bias[:, 1], 1), 0.0)
        self.ctrl_lo = model.actuator_ctrlrange[self.act_ids, 0]
        self.ctrl_hi = model.actuator_ctrlrange[self.act_ids, 1]
        self.q_cmd = self.q_actual()

    def q_actual(self) -> np.ndarray:
        """Current arm joint angles."""
        return self.data.qpos[self.ik.qadr].copy()

    def qvel(self) -> np.ndarray:
        """Current arm joint velocities."""
        return self.data.qvel[self.ik.dadr].copy()

    def command(self, q_des: np.ndarray, qd_des: np.ndarray | None = None) -> None:
        """Write a joint target (plus optional velocity feed-forward) to the servos."""
        self.q_cmd = np.asarray(q_des, dtype=float).copy()
        ctrl = self.q_cmd if qd_des is None else self.q_cmd + self.ff * qd_des
        self.data.ctrl[self.act_ids] = np.clip(ctrl, self.ctrl_lo, self.ctrl_hi)

    def _steps_per_waypoint(self) -> int:
        return max(1, int(round(self.WAYPOINT_DT / self.model.opt.timestep)))

    def _track(self, report: MoveReport, step: StepFn, n: int) -> None:
        step(n)
        report.steps += n
        report.max_abs_vel = np.maximum(report.max_abs_vel, np.abs(self.qvel()))

    def settle(self, step: StepFn, report: MoveReport) -> None:
        """Hold the final command until the arm stops moving (bounded time)."""
        n = self._steps_per_waypoint()
        dt = n * self.model.opt.timestep
        t = 0.0
        while t < self.SETTLE_MAX_S:
            self._track(report, step, n)
            t += dt
            if t >= self.SETTLE_MIN_S and np.max(np.abs(self.qvel())) < self.SETTLE_VEL:
                break

    def execute_cartesian(self, pos: Sequence[float], quat: Sequence[float] | None,
                          duration_s: float, excluded: Sequence[int], step: StepFn,
                          start_pose: tuple[np.ndarray, np.ndarray]) -> MoveReport:
        """Straight-line TCP motion to ``pos``/``quat`` with IK at every waypoint.

        ``start_pose`` is the actual TCP (pos, quat) at the start. Excluded joints are
        held at their current actual angle.
        """
        goal_p = np.asarray(pos, dtype=float)
        goal_q = quat_normalize(TOP_DOWN_QUAT if quat is None else quat)
        p0, q0 = start_pose
        if np.dot(q0, goal_q) < 0:
            goal_q = -goal_q
        # A parallel gripper looks the same turned 180 deg about its axis; prefer that
        # over spinning the wrist half a turn.
        flipped = False
        alt = quat_mul(goal_q, np.array([0.0, 0.0, 0.0, 1.0]))
        if np.linalg.norm(quat_error(goal_q, q0)) > self.FLIP_ANGLE and                 np.linalg.norm(quat_error(alt, q0)) < np.linalg.norm(quat_error(goal_q, q0)):
            goal_q, flipped = alt, True
        # Plan from the actual configuration so the first command is where the arm is
        # (after a blocked or faulty move the old command may be far away).
        q_prev = self.q_actual()
        excl = sorted({int(j) for j in excluded})
        dist = float(np.linalg.norm(goal_p - p0))
        angle = float(np.linalg.norm(quat_error(goal_q, q0)))
        duration_s = max(duration_s, self.MIN_JERK_PEAK * dist / self.MAX_TCP_SPEED,
                         self.MIN_JERK_PEAK * angle / self.MAX_TCP_ROT_SPEED)

        n_wp = max(1, int(round(duration_s / self.WAYPOINT_DT)))
        n_sub = self._steps_per_waypoint()
        report = MoveReport(q_cmd_final=q_prev.copy(), duration_s=duration_s, flipped=flipped)
        dt = n_sub * self.model.opt.timestep
        max_dq = self.MAX_JOINT_STEP_SPEED * dt
        q_goal = q_prev
        for k in range(1, n_wp + 1):
            s = min_jerk(k / n_wp)
            wp_p = p0 + s * (goal_p - p0)
            wp_q = quat_slerp(q0, goal_q, s)
            last = k == n_wp
            res = self.ik.solve(wp_p, wp_q, q_prev, excluded=excl, max_iters=200 if last else 8)
            q_goal = res.q
            q_next = q_prev + np.clip(q_goal - q_prev, -max_dq, max_dq)  # never jump
            self.command(q_next, None if last else (q_next - q_prev) / dt)
            q_prev = q_next
            report.ik_pos_error, report.ik_rot_error = res.pos_error, res.rot_error
            self._track(report, step, n_sub)
        # If the speed cap held the command back, finish the approach at the capped rate.
        t = 0.0
        while np.max(np.abs(q_goal - q_prev)) > 1e-4 and t < self.CATCH_UP_MAX_S:
            q_next = q_prev + np.clip(q_goal - q_prev, -max_dq, max_dq)
            self.command(q_next)
            q_prev = q_next
            self._track(report, step, n_sub)
            t += dt
        self.command(q_prev)
        self.settle(step, report)
        report.q_cmd_final = q_prev.copy()
        return report

    def execute_joint(self, q_goal: Sequence[float], duration_s: float, step: StepFn) -> MoveReport:
        """Minimum-jerk joint-space motion to ``q_goal`` (clamped to the limits)."""
        q_goal = np.clip(np.asarray(q_goal, dtype=float), self.ik.lower, self.ik.upper)
        q_start = self.q_actual()
        duration_s = max(duration_s, self.MIN_JERK_PEAK * float(np.max(np.abs(q_goal - q_start)))
                         / self.MAX_JOINT_SPEED)
        n_wp = max(1, int(round(duration_s / self.WAYPOINT_DT)))
        n_sub = self._steps_per_waypoint()
        report = MoveReport(q_cmd_final=q_goal.copy(), duration_s=duration_s)
        for k in range(1, n_wp + 1):
            u = k / n_wp
            q_des = q_start + min_jerk(u) * (q_goal - q_start)
            qd_des = (q_goal - q_start) * min_jerk_rate(u) / duration_s
            self.command(q_des, None if k == n_wp else qd_des)
            self._track(report, step, n_sub)
        self.command(q_goal)
        self.settle(step, report)
        return report
