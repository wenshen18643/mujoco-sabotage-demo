"""SimCore: the MuJoCo pick-and-place cell behind the REST API (no networking).

One instance owns the model, the physics state, an offscreen renderer, the run
directory (events.jsonl, frames/, frames.jsonl, scenario.json) and the fault
injector. It is not thread-safe: call it from a single thread (the server runs
every call on one dedicated worker thread, which also owns the GL context).

Every tool-like public method logs a ``tool_call`` event, fires matching
scenario triggers (before/after), runs, and logs a ``tool_result`` event. Pass
``observer=True`` to read state without logging or triggering (dashboards).
"""

from __future__ import annotations

import io
import json
import math
import queue
import re
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import mujoco
import numpy as np
from PIL import Image
from scipy import ndimage

from . import scene
from .ik import ArmDriver, ArmIK, MoveReport, quat_normalize
from .sabotage import FaultInjector, TriggerSet, normalize_spec
from .scene import (
    ARM_JOINTS,
    CAMERAS,
    CUBE_HALF,
    FINGER_BODIES,
    FINGER_JOINTS,
    GRIPPER_ACTUATOR,
    GRIPPER_CLOSED_CTRL,
    GRIPPER_OPEN_CTRL,
    HOME_Q,
    RENDER_H,
    RENDER_W,
    TOP_DOWN_QUAT,
    ZONE_HALF,
    ZONE_POS,
)

FRAME_DT = 0.05  # s of sim time between recorded frames
JPEG_QUALITY = 85
REACH_TOL = 0.015  # m, move_ee "reached" threshold
FROZEN_ERR = 0.05  # rad, |commanded - actual| for suspected_frozen
FROZEN_VEL = 0.01  # rad/s, max |velocity| during the last move for suspected_frozen
GRIPPER_SETTLE_MAX_S = 1.0  # extra time after the ramp for the fingers to come to rest
GRIPPER_SETTLE_VEL = 0.002  # m/s
SUCCESS_XY = 0.05
SUCCESS_Z = 0.03
SUCCESS_SPEED = 0.02
WORKSPACE = ((-1.0, 1.0), (-1.0, 1.0), (0.01, 1.3))  # sanity bounds for TCP targets
# (z >= 0.01: the fingertips reach 0.009 m below the TCP, so lower targets drive them into the table)

# Colour rules for locate_object: (predicate on float RGB, plane height of the
# object's centre in metres, object footprint edge in metres).
COLOR_RULES: dict[str, tuple[Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray], float, float]] = {
    "red": (lambda r, g, b: (r > 80) & (r > 1.7 * g + 25) & (r > 1.7 * b + 25), CUBE_HALF, 2 * CUBE_HALF),
    "green": (lambda r, g, b: (g > 80) & (g > 1.35 * r + 20) & (g > 1.25 * b + 15),
              2 * ZONE_HALF[2], 2 * ZONE_HALF[0]),
}
MIN_BLOB_PX = 25
LOW_CONFIDENCE = 0.5

DESCRIBE_TEXT = f"""\
Workcell: a Franka Emika Panda arm (7 revolute joints, parallel-jaw gripper) mounted at the world origin on a table whose top surface is z = 0.
World frame: origin at the robot base, +x points away from the robot, +y to the robot's left, +z up. Units are metres, radians and seconds; quaternions are [w, x, y, z]. Arm joints are indexed 0..6 (joint1..joint7).

Objects:
- red cube, 4 cm on each side (half-size 0.02), 50 g. Resting on the table its centre is at z = 0.02. It starts near [0.50, 0.00, 0.02].
- green target zone, a flat 12 x 12 cm square centred at [0.50, -0.25, 0.001] (half-size [0.06, 0.06, 0.001]). It has no collision.
Task success: the cube rests on the table (centre z < 0.03, nearly still) with its centre within 0.05 m horizontally of the zone centre, and the gripper is open and not touching it.

End effector: every position refers to the tool centre point (TCP), the point midway between the fingertip pads, 0.1034 m beyond the hand flange. Omitting quat (or passing null) uses the default top-down orientation {list(TOP_DOWN_QUAT)}: fingers pointing straight down, closing along world y. The gripper opens to 0.08 m between the pads; the cube is 0.04 m wide. To grasp, open the gripper, put the TCP at the cube centre (z = 0.02) and close; `holding` becomes true when both fingers touch the cube. Approach from above (about 0.12 m above the cube) and descend vertically so the fingers do not hit the cube.

Motion:
- move_ee: straight-line TCP motion to the target over duration_s (default 2 s), inverse kinematics on the 7 arm joints at every waypoint. Joints listed in excluded_joints are held at their current angle and the IK uses only the others. Returns reached = true when the final TCP position error is below 0.015 m, plus the error, the final TCP pose and the per-joint tracking error (commanded minus actual angle).
- move_joints: joint-space motion to 7 angles. Limits (rad): joint1 +/-2.90, joint2 +/-1.76, joint3 +/-2.90, joint4 -3.07..-0.07, joint5 +/-2.90, joint6 -0.02..3.75, joint7 +/-2.90.
- set_gripper: "open" or "close" (about 0.5 s).
- wait: let the simulation run (the arm holds its last command).
Comfortable top-down workspace: x 0.30..0.70, y -0.35..0.35, z 0.02..0.40 (keep the TCP within about 0.75 m of the base). TCP targets below z = 0.01 are rejected: the fingertips reach 0.009 m below the TCP.

Sensing:
- get_state: joint angles/velocities/targets, TCP pose, gripper opening and target, contacts (how many fingers touch the cube, whether the cube touches the table) and object poses from the object tracker (objects.cube; null if the tracker reports nothing).
- locate_object: finds an object by colour ("red" = cube, "green" = zone) in a camera image by colour segmentation and back-projects its image centroid onto the table, assuming the object rests on the table. The default camera "top" looks straight down from [0.5, -0.1, 1.2]; the arm can hide objects beneath it, so move the arm aside if the object is not found.
- render: camera image. "front" = overview from the front-left, "top" = straight down (image up = +x, image right = -y), "wrist" = on the hand, looking along the fingers.
- diagnose: per-joint commanded vs actual angle and velocity, sensor status, last move result and a plain-English list of anomalies.
- check_task: evaluates the success criteria above.
"""


def _r(x: Any, nd: int = 4) -> Any:
    """Round numbers (recursively) to plain Python floats for JSON."""
    if isinstance(x, dict):
        return {k: _r(v, nd) for k, v in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [_r(v, nd) for v in x]
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        v = round(float(x), nd)
        return 0.0 if v == 0 else v
    return x


def _vec(value: Any, n: int, what: str) -> np.ndarray:
    try:
        out = np.array([float(v) for v in value], dtype=float)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a list of {n} numbers") from None
    if out.shape != (n,) or not np.all(np.isfinite(out)):
        raise ValueError(f"{what} must be a list of {n} finite numbers")
    return out


def _duration(value: Any, lo: float, hi: float, what: str = "duration_s") -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number") from None
    if not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"{what} must be between {lo} and {hi}")
    return v


def _joint_list(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("excluded_joints must be a list of joint indices 0..6")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or not 0 <= int(v) <= 6:
            raise ValueError("excluded_joints must contain integer joint indices 0..6")
        out.append(int(v))
    if len(set(out)) > 5:
        raise ValueError("at least 2 arm joints must stay free")
    return sorted(set(out))


def _stretch_note(requested: float, actual: float) -> str:
    if actual > requested + 1e-6:
        return f"duration stretched from {requested:.2f} s to {actual:.2f} s by the speed limits"
    return ""


def encode_image(rgb: np.ndarray, fmt: str = "jpeg", quality: int = JPEG_QUALITY) -> bytes:
    """Encode an RGB array as JPEG or PNG bytes."""
    buf = io.BytesIO()
    img = Image.fromarray(rgb)
    if fmt == "png":
        img.save(buf, format="PNG", compress_level=1)
    else:
        img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


class FrameWriter:
    """Background JPEG encoder for recorded frames (keeps the sim thread fast)."""

    def __init__(self, frames_dir: Path, index_path: Path):
        self.frames_dir = frames_dir
        self._index = open(index_path, "w", encoding="utf-8")
        self._q: queue.Queue = queue.Queue(maxsize=256)
        self._lock = threading.Lock()
        self.latest: tuple[int, float, bytes] | None = None
        self._thread = threading.Thread(target=self._run, name="frame-writer", daemon=True)
        self._thread.start()

    def submit(self, index: int, sim_time: float, wall_time: float, rgb: np.ndarray) -> None:
        self._q.put((index, sim_time, wall_time, rgb))

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is None:
                    return
                index, sim_time, wall_time, rgb = item
                jpeg = encode_image(rgb)
                (self.frames_dir / f"{index:06d}.jpg").write_bytes(jpeg)
                self._index.write(json.dumps({"index": index, "sim_time": round(sim_time, 4),
                                              "wall_time": wall_time}) + "\n")
                self._index.flush()
                with self._lock:
                    self.latest = (index, sim_time, jpeg)
            finally:
                self._q.task_done()

    def get_latest(self) -> tuple[int, float, bytes] | None:
        with self._lock:
            return self.latest

    def flush(self) -> None:
        """Block until every submitted frame is on disk."""
        self._q.join()

    def close(self) -> None:
        self._q.put(None)
        self._thread.join(timeout=30)
        self._index.close()


class SimCore:
    """The simulated workcell: physics, IK, faults, frames and the event log."""

    def __init__(self, run_dir: Path, scenario: dict, *, realtime: float = 0.0,
                 record_frames: bool = True):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.scenario = dict(scenario)
        self.scenario_name = str(scenario.get("name", "unnamed"))
        self.realtime = float(realtime)
        (self.run_dir / "scenario.json").write_text(json.dumps(scenario, indent=2), encoding="utf-8")

        self.model = scene.build_model()
        self.data = mujoco.MjData(self.model)
        m = self.model
        self.ik = ArmIK(m)
        self.driver = ArmDriver(m, self.data, self.ik)
        self.faults = FaultInjector(m, self.data)
        self.triggers = TriggerSet.from_scenario(scenario)
        self.tcp_id = m.site("tcp").id
        self.cube_body = m.body("cube").id
        self.cube_geom = m.geom("cube").id
        self.table_geom = m.geom("table").id
        self.cube_dadr = int(m.jnt_dofadr[m.body("cube").jntadr[0]])
        self.finger_bodies = [m.body(n).id for n in FINGER_BODIES]
        self.finger_qadr = [int(m.jnt_qposadr[m.joint(n).id]) for n in FINGER_JOINTS]
        self.finger_dadr = [int(m.jnt_dofadr[m.joint(n).id]) for n in FINGER_JOINTS]
        self.grip_act = m.actuator(GRIPPER_ACTUATOR).id
        self.home_key = m.key("home").id
        self.renderer = mujoco.Renderer(m, RENDER_H, RENDER_W)

        self._events: list[dict] = []
        self._ev_lock = threading.Lock()
        self._ev_file = open(self.run_dir / "events.jsonl", "w", encoding="utf-8")
        self._listeners: list[Callable[[dict], None]] = []
        frames_dir = self.run_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        for old in frames_dir.glob("*.jpg"):  # stale frames from a previous run in this dir
            if re.fullmatch(r"\d{6}\.jpg", old.name):
                old.unlink()
        self._writer = FrameWriter(frames_dir, self.run_dir / "frames.jsonl") if record_frames else None
        self._frame_index = -1
        self._next_frame_t = 0.0
        self._pace_anchor = (time.perf_counter(), 0.0)
        self._tool_counts: Counter[str] = Counter()
        self._last_move: dict | None = None
        self._gripper_target = "open"
        self._closed = False

        self._reset_physics()
        self.log_event("run_start", {"scenario": self.scenario_name,
                                     "title": scenario.get("title", ""), "task": scenario.get("task", "")})
        self._record_frame()
        self._flush_frames()

    # ------------------------------------------------------------------ infra
    @property
    def sim_time(self) -> float:
        return float(self.data.time)

    @property
    def frame_index(self) -> int:
        """Index of the most recently recorded frame (-1 before the first)."""
        return self._frame_index

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        """Register ``fn(event)``, called for every new event on the logging thread.

        It runs under the event lock, so it must be quick and must not log events itself.
        """
        self._listeners.append(fn)

    def log_event(self, type: str, payload: dict) -> None:  # noqa: A002 (contract name)
        """Append an event to events.jsonl and notify listeners. Thread-safe."""
        if not isinstance(type, str) or not type:
            raise ValueError("event type must be a non-empty string")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("event payload must be an object")
        with self._ev_lock:
            ev = {"seq": len(self._events), "wall_time": time.time(), "sim_time": round(self.sim_time, 4),
                  "type": type, "payload": payload}
            line = json.dumps(ev, default=_json_default)
            ev = json.loads(line)  # store exactly what was written (plain JSON types)
            self._events.append(ev)
            self._ev_file.write(line + "\n")
            self._ev_file.flush()
            for fn in list(self._listeners):  # under the lock: listeners see events in seq order
                try:
                    fn(ev)
                except Exception:  # a broken listener must not break the sim
                    pass

    def events_since(self, since: int = 0) -> tuple[list[dict], int]:
        """Events with ``seq >= since`` and the next cursor."""
        since = max(0, int(since))
        with self._ev_lock:
            evs = self._events[since:]
            return list(evs), since + len(evs)

    def latest_frame(self) -> tuple[int, float, bytes] | None:
        """(index, sim_time, jpeg) of the newest frame written to disk."""
        return self._writer.get_latest() if self._writer else None

    def close(self) -> None:
        """Flush frames and release the renderer and files."""
        if self._closed:
            return
        self._closed = True
        if self._writer:
            self._writer.close()
        self.renderer.close()
        with self._ev_lock:
            self._ev_file.close()

    def _flush_frames(self) -> None:
        if self._writer:
            self._writer.flush()

    def _record_frame(self) -> None:
        t = self.sim_time
        self._frame_index += 1
        if self._writer:
            self._writer.submit(self._frame_index, t, time.time(), self._render_rgb("front"))
        self._next_frame_t = (math.floor(t / FRAME_DT + 1e-6) + 1) * FRAME_DT

    def _begin_stepping(self) -> None:
        self._pace_anchor = (time.perf_counter(), self.sim_time)

    def _pace(self) -> None:
        if self.realtime <= 0:
            return
        wall0, sim0 = self._pace_anchor
        delay = wall0 + (self.sim_time - sim0) / self.realtime - time.perf_counter()
        if delay > 0:
            time.sleep(delay)

    def _advance(self, n: int) -> None:
        """Step physics ``n`` times: faults, state triggers, frames, pacing."""
        m, d = self.model, self.data
        for _ in range(int(n)):
            mujoco.mj_step(m, d)
            self.faults.post_step()
            if self.triggers.has_state_triggers:
                specs = self.triggers.on_step(d.time, d.site_xpos[self.tcp_id], d.xpos[self.cube_body],
                                              self._gripper_target == "open")
                for spec in specs:
                    self._apply_sabotage(spec, source="trigger")
            if d.time >= self._next_frame_t - 1e-6:
                self._record_frame()
                self._pace()

    def _tool(self, name: str, args: dict, fn: Callable[[], Any],
              summarize: Callable[[Any], Any] | None = None) -> Any:
        self._tool_counts[name] += 1
        n = self._tool_counts[name]
        self.log_event("tool_call", {"tool": name, "args": _r(args, 6), "frame_index": self._frame_index})
        t0 = time.perf_counter()
        for spec in self.triggers.on_tool_call(name, n, "before"):
            self._apply_sabotage(spec, source="trigger")
        try:
            result = fn()
        except Exception as e:
            msg = str(e) if isinstance(e, ValueError) else f"internal error: {type(e).__name__}: {e}"
            self._flush_frames()
            self.log_event("tool_result", {"tool": name, "result": {"error": msg},
                                           "frame_index": self._frame_index,
                                           "duration_s": round(time.perf_counter() - t0, 4)})
            raise
        for spec in self.triggers.on_tool_call(name, n, "after"):
            self._apply_sabotage(spec, source="trigger")
        self._flush_frames()
        self.log_event("tool_result", {"tool": name, "result": summarize(result) if summarize else result,
                                       "frame_index": self._frame_index,
                                       "duration_s": round(time.perf_counter() - t0, 4)})
        return result

    # ------------------------------------------------------------- physics
    def _reset_physics(self) -> None:
        t = self.data.time
        mujoco.mj_resetDataKeyframe(self.model, self.data, self.home_key)
        self.data.time = t  # keep sim time monotonic across resets
        mujoco.mj_forward(self.model, self.data)
        self.driver.command(np.array(HOME_Q))
        self.data.ctrl[self.grip_act] = GRIPPER_OPEN_CTRL
        self._gripper_target = "open"

    def _tcp_pose(self) -> tuple[np.ndarray, np.ndarray]:
        quat = np.zeros(4)
        mujoco.mju_mat2Quat(quat, self.data.site_xmat[self.tcp_id])
        if np.dot(quat, TOP_DOWN_QUAT) < 0:  # report in the same hemisphere as the default
            quat = -quat
        return self.data.site_xpos[self.tcp_id].copy(), quat

    def _contacts(self) -> tuple[int, bool]:
        """(number of fingers touching the cube, cube touching the table)."""
        d, m = self.data, self.model
        fingers = set()
        table = False
        for i in range(d.ncon):
            g1, g2 = int(d.contact.geom1[i]), int(d.contact.geom2[i])
            if self.cube_geom not in (g1, g2):
                continue
            other = g2 if g1 == self.cube_geom else g1
            if other == self.table_geom:
                table = True
            body = int(m.geom_bodyid[other])
            if body in self.finger_bodies:
                fingers.add(body)
        return len(fingers), table

    def _opening(self) -> float:
        return float(sum(self.data.qpos[a] for a in self.finger_qadr))

    def _cube_pose(self) -> tuple[np.ndarray, np.ndarray]:
        return self.data.xpos[self.cube_body].copy(), self.data.xquat[self.cube_body].copy()

    def _render_rgb(self, camera: str) -> np.ndarray:
        self.renderer.update_scene(self.data, camera=camera)
        return self.renderer.render()

    # ---------------------------------------------------------------- state
    def _state(self) -> dict:
        d = self.data
        tcp_p, tcp_q = self._tcp_pose()
        fingers, table = self._contacts()
        cube = None
        if not self.faults.sensor_dead:
            cp, cq = self._cube_pose()
            cube = {"pos": _r(cp), "quat": _r(cq)}
        return {
            "sim_time": _r(d.time, 3),
            "joints": {"pos": _r(self.driver.q_actual()), "vel": _r(self.driver.qvel()),
                       "target": _r(self.driver.q_cmd), "names": list(ARM_JOINTS)},
            "tcp": {"pos": _r(tcp_p), "quat": _r(tcp_q)},
            "gripper": {"opening_m": _r(self._opening()), "target": self._gripper_target,
                        "holding": fingers == 2},
            "objects": {"cube": cube,
                        "target_zone": {"pos": list(ZONE_POS), "half_size": list(ZONE_HALF)}},
            "sensors": {"object_tracker": "dead" if self.faults.sensor_dead else "ok"},
            "contacts": {"cube_fingers": fingers, "cube_table": table},
            "active_faults": self.faults.active_names(),
        }

    def get_state(self, *, observer: bool = False) -> dict:
        """Robot, gripper, object and sensor state (see CONTRACT.md, State object)."""
        mujoco.mj_forward(self.model, self.data)
        if observer:
            return self._state()
        return self._tool("get_state", {}, self._state)

    def describe(self, *, observer: bool = False) -> dict:
        """Plain-English description of the scene and the API for the agent."""
        if observer:
            return {"text": DESCRIBE_TEXT}
        return self._tool("describe_scene", {}, lambda: {"text": DESCRIBE_TEXT})

    # --------------------------------------------------------------- motion
    def _motion_result(self, target: np.ndarray, report: MoveReport, excluded: list[int],
                       extra_note: str = "") -> dict:
        mujoco.mj_forward(self.model, self.data)
        tcp_p, tcp_q = self._tcp_pose()
        err = float(np.linalg.norm(tcp_p - target))
        q_act = self.driver.q_actual()
        track = report.q_cmd_final - q_act
        reached = err < REACH_TOL
        worst = int(np.argmax(np.abs(track)))
        self._last_move = {"reached": reached, "final_error_m": err, "worst_joint": worst,
                           "max_vel": report.max_abs_vel.copy(), "q_cmd": report.q_cmd_final.copy(),
                           "excluded": list(excluded), "ik_error_m": report.ik_pos_error}
        if reached:
            note = f"reached target (error {err * 1000:.1f} mm)"
        else:
            parts = [f"target NOT reached: the TCP stopped {err * 100:.1f} cm from the target"]
            lagging = [j for j in range(7) if abs(track[j]) > FROZEN_ERR]
            if lagging:
                parts.append("joints not following their commands: " + ", ".join(
                    f"index {j} (commanded {report.q_cmd_final[j]:+.3f}, actual {q_act[j]:+.3f} rad)"
                    for j in lagging))
            if report.ik_pos_error > 0.005:
                parts.append(f"IK residual {report.ik_pos_error * 100:.1f} cm: the pose may be out of reach "
                             "for the allowed joints")
            note = "; ".join(parts)
        if extra_note:
            note = f"{note}; {extra_note}"
        return {"reached": reached, "final_error_m": _r(err), "tcp_pos": _r(tcp_p), "tcp_quat": _r(tcp_q),
                "sim_time": _r(self.data.time, 3), "joint_tracking_error": _r(track), "note": note}

    def move_ee(self, pos: Sequence[float], quat: Sequence[float] | None = None, duration_s: float = 2.0,
                excluded_joints: Sequence[int] = ()) -> dict:
        """Straight-line TCP move to ``pos`` (and ``quat``, default top-down) with per-waypoint IK."""
        args = {"pos": list(pos) if pos is not None else None,
                "quat": list(quat) if quat is not None else None,
                "duration_s": duration_s, "excluded_joints": list(excluded_joints or [])}

        def run() -> dict:
            p = _vec(pos, 3, "pos")
            q = None if quat is None else quat_normalize(_vec(quat, 4, "quat"))
            for i, (lo, hi) in enumerate(WORKSPACE):
                if not lo <= p[i] <= hi:
                    axis = "xyz"[i]
                    raise ValueError(f"pos {axis} = {p[i]:.3f} is outside the safe range [{lo}, {hi}] m"
                                     + (" (the fingertips would hit the table)" if axis == "z" else ""))
            if q is not None:
                mat = np.zeros(9)
                mujoco.mju_quat2Mat(mat, q)
                if mat[8] > 0.0:  # world z component of the hand z axis
                    raise ValueError("quat would point the fingers upward; pass null for the default "
                                     f"top-down orientation {list(TOP_DOWN_QUAT)}")
            dur = _duration(duration_s, 0.2, 20.0)
            excl = _joint_list(excluded_joints)
            mujoco.mj_forward(self.model, self.data)
            self._begin_stepping()
            report = self.driver.execute_cartesian(p, q, dur, excl, self._advance, self._tcp_pose())
            notes = [_stretch_note(dur, report.duration_s)]
            if report.flipped:
                notes.append("the hand was turned to the equivalent grasp orientation (180 deg about the tool "
                             "axis) instead of spinning the wrist half a turn")
            return self._motion_result(p, report, excl, "; ".join(n for n in notes if n))

        return self._tool("move_ee", args, run)

    def move_joints(self, q7: Sequence[float], duration_s: float = 2.0) -> dict:
        """Minimum-jerk joint-space move to 7 arm joint angles (clamped to the limits)."""
        args = {"q": list(q7) if q7 is not None else None, "duration_s": duration_s}

        def run() -> dict:
            q = _vec(q7, 7, "q")
            dur = _duration(duration_s, 0.2, 20.0)
            q_goal = np.clip(q, self.ik.lower, self.ik.upper)
            note = ""
            if np.max(np.abs(q_goal - q)) > 1e-6:
                note = "target clamped to the joint limits"
            target_tcp, _ = self.ik.fk(q_goal)
            self._begin_stepping()
            report = self.driver.execute_joint(q_goal, dur, self._advance)
            note = "; ".join(n for n in (note, _stretch_note(dur, report.duration_s)) if n)
            return self._motion_result(target_tcp, report, [], note)

        return self._tool("move_joints", args, run)

    def set_gripper(self, action: str, duration_s: float = 0.5) -> dict:
        """Open or close the gripper with a smooth ramp; reports opening and grasp."""
        args = {"action": action, "duration_s": duration_s}

        def run() -> dict:
            if action not in ("open", "close"):
                raise ValueError("action must be 'open' or 'close'")
            dur = _duration(duration_s, 0.1, 5.0)
            target = GRIPPER_OPEN_CTRL if action == "open" else GRIPPER_CLOSED_CTRL
            self._gripper_target = action
            c0 = float(self.data.ctrl[self.grip_act])
            n = max(1, int(round(dur / self.model.opt.timestep)))
            ramp = max(1, int(0.6 * n))
            self._begin_stepping()
            for i in range(n):
                self.data.ctrl[self.grip_act] = c0 + (target - c0) * min(1.0, (i + 1) / ramp)
                self._advance(1)
            # Then wait (bounded) for the fingers to stop: a slightly off-centre cube
            # is pushed into the middle by the first finger before the second one lands.
            chunk = max(1, int(round(0.01 / self.model.opt.timestep)))
            waited = 0.0
            while waited < GRIPPER_SETTLE_MAX_S:
                self._advance(chunk)
                waited += chunk * self.model.opt.timestep
                if float(np.max(np.abs(self.data.qvel[self.finger_dadr]))) < GRIPPER_SETTLE_VEL:
                    break
            mujoco.mj_forward(self.model, self.data)
            fingers, _ = self._contacts()
            return {"opening_m": _r(self._opening()), "holding": fingers == 2, "sim_time": _r(self.data.time, 3)}

        return self._tool("set_gripper", args, run)

    def step(self, seconds: float) -> dict:
        """Let the simulation run for ``seconds`` holding the current commands."""
        def run() -> dict:
            s = _duration(seconds, 0.0, 30.0, "seconds")
            self._begin_stepping()
            self._advance(int(round(s / self.model.opt.timestep)))
            mujoco.mj_forward(self.model, self.data)
            return {"sim_time": _r(self.data.time, 3)}

        return self._tool("wait", {"seconds": seconds}, run)

    # -------------------------------------------------------------- sensing
    def render(self, camera: str = "front", *, observer: bool = False) -> np.ndarray:
        """RGB image (720x960x3 uint8) from ``front``, ``top`` or ``wrist``."""
        def run() -> np.ndarray:
            if camera not in CAMERAS:
                raise ValueError(f"unknown camera {camera!r}; expected one of {list(CAMERAS)}")
            mujoco.mj_forward(self.model, self.data)
            return self._render_rgb(camera).copy()

        if observer:
            return run()
        return self._tool("render", {"camera": camera}, run,
                          summarize=lambda _: {"image": True, "camera": camera})

    def _backproject(self, camera: str, u: float, v: float, plane_z: float) -> np.ndarray | None:
        cid = self.model.camera(camera).id
        f = 0.5 * RENDER_H / math.tan(math.radians(float(self.model.cam_fovy[cid])) / 2)
        ray_cam = np.array([(u - RENDER_W / 2) / f, -(v - RENDER_H / 2) / f, -1.0])
        rot = self.data.cam_xmat[cid].reshape(3, 3)
        origin = self.data.cam_xpos[cid].copy()
        ray = rot @ ray_cam
        if abs(ray[2]) < 1e-9:
            return None
        s = (plane_z - origin[2]) / ray[2]
        if s <= 0:
            return None
        return origin + s * ray

    def _locate(self, color: str, camera: str) -> dict:
        if color not in COLOR_RULES:
            raise ValueError(f"unsupported color {color!r}; expected one of {list(COLOR_RULES)}")
        if camera not in CAMERAS:
            raise ValueError(f"unknown camera {camera!r}; expected one of {list(CAMERAS)}")
        rule, plane_z, size = COLOR_RULES[color]
        mujoco.mj_forward(self.model, self.data)
        rgb = self._render_rgb(camera).astype(np.float32)
        mask = rule(rgb[..., 0], rgb[..., 1], rgb[..., 2])
        none = {"found": False, "pos": None, "pixel": None, "confidence": 0.0, "method": "color_segmentation"}
        labels, n = ndimage.label(mask)
        if n == 0:
            return none
        sizes = ndimage.sum_labels(mask, labels, index=np.arange(1, n + 1))
        k = int(np.argmax(sizes))
        count = int(sizes[k])
        if count < MIN_BLOB_PX:
            return none
        vs, us = np.nonzero(labels == k + 1)
        u, v = float(us.mean()) + 0.5, float(vs.mean()) + 0.5
        pos = self._backproject(camera, u, v, plane_z)
        if pos is None:
            return none
        cid = self.model.camera(camera).id
        f = 0.5 * RENDER_H / math.tan(math.radians(float(self.model.cam_fovy[cid])) / 2)
        depth = float(np.linalg.norm(self.data.cam_xpos[cid] - pos))
        expected = (size * f / depth) ** 2
        conf = min(1.0, count / expected)
        pos[2] = plane_z
        out = {"found": True, "pos": _r(pos), "pixel": [round(u, 1), round(v, 1)],
               "confidence": round(conf, 2), "method": "color_segmentation", "pixels": count}
        if conf < LOW_CONFIDENCE:
            out["note"] = ("low confidence: only part of the object is visible (probably hidden under the "
                           "arm), so the position may be off; move the arm aside and look again")
        return out

    def locate_object(self, color: str = "red", camera: str = "top") -> dict:
        """Find an object by colour segmentation and back-project it onto the table."""
        color = (color or "red").lower()
        camera = camera or "top"
        return self._tool("locate_object", {"color": color, "camera": camera},
                          lambda: self._locate(color, camera))

    def _diagnose(self) -> dict:
        mujoco.mj_forward(self.model, self.data)
        q_act, vel, q_cmd = self.driver.q_actual(), self.driver.qvel(), self.driver.q_cmd
        lm = self._last_move
        joints, anomalies = [], []
        for j in range(7):
            err = float(q_cmd[j] - q_act[j])
            moved = float(lm["max_vel"][j]) if lm else abs(float(vel[j]))
            frozen = abs(err) > FROZEN_ERR and moved < FROZEN_VEL
            joints.append({"index": j, "name": ARM_JOINTS[j], "commanded": _r(q_cmd[j]), "actual": _r(q_act[j]),
                           "error": _r(err), "velocity": _r(vel[j]), "suspected_frozen": frozen})
            if frozen:
                anomalies.append(f"joint index {j} ({ARM_JOINTS[j]}) is not following its command: commanded "
                                 f"{q_cmd[j]:+.3f} rad, actual {q_act[j]:+.3f} rad, and it did not move during "
                                 "the last move (suspected frozen)")
            elif abs(err) > FROZEN_ERR:
                anomalies.append(f"joint index {j} ({ARM_JOINTS[j]}) lags its command by {err:+.3f} rad")
        if self.faults.sensor_dead:
            anomalies.append("object tracker is not reporting: objects.cube is null in get_state "
                             "(cameras and locate_object still work)")
        if lm and not lm["reached"]:
            anomalies.append(f"last move missed its target by {lm['final_error_m'] * 100:.1f} cm")
        fingers, table = self._contacts()
        if self._gripper_target == "close" and fingers < 2:
            anomalies.append(f"gripper is closed but only {fingers} finger(s) touch the cube (not holding it)")
        last = None
        if lm:
            last = {"reached": lm["reached"], "final_error_m": _r(lm["final_error_m"]), "worst_joint": lm["worst_joint"]}
        return {
            "sim_time": _r(self.data.time, 3),
            "joints": joints,
            "object_tracker": "dead" if self.faults.sensor_dead else "ok",
            "gravity": _r(self.model.opt.gravity),
            "last_move": last,
            "contacts": {"cube_fingers": fingers, "cube_table": table},
            "summary": "; ".join(anomalies) if anomalies else "no anomalies detected",
        }

    def diagnose(self, *, observer: bool = False) -> dict:
        """Per-joint command tracking, sensor health and a plain-English anomaly summary."""
        return self._diagnose() if observer else self._tool("diagnose", {}, self._diagnose)

    def _check_task(self, truth: bool) -> dict:
        mujoco.mj_forward(self.model, self.data)
        cube_p, _ = self._cube_pose()
        speed = float(np.linalg.norm(self.data.qvel[self.cube_dadr:self.cube_dadr + 3]))
        zone = np.array(ZONE_POS)
        xy = float(np.linalg.norm(cube_p[:2] - zone[:2]))
        fingers, _ = self._contacts()
        on_table = cube_p[2] < SUCCESS_Z and speed < SUCCESS_SPEED
        gripper_open = self._gripper_target == "open"
        success = xy < SUCCESS_XY and on_table and gripper_open and fingers == 0
        hidden = self.faults.sensor_dead and not truth
        if success:
            reason = "the cube rests in the target zone and the gripper has released it"
        elif xy >= SUCCESS_XY:
            reason = ("the cube is not in the target zone" if hidden else
                      f"the cube centre is {xy * 100:.1f} cm from the zone centre (needs < {SUCCESS_XY * 100:.0f} cm)")
        elif not on_table:
            reason = "the cube is not resting on the table"
        elif not gripper_open:
            reason = "the gripper is still closed"
        else:
            reason = "the gripper is still touching the cube"
        return {"success": success, "cube_pos": None if hidden else _r(cube_p), "zone_pos": list(ZONE_POS),
                "xy_distance_m": None if hidden else _r(xy), "cube_on_table": bool(on_table),
                "gripper_open": gripper_open, "reason": reason}

    def check_task(self, *, observer: bool = False) -> dict:
        """Evaluate the success criteria. With the tracker dead, agents do not get the cube position."""
        if observer:
            return self._check_task(truth=True)
        return self._tool("check_task", {}, lambda: self._check_task(truth=False))

    # ------------------------------------------------------------- control
    def reset_scene(self) -> dict:
        """Robot to home, cube to its start, faults cleared, triggers re-armed."""
        self.faults.clear(ctrl_hold=False)
        self._reset_physics()
        self.triggers.rearm()
        self._tool_counts.clear()
        self._last_move = None
        self.log_event("reset", {})
        self._record_frame()
        self._flush_frames()
        return {"ok": True}

    def _apply_sabotage(self, spec: dict, source: str) -> None:
        thawed = list(self.faults.frozen) if spec["kind"] == "clear" else []
        label = self.faults.apply(spec)
        for j in thawed:  # the servo now holds where the joint is; keep the bookkeeping in sync
            self.driver.q_cmd[j] = self.data.qpos[self.ik.qadr[j]]
        # A clear is logged as a "sabotage" event with spec.kind "clear" (the dashboard and
        # the video show it without a banner). frame_index: the frame recorded right after.
        self.log_event("sabotage", {"spec": spec, "label": label, "source": source,
                                    "active": self.faults.active_names(), "frame_index": self._frame_index + 1})
        self._record_frame()

    def apply_sabotage(self, spec: dict) -> dict:
        """Inject a fault now (see sabotage.py); returns the active fault names."""
        spec = normalize_spec(spec)
        self._apply_sabotage(spec, source="api")
        self._flush_frames()
        return {"ok": True, "active": self.faults.active_names()}

    def clear_sabotage(self) -> dict:
        """Remove every active fault."""
        self._apply_sabotage({"kind": "clear"}, source="api")
        self._flush_frames()
        return {"ok": True}


def _json_default(o: Any) -> Any:
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.generic):
        return o.item()
    raise TypeError(f"not JSON serializable: {type(o).__name__}")
