# Run log

Every run made during integration, in order, with numbers copied from each
run's `summary.json`. Nothing in any run directory (events, frames, stream) was
edited by hand, with one exception made for publication: the first line of each
`sim_server.log` had the local absolute path in `run_dir=` shortened to
`runs\<run id>`. Run directories are under `runs/<run id>/`. This repo keeps
`events.jsonl`, `frames.jsonl`, `summary.json`, `scenario.json` and
`sim_server.log` for the four chapter runs; the frames and the raw agent stream
are in `runs_with_frames.zip` on the v1.0 release. The other runs listed below
are not included.

- **success**: the independent judge, `GET /check_task?source=director`, called by
  the director after the agent has finished.
- **claimed**: what the agent itself reported through `report_outcome`.
- **tool calls**: MCP tool calls made by the agent, `report_outcome` included.
- **agent wall**: seconds from launching Claude Code until it exits. **total**: the
  whole director run (sim start, judge, shutdown), not counting video rendering.
- **cost**: `total_cost_usd` from Claude Code's result message. This is Claude
  Code's API-price equivalent; the runs used the subscription login, so nothing
  was billed per token.

## Chapter attempts (Claude Code, `--model opus --max-turns 60`)

Each of the four chapters succeeded on its first attempt. No attempt failed, so
none were re-run, and no pipeline bug forced a re-run.

| # | scenario | attempt | run id | success | claimed | tool calls | turns | sim time | agent wall | total | cost |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | baseline | 1 of 1 | `20261107_101349_baseline` | true | success | 16 | 17 | 14.51 s | 46.2 s | 50.3 s | $0.160 |
| 2 | frozen_joint | 1 of 1 | `20261107_101509_frozen_joint` | true | success | 17 | 18 | 16.26 s | 64.0 s | 68.5 s | $0.155 |
| 3 | blind_grasp | 1 of 1 | `20261107_101645_blind_grasp` | true | success | 20 | 21 | 16.85 s | 70.0 s | 74.1 s | $0.197 |
| 4 | moved_cube | 1 of 1 | `20261107_101831_moved_cube` | true | success | 24 | 25 | 22.36 s | 71.6 s | 75.7 s | $0.169 |

Totals: 4 chapter attempts, 4 successes, 77 tool calls, $0.68 equivalent. Every
run had `tool_errors` = 0. Each run delivered at least one camera image to the
model (`images_delivered` 1, 1, 3, 1).

### What the trace shows in each fault chapter

The full narration for each chapter is in `trace_<name>.md`.

- **frozen_joint** (joint index 0 frozen before the 2nd `move_ee`):
  1. The descent still reached, 3.6 mm off. The agent noted that joint 0 was
     lagging by 0.007 rad and kept going.
  2. After the lift it ran `diagnose` on its own initiative. That returned "no
     anomalies detected", because the lag was below the 0.05 rad threshold.
  3. The transport then stopped 14.4 cm short of the zone (`reached: false`).
  4. A second `diagnose` reported joint 0 `suspected_frozen`.
  5. The agent retried with `excluded_joints: [0]`, which reached within 0.1 mm.
     It kept the joint excluded for the descent, release and retreat.
  6. Judge: 1.8 mm from the zone centre.
- **blind_grasp** (tracker cut and cube moved 14 cm, right after the 1st `move_ee`):
  1. `get_state` returned `cube: null`, and the agent said so.
  2. `locate_object` on the top camera found the cube at [0.582, 0.123] with
     confidence 1.0. The agent confirmed it with a top-camera `render`.
  3. It re-planned the pre-grasp to the new position. There `locate_object`
     found nothing, and the agent correctly guessed that its own hand was
     hiding the cube.
  4. It confirmed the cube with the `wrist` camera, then grasped it
     (`holding: true`).
  5. Judge: 1.2 mm from the zone centre.
- **moved_cube** (cube moved 15 cm in y while the gripper was 6 cm above it):
  1. The descent reached, but `set_gripper close` returned `holding: false` with
     an opening of 0.0004 m.
  2. The agent opened the gripper and re-observed the cube with `get_state` and
     then `locate_object`, which put it at [0.50, 0.15].
  3. It lifted, moved over the new position and checked it again before
     descending. The second grasp held.
  4. Judge: 3.4 mm from the zone centre.

## Integration smoke run (cheap model, before the chapters)

| scenario | run id | model | success | claimed | tool calls | agent wall | cost | notes |
|---|---|---|---|---|---|---|---|---|
| baseline | `20261107_015110_baseline` | sonnet, `--max-turns 40` | true | success | 13 | 52.7 s | $0.042 | First run of the real simulator end to end: director → Claude Code → MCP stdio → REST → SimCore → video. It passed on the first try with `images_delivered` = 1, so no integration bug showed up. Sonnet wrote a sentence before only 4 of its 13 calls; Opus narrates nearly every call. |

## Live-demo check (dashboard button, after the chapters)

I wanted to check the live-demo path before documenting it, so I ran one more
Opus baseline. Headless Edge had the dashboard open, and when the agent started
its 2nd `move_ee` (the descent), a real DOM click pressed **Teleport cube
(+15 cm y)**. This run is not a chapter attempt and is not in the film.

| scenario | run id | model | success | claimed | tool calls | agent wall | cost | notes |
|---|---|---|---|---|---|---|---|---|
| baseline + manual teleport | `20261107_111126_baseline` | opus | true | success | 24 | 70.6 s | $0.180 | Clicked at sim t = 2.64 s, during the descent. The simulator serializes calls, so the fault was applied when the descent finished (sim t = 4.77 s, `source: "api"`), 1.9 s after the click. The agent closed on nothing, re-observed (`get_state`, `locate_object`), re-grasped at [0.50, 0.15] and placed the cube 3.4 mm from the zone centre. |

## Runs that were already there (not part of this log's attempts)

`20261107_011224_fake_baseline` and `20261107_011549_fake_frozen_joint` were
made by builder B against `fake_sim_server.py` (no MuJoCo) before integration.
The second one stopped on `--max-turns 12`. Neither is used in any deliverable.

## Videos made from these runs

All with `--hold-scale 0.2`, which shortens text holds. Motion still plays in
real time, and any narrative line is held for at least 1.2 s.

| file | length |
|---|---|
| `chapter_1_baseline.mp4` | 46.2 s |
| `chapter_2_frozen_joint.mp4` | 50.1 s |
| `chapter_3_blind_grasp.mp4` | 56.7 s |
| `chapter_4_moved_cube.mp4` | 67.4 s |
| `sabotage_demo_final.mp4` | 3 min 55.9 s (title card with byline, 4 chapter cards, 4 chapters, results card) |
| `teaser.gif` | 25 s at 2.9x, from moved_cube |

Chapter 4 runs past the 60 s target because the agent made 24 calls and needed
two grasps.
