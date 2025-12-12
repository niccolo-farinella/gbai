#!/usr/bin/env python3
"""
ai_player_v3.py - Stable high-level AI controller for Pokémon Emerald.

Key design points:

- Listens on TCP for JSON snapshots from emerald_bridge_v4.lua.
- Keeps a world model using overworld_nav.json + semantic_locations.json.
- Talks to an LLM (via Ollama HTTP API) ONLY in OVERWORLD and only when a new
  high-level decision is needed (no spamming at every tick).
- Executes high-level MOVE decisions by computing an explicit BFS path on the
  overworld graph and then following it step-by-step.
- In BATTLE mode, it ignores the LLM and just auto-presses A until the battle
  and post-battle dialog end.
- Before each LLM call, it refreshes the current GameState from the latest Lua
  snapshot and re-checks MODE, so transitions OVERWORLD <-> BATTLE cannot get
  "stuck".
- Includes:
    * runtime schema validation for LLM decisions (Decision.from_llm_payload)
    * a light FeedbackEngine used only to enrich the LLM prompt
    * a simple early-game StoryTracker based on semantic locations
    * pathfinding fallback towards the NEAREST reachable PokéCenter when the
      requested HEAL target is not reachable in the current overworld graph
"""

import json
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
from collections import deque
import http.client

# =========================
# CONFIG
# =========================

HOST = "127.0.0.1"
PORT = 8765

# PROJECT ROOT LAYOUT:
#   project/gbai/scripts/ai-player/ai_player_v3.py     (this file)
#   project/gbai/scripts/ai-player/overworld-json/*.json
#   project/gbai/scripts/ai-player/overworld-semantic/*.json
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR_JSON_OVERWORLD = PROJECT_ROOT / "scripts/ai-player/overworld-json"
DATA_DIR_JSON_SEMANTIC = PROJECT_ROOT / "scripts/ai-player/overworld-semantic"

OVERWORLD_NAV_JSON = DATA_DIR_JSON_OVERWORLD / "overworld_nav.json"
OVERWORLD_META_JSON = DATA_DIR_JSON_OVERWORLD / "overworld_metadata.json"  # reserved
SEMANTIC_LOC_JSON = DATA_DIR_JSON_SEMANTIC / "semantic_locations.json"

# Optional README for Ollama context (RAG init)
README_CANDIDATES = [
    PROJECT_ROOT / "scripts/ai-player/readMe-ollama-rag-init.txt"
]

# Input defaults
DEFAULT_MOVE_HOLD_FRAMES = 15
DEFAULT_BUTTON_HOLD_FRAMES = 4

# LLM / Ollama config
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "127.0.0.1")
OLLAMA_PORT = int(os.environ.get("OLLAMA_PORT", "11434"))
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")

# Minimum time between two *new* LLM high-level decisions (only OVERWORLD)
LLM_DECISION_COOLDOWN = 3.0  # seconds

# Safety limit for BFS (to avoid absurd paths)
MAX_BFS_NODES = 20000

# Mapping from abstract directions to Lua bridge button names
DIR_TO_BUTTON = {
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
}

# Mapping from directions to deltas
DIR_TO_DELTA = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}


# =========================
# WORLD MODEL + PATHFINDING
# =========================


class World:
    """
    World wrapper around overworld_nav.json + semantic_locations.json.

    Responsibilities:
    - map_group/map_num <-> map_name
    - nearest semantic location lookup
    - BFS pathfinding between two absolute positions (map_name, x, y)
    - BFS to nearest Pokémon Center 1F (fallback HEAL target)
    """

    def __init__(self, nav_path: Path, sem_path: Path):
        if not nav_path.exists():
            raise SystemExit(f"[world] ERRORE: file {nav_path} non trovato.")
        if not sem_path.exists():
            raise SystemExit(f"[world] ERRORE: file {sem_path} non trovato.")

        with open(nav_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.meta = data.get("meta", {})
        self.maps: Dict[str, Dict[str, Any]] = data["maps"]
        self.index: List[Dict[str, Any]] = data["index"]

        # (group, num) -> map_name
        self.by_group_num: Dict[Tuple[int, int], str] = {}
        for entry in self.index:
            self.by_group_num[(entry["group"], entry["num"])] = entry["map_name"]

        # Semantic locations
        with open(sem_path, "r", encoding="utf-8") as f:
            sem_data = json.load(f)
        self.semantic: Dict[str, Dict[str, Any]] = sem_data["locations"]

        # Precompute semantic entries grouped by (group, num)
        self.semantic_by_map: Dict[Tuple[int, int], List[Tuple[str, Dict[str, Any]]]] = {}
        for key, loc in self.semantic.items():
            k = (loc["map_group"], loc["map_num"])
            self.semantic_by_map.setdefault(k, []).append((key, loc))

        # Count PokéCenter maps for info
        self.pokecenter_maps: Set[str] = {
            mname for mname in self.maps.keys() if "PokemonCenter_1F" in mname
        }

        print(f"[world] Caricate {len(self.index)} mappe.")
        print(f"[world] Rilevate {len(self.semantic)} location semantiche.")
        print(f"[world] Rilevati {len(self.pokecenter_maps)} Pokémon Center 1F.")

    # ---- map / semantic helpers ----

    def map_name_from_group_num(self, group: int, num: int) -> Optional[str]:
        return self.by_group_num.get((group, num))

    def semantic_node_from_key(self, key: str) -> Optional[Tuple[str, int, int]]:
        loc = self.semantic.get(key)
        if not loc:
            return None
        map_name = self.map_name_from_group_num(loc["map_group"], loc["map_num"])
        if map_name is None:
            return None
        return (map_name, int(loc["x"]), int(loc["y"]))

    def nearest_semantic_location(
        self, group: int, num: int, x: int, y: int
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]], Optional[float]]:
        key = (group, num)
        candidates = self.semantic_by_map.get(key)
        if not candidates:
            return None, None, None
        best_key = None
        best_loc = None
        best_dist = 1e9
        for skey, loc in candidates:
            dx = loc["x"] - x
            dy = loc["y"] - y
            d = (dx * dx + dy * dy) ** 0.5
            if d < best_dist:
                best_dist = d
                best_key = skey
                best_loc = loc
        return best_key, best_loc, best_dist

    # ---- neighbours (copiato dal backend) ----

    def neighbors(
        self,
        node: Tuple[str, int, int],
    ) -> List[Tuple[Tuple[str, int, int], str]]:
        """
        Node = (map_name, x, y)
        Returns a list of (next_node, dir_name) where dir_name in {up,down,left,right}.
        """
        map_name, x, y = node
        if map_name not in self.maps:
            return []

        m = self.maps[map_name]
        w = m["width"]
        h = m["height"]
        grid = m["grid"]
        conns = {c["direction"]: c for c in m.get("connections", [])}
        warps = m.get("warp_events", [])

        res: List[Tuple[Tuple[str, int, int], str]] = []

        directions = [
            ("up", (0, -1)),
            ("down", (0, 1)),
            ("left", (-1, 0)),
            ("right", (1, 0)),
        ]

        for dir_name, (dx, dy) in directions:
            nx = x + dx
            ny = y + dy

            # Case 1: still inside same map
            if 0 <= nx < w and 0 <= ny < h:
                # warp?
                warp_here = None
                for w_ev in warps:
                    if w_ev["x"] == nx and w_ev["y"] == ny:
                        warp_here = w_ev
                        break

                if warp_here is not None:
                    dest_map_name = warp_here["dest_map_name"]
                    if dest_map_name is None:
                        continue
                    if dest_map_name not in self.maps:
                        continue
                    dest_map = self.maps[dest_map_name]
                    dest_warp_id = int(warp_here["dest_warp_id"])
                    dest_warps = dest_map.get("warp_events", [])

                    if 0 <= dest_warp_id < len(dest_warps):
                        dest_w = dest_warps[dest_warp_id]
                        tx = dest_w["x"]
                        ty = dest_w["y"]
                        if 0 <= tx < dest_map["width"] and 0 <= ty < dest_map["height"]:
                            if dest_map["grid"][ty][tx]:
                                res.append(((dest_map_name, tx, ty), dir_name))
                                continue  # no normal step

                # No warp: normal step if walkable
                if grid[ny][nx]:
                    res.append(((map_name, nx, ny), dir_name))
            else:
                # Case 2: out of bounds -> connection
                conn = conns.get(dir_name)
                if conn is None:
                    continue

                dest_map_name = conn["map_name"]
                if dest_map_name is None or dest_map_name not in self.maps:
                    continue
                dest_map = self.maps[dest_map_name]
                dw = dest_map["width"]
                dh = dest_map["height"]
                offset = int(conn.get("offset", 0))

                if dir_name == "up":
                    tx = x + offset
                    ty = dh - 1
                elif dir_name == "down":
                    tx = x + offset
                    ty = 0
                elif dir_name == "left":
                    tx = dw - 1
                    ty = y + offset
                else:  # "right"
                    tx = 0
                    ty = y + offset

                if 0 <= tx < dw and 0 <= ty < dh and dest_map["grid"][ty][tx]:
                    res.append(((dest_map_name, tx, ty), dir_name))

        return res

    # ---- BFS generica ----

    def bfs_shortest_path(
        self,
        start: Tuple[str, int, int],
        goal: Tuple[str, int, int],
        blocked: Optional[Set[Tuple[str, int, int]]] = None,
    ) -> Tuple[List[Tuple[str, int, int]], List[str]]:
        """
        Standard BFS on the overworld graph.
        Returns (path_nodes, directions)
        """
        if start is None or goal is None:
            return [], []

        if start[0] not in self.maps or goal[0] not in self.maps:
            print(f"[path] ERRORE: mappa sconosciuta: start={start[0]} goal={goal[0]}")
            return [], []

        if blocked is None:
            blocked = set()

        q: Deque[Tuple[str, int, int]] = deque()
        came_from: Dict[Tuple[str, int, int], Optional[Tuple[str, int, int]]] = {}
        move_from: Dict[Tuple[str, int, int], Optional[str]] = {}

        q.append(start)
        came_from[start] = None
        move_from[start] = None

        visited = 0

        while q:
            cur = q.popleft()
            visited += 1
            if visited > MAX_BFS_NODES:
                print("[path] BFS abortita: troppi nodi esplorati.")
                break

            if cur == goal:
                break

            for nxt, dir_name in self.neighbors(cur):
                if (nxt[0], nxt[1], nxt[2]) in blocked:
                    continue
                if nxt not in came_from:
                    came_from[nxt] = cur
                    move_from[nxt] = dir_name
                    q.append(nxt)

        if goal not in came_from:
            print("[path] Nessun path trovato.")
            return [], []

        path: List[Tuple[str, int, int]] = []
        node = goal
        while node is not None:
            path.append(node)
            node = came_from[node]
        path.reverse()

        dirs: List[str] = []
        for i in range(1, len(path)):
            dirs.append(move_from[path[i]] or "up")

        print(
            f"[path] Trovato path di {len(path) - 1} passi "
            f"(nodi visitati: {visited})."
        )
        return path, dirs

    # ---- BFS verso PokéCenter più vicino ----

    def is_pokecenter_goal(self, node: Tuple[str, int, int]) -> bool:
        map_name, x, y = node
        return "PokemonCenter_1F" in map_name

    def bfs_to_nearest_pokecenter(
        self,
        start: Tuple[str, int, int],
        blocked: Optional[Set[Tuple[str, int, int]]] = None,
    ) -> Tuple[List[Tuple[str, int, int]], List[str]]:
        """
        BFS non pesata verso qualunque mappa *_PokemonCenter_1F.
        """
        if start is None:
            return [], []

        start_map, sx, sy = start
        if start_map not in self.maps:
            print(f"[path] ERRORE: mappa sconosciuta: {start_map}")
            return [], []

        if blocked is None:
            blocked = set()

        q: Deque[Tuple[str, int, int]] = deque()
        came_from: Dict[Tuple[str, int, int], Optional[Tuple[str, int, int]]] = {}
        move_from: Dict[Tuple[str, int, int], Optional[str]] = {}

        q.append(start)
        came_from[start] = None
        move_from[start] = None

        visited = 0
        goal: Optional[Tuple[str, int, int]] = None

        while q:
            cur = q.popleft()
            visited += 1
            if visited > MAX_BFS_NODES:
                print("[path] BFS PokéCenter abortita: troppi nodi.")
                break

            if self.is_pokecenter_goal(cur):
                goal = cur
                break

            for nxt, dir_name in self.neighbors(cur):
                if (nxt[0], nxt[1], nxt[2]) in blocked:
                    continue
                if nxt not in came_from:
                    came_from[nxt] = cur
                    move_from[nxt] = dir_name
                    q.append(nxt)

        if goal is None:
            print("[path] Nessun PokéCenter raggiungibile.")
            return [], []

        path: List[Tuple[str, int, int]] = []
        node = goal
        while node is not None:
            path.append(node)
            node = came_from[node]
        path.reverse()

        dirs: List[str] = []
        for i in range(1, len(path)):
            dirs.append(move_from[path[i]] or "up")

        print(
            f"[path] PokéCenter più vicino trovato in {len(path) - 1} passi "
            f"(nodi visitati: {visited})."
        )
        return path, dirs


# =========================
# NETWORK / STATE FROM LUA
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
    map_name: Optional[str] = None
    nearest_semantic_key: Optional[str] = None
    nearest_semantic_desc: Optional[str] = None
    nearest_semantic_type: Optional[str] = None
    nearest_semantic_region: Optional[str] = None

    @property
    def hp_ratio(self) -> float:
        if self.max_hp <= 0:
            return 1.0
        return self.hp / self.max_hp


class LuaBridge:
    """
    Handles the TCP link to emerald_bridge_v4.lua.

    - Receives JSON snapshots with mode/map/party.
    - Maintains the latest GameState (thread-safe).
    - Sends input commands like "UP:15" or "A:4".
    """

    def __init__(self, world: World):
        self.world = world
        self.sock: Optional[socket.socket] = None
        self.sock_file = None
        self.latest_state: Optional[GameState] = None
        self.frame_counter = 0
        self._lock = threading.Lock()
        self._running = False

    # --- connection ---

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(1)
        print(f"[net] In attesa di mGBA su {HOST}:{PORT} ...")
        conn, addr = srv.accept()
        print(f"[net] GBA connesso da {addr}.")
        self.sock = conn
        self.sock_file = conn.makefile("rwb")
        self._running = True

        t = threading.Thread(target=self._reader_loop, daemon=True)
        t.start()

    def _reader_loop(self):
        while self._running:
            try:
                line = self.sock_file.readline()
                if not line:
                    print("[net] Connessione chiusa dal GBA.")
                    self._running = False
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                self._handle_snapshot_line(line)
            except Exception as e:
                print(f"[net] Errore nella lettura: {e}")
                self._running = False
                break

    def _handle_snapshot_line(self, line: str):
        try:
            raw = json.loads(line)
        except Exception as e:
            print(f"[net] JSON snapshot non valido: {e} | line={line!r}")
            return

        mode = raw.get("mode", "OVERWORLD")
        m = raw.get("map") or {}
        p = raw.get("party") or {}

        self.frame_counter += 1
        group = int(m.get("group", 0))
        num = int(m.get("num", 0))
        x = int(m.get("x", 0))
        y = int(m.get("y", 0))
        hp = int(p.get("hp", 0))
        max_hp = int(p.get("max_hp", 1))

        map_name = self.world.map_name_from_group_num(group, num)
        sem_key, sem_loc, _ = self.world.nearest_semantic_location(group, num, x, y)

        gs = GameState(
            frame=self.frame_counter,
            mode=mode,
            map_group=group,
            map_num=num,
            x=x,
            y=y,
            hp=hp,
            max_hp=max_hp,
            map_name=map_name,
            nearest_semantic_key=sem_key,
            nearest_semantic_desc=(sem_loc or {}).get("desc") if sem_loc else None,
            nearest_semantic_type=(sem_loc or {}).get("type") if sem_loc else None,
            nearest_semantic_region=(sem_loc or {}).get("region") if sem_loc else None,
        )

        with self._lock:
            self.latest_state = gs

    # --- public API ---

    def snapshot(self) -> Optional[GameState]:
        with self._lock:
            if self.latest_state is None:
                return None
            return self.latest_state

    def wait_for_new_state(self, prev_frame: int, timeout: float = 1.0) -> Optional[GameState]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.snapshot()
            if st and st.frame > prev_frame:
                return st
            time.sleep(0.02)
        return self.snapshot()

    def send_input(self, key: str, hold_frames: int):
        """
        key: 'UP','DOWN','LEFT','RIGHT','A','B','START','SELECT'
        """
        if not self.sock_file:
            return
        line = f"{key}:{int(hold_frames)}\n"
        try:
            self.sock_file.write(line.encode("utf-8"))
            self.sock_file.flush()
            print(f"[input] {line.strip()}")
        except Exception as e:
            print(f"[net] Errore nell'invio input: {e}")


# =========================
# DECISION SCHEMA + FEEDBACK
# =========================


@dataclass
class Decision:
    action: str  # MOVE | PRESS | EXPLORE | WAIT
    target: Optional[str] = None  # semantic location key for MOVE/EXPLORE
    button: Optional[str] = None  # A/B/START/SELECT/UP/DOWN/LEFT/RIGHT
    policy: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_llm_payload(payload: Any) -> "Decision":
        """
        Runtime schema validation + normalization.
        Accepts:
        - dict
        - JSON string
        """
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                raise ValueError("Decision JSON string non valido")

        if not isinstance(payload, dict):
            raise ValueError("Decision deve essere un oggetto JSON")

        action = str(payload.get("action", "")).upper().strip()
        if action not in {"MOVE", "PRESS", "EXPLORE", "WAIT"}:
            action = "WAIT"

        target = payload.get("target")
        if target is not None:
            target = str(target).strip()
            if target == "":
                target = None

        button = payload.get("button")
        if button is not None:
            button = str(button).upper().strip()
            if button == "":
                button = None

        policy = payload.get("policy") or {}
        if not isinstance(policy, dict):
            policy = {}

        # If not PRESS, ignore button
        if action != "PRESS":
            button = None

        return Decision(action=action, target=target, button=button, policy=policy)


@dataclass
class Feedback:
    outcome: str  # e.g. "OK_PROGRESS", "NO_PROGRESS_HEALTHY", "NO_HEAL_WHEN_LOW_HP", "BATTLE_LOST"
    comment: str


class FeedbackEngine:
    LOW_HP_THRESHOLD = 0.3
    HEALTHY_HP_THRESHOLD = 0.7

    def __init__(self):
        self.last_feedback: Optional[Feedback] = None

    def compute(
        self,
        prev_state: Optional[GameState],
        new_state: Optional[GameState],
        decision: Optional[Decision],
    ) -> Optional[Feedback]:
        if prev_state is None or new_state is None or decision is None:
            return None

        # BATTLE -> OVERWORLD with HP = 0 => assume loss
        if prev_state.mode == "BATTLE" and new_state.mode == "OVERWORLD":
            if new_state.hp <= 0:
                fb = Feedback(
                    outcome="BATTLE_LOST",
                    comment="Hai perso la lotta: HP del Pokémon attivo a 0 dopo la battaglia.",
                )
                self.last_feedback = fb
                return fb

        # OVERWORLD -> OVERWORLD: check movement and healing
        if prev_state.mode == "OVERWORLD" and new_state.mode == "OVERWORLD":
            moved = (
                (prev_state.map_group != new_state.map_group)
                or (prev_state.map_num != new_state.map_num)
                or (prev_state.x != new_state.x)
                or (prev_state.y != new_state.y)
            )

            hp_ratio = new_state.hp_ratio

            if hp_ratio >= self.HEALTHY_HP_THRESHOLD:
                if not moved:
                    fb = Feedback(
                        outcome="NO_PROGRESS_HEALTHY",
                        comment="La squadra è in buona salute ma non ti sei mosso dopo la decisione.",
                    )
                    self.last_feedback = fb
                    return fb
                else:
                    fb = Feedback(
                        outcome="OK_PROGRESS",
                        comment="Squadra sana e hai effettuato progresso nell'overworld.",
                    )
                    self.last_feedback = fb
                    return fb

            if hp_ratio <= self.LOW_HP_THRESHOLD:
                if decision.action in {"MOVE", "EXPLORE"}:
                    fb = Feedback(
                        outcome="NO_HEAL_WHEN_LOW_HP",
                        comment="HP bassi: sarebbe prudente dirigersi verso un Centro Pokémon.",
                    )
                    self.last_feedback = fb
                    return fb

        fb = Feedback(
            outcome="NEUTRAL",
            comment="Nessun effetto evidente (né molto positivo né molto negativo).",
        )
        self.last_feedback = fb
        return fb


# =========================
# BRAIN / LLM CLIENT
# =========================


class Brain:
    def __init__(self):
        self.last_decision_time = 0.0
        self.extra_context: Optional[str] = self._load_readme_context()

    @staticmethod
    def _load_readme_context() -> Optional[str]:
        for p in README_CANDIDATES:
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        text = f.read().strip()
                    print(f"[brain] Caricato README di contesto da {p}.")
                    return text
                except Exception as e:
                    print(f"[brain] Impossibile leggere {p}: {e}")
        print("[brain] Nessun README di contesto trovato (ok, opzionale).")
        return None

    def can_query(self) -> bool:
        return (time.time() - self.last_decision_time) >= LLM_DECISION_COOLDOWN

    def _build_prompt(
        self,
        state: GameState,
        feedback: Optional[Feedback],
        story_index: int,
        story_path: List[str],
    ) -> str:
        lines: List[str] = []
        lines.append("You are an AI playing Pokémon Emerald. You control only HIGH-LEVEL decisions.")
        lines.append("")
        lines.append("CURRENT STATE:")
        lines.append(f"- Map position: group={state.map_group}, num={state.map_num}, x={state.x}, y={state.y}")
        lines.append(f"- Nearest semantic location key: {state.nearest_semantic_key}")
        lines.append(f"- Mode: {state.mode}")
        lines.append(f"- Active Pokémon HP: {state.hp}/{state.max_hp} (~{int(state.hp_ratio * 100)}%)")
        if state.nearest_semantic_region:
            lines.append(f"- Region: {state.nearest_semantic_region} (semantic type={state.nearest_semantic_type})")
        lines.append("")
        lines.append("STORY PROGRESSION:")
        lines.append(f"- Current story step index: {story_index}")
        if 0 <= story_index < len(story_path):
            lines.append(f"- Current story location key: {story_path[story_index]}")
        next_idx = story_index + 1
        if 0 <= next_idx < len(story_path):
            lines.append(f"- Next main story target: {story_path[next_idx]}")
        else:
            lines.append("- Next main story target: NONE (end of defined early-game path).")
        lines.append("")
        if feedback is not None:
            lines.append("LAST ACTION FEEDBACK:")
            lines.append(f"- Outcome: {feedback.outcome}")
            lines.append(f"- Comment: {feedback.comment}")
            lines.append("")
        else:
            lines.append("LAST ACTION FEEDBACK:")
            lines.append("- Outcome: NONE (first decision or no data).")
            lines.append("")
        lines.append("WORLD KNOWLEDGE (compact):")
        lines.append('- Semantic locations are keys like "OLDALE_POKECENTER", "ROUTE_102_MID", "RUSTBORO_GYM".')
        lines.append("- You choose only HIGH-LEVEL actions; low-level steps and timing are handled by the backend.")
        lines.append("- When HP is low, you should MOVE towards a HEAL-type location (Pokémon Center).")
        lines.append("- When HP is healthy, you should progress the early-game story towards Rustboro and its Gym.")
        lines.append("")

        # Optional extended README context (only once)
        if self.extra_context:
            lines.append("ADDITIONAL CONTEXT (game/world/navigation guidelines):")
            lines.append(self.extra_context)
            lines.append("")

        # Very short explicit schema reminder (to avoid 3x repetition)
        lines.append("OUTPUT FORMAT:")
        lines.append("Respond with EXACTLY one JSON object with fields:")
        lines.append('  action: "MOVE" | "PRESS" | "EXPLORE" | "WAIT"')
        lines.append('  target: semantic location key or null')
        lines.append('  button: "A" | "B" | "START" | "SELECT" | null')
        lines.append('  policy: { "avoid_optional_trainers": bool, "allow_grass_encounters": bool }')
        lines.append("No extra text, no comments.")
        return "\n".join(lines)

    def query_llm(
        self,
        state: GameState,
        feedback: Optional[Feedback],
        story_index: int,
        story_path: List[str],
    ) -> Decision:
        prompt = self._build_prompt(state, feedback, story_index, story_path)

        print("\n[PROMPT TO LLM] ------------------")
        print(prompt)
        print("----------------------------------")

        conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=60)
        headers = {"Content-Type": "application/json"}
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
        }

        try:
            conn.request("POST", "/api/generate", body=json.dumps(payload), headers=headers)
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()

        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            print("[ERR] Risposta LLM non in JSON, uso WAIT.")
            self.last_decision_time = time.time()
            return Decision(action="WAIT")

        text = data.get("response", "")
        text_stripped = text.strip()
        print("[RAW LLM RESPONSE] ------------------")
        print(text_stripped)
        print("-------------------------------------")

        try:
            start = text_stripped.find("{")
            end = text_stripped.rfind("}")
            if start != -1 and end != -1 and end > start:
                decision_obj = json.loads(text_stripped[start : end + 1])
            else:
                decision_obj = json.loads(text_stripped)
        except Exception:
            print("[ERR] Impossibile parsare il JSON di decisione, uso WAIT.")
            self.last_decision_time = time.time()
            return Decision(action="WAIT")

        try:
            decision = Decision.from_llm_payload(decision_obj)
        except Exception as e:
            print(f"[ERR] Decision non valida: {e}, uso WAIT.")
            decision = Decision(action="WAIT")

        self.last_decision_time = time.time()
        return decision


# =========================
# STORY STATE MACHINE
# =========================


EARLY_GAME_STORY_PATH = [
    "PLAYER_HOME_BEDROOM",
    "BIRCH_LAB",
    "LITTLEROOT_CENTER",
    "ROUTE_101_MID",
    "OLDALE_CENTER",
    "OLDALE_POKECENTER",
    "ROUTE_102_MID",
    "PETALBURG_POKECENTER",
    "RUSTBORO_CENTER",
    "RUSTBORO_POKECENTER",
    "RUSTBORO_GYM",
]


class StoryTracker:
    def __init__(self, story_path: List[str]):
        self.story_path = story_path
        self.story_index = 0

    def update_with_state(self, state: GameState):
        key = state.nearest_semantic_key
        if key is None:
            return
        try:
            idx = self.story_path.index(key)
        except ValueError:
            return
        if idx > self.story_index:
            self.story_index = idx

    def get_current(self) -> Tuple[int, Optional[str]]:
        if 0 <= self.story_index < len(self.story_path):
            return self.story_index, self.story_path[self.story_index]
        return self.story_index, None

    def get_next(self) -> Optional[str]:
        nxt = self.story_index + 1
        if 0 <= nxt < len(self.story_path):
            return self.story_path[nxt]
        return None


# =========================
# AGENT SYSTEM
# =========================


class AgentSystem:
    def __init__(self):
        self.world = World(OVERWORLD_NAV_JSON, SEMANTIC_LOC_JSON)
        self.lua = LuaBridge(self.world)
        self.brain = Brain()
        self.feedback_engine = FeedbackEngine()
        self.story = StoryTracker(EARLY_GAME_STORY_PATH)

        self.last_state: Optional[GameState] = None
        self.last_decision: Optional[Decision] = None

        # Path currently being executed (directions like "up","down",...)
        self.current_path_dirs: Deque[str] = deque()
        self.current_path_target_key: Optional[str] = None

        # Dynamic blocked tiles (NPCs, moving trainers, etc.)
        self.blocked_tiles: Set[Tuple[str, int, int]] = set()

        # Info about the last move we actually sent
        self.last_move_info: Optional[Dict[str, Any]] = None

    def run(self):
        self.lua.start()

        # Wait for first state
        state = None
        while state is None:
            state = self.lua.snapshot()
            if state is None:
                time.sleep(0.1)

        self.last_state = state
        self.story.update_with_state(state)

        print("[agent] Avvio loop principale AI.")
        prev_frame = state.frame

        while True:
            # 1) Wait for new snapshot
            state = self.lua.wait_for_new_state(prev_frame, timeout=1.0)
            if state is None:
                print("[agent] Nessuno stato disponibile, esco.")
                break

            # 1bis) Evaluate outcome of the last move (for collisions / progress)
            self._evaluate_last_move(state)

            # Update story tracker and feedback
            self.story.update_with_state(state)
            feedback = self.feedback_engine.compute(self.last_state, state, self.last_decision)
            self.last_state = state
            prev_frame = state.frame

            # 2) BATTLE mode: ignore LLM and just mash A
            if state.mode == "BATTLE":
                self._handle_battle_mode()
                continue

            # 3) OVERWORLD: if we have a path, keep following it (no new LLM call)
            if self.current_path_dirs:
                self._step_along_current_path(state)
                continue

            # 4) No path: possibly ask LLM for a NEW high-level decision
            if not self.brain.can_query():
                # Cooldown active -> stand still
                print("[FALLBACK] OVERWORLD: cooldown LLM -> STAND STILL")
                continue

            # Strictly refresh state before querying the LLM (mode sync)
            fresh_state = self.lua.snapshot() or state

            if fresh_state.mode == "BATTLE":
                # Mode changed to battle while we were about to query -> battle logic
                self._handle_battle_mode()
                continue

            self.story.update_with_state(fresh_state)
            story_index, _ = self.story.get_current()

            decision = self.brain.query_llm(
                fresh_state,
                self.feedback_engine.last_feedback,
                story_index,
                self.story.story_path,
            )
            self.last_decision = decision

            # Execute the high-level decision (this may create a path)
            self._execute_high_level_decision(fresh_state, decision)

    # ---- internal helpers ----

    def _handle_battle_mode(self):
        print("[MODE] BATTLE -> auto-press A")
        self.lua.send_input("A", DEFAULT_BUTTON_HOLD_FRAMES)
        # Do NOT clear current_path_dirs; after the battle we can try to resume.

    def _evaluate_last_move(self, state: GameState):
        """
        Compare the new state with the last move we sent.
        If we clearly hit a collision on a predicted tile in OVERWORLD,
        mark that tile as blocked and clear the current path so it will be replanned.
        """
        if self.last_move_info is None:
            return

        lm = self.last_move_info
        # Ensure we have advanced at least one frame
        if state.frame <= lm["frame"]:
            return

        # If map changed (warp/connection), we consider the move successful.
        if state.map_group != lm["map_group"] or state.map_num != lm["map_num"]:
            print(
                f"[path] Warp/connection: map ({lm['map_group']},{lm['map_num']}) -> "
                f"({state.map_group},{state.map_num}), pos=({state.x},{state.y})"
            )
            if self.current_path_dirs and self.current_path_dirs[0] == lm["dir_name"]:
                self.current_path_dirs.popleft()
            self.last_move_info = None
            return

        # Same map: check if we reached the expected tile
        px, py = lm["x"], lm["y"]
        dx, dy = DIR_TO_DELTA.get(lm["dir_name"], (0, 0))
        expected_x = px + dx
        expected_y = py + dy

        if (state.x, state.y) == (expected_x, expected_y):
            # Movement succeeded
            if self.current_path_dirs and self.current_path_dirs[0] == lm["dir_name"]:
                self.current_path_dirs.popleft()
            self.last_move_info = None
            return

        # Neither warp/connection nor correct step: treat as collision
        blocked_tile = (lm["map_name"], expected_x, expected_y)
        print(
            f"[path] Collisione o blocco su {blocked_tile}, nuovo stato=({state.map_name},{state.x},{state.y})."
        )
        self.blocked_tiles.add(blocked_tile)
        # Drop current path so that next decision can replan
        self.current_path_dirs.clear()
        self.last_move_info = None

    def _step_along_current_path(self, state: GameState):
        """
        Take one step along the current path: send the D-Pad input that corresponds
        to the next direction in the queue and remember what we attempted.
        """
        if not self.current_path_dirs:
            return

        dir_name = self.current_path_dirs[0]
        btn = DIR_TO_BUTTON.get(dir_name)
        if btn is None:
            print(f"[path] Direzione sconosciuta nel path: {dir_name}, scarto.")
            self.current_path_dirs.popleft()
            return

        print(f"[ACT] Follow path: {dir_name}")
        # Remember what we are trying to do
        self.last_move_info = {
            "frame": state.frame,
            "map_group": state.map_group,
            "map_num": state.map_num,
            "map_name": state.map_name,
            "x": state.x,
            "y": state.y,
            "dir_name": dir_name,
        }
        self.lua.send_input(btn, DEFAULT_MOVE_HOLD_FRAMES)

    def _execute_high_level_decision(self, state: GameState, decision: Decision):
        print(
            f"[DECISION] action={decision.action}, "
            f"target={decision.target}, button={decision.button}, policy={decision.policy}"
        )

        if state.mode == "BATTLE":
            self._handle_battle_mode()
            return

        if decision.action == "PRESS":
            btn = decision.button or "A"
            self.lua.send_input(btn, DEFAULT_BUTTON_HOLD_FRAMES)
            # Press decisions do not affect path
            return

        if decision.action in {"MOVE", "EXPLORE"}:
            self._plan_path_for_move(state, decision)
            # The actual step will be taken on the next loop iteration
            return

        if decision.action == "WAIT":
            print("[ACT] WAIT / STAND STILL")
            return

        print("[ACT] Azione sconosciuta, STAND STILL")

    def _plan_path_for_move(self, state: GameState, decision: Decision):
        """
        Compute a BFS path for a MOVE / EXPLORE decision.

        - MOVE with a valid target key:
            * try BFS to that semantic location
            * if unreachable and the target is HEAL, fallback to nearest reachable PokéCenter
        - EXPLORE or MOVE with invalid target:
            * do a very short local exploration step without BFS
        """
        # Reset previous path info
        self.current_path_dirs.clear()
        self.last_move_info = None

        start_map_name = state.map_name
        if start_map_name is None:
            print("[path] Nessun map_name nello stato corrente, non posso pianificare.")
            return
        start_node = (start_map_name, state.x, state.y)

        target_key = decision.target

        # Case 1: EXPLORE or MOVE with no target -> local exploration
        if decision.action == "EXPLORE" or not target_key:
            print("[path] EXPLORE/MOVE senza target valido, uso fallback locale (UP).")
            self.current_path_dirs.append("up")
            self.current_path_target_key = None
            return

        # Case 2: MOVE with target key: try semantic lookup
        goal_node = self.world.semantic_node_from_key(target_key)

        path_dirs: List[str] = []
        used_fallback = False

        if goal_node is not None:
            print(f"[path] Pianifico path da {start_node} a {goal_node} (target={target_key}).")
            path, dirs = self.world.bfs_shortest_path(start_node, goal_node, blocked=self.blocked_tiles)
            path_dirs = dirs

        # If direct path failed and it's a HEAL target, fallback to nearest PokéCenter
        if not path_dirs:
            loc_info = self.world.semantic.get(target_key)
            if loc_info and loc_info.get("type") == "HEAL":
                print(
                    "[path] Target HEAL non raggiungibile, provo PokéCenter "
                    "più vicino come fallback."
                )
                path, dirs = self.world.bfs_to_nearest_pokecenter(start_node, blocked=self.blocked_tiles)
                path_dirs = dirs
                used_fallback = bool(dirs)

        if not path_dirs:
            print("[path] Nessun percorso trovato (nemmeno verso PokéCenter vicino), fallback locale (UP).")
            self.current_path_target_key = None
            self.current_path_dirs.append("up")
            return

        print(f"[path] Path con {len(path_dirs)} mosse. Prime 20: {path_dirs[:20]}")
        self.current_path_dirs = deque(path_dirs)
        # Target key rimane l'originale (anche se abbiamo usato fallback); è solo informativo
        self.current_path_target_key = target_key
        if used_fallback:
            print("[path] NOTE: in realtà il path va al PokéCenter più vicino, non al target HEAL specifico.")

# =========================
# MAIN
# =========================


def main():
    agent = AgentSystem()
    agent.run()


if __name__ == "__main__":
    main()
