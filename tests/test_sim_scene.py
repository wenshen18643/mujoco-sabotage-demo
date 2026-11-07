"""(a) Scene: it loads, all three cameras render non-empty images, and the
gripper/TCP facts the rest of the demo relies on hold."""

from __future__ import annotations

import mujoco
import numpy as np
import pytest

import sim_helpers  # noqa: F401  (puts demo/ on sys.path)
from sabotage_demo import scene
from sabotage_demo.ik import ArmIK


@pytest.fixture(scope="module")
def model() -> mujoco.MjModel:
    return scene.build_model()


def _home(model: mujoco.MjModel) -> mujoco.MjData:
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key("home").id)
    mujoco.mj_forward(model, data)
    return data


def test_scene_contents(model):
    for cam in scene.CAMERAS:
        assert model.camera(cam).id >= 0
    assert model.site("tcp").id >= 0
    assert (model.vis.global_.offwidth, model.vis.global_.offheight) == (960, 720)
    table = model.geom("table")
    assert table.pos[2] + table.size[2] == pytest.approx(0.0, abs=1e-9)  # top face at z = 0
    cube = model.geom("cube")
    np.testing.assert_allclose(cube.size, [0.02, 0.02, 0.02])
    assert model.body("cube").mass[0] == pytest.approx(0.05)
    zone = model.geom("target_zone")
    assert zone.contype[0] == 0 and zone.conaffinity[0] == 0
    np.testing.assert_allclose(zone.size, [0.06, 0.06, 0.001])
    data = _home(model)
    np.testing.assert_allclose(data.xpos[model.body("cube").id], scene.CUBE_START, atol=1e-6)
    np.testing.assert_allclose(data.xpos[model.body("target_zone").id], scene.ZONE_POS, atol=1e-6)
    assert model.actuator(scene.GRIPPER_ACTUATOR).ctrlrange.tolist() == [0.0, 255.0]


def test_cameras_render_non_empty(model):
    data = _home(model)
    with mujoco.Renderer(model, scene.RENDER_H, scene.RENDER_W) as renderer:
        images = {}
        for cam in scene.CAMERAS:
            renderer.update_scene(data, camera=cam)
            img = renderer.render()
            assert img.shape == (720, 960, 3) and img.dtype == np.uint8
            assert img.std() > 15, f"{cam} image looks blank"
            images[cam] = img.astype(int)
    r, g, b = (images["front"][..., i] for i in range(3))
    assert ((r > 120) & (r > 2 * g) & (r > 2 * b)).sum() > 100, "front camera must see the red cube"
    assert ((g > 100) & (g > 1.4 * r) & (g > 1.2 * b)).sum() > 1000, "front camera must see the green zone"


def test_gripper_ctrl_255_is_open_0_is_closed(model):
    data = _home(model)
    grip = model.actuator(scene.GRIPPER_ACTUATOR).id
    fingers = [model.jnt_qposadr[model.joint(n).id] for n in scene.FINGER_JOINTS]
    data.ctrl[grip] = scene.GRIPPER_CLOSED_CTRL
    mujoco.mj_step(model, data, nstep=1000)
    assert max(data.qpos[fingers]) < 0.003
    data.ctrl[grip] = scene.GRIPPER_OPEN_CTRL
    mujoco.mj_step(model, data, nstep=1000)
    assert min(data.qpos[fingers]) > 0.037


def test_top_down_quat_points_fingers_down(model):
    ik = ArmIK(model)
    res = ik.solve([0.5, 0.0, 0.14], None, scene.HOME_Q, max_iters=300)
    assert res.pos_error < 1e-3 and res.rot_error < 1e-2
    data = ik.data
    rot = data.site_xmat[model.site("tcp").id].reshape(3, 3)
    np.testing.assert_allclose(rot[:, 2], [0, 0, -1], atol=1e-2)  # hand z = world -z
    np.testing.assert_allclose(rot[:, 0], [1, 0, 0], atol=1e-2)  # hand x = world x
