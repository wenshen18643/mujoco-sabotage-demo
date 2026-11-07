"""Run one sabotage scenario end to end with Claude Code as the robot operator.

    cd demo
    ..\\.venv\\Scripts\\python.exe -m sabotage_demo.director --scenario scenarios/baseline.json

Steps: create runs/<YYYYMMDD_HHMMSS_<scenario>>, start sim_server.py on --port,
write an MCP config that points server ``mujoco`` at ``sabotage_demo.mcp_server``,
run Claude Code (subscription login, no API key) with only the mujoco tools,
stream its text to the sim server's /events, judge with /check_task, write
summary.json, stop the sim server, and render the video (builder C's module).

Agent integration: the ``claude-agent-sdk`` Python package (default,
``--runner sdk``) drives the logged-in Claude Code CLI. ``--runner cli`` is a
fallback that runs ``claude -p --output-format stream-json`` directly. Both
paths feed the same raw stream-json messages into :class:`StreamProcessor`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .mcp_server import TOOL_NAMES
from .prompts import SYSTEM_PROMPT, build_task_prompt

DEMO_DIR = Path(__file__).resolve().parents[1]
PKG_DIR = DEMO_DIR / "sabotage_demo"
MCP_SERVER_NAME = "mujoco"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
ALLOWED_TOOLS = [TOOL_PREFIX + n for n in TOOL_NAMES]

DEFAULT_SIM_CMD = "{python} -m sabotage_demo.sim_server --scenario {scenario} --run-dir {run_dir} --port {port}"
FAKE_SIM_CMD = "{python} -m sabotage_demo.fake_sim_server --scenario {scenario} --run-dir {run_dir} --port {port}"

# Environment for the Claude Code subprocess (merged over os.environ).
AGENT_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # no claude.ai connectors: only the mujoco tools
    "MCP_TIMEOUT": "30000",  # ms to wait for the mujoco server to start
    "MCP_TOOL_TIMEOUT": "180000",  # ms per tool call (slow moves + frame recording)
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUNBUFFERED": "1",
    "DISABLE_AUTOUPDATER": "1",
}

RawHandler = Callable[[dict], Awaitable[None]]


# --------------------------------------------------------------------------
# Small pure helpers (unit-tested)
# --------------------------------------------------------------------------


def make_run_id(scenario_name: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in scenario_name) or "scenario"
    return f"{now:%Y%m%d_%H%M%S}_{safe}"


def build_mcp_config(python: str, url: str, demo_dir: Path = DEMO_DIR) -> dict:
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": str(python),
                "args": ["-m", "sabotage_demo.mcp_server", "--url", url],
                "env": {"PYTHONPATH": str(demo_dir), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"},
            }
        }
    }


def build_summary(
    *,
    scenario: str,
    judge: dict | None,
    outcome: dict | None,
    tool_calls: int,
    sim_time: float | None,
    wall_s: float,
    model: str,
    extra: dict | None = None,
) -> dict:
    """summary.json content. The first seven keys are frozen by CONTRACT.md."""
    summary = {
        "scenario": scenario,
        "success": bool(judge.get("success")) if judge else False,
        "agent_claimed": (outcome or {}).get("status") if outcome else None,
        "tool_calls": int(tool_calls),
        "sim_time": sim_time,
        "wall_s": round(float(wall_s), 2),
        "model": model,
    }
    if extra:
        summary.update({k: v for k, v in extra.items() if k not in summary})
    return summary


def write_summary(run_dir: Path, summary: dict) -> Path:
    path = Path(run_dir) / "summary.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def format_sim_cmd(template: str, *, python: str, scenario: Path, run_dir: Path, port: int) -> list[str]:
    tokens = shlex.split(template, posix=os.name != "nt")
    if os.name == "nt":
        tokens = [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in tokens]
    values = {"python": str(python), "scenario": str(scenario), "run_dir": str(run_dir), "port": str(port)}
    return [t.format(**values) for t in tokens]


def _short(value: Any, n: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3] + "..."


def _fmt_args(args: dict) -> str:
    parts = []
    for k, v in (args or {}).items():
        if isinstance(v, list):
            v = "[" + ", ".join(f"{x:.3f}" if isinstance(x, float) else str(x) for x in v) + "]"
        elif isinstance(v, str) and len(v) > 60:
            v = repr(v[:57] + "...")
        parts.append(f"{k}={v}")
    return " ".join(parts)


def summarize_tool_result(tool: str, content: Any) -> str:
    """One console line for a tool result (content is str or list of blocks)."""
    texts, images = [], 0
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                texts.append(b.get("text", ""))
            elif b.get("type") == "image":
                images += 1
    text = "\n".join(texts)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    prefix = f"[{images} image] " if images else ""
    if not isinstance(data, dict):
        return prefix + _short(text)
    pick: dict[str, Any]
    if tool in ("move_ee", "move_joints"):
        pick = {k: data.get(k) for k in ("reached", "final_error_m", "note") if data.get(k) not in (None, "")}
    elif tool == "set_gripper":
        pick = {k: data.get(k) for k in ("holding", "opening_m")}
    elif tool == "get_state":
        cube = (data.get("objects") or {}).get("cube")
        pick = {"cube": cube.get("pos") if isinstance(cube, dict) else None, "tcp": (data.get("tcp") or {}).get("pos"), "holding": (data.get("gripper") or {}).get("holding")}
    elif tool == "locate_object":
        pick = {k: data.get(k) for k in ("found", "pos", "confidence")}
    elif tool == "diagnose":
        pick = {"summary": data.get("summary")}
    elif tool == "check_task":
        pick = {k: data.get(k) for k in ("success", "xy_distance_m", "reason")}
    else:
        pick = data
    return prefix + _short(pick)


# --------------------------------------------------------------------------
# Console
# --------------------------------------------------------------------------


class Console:
    COLORS = {"agent": "36", "think": "90", "call": "33", "result": "37", "sabotage": "1;41;97", "info": "32", "warn": "1;33", "error": "1;31", "judge": "1;35"}

    def __init__(self, color: bool | None = None, quiet: bool = False):
        if color is None:
            color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        self.color = color
        self.quiet = quiet
        if color and os.name == "nt":
            os.system("")  # enable VT escape processing in conhost

    def line(self, kind: str, label: str, text: str) -> None:
        if self.quiet and kind in ("think", "result"):
            return
        stamp = time.strftime("%H:%M:%S")
        head = f"{stamp} {label:<9}|"
        if self.color:
            print(f"\033[{self.COLORS.get(kind, '0')}m{head}\033[0m {text}", flush=True)
        else:
            print(f"{head} {text}", flush=True)

    def info(self, text: str) -> None:
        self.line("info", "director", text)

    def warn(self, text: str) -> None:
        self.line("warn", "WARNING", text)

    def error(self, text: str) -> None:
        self.line("error", "ERROR", text)


# --------------------------------------------------------------------------
# Stream processing (shared by SDK and CLI paths)
# --------------------------------------------------------------------------


class StreamProcessor:
    """Consumes raw Claude Code stream-json messages.

    Writes each message to agent_stream.jsonl, prints the live console log, and
    returns the sim events to post (``agent_text`` / ``agent_thinking``).
    ``tool_call``/``tool_result`` are NOT returned: sim_server logs those itself.
    """

    def __init__(self, stream_path: Path | None, console: Console | None = None, tool_prefix: str = TOOL_PREFIX):
        self.stream_path = stream_path
        self._fh = open(stream_path, "a", encoding="utf-8") if stream_path else None
        self.console = console
        self.tool_prefix = tool_prefix
        self.tool_calls = 0
        self.tool_sequence: list[str] = []
        self.pending: dict[str, str] = {}
        self.outcome: dict | None = None
        self.result: dict | None = None
        self.init: dict | None = None
        self.last_text: str = ""
        self.images_delivered = 0
        self.tool_errors = 0
        self.messages = 0

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def _say(self, kind: str, label: str, text: str) -> None:
        if self.console:
            self.console.line(kind, label, text)

    def _short_name(self, name: str) -> str:
        return name[len(self.tool_prefix):] if name.startswith(self.tool_prefix) else name

    def handle(self, msg: dict) -> list[tuple[str, dict]]:
        if not isinstance(msg, dict):
            return []
        mtype = msg.get("type")
        if mtype in ("control_request", "control_response", "control_cancel_request", "keep_alive"):
            return []
        self.messages += 1
        if self._fh:
            self._fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self._fh.flush()
        out: list[tuple[str, dict]] = []

        if mtype == "system" and msg.get("subtype") == "init":
            self.init = msg
            servers = {s.get("name"): s.get("status") for s in msg.get("mcp_servers", []) if isinstance(s, dict)}
            tools = [t for t in msg.get("tools", []) if isinstance(t, str)]
            mine = [t for t in tools if t.startswith(self.tool_prefix)]
            others = [t for t in tools if not t.startswith(self.tool_prefix)]
            self._say("info", "claude", f"session started: model={msg.get('model')} mcp={servers} tools={len(mine)} mujoco + {len(others)} other")
            if servers.get(MCP_SERVER_NAME) not in (None, "connected"):
                self._say("error", "ERROR", f"MCP server '{MCP_SERVER_NAME}' status: {servers.get(MCP_SERVER_NAME)}")
            if others:
                self._say("warn", "WARNING", f"agent also has non-mujoco tools: {others}")

        elif mtype == "assistant":
            if msg.get("parent_tool_use_id"):
                return out
            for block in (msg.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        self.last_text = text
                        self._say("agent", "agent", text)
                        out.append(("agent_text", {"text": text}))
                elif btype == "thinking":
                    text = (block.get("thinking") or "").strip()
                    if text:
                        self._say("think", "thinking", _short(text, 220))
                        out.append(("agent_thinking", {"text": text}))
                elif btype == "tool_use":
                    name = self._short_name(block.get("name", "?"))
                    args = block.get("input") or {}
                    if block.get("name", "").startswith(self.tool_prefix):
                        self.tool_calls += 1
                    self.tool_sequence.append(name)
                    self.pending[block.get("id", "")] = name
                    if name == "report_outcome":
                        self.outcome = {"status": args.get("status"), "summary": args.get("summary", "")}
                    self._say("call", "tool ->", f"{name} {_fmt_args(args)}".rstrip())

        elif mtype == "user":
            if msg.get("parent_tool_use_id"):
                return out
            content = (msg.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    name = self.pending.pop(block.get("tool_use_id", ""), "?")
                    bcontent = block.get("content")
                    if isinstance(bcontent, list):
                        self.images_delivered += sum(1 for b in bcontent if isinstance(b, dict) and b.get("type") == "image")
                    if block.get("is_error"):
                        self.tool_errors += 1
                        self._say("error", "tool <-", f"{name} ERROR {summarize_tool_result(name, bcontent)}")
                    else:
                        self._say("result", "tool <-", f"{name} {summarize_tool_result(name, bcontent)}")

        elif mtype == "result":
            self.result = msg
            cost = msg.get("total_cost_usd")
            cost_s = f" cost=${cost:.3f}" if isinstance(cost, (int, float)) else ""
            self._say("info", "claude", f"finished: {msg.get('subtype')} turns={msg.get('num_turns')}{cost_s} is_error={msg.get('is_error')}")
            if msg.get("is_error") and msg.get("result"):
                self._say("error", "ERROR", _short(msg.get("result"), 300))

        return out


# --------------------------------------------------------------------------
# Claude Code runners
# --------------------------------------------------------------------------


def find_claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(cand) if cand.exists() else None


def _sdk_block_to_raw(block: Any) -> dict:
    from claude_agent_sdk import TextBlock, ThinkingBlock, ToolResultBlock, ToolUseBlock

    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ThinkingBlock):
        return {"type": "thinking", "thinking": block.thinking}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultBlock):
        return {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content, "is_error": block.is_error}
    return {"type": type(block).__name__}


def sdk_message_to_raw(message: Any) -> dict:
    """Fallback converter (typed SDK message -> stream-json dict) used only if
    the raw-stream tee transport is unavailable."""
    import dataclasses

    from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, UserMessage

    if isinstance(message, AssistantMessage):
        return {"type": "assistant", "parent_tool_use_id": message.parent_tool_use_id, "message": {"model": message.model, "content": [_sdk_block_to_raw(b) for b in message.content]}}
    if isinstance(message, UserMessage):
        content = message.content if isinstance(message.content, str) else [_sdk_block_to_raw(b) for b in message.content]
        return {"type": "user", "parent_tool_use_id": message.parent_tool_use_id, "message": {"content": content}}
    if isinstance(message, SystemMessage):
        return {"type": "system", "subtype": message.subtype, **(message.data or {})}
    if isinstance(message, ResultMessage):
        return {"type": "result", **{k: v for k, v in dataclasses.asdict(message).items() if v is not None}}
    return {"type": type(message).__name__}


async def run_agent_sdk(*, task: str, system_prompt: str, mcp_config: Path, model: str, max_turns: int, cwd: Path, on_raw: RawHandler, stderr_path: Path, cli_path: str | None) -> None:
    from claude_agent_sdk import ClaudeAgentOptions, Transport, query

    stderr_fh = open(stderr_path, "a", encoding="utf-8")
    options = ClaudeAgentOptions(
        mcp_servers=str(mcp_config),
        strict_mcp_config=True,
        tools=[],  # no built-in tools (no Bash/Read/Edit): the agent only has the robot
        allowed_tools=list(ALLOWED_TOOLS),
        permission_mode="bypassPermissions",
        model=model,
        system_prompt=system_prompt,
        max_turns=max_turns,
        cwd=str(cwd),
        setting_sources=[],  # ignore user/project settings, hooks and CLAUDE.md
        env=dict(AGENT_ENV),
        cli_path=cli_path,
        stderr=lambda line: (stderr_fh.write(line.rstrip("\n") + "\n"), stderr_fh.flush()),
    )

    class TeeTransport(Transport):
        """Pass-through transport that hands every raw stream-json dict to on_raw."""

        def __init__(self, inner: Transport):
            self.inner = inner

        async def connect(self) -> None:
            await self.inner.connect()

        async def write(self, data: str) -> None:
            await self.inner.write(data)

        def read_messages(self):
            return self._read()

        async def _read(self):
            async for raw in self.inner.read_messages():
                try:
                    await on_raw(raw)
                except Exception as e:  # never break the agent loop on a logging problem
                    print(f"[director] stream handler error: {e!r}", file=sys.stderr)
                yield raw

        async def close(self) -> None:
            await self.inner.close()

        def is_ready(self) -> bool:
            return self.inner.is_ready()

        async def end_input(self) -> None:
            await self.inner.end_input()

    try:
        try:
            from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

            transport: Transport | None = TeeTransport(SubprocessCLITransport(prompt=task, options=options))
        except Exception:  # internal API moved: fall back to typed messages
            transport = None
        async for message in query(prompt=task, options=options, transport=transport):
            if transport is None:
                await on_raw(sdk_message_to_raw(message))
    finally:
        stderr_fh.close()


def build_cli_command(*, cli: str, task: str, system_prompt: str, mcp_config: Path, model: str, max_turns: int) -> list[str]:
    return [
        cli, "-p", task,
        "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(mcp_config), "--strict-mcp-config",
        "--tools", "",
        "--allowedTools", ",".join(ALLOWED_TOOLS),
        "--permission-mode", "bypassPermissions",
        "--model", model,
        "--max-turns", str(max_turns),
        "--system-prompt", system_prompt,
        "--setting-sources=",
    ]  # fmt: skip


async def run_agent_cli(*, task: str, system_prompt: str, mcp_config: Path, model: str, max_turns: int, cwd: Path, on_raw: RawHandler, stderr_path: Path, cli_path: str | None) -> None:
    cli = cli_path or find_claude_cli()
    if not cli:
        raise RuntimeError("claude CLI not found on PATH")
    cmd = build_cli_command(cli=cli, task=task, system_prompt=system_prompt, mcp_config=mcp_config, model=model, max_turns=max_turns)
    env = {**os.environ, **AGENT_ENV}
    env.pop("CLAUDECODE", None)
    with open(stderr_path, "ab") as err:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=err, limit=64 * 1024 * 1024)
        assert proc.stdout is not None
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                print(f"[director] non-JSON from claude: {line[:200]!r}", file=sys.stderr)
                continue
            await on_raw(raw)
        rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"claude exited with code {rc} (see {stderr_path.name})")


# --------------------------------------------------------------------------
# Sim server process
# --------------------------------------------------------------------------


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def child_env() -> dict:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    env["PYTHONPATH"] = os.pathsep.join([str(DEMO_DIR)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    return env


def start_sim_server(cmd: list[str], log_path: Path) -> subprocess.Popen:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    log = open(log_path, "ab")
    return subprocess.Popen(cmd, cwd=str(DEMO_DIR), env=child_env(), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)


async def wait_for_health(url: str, proc: subprocess.Popen, log_path: Path, timeout_s: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:] if log_path.exists() else ""
                raise RuntimeError(f"sim server exited with code {proc.returncode} before becoming healthy:\n{tail}")
            try:
                r = await client.get(f"{url}/health")
                if r.status_code == 200 and r.json().get("ok"):
                    return r.json()
            except (httpx.HTTPError, ValueError):
                pass
            await asyncio.sleep(0.3)
    raise RuntimeError(f"sim server did not answer {url}/health within {timeout_s:.0f} s (see {log_path})")


def stop_sim_server(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=8)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


async def post_event(http: httpx.AsyncClient, etype: str, payload: dict, console: Console | None = None) -> None:
    try:
        r = await http.post("/events", json={"type": etype, "payload": payload})
        if r.status_code >= 400 and console:
            console.warn(f"POST /events {etype} -> HTTP {r.status_code}: {r.text[:200]}")
    except httpx.HTTPError as e:
        if console:
            console.warn(f"POST /events {etype} failed: {e!r}")


async def poll_events(http: httpx.AsyncClient, console: Console, stop: asyncio.Event, interval_s: float = 0.4) -> None:
    """Print simulator-side story events (sabotage) as they happen."""
    since = 0
    while True:
        try:
            r = await http.get("/events", params={"since": since})
            data = r.json()
            for ev in data.get("events", []):
                if ev.get("type") == "sabotage":
                    p = ev.get("payload") or {}
                    console.line("sabotage", "SABOTAGE", f"{p.get('label') or p.get('spec')} (sim t={ev.get('sim_time')})")
            since = int(data.get("next", since))
        except (httpx.HTTPError, ValueError):
            pass
        if stop.is_set():
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass


# --------------------------------------------------------------------------
# Main flow
# --------------------------------------------------------------------------


def _resolve(p: str) -> Path:
    """Resolve a relative path against cwd, demo/ or demo/sabotage_demo/.

    The first candidate that exists wins (so ``scenarios/x.json`` from demo/ and
    ``../scenarios/x.json`` from the package dir both work); for paths that do
    not exist yet (a new runs dir) the first candidate inside demo/ wins.
    """
    path = Path(p)
    if path.is_absolute():
        return path
    candidates = [(base / path).resolve() for base in (Path.cwd(), DEMO_DIR, PKG_DIR)]
    for c in candidates:
        if c.exists():
            return c
    for c in candidates:
        if c == DEMO_DIR or DEMO_DIR in c.parents:
            return c
    return candidates[0]


async def run(args: argparse.Namespace) -> int:
    console = Console(color=None if not args.no_color else False, quiet=args.quiet)
    python = sys.executable
    scenario_path = _resolve(args.scenario)
    if not scenario_path.exists():
        console.error(f"scenario not found: {scenario_path}")
        return 1
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    name = scenario.get("name") or scenario_path.stem
    runs_dir = _resolve(args.runs_dir) if args.runs_dir else DEMO_DIR / "runs"
    run_id = make_run_id(name)
    run_dir = (runs_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "scenario.json").write_text(json.dumps(scenario, indent=2) + "\n", encoding="utf-8")

    url = f"http://127.0.0.1:{args.port}"
    template = args.sim_server_cmd or (FAKE_SIM_CMD if args.fake_sim else DEFAULT_SIM_CMD)
    if template == DEFAULT_SIM_CMD and not (PKG_DIR / "sim_server.py").exists():
        console.error(f"{PKG_DIR / 'sim_server.py'} does not exist yet (use --fake-sim to test with the stand-in)")
        return 1
    if port_in_use(args.port):
        console.error(f"port {args.port} is already in use; stop the other server or pass --port")
        return 1

    console.info(f"scenario {name}: {scenario.get('title', '')}")
    console.info(f"run dir   {run_dir}")
    cmd = format_sim_cmd(template, python=python, scenario=scenario_path, run_dir=run_dir, port=args.port)
    sim_log = run_dir / "sim_server.log"
    proc = start_sim_server(cmd, sim_log)
    t_start = time.time()
    exit_code = 1
    summary: dict | None = None
    try:
        health = await wait_for_health(url, proc, sim_log, timeout_s=args.health_timeout)
        console.info(f"sim server up: {url}  dashboard: {url}/  ({'FAKE ' if health.get('fake') else ''}scenario={health.get('scenario')})")

        mcp_config = run_dir / "mcp_config.json"
        mcp_config.write_text(json.dumps(build_mcp_config(python, url), indent=2) + "\n", encoding="utf-8")
        task = build_task_prompt(scenario)
        cli_path = args.claude_cli or find_claude_cli()
        console.info(f"agent: Claude Code via {args.runner}, model={args.model}, max_turns={args.max_turns}, cli={cli_path or 'bundled'}")
        console.info(f"task: {task.splitlines()[0]}")

        processor = StreamProcessor(run_dir / "agent_stream.jsonl", console)
        stop = asyncio.Event()
        async with httpx.AsyncClient(base_url=url, timeout=30.0) as http:

            async def on_raw(raw: dict) -> None:
                for etype, payload in processor.handle(raw):
                    await post_event(http, etype, payload, console)

            poller = asyncio.create_task(poll_events(http, console, stop))
            runner = run_agent_sdk if args.runner == "sdk" else run_agent_cli
            agent_error: str | None = None
            t_agent = time.time()
            try:
                await runner(task=task, system_prompt=SYSTEM_PROMPT, mcp_config=mcp_config, model=args.model, max_turns=args.max_turns, cwd=DEMO_DIR, on_raw=on_raw, stderr_path=run_dir / "claude_stderr.log", cli_path=cli_path)
            except Exception as e:
                subtype = (processor.result or {}).get("subtype", "")
                if subtype.startswith("error_max"):  # max turns / budget: a normal stop, not a crash
                    agent_error = subtype
                    console.warn(f"agent stopped early: {subtype} (raise --max-turns?)")
                else:
                    agent_error = f"{type(e).__name__}: {e}"
                    console.error(f"agent run failed: {agent_error}")
                    hint = _auth_hint(agent_error + " " + json.dumps(processor.result or {}))
                    if hint:
                        console.error(hint)
            finally:
                processor.close()
            wall_agent = time.time() - t_agent

            if processor.outcome is None:
                reason = agent_error or (processor.result or {}).get("subtype") or "agent ended"
                payload = {"status": "none", "summary": f"Agent did not call report_outcome ({reason}). Last words: {processor.last_text[:400]}"}
                await post_event(http, "outcome", payload, console)

            judge: dict | None = None
            try:
                # source=director lets sim_server tell the judge's call apart from the agent's
                # (extra query param; ignored by servers that do not look at it)
                judge = (await http.get("/check_task", params={"source": "director"})).json()
            except (httpx.HTTPError, ValueError) as e:
                console.error(f"GET /check_task failed: {e!r}")
            success = bool(judge and judge.get("success"))
            await post_event(http, "judge", {"success": success, "detail": judge or {}}, console)
            console.line("judge", "JUDGE", f"{'SUCCESS' if success else 'FAILED'}: {(judge or {}).get('reason', 'no check_task result')}")

            sim_time = None
            try:
                sim_time = (await http.get("/health")).json().get("sim_time")
            except (httpx.HTTPError, ValueError):
                pass
            await post_event(http, "run_end", {"success": success, "tool_calls": processor.tool_calls, "sim_time": sim_time, "wall_s": round(wall_agent, 2)}, console)
            await asyncio.sleep(0.5)
            stop.set()
            await poller

        result = processor.result or {}
        summary = build_summary(
            scenario=name,
            judge=judge,
            outcome=processor.outcome,
            tool_calls=processor.tool_calls,
            sim_time=sim_time,
            wall_s=wall_agent,
            model=args.model,
            extra={
                "run_id": run_id,
                "title": scenario.get("title"),
                "runner": args.runner,
                "judge_reason": (judge or {}).get("reason"),
                "agent_summary": (processor.outcome or {}).get("summary"),
                "tool_sequence": processor.tool_sequence,
                "images_delivered": processor.images_delivered,
                "tool_errors": processor.tool_errors,
                "agent_result": {k: result.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms", "total_cost_usd", "session_id")} if result else None,
                "agent_error": agent_error,
                "total_wall_s": round(time.time() - t_start, 2),
            },
        )
        write_summary(run_dir, summary)
        exit_code = 0 if success else 3
    except KeyboardInterrupt:
        console.warn("interrupted")
        exit_code = 130
    except Exception as e:
        console.error(f"{type(e).__name__}: {e}")
        exit_code = 1
    finally:
        stop_sim_server(proc)
        console.info("sim server stopped")

    if summary is not None and not args.no_video:
        if (PKG_DIR / "video.py").exists():
            console.info("rendering video ...")
            r = subprocess.run([python, "-m", "sabotage_demo.video", str(run_dir)], cwd=str(DEMO_DIR), env=child_env())
            if r.returncode != 0:
                console.warn(f"video.py exited with code {r.returncode}")
        else:
            console.warn("sabotage_demo/video.py not found; skipping video")

    if summary is not None:
        console.info(f"summary: success={summary['success']} agent_claimed={summary['agent_claimed']} tool_calls={summary['tool_calls']} sim_time={summary['sim_time']} wall_s={summary['wall_s']}")
        console.info(f"outputs: {run_dir}")
    return exit_code


def _auth_hint(text: str) -> str | None:
    t = text.lower()
    if any(s in t for s in ("/login", "not logged in", "invalid api key", "authentication", "oauth", "401")):
        return "Claude Code does not seem to be logged in: run `claude` once interactively and use /login (subscription), then retry."
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m sabotage_demo.director", description="Run a sabotage scenario end to end with Claude Code as the operator.")
    p.add_argument("--scenario", required=True, help="scenario JSON, e.g. scenarios/frozen_joint.json")
    p.add_argument("--model", default="opus", help="Claude Code model alias or id (default opus)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--runs-dir", default=None, help="parent of run directories (default demo/runs)")
    p.add_argument("--max-turns", type=int, default=60)
    p.add_argument("--sim-server-cmd", default=None, help="command template with {python} {scenario} {run_dir} {port}")
    p.add_argument("--fake-sim", action="store_true", help="use the test-only fake_sim_server instead of sim_server")
    p.add_argument("--runner", choices=["sdk", "cli"], default="sdk", help="claude-agent-sdk (default) or raw `claude -p` subprocess")
    p.add_argument("--claude-cli", default=None, help="path to the claude executable (default: from PATH)")
    p.add_argument("--health-timeout", type=float, default=120.0)
    p.add_argument("--no-video", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--quiet", action="store_true", help="hide tool results and thinking in the console")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = parse_args(argv)
    # Subscription auth only: never let a stray API key redirect billing, and
    # allow launching from inside another Claude Code session.
    if os.environ.pop("ANTHROPIC_API_KEY", None):
        print("[director] ignoring ANTHROPIC_API_KEY: the agent uses the Claude Code subscription login", file=sys.stderr)
    os.environ.pop("CLAUDECODE", None)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
