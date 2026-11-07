"""Video composer tests (builder C): fixture runs -> MP4 / compiled MP4 / GIF.

Run from demo/:  python -m pytest tests/test_video_compose.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

DEMO = Path(__file__).resolve().parents[1]
if str(DEMO) not in sys.path:
    sys.path.insert(0, str(DEMO))

from sabotage_demo import video as V  # noqa: E402
from sabotage_demo.make_fixture_run import make_fixture_run  # noqa: E402

PRESET = "ultrafast"   # keep the suite fast; output format is identical
COMPILE_HOLD_SCALE = 0.5


def _read_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def expected_seconds(run_dir: Path, hold_scale: float = 1.0) -> float:
    """Independent estimate of the contract timeline for one run.

    Every recorded frame after the first plays once at 20 fps; each event that is
    not followed by new frames holds for min(3.0, 0.8 + 0.03 * len(text)); plus
    the 1.5 s tail hold after the run.
    """
    events = _read_jsonl(run_dir / "events.jsonl")
    times = [f["sim_time"] for f in _read_jsonl(run_dir / "frames.jsonl")]
    anchors, prev = [], 0
    for ev in events:
        fi = (ev.get("payload") or {}).get("frame_index")
        if fi is None:
            fi = max(i for i, t in enumerate(times) if t <= ev["sim_time"] + 1e-6)
        prev = max(prev, fi)
        anchors.append(prev)
    holds = 0.0
    for i, ev in enumerate(events):
        text = V.trace_text(ev)
        follows = i + 1 < len(events) and anchors[i + 1] > anchors[i]
        if text and not follows:
            holds += hold_scale * min(3.0, 0.8 + 0.03 * len(text))
    return (len(times) - 1) / 20.0 + holds + V.TAIL_S


@pytest.fixture(scope="module")
def runs(tmp_path_factory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("runs")
    return {
        "moved": make_fixture_run("moved", root, sabotage="teleport", time_scale=0.5, short=True),
        "frozen": make_fixture_run("frozen", root, sabotage="freeze", time_scale=0.5, short=True, fail=True),
    }


@pytest.fixture(scope="module")
def single(runs) -> dict:
    out = runs["moved"] / "video.mp4"
    gif = runs["moved"] / "teaser.gif"
    rep = V.compose([runs["moved"]], out, gif=gif, preset=PRESET, log=lambda *a: None)
    return rep


@pytest.fixture(scope="module")
def compiled(runs, tmp_path_factory) -> dict:
    out = tmp_path_factory.mktemp("final") / "final.mp4"
    # hold_scale 0.5 keeps the suite fast and exercises --hold-scale
    return V.compose([runs["moved"], runs["frozen"]], out, compile_mode=True, title="Sabotage demo test",
                     hold_scale=COMPILE_HOLD_SCALE, preset=PRESET, log=lambda *a: None)


# --------------------------------------------------------------------------- fixture
def test_fixture_is_contract_conformant(runs):
    d = runs["moved"]
    for name in ("events.jsonl", "frames.jsonl", "scenario.json", "summary.json"):
        assert (d / name).is_file(), name
    frames = sorted((d / "frames").glob("*.jpg"))
    assert len(frames) == len(_read_jsonl(d / "frames.jsonl")) > 50
    assert frames[0].name == "000000.jpg"
    from PIL import Image

    with Image.open(frames[0]) as im:
        assert im.size == (960, 720)
    events = _read_jsonl(d / "events.jsonl")
    types = [e["type"] for e in events]
    assert types[0] == "run_start" and types[-1] == "run_end"
    for t in ("agent_text", "tool_call", "tool_result", "sabotage", "judge", "outcome"):
        assert t in types, t
    assert [e["seq"] for e in events] == list(range(len(events)))
    for e in events:
        assert set(e) == {"seq", "wall_time", "sim_time", "type", "payload"}
        if e["type"] in ("tool_call", "tool_result"):
            assert isinstance(e["payload"]["frame_index"], int)
    summary = json.loads((d / "summary.json").read_text())
    assert set(summary) >= {"scenario", "success", "agent_claimed", "tool_calls", "sim_time", "wall_s", "model"}


# --------------------------------------------------------------------------- formatting
def test_trace_formatting():
    assert V.format_tool_call("mcp__mujoco__move_ee", {"pos": [0.5, 0.0, 0.14], "quat": None,
                                                       "excluded_joints": []}) == "> move_ee pos=[0.5,0.0,0.14]"
    assert V.format_tool_call("set_gripper", {"action": "close"}) == "> set_gripper action=close"
    assert V.format_tool_call("get_state", {}) == "> get_state"
    txt, err = V.format_tool_result("move_ee", {"reached": False, "final_error_m": 0.0712, "note": "joint4 lag"})
    assert txt == "← NOT reached err=0.071m joint4 lag" and not err
    txt, err = V.format_tool_result("set_gripper", {"opening_m": 0.0402, "holding": True, "sim_time": 3})
    assert txt == "← opening=0.040m holding=true"
    txt, err = V.format_tool_result("move_ee", {"error": "joint index out of range"})
    assert err and "error" in txt
    txt, _ = V.format_tool_result("check_task", {"success": True, "xy_distance_m": 0.004})
    assert txt == "← success=true dist=0.004m"
    state = {"tcp": {"pos": [0.5, 0, 0.1]}, "gripper": {"holding": False}, "objects": {"cube": None}}
    assert "tracker dead" in V.format_tool_result("get_state", state)[0]
    assert V.hold_seconds("x" * 10) == pytest.approx(1.1)
    assert V.hold_seconds("x" * 500) == 3.0


def test_wrap_keeps_every_char_and_width():
    text = "The cube is now at [0.50, 0.15, 0.02]; it was moved during my approach.\nRe-planning now."
    lines = V.wrap(text, 20, 2)
    assert all((t - s) + ind <= 20 for s, t, ind in lines)
    joined = "".join(text[s:t] for s, t, _ in lines).replace(" ", "")
    assert joined == text.replace(" ", "").replace("\n", "")


def test_timeline_rule(runs):
    run = V.load_run(runs["moved"])
    plan = V.build_run_plan(run)
    # real-time playback: every recorded frame shown, 1.5 output frames each on average
    shown = {s.frame for s in plan.specs}
    assert shown >= set(range(1, run.n_frames))
    moving = sum(1 for a, b in zip(plan.specs, plan.specs[1:]) if b.frame != a.frame)
    assert moving == run.n_frames - 1
    assert plan.n / V.FPS == pytest.approx(expected_seconds(runs["moved"]), abs=0.2)
    # sabotage banner lasts 2 s and judge stamp appears
    with_banner = [s for s in plan.specs if s.banner >= 0]
    assert len(with_banner) == int(V.BANNER_S * V.FPS)
    assert plan.verdict is True and plan.judge_k is not None
    assert plan.specs[-1].stamp_age > 0


# --------------------------------------------------------------------------- outputs
def test_single_run_mp4(single, runs):
    out = Path(single["out"])
    assert out.is_file() and out.stat().st_size > 0
    expected = expected_seconds(runs["moved"])
    assert single["measured_s"] == pytest.approx(expected, rel=0.10)
    assert single["measured_s"] == pytest.approx(single["planned_s"], abs=0.1)
    import imageio_ffmpeg

    reader = imageio_ffmpeg.read_frames(str(out))
    meta = next(reader)
    reader.close()
    assert tuple(meta["size"]) == (1280, 720)
    assert meta["fps"] == pytest.approx(30.0)
    assert "yuv420p" in meta.get("pix_fmt", "yuv420p")


def test_gif_teaser(single):
    from PIL import Image

    g = single["gif"]
    assert Path(g["path"]).is_file()
    with Image.open(g["path"]) as im:
        assert im.width == 480
        n = getattr(im, "n_frames", 1)
        assert n == g["frames"] > 12
        dur = sum((im.seek(i), im.info.get("duration", 0))[1] for i in range(n)) / 1000.0
    assert dur <= 25.5
    assert 10.5 <= n / dur <= 13.5


def test_compile_has_chapter_cards(compiled, runs):
    out = Path(compiled["out"])
    assert out.is_file() and out.stat().st_size > 0
    chapters = compiled["chapters"]
    kinds = [c["kind"] for c in chapters]
    assert kinds == ["card", "card", "run", "card", "run", "card"]   # title, ch1, run, ch2, run, end
    expected = (V.TITLE_CARD_S + 2 * V.CARD_S + V.END_CARD_S
                + expected_seconds(runs["moved"], COMPILE_HOLD_SCALE)
                + expected_seconds(runs["frozen"], COMPILE_HOLD_SCALE))
    assert compiled["measured_s"] == pytest.approx(expected, rel=0.10)

    # decode single frames (seek) and compare the middle of each chapter card with the rendered card
    def frame_at(t: float) -> np.ndarray:
        import imageio_ffmpeg

        reader = imageio_ffmpeg.read_frames(str(out), input_params=["-ss", f"{t:.3f}"])
        meta = next(reader)
        w, h = meta["size"]
        buf = next(reader)
        reader.close()
        return np.frombuffer(buf, np.uint8).reshape(h, w, 3)

    cards = [
        (chapters[1]["start_s"] + V.CARD_S / 2, V.make_chapter_card(1, V.load_run(runs["moved"]))),
        (chapters[3]["start_s"] + V.CARD_S / 2, V.make_chapter_card(2, V.load_run(runs["frozen"]))),
    ]
    for t, card in cards:
        diff = np.abs(frame_at(t).astype(np.int16) - card.astype(np.int16)).mean()
        assert diff < 6.0, f"frame at {t:.2f}s does not look like its chapter card (mean diff {diff:.1f})"
    run_frame = frame_at(chapters[2]["start_s"] + 3.0)
    assert np.abs(run_frame.astype(np.int16) - cards[0][1].astype(np.int16)).mean() > 15.0
