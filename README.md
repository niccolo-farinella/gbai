# Pokémon Emerald – AI Player

Questo modulo implementa un **AI player** per Pokémon Smeraldo basato su:

- un backend di navigazione sul mondo di gioco (Hoenn),
- un bridge verso l’emulatore mGBA,
- un LLM esterno (Ollama) che prende **decisioni ad alto livello**,
- una serie di file JSON che descrivono geometria, trainer/NPC e “location semantiche”.

L’obiettivo è che il modello **non impari a “camminare per Hoenn”**, ma impari a:

- scegliere **obiettivi semantici** (es. “vai al Centro Pokémon di Oldale”, “vai alla Palestra di Roxanne”),
- decidere quando curarsi, esplorare o progredire nella storia,
- delegare la navigazione low-level al backend (pathfinding + collisioni, warp, NPC dinamici).

---

## 1. Componenti principali

### 1.1 Script e moduli

- `emerald_bridge_v3.lua`  
  Script Lua per mGBA.  
  Si occupa di:
  - leggere lo stato di gioco dalla RAM di Pokémon Emerald,
  - serializzare lo stato in JSON (modalità, mappa, coordinate, HP…),
  - ricevere comandi in formato `KEY:frames` (es. `UP:14`, `A:10`) dal backend,
  - applicare gli input all’emulatore.

- `backend-movement_v4.py`  
  Backend di navigazione & pathfinding (già esistente).  
  Contiene la classe:

  - `World`  
    - carica `overworld_nav.json`,
    - costruisce il grafo delle mappe (tile walkable, warp, connections),
    - espone:
      - `map_name_from_group_num(group, num)` per risolvere `(map_group, map_num)` → nome mappa,
      - `neighbors((map_name, x, y))` per enumerare i vicini (inclusi warp/connection).

  Questo file può contenere anche un server di test e funzioni per pathfinding verso i Pokécenter: l’AI player riusa in particolare **la classe `World`**.

- `ai_player_v3.py`  
  Orchestratore centrale (AI player).  
  Si occupa di:

  - aprire un socket TCP verso `emerald_bridge_v3.lua` (mGBA),
  - mantenere lo stato corrente (`latest_state`) nel formato prodotto dal Lua,
  - usare `World` per pianificare path tra mappe, con gestione di collisioni dinamiche,
  - costruire prompt situazionali e chiamare un modello LLM via API Ollama,
  - interpretare la risposta JSON dell’LLM e:
    - inviare comandi `KEY:frames` a mGBA,
    - impostare/aggiornare obiettivi di navigazione (path) verso location semantiche.

### 1.2 File di dati

- `overworld_nav.json`  
  Descrive la geometria e la connettività di Hoenn.

  Contiene:

  - `meta`, `index` (mappatura `map_group/map_num ↔ map_name`),
  - per ogni mappa `maps[map_name]`:
    - `width`, `height`,
    - `grid[y][x]` (walkable / non walkable),
    - `warp_events` (porte, transizioni di mappa),
    - `connections` (bordi che portano ad altre mappe).

  È la **source of truth** per:
  - pathfinding globale,
  - movimenti tra mappe,
  - validazione delle coordinate.

- `overworld_metadata.json`  
  Generato automaticamente a partire dal repo `pokeemerald` (decomp).  
  Contiene, per ogni mappa:

  ```json
  {
    "meta": { ... },
    "maps": {
      "Route102": {
        "trainers": [
          {
            "position": {"x": 5, "y": 10},
            "movement_type": "MOVEMENT_TYPE_WANDER_AROUND",
            "movement_pattern": "random_walk",
            "script_label": "Route102_EventScript_Rick",
            "trainer_constant": "TRAINER_RICK",
            "opponent_id": 615,
            "trainer_type": "TRAINER_TYPE_NORMAL",
            "sight_range": 4,
            "in_nav_bounds": true,
            "object_event_index": 3
          }
        ],
        "npcs": [
          {
            "position": {"x": 8, "y": 7},
            "movement_pattern": "static",
            "script_label": "Route102_EventScript_Man",
            "flag": "FLAG_HIDE_..."
          }
        ]
      }
    }
  }


Serve per:

sapere dove sono trainer e NPC,

conoscere il loro pattern di movimento (static/patrol/random…),

sapere il raggio di vista (sight_range),

arricchire il prompt al LLM con info locali sui pericoli.

semantic_locations.json
File di location semantiche e obiettivi globali.

Esempio (versione early game fino a Rustboro):

{
  "_comment": "Mappa semantica per early game (Littleroot -> Rustboro).",
  "locations": {
    "PLAYER_HOME_BEDROOM": {
      "desc": "Camera da letto del protagonista a Littleroot.",
      "map_group": 1,
      "map_num": 1,
      "x": 4,
      "y": 4,
      "region": "LITTLEROOT",
      "type": "HOME"
    },
    "LITTLEROOT_CENTER": {
      "desc": "Centro di Littleroot Town (fuori).",
      "map_group": 0,
      "map_num": 9,
      "x": 10,
      "y": 10,
      "region": "LITTLEROOT",
      "type": "TOWN"
    },
    "BIRCH_LAB": {
      "desc": "Laboratorio del Professor Birch a Littleroot.",
      "map_group": 1,
      "map_num": 4,
      "x": 6,
      "y": 6,
      "region": "LITTLEROOT",
      "type": "LAB"
    },
    "ROUTE_101_MID": {
      "desc": "Zona centrale della Route 101.",
      "map_group": 0,
      "map_num": 16,
      "x": 10,
      "y": 10,
      "region": "EARLY_GAME",
      "type": "ROUTE"
    },
    "OLDALE_CENTER": {
      "desc": "Centro di Oldale Town (fuori).",
      "map_group": 0,
      "map_num": 10,
      "x": 10,
      "y": 10,
      "region": "EARLY_GAME",
      "type": "TOWN"
    },
    "OLDALE_POKECENTER": {
      "desc": "Centro Pokémon di Oldale Town.",
      "map_group": 2,
      "map_num": 2,
      "x": 7,
      "y": 4,
      "region": "EARLY_GAME",
      "type": "HEAL"
    },
    "ROUTE_102_MID": {
      "desc": "Zona centrale della Route 102.",
      "map_group": 0,
      "map_num": 17,
      "x": 25,
      "y": 10,
      "region": "EARLY_GAME",
      "type": "ROUTE"
    },
    "PETALBURG_POKECENTER": {
      "desc": "Centro Pokémon di Petalburg City.",
      "map_group": 8,
      "map_num": 4,
      "x": 7,
      "y": 4,
      "region": "MID_GAME",
      "type": "HEAL"
    },
    "RUSTBORO_CENTER": {
      "desc": "Centro di Rustboro City (fuori).",
      "map_group": 0,
      "map_num": 3,
      "x": 20,
      "y": 30,
      "region": "EARLY_GAME",
      "type": "CITY"
    },
    "RUSTBORO_POKECENTER": {
      "desc": "Centro Pokémon di Rustboro City.",
      "map_group": 11,
      "map_num": 5,
      "x": 7,
      "y": 4,
      "region": "EARLY_GAME",
      "type": "HEAL"
    },
    "RUSTBORO_GYM": {
      "desc": "Palestra di Roxanne (tipo Roccia).",
      "map_group": 11,
      "map_num": 3,
      "x": 5,
      "y": 10,
      "region": "EARLY_GAME",
      "type": "GYM"
    }
  },
  "objectives": [
    "Heal at nearest HEAL-type location if average team HP < 30%.",
    "Progress the main story: obtain starter, deliver package to Rustboro, then challenge RUSTBORO_GYM.",
    "Avoid unnecessary battles when the team is weak; seek trainer battles on ROUTE-type locations when grinding is needed."
  ]
}


Questo file è il vocabolario di obiettivi che il modello può usare quando sceglie MOVE.
Non deve mai decidere coordinate nude, ma sempre un target di questo elenco.

2. Architettura runtime

Flusso dati semplificato:

mGBA + emerald_bridge_v3.lua

legge dalla RAM di Emerald la modalità (mode), mappa (map_group, map_num), coordinate x, y, HP, informazioni di base del party, ecc.;

invia periodicamente una riga JSON su TCP al backend;

riceve comandi KEY:frames (es. UP:14, A:10) e li applica come input al gioco.

AI Player (ai_player_v3.py)

apre il socket server (porta configurata, es. 8765) e accetta la connessione da mGBA;

mantiene latest_state (l’ultimo JSON ricevuto da mGBA);

usa World (da backend-movement_v4.py) per interpretare map_group/map_num e pianificare path cross-map;

costruisce un prompt con:

stato corrente,

location semantica più vicina,

elenco di semantic_locations,

objectives,

riassunto dei trainer/NPC sulla mappa corrente (da overworld_metadata).

chiama un modello LLM su Ollama (/api/generate) e riceve un JSON con la decisione:

action, target, button, policy.

esegue la decisione:

se action == "MOVE" → calcola path verso target semantico usando World + BFS, con gestione di blocked_tiles per collisioni dinamiche;

se action == "PRESS" → invia il bottone (A/B/START/SELECT) a mGBA;

se action == "EXPLORE" → esegue un singolo passo casuale;

se action == "WAIT" → non manda input.

Pathfinding & collisioni dinamiche

la classe Navigator usa World.neighbors((map_name, x, y)) per eseguire BFS globale;

mantiene:

current_dirs: la lista di direzioni ("up", "down", "left", "right") da eseguire,

blocked_tiles: insieme di (map_name, x, y) che hanno prodotto collisione dinamica (NPC/trainer che si muovono, script temporanei, ecc.);

ogni volta che un input non produce cambiamento di stato (coordinate / mappa), il tile target viene marcato in blocked_tiles e il path viene ricalcolato verso lo stesso target.

3. Interfaccia verso il modello (LLM contract)

Questa sezione è pensata come documento di interfaccia: può essere riutilizzata come system prompt o documentazione per qualsiasi modello LLM che voglia giocare tramite questo backend.

3.1 Cosa riceve il modello

Ad ogni decision step il modello riceve:

uno stato JSON (non mostrato per esteso qui) con almeno:

mode: "OVERWORLD" o "BATTLE";

map:

group, num (identificatori numerici della mappa),

x, y (coordinate tile);

party:

hp, max_hp per il Pokémon attivo (proxy della salute del team).

informazioni contestuali aggiuntive:

una semantic location key più vicina alla posizione corrente
(es. LITTLEROOT_CENTER, OLDALE_POKECENTER, RUSTBORO_GYM).

un dizionario di semantic locations:

{
  "location_key": {
    "type": "HEAL" | "GYM" | "TOWN" | "CITY" | "ROUTE" | "LAB" | "...",
    "region": "LITTLEROOT" | "EARLY_GAME" | "...",
    "map_group": 0,
    "map_num": 10
  }
}


una lista di obiettivi ad alto livello, ad esempio:

[
  "Heal at nearest HEAL-type location if average team HP < 30%.",
  "Progress the main story: obtain starter, deliver package to Rustboro, then challenge RUSTBORO_GYM.",
  "Avoid unnecessary battles when the team is weak; seek trainer battles on ROUTE-type locations when grinding is needed."
]


un riassunto dei trainer/NPC presenti nella mappa, ad esempio:

"4 trainers (max sight 4, patrol=1)."


Il modello non vede mai la griglia di tile.
Tutto il pathfinding low-level, le collisioni e i warp sono gestiti dal backend.

3.2 Cosa decide il modello

Ad ogni step il modello deve restituire un solo oggetto JSON, e nient’altro, con il seguente schema:

{
  "action": "MOVE" | "PRESS" | "EXPLORE" | "WAIT",
  "target": "string or null",
  "button": "A" | "B" | "START" | "SELECT" | null,
  "policy": {
    "avoid_optional_trainers": true | false,
    "allow_grass_encounters": true | false
  }
}

Semantica dei campi

"MOVE": scegli una semantic location come obiettivo di navigazione.

target deve essere una delle chiavi note in semantic_locations.locations
(es. "OLDALE_POKECENTER", "RUSTBORO_GYM").

Il backend calcola un path e muove il personaggio passo-passo fino al target (con warp, transizioni e collisioni dinamiche gestite automaticamente).

"PRESS": premi un tasto per avanzare dialoghi, menu o prompt di battaglia.

tipicamente "button": "A".

usalo quando un textbox o un prompt in battle richiedono conferma.

"EXPLORE": consenti al backend di effettuare un movimento locale esplorativo (un singolo passo).

utile per esplorare senza un obiettivo globale rigoroso.

"WAIT": non fare nulla in questo tick.

caso raro; usalo solo quando muoversi o premere un tasto sarebbe chiaramente sfavorevole.

3.3 Policy hints

L’oggetto policy è opzionale ma raccomandato. Serve a esprimere preferenze di alto livello:

avoid_optional_trainers:

true → evita, quando possibile, i trainer opzionali (team debole, bassa salute).

false → i trainer opzionali sono accettabili o desiderati (fase di grinding).

allow_grass_encounters:

true → incontri in erba alta sono accettabili o desiderati.

false → cerca di minimizzare gli incontri wild.

Il backend può usare queste informazioni per modificare il costo dei tile o dei cone di visione dei trainer nel pathfinding (es. evitare i tile di vista dei trainer opzionali, penalizzare l’erba alta).

3.4 Linee guida di strategia

Alcune linee guida da seguire:

Se hp / max_hp < 0.3, è fortemente consigliato decidere di curarsi:

{
  "action": "MOVE",
  "target": "<nearest HEAL-type location>",
  "button": null,
  "policy": {
    "avoid_optional_trainers": true,
    "allow_grass_encounters": false
  }
}


Per progredire nella storia (early game), priorità tipica:

"BIRCH_LAB" e "LITTLEROOT_CENTER" all’inizio.

"ROUTE_101_MID" -> "OLDALE_CENTER" -> "ROUTE_102_MID" -> "RUSTBORO_CENTER".

Infine "RUSTBORO_GYM" per la prima palestra.

In modalità "BATTLE", se non esiste ancora un’interfaccia di battle più ricca, preferire:

{
  "action": "PRESS",
  "target": null,
  "button": "A",
  "policy": {
    "avoid_optional_trainers": true,
    "allow_grass_encounters": true
  }
}


per far avanzare i prompt di battaglia e i dialoghi.

3.5 Requisiti di output

L’output del modello deve essere solo il JSON di decisione:

nessun testo fuori dal JSON,

nessun commento,

nessuna spiegazione,

nessuna seconda struttura JSON.

Il JSON deve essere valido:

nessuna virgola finale,

nessun commento inline,

chiavi e stringhe quotate correttamente.

4. Esecuzione e configurazione
4.1 Prerequisiti

Python 3.x

mGBA con supporto Lua

Repo pokeemerald per generare/aggiornare overworld_nav.json e overworld_metadata.json (già fatto a monte)

Server Ollama configurato con il modello:

ad es. llama3.2:3b-instruct-q8_0

endpoint HTTP accessibile da ai_player_v3.py

4.2 Parametri principali (ai_player_v3.py)

Nel file:

HOST = "127.0.0.1"
PORT = 8765

WORLD_FILE     = "overworld_nav.json"
METADATA_FILE  = "overworld_metadata.json"
SEMANTIC_FILE  = "semantic_locations.json"

OLLAMA_URL   = "http://<host>:<port>/api/generate"
OLLAMA_MODEL = "llama3.2:3b-instruct-q8_0"


Adattare:

OLLAMA_URL all’indirizzo del tuo server Ollama,

eventualmente OLLAMA_MODEL al nome del modello disponibile.

4.3 Flusso di esecuzione

Avvia mGBA con ROM di Pokémon Emerald.

Carica ed esegui emerald_bridge_v3.lua in mGBA.

Avvia il backend AI:

python ai_player_v3.py


Lo script attende la connessione da mGBA (wait_for_gba()).

Una volta connessi:

lo stato viene letto in streaming dal Lua client,

ai_player_v3.py inizia il loop decisionale:

se c’è un path attivo → consuma uno step e controlla il nuovo stato,

se non c’è un path attivo → chiama il modello per una nuova decisione.
