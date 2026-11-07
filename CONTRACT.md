# Sabotage Demo: interface contract (v1, frozen)

> Historical document. It was written while this project lived in a `demo/`
> folder inside a clone of agent-next/mujoco-agent; `demo/` here means the root
> of this repository, and `<workspace>` the parent folder on the build machine.

Three builders work in parallel against this document. Do not change a name,
field, path, or unit here without a note in `demo/CONTRACT_CHANGES.md`.

## Purpose

Claude Code, acting as a robot operator, drives a Franka Panda in MuJoCo to put
a red cube in a green target zone. While it works, faults are injected
("sabotage"): a joint freezes, the object sensor dies and the cube is moved, or
the cube is teleported mid-reach. The agent must notice, diagnose and recover.
Output: a reproducible MP4 with the sim view on the left and the agent's live
reasoning on the right, plus a browser dashboard for live demos.

## Environment

- Repo: `<workspace>\mujoco-agent`. All demo code lives under `demo/`.
  Never modify tracked files outside `demo/`. Never `git commit`.
- Python: `<workspace>\mujoco-agent\.venv\Scripts\python.exe` (3.12,
  mujoco 4.0.0, mcp 1.30.0, fastapi, uvicorn, websockets, pillow, imageio,
  imageio-ffmpeg, numpy, scipy, anthropic). The venv has no pip: add packages
  with `uv pip install --python <that python> <pkg>`.
- Menagerie: `<workspace>\mujoco_menagerie` (env
  `MUJOCO_MENAGERIE_PATH`). Panda: `franka_emika_panda/panda.xml` (7 arm joints
  `joint1..joint7`, actuators `actuator1..actuator7` position-controlled,
  `actuator8` drives the gripper tendon, ctrlrange 0..255, keyframe `home`).
- Headless rendering works with the default GL backend on this Windows box
  (`mujoco.Renderer`). No `MUJOCO_GL` needed. ffmpeg is on PATH.
- Claude Code CLI `claude` 2.1.318 is on PATH and logged in (subscription).
  No `ANTHROPIC_API_KEY` exists and none should be required.

## Layout

```
demo/
  CONTRACT.md                  this file
  README.md                    how to run everything (builder B writes, others append)
  sabotage_demo/               python package (run with the venv python, cwd = demo/)
    __init__.py
    scene.py                   builds the MJCF (builder A)
    ik.py                      IK + trajectory (builder A)
    sim_core.py                SimCore class (builder A)
    sabotage.py                fault injection + triggers (builder A)
    sim_server.py              FastAPI REST/WS server around SimCore (builder A)
    mcp_server.py              thin stdio MCP server, calls the REST API (builder B)
    director.py                runs a scenario end to end with Claude Code (builder B)
    prompts.py                 operator system prompt + task prompt (builder B)
    video.py                   composes MP4 from a run dir (builder C)
    dashboard/index.html       live browser UI, served by sim_server at / (builder C)
  scenarios/*.json             scenario definitions (builder A)
  tests/                       pytest; name tests test_<area>_*.py
  runs/<run_id>/               one directory per run (gitignored via .git/info/exclude)
```

## Scene (builder A)

- Panda base at world origin on a table surface at z = 0. Table is a large box
  geom whose top face is z = 0, colour light grey. Floor below, neutral.
- Cube: red box, half-size 0.02 m (4 cm cube), mass 0.05 kg, free joint, body
  name `cube`, starts at `[0.50, 0.00, 0.02]`. Friction high enough to be
  grasped and lifted by the Panda fingers (verify in tests).
- Target zone: flat green box geom, body/geom name `target_zone`, size
  0.06 x 0.06 x 0.001 m, centred at `[0.50, -0.25, 0.001]`, no collision
  (contype 0 conaffinity 0).
- Grasp site: site `tcp` on the `hand` body at the tool centre point between
  the fingertips (Panda TCP offset is 0.1034 m along hand z). All end effector
  positions in this contract are the `tcp` site in world frame.
- Cameras (fixed, defined in MJCF): `front` (looks at the workspace from +x,
  elevated, robot and both cube and zone visible), `top` (straight down over the
  workspace centre `[0.5, -0.1, 1.2]`, used for vision fallback), `wrist`
  (attached to `hand`, looking along the fingers). Offscreen buffer 960x720.
- Default end effector orientation: fingers pointing straight down (hand z axis
  = world -z), yaw aligned with world x. Expose it as the quaternion constant
  `TOP_DOWN_QUAT` (w, x, y, z order, MuJoCo convention).
- Joint indexing: arm joints are 0..6 everywhere in the API (joint1 = 0).

## SimCore (builder A, `sim_core.py`)

Pure Python class, no network. Every stepping method records a frame from the
`front` camera every 0.05 s of sim time (see Frames). Every public method
appends an event (see Events).

```
SimCore(run_dir: Path, scenario: dict)
.get_state() -> dict                         see /state
.move_ee(pos, quat=None, duration_s=2.0, excluded_joints=()) -> dict
.move_joints(q7, duration_s=2.0) -> dict
.set_gripper(action: "open"|"close", duration_s=0.5) -> dict
.step(seconds: float) -> dict
.render(camera="front") -> np.ndarray (H,W,3 uint8)
.locate_object(color="red", camera="top") -> dict
.diagnose() -> dict
.check_task() -> dict
.reset_scene() -> dict
.apply_sabotage(spec: dict) -> dict          see Sabotage
.clear_sabotage() -> dict
.log_event(type, payload) -> None
```

`move_ee` semantics: straight-line Cartesian interpolation from the current
`tcp` pose to the target over `duration_s`, IK (damped least squares on the 7
arm joints, Jacobian of site `tcp`) solved at each waypoint, joints in
`excluded_joints` held at their current values (Jacobian columns zeroed).
Position actuators are driven with the IK solution. Returns
`reached` = final `tcp` position error < 0.015 m.

`set_gripper("close")` drives actuator8 to closed; `holding` is true when both
fingers have contact with `cube`. `opening_m` is the distance between the
finger pads.

## REST API (builder A serves, builder B and C consume)

`sim_server.py --scenario <path.json> --run-dir <dir> --port 8765` serves on
`http://127.0.0.1:8765`. All bodies are JSON. Errors return HTTP 400 with
`{"error": "..."}`. Lengths are metres, angles radians, time seconds.

| method and path | body | returns |
|---|---|---|
| GET `/health` | | `{"ok": true, "scenario": name, "sim_time": t}` |
| GET `/describe` | | `{"text": <plain-English scene/API description for the agent>}` |
| GET `/state` | | State object below |
| POST `/move_ee` | `{"pos":[x,y,z], "quat":[w,x,y,z] or null, "duration_s":2.0, "excluded_joints":[]}` | `{"reached": bool, "final_error_m": f, "tcp_pos":[..], "tcp_quat":[..], "sim_time": t, "joint_tracking_error":[7], "note": str}` |
| POST `/move_joints` | `{"q":[7], "duration_s":2.0}` | same shape as move_ee |
| POST `/gripper` | `{"action":"open"|"close", "duration_s":0.5}` | `{"opening_m": f, "holding": bool, "sim_time": t}` |
| POST `/step` | `{"seconds": f}` | `{"sim_time": t}` |
| GET `/render?camera=front&format=jpeg` | | image bytes, `Content-Type: image/jpeg`. Also `format=png`. |
| POST `/locate_object` | `{"color":"red", "camera":"top"}` | `{"found": bool, "pos":[x,y,z] or null, "pixel":[u,v] or null, "confidence": 0..1, "method": "color_segmentation"}` |
| GET `/diagnose` | | Diagnose object below |
| GET `/check_task` | | `{"success": bool, "cube_pos":[..], "zone_pos":[..], "xy_distance_m": f, "cube_on_table": bool, "gripper_open": bool, "reason": str}` |
| POST `/reset` | | `{"ok": true}` |
| POST `/sabotage` | Sabotage spec below | `{"ok": true, "active": [..]}` |
| POST `/sabotage/clear` | | `{"ok": true}` |
| POST `/events` | `{"type": str, "payload": {}}` | `{"ok": true}` (director posts agent events here) |
| GET `/events?since=<n>` | | `{"events": [..], "next": n}` |
| GET `/frames/latest.jpg` | | latest recorded frame as JPEG |
| GET `/` | | `demo/sabotage_demo/dashboard/index.html` |
| WS `/ws` | | server pushes `{"kind":"event", ...event}` and `{"kind":"frame","index":n,"sim_time":t,"jpeg_b64":"..."}` (frames throttled to 10/s wall clock) |

`success` in `/check_task`: cube centre within 0.05 m (xy) of the zone centre,
cube resting on the table (z < 0.03, speed < 0.02 m/s), gripper open and not
in contact with the cube.

### State object

```
{
  "sim_time": 12.34,
  "joints": {"pos":[7], "vel":[7], "target":[7], "names":["joint1",..]},
  "tcp": {"pos":[3], "quat":[4]},
  "gripper": {"opening_m": 0.08, "target": "open"|"close", "holding": false},
  "objects": {
    "cube": {"pos":[3], "quat":[4]} | null,          null when object sensor is dead
    "target_zone": {"pos":[3], "half_size":[0.06,0.06,0.001]}
  },
  "sensors": {"object_tracker": "ok"|"dead"},
  "contacts": {"cube_fingers": 0..2, "cube_table": bool},
  "active_faults": ["freeze_joint:3"]      names only; the agent is never told what they do
}
```

Note: `active_faults` is for the dashboard and video. `mcp_server.py` MUST
strip `active_faults` before returning state to the agent.

### Diagnose object

```
{
  "sim_time": t,
  "joints": [ {"index": i, "name": "joint4", "commanded": q, "actual": q, "error": e,
               "velocity": v, "suspected_frozen": bool}, ... 7 ],
  "object_tracker": "ok"|"dead",
  "gravity": [0,0,-9.81],
  "last_move": {"reached": bool, "final_error_m": f, "worst_joint": i} | null,
  "contacts": {...same as state.contacts},
  "summary": "plain-English list of anomalies, or 'no anomalies detected'"
}
```

`suspected_frozen` is true when |commanded - actual| > 0.05 rad while the
joint velocity stayed < 0.01 rad/s during the last move.

## Sabotage (builder A)

Spec objects accepted by `/sabotage` and by scenario triggers:

- `{"kind": "freeze_joint", "joint": 3}`: the arm joint stops responding and
  holds its current angle (implementation: actuator output for that joint is
  disabled and the joint is clamped each step). Must make a 7-joint IK move
  miss by > 0.02 m while the same move with the joint in `excluded_joints`
  reaches. Verified by a test.
- `{"kind": "cut_object_sensor"}`: `state.objects.cube` becomes null,
  `sensors.object_tracker` = "dead". Physics unaffected. `/locate_object` still works.
- `{"kind": "teleport_cube", "pos": [x,y,z]}` or `{"kind":"teleport_cube","delta":[dx,dy,dz]}`:
  cube moved instantly, velocities zeroed. If held, it is torn out of the gripper.
- `{"kind": "clear"}`: remove all faults.

Scenario file:

```
{
  "name": "frozen_joint",
  "title": "Chapter 2: a joint dies mid-task",
  "task": "Put the red cube in the green target zone.",
  "triggers": [
    {"when": {"type": "tool_call", "tool": "move_ee", "n": 2, "phase": "before"},
     "sabotage": {"kind": "freeze_joint", "joint": 3}}
  ]
}
```

Trigger `when` types: `tool_call` (tool name, n-th call, phase before/after),
`ee_near_cube` (`dist` m, fires once, only while gripper open),
`cube_lifted` (`z` m, fires once), `sim_time` (`t`), `manual` (dashboard only).
Builder A ships: `baseline.json`, `frozen_joint.json`, `blind_grasp.json`
(cut_object_sensor plus teleport_cube delta [0, 0.12, 0] after the 1st move_ee),
`moved_cube.json` (teleport_cube delta [0.0, 0.15, 0] on ee_near_cube 0.06).
Builder A tunes joints/deltas so a scripted recovery succeeds, and documents why.

## MCP tools (builder B, `mcp_server.py`)

Stdio MCP server named `mujoco`. Each tool calls the REST API and returns the
JSON as text; `render` returns an MCP image block (JPEG) plus a short text
caption. Tool names and schemas:

| tool | input | note |
|---|---|---|
| `describe_scene` | `{}` | GET /describe |
| `get_state` | `{}` | GET /state, `active_faults` removed |
| `move_ee` | `{pos, quat?, duration_s?, excluded_joints?}` | POST /move_ee |
| `move_joints` | `{q, duration_s?}` | |
| `set_gripper` | `{action}` | |
| `wait` | `{seconds}` | POST /step |
| `render` | `{camera?}` | image block + caption "camera=<name> sim_time=<t>" |
| `locate_object` | `{color?, camera?}` | |
| `diagnose` | `{}` | |
| `check_task` | `{}` | |
| `report_outcome` | `{status: "success"|"failed", summary}` | POST /events type `outcome` |

Claude Code sees them as `mcp__mujoco__<tool>`.

## Events (builder A owns `runs/<id>/events.jsonl`; builder B posts agent events)

One JSON object per line, appended in order:

```
{"seq": n, "wall_time": unix_float, "sim_time": t, "type": <type>, "payload": {...}}
```

Types and payloads:

- `run_start` `{"scenario": name, "title": str, "task": str}`
- `tool_call` `{"tool": name, "args": {...}, "frame_index": n}` (frame index at call start; written by sim_server for every REST call that corresponds to a tool)
- `tool_result` `{"tool": name, "result": {...} or {"image": true}, "frame_index": n, "duration_s": f}`
- `sabotage` `{"spec": {...}, "label": "Joint 4 frozen"}`
- `agent_text` `{"text": str}` (assistant visible text, posted by director)
- `agent_thinking` `{"text": str}` (if the stream exposes it; optional)
- `outcome` `{"status": str, "summary": str}` (agent's own claim)
- `judge` `{"success": bool, "detail": {...check_task...}}` (independent check by director)
- `run_end` `{"success": bool, "tool_calls": n, "sim_time": t, "wall_s": f}`

## Frames (builder A writes, builder C reads)

`runs/<id>/frames/%06d.jpg` from camera `front`, 960x720, JPEG quality 85,
recorded every 0.05 s of sim time during any stepping call, plus one frame at
run start and one after every sabotage. `runs/<id>/frames.jsonl` has one line
per frame: `{"index": n, "sim_time": t, "wall_time": w}`.

## Run directory

```
runs/<id>/
  scenario.json        copy of the scenario
  events.jsonl
  frames/ frames.jsonl
  agent_stream.jsonl   raw Claude Code stream (builder B)
  summary.json         {"scenario", "success", "agent_claimed", "tool_calls", "sim_time", "wall_s", "model"}
  video.mp4            builder C output
```

`<id>` = `YYYYMMDD_HHMMSS_<scenario>`.

## Video (builder C, `video.py`)

`python -m sabotage_demo.video <run_dir> [--out video.mp4]` and
`python -m sabotage_demo.video --compile <run_dir>... --out final.mp4 --title "..."`.
1280x720, 30 fps, H.264 yuv420p via imageio-ffmpeg. Layout: sim frame left
(scaled to fit 853x640 at y=60), agent trace panel right (dark, monospace,
newest at bottom, tool calls highlighted, agent text in a different colour),
title bar top (scenario title, sim time, tool call count), red flashing banner
across the sim frame for 2 s when a `sabotage` event fires, green stamp when
`judge.success` is true, chapter title card (2 s) between runs in compile mode.
Timeline rule: frames between a tool_call's `frame_index` and its tool_result's
`frame_index` play at real time (20 recorded fps shown at 30 fps by frame
duplication). Events with no new frames hold the last frame for
`min(3.0, 0.8 + 0.03 * len(text))` seconds while the trace updates.
