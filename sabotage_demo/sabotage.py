"""Fault injection ("sabotage") and scenario triggers.

Faults (CONTRACT.md, "Sabotage"):

- ``freeze_joint``: the arm joint's position servo is switched off (gain and
  bias zeroed) and the joint is clamped to the angle it had when the fault hit,
  after every physics step. Commands keep arriving, nothing moves.
- ``cut_object_sensor``: the object tracker stops reporting the cube. Physics
  and cameras are unaffected.
- ``teleport_cube``: the cube jumps to ``pos`` or by ``delta``; velocities are
  zeroed, so a held cube is torn out of the gripper.
- ``clear``: remove every fault. A thawed joint's servo target is reset to
  where the joint is, so it does not snap to a stale command.

Triggers (``scenario["triggers"]``) fire each spec at most once:
``tool_call`` (tool, n-th call, phase before/after), ``ee_near_cube`` (dist,
only while the gripper is commanded open), ``cube_lifted`` (z), ``sim_time``
(t) and ``manual`` (never fires on its own; the dashboard posts /sabotage).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import mujoco
import numpy as np

from .scene import ARM_ACTUATORS, ARM_JOINTS, CUBE_HALF

FAULT_KINDS = ("freeze_joint", "cut_object_sensor", "teleport_cube", "clear")
TRIGGER_TYPES = ("tool_call", "ee_near_cube", "cube_lifted", "sim_time", "manual")


def _vec(value: Any, n: int, what: str) -> list[float]:
    try:
        out = [float(v) for v in value]
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a list of {n} numbers") from None
    if len(out) != n or not all(np.isfinite(out)):
        raise ValueError(f"{what} must be a list of {n} finite numbers")
    return out


def normalize_spec(spec: Any) -> dict:
    """Validate a sabotage spec and return a clean copy (raises ``ValueError``)."""
    if not isinstance(spec, dict):
        raise ValueError("sabotage spec must be a JSON object")
    kind = spec.get("kind")
    if kind not in FAULT_KINDS:
        raise ValueError(f"unknown sabotage kind {kind!r}; expected one of {list(FAULT_KINDS)}")
    if kind == "freeze_joint":
        joint = spec.get("joint")
        if isinstance(joint, bool) or not isinstance(joint, (int, float)) or int(joint) != joint \
                or not 0 <= int(joint) <= 6:
            raise ValueError("freeze_joint needs an integer 'joint' in 0..6")
        return {"kind": kind, "joint": int(joint)}
    if kind == "teleport_cube":
        if "pos" in spec and spec["pos"] is not None:
            return {"kind": kind, "pos": _vec(spec["pos"], 3, "teleport_cube.pos")}
        if "delta" in spec and spec["delta"] is not None:
            return {"kind": kind, "delta": _vec(spec["delta"], 3, "teleport_cube.delta")}
        raise ValueError("teleport_cube needs 'pos' or 'delta'")
    return {"kind": kind}


class FaultInjector:
    """Applies faults to a live ``MjModel``/``MjData`` pair."""

    def __init__(self, model: mujoco.MjModel, data: mujoco.MjData):
        self.model = model
        self.data = data
        self.act_ids = [model.actuator(n).id for n in ARM_ACTUATORS]
        jids = [model.joint(n).id for n in ARM_JOINTS]
        self.qadr = [int(model.jnt_qposadr[j]) for j in jids]
        self.dadr = [int(model.jnt_dofadr[j]) for j in jids]
        cube_joint = model.body("cube").jntadr[0]
        self.cube_qadr = int(model.jnt_qposadr[cube_joint])
        self.cube_dadr = int(model.jnt_dofadr[cube_joint])
        self._gain0 = model.actuator_gainprm.copy()
        self._bias0 = model.actuator_biasprm.copy()
        self.frozen: dict[int, float] = {}
        self.sensor_dead = False

    def active_names(self) -> list[str]:
        """Fault names for the dashboard, e.g. ``["freeze_joint:0", "cut_object_sensor"]``."""
        names = [f"freeze_joint:{j}" for j in sorted(self.frozen)]
        if self.sensor_dead:
            names.append("cut_object_sensor")
        return names

    def apply(self, spec: dict) -> str:
        """Apply a normalized spec and return its human label."""
        kind = spec["kind"]
        if kind == "freeze_joint":
            j = spec["joint"]
            if j not in self.frozen:
                self.frozen[j] = float(self.data.qpos[self.qadr[j]])
                a = self.act_ids[j]
                self.model.actuator_gainprm[a, :] = 0.0
                self.model.actuator_biasprm[a, :] = 0.0
                self.data.qvel[self.dadr[j]] = 0.0
            return f"Joint {j + 1} frozen"
        if kind == "cut_object_sensor":
            self.sensor_dead = True
            return "Object tracker cut"
        if kind == "teleport_cube":
            return self._teleport(spec)
        self.clear()
        return "Faults cleared"

    def _teleport(self, spec: dict) -> str:
        qa, da = self.cube_qadr, self.cube_dadr
        old = self.data.qpos[qa:qa + 3].copy()
        new = np.array(spec["pos"]) if "pos" in spec else old + np.array(spec["delta"])
        new[2] = max(new[2], CUBE_HALF)  # never below the table top
        self.data.qpos[qa:qa + 3] = new
        self.data.qvel[da:da + 6] = 0.0
        mujoco.mj_forward(self.model, self.data)
        return f"Cube moved {np.linalg.norm(new - old) * 100:.0f} cm"

    def clear(self, ctrl_hold: bool = True) -> None:
        """Remove all faults; thawed joints hold their current angle."""
        for j in list(self.frozen):
            a = self.act_ids[j]
            self.model.actuator_gainprm[a, :] = self._gain0[a]
            self.model.actuator_biasprm[a, :] = self._bias0[a]
            if ctrl_hold:
                self.data.ctrl[a] = self.data.qpos[self.qadr[j]]
        self.frozen.clear()
        self.sensor_dead = False

    def post_step(self) -> None:
        """Clamp frozen joints; call after every ``mj_step``."""
        for j, angle in self.frozen.items():
            self.data.qpos[self.qadr[j]] = angle
            self.data.qvel[self.dadr[j]] = 0.0


@dataclass
class Trigger:
    """One scenario trigger: a condition and the sabotage spec(s) it fires once."""

    when: dict
    specs: list[dict]
    fired: bool = False

    @classmethod
    def from_json(cls, obj: Any) -> Trigger:
        if not isinstance(obj, dict) or not isinstance(obj.get("when"), dict):
            raise ValueError("trigger must be an object with a 'when' object")
        when = dict(obj["when"])
        if when.get("type") not in TRIGGER_TYPES:
            raise ValueError(f"unknown trigger type {when.get('type')!r}")
        raw = obj.get("sabotage")
        specs = [normalize_spec(s) for s in (raw if isinstance(raw, list) else [raw])]
        return cls(when=when, specs=specs)


@dataclass
class TriggerSet:
    """Evaluates scenario triggers; each trigger fires at most once until ``rearm``."""

    triggers: list[Trigger] = field(default_factory=list)

    @classmethod
    def from_scenario(cls, scenario: dict) -> TriggerSet:
        return cls([Trigger.from_json(t) for t in scenario.get("triggers", []) or []])

    def rearm(self) -> None:
        for t in self.triggers:
            t.fired = False

    @property
    def has_state_triggers(self) -> bool:
        return any(not t.fired and t.when["type"] in ("ee_near_cube", "cube_lifted", "sim_time")
                   for t in self.triggers)

    def _fire(self, t: Trigger) -> list[dict]:
        t.fired = True
        return list(t.specs)

    def on_tool_call(self, tool: str, n: int, phase: str) -> list[dict]:
        """Specs to apply for the ``n``-th call of ``tool`` at ``phase`` (before/after)."""
        out: list[dict] = []
        for t in self.triggers:
            w = t.when
            if t.fired or w["type"] != "tool_call":
                continue
            if w.get("tool") == tool and int(w.get("n", 1)) == n and w.get("phase", "before") == phase:
                out += self._fire(t)
        return out

    def on_step(self, sim_time: float, tcp_pos: np.ndarray, cube_pos: np.ndarray,
                gripper_open: bool) -> list[dict]:
        """Specs to apply given the current physical state (called every physics step)."""
        out: list[dict] = []
        for t in self.triggers:
            if t.fired:
                continue
            w = t.when
            kind = w["type"]
            if kind == "ee_near_cube":
                hit = gripper_open and float(np.linalg.norm(tcp_pos - cube_pos)) < float(w.get("dist", 0.05))
            elif kind == "cube_lifted":
                hit = float(cube_pos[2]) > float(w.get("z", 0.05))
            elif kind == "sim_time":
                hit = sim_time >= float(w.get("t", float("inf")))
            else:
                continue
            if hit:
                out += self._fire(t)
        return out
