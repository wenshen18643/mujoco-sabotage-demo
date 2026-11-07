"""MJCF scene for the sabotage demo: Menagerie Panda, table, red cube, green zone.

The scene is composed with ``mujoco.MjSpec``: ``panda.xml`` is loaded from the
Menagerie checkout (its mesh directory is pinned to an absolute path) and the
table, cube, target zone, lights and cameras are added programmatically.

Run ``python -m sabotage_demo.scene --out <dir>`` (cwd = demo/) to render the
three cameras to PNG and write the composed XML for inspection.

Gripper facts (verified against panda.xml): ``actuator8`` drives the fixed
tendon ``split`` = 0.5 * (finger_joint1 + finger_joint2) with gain
0.01568627 and bias [0, -100, -10]. Its equilibrium tendon length is
``ctrl * 0.04 / 255``, so ctrl 255 = fully open (each finger 0.04 m, 0.083 m
between the pads) and ctrl 0 = closed. When the fingers are blocked by the
4 cm cube the actuator squeezes with roughly 100 N/m * 0.0185 m = 1.85 N on the
tendon, i.e. about 0.9 N per finger.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import mujoco
import numpy as np

MENAGERIE_PATH = Path(
    os.environ.get("MUJOCO_MENAGERIE_PATH", r"C:\Users\Shen\projects\mujoco_menagerie")
)
PANDA_XML = MENAGERIE_PATH / "franka_emika_panda" / "panda.xml"

RENDER_W, RENDER_H = 960, 720
CAMERAS = ("front", "top", "wrist")

ARM_JOINTS = tuple(f"joint{i}" for i in range(1, 8))
ARM_ACTUATORS = tuple(f"actuator{i}" for i in range(1, 8))
FINGER_JOINTS = ("finger_joint1", "finger_joint2")
GRIPPER_ACTUATOR = "actuator8"
GRIPPER_OPEN_CTRL = 255.0  # tendon target 0.04 m (fingers fully open)
GRIPPER_CLOSED_CTRL = 0.0  # tendon target 0 m (fingers closed)
FINGER_BODIES = ("left_finger", "right_finger")

# Home keyframe of panda.xml (arm part). TCP sits at about [0.554, 0, 0.521].
HOME_Q = (0.0, 0.0, 0.0, -1.57079, 0.0, 1.57079, -0.7853)

TCP_OFFSET = 0.1034  # m along the hand z axis, centre of the fingertip pads
# Fingers straight down (hand z = world -z), hand x = world x: a 180 degree
# rotation about world x. The fingers then close along world y, which the
# front camera sees side-on.
TOP_DOWN_QUAT = (0.0, 1.0, 0.0, 0.0)

CUBE_HALF = 0.02
CUBE_MASS = 0.05
CUBE_START = (0.50, 0.00, 0.02)
CUBE_RGBA = (0.86, 0.07, 0.06, 1.0)
ZONE_POS = (0.50, -0.25, 0.001)
ZONE_HALF = (0.06, 0.06, 0.001)
ZONE_RGBA = (0.10, 0.72, 0.22, 1.0)
TABLE_HEIGHT = 0.76

# Fixed cameras: (position, look-at point, vertical field of view in degrees).
FRONT_CAM = ((1.45, -0.80, 0.86), (0.30, -0.14, 0.33), 39.0)  # integration: ~11% tighter, lift poses and home stay in frame
TOP_CAM_POS = (0.5, -0.1, 1.2)
TOP_CAM_FOVY = 45.0
WRIST_CAM = ((0.065, 0.0, 0.035), (0.0, 0.0, 0.25), 65.0)  # in the hand frame


def lookat_quat(pos, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """Quaternion (w, x, y, z) of a MuJoCo camera at ``pos`` looking at ``target``.

    MuJoCo cameras look along their local -z axis with +y up in the image.
    """
    pos, target, up = (np.asarray(v, dtype=float) for v in (pos, target, up))
    fwd = target - pos
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    if np.linalg.norm(right) < 1e-9:  # looking along `up`: pick any right vector
        right = np.cross(fwd, (1.0, 0.0, 0.0))
    right /= np.linalg.norm(right)
    cam_up = np.cross(right, fwd)
    mat = np.column_stack([right, cam_up, -fwd])  # columns: camera x, y, z in parent
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, mat.reshape(-1))
    return quat


def _add_textures(spec: mujoco.MjSpec) -> None:
    sky = spec.add_texture(name="sky", type=mujoco.mjtTexture.mjTEXTURE_SKYBOX)
    sky.builtin = mujoco.mjtBuiltin.mjBUILTIN_GRADIENT
    sky.rgb1 = [0.92, 0.94, 0.97]
    sky.rgb2 = [0.42, 0.47, 0.55]
    sky.width, sky.height = 512, 3072

    floor = spec.add_texture(name="floor_tex", type=mujoco.mjtTexture.mjTEXTURE_2D)
    floor.builtin = mujoco.mjtBuiltin.mjBUILTIN_CHECKER
    floor.rgb1 = [0.36, 0.38, 0.41]
    floor.rgb2 = [0.31, 0.33, 0.36]
    floor.width, floor.height = 512, 512
    floor.mark = mujoco.mjtMark.mjMARK_EDGE
    floor.markrgb = [0.42, 0.44, 0.47]

    wood = spec.add_texture(name="table_tex", type=mujoco.mjtTexture.mjTEXTURE_2D)
    wood.builtin = mujoco.mjtBuiltin.mjBUILTIN_FLAT
    wood.rgb1 = [0.74, 0.74, 0.73]
    wood.rgb2 = [0.71, 0.71, 0.70]
    wood.random = 0.02
    wood.width, wood.height = 256, 256

    def material(name, tex=None, rgba=(1, 1, 1, 1), repeat=(1, 1), refl=0.0, spec_=0.3, shin=0.3,
                 uniform=False):
        mat = spec.add_material(name=name)
        mat.rgba = list(rgba)
        mat.reflectance = refl
        mat.specular = spec_
        mat.shininess = shin
        mat.texrepeat = list(repeat)
        mat.texuniform = uniform
        if tex:
            mat.textures[mujoco.mjtTextureRole.mjTEXROLE_RGB] = tex
        return mat

    material("floor_mat", "floor_tex", repeat=(4, 4), refl=0.08, uniform=True)
    material("table_mat", "table_tex", repeat=(1, 1), refl=0.05, spec_=0.25, shin=0.4)
    material("leg_mat", rgba=(0.30, 0.31, 0.33, 1), spec_=0.6, shin=0.6)
    material("cube_mat", rgba=CUBE_RGBA, spec_=0.25, shin=0.35)
    material("zone_mat", rgba=ZONE_RGBA, spec_=0.1, shin=0.1)


def _add_lights(spec: mujoco.MjSpec) -> None:
    wb = spec.worldbody
    key = wb.add_light(name="key", type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    key.pos = [0.9, 0.6, 2.5]
    key.dir = list(-np.array([0.45, 0.35, 1.0]) / np.linalg.norm([0.45, 0.35, 1.0]))
    key.diffuse = [0.55, 0.55, 0.53]
    key.specular = [0.25, 0.25, 0.25]
    key.ambient = [0.0, 0.0, 0.0]
    key.castshadow = True

    fill = wb.add_light(name="fill", type=mujoco.mjtLightType.mjLIGHT_DIRECTIONAL)
    fill.pos = [2.0, -1.5, 1.5]
    fill.dir = list(-np.array([1.0, -0.8, 0.6]) / np.linalg.norm([1.0, -0.8, 0.6]))
    fill.diffuse = [0.20, 0.21, 0.23]
    fill.specular = [0.05, 0.05, 0.05]
    fill.castshadow = False

    top = wb.add_light(name="top", type=mujoco.mjtLightType.mjLIGHT_SPOT)
    top.pos = [0.45, -0.1, 2.2]
    top.dir = [0.0, 0.0, -1.0]
    top.cutoff = 45.0
    top.exponent = 2.0
    top.diffuse = [0.12, 0.12, 0.12]
    top.specular = [0.0, 0.0, 0.0]
    top.castshadow = False


def _add_table(spec: mujoco.MjSpec) -> None:
    wb = spec.worldbody
    floor = wb.add_geom(name="floor", type=mujoco.mjtGeom.mjGEOM_PLANE)
    floor.size = [0, 0, 0.05]
    floor.pos = [0, 0, -TABLE_HEIGHT]
    floor.material = "floor_mat"

    x0, x1, y0, y1 = -0.45, 1.05, -0.85, 0.65  # table top extent
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy, ht = (x1 - x0) / 2, (y1 - y0) / 2, 0.025
    top = wb.add_geom(name="table", type=mujoco.mjtGeom.mjGEOM_BOX)
    top.size = [hx, hy, ht]
    top.pos = [cx, cy, -ht]  # top face exactly at z = 0
    top.material = "table_mat"
    top.friction = [1.0, 0.005, 0.0001]

    leg_h = (TABLE_HEIGHT - 2 * ht) / 2
    for i, (lx, ly) in enumerate([(x0 + 0.06, y0 + 0.06), (x0 + 0.06, y1 - 0.06),
                                  (x1 - 0.06, y0 + 0.06), (x1 - 0.06, y1 - 0.06)]):
        leg = wb.add_geom(name=f"table_leg{i}", type=mujoco.mjtGeom.mjGEOM_BOX)
        leg.size = [0.025, 0.025, leg_h]
        leg.pos = [lx, ly, -2 * ht - leg_h]
        leg.material = "leg_mat"
        leg.contype = 0
        leg.conaffinity = 0


def _add_objects(spec: mujoco.MjSpec) -> None:
    wb = spec.worldbody
    cube = wb.add_body(name="cube", pos=list(CUBE_START))
    cube.add_freejoint(name="cube_free")
    g = cube.add_geom(name="cube", type=mujoco.mjtGeom.mjGEOM_BOX)
    g.size = [CUBE_HALF] * 3
    g.mass = CUBE_MASS
    g.material = "cube_mat"
    g.friction = [1.5, 0.02, 0.002]
    g.condim = 4  # torsional friction keeps the cube from swinging between the pads
    g.priority = 1  # its friction wins over the default fingertip friction

    zone = wb.add_body(name="target_zone", pos=list(ZONE_POS))
    z = zone.add_geom(name="target_zone", type=mujoco.mjtGeom.mjGEOM_BOX)
    z.size = list(ZONE_HALF)
    z.material = "zone_mat"
    z.contype = 0
    z.conaffinity = 0
    z.mass = 0.0


def _add_cameras(spec: mujoco.MjSpec) -> None:
    wb = spec.worldbody
    pos, target, fovy = FRONT_CAM
    front = wb.add_camera(name="front", pos=list(pos), quat=list(lookat_quat(pos, target)))
    front.fovy = fovy

    # Straight down; image up = world +x (away from the robot), image right = world -y.
    top_quat = np.zeros(4)
    mat = np.column_stack([(0.0, -1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)])
    mujoco.mju_mat2Quat(top_quat, mat.reshape(-1))
    top = wb.add_camera(name="top", pos=list(TOP_CAM_POS), quat=list(top_quat))
    top.fovy = TOP_CAM_FOVY

    hand = spec.body("hand")
    wpos, wtarget, wfovy = WRIST_CAM
    wrist = hand.add_camera(name="wrist", pos=list(wpos),
                            quat=list(lookat_quat(wpos, wtarget, up=(1.0, 0.0, 0.0))))
    wrist.fovy = wfovy


def build_spec(panda_xml: Path = PANDA_XML) -> mujoco.MjSpec:
    """Return the composed scene as an (uncompiled) ``MjSpec``."""
    spec = mujoco.MjSpec.from_file(str(panda_xml))
    spec.meshdir = str((Path(panda_xml).parent / "assets").resolve())
    spec.modelname = "sabotage_demo"

    spec.option.timestep = 0.002
    spec.option.cone = mujoco.mjtCone.mjCONE_ELLIPTIC
    spec.option.impratio = 10.0  # stiff friction: the grasp does not creep

    vis = spec.visual
    vis.global_.offwidth = RENDER_W
    vis.global_.offheight = RENDER_H
    vis.quality.shadowsize = 8192
    vis.quality.offsamples = 8
    vis.headlight.ambient = [0.20, 0.20, 0.22]
    vis.headlight.diffuse = [0.12, 0.12, 0.12]
    vis.headlight.specular = [0.0, 0.0, 0.0]
    vis.map.shadowclip = 1.2  # tighter shadow frustum = sharper shadows (integration)
    vis.map.znear = 0.005
    vis.rgba.haze = [0.80, 0.83, 0.87, 1.0]

    # A real Panda compensates gravity in its joint controllers; do the same so
    # position errors come from dynamics and faults, not from sag.
    for name in ("link1", "link2", "link3", "link4", "link5", "link6", "link7", "hand",
                 *FINGER_BODIES):
        spec.body(name).gravcomp = 1.0

    tcp = spec.body("hand").add_site(name="tcp", pos=[0, 0, TCP_OFFSET])
    tcp.size = [0.004, 0, 0]
    tcp.group = 5  # never rendered
    tcp.rgba = [1, 1, 0, 0]

    _add_textures(spec)
    _add_lights(spec)
    _add_table(spec)
    _add_objects(spec)
    _add_cameras(spec)

    # Keyframe "home" from panda.xml has qpos for the arm and fingers only; extend
    # it with the cube's free joint so resets put the cube back at its start.
    key = spec.key("home")
    key.qpos = list(HOME_Q) + [0.04, 0.04] + list(CUBE_START) + [1.0, 0.0, 0.0, 0.0]
    key.ctrl = list(HOME_Q) + [GRIPPER_OPEN_CTRL]
    return spec


def build_model(panda_xml: Path = PANDA_XML) -> mujoco.MjModel:
    """Compile the demo scene into an ``MjModel``."""
    return build_spec(panda_xml).compile()


def _main() -> None:
    from PIL import Image

    ap = argparse.ArgumentParser(description="Render the demo scene from its three cameras.")
    ap.add_argument("--out", default="scene_preview", help="output directory")
    ap.add_argument("--xml", action="store_true", help="also write the composed MJCF")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    spec = build_spec()
    model = spec.compile()
    data = mujoco.MjData(model)
    mujoco.mj_resetDataKeyframe(model, data, model.key("home").id)
    mujoco.mj_forward(model, data)
    with mujoco.Renderer(model, RENDER_H, RENDER_W) as renderer:
        for cam in CAMERAS:
            renderer.update_scene(data, camera=cam)
            Image.fromarray(renderer.render()).save(out / f"{cam}.png")
    if args.xml:
        (out / "scene.xml").write_text(spec.to_xml(), encoding="utf-8")
    print(f"wrote {', '.join(CAMERAS)} to {out.resolve()}")


if __name__ == "__main__":
    _main()
