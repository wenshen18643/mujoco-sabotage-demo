"""Stub server for developing/testing sabotage_demo/dashboard/index.html (builder C).

Not a simulator. It serves the dashboard at ``/`` and fakes the parts of
sim_server's API the page uses (CONTRACT.md "REST API"): ``/health``,
``/state``, ``/events``, ``/frames/latest.jpg``, ``/sabotage``,
``/sabotage/clear`` and ``WS /ws`` (event backlog, then live events and
``frame`` messages at <= 10/s). Events and frames come from replaying a run
directory in a loop (default: a fresh fixture run from make_fixture_run).

Test hooks (not part of the contract):
    POST /_stub/ws         {"accept": false}  close sockets and refuse new ones
    POST /_stub/fail_next  {"error": "..."}   next /sabotage returns HTTP 400
    GET  /_stub/stats                         request counters + sabotage bodies

    cd demo && ..\\.venv\\Scripts\\python.exe tests\\dashboard_stub_server.py --port 8799
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
if str(DEMO) not in sys.path:
    sys.path.insert(0, str(DEMO))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402

from sabotage_demo import video as V  # noqa: E402

DASHBOARD = DEMO / "sabotage_demo" / "dashboard" / "index.html"


class Stub:
    def __init__(self, run_dir: Path, speed: float = 1.0, loop_replay: bool = True) -> None:
        self.run = V.load_run(run_dir)
        self.anchors = V._anchors(self.run)
        self.speed = speed
        self.loop_replay = loop_replay
        self.scenario = self.run.scenario.get("name") or "stub"
        self.events: list[dict] = []
        self.faults: list[str] = []
        self.tracker_dead = False
        self.frame_index = 0
        self.sim_time = 0.0
        self.tcp = [0.31, 0.0, 0.40]
        self.cube = [0.5, 0.0, 0.02]
        self.opening, self.holding = 0.08, False
        self.joints = [0.0, -0.3, 0.0, -2.2, 0.0, 1.9, 0.78]
        self.ws_accept = True
        self.fail_next: str | None = None
        self.hits: Counter = Counter()
        self.sabotage_posts: list[dict] = []
        self.clients: set[asyncio.Queue] = set()
        self.sockets: set[WebSocket] = set()
        self.lock = threading.Lock()

    # ------------------------------------------------------------------ state
    def log(self, type_: str, payload: dict) -> dict:
        with self.lock:
            ev = {"seq": len(self.events), "wall_time": time.time(), "sim_time": round(self.sim_time, 3),
                  "type": type_, "payload": payload}
            self.events.append(ev)
        self._broadcast({"kind": "event", **ev})
        self._absorb(ev)
        return ev

    def _absorb(self, ev: dict) -> None:
        p = ev["payload"]
        if ev["type"] == "tool_result" and isinstance(p.get("result"), dict):
            r = p["result"]
            if isinstance(r.get("tcp_pos"), list):
                self.tcp = r["tcp_pos"]
            if "opening_m" in r:
                self.opening, self.holding = r["opening_m"], bool(r.get("holding"))
            cube = (r.get("objects") or {}).get("cube") if isinstance(r.get("objects"), dict) else None
            if isinstance(cube, dict):
                self.cube = cube["pos"]
        elif ev["type"] == "sabotage":
            self._apply_fault(p.get("spec") or {})
        elif ev["type"] == "run_start":
            self.faults.clear()
            self.tracker_dead = False

    def _apply_fault(self, spec: dict) -> None:
        kind = spec.get("kind")
        if kind == "freeze_joint":
            name = f"freeze_joint:{int(spec['joint'])}"
            if name not in self.faults:
                self.faults.append(name)
        elif kind == "cut_object_sensor":
            self.tracker_dead = True
            if "cut_object_sensor" not in self.faults:
                self.faults.append("cut_object_sensor")
        elif kind == "teleport_cube":
            if spec.get("delta") is not None:
                self.cube = [a + b for a, b in zip(self.cube, spec["delta"])]
            elif spec.get("pos") is not None:
                self.cube = list(spec["pos"])
        elif kind == "clear":
            self.faults.clear()
            self.tracker_dead = False

    def state(self) -> dict:
        return {
            "sim_time": round(self.sim_time, 3),
            "joints": {"pos": self.joints, "vel": [0.0] * 7, "target": self.joints,
                       "names": [f"joint{i}" for i in range(1, 8)]},
            "tcp": {"pos": self.tcp, "quat": [0.0, 1.0, 0.0, 0.0]},
            "gripper": {"opening_m": self.opening, "target": "open" if self.opening > 0.04 else "close",
                        "holding": self.holding},
            "objects": {"cube": None if self.tracker_dead else {"pos": self.cube, "quat": [1, 0, 0, 0]},
                        "target_zone": {"pos": [0.5, -0.25, 0.001], "half_size": [0.06, 0.06, 0.001]}},
            "sensors": {"object_tracker": "dead" if self.tracker_dead else "ok"},
            "contacts": {"cube_fingers": 2 if self.holding else 0, "cube_table": not self.holding},
            "active_faults": list(self.faults),
        }

    def latest_jpeg(self) -> bytes | None:
        files = self.run.frame_files
        if not files:
            return None
        p = files[min(self.frame_index, len(files) - 1)]
        return p.read_bytes() if p else None

    # ------------------------------------------------------------------ fan-out
    def _broadcast(self, msg: dict) -> None:
        loop = getattr(self, "loop", None)
        if loop is None:
            return
        for q in list(self.clients):
            loop.call_soon_threadsafe(q.put_nowait, msg)

    async def replay(self) -> None:
        """Replay the run: frames at 20 fps real time, events at their frame positions."""
        dt = 0.05 / self.speed
        while True:
            self.frame_index = 0
            for ev, pos in zip(self.run.events, self.anchors):
                while self.frame_index < pos:
                    self.frame_index += 1
                    self.sim_time = self.run.frame_times[self.frame_index]
                    await asyncio.sleep(dt)
                self.log(ev["type"], ev.get("payload") or {})
                pause = {"agent_text": 0.9, "sabotage": 0.6, "judge": 1.0}.get(ev["type"], 0.25)
                await asyncio.sleep(pause / self.speed)
            if not self.loop_replay:
                return
            await asyncio.sleep(4.0 / self.speed)

    async def push_frames(self) -> None:
        last = -1
        while True:
            await asyncio.sleep(0.1)
            if self.frame_index == last or not self.clients:
                continue
            last = self.frame_index
            jpg = self.latest_jpeg()
            if jpg is None:
                continue
            msg = {"kind": "frame", "index": last, "sim_time": round(self.sim_time, 3),
                   "jpeg_b64": base64.b64encode(jpg).decode("ascii")}
            for q in list(self.clients):
                if q.qsize() < 20:
                    q.put_nowait(msg)


def build_app(stub: Stub) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        stub.loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(stub.replay()), asyncio.create_task(stub.push_frames())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="dashboard stub", lifespan=lifespan)

    @app.middleware("http")
    async def count(request: Request, call_next):
        stub.hits[request.url.path] += 1
        return await call_next(request)

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "scenario": stub.scenario, "sim_time": round(stub.sim_time, 3)}

    @app.get("/state")
    async def state() -> dict:
        return stub.state()

    @app.get("/events")
    async def events(since: int = 0) -> dict:
        since = max(0, since)
        evs = stub.events[since:]
        return {"events": evs, "next": since + len(evs)}

    @app.get("/frames/latest.jpg")
    async def latest() -> Response:
        jpg = stub.latest_jpeg()
        if jpg is None:
            return JSONResponse({"error": "no frame recorded yet"}, status_code=404)
        return Response(jpg, media_type="image/jpeg", headers={
            "X-Frame-Index": str(stub.frame_index), "X-Sim-Time": f"{stub.sim_time:.3f}",
            "Cache-Control": "no-store"})

    @app.post("/sabotage")
    async def sabotage(request: Request):
        body = await request.json()
        stub.sabotage_posts.append(body)
        if stub.fail_next is not None:
            msg, stub.fail_next = stub.fail_next, None
            return JSONResponse({"error": msg}, status_code=400)
        kind = body.get("kind")
        if kind not in ("freeze_joint", "cut_object_sensor", "teleport_cube", "clear"):
            return JSONResponse({"error": f"unknown sabotage kind {kind!r}"}, status_code=400)
        if kind == "freeze_joint" and not (isinstance(body.get("joint"), int) and 0 <= body["joint"] <= 6):
            return JSONResponse({"error": "joint must be an integer 0..6"}, status_code=400)
        stub.log("sabotage", {"spec": body, "label": V.sabotage_label({"spec": body})})
        return {"ok": True, "active": list(stub.faults)}

    @app.post("/sabotage/clear")
    async def clear() -> dict:
        stub.log("sabotage", {"spec": {"kind": "clear"}, "label": "Faults cleared"})
        return {"ok": True}

    @app.post("/_stub/ws")
    async def ws_mode(request: Request) -> dict:
        stub.ws_accept = bool((await request.json()).get("accept", True))
        if not stub.ws_accept:
            for s in list(stub.sockets):
                try:
                    await s.close(code=1013)
                except RuntimeError:
                    pass
        return {"ok": True}

    @app.post("/_stub/fail_next")
    async def fail_next(request: Request) -> dict:
        stub.fail_next = str((await request.json()).get("error") or "stub failure")
        return {"ok": True}

    @app.get("/_stub/stats")
    async def stats() -> dict:
        return {"hits": dict(stub.hits), "sabotage_posts": stub.sabotage_posts, "events": len(stub.events),
                "ws_clients": len(stub.sockets)}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        stub.hits["/ws"] += 1
        if not stub.ws_accept:
            await websocket.close(code=1013)
            return
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        stub.clients.add(q)
        stub.sockets.add(websocket)
        try:
            backlog = list(stub.events)
            last = backlog[-1]["seq"] if backlog else -1
            for ev in backlog:
                await websocket.send_json({"kind": "event", **ev})
            while True:
                msg = await q.get()
                if msg.get("kind") == "event" and msg.get("seq", 1 << 62) <= last:
                    continue
                await websocket.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            stub.clients.discard(q)
            stub.sockets.discard(websocket)

    return app


def default_run(tmp_root: Path | None = None, short: bool = False) -> Path:
    from sabotage_demo.make_fixture_run import make_fixture_run

    root = tmp_root or Path(tempfile.mkdtemp(prefix="dash_stub_"))
    return make_fixture_run("dashboard_stub", root, sabotage="teleport", time_scale=0.6 if short else 1.0,
                            short=short)


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    ap = argparse.ArgumentParser(description="Stub server for the dashboard (no simulator).")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--replay", default=None, help="run dir to replay (default: fresh fixture run)")
    ap.add_argument("--speed", type=float, default=1.0)
    a = ap.parse_args(argv)
    run_dir = Path(a.replay) if a.replay else default_run()
    stub = Stub(run_dir, a.speed)
    print(f"dashboard stub: http://127.0.0.1:{a.port}/  (replaying {run_dir})")
    uvicorn.run(build_app(stub), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
