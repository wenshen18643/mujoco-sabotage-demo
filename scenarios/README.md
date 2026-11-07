# Scenarios

Each file is a scenario for `sim_server.py` / `SimCore` (format in
`../CONTRACT.md`, "Sabotage"). Triggers fire once each. Tool names in
`tool_call` triggers are MCP tool names, and `n` counts calls of that tool
from 1.

The expected agent flow is the one `prompts.py` asks for: open, pre-grasp
0.12 m above the cube (`move_ee` #1), descend to z = 0.02 (#2), close, lift
to z = 0.20 (#3), carry to above the zone (#4), lower to z = 0.03 (#5), open,
retreat (#6). Every scenario below was tuned and verified against that flow by
a scripted operator (`tests/test_sim_faults.py`), including the recovery.

| file | fault | when |
|---|---|---|
| `baseline.json` | none | |
| `frozen_joint.json` | `freeze_joint` joint **0** (joint1, the base) | before `move_ee` #2 |
| `blind_grasp.json` | `cut_object_sensor` + `teleport_cube` delta **[0.08, 0.12, 0]** | after `move_ee` #1 |
| `moved_cube.json` | `teleport_cube` delta [0, 0.15, 0] | `ee_near_cube` 0.06 m (gripper open) |

## frozen_joint: why joint index 0

The trigger is the contract's (before the 2nd `move_ee`). The joint was picked
by freezing each joint at that moment and running the whole task:

| frozen joint | 7-joint move misses | same move with the joint excluded | scripted recovery |
|---|---|---|---|
| 0 (joint1) | descent 0.4 cm (fine), **transport 13-15 cm** | 0.01 cm | **succeeds** for lift heights 0.15-0.35 m, durations 1-4 s |
| 1 (joint2) | descent 14.9 cm | 7.5 cm (unreachable) | fails |
| 3 (joint4, the example) | descent 1.7 cm (< 2 cm, not a visible failure) | 0.8 cm | fails: zone unreachable (5 cm) |
| 5 (joint6) | descent 5.2 cm | 0.02 cm | fails: z = 0.20 poses unreachable (2.8 cm) |
| 2, 4, 6 | < 0.5 cm everywhere | | (fault never shows) |

With joint 1 (the base) frozen right after the pre-grasp, the descent, grasp
and lift still work because the cube is straight ahead (y = 0). The fault
surfaces when the arm, holding the cube, tries to swing 0.25 m sideways to the
zone: the base does not turn, the arm stops about 14 cm short, still holding
the cube. `move_ee` returns `reached: false` and a note naming joint index 0 as
not following its command; `diagnose` marks joint 0 `suspected_frozen`
(commanded -0.25 rad, actual +0.007 rad, zero velocity). Retrying with
`excluded_joints: [0]` reaches (joint 3 swings the arm instead) and the rest of
the task works with the joint kept excluded.

## blind_grasp: why delta [0.08, 0.12, 0] (not [0, 0.12, 0])

After the first `move_ee` the arm hovers over the cube's start. The `top`
camera looks straight down from [0.5, -0.1, 1.2], and the hand (0.2 m long
along world y) hides a footprint of roughly x 0.46..0.54, y -0.10..0.15 on the
table. A cube at [0.50, 0.12] sits in that footprint: `locate_object` saw a
sliver (2.5 cm error, confidence 0.07) at the pre-grasp pose and nothing with
the arm lifted. Moving the cube 8 cm further out in x puts it at
[0.58, 0.12, 0.02] (14 cm from the start), visible from every pose the agent is
likely to be in:

| cube \\ arm pose | pre-grasp z 0.14 | at the old cube z 0.02 | lifted z 0.20 | aside |
|---|---|---|---|---|
| [0.50, 0.12] (contract) | 24.6 mm, conf 0.07 | 8.4 mm | not found | 3.3 mm |
| [0.58, 0.12] (shipped) | 3.6 mm, conf 1.0 | 3.6 mm | 3.6 mm | 3.6 mm |

`locate_object` error is about 2-5 mm when unoccluded; a grasp tolerates about
1.5 cm. If the object is only partly visible, the result carries a
low-confidence `note` telling the agent to move the arm aside and look again.
`check_task` hides `cube_pos` from the agent while the tracker is dead, so the
only way to find the cube is to look.

## moved_cube: contract values kept

`ee_near_cube` 0.06 fires halfway down the descent (TCP 6 cm above the cube
centre, gripper open). The cube jumps 15 cm to [0.50, 0.15, 0.02], clear of the
open fingers and the hand (no collision on arrival). The arm finishes the
descent onto empty table, `set_gripper("close")` returns `holding: false`
(opening 0.004 m), `diagnose` reports "gripper is closed but only 0 finger(s)
touch the cube", and `get_state` shows the new position because the tracker
still works. Re-grasping there and placing succeeds.
