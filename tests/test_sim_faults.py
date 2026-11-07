"""(c) frozen joint, (d) blind grasp, (e) moved cube: each shipped scenario's
fault fires on its trigger, produces a visible and diagnosable failure, and a
careful scripted operator recovers and completes the task."""

from __future__ import annotations

import json

import numpy as np

from sim_helpers import Z_LIFT, grasp_at, load_scenario, move, place_in_zone
from sabotage_demo.scene import CUBE_START, ZONE_POS
from sabotage_demo.sim_core import SimCore


def _sabotage_events(sim: SimCore) -> list[dict]:
    return [e for e in sim.events_since(0)[0] if e["type"] == "sabotage"]


def _assert_frame_after(run, event: dict) -> None:
    frames = [json.loads(line) for line in (run / "frames.jsonl").read_text(encoding="utf-8").splitlines()]
    idx = event["payload"]["frame_index"]
    assert frames[idx]["index"] == idx
    assert abs(frames[idx]["sim_time"] - event["sim_time"]) < 1e-3, "no frame recorded right after the fault"
    assert (run / "frames" / f"{idx:06d}.jpg").is_file()


def test_frozen_joint_miss_diagnose_and_recover(tmp_path):
    scenario = load_scenario("frozen_joint")
    joint = scenario["triggers"][0]["sabotage"]["joint"]
    run = tmp_path / "run"
    sim = SimCore(run, scenario)
    try:
        closed = grasp_at(sim, CUBE_START[:2])  # the 2nd move_ee (the descent) fires the freeze
        (ev,) = _sabotage_events(sim)
        assert ev["payload"]["label"] == f"Joint {joint + 1} frozen"
        assert sim.get_state()["active_faults"] == [f"freeze_joint:{joint}"]
        frozen_angle = sim.get_state()["joints"]["pos"][joint]
        assert closed["holding"]
        move(sim, [CUBE_START[0], CUBE_START[1], Z_LIFT])

        above_zone = [ZONE_POS[0], ZONE_POS[1], Z_LIFT]
        miss = move(sim, above_zone, must_reach=False)  # the normal 7-joint IK move
        assert not miss["reached"] and miss["final_error_m"] > 0.02, miss
        assert f"index {joint}" in miss["note"]
        assert sim.get_state()["gripper"]["holding"], "the failed move must not drop the cube"

        diag = sim.diagnose()
        assert [j["index"] for j in diag["joints"] if j["suspected_frozen"]] == [joint]
        assert diag["last_move"]["worst_joint"] == joint and "suspected frozen" in diag["summary"]

        hit = move(sim, above_zone, excluded=[joint])  # same move, joint excluded
        assert hit["reached"] and hit["final_error_m"] < 0.015
        check = place_in_zone(sim, excluded=[joint], lift_first=False)
        assert check["success"], check
        assert abs(sim.get_state()["joints"]["pos"][joint] - frozen_angle) < 1e-6
    finally:
        sim.close()
    _assert_frame_after(run, ev)


def test_blind_grasp_locate_and_recover(tmp_path):
    run = tmp_path / "run"
    sim = SimCore(run, load_scenario("blind_grasp"))
    try:
        move(sim, [CUBE_START[0], CUBE_START[1], CUBE_START[2] + 0.12])  # 1st move_ee fires both faults
        labels = [e["payload"]["label"] for e in _sabotage_events(sim)]
        assert labels[0] == "Object tracker cut" and labels[1].startswith("Cube moved")
        state = sim.get_state()
        assert state["objects"]["cube"] is None
        assert state["sensors"]["object_tracker"] == "dead"
        assert sim.check_task()["cube_pos"] is None, "check_task must not leak the cube position"
        assert "object tracker" in sim.diagnose()["summary"]

        truth = sim.data.xpos[sim.cube_body].copy()
        assert np.linalg.norm(truth[:2] - np.array(CUBE_START[:2])) > 0.1  # it really moved
        seen = sim.locate_object(color="red", camera="top")
        assert seen["found"] and seen["confidence"] > 0.8
        assert np.linalg.norm(np.array(seen["pos"]) - truth) < 0.015, (seen, truth)

        closed = grasp_at(sim, seen["pos"][:2])
        assert closed["holding"]
        assert place_in_zone(sim)["success"]
    finally:
        sim.close()
    for ev in _sabotage_events(sim):
        _assert_frame_after(run, ev)


def test_moved_cube_mid_reach_then_regrasp(tmp_path):
    run = tmp_path / "run"
    sim = SimCore(run, load_scenario("moved_cube"))
    try:
        closed = grasp_at(sim, CUBE_START[:2])
        (ev,) = _sabotage_events(sim)
        assert ev["payload"]["source"] == "trigger" and ev["payload"]["label"] == "Cube moved 15 cm"
        calls = [e for e in sim.events_since(0)[0] if e["type"] == "tool_call" and e["seq"] < ev["seq"]]
        assert calls[-1]["payload"]["tool"] == "move_ee"  # fired mid-reach, during the descent
        assert not closed["holding"] and closed["opening_m"] < 0.01  # closed on nothing

        state = sim.get_state()
        new_pos = state["objects"]["cube"]["pos"]
        assert abs(new_pos[1] - CUBE_START[1] - 0.15) < 0.01
        assert "only 0 finger" in sim.diagnose()["summary"]

        sim.set_gripper("open")
        tcp = state["tcp"]["pos"]
        move(sim, [tcp[0], tcp[1], Z_LIFT])
        regrasp = grasp_at(sim, new_pos[:2])
        assert regrasp["holding"]
        assert len(_sabotage_events(sim)) == 1  # fires once
        assert place_in_zone(sim)["success"]
    finally:
        sim.close()
    _assert_frame_after(run, ev)


def test_manual_freeze_then_clear_does_not_snap(tmp_path):
    sim = SimCore(tmp_path / "run", load_scenario("baseline"), record_frames=False)
    try:
        move(sim, [0.5, 0.0, 0.2])
        assert sim.apply_sabotage({"kind": "freeze_joint", "joint": 3})["active"] == ["freeze_joint:3"]
        move(sim, [0.5, 0.0, 0.1], must_reach=False)
        q_before = sim.get_state()["joints"]["pos"]
        assert sim.clear_sabotage() == {"ok": True}
        sim.step(0.5)
        q_after = sim.get_state()["joints"]["pos"]
        assert np.max(np.abs(np.subtract(q_after, q_before))) < 0.01, "thawed joint snapped to a stale command"
        assert sim.get_state()["active_faults"] == []
        assert sim.reset_scene() == {"ok": True}
        np.testing.assert_allclose(sim.get_state()["objects"]["cube"]["pos"], CUBE_START, atol=1e-3)
    finally:
        sim.close()
