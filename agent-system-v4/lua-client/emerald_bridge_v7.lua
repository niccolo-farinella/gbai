-- emerald_bridge_v6.lua
-- mGBA Lua bridge for Pokémon Emerald (U) (BPEE)
--
-- Protocol:
--   Lua -> Python: one JSON snapshot per line
--   Python -> Lua: "KEY:FRAMES\n" (e.g., "UP:15\n")
--
-- IMPORTANT (mGBA stability):
--   Do not run a top-level busy loop. Use callbacks instead.

local BRIDGE_VERSION = "emerald_bridge_v7"-- ============================================================
-- Logging
-- ============================================================
-- In mGBA, `print()` output isn't always obvious depending on which scripting
-- UI panel is open. A TextBuffer is reliably visible under Tools -> Scripting.
local logbuf = nil
if console and console.createBuffer then
  logbuf = console:createBuffer("AI Bridge v7")
  pcall(function() logbuf:setSize(90, 12) end)
end

local function slog(msg)
  msg = "[bridge] " .. tostring(msg)
  if logbuf and logbuf.print then
    pcall(function() logbuf:print(msg .. "\n") end)
  end
  -- Keep print as a secondary output path
  pcall(function() print(msg) end)
end

-- Connection settings
local HOST = "127.0.0.1"
local PORT = 8765

-- Runtime throttles
local SEND_EVERY_N_FRAMES = 3
local CONNECT_RETRY_EVERY_N_FRAMES = 60

-- Debugging (keep OFF by default; printing every frame will lag)
local DEBUG = false
local function dlog(msg)
  if DEBUG then
    slog(msg)
  end
end

-- Socket library
-- mGBA exposes a built-in `socket` object (NOT LuaSocket). See mGBA scripting docs.
-- Avoid `require('socket')` here: on some builds this can hang or fail in a way
-- that prevents the script from starting cleanly.
local socket = rawget(_G, "socket")

-- mGBA helpers
if not emu then
  error("mGBA 'emu' object not found (this script must be run inside mGBA).")
end

slog("Initializing " .. BRIDGE_VERSION .. " (" .. tostring(system and system.version or "unknown") .. ")")
if not socket then
  slog("WARNING: mGBA socket API not available; running disconnected")
end

-- ============================================================
-- Keys (FIX: util.makeBitmask can throw on some mGBA builds)
-- ============================================================

local GBA_KEY = (C and C.GBA_KEY) or {}

-- Detect how C.GBA_KEY is represented, using the A key as a discriminator:
--   - bit index form: A == 0
--   - bitmask form : A == 1 (0x0001)
local KEY_ENUM_MODE = "unknown" -- "bit_index" | "bit_mask" | "unknown"
do
  local a = tonumber(GBA_KEY.A)
  if a == 0 then
    KEY_ENUM_MODE = "bit_index"
  elseif a == 1 then
    KEY_ENUM_MODE = "bit_mask"
  elseif a ~= nil then
    -- Best-effort: if it's a number but not 0, treat as bitmask.
    KEY_ENUM_MODE = "bit_mask"
  end
end

local function make_key_mask(key_enum)
  if key_enum == nil then
    return 0
  end

  local n = tonumber(key_enum)

  -- Prefer deterministic local mapping (avoids util.makeBitmask crashes).
  if n ~= nil then
    if KEY_ENUM_MODE == "bit_mask" then
      return n
    elseif KEY_ENUM_MODE == "bit_index" then
      -- Exponentiation is supported across Lua versions; keys are 0..9.
      return math.floor(2 ^ n)
    end
  end

  -- Guarded fallback to util.makeBitmask.
  if util and util.makeBitmask then
    local ok, res = pcall(function() return util.makeBitmask(key_enum) end)
    if ok and type(res) == "number" then
      return res
    end
    -- Some builds accept a table of enums.
    local ok2, res2 = pcall(function() return util.makeBitmask({ key_enum }) end)
    if ok2 and type(res2) == "number" then
      return res2
    end
  end

  -- Last resort: attempt index-style mapping if coercible.
  if n ~= nil then
    return math.floor(2 ^ n)
  end
  return 0
end

-- If C.GBA_KEY is unavailable, fall back to the GBA KEYINPUT bit definitions.
local FALLBACK_MASK = {
  A      = 0x0001,
  B      = 0x0002,
  SELECT = 0x0004,
  START  = 0x0008,
  RIGHT  = 0x0010,
  LEFT   = 0x0020,
  UP     = 0x0040,
  DOWN   = 0x0080,
  R      = 0x0100,
  L      = 0x0200,
}

local function build_key_masks()
  local km = {}
  for k, fb in pairs(FALLBACK_MASK) do
    if GBA_KEY[k] ~= nil then
      km[k] = make_key_mask(GBA_KEY[k])
    else
      km[k] = fb
    end
    if type(km[k]) ~= "number" then
      km[k] = 0
    end
  end
  return km
end

local KEY_MASK = build_key_masks()

-- ============================================================
-- Game addresses (vanilla Emerald (U) / BPEE)
-- ============================================================
-- NOTE: These are the minimum required by ai_player_v6.py today.
-- UI flags are heuristics based on gScriptContext2_Enabled and gMain.callback2.

local ADDR_SAVE_BLOCK_1_PTR   = 0x03005D8C  -- gSaveBlock1Ptr
local ADDR_BATTLE_TYPE_FLAGS  = 0x02022FEC  -- gBattleTypeFlags (nonzero => battle-ish)

-- UI / engine state (BPEE / Emerald (U))
local ADDR_SCRIPT_CTX2_ENABLED = 0x03000DF4  -- gScriptContext2_Enabled (u8; 1 => player locked by script/text)
local ADDR_GMAIN_CB2           = 0x030022C4  -- gMain.callback2 (u32 function ptr; used as battle/menu discriminator)
-- SaveBlock1 layout (field)
local SB1_PLAYER_X   = 0x0000 -- u16
local SB1_PLAYER_Y   = 0x0002 -- u16
local SB1_MAP_GROUP  = 0x0004 -- u8
local SB1_MAP_NUM    = 0x0005 -- u8

-- Parties
local ADDR_PLAYER_PARTY = 0x020244EC
local PARTY_MON_SIZE    = 100
local MON_HP_OFFSET     = 0x56
local MON_MAX_HP_OFFSET = 0x58

-- ============================================================
-- Memory read helpers
-- ============================================================

local function u8(addr)  return emu:read8(addr)  end
local function u16(addr) return emu:read16(addr) end
local function u32(addr) return emu:read32(addr) end

-- ============================================================
-- ROM identity (sanity check)
-- ============================================================
-- This bridge hardcodes addresses for vanilla Emerald (U) / BPEE.
-- If you load a different ROM (v1.1, other region, hack), reads may be wrong.
local function read_game_code()
  local base = 0x080000AC -- ROM header game code (4 ASCII chars)
  local t = {}
  for i = 0, 3 do
    local b = u8(base + i)
    t[#t + 1] = string.char(b or 0)
  end
  return table.concat(t)
end

local GAME_CODE = nil
do
  local ok, code = pcall(read_game_code)
  if ok then
    GAME_CODE = code
  end
  if GAME_CODE and GAME_CODE ~= "BPEE" then
    slog("WARNING: ROM game code is '" .. tostring(GAME_CODE) .. "' (expected 'BPEE'). Address mapping may be wrong.")
  else
    slog("ROM game code: " .. tostring(GAME_CODE or "unknown"))
  end
end


-- ============================================================
-- Snapshot building
-- ============================================================

local function read_saveblock1_ptr()
  local p = u32(ADDR_SAVE_BLOCK_1_PTR)
  if not p or p == 0 then return nil end
  -- SaveBlock pointers are in EWRAM; basic sanity check.
  if p < 0x02000000 or p >= 0x03000000 then return nil end
  return p
end

local function read_position()
  local sb1 = read_saveblock1_ptr()
  if not sb1 then
    return 0, 0, 0, 0
  end
  local x = u16(sb1 + SB1_PLAYER_X)
  local y = u16(sb1 + SB1_PLAYER_Y)
  local g = u8(sb1 + SB1_MAP_GROUP)
  local n = u8(sb1 + SB1_MAP_NUM)
  return g, n, x, y
end

local function sum_party_hp(party_base)
  local hp_sum = 0
  local max_sum = 0
  for i = 0, 5 do
    local mon = party_base + (i * PARTY_MON_SIZE)
    local hp  = u16(mon + MON_HP_OFFSET)
    local mhp = u16(mon + MON_MAX_HP_OFFSET)
    if mhp and mhp > 0 then
      hp_sum = hp_sum + hp
      max_sum = max_sum + mhp
    end
  end
  return hp_sum, max_sum
end


-- ============================================================
-- Mode / UI heuristics
-- ============================================================
-- IMPORTANT: gBattleTypeFlags is not reliably cleared immediately after a battle.
-- To avoid getting stuck in "BATTLE" forever, we combine it with gMain.callback2:
--   - we learn a baseline callback2 pointer for the overworld ("free control")
--   - we treat "battle active" only when battle flags are set AND callback2 differs
--   - we add a small hysteresis window to smooth transitions/fades
local OVERWORLD_CB2 = nil
local battle_latched = false
local battle_exit_grace = 0
local BATTLE_EXIT_GRACE_FRAMES = 20

local function safe_read_u8(addr)
  local ok, v = pcall(function() return emu:read8(addr) end)
  if ok then return v end
  return nil
end

local function safe_read_u32(addr)
  local ok, v = pcall(function() return emu:read32(addr) end)
  if ok then return v end
  return nil
end

local function ptr_in_rom(p)
  return type(p) == "number" and p >= 0x08000000 and p < 0x0A000000
end

local function compute_mode_and_ui()
  local flags = safe_read_u32(ADDR_BATTLE_TYPE_FLAGS) or 0
  local script2 = safe_read_u8(ADDR_SCRIPT_CTX2_ENABLED) or 0
  local cb2 = safe_read_u32(ADDR_GMAIN_CB2)

  local cb2_valid = ptr_in_rom(cb2)

  -- Learn the overworld callback2 value when we are confidently free in the field.
  if flags == 0 and script2 == 0 and cb2_valid then
    OVERWORLD_CB2 = cb2
  end

  local cb2_diff = (OVERWORLD_CB2 ~= nil and cb2_valid and cb2 ~= OVERWORLD_CB2) or false

  -- Battle detection:
  -- - If flags are set but callback2 == overworld baseline, treat as stale leftover.
  -- - If we don't yet know the baseline (early boot), fall back to flags.
  local battle_instant = false
  if flags ~= 0 then
    battle_instant = (OVERWORLD_CB2 == nil) and true or cb2_diff
  end

  if battle_instant then
    battle_latched = true
    battle_exit_grace = BATTLE_EXIT_GRACE_FRAMES
  elseif battle_latched then
    if battle_exit_grace > 0 then
      battle_exit_grace = battle_exit_grace - 1
    else
      battle_latched = false
    end
  end

  local in_battle_now = battle_latched

  -- UI flags:
  -- script2 == 1 typically means a script/textbox is running and the player is locked.
  local textbox_open = (script2 == 1) and 1 or 0
  -- If callback2 differs from the overworld baseline (and we're not in battle),
  -- assume a menu / special callback is active (start menu, bag, etc.).
  local menu_open = (not in_battle_now and cb2_diff and script2 == 0) and 1 or 0
  local control_enabled = (in_battle_now or textbox_open == 1 or menu_open == 1) and 0 or 1

  if DEBUG then
    dlog(string.format("mode=%s flags=0x%08X script2=%d cb2=0x%08X ow=0x%08X diff=%s",
      in_battle_now and "BATTLE" or "OVERWORLD",
      flags,
      script2,
      cb2 or 0,
      OVERWORLD_CB2 or 0,
      tostring(cb2_diff)
    ))
  end

  return in_battle_now, textbox_open, menu_open, control_enabled
end

-- Back-compat helper (avoid scattering compute calls throughout the code)
local function in_battle()
  local b = select(1, compute_mode_and_ui())
  return b
end


local function json_escape(s)
  s = tostring(s)
  s = s:gsub('\\', '\\\\')
  s = s:gsub('"', '\\"')
  s = s:gsub('\n', '\\n')
  s = s:gsub('\r', '\\r')
  s = s:gsub('\t', '\\t')
  return s
end

local function make_snapshot(frame)
  local in_battle_now, textbox_open, menu_open, control_enabled = compute_mode_and_ui()
  local mode = in_battle_now and "BATTLE" or "OVERWORLD"
  local map_group, map_num, px, py = read_position()
  local php, pmax = sum_party_hp(ADDR_PLAYER_PARTY)


  return string.format(
    '{"frame":%d,' ..
    '"mode":"%s",' ..
    '"map":{"group":%d,"num":%d,"x":%d,"y":%d},' ..
    '"party":{"hp":%d,"max_hp":%d},' ..
    '"ui":{"textbox_open":%d,"menu_open":%d,"control_enabled":%d},' ..
    '"bridge":{"version":"%s"}}',
    frame,
    json_escape(mode),
    map_group, map_num, px, py,
    php, pmax,
    textbox_open, menu_open, control_enabled,
    json_escape(BRIDGE_VERSION)
  )
end

-- ============================================================
-- Socket + command processing
-- ============================================================

local sock = nil
local last_connect_attempt_frame = -CONNECT_RETRY_EVERY_N_FRAMES

local current_key_mask = 0
local current_key_frames_left = 0
local cmd_queue = {}

-- RX buffering (mGBA socket API reads raw bytes, not lines)
local rx_buf = ""
local MAX_RX_BUF = 8192

local function close_socket()
  if sock then
    -- mGBA's socket API doesn't guarantee a close() method in all builds.
    -- Use pcall to avoid crashing the script.
    pcall(function() sock:close() end)
    pcall(function() sock:shutdown() end)
  end
  sock = nil
  rx_buf = ""
  cmd_queue = {}
  current_key_mask = 0
  current_key_frames_left = 0
end

local function try_connect(frame)
  if not socket then
    -- No socket library available in this build; keep running but disconnected.
    return false
  end
  if sock ~= nil then
    return true
  end
  if (frame - last_connect_attempt_frame) < CONNECT_RETRY_EVERY_N_FRAMES then
    return false
  end
  last_connect_attempt_frame = frame

  -- Prefer mGBA's built-in `socket.connect` helper if present.
  if socket.connect then
    local ok, s_or_nil, err = pcall(function() return socket.connect(HOST, PORT) end)
    if ok and s_or_nil then
      sock = s_or_nil
      slog("Connected to " .. HOST .. ":" .. tostring(PORT))
      return true
    end
    dlog("socket.connect failed: " .. tostring(err or s_or_nil))
    return false
  end

  -- Fallback: tcp() + connect()
  local s, terr
  if socket.tcp then
    s, terr = socket.tcp()
  else
    s = nil
    terr = "socket.tcp not available"
  end
  if not s then
    dlog("socket.tcp failed: " .. tostring(terr))
    return false
  end

  -- NOTE: mGBA's connect() is blocking; ensure Python is already listening.
  local rc, cerr = s:connect(HOST, PORT)
  if rc == nil then
    dlog("connect failed: " .. tostring(cerr))
    pcall(function() s:close() end)
    return false
  end
  if type(rc) == "number" and rc ~= 0 then
    dlog("connect returned error code: " .. tostring(rc))
    pcall(function() s:close() end)
    return false
  end

  sock = s
  slog("Connected to " .. HOST .. ":" .. tostring(PORT))
  return true
end

local function set_keys(mask)
  -- In mGBA, emu:setKeys(bitmask)
  emu:setKeys(mask)
end

local function poll_commands()
  if not sock then
    return
  end

  -- mGBA socket API provides hasdata() and raw receive(maxBytes).
  local has = true
  if sock.hasdata then
    local ok, res = pcall(function() return sock:hasdata() end)
    if ok then
      has = res
    end
  end
  if not has then
    return
  end

  local data, err = sock:receive(4096)
  if not data then
    -- Benign non-blocking cases (exact strings differ by platform/build).
    local e = tostring(err or "")
    local el = e:lower()
    if el:find("block", 1, true) or el:find("again", 1, true) or el:find("timeout", 1, true) then
      return
    end
    if e ~= "" then
      dlog("receive error: " .. e)
    end
    close_socket()
    return
  end

  rx_buf = rx_buf .. data
  if #rx_buf > MAX_RX_BUF then
    dlog("RX buffer overflow (" .. tostring(#rx_buf) .. " bytes); dropping buffer.")
    rx_buf = ""
  end

  while true do
    local nl = rx_buf:find("\n", 1, true)
    if not nl then
      break
    end
    local line = rx_buf:sub(1, nl - 1)
    rx_buf = rx_buf:sub(nl + 1)
    line = line:gsub("\r", "")

    local key, frames = line:match("^([A-Z_]+):(%d+)$")
    if key and frames then
      local m = KEY_MASK[key]
      local f = tonumber(frames) or 0
      if m and m ~= 0 and f > 0 then
        table.insert(cmd_queue, { mask = m, frames = f, key = key })
      end
    end
  end
end

local function step_input_hold()
  if current_key_frames_left <= 0 and #cmd_queue > 0 then
    local cmd = table.remove(cmd_queue, 1)
    current_key_mask = cmd.mask
    current_key_frames_left = cmd.frames
  end

  if current_key_frames_left > 0 then
    set_keys(current_key_mask)
    current_key_frames_left = current_key_frames_left - 1
    if current_key_frames_left == 0 then
      set_keys(0)
      current_key_mask = 0
    end
  else
    if current_key_mask ~= 0 then
      set_keys(0)
      current_key_mask = 0
    end
  end
end

local function send_snapshot(frame)
  if not sock then
    return
  end

  local snap = make_snapshot(frame)
  local res, err = sock:send(snap .. "\n")
  if res == nil then
    dlog("send failed: " .. tostring(err))
    close_socket()
  end
end

-- ============================================================
-- Frame handler (callbacks)
-- ============================================================

local last_sent_frame = -SEND_EVERY_N_FRAMES
local DISABLED = false

local function get_frame()
  if emu.currentFrame then
    return emu:currentFrame()
  elseif emu.framecount then
    return emu:framecount()
  else
    last_sent_frame = last_sent_frame + 1
    return last_sent_frame
  end
end

local function on_frame()
  if DISABLED then
    return
  end

  local frame = get_frame()

  -- (1) Ensure we are connected (non-blocking, with backoff)
  try_connect(frame)

  -- (2) Apply any ongoing input hold
  step_input_hold()

  -- (3) Pull new commands from Python
  poll_commands()

  -- (4) Send snapshot
  if (frame - last_sent_frame) >= SEND_EVERY_N_FRAMES then
    send_snapshot(frame)
    last_sent_frame = frame
  end
end

if callbacks and callbacks.add then
  callbacks:add("frame", function()
    local ok, err = pcall(on_frame)
    if not ok then
      slog("Fatal error in on_frame: " .. tostring(err))
      DISABLED = true
      close_socket()
      -- release any held keys
      pcall(function() emu:setKeys(0) end)
    end
  end)
  slog("Loaded " .. BRIDGE_VERSION .. " (callbacks mode)")
else
  error("mGBA 'callbacks' API not found; cannot run safely without a frame callback.")
end
