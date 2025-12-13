import json

with open("overworld_nav.json", "r", encoding="utf-8") as f:
    nav = json.load(f)

for map_name, mp in nav["maps"].items():
    grid = mp.get("grid")
    if not grid:
        continue
    h = len(grid)
    w = len(grid[0]) if h else 0
    for we in mp.get("warp_events") or []:
        x, y = int(we["x"]), int(we["y"])
        if 0 <= x < w and 0 <= y < h:
            grid[y][x] = True

with open("overworld_nav_patched.json", "w", encoding="utf-8") as f:
    json.dump(nav, f)