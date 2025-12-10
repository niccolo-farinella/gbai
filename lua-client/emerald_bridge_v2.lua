-- emerald_bridge.lua
-- Script1: gira dentro mGBA, legge RAM di Pokémon Emerald e manda snapshot via TCP
-- + riceve comandi JSON dal backend e li traduce in input GBA.

---------------------------------------
-- CONFIG
---------------------------------------

local HOST = "127.0.0.1"
local PORT = 8765

-- Invia uno snapshot ogni N frame per non sovraccaricare
local FRAME_INTERVAL = 30

-- Indirizzi noti per Pokémon Emerald (U):
-- RTC array buffer (8 byte)
local ADDR_RTC = 0x03000460      -- RTC array buffer (Emerald U)

-- Puntatore a SaveBlock1 (IWRAM)
local ADDR_SAVE1_PTR = 0x03005D8C  -- pointer to SaveBlock1 in Emerald

-- Lunghezza blocco da leggere da SaveBlock1 (puoi aumentare se ti serve più roba)
local SAVE1_LEN = 0x400  -- 1KB di dati grezzi

-- Durata di default (in frame) di ciascun comando direzionale
local INPUT_FRAMES_DEFAULT = 12


---------------------------------------
-- STATO INTERNO
---------------------------------------

local sock = nil
local connected = false
local last_frame_sent = -1

local logBuffer = console:createBuffer("emerald_bridge")
logBuffer:setSize(80, 10)

-- stato per input
local current_keys_mask = 0
local input_frames_left = 0
local recv_buffer = ""

-- precomputiamo le maschere direzionali con util.makeBitmask
local DIR_MASKS = {
    UP    = util.makeBitmask({C.GBA_KEY.UP}),
    DOWN  = util.makeBitmask({C.GBA_KEY.DOWN}),
    LEFT  = util.makeBitmask({C.GBA_KEY.LEFT}),
    RIGHT = util.makeBitmask({C.GBA_KEY.RIGHT}),
}


---------------------------------------
-- UTIL
---------------------------------------

local function log(msg)
    logBuffer:clear()
    logBuffer:print(msg .. "\n")
end

local function to_hex(str)
    -- Converti stringa di byte in stringa esadecimale continua (es: "0A3F...")
    local t = {}
    for i = 1, #str do
        t[#t + 1] = string.format("%02X", str:byte(i))
    end
    return table.concat(t)
end

local function read_u32(addr)
    return emu:read32(addr)
end

local function read_u16(addr)
    return emu:read16(addr)
end

local function read_u8(addr)
    return emu:read8(addr)
end

local function safe_read_range(addr, length)
    local ok, data = pcall(function()
        return emu:readRange(addr, length)
    end)
    if not ok or not data then
        return ""
    end
    return data
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

    log(string.format("emerald_bridge: connecting to %s:%d", HOST, PORT))

    local ok, res = pcall(socket.connect, HOST, PORT)
    if not ok then
        log("emerald_bridge: connect error: " .. tostring(res))
        connected = false
        sock = nil
        return
    end

    sock = res
    if not sock then
        log("emerald_bridge: socket.connect returned nil")
        connected = false
        return
    end

    connected = true
    log("emerald_bridge: connected")
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
        log("emerald_bridge: send failed: " .. tostring(err))
        connected = false
        sock = nil
    end
end


---------------------------------------
-- COSTRUZIONE SNAPSHOT
---------------------------------------

-- SaveBlock1 (Emerald):
-- struct SaveBlock1 {
--   struct Coords16 pos;         // 0x00: s16 x, s16 y
--   struct WarpData location;    // 0x04: s16 x,y; u8 elevation; u8 mapGroup; u8 mapNum; u8 warpId
--   ...
-- }
-- vedi documentazione / decomp. 

local function build_snapshot_json()
    local frame = emu:currentFrame()

    -- RTC grezzo
    local rtc_raw = safe_read_range(ADDR_RTC, 8)
    local rtc_hex = to_hex(rtc_raw)

    -- Puntatore a SaveBlock1
    local save1_ptr = read_u32(ADDR_SAVE1_PTR)
    local save1_hex = ""
    local map_group = 0
    local map_num   = 0
    local pos_x     = 0
    local pos_y     = 0

    if save1_ptr ~= 0 then
        local raw = safe_read_range(save1_ptr, SAVE1_LEN)
        save1_hex = to_hex(raw)

        -- estraiamo posizione e mappa attuale da SaveBlock1
        pos_x = read_u16(save1_ptr + 0)  -- Coords16.x
        pos_y = read_u16(save1_ptr + 2)  -- Coords16.y

        -- location.mapGroup/mapNum (WarpData a offset 0x4)
        map_group = read_u8(save1_ptr + 9)
        map_num   = read_u8(save1_ptr + 10)
    end

    local mode = "movement"

    -- JSON minimale per il backend:
    --  - type: "snapshot"
    --  - frame
    --  - mode
    --  - map_group, map_num
    --  - player_x, player_y
    --  - rtc_hex, save1_ptr, save1_hex (extra/debug)
    local json = string.format(
        '{"type":"snapshot","mode":"%s","frame":%d,"map_group":%d,"map_num":%d,' ..
        '"player_x":%d,"player_y":%d,' ..
        '"rtc_hex":"%s","save1_ptr":"%08X","save1_hex":"%s"}',
        mode,
        frame,
        map_group,
        map_num,
        pos_x,
        pos_y,
        rtc_hex,
        save1_ptr,
        save1_hex
    )

    return json
end


---------------------------------------
-- INPUT HANDLING
---------------------------------------

local function apply_current_keys()
    -- viene chiamato ogni frame per applicare la maschera
    if current_keys_mask ~= 0 then
        emu:setKeys(current_keys_mask)
    else
        emu:setKeys(0)
    end
end

local function set_direction(dir, frames)
    dir = string.upper(dir or "")
    frames = frames or INPUT_FRAMES_DEFAULT

    if dir == "NONE" then
        current_keys_mask = 0
        input_frames_left = 0
        log("emerald_bridge: MOVE NONE (stop)")
        return
    end

    local mask = DIR_MASKS[dir]
    if mask then
        current_keys_mask = mask
        input_frames_left = frames
        log(string.format("emerald_bridge: MOVE %s (%d frames)", dir, frames))
    else
        log("emerald_bridge: unknown MOVE/input direction: " .. dir)
    end
end

local function handle_input_json(line)
    -- JSON molto semplice, es:
    -- {"type":"input","buttons":["up"],"hold_frames":15}
    if not line:match('"type"%s*:%s*"input"') then
        return
    end

    -- estrai primo pulsante in buttons[]
    local btn = line:match('"buttons"%s*:%s*%[%s*"(.-)"')
    if not btn then
        return
    end

    local dir = string.upper(btn)

    -- estrai hold_frames se presente
    local hold_str = line:match('"hold_frames"%s*:%s*(%d+)')
    local frames = INPUT_FRAMES_DEFAULT
    if hold_str then
        local n = tonumber(hold_str)
        if n and n > 0 then
            frames = n
        end
    end

    set_direction(dir, frames)
end

local function handle_command_line(line)
    line = trim(line)
    if line == "" then return end

    -- Se sembra JSON, prova a trattarlo come comando input
    if line:sub(1, 1) == "{" then
        handle_input_json(line)
        return
    end

    -- Compatibilità retro: "MOVE UP"
    local cmd, arg = line:match("^(%S+)%s*(.*)$")
    if not cmd then return end
    cmd = string.upper(cmd)

    if cmd == "MOVE" then
        if arg == nil or arg == "" then
            arg = "NONE"
        end
        set_direction(arg, INPUT_FRAMES_DEFAULT)
    end
end

local function poll_commands()
    if not connected or not sock then
        return
    end

    -- controlla se ci sono dati in arrivo
    local has = false
    local ok, err = pcall(function()
        has = sock:hasdata()
    end)
    if not ok or not has then
        return
    end

    local data, recv_err = sock:receive(1024)
    if not data then
        -- probabilmente disconnesso
        log("emerald_bridge: receive error: " .. tostring(recv_err))
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
        handle_command_line(line)
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
        connect_socket()
        if not connected then
            local f = emu:currentFrame()
            if f % 120 ~= 0 then
                return
            end
            connect_socket()
            return
        end
    end

    -- 1) applichiamo eventuali input ancora attivi
    if input_frames_left > 0 then
        input_frames_left = input_frames_left - 1
        if input_frames_left <= 0 then
            current_keys_mask = 0
        end
    end
    apply_current_keys()

    -- 2) poll comandi in arrivo dal backend
    poll_commands()

    -- 3) snapshot (solo ogni FRAME_INTERVAL frame)
    local frame = emu:currentFrame()
    if last_frame_sent ~= -1 and (frame - last_frame_sent) < FRAME_INTERVAL then
        return
    end
    last_frame_sent = frame

    local json = build_snapshot_json()
    send_line(json)
end


---------------------------------------
-- REGISTRAZIONE CALLBACK
---------------------------------------

callbacks:add("frame", on_frame)

log("emerald_bridge: script loaded, waiting for backend...")
