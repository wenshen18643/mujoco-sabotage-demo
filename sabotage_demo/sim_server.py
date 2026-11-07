"""FastAPI REST + WebSocket server around a single ``SimCore`` (see CONTRACT.md).

Run from ``demo/``::

    python -m sabotage_demo.sim_server --scenario ../scenarios/baseline.json --run-dir ../runs/<id> --port 8765

Every simulator call runs on one dedicated worker thread (which also owns the
OpenGL context), so requests are serialized. Tool-like routes log
``tool_call``/``tool_result`` events and fire scenario triggers. Requests that
send ``X-Observer: 1`` (or ``?observer=1``), and browsers, read state without
being logged as agent tool calls.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .sim_core import SimCore, encode_image

PKG_DIR = Path(__file__).resolve().parent
DEMO_DIR = PKG_DIR.parent
DASHBOARD = PKG_DIR / "dashboard" / "index.html"
PLACEHOLDER_HTML = ("<!doctype html><meta charset='utf-8'><title>Sabotage demo</title>"
                    "<p>Dashboard not built yet (sabotage_demo/dashboard/index.html). "
                    "Latest frame:</p><img src='/frames/latest.jpg' width='640'>")
WS_FRAME_PERIOD = 0.1  # s wall clock: at most 10 frames/s per client
WS_MAX_QUEUE = 2000


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def is_observer(request: Request) -> bool:
    """True for dashboard/director reads that must not count as agent tool calls.

    Observers: header ``X-Observer: 1``, query ``observer=1``, query ``source=<x>``
    with x != "agent" (the director's judge sends ``source=director``), or a browser.
    """
    if _truthy(request.headers.get("x-observer")) or _truthy(request.query_params.get("observer")):
        return True
    source = request.query_params.get("source")
    if source and source.strip().lower() != "agent":
        return True
    return "mozilla" in request.headers.get("user-agent", "").lower()


async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    body = json.loads(raw)
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _opt(body: dict, key: str, default: Any) -> Any:
    value = body.get(key)
    return default if value is None else value


class SimServer:
    """Owns the SimCore, its worker thread and the WebSocket fan-out."""

    def __init__(self, run_dir: Path, scenario: dict, realtime: float = 1.0, record_frames: bool = True):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim")
        self.core: SimCore = self.executor.submit(
            SimCore, run_dir, scenario, realtime=realtime, record_frames=record_frames).result()
        self.scenario = scenario
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self.core.add_listener(self._on_event)
        self._pusher: asyncio.Task | None = None

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """Run ``fn`` on the simulator thread."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, functools.partial(fn, *args, **kwargs))

    def _on_event(self, ev: dict) -> None:  # any thread
        loop = self.loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._broadcast, {"kind": "event", **ev})

    def _broadcast(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.qsize() < WS_MAX_QUEUE:
                q.put_nowait(msg)

    def frame_message(self) -> dict | None:
        frame = self.core.latest_frame()
        if frame is None:
            return None
        index, sim_time, jpeg = frame
        return {"kind": "frame", "index": index, "sim_time": round(sim_time, 3),
                "jpeg_b64": base64.b64encode(jpeg).decode("ascii")}

    async def _push_frames(self) -> None:
        last = -1
        while True:
            await asyncio.sleep(WS_FRAME_PERIOD)
            if not self.clients:
                continue
            frame = self.core.latest_frame()
            if frame is None or frame[0] == last:
                continue
            last = frame[0]
            msg = self.frame_message()
            for q in list(self.clients):
                if q.qsize() < 20:  # slow client: skip frames, never events
                    q.put_nowait(msg)

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._pusher = asyncio.create_task(self._push_frames())

    async def stop(self) -> None:
        if self._pusher:
            self._pusher.cancel()
        self.loop = None
        await asyncio.get_running_loop().run_in_executor(self.executor, self.core.close)
        self.executor.shutdown(wait=True)


def create_app(run_dir: Path, scenario: dict, realtime: float = 1.0, record_frames: bool = True) -> FastAPI:
    """Build the FastAPI app (the SimCore is created immediately)."""
    srv = SimServer(Path(run_dir), scenario, realtime=realtime, record_frames=record_frames)
    core = srv.core

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await srv.start()
        try:
            yield
        finally:
            await srv.stop()

    app = FastAPI(title="Sabotage demo simulator", lifespan=lifespan)
    app.state.server = srv

    @app.exception_handler(ValueError)
    async def _bad_request(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def _missing(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"error": f"missing field {exc}"}, status_code=400)

    @app.exception_handler(TypeError)
    async def _type(_: Request, exc: TypeError) -> JSONResponse:
        return JSONResponse({"error": f"bad request: {exc}"}, status_code=400)

    @app.exception_handler(Exception)
    async def _internal(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": f"internal error: {type(exc).__name__}: {exc}"}, status_code=500)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "scenario": core.scenario_name, "sim_time": round(core.sim_time, 3)}

    @app.get("/scenario")
    async def get_scenario() -> dict:
        return srv.scenario

    @app.get("/describe")
    async def describe(request: Request) -> dict:
        return await srv.call(core.describe, observer=is_observer(request))

    @app.get("/state")
    async def state(request: Request) -> dict:
        return await srv.call(core.get_state, observer=is_observer(request))

    @app.post("/move_ee")
    async def move_ee(request: Request) -> dict:
        b = await _body(request)
        if b.get("pos") is None:
            raise ValueError("pos is required: [x, y, z] in metres")
        return await srv.call(core.move_ee, b["pos"], b.get("quat"), _opt(b, "duration_s", 2.0),
                              _opt(b, "excluded_joints", []))

    @app.post("/move_joints")
    async def move_joints(request: Request) -> dict:
        b = await _body(request)
        if b.get("q") is None:
            raise ValueError("q is required: 7 joint angles in radians")
        return await srv.call(core.move_joints, b["q"], _opt(b, "duration_s", 2.0))

    @app.post("/gripper")
    async def gripper(request: Request) -> dict:
        b = await _body(request)
        return await srv.call(core.set_gripper, b.get("action"), _opt(b, "duration_s", 0.5))

    @app.post("/step")
    async def step(request: Request) -> dict:
        b = await _body(request)
        if b.get("seconds") is None:
            raise ValueError("seconds is required")
        return await srv.call(core.step, b["seconds"])

    @app.get("/render")
    async def render(request: Request, camera: str = "front", format: str = "jpeg") -> Response:  # noqa: A002
        fmt = format.lower()
        if fmt not in ("jpeg", "jpg", "png"):
            raise ValueError("format must be 'jpeg' or 'png'")
        rgb = await srv.call(core.render, camera, observer=is_observer(request))
        data = await run_in_threadpool(encode_image, rgb, "png" if fmt == "png" else "jpeg")
        return Response(data, media_type="image/png" if fmt == "png" else "image/jpeg",
                        headers={"X-Sim-Time": f"{core.sim_time:.3f}", "X-Camera": camera,
                                 "Cache-Control": "no-store"})

    @app.post("/locate_object")
    async def locate_object(request: Request) -> dict:
        b = await _body(request)
        return await srv.call(core.locate_object, _opt(b, "color", "red"), _opt(b, "camera", "top"))

    @app.get("/diagnose")
    async def diagnose(request: Request) -> dict:
        return await srv.call(core.diagnose, observer=is_observer(request))

    @app.get("/check_task")
    async def check_task(request: Request) -> dict:
        return await srv.call(core.check_task, observer=is_observer(request))

    @app.post("/reset")
    async def reset() -> dict:
        return await srv.call(core.reset_scene)

    @app.post("/sabotage")
    async def sabotage(request: Request) -> dict:
        b = await _body(request)
        return await srv.call(core.apply_sabotage, b)

    @app.post("/sabotage/clear")
    async def sabotage_clear() -> dict:
        return await srv.call(core.clear_sabotage)

    @app.post("/events")
    async def post_event(request: Request) -> dict:
        b = await _body(request)
        etype = b.get("type")
        if not isinstance(etype, str) or not etype:
            raise ValueError("type must be a non-empty string")
        payload = b.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        core.log_event(etype, payload)  # thread-safe; does not wait for a running move
        return {"ok": True}

    @app.get("/events")
    async def get_events(since: int = 0) -> dict:
        events, nxt = core.events_since(since)
        return {"events": events, "next": nxt}

    @app.get("/frames/latest.jpg")
    async def latest_frame() -> Response:
        frame = core.latest_frame()
        if frame is None:
            return JSONResponse({"error": "no frame recorded yet"}, status_code=404)
        return Response(frame[2], media_type="image/jpeg",
                        headers={"X-Frame-Index": str(frame[0]), "X-Sim-Time": f"{frame[1]:.3f}",
                                 "Cache-Control": "no-store"})

    @app.get("/")
    async def index() -> HTMLResponse:
        if DASHBOARD.is_file():
            return HTMLResponse(DASHBOARD.read_text(encoding="utf-8"))
        return HTMLResponse(PLACEHOLDER_HTML)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        q: asyncio.Queue = asyncio.Queue()
        srv.clients.add(q)  # subscribe first, then replay history (dedup by seq)
        try:
            backlog, _ = core.events_since(0)
            last_seq = backlog[-1]["seq"] if backlog else -1
            for ev in backlog:
                await websocket.send_json({"kind": "event", **ev})
            frame = srv.frame_message()
            if frame:
                await websocket.send_json(frame)

            async def drain_incoming() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return

            reader = asyncio.create_task(drain_incoming())
            try:
                while not reader.done():
                    getter = asyncio.create_task(q.get())
                    done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                    if getter not in done:
                        getter.cancel()
                        break
                    msg = getter.result()
                    if msg.get("kind") == "event" and msg.get("seq", 1 << 62) <= last_seq:
                        continue
                    await websocket.send_json(msg)
            finally:
                reader.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            srv.clients.discard(q)

    return app


def default_run_dir(scenario_name: str) -> Path:
    """``demo/runs/YYYYMMDD_HHMMSS_<scenario>``."""
    return DEMO_DIR / "runs" / f"{time.strftime('%Y%m%d_%H%M%S')}_{scenario_name}"


def _candidates(path: str) -> list[Path]:
    p = Path(path)
    if p.is_absolute():
        return [p]
    return [(base / p).resolve() for base in (Path.cwd(), PKG_DIR, DEMO_DIR)]


def resolve_scenario(path: str) -> Path:
    """Relative paths: the cwd, then the package dir, then demo/ (so ``../scenarios/x.json``
    works from ``demo/`` as in CONTRACT.md)."""
    for cand in _candidates(path):
        if cand.is_file():
            return cand
    raise SystemExit(f"scenario not found: {path} (tried {', '.join(map(str, _candidates(path)))})")


def resolve_run_dir(path: str) -> Path:
    """Relative paths resolve against the cwd, the package dir, then demo/; the first
    candidate inside demo/ wins (``../runs/<id>`` from ``demo/`` -> ``demo/runs/<id>``)."""
    cands = _candidates(path)
    for cand in cands:
        if cand == DEMO_DIR or DEMO_DIR in cand.parents:
            return cand
    return cands[0]


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="Sabotage demo simulator: REST + WebSocket server.")
    ap.add_argument("--scenario", required=True,
                    help="scenario JSON; relative paths are tried against the cwd, demo/sabotage_demo/ and demo/")
    ap.add_argument("--run-dir", help="run directory (default: demo/runs/<timestamp>_<scenario>); a relative "
                                      "path resolves to the first of cwd, demo/sabotage_demo/, demo/ inside demo/")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--realtime", type=float, default=1.0,
                    help="pace motions at this multiple of real time (0 = as fast as possible)")
    ap.add_argument("--no-frames", action="store_true", help="do not record frames (debugging only)")
    args = ap.parse_args(argv)

    scenario = json.loads(resolve_scenario(args.scenario).read_text(encoding="utf-8"))
    run_dir = resolve_run_dir(args.run_dir) if args.run_dir else default_run_dir(scenario.get("name", "run"))
    app = create_app(run_dir, scenario, realtime=args.realtime, record_frames=not args.no_frames)
    print(f"sim_server: scenario={scenario.get('name')} run_dir={run_dir} "
          f"url=http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
