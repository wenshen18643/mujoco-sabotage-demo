"""Integration polish (video trace colours, NO CLAIM line, text holds, chips) and trace_md."""

from __future__ import annotations

import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[1]
if str(DEMO) not in sys.path:
    sys.path.insert(0, str(DEMO))

from sabotage_demo import video as V  # noqa: E402
from sabotage_demo.make_fixture_run import make_fixture_run  # noqa: E402
from sabotage_demo.trace_md import build_trace  # noqa: E402


def test_no_claim_line_is_concise():
    long = "Agent did not call report_outcome (error_max_turns). Last words: " + "x" * 400
    txt = V.trace_text({"type": "outcome", "payload": {"status": "none", "summary": long}})
    assert txt == "NO CLAIM agent stopped without report_outcome (turn limit reached)"
    assert "Last words" not in txt


def test_failure_results_use_alert_colour():
    def colour(result):
        return V.trace_entry({"type": "tool_result", "payload": {"tool": "x", "result": result}}).body_color
    assert colour({"reached": False, "final_error_m": 0.14}) == V.ERROR
    assert colour({"holding": False, "opening_m": 0.0004}) == V.ERROR          # closed on nothing
    assert colour({"joints": [], "summary": "joint index 0 is not following"}) == V.ERROR
    assert colour({"reached": True, "final_error_m": 0.0}) == V.RESULT
    assert colour({"holding": False, "opening_m": 0.08}) == V.RESULT           # opened: fine
    assert colour({"joints": [], "summary": "no anomalies detected"}) == V.RESULT


def test_text_events_hold_at_least_min(tmp_path):
    run_dir = make_fixture_run("hold", tmp_path, sabotage="freeze", time_scale=0.5, short=True)
    run = V.load_run(run_dir)
    plan = V.build_run_plan(run, hold_scale=0.1)
    # count consecutive output frames per newly shown agent_text entry while no frames advance
    holds, prev = {}, None
    for s in plan.specs:
        if s.n_entries and plan.entries[s.n_entries - 1].kind == "agent_text":
            key = s.n_entries
            holds[key] = holds.get(key, 0) + (1 if prev is None or prev.frame == s.frame else 0)
        prev = s
    assert holds and min(holds.values()) >= int(V.MIN_TEXT_HOLD_S * V.FPS) - 1


def test_trace_markdown_tells_the_story(tmp_path):
    run_dir = make_fixture_run("story", tmp_path, sabotage="freeze", time_scale=0.5, short=True)
    md = build_trace(run_dir)
    assert md.startswith("# ")
    assert "SABOTAGE" in md and "`> move_ee" in md and "Independent judge" in md
