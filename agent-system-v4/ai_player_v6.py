#!/usr/bin/env python3
"""
ai_player_v5_strategist_py_nav.py — Strategist (Ollama) + deterministic Python Navigator (no LLM Navigator).

Primary fix in this build:
- Ensures multi-step plan execution proceeds (does not get trapped by unreliable ui.menu_open/control_enabled flags).
- Removes ANY Strategist call from dialog/menu handling (prevents 20–40s freezes that starve plan execution).
- Decouples input pacing (busy_until) from progress evaluation (check_after), and adds a 2-phase movement check
  to avoid false stuck events due to delayed position updates.

Protocol:
- Lua -> Python: JSON snapshot per line
- Python -> Lua: "KEY:FRAMES\n"

Environment (optional):
- OLLAMA_HOST, OLLAMA_PORT, OLLAMA_MODEL_STRAT, LLM_TIMEOUT_SECONDS
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

# =========================
# CONFIG
# =========================

HOST = "127.0.0.1"
PORT = 8765

BASE_DIR = Path(__file__).resolve().parent


def _first_existing(candidates: List[Path]) -> Path:
    for p in candidates:
        if p.exists():
            return p
    raise SystemExit("[ERR] Required file not found. Tried:\n" + "\n".join(f" - {c}" for c in candidates))


OVERWORLD_NAV_JSON = _first_existing([
    BASE_DIR / "overworld_nav.json",
    BASE_DIR / "overworld-json" / "overworld_nav.json",
])

OVERWORLD_META_JSON = _first_existing([
    BASE_DIR / "overworld_metadata.json",
    BASE_DIR / "overworld-json" / "overworld_metadata.json",
])

SEMANTIC_LOC_JSON = _first_existing([
    BASE_DIR / "semantic_locations.json",
    BASE_DIR / "overworld-semantic" / "semantic_locations.json",
])

README_CONTEXT_CANDIDATES = [
    BASE_DIR / "readMe-ollama-rag-init.txt",
]

DEFAULT_MOVE_HOLD_FRAMES = 15
DEFAULT_BUTTON_HOLD_FRAMES = 4

# Ollama (Strategist only)
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_MODEL_STRAT = os.environ.get("OLLAMA_MODEL_STRAT", "gbai-strategist")
LLM_TIMEOUT_SECONDS = int(os.environ.get("LLM_TIMEOUT_SECONDS", "180"))

# Cooldowns (seconds)
STRATEGIST_COOLDOWN_SECONDS = 2.5
NAVIGATOR_COOLDOWN_SECONDS = 0.5
BATTLE_PRESS_INTERVAL_SECONDS = 0.25

# Plan execution
MAX_PLAN_BUTTONS = 12

# Stuck detection
STUCK_MAX_CONSECUTIVE = 3
STUCK_REPLAN_COOLDOWN_SECONDS = 1.0

# Goal reached tolerance
GOAL_TOLERANCE_MANHATTAN = 1

# Status log pacing
STATUS_LOG_INTERVAL_SECONDS = 1.0

# UI recovery pacing
UI_PRESS_INTERVAL_SECONDS = 0.35

# Movement progress grace (frames) before evaluating no-progress
MOVEMENT_GRACE_FRAMES = 24
# Extra re-check grace (frames) on first "no progress" (delayed position updates)
MOVEMENT_RECHECK_FRAMES = 36

# =========================
# JSON helpers
# =========================

def _load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _try_load_text(paths: List[Path]) -> Optional[str]:
    for p in paths:
        try:
            if p.exists():
                return p.read_text(encoding="utf-8")
        except Exception:
            continue
    return None


def _extract_json_object(text: str) -> Any:
    if not text:
        return None
    s = text.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except Exception:
            return None
    return None


# =========================
# WORLD (Python-side nav)
# =========================

class World:
    def __init__(self, sem_path: Path, nav_path: Path, meta_path: Path):
        sem = _load_json(sem_path)
        self.semantic: Dict[str, Dict[str, Any]] = sem.get("locations", {})

        self.nav: Dict[str, Any] = _load_json(nav_path)
        self.meta: Dict[str, Any] = _load_json(meta_path)

        self.map_index: Dict[Tuple[int, int], str] = {}
        for it in self.nav.get("index", []) or []:
            try:
                self.map_index[(int(it.get("group", 0)), int(it.get("num", 0)))] = str(it.get("map_name"))
            except Exception:
                continue

        self.maps: Dict[str, Any] = self.nav.get("maps", {}) or {}
        self.meta_maps: Dict[str, Any] = self.meta.get("maps", {}) or {}

        # Map adjacency (connections + warps)
        self.adj: Dict[str, List[Dict[str, Any]]] = {}
        for name, mp in (self.maps or {}).items():
            edges: List[Dict[str, Any]] = []
            for c in mp.get("connections", []) or []:
                if c.get("map_name"):
                    edges.append({
                        "kind": "connection",
                        "dest_map_name": c.get("map_name"),
                        "direction": c.get("direction"),
                        "offset": c.get("offset"),
                    })
            for w in mp.get("warp_events", []) or []:
                if w.get("dest_map_name"):
                    edges.append({
                        "kind": "warp",
                        "dest_map_name": w.get("dest_map_name"),
                        "x": int(w.get("x", 0)),
                        "y": int(w.get("y", 0)),
                        "elevation": int(w.get("elevation", 0)),
                        "dest_warp_id": str(w.get("dest_warp_id", "0")),
                    })
            self.adj[name] = edges

        print(f"[world] Semantic locations: {len(self.semantic)}")
        print(f"[world] Overworld maps: {len(self.maps)}")

    def semantic_summary_compact(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for k, v in self.semantic.items():
            items.append({
                "key": k,
                "type": v.get("type"),
                "region": v.get("region"),
                "map_group": v.get("map_group"),
                "map_num": v.get("map_num"),
                "x": v.get("x"),
                "y": v.get("y"),
            })
        return items

    def map_name_for(self, map_group: int, map_num: int) -> Optional[str]:
        return self.map_index.get((int(map_group), int(map_num)))

    def semantic_target(self, key: str) -> Optional[Dict[str, Any]]:
        v = self.semantic.get(key)
        if not v:
            return None
        g = int(v.get("map_group", 0))
        n = int(v.get("map_num", 0))
        return {
            "key": key,
            "desc": v.get("desc"),
            "type": v.get("type"),
            "region": v.get("region"),
            "map_group": g,
            "map_num": n,
            "map_name": self.map_name_for(g, n),
            "x": int(v.get("x", 0)),
            "y": int(v.get("y", 0)),
        }

    def next_overworld_edge(self, cur_map_name: str, target_map_name: str) -> Optional[Dict[str, Any]]:
        if not cur_map_name or not target_map_name:
            return None
        if cur_map_name == target_map_name:
            return {"kind": "already_there"}

        q = deque([cur_map_name])
        prev: Dict[str, Tuple[str, Dict[str, Any]]] = {cur_map_name: ("", {})}

        while q:
            u = q.popleft()
            if u == target_map_name:
                break
            for e in self.adj.get(u, []) or []:
                v = e.get("dest_map_name")
                if not v or v in prev:
                    continue
                prev[v] = (u, e)
                q.append(v)

        if target_map_name not in prev:
            return None

        v = target_map_name
        while prev[v][0] and prev[v][0] != cur_map_name:
            v = prev[v][0]

        parent, edge = prev[v]
        if parent != cur_map_name:
            edge = prev[target_map_name][1]
        return edge

    def _pick_border_entry(self, map_name: str, direction: str) -> Optional[Tuple[int, int]]:
        mp = self.maps.get(map_name)
        if not mp:
            return None
        w = int(mp.get("width", 0))
        h = int(mp.get("height", 0))
        grid = mp.get("grid")
        if not grid or w <= 0 or h <= 0:
            return None

        def walkable(x: int, y: int) -> bool:
            try:
                return bool(grid[y][x])
            except Exception:
                return False

        if direction == "left":
            x = 0
            y0 = h // 2
            for d in range(h):
                y = y0 + (d if d % 2 == 0 else -d)
                if 0 <= y < h and walkable(x, y):
                    return (x, y)
        elif direction == "right":
            x = w - 1
            y0 = h // 2
            for d in range(h):
                y = y0 + (d if d % 2 == 0 else -d)
                if 0 <= y < h and walkable(x, y):
                    return (x, y)
        elif direction == "up":
            y = 0
            x0 = w // 2
            for d in range(w):
                x = x0 + (d if d % 2 == 0 else -d)
                if 0 <= x < w and walkable(x, y):
                    return (x, y)
        elif direction == "down":
            y = h - 1
            x0 = w // 2
            for d in range(w):
                x = x0 + (d if d % 2 == 0 else -d)
                if 0 <= x < w and walkable(x, y):
                    return (x, y)
        return None

    def connection_subgoal(self, cur_map_name: str, dest_map_name: str) -> Optional[Dict[str, Any]]:
        mp = self.maps.get(cur_map_name)
        if not mp:
            return None
        for c in mp.get("connections", []) or []:
            if c.get("map_name") == dest_map_name:
                direction = c.get("direction")
                entry = self._pick_border_entry(cur_map_name, direction)
                if not entry:
                    return None
                x, y = entry
                return {"kind": "connection", "direction": direction, "x": x, "y": y, "dest_map_name": dest_map_name}
        return None

    def warp_subgoal(self, cur_map_name: str, dest_map_name: str) -> Optional[Dict[str, Any]]:
        mp = self.maps.get(cur_map_name)
        if not mp:
            return None
        for w in mp.get("warp_events", []) or []:
            if w.get("dest_map_name") == dest_map_name:
                return {
                    "kind": "warp",
                    "x": int(w.get("x", 0)),
                    "y": int(w.get("y", 0)),
                    "dest_map_name": dest_map_name,
                    "dest_warp_id": str(w.get("dest_warp_id", "0")),
                    "hint": "Step onto the tile to trigger the warp; if it fails, face it and press A.",
                }
        return None


# =========================
# STATE FROM LUA
# =========================

@dataclass
class GameState:
    frame: int
    mode: str
    map_group: int
    map_num: int
    x: int
    y: int
    hp: int
    max_hp: int
    textbox_open: bool = False
    menu_open: bool = False
    control_enabled: bool = True
    map_name: Optional[str] = None

    @property
    def hp_ratio(self) -> float:
        if self.max_hp <= 0:
            return 1.0
        return self.hp / self.max_hp


class LuaBridge:
    """TCP link to emerald_bridge_v5.lua."""

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self.server: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.rfile = None  # type: ignore
        self.running = False
        self.frame_counter = 0
        self._lock = threading.Lock()
        self.latest_state: Optional[GameState] = None
        self._send_lock = threading.Lock()

    def start(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(1)
        print(f"[net] Waiting mGBA on {self.host}:{self.port} ...")
        conn, addr = self.server.accept()
        print(f"[net] Connected: {addr}")
        self.conn = conn
        self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.rfile = conn.makefile("rb")
        self.running = True
        threading.Thread(target=self._reader_loop, daemon=True).start()

    def _reader_loop(self):
        try:
            while self.running:
                line = self.rfile.readline()
                if not line:
                    raise ConnectionError("Lua socket closed (EOF).")
                s = line.decode("utf-8", errors="replace").strip()
                if s:
                    self._handle_snapshot(s)
        except Exception as e:
            print(f"[net] Reader loop error: {e}")
        finally:
            self.running = False

    def _handle_snapshot(self, s: str):
        try:
            raw = json.loads(s)
        except Exception as e:
            print(f"[net] Bad JSON: {e} | {s[:120]!r}")
            return

        self.frame_counter += 1
        mode = str(raw.get("mode", "OVERWORLD")).upper()
        m = raw.get("map") or {}
        p = raw.get("party") or {}
        ui = raw.get("ui") or {}

        gs = GameState(
            frame=int(raw.get("frame", self.frame_counter)),
            mode=mode,
            map_group=int(m.get("group", 0)),
            map_num=int(m.get("num", 0)),
            x=int(m.get("x", 0)),
            y=int(m.get("y", 0)),
            hp=int(p.get("hp", 0)),
            max_hp=max(1, int(p.get("max_hp", 1))),
            textbox_open=bool(ui.get("textbox_open", raw.get("textbox_open", False))),
            menu_open=bool(ui.get("menu_open", raw.get("menu_open", False))),
            control_enabled=bool(ui.get("control_enabled", raw.get("control_enabled", True))),
            map_name=(m.get("name") or None),
        )
        with self._lock:
            self.latest_state = gs

    def snapshot(self) -> Optional[GameState]:
        with self._lock:
            return self.latest_state

    def wait_for_new_state(self, prev_frame: int, timeout: float = 1.0) -> Optional[GameState]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.snapshot()
            if st and st.frame > prev_frame:
                return st
            time.sleep(0.01)
        return self.snapshot()

    def send_input(self, key: str, hold_frames: int) -> bool:
        if not self.conn:
            return False
        line = f"{key}:{int(hold_frames)}\n".encode("utf-8")
        try:
            with self._send_lock:
                self.conn.sendall(line)
            print(f"[input] {key}:{int(hold_frames)}")
            return True
        except Exception as e:
            print(f"[net] Send error: {e}")
            return False


# =========================
# OLLAMA CLIENT (Strategist)
# =========================

class OllamaClient:
    def __init__(self, host: str, port: int, timeout: int):
        self.host = host
        self.port = port
        self.timeout = timeout

    def generate(self, model: str, prompt: str, context: Optional[List[int]] = None) -> Tuple[str, Optional[List[int]]]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/json"}
        payload: Dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
        if context is not None:
            payload["context"] = context
        try:
            conn.request("POST", "/api/generate", body=json.dumps(payload), headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()

        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return "", context

        txt = (data.get("response") or "").strip()
        new_ctx = data.get("context", context)
        return txt, new_ctx


# =========================
# SCHEMAS
# =========================

@dataclass
class StrategistDecision:
    intent: str = "WAIT"               # GO_TO | PRESS | WAIT | BATTLE_AUTO
    goal: Optional[str] = None
    button: Optional[str] = None
    repeat: int = 1
    policy: Dict[str, Any] = None      # type: ignore

    @staticmethod
    def parse(payload_text: str) -> "StrategistDecision":
        obj = _extract_json_object(payload_text)
        if not isinstance(obj, dict):
            return StrategistDecision(intent="WAIT", policy={"avoid_optional_trainers": True, "allow_grass_encounters": False})

        action = str(obj.get("action", obj.get("intent", "WAIT"))).upper().strip()
        if action == "MOVE":
            action = "GO_TO"
        if action == "HANDLE_DIALOG":
            action = "PRESS"
        if action not in {"GO_TO", "PRESS", "BATTLE_AUTO", "WAIT"}:
            action = "WAIT"

        goal = obj.get("target") or obj.get("goal")
        goal = str(goal).strip() if goal is not None else None
        if goal and goal.lower().strip() == "none":
            goal = None

        button = obj.get("button")
        button = str(button).upper().strip() if button is not None else None
        if button and button not in {"A", "B", "START", "SELECT"}:
            button = "A"

        repeat = obj.get("repeat", 1)
        try:
            repeat = int(repeat)
        except Exception:
            repeat = 1
        repeat = max(1, min(6, repeat))

        policy = obj.get("policy") if isinstance(obj.get("policy"), dict) else {}

        # Normalize: GO_TO with missing/empty target becomes WAIT
        if action == "GO_TO" and (goal is None or goal == "" or str(goal).lower().strip() == "none"):
            action = "WAIT"
            goal = None

        return StrategistDecision(intent=action, goal=goal, button=button, repeat=repeat, policy=policy)


# =========================
# PROMPTS
# =========================

def strategist_bootstrap_prompt(readme_text: str, semantic_keys: List[Dict[str, Any]]) -> str:
    header = {
        "role": "STRATEGIST",
        "mission": [
            "Decide WHAT to do next in Pokémon Emerald.",
            "Return only one JSON object strictly matching the schema."
        ],
        "schema": {
            "action": "GO_TO | PRESS | WAIT",
            "target": "semantic key or null",
            "button": "A|B|START|SELECT or null",
            "repeat": "1..6",
            "policy": {"avoid_optional_trainers": "bool", "allow_grass_encounters": "bool"},
        },
        "rules": [
            "Never invent targets: target must be null or one of the provided semantic keys.",
            "If state.mode == BATTLE: use PRESS with button A.",
            "If ui.textbox_open: use PRESS with button A.",
            "Never output GO_TO with a null/empty target.",
            "Output MUST be valid JSON and nothing else."
        ],
        "semantic_locations": semantic_keys,
        "readme": readme_text[:12000] if readme_text else "",
        "ack": {"action": "WAIT", "target": None, "button": None, "repeat": 1, "policy": {"avoid_optional_trainers": True, "allow_grass_encounters": False}},
    }
    return json.dumps(header, ensure_ascii=False)


def strategist_runtime_prompt(state: GameState, semantic_keys: List[Dict[str, Any]], last_goal: Optional[str]) -> str:
    # ui.menu_open / ui.control_enabled are soft/unreliable in some Lua feeds: do not let them dominate the LLM.
    payload = {
        "state": {
            "mode": state.mode,
            "map": {"group": state.map_group, "num": state.map_num, "x": state.x, "y": state.y, "name": state.map_name},
            "party": {"hp": state.hp, "max_hp": state.max_hp, "hp_pct": round(state.hp_ratio, 3)},
            "ui": {"textbox_open": state.textbox_open, "menu_open": False, "control_enabled": True},
        },
        "current_goal": last_goal,
        "known_semantic_locations": semantic_keys,
        "task": "Return ONE JSON object strictly matching the Strategist schema.",
    }
    return json.dumps(payload, ensure_ascii=False)


# =========================
# AGENT SYSTEM
# =========================

class AgentSystemV5:
    def __init__(self):
        self.world = World(SEMANTIC_LOC_JSON, OVERWORLD_NAV_JSON, OVERWORLD_META_JSON)
        self.lua = LuaBridge()
        self.ollama = OllamaClient(OLLAMA_HOST, OLLAMA_PORT, LLM_TIMEOUT_SECONDS)

        self.ctx_strat: Optional[List[int]] = None
        self.strat_bootstrapped = False

        self.current_goal: Optional[str] = None
        self.current_policy: Dict[str, Any] = {"avoid_optional_trainers": True, "allow_grass_encounters": False}
        self.plan_queue: Deque[Tuple[str, int]] = deque()

        self.input_busy_until_frame: int = 0

        self.last_strat_time = 0.0
        self.last_nav_time = 0.0
        self.last_battle_press_time = 0.0
        self.last_replan_time = 0.0
        self.last_status_log_time = 0.0
        self.last_ui_press_time = 0.0
        # inflight guard
        self.strat_inflight = False

        # (btn, sent_frame, busy_until, map_group, map_num, packed_xy)
        self.last_sent_input: Optional[Tuple[str, int, int, int, int, int]] = None
        self.consecutive_no_progress = 0

        self.semantic_compact = sorted(self.world.semantic_summary_compact(), key=lambda d: d["key"])
        self.readme_text = _try_load_text(README_CONTEXT_CANDIDATES) or ""

    def bootstrap_models(self):
        if self.strat_bootstrapped:
            return
        prompt = strategist_bootstrap_prompt(self.readme_text, self.semantic_compact)
        _, self.ctx_strat = self.ollama.generate(OLLAMA_MODEL_STRAT, prompt, context=self.ctx_strat)
        self.strat_bootstrapped = True
        print("[boot] Strategist bootstrapped (context cached).")

    # ---------- main loop ----------

    def run(self):
        self.lua.start()

        st = None
        while st is None:
            st = self.lua.snapshot()
            time.sleep(0.05)

        if not st.map_name:
            st.map_name = self.world.map_name_for(st.map_group, st.map_num)

        prev_frame = st.frame
        print("[agent] Starting main loop (v5, strategist-only + python navigator).")
        self.bootstrap_models()

        while True:
            st = self.lua.wait_for_new_state(prev_frame, timeout=1.0)
            if st is None:
                print("[agent] No state; exiting.")
                break
            prev_frame = st.frame

            if not st.map_name:
                st.map_name = self.world.map_name_for(st.map_group, st.map_num)

            self._status_log(st)
            self._update_progress_and_stuck(st)

            # BATTLE
            if st.mode == "BATTLE":
                self._handle_battle(st)
                continue

            # Goal reached
            if self.current_goal and self._is_goal_reached(st, self.current_goal):
                print(f"[goal] Reached {self.current_goal}. Clearing goal/plan.")
                self.current_goal = None
                self.plan_queue.clear()

            # Textbox is a hard stop.
            if st.textbox_open:
                self._handle_textbox(st)
                continue

            # Execute plan if ready
            if self.plan_queue and st.frame >= self.input_busy_until_frame:
                self._execute_next_plan_step(st)
                continue

            # Respect pacing
            if st.frame < self.input_busy_until_frame:
                continue

            now = time.time()

            # 2) Navigator: if we have a goal but no plan, compute a plan
            if self.current_goal and (not self.plan_queue) and ((now - self.last_nav_time) >= NAVIGATOR_COOLDOWN_SECONDS):
                self._query_navigator_for_plan(st, event=None, force=False)
                continue

            # 3) Strategist: ONLY when no goal, AND no plan, AND cooldown passed
            if self.current_goal is None and (not self.plan_queue) and ((now - self.last_strat_time) >= STRATEGIST_COOLDOWN_SECONDS):
                self._query_strategist_for_goal(st)
                # fallthrough: if Strategist set a goal, let next loop handle nav/plan
                continue

            # Soft menu recovery only when idle.
            if (not self.plan_queue) and st.menu_open:
                self._try_close_menu_soft(st)

    # ---------- logging ----------

    def _status_log(self, st: GameState):
        now = time.time()
        if (now - self.last_status_log_time) < STATUS_LOG_INTERVAL_SECONDS:
            return
        self.last_status_log_time = now
        print(
            "[status] "
            f"frame={st.frame} mode={st.mode} "
            f"pos={st.map_group}:{st.map_num}@({st.x},{st.y}) "
            f"ui(tb={int(st.textbox_open)} menu={int(st.menu_open)} ctrl={int(st.control_enabled)}) "
            f"goal={self.current_goal} plan={len(self.plan_queue)} busy_until={self.input_busy_until_frame}"
        )

    # ---------- handlers ----------

    def _handle_battle(self, st: GameState):
        now = time.time()
        if (now - self.last_battle_press_time) >= BATTLE_PRESS_INTERVAL_SECONDS:
            if self.lua.send_input("A", DEFAULT_BUTTON_HOLD_FRAMES):
                self.last_battle_press_time = now
                self.input_busy_until_frame = max(self.input_busy_until_frame, st.frame + DEFAULT_BUTTON_HOLD_FRAMES)

    def _handle_textbox(self, st: GameState):
        now = time.time()
        if (now - self.last_ui_press_time) < UI_PRESS_INTERVAL_SECONDS:
            return
        self.last_ui_press_time = now
        self._press("A", st, repeat=1)

    def _try_close_menu_soft(self, st: GameState):
        now = time.time()
        if (now - self.last_ui_press_time) < UI_PRESS_INTERVAL_SECONDS:
            return
        self.last_ui_press_time = now
        self._press("B", st, repeat=1)

    def _query_strategist_for_goal(self, st: GameState):
        # Guard against re-entry
        if self.strat_inflight:
            return

        now = time.time()
        if (now - self.last_strat_time) < STRATEGIST_COOLDOWN_SECONDS:
            return

        self.strat_inflight = True
        t0 = time.time()
        try:
            prompt = strategist_runtime_prompt(st, self.semantic_compact, self.current_goal)
            txt, self.ctx_strat = self.ollama.generate(OLLAMA_MODEL_STRAT, prompt, context=self.ctx_strat)
            self.last_strat_time = time.time()  # END timestamp
        finally:
            self.strat_inflight = False
        t1 = time.time()

        dec = StrategistDecision.parse(txt)
        print(f"[STRATEGIST] {dec} (lat={t1 - t0:.2f}s)")

        if dec.intent == "GO_TO" and dec.goal:
            changed = (dec.goal != self.current_goal)
            self.current_goal = dec.goal
            self.current_policy = self._normalize_policy(dec.policy)
            if changed:
                self.plan_queue.clear()

            # Deterministic navigator is cheap: plan immediately (force bypasses nav cooldown)
            self._query_navigator_for_plan(st, event={"type": "NEW_GOAL"}, force=True)
            return

        if dec.intent in {"PRESS", "BATTLE_AUTO"}:
            self._press(dec.button or "A", st, repeat=dec.repeat)

    # ---------- deterministic python navigator ----------

    def _query_navigator_for_plan(self, st: GameState, event: Optional[Dict[str, Any]], force: bool = False):
        now = time.time()
        if (not force) and ((now - self.last_nav_time) < NAVIGATOR_COOLDOWN_SECONDS):
            return
        if not self.current_goal:
            return

        goal = self.world.semantic_target(self.current_goal)
        if not goal:
            print(f"[NAVIGATOR/PY] Unknown goal key: {self.current_goal}. Clearing goal.")
            self.current_goal = None
            return

        cur_map_name = st.map_name or self.world.map_name_for(st.map_group, st.map_num) or f"MAP_{st.map_group}_{st.map_num}"
        tgt_map_name = goal.get("map_name")

        # Decide subgoal (where to head on the current map)
        subgoal: Dict[str, Any] = {"kind": "local", "x": int(goal["x"]), "y": int(goal["y"]), "map_name": tgt_map_name}

        if tgt_map_name and cur_map_name != tgt_map_name:
            edge = self.world.next_overworld_edge(cur_map_name, tgt_map_name)
            if edge and edge.get("kind") == "connection":
                sg = self.world.connection_subgoal(cur_map_name, edge.get("dest_map_name"))
                if sg:
                    subgoal = sg
            elif edge and edge.get("kind") == "warp":
                sg = self.world.warp_subgoal(cur_map_name, edge.get("dest_map_name"))
                if sg:
                    subgoal = sg
            else:
                subgoal = {"kind": "map_route_unknown", "hint": "No known path; explore edges and look for exits/warps."}

        buttons = self._python_build_plan(st, cur_map_name, subgoal, event=event)
        self.last_nav_time = time.time()  # end-ish timestamp (cheap, but consistent)

        if buttons:
            self.plan_queue = deque(buttons)
            print(f"[NAVIGATOR/PY] kind={subgoal.get('kind')} buttons={buttons[:8]}{' ...' if len(buttons) > 8 else ''}")
            if st.frame >= self.input_busy_until_frame:
                self._execute_next_plan_step(st)
        else:
            print(f"[NAVIGATOR/PY] No plan (kind={subgoal.get('kind')}).")

    def _python_build_plan(self, st: GameState, map_name: str, subgoal: Dict[str, Any], event: Optional[Dict[str, Any]]):
        mp = self.world.maps.get(map_name) or {}
        grid = mp.get("grid")
        if not grid:
            return self._python_fallback_explore(st, map_name)

        sx, sy = int(st.x), int(st.y)
        tx = int(subgoal.get("x", sx))
        ty = int(subgoal.get("y", sy))

        blocked = set()
        if bool(self.current_policy.get("avoid_optional_trainers", True)):
            blocked |= self._trainer_avoid_tiles(map_name, radius=1)

        if event and str(event.get("type")) == "STUCK":
            shim = self._python_stuck_shim(st, map_name, blocked)
            if shim:
                return shim

        path_dirs = self._python_path_dirs(grid, sx, sy, tx, ty, blocked=blocked)
        if path_dirs is None:
            return self._python_fallback_explore(st, map_name, target=(tx, ty), blocked=blocked)

        buttons = self._compress_dirs_to_buttons(path_dirs)

        if subgoal.get("kind") == "connection" and subgoal.get("direction"):
            btn = self._dir_to_button(str(subgoal.get("direction")))
            if btn:
                buttons += [(btn, DEFAULT_MOVE_HOLD_FRAMES)]

        return buttons[:MAX_PLAN_BUTTONS]

    def _trainer_avoid_tiles(self, map_name: str, radius: int = 1):
        out = set()
        m = (self.world.meta_maps.get(map_name, {}) or {}).get("npcs", []) or []
        for npc in m:
            ttype = str(npc.get("raw_trainer_type", ""))
            if not ttype or ttype == "TRAINER_TYPE_NONE":
                continue
            pos = npc.get("position") or {}
            try:
                x = int(pos.get("x"))
                y = int(pos.get("y"))
            except Exception:
                continue
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if abs(dx) + abs(dy) <= radius:
                        out.add((x + dx, y + dy))
        return out

    def _python_stuck_shim(self, st: GameState, map_name: str, blocked: set):
        mp = self.world.maps.get(map_name) or {}
        grid = mp.get("grid")
        if not grid:
            return None
        for btn in ["UP", "DOWN", "LEFT", "RIGHT"]:
            nx, ny = st.x, st.y
            if btn == "UP":
                ny -= 1
            elif btn == "DOWN":
                ny += 1
            elif btn == "LEFT":
                nx -= 1
            elif btn == "RIGHT":
                nx += 1
            if (nx, ny) in blocked:
                continue
            if self._grid_walkable(grid, nx, ny):
                return [(btn, DEFAULT_MOVE_HOLD_FRAMES)]
        return [("B", DEFAULT_BUTTON_HOLD_FRAMES), ("A", DEFAULT_BUTTON_HOLD_FRAMES)]

    def _grid_walkable(self, grid, x: int, y: int) -> bool:
        try:
            return bool(grid[y][x])
        except Exception:
            return False

    def _python_path_dirs(self, grid, sx: int, sy: int, tx: int, ty: int, blocked: set):
        if (sx, sy) == (tx, ty):
            return []

        h = len(grid)
        w = len(grid[0]) if h else 0

        def inb(x, y):
            return 0 <= x < w and 0 <= y < h

        start = (sx, sy)
        goal = (tx, ty)

        q = deque([start])
        prev = {start: None}
        prev_dir = {}

        dirs = [(0, -1, "UP"), (0, 1, "DOWN"), (-1, 0, "LEFT"), (1, 0, "RIGHT")]

        while q:
            x, y = q.popleft()
            for dx, dy, b in dirs:
                nx, ny = x + dx, y + dy
                if not inb(nx, ny):
                    continue
                if (nx, ny) in prev:
                    continue
                if (nx, ny) in blocked and (nx, ny) != goal:
                    continue
                if not self._grid_walkable(grid, nx, ny):
                    continue
                prev[(nx, ny)] = (x, y)
                prev_dir[(nx, ny)] = b
                if (nx, ny) == goal:
                    q.clear()
                    break
                q.append((nx, ny))

        if goal not in prev:
            return None

        cur = goal
        out = []
        while cur != start:
            out.append(prev_dir[cur])
            cur = prev[cur]
            if cur is None:
                return None
        out.reverse()
        return out

    def _compress_dirs_to_buttons(self, dirs: List[str]):
        if not dirs:
            return []
        out: List[Tuple[str, int]] = []
        cur = dirs[0]
        run = 1
        for d in dirs[1:]:
            if d == cur:
                run += 1
                continue
            out.extend(self._emit_run(cur, run))
            cur, run = d, 1
        out.extend(self._emit_run(cur, run))
        return out

    def _emit_run(self, btn: str, steps: int):
        total_frames = steps * DEFAULT_MOVE_HOLD_FRAMES
        out = []
        while total_frames > 0 and len(out) < MAX_PLAN_BUTTONS:
            chunk = min(60, total_frames)
            out.append((btn, int(chunk)))
            total_frames -= chunk
        return out

    def _dir_to_button(self, direction: str) -> Optional[str]:
        d = direction.lower().strip()
        if d == "left":
            return "LEFT"
        if d == "right":
            return "RIGHT"
        if d == "up":
            return "UP"
        if d == "down":
            return "DOWN"
        return None

    def _python_fallback_explore(self, st: GameState, map_name: str, target: Optional[Tuple[int, int]] = None, blocked: Optional[set] = None):
        mp = self.world.maps.get(map_name) or {}
        grid = mp.get("grid")
        blocked = blocked or set()
        if not grid:
            return [("A", DEFAULT_BUTTON_HOLD_FRAMES)]

        if target:
            tx, ty = target
            prefs = []
            if st.x < tx:
                prefs.append("RIGHT")
            if st.x > tx:
                prefs.append("LEFT")
            if st.y < ty:
                prefs.append("DOWN")
            if st.y > ty:
                prefs.append("UP")
            prefs += ["UP", "DOWN", "LEFT", "RIGHT"]
        else:
            prefs = ["UP", "RIGHT", "DOWN", "LEFT"]

        for btn in prefs:
            nx, ny = st.x, st.y
            if btn == "UP":
                ny -= 1
            elif btn == "DOWN":
                ny += 1
            elif btn == "LEFT":
                nx -= 1
            elif btn == "RIGHT":
                nx += 1
            if (nx, ny) in blocked:
                continue
            if self._grid_walkable(grid, nx, ny):
                return [(btn, DEFAULT_MOVE_HOLD_FRAMES)]

        return [("B", DEFAULT_BUTTON_HOLD_FRAMES), ("A", DEFAULT_BUTTON_HOLD_FRAMES)]

    # ---------- IO helpers ----------

    def _execute_next_plan_step(self, st: GameState):
        if not self.plan_queue:
            return
        btn, frames = self.plan_queue.popleft()

        sent_frame = st.frame
        busy_until = sent_frame + int(frames)
        self.input_busy_until_frame = max(self.input_busy_until_frame, busy_until)

        if btn in {"UP", "DOWN", "LEFT", "RIGHT"}:
            check_after = sent_frame + int(frames) + MOVEMENT_GRACE_FRAMES
        else:
            check_after = sent_frame + int(frames) + 1

        self.last_sent_input = (btn, sent_frame, check_after, st.map_group, st.map_num, st.x * 1000 + st.y, 0)
        self.lua.send_input(btn, frames)

    def _press(self, btn: str, st: GameState, repeat: int = 1):
        btn = (btn or "A").upper().strip()
        if btn not in {"A", "B", "START", "SELECT"}:
            btn = "A"
        repeat = max(1, min(6, int(repeat)))
        for _ in range(repeat):
            if self.lua.send_input(btn, DEFAULT_BUTTON_HOLD_FRAMES):
                self.input_busy_until_frame = max(self.input_busy_until_frame, st.frame + DEFAULT_BUTTON_HOLD_FRAMES)
            time.sleep(0.03)

    # ---------- progress / stuck ----------

    def _update_progress_and_stuck(self, st: GameState):
        if self.last_sent_input is None:
            return

        btn, sent_frame, check_after, mg, mn, packed_xy, phase = self.last_sent_input
        px = packed_xy // 1000
        py = packed_xy % 1000

        if st.frame <= check_after:
            return

        moved = (st.map_group != mg) or (st.map_num != mn) or (st.x != px) or (st.y != py)

        if btn in {"A", "B", "START", "SELECT"}:
            self.consecutive_no_progress = 0
            self.last_sent_input = None
            return

        if moved:
            self.consecutive_no_progress = 0
            self.last_sent_input = None
            return

        if phase == 0:
            self.last_sent_input = (btn, sent_frame, st.frame + MOVEMENT_RECHECK_FRAMES, mg, mn, packed_xy, 1)
            return

        self.consecutive_no_progress += 1
        print(f"[stuck] No progress after {btn}. count={self.consecutive_no_progress}/{STUCK_MAX_CONSECUTIVE}")
        self.last_sent_input = None

        if self.consecutive_no_progress >= STUCK_MAX_CONSECUTIVE:
            now = time.time()
            if (now - self.last_replan_time) < STUCK_REPLAN_COOLDOWN_SECONDS:
                return
            self.last_replan_time = now
            self.plan_queue.clear()
            event = {
                "type": "STUCK",
                "details": {
                    "last_button": btn,
                    "position": {"group": st.map_group, "num": st.map_num, "x": st.x, "y": st.y, "name": st.map_name},
                    "goal": self.current_goal,
                },
            }
            print("[stuck] Triggering Navigator replan with STUCK event.")
            self._query_navigator_for_plan(st, event=event, force=True)

    # ---------- utilities ----------

    @staticmethod
    def _normalize_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
        out = {"avoid_optional_trainers": True, "allow_grass_encounters": False}
        if not isinstance(policy, dict):
            return out
        if "avoid_optional_trainers" in policy:
            out["avoid_optional_trainers"] = bool(policy.get("avoid_optional_trainers"))
        if "allow_grass_encounters" in policy:
            out["allow_grass_encounters"] = bool(policy.get("allow_grass_encounters"))
        return out

    def _is_goal_reached(self, st: GameState, goal_key: str) -> bool:
        tgt = self.world.semantic_target(goal_key)
        if not tgt:
            return False
        if st.map_group != int(tgt.get("map_group", -999)) or st.map_num != int(tgt.get("map_num", -999)):
            return False
        gx, gy = int(tgt.get("x", 10**9)), int(tgt.get("y", 10**9))
        return abs(st.x - gx) + abs(st.y - gy) <= GOAL_TOLERANCE_MANHATTAN


def main():
    AgentSystemV5().run()


if __name__ == "__main__":
    main()
