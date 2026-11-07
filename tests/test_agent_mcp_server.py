"""mcp_server: tool names/schemas, REST mapping, active_faults stripping (httpx mocked),
plus one stdio round trip against the fake sim server (no Claude involved)."""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import jsonschema
import pytest

from sabotage_demo import mcp_server
from sabotage_demo.mcp_server import TOOLS, SimClient, SimError, dispatch, strip_key

DEMO_DIR = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONTRACT_TOOLS = {
    "describe_scene", "get_state", "move_ee", "move_joints", "set_gripper", "wait",
    "render", "locate_object", "diagnose", "check_task", "report_outcome",
}  # fmt: skip

STATE = {
    "sim_time": 1.5,
    "joints": {"pos": [0.0] * 7, "vel": [0.0] * 7, "target": [0.0] * 7, "names": [f"joint{i}" for i in range(1, 8)]},
    "tcp": {"pos": [0.3, 0.0, 0.5], "quat": [0.0, 1.0, 0.0, 0.0]},
    "gripper": {"opening_m": 0.08, "target": "open", "holding": False},
    "objects": {"cube": {"pos": [0.5, 0.0, 0.02], "quat": [1, 0, 0, 0]}, "target_zone": {"pos": [0.5, -0.25, 0.001], "half_size": [0.06, 0.06, 0.001]}},
    "sensors": {"object_tracker": "ok"},
    "contacts": {"cube_fingers": 0, "cube_table": True},
    "active_faults": ["freeze_joint:3"],
}

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes\xff\xd9"


class Recorder:
    """httpx MockTransport handler that records requests and serves canned data."""

    def __init__(self, render_headers: dict | None = None, fail: dict | None = None):
        self.requests: list[httpx.Request] = []
        self.render_headers = render_headers if render_headers is not None else {"X-Sim-Time": "1.2345"}
        self.fail = fail or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path in self.fail:
            return httpx.Response(400, json={"error": self.fail[path]})
        if path == "/state":
            return httpx.Response(200, json=STATE)
        if path == "/diagnose":
            return httpx.Response(200, json={"summary": "joint4 suspected frozen", "joints": [], "active_faults": ["freeze_joint:3"]})
        if path == "/render":
            return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg", **self.render_headers})
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "scenario": "x", "sim_time": 9.87})
        if path == "/describe":
            return httpx.Response(200, json={"text": "A Panda on a table."})
        if path in ("/move_ee", "/move_joints"):
            return httpx.Response(200, json={"reached": True, "final_error_m": 0.0012345678, "tcp_pos": [0.5, 0, 0.14], "tcp_quat": [0, 1, 0, 0], "sim_time": 3.0, "joint_tracking_error": [0.0] * 7, "note": ""})
        return httpx.Response(200, json={"ok": True, "echo": json.loads(request.content or b"{}")})


def run(coro):
    return asyncio.run(coro)


async def call(rec: Recorder, name: str, args: dict | None = None):
    sim = SimClient("http://sim.test", transport=httpx.MockTransport(rec))
    try:
        return await dispatch(sim, name, args or {})
    finally:
        await sim.aclose()


# ---- schemas ---------------------------------------------------------------


def test_tool_names_match_contract():
    assert {t.name for t in TOOLS} == CONTRACT_TOOLS
    assert len(TOOLS) == len(CONTRACT_TOOLS)


@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_tool_schema_is_valid_json_schema(tool):
    schema = tool.inputSchema
    assert schema["type"] == "object"
    jsonschema.Draft202012Validator.check_schema(schema)
    assert tool.description and len(tool.description) > 60


def test_schema_details():
    by = {t.name: t.inputSchema for t in TOOLS}
    assert by["move_ee"]["required"] == ["pos"]
    assert by["move_ee"]["properties"]["pos"]["minItems"] == 3
    assert by["move_ee"]["properties"]["excluded_joints"]["items"]["maximum"] == 6
    assert by["move_joints"]["properties"]["q"]["minItems"] == 7
    assert by["set_gripper"]["properties"]["action"]["enum"] == ["open", "close"]
    assert by["report_outcome"]["properties"]["status"]["enum"] == ["success", "failed"]
    assert set(by["report_outcome"]["required"]) == {"status", "summary"}
    assert "required" not in by["render"]
    jsonschema.validate({"pos": [0.5, 0, 0.1], "excluded_joints": [3]}, by["move_ee"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"pos": [0.5, 0]}, by["move_ee"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"pos": [0.5, 0, 0.1], "excluded_joints": [7]}, by["move_ee"])


def test_descriptions_mention_key_conventions():
    by = {t.name: t.description for t in TOOLS}
    assert "excluded_joints" in by["move_ee"] and "reached" in by["move_ee"]
    assert "holding" in by["get_state"] and "null" in by["get_state"]
    assert "dead" in by["locate_object"] or "null" in by["locate_object"]
    assert "holding" in by["set_gripper"]


# ---- REST mapping ------------------------------------------------------------


def test_strip_key_recursive():
    data = {"a": 1, "active_faults": ["x"], "nested": [{"active_faults": [], "b": 2}]}
    assert strip_key(data) == {"a": 1, "nested": [{"b": 2}]}


def test_get_state_strips_active_faults():
    rec = Recorder()
    out = run(call(rec, "get_state"))
    assert len(out) == 1 and out[0].type == "text"
    data = json.loads(out[0].text)
    assert "active_faults" not in data
    assert "active_faults" not in out[0].text
    assert data["objects"]["cube"]["pos"] == [0.5, 0.0, 0.02]
    assert rec.requests[0].method == "GET" and rec.requests[0].url.path == "/state"


def test_other_tools_never_leak_active_faults():
    out = run(call(Recorder(), "diagnose"))
    assert "active_faults" not in out[0].text
    assert json.loads(out[0].text)["summary"] == "joint4 suspected frozen"


def test_move_ee_body_defaults_and_rounding():
    rec = Recorder()
    out = run(call(rec, "move_ee", {"pos": [0.5, 0, 0.14]}))
    body = json.loads(rec.requests[0].content)
    assert rec.requests[0].url.path == "/move_ee"
    assert body == {"pos": [0.5, 0.0, 0.14], "quat": None, "duration_s": 2.0, "excluded_joints": []}
    assert json.loads(out[0].text)["final_error_m"] == 0.0012


def test_move_ee_with_excluded_joints_and_quat():
    rec = Recorder()
    run(call(rec, "move_ee", {"pos": [0.5, 0, 0.14], "quat": [0, 1, 0, 0], "duration_s": 3, "excluded_joints": [3]}))
    body = json.loads(rec.requests[0].content)
    assert body["quat"] == [0.0, 1.0, 0.0, 0.0] and body["excluded_joints"] == [3] and body["duration_s"] == 3.0


@pytest.mark.parametrize(
    "tool,args,method,path,body",
    [
        ("move_joints", {"q": [0] * 7}, "POST", "/move_joints", {"q": [0.0] * 7, "duration_s": 2.0}),
        ("set_gripper", {"action": "close"}, "POST", "/gripper", {"action": "close", "duration_s": 0.5}),
        ("wait", {"seconds": 1}, "POST", "/step", {"seconds": 1.0}),
        ("locate_object", {}, "POST", "/locate_object", {"color": "red", "camera": "top"}),
        ("diagnose", {}, "GET", "/diagnose", None),
        ("check_task", {}, "GET", "/check_task", None),
        ("report_outcome", {"status": "success", "summary": "done"}, "POST", "/events", {"type": "outcome", "payload": {"status": "success", "summary": "done"}}),
    ],
)
def test_rest_mapping(tool, args, method, path, body):
    rec = Recorder()
    run(call(rec, tool, args))
    req = rec.requests[0]
    assert (req.method, req.url.path) == (method, path)
    if body is not None:
        assert json.loads(req.content) == body


def test_describe_returns_plain_text():
    out = run(call(Recorder(), "describe_scene"))
    assert out[0].text == "A Panda on a table."


def test_render_returns_image_block_and_caption():
    rec = Recorder()
    out = run(call(rec, "render", {"camera": "top"}))
    assert [b.type for b in out] == ["image", "text"]
    assert out[0].mimeType == "image/jpeg"
    assert base64.b64decode(out[0].data) == JPEG
    assert out[1].text == "camera=top sim_time=1.23"
    assert rec.requests[0].url.params["camera"] == "top" and rec.requests[0].url.params["format"] == "jpeg"


def test_render_without_header_asks_health_for_sim_time():
    rec = Recorder(render_headers={})
    out = run(call(rec, "render"))
    assert out[1].text == "camera=front sim_time=9.87"
    assert [r.url.path for r in rec.requests] == ["/render", "/health"]


def test_http_400_becomes_tool_error():
    rec = Recorder(fail={"/move_ee": "pos out of workspace"})
    with pytest.raises(SimError, match="pos out of workspace"):
        run(call(rec, "move_ee", {"pos": [5, 5, 5]}))


def test_unreachable_server_is_a_clear_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    async def go():
        sim = SimClient("http://127.0.0.1:1", transport=httpx.MockTransport(boom))
        try:
            await dispatch(sim, "get_state", {})
        finally:
            await sim.aclose()

    with pytest.raises(SimError, match="not reachable"):
        run(go())


def test_mcp_call_tool_handler_reports_errors_as_is_error():
    import mcp.types as types

    rec = Recorder(fail={"/state": "boom"})
    sim = SimClient("http://sim.test", transport=httpx.MockTransport(rec))
    server = mcp_server.build_server(sim)
    handler = server.request_handlers[types.CallToolRequest]

    async def go():
        req = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name="get_state", arguments={}))
        res = await handler(req)
        await sim.aclose()
        return res.root

    res = run(go())
    assert res.isError is True and "boom" in res.content[0].text


# ---- stdio round trip (real subprocess, fake sim server, no Claude) ----------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def fake_sim(tmp_path):
    import uvicorn

    from sabotage_demo.fake_sim_server import create_app

    scenario = json.loads((FIXTURES / "fake_baseline.json").read_text())
    app = create_app(tmp_path, scenario)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(url + "/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield url, tmp_path
    server.should_exit = True
    th.join(timeout=5)


def test_stdio_roundtrip_against_fake_sim(fake_sim):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    url, run_dir = fake_sim
    httpx.post(url + "/sabotage", json={"kind": "cut_object_sensor"})

    async def go():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "sabotage_demo.mcp_server", "--url", url],
            cwd=str(DEMO_DIR),
            env={"PYTHONPATH": str(DEMO_DIR), "PYTHONIOENCODING": "utf-8"},
        )
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                state = await session.call_tool("get_state", {})
                img = await session.call_tool("render", {"camera": "front"})
                loc = await session.call_tool("locate_object", {})
                bad = await session.call_tool("move_ee", {"pos": [1, 2]})
                return init, tools, state, img, loc, bad

    init, tools, state, img, loc, bad = asyncio.run(go())
    assert init.serverInfo.name == "mujoco"
    assert {t.name for t in tools.tools} == CONTRACT_TOOLS
    data = json.loads(state.content[0].text)
    assert "active_faults" not in data
    assert data["objects"]["cube"] is None and data["sensors"]["object_tracker"] == "dead"
    assert img.content[0].type == "image" and base64.b64decode(img.content[0].data)[:2] == b"\xff\xd8"
    assert img.content[1].text.startswith("camera=front sim_time=")
    assert json.loads(loc.content[0].text)["found"] is True
    assert bad.isError is True
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["type"] == "tool_call" and e["payload"]["tool"] == "render" for e in events)
