"""IK and trajectory quality: a 20 cm move over 2 s is straight, smooth and
accurate; excluded joints are held."""

from __future__ import annotations

import mujoco
import numpy as np

import sim_helpers  # noqa: F401  (puts demo/ on sys.path)
from sabotage_demo import scene
from sabotage_demo.ik import ArmDriver, ArmIK


def _setup():
    model = scene.build_model()
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key("home").id)
    mujoco.mj_forward(model, data)
    ik = ArmIK(model)
    return model, data, ik, ArmDriver(model, data, ik)


def _tcp_pose(model, data):
    sid = model.site("tcp").id
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, data.site_xmat[sid])
    return data.site_xpos[sid].copy(), quat


def test_20cm_move_in_2s_is_straight_smooth_and_accurate():
    model, data, ik, drv = _setup()
    start, goal = np.array([0.42, 0.10, 0.20]), np.array([0.42, -0.10, 0.20])
    drv.execute_cartesian(start, None, 2.0, (), lambda n: mujoco.mj_step(model, data, nstep=n),
                          _tcp_pose(model, data))
    tcp, joints, sid = [], [], model.site("tcp").id

    def step(n: int) -> None:
        for _ in range(n):
            mujoco.mj_step(model, data)
            tcp.append(data.site_xpos[sid].copy())
            joints.append(drv.q_actual())

    drv.execute_cartesian(goal, None, 2.0, (), step, _tcp_pose(model, data))
    mujoco.mj_forward(model, data)
    tcp, joints = np.array(tcp), np.array(joints)
    dt = model.opt.timestep

    final_err = np.linalg.norm(data.site_xpos[sid] - goal)
    assert final_err < 0.015, final_err
    assert final_err < 0.002  # in practice sub-millimetre

    direction = (goal - start) / np.linalg.norm(goal - start)
    rel = tcp - start
    off_line = np.linalg.norm(rel - np.outer(rel @ direction, direction), axis=1)
    assert off_line.max() < 0.005, f"path deviates {off_line.max():.4f} m from the straight line"

    speed = np.linalg.norm(np.diff(tcp, axis=0), axis=1) / dt
    assert speed.max() < 0.3  # min-jerk peak for 0.2 m in 2 s is 0.1875 m/s
    assert speed[:10].max() < 0.02 and speed[-10:].max() < 0.02  # starts and ends at rest
    qd = np.abs(np.diff(joints, axis=0)) / dt
    assert qd.max() < 1.0, f"joint speed spike {qd.max():.2f} rad/s"
    qdd = np.abs(np.diff(qd, axis=0)) / dt
    assert np.percentile(qdd, 99) < 20.0, "joint acceleration spikes (snapping)"
    _, quat = _tcp_pose(model, data)
    assert abs(abs(np.dot(quat, scene.TOP_DOWN_QUAT)) - 1.0) < 1e-3


def test_excluded_joints_hold_their_angle():
    model, _, ik, _ = _setup()
    q0 = ik.solve([0.5, 0.0, 0.14], None, scene.HOME_Q, max_iters=300).q
    res = ik.solve([0.5, -0.25, 0.20], None, q0, excluded=[0], max_iters=300)
    assert res.q[0] == q0[0]
    assert res.pos_error < 1e-3
    free = ik.solve([0.5, -0.25, 0.20], None, q0, max_iters=300)
    assert abs(free.q[0] - q0[0]) > 0.1  # the unconstrained solution does use joint 1
