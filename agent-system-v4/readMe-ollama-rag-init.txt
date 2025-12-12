# Pokémon Emerald — LLM Context (Strategist + Navigator)

This document is injected into the **Strategist** model at bootstrap time to provide game/domain context.
It is **not** the output schema (the schema is provided separately by the backend / Modelfile).

## Roles (high level)
- **Strategist** decides *what to do next*: story progression, dialog/menu advancement, and high‑level goals.
- **Navigator** decides *how to move* in the overworld by outputting short controller input sequences.

## Game concepts (quick primer)
- **Overworld**: walking around towns/routes/buildings.
- **Dialog / textbox**: NPC conversations, cutscenes, prompts (usually advanced with **A**).
- **Menu**: Start menu, bag, party, etc. Often advanced/closed with **B** or navigated with D‑pad.
- **Battle**: turn-based combat. Until richer battle state is available, default behavior is safe “press A” progression.

### Common locations
- **Pokémon Center (HEAL)**: heals the party (usually by speaking to the nurse and confirming with **A**).
- **Gym (GYM)**: major story battles. Requires being in the correct town and sometimes clearing trainers inside.
- **Routes (ROUTE)**: transitions between towns; often contain trainers and tall grass.
- **Tall grass**: random encounters; avoid if policy says so.

## Interpreting backend state
You receive a state snapshot with:
- `state.mode`: `"OVERWORLD"` or `"BATTLE"`.
- `state.ui.textbox_open`: dialog box visible (advance with A unless it is a yes/no or a menu close case).
- `state.ui.menu_open`: a menu is open (often B closes; sometimes A confirms).
- `state.ui.control_enabled`: if false, the player cannot move (cutscene / forced sequence).

### Default action selection heuristics
1) **If `state.mode == "BATTLE"`**
   - Prefer `intent="BATTLE_AUTO"` and `button="A"`.

2) **If in OVERWORLD and a textbox/menu is open OR control is disabled**
   - Prefer `intent="HANDLE_DIALOG"`.
   - Default `button="A"` (advance/confirm).
   - Use `button="B"` only if the UI is clearly a “back/cancel/close” situation (e.g., stuck in menu screens).

3) **HP safety**
   - Let `hp_pct = state.party.hp / state.party.max_hp`.
   - If `hp_pct < 0.30`, strongly prefer going to a **HEAL** semantic location:
     - `intent="GO_TO"`, `goal=<HEAL-type semantic key>`
     - `policy.avoid_optional_trainers=true`
     - `policy.allow_grass_encounters=false`

4) **Story progression**
   - When safe (hp_pct ≥ 0.30), prefer progressing the main story by choosing the next reasonable **semantic goal** (town, gym, lab, etc.).
   - Never invent semantic keys: select only from `known_semantic_locations` provided at runtime.

## Policy usage (pathing hints)
- `avoid_optional_trainers=true`: prefer safer routes, avoid avoidable trainers where possible.
- `allow_grass_encounters=false`: avoid tall grass / wild encounters where possible.

## Early-game (example objective chain)
This project’s early-game objectives typically follow:
`PLAYER_HOME_BEDROOM` → `BIRCH_LAB` → `LITTLEROOT_CENTER` → `ROUTE_101_MID` → `OLDALE_CENTER` → `ROUTE_102_MID` → `PETALBURG_POKECENTER` → `RUSTBORO_CENTER` → `RUSTBORO_GYM`.

(Exact keys available depend on the runtime `known_semantic_locations` list.)
