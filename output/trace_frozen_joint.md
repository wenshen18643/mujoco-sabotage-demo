# Chapter 2: a joint dies mid-task

Run `20261107_101509_frozen_joint` · model `opus` · task: *Put the red cube in the green target zone.*

| judge (check_task) | agent claimed | tool calls | sim time | agent wall time | cost |
|---|---|---|---|---|---|
| **success** | success | 17 | 16.26 s | 63.96 s | $0.155 |

Injected faults (the agent is never told): `{"kind": "freeze_joint", "joint": 0}` when `{"type": "tool_call", "tool": "move_ee", "n": 2, "phase": "before"}`

Legend: plain paragraphs are the agent's own words; numbered `>` lines are tool calls and `←` their results (abridged to the key fields); 🔴 **SABOTAGE** lines are faults fired by the simulator. `report_outcome` is not a simulator call, so it appears as the agent's claim rather than a numbered step. *(thinking)* lines are the reasoning summaries Claude Code exposes in its stream. `t+` is wall-clock seconds since the run started.

---

I'll start by reading the scene description to learn the workcell layout and conventions.

**1.** `> describe_scene` <sub>t+ 12.1s · sim 0.00s</sub>  
`← scene description (3342 chars)`

The scene is clear: cube near [0.50, 0.00, 0.02], zone at [0.50, -0.25]; now I'll read the live state to get actual positions.

**2.** `> get_state` <sub>t+ 13.4s · sim 0.00s</sub>  
`← tcp=[0.554,0.0,0.521] holding=false cube=[0.5,0.0,0.02]`

Tracker reports the cube at [0.50, 0.00, 0.02] and the gripper is already open (0.08 m), so I'll move to pre-grasp 0.12 m above the cube at [0.50, 0.00, 0.14].

**3.** `> move_ee pos=[0.5,0.0,0.14] duration_s=2.0` <sub>t+ 15.3s · sim 0.00s</sub>  
`← reached err=0.000m`

Pre-grasp reached with 0 mm error; I'll re-confirm the cube position before descending.

**4.** `> get_state` <sub>t+ 18.6s · sim 2.13s</sub>  
`← tcp=[0.5,0.0,0.14] holding=false cube=[0.5,0.0,0.02]`

Cube is unchanged at [0.50, 0.00, 0.02]; descending vertically to grasp height z = 0.02.

**5.** `> move_ee pos=[0.5,0.0,0.02] duration_s=2.0` <sub>t+ 20.3s · sim 2.13s</sub>  
🔴 **SABOTAGE (hidden from agent): Joint 1 frozen** `{"kind": "freeze_joint", "joint": 0}` <sub>t+ 20.3s · sim 2.13s</sub>  
`← reached err=0.004m`

Reached at z = 0.02 with 3.6 mm error, though joint 0 shows a -0.0071 rad tracking error (TCP 3.6 mm off in y), which I'll watch; closing the gripper now.

**6.** `> set_gripper action=close duration_s=0.5` <sub>t+ 25.1s · sim 4.26s</sub>  
`← opening=0.040m holding=true`

Grasp is good (opening 0.0396 m, holding = true); lifting straight up to z = 0.20.

**7.** `> move_ee pos=[0.5,0.0,0.2] duration_s=2.0` <sub>t+ 27.3s · sim 4.90s</sub>  
`← reached err=0.004m`

Lift reached z = 0.20, but joint 0 has lagged its command by ~0.0072 rad on two moves in a row, and the transport needs a large joint-0 rotation, so I'll run diagnose first.

**8.** `> diagnose` <sub>t+ 32.4s · sim 7.03s</sub>  
`← no anomalies detected`

Diagnose reports no anomalies; joint 0 isn't flagged frozen, though its angle has stayed at 0.0073 rad. I'll do the transport to [0.50, -0.25, 0.20] and check closely whether joint 0 follows.

**9.** `> move_ee pos=[0.5,-0.25,0.2] duration_s=2.0` <sub>t+ 34.8s · sim 7.03s</sub>  
`← NOT reached err=0.144m target NOT reached: the TCP stopped 14.4 cm from the target; joints not following their commands: index 0 (commanded -0.250, actual +0.007 rad)`

The transport failed: the TCP stopped 14.4 cm short, and joint 0 is stuck at +0.007 rad while commanded −0.250 rad. I'll run diagnose to confirm before I re-plan.

**10.** `> diagnose` <sub>t+ 38.6s · sim 9.16s</sub>  
`← joint index 0 (joint1) is not following its command: commanded -0.250 rad, actual +0.007 rad, and it did not move during the last move (suspected frozen); last move missed its target by 14.4 cm`

Diagnose confirms joint 0 is frozen at +0.0073 rad, and the cube is still held (2 finger contacts). I'll retry the transport with joint 0 excluded, and keep it excluded from here on.

**11.** `> move_ee pos=[0.5,-0.25,0.2] duration_s=2.0 excluded_joints=[0]` <sub>t+ 40.7s · sim 9.16s</sub>  
`← reached err=0.000m`

With joint 0 excluded, the arm reached the spot above the zone (0.1 mm error). Descending to z = 0.03 with joint 0 still excluded.

**12.** `> move_ee pos=[0.5,-0.25,0.03] duration_s=2.0 excluded_joints=[0]` <sub>t+ 44.7s · sim 11.29s</sub>  
`← reached err=0.000m`

Reached z = 0.03 over the zone centre (0.1 mm error); opening the gripper to release the cube.

**13.** `> set_gripper action=open duration_s=0.5` <sub>t+ 48.2s · sim 13.42s</sub>  
`← opening=0.080m holding=false`

The gripper is open (0.0796 m) and the cube is released; retreating straight up to z = 0.20 with joint 0 still excluded.

**14.** `> move_ee pos=[0.5,-0.25,0.2] duration_s=2.0 excluded_joints=[0]` <sub>t+ 50.6s · sim 14.13s</sub>  
`← reached err=0.000m`

The retreat reached z = 0.20 with 0 mm error; now running check_task to verify the success criteria.

**15.** `> check_task` <sub>t+ 54.0s · sim 16.26s</sub>  
`← success=true dist=0.002m`

check_task reports success, with the cube 1.8 mm from the zone centre. I'll render the front camera to confirm it visually.

**16.** `> render camera=front` <sub>t+ 55.4s · sim 16.26s</sub>  
`← image camera=front`

In the image, the red cube sits near the centre of the green zone, and the open gripper hangs well above it without touching. I'll log the outcome.

**Agent's claim (report_outcome): success**

> I picked up the red cube at [0.50, 0.00] and placed it in the green zone; check_task confirms success, with the cube at [0.502, -0.250], 1.8 mm from the zone centre, and the gripper open. Joint 0 (joint1) froze at about +0.007 rad. It first showed as a small lag during the descent, then caused the transport move to stop 14.4 cm short of its target. Diagnose confirmed joint 0 as suspected frozen, so I retried the transport with joint 0 excluded and kept it excluded for every later move, and all of them reached within 0.1 mm. The grasp held throughout, and the front camera shows the cube centred in the zone with the gripper clear above it.

The red cube is in the green target zone. It sits 1.8 mm from the zone centre and check_task passed.

**Problem: joint 0 (the base joint) froze at about +0.007 rad.** It first showed as a small lag of 0.007 rad while lowering to grab the cube. That cost only 3.6 mm, so I picked up the cube and lifted it. The move over to the zone then stopped 14.4 cm short because that move needs the base joint to turn. Diagnose confirmed joint 0 was frozen, and the cube was still held.

**Fix:** I retried the move with joint 0 locked in place, so the other six joints did the work. I kept it locked for the lowering, release and lift-off moves too, and each of those reached within 0.1 mm.

The front camera shows the cube near the middle of the green square, with the open gripper above it and not touching it. I logged the outcome as a success.

Joint 0 is still frozen, so the arm needs checking before its next job. Without that joint the arm can't turn at the base, so it can reach less of the table.

---

**Independent judge (`GET /check_task?source=director`): TASK VERIFIED** · cube 1.8 mm from the zone centre · the cube rests in the target zone and the gripper has released it

Run end: 17 tool calls, sim 16.26 s, agent wall 63.96 s.
