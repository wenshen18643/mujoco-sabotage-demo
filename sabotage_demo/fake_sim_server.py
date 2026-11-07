"""TEST-ONLY stand-in for sim_server.py. No physics, no MuJoCo.

Implements the REST routes from CONTRACT.md with a tiny kinematic state
machine (TCP teleports along straight lines, the cube sticks to the gripper
when grasped, faults behave like the real ones at the API level) and a
synthetic 960x720 JPEG renderer (top-down sketch with a red square for the
cube) so the MCP server and the director can be developed and tested without
builder A's simulator. Do NOT use it for the demo video.

    python -m sabotage_demo.fake_sim_server --scenario ../scenarios/x.json --run-dir ../runs/x --port 8765

Not implemented: WS /ws, the dashboard, real IK. Scenario triggers supported:
tool_call, ee_near_cube, cube_lifted.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image, ImageDraw

W, H = 960, 720
TOP_DOWN_QUAT = [0.0, 1.0, 0.0, 0.0]
HOME_Q = [0.0, 0.0, 0.0, -1.5708, 0.0, 1.5708, -0.7854]
HOME_TCP = [0.307, 0.0, 0.487]
CUBE_START = [0.50, 0.00, 0.02]
ZONE = [0.50, -0.25, 0.001]
ZONE_HALF = [0.06, 0.06, 0.001]
FRAME_DT = 0.05
SPEED = 0.25  # m/s nominal

ROUTE_TOOLS = {
    "/describe": "describe_scene",
    "/state": "get_state",
    "/move_ee": "move_ee",
    "/move_joints": "move_joints",
    "/gripper": "set_gripper",
    "/step": "wait",
    "/render": "render",
    "/locate_object": "locate_object",
    "/diagnose": "diagnose",
    "/check_task": "check_task",
}

DESCRIBE_TEXT = (
    "FAKE SIMULATOR (test stand-in). A Franka Panda arm is mounted at the world origin on a "
    "table whose top is z = 0. World frame: +x forward, +y left, +z up; metres, radians, "
    "seconds. A red 4 cm cube (centre z = 0.02 when resting) starts near [0.50, 0.00, 0.02]. "
    "A flat green target zone (12 x 12 cm) is centred at [0.50, -0.25, 0.001]. Cameras: "
    "front, top, wrist. The TCP is the point between the fingertips; the default "
    "orientation points the fingers straight down."
)


def _dist(a, b) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


class FakeSim:
    def __init__(self, run_dir: Path, scenario: dict):
        self.run_dir = run_dir
        self.scenario = scenario
        self.lock = threading.RLock()
        self.events: list[dict] = []
        self.frame_index = -1
        (run_dir / "frames").mkdir(parents=True, exist_ok=True)
        self._events_f = open(run_dir / "events.jsonl", "a", encoding="utf-8")
        self._frames_f = open(run_dir / "frames.jsonl", "a", encoding="utf-8")
        self.tool_counts: dict[str, int] = {}
        self.fired: set[int] = set()
        self.reset()
        self.log_event("run_start", {"scenario": scenario.get("name", "?"), "title": scenario.get("title", ""), "task": scenario.get("task", "")})
        self.record_frame()

    # ---- state -------------------------------------------------------
    def reset(self) -> None:
        self.sim_time = 0.0
        self.q = list(HOME_Q)
        self.q_target = list(HOME_Q)
        self.tcp = list(HOME_TCP)
        # test fixtures may start the cube elsewhere: {"fake_initial": {"cube_pos": [x, y, z]}}
        self.cube = [float(v) for v in self.scenario.get("fake_initial", {}).get("cube_pos", CUBE_START)]
        self.cube_vel = 0.0
        self.grip_target = "open"
        self.opening = 0.08
        self.holding = False
        self.frozen: set[int] = set()
        self.tracker_dead = False
        self.active: list[str] = []
        self.last_move: dict | None = None
        self.max_cube_z = self.cube[2]

    def log_event(self, type_: str, payload: dict) -> dict:
        with self.lock:
            ev = {"seq": len(self.events), "wall_time": time.time(), "sim_time": round(self.sim_time, 4), "type": type_, "payload": payload}
            self.events.append(ev)
            self._events_f.write(json.dumps(ev) + "\n")
            self._events_f.flush()
            return ev

    def state(self) -> dict:
        fingers = 2 if self.holding else 0
        return {
            "sim_time": round(self.sim_time, 4),
            "joints": {"pos": self.q, "vel": [0.0] * 7, "target": self.q_target, "names": [f"joint{i + 1}" for i in range(7)]},
            "tcp": {"pos": self.tcp, "quat": TOP_DOWN_QUAT},
            "gripper": {"opening_m": self.opening, "target": self.grip_target, "holding": self.holding},
            "objects": {
                "cube": None if self.tracker_dead else {"pos": self.cube, "quat": [1.0, 0.0, 0.0, 0.0]},
                "target_zone": {"pos": ZONE, "half_size": ZONE_HALF},
            },
            "sensors": {"object_tracker": "dead" if self.tracker_dead else "ok"},
            "contacts": {"cube_fingers": fingers, "cube_table": (not self.holding) and self.cube[2] < 0.025},
            "active_faults": list(self.active),
        }

    # ---- frames ------------------------------------------------------
    def render(self, camera: str = "front") -> bytes:
        img = Image.new("RGB", (W, H), (205, 205, 210))
        d = ImageDraw.Draw(img)

        def px(p):  # top-down-ish projection of the workspace
            u = W / 2 - (p[1] + 0.1) * 1100
            v = H - 80 - (p[0] - 0.1) * 900 - p[2] * 600
            return u, v

        d.rectangle([0, 0, W, 60], fill=(40, 40, 48))
        d.text((12, 20), f"FAKE SIM  camera={camera}  sim_time={self.sim_time:6.2f}s  faults={len(self.active)}", fill=(255, 255, 255))
        zu, zv = px(ZONE)
        d.rectangle([zu - 66, zv - 54, zu + 66, zv + 54], fill=(40, 170, 60))
        bu, bv = px([0.0, 0.0, 0.0])
        d.rectangle([bu - 40, bv - 30, bu + 40, bv + 30], fill=(235, 235, 240), outline=(60, 60, 60))
        cu, cv = px(self.cube)
        s = 22 + self.cube[2] * 40
        d.rectangle([cu - s, cv - s, cu + s, cv + s], fill=(220, 30, 30), outline=(90, 0, 0), width=3)
        tu, tv = px(self.tcp)
        d.line([bu, bv, tu, tv], fill=(90, 90, 100), width=10)
        gap = 6 + self.opening * 300
        d.rectangle([tu - gap - 6, tv - 16, tu - gap, tv + 16], fill=(30, 30, 30))
        d.rectangle([tu + gap, tv - 16, tu + gap + 6, tv + 16], fill=(30, 30, 30))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def record_frame(self) -> None:
        self.frame_index += 1
        (self.run_dir / "frames" / f"{self.frame_index:06d}.jpg").write_bytes(self.render("front"))
        self._frames_f.write(json.dumps({"index": self.frame_index, "sim_time": round(self.sim_time, 4), "wall_time": time.time()}) + "\n")
        self._frames_f.flush()

    def advance(self, seconds: float, per_step=None) -> None:
        n = max(1, int(round(seconds / FRAME_DT)))
        for i in range(n):
            self.sim_time += seconds / n
            if per_step:
                per_step((i + 1) / n)
            self.check_state_triggers()
            self.record_frame()

    # ---- sabotage ----------------------------------------------------
    def apply_sabotage(self, spec: dict) -> dict:
        kind = spec.get("kind")
        label = kind
        if kind == "freeze_joint":
            j = int(spec["joint"])
            self.frozen.add(j)
            self.active.append(f"freeze_joint:{j}")
            label = f"Joint {j + 1} frozen"
        elif kind == "cut_object_sensor":
            self.tracker_dead = True
            self.active.append("cut_object_sensor")
            label = "Object sensor cut"
        elif kind == "teleport_cube":
            if "pos" in spec:
                self.cube = [float(v) for v in spec["pos"]]
            else:
                self.cube = [c + float(dv) for c, dv in zip(self.cube, spec.get("delta", [0, 0, 0]))]
            self.cube[2] = max(self.cube[2], 0.02)
            if self.holding:
                self.holding = False
                self.cube[2] = 0.02
            label = "Cube moved"
        elif kind == "clear":
            self.clear_sabotage()
            label = "Faults cleared"
        else:
            raise ValueError(f"unknown sabotage kind {kind!r}")
        self.log_event("sabotage", {"spec": spec, "label": label})
        self.record_frame()
        return {"ok": True, "active": list(self.active)}

    def clear_sabotage(self) -> None:
        self.frozen.clear()
        self.tracker_dead = False
        self.active = []

    def fire_triggers(self, pred) -> None:
        for i, trig in enumerate(self.scenario.get("triggers", [])):
            if i in self.fired:
                continue
            if pred(trig.get("when", {})):
                self.fired.add(i)
                self.apply_sabotage(trig["sabotage"])

    def check_state_triggers(self) -> None:
        def pred(w):
            if w.get("type") == "ee_near_cube":
                return self.grip_target == "open" and _dist(self.tcp, self.cube) < float(w.get("dist", 0.05))
            if w.get("type") == "cube_lifted":
                return self.cube[2] > float(w.get("z", 0.05))
            if w.get("type") == "sim_time":
                return self.sim_time >= float(w.get("t", 1e9))
            return False

        self.fire_triggers(pred)

    def tool_trigger(self, tool: str, phase: str) -> None:
        n = self.tool_counts.get(tool, 0)
        self.fire_triggers(lambda w: w.get("type") == "tool_call" and w.get("tool") == tool and int(w.get("n", 1)) == n and w.get("phase", "before") == phase)

    # ---- motions -----------------------------------------------------
    def move_ee(self, pos, quat=None, duration_s=2.0, excluded_joints=()) -> dict:
        target = [float(v) for v in pos]
        excl = {int(j) for j in excluded_joints or []}
        blocking = self.frozen - excl
        start = list(self.tcp)
        if blocking:
            # a frozen joint the planner relies on: the arm stops short
            end = [t + (s - t) * 0.6 for s, t in zip(start, target)]
            end[2] += 0.04
        else:
            end = list(target)

        def step(f):
            self.tcp = [s + (e - s) * f for s, e in zip(start, end)]
            if self.holding:
                self.cube = [self.tcp[0], self.tcp[1], self.tcp[2]]
                self.max_cube_z = max(self.max_cube_z, self.cube[2])

        self.advance(float(duration_s), step)
        err = _dist(self.tcp, target)
        tracking = [0.0] * 7
        for j in blocking:
            tracking[j] = 0.35
        worst = max(range(7), key=lambda j: abs(tracking[j])) if blocking else None
        self.last_move = {"reached": err < 0.015, "final_error_m": err, "worst_joint": worst}
        note = "" if err < 0.015 else f"target not reached: final error {err:.3f} m"
        return {"reached": err < 0.015, "final_error_m": err, "tcp_pos": self.tcp, "tcp_quat": TOP_DOWN_QUAT, "sim_time": round(self.sim_time, 4), "joint_tracking_error": tracking, "note": note}

    def gripper(self, action: str, duration_s: float = 0.5) -> dict:
        if action not in ("open", "close"):
            raise ValueError("action must be 'open' or 'close'")
        self.grip_target = action
        if action == "close":
            if _dist(self.tcp, self.cube) < 0.025:
                self.holding, self.opening = True, 0.04
            else:
                self.holding, self.opening = False, 0.0
        else:
            if self.holding:
                self.cube = [self.tcp[0], self.tcp[1], 0.02]
            self.holding, self.opening = False, 0.08
        self.advance(float(duration_s))
        return {"opening_m": self.opening, "holding": self.holding, "sim_time": round(self.sim_time, 4)}

    def diagnose(self) -> dict:
        joints = []
        for i in range(7):
            frozen = i in self.frozen and self.last_move is not None and not self.last_move.get("reached", True)
            err = 0.35 if frozen else 0.001
            joints.append({"index": i, "name": f"joint{i + 1}", "commanded": self.q[i] + err, "actual": self.q[i], "error": err, "velocity": 0.0, "suspected_frozen": frozen})
        anomalies = [f"joint index {j['index']} ({j['name']}) did not follow its command and appears frozen" for j in joints if j["suspected_frozen"]]
        if self.tracker_dead:
            anomalies.append("object tracker is not reporting (objects.cube is null)")
        return {
            "sim_time": round(self.sim_time, 4),
            "joints": joints,
            "object_tracker": "dead" if self.tracker_dead else "ok",
            "gravity": [0, 0, -9.81],
            "last_move": self.last_move,
            "contacts": self.state()["contacts"],
            "summary": "; ".join(anomalies) if anomalies else "no anomalies detected",
        }

    def check_task(self) -> dict:
        xy = math.hypot(self.cube[0] - ZONE[0], self.cube[1] - ZONE[1])
        on_table = (not self.holding) and self.cube[2] < 0.03
        gopen = self.grip_target == "open" and not self.holding
        ok = xy < 0.05 and on_table and gopen
        reason = "cube is in the target zone" if ok else ("cube not within 0.05 m of zone centre" if xy >= 0.05 else ("cube not resting on table" if not on_table else "gripper not open / still touching cube"))
        return {"success": ok, "cube_pos": self.cube, "zone_pos": ZONE, "xy_distance_m": xy, "cube_on_table": on_table, "gripper_open": gopen, "reason": reason}


def create_app(run_dir: Path, scenario: dict) -> FastAPI:
    sim = FakeSim(run_dir, scenario)
    app = FastAPI(title="FAKE sim server (test only)")
    app.state.sim = sim

    def err(msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=400)

    def tool(route: str, args: dict, fn):
        name = ROUTE_TOOLS[route]
        with sim.lock:
            sim.tool_counts[name] = sim.tool_counts.get(name, 0) + 1
            sim.log_event("tool_call", {"tool": name, "args": args, "frame_index": sim.frame_index})
            t0 = time.time()
            sim.tool_trigger(name, "before")
            try:
                result = fn()
            except (KeyError, ValueError, TypeError) as e:
                return err(f"{type(e).__name__}: {e}")
            sim.tool_trigger(name, "after")
            logged = {"image": True} if isinstance(result, Response) else result
            sim.log_event("tool_result", {"tool": name, "result": logged, "frame_index": sim.frame_index, "duration_s": time.time() - t0})
        return result

    @app.get("/health")
    def health():
        return {"ok": True, "scenario": scenario.get("name", "?"), "sim_time": round(sim.sim_time, 4), "fake": True}

    @app.get("/describe")
    def describe():
        return tool("/describe", {}, lambda: {"text": DESCRIBE_TEXT})

    @app.get("/state")
    def state():
        return tool("/state", {}, sim.state)

    @app.post("/move_ee")
    async def move_ee(req: Request):
        b = await req.json()
        return tool("/move_ee", b, lambda: sim.move_ee(b["pos"], b.get("quat"), b.get("duration_s", 2.0), b.get("excluded_joints") or []))

    @app.post("/move_joints")
    async def move_joints(req: Request):
        b = await req.json()

        def run():
            if len(b["q"]) != 7:
                raise ValueError("q must have 7 values")
            sim.q_target = [float(v) for v in b["q"]]
            sim.advance(float(b.get("duration_s", 2.0)))
            sim.q = list(sim.q_target)
            return {"reached": True, "final_error_m": 0.0, "tcp_pos": sim.tcp, "tcp_quat": TOP_DOWN_QUAT, "sim_time": round(sim.sim_time, 4), "joint_tracking_error": [0.0] * 7, "note": "fake: tcp not recomputed"}

        return tool("/move_joints", b, run)

    @app.post("/gripper")
    async def gripper(req: Request):
        b = await req.json()
        return tool("/gripper", b, lambda: sim.gripper(b["action"], b.get("duration_s", 0.5)))

    @app.post("/step")
    async def step(req: Request):
        b = await req.json()

        def run():
            sim.advance(float(b["seconds"]))
            return {"sim_time": round(sim.sim_time, 4)}

        return tool("/step", b, run)

    @app.get("/render")
    def render(camera: str = "front", format: str = "jpeg"):
        def run():
            if camera not in ("front", "top", "wrist"):
                raise ValueError(f"unknown camera {camera!r}")
            return Response(sim.render(camera), media_type="image/jpeg", headers={"X-Sim-Time": f"{sim.sim_time:.4f}"})

        return tool("/render", {"camera": camera, "format": format}, run)

    @app.post("/locate_object")
    async def locate(req: Request):
        b = await req.json()

        def run():
            if (b.get("color") or "red") != "red":
                return {"found": False, "pos": None, "pixel": None, "confidence": 0.0, "method": "color_segmentation"}
            p = [sim.cube[0] + 0.003, sim.cube[1] - 0.002, 0.02 if not sim.holding else sim.cube[2]]
            return {"found": True, "pos": p, "pixel": [480, 360], "confidence": 0.93, "method": "color_segmentation"}

        return tool("/locate_object", b, run)

    @app.get("/diagnose")
    def diagnose():
        return tool("/diagnose", {}, sim.diagnose)

    @app.get("/check_task")
    def check_task():
        return tool("/check_task", {}, sim.check_task)

    @app.post("/reset")
    def reset():
        with sim.lock:
            sim.reset()
        return {"ok": True}

    @app.post("/sabotage")
    async def sabotage(req: Request):
        b = await req.json()
        try:
            with sim.lock:
                return sim.apply_sabotage(b)
        except (KeyError, ValueError) as e:
            return err(str(e))

    @app.post("/sabotage/clear")
    def sabotage_clear():
        with sim.lock:
            sim.clear_sabotage()
        return {"ok": True}

    @app.post("/events")
    async def post_event(req: Request):
        b = await req.json()
        if not isinstance(b.get("type"), str):
            return err("type must be a string")
        sim.log_event(b["type"], b.get("payload") or {})
        return {"ok": True}

    @app.get("/events")
    def get_events(since: int = 0):
        with sim.lock:
            evs = sim.events[since:]
            return {"events": evs, "next": since + len(evs)}

    @app.get("/frames/latest.jpg")
    def latest():
        return Response(sim.render("front"), media_type="image/jpeg")

    @app.get("/")
    def index():
        return HTMLResponse("<h1>FAKE sim server (test only)</h1><img src='/frames/latest.jpg'>")

    return app


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="TEST-ONLY fake sim server")
    p.add_argument("--scenario", required=True)
    p.add_argument("--run-dir", required=True)
    p.add_argument("--port", type=int, default=8765)
    a = p.parse_args(argv)
    run_dir = Path(a.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    scenario = json.loads(Path(a.scenario).read_text(encoding="utf-8"))
    app = create_app(run_dir, scenario)
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
