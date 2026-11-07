"""Scripted operator used by the test_sim_* tests (a careful human, not an LLM)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[1]
if str(DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(DEMO_DIR))

from sabotage_demo.scene import ZONE_POS  # noqa: E402

SCENARIOS = DEMO_DIR / "scenarios"
Z_PRE, Z_GRASP, Z_LIFT, Z_PLACE = 0.12, 0.02, 0.20, 0.03


def load_scenario(name: str) -> dict:
    return json.loads((SCENARIOS / f"{name}.json").read_text(encoding="utf-8"))


def move(sim, pos, excluded=(), must_reach=True) -> dict:
    res = sim.move_ee(list(map(float, pos)), excluded_joints=list(excluded))
    if must_reach:
        assert res["reached"], f"move to {pos} failed: {res}"
    return res


def grasp_at(sim, xy, excluded=()) -> dict:
    """Open, pre-grasp 12 cm above, descend, close. Returns the close result."""
    x, y = xy
    sim.set_gripper("open")
    move(sim, [x, y, Z_GRASP + Z_PRE], excluded)
    move(sim, [x, y, Z_GRASP], excluded)
    return sim.set_gripper("close")


def place_in_zone(sim, excluded=(), lift_first=True) -> dict:
    """Lift, carry over the zone, lower, release, retreat. Returns check_task."""
    zx, zy = ZONE_POS[0], ZONE_POS[1]
    if lift_first:
        tcp = sim.get_state()["tcp"]["pos"]
        move(sim, [tcp[0], tcp[1], Z_LIFT], excluded)
    move(sim, [zx, zy, Z_LIFT], excluded)
    move(sim, [zx, zy, Z_PLACE], excluded)
    opened = sim.set_gripper("open")
    assert not opened["holding"]
    move(sim, [zx, zy, Z_LIFT], excluded)
    return sim.check_task()
