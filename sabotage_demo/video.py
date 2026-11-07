"""Compose the sabotage-demo MP4 (and an optional teaser GIF) from run dirs (builder C).

    python -m sabotage_demo.video <run_dir> [--out video.mp4] [--gif [teaser.gif]]
    python -m sabotage_demo.video --compile <run_dir>... --out final.mp4 --title "..."

Output: 1280x720, 30 fps, H.264 yuv420p (imageio-ffmpeg). Layout, per
CONTRACT.md "Video":

    y 0..60     title bar: scenario title + task, sim time, tool-call count
    x 0..853    sim frame (960x720 scaled to 853x640 at y=60), 20 px progress
                strip under it (sabotage ticks red, judge tick green/red)
    x 853..1280 agent trace panel (monospace, newest at bottom, auto-scroll)

Timeline rule (contract): recorded frames between a tool_call's frame_index and
its tool_result's frame_index play at real time (20 recorded fps shown at 30 fps
by duplication). An event that brings no new frames holds the last frame for
``min(3.0, 0.8 + 0.03 * len(text))`` s (``text`` = its trace line) while the
trace reveals it. Sabotage: red flashing banner for 2 s. Judge: stamp
"TASK VERIFIED" (green) or "TASK FAILED" (red). ``--compile`` adds an opening
title card, a 2 s chapter card per run and an end card with per-chapter
outcomes.

Speed: recorded frames are decoded and resized on a thread pool ahead of use,
the trace panel and title bar are re-rendered only when their content changes,
and overlays are pre-multiplied sprites blended with numpy.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- layout
OUT_W, OUT_H, FPS, REC_FPS = 1280, 720, 30, 20
TITLE_H = 60
SIM_X, SIM_Y, SIM_W, SIM_H = 0, 60, 853, 640
PANEL_X, PANEL_Y, PANEL_W, PANEL_H = 853, 60, 427, 660
STRIP_Y, STRIP_H = 700, 20

BANNER_S = 2.0
CARD_S = 2.0
TITLE_CARD_S = 2.5
END_CARD_S = 4.0
TAIL_S = 1.5
FADE_FRAMES = 8
REVEAL_FRAC = 0.6          # typewriter finishes after this share of the hold
GIF_W, GIF_FPS, GIF_MAX_S, GIF_LEAD_S, GIF_TAIL_S = 480, 12, 25.0, 3.0, 1.2
GIF_HOLD_SCALE = 0.5       # teaser only: shorter text holds so the story fits
MIN_TEXT_HOLD_S = 1.2      # integration: narrative entries (agent text, claim, sabotage, judge) never hold less
TEXT_KINDS = {"agent_text", "agent_thinking", "outcome", "sabotage", "judge"}
DEFAULT_SUBTITLE = "Claude Code operates a Franka Panda in MuJoCo over MCP while faults are injected"

MONO_SIZE, LINE_H = 14, 19
PANEL_HEADER_H = 38
PANEL_PAD_L, PANEL_PAD_R, PANEL_PAD_B = 18, 12, 10

# --------------------------------------------------------------------------- colours
BG = (13, 17, 23)
TITLE_BG = (9, 12, 17)
BORDER = (40, 46, 54)
DIM = (125, 133, 144)
TEXT = (201, 209, 217)
BRIGHT = (240, 246, 252)
AGENT = (236, 242, 248)
AGENT_BAR = (88, 166, 255)
TOOL = (227, 179, 65)
ARGS = (121, 192, 255)
RESULT = (110, 118, 129)
ERROR = (240, 136, 62)
SABOTAGE = (255, 95, 95)
OK_GREEN = (63, 185, 80)
FAIL_RED = (248, 81, 73)
CLAIM = (210, 168, 255)
THINK = (139, 148, 158)
ACCENT = (88, 166, 255)

# --------------------------------------------------------------------------- fonts
_FONT_DIRS = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
              Path("/usr/share/fonts/truetype/dejavu"), Path("/Library/Fonts")]
_FONT_CANDIDATES = {
    "mono": ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"],
    "mono_bold": ["consolab.ttf", "courbd.ttf", "DejaVuSansMono-Bold.ttf", "consola.ttf", "cour.ttf"],
    "mono_italic": ["consolai.ttf", "couri.ttf", "DejaVuSansMono-Oblique.ttf", "consola.ttf", "cour.ttf"],
    "ui": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
    "ui_semibold": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "ui_bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "ui_black": ["seguibl.ttf", "segoeuib.ttf", "ariblk.ttf", "DejaVuSans-Bold.ttf"],
}
_font_cache: dict[tuple[str, int], ImageFont.ImageFont] = {}


def font(kind: str, size: int):
    """Load a system font by role, falling back down the list, then PIL's default."""
    key = (kind, size)
    if key not in _font_cache:
        f = None
        for name in _FONT_CANDIDATES[kind]:
            for d in _FONT_DIRS:
                p = d / name
                if p.exists():
                    try:
                        f = ImageFont.truetype(str(p), size)
                        break
                    except OSError:
                        continue
            if f is not None:
                break
        _font_cache[key] = f if f is not None else ImageFont.load_default(size=size)
    return _font_cache[key]


# --------------------------------------------------------------------------- trace text
# The dashboard (dashboard/index.html) mirrors these rules in JS; keep them in sync.
_TOOL_PREFIX = re.compile(r"^(mcp__[a-z0-9_-]+__|/)")


def short_tool(name) -> str:
    return _TOOL_PREFIX.sub("", str(name or "?"))


def fmt_num(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = f"{float(v):.3f}".rstrip("0")
    if s.endswith("."):
        s += "0"
    return "0.0" if s == "-0.0" else s


def fmt_val(v, max_str: int = 48) -> str:
    if v is None:
        return "null"
    if isinstance(v, (bool, int, float)):
        return fmt_num(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(fmt_val(x, max_str) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{fmt_val(x, max_str)}" for k, x in v.items()) + "}"
    s = str(v).replace("\n", " ")
    if len(s) > max_str:
        s = s[: max_str - 1] + "…"
    return f'"{s}"' if (" " in s or not s) else s


def format_tool_call(tool, args) -> str:
    parts = []
    if isinstance(args, dict):
        for k, v in args.items():
            if v is None or v == [] or v == {}:
                continue
            parts.append(f"{k}={fmt_val(v)}")
    elif args not in (None, {}, []):
        parts.append(fmt_val(args))
    return "> " + short_tool(tool) + ("" if not parts else " " + " ".join(parts))


def _m(v) -> str:
    try:
        return f"{float(v):.3f}m"
    except (TypeError, ValueError):
        return str(v)


def format_tool_result(tool, result) -> tuple[str, bool]:
    """One-liner with the key fields. Returns (text, is_error)."""
    t = short_tool(tool)
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            return "← " + result.replace("\n", " ")[:120], False
    if not isinstance(result, dict):
        return "← " + fmt_val(result), False
    if "error" in result and result["error"]:
        return f"← error: {str(result['error']).strip()}", True
    if result.get("image") is True:
        cam = result.get("camera")
        return "← image" + (f" camera={cam}" if cam else ""), False
    p: list[str] = []
    if "reached" in result:
        p.append("reached" if result["reached"] else "NOT reached")
        if result.get("final_error_m") is not None:
            p.append(f"err={_m(result['final_error_m'])}")
        if not result["reached"] and result.get("note"):
            p.append(str(result["note"]))
    if "opening_m" in result:
        p.append(f"opening={_m(result['opening_m'])}")
    if "success" in result:
        p.append(f"success={fmt_num(bool(result['success']))}")
        if result.get("xy_distance_m") is not None:
            p.append(f"dist={_m(result['xy_distance_m'])}")
        if not result["success"] and result.get("reason"):
            p.append(str(result["reason"]))
    if "found" in result:
        p.append(f"found={fmt_num(bool(result['found']))}")
        if result.get("pos") is not None:
            p.append(f"pos={fmt_val(result['pos'])}")
        if result.get("confidence") is not None:
            p.append(f"conf={float(result['confidence']):.2f}")
    if isinstance(result.get("tcp"), dict) and "objects" in result:   # get_state
        p.append(f"tcp={fmt_val(result['tcp'].get('pos'))}")
        g = result.get("gripper") or {}
        if "holding" in g:
            p.append(f"holding={fmt_num(bool(g['holding']))}")
        cube = (result.get("objects") or {}).get("cube")
        p.append(f"cube={fmt_val(cube.get('pos'))}" if isinstance(cube, dict) else "cube=null (tracker dead)")
    elif "holding" in result:
        p.append(f"holding={fmt_num(bool(result['holding']))}")
    if "summary" in result and isinstance(result["summary"], str):        # diagnose
        p.append(result["summary"])
    if not p and isinstance(result.get("text"), str):                     # describe_scene
        p.append(f"scene description ({len(result['text'])} chars)")
    if not p and "ok" in result:
        p.append("ok" if result["ok"] else "not ok")
    if not p and "sim_time" in result:
        p.append(f"t={float(result['sim_time']):.2f}s")
    if not p:
        for k, v in result.items():
            if isinstance(v, (bool, int, float, str)) and len(p) < 3:
                p.append(f"{k}={fmt_val(v, 32)}")
    if t == "wait" and "sim_time" in result and not any(x.startswith("t=") for x in p):
        p.append(f"t={float(result['sim_time']):.2f}s")
    return "← " + " ".join(p), False


def sabotage_label(payload: dict) -> str:
    if payload.get("label"):
        return str(payload["label"])
    spec = payload.get("spec") or {}
    kind = spec.get("kind")
    if kind == "freeze_joint":
        return f"Joint {int(spec.get('joint', 0)) + 1} frozen"
    if kind == "cut_object_sensor":
        return "Object tracker cut"
    if kind == "teleport_cube":
        if spec.get("delta") is not None:
            d = np.array(spec["delta"], float)
            return f"Cube teleported {np.linalg.norm(d) * 100:.0f} cm"
        return "Cube teleported"
    if kind == "clear":
        return "Faults cleared"
    return str(kind or "sabotage")


PERSISTENT_FAULTS = {"freeze_joint", "cut_object_sensor"}


def _clean_agent_text(s: str, limit: int) -> str:
    s = s.replace("\r", "").replace("**", "").replace("`", "")
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


@dataclass
class Entry:
    kind: str
    text: str
    prefix: int            # first `prefix` chars use the prefix style
    prefix_color: tuple
    body_color: tuple
    prefix_font: str = "mono_bold"
    body_font: str = "mono"
    bar: tuple | None = None
    indent: int = 2        # hanging indent of wrapped lines, in chars
    gap: int = 7           # px above the entry
    typewriter: bool = False
    lines: list = field(default_factory=list)   # (start, end, indent) after wrap


_STOP_REASONS = {"error_max_turns": "turn limit reached", "error_max_budget_usd": "budget limit reached",
                 "error_during_execution": "agent error", "success": "agent ended"}


def no_claim_text(summary: str) -> str:
    """Concise line for the director's status-none outcome (integration: drop the 'last words' dump)."""
    m = re.search(r"\(([^)]*)\)", summary or "")
    reason = (m.group(1).strip() if m else "").split(":")[0].strip()
    reason = _STOP_REASONS.get(reason, reason)
    return "NO CLAIM agent stopped without report_outcome" + (f" ({reason})" if reason else "")


def result_is_alarm(result) -> bool:
    """Integration: results that signal a problem (move missed, gripper closed on nothing,
    diagnose found anomalies, task check failed) are drawn in the alert colour."""
    if not isinstance(result, dict):
        return False
    if result.get("reached") is False:
        return True
    if result.get("holding") is False and isinstance(result.get("opening_m"), (int, float)) and result["opening_m"] < 0.01:
        return True
    summ = result.get("summary")
    if isinstance(summ, str) and "joints" in result and summ.strip() and summ.strip() != "no anomalies detected":
        return True
    return result.get("success") is False


def trace_entry(ev: dict) -> Entry | None:
    """Map one contract event to a trace-panel entry (None = not shown)."""
    typ = ev.get("type")
    p = ev.get("payload") or {}
    if typ == "run_start":
        task = str(p.get("task") or "")
        txt = "TASK " + task
        return Entry(typ, txt, 4, DIM, TEXT, indent=5, gap=0)
    if typ == "agent_text":
        txt = _clean_agent_text(str(p.get("text") or ""), 520)
        if not txt:
            return None
        return Entry(typ, txt, 0, AGENT, AGENT, body_font="mono", bar=AGENT_BAR, indent=0, gap=12, typewriter=True)
    if typ == "agent_thinking":
        txt = _clean_agent_text(str(p.get("text") or ""), 280)
        if not txt:
            return None
        return Entry(typ, txt, 0, THINK, THINK, body_font="mono_italic", bar=(60, 66, 76), indent=0, gap=10,
                     typewriter=True)
    if typ == "tool_call":
        txt = format_tool_call(p.get("tool"), p.get("args"))
        return Entry(typ, txt, 2 + len(short_tool(p.get("tool"))), TOOL, ARGS, indent=4, gap=9)
    if typ == "tool_result":
        txt, err = format_tool_result(p.get("tool"), p.get("result"))
        alarm = err or result_is_alarm(p.get("result"))
        return Entry(typ, txt, 0, RESULT, ERROR if alarm else RESULT, body_font="mono", indent=4, gap=2)
    if typ == "sabotage":
        spec = p.get("spec") or {}
        if spec.get("kind") == "clear":
            return Entry(typ, "!! faults cleared", 2, SABOTAGE, SABOTAGE, indent=3, gap=10)
        head = "!! SABOTAGE (hidden from agent) "
        txt = head + sabotage_label(p)
        return Entry(typ, txt, len(head) - 1, SABOTAGE, SABOTAGE, body_font="mono_bold", indent=3, gap=12)
    if typ == "outcome":
        status = str(p.get("status") or "?")
        summ = str(p.get("summary") or "").strip()
        if status == "none":   # director: agent never called report_outcome
            return Entry(typ, no_claim_text(summ), 8, CLAIM, DIM, indent=2, gap=12, typewriter=True)
        txt = f"AGENT CLAIMS {status}" + (f": {summ}" if summ else "")
        return Entry(typ, _clean_agent_text(txt, 300), 12, CLAIM, CLAIM, indent=2, gap=12, typewriter=True)
    if typ == "judge":
        ok = bool(p.get("success"))
        det = p.get("detail") or {}
        extra = ""
        if det.get("xy_distance_m") is not None:
            extra = f" · cube {float(det['xy_distance_m']):.3f} m from zone centre"
        if not ok and det.get("reason"):
            extra += f" · {det['reason']}"
        txt = ("JUDGE TASK VERIFIED" if ok else "JUDGE TASK FAILED") + extra
        return Entry(typ, txt, 5, OK_GREEN if ok else FAIL_RED, OK_GREEN if ok else FAIL_RED,
                     body_font="mono_bold", indent=2, gap=12)
    if typ == "run_end":
        parts = ["run end"]
        if p.get("tool_calls") is not None:
            parts.append(f"{p['tool_calls']} tool calls")
        if p.get("sim_time") is not None:
            parts.append(f"sim {float(p['sim_time']):.1f} s")
        if p.get("wall_s") is not None:
            parts.append(f"wall {float(p['wall_s']):.0f} s")
        return Entry(typ, " · ".join(parts), 0, DIM, DIM, indent=2, gap=10)
    return None


def trace_text(ev: dict) -> str:
    """The trace line for an event ('' if not shown). Its length sets the hold time."""
    e = trace_entry(ev)
    return e.text if e else ""


def hold_seconds(text: str, scale: float = 1.0) -> float:
    return scale * min(3.0, 0.8 + 0.03 * len(text))


def wrap(text: str, cols: int, indent: int) -> list[tuple[int, int, int]]:
    """Word-wrap ``text`` into (start, end, indent) char ranges; '\\n' is a hard break."""
    lines: list[tuple[int, int, int]] = []
    pos = 0
    n = len(text)
    first = True
    while True:
        nl = text.find("\n", pos)
        par_end = n if nl < 0 else nl
        if pos == par_end:
            lines.append((pos, pos, 0 if first else indent))
            first = False
        while pos < par_end:
            ind = 0 if first else indent
            width = max(8, cols - ind)
            if par_end - pos <= width:
                lines.append((pos, par_end, ind))
                pos = par_end
            else:
                cut = text.rfind(" ", pos, pos + width + 1)
                if cut <= pos:
                    cut, nxt = pos + width, pos + width
                else:
                    nxt = cut + 1
                lines.append((pos, cut, ind))
                pos = nxt
                while pos < par_end and text[pos] == " ":
                    pos += 1
            first = False
        if nl < 0:
            break
        pos = nl + 1
    return lines


# --------------------------------------------------------------------------- run data
@dataclass
class Run:
    dir: Path
    events: list[dict]
    frame_times: list[float]
    frame_files: list[Path | None]
    scenario: dict
    summary: dict

    @property
    def n_frames(self) -> int:
        return len(self.frame_files)

    def _run_start(self) -> dict:
        for e in self.events:
            if e.get("type") == "run_start":
                return e.get("payload") or {}
        return {}

    @property
    def title(self) -> str:
        return str(self.scenario.get("title") or self._run_start().get("title") or self.scenario.get("name")
                   or self.dir.name)

    @property
    def task(self) -> str:
        return str(self.scenario.get("task") or self._run_start().get("task") or "")

    @property
    def model(self) -> str:
        return str(self.summary.get("model") or "")


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue   # tolerate a torn last line from a crashed run
    return out


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_run(run_dir: str | Path) -> Run:
    d = Path(run_dir).resolve()
    if not (d / "events.jsonl").exists():
        raise FileNotFoundError(f"{d} has no events.jsonl")
    events = _read_jsonl(d / "events.jsonl")
    if all("seq" in e for e in events):
        events.sort(key=lambda e: e["seq"])
    times: dict[int, float] = {}
    for fr in _read_jsonl(d / "frames.jsonl"):
        if "index" in fr:
            times[int(fr["index"])] = float(fr.get("sim_time") or 0.0)
    files: dict[int, Path] = {}
    fdir = d / "frames"
    if fdir.is_dir():
        for p in fdir.glob("*.jpg"):
            if p.stem.isdigit():
                files[int(p.stem)] = p
    n = max([-1, *times.keys(), *files.keys()]) + 1
    ft: list[float] = []
    for i in range(n):
        ft.append(times[i] if i in times else (ft[-1] + 1.0 / REC_FPS if ft else 0.0))
    return Run(d, events, ft, [files.get(i) for i in range(n)], _read_json(d / "scenario.json"),
               _read_json(d / "summary.json"))


# --------------------------------------------------------------------------- run timeline
@dataclass(slots=True)
class Spec:
    frame: int          # recorded frame index (-1 = none)
    n_entries: int      # trace entries visible
    reveal: int         # chars of the newest entry revealed (-1 = all)
    sim_time: float
    tools: int
    banner: int         # index into plan.banners (-1 = none)
    banner_age: int
    faults: tuple
    stamp_age: int      # frames since judge (-1 = none)


@dataclass
class RunPlan:
    run: Run
    entries: list[Entry]
    specs: list[Spec]
    banners: list[str]
    verdict: bool | None
    markers: list[tuple[int, str]]      # (frame k, "sabotage"|"judge_ok"|"judge_fail")
    first_sabotage_k: int | None
    judge_k: int | None
    tool_calls: int
    fault_labels: list[str]

    @property
    def n(self) -> int:
        return len(self.specs)


def _anchors(run: Run) -> list[int]:
    """Frame position of every event: explicit frame_index for tool events, else by sim_time."""
    n = run.n_frames
    last = max(0, n - 1)
    explicit: list[int | None] = []
    for e in run.events:
        fi = (e.get("payload") or {}).get("frame_index")
        explicit.append(min(max(int(fi), 0), last) if isinstance(fi, (int, float)) and n else None)
    # next explicit anchor after each event caps how far a sim_time lookup may scan
    nxt_cap = [last] * len(run.events)
    cap = last
    for i in range(len(run.events) - 1, -1, -1):
        nxt_cap[i] = cap
        if explicit[i] is not None:
            cap = explicit[i]
    out: list[int] = []
    pos = 0
    for i, e in enumerate(run.events):
        if explicit[i] is not None:
            p = explicit[i]
        else:
            p = pos
            t = e.get("sim_time")
            if isinstance(t, (int, float)) and n:
                while p + 1 <= nxt_cap[i] and run.frame_times[p + 1] <= float(t) + 1e-6:
                    p += 1
        pos = max(pos, p)
        out.append(pos)
    return out


def build_run_plan(run: Run, hold_scale: float = 1.0, cols: int | None = None) -> RunPlan:
    cols = cols or panel_cols()
    specs: list[Spec] = []
    entries: list[Entry] = []
    banners: list[str] = []
    markers: list[tuple[int, str]] = []
    faults: list[str] = []
    st = {"shown": 0, "sim": run.frame_times[0] if run.n_frames else 0.0, "tools": 0, "acc": 0.0,
          "banner": -1, "banner_k": 0, "stamp_k": None}
    verdict: bool | None = None
    first_sab = judge_k = None

    def emit(reveal: int = -1) -> None:
        k = len(specs)
        b = st["banner"]
        age = k - st["banner_k"]
        if b >= 0 and age >= BANNER_S * FPS:
            b = -1
        sk = st["stamp_k"]
        specs.append(Spec(st["shown"] if run.n_frames else -1, len(entries), reveal, st["sim"], st["tools"],
                          b, age if b >= 0 else 0, tuple(faults), -1 if sk is None else k - sk))

    def play_to(pos: int) -> None:
        n_new = pos - st["shown"]
        if n_new <= 0:
            return
        exact = n_new * FPS / REC_FPS + st["acc"]
        out_n = int(exact)
        st["acc"] = exact - out_n
        base = st["shown"]
        for j in range(out_n):
            st["shown"] = base + 1 + min(n_new - 1, (j * n_new) // out_n)
            st["sim"] = run.frame_times[st["shown"]]
            emit()
        st["shown"] = pos
        st["sim"] = run.frame_times[pos]

    anchors = _anchors(run)
    events = run.events
    for i, ev in enumerate(events):
        pos = anchors[i]
        play_to(pos)
        typ = ev.get("type")
        p = ev.get("payload") or {}
        if typ == "tool_call":
            st["tools"] += 1
        elif typ == "sabotage":
            kind = (p.get("spec") or {}).get("kind")
            if kind == "clear":
                faults.clear()
            else:
                label = sabotage_label(p)
                banners.append(label)
                st["banner"], st["banner_k"] = len(banners) - 1, len(specs)
                markers.append((len(specs), "sabotage"))
                if first_sab is None:
                    first_sab = len(specs)
                if kind in PERSISTENT_FAULTS and label not in faults:
                    faults.append(label)
        elif typ == "judge":
            verdict = bool(p.get("success"))
            st["stamp_k"] = judge_k = len(specs)
            markers.append((len(specs), "judge_ok" if verdict else "judge_fail"))
        entry = trace_entry(ev)
        if entry is not None:
            entry.lines = wrap(entry.text, cols, entry.indent)
            entries.append(entry)
            new_frames_follow = i + 1 < len(events) and anchors[i + 1] > pos
            if not new_frames_follow:
                hold = hold_seconds(entry.text, hold_scale)
                if entry.kind in TEXT_KINDS:
                    hold = max(hold, MIN_TEXT_HOLD_S)
                n_hold = max(1, int(round(hold * FPS)))
                n_rev = max(1, int(n_hold * REVEAL_FRAC)) if entry.typewriter else 0
                L = len(entry.text)
                for j in range(n_hold):
                    emit(-1 if j >= n_rev - 1 else max(1, math.ceil(L * (j + 1) / n_rev)))
    if run.n_frames:
        play_to(run.n_frames - 1)
    if verdict is None and isinstance(run.summary.get("success"), bool):
        verdict = bool(run.summary["success"])
        st["stamp_k"] = judge_k = len(specs)
        markers.append((len(specs), "judge_ok" if verdict else "judge_fail"))
    for _ in range(int(round(TAIL_S * FPS))):
        emit()
    tool_calls = st["tools"]
    if isinstance(run.summary.get("tool_calls"), int):
        tool_calls = run.summary["tool_calls"]
    return RunPlan(run, entries, specs, banners, verdict, markers, first_sab, judge_k, tool_calls,
                   [b for b in banners])


# --------------------------------------------------------------------------- drawing helpers
class Sprite:
    """Pre-multiplied RGBA overlay that blends onto a uint8 RGB view with numpy."""

    def __init__(self, img: Image.Image) -> None:
        a = np.asarray(img.convert("RGBA"), dtype=np.float32) / 255.0
        alpha = a[..., 3:4]
        self.pm = a[..., :3] * alpha * 255.0
        self.inv = 1.0 - alpha
        self.h, self.w = a.shape[:2]

    def blit(self, dst: np.ndarray, x: int, y: int) -> None:
        H, W = dst.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + self.w), min(H, y + self.h)
        if x1 <= x0 or y1 <= y0:
            return
        sx, sy = x0 - x, y0 - y
        reg = dst[y0:y1, x0:x1]
        out = reg * self.inv[sy:sy + y1 - y0, sx:sx + x1 - x0] + self.pm[sy:sy + y1 - y0, sx:sx + x1 - x0]
        reg[...] = np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _text_w(f, s: str) -> float:
    return f.getlength(s)


def _spaced(draw: ImageDraw.ImageDraw, xy, s: str, f, fill, spacing: float) -> float:
    """Draw letter-spaced text; returns its width."""
    x, y = xy
    for ch in s:
        draw.text((x, y), ch, font=f, fill=fill)
        x += f.getlength(ch) + spacing
    return x - xy[0] - spacing


def _spaced_w(f, s: str, spacing: float) -> float:
    return sum(f.getlength(ch) for ch in s) + spacing * max(0, len(s) - 1)


def _ellipsize(f, s: str, max_w: float) -> str:
    if _text_w(f, s) <= max_w:
        return s
    while s and _text_w(f, s + "…") > max_w:
        s = s[:-1]
    return s.rstrip() + "…"


def _wrap_px(f, s: str, max_w: float) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if _text_w(f, t) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _gradient(w: int, h: int, top, bottom, vignette: float = 0.0) -> np.ndarray:
    g = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None, None]
    arr = np.array(top, np.float32) * (1 - g) + np.array(bottom, np.float32) * g
    arr = np.broadcast_to(arr, (h, w, 3)).copy()
    if vignette:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        arr *= (1.0 - vignette * np.clip(r - 0.4, 0, 1))[..., None]
    return np.clip(arr, 0, 255).astype(np.uint8)


def _pill(text: str, f, fg, bg, pad_x=12, pad_y=5, radius=None, outline=None) -> Image.Image:
    tw = math.ceil(_text_w(f, text))
    asc, desc = f.getmetrics()
    h = asc + desc + 2 * pad_y
    img = Image.new("RGBA", (tw + 2 * pad_x, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, img.width - 1, h - 1], radius=radius if radius is not None else h // 2, fill=bg,
                        outline=outline, width=2 if outline else 0)
    d.text((pad_x, pad_y), text, font=f, fill=fg)
    return img


def panel_cols() -> int:
    cw = font("mono", MONO_SIZE).getlength("M")
    return int((PANEL_W - PANEL_PAD_L - PANEL_PAD_R) // cw)


def _fade(arr: np.ndarray, f: float) -> np.ndarray:
    if f >= 1.0:
        return arr
    return ((arr.astype(np.uint16) * int(max(0.0, f) * 256)) >> 8).astype(np.uint8)


class _LRU(OrderedDict):
    def __init__(self, cap: int) -> None:
        super().__init__()
        self.cap = cap

    def get_or(self, key, make):
        if key in self:
            self.move_to_end(key)
            return self[key]
        v = make()
        self[key] = v
        if len(self) > self.cap:
            self.popitem(last=False)
        return v


# --------------------------------------------------------------------------- per-run renderers
class FrameLoader:
    """Decode + resize recorded JPEGs on a thread pool, in the order they will be used."""

    def __init__(self, run: Run, order: list[int], pool: ThreadPoolExecutor, lookahead: int = 32) -> None:
        self.run, self.pool, self.lookahead = run, pool, lookahead
        self.order = [k for i, k in enumerate(order) if i == 0 or order[i - 1] != k]
        self.i = 0
        self.pending: deque = deque()
        self.cur: tuple[int | None, np.ndarray | None] = (None, None)
        self.black = np.zeros((SIM_H, SIM_W, 3), np.uint8)

    def _path(self, idx: int) -> Path | None:
        files = self.run.frame_files
        for j in range(min(idx, len(files) - 1), -1, -1):
            if files[j] is not None:
                return files[j]
        return None

    def _load(self, idx: int) -> np.ndarray:
        p = self._path(idx) if idx >= 0 else None
        if p is None:
            return self.black
        try:
            with Image.open(p) as im:
                im = im.convert("RGB")
                if im.size != (SIM_W, SIM_H):
                    im = im.resize((SIM_W, SIM_H), Image.Resampling.LANCZOS)
                return np.asarray(im)
        except OSError:
            return self.black

    def _fill(self) -> None:
        while len(self.pending) < self.lookahead and self.i < len(self.order):
            k = self.order[self.i]
            self.pending.append((k, self.pool.submit(self._load, k)))
            self.i += 1

    def get(self, idx: int) -> np.ndarray:
        if self.cur[0] == idx:
            return self.cur[1]
        self._fill()
        while self.pending:
            k, fut = self.pending.popleft()
            self._fill()
            if k == idx:
                self.cur = (idx, fut.result())
                return self.cur[1]
        self.cur = (idx, self._load(idx))
        return self.cur[1]

    def close(self) -> None:
        for _, fut in self.pending:
            fut.cancel()
        self.pending.clear()


class TracePanel:
    def __init__(self, plan: RunPlan) -> None:
        self.entries = plan.entries
        self.mono = {k: font(k, MONO_SIZE) for k in ("mono", "mono_bold", "mono_italic")}
        self.cw = self.mono["mono"].getlength("M")
        base = Image.new("RGB", (PANEL_W, PANEL_H), BG)
        d = ImageDraw.Draw(base)
        d.line([(0, 0), (0, PANEL_H)], fill=BORDER, width=1)
        hf = font("ui_semibold", 12)
        _spaced(d, (PANEL_PAD_L, 12), "AGENT TRACE", hf, DIM, 1.6)
        model = plan.run.model
        if model:
            mf = font("mono", 12)
            model = _ellipsize(mf, model, PANEL_W - 170)
            d.text((PANEL_W - PANEL_PAD_R - _text_w(mf, model), 13), model, font=mf, fill=(90, 98, 110))
        d.line([(PANEL_PAD_L, PANEL_HEADER_H - 1), (PANEL_W - PANEL_PAD_R, PANEL_HEADER_H - 1)], fill=BORDER)
        self.base = base
        top = PANEL_HEADER_H
        self.fade_rows = 34
        a = np.linspace(1.0, 0.0, self.fade_rows, dtype=np.float32)[:, None, None]
        self.fade_a = a
        self.fade_top = top
        self.base_arr = np.array(base)
        self.lines: dict[tuple[int, int, int], np.ndarray] = {}
        self.cache = _LRU(6)

    def get(self, n: int, reveal: int) -> np.ndarray:
        return self.cache.get_or((n, reveal), lambda: self._render(n, reveal))

    def _line_img(self, e: Entry, s: int, t: int) -> np.ndarray:
        """Rasterise text[s:t] of an entry onto the panel background (one LINE_H row)."""
        cw = self.cw
        w = max(1, int(math.ceil((t - s) * cw)) + 2)
        img = Image.new("RGB", (w, LINE_H), BG)
        d = ImageDraw.Draw(img)
        x = 0.0
        if s < e.prefix:
            seg = e.text[s:min(t, e.prefix)]
            if seg:
                d.text((x, 0), seg, font=self.mono[e.prefix_font], fill=e.prefix_color)
                x += len(seg) * cw
        seg = e.text[max(s, e.prefix):t]
        if seg:
            d.text((x, 0), seg, font=self.mono[e.body_font], fill=e.body_color)
        return np.asarray(img)

    def _line(self, ei: int, s: int, t: int) -> np.ndarray:
        key = (ei, s, t)
        img = self.lines.get(key)
        if img is None:
            img = self._line_img(self.entries[ei], s, t)
            if t == self.entries[ei].lines[-1][1] or any(ln[1] == t for ln in self.entries[ei].lines):
                self.lines[key] = img      # only complete lines are worth caching
        return img

    def _render(self, n: int, reveal: int) -> np.ndarray:
        arr = self.base_arr.copy()
        top = PANEL_HEADER_H + 4
        y = PANEL_H - PANEL_PAD_B
        for ei in range(n - 1, -1, -1):
            e = self.entries[ei]
            lines = e.lines
            r = reveal if (ei == n - 1 and reveal >= 0) else None
            if r is not None:
                lines = [ln for li, ln in enumerate(lines) if li == 0 or ln[0] < r]
            h = len(lines) * LINE_H
            y0 = y - h
            for li, (s, t, ind) in enumerate(lines):
                ly = y0 + li * LINE_H
                if ly + LINE_H <= PANEL_HEADER_H:
                    continue
                ve = t if r is None else min(t, r)
                if ve <= s:
                    continue
                img = self._line(ei, s, ve)
                x = int(PANEL_PAD_L + ind * self.cw)
                cy0 = max(ly, PANEL_HEADER_H)
                w = min(img.shape[1], PANEL_W - x)
                arr[cy0:ly + LINE_H, x:x + w] = img[cy0 - ly:, :w]
            if e.bar is not None:
                b0, b1 = max(PANEL_HEADER_H, y0 + 2), y0 + h - 3
                if b1 > b0:
                    arr[b0:b1, 7:10] = e.bar
            y = y0 - e.gap
            if y < top - LINE_H:
                break
        ft = self.fade_top
        band = arr[ft:ft + self.fade_rows, 1:].astype(np.float32)
        band = band * (1 - self.fade_a) + np.array(BG, np.float32) * self.fade_a
        arr[ft:ft + self.fade_rows, 1:] = band.astype(np.uint8)
        arr[:PANEL_HEADER_H] = self.base_arr[:PANEL_HEADER_H]
        return arr


class TitleBar:
    def __init__(self, title: str, task: str) -> None:
        img = Image.new("RGB", (OUT_W, TITLE_H), TITLE_BG)
        d = ImageDraw.Draw(img)
        d.line([(0, TITLE_H - 1), (OUT_W, TITLE_H - 1)], fill=BORDER)
        d.rectangle([0, 0, 4, TITLE_H - 2], fill=ACCENT)
        tf, sf = font("ui_semibold", 23), font("ui", 13)
        max_w = OUT_W - 330 - 24
        d.text((22, 5), _ellipsize(tf, title, max_w), font=tf, fill=BRIGHT)
        if task:
            d.text((23, 36), _ellipsize(sf, task, max_w), font=sf, fill=DIM)
        self.lf, self.vf = font("ui_semibold", 10), font("mono_bold", 21)
        self.slots = []
        right = OUT_W - 24
        for label, w in (("TOOL CALLS", 96), ("SIM TIME", 140)):
            x = right - w
            _spaced(d, (x + w - _spaced_w(self.lf, label, 1.4), 9), label, self.lf, DIM, 1.4)
            self.slots.append((x, w))
            right = x - 26
        self.base = img
        self.cache = _LRU(4)

    def get(self, sim_time: float, tools: int) -> np.ndarray:
        key = (f"{sim_time:7.2f}", tools)
        return self.cache.get_or(key, lambda: self._render(*key))

    def _render(self, sim_s: str, tools: int) -> np.ndarray:
        img = self.base.copy()
        d = ImageDraw.Draw(img)
        for (x, w), val in zip(self.slots, (f"{tools:3d}", f"{sim_s} s")):
            d.text((x + w - _text_w(self.vf, val), 24), val, font=self.vf, fill=BRIGHT)
        return np.asarray(img)


class ProgressStrip:
    def __init__(self, plan: RunPlan) -> None:
        self.n = max(1, plan.n)
        self.base = np.empty((STRIP_H, SIM_W, 3), np.uint8)
        self.base[:] = TITLE_BG
        self.base[9:11, 12:SIM_W - 12] = (33, 38, 45)
        self.ticks = []
        for k, kind in plan.markers:
            x = 12 + int((SIM_W - 24) * k / self.n)
            col = SABOTAGE if kind == "sabotage" else (OK_GREEN if kind == "judge_ok" else FAIL_RED)
            self.ticks.append((x, col))

    def get(self, k: int) -> np.ndarray:
        s = self.base.copy()
        x = 12 + int((SIM_W - 24) * min(1.0, (k + 1) / self.n))
        s[9:11, 12:x] = ACCENT
        for tx, col in self.ticks:
            if tx <= x:
                s[5:15, max(0, tx - 1):tx + 2] = col
            else:
                s[7:13, max(0, tx - 1):tx + 2] = tuple(int(c * 0.55) for c in col)
        return s


class Overlays:
    """Banner, stamp and fault-chip sprites (cached across runs)."""

    def __init__(self) -> None:
        self.banners: dict[str, tuple[list[Sprite], Sprite, Sprite]] = {}
        self.stamps: dict[bool, list[Sprite]] = {}
        self.chips: dict[str, Sprite] = {}
        self.border_on = None

    def banner(self, label: str):
        if label not in self.banners:
            bright, dim = self._banner_img(label, 236), self._banner_img(label, 170)
            grow = []
            for frac in (0.25, 0.5, 0.75):
                h = max(2, int(bright.height * frac))
                top = (bright.height - h) // 2
                grow.append(Sprite(bright.crop((0, top, bright.width, top + h))))
            self.banners[label] = (grow, Sprite(bright), Sprite(dim))
        return self.banners[label]

    @staticmethod
    def _banner_img(label: str, alpha: int) -> Image.Image:
        bh = 136
        img = Image.new("RGBA", (SIM_W, bh), (196, 22, 36, alpha))
        d = ImageDraw.Draw(img)
        stripe = 12
        for x in range(-bh, SIM_W + bh, 28):
            for y0 in (0, bh - stripe):
                d.polygon([(x, y0), (x + 14, y0), (x + 14 + stripe, y0 + stripe), (x + stripe, y0 + stripe)],
                          fill=(20, 8, 10, min(255, alpha + 10)))
        bf, lf = font("ui_black", 50), font("ui_semibold", 25)
        w = _spaced_w(bf, "SABOTAGE", 8)
        _spaced(d, ((SIM_W - w) / 2, 18), "SABOTAGE", bf, (255, 255, 255, 255), 8)
        lab = _ellipsize(lf, label, SIM_W - 60)
        d.text(((SIM_W - _text_w(lf, lab)) / 2, 84), lab, font=lf, fill=(255, 236, 236, 255))
        return img

    def border(self) -> Sprite:
        if self.border_on is None:
            img = Image.new("RGBA", (SIM_W, SIM_H), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            for i in range(7):
                d.rectangle([i, i, SIM_W - 1 - i, SIM_H - 1 - i], outline=(230, 30, 45, 230 - i * 28))
            self.border_on = Sprite(img)
        return self.border_on

    def stamp(self, ok: bool) -> list[Sprite]:
        if ok not in self.stamps:
            text = "TASK VERIFIED" if ok else "TASK FAILED"
            col = (46, 204, 92) if ok else (240, 60, 60)
            f = font("ui_black", 56)
            tw = _spaced_w(f, text, 4)
            asc, desc = f.getmetrics()
            pad_x, pad_y = 34, 16
            w, h = int(tw + 2 * pad_x), int(asc + desc + 2 * pad_y)
            img = Image.new("RGBA", (w + 24, h + 24), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.rounded_rectangle([12, 12, 12 + w, 12 + h], radius=14, fill=(8, 12, 10, 150), outline=col + (255,),
                                width=7)
            d.rounded_rectangle([24, 24, w, h], radius=8, outline=col + (150,), width=2)
            _spaced(d, (12 + pad_x, 12 + pad_y - 4), text, f, col + (255,), 4)
            img = img.rotate(-6, resample=Image.Resampling.BICUBIC, expand=True)
            seq = []
            for scale, alpha in ((1.7, 0.25), (1.45, 0.5), (1.25, 0.75), (1.1, 0.95), (0.97, 1.0), (1.0, 1.0)):
                im = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.BICUBIC)
                if alpha < 1.0:
                    a = im.getchannel("A").point(lambda v, al=alpha: int(v * al))
                    im.putalpha(a)
                seq.append(Sprite(im))
            self.stamps[ok] = seq
        return self.stamps[ok]

    def chip(self, label: str) -> Sprite:
        if label not in self.chips:
            self.chips[label] = Sprite(_pill("FAULT  " + label, font("ui_semibold", 15), (255, 255, 255, 255),
                                             (190, 24, 40, 225)))
        return self.chips[label]


# --------------------------------------------------------------------------- cards
def _card_base() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.fromarray(_gradient(OUT_W, OUT_H, (20, 26, 36), (8, 10, 14), vignette=0.35))
    return img, ImageDraw.Draw(img)


def _centered(d, y, s, f, fill, spacing=0.0):
    w = _spaced_w(f, s, spacing) if spacing else _text_w(f, s)
    if spacing:
        _spaced(d, ((OUT_W - w) / 2, y), s, f, fill, spacing)
    else:
        d.text(((OUT_W - w) / 2, y), s, font=f, fill=fill)


def split_chapter_title(title: str, index: int) -> tuple[str, str]:
    m = re.match(r"^\s*(chapter\s+\w+)\s*[:\-–—]\s*(.+)$", title, re.I)
    if m:
        head = m.group(2).strip()
        return m.group(1).upper(), head[:1].upper() + head[1:]
    return f"CHAPTER {index}", title


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def describe_triggers(scenario: dict) -> str:
    out = []
    for trig in scenario.get("triggers") or []:
        sab, when = trig.get("sabotage") or {}, trig.get("when") or {}
        kind = sab.get("kind")
        if kind == "freeze_joint":
            what = f"joint {int(sab.get('joint', 0)) + 1} freezes"
        elif kind == "cut_object_sensor":
            what = "the object tracker dies"
        elif kind == "teleport_cube":
            if sab.get("delta") is not None:
                what = f"the cube jumps {np.linalg.norm(np.array(sab['delta'], float)) * 100:.0f} cm"
            else:
                what = "the cube is teleported"
        else:
            what = str(kind)
        wt = when.get("type")
        if wt == "tool_call":
            phase = "before" if when.get("phase", "before") == "before" else "after"
            cond = f"{phase} the {_ordinal(int(when.get('n', 1)))} {when.get('tool', 'tool')} call"
        elif wt == "ee_near_cube":
            cond = f"when the gripper comes within {float(when.get('dist', 0)) * 100:.0f} cm of the cube"
        elif wt == "cube_lifted":
            cond = f"when the cube is lifted above {float(when.get('z', 0)) * 100:.0f} cm"
        elif wt == "sim_time":
            cond = f"at t = {float(when.get('t', 0)):.1f} s"
        elif wt == "manual":
            cond = "from the dashboard"
        else:
            cond = ""
        out.append(f"{what} {cond}".strip())
    return "; ".join(out)


def make_title_card(title: str, subtitle: str, bylines: list[str] | None = None) -> np.ndarray:
    img, d = _card_base()
    tf = font("ui_bold", 60)
    lines = _wrap_px(tf, title, OUT_W - 200)[:3]
    y = OUT_H / 2 - (len(lines) * 74) / 2 - 30
    d.rectangle([OUT_W / 2 - 40, y - 34, OUT_W / 2 + 40, y - 30], fill=ACCENT)
    for ln in lines:
        _centered(d, y, ln, tf, BRIGHT)
        y += 74
    if subtitle:
        _centered(d, y + 20, subtitle, font("ui", 24), DIM)
    by = y + 96
    for i, line in enumerate(bylines or []):   # integration: author / context lines
        _centered(d, by, line, font("ui_semibold" if i == 0 else "ui", 22 if i == 0 else 19), TEXT if i == 0 else DIM)
        by += 34
    return np.asarray(img)


def make_chapter_card(index: int, run: Run) -> np.ndarray:
    img, d = _card_base()
    kicker, head = split_chapter_title(run.title, index)
    hf = font("ui_bold", 58)
    lines = _wrap_px(hf, head, OUT_W - 220)[:3]
    trig = describe_triggers(run.scenario)
    block = 40 + len(lines) * 72 + 40 + (44 if run.task else 0) + (40 if trig else 0)
    y = (OUT_H - block) / 2
    _centered(d, y, kicker, font("ui_semibold", 22), ACCENT, 5)
    y += 48
    for ln in lines:
        _centered(d, y, ln, hf, BRIGHT)
        y += 72
    d.rectangle([OUT_W / 2 - 36, y + 14, OUT_W / 2 + 36, y + 17], fill=(60, 68, 80))
    y += 40
    if run.task:
        _centered(d, y, _ellipsize(font("ui", 25), run.task, OUT_W - 200), font("ui", 25), TEXT)
        y += 44
    if trig:
        tf = font("ui_semibold", 21)
        s = _ellipsize(tf, "Sabotage: " + trig, OUT_W - 200)
        _centered(d, y, s, tf, SABOTAGE)
    else:
        _centered(d, y, "No sabotage: baseline run", font("ui_semibold", 21), DIM)
    return np.asarray(img)


def make_end_card(title: str, plans: list[RunPlan]) -> np.ndarray:
    img, d = _card_base()
    tf, rf, sf, pf = font("ui_bold", 40), font("ui_semibold", 24), font("ui", 17), font("ui_bold", 16)
    rows = plans[:6]
    row_h = int(min(100, (OUT_H - 220) / max(1, len(rows))))
    y = (OUT_H - (80 + len(rows) * row_h)) / 2
    _centered(d, y, _ellipsize(tf, title or "Results", OUT_W - 200), tf, BRIGHT)
    y += 80
    x0, x1 = 170, OUT_W - 170
    for i, p in enumerate(rows, 1):
        d.line([(x0, y), (x1, y)], fill=(40, 46, 56))
        ry = y + (row_h - 82) / 2 + 4
        kicker, head = split_chapter_title(p.run.title, i)
        d.text((x0, ry), kicker, font=font("ui_semibold", 13), fill=ACCENT)
        d.text((x0, ry + 18), _ellipsize(rf, head, x1 - x0 - 230), font=rf, fill=BRIGHT)
        bits = [f"{p.tool_calls} tool calls", f"{p.run.frame_times[-1] if p.run.n_frames else 0:.1f} s sim"]
        if p.fault_labels:
            bits.append("sabotage: " + ", ".join(dict.fromkeys(p.fault_labels)))
        else:
            bits.append("no sabotage")
        d.text((x0, ry + 54), _ellipsize(sf, " · ".join(bits), x1 - x0 - 230), font=sf, fill=DIM)
        if p.verdict is None:
            pill = _pill("NO VERDICT", pf, (230, 230, 230, 255), (70, 76, 86, 255), pad_x=16, pad_y=7)
        elif p.verdict:
            pill = _pill("VERIFIED", pf, (255, 255, 255, 255), (35, 134, 54, 255), pad_x=16, pad_y=7)
        else:
            pill = _pill("FAILED", pf, (255, 255, 255, 255), (200, 40, 48, 255), pad_x=16, pad_y=7)
        img.paste(pill, (x1 - pill.width, int(ry + 22)), pill)
        y += row_h
    d.line([(x0, y), (x1, y)], fill=(40, 46, 56))
    return np.asarray(img)


# --------------------------------------------------------------------------- program
@dataclass
class Segment:
    kind: str                 # "card" | "run"
    n: int
    label: str
    card: np.ndarray | None = None
    plan: RunPlan | None = None
    fade_in: bool = True
    fade_out: bool = True
    start: int = 0


def build_program(runs: list[Run], compile_mode: bool = False, title: str | None = None,
                  hold_scale: float = 1.0, subtitle: str | None = None,
                  bylines: list[str] | None = None) -> list[Segment]:
    segs: list[Segment] = []
    plans = [build_run_plan(r, hold_scale) for r in runs]
    if compile_mode:
        if title:
            n = len(runs)
            sub = subtitle or f"{n} chapter{'s' if n != 1 else ''} · {DEFAULT_SUBTITLE}"
            card_s = TITLE_CARD_S + (1.0 if bylines else 0.0)
            segs.append(Segment("card", int(card_s * FPS), title, make_title_card(title, sub, bylines)))
        for i, (r, p) in enumerate(zip(runs, plans), 1):
            segs.append(Segment("card", int(CARD_S * FPS), r.title, make_chapter_card(i, r)))
            segs.append(Segment("run", p.n, r.title, plan=p))
        segs.append(Segment("card", int(END_CARD_S * FPS), "Results", make_end_card(title or "Results", plans)))
    else:
        for p in plans:
            segs.append(Segment("run", p.n, p.run.title, plan=p, fade_out=False))
    k = 0
    for s in segs:
        s.start = k
        k += s.n
    return segs


class Composer:
    def __init__(self, workers: int = 4) -> None:
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.ov = Overlays()
        self.canvas = np.zeros((OUT_H, OUT_W, 3), np.uint8)
        self._keys: dict[str, tuple] = {}

    def close(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    def frames(self, program: list[Segment], select: set[int] | None = None):
        """Yield (global_index, HxWx3 uint8) for every (or every selected) output frame."""
        for seg in program:
            ks = range(seg.n) if select is None else [k for k in range(seg.n) if seg.start + k in select]
            if not ks:
                continue
            if seg.kind == "card":
                for k in ks:
                    yield seg.start + k, _fade(seg.card, self._fade_factor(seg, k))
                continue
            plan = seg.plan
            self._keys.clear()
            loader = FrameLoader(plan.run, [plan.specs[k].frame for k in ks], self.pool)
            panel, tbar, strip = TracePanel(plan), TitleBar(plan.run.title, plan.run.task), ProgressStrip(plan)
            try:
                for k in ks:
                    out = self._run_frame(plan, k, loader, panel, tbar, strip)
                    yield seg.start + k, _fade(out, self._fade_factor(seg, k))
            finally:
                loader.close()

    @staticmethod
    def _fade_factor(seg: Segment, k: int) -> float:
        f = 1.0
        if seg.fade_in and k < FADE_FRAMES:
            f = min(f, (k + 1) / (FADE_FRAMES + 1))
        if seg.fade_out and seg.n - 1 - k < FADE_FRAMES:
            f = min(f, (seg.n - k) / (FADE_FRAMES + 1))
        return f

    def _run_frame(self, plan: RunPlan, k: int, loader: FrameLoader, panel: TracePanel, tbar: TitleBar,
                   strip: ProgressStrip) -> np.ndarray:
        """Compose one frame into the shared canvas, redrawing only regions whose inputs changed."""
        s = plan.specs[k]
        c = self.canvas
        pid = id(plan)
        tkey = (pid, f"{s.sim_time:7.2f}", s.tools)
        if tkey != self._keys.get("title"):
            c[0:TITLE_H] = tbar.get(s.sim_time, s.tools)
            self._keys["title"] = tkey
        n_stamp = len(self.ov.stamp(bool(plan.verdict))) if s.stamp_age >= 0 else 0
        skey = (pid, s.frame, s.faults, s.banner, s.banner_age if s.banner >= 0 else 0,
                min(s.stamp_age, n_stamp - 1) if s.stamp_age >= 0 else -1)
        if skey != self._keys.get("sim"):
            self._draw_sim(plan, s, loader)
            self._keys["sim"] = skey
        c[STRIP_Y:STRIP_Y + STRIP_H, SIM_X:SIM_X + SIM_W] = strip.get(k)
        pkey = (pid, s.n_entries, s.reveal)
        if pkey != self._keys.get("panel"):
            c[PANEL_Y:PANEL_Y + PANEL_H, PANEL_X:PANEL_X + PANEL_W] = panel.get(s.n_entries, s.reveal)
            self._keys["panel"] = pkey
        return c

    def _draw_sim(self, plan: RunPlan, s: Spec, loader: FrameLoader) -> None:
        sim = self.canvas[SIM_Y:SIM_Y + SIM_H, SIM_X:SIM_X + SIM_W]
        sim[...] = loader.get(s.frame)
        if s.stamp_age >= 0:
            # dim the scene a little so the stamp reads well
            sim[...] = ((sim.astype(np.uint16) * 170) >> 8).astype(np.uint8)
        y = 14
        for label in s.faults:   # integration: top-right, clear of any status text in the frame's top-left
            chip = self.ov.chip(label)
            chip.blit(sim, SIM_W - 14 - chip.w, y)
            y += chip.h + 6
        if s.banner >= 0:
            grow, bright, dim = self.ov.banner(plan.banners[s.banner])
            age = s.banner_age
            on = (age // 8) % 2 == 0
            spr = grow[age] if age < len(grow) else (bright if on else dim)
            spr.blit(sim, 0, (SIM_H - spr.h) // 2)
            if on:
                self.ov.border().blit(sim, 0, 0)
        if s.stamp_age >= 0:
            seq = self.ov.stamp(bool(plan.verdict))
            spr = seq[min(s.stamp_age, len(seq) - 1)]
            spr.blit(sim, (SIM_W - spr.w) // 2, (SIM_H - spr.h) // 2)


# --------------------------------------------------------------------------- outputs
def write_mp4(program: list[Segment], out: Path, crf: int = 20, preset: str = "medium",
              composer: Composer | None = None, progress=None) -> int:
    import imageio_ffmpeg

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".partial" + out.suffix)
    own = composer is None
    composer = composer or Composer()
    writer = imageio_ffmpeg.write_frames(
        str(tmp), (OUT_W, OUT_H), fps=FPS, codec="libx264", pix_fmt_in="rgb24", pix_fmt_out="yuv420p",
        quality=None, macro_block_size=16, ffmpeg_log_level="error",
        output_params=["-preset", preset, "-crf", str(crf), "-movflags", "+faststart"])
    writer.send(None)
    n = 0
    total = sum(s.n for s in program)
    try:
        for _, frame in composer.frames(program):
            writer.send(frame)          # contiguous uint8 HxWx3; written straight to ffmpeg's stdin
            n += 1
            if progress and n % 300 == 0:
                progress(n, total)
    finally:
        writer.close()
        if own:
            composer.close()
    os.replace(tmp, out)
    return n


def gif_window(plan: RunPlan) -> tuple[int, int]:
    """(start, end) output frames of the teaser stretch within one run plan."""
    if plan.first_sabotage_k is None:
        return 0, plan.n
    start = max(0, plan.first_sabotage_k - int(GIF_LEAD_S * FPS))
    end = plan.n if plan.judge_k is None else min(plan.n, plan.judge_k + int(GIF_TAIL_S * FPS))
    return start, max(end, start + 1)


def write_gif(runs: list[Run], out: Path, composer: Composer | None = None) -> dict:
    """480-px, 12 fps, <= 25 s GIF from 3 s before the first sabotage to the judge stamp.

    Uses its own plan with shorter text holds (GIF_HOLD_SCALE); if the stretch is
    still longer than 25 s it is played faster and a speed badge is drawn.
    """
    import imageio_ffmpeg

    run = next((r for r in runs if any(e.get("type") == "sabotage" for e in r.events)), runs[0])
    plan = build_run_plan(run, GIF_HOLD_SCALE)
    start, end = gif_window(plan)
    span_s = (end - start) / FPS
    speed = max(1.0, span_s / GIF_MAX_S)
    n_out = max(1, min(int(GIF_MAX_S * GIF_FPS), int(span_s / speed * GIF_FPS)))
    picks = [min(end - 1, start + int(round(j * speed * FPS / GIF_FPS))) for j in range(n_out)]
    gh = int(round(OUT_H * GIF_W / OUT_W / 2)) * 2
    seg = Segment("run", plan.n, run.title, plan=plan, fade_in=False, fade_out=False)
    own = composer is None
    composer = composer or Composer()
    by_k: dict[int, bytes] = {}
    badge = None
    if speed > 1.05:
        badge = _pill(f"{speed:.1f}x", font("ui_bold", 12), (255, 255, 255, 255), (0, 0, 0, 170), pad_x=7, pad_y=2)
    try:
        for k, frame in composer.frames([seg], set(picks)):
            im = Image.fromarray(frame).reduce(2).resize((GIF_W, gh), Image.Resampling.LANCZOS)
            if badge is not None:
                by = int((STRIP_Y) * gh / OUT_H) - badge.height - 6
                im.paste(badge, (6, by), badge)
            by_k[k] = im.tobytes()
    finally:
        if own:
            composer.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{GIF_W}x{gh}", "-r", str(GIF_FPS), "-i", "-",
           "-vf", "split[a][b];[a]palettegen=max_colors=192:stats_mode=full[p];"
                  "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
           "-loop", "0", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in picks:
            proc.stdin.write(by_k[k])
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace")
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg gif failed: {err.strip()}")
    return {"path": str(out), "frames": n_out, "duration_s": n_out / GIF_FPS, "speed": round(speed, 2),
            "width": GIF_W, "height": gh, "bytes": out.stat().st_size}


def probe_duration(path: str | Path) -> float:
    """Measured container duration in seconds (ffprobe if on PATH, else imageio-ffmpeg)."""
    import shutil

    exe = shutil.which("ffprobe")
    if exe:
        r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration", "-of",
                            "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True)
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    import imageio_ffmpeg

    return float(imageio_ffmpeg.count_frames_and_secs(str(path))[1])


def compose(run_dirs: list[str | Path], out: str | Path | None = None, *, compile_mode: bool = False,
            title: str | None = None, hold_scale: float = 1.0, gif: str | Path | None = None, mp4: bool = True,
            crf: int = 20, preset: str = "medium", log=print, subtitle: str | None = None,
            bylines: list[str] | None = None) -> dict:
    """Render the MP4 (and optional GIF). Returns a report dict."""
    runs = [load_run(d) for d in run_dirs]
    if not compile_mode and len(runs) != 1:
        raise ValueError("pass exactly one run dir, or use --compile")
    if out is None:
        out = runs[0].dir / "video.mp4" if not compile_mode else runs[0].dir.parent / "final.mp4"
    out = Path(out).resolve()
    t0 = time.perf_counter()
    program = build_program(runs, compile_mode, title, hold_scale, subtitle, bylines)
    total = sum(s.n for s in program)
    report: dict = {"out": str(out), "frames": total, "planned_s": round(total / FPS, 3),
                    "chapters": [{"start_s": round(s.start / FPS, 2), "kind": s.kind, "label": s.label}
                                 for s in program]}
    composer = Composer()
    try:
        if mp4:
            def prog(n, tot):
                log(f"  {n}/{tot} frames ({time.perf_counter() - t0:.1f}s)")

            write_mp4(program, out, crf, preset, composer, prog)
            report["measured_s"] = round(probe_duration(out), 3)
            report["bytes"] = out.stat().st_size
        if gif:
            report["gif"] = write_gif(runs, Path(gif).resolve(), composer)
    finally:
        composer.close()
    report["render_s"] = round(time.perf_counter() - t0, 2)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sabotage_demo.video",
                                 description="Compose the sabotage demo video from run directories.")
    ap.add_argument("run_dirs", nargs="+", help="run directory (or several with --compile)")
    ap.add_argument("--compile", action="store_true", help="concatenate runs as chapters with title/end cards")
    ap.add_argument("--out", default=None, help="MP4 path (default <run_dir>/video.mp4, or runs/final.mp4)")
    ap.add_argument("--title", default=None, help="opening card / end card title in --compile mode")
    ap.add_argument("--subtitle", default=None, help="opening card subtitle in --compile mode")
    ap.add_argument("--byline", action="append", default=None,
                    help="extra opening-card line under the subtitle (repeatable; first is emphasised)")
    ap.add_argument("--gif", nargs="?", const="", default=None,
                    help="also write a 480 px teaser GIF (default path: next to the MP4, .gif)")
    ap.add_argument("--no-mp4", action="store_true", help="skip the MP4 (e.g. only --gif)")
    ap.add_argument("--hold-scale", type=float, default=1.0,
                    help="multiply the contract's text hold times (1.0 = contract)")
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--preset", default="medium")
    a = ap.parse_args(argv)
    if not a.compile and len(a.run_dirs) > 1:
        ap.error("several run dirs need --compile")
    out = Path(a.out) if a.out else None
    gif = None
    if a.gif is not None:
        if a.gif:
            gif = Path(a.gif)
        else:
            base = out or (Path(a.run_dirs[0]) / "video.mp4" if not a.compile else Path(a.run_dirs[0]).parent / "final.mp4")
            gif = Path(base).with_name("teaser.gif" if not a.compile else Path(base).stem + ".gif")
    rep = compose(a.run_dirs, out, compile_mode=a.compile, title=a.title, hold_scale=a.hold_scale, gif=gif,
                  mp4=not a.no_mp4, crf=a.crf, preset=a.preset, subtitle=a.subtitle, bylines=a.byline)
    if not a.no_mp4:
        print(f"wrote {rep['out']}  {rep['frames']} frames  planned {rep['planned_s']:.2f}s  "
              f"measured {rep.get('measured_s', float('nan')):.2f}s  {rep.get('bytes', 0) / 1e6:.1f} MB")
        if a.compile:
            for ch in rep["chapters"]:
                m, s = divmod(ch["start_s"], 60)
                print(f"  {int(m):02d}:{s:05.2f}  {ch['kind']:4s}  {ch['label']}")
    if "gif" in rep:
        g = rep["gif"]
        print(f"wrote {g['path']}  {g['width']}x{g['height']}  {g['frames']} frames  {g['duration_s']:.1f}s  "
              f"speed {g['speed']}x  {g['bytes'] / 1e6:.1f} MB")
    print(f"render time {rep['render_s']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
