-- emerald_agent_bridge_v5.lua
-- Fix: setKeys(0) per rilasciare tasti + Safe Socket
local HOST = "127.0.0.1"
local PORT = 8765
local FRAME_INTERVAL = 20
local ADDR_SAVE1_PTR = 0x03005D8C
local ADDR_BATTLE_FLAGS = 0x02022B4C 
local ADDR_PARTY_PTR = 0x020244EC

local sock = nil
local connected = false
local recv_buffer = ""
local last_state_sent = -1
local KEYS = { A=1, B=2, SELECT=4, START=8, RIGHT=16, LEFT=32, UP=64, DOWN=128, R=256, L=512 }
local current_key_mask = 0
local input_frames_left = 0

function log(msg) console:log("[LuaBridge] " .. msg) end

function connect()
    if sock then pcall(function() sock:close() end) end
    sock = socket.tcp()
    local res, err = sock:connect(HOST, PORT)
    if res then
        connected = true
        log("Connesso.")
        pcall(function() sock:settimeout(0) end)
    else
        sock = nil
        connected = false
    end
end

function get_state_json()
    local sb1 = emu:read32(ADDR_SAVE1_PTR)
    if sb1 == 0 then return "{}" end
    local x = emu:read16(sb1 + 0x00)
    local y = emu:read16(sb1 + 0x02)
    local g = emu:read8(sb1 + 0x04)
    local n = emu:read8(sb1 + 0x05)
    local hp = emu:read16(ADDR_PARTY_PTR + 0x56)
    local mhp = emu:read16(ADDR_PARTY_PTR + 0x58)
    local mode = (emu:read32(ADDR_BATTLE_FLAGS) ~= 0) and "BATTLE" or "OVERWORLD"
    
    return string.format('{"mode":"%s", "map":{"group":%d,"num":%d,"x":%d,"y":%d}, "party":{"hp":%d,"max_hp":%d}}\n',
        mode, g, n, x, y, hp, mhp)
end

function process_input()
    if input_frames_left > 0 then
        emu:setKeys(current_key_mask)
        input_frames_left = input_frames_left - 1
    else
        current_key_mask = 0
        emu:setKeys(0) -- FIX IMPORTANTE: Rilascia tasti
    end
end

function read_net()
    if not connected then return end
    local chunk, err, part = sock:receive(0)
    if part then chunk = part end
    if chunk and #chunk > 0 then recv_buffer = recv_buffer .. chunk end
    
    while true do
        local nl = string.find(recv_buffer, "\n")
        if not nl then break end
        local line = string.sub(recv_buffer, 1, nl - 1)
        recv_buffer = string.sub(recv_buffer, nl + 1)
        local k, f = string.match(line, "([^:]+):(%d+)")
        if k and KEYS[k] then
            current_key_mask = KEYS[k]
            input_frames_left = tonumber(f)
        end
    end
end

callbacks:add("frame", function()
    if not connected then
        if emu:currentFrame() % 120 == 0 then connect() end
        return
    end
    
    process_input()
    pcall(read_net)
    
    if emu:currentFrame() - last_state_sent > FRAME_INTERVAL then
        local ok, err = pcall(function() sock:send(get_state_json()) end)
        if not ok then connected = false end
        last_state_sent = emu:currentFrame()
    end
end)
log("Agent Bridge v5 Caricato.")