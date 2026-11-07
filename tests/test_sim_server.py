"""(f) REST smoke test against a live sim_server on a random port: every route
answers in the contract shape, errors are HTTP 400 JSON, observers are not
logged as tool calls, and the WebSocket streams events and frames."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest
from websockets.sync.client import connect

from sim_helpers import DEMO_DIR


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    run_dir = tmp_path_factory.mktemp("server") / "run"
    port = _free_port()
    log = open(run_dir.parent / "server.log", "wb")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "sabotage_demo.sim_server", "--scenario", "scenarios/baseline.json",
         "--run-dir", str(run_dir), "--port", str(port), "--realtime", "0"],
        cwd=DEMO_DIR, stdout=log, stderr=subprocess.STDOUT, env=env)
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 90
    while True:
        assert proc.poll() is None, (run_dir.parent / "server.log").read_text(errors="replace")
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        assert time.monotonic() < deadline, "sim_server did not come up"
        time.sleep(0.3)
    with httpx.Client(base_url=url, timeout=60) as client:
        yield client, run_dir, url
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


def test_rest_routes(server):
    http, run_dir, _ = server
    health = http.get("/health").json()
    assert health["ok"] is True and health["scenario"] == "baseline" and health["sim_time"] == 0.0
    assert "Panda" in http.get("/describe").json()["text"]

    state = http.get("/state").json()
    assert set(state) >= {"sim_time", "joints", "tcp", "gripper", "objects", "sensors", "contacts", "active_faults"}
    assert len(state["joints"]["pos"]) == 7 and state["objects"]["cube"]["pos"][2] == pytest.approx(0.02, abs=1e-3)

    moved = http.post("/move_ee", json={"pos": [0.5, 0.0, 0.14], "quat": None, "duration_s": 1.0,
                                        "excluded_joints": []}).json()
    assert moved["reached"] is True and moved["final_error_m"] < 0.015
    assert set(moved) == {"reached", "final_error_m", "tcp_pos", "tcp_quat", "sim_time", "joint_tracking_error", "note"}
    joints = http.post("/move_joints", json={"q": state["joints"]["pos"], "duration_s": 1.0}).json()
    assert joints["reached"] is True
    grip = http.post("/gripper", json={"action": "close", "duration_s": 0.3}).json()
    assert set(grip) == {"opening_m", "holding", "sim_time"} and grip["holding"] is False
    stepped = http.post("/step", json={"seconds": 0.25}).json()
    assert stepped["sim_time"] > grip["sim_time"]

    jpeg = http.get("/render", params={"camera": "front", "format": "jpeg"})
    assert jpeg.status_code == 200 and jpeg.headers["content-type"] == "image/jpeg"
    assert jpeg.content[:2] == b"\xff\xd8" and len(jpeg.content) > 10_000
    png = http.get("/render", params={"camera": "wrist", "format": "png"})
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"

    located = http.post("/locate_object", json={"color": "green", "camera": "top"}).json()
    assert located["found"] and located["method"] == "color_segmentation"
    diag = http.get("/diagnose").json()
    assert len(diag["joints"]) == 7 and isinstance(diag["summary"], str)
    check = http.get("/check_task").json()
    assert check["success"] is False and check["zone_pos"] == [0.5, -0.25, 0.001]

    cut = http.post("/sabotage", json={"kind": "cut_object_sensor"}).json()
    assert cut == {"ok": True, "active": ["cut_object_sensor"]}
    assert http.get("/state").json()["objects"]["cube"] is None
    assert http.post("/sabotage/clear").json() == {"ok": True}
    assert http.get("/state").json()["objects"]["cube"] is not None
    assert http.post("/reset").json() == {"ok": True}

    bad_requests = [("/move_ee", {}), ("/move_ee", {"pos": [0.5, 0.0]}), ("/gripper", {"action": "wiggle"}),
                    ("/sabotage", {"kind": "explode"}),
                    ("/move_ee", {"pos": [0.5, 0, 0.1], "excluded_joints": [9]}),
                    ("/move_ee", {"pos": [0.5, 0, -0.05]}), ("/move_ee", {"pos": [0.5, 0, 0.2], "quat": [1, 0, 0, 0]})]
    for path, body in bad_requests:
        bad = http.post(path, json=body)
        assert bad.status_code == 400 and "error" in bad.json(), (path, body, bad.text)
    assert http.get("/render", params={"camera": "nope"}).status_code == 400

    before = http.get("/events").json()["next"]
    http.get("/state", headers={"X-Observer": "1"})
    http.get("/check_task", params={"source": "director"})
    assert http.get("/events").json()["next"] == before, "observer reads must not be logged"
    assert http.post("/events", json={"type": "agent_text", "payload": {"text": "hello"}}).json() == {"ok": True}

    log = http.get("/events", params={"since": 0}).json()
    events = log["events"]
    assert log["next"] == len(events) and events[-1]["type"] == "agent_text"
    assert http.get("/events", params={"since": log["next"]}).json() == {"events": [], "next": log["next"]}
    tools = [e["payload"]["tool"] for e in events if e["type"] == "tool_call"]
    assert tools[:4] == ["describe_scene", "get_state", "move_ee", "move_joints"]
    assert "render" in tools and "wait" in tools and "set_gripper" in tools
    call = next(e for e in events if e["type"] == "tool_call" and e["payload"]["tool"] == "move_ee")
    assert isinstance(call["payload"]["frame_index"], int)
    assert any(e["type"] == "sabotage" and e["payload"]["label"] == "Object tracker cut" for e in events)

    latest = http.get("/frames/latest.jpg")
    assert latest.status_code == 200 and latest.content[:2] == b"\xff\xd8"
    assert http.get("/").status_code == 200

    lines = (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["type"] == "run_start" and len(lines) == len(events)
    assert len(list((run_dir / "frames").glob("*.jpg"))) > 40


def test_websocket_streams_events_and_frames(server):
    http, _, url = server
    with connect(url.replace("http", "ws") + "/ws", max_size=None, open_timeout=10) as ws:
        first = json.loads(ws.recv(timeout=10))
        assert first["kind"] == "event" and first["type"] == "run_start"
        http.post("/step", json={"seconds": 0.5})
        kinds, deadline = set(), time.monotonic() + 10
        while kinds != {"event", "frame"} and time.monotonic() < deadline:
            msg = json.loads(ws.recv(timeout=10))
            kinds.add(msg["kind"])
            if msg["kind"] == "frame":
                assert msg["jpeg_b64"] and isinstance(msg["index"], int) and "sim_time" in msg
        assert kinds == {"event", "frame"}


def test_cli_paths_resolve_like_the_contract(monkeypatch):
    from sabotage_demo.sim_server import resolve_run_dir, resolve_scenario

    monkeypatch.chdir(DEMO_DIR)  # CONTRACT.md: run from demo/ with ../scenarios and ../runs
    assert resolve_scenario("../scenarios/baseline.json") == DEMO_DIR / "scenarios" / "baseline.json"
    assert resolve_scenario("scenarios/baseline.json") == DEMO_DIR / "scenarios" / "baseline.json"
    assert resolve_run_dir("../runs/x") == DEMO_DIR / "runs" / "x"
    assert resolve_run_dir("runs/x") == DEMO_DIR / "runs" / "x"
    absolute = DEMO_DIR.parent / "elsewhere" / "run"
    assert resolve_run_dir(str(absolute)) == absolute
