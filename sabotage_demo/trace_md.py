"""Turn a run's events.jsonl into a readable Markdown story (integration).

    python -m sabotage_demo.trace_md <run_dir> [--out trace.md] [--name baseline]

Everything comes from the run directory as recorded: the agent's narration
(``agent_text``), every tool call with a one-line result (same wording as the
video's trace panel), sabotage events, the agent's own claim (``outcome``) and
the independent judge. Nothing is edited or summarised by hand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .video import format_tool_call, format_tool_result, sabotage_label


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _quote(text: str) -> str:
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.strip().splitlines())


def build_trace(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    events = _read_jsonl(run_dir / "events.jsonl")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8")) if (run_dir / "summary.json").exists() else {}
    scenario = json.loads((run_dir / "scenario.json").read_text(encoding="utf-8")) if (run_dir / "scenario.json").exists() else {}
    t0 = events[0]["wall_time"] if events else 0.0
    ar = summary.get("agent_result") or {}
    cost = ar.get("total_cost_usd")

    lines = [f"# {scenario.get('title') or summary.get('title') or run_dir.name}", ""]
    lines += [
        f"Run `{run_dir.name}` · model `{summary.get('model')}` · task: *{scenario.get('task', '')}*",
        "",
        "| judge (check_task) | agent claimed | tool calls | sim time | agent wall time | cost |",
        "|---|---|---|---|---|---|",
        f"| {'**success**' if summary.get('success') else '**FAILED**'} | {summary.get('agent_claimed')} | "
        f"{summary.get('tool_calls')} | {summary.get('sim_time')} s | {summary.get('wall_s')} s | "
        f"{'$%.3f' % cost if isinstance(cost, (int, float)) else 'n/a'} |",
        "",
    ]
    trig = scenario.get("triggers") or []
    if trig:
        lines.append("Injected faults (the agent is never told): " + "; ".join(
            f"`{json.dumps(t.get('sabotage'))}` when `{json.dumps(t.get('when'))}`" for t in trig))
    else:
        lines.append("No faults injected (baseline).")
    lines += ["", "Legend: plain paragraphs are the agent's own words; numbered `>` lines are tool calls and `←` their "
              "results (abridged to the key fields); 🔴 **SABOTAGE** lines are faults fired by the simulator. "
              "`report_outcome` is not a simulator call, so it appears as the agent's claim rather than a numbered step. "
              "*(thinking)* lines are the reasoning summaries Claude Code exposes in its stream. "
              "`t+` is wall-clock seconds since the run started.", "", "---", ""]

    step = 0
    for ev in events:
        typ, p = ev.get("type"), ev.get("payload") or {}
        tw = f"t+{ev.get('wall_time', t0) - t0:5.1f}s"
        if typ == "agent_text":
            text = (p.get("text") or "").strip()
            if text:
                lines += [text, ""]
        elif typ == "agent_thinking":
            text = (p.get("text") or "").strip()
            if text:
                lines += ["*(thinking)* " + text.replace("\n", " "), ""]
        elif typ == "tool_call":
            step += 1
            lines.append(f"**{step}.** `{format_tool_call(p.get('tool'), p.get('args'))}` <sub>{tw} · sim {ev.get('sim_time', 0):.2f}s</sub>  ")
        elif typ == "tool_result":
            txt, err = format_tool_result(p.get("tool"), p.get("result"))
            if lines and lines[-1] == "":   # a sabotage line came in between: keep the result attached to it
                lines.pop()
                lines[-1] += "  "
            lines += [f"`{txt}`" + ("  **(error)**" if err else ""), ""]
        elif typ == "sabotage":
            spec = p.get("spec") or {}
            label = "Faults cleared" if spec.get("kind") == "clear" else sabotage_label(p)
            lines += [f"🔴 **SABOTAGE (hidden from agent): {label}** `{json.dumps(spec)}` <sub>{tw} · sim {ev.get('sim_time', 0):.2f}s</sub>", ""]
        elif typ == "outcome":
            status = p.get("status")
            head = "**Agent's claim (report_outcome): " + str(status) + "**" if status != "none" else "**The agent never called report_outcome.**"
            lines += [head, "", _quote(p.get("summary") or "(no summary)"), ""]
        elif typ == "judge":
            det = p.get("detail") or {}
            verdict = "TASK VERIFIED" if p.get("success") else "TASK FAILED"
            dist = det.get("xy_distance_m")
            lines += ["---", "", f"**Independent judge (`GET /check_task?source=director`): {verdict}**"
                      + (f" · cube {dist * 1000:.1f} mm from the zone centre" if isinstance(dist, (int, float)) else "")
                      + f" · {det.get('reason', '')}", ""]
        elif typ == "run_end":
            lines += [f"Run end: {p.get('tool_calls')} tool calls, sim {p.get('sim_time')} s, agent wall {p.get('wall_s')} s.", ""]
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Write a run's agent narration and tool calls as Markdown.")
    ap.add_argument("run_dir")
    ap.add_argument("--out", default=None, help="default <run_dir>/trace.md")
    a = ap.parse_args(argv)
    run_dir = Path(a.run_dir)
    out = Path(a.out) if a.out else run_dir / "trace.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_trace(run_dir), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
