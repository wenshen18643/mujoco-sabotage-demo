"""Stdio MCP server ``mujoco``: a thin adapter from MCP tools to the sim REST API.

Run (cwd = demo/):

    python -m sabotage_demo.mcp_server --url http://127.0.0.1:8765

Every tool maps onto one REST route of ``sim_server.py`` (see CONTRACT.md,
"MCP tools"). The server never writes to stdout except MCP JSON-RPC frames
(LF-terminated, UTF-8, no BOM); all logging goes to stderr.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from io import TextIOWrapper
from typing import Any

import httpx

import mcp.types as types

log = logging.getLogger("mujoco-mcp")

DEFAULT_URL = "http://127.0.0.1:8765"
HTTP_TIMEOUT_S = 120.0
CAMERAS = ["front", "top", "wrist"]

SERVER_INSTRUCTIONS = (
    "Tools for operating a simulated Franka Panda arm on a table. World frame: "
    "origin at the robot base, +x forward (away from the robot), +y to the robot's "
    "left, +z up, table top at z = 0. Units: metres, radians, seconds. "
    "Quaternions are [w, x, y, z]. Arm joints are indexed 0..6."
)

# --------------------------------------------------------------------------
# Tool definitions (names and schemas frozen by CONTRACT.md)
# --------------------------------------------------------------------------

_VEC3 = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 3,
    "maxItems": 3,
}

TOOLS: list[types.Tool] = [
    types.Tool(
        name="describe_scene",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Plain-English description of the workcell: the robot, the objects, the "
            "cameras, the coordinate frame and the conventions used by every other "
            "tool. Call once at the start of a task."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="get_state",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Current state of the robot and scene (metres, radians, seconds; world "
            "frame with +z up and the table top at z = 0). Fields: sim_time; joints "
            "(pos, vel, target for the 7 arm joints, indices 0..6); tcp (the tool "
            "centre point between the fingertips: pos [x,y,z], quat [w,x,y,z]); "
            "gripper (opening_m = distance between the finger pads, target "
            "'open'|'close', holding = true only when BOTH fingers are touching the "
            "cube); objects.cube (pos/quat of the cube centre, or null when the "
            "object tracker is not reporting; use locate_object then); "
            "objects.target_zone (pos, half_size); sensors.object_tracker "
            "('ok'|'dead'); contacts (cube_fingers 0..2, cube_table). "
            "Observe with this before acting."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="move_ee",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        description=(
            "Move the tool centre point (TCP, between the fingertips) in a straight "
            "line to `pos` [x, y, z] in metres, world frame, solving inverse "
            "kinematics along the way. Omit `quat` to use the default top-down grasp "
            "orientation (fingers pointing straight down), which is right for "
            "picking and placing. Returns: reached (true when the final TCP position "
            "error is below 0.015 m), final_error_m, tcp_pos, tcp_quat, sim_time, "
            "joint_tracking_error (per joint, commanded minus actual, rad) and a "
            "note. ALWAYS check `reached` and final_error_m after a move. "
            "`excluded_joints` lists arm joint indices (0..6) that the planner must "
            "hold at their current angle and plan around using the remaining "
            "joints; use it when diagnose reports a joint as suspected_frozen."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pos": {**_VEC3, "description": "Target TCP position [x, y, z] in metres (world frame)."},
                "quat": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                    "description": "Optional target orientation [w, x, y, z]. Omit for the default top-down orientation.",
                },
                "duration_s": {
                    "type": "number",
                    "minimum": 0.2,
                    "maximum": 20.0,
                    "default": 2.0,
                    "description": "Duration of the motion in seconds of sim time (default 2.0).",
                },
                "excluded_joints": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 6},
                    "uniqueItems": True,
                    "default": [],
                    "description": "Arm joint indices (0..6) to hold fixed and plan around, e.g. [3] if joint index 3 is frozen.",
                },
            },
            "required": ["pos"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="move_joints",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        description=(
            "Move the 7 arm joints to the absolute angles `q` (radians, joint "
            "indices 0..6) with a smooth joint-space trajectory. Returns the same "
            "fields as move_ee (reached, final_error_m, tcp_pos, ...). Mostly useful "
            "to return to a known posture; prefer move_ee for task motions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "q": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 7,
                    "maxItems": 7,
                    "description": "Target joint angles in radians, 7 values for joints 0..6.",
                },
                "duration_s": {
                    "type": "number",
                    "minimum": 0.2,
                    "maximum": 20.0,
                    "default": 2.0,
                    "description": "Duration of the motion in seconds of sim time (default 2.0).",
                },
            },
            "required": ["q"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="set_gripper",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        description=(
            "Open or close the parallel gripper. Returns opening_m (distance "
            "between the finger pads; about 0.08 fully open, about 0.04 when "
            "closed on the 4 cm cube, near 0 when closed on nothing) and holding "
            "(true only when both fingers are in contact with the cube). After "
            "closing, holding = false means the grasp failed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["open", "close"], "description": "'open' or 'close'."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="wait",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        description=(
            "Advance the simulation by `seconds` of sim time without commanding "
            "anything new (for example to let the cube settle). Returns sim_time."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "minimum": 0.0, "maximum": 10.0, "description": "Sim seconds to advance."},
            },
            "required": ["seconds"],
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="render",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Take a picture from one of the fixed cameras and return it as an "
            "image (960x720 JPEG). Cameras: 'front' (default; looks at the "
            "workspace from +x, robot, cube and green target zone visible), 'top' "
            "(straight down over the workspace), 'wrist' (on the hand, looking "
            "along the fingers). The red object is the cube; the flat green square "
            "is the target zone. Use it to visually confirm the scene, "
            "especially before declaring success."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "camera": {"type": "string", "enum": CAMERAS, "default": "front", "description": "Camera name."},
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="locate_object",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Vision fallback: find an object by colour segmentation in a camera "
            "image and back-project it to world coordinates. Works even when the "
            "object tracker is dead and get_state reports objects.cube = null. "
            "Defaults: color 'red' (the cube), camera 'top' (best for this). "
            "Returns found, pos [x, y, z] (estimated cube centre, metres), pixel "
            "[u, v], confidence 0..1 and method."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "color": {"type": "string", "default": "red", "description": "Colour to look for, e.g. 'red'."},
                "camera": {"type": "string", "enum": CAMERAS, "default": "top", "description": "Camera to use ('top' recommended)."},
            },
            "additionalProperties": False,
        },
    ),
    types.Tool(
        name="diagnose",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Health check of the arm and sensors. Returns, per joint: commanded vs "
            "actual angle, error, velocity and suspected_frozen (true when the joint "
            "did not follow its command during the last move); object_tracker "
            "status ('ok'|'dead'); last_move (reached, final_error_m, worst_joint); "
            "contacts; and a plain-English summary of anomalies. Call it whenever "
            "a move does not reach or something looks inconsistent."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="check_task",
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
        description=(
            "Check whether the task is complete. success requires the cube centre "
            "within 0.05 m (xy) of the target zone centre, the cube resting on the "
            "table, and the gripper open and not touching the cube. Returns success, "
            "cube_pos, zone_pos, xy_distance_m, cube_on_table, gripper_open and a "
            "reason. Call before claiming success."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    types.Tool(
        name="report_outcome",
        annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
        description=(
            "Report the final outcome of the task to the operator log. Call exactly "
            "once, at the very end. status is 'success' or 'failed'; summary is a "
            "short account of what happened, including any problems you found and "
            "how you handled them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["success", "failed"]},
                "summary": {"type": "string", "description": "Two to five sentences."},
            },
            "required": ["status", "summary"],
            "additionalProperties": False,
        },
    ),
]

TOOL_NAMES = [t.name for t in TOOLS]

# --------------------------------------------------------------------------
# REST client and tool dispatch (no MCP plumbing, unit-testable)
# --------------------------------------------------------------------------


class SimError(Exception):
    """A REST call failed; the message is shown to the agent as a tool error."""


def strip_key(obj: Any, key: str = "active_faults") -> Any:
    """Return a copy of ``obj`` with ``key`` removed from every nested dict."""
    if isinstance(obj, dict):
        return {k: strip_key(v, key) for k, v in obj.items() if k != key}
    if isinstance(obj, list):
        return [strip_key(v, key) for v in obj]
    return obj


def round_floats(obj: Any, ndigits: int = 4) -> Any:
    """Round floats for compact, readable tool output (0.1 mm resolution)."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, dict):
        return {k: round_floats(v, ndigits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [round_floats(v, ndigits) for v in obj]
    return obj


def to_text(obj: Any) -> types.TextContent:
    clean = round_floats(strip_key(obj))
    return types.TextContent(type="text", text=json.dumps(clean, separators=(", ", ": ")))


class SimClient:
    """Async REST client for sim_server.py."""

    def __init__(self, base_url: str = DEFAULT_URL, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=HTTP_TIMEOUT_S, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.TimeoutException as e:
            raise SimError(f"simulator did not answer {method} {path} within {HTTP_TIMEOUT_S:.0f} s") from e
        except httpx.HTTPError as e:
            raise SimError(f"simulator not reachable at {self.base_url} ({type(e).__name__}: {e})") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("error") or resp.text
            except Exception:
                detail = resp.text
            raise SimError(f"simulator rejected {method} {path} (HTTP {resp.status_code}): {detail}")
        return resp

    async def get_json(self, path: str, **params: Any) -> Any:
        return (await self._request("GET", path, params=params or None)).json()

    async def post_json(self, path: str, body: dict | None = None) -> Any:
        return (await self._request("POST", path, json=body if body is not None else {})).json()

    async def get_bytes(self, path: str, **params: Any) -> httpx.Response:
        return await self._request("GET", path, params=params or None)


async def dispatch(sim: SimClient, name: str, args: dict[str, Any] | None) -> list[types.TextContent | types.ImageContent]:
    """Execute one tool against the REST API and return MCP content blocks."""
    args = dict(args or {})
    log.info("tool %s %s", name, json.dumps(args))

    if name == "describe_scene":
        data = await sim.get_json("/describe")
        text = data.get("text") if isinstance(data, dict) else None
        return [types.TextContent(type="text", text=text if isinstance(text, str) else json.dumps(data))]

    if name == "get_state":
        return [to_text(await sim.get_json("/state"))]

    if name == "move_ee":
        body = {
            "pos": [float(v) for v in args["pos"]],
            "quat": [float(v) for v in args["quat"]] if args.get("quat") is not None else None,
            "duration_s": float(args.get("duration_s", 2.0)),
            "excluded_joints": [int(j) for j in args.get("excluded_joints") or []],
        }
        return [to_text(await sim.post_json("/move_ee", body))]

    if name == "move_joints":
        body = {"q": [float(v) for v in args["q"]], "duration_s": float(args.get("duration_s", 2.0))}
        return [to_text(await sim.post_json("/move_joints", body))]

    if name == "set_gripper":
        return [to_text(await sim.post_json("/gripper", {"action": args["action"], "duration_s": 0.5}))]

    if name == "wait":
        return [to_text(await sim.post_json("/step", {"seconds": float(args["seconds"])}))]

    if name == "render":
        camera = args.get("camera") or "front"
        resp = await sim.get_bytes("/render", camera=camera, format="jpeg")
        mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        sim_time = resp.headers.get("x-sim-time")
        if sim_time is None:
            try:
                sim_time = (await sim.get_json("/health")).get("sim_time")
            except SimError:
                sim_time = None
        try:
            sim_time_s = f"{float(sim_time):.2f}"
        except (TypeError, ValueError):
            sim_time_s = "?"
        return [
            types.ImageContent(type="image", data=base64.b64encode(resp.content).decode("ascii"), mimeType=mime),
            types.TextContent(type="text", text=f"camera={camera} sim_time={sim_time_s}"),
        ]

    if name == "locate_object":
        body = {"color": args.get("color") or "red", "camera": args.get("camera") or "top"}
        return [to_text(await sim.post_json("/locate_object", body))]

    if name == "diagnose":
        return [to_text(await sim.get_json("/diagnose"))]

    if name == "check_task":
        return [to_text(await sim.get_json("/check_task"))]

    if name == "report_outcome":
        status = args["status"]
        summary = str(args.get("summary", ""))
        await sim.post_json("/events", {"type": "outcome", "payload": {"status": status, "summary": summary}})
        return [types.TextContent(type="text", text=f"Outcome recorded: {status}.")]

    raise SimError(f"unknown tool {name!r}; available: {', '.join(TOOL_NAMES)}")


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------


def build_server(sim: SimClient):
    from mcp.server.lowlevel import Server

    import anyio

    server: Server = Server("mujoco", instructions=SERVER_INSTRUCTIONS)
    # One robot, one command at a time: requests are handled concurrently by the
    # MCP session, so serialize them (FIFO, i.e. in the order the client sent them).
    robot_lock = anyio.Lock()

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent | types.ImageContent]:
        try:
            async with robot_lock:
                return await dispatch(sim, name, arguments)
        except SimError as e:
            log.warning("tool %s failed: %s", name, e)
            raise

    return server


async def serve(url: str) -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    # Take the real stdout for MCP frames, then point sys.stdout at stderr so a
    # stray print() can never corrupt the JSON-RPC stream. newline="\n" stops
    # Windows from translating frame terminators to CRLF; "utf-8" has no BOM.
    out_buffer = sys.stdout.buffer
    sys.stdout = sys.stderr
    stdin = anyio.wrap_file(TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace"))
    stdout = anyio.wrap_file(TextIOWrapper(out_buffer, encoding="utf-8", newline="\n"))

    sim = SimClient(url)
    server = build_server(sim)
    log.info("mujoco MCP server starting, sim at %s", url)
    try:
        async with stdio_server(stdin=stdin, stdout=stdout) as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await sim.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Stdio MCP server 'mujoco' backed by the sim REST API.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"sim_server base URL (default {DEFAULT_URL})")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s mujoco-mcp %(levelname)s %(message)s",
    )
    import anyio

    anyio.run(serve, args.url)


if __name__ == "__main__":
    main()
