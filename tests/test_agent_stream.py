"""director.StreamProcessor fed with recorded Claude Code stream-json lines."""

from __future__ import annotations

import json
from pathlib import Path

from sabotage_demo.director import StreamProcessor, summarize_tool_result

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RECORDED = FIXTURES / "recorded_stream_fake_baseline.jsonl"  # real run: sonnet vs fake_sim_server

EXPECTED_SEQUENCE = [
    "describe_scene", "get_state", "set_gripper", "move_ee", "move_ee", "set_gripper", "move_ee",
    "move_ee", "move_ee", "set_gripper", "move_ee", "check_task", "render", "report_outcome",
]  # fmt: skip


def feed(lines, stream_path=None):
    proc = StreamProcessor(stream_path)
    posted = []
    for line in lines:
        posted.extend(proc.handle(json.loads(line) if isinstance(line, str) else line))
    proc.close()
    return proc, posted


def test_recorded_stream_tool_sequence_and_outcome(tmp_path):
    lines = RECORDED.read_text(encoding="utf-8").splitlines()
    out = tmp_path / "agent_stream.jsonl"
    proc, posted = feed(lines, out)
    assert proc.tool_sequence == EXPECTED_SEQUENCE
    assert proc.tool_calls == 14
    assert proc.outcome["status"] == "success" and "green zone" in proc.outcome["summary"]
    assert proc.images_delivered == 1
    assert proc.tool_errors == 0
    assert {k: proc.init["mcp_servers"][0][k] for k in ("name", "status")} == {"name": "mujoco", "status": "connected"}
    assert proc.result["subtype"] == "success" and proc.result["is_error"] is False
    assert not proc.pending  # every tool_use got a tool_result
    # raw stream is mirrored line for line
    assert len(out.read_text(encoding="utf-8").splitlines()) == len(lines)


def test_only_agent_text_and_thinking_are_posted():
    proc, posted = feed(RECORDED.read_text(encoding="utf-8").splitlines())
    kinds = {k for k, _ in posted}
    assert kinds == {"agent_text"}  # tool_call/tool_result are logged by sim_server, not us
    assert posted[0][1]["text"].startswith("Cube is still at")
    assert "front camera image shows the red cube" in posted[-1][1]["text"]
    assert proc.last_text == posted[-1][1]["text"]


def test_thinking_blocks_and_ignored_messages():
    msgs = [
        {"type": "control_request", "request_id": "1", "request": {}},
        {"type": "system", "subtype": "init", "model": "m", "tools": ["mcp__mujoco__get_state", "Bash"], "mcp_servers": [{"name": "mujoco", "status": "failed"}]},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "The cube might have moved.", "signature": "x"},
            {"type": "thinking", "thinking": "", "signature": "y"},
            {"type": "text", "text": "  "},
            {"type": "tool_use", "id": "t1", "name": "mcp__mujoco__diagnose", "input": {}},
        ]}},
        {"type": "assistant", "parent_tool_use_id": "sub", "message": {"content": [{"type": "text", "text": "subagent chatter"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": [{"type": "text", "text": "simulator not reachable"}]}]}},
        {"type": "rate_limit_event", "rate_limit_info": {}},
    ]
    proc, posted = feed(msgs)
    assert posted == [("agent_thinking", {"text": "The cube might have moved."})]
    assert proc.tool_sequence == ["diagnose"] and proc.tool_calls == 1 and proc.tool_errors == 1
    assert proc.outcome is None and proc.messages == 5  # control_request not counted


def test_non_mujoco_tools_are_not_counted():
    proc, _ = feed([{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "a", "name": "ToolSearch", "input": {}}]}}])
    assert proc.tool_calls == 0 and proc.tool_sequence == ["ToolSearch"]


def test_summarize_tool_result():
    move = json.dumps({"reached": False, "final_error_m": 0.061, "note": "target not reached"})
    assert summarize_tool_result("move_ee", [{"type": "text", "text": move}]) == '{"reached":false,"final_error_m":0.061,"note":"target not reached"}'
    state = json.dumps({"objects": {"cube": None}, "tcp": {"pos": [0.3, 0, 0.5]}, "gripper": {"holding": False}})
    assert '"cube":null' in summarize_tool_result("get_state", state)
    img = [{"type": "image", "source": {"type": "base64", "data": "x"}}, {"type": "text", "text": "camera=front sim_time=1.00"}]
    assert summarize_tool_result("render", img).startswith("[1 image] camera=front")
    assert summarize_tool_result("diagnose", json.dumps({"summary": "joint4 frozen"})) == '{"summary":"joint4 frozen"}'
    assert len(summarize_tool_result("describe_scene", "x" * 1000)) <= 160
