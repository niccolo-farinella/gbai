#!/usr/bin/env python3
import socket
import json
import threading
import time
import os
import sys
from collections import deque

# =========================
# CONFIG
# =========================

HOST = "127.0.0.1"
PORT = 8765

WORLD_JSON = "overworld_nav.json"

# Quanto tempo tenere premuto un tasto (in frame) lato emulatore
DEFAULT_HOLD_FRAMES = 15

# Pausa (in secondi) tra un comando e l'altro nella demo
STEP_INTERVAL_SECONDS = 1.0

DIR_TO_DELTA = {
    "up":    (0, -1),
    "down":  (0,  1),
    "left":  (-1, 0),
    "right": (1,  0),
}


# =========================
# WORLD / PATHFINDING
# =========================

class World:
    def __init__(self, json_path: str):
        if not os.path.exists(json_path):
            print(f"[world] ERRORE: file {json_path} non trovato.")
            sys.exit(1)

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.meta = data.get("meta", {})
        self.maps = data["maps"]
        self.index = data["index"]

        # (group, num) -> map_name
        self.by_group_num = {}
        for entry in self.index:
            g = entry["group"]
            n = entry["num"]
            name = entry["map_name"]
            self.by_group_num[(g, n)] = name

        # map_id -> map_name
        self.by_map_id = {}
        for name, m in self.maps.items():
            self.by_map_id[m["map_id"]] = name

        # tutte le mappe Pokemon Center 1F
        self.pokecenter_maps = [
            name for name in self.maps.keys()
            if "PokemonCenter_1F" in name
        ]

        print(f"[world] Caricate {len(self.maps)} mappe.")
        print(f"[world] Rilevati {len(self.pokecenter_maps)} Pokémon Center 1F.")

    def map_name_from_group_num(self, group: int, num: int):
        return self.by_group_num.get((group, num))

    def get_map(self, map_name: str):
        return self.maps[map_name]

    def neighbors(self, node):
        """
        Node = (map_name, x, y)
        Ritorna lista di (next_node, dir_name) dove dir_name in {up,down,left,right}.
        """
        map_name, x, y = node
        m = self.maps[map_name]
        w = m["width"]
        h = m["height"]
        grid = m["grid"]
        conns = {c["direction"]: c for c in m.get("connections", [])}
        warps = m.get("warp_events", [])

        res = []

        directions = [
            ("up",    (0, -1)),
            ("down",  (0,  1)),
            ("left",  (-1, 0)),
            ("right", (1,  0)),
        ]

        for dir_name, (dx, dy) in directions:
            nx = x + dx
            ny = y + dy

            # Caso 1: nuovo tile dentro i bounds della mappa corrente
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
                        # warp verso mappa non esportata
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
                                continue  # non aggiungiamo il passo normale

                # Nessun warp: movimento normale
                if grid[ny][nx]:
                    res.append(((map_name, nx, ny), dir_name))

            else:
                # Caso 2: fuori dai bounds -> connection (bordi tra mappe)
                conn = conns.get(dir_name)
                if conn is None:
                    continue

                dest_map_name = conn["map_name"]
                if dest_map_name is None:
                    continue
                dest_map = self.maps[dest_map_name]
                dw = dest_map["width"]
                dh = dest_map["height"]
                offset = conn["offset"]

                if dir_name == "up":
                    tx = x + offset
                    ty = dh - 1
                elif dir_name == "down":
                    tx = x + offset
                    ty = 0
                elif dir_name == "left":
                    tx = dw - 1
                    ty = y + offset
                else:  # right
                    tx = 0
                    ty = y + offset

                if 0 <= tx < dw and 0 <= ty < dh and dest_map["grid"][ty][tx]:
                    res.append(((dest_map_name, tx, ty), dir_name))

        return res

    def is_pokecenter_goal(self, node):
        map_name, x, y = node
        return "PokemonCenter_1F" in map_name

    def bfs_to_nearest_pokecenter(self, start, blocked=None):
        """
        BFS non pesata verso qualunque mappa *_PokemonCenter_1F.
        start = (map_name, x, y)
        blocked: set di (map_name, x, y) da trattare come non attraversabili (NPC, ostacoli dinamici).
        """
        if start is None:
            return None, None

        start_map, sx, sy = start
        if start_map not in self.maps:
            print(f"[path] ERRORE: mappa sconosciuta: {start_map}")
            return None, None

        if blocked is None:
            blocked = set()

        q = deque()
        came_from = {}
        move_from = {}

        q.append(start)
        came_from[start] = None
        move_from[start] = None

        visited = 0

        while q:
            cur = q.popleft()
            visited += 1

            if self.is_pokecenter_goal(cur):
                path = []
                node = cur
                while node is not None:
                    path.append(node)
                    node = came_from[node]
                path.reverse()

                dirs = []
                for i in range(1, len(path)):
                    dirs.append(move_from[path[i]])

                print(f"[path] Trovato Pokémon Center in {len(path) - 1} passi, nodi visitati: {visited}")
                return path, dirs

            for nxt, dir_name in self.neighbors(cur):
                if (nxt[0], nxt[1], nxt[2]) in blocked:
                    continue
                if nxt not in came_from:
                    came_from[nxt] = cur
                    move_from[nxt] = dir_name
                    q.append(nxt)

        print("[path] Nessun Pokémon Center raggiungibile dal punto corrente (con i blocchi attuali).")
        return None, None


# =========================
# BACKEND / NETWORK
# =========================

class Backend:
    def __init__(self, world: World):
        self.world = world
        self.sock = None
        self.sock_file = None
        self.lock = threading.Lock()

        self.latest_snapshot = None   # ultima riga JSON grezza
        self.latest_state = None      # dict con campi decodificati utili

        self.running = True

        # Tiles dinamicamente bloccati perché hanno causato collisioni
        # elementi: (map_name, x, y)
        self.blocked_tiles = set()

    # ---- networking ----

    def start_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((HOST, PORT))
        srv.listen(1)
        print(f"[backend] Listening on {HOST}:{PORT} ...")

        conn, addr = srv.accept()
        print(f"[backend] Connection from {addr}")
        self.sock = conn
        self.sock_file = conn.makefile("rwb")

        t = threading.Thread(target=self._reader_loop, daemon=True)
        t.start()

        self._command_loop()

        self.running = False
        try:
            self.sock_file.close()
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
        try:
            srv.close()
        except Exception:
            pass

    def _reader_loop(self):
        while self.running:
            try:
                line = self.sock_file.readline()
                if not line:
                    print("[backend] Connessione chiusa dal client.")
                    break

                line = line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue

                msg = json.loads(line)
                msg_type = msg.get("type")

                if msg_type == "snapshot":
                    self._handle_snapshot(msg)
                else:
                    print(f"[backend] Messaggio non riconosciuto: {msg_type}")

            except Exception as e:
                print(f"[backend] ERRORE in reader_loop: {e}")
                break

        self.running = False

    def _send_json(self, obj):
        if not self.sock_file:
            print("[backend] Nessun client collegato, impossibile inviare input.")
            return
        data = json.dumps(obj).encode("utf-8") + b"\n"
        with self.lock:
            self.sock_file.write(data)
            self.sock_file.flush()

    # ---- snapshot / stato ----

    def _decode_state_from_save1(self, msg):
        """
        Decodifica x, y, map_group, map_num direttamente da save1_hex.

        Emerald (U), gSaveBlock1:
          0x00: u16 xCoord
          0x02: u16 yCoord
          0x04: u8  mapGroup
          0x05: u8  mapNumber
        """
        save_hex = msg.get("save1_hex")
        if not save_hex:
            return None

        try:
            data = bytes.fromhex(save_hex)
        except ValueError:
            return None

        if len(data) < 6:
            return None

        x = int.from_bytes(data[0:2], "little", signed=True)
        y = int.from_bytes(data[2:4], "little", signed=True)
        mg = data[4]
        mn = data[5]

        map_name = self.world.map_name_from_group_num(mg, mn)

        return {
            "frame": msg.get("frame", -1),
            "map_group": mg,
            "map_num": mn,
            "map_name": map_name,
            "x": x,
            "y": y,
        }

    def _handle_snapshot(self, msg):
        self.latest_snapshot = msg
        state = self._decode_state_from_save1(msg)
        self.latest_state = state

        if state is not None:
            print(
                f"[snapshot] frame={state['frame']} "
                f"map=({state['map_group']},{state['map_num']})={state['map_name']} "
                f"pos=({state['x']},{state['y']})"
            )
        else:
            print("[snapshot] Nessuno stato valido estratto da snapshot.")

    # ---- API per comandi ----

    def print_state(self):
        if not self.latest_state:
            print("[state] Nessuno snapshot valido ancora ricevuto.")
            return
        st = self.latest_state
        print(
            f"[state] frame={st['frame']} "
            f"map=({st['map_group']},{st['map_num']})={st['map_name']} "
            f"pos=({st['x']},{st['y']})"
        )

    def send_dpad(self, direction: str, hold_frames: int = DEFAULT_HOLD_FRAMES):
        """
        direction: 'up' | 'down' | 'left' | 'right'
        """
        msg = {
            "type": "input",
            "buttons": [direction],
            "hold_frames": hold_frames,
        }
        print(f"[input] {direction} (hold={hold_frames})")
        self._send_json(msg)

    def _wait_for_next_state(self, prev_frame, timeout=STEP_INTERVAL_SECONDS):
        """
        Aspetta un nuovo snapshot (frame > prev_frame) fino a timeout.
        Ritorna latest_state (anche se non è avanzato) alla fine.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.latest_state
            if st and st["frame"] > prev_frame:
                return st
            time.sleep(0.05)
        return self.latest_state

    def run_demo_to_nearest_pokecenter(self):
        """
        Usa lo stato corrente (mappa + coordinate) per pianificare
        un path verso il primo Pokémon Center raggiungibile, con gestione
        di collisioni dinamiche (NPC che ti bloccano).
        """
        if not self.latest_state:
            print("[demo] Nessuno stato disponibile. Aspetta uno snapshot e riprova.")
            return

        # puliamo i blocchi dinamici a ogni nuova demo
        self.blocked_tiles.clear()

        max_replans = 10
        replans = 0

        while replans <= max_replans:
            st = self.latest_state
            if not st or st["map_name"] is None:
                print("[demo] Stato corrente non valido, annullo.")
                return

            start = (st["map_name"], st["x"], st["y"])
            print(f"[demo] Ricalcolo path da {start}, blocchi dinamici: {len(self.blocked_tiles)}")

            path, dirs = self.world.bfs_to_nearest_pokecenter(start, blocked=self.blocked_tiles)
            if not path:
                print("[demo] Nessun Pokémon Center trovato con i blocchi attuali, annullo.")
                return

            print(f"[demo] Path con {len(dirs)} mosse.")
            preview = 20
            print(f"[demo] Prime {min(preview, len(dirs))} mosse: {dirs[:preview]}")

            blocked_during_run = False

            for i, d in enumerate(dirs, start=1):
                if d not in DIR_TO_DELTA:
                    print(f"[demo] Direzione sconosciuta: {d}, interrompo.")
                    return

                prev = self.latest_state
                if not prev:
                    print("[demo] Nessuno stato prima di inviare il comando, interrompo.")
                    return

                prev_map = prev["map_name"]
                px, py = prev["x"], prev["y"]
                prev_frame = prev["frame"]
                dx, dy = DIR_TO_DELTA[d]
                target_tile = (prev_map, px + dx, py + dy)

                print(f"[demo] Step {i}/{len(dirs)}: {d} verso {target_tile}")
                self.send_dpad(d, DEFAULT_HOLD_FRAMES)

                # aspetta nuovo stato
                new_state = self._wait_for_next_state(prev_frame)
                if not new_state:
                    print("[demo] Nessuno stato dopo il comando, interrompo.")
                    return

                # se è cambiata la mappa (warp/connection) consideriamo valido e andiamo avanti
                if new_state["map_name"] != prev_map:
                    print(
                        f"[demo] Warp/connection: {prev_map} -> {new_state['map_name']} "
                        f"pos=({new_state['x']},{new_state['y']})"
                    )
                    continue

                # stessa mappa: controlliamo se il movimento è riuscito
                nx, ny = new_state["x"], new_state["y"]
                if (nx, ny) == (px + dx, py + dy):
                    # movimento come previsto
                    continue

                # se siamo ancora fermi o in una posizione diversa da quella attesa,
                # assumiamo collisione/blocco
                print(
                    f"[demo] Collisione o blocco su {target_tile}, "
                    f"nuovo stato=({new_state['map_name']},{nx},{ny})."
                )
                self.blocked_tiles.add(target_tile)
                replans += 1
                blocked_during_run = True
                break  # esce dal for, si ricalcola il path dal nuovo stato

            if not blocked_during_run:
                print("[demo] Sequenza completata senza ulteriori blocchi.")
                return

        print("[demo] Troppi ricalcoli, mi fermo per evitare loop infiniti.")

    # ---- CLI ----

    def _command_loop(self):
        print("Comandi disponibili:")
        print("  state  - mostra mappa e coordinate correnti (dall'ultimo snapshot)")
        print("  demo   - vai al Pokémon Center 1F più vicino")
        print("  quit   - esci")

        while self.running:
            try:
                cmd = input("backend> ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                cmd = "quit"

            if cmd == "state":
                self.print_state()
            elif cmd == "demo":
                self.run_demo_to_nearest_pokecenter()
            elif cmd in ("quit", "exit"):
                print("[backend] Uscita richiesta.")
                break
            elif cmd == "":
                continue
            else:
                print(f"[backend] Comando sconosciuto: {cmd}")


# =========================
# MAIN
# =========================

def main():
    world = World(WORLD_JSON)
    backend = Backend(world)
    backend.start_server()


if __name__ == "__main__":
    main()
