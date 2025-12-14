# gbAI — Pokémon Emerald AI Player (Agent System v6)

## Overview
This project runs Pokémon Emerald in **mGBA** with a **Lua bridge** that:
1) streams game state snapshots to a Python backend over TCP, and
2) receives controller input commands back from Python and applies them in-game.

The Python backend (`ai_player_v6.py`) orchestrates **two Ollama models**:
- **Strategist**: decides *WHAT* to do next (goal selection, dialog, battle stance).
- **Navigator**: decides *HOW* to move in the overworld by outputting a short micro‑plan of controller inputs.

## Architecture (data flow)
1) `emerald_bridge_v7.lua` sends a JSON line every `FRAME_INTERVAL` frames with:
    - `frame`, `mode` (OVERWORLD/BATTLE), `ui` flags, `map` (group/num/x/y), and `party` HP.
2) Python reads snapshots, routes control by mode/UI flags, and asks:
    - Strategist for a high-level decision (goal/dialog/battle).
    - Navigator for a movement plan when a GO_TO goal is active.
3) Python converts plans into controller commands and sends them to Lua as **one line per input**:
    - `KEY:FRAMES\n` (example: `UP:12`, `A:4`).

## Model output schemas (must be JSON-only)
### Strategist output schema
See `ollama/model-injection/Strategist.json` and `ollama/model-injection/Modelfile.strategist`.

```json
{
  "intent": "GO_TO | HANDLE_DIALOG | BATTLE_AUTO | WAIT",
  "goal": "semantic_key or null",
  "button": "A|B|START|SELECT or null",
  "repeat": 1,
  "policy": {
    "avoid_optional_trainers": true,
    "allow_grass_encounters": false
  }
}
```

### Navigator output schema
Navigator returns a short list of input tuples `["BUTTON", FRAMES]` (max 12).

```json
{
  "intent": "PLAN | STEP | WAIT | RECOVER",
  "buttons": [["UP", 15], ["RIGHT", 15]],
  "confidence": 0.0,
  "note": "optional short note"
}
```

## World data
The backend loads:
- `overworld-semantic/semantic_locations.json` (authoritative list of valid GO_TO goals for Strategist).
- `overworld-json/overworld_nav_patched.json` and `overworld-json/overworld_metadata.json` (navigation graph + map metadata).

## Setup
### 1) Build Ollama models
Create the two models from Modelfiles:
- `gbai-strategist` (from `ollama/model-injection/Modelfile.strategist`).

Example (paths may differ):
```bash
ollama create gbai-strategist -f ollama/model-injection/Modelfile.strategist
```

### 2) Run mGBA + Lua bridge
1) Open Pokémon Emerald (U) ROM in mGBA.
2) Load the Lua script `emerald_bridge_v7.lua`.
3) Ensure the Lua script is configured for the same host/port as Python (default `127.0.0.1:8765`).

### 3) Run the backend
From the backend folder:
```bash
python ai_player_v6.py
```

Environment variables (optional) are supported in the backend:
- `OLLAMA_HOST`, `OLLAMA_PORT`
- `OLLAMA_MODEL_NAV`, `OLLAMA_MODEL_STRAT`
- `LLM_TIMEOUT_SECONDS`

## Troubleshooting
### A) Ollama request timeouts / slow responses
Symptoms:
- Python `TimeoutError: timed out` during Ollama `/api/generate` calls.

Actions:
- Increase `LLM_TIMEOUT_SECONDS` (e.g. 120–180) in environment variables.
- Ensure the model is warmed up (first request can be slow).

### B) Strategist logs appear, but the game does not move (no inputs applied)
The Lua bridge applies inputs only when it receives lines `KEY:FRAMES` and parses them.

Checklist:
1) Verify Python is actually sending inputs to the socket (you should see send logs on Python side).
2) Verify the port matches in both Lua and Python (`PORT = 8765`).
3) Verify the command format and casing: Lua uppercases keys and expects `:` + integer frames + newline.
4) Lua non-blocking receive: in some environments, `socket:receive(1024)` can return `(nil, "timeout", partial)`.
   Current code treats `nil` as a hard error and disconnects.

Recommended hardening patch (Lua):
```lua
local data, recv_err, partial = sock:receive(1024)
local chunk = data or partial
if not chunk or #chunk == 0 then
  if recv_err == "timeout" then return end
  connected = false; sock = nil; return
end
recv_buffer = recv_buffer .. chunk
```

### C) UI flags prevent movement
The backend routes to dialog/menu handling when:
- `textbox_open == true` OR `menu_open == true` OR `control_enabled == false`.

If these flags are wrong (memory addresses are version-dependent), the agent may spam dialog handling and never walk.
In that case:
- temporarily disable the UI flag addresses in Lua (set `ADDR_UI_TEXTBOX`, `ADDR_UI_MENU`, `ADDR_UI_CONTROL` to `nil`), or
- correct the addresses for your ROM version.
