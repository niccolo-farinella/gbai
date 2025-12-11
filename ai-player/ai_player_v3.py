#!/usr/bin/env python3
import socket
import json
import time
import urllib.request
import urllib.error
import sys
import os
from collections import deque
import importlib.util
from pathlib import Path

# ================= CONFIG =================
HOST = "127.0.0.1"
PORT = 8765

WORLD_FILE     = "overworld-json/overworld_nav.json"
METADATA_FILE  = "overworld-json/overworld_metadata.json"
SEMANTIC_FILE  = "overworld-semantic/semantic_locations.json"

OLLAMA_URL   = "http://85.215.53.215:61434/api/generate"
OLLAMA_MODEL = "llama3.2:3b-instruct-q8_0"
# =========================================

# Carichiamo World dinamicamente da backend-movement_v4.py senza richiedere rename
BACKEND_PATH = Path(__file__).with_name("backend-movement_v4.py")
spec = importlib.util.spec_from_file_location("backend_movement_v4", BACKEND_PATH)
backend_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend_mod)
World = backend_mod.World

DIR_NAME_TO_KEY = {
    "up": "UP",
    "down": "DOWN",
    "left": "LEFT",
    "right": "RIGHT",
}
KEY_TO_DELTA = {
    "UP":    (0, -1),
    "DOWN":  (0,  1),
    "LEFT":  (-1, 0),
    "RIGHT": (1,  0),
}


class Navigator:
    """
    Usa World (overworld_nav) per pianificare path cross-map e gestisce i blocchi dinamici
    (NPC in movimento, trainer che si spostano, ecc.).
    """
    def __init__(self, world: World):
        self.world = world
        self.blocked_tiles = set()  # (map_name, x, y)
        self.current_dirs = []      # lista di dir_name: "up","down",...

    # --------- BFS generica verso un target arbitrario ---------

    def _bfs_to_target(self, start_node, is_goal_fn):
        from collections import deque

        if start_node is None:
            return None, None

        if start_node[0] not in self.world.maps:
            print(f"[path] mappa sconosciuta: {start_node[0]}")
            return None, None

        q = deque()
        came_from = {}
        move_from = {}

        q.append(start_node)
        came_from[start_node] = None
        move_from[start_node] = None

        visited = 0

        while q:
            cur = q.popleft()
            visited += 1

            if is_goal_fn(cur):
                path = []
                node = cur
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                path.reverse()

                dirs = []
                for i in range(1, len(path)):
                    dirs.append(move_from[path[i]])

                print(f"[path] Trovato target in {len(path) - 1} passi, nodi visitati: {visited}")
                return path, dirs

            for nxt, dir_name in self.world.neighbors(cur):
                if (nxt[0], nxt[1], nxt[2]) in self.blocked_tiles:
                    continue
                if nxt not in came_from:
                    came_from[nxt] = cur
                    move_from[nxt] = dir_name
                    q.append(nxt)

        print("[path] Nessun target raggiungibile (con i blocchi attuali).")
        return None, None

    def plan_to_absolute(self, start_map_name, sx, sy, dest_map_name, dx, dy):
        """
        Pianifica un path globale verso (dest_map_name, dx, dy).
        """
        start = (start_map_name, sx, sy)

        def is_goal(node):
            m, x, y = node
            return (m == dest_map_name) and (x == dx) and (y == dy)

        path, dirs = self._bfs_to_target(start, is_goal)
        self.current_dirs = dirs or []
        print(f"[NAV] plan_to_absolute: {len(self.current_dirs)} mosse.")
        return self.current_dirs

    def clear_path(self):
        self.current_dirs = []

    # --------- Esecuzione step-by-step con gestione collisioni ---------

    def step_along_path(self, agent) -> bool:
        """
        Esegue UN passo del path corrente usando l'AgentSystem (che conosce socket e latest_state).
        Ritorna:
          - True se il passo è stato eseguito correttamente o warp
          - False se c'è stata collisione (richiede ricalcolo path esterno)
        """
        if not self.current_dirs:
            return True  # niente da fare

        dir_name = self.current_dirs.pop(0)
        key = DIR_NAME_TO_KEY.get(dir_name)
        if not key:
            print(f"[NAV] Direzione sconosciuta nel path: {dir_name}")
            return False

        prev = agent.latest_state
        if not prev:
            print("[NAV] Nessuno stato prima del passo, annullo.")
            return False

        mg = prev["map"]["group"]
        mn = prev["map"]["num"]
        px = prev["map"]["x"]
        py = prev["map"]["y"]

        map_name = agent.world.map_name_from_group_num(mg, mn)
        if map_name is None:
            print(f"[NAV] map_name non risolto per ({mg},{mn}), annullo.")
            return False

        dx, dy = KEY_TO_DELTA[key]
        target_tile = (map_name, px + dx, py + dy)

        print(f"[NAV] Step {dir_name} → {target_tile}")
        agent.send_input(key, agent.default_hold_frames)

        # attendiamo un nuovo stato "stabile" dopo l'input
        deadline = time.time() + agent.step_interval
        new_state = None
        while time.time() < deadline:
            agent.update_state()
            st = agent.latest_state
            if not st:
                time.sleep(0.05)
                continue

            # se qualcosa è cambiato (posizione o mappa), assumiamo nuovo stato
            if (
                st["map"]["group"] != mg
                or st["map"]["num"] != mn
                or st["map"]["x"] != px
                or st["map"]["y"] != py
            ):
                new_state = st
                break

            time.sleep(0.05)

        if not new_state:
            # nessun cambiamento evidente → consideriamo collisione
            print("[NAV] Nessun cambio di stato dopo il movimento → collisione.")
            self.blocked_tiles.add(target_tile)
            return False

        new_mg = new_state["map"]["group"]
        new_mn = new_state["map"]["num"]
        new_x = new_state["map"]["x"]
        new_y = new_state["map"]["y"]
        new_map_name = agent.world.map_name_from_group_num(new_mg, new_mn)

        # warp / connection
        if new_map_name != map_name:
            print(f"[NAV] Warp/connection: {map_name} -> {new_map_name} @ ({new_x},{new_y})")
            return True

        # stesso map, controlliamo se lo step è quello atteso
        if (new_x, new_y) == (px + dx, py + dy):
            return True

        # posizione diversa da quella attesa → collisione/ostacolo dinamico
        print(
            f"[NAV] Collisione dinamica su {target_tile}, nuovo stato=({new_map_name},{new_x},{new_y})"
        )
        self.blocked_tiles.add(target_tile)
        return False


class GameBrain:
    def __init__(self, semantic_path, world: World, metadata_path):
        # semantic
        if not os.path.exists(semantic_path):
            self.knowledge = {"locations": {}, "objectives": []}
        else:
            with open(semantic_path, "r", encoding="utf-8") as f:
                self.knowledge = json.load(f)

        # metadata trainer/NPC
        if os.path.exists(metadata_path):
            with open(metadata_path, "r", encoding="utf-8") as f:
                self.metadata = json.load(f)
        else:
            self.metadata = {"maps": {}}

        self.world = world
        self.last_decision_time = 0.0
        self.cooldown = 2.0

    # ------- helpers semantici -------

    def get_location_name(self, g, n, x, y):
        """
        Prova a mappare (group,num,x,y) a un nome semantico della zona.
        Ritorna la chiave della semantic location o una descrizione generica.
        """
        best_key = None
        best_dist = 9999
        for key, data in self.knowledge.get("locations", {}).items():
            if data.get("map_group") != g or data.get("map_num") != n:
                continue
            dist = abs(data.get("x", 0) - x) + abs(data.get("y", 0) - y)
            if dist < best_dist:
                best_dist = dist
                best_key = key

        if best_key is not None:
            return best_key
        return f"map({g},{n})@({x},{y})"

    def summarize_trainers_here(self, g, n):
        map_name = self.world.map_name_from_group_num(g, n)
        if not map_name:
            return "No metadata."

        mdata = self.metadata.get("maps", {}).get(map_name, {})
        trainers = mdata.get("trainers", [])
        if not trainers:
            return "No visible trainers."

        count = len(trainers)
        max_sight = max(t.get("sight_range", 0) for t in trainers)
        patrols = sum(1 for t in trainers if t.get("movement_pattern") == "patrol")
        return f"{count} trainers (max sight {max_sight}, patrol={patrols})."

    def semantic_locations_summary(self):
        """
        Riduce le semantic locations a qualcosa di digeribile dal modello:
        key -> {type, region, map_group, map_num}
        """
        out = {}
        for key, data in self.knowledge.get("locations", {}).items():
            out[key] = {
                "type": data.get("type", "GENERIC"),
                "region": data.get("region", "UNKNOWN"),
                "map_group": data.get("map_group"),
                "map_num": data.get("map_num"),
            }
        return out

    # ------- chiamata LLM -------

    def ask_ollama(self, state):
        # rate limit
        if time.time() - self.last_decision_time < self.cooldown:
            return None

        g = state["map"]["group"]
        n = state["map"]["num"]
        x = state["map"]["x"]
        y = state["map"]["y"]

        loc_key = self.get_location_name(g, n, x, y)
        trainer_info = self.summarize_trainers_here(g, n)

        loc_summaries = self.semantic_locations_summary()
        objectives = self.knowledge.get("objectives", [])

        hp = state["party"]["hp"]
        mhp = state["party"]["max_hp"]
        hp_pct = 0
        if mhp > 0:
            hp_pct = int(100 * hp / mhp)

        # schema JSON da mostrare al modello
        schema_str = (
            '{\\n'
            '  "action": "MOVE" | "PRESS" | "EXPLORE" | "WAIT",\\n'
            '  "target": "string or null",\\n'
            '  "button": "A" | "B" | "START" | "SELECT" | null,\\n'
            '  "policy": {\\n'
            '    "avoid_optional_trainers": true | false,\\n'
            '    "allow_grass_encounters": true | false\\n'
            '  }\\n'
            '}'
        )

        prompt = f"""You are an AI playing Pokémon Emerald. You control only HIGH-LEVEL decisions.

CURRENT STATE:
- Map position: group={g}, num={n}, x={x}, y={y}
- Nearest semantic location key: {loc_key}
- Mode: {state['mode']}
- Active Pokémon HP: {hp}/{mhp} (~{hp_pct}%)

WORLD KNOWLEDGE:
- Semantic locations (keys and metadata): {json.dumps(loc_summaries, ensure_ascii=False)}
- Current high-level objectives: {json.dumps(objectives, ensure_ascii=False)}
- Local trainers/NPCs: {trainer_info}

DECISION INTERFACE (VERY IMPORTANT):
You must respond with a SINGLE JSON object (no surrounding text) with this exact schema:
{schema_str}

GUIDELINES:
- If a dialog box or battle requires confirmation, use:
    {{ "action": "PRESS", "target": null, "button": "A", "policy": {{...}} }}
- If HP% < 30, strongly prefer:
    {{ "action": "MOVE", "target": <a location key whose type is \\"HEAL\\">, "button": null, ... }}
- To progress story (early game), prefer locations with type "GYM", "LAB", or "CITY"/"TOWN".
- EXPLORE is for local exploration when you have no urgent need to heal or progress story.
- WAIT is rare: use it only if pressing a button or moving would be clearly harmful.

Remember:
- 'target' MUST be either null or one of the keys in the semantic locations dictionary above.
- Always return VALID JSON only (no comments, no trailing commas)."""

        print(f"\n[PROMPT TO LLM] ------------------\n{prompt}\n----------------------------------")

        try:
            payload = {
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
            }
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as res:
                self.last_decision_time = time.time()
                raw = res.read().decode()
                outer = json.loads(raw)
                return outer.get("response")
        except Exception as e:
            print(f"[ERR] Brain/Ollama: {e}")
            return None


class AgentSystem:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((HOST, PORT))
        self.sock.listen(1)
        self.conn = None

        self.world = World(WORLD_FILE)
        self.navigator = Navigator(self.world)
        self.brain = GameBrain(SEMANTIC_FILE, self.world, METADATA_FILE)

        self.latest_state = None
        self.current_target = None  # chiave semantic_location

        # Parametri di tempo
        self.step_interval = 0.5
        self.default_hold_frames = 14

    # ------- networking -------

    def wait_for_gba(self):
        print(f"In attesa di mGBA su {PORT}...")
        self.conn, _ = self.sock.accept()
        # usiamo non-blocking per leggere periodicamente
        self.conn.setblocking(False)
        print("GBA connesso.")

    def send_input(self, key, frames=10):
        try:
            msg = f"{key}:{frames}\n".encode()
            self.conn.sendall(msg)
        except Exception as e:
            print(f"[ERR] send_input: {e}")

    def update_state(self):
        if not self.conn:
            return
        try:
            data = self.conn.recv(8192).decode()
        except BlockingIOError:
            return
        except Exception:
            return

        for line in data.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                if "map" in s:
                    self.latest_state = s
            except Exception:
                pass

    # ------- mapping semantic target → coordinate assoluta -------

    def resolve_semantic_target(self, key):
        locs = self.brain.knowledge.get("locations", {})
        data = locs.get(key)
        if not data:
            return None

        g = data["map_group"]
        n = data["map_num"]
        x = data.get("x", 0)
        y = data.get("y", 0)
        map_name = self.world.map_name_from_group_num(g, n)
        if not map_name:
            return None

        return (map_name, x, y)

    # ------- loop principale -------

    def run(self):
        self.wait_for_gba()

        while True:
            # Aggiorna stato
            self.update_state()
            if not self.latest_state:
                time.sleep(0.05)
                continue

            st = self.latest_state

            # Se in battle → priorità ai bottoni (si delega tutto al LLM via PRESS)
            if st["mode"] == "BATTLE":
                decision_json = self.brain.ask_ollama(st)
                if decision_json:
                    self._handle_decision(decision_json, battle_mode=True)
                time.sleep(0.1)
                continue

            # 1) se abbiamo un path attivo, cerchiamo di consumarlo
            if self.navigator.current_dirs:
                ok = self.navigator.step_along_path(self)
                if not ok:
                    # collisione: forziamo ricalcolo path sul target attuale
                    self._replan_to_current_target()
                time.sleep(0.05)
                continue

            # 2) nessun path attivo → chiediamo al LLM un nuovo obiettivo
            decision_json = self.brain.ask_ollama(st)
            if decision_json:
                self._handle_decision(decision_json, battle_mode=False)

            time.sleep(0.1)

    # ------- gestione decisione LLM -------

    def _handle_decision(self, decision_json, battle_mode: bool):
        try:
            dec = json.loads(decision_json) if isinstance(decision_json, str) else decision_json
        except Exception as e:
            print(f"[ERR] parsing decision_json: {e}")
            return

        print(f"[LLM] decision: {dec}")

        # estrai policy (non ancora usata nel pathfinding, ma tenuta per futuro)
        policy = dec.get("policy") or {}
        avoid_opt = bool(policy.get("avoid_optional_trainers", True))
        allow_grass = bool(policy.get("allow_grass_encounters", True))
        # per ora li ignoriamo, ma potresti loggarli o usarli in futuro

        # 1) bottoni immediati
        btn = dec.get("button")
        if btn in ["A", "B", "START", "SELECT"]:
            print(f"[ACT] Press {btn}")
            self.send_input(btn, 10)
            return

        action = dec.get("action")

        if battle_mode:
            # in battle ignoriamo MOVE / EXPLORE / WAIT (si ri-chiederà la prossima volta)
            return

        if action == "MOVE":
            target_key = dec.get("target")
            if not target_key:
                print("[NAV] MOVE senza target, ignorato.")
                return

            resolved = self.resolve_semantic_target(target_key)
            if not resolved:
                print(f"[NAV] Target semantico sconosciuto: {target_key}")
                return

            map_name, tx, ty = resolved
            self.current_target = target_key
            # opzionale: puoi decidere se pulire o mantenere i blocchi tra obiettivi diversi
            self.navigator.blocked_tiles.clear()

            g = self.latest_state["map"]["group"]
            n = self.latest_state["map"]["num"]
            sx = self.latest_state["map"]["x"]
            sy = self.latest_state["map"]["y"]
            start_map_name = self.world.map_name_from_group_num(g, n)

            if not start_map_name:
                print(f"[NAV] map_name non risolto per ({g},{n}), impossibile pianificare.")
                return

            self.navigator.plan_to_absolute(start_map_name, sx, sy, map_name, tx, ty)

        elif action == "EXPLORE":
            # per ora: passo random semplice, ma sempre monitorato
            import random
            key = random.choice(["UP", "DOWN", "LEFT", "RIGHT"])
            print(f"[EXPLORE] step casuale: {key}")
            self.send_input(key, self.default_hold_frames)

        elif action == "WAIT":
            print("[ACT] WAIT (nessun input).")
            # niente input, solo pausa

    def _replan_to_current_target(self):
        if not self.current_target or not self.latest_state:
            return

        resolved = self.resolve_semantic_target(self.current_target)
        if not resolved:
            print(f"[NAV] replan: target {self.current_target} non più risolvibile.")
            self.navigator.clear_path()
            return

        map_name, tx, ty = resolved
        g = self.latest_state["map"]["group"]
        n = self.latest_state["map"]["num"]
        sx = self.latest_state["map"]["x"]
        sy = self.latest_state["map"]["y"]
        start_map_name = self.world.map_name_from_group_num(g, n)

        if not start_map_name:
            print(f"[NAV] replan: map_name non risolto per ({g},{n}).")
            self.navigator.clear_path()
            return

        print("[NAV] ricalcolo path per collisione dinamica.")
        self.navigator.plan_to_absolute(start_map_name, sx, sy, map_name, tx, ty)


if __name__ == "__main__":
    AgentSystem().run()
