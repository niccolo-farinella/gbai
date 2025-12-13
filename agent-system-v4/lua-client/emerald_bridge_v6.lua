-- emerald_bridge_v6.lua
-- mGBA Lua bridge for Pokémon Emerald (U) (BPEE)
--
-- Protocol:
--   Lua -> Python: one JSON snapshot per line
--   Python -> Lua: "KEY:FRAMES\n" (e.g., "UP:15\n")
--
-- IMPORTANT (mGBA stability):
--   Do not run a top-level busy loop. Use callbacks instead.

local BRIDGE_VERSION = "emerald_bridge_v6.3"

-- ============================================================
-- Logging
-- ============================================================
-- In mGBA, `print()` output isn't always obvious depending on which scripting
-- UI panel is open. A TextBuffer is reliably visible under Tools -> Scripting.
local logbuf = nil
if console and console.createBuffer then
  logbuf = console:createBuffer("AI Bridge v6")
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
local SEND_EVERY_N_FRAMES = 1
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
-- UI flags remain intentionally conservative (0) until we standardize their mapping.

local ADDR_SAVE_BLOCK_1_PTR   = 0x03005D8C  -- gSaveBlock1Ptr
local ADDR_BATTLE_TYPE_FLAGS  = 0x02022FEC  -- gBattleTypeFlags (nonzero => battle-ish)

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

local function in_battle()
  local flags = u32(ADDR_BATTLE_TYPE_FLAGS)
  return flags and flags ~= 0
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
  local mode = in_battle() and "BATTLE" or "OVERWORLD"
  local map_group, map_num, px, py = read_position()
  local php, pmax = sum_party_hp(ADDR_PLAYER_PARTY)

  -- UI flags: keep conservative defaults until we add stable symbol-based reads.
  local textbox_open = 0
  local menu_open = 0
  local control_enabled = 1

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
