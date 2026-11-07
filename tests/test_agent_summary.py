"""director helpers: summary.json, run ids, MCP config, command lines; prompt rules."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from sabotage_demo import director
from sabotage_demo.director import ALLOWED_TOOLS, build_cli_command, build_mcp_config, build_summary, format_sim_cmd, make_run_id, write_summary
from sabotage_demo.prompts import SYSTEM_PROMPT, build_task_prompt, prompt_word_count

CONTRACT_KEYS = ["scenario", "success", "agent_claimed", "tool_calls", "sim_time", "wall_s", "model"]


def test_build_summary_contract_keys_first():
    s = build_summary(scenario="frozen_joint", judge={"success": True, "reason": "ok"}, outcome={"status": "success", "summary": "done"}, tool_calls=17, sim_time=42.5, wall_s=88.1234, model="opus", extra={"run_id": "x", "success": "ignored"})
    assert list(s)[:7] == CONTRACT_KEYS
    assert s["success"] is True and s["agent_claimed"] == "success" and s["tool_calls"] == 17
    assert s["wall_s"] == 88.12 and s["run_id"] == "x"


def test_build_summary_without_judge_or_outcome():
    s = build_summary(scenario="b", judge=None, outcome=None, tool_calls=0, sim_time=None, wall_s=1, model="sonnet")
    assert s["success"] is False and s["agent_claimed"] is None and s["sim_time"] is None


def test_write_summary_roundtrip(tmp_path):
    s = build_summary(scenario="b", judge={"success": False}, outcome={"status": "failed"}, tool_calls=3, sim_time=1.0, wall_s=2.0, model="opus", extra={"note": "café"})
    path = write_summary(tmp_path, s)
    assert path == tmp_path / "summary.json"
    assert json.loads(path.read_text(encoding="utf-8")) == s
    assert not list(tmp_path.glob("*.tmp"))


def test_make_run_id():
    assert make_run_id("frozen_joint", datetime(2026, 11, 7, 1, 2, 3)) == "20261107_010203_frozen_joint"
    assert re.fullmatch(r"\d{8}_\d{6}_a_b", make_run_id("a b"))


def test_mcp_config_points_at_module():
    cfg = build_mcp_config(r"C:\venv\python.exe", "http://127.0.0.1:8765", Path(r"C:\demo"))
    srv = cfg["mcpServers"]["mujoco"]
    assert srv["type"] == "stdio" and srv["command"] == r"C:\venv\python.exe"
    assert srv["args"] == ["-m", "sabotage_demo.mcp_server", "--url", "http://127.0.0.1:8765"]
    assert srv["env"]["PYTHONPATH"] == str(Path(r"C:\demo"))


def test_allowed_tools_are_explicit_mujoco_list():
    assert len(ALLOWED_TOOLS) == 11 and all(t.startswith("mcp__mujoco__") for t in ALLOWED_TOOLS)


def test_format_sim_cmd():
    cmd = format_sim_cmd(director.DEFAULT_SIM_CMD, python=r"C:\py\python.exe", scenario=Path(r"C:\s\b.json"), run_dir=Path(r"C:\r\x"), port=8765)
    assert cmd == [r"C:\py\python.exe", "-m", "sabotage_demo.sim_server", "--scenario", str(Path(r"C:\s\b.json")), "--run-dir", str(Path(r"C:\r\x")), "--port", "8765"]


def test_cli_command_flags():
    cmd = build_cli_command(cli="claude", task="T", system_prompt="S", mcp_config=Path("cfg.json"), model="opus", max_turns=60)
    for flag in ("--output-format", "--verbose", "--mcp-config", "--strict-mcp-config", "--allowedTools", "--permission-mode", "--max-turns", "--system-prompt"):
        assert flag in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    assert cmd[cmd.index("--tools") + 1] == ""


def test_prompt_rules():
    low = SYSTEM_PROMPT.lower()
    assert prompt_word_count() < 600
    for banned in ("sabotage", "inject", "scenario", "chapter"):
        assert banned not in low
    for word in (r"\bfault", r"\btest"):  # "default" is fine; "fault"/"test" are not
        assert not re.search(word, low), word
    for needed in ("get_state", "excluded_joints", "diagnose", "locate_object", "check_task", "render", "report_outcome", "holding", "reached", "0.12", "0.02", "0.20", "0.03", "one tool call per message"):
        assert needed.lower() in low, needed


def test_task_prompt_uses_only_task_text():
    p = build_task_prompt({"name": "frozen_joint", "title": "Chapter 2: a joint dies mid-task", "task": "Put the red cube in the green target zone."})
    assert "Put the red cube in the green target zone." in p
    assert "joint dies" not in p and "frozen" not in p
