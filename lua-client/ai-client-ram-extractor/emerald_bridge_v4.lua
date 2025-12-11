-- emerald_bridge_v4.lua
-- Bridge mGBA <-> ai_player_v3.py
-- - Legge stato di Pokémon Emerald e lo manda via TCP
-- - Riceve comandi "KEY:FRAMES" (es: "UP:12", "A:4") e li applica come input GBA

---------------------------------------
-- CONFIG
---------------------------------------

local HOST = "127.0.0.1"
local PORT = 8765

-- Invia uno snapshot ogni N frame per non sovraccaricare
local FRAME_INTERVAL = 20

-- Indirizzi noti per Pokémon Emerald (U)
local ADDR_SAVE1_PTR    = 0x03005D8C  -- pointer a SaveBlock1
local ADDR_BATTLE_FLAGS = 0x02022B4C  -- battle flags (!=0 -> in battaglia)
local ADDR_PARTY_PTR    = 0x020244EC  -- primo Pokémon in party

---------------------------------------
-- STATO INTERNO
---------------------------------------

local sock = nil
local connected = false
local last_frame_sent = -1
local recv_buffer = ""

-- stato per input
local current_keys_mask = 0
local input_frames_left = 0

-- buffer di log (come nella v2 che funzionava)
local logBuffer = console:createBuffer("emerald_bridge_v4")
logBuffer:setSize(80, 10)

-- Mappa tasti -> bitmask corretta usando C.GBA_KEY + util.makeBitmask
local KEYS = {
    A      = util.makeBitmask({C.GBA_KEY.A}),
    B      = util.makeBitmask({C.GBA_KEY.B}),
    SELECT = util.makeBitmask({C.GBA_KEY.SELECT}),
    START  = util.makeBitmask({C.GBA_KEY.START}),
    RIGHT  = util.makeBitmask({C.GBA_KEY.RIGHT}),
    LEFT   = util.makeBitmask({C.GBA_KEY.LEFT}),
    UP     = util.makeBitmask({C.GBA_KEY.UP}),
    DOWN   = util.makeBitmask({C.GBA_KEY.DOWN}),
    R      = util.makeBitmask({C.GBA_KEY.R}),
    L      = util.makeBitmask({C.GBA_KEY.L}),
}

---------------------------------------
-- UTIL
---------------------------------------

local function log(msg)
    logBuffer:clear()
    logBuffer:print(tostring(msg) .. "\n")
end

local function trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

---------------------------------------
-- SOCKET
---------------------------------------

local function connect_socket()
    if connected then
        return
    end

    log(string.format("emerald_bridge_v4: connecting to %s:%d", HOST, PORT))

    local ok, res = pcall(socket.connect, HOST, PORT)
    if not ok then
        log("emerald_bridge_v4: connect error: " .. tostring(res))
        connected = false
        sock = nil
        return
    end

    sock = res
    if not sock then
        log("emerald_bridge_v4: socket.connect returned nil")
        connected = false
        return
    end

    -- non bloccare il frame loop
    pcall(function() sock:settimeout(0) end)

    connected = true
    log("emerald_bridge_v4: connected")
end

local function send_line(line)
    if not connected or not sock then
        return
    end
    if not line:match("\n$") then
        line = line .. "\n"
    end

    local ok, err = pcall(function()
        sock:send(line)
    end)

    if not ok then
        log("emerald_bridge_v4: send failed: " .. tostring(err))
        connected = false
        sock = nil
    end
end

---------------------------------------
-- COSTRUZIONE STATO JSON
---------------------------------------

local function get_state_json()
    local sb1 = emu:read32(ADDR_SAVE1_PTR)
    if sb1 == 0 then
        -- Nessun saveblock valido: manda JSON vuoto
        return "{}\n"
    end

    -- Coords + mappa (come SaveBlock1)
    local x = emu:read16(sb1 + 0x00) -- Coords16.x
    local y = emu:read16(sb1 + 0x02) -- Coords16.y
    local g = emu:read8(sb1 + 0x04)  -- mapGroup
    local n = emu:read8(sb1 + 0x05)  -- mapNum

    -- HP del primo Pokémon in party
    local hp  = emu:read16(ADDR_PARTY_PTR + 0x56)
    local mhp = emu:read16(ADDR_PARTY_PTR + 0x58)

    -- Determina la modalità (OVERWORLD/BATTLE)
    local mode = "OVERWORLD"
    if emu:read32(ADDR_BATTLE_FLAGS) ~= 0 then
        mode = "BATTLE"
    end

    -- Formato esattamente come ai_player_v3.py si aspetta
    return string.format(
        '{"mode":"%s",' ..
        '"map":{"group":%d,"num":%d,"x":%d,"y":%d},' ..
        '"party":{"hp":%d,"max_hp":%d}}\n',
        mode, g, n, x, y, hp, mhp
    )
end

---------------------------------------
-- INPUT HANDLING
---------------------------------------

local function apply_current_keys()
    if current_keys_mask ~= 0 then
        emu:setKeys(current_keys_mask)
    else
        emu:setKeys(0)
    end
end

local function process_input()
    -- viene chiamato ogni frame per applicare la maschera
    if input_frames_left > 0 and current_keys_mask ~= 0 then
        input_frames_left = input_frames_left - 1
        if input_frames_left <= 0 then
            current_keys_mask = 0
        end
    else
        current_keys_mask = 0
    end
    apply_current_keys()
end

local function handle_input_line(line)
    -- Linea nel formato "KEY:FRAMES", es: "UP:12", "A:4"
    line = trim(line)
    if line == "" then
        return
    end

    local k, f = string.match(line, "([^:]+):(%d+)")
    if not k or not f then
        return
    end

    k = string.upper(trim(k))
    local frames = tonumber(f) or 0
    if frames < 1 then
        frames = 1
    end

    -- Caso speciale: "NONE:0" per rilasciare forzatamente
    if k == "NONE" then
        current_keys_mask  = 0
        input_frames_left = 0
        emu:setKeys(0)
        log("emerald_bridge_v4: input NONE -> release keys")
        return
    end

    local mask = KEYS[k]
    if not mask then
        -- Tasto sconosciuto: ignora
        log("emerald_bridge_v4: unknown key '" .. k .. "'")
        return
    end

    current_keys_mask  = mask
    input_frames_left = frames
    log(string.format("emerald_bridge_v4: INPUT %s (%d frames)", k, frames))
end

local function poll_commands()
    if not connected or not sock then
        return
    end

    -- controlla se ci sono dati in arrivo (come v2, che funzionava)
    local has = false
    local ok, err = pcall(function()
        has = sock:hasdata()
    end)
    if not ok or not has then
        return
    end

    local data, recv_err = sock:receive(1024)
    if not data then
        log("emerald_bridge_v4: receive error: " .. tostring(recv_err))
        connected = false
        sock = nil
        return
    end

    recv_buffer = recv_buffer .. data
    while true do
        local nl = recv_buffer:find("\n", 1, true)
        if not nl then break end
        local line = recv_buffer:sub(1, nl - 1)
        recv_buffer = recv_buffer:sub(nl + 1)
        handle_input_line(line)
    end
end

---------------------------------------
-- CALLBACK PER FRAME
---------------------------------------

local function on_frame()
    if not emu then
        return
    end

    if not connected then
        -- prova a connetterti ogni ~2s
        local f = emu:currentFrame()
        if f % 120 ~= 0 then
            return
        end
        connect_socket()
        return
    end

    -- 1) applica eventuali input ancora attivi
    process_input()

    -- 2) poll comandi in arrivo dal backend
    poll_commands()

    -- 3) snapshot (solo ogni FRAME_INTERVAL frame)
    local frame = emu:currentFrame()
    if last_frame_sent ~= -1 and (frame - last_frame_sent) < FRAME_INTERVAL then
        return
    end
    last_frame_sent = frame

    local json = get_state_json()
    send_line(json)
end

---------------------------------------
-- REGISTRAZIONE CALLBACK
---------------------------------------

callbacks:add("frame", on_frame)

log("emerald_bridge_v4: script loaded, waiting for backend.")
