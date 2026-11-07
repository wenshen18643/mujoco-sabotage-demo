"""(b) Baseline: a scripted pick and place with no faults reaches
check_task.success, the cube stays in the gripper, and the run directory gets
events and frames in the contract format."""

from __future__ import annotations

import json

import numpy as np
from PIL import Image

from sim_helpers import Z_LIFT, Z_PLACE, grasp_at, load_scenario, move
from sabotage_demo.scene import CUBE_START, ZONE_POS
from sabotage_demo.sim_core import FRAME_DT, SimCore


def test_baseline_pick_and_place(tmp_path):
    run = tmp_path / "run"
    sim = SimCore(run, load_scenario("baseline"))
    try:
        state = sim.get_state()
        cube = state["objects"]["cube"]["pos"]
        np.testing.assert_allclose(cube, CUBE_START, atol=1e-3)
        assert state["active_faults"] == [] and state["sensors"]["object_tracker"] == "ok"

        closed = grasp_at(sim, cube[:2])
        assert closed["holding"]
        assert closed["opening_m"] > 0.037, "fingers penetrate the cube"

        move(sim, [cube[0], cube[1], Z_LIFT])
        move(sim, [ZONE_POS[0], ZONE_POS[1], Z_LIFT])
        carried = sim.get_state()
        assert carried["gripper"]["holding"], "cube slipped out during transport"
        gap = np.subtract(carried["objects"]["cube"]["pos"], carried["tcp"]["pos"])
        assert np.linalg.norm(gap) < 0.01, f"cube shifted in the grasp by {gap}"

        move(sim, [ZONE_POS[0], ZONE_POS[1], Z_PLACE])
        assert not sim.set_gripper("open")["holding"]
        move(sim, [ZONE_POS[0], ZONE_POS[1], Z_LIFT])
        check = sim.check_task()
        assert check["success"], check
        assert check["xy_distance_m"] < 0.01
        assert sim.diagnose()["summary"] == "no anomalies detected"
    finally:
        sim.close()

    events = [json.loads(line) for line in (run / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[0]["type"] == "run_start" and events[0]["payload"]["scenario"] == "baseline"
    assert {"seq", "wall_time", "sim_time", "type", "payload"} <= set(events[1])
    assert not any(e["type"] == "sabotage" for e in events)
    calls = [e for e in events if e["type"] == "tool_call"]
    results = [e for e in events if e["type"] == "tool_result"]
    assert len(calls) == len(results) > 10
    assert all(r["payload"]["frame_index"] >= c["payload"]["frame_index"] for c, r in zip(calls, results, strict=True))

    frames = [json.loads(line) for line in (run / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
    jpgs = sorted((run / "frames").glob("*.jpg"))
    assert len(frames) == len(jpgs) > 200
    assert [f["index"] for f in frames] == list(range(len(frames)))
    assert frames[0]["sim_time"] == 0.0
    gaps = np.diff([f["sim_time"] for f in frames])
    assert np.allclose(gaps, FRAME_DT, atol=1e-3), "frames must be every 0.05 s of sim time"
    with Image.open(jpgs[-1]) as img:
        assert img.size == (960, 720) and img.format == "JPEG"
    assert json.loads((run / "scenario.json").read_text(encoding="utf-8"))["name"] == "baseline"
