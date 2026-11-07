# Contract changes

> Historical document. It was written while this project lived in a `demo/`
> folder inside a clone of agent-next/mujoco-agent; `demo/` here means the root
> of this repository, and `<workspace>` the parent folder on the build machine.

Deviations from and additions to CONTRACT.md v1, newest last.

## Builder B (mcp_server, director, prompts)

All additive; no frozen name, field, path or unit changed.

- **`outcome` status `none`**: when the agent never calls `report_outcome`
  (max turns, crash), the director posts `outcome` with
  `{"status": "none", "summary": "Agent did not call report_outcome (...)"}`.
  Agent-reported statuses stay `success` / `failed`.
- **`GET /check_task?source=director`**: the director's judge call adds this
  query parameter so sim_server *can* skip logging it as an agent
  `tool_call`/`tool_result` (otherwise the video shows a check_task call after
  the agent has finished). Servers that ignore the parameter behave as the
  contract says.
- **`render` caption sim time**: `mcp_server` reads an optional `X-Sim-Time`
  response header from `GET /render`; if absent it calls `GET /health` for the
  caption's `sim_time`. Either works; the header saves a round trip.
- **`active_faults` stripping** is recursive over every tool result, not only
  `get_state`, so a leak from another route cannot reach the agent.
- **Tool annotations / serialization**: observation tools carry MCP
  `readOnlyHint: true`; `mcp_server` serializes all tool calls (FIFO lock) so
  parallel tool calls from the model can never reach the robot out of order.
- **Extra run-dir files**: `mcp_config.json`, `sim_server.log`,
  `claude_stderr.log`.
- **Extra summary.json keys** after the seven contract keys: `run_id`,
  `title`, `runner`, `judge_reason`, `agent_summary`, `tool_sequence`,
  `images_delivered`, `tool_errors`, `agent_result` (subtype, num_turns,
  cost), `agent_error`, `total_wall_s`.
- **Director flags beyond the brief**: `--fake-sim`, `--runner sdk|cli`,
  `--claude-cli`, `--health-timeout`, `--no-color`, `--quiet`. Exit codes:
  0 judge success, 3 task failed, 1 infrastructure error, 130 interrupted.
- **Test fixtures** live in `demo/tests/fixtures/` (fake-server scenarios with
  an optional `fake_initial.cube_pos` key understood only by
  `fake_sim_server.py`, plus a recorded Claude Code stream).

## Builder C (video, dashboard)

All additive or interpretations; no frozen name, field, path or unit changed.

- **Hold length**: `len(text)` in the contract's hold rule is the length of the
  one-line trace text shown for the event (e.g. `> move_ee pos=[0.5,0.0,0.14]`,
  `← reached err=0.004m`), so tool calls/results without frames hold too.
  An event "has new frames" when the next event's frame position is later.
  Events without `frame_index` are placed by `sim_time` (latest frame with
  `sim_time <=` the event's), so a sabotage fired mid-move overlays the moving
  frames instead of pausing them. `--hold-scale` (default 1.0 = contract)
  shortens holds when wanted.
- **Extra video timeline**: each run ends with a 1.5 s hold so the verdict
  stamp is readable; 8-frame fades at run/card boundaries; `--compile` adds a
  2.5 s opening title card when `--title` is given and a 4 s end card with
  per-chapter outcomes (both beyond the 2 s chapter cards in the contract).
- **Extra layout**: a 20 px progress strip under the sim frame (y 700..720)
  with sabotage/judge ticks; persistent faults (freeze_joint,
  cut_object_sensor) keep a red "FAULT" chip on the sim frame after the 2 s
  banner; the scene is dimmed slightly under the stamp.
- **Verdict fallback**: no `judge` event -> stamp from `summary.json.success`
  at the end of the run; neither -> no stamp ("NO VERDICT" on the end card).
  `outcome` with status `none` (builder B) is shown as `NO CLAIM: ...`.
- **Extra CLI flags** on `sabotage_demo.video`: `--gif [PATH]`, `--no-mp4`,
  `--hold-scale`, `--crf`, `--preset`. The GIF uses its own plan with halved
  holds and is sped up (with an `Nx` badge) if the stretch exceeds 25 s.
- **Dashboard "Clear faults"** POSTs `{"kind": "clear"}` to `/sabotage` and
  falls back to `POST /sabotage/clear` on HTTP 400/404/422.
- **Extra files**: `sabotage_demo/make_fixture_run.py` (synthetic run dirs,
  `runs/fixture_<name>/`), `tests/dashboard_stub_server.py` (stub server for
  the page), tests `tests/test_video_compose.py`, `tests/test_dashboard_page.py`.

## Builder A (scene, ik, sim_core, sabotage, sim_server, scenarios)

Changed values (the contract allowed tuning them; reasons in `scenarios/README.md`):

- **`frozen_joint.json` freezes joint index 0** (joint1, the base), not the
  example's 3. Joint 3 frozen before the descent misses by only 1.7 cm and the
  6-joint recovery cannot reach the zone. Joint 0 frozen there leaves the grasp
  and lift working, then the 7-joint transport to the zone misses by 13-15 cm,
  and the same move with `excluded_joints: [0]` reaches.
- **`blind_grasp.json` teleport delta is [0.08, 0.12, 0]**, not [0, 0.12, 0]:
  [0.50, 0.12] is hidden from the `top` camera by the hand at the pre-grasp
  pose, so `locate_object` could not find it.
- **`TOP_DOWN_QUAT = [0, 1, 0, 0]`**: hand x = world x, fingers close along
  world y (the `front` camera sees them side-on). The Menagerie `home`
  keyframe (the reset pose) has the hand yawed 90 degrees from this, so the
  first `move_ee` also turns the wrist.

Semantics the contract left open:

- Tool names in events and in `tool_call` triggers are the MCP tool names
  (`describe_scene, get_state, move_ee, move_joints, set_gripper, wait, render,
  locate_object, diagnose, check_task`). Event order per call: `tool_call`,
  "before" sabotage, the call, "after" sabotage, `tool_result` (the same order
  as `fake_sim_server.py`). A call that fails logs `tool_result` with
  `{"error": msg}`. `frame_index` = index of the latest recorded frame.
- **Observer reads are not tool calls.** `GET /state, /render, /diagnose,
  /check_task, /describe` with header `X-Observer: 1`, query `observer=1`,
  query `source=<anything but "agent">` (the director's
  `GET /check_task?source=director`) or a browser User-Agent are served
  without `tool_call`/`tool_result` events and do not count for triggers.
- **`check_task` hides the cube while the tracker is dead**: for agent calls
  `cube_pos` and `xy_distance_m` are `null` and `reason` has no numbers
  (otherwise the agent could read the position the fault hides). `success` is
  always computed from ground truth, and observers get the true values.
- A `clear` is logged as a `sabotage` event with `spec.kind == "clear"` and
  label "Faults cleared". Clearing a frozen joint resets its servo target to
  its current angle, so it does not snap to a stale command.
- `POST /reset`: arm to the `home` keyframe, gripper open, cube to its start,
  faults cleared, triggers re-armed, tool counts zeroed. `sim_time` is not
  reset (stays monotonic for frames/events); a `reset` event is logged.
- `set_gripper` ramps the command over 60% of `duration_s`, then waits (up to
  1 s more) for the fingers to stop, so an off-centre cube is pushed into the
  middle before `holding` is evaluated. `opening_m` = finger_joint1 +
  finger_joint2 (0.08 fully open, about 0.04 on the cube). actuator8: ctrl 255
  = open, 0 = closed.
- `move_ee` rejects (HTTP 400): TCP z < 0.01 (the fingertips reach 0.009 m
  below the TCP), |x| or |y| > 1 m, z > 1.3 m, a `quat` that points the
  fingers upward, `duration_s` outside 0.2..20, `excluded_joints` leaving fewer
  than 2 free joints. `quat: null` means `TOP_DOWN_QUAT`.
- `suspected_frozen` uses the largest |velocity| seen during the last
  `move_ee`/`move_joints`. A joint passed in `excluded_joints` is commanded to
  stay where it is, so it is not flagged during that move.

Additive fields and routes:

- `GET /scenario` returns the scenario JSON (for the dashboard).
- `sabotage` event payload adds `source` (`"trigger"` | `"api"`), `active`
  (fault names after the change) and `frame_index` (the frame recorded right
  after the fault).
- `locate_object` adds `pixels` (blob size) and, when `confidence < 0.5`, a
  `note` saying the object is probably partly hidden by the arm. Colours:
  `red` (cube, back-projected to z = 0.02) and `green` (zone, z = 0.002).
- `GET /render` sends `X-Sim-Time` and `X-Camera` headers; the `render`
  `tool_result` is `{"image": true, "camera": name}`.
  `GET /frames/latest.jpg` sends `X-Frame-Index` and `X-Sim-Time`.
- `sim_server.py` flags: `--realtime F` (default 1.0: motions run at wall-clock
  speed so the dashboard shows them live; 0 = as fast as possible),
  `--host`, `--no-frames`; `--run-dir` is optional (default
  `demo/runs/<YYYYMMDD_HHMMSS>_<scenario>`). Starting on an existing run dir
  truncates its events.jsonl/frames.jsonl and removes old frames.
- `SimCore(run_dir, scenario, *, realtime=0.0, record_frames=True)`, plus
  `describe()`, `events_since(n)`, `latest_frame()`, `add_listener(fn)`,
  `close()`, and an `observer=True` keyword on the read-only methods.
- Scenario triggers may give `"sabotage"` as a list of specs.
- `sim_server.py` resolves a relative `--scenario` against the cwd, then
  `demo/sabotage_demo/`, then `demo/` (first existing file), and a relative
  `--run-dir` to the first of those candidates that lies inside `demo/`. So
  the contract's `--scenario ../scenarios/baseline.json --run-dir ../runs/<id>`
  works from `demo/` and writes to `demo/runs/<id>`; absolute paths are used
  as given.

## Integration

The pipeline worked end to end on the first real run: director → Claude Code
(subscription, SDK runner) → `mcp_server` over stdio → REST → `SimCore` →
`video.py`. The Sonnet smoke run `20261107_015110_baseline` passed with judge
success, `agent_claimed` = success and `images_delivered` = 1. Paths,
encoding, timeouts, event shapes, the `source=director` observer marker and
`frame_index` alignment all needed no change, so the director, `mcp_server`,
`sim_server` and `sim_core` are untouched. Every change below is visual
polish, a tool or documentation. No frozen name, field, route or unit changed.

- **`scene.py`, front camera** (`FRONT_CAM`): the look-at point moved from
  (0.30, -0.10, 0.32) to (0.30, -0.14, 0.33) and `fovy` went from 43° to 39°.
  The cube and zone are about 11 % larger on screen (the cube's area went from
  774 to 976 px at 960x720). Renders from `SimCore` show the arm fully in
  frame in the `home` pose and at lift height 0.20 m over the cube, over the
  zone, over the blind_grasp position [0.58, 0.12] and over the moved_cube
  position [0.50, 0.15]. The camera position, the `top` and `wrist` cameras,
  and therefore `locate_object`, are unchanged.
- **`scene.py`, shadows**: `visual.quality.shadowsize` went from 4096 to 8192
  and `visual.map.shadowclip` from 1.5 to 1.2, giving sharper shadow edges.
  Rendering still costs about 5 ms per 960x720 frame.
- **`video.py`**:
  - FAULT chips are drawn at the top-right of the sim frame instead of the
    top-left, so they no longer cover status text baked into a frame. The
    fake simulator draws such text at y ≈ 83 px of the output.
  - The `outcome` with status `none` is now one concise line:
    `NO CLAIM agent stopped without report_outcome (turn limit reached)`.
    The "last words" dump is gone.
  - New `MIN_TEXT_HOLD_S = 1.2`: narrative entries (`agent_text`,
    `agent_thinking`, `outcome`, `sabotage`, `judge`) hold at least 1.2 s
    whatever `--hold-scale` says. At scale 1.0 the contract formula already
    gives at least 1.2 s for any text of 14 characters or more.
  - Tool results that signal a problem are drawn in the alert colour, as tool
    errors already were: `reached: false`, a gripper closed on nothing
    (`holding: false` with opening < 1 cm), a `diagnose` summary other than
    "no anomalies detected", and `success: false`. The text is unchanged.
  - New `--subtitle` flag for the opening card. The default subtitle names
    Claude Code, the Franka Panda and MuJoCo.
  - New repeatable `--byline` flag: extra lines under the subtitle; adds 1 s
    to the title card. The film uses "Wen Shen" and "Pre-assessment for
    Dr Chua, SHRDC".
  - The deliverables use `--hold-scale 0.2`, which gives chapters of 46 to 67 s
    and a final film of 3:56.
- **New `sabotage_demo/trace_md.py`**: `python -m sabotage_demo.trace_md
  <run_dir> --out trace.md` writes a run's narration, tool calls (in the same
  one-line wording as the video), sabotage events, the agent's claim and the
  judge as Markdown. It only reads files.
- **New `tests/test_video_integration.py`**: covers the NO CLAIM line, the
  alert colours, the minimum text hold and `trace_md`.
- **New `demo/output/`**: the deliverables (final film, chapters, GIF,
  dashboard screenshot, run log, traces).
- **New `demo/.integration/`**: scratch scripts I used to verify things. They
  are not part of the package:
  - `render_poses.py`, `cam_variant.py`, `sheet.py`: camera framing checks.
  - `dashboard_shot.py`: a headless-Edge screenshot of the live dashboard
    during a real run, reusing the CDP client in `tests/test_dashboard_page.py`.
  - `durations.py`, `breakdown.py`: planned video length per `--hold-scale`.
  - `moments.py`: extracts frames at the sabotage, diagnosis and judge moments.
  - `logs/`: console logs of every director run.

### Known weaknesses

The builder sections did not have a list like this; I wrote it from what I saw
during integration.

- **The operator prompt is a runbook.** `prompts.py` never mentions faults or
  testing. It does, however, tell the agent the generic recovery for each
  symptom: a missed move means call `diagnose`; a suspected frozen joint means
  use `excluded_joints`; a null cube means use `locate_object`;
  `holding: false` means re-observe and re-grasp. The agent applied the right
  procedure at the right moment and explained why. It did not invent a
  procedure.
- **`diagnose` does the fault detection.** `suspected_frozen` and the plain
  English summary come from the simulator's ground truth. The agent's job is
  to notice the symptom, ask, and act on the answer. In frozen_joint the early
  0.007 rad lag stayed below the 0.05 rad threshold, so `diagnose` first said
  "no anomalies detected".
- **`check_task` is a ground-truth oracle.** It hides the cube position while
  the tracker is dead, but its `success` flag is always computed from the true
  state, and the agent may call it. A real cell would not have this.
- **Faults are tuned to be recoverable** and fire at deterministic moments
  (the n-th `move_ee`, or the gripper 6 cm from the cube). See
  `scenarios/README.md` for how joint 0 and the [0.08, 0.12, 0] delta were
  chosen.
- **The world waits for the agent.** Sim time advances only inside tool calls,
  so nothing happens while the model thinks (1 to 5 s per step). A real robot
  and a real disturbance would not wait.
- **Easy perception.** `locate_object` segments a saturated red cube on a plain
  grey table, with no noise, clutter or lighting changes. IK, gravity
  compensation and contacts are idealised.
- **n = 1 per chapter.** Each chapter passed on its first Opus attempt; there
  are no repeated-seed statistics. LLM runs are not deterministic, so a re-run
  can take a different path, such as extra checks, or can fail.
- **Two joint numbering schemes.** Banners and chapter cards use 1-based joint
  names ("Joint 1 frozen" = `joint1`), while the API and the agent use 0-based
  indices ("joint 0"). Both are correct, but they can confuse a viewer.
- **The video is composed afterwards.** It is built from the logged events and
  recorded frames, not screen-captured. Motion plays in real time; text holds
  are compressed with `--hold-scale 0.2`. The live dashboard shows the same
  events as they happen.
- **Cost figures** are Claude Code's `total_cost_usd` estimate. The runs used
  the subscription login.
- `summary.tool_calls` counts `report_outcome`, but `report_outcome` has no
  `tool_call` event because it posts `outcome` directly. The trace panel's
  counter is therefore one lower until the end card, which uses `summary.json`.
- **Dashboard sabotage lands between robot commands.** `sim_server` runs every simulator call on one worker thread, so a button pressed during a motion is applied when that motion ends (1.9 s after the click in the live check `20261107_111126_baseline`). Scenario triggers such as `ee_near_cube` do fire mid-motion.
