"""Write a synthetic but contract-conformant run directory (builder C).

The fixture stands in for a real director run so ``video.py`` and the dashboard
can be developed and tested without MuJoCo or Claude Code. It writes
``runs/fixture_<name>/`` with:

    scenario.json  events.jsonl  frames/%06d.jpg  frames.jsonl  summary.json

Frames are 960x720 JPEGs (quality 85) of a crude perspective scene: grey table,
green target zone, red cube and a stick-figure arm, one frame per 0.05 s of
sim time during stepping calls, plus one at run start and one after every
sabotage, exactly as CONTRACT.md "Frames" describes.

Usage (cwd = demo/):

    python -m sabotage_demo.make_fixture_run --name demo
    python -m sabotage_demo.make_fixture_run --name frozen --sabotage freeze
    python -m sabotage_demo.make_fixture_run --name quick --short --time-scale 0.5
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DEMO_DIR = Path(__file__).resolve().parent.parent
RUNS_DIR = DEMO_DIR / "runs"

W, H = 960, 720
FRAME_DT = 0.05
WALL_T0 = 1762500000.0

CUBE_HALF = 0.02
ZONE_POS = np.array([0.50, -0.25, 0.001])
ZONE_HALF = 0.06
CUBE_START = np.array([0.50, 0.00, 0.02])
TCP_HOME = np.array([0.31, 0.00, 0.40])
HOVER_Z = 0.14
GRASP_Z = 0.025

TITLES = {
    "teleport": "Fixture: the cube moves mid-reach",
    "freeze": "Fixture: a joint dies mid-task",
    "tracker": "Fixture: blind grasp",
    "none": "Fixture: baseline",
}
TASK = "Put the red cube in the green target zone."


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("consola.ttf", "cour.ttf"):
        p = Path(r"C:\Windows\Fonts") / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default()


class _Camera:
    """Tiny pinhole camera looking at the workspace from +x, like `front`."""

    def __init__(self, pos=(1.30, 0.62, 0.78), target=(0.33, -0.08, 0.08), f=900.0):
        self.c = np.array(pos, float)
        fwd = np.array(target, float) - self.c
        self.fwd = fwd / np.linalg.norm(fwd)
        right = np.cross(self.fwd, [0.0, 0.0, 1.0])
        self.right = right / np.linalg.norm(right)
        self.up = np.cross(self.right, self.fwd)
        self.f = f

    def __call__(self, p) -> tuple[float, float]:
        d = np.asarray(p, float) - self.c
        z = max(1e-3, float(d @ self.fwd))
        return (W / 2 + self.f * float(d @ self.right) / z, H / 2 - self.f * float(d @ self.up) / z)

    def depth(self, p) -> float:
        return float((np.asarray(p, float) - self.c) @ self.fwd)


class _Painter:
    def __init__(self) -> None:
        self.cam = _Camera()
        self.font = _font(18)
        self.background = self._background()

    def _background(self) -> Image.Image:
        img = Image.new("RGB", (W, H))
        px = np.zeros((H, W, 3), np.uint8)
        grad = np.linspace(0, 1, H)[:, None]
        px[...] = (np.array([46, 52, 62]) * (1 - grad) + np.array([24, 26, 30]) * grad)[:, None, :].astype(np.uint8)
        img = Image.fromarray(px)
        d = ImageDraw.Draw(img)
        cam = self.cam
        table = [(-0.25, -0.75, 0), (1.0, -0.75, 0), (1.0, 0.75, 0), (-0.25, 0.75, 0)]
        d.polygon([cam(p) for p in table], fill=(176, 178, 182))
        # table front edge thickness
        edge = [(1.0, -0.75, 0), (1.0, 0.75, 0), (1.0, 0.75, -0.04), (1.0, -0.75, -0.04)]
        d.polygon([cam(p) for p in edge], fill=(120, 122, 126))
        # faint grid so motion is easy to see
        for gx in np.arange(0.0, 1.01, 0.1):
            d.line([cam((gx, -0.75, 0)), cam((gx, 0.75, 0))], fill=(164, 166, 170), width=1)
        for gy in np.arange(-0.7, 0.71, 0.1):
            d.line([cam((-0.25, gy, 0)), cam((1.0, gy, 0))], fill=(164, 166, 170), width=1)
        zx, zy, _ = ZONE_POS
        zone = [(zx - ZONE_HALF, zy - ZONE_HALF, 0.001), (zx + ZONE_HALF, zy - ZONE_HALF, 0.001),
                (zx + ZONE_HALF, zy + ZONE_HALF, 0.001), (zx - ZONE_HALF, zy + ZONE_HALF, 0.001)]
        d.polygon([cam(p) for p in zone], fill=(40, 170, 80), outline=(20, 110, 50))
        # robot base
        bx, by = cam((0, 0, 0))
        d.ellipse([bx - 46, by - 16, bx + 46, by + 16], fill=(60, 62, 68))
        return img

    def _cube(self, d: ImageDraw.ImageDraw, c) -> None:
        cam = self.cam
        h = CUBE_HALF
        x, y, z = c
        # shadow
        sx, sy = cam((x, y, 0.0))
        r = 0.028 * cam.f / max(cam.depth((x, y, 0)), 1e-3)
        d.ellipse([sx - r * 1.2, sy - r * 0.45, sx + r * 1.2, sy + r * 0.45], fill=(130, 132, 136))
        corners = [np.array([x + sx_, y + sy_, z + sz_]) for sx_ in (-h, h) for sy_ in (-h, h) for sz_ in (-h, h)]
        pts = [cam(p) for p in corners]
        hull = _convex_hull(pts)
        d.polygon(hull, fill=(150, 18, 18))
        top = [cam((x - h, y - h, z + h)), cam((x + h, y - h, z + h)), cam((x + h, y + h, z + h)), cam((x - h, y + h, z + h))]
        d.polygon(top, fill=(232, 40, 36))

    def _arm(self, d: ImageDraw.ImageDraw, tcp, opening) -> None:
        cam = self.cam
        shoulder = np.array([0.0, 0.0, 0.333])
        wrist = np.asarray(tcp) + np.array([0.0, 0.0, 0.107])
        mid = (shoulder + wrist) / 2
        horiz = wrist[:2] - shoulder[:2]
        n = np.linalg.norm(horiz)
        back = np.array([*(horiz / n if n > 1e-6 else np.zeros(2)), 0.0])
        elbow = mid + np.array([0.0, 0.0, 0.24]) - 0.08 * back
        flange = wrist + np.array([0.0, 0.0, 0.05])
        chain = [np.array([0.0, 0.0, 0.0]), shoulder, elbow, flange, wrist]
        pts = [cam(p) for p in chain]
        for a, b in zip(pts, pts[1:]):
            d.line([a, b], fill=(40, 42, 48), width=26)
        for a, b in zip(pts, pts[1:]):
            d.line([a, b], fill=(236, 236, 240), width=18)
        for p in pts[1:]:
            d.ellipse([p[0] - 13, p[1] - 13, p[0] + 13, p[1] + 13], fill=(222, 224, 230), outline=(40, 42, 48), width=3)
        half = max(0.004, opening / 2)
        for s in (-1, 1):
            top = cam(wrist + np.array([0, s * (half + 0.006), -0.01]))
            tip = cam(np.asarray(tcp) + np.array([0, s * (half + 0.006), -0.012]))
            d.line([top, tip], fill=(30, 30, 34), width=9)
        hand_a = cam(wrist + np.array([0, -0.055, 0]))
        hand_b = cam(wrist + np.array([0, 0.055, 0]))
        d.line([hand_a, hand_b], fill=(30, 30, 34), width=14)

    def render(self, tcp, opening, cube, t: float, index: int, note: str = "") -> Image.Image:
        img = self.background.copy()
        d = ImageDraw.Draw(img)
        cube_in_front = self.cam.depth(cube) < self.cam.depth(tcp)
        if not cube_in_front:
            self._cube(d, cube)
        self._arm(d, tcp, opening)
        if cube_in_front:
            self._cube(d, cube)
        d.text((16, 14), f"FIXTURE  t={t:6.2f}s  frame {index:04d}", font=self.font, fill=(235, 235, 235))
        if note:
            d.text((16, 40), note, font=self.font, fill=(255, 120, 120))
        return img


def _convex_hull(pts):
    pts = sorted(set((round(x, 2), round(y, 2)) for x, y in pts))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _r(v, nd=4):
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_r(x, nd) for x in v]
    return round(float(v), nd)


class FixtureRun:
    """Scripted fake sim that writes events and frames in contract format."""

    def __init__(self, run_dir: Path, scenario: dict, time_scale: float = 1.0) -> None:
        self.dir = run_dir
        self.scenario = scenario
        self.ts = time_scale
        self.painter = _Painter()
        (run_dir / "frames").mkdir(parents=True, exist_ok=True)
        self.ev_f = open(run_dir / "events.jsonl", "w", encoding="utf-8")
        self.fr_f = open(run_dir / "frames.jsonl", "w", encoding="utf-8")
        self.seq = 0
        self.t = 0.0
        self.wall = WALL_T0
        self.n_frames = 0
        self.tool_calls = 0
        self.tcp = TCP_HOME.copy()
        self.opening = 0.08
        self.cube = CUBE_START.copy()
        self.held = False
        self.frozen: int | None = None
        self.tracker_dead = False
        self.near_cube_armed = None  # (dist, spec, label)
        self.note = ""

    # -- low level -----------------------------------------------------------
    def frame(self) -> None:
        idx = self.n_frames
        img = self.painter.render(self.tcp, self.opening, self.cube, self.t, idx, self.note)
        img.save(self.dir / "frames" / f"{idx:06d}.jpg", quality=85)
        self.fr_f.write(json.dumps({"index": idx, "sim_time": round(self.t, 4), "wall_time": round(self.wall, 4)}) + "\n")
        self.n_frames += 1

    def event(self, type_: str, payload: dict) -> None:
        self.ev_f.write(json.dumps({"seq": self.seq, "wall_time": round(self.wall, 4), "sim_time": round(self.t, 4),
                                    "type": type_, "payload": payload}) + "\n")
        self.seq += 1

    def think(self, seconds: float) -> None:
        self.wall += seconds

    def say(self, text: str, think_s: float = 3.0) -> None:
        self.think(think_s)
        self.event("agent_text", {"text": text})

    def stepping(self, duration: float, update) -> None:
        """Advance sim time, calling update(alpha) and recording a frame per 0.05 s."""
        n = max(1, int(round(duration / FRAME_DT)))
        for i in range(1, n + 1):
            self.t += FRAME_DT
            self.wall += FRAME_DT * 0.35
            update(i / n)
            if self.held:
                self.cube = self.tcp + np.array([0.0, 0.0, -0.005])
            self.frame()
            self._check_near_cube()

    def _check_near_cube(self) -> None:
        if not self.near_cube_armed or self.opening < 0.03:
            return
        dist, spec, label = self.near_cube_armed
        if np.linalg.norm(self.tcp - self.cube) < dist:
            self.near_cube_armed = None
            self.sabotage(spec, label)

    def sabotage(self, spec: dict, label: str) -> None:
        kind = spec["kind"]
        if kind == "teleport_cube":
            self.held = False
            self.cube = self.cube + np.array(spec["delta"])
        elif kind == "freeze_joint":
            self.frozen = spec["joint"]
        elif kind == "cut_object_sensor":
            self.tracker_dead = True
        self.event("sabotage", {"spec": spec, "label": label})
        self.frame()  # one frame after every sabotage

    def tool(self, name: str, args: dict, fn, think_s: float = 1.2, before=None):
        self.think(think_s)
        self.tool_calls += 1
        self.event("tool_call", {"tool": name, "args": args, "frame_index": self.n_frames - 1})
        w0 = self.wall
        if before is not None:   # a "phase: before" trigger fires inside the call
            before()
        result = fn()
        self.wall += 0.04
        payload_result = {"image": True} if name == "render" else result
        self.event("tool_result", {"tool": name, "result": payload_result, "frame_index": self.n_frames - 1,
                                   "duration_s": round(self.wall - w0, 3)})
        return result

    # -- tools ---------------------------------------------------------------
    def state(self) -> dict:
        return {
            "sim_time": _r(self.t),
            "joints": {"pos": [0.0, -0.3, 0.0, -2.2, 0.0, 1.9, 0.78], "vel": [0.0] * 7,
                       "target": [0.0, -0.3, 0.0, -2.2, 0.0, 1.9, 0.78],
                       "names": [f"joint{i}" for i in range(1, 8)]},
            "tcp": {"pos": _r(self.tcp), "quat": [0.0, 1.0, 0.0, 0.0]},
            "gripper": {"opening_m": _r(self.opening), "target": "open" if self.opening > 0.04 else "close",
                        "holding": self.held},
            "objects": {"cube": None if self.tracker_dead else {"pos": _r(self.cube), "quat": [1.0, 0.0, 0.0, 0.0]},
                        "target_zone": {"pos": _r(ZONE_POS), "half_size": [0.06, 0.06, 0.001]}},
            "sensors": {"object_tracker": "dead" if self.tracker_dead else "ok"},
            "contacts": {"cube_fingers": 2 if self.held else 0, "cube_table": not self.held},
        }

    def move_ee(self, pos, duration_s: float = 2.0, excluded_joints=(), before=None):
        target = np.array(pos, float)
        start = self.tcp.copy()
        dur = duration_s * self.ts
        args = {"pos": _r(pos, 3), "quat": None, "duration_s": duration_s, "excluded_joints": list(excluded_joints)}
        miss = False

        def run():
            nonlocal miss
            miss = self.frozen is not None and self.frozen not in excluded_joints
            goal = start + 0.55 * (target - start) + np.array([0.0, 0.035, 0.03]) if miss else target

            def upd(a):
                s = a * a * (3 - 2 * a)
                self.tcp = start + s * (goal - start)

            self.stepping(dur, upd)
            err = float(np.linalg.norm(self.tcp - target))
            jerr = [0.0] * 7
            if miss:
                jerr[self.frozen] = 0.62
            return {"reached": err < 0.015, "final_error_m": _r(err), "tcp_pos": _r(self.tcp),
                    "tcp_quat": [0.0, 1.0, 0.0, 0.0], "sim_time": _r(self.t), "joint_tracking_error": jerr,
                    "note": f"joint{self.frozen + 1} did not track its command" if miss else ""}

        return self.tool("move_ee", args, run, before=before)

    def gripper(self, action: str):
        start = self.opening
        target_open = 0.08 if action == "open" else 0.0
        grasp = action == "close" and np.linalg.norm(self.tcp - self.cube) < 0.02
        if grasp:
            target_open = 2 * CUBE_HALF
        if action == "open":
            self.held = False

        def upd(a):
            self.opening = start + a * (target_open - start)
            if not self.held and action == "open" and self.cube[2] > CUBE_HALF:
                self.cube = np.array([self.cube[0], self.cube[1], max(CUBE_HALF, self.cube[2] - 0.02)])

        def run():
            self.stepping(0.5 * self.ts, upd)
            if grasp:
                self.held = True
            return {"opening_m": _r(self.opening), "holding": self.held, "sim_time": _r(self.t)}

        return self.tool("set_gripper", {"action": action}, run)

    def get_state(self):
        return self.tool("get_state", {}, self.state)

    def describe(self):
        return self.tool("describe_scene", {}, lambda: {"text": "A Franka Panda on a table with a 4 cm red cube and a "
                                                                "green 12 cm target zone. Units: metres, radians."})

    def diagnose(self):
        def run():
            joints = []
            for i in range(7):
                frozen = i == self.frozen
                joints.append({"index": i, "name": f"joint{i + 1}", "commanded": -2.2 if frozen else 0.0,
                               "actual": -1.58 if frozen else 0.0, "error": 0.62 if frozen else 0.001,
                               "velocity": 0.0, "suspected_frozen": frozen})
            summary = (f"joint{self.frozen + 1} suspected frozen: commanded -2.20 rad, actual -1.58 rad, no motion"
                       if self.frozen is not None else "no anomalies detected")
            return {"sim_time": _r(self.t), "joints": joints,
                    "object_tracker": "dead" if self.tracker_dead else "ok", "gravity": [0, 0, -9.81],
                    "last_move": {"reached": self.frozen is None, "final_error_m": 0.071 if self.frozen is not None else 0.003,
                                  "worst_joint": self.frozen if self.frozen is not None else 0},
                    "contacts": self.state()["contacts"], "summary": summary}

        return self.tool("diagnose", {}, run)

    def locate(self):
        return self.tool("locate_object", {"color": "red", "camera": "top"},
                         lambda: {"found": True, "pos": _r(self.cube + np.array([0.002, -0.003, 0.0]), 3),
                                  "pixel": [412, 288], "confidence": 0.94, "method": "color_segmentation"})

    def render_tool(self):
        return self.tool("render", {"camera": "top"}, lambda: {"image": True})

    def check_task(self, force_fail: bool = False):
        def run():
            dxy = float(np.linalg.norm(self.cube[:2] - ZONE_POS[:2]))
            ok = dxy < 0.05 and self.cube[2] < 0.03 and self.opening > 0.04 and not force_fail
            reason = "cube in zone, resting, gripper open" if ok else (
                f"cube {dxy:.3f} m from zone centre (limit 0.05)")
            return {"success": ok, "cube_pos": _r(self.cube), "zone_pos": _r(ZONE_POS), "xy_distance_m": _r(dxy),
                    "cube_on_table": bool(self.cube[2] < 0.03), "gripper_open": bool(self.opening > 0.04),
                    "reason": reason}

        return self.tool("check_task", {}, run)

    def wait(self, seconds: float):
        return self.tool("wait", {"seconds": seconds},
                         lambda: (self.stepping(seconds * self.ts, lambda a: None), {"sim_time": _r(self.t)})[1])

    def close(self) -> None:
        self.ev_f.close()
        self.fr_f.close()


def _script(fx: FixtureRun, sabotage: str, fail: bool, short: bool) -> tuple[bool, bool]:
    """Play a plausible agent session. Returns (judge_success, agent_claimed_success)."""
    fx.say("I'll read the scene description and current state first, then plan a top-down grasp.", 2.0)
    if not short:
        fx.describe()
    fx.get_state()
    fx.say("Cube at [0.50, 0.00, 0.02], zone at [0.50, -0.25]. Plan: hover above the cube, descend, close, "
           "carry it over the zone, lower and release.", 4.0)
    if sabotage == "freeze":
        fx.scenario["triggers"] = [{"when": {"type": "tool_call", "tool": "move_ee", "n": 2, "phase": "before"},
                                    "sabotage": {"kind": "freeze_joint", "joint": 3}}]
    elif sabotage == "teleport":
        fx.scenario["triggers"] = [{"when": {"type": "ee_near_cube", "dist": 0.06},
                                    "sabotage": {"kind": "teleport_cube", "delta": [0.0, 0.15, 0.0]}}]
        fx.near_cube_armed = (0.06, {"kind": "teleport_cube", "delta": [0.0, 0.15, 0.0]}, "Cube teleported +15 cm in y")
    elif sabotage == "tracker":
        fx.scenario["triggers"] = [
            {"when": {"type": "tool_call", "tool": "move_ee", "n": 1, "phase": "after"},
             "sabotage": {"kind": "cut_object_sensor"}},
            {"when": {"type": "tool_call", "tool": "move_ee", "n": 1, "phase": "after"},
             "sabotage": {"kind": "teleport_cube", "delta": [0.0, 0.12, 0.0]}}]

    fx.move_ee([0.5, 0.0, HOVER_Z], 2.0)
    if sabotage == "tracker":
        fx.sabotage({"kind": "cut_object_sensor"}, "Object tracker cut")
        fx.sabotage({"kind": "teleport_cube", "delta": [0.0, 0.12, 0.0]}, "Cube moved +12 cm in y")
    fx.gripper("open")
    freeze = None
    if sabotage == "freeze":
        def freeze():
            fx.sabotage({"kind": "freeze_joint", "joint": 3}, "Joint 4 frozen")
    r = fx.move_ee([0.5, 0.0, GRASP_Z], 1.5, before=freeze)
    target = fx.cube.copy()

    if sabotage == "freeze":
        fx.say(f"The descent missed: final error {r['final_error_m']:.3f} m and joint 4 shows a large tracking "
               "error. Running diagnose before trying again.", 3.0)
        fx.diagnose()
        fx.say("Joint 4 is frozen. I'll keep it out of the IK solve (excluded_joints=[3]) and retry the descent "
               "with the remaining six joints.", 4.0)
        fx.move_ee([0.5, 0.0, HOVER_Z], 1.0, excluded_joints=(3,))
        fx.move_ee([0.5, 0.0, GRASP_Z], 1.5, excluded_joints=(3,))
        excl: tuple = (3,)
    else:
        excl = ()
    g = fx.gripper("close")
    if not g["holding"]:
        fx.say("holding=false and the fingers closed all the way. The cube is not where I expected. "
               "Checking state.", 3.0)
        fx.get_state()
        if fx.tracker_dead:
            fx.say("objects.cube is null: the object tracker is dead. Falling back to the top camera and "
                   "colour segmentation.", 3.5)
            if not short:
                fx.render_tool()
            loc = fx.locate()
            target = np.array(loc["pos"])
            target[2] = CUBE_HALF
        else:
            target = fx.cube.copy()
        fx.say(f"The cube is now at [{target[0]:.2f}, {target[1]:.2f}, {target[2]:.2f}], it was moved during my "
               "approach. Re-planning from the new position.", 3.5)
        fx.gripper("open")
        fx.move_ee([0.5, 0.0, HOVER_Z], 1.0, excl)
        fx.move_ee([float(target[0]), float(target[1]), HOVER_Z], 1.5, excl)
        fx.move_ee([float(target[0]), float(target[1]), GRASP_Z], 1.5, excl)
        fx.gripper("close")
    fx.move_ee([float(fx.tcp[0]), float(fx.tcp[1]), HOVER_Z], 1.0, excl)
    if not short:
        fx.get_state()
        fx.say("Grasp confirmed (holding=true). Carrying it over the target zone.", 2.0)
    drop = ZONE_POS.copy()
    if fail:
        drop = drop + np.array([0.0, 0.11, 0.0])
    fx.move_ee([float(drop[0]), float(drop[1]), HOVER_Z], 2.5, excl)
    fx.move_ee([float(drop[0]), float(drop[1]), 0.035], 1.2, excl)
    fx.gripper("open")
    fx.move_ee([float(drop[0]), float(drop[1]), HOVER_Z], 1.0, excl)
    if not short:
        fx.wait(0.5)
    ct = fx.check_task()
    claimed = True
    if ct["success"]:
        fx.say(f"check_task reports success: cube {ct['xy_distance_m']:.3f} m from the zone centre, resting on "
               "the table, gripper open.", 2.5)
    else:
        fx.say("check_task says the cube is outside the zone. I'm out of retries, reporting what I have.", 2.5)
        claimed = False
    status = "success" if claimed else "failed"
    summary = ("Placed the cube in the zone after recovering from the fault." if claimed
               else "Cube released outside the zone.")
    fx.tool("report_outcome", {"status": status, "summary": summary}, lambda: {"ok": True})
    fx.event("outcome", {"status": status, "summary": summary})
    fx.think(1.5)
    detail = {"success": bool(ct["success"]), "cube_pos": ct["cube_pos"], "zone_pos": ct["zone_pos"],
              "xy_distance_m": ct["xy_distance_m"], "cube_on_table": ct["cube_on_table"],
              "gripper_open": ct["gripper_open"], "reason": ct["reason"]}
    fx.event("judge", {"success": bool(ct["success"]), "detail": detail})
    return bool(ct["success"]), claimed


def make_fixture_run(name: str = "demo", runs_root: Path | str | None = None, sabotage: str = "teleport",
                     fail: bool = False, time_scale: float = 1.0, short: bool = False,
                     title: str | None = None) -> Path:
    """Create runs/fixture_<name>/ and return its path (overwrites an existing one)."""
    if sabotage not in TITLES:
        raise ValueError(f"sabotage must be one of {sorted(TITLES)}")
    root = Path(runs_root) if runs_root else RUNS_DIR
    run_dir = root / f"fixture_{name}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    scenario = {"name": f"fixture_{name}", "title": title or TITLES[sabotage], "task": TASK, "triggers": []}
    fx = FixtureRun(run_dir, scenario, time_scale)
    fx.event("run_start", {"scenario": scenario["name"], "title": scenario["title"], "task": TASK})
    fx.frame()  # one frame at run start
    try:
        success, claimed = _script(fx, sabotage, fail, short)
        wall_s = round(fx.wall - WALL_T0 + 2.0, 2)
        fx.think(2.0)
        fx.event("run_end", {"success": success, "tool_calls": fx.tool_calls, "sim_time": round(fx.t, 3),
                             "wall_s": wall_s})
    finally:
        fx.close()
    (run_dir / "scenario.json").write_text(json.dumps(scenario, indent=2), encoding="utf-8")
    summary = {"scenario": scenario["name"], "success": success, "agent_claimed": claimed,
               "tool_calls": fx.tool_calls, "sim_time": round(fx.t, 3), "wall_s": wall_s, "model": "fixture"}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return run_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--name", default="demo", help="run dir becomes runs/fixture_<name>")
    ap.add_argument("--sabotage", choices=sorted(TITLES), default="teleport")
    ap.add_argument("--fail", action="store_true", help="drop the cube outside the zone (judge fails)")
    ap.add_argument("--time-scale", type=float, default=1.0, help="scale all motion durations (fewer frames)")
    ap.add_argument("--short", action="store_true", help="fewer agent turns and frame-less tool calls")
    ap.add_argument("--title", default=None)
    ap.add_argument("--runs-root", default=None, help=f"default {RUNS_DIR}")
    a = ap.parse_args(argv)
    run_dir = make_fixture_run(a.name, a.runs_root, a.sabotage, a.fail, a.time_scale, a.short, a.title)
    n = len(list((run_dir / "frames").glob("*.jpg")))
    print(f"wrote {run_dir} ({n} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
