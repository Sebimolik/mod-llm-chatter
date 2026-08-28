# mod-llm-chatter - Logic Documentation

This document describes the current runtime logic of `mod-llm-chatter`.

It is meant to answer two practical questions:

1. What does the module do at runtime?
2. Where does that logic live?


---

## 1. Overview

`mod-llm-chatter` creates ambient and reactive bot chat for AzerothCore.
It combines C++ event capture and in-game delivery with a Python bridge
that builds prompts, calls an LLM, and writes final messages back to the
database.

High-level behavior:

- ambient General-channel chatter in the open world
- ambient Guild statements and two- or three-bot conversations
- player-driven Guild replies with per-login rolling session memory
- reactive party chatter for grouped bots
- General-channel reactions to real player chat
- world event chatter for weather, holidays, transports, and nearby
  points of interest
- battleground chatter for flag, node, PvP, milestone, and related BG
  events
- PvE raid chatter for boss encounters, lifted group features, and
  idle morale
- real-time subzone lore tracking with ~3,000 subzone descriptions
  injected into prompts
- screenshot vision: host-side agent captures the game window, sends
  to a vision LLM, and bots react to what the player sees on screen
- proximity chatter: ambient `/say` conversations between bots, NPCs,
  and the player as they move through the world, with NPC speech bubbles
  and natural player reply detection
- in-game addon bridge: `.llmc` command lets the Chatter Companion addon
  read and write bot personality traits and tone from the game UI
- MultiBot-Chatless bridge coexistence: hidden `MBOT` addon traffic is
  ignored by chatter logging and left for `mod-multibot-bridge` by
  default, so chatter does not block the addon's chatless
  communication. An optional fallback handler remains available for
  installs without the bridge

---

## 2. Runtime Pipeline

### C++ side

The C++ module:

- detects hooks and world events
- inserts queue rows into MySQL
- runs the message delivery tick
- plays party text emotes when appropriate

### Host-side screenshot agent (optional)

The screenshot agent runs on the host machine (outside Docker):

- captures the WoW game window at configurable intervals
- sends screenshots to a vision LLM for structured analysis
- inserts `bot_group_screenshot_observation` events directly into MySQL

### Python side

The Python bridge:

- polls `llm_chatter_events` and `llm_chatter_queue`
- routes by event type
- builds prompt context
- calls the configured LLM provider
- writes final output rows to `llm_chatter_messages`

### Delivery

After Python writes the message rows, C++ delivers them in game on the
world tick.

Party channel may play text emotes.
General, Guild, raid, and battleground delivery do not play text
emotes.

---

## 2b. Queueing, Timing, and the Main Bridge Loop

The runtime is split into three DB-backed stages, and the Python bridge
does not process all of them inline in one thread.

### The three stages

1. **Request/event creation**
   - C++ inserts either:
     - legacy ambient requests into `llm_chatter_queue`
     - reactive/event work into `llm_chatter_events`
2. **Bridge processing**
   - Python claims ready work, builds prompts, calls the LLM, and writes
     final chat rows to `llm_chatter_messages`
3. **In-game delivery**
   - C++ world tick delivers ready rows from `llm_chatter_messages`

### What the main bridge loop actually does

`llm_chatter_bridge.py` runs one long-lived coordinator loop. That loop:

- harvests completed futures
- runs periodic cleanup
- claims ready events from `llm_chatter_events`
- submits them to worker threads
- periodically submits timer-like jobs such as:
  - legacy ambient request processing
  - idle group chatter checks
  - bot-question checks
  - pre-cache refills
- sleeps for `LLMChatter.Bridge.PollIntervalSeconds` between iterations

So the bridge is:

- one coordinator loop
- a `ThreadPoolExecutor`
- multiple worker tasks running in parallel

It is **not** "one loop that processes every message end to end by
itself."

### Event workers vs timer-style jobs

Reactive/event rows from `llm_chatter_events` are claimed in priority
order and then processed in worker threads via `process_single_event()`.

Timer-style jobs are separate worker submissions launched only when
their interval elapses:

- idle chatter
- bot questions
- pre-cache refill
- legacy ambient request processing

These jobs share the same executor, but they are not fetched from the
event table.

### Group serialization

The bridge allows parallel processing overall, but events with the same
`group_id` are wrapped in a per-group lock. That means one party's work
is serialized even while different groups can process concurrently.

Session 69 refined this by separating urgent/high and filler lock lanes
for the same group so queued filler work is less likely to block queued
urgent work.

### Event queue ordering

`llm_chatter_events` is fetched with:

- `status = 'pending'`
- `react_after <= NOW()` or null
- `expires_at > NOW()` or null
- `ORDER BY priority DESC, created_at ASC`

So event priority matters at claim time.

### Legacy ambient queue ordering

`llm_chatter_queue` is the older ambient queue. It is processed FIFO:

- `ORDER BY created_at ASC`

`LLMChatter.MaxPendingRequests` currently gates this queue only.

### Final message delivery ordering

Python inserts final rows into `llm_chatter_messages` with:

- `deliver_at = NOW() + delay_seconds`

C++ delivery now has two modes:

- fallback mode:
  - `WHERE delivered = 0 AND deliver_at <= NOW()`
  - `ORDER BY deliver_at ASC LIMIT 1`
- priority mode, when
  `LLMChatter.PrioritySystem.Enable = 1` and
  `LLMChatter.PrioritySystem.DeliveryOrderEnable = 1`:
  - ready rows are `LEFT JOIN`ed back to `llm_chatter_events`
  - ordered by `COALESCE(e.priority, 0) DESC, m.deliver_at ASC`

So urgent event-backed rows can now overtake filler rows at final
delivery time, while ambient rows with `event_id = NULL` stay lowest
priority.

### Party chat pacing gate

Party chat has an additional per-group pacing layer to avoid several
LLM-generated lines landing in the chat window at nearly the same time.

The gate uses `llm_party_chat_pacing`:

- Python inserts party messages with `group_id`, `delivery_policy`, and
  `delivery_reason`, then reserves the next visible slot for that group.
- Filler work such as idle chatter, bot questions, nearby-object
  comments, screenshot observations, and observer comments can defer
  before the LLM call when the group's party chat is already busy.
- Responsive and contextual messages are delayed only enough to avoid
  overlap. Urgent and bypass-style feedback can remain immediate.
- C++ delivery refreshes the pacing row when a party line is actually
  sent, so late delivery does not cause following lines to bunch up.
- C++ instant paths that do not use `llm_chatter_messages`, including
  pre-cached combat/state/spell reactions and farewell packets, record
  gate activity after sending. They are not delayed, but they still
  suppress immediate filler spam behind them.

### Current priority behavior and remaining limits

The system now has real priority behavior across multiple stages, but it
is still not a single perfect global scheduler across:

- legacy ambient requests
- event workers
- background timer jobs
- final delivery

What Session 69 added:

- centralized C++ event priority bands
- config-backed react ranges per tier
- bridge urgent-backlog yield for filler jobs
- priority-aware final delivery ordering
- bridge safety mode that suppresses filler first under overload

Also, `GlobalMessageCap` and `TransportBypassGlobalCap` are currently
legacy config values. They are no longer the intended main control path;
the active design direction is priority tiers plus provider-safety
suppression.

### Timing layers

There are two separate delays that are easy to confuse:

- **Reaction delay**: C++ sets `react_after` when queueing an event
- **Delivery delay**: Python sets `deliver_at` when writing the final
  message row

Most delivery delays use `calculate_dynamic_delay()` in
`chatter_shared.py`:

- `responsive=True` for player-directed replies
- ambient/group conversation paths can also include reading time from
  `prev_message_length`

This separation is important if you plan to redesign priorities, because
priority currently influences claim order more than final speak order.

---

## 3. C++ File Ownership

### `src/LLMChatterScript.cpp`

Registration coordinator only. Calls:

- `AddLLMChatterWorldScripts()`
- `AddLLMChatterGroupScripts()`
- `AddLLMChatterPlayerScripts()`
- `AddLLMChatterBGScripts()`
- `AddLLMChatterRaidScripts()`

### `src/LLMChatterShared.cpp`

Owns shared helpers used across domains:

The shared timing logic now uses table-driven priority and reaction-delay
registries instead of a single long conditional block.

- `EscapeString()`
- `JsonEscape()`
- `GetZoneName()`
- `GetChatterClassName()`
- `GetRaceName()`
- `BuildBotIdentityFields()` — emits `bot_name`, `bot_class`,
  `bot_race`, `bot_gender`, `bot_level` into event JSON
- `QueueChatterEvent()`
- `BuildBotStateJson()`
- `AppendRaidContext()`
- `GroupHasBots()`
- `CanSpeakInGeneralChannel()`
- `GetTextEmoteName()` — reverse emote ID-to-name lookup (170+ entries)
- `SendUnitTextEmote(Unit*, uint32, const std::string&)` — unified text emote
  sender for any unit (bot or creature); both `SendBotTextEmote` overloads
  delegate to this; `SendCreatureTextEmote` was removed in favour of this
  single shared implementation
- `IsEventOnCooldown()` / `SetEventCooldown()` — shared event cooldown
  helper (cache-first, DB fallback) used by world, ambient, and nearby
- shared link conversion helpers
- shared emote and delivery helpers

Critical contract:

- direct callers of `QueueChatterEvent()` must provide `extraData` that
  is already SQL-safe for insertion into a single-quoted SQL string
- all event hooks include `bot_gender` (and `player_gender` where
  applicable) in the `extra_data` JSON so Python prompt builders can
  use correct pronouns via `resolve_gender()`

### `src/LLMChatterWorld.cpp`

Owns world and environment behavior:

- `LLMChatterWorldScript`
- `LLMChatterGameEventScript`
- `LLMChatterALEScript`
- delivery tick coordination only
- ambient and nearby delegation only
- transport polling and route announcements
- world-private `QueueEvent()`

### `src/LLMChatterDelivery.cpp`

Owns outbound delivery behavior:

- `DeliverPendingMessagesImpl()`
- DB polling for ready `llm_chatter_messages` rows
- pre-send facing selection
- party, raid, BG, yell, and General delivery paths
- post-send delivery and retry updates

### `src/LLMChatterAmbient.cpp`

Owns ambient world/event behavior:

- day/night transitions
- holiday start/stop routing
- weather state and transition handling
- ambient zone selection and faction choice
- ambient chatter queue writes

### `src/LLMChatterNearby.cpp`

Owns nearby scan behavior:

- nearby-object and nearby-creature scanning
- POI helper structs and interest scoring
- nearby-local cooldown state
- direct nearby event queue insertion

### `src/LLMChatterGroupInternal.h`

Shared internal header for the group domain TUs:

- struct definitions: `GroupJoinEntry`, `GroupJoinBatch`,
  `QuestAcceptEntry`, `QuestAcceptBatch`
- extern declarations for all shared cooldown maps, batch containers,
  mutexes, emote cooldowns, named boss cache
- `EmoteTargetType` enum
- shared helper and domain entry-point declarations

### `src/LLMChatterGroup.cpp`

Retains core group glue:

- shared state variable definitions
- shared helpers: `GroupHasRealPlayer`, `GetRandomBotInGroup`,
  `CountBotsInGroup`, `IsLikelyPlayerbotControlCommand`, pre-cache
  helpers
- MultiBot-Chatless bridge coexistence. By default,
  `mod-multibot-bridge` owns `MBOT` packets and mod-llm-chatter only
  suppresses its own ignored-addon debug log for that hidden protocol.
  It does not consume or block the addon's communication in this mode.
  When `LLMChatter.MultiBotCompat.Enable = 1`, a fallback handler
  answers handshake, ping, roster, state, and detail refreshes for
  installs without the bridge
- `CleanupGroupSession()` coordinator
- named-boss cache
 - thin `LLMChatterGroupPlayerScript` wrappers
 - group registration

### `src/LLMChatterGroupCombat.cpp`

Owns the moved group PlayerScript implementation bodies plus the
remaining zone/state helpers:

- kill, death, loot, combat, chat, level, quest objectives, quest
  complete, achievement, spell, resurrect, corpse run, dungeon entry,
  and emote dispatch hook implementations
- `HandleGroupPlayerUpdateZone()`
- `CheckGroupCombatState()`
- file-local `QueueStateCallout()`

### `src/LLMChatterGroupJoin.cpp`

Owns join batching and GroupScript:

- `QueueBotGreetingEvent()`
- `EnsureGroupJoinQueued()`
- `FlushGroupJoinBatches()`
- `LLMChatterGroupScript` (GroupScript: `OnAddMember`, `OnRemoveMember`
  with farewell, `OnDisband`)

### `src/LLMChatterGroupEmote.cpp`

Owns emote reaction system:

- `DelayedMirrorEmoteEvent`, `DelayedCreatureMirrorEmoteEvent`
- emote static data: mirror map, denylist, combat callouts, contagious
  set
- `HandleEmoteAtGroupBot()`, `HandleEmoteAtCreature()`,
  `HandleEmoteObserver()`
- `EvictEmoteCooldowns()`

### `src/LLMChatterGroupQuest.cpp`

Owns quest accept batching and CreatureScript:

- `FlushQuestAcceptBatches()`
- `LLMChatterCreatureScript` (`CanCreatureQuestAccept` with
  debounce/immediate paths)

### `src/LLMChatterPlayer.cpp`

Owns player General-channel behavior:

- `LLMChatterPlayerScript`
- `EnsureBotInGeneralChannel()`
- General chat cooldowns
- `OnPlayerCanUseChat(..., Channel*)`
- writes to `llm_general_chat_history`

### `src/LLMChatterBG.cpp`

Owns battleground-specific hooks and BG queue helpers.

---

## 4. Current Python File Ownership

### Bridge and orchestration

- `tools/llm_chatter_bridge.py`
- `tools/chatter_event_registry.py`
- `tools/chatter_ambient.py`

### Group domain

- `tools/chatter_group.py`
- `tools/chatter_group_handlers.py`
- `tools/chatter_handler_pipeline.py`
- `tools/chatter_group_prompts.py`
- `tools/chatter_group_state.py`

### Routing and handler pipeline

The bridge no longer relies only on a manually maintained in-file
handler map.

- `chatter_event_registry.py` is now the Python-side event registry for
  live event types, handler module/function resolution, producer notes,
  and payload-field documentation
- `build_handler_map()` dynamically imports handlers from that registry
  during bridge startup
- `player_general_msg` still uses a local adapter path because its
  function signature differs from the group-event handlers
- `chatter_handler_pipeline.py` centralizes the shared setup/teardown
  path used by most single-reaction `bot_group_*` handlers via
  `run_group_handler()`

### General/shared support

- `tools/chatter_general.py`
- `tools/chatter_group_general_reaction.py` - queues and handles
  `bot_group_general_reaction` events so grouped bots can react in party
  chat to bot-authored General lines
- `tools/chatter_shared.py`
- `tools/chatter_text.py`
- `tools/chatter_llm.py` — LLM call dispatch, system prompt splitting
- `tools/chatter_db.py`
- `tools/chatter_links.py`
- `tools/chatter_events.py`
- `tools/chatter_prompts.py`
- `tools/chatter_constants.py`
- `tools/chatter_cache.py`
- `tools/talent_catalog.py`
- `tools/spell_names.py`

### BG / raid support

- `tools/chatter_battlegrounds.py`
- `tools/chatter_bg_prompts.py`
- `tools/chatter_raid_base.py`
- `tools/chatter_raids.py`
- `tools/chatter_raid_prompts.py`

### Emote reaction

- `tools/chatter_emote_reaction.py`
- `tools/chatter_emote_observer.py`

### Proximity chatter

- `tools/chatter_proximity.py`

### Screenshot vision

- `tools/screenshot_agent.py`
- `tools/chatter_screenshot_handler.py`

### Development tools

- `tools/chatter_request_logger.py`
- `tools/chatter_log_viewer.py`

---

## 4b. Config Pipeline

C++ and Python read configuration independently. There is no C++ →
Python config relay.

- **C++ config**: `LLMChatterConfig.cpp` loads values from
  `mod_llm_chatter.conf` via the AzerothCore `sConfigMgr` API. These
  are values that C++ needs at runtime (cooldowns, chances for C++
  hooks, thresholds). Stored as member variables in `LLMChatterConfig`.

- **Python config**: `parse_config()` in `chatter_shared.py` reads the
  same `.conf` file directly from disk on bridge startup. Values are
  stored in a Python dict and accessed via `config.get('Key', default)`.
  Python-only config keys (e.g., `BotQuestionChance`, `IdleChance`,
  `ActionChance`, `ConversationBias`) are never loaded by C++ — they
  exist only in the `.conf` file and are read only by Python.

When adding a new Python-only config key:
1. Add the key + comment to `conf/mod_llm_chatter.conf.dist`
2. Add the key to your active server config file
3. Read it in Python via `config.get('LLMChatter.GroupChatter.KeyName', default)`
4. No C++ changes needed

Some narrow server-side compatibility switches may be read directly
through `sConfigMgr` instead of being stored on `LLMChatterConfig`.
`LLMChatter.MultiBotCompat.Enable` follows that shape.

---

## 5. Supported Providers

Configured through:

- `LLMChatter.Provider`
- `LLMChatter.Model`

Supported providers:

- Anthropic
- OpenAI
- Google Gemini
- OpenRouter
- Ollama

Examples:

```ini
LLMChatter.Provider = anthropic
LLMChatter.Model = haiku
```

```ini
LLMChatter.Provider = openai
LLMChatter.Model = gpt4o-mini
```

```ini
LLMChatter.Provider = google
LLMChatter.Model = gemini-3.1-flash-lite
```

```ini
LLMChatter.Provider = google
LLMChatter.Model = gemini-2.5-flash
LLMChatter.Google.ThinkingBudget = 0
```

```ini
LLMChatter.Provider = openrouter
LLMChatter.Model = openai/gpt-4o-mini
LLMChatter.OpenRouter.ApiKey = sk-or-v1-xxxxx
```

```ini
LLMChatter.Provider = ollama
LLMChatter.Model = qwen3:4b
```

### System prompt support

`call_llm()` in `chatter_llm.py` supports automatic system/user
prompt splitting. Prompt builders can return a `PromptParts` object
(from `chatter_shared.py`) instead of a plain string. `PromptParts`
wraps a single prompt string, and `_split_prompt()` in
`chatter_llm.py` detects the boundary between format/rules
instructions and scene-specific content, splitting them into a
system message and a user message.

Provider behavior:

- **Anthropic**: system content passed via the `system=` parameter
  on the API call (native system prompt support); sampling temperature
  is sent through `extra_body` for Anthropic SDK v1 compatibility
- **OpenAI**: system content sent as a `{"role": "system", ...}`
  message prepended to the messages array
- **Google Gemini**: uses Google's OpenAI-compatible chat-completions
  endpoint, so system content is sent as a system role message
- **OpenRouter**: uses OpenRouter's OpenAI-compatible
  chat-completions endpoint with optional attribution headers
- **Ollama**: same as OpenAI (system role message)

When a plain string is passed to `call_llm()` instead of
`PromptParts`, the entire prompt is sent as a single user message
(backward-compatible behavior).

### Key helpers in `chatter_llm.py`

| Function | Purpose |
|---|---|
| `_split_prompt()` | Detects `PromptParts` and splits into system + user content |
| `_build_chat_messages()` | Assembles the provider-specific messages array |
| `_ollama_user_msg()` | Formats the user message for Ollama's chat API |
| `_apply_google_options()` | Applies Gemini reasoning/thinking settings for OpenAI compatibility |
| `_openrouter_headers()` | Builds optional OpenRouter attribution headers |

---

## 6. Chatter Modes

Configured through:

- `LLMChatter.ChatterMode`

Modes:

- `normal`: casual MMO-style chat
- `roleplay`: in-character, race/class-influenced chat

The Python prompt builders are mode-aware and choose different tone,
mood, and style guidance based on the configured mode.

---

## 7. Ambient Open-World Chatter

Ambient chatter is the original module behavior.

### Trigger shape

The system periodically:

1. checks for a valid real player in the open world
2. finds eligible bots in the same zone
3. filters to bots that can actually speak in that zone's General
   channel
4. queues either a one-line statement or a multi-bot conversation

### Eligibility rules

Ambient chatter candidates must:

- be bots
- be in the same zone as the real player
- be in the world and alive
- not be grouped with a real player
- be members of the current General channel

### Message families

Ambient requests can become:

- plain statements
- quest statements
- loot statements
- quest + reward statements
- trade-style statements
- NPC gossip statements/conversations
- bot gossip statements/conversations
- multi-bot conversations

NPC and bot gossip are selected by additive Python-side RNG gates
(`AmbientNpcGossipChance`, `AmbientBotGossipChance`) before the
regular plain/quest/loot/trade/spell mix. If a target cannot be
resolved, the request falls back to plain ambient chatter.

NPC gossip targets are service/social NPCs spawned in the current zone,
with the prompt receiving the NPC name, title/subname, function, creature
kind, and combat-style class when available. Bot gossip targets are
online random bots in the current zone, excluding the speaking bots, with
the prompt receiving name, race, class, and level.

Recently selected gossip targets are held in a per-zone cooldown
(`AmbientGossipTargetCooldownSeconds`) so high test chances do not make
the same NPC or bot become the subject repeatedly.

Prompt generation and runtime logic live mainly in:

- `tools/chatter_ambient.py`
- `tools/chatter_prompts.py`
- `tools/chatter_shared.py`

---

## 8. General-Channel Player Reactions

When a real player speaks in General, the module can queue a
`player_general_msg` event.

### C++ ownership

Current C++ ownership lives in:

- `LLMChatterPlayer.cpp`

Relevant responsibilities:

- `OnPlayerCanUseChat(..., Channel*)`
- bot membership enforcement for General
- per-zone General cooldown handling
- writing/retaining `llm_general_chat_history`

Shared note:

- `CanSpeakInGeneralChannel()` is a shared helper in
  `LLMChatterShared.cpp`; `LLMChatterPlayer.cpp` still owns
  `EnsureBotInGeneralChannel()` and the General-channel hook paths

### Python ownership

Python handling lives in:

- `tools/chatter_general.py`

That path:

- selects responding bot(s)
- builds the player-reaction prompt
- dispatches the reaction through the bridge path

### Relevant files

| File | Purpose |
|---|---|
| `LLMChatterPlayer.cpp` | General-channel hook, cooldowns, history writes |
| `LLMChatterConfig.h/.cpp` | General-channel config |
| `chatter_general.py` | Prompt building and event handler |
| `chatter_shared.py` | `PromptParts` class, addressed-bot detection, quick LLM analysis |
| `llm_chatter_bridge.py` | Event dispatch entry |

### General-to-party relay

Bot-authored General messages can trigger a party-chat reaction for an
active group in the same zone. The bridge queues
`bot_group_general_reaction` from General-producing Python paths, then
`tools/chatter_group_general_reaction.py` generates either one party
statement or a short 2-3 bot party conversation.

The relay chance is controlled by
`LLMChatter.GroupChatter.GeneralRelayChance` and defaults to 10%. When a
relay fires, the first party line is scheduled for 3-6 seconds after the
General line's planned visible time. The first party line must reference
the General speaker by name.

---

## 9. Group Chatter

Group chatter covers party-channel bot reactions when bots are grouped
with a real player.

### Event families

Examples include:

- bot group join
- group player message
- kill and wipe reactions
- death and resurrection reactions
- loot reactions
- spell cast reactions
- quest accept/objective/complete reactions
- zone transitions
- dungeon entry reactions
- nearby-object observations

Note: subzone discovery reactions (`OnPlayerGiveXP` with `XPSOURCE_EXPLORE`) have been
removed. They caused duplicate messages alongside zone transition events. Discovery
context is now covered by zone transition events instead.

### C++ ownership

Current group-side ownership is in:

- `LLMChatterGroup.cpp`
- `LLMChatterGroupCombat.cpp`

Important responsibilities:

- batch accumulation and flush
- per-group cooldown and dedup state
- named-boss cache loading
- combat state callouts
- direct event queue inserts for many `bot_group_*` events

### Python ownership

Current Python group ownership is split across:

- `chatter_group.py`
- `chatter_group_handlers.py`
- `chatter_group_prompts.py`
- `chatter_group_state.py`

### Pre-cache path

Some group reactions use a pre-cache path for faster replies.

That path is separate from live event generation and lives mainly in:

- `tools/chatter_cache.py`
- `tools/chatter_group_prompts.py`

---

## 10. World Events

World-owned C++ logic now spans `LLMChatterWorld.cpp`,
`LLMChatterAmbient.cpp`, and `LLMChatterNearby.cpp`.

### Main categories

- holiday events
- day/night transitions
- weather changes and weather ambient chatter
- transport arrivals triggered by transport objects entering a new
  player-relevant zone, with delivery in General channel
- pending message delivery
- nearby-object / nearby-creature scan events
- proximity chatter scans (delegated to `LLMChatterProximity.cpp`)

### World-to-group boundary

The world layer intentionally calls a narrow group-owned surface:

- `LoadNamedBossCache()`
- `CheckGroupCombatState()`
- `FlushQuestAcceptBatches()`
- `FlushGroupJoinBatches()`

That surface is declared in `LLMChatterGroup.h`.

---

## 11. Nearby Object / Creature Awareness

Bots can notice nearby points of interest and comment on them or start a
short group conversation.

### C++ ownership

Current C++ scanning logic lives in:

- `LLMChatterNearby.cpp`

Specifically:

- `CheckNearbyGameObjects()`
- `NearbyGameObjectCheck`
- `NearbyCreatureCheck`

### Scanned interest types

The scan can surface things like:

- quest NPCs
- rare mobs
- trainers
- vendors
- innkeepers
- flightmasters
- chests
- text / book objects
- spell-focus objects
- critters and beasts

### Suppression

The feature is gated by:

- RNG chance
- per-group per-zone cooldown
- per-bot per-name cooldown
- combat suppression
- mounted/flying/BG suppression

### Python handling

Python handling lives in:

- `chatter_group_handlers.py`
- `chatter_group_prompts.py`

That path can produce either:

- a single reaction
- a short nearby-object conversation

---

## 12. Weather, Transport, and Holiday Behavior

### Weather

The world layer tracks current weather state per zone and queues:

- `weather_change`
- `weather_ambient`

Python can then naturally reference weather in prompts and event
reactions.

### Transport

Transport arrivals are world-owned C++ events with verified bot GUIDs in
`extra_data` so Python only uses bots that can actually speak in the
zone channel.

Current transport logic is:

1. poll live transport objects on the world timer
2. detect an actual zone/map transition per live transport GUID
3. ignore the transition unless the destination zone currently contains
   a real player
4. choose eligible General-channel bots already in that zone
5. write those GUIDs into `verified_bots`
6. suppress redispatch for the same transport entry until the transport
   cooldown window expires

This is intentionally an early-warning model. The message should appear
while the boat or zeppelin is approaching, not only after it has fully
docked.

### Holidays

Holiday chatter is also world-owned and queues zone/city-specific event
rows instead of speaking directly.

---

## 13. Battleground Chatter

Battleground-specific logic is self-contained in its own files.

### C++ ownership

- `LLMChatterBG.cpp`
- `LLMChatterBG.h`

### Python ownership

- `chatter_battlegrounds.py`
- `chatter_bg_prompts.py`
- `chatter_raid_base.py`

### Typical BG events

- match start / end
- flag pickup / drop / capture / return
- node assault / capture
- PvP kill
- score milestones
- arrival greetings
- BG idle chatter

### Current BG routing policy

The older broad "party plus battleground" duplication is no longer the
intended behavior.

BG-wide only:

- match start
- match end
- flag pickup / drop / capture / return

Subgroup/party only:

- PvP kills
- node assault / capture chatter
- score milestones
- spell/state chatter
- idle chatter
- flag-carrier self-messages

This keeps strategic objective callouts visible to the whole team while
reducing duplicated tactical chatter.

### BG brevity tuning

BG prompts now use a dedicated token cap plus stricter brevity
instructions so chatter stays short and tactical.

| Key | Default | Purpose |
|---|---|---|
| `BGChatter.MaxTokens` | 32 | Max token cap for BG prompt paths |

### Flag-carrier context persistence

BG prompts continue to receive both:

- `friendly_flag_carrier`
- `enemy_flag_carrier`

from `AppendBGContext()` in `LLMChatterBG.cpp`.

That means if a real player is carrying the enemy flag, later BG prompt
requests continue to know that until the flag is dropped, returned, or
captured.

---

## 13a. PvE Raid Chatter

Raid chatter extends group features into raid instances and adds
raid-specific events.

### Phase 1 (Session 70b): Boss Encounters

C++ `LLMChatterRaid.cpp` owns raid-specific boss hooks:

- `raid_boss_pull` — fires on boss engage
- `raid_boss_kill` — fires on boss death
- `raid_boss_wipe` — fires on raid wipe during boss encounter

Python handling lives in:

- `chatter_raids.py` — event handlers
- `chatter_raid_prompts.py` — prompt builders with instance/wing context

### Phase 2 (Session 71): Lifted Guards and Morale

Five suppression guards were changed from `IsRaid() || IsBattleground()`
to BG-only, allowing existing group features to fire inside raids:

- **Loot** — epic quality gate (quality >= 4) for raids
- **Nearby objects** — `CheckNearbyGameObjects()` no longer suppressed
- **Quest objectives** — now BG-only guard
- **Quest complete** — now BG-only guard
- **Quest accept batch** — now BG-only guard
- **Join batch** — now BG-only guard

Guards kept suppressed (not suitable for raids):
OnAddMember, OnRemoveMember, LevelUp, Discovery.

Zone transitions are now allowed in raids (Session 94) — subzone
changes fire inside raid instances for wing/area commentary.

New event: `raid_idle_morale` — ambient morale chatter between boss
encounters. `CheckRaidIdleMorale()` in `LLMChatterWorld.cpp` fires on
the world timer. Suppressed during active combat only — dead/ghost
members no longer block morale (Session 94 relaxation).

### Phase 3 (Session 94): Battle Cries, Banter, Idle Boost

**Battle cries**: `_maybe_raid_battle_cry()` in
`chatter_group_handlers.py`. After a party combat reaction in a raid
instance, a different bot shouts a short battle cry in raid chat.
`build_raid_battle_cry_prompt()` produces 5-15 word race/class
flavored war shouts. `BattleCryChance=70`, no cooldown.

**Raid banter**: `build_raid_banter_prompt()` in
`chatter_raid_prompts.py`. Casual between-pulls humor with 10 random
topic hints. `process_raid_idle_morale_event()` picks morale or
banter with 50/50 probability.

**Raid idle boost**: Groups in `RAID_MAP_IDS` (24 Classic/TBC/WotLK
raid map IDs in `chatter_constants.py`) get 2x idle chance and 0.5x
idle cooldown.

**Dead bot awareness**: Idle chatter queries `characters.health` via
LEFT JOIN. Dead bots (`health==0`) get ghost-themed prompt injection
and `[DEAD]` tags in conversation participant lists.

### C++ ownership

| File | Responsibility |
|---|---|
| `LLMChatterRaid.cpp` | Boss pull/kill/wipe hooks |
| `LLMChatterWorld.cpp` | `CheckRaidIdleMorale()` |
| `LLMChatterGroup.cpp` | Lifted guards for loot/quest/join-batch |
| `LLMChatterShared.cpp` | `AppendRaidContext()` |
| `LLMChatterConfig.h/.cpp` | 3 morale config keys |

### Python ownership

| File | Responsibility |
|---|---|
| `chatter_raids.py` | Boss, morale, and banter event handlers |
| `chatter_raid_prompts.py` | Boss, morale, battle cry, and banter prompts |
| `chatter_group_handlers.py` | `_maybe_raid_battle_cry()` (combat follow-up) |
| `chatter_raid_base.py` | Shared dispatch, subgroup workers |
| `llm_chatter_bridge.py` | Event routing for `raid_*` types |

### Config keys

| Key | Default | Purpose |
|---|---|---|
| `MoraleEnable` | 1 | Enable/disable morale chatter |
| `MoraleCooldown` | 300 | Per-group cooldown (seconds) |
| `MoraleChance` | 30 | % chance per check |
| `BattleCryChance` | 70 | % chance for raid battle cry on combat |

### Dispatch model

Raid events use `dual_worker_dispatch()` from `chatter_raid_base.py`
for sub-group (party chat) delivery. Boss cooldown is enforced with
per-group, event-type-specific keys including `groupCounter` for
multi-group instances.

---

## 13b. Player Message Conversations (Multi-Bot Replies)

When a player speaks in party chat, the system can trigger a multi-bot
conversation instead of a single-bot reply. This makes groups feel
more socially dynamic.

Known playerbot control commands are not supposed to reach this
conversation path in current source:

- C++ now blocks them before creating `bot_group_player_msg` events
- Python keeps `_is_playerbot_command()` as a fallback skip layer

### Trigger logic

1. `find_addressed_bot()` in `chatter_shared.py` always fires an LLM
   call (even when name matching succeeds) to assess whether the
   message is `multi_addressed` — i.e., directed at the group rather
   than a single bot. Returns a dict:
   `{"bot": name, "multi_addressed": bool}`.
2. When `multi_addressed=True` and at least 2 bots are available,
   the conversation path is forced (bypasses the RNG gate).
3. Otherwise, the conversation path fires with probability
   `PlayerMsgConversationChance` (default 30%), scaled by bot count
   in the group.

### Multi-addressed detection

The LLM intent check detects plural pronouns ("you guys", "everyone",
"team"), group-directed questions ("what should we do?"), and messages
mentioning multiple bot names. This ensures group-directed speech
gets multi-bot replies without relying on RNG.

### Architecture

Uses Architecture B: a single LLM call returns a JSON array of 2-3
bot replies. `PlayerMsgSecondBotChance` (default 25%) controls whether
a third bot participates beyond the guaranteed two.

### Relevant files

| File | Responsibility |
|---|---|
| `chatter_shared.py` | `find_addressed_bot()` with multi-addressed intent |
| `chatter_group_prompts.py` | `build_player_msg_conversation_prompt()` |
| `chatter_group_handlers.py` | `execute_player_msg_conversation()` |
| `chatter_group.py` | Routing: single reply vs conversation |

### Config keys

| Key | Default | Purpose |
|---|---|---|
| `PlayerMsgConversationChance` | 30 | % chance of multi-bot reply to player message |
| `PlayerMsgSecondBotChance` | 25 | % chance a 3rd bot joins the conversation |

---

## 13d. Bot-Initiated Questions

Bots can periodically ask the real player creative questions in party
chat, making them feel socially interested in the player rather than
only reacting to events.

### Trigger logic

A Python timer fires every `BotQuestionCheckInterval` (default 30s).
Each tick:

1. Randomly selects one active group
2. Checks cooldown (10 min default) and inflight guard
3. Rolls `BotQuestionChance` (default 1%)
4. Combat suppression: checks for recent combat/kill/spell/death
   events (90s window via JSON_EXTRACT on `llm_chatter_events`)
5. Gets player name from `get_group_player_name()` or join event
   fallback
6. Selects a random bot, builds prompt with player context
7. Validates response ends with `?` (retry once if not)
8. Delivers via `insert_chat_message()` and stores in chat history

### Reply path (existing, no changes)

When the player replies, it fires `bot_group_player_msg`. The
original question is in `llm_group_chat_history`, so the bot's
reply is contextually aware. `PlayerMsgSecondBotChance` (25%)
can trigger a second bot chiming in.

### Config keys

| Key | Default | Purpose |
|---|---|---|
| `BotQuestionEnable` | 1 | Enable/disable feature |
| `BotQuestionChance` | 1 | % chance per tick |
| `BotQuestionCooldown` | 600 | Per-group cooldown (seconds) |
| `BotQuestionCheckInterval` | 30 | Timer interval (seconds) |

### Relevant files

| File | Responsibility |
|---|---|
| `chatter_group.py` | `check_bot_questions()` main logic |
| `chatter_group_prompts.py` | `build_bot_question_prompt()`, `BOT_QUESTION_TOPICS` |
| `llm_chatter_bridge.py` | Timer integration in main loop |

---

## 13e. Quest Conversations

Quest events (complete, objectives, accept) can trigger multi-bot
conversations instead of single-statement reactions, controlled by
`QuestConversationChance` (default 30%).

### Decision flow

Each of the 3 single-quest handlers (not `quest_accept_batch`)
checks after marking the event as `processing`:

1. Read `QuestConversationChance` from config
2. Call `get_group_members()` to count bots
3. Gate: `len(members) >= 2 and roll <= chance`
4. If conversation: call `_quest_*_conversation()` helper
5. If statement: existing `run_single_reaction()` path

### Conversation helpers

Two shared functions avoid code triplication:

- `_quest_conversation_pick_bots()` — picks 2-3 bots (reactor
  always included), looks up traits + class/race from DB. Returns
  `(bots, traits_map, bot_guids)` or `None`.
- `_quest_conversation_deliver()` — per-message cleanup
  (`strip_speaker_prefix`, `cleanup_message`, 255-char clamp),
  staggered delays via `calculate_dynamic_delay()`, first message
  gets action, stores chat history, marks event completed.

Three orchestration functions call these shared helpers:

- `_quest_complete_conversation()` — includes turn-in NPC lookup
- `_quest_objectives_conversation()` — no NPC, no mood update
- `_quest_accept_conversation()` — includes quest_level/zone_name

### Failure handling

If `call_llm()` fails or `parse_conversation_response()` returns
empty, the helper returns `False` and the handler falls through to
the existing statement path.

### Prompt builders

Three new functions in `chatter_group_prompts.py`:

| Function | Quest context |
|---|---|
| `build_quest_complete_conversation_prompt()` | "TRANSACTION COMPLETE", turn-in NPC, celebration |
| `build_quest_objectives_conversation_prompt()` | "PENDING TURN-IN", relief, readiness |
| `build_quest_accept_conversation_prompt()` | "PREPARATION", quest level, zone, anticipation |

### Config

| Key | Default | Purpose |
|---|---|---|
| `QuestConversationChance` | 30 | % chance per quest event |

### Relevant files

| File | Responsibility |
|---|---|
| `chatter_group_handlers.py` | 3 handler mods + 5 helpers |
| `chatter_group_prompts.py` | 3 conversation prompt builders |

---

## 13f. Achievement Event Batching

When multiple bots in the same group earn the same achievement within a
2-second window, the module can collapse those duplicate events into a
single congratulatory reaction.

### Why this exists

Without batching, simultaneous achievement events produce repetitive
party spam and can trigger multiple nearly identical LLM calls.

### Batch logic

`_check_achievement_batch()` in `chatter_group_handlers.py`:

1. queries neighboring `bot_group_achievement` rows for the same
   `group_id` and `achievement_name`
2. considers both `pending` and `processing` rows to avoid ownership
   races
3. assigns the batch to the lowest event ID
4. marks duplicate rows completed
5. returns either:
   - `None` for normal single processing
   - `'already_batched'` for a duplicate row
   - `list[str]` of achiever names for the batch owner

### Relevant files

| File | Responsibility |
|---|---|
| `chatter_group_handlers.py` | `_check_achievement_batch()` and achievement event routing |
| `chatter_group_prompts.py` | Group achievement reaction prompt builder |
| `chatter_bg_prompts.py` | BG-side achievement prompt path |

---

## 13g. Talent Context Injection

The bridge can inject talent-based context into prompts so bots sound
more like their build and spec without literally naming talents.

### Shared construction

`build_talent_context()` in `chatter_shared.py`:

1. loads the character's talents from the DB
2. finds the dominant talent tree
3. picks one talent from that tree
4. looks up a short natural-language description from
   `talent_catalog.py`
5. rewrites wording for `speaker` or `target` perspective
6. adds a guardrail telling the LLM not to name the talent directly

### Injection points

Talent context is invoked from:

- group event handlers
- group player-message paths
- General-channel player reactions
- battleground paths through `chatter_raid_base.py` and
  `chatter_battlegrounds.py`

### Config

| Key | Default | Purpose |
|---|---|---|
| `TalentInjectionChance` | 40 | % chance a given prompt gets talent context |

### Relevant files

| File | Responsibility |
|---|---|
| `chatter_shared.py` | `build_talent_context()` and catalog lookup |
| `chatter_group_handlers.py` | `_maybe_talent_context()` for group events |
| `chatter_group.py` | Talent-aware player/idle/group paths |
| `chatter_general.py` | Talent-aware General prompts |
| `chatter_raid_base.py` | Shared BG/raid talent injection path |
| `talent_catalog.py` | Static talent descriptions |

---

## 13h. Humor Hints and Conversation Pacing

Two later prompt/delivery changes affect how messages feel even though
they did not introduce new event types.

### Humor hints

`_pick_length_hint()` in `chatter_group_prompts.py` now optionally adds
humor guidance through `_maybe_humor_hint()`:

- 40% chance in normal mode
- 35% chance in roleplay mode

This applies across the group prompt builders that use the shared length
hint path. General-channel prompts were also retuned in the same period
to encourage humor more often.

### Ambient conversation pacing

Ambient multi-bot conversations in `chatter_ambient.py` now pass
`prev_message_length` into `calculate_dynamic_delay()`. That gives later
participants a reading delay before they reply to the previous message,
instead of only reacting to their own output length.

---

## 13i. Emote and Action Prompt Gating

### EmoteChance

`LLMChatter.EmoteChance` (default 50) is an RNG gate that controls
whether the emote list is included in prompts. When the roll fails,
the emote instruction block is omitted entirely, saving ~500 tokens
per LLM call. Applied centrally in `append_json_instruction()` and
`append_conversation_json_instruction()` in `chatter_prompts.py`.

If the parser later returns `"emote": null`, insert paths should keep it
null. Python should not synthesize a fallback emote on insert. The
prompt-side `EmoteChance` roll is the source of truth for whether the
LLM was asked for an emote at all.

### ActionChance (dual strategy)

`LLMChatter.ActionChance` (default 10) controls whether action
narrations appear in bot messages. Two strategies:

- **Single statements**: pre-call RNG in `append_json_instruction()`
  decides before the LLM call whether to request an action (saves
  tokens when disabled).
- **Conversations** (General, Proximity, Group idle, Group handlers,
  Screenshot vision): prompts always request actions (in RP mode).
  Python enforces ActionChance per-message post-parse via
  `strip_conversation_actions()` in `chatter_shared.py`. This avoids
  trusting the LLM to randomize naturally.

In normal mode, actions are suppressed at the prompt level for all
conversation paths (no wasted tokens).

### Config keys

| Key | Default | Purpose |
|---|---|---|
| `LLMChatter.EmoteChance` | 50 | % chance emote list is included in prompt (not applied to General channel — emotes are proximity-based) |
| `LLMChatter.ActionChance` | 10 | % chance eligible responses retain/include an action after action gating |

---

## 13j. Emote Reaction System

When a player performs a `/emote` (text emote), the
`OnPlayerTextEmote` hook owned by `LLMChatterGroupCombat.cpp`
classifies the target and routes it through the group-aware reaction
paths below when eligible. Bot verbal reactions and observer chatter
still require grouped bots. Direct creature mirror reactions do not.

### Three reaction paths

| Path | Trigger | Behavior |
|------|---------|----------|
| Silent mirror | Player emotes at a group bot | Bot mirrors back a matching emote (e.g. wave to wave, rude to chicken) via `DelayedMirrorEmoteEvent` with natural timing. Per-bot cooldown `_emoteReactCooldowns` |
| Directed verbal reaction | Player emotes at a group bot (after mirror) | Targeted bot queues a `bot_group_emote_reaction` event. Python handler builds an LLM prompt and the bot responds verbally. Per-bot cooldown `_emoteVerbalCooldowns` |
| Observer comment | Player emotes at a creature, external player, or nobody | A random group bot queues a `bot_group_emote_observer` event. Python handler has the bot make an offhand remark about the emote. Per-group cooldown `_emoteObserverCooldowns` |

Creatures also mirror emotes directed at them via
`DelayedCreatureMirrorEmoteEvent`. That direct mirror path is allowed
even when the player is solo; only the observer-comment path remains
group-gated.

### Ownership split for emote bugs

- edit `LLMChatterGroupCombat.cpp` when the bug is about target
  classification, solo-vs-group gating, or which reaction path runs
- edit `LLMChatterGroupEmote.cpp` when the bug is about mirror maps,
  mirror cooldowns, creature/bot facing, or delayed emote execution

### Emote coverage

All ~170 social text emotes trigger reactions. A denylist of 4 emotes
is excluded: `BRB`, `MESSAGE`, `MOUNT_SPECIAL`, `STOPATTACK`. Combat
callout emotes (`CHARGE`, `OPENFIRE`, `INCOMING`, `RETREAT`, `FLEE`)
are excluded from observer comments only.

### C++ shared infrastructure

- `GetTextEmoteName(uint32)` in `LLMChatterShared.cpp` — reverse
  emote ID-to-name lookup covering 170+ entries including high-ID range
  381-451
- `SendUnitTextEmote(Unit*, uint32, const std::string&)` — consolidated
  emote packet helper; `SendBotTextEmote` overloads delegate to it
- `s_mirrorEmoteMap` — 30+ entries mapping incoming emote to response
  emote (wave to wave, rude to chicken, etc.)
- `s_contagiousEmotes` — emotes that spread naturally (laugh, cheer,
  dance, etc.)

### Python handlers

| File | Event type | Purpose |
|------|------------|---------|
| `tools/chatter_emote_reaction.py` | `bot_group_emote_reaction` | Directed verbal reaction prompt and delivery |
| `tools/chatter_emote_observer.py` | `bot_group_emote_observer` | Observer comment prompt and delivery |

### Config keys

| Key | Default | Purpose |
|-----|---------|---------|
| `LLMChatter.EmoteReactions.Enable` | 1 | Master toggle |
| `LLMChatter.EmoteReactions.MirrorChance` | 80 | % chance bot mirrors back the emote |
| `LLMChatter.EmoteReactions.MirrorCooldown` | 30 | Seconds per-bot cooldown for mirroring |
| `LLMChatter.EmoteReactions.ReactionChance` | 40 | % chance of verbal reaction after mirror |
| `LLMChatter.EmoteReactions.ObserverChance` | 25 | % chance of observer bot commenting |
| `LLMChatter.EmoteReactions.ObserverCooldown` | 60 | Seconds per-group cooldown for observer |
| `LLMChatter.EmoteReactions.MoodSpreadChance` | 30 | % chance contagious emote spreads mood |

### Cooldown eviction

`EvictEmoteCooldowns()` runs hourly to clean up stale entries from
all three emote cooldown maps.

---

## 13k. LLM Request Logging

Every `call_llm()` invocation can be recorded to a JSONL log file for
debugging and analysis. This is a Python-only development feature with
no C++ involvement.

### Files

| File | Purpose |
|---|---|
| `tools/chatter_request_logger.py` | Thread-safe JSONL logger |
| `tools/chatter_log_viewer.py` | Zero-dependency stdlib web UI |

### Logger

`chatter_request_logger.py` provides:

- `init_request_logger(config)` — called once at bridge startup; reads
  config, creates the log directory, sets up the global state
- `log_request(label, prompt, response, model, provider, duration_ms)` —
  called from `call_llm()` via lazy import in `finally` block; writes
  one JSONL line per call
- Rotation: when the log file exceeds `MaxSizeMB`, it is renamed to
  `.1.jsonl` and a fresh file begins

Each JSONL record contains:

```json
{
  "timestamp": "2026-03-18T12:34:56.789",
  "label": "group_join",
  "model": "claude-haiku-4-5",
  "provider": "anthropic",
  "duration_ms": 421,
  "zone_name": "Elwynn Forest",
  "zone_flavor": "A peaceful woodland...",
  "subzone_name": "Goldshire",
  "subzone_lore": "A small hamlet...",
  "speaker_talent": "...",
  "target_talent": "...",
  "system_prompt": "...",
  "prompt": "...",
  "response": "..."
}
```

Metadata fields (zone_name, zone_flavor, subzone_name, subzone_lore,
speaker_talent, target_talent, system_prompt) are only written when
non-empty — absent fields mean the context was not available for that
call. The `system_prompt` field contains the system message content
when the prompt was split via `PromptParts`; absent when the full
prompt was sent as a single user message.

### Labels

Every `call_llm()` call site passes a descriptive `label=` keyword
argument so log entries can be filtered by feature. All 27 call sites
are labelled:

| Label | Source |
|---|---|
| `event_conv` / `event_statement` | `llm_chatter_bridge.py` |
| `ambient_statement` / `ambient_conv` | `chatter_ambient.py` |
| `precache` | `chatter_cache.py` |
| `general_player_msg` / `general_followup` / `general_conv` | `chatter_general.py` |
| `group_join` / `group_welcome` / `group_player_msg` / `group_composition` / `group_idle` / `group_idle_conv` / `group_bot_question` | `chatter_group.py` |
| `group_nearby_obj` / `group_player_msg_conv` / `group_quest_conv` | `chatter_group_handlers.py` |
| `group_farewell` | `chatter_group_state.py` |
| `single_reaction` | `chatter_shared.py` |

### Web viewer

`chatter_log_viewer.py` is a standalone script with no external
dependencies (Python stdlib only). Run it on the host:

```bash
python modules/mod-llm-chatter/tools/chatter_log_viewer.py \
    --log modules/mod-llm-chatter/logs/llm_requests.jsonl \
    --port 5555
```

Then open `http://localhost:5555`.

Features:

- entry list (left panel) + detail view (right panel)
- draggable vertical column divider and horizontal prompt/response divider
- semantic prompt section highlighting with colored left borders:
  IDENTITY, TRAITS, CONTEXT, TASK, RULES, FORMAT, STYLE
- section pill badges in the prompt header
- system prompt pane with copy button, visible when the JSONL entry
  contains a `system_prompt` field
- JSON pretty-print for structured responses
- copy buttons for prompt, response, and system prompt
- filtering by label and text search
- pagination
- auto-refresh every 30s (toggleable)

### Docker bind mount

The log file is written inside the container at `/logs/llm_requests.jsonl`
and mapped to the host at `modules/mod-llm-chatter/logs/` via a bind
mount in `docker-compose.override.yml`:

```yaml
volumes:
  - ./modules/mod-llm-chatter/logs:/logs:rw
```

**Applying mount changes** requires container recreation, not just restart:

```bash
docker compose --profile dev up -d ac-llm-chatter-bridge
```

### Config keys

All three keys are `[BRIDGE]` scope (Python-only; no server restart needed).

| Key | Default | Purpose |
|---|---|---|
| `LLMChatter.RequestLog.Enable` | 1 | Enable/disable logging |
| `LLMChatter.RequestLog.Path` | `/logs/llm_requests.jsonl` | Log file path inside container |
| `LLMChatter.RequestLog.MaxSizeMB` | 50 | Rotation threshold |

---

## 13c. Responsive Delays

Player-directed replies use faster timing than ambient chatter. The
`calculate_dynamic_delay()` function in `chatter_shared.py` accepts a
`responsive=True` parameter that:

- skips distraction simulation
- uses shorter reaction and typing windows
- enforces a 2-second floor (vs 4 seconds for ambient)
- skips reading time for multi-bot conversation follow-up messages

All player message paths (single reply, conversation, multi-addressed)
use responsive delays. Ambient chatter, idle banter, and world events
continue to use standard timing.

---

## 13l. Shared `chatter_shared.py` Helpers

Key helpers provided by `chatter_shared.py` for use across prompt and
delivery code:

| Helper | Purpose |
|--------|---------|
| `calculate_dynamic_delay(responsive=False)` | Delivery timing — skips distraction sim and uses a 2s floor when `responsive=True` |
| `find_addressed_bot(...)` | Named-bot detection + multi-addressed intent classification via LLM |
| `should_include_action()` | Single RNG roll gating narrator action inclusion (`random.random() < get_action_chance()`). Use at conversation delivery sites instead of calling `get_action_chance()` directly to avoid double-rolling the probability |
| `PromptParts(str)` | System/user prompt split wrapper; auto-detected by `call_llm()` |
| `build_talent_context(...)` | Talent-aware personality context builder |
| `build_race_class_context(...)` | Race/class identity and speech-trait injector |

The `should_include_action()` helper was introduced to fix an
`ActionChance` double-roll bug: `append_conversation_json_instruction`
was pre-filtering speakers with its own `_action_chance` RNG gate, then
delivery was rolling again, making effective probability p² instead of p.
The fix is: the prompt instruction now lists **all** eligible speakers
without pre-filtering; delivery enforces `ActionChance` once via
`should_include_action()`.

---

## 13m. Dungeon Context Injection

When a group is inside a dungeon or raid instance, party chatter prompt
builders replace zone/subzone lore with dungeon-specific flavor text.

`get_group_location()` now threads `map_id` to all major party chatter
prompt builders. Each builder calls `get_dungeon_flavor(map_id)` — if a
flavor entry exists for that map, it replaces the zone/subzone lore
block with the dungeon's atmospheric description and tone.

Affected prompt builders:

- kill reaction
- loot reaction
- death reaction
- achievement / group achievement
- wipe reaction
- corpse run commentary
- nearby object (both statement and conversation)

Builders that intentionally do **not** receive dungeon context injection:

- OOM / low-health callouts (state-focused, not location-flavored)
- level-up (character milestone, independent of location)

---

## 13n. Persistent Bot–Player Memory

### Overview

Bots accumulate a bounded journal of shared moments with real players.
On re-invite, the bot delivers a reunion greeting that references past
experiences rather than treating the player as a stranger.

Memories are only generated for player-owned ("alt") bots — never for
random/ownerless bots — and every memory carries a 1-10 importance score.
Low-importance memories decay and are the first to be evicted as a
bot–player pair's memory pool grows, while high-importance memories (raid
boss kills, core narrative moments) persist indefinitely. The single
highest-value memory for each bot–player pair is always protected from
eviction (see "Eviction guard" below), and party-wide events (boss/rare
kills, wipes) generate one shared memory per event instead of one per
bot.

Three subsystems sit on top of that journal, each documented in its own
subsection below:

- **Condensation** — before the hard cap is reached, a pair's least valuable
  memories are folded into short digests instead of simply being deleted, so
  the pool stays meaningful rather than either growing forever or being
  trimmed to bare deletions.
- **Relationship tracking** — a single running summary per pair of how the
  bot generally *feels* about that player, kept current by a timestamp
  watermark over the journal.
- **Session vibe** — a short-lived group-wide mood cue set by a
  sufficiently important memory, so a party's tone lingers after something
  significant instead of resetting instantly.

Condensation and relationship tracking interact in a non-obvious way (a
digest can land *behind* the relationship watermark, and the two run on
independent executors); that interaction is spelled out in
"Relationship tracking" step 3 and "Memory condensation" step 6.

### Memory lifecycle

1. **Group join** (`process_group_join_event` / `process_group_join_batch_event`)
   - `start_session()` registers the bot in the in-memory session tracker
   - `get_bot_memories()` fetches up to 3 active memories for this
     bot–player pair, ranked by decay-aware `effective_score` (see
     "Importance scoring and decay" below) and trimmed to
     `LLMChatter.Memory.MaxInjectTokens`
   - If memories exist: `player_name_known=True` → reunion greeting mode
   - If no memories (first meeting): a `first_meeting` memory is inserted
     directly with `active=1` and `memory_type='first_meeting'`, guarded by
     `INSERT...SELECT...WHERE NOT EXISTS` to prevent duplicates on re-join.
     This memory is immune to short-session discard, because it is inserted
     `active=1` and that discard only targets `active=0` rows. It is not
     otherwise privileged: see "Known gap: `first_meeting` rows are not
     protected from deletion" below.
   - C++ resolves `is_altbot` (`PlayerbotAI::IsAltBot()`) at join time and
     puts it in the join event payload; `assign_bot_traits()` in Python
     writes it into `llm_group_bot_traits.is_altbot`. Only altbots ever get
     memories generated for them (see "Altbot-only generation" below).

2. **During the session** — event handlers may call `_generate_and_store_memory()`
   (via `queue_memory()`) to produce LLM-generated memories (boss kills, notable
   events) for a single bot, or `queue_shared_event_memory()` for party-wide
   kill/wipe events (see "Shared event memories" below). These are
   inserted with `active=0` until flush, and each carries an
   LLM-assigned `importance_score` (1-10; defaults to 5 if the LLM
   response is missing or unparseable).

3. **Group farewell** (`process_group_farewell_event` → `flush_session_memories()`)
   - If session was long enough (`SessionMinutes` threshold): activates this
     bot's `active=0` session rows, then calls
     `_maybe_queue_relationship_update()` on the same open cursor (see
     "Relationship tracking" below), then prunes down to the
     `MaxPerBotPlayer` cap by calling `_evict_one_used()` in a loop until the
     pair is back under the cap or nothing is left to evict. The prune carries
     no `memory_type` exemption — the only row it protects is the pair's single
     highest-`effective_score` row (see "Eviction guard"). `first_meeting` rows
     are eligible like any other; see "Known gap" below.
   - If session was too short: deletes all `active=0` rows for this session.
     `first_meeting` rows are inserted `active=1`, so this path never touches
     them.

4. **Reunion greeting** — when `get_bot_memories()` returns a non-empty list,
   the greeting prompt enters reunion mode: injects `<past_memories>` block,
   uses familiar tone, optionally recalls a specific memory (`recall_memory`).
   The `is_reunion` flag is `bool(memories and player_name_known)`, so a first
   meeting (where `memories=[]`) always produces a fresh greeting even though
   `player_name_known` is set to `True` for internal tracking.

5. **Cap enforcement** — when a bot–player pair's active memory count
   hits `MaxPerBotPlayer`, `_ensure_cap_and_insert()` calls
   `_evict_one_used()` synchronously before inserting the new memory (see
   "Eviction guard" below for the rule protecting the single most
   valuable memory from ever being evicted).

### Importance scoring and decay

Every memory the LLM generates is asked to also rate the moment on a
1-10 importance scale, using the same rubric text
(`_IMPORTANCE_RUBRIC` in `chatter_memory.py`) across single-bot memory
generation and shared event memories:

- **1-3 (Ambient)** — casual chat, minor zone banter
- **4-6 (Narrative)** — personal preferences stated by the player, minor
  achievements
- **7-8 (Milestones)** — leveling milestones, acquiring rare gear, wipe
  encounters
- **9-10 (Core Bonds)** — defeating raid bosses together, major
  narrative turning points

Retrieval (`get_bot_memories()`) and eviction (`_evict_one_used()`) both
rank memories by a decay-aware `effective_score` rather than the raw
`importance_score`:

- Memories with `importance_score <= LLMChatter.Memory.DecayMaxImportance`
  (default `3`, ambient) decay by roughly one point per
  `LLMChatter.Memory.DecayDays` days (default `30`), floored at 1:
  `GREATEST(1, importance_score - TIMESTAMPDIFF(DAY, created_at, NOW())
  / 30)`. The expression is built by `_effective_score_sql()`.
- Memories above that threshold (narrative and up) never decay — they
  keep their raw score indefinitely.

This means ambient chatter fades out of relevance over time while
milestone and core-bond memories keep surfacing in reunion greetings and
recall no matter how old they are.

`get_bot_memories()` uses `effective_score` for both the row-count cap
(`count`, default 3) and a token budget
(`LLMChatter.Memory.MaxInjectTokens`, default 400): it over-fetches a
larger candidate pool, then accumulates candidates in `effective_score`
order until either the row count or token budget would be exceeded. The
first candidate is always kept even if it alone exceeds the budget, so a
single oversized memory can never starve the result to empty. Only the
memories actually selected are marked `used = 1`.

### Altbot-only generation

Memory generation is restricted to player-owned bots — bots the player
actually controls as an alt via mod-playerbots — and skipped for random/
ownerless bots that happen to be in the party. The flag flows from C++
through to the Python generation gate:

1. `PlayerbotAI::IsAltBot()` is evaluated when a bot joins a group, in
   `LLMChatterGroupJoin.cpp` (`QueueBotGreetingEvent()` and
   `EnsureGroupJoinQueued()`/`FlushGroupJoinBatches()`).
2. The result is written both into the `bot_group_join`/
   `bot_group_join_batch` event payload as `is_altbot` and persisted to
   `llm_group_bot_traits.is_altbot` (default `1`, so pre-existing rows and
   any lookup failure fail open as altbot).
3. `queue_memory()` (single-bot memory generation) calls `_is_altbot()`
   to look up `llm_group_bot_traits.is_altbot` for the bot before
   submitting any generation job; non-altbots return early and never
   reach the LLM.
4. `queue_shared_event_memory()` (party-wide kill/wipe memories) does not
   re-check `is_altbot` itself — its callers (`_kill_post_success()`,
   `_wipe_post_success()` in `chatter_group_handlers.py`) already filter
   the candidate bot list with `... AND t.is_altbot = 1` before calling
   it.

### Eviction guard

`_evict_one_used()` never evicts the single highest-`effective_score`
memory for a given `(bot_guid, player_guid)` pair. Both of its DELETE
queries (the preferred `used=1` pass and the `used=0` fallback pass, see
"Importance scoring and decay" above) exclude that pair's current top row
via a subquery:

```sql
AND id != (SELECT id FROM (
  SELECT id FROM llm_bot_memories
  WHERE bot_guid = %s AND player_guid = %s AND active = 1
  ORDER BY effective_score DESC, created_at DESC LIMIT 1
) t)
```

(MySQL requires the extra subquery wrapping since you can't otherwise
select from the same table you're deleting from.) This guarantees a bot
never fully forgets the single most meaningful moment it shares with a
player, without needing a background condensation pass to preserve it —
if the memory pool ever fills up, only the lowest-value rows get evicted,
one at a time, and the top row is always the last one left standing. On a
pool with only one row, that row is by definition the top row, so both
queries find nothing eligible to evict and `_evict_one_used()` simply
returns `False` — the same "nothing to evict" outcome
`_ensure_cap_and_insert()` handles by declining the insert.

### Shared event memories (kill/wipe batching)

Boss/rare kills and wipes are witnessed by the whole party at once, so
instead of one LLM call per altbot present, `queue_shared_event_memory()`
makes a single call via `_generate_shared_event_memory()` and inserts the
resulting memory verbatim into every present altbot's memory pool.

The prompt deliberately asks for a first-person-**plural** memory
("we"/"our party") rather than a first-person-singular one, and the
identical text is stored for each bot — there is no per-bot name
substitution or templating. This was chosen because every bot genuinely
witnessed the same event together, so a shared "we" memory reads as more
truthful than several bots independently claiming an identical personal
"I" story, and it avoids baking any placeholder/substitution syntax into
stored memory text that would otherwise leak into `get_bot_memories()`
and other consumers.

### Player memory inspection command

`.llmc memory <botname>` (`SEC_PLAYER`, `src/LLMChatterCommand.cpp`)
lets a player see what one of their own bots remembers about them. Unlike
the rest of the `.llmc` surface (which is addon-protocol traffic sent by
the Chatter Companion addon and replies via `SendAddonLine`/`CHATTER_ADDON`),
this subcommand is meant to be typed directly in chat and replies with
plain `SendSysMessage`/`PSendSysMessage` text.

The bot name is resolved to a `bot_guid` the same way `roster`/`forget`
scope bots to a player: a case-insensitive match against `characters.name`
restricted to bots that already have at least one `llm_bot_memories` row
for `player_guid = <invoking player>`. A player can never query another
player's bot memories this way, since the resolution itself is scoped to
their own `player_guid`.

It's a synchronous `CharacterDatabase.Query` read — no event is queued
and the Python bridge is never involved, since this is a read-only lookup
with no LLM generation needed. Rows are ordered by the same decay-aware
effective-importance expression as `get_bot_memories()`
(`_effective_score_sql()` in `chatter_memory.py`), reimplemented inline in
SQL and driven by the same `LLMChatter.Memory.DecayMaxImportance` /
`LLMChatter.Memory.DecayDays` config keys, capped at the 10 highest-ranked
`active = 1` memories.

### Manual and automatic cleanup

`.llmc memoryclean` (GM-only, `src/LLMChatterCommand.cpp`) runs two
`DELETE`s directly against `CharacterDatabase`, dropping `llm_bot_memories`
and `llm_bot_relationships` rows whose `bot_guid`/`player_guid` no longer
resolve to an existing character (e.g. after a character deletion) — the same
`characters`-orphan `LEFT JOIN` shape in both statements, and the same pattern
`.llmc forget` already uses.

It is a subcommand of the module's single `.llmc` root (there is no separate
`.llm` root command — `llm` is a literal prefix of `llmc`, which AzerothCore's
partial-match command dispatcher resolves ambiguously). Because that root is
registered `SEC_PLAYER` for the player-facing subcommands, `memoryclean`
enforces `SEC_GAMEMASTER` itself via `ChatHandler::IsAvailable()` inside
`HandleRootCommand()` rather than through the command table.

**Known capability gap:** `.llmc memoryclean` is in-game only — the `.llmc`
root is registered `Console::No`, so it cannot be run from the server console
or over SOAP. The same cleanup runs unattended every 24 hours via
`purge_orphaned_memories()`, so this is a convenience limitation, not a
functional one.

The same cleanup also runs automatically: `llm_chatter_bridge.py`'s main
loop calls `purge_orphaned_memories()` (which issues the identical pair of
`DELETE`s) on a fixed 24-hour interval (`memory_gc_interval`), independent
of whether a GM ever runs the manual command.

### Relationship tracking

Distinct from the `llm_bot_memories` journal, `llm_bot_relationships` holds
ONE running, LLM-maintained description per `(bot_guid, player_guid)` pair
of how that bot generally *feels* about that specific player — a standing
disposition, not a list of specific recollections.

**The watermark is a timestamp, not an id.** `updated_through_created_at`
(DATETIME, default `1970-01-01 00:00:00` — the "nothing folded in yet"
sentinel, expressible as DATETIME precisely because it is out of range for
TIMESTAMP) records the `created_at` of the newest memory already absorbed
into the summary. It replaced an earlier `updated_through_memory_id` column,
which condensation broke: condensation deletes its source rows and re-inserts
their content as digests with fresh, higher auto-increment ids, so an id
watermark made already-summarized material look brand new. A digest inherits
its *oldest* source's `created_at`, so a timestamp watermark does not have
that problem — but it creates a different one, handled in step 3 below.

1. **Trigger** — right after `flush_session_memories()` activates a
   departing bot's session rows (`rows_activated > 0`),
   `_maybe_queue_relationship_update()` reuses the same open cursor to
   compare the count of `active = 1` memories with
   `created_at > updated_through_created_at` (epoch for a first-time pair
   with no row yet) against
   `LLMChatter.Memory.Relationship.UpdateThreshold`. Meeting the threshold
   submits `_maybe_update_relationship()` to `relationship_executor` — a
   dedicated, single-worker `ThreadPoolExecutor` kept separate from both
   `memory_executor` and `condensation_executor` so this lower-frequency
   background work never competes with latency-sensitive memory generation.
   Everything here is wrapped in try/except: it can never block or fail the
   farewell flow it is piggybacking on.
2. **Summarization** — `_maybe_update_relationship()` runs on **two
   short-lived connections**, never one held across the LLM call:
   - *Read connection*: fetches the current summary and watermark, pulls
     active memories newer than the watermark ordered by
     `(created_at, id)` and capped at `_RELATIONSHIP_MAX_INPUT_CHARS`
     (~6000 chars, mirroring the Guild Chat session summarizer's
     `SummaryMaxInputChars` concept — see 13s below), extends that trim
     forward to cover every remaining row sharing the last kept row's
     `created_at` (`_extend_candidates_to_timestamp_boundary()`, so a trim
     through the middle of a same-second group cannot orphan the leftovers
     behind a strict `created_at >` comparison), and resolves display
     names. Then it **closes, before the LLM call**. mysql-connector runs
     with autocommit off, so holding it open would pin that SELECT's read
     view — and InnoDB purge — for the entire multi-second round-trip.
   - The memories are rendered into the prompt with
     `sanitize_memory_for_prompt(..., max_chars=MEMORY_TEXT_MAX_CHARS)`
     (500), not the routine 200-char preview default: the watermark
     advances past every row folded in here, so each one gets exactly this
     one chance to reach the summary. The prompt goes through the same
     `append_json_instruction(..., message_only=True)` call the Guild
     session summarizer uses, so the language rule and lore guardrail are
     applied identically and must not be injected a second time by hand.
     Like all memory-side internal work it routes to the cheap model when
     `LLMChatter.Memory.UseQuickModel` is on.
   - *Write connection*: opened only once a usable summary is in hand. The
     response is hard-truncated to
     `LLMChatter.Memory.Relationship.MaxChars` via `_trim_summary()`
     (shared through `chatter_text.py`, not a third near-identical copy).
3. **Optimistic concurrency on the write** — this is the least obvious part
   of the whole memory system, and the reason the write is not a blind
   upsert.

   `_condense_low_value_memories()` runs on a *different* executor
   (`condensation_executor`) and is unaware of this one; the
   `_condensing_pairs` guard set only prevents a second *condensation* pass
   for a pair, not a concurrent relationship pass. Condensation can
   therefore commit `_rewind_relationship_watermark()` for this exact pair
   while the LLM call in step 2 is still in flight, deliberately moving
   `updated_through_created_at` **backward** so a freshly written digest
   gets re-folded here next time (see "Memory condensation" below for why
   it must).

   If this pass then wrote its own (now stale, higher) watermark
   unconditionally it would silently undo that rewind and strand the
   digest's content permanently — the digest's source rows are already
   deleted, and the digest itself sorts behind the higher watermark, so no
   later pass could ever reach it. A `GREATEST()` guard fails for exactly
   the same reason: it also refuses to go backward. So:
   - If a relationship row existed at read time, the advance is a
     conditional `UPDATE ... AND updated_through_created_at <=> %s`
     against `watermark_at_read` — the raw value captured from the read
     connection *before it closed* (a plain Python value, so the
     connection swap is irrelevant). `<=>` rather than `=` so a NULL
     watermark compares correctly.
   - If that UPDATE matches zero rows, someone moved the watermark
     underneath us. The pass still writes the **summary** (it is real LLM
     work, and re-folding already-summarized material is harmless by
     design — the prompt is "update this summary with these memories") but
     leaves the watermark exactly where the other writer put it, so the
     next pass resumes from the real position instead of skipping whatever
     the rewind was recovering.
   - If no row existed at read time there is no watermark to race against
     (the rewind helper never *creates* a row), so a plain
     `INSERT ... ON DUPLICATE KEY UPDATE` is correct.
4. **Watermark sanity check** — `_sanitized_relationship_watermark()` runs
   on every read. A watermark ahead of the database clock can never be
   passed by `created_at > watermark`, so the pair would stall silently
   forever; that case is logged loudly and reset to the epoch, re-folding
   the pair once (cheap and self-correcting). A watermark merely newer than
   the pair's newest memory is *not* an error — it is the normal state
   right after condensing already-summarized rows, and the pair's next
   memory clears it.
5. **Fail-safe** — on no new memories, an LLM call failure, or an empty/
   unparseable response, the function returns without writing anything;
   the watermark is left untouched so the next qualifying farewell
   retries. Never a partial or corrupt overwrite.
6. **Read paths** — `get_relationship_summary(db, bot_guid, player_guid)`
   returns the current summary or `None` (first-time pair / never
   reached the threshold). `chatter_group.py` fetches it right before
   the player-scoped prompt builders it feeds
   (`build_player_response_prompt()`'s two call sites and
   `build_bot_question_prompt()`'s LEAN MEMORY PATH call site) and passes
   it as `relationship_summary=`. Both prompt builders inject it as a
   `<relationship>` block positioned after identity/personality/tone and
   before `<past_memories>`, explicitly framed as an ongoing disposition
   ("let this color your tone, not the topic") rather than something to
   recite — the same distinction already drawn between `<past_memories>`
   and `<party_memories>`.
7. **Command surfacing** — `.llmc memory <botname>` prints one extra line,
   `"<Bot>'s feelings about you: <summary>"`, ahead of the memory list,
   truncated with the same `TruncateMemoryText(..., 400)` used for
   individual memory previews. Silently omitted if no row exists yet —
   no "no relationship yet" noise.
8. **Cleanup** — both `purge_orphaned_memories()` (24-hour periodic pass)
   and `.llmc memoryclean` (GM command) now issue a second `DELETE`
   against `llm_bot_relationships` with the identical
   `characters`-orphan `LEFT JOIN` shape as `llm_bot_memories`, in the
   same function/command.

### Memory condensation

Distinct from both eviction (`_evict_one_used()`, hard-cap driven, deletes
with no replacement) and relationship tracking (a running disposition
summary), condensation proactively folds a bot-player pair's *low-value*
memories into a small number of higher-quality digest memories, so a
pair's memory pool stays meaningful instead of either growing forever or
being trimmed down to bare deletions once it hits the cap.

1. **Trigger** — `_ensure_cap_and_insert()` calls
   `_maybe_trigger_condensation()` right after every successful insert,
   passing the pool's *derived* post-insert size (pre-insert count, plus one
   if the row landed active, minus one if an eviction made room) rather than
   re-querying, since this sits on the hot path of every memory write. Once
   a pair's active memory count crosses
   `LLMChatter.Memory.Condensation.TriggerPercent` of
   `LLMChatter.Memory.MaxPerBotPlayer`, a background job is submitted to
   `condensation_executor` — a dedicated, single-worker `ThreadPoolExecutor`
   kept separate from both `memory_executor` and `relationship_executor` so
   a slow condensation LLM call can never compete with either. A
   `MaxPerBotPlayer <= 0` misconfiguration returns early rather than
   dividing through it. A `(bot_guid, player_guid)` guard set
   (`_condensing_pairs`) prevents duplicate concurrent submissions for the
   same pair; the `submit()` call itself is wrapped in a try/except so an
   executor-shutdown race can never leave a pair stuck in the guard set (which
   would block that pair's condensation forever) or propagate past the insert
   it's piggybacking on.

   Note the trigger is deliberately **proactive**, firing well before the
   hard cap. A reactive trigger (condensing at the cap) would always lose the
   race against `_evict_one_used()`, which runs inline while condensation
   needs an LLM round-trip — by the time the pass finished, eviction would
   already have deleted the rows it meant to fold.
2. **Candidate selection (read phase)** — `_get_condensation_candidates()`
   selects the pair's active rows with `importance_score` **below**
   `LLMChatter.Memory.Condensation.ProtectFloor`, excludes rows whose
   `condensation_generation` has already reached
   `LLMChatter.Memory.Condensation.MaxGenerations`, excludes the pair's
   single highest-`effective_score` row (the same guard `_evict_one_used()`
   uses, via `_top_row_exclusion_sql()`, so a bot's most valuable memory of a
   player is never condensed away), orders by decay-aware `effective_score`
   ascending (least valuable first), and caps the result at
   `LLMChatter.Memory.Condensation.MaxCandidates` rows (clamped up to the
   default if configured `< 1`).

   Because rows are ordered least-valuable-first and the `LIMIT` is applied
   in SQL, an oversized pool of eligible memories is condensed *gradually
   over several passes* — the "leave core memories alone, tidy the routine
   stuff a little at a time" behavior — rather than an entire backlog being
   folded into `MaxDigests` digests in one shot.

   At least `LLMChatter.Memory.Condensation.MinCandidates` rows (clamped up
   to the default if configured `< 2`, since folding fewer than two rows is
   not condensation) must survive selection or the pass is skipped silently;
   the trigger fires again next time a qualifying insert crosses the
   threshold.
3. **Prompt** — `_build_condensation_prompt()` asks the LLM to fold the
   candidate batch into 1 to `LLMChatter.Memory.Condensation.MaxDigests`
   digest memories, reusing `_IMPORTANCE_RUBRIC` so a digest's suggested
   importance stays on the same 1-10 scale as every other
   memory-generation prompt in this file, and threading bot and player
   identity so grammatical gender stays correct.

   The candidate texts are rendered with
   `sanitize_memory_for_prompt(..., max_chars=MEMORY_TEXT_MAX_CHARS)` —
   the full 500-char stored ceiling, **not** the routine 200-char
   (`DEFAULT_PROMPT_MEMORY_CHARS`) preview used for ordinary recall
   injection. Condensation *deletes* every row it shows the model, so
   anything the prompt truncated away would be destroyed without the model
   ever having seen it.

   The batch is additionally capped by cumulative character count
   (`_cap_candidates_by_chars()` against `_CONDENSATION_MAX_INPUT_CHARS`,
   defense in depth mirroring `_RELATIONSHIP_MAX_INPUT_CHARS`) so a prompt
   is provably bounded regardless of how `MaxCandidates` or individual
   memory lengths are configured. Whatever survives that trim is exactly
   what is folded *and* deleted — a memory cut by the char budget is left
   untouched for a future pass, never silently deleted unrepresented. At
   least the first candidate is always kept, so one oversized memory can't
   stall condensation entirely.

   `MaxDigests` is clamped to `max(1, MinCandidates - 1)`. Digest inserts
   bypass `_ensure_cap_and_insert()`, so they are not themselves cap-checked
   or eviction-guarded; that is only safe while a pass strictly *shrinks*
   the pool. With `MaxDigests >= MinCandidates` a pass could replace N rows
   with N or more digests and, repeated, grow the pool past
   `MaxPerBotPlayer` with nothing to trim it back.
4. **Connection lifecycle — two short-lived connections, never one**
   The read phase (candidate selection plus bot/player identity lookups)
   runs on one connection which is **closed before the LLM call**, and the
   write phase opens a fresh one only once a usable response is in hand.
   mysql-connector runs with autocommit off, so keeping the read connection
   open would hold that SELECT's transaction and read view — and pin InnoDB's
   purge — for the whole multi-second round-trip. Atomicity is only ever
   needed across the insert-then-delete pair, and that lives entirely on the
   second connection.
5. **Storage and atomicity (write phase)** — on the write connection, in
   **one transaction sharing a single `conn.commit()`**: insert the digest
   row(s), delete the source rows, and (if needed) rewind the relationship
   watermark. A crash between insert and delete can therefore never lose
   memories without gaining their replacement.

   Each digest's `importance_score` is bounded by
   `min(llm_suggested_score, max(source_importances))`, so a digest can
   never look more important than the single most important memory it
   absorbed. Since every source is already below `ProtectFloor` by
   construction, that also guarantees a digest can never itself cross the
   protect floor — which is exactly why re-condensation has to be bounded by
   `condensation_generation` instead.

   Three details keep a digest from crowding out the real memories that
   outlive it:
   - it inherits the **oldest** source's `created_at` rather than `NOW()`,
     so its decay clock is not reset and it can never score higher than the
     already-decayed rows it replaced. The auto-increment `id` still records
     real insertion order for auditing;
   - it is inserted `used=1`, so `_evict_one_used()`'s preference for `used`
     rows treats it like any other read memory rather than making it last to
     go;
   - it carries `max(source generations) + 1` in `condensation_generation`,
     capping how many times the same material can be re-folded
     (`MaxGenerations`, default 2: originals are gen 0, can fold into a gen-1
     digest, which can fold into a gen-2 digest, which is final).
6. **Relationship watermark rewind (same transaction)** — inheriting the
   oldest source's `created_at` interacts badly with the relationship
   summary's watermark, so condensation repairs it inline.

   `_maybe_update_relationship()` picks up new material with
   `created_at > updated_through_created_at`. Folding a source memory
   *newer* than that watermark into a digest stamped *older* than it would
   leave that content permanently invisible to relationship tracking: the
   sources are deleted and the digest sorts behind the watermark, so no
   later pass can ever reach it — and each further pass can fold yet another
   already-summarized memory into an older-stamped digest, so the loss
   compounds instead of resolving.

   `_rewind_relationship_watermark()` therefore sets the watermark to one
   second before the digest's `created_at` whenever a condensed source was
   newer than it, guaranteeing the next relationship pass re-folds the
   digest. Re-folding already-summarized material is harmless (the prompt is
   "update this summary with these memories"); losing it is not.

   Two hard guarantees, both enforced in the `UPDATE`'s WHERE clause:
   - **rewind only, never advance** — the update requires the current
     watermark to be strictly newer than the target, so a pair whose
     watermark already sits behind the digest keeps it and does not skip
     unrelated unsummarized memories in between;
   - **never creates a row** — it is an `UPDATE`, not an upsert, so a pair
     with no relationship row yet stays without one; the relationship pass
     creates that row on its own terms.

   It also only fires when a source really was newer than the watermark:
   ordinary condensation of long-summarized memories changes nothing. See
   "Relationship tracking" step 3 above for the optimistic-concurrency guard
   on the other side of this interaction, which exists specifically so a
   concurrent relationship pass cannot clobber this rewind.
7. **Fail-safe** — on any failure (LLM call failure, missing/unparseable
   response, or zero usable digests), the function logs and aborts with
   no fallback deletion of source rows — a malformed response never
   causes data loss, unlike the eviction path it complements. The
   `_condensing_pairs` entry is always cleared in a `finally` block, and
   both connections are closed there too, so a failed run never permanently
   blocks future attempts or leaks a connection.

### Session vibe

A group carries a short-lived ambient "vibe" — a mood cue that lingers after
something significant happens, so the party doesn't snap back to a neutral
tone the moment a wipe or a boss kill is over.

1. **Set** — `_ensure_cap_and_insert()` writes the vibe whenever a newly
   inserted memory's `importance_score` is at or above
   `LLMChatter.GroupChatter.VibeImportanceThreshold` (default `5`). It
   stores the memory's `mood` as the vibe and its `memory_type` as
   `source_type`, so the prompt can name the *cause* and not just the mood.
   The in-memory session (if one exists) is updated **and** the row is
   UPSERTed into `llm_group_vibe` via `upsert_group_vibe()` in
   `chatter_db.py`, reusing the caller's connection rather than opening a
   second one on the hot path. The DB write is unconditional: gating it on a
   live in-memory session would drop the vibe exactly when it matters most
   (right after a restart or a session CLEANUP wipe). This block is
   deliberately *not* lock-protected — it can already run inside the
   non-reentrant per-group lock, so re-acquiring would deadlock, and a couple
   of plain field writes are safe enough for a best-effort cue. Both the
   `importance` and `group_id` values are range-coerced against the unsigned
   columns so a malformed score can't fail the INSERT under strict mode, and
   the whole persist is fail-open (a DB hiccup must never break the memory
   insert it's part of).
2. **Read and lazy expiry** — `get_session_vibe_details(group_id, config)`
   returns `(vibe, source_type)`, or `(None, None)`. There is no background
   timer; the vibe simply "expires" once
   `LLMChatter.GroupChatter.VibeDurationSeconds` (default `600`) have elapsed
   since `set_at`, and callers re-check on every read. Expiry is anchored to
   the **original** `set_at`, so a restart cannot extend a vibe's lifetime.
   The read fast-paths the in-memory value, falls back to the DB row when
   that is missing or expired, and re-seeds the in-memory session from the DB
   so repeat reads stay cheap. A row found expired is deleted **conditionally
   on the `set_at` that was read** (`delete_group_vibe()`), so a
   concurrently-written newer vibe is never destroyed.
3. **Duration validation** — `VibeDurationSeconds` is parsed defensively.
   Empty, non-numeric, NaN, infinite, zero and negative values are all
   rejected in favour of the documented `DEFAULT_VIBE_DURATION_SECONDS`
   (600). Zero/negative are specifically rejected rather than honoured
   because they would expire every vibe the instant it was set, with no other
   symptom — the feature would look simply broken. Because this runs on the
   group-event hot path, the warning is emitted **once per distinct bad
   value** (`_warn_invalid_vibe_duration()`), which keeps a misconfiguration
   visible without flooding the log and still re-reports if the admin edits
   the key to a *different* bad value.
4. **Negative-result cache** — the common case is "this group has no vibe",
   and paying for a DB round-trip on every group event to learn that is
   wasteful. When a DB check finds no usable vibe, the session records a
   `vibe_miss_until` marker suppressing further checks for
   `_VIBE_MISS_CACHE_SECONDS` (45s). This is only consulted when the
   in-memory fast path already found nothing, and it is cleared the instant a
   vibe is actually written (`_ensure_cap_and_insert()` pops it), so the
   staleness window can only ever delay discovering an **absence**, never
   hide a live vibe.
5. **Teardown** — `teardown_group_session()` clears the in-memory session
   and its lock; `cleanup_stale_groups()` in `chatter_db.py` also deletes the
   group's `llm_group_vibe` row, and `cleanup_all_session_data()` truncates
   the table when no players are online. This matters: group ids are reissued
   after a worldserver restart, so a leftover row would let a disbanded
   group's mood bleed into an unrelated party that inherits its id while the
   vibe is still inside its window.
6. **Consumers** — `chatter_group.py` (idle chatter, player-response and
   bot-question paths) and `chatter_handler_pipeline.py` (the shared group
   reaction pipeline) call `get_session_vibe_details()` and render it with
   `build_session_vibe_line()` in `chatter_prompts.py`, which names the cause
   from `source_type` and asks for the feeling to show in the *delivery*
   rather than be stated. The whole sentence — scaffolding included — is
   localized. `get_vibe_mood_pool()` additionally maps the vibe onto the
   separate conversation-mood vocabulary via `VIBE_MOOD_FAMILIES` /
   `VIBE_FAMILY_MOODS` in `chatter_constants.py`, biasing the mood roll
   instead of replacing it. `get_session_vibe()` is a thin wrapper for
   callers that only need the word.

### Orphan recovery and startup cap enforcement

`activate_orphaned_memories()` runs at bridge startup and promotes `active=0`
rows left behind by sessions that ended without a clean farewell (bridge
crash, server restart). Groups still present in `llm_group_bot_traits` are
skipped — those are live sessions that rehydration will handle. A pair whose
orphaned rows span at least `SessionMinutes` is promoted to `active=1`;
anything shorter is discarded.

A bulk promotion can push a pair over `LLMChatter.Memory.MaxPerBotPlayer`, so
the function trims each promoted pair back down afterwards. Two properties of
that trim matter:

- **`max_per <= 0` is treated as a misconfiguration, not as "forget
  everything."** Without the guard the excess slice degenerates to
  `ids[:len(ids)]` and silently wipes every active memory the pair has, at
  startup. It now logs a warning and skips the trim, matching how every other
  cap-consuming path in the file bails.
- **It deletes by the same rule as every other eviction path.** Ordering uses
  the decay-aware `_effective_score_sql()` (not raw `importance_score`), and
  the pair's single top-scoring row is protected via
  `_top_row_exclusion_sql()`, exactly as in `_evict_one_used()` and
  `_get_condensation_candidates()`. Deleting by a *different* rule here is
  precisely how a row that eviction would have protected gets dropped
  instead. Because the protected top row is excluded from the candidate id
  list, the pair's real active count is `len(ids) + 1`.

### Known gap: `first_meeting` rows are not protected from deletion

Older revisions of this document described `first_meeting` memories as
"immune to prune". **That is not what the code does, and the wording has been
corrected above.** As of this branch:

- `_evict_one_used()` has no `memory_type` filter — a `first_meeting` row is
  an ordinary eviction candidate.
- `_get_condensation_candidates()` has no `memory_type` filter either, and
  `insert_first_meeting_memory()` stores importance `5` by default, which is
  below the default `ProtectFloor` of `7` — so first-meeting memories are
  condensation-eligible.
- the orphan-recovery cap trim in `activate_orphaned_memories()` likewise has
  no exemption.

The only protection a `first_meeting` row actually receives is the generic
top-row exclusion (`_top_row_exclusion_sql()`), which protects it only while
it happens to be the pair's single highest-`effective_score` memory. It *is*
genuinely immune to short-session discard, because it is inserted `active=1`
and that DELETE only targets `active=0` rows, and
`insert_first_meeting_memory()` will never create a duplicate for a pair that
already has one.

This is a **pre-existing gap, not something this branch introduced** — it was
surfaced in the discussion on upstream PR #54. Adding a
`memory_type != 'first_meeting'` exemption to all three paths is planned as a
separate follow-up; it is deliberately out of scope here because it changes
retention behavior rather than documenting it. Until then, treat any claim of
first-meeting immunity as scoped strictly to the short-session discard path.

### Files

| File | Role |
|------|------|
| `chatter_memory.py` | Session tracking, memory generation (with `player_name` threading and DB fallback), importance scoring, decay-aware retrieval/eviction (with top-memory eviction guard), shared event memories, orphan recovery + startup cap trim (`activate_orphaned_memories`), orphan purge, flush, retrieval; relationship tracking (`relationship_executor`, `_maybe_queue_relationship_update`, `_maybe_update_relationship`, `_sanitized_relationship_watermark`, `_extend_candidates_to_timestamp_boundary`, `get_relationship_summary`); low-value-memory condensation (`condensation_executor`, `_maybe_trigger_condensation`, `_condense_low_value_memories`, `_get_condensation_candidates`, `_cap_candidates_by_chars`, `_insert_memory_row`, `_rewind_relationship_watermark`); session vibe read/expiry (`get_session_vibe_details`, `get_session_vibe`, `_warn_invalid_vibe_duration`, `_VIBE_MISS_CACHE_SECONDS` negative cache) |
| `chatter_db.py` | `llm_group_vibe` persistence helpers (`upsert_group_vibe`, `get_group_vibe`, `delete_group_vibe` — all accept a caller-owned connection); deletes the group's vibe row in `cleanup_stale_groups()` and clears the table in `cleanup_all_session_data()` |
| `chatter_prompts.py` | `build_session_vibe_line()` (localized, names the cause from `source_type`), `get_vibe_mood_word()`, `get_vibe_source_phrase()`, `get_vibe_mood_pool()` |
| `chatter_handler_pipeline.py` | Reads the session vibe in the shared `run_group_handler()` pipeline and injects the rendered vibe line alongside the per-bot mood line |
| `chatter_group.py` | Calls `start_session`, `get_bot_memories`, `get_relationship_summary`, first-meeting insert |
| `chatter_group_handlers.py` | `_kill_post_success()` / `_wipe_post_success()` filter altbot candidates and call `queue_shared_event_memory()` for party-wide kill/wipe memories |
| `chatter_group_prompts.py` | `build_bot_greeting_prompt` — reunion mode and `<past_memories>` injection; `build_player_response_prompt()` / `build_bot_question_prompt()` — `<relationship>` block injection |
| `llm_chatter_bridge.py` | 24-hour periodic `purge_orphaned_memories()` call in the main loop; drains `memory_executor`, `relationship_executor`, and `condensation_executor` on shutdown |
| `src/LLMChatterCommand.cpp` | `.llmc memoryclean` GM command, runs the orphan-purge `DELETE`s directly |
| `src/LLMChatterCommand.cpp` | `.llmc memory <botname>` player command, synchronous decay-ordered memory readout plus the relationship line |
| `src/LLMChatterGroupJoin.cpp` | Resolves `PlayerbotAI::IsAltBot()` at join time and threads `is_altbot` into the join event payload |

### Database tables

| Table | Purpose |
|-------|---------|
| `llm_bot_memories` | Per-bot-per-player memory journal. `memory_type` includes `first_meeting`, `boss_kill`, `party_member`, `ambient`, `condensed` (see "Memory condensation" above), etc. `importance_score` (TINYINT UNSIGNED, default 5) drives decay-aware ranking; `condensation_generation` (TINYINT UNSIGNED, default 0) caps how many times the same material may be re-folded. |
| `llm_bot_identities` | Persistent personality traits keyed by `bot_guid`. Regenerated only on `IdentityVersion` bump. |
| `llm_group_bot_traits` | `is_altbot` (TINYINT(1), default 1) marks whether a bot is player-owned; gates memory generation. |
| `llm_bot_relationships` | One running LLM-maintained `summary` per `(bot_guid, player_guid)`, plus `updated_through_created_at` (DATETIME, default `1970-01-01 00:00:00`) — the timestamp watermark marking the newest memory already folded into the summary. Replaced the earlier `updated_through_memory_id` column, which condensation broke (migrations `20260828_relationship_timestamp_watermark.sql`, `20260830_drop_relationship_id_watermark.sql`). Condensed from `llm_bot_memories`, not itself journaled. |
| `llm_group_vibe` | One row per `group_id` holding the group's current ambient mood: `vibe` (the mood word), `source_type` (the `memory_type` that set it; NULL = unknown cause, prompt falls back to sourceless phrasing), `importance`, and `set_at` (unix seconds, the expiry anchor). UPSERTed on any memory crossing `VibeImportanceThreshold`, lazily expired on read, deleted on group teardown. |

### Config keys

| Key | Default | Purpose |
|-----|---------|---------|
| `LLMChatter.Memory.Enable` | `1` | Master toggle |
| `LLMChatter.Memory.SessionMinutes` | `3` | Minimum session length (minutes) before a session's memories are activated |
| `LLMChatter.Memory.MaxPerBotPlayer` | `30` | Cap on active memories per bot–player pair. Also the base for the condensation trigger; a non-positive value disables the condensation trigger and the startup cap trim rather than deleting everything |
| `LLMChatter.Memory.RecallChance` | `20` | % chance a specific memory is highlighted in reunion greeting |
| `LLMChatter.Memory.UseQuickModel` | `1` | When a quick model IS configured (`LLMChatter.QuickAnalyze.Provider`/`.Model`, both empty by default), route memory-side internal LLM work (memory generation, shared-event memories, condensation digests, relationship summaries) to it instead of `LLMChatter.Model`. This text is never read verbatim by a player — it is stored, re-injected as context, and re-worded by the main model before it reaches chat — so it is safe to route cheaply once a quick model exists. No effect at all if no quick model is configured; set `0` to force memory work onto the main model even when one is |
| `LLMChatter.Memory.IdentityVersion` | `1` | Bump to force personality regeneration for all bots |
| `LLMChatter.Memory.MaxInjectTokens` | `400` | Approximate token budget for memories injected into a single prompt (reunion greeting, recall) |
| `LLMChatter.Memory.DecayMaxImportance` | `3` | Highest importance score still subject to decay; higher-scored memories never decay |
| `LLMChatter.Memory.DecayDays` | `30` | Days a decaying memory takes to lose one point of importance (floored at 1) |
| `LLMChatter.Memory.Relationship.Enable` | `1` | Master toggle for background relationship-summary condensation |
| `LLMChatter.Memory.Relationship.UpdateThreshold` | `5` | New active memories needed since the last update before a re-summarization is triggered at farewell |
| `LLMChatter.Memory.Relationship.MaxChars` | `400` | Maximum stored relationship-summary length |
| `LLMChatter.Memory.Relationship.MaxTokens` | `300` | Output budget for the relationship-condensation LLM call |
| `LLMChatter.Memory.Condensation.Enable` | `1` | Master toggle for background low-value-memory condensation |
| `LLMChatter.Memory.Condensation.TriggerPercent` | `80` | Percent of `MaxPerBotPlayer` a pair's active memory count must reach before a condensation pass is submitted |
| `LLMChatter.Memory.Condensation.ProtectFloor` | `7` | Memories at or above this importance score are never condensed |
| `LLMChatter.Memory.Condensation.MinCandidates` | `4` | Minimum eligible candidates required before a condensation LLM call is worth making |
| `LLMChatter.Memory.Condensation.MaxCandidates` | `8` | Maximum least-valuable eligible candidates pulled into a single condensation pass; keeps condensation gradual and incremental instead of folding an entire oversized pool away in one shot |
| `LLMChatter.Memory.Condensation.MaxDigests` | `2` | Maximum digest memories one condensation pass may produce. Clamped at runtime to `max(1, MinCandidates - 1)` so a pass always retires at least one row and can never grow the memory pool |
| `LLMChatter.Memory.Condensation.MaxTokens` | `500` | Output budget for the condensation LLM call |
| `LLMChatter.Memory.Condensation.MaxGenerations` | `2` | How deep digests-of-digests may go. A digest carries `max(source generations) + 1`; rows at or above this are never picked as candidates again. Without it, digests (always below `ProtectFloor` by construction) would be re-condensed forever, drifting further from the source facts each round |
| `LLMChatter.GroupChatter.VibeImportanceThreshold` | `5` | Minimum `importance_score` a new memory must reach to set the group's ambient session vibe. Raise it so only genuinely big moments change the party's mood |
| `LLMChatter.GroupChatter.VibeDurationSeconds` | `600` | How long a session vibe stays active, measured from when it was set. Non-positive, non-numeric, NaN and infinite values are rejected in favour of the 600s default with a one-time warning, since they would expire every vibe instantly |

---

## 13o. Queue and Message Cleanup

The system has four cleanup layers that work together to ensure stale
queue entries and undelivered messages are never visible to players after
a group ends.

| Layer | Trigger | Scope | Mechanism |
|-------|---------|-------|-----------|
| `OnRemoveMember` | Bot removed from group | That bot only | Cancels queue entries containing `bot_guid`; marks messages delivered |
| `CleanupGroupSession` | Group disbands or no real player remains | Full group | Cancels all queue entries for group bots; marks messages delivered. Runs **before** deleting `llm_group_bot_traits` so IN-subqueries resolve correctly |
| Bridge TTL | Every poll cycle | Global (5-min window) | Cancels `llm_chatter_queue` entries `> 5 MINUTE` old; marks messages `> 5 MINUTE` past `deliver_at` |
| `OnPlayerLogin` | Real player logs in | Global (30-second grace) | Crash-recovery only. Cancels queue entries `> 30 SECOND` old; marks messages `> 30 SECOND` past `deliver_at`. Protects freshly-queued entries from other online players |

The 30-second grace in `OnPlayerLogin` means other players' active entries
(queued < 30 seconds ago) are safe. In normal operation `CleanupGroupSession`
handles everything; `OnPlayerLogin` only matters after a server crash.

---

## 13p. Screenshot Vision

Bots can react to what the player actually sees on screen. A host-side
Python agent captures the WoW game window, sends the screenshot to a
vision-capable LLM, and the bridge generates in-character party chat
from the resulting description.

### Two-stage architecture

**Stage 1 — Host agent** (`screenshot_agent.py`):

1. Captures the WoW game window via Win32 API (`BitBlt`)
2. Crops UI elements (bottom 20%, sides 12%) to isolate the 3D world
3. Resizes to `MaxWidthPx` and encodes as JPEG (`JpegQuality`)
4. Sends to vision LLM (OpenAI, Anthropic, Google, or OpenRouter)
5. Receives structured JSON: environment description, atmosphere,
   canonical tags (`landmark_type`, `biome`, `weather`, `time_of_day`,
   `creature_presence`)
6. Canonical tag dedup prevents repeated observations of the same scene
7. Inserts `bot_group_screenshot_observation` event into
   `llm_chatter_events` via direct MySQL connection. If available, the
   selected bot's live travel state from `llm_group_bot_traits` is
   embedded into event `extra_data`.

**Stage 2 — Bridge handler** (`chatter_screenshot_handler.py`):

1. Claims the event from `llm_chatter_events`
2. Resolves zone/subzone context via existing `get_zone_name()`,
   `get_zone_flavor()`, `get_subzone_name()`, `get_subzone_lore()`,
   `get_dungeon_flavor()`, `get_time_of_day_context()`
3. Builds bot identity via `build_bot_identity(name, race, class, gender)`
4. Adds live travel context when present. This lets the LLM use taxi
   flight, flying mount, ground mount, swimming, or world-transport
   context while avoiding impossible ground actions.
5. Rolls for single statement (`run_single_reaction()`) or multi-bot
   conversation (`append_conversation_json_instruction()` +
   `parse_conversation_response()`)
6. Writes messages to `llm_chatter_messages` for C++ delivery

### Config keys

All under `LLMChatter.Screenshot.*`:

| Key | Default | Purpose |
|---|---|---|
| `Enable` | 0 | Enable/disable the feature |
| `IntervalMinSeconds` | 45 | Minimum seconds between captures |
| `IntervalMaxSeconds` | 90 | Maximum seconds between captures |
| `Chance` | 60 | % chance per interval tick |
| `VisionProvider` | openai | Vision LLM provider (openai, anthropic, google, or openrouter) |
| `VisionModel` | gpt-4o-mini | Vision model name |
| `ConversationChance` | 30 | % chance of multi-bot conversation vs statement |
| `MaxWidthPx` | 800 | Max image width for vision API |
| `JpegQuality` | 70 | JPEG compression quality |
| `BoundAccountId` | 0 | Account ID to find grouped bots |
| `DBHost` | 127.0.0.1 | MySQL host (host machine, not Docker) |

### Relevant files

| File | Responsibility |
|---|---|
| `tools/screenshot_agent.py` | Host-side capture, vision API, event insertion |
| `tools/chatter_screenshot_handler.py` | Bridge handler, prompt building, delivery |
| `tools/llm_chatter_bridge.py` | Event routing via registry-built handler map |
| `tools/chatter_event_registry.py` | Registry entry for `bot_group_screenshot_observation` |
| `conf/mod_llm_chatter.conf.dist` | Config key definitions |

### Notes

- The agent runs on the host machine, not inside Docker
- No C++ changes are required
- The vision biome tag is excluded from bot prompts (unreliable);
  zone/subzone names from the database are authoritative
- Indoor scenes are explicitly supported in the vision prompt
- A `skip_reason` field in the structured JSON aids debugging when
  screenshots are rejected (e.g., loading screen, character select)
- Cost: approximately $0.05-0.10/hour at default settings with
  GPT-4o-mini

---

## 13q. Proximity Chatter

Bots and NPCs can engage in ambient `/say` conversations as the player
moves through the world. Unlike General-channel chatter (zone-wide) or
party chat (group-scoped), proximity chatter is spatially local — only
players and bots within `/say` range (~40 yards) see it.

### Scan and trigger

C++ `CheckProximityChatter()` runs on a configurable timer (default
30s) in `LLMChatterWorld.cpp`, delegating to
`LLMChatterProximity.cpp`:

1. Iterates real players in the world
2. Scans within `ProximityChatter.ScanRadius` (default 40 yards) for
   eligible humanoid NPCs and party bots
3. NPC eligibility: all humanoids — guards, vendors, trainers,
   innkeepers, quest givers, citizens, sentinels, children
4. Bot eligibility: party bots can participate, but conversations
   where all speakers are party bots are skipped (idle chat handles
   that case)
5. Rolls `ProximityChatter.TriggerChance` (default 30%)
6. Selects 1-4 speakers from the candidate pool
7. Queues either a `proximity_say` (single statement) or
   `proximity_conversation` (multi-speaker) event

NPCs are identified by spawn GUID (`Creature::GetSpawnId()`) rather
than entry ID, allowing per-instance entity cooldowns (default 60s).

### Delivery channels

Two new delivery channels in `LLMChatterDelivery.cpp`:

| Channel | Packet | Visual |
|---------|--------|--------|
| `say` | `CHAT_MSG_SAY` | Normal `/say` text for bots |
| `msay` | `CHAT_MSG_MONSTER_SAY` | NPC speech bubble |

Speaker facing: each speaker faces the next speaker in the
conversation sequence via `SetFacingToObject()`. NPCs have their
orientation reset after delivery via a `BasicEvent` timer.

### Player reply detection

When a real player speaks in `/say` near a recent proximity scene,
`HandleProximityPlayerSay()` in `LLMChatterGroupCombat.cpp` detects
the reply and queues a `proximity_reply` event. The `ProximityScene`
struct tracks:

- active conversation participants
- scene location and timestamp
- the original topic context

This enables natural player-to-NPC/bot exchanges without requiring
the player to target or emote at anyone.

### Topic pool

`PROXIMITY_CHAT_TOPICS` in `chatter_constants.py` provides 250+
topics across 17 categories:

- weather, travel, local flavor, trade, rumors, daily life, military,
  faction politics, wildlife, profession, food and drink, history,
  adventure, philosophy, humor, seasonal, and general social

### Python handling

`chatter_proximity.py` owns all three event handlers:

| Event type | Handler | Behavior |
|------------|---------|----------|
| `proximity_say` | Single NPC or bot statement | One speaker, zone context + topic |
| `proximity_conversation` | Multi-speaker conversation | 2-4 speakers with staggered delivery |
| `proximity_reply` | Player reply response | NPC/bot replies to player `/say` |

Prompts include nearby entity names so speakers can address each other
by name. Uses global `EmoteChance` and `ActionChance` gates (not custom
proximity-specific ones).

### C++ ownership

| File | Responsibility |
|------|----------------|
| `LLMChatterProximity.cpp` | Scan, filter, select, queue, scene tracking |
| `LLMChatterProximity.h` | Declarations for world and group combat |
| `LLMChatterDelivery.cpp` | `say` and `msay` channel delivery, facing, NPC reset |
| `LLMChatterWorld.cpp` | Timer delegation |
| `LLMChatterGroupCombat.cpp` | `HandleProximityPlayerSay()` hook |
| `LLMChatterShared.cpp` | `FindCreatureBySpawnId()`, `GetCreatureRoleName()` |
| `LLMChatterConfig.h/.cpp` | 15 proximity config members |

### Python ownership

| File | Responsibility |
|------|----------------|
| `chatter_proximity.py` | All 3 event handlers and prompt builders |
| `chatter_constants.py` | `PROXIMITY_CHAT_TOPICS` (250+ entries) |
| `chatter_db.py` | `npc_spawn_id` and `player_guid` params on insert |
| `chatter_event_registry.py` | 3 new `EventSpec` entries |

### Config keys

All under `LLMChatter.ProximityChatter.*`:

| Key | Default | Purpose |
|-----|---------|---------|
| `Enable` | 1 | Master toggle |
| `CheckIntervalSeconds` | 30 | Scan timer interval |
| `ScanRadius` | 40 | Yards around player to scan |
| `TriggerChance` | 30 | % chance per scan per player |
| `ConversationChance` | 50 | % multi-speaker vs single statement |
| `EntityCooldown` | 60 | Seconds per-entity (spawn GUID) cooldown |
| `PlayerAddressChance` | 20 | % chance to address the real player |
| `MaxSpeakers` | 4 | Maximum speakers per conversation |
| `LineDelayMin` | 3 | Min seconds between conversation lines |
| `LineDelayMax` | 5 | Max seconds between conversation lines |
| `ReplyWindowSeconds` | 60 | How long a scene stays active for replies |
| `ReplyChance` | 80 | % chance to reply when player speaks in scene |
| `MaxTopicLength` | 0 | Max topic hint length (0 = no limit) |
| `NPCOnly` | 0 | When 1, only NPCs speak (no party bots) |
| `ListenRange` | 40 | `/say` audibility range (should match ScanRadius) |

### Schema changes

Migration `20260403_proximity_chatter.sql` adds two columns to
`llm_chatter_messages`:

- `npc_spawn_id` INT UNSIGNED DEFAULT NULL — creature spawn GUID for
  NPC speakers
- `player_guid` INT UNSIGNED DEFAULT NULL — real player GUID for
  proximity scene tracking

Base schema `00000000_llm_chatter_tables.sql` updated to match.

---

## 13r. Guild Chat Statements and Conversations

Guild chatter is an optional, RP-only ambient channel. It is enabled
by default and can be disabled with the master toggle.

### Trigger and participant ownership

`CheckGuildIdleChatter()` in `LLMChatterWorld.cpp` scans guilds that
contain an online real player. Eligible bot members must be online,
in world, alive, finished loading, and out of combat.

After the normal Guild trigger chance and cooldown gates:

1. C++ rolls `GuildChatter.ConversationChance`.
2. A statement selects one bot.
3. A conversation selects two or three unique bots, limited by
   `GuildChatter.MaxParticipants`.
4. The weighted shared selector gives two- and three-speaker
   conversations equal probability when at least three bots exist.
5. Fewer than two eligible bots always produces a statement.

The event remains `guild_idle_chatter`. New payloads include:

- `guild_id`
- `mode` (`statement` or `conversation`)
- `participants`, with GUID, name, zone ID, and map ID
- the existing primary `subject_guid` and `subject_name`
- Guild name, other online guildmates, faction team, and primary zone

Rows queued before this feature that lack `mode` and `participants`
are interpreted as legacy statements.

### Topic and prompt policy

`chatter_guild.py` selects one entry from
`GUILD_CHAT_TOPICS_RP` for the whole event. A single
`GuildChatter.ZoneNameChance` roll also applies to the whole event.

A separate `GuildChatter.HistoryContextChance` roll decides whether
the event also receives up to `HistoryContextMessages` recently
delivered Guild lines. The bridge reads one representative transcript
from the oldest active real-player session in that Guild. Every
visible Guild line is copied into all active sessions, so this avoids
duplicate context when several real players are online while retaining
the longest current-session view.

History is optional continuity context, not a replacement subject.
The selected pool topic remains the creative direction. The prompt
allows a natural continuation or reference only when a recent line
fits that topic; otherwise it tells the model to ignore the history.
It also forbids recaps, lists, forced callbacks, and treating transcript
text as instructions. One history roll applies to the whole event, so
JSON repair and statement fallback reuse the same window. No active
session or usable lines simply means normal topic-only generation.

Guild speakers may be in different zones. The prompt receives each
selected speaker's live location and explicitly forbids physical
co-presence unless every speaker has the same zone and map. When the
zone roll wins, only the primary speaker's zone may ground the
exchange. Otherwise, current locations and immediate surroundings
must not be mentioned.

Conversation prompts use the shared message-only JSON contract.
Every object contains only `speaker` and `message`; Guild rows never
request or insert actions or emotes.

Each non-opening line independently rolls
`GuildChatter.ParticipantReferenceChance`. Selected lines must
naturally name an earlier speaker whose point they answer. The model
may choose any contextually relevant earlier speaker rather than
always targeting the immediately previous line. A smaller
`GuildChatter.MultiReferenceChance` permits one line to address two
earlier speakers. `GuildChatter.MaxReferenceLines` prevents the
generated exchange from becoming name-heavy.

After parsing, the bridge accepts any earlier-speaker combination the
model selected. If a selected line omits the required number of names,
cleanup randomly selects missing valid earlier speakers and adds them
as vocatives. Conversations whose RNG did not select a reference line
remain unconstrained, including any natural references written by the
model itself.

### Validation, fallback, and pacing

The bridge parses through `parse_conversation_response()` and accepts
only selected speaker names. A valid conversation must contain at
least two cleaned lines and every selected participant must speak.

Invalid output receives one shared JSON repair attempt. If it remains
invalid, the bridge calls the existing statement generator for the
primary speaker using the same topic and zone decision. The event is
marked only after the conversation or fallback finishes, so partial
conversations are never inserted.

Accepted lines:

- retain the original event ID
- use increasing sequence values
- start at a two-second delay
- add `calculate_dynamic_delay()` for each later line
- use the actual speaker GUID
- insert with `channel='guild'` and
  `owner_subsystem='guild'`

One whole exchange consumes one existing per-Guild cooldown.

### Guild configuration

| Key | Default | Owner | Purpose |
|-----|---------|-------|---------|
| `Enable` | 1 | Server | Master Guild chatter toggle |
| `Chance` | 15 | Server | Trigger chance per eligible scan |
| `Cooldown` | 300 | Server | Seconds between Guild events |
| `ScanInterval` | 30 | Server | Seconds between Guild scans |
| `ConversationChance` | 50 | Server | Conversation vs statement |
| `MaxParticipants` | 3 | Server | Conversation cap, clamped to 2-3 |
| `MaxTokens` | 200 | Bridge | Base generation token budget |
| `MaxConversationLines` | 4 | Bridge | Maximum conversation lines |
| `HistoryContextChance` | 35 | Bridge | Chance to attach recent visible Guild history |
| `HistoryContextMessages` | 15 | Bridge | Maximum autonomous-history lines |
| `ParticipantReferenceChance` | 25 | Bridge | Per-reply name-reference chance |
| `MultiReferenceChance` | 15 | Bridge | Conditional two-name chance |
| `MaxReferenceLines` | 2 | Bridge | Forced reference-line cap |
| `ZoneNameChance` | 20 | Bridge | Primary-zone mention chance |

---

## 13s. Player-Driven Guild Replies and Session Memory

When Guild chatter and `GuildChatter.PlayerReplies.Enable` are enabled,
every eligible real-player Guild message queues a bot response. The
guarantee applies when the player still has a current login session, at
least one eligible Guild bot exists, and the configured LLM returns a
usable response.

### Session lifecycle and transcript

`LLMChatterGuild.cpp` owns the session boundary:

- login clears any stale row and creates a fresh per-player session
- logout cancels pending turns and deletes the session and transcript
- a new login never inherits the previous login's Guild memory
- each Guild line from a real player is recorded in every active
  session for that Guild
- successfully delivered bot Guild lines are likewise recorded in
  every active Guild session

`llm_guild_session_history.source_kind` distinguishes `player`, `reply`,
and `ambient` lines. The reply prompt may see recent visible lines of
all three kinds. By default, the latest 15 visible Guild messages are
always available when a real player's Guild message is being answered,
including ambient statements and every line of ambient conversations.
Autonomous Guild statements and conversations receive the same raw
visible-line window only when their independent history-context roll
wins. They never receive the compact rolling summary. Rolling summaries
include only player-driven interaction (`player` and delivered `reply`)
so older ambient chatter does not dilute the relationship memory.

### Turn ownership and interruption

Each player session has a monotonically increasing `turn_id`. A new
player message:

1. advances the turn
2. cancels a pending or queued login greeting
3. cancels older pending `guild_player_message` events
4. consumes any undelivered continuation rows from the older turn
5. queues the new turn after `PlayerReplies.DebounceSeconds`

The bridge checks the session and turn before generation and again
before inserting output. A player can therefore interrupt a bot
conversation naturally without receiving obsolete continuation lines.

### Reply selection

Eligible Guild bots are live, loaded, alive, out of combat, and in the
same Guild. The server shuffles and caps this candidate set; the bridge
then rolls one of three response shapes:

- one direct bot reply
- two or three independent bot replies
- a coherent two- or three-bot conversation started by the player

An explicitly addressed bot is the primary responder. Otherwise,
recent speakers receive a soft configurable weight penalty so the same
Guild member does not dominate every exchange.

The conversation roll remains independent and runs first. If it fails,
the bridge rolls the independent multi-reply chance. A message clearly
addressed to several guildmates receives a configurable bonus to that
second roll, but never forces multiple responses. With the defaults, an
ordinary player message is approximately 68% single reply, 12%
independent multi-reply, and 20% conversation.

Bridge-side RNG decides whether to request:

- a natural player-name address
- a subtle callback to earlier session material
- a follow-up question
- participant-name references inside multi-bot conversations

The model decides the wording and contextual relationship. Where a
requested name is omitted, deterministic cleanup inserts it as a
natural vocative. These features are intentionally probabilistic, not
systematic.

### Rolling summary

Prompts use a hybrid memory:

- older interaction material in a compact factual summary
- the newest configured number of messages verbatim

When older unsummarized interaction text reaches
`SessionMemory.SummaryThresholdChars`, the bridge makes one additional
summary call after reply rows have been queued. It uses the same client,
provider, and model configured for all chatter; there is no dependency
on Anthropic or any specific model. The compact prompt preserves exact
names, explicit player facts, established opinions, unresolved
questions, and promises while rejecting invention.

Recent player interaction also suppresses ambient Guild triggers for
`PlayerReplies.IdleSuppressionSeconds`, giving the exchange a natural
period of silence. The first bot reply waits for a Guild-specific
8-20-second delay by default. Additional replies use the shared full
reading, typing, and distraction pacing rather than the faster direct
response path used by other chatter.

### Player-reply configuration

| Key | Default | Owner | Purpose |
|-----|---------|-------|---------|
| `PlayerReplies.Enable` | 1 | Server | Capture player Guild turns |
| `PlayerReplies.DebounceSeconds` | 2 | Server | Rapid-turn collection window |
| `PlayerReplies.IdleSuppressionSeconds` | 90 | Server | Ambient silence after interaction |
| `PlayerReplies.MaxCandidates` | 12 | Server | Live Guild-bot candidate cap |
| `PlayerReplies.MultiReplyChance` | 15 | Bridge | Independent multi-reply chance after the conversation roll |
| `PlayerReplies.MultiAddressedBonus` | 15 | Bridge | Added multi-reply chance for group-directed messages |
| `PlayerReplies.ConversationChance` | 20 | Bridge | Multi-bot conversation chance |
| `PlayerReplies.MaxResponders` | 3 | Bridge | Reply participant cap |
| `PlayerReplies.PlayerNameChance` | 35 | Bridge | Player-name address chance |
| `PlayerReplies.CallbackChance` | 25 | Bridge | Earlier-session callback chance |
| `PlayerReplies.FollowupQuestionChance` | 20 | Bridge | Natural question chance |
| `PlayerReplies.RecentSpeakerPenalty` | 60 | Bridge | Recent-speaker weight reduction |
| `PlayerReplies.FirstDelayMin` | 8 | Bridge | Minimum first reply delay |
| `PlayerReplies.FirstDelayMax` | 20 | Bridge | Maximum first reply delay |
| `SessionMemory.Enable` | 1 | Bridge | Include and compact session memory |
| `SessionMemory.SummaryThresholdChars` | 3500 | Bridge | Compaction threshold |
| `SessionMemory.SummaryMaxInputChars` | 8000 | Bridge | Per-call transcript input cap |
| `SessionMemory.KeepRecentMessages` | 15 | Bridge | Latest visible Guild lines for player-reply prompts |
| `SessionMemory.SummaryMaxTokens` | 300 | Bridge | Summary output token budget |
| `SessionMemory.SummaryMaxChars` | 1200 | Bridge | Stored summary hard limit |

Existing installations must apply
`data/sql/characters/updates/20260724_guild_player_sessions.sql`.

---

## 13t. Guild Login Greetings

When Guild chatter and `GuildChatter.LoginGreeting.Enable` are enabled,
a real guild member's full character login schedules one greeting
attempt. `LLMChatterGuild.cpp` uses the existing
`PLAYERHOOK_ON_LOGIN`; AzerothCore and `mod-playerbots` remain
unmodified read-only dependencies.

### Deferred bot readiness

The login hook does not immediately select a speaker. Playerbots may
finish loading asynchronously, and supported server configurations may
wait 30 seconds after the first real player arrives before starting
random bots.

The Guild subsystem stores a small pending record containing the player
GUID, Guild ID, session ID, initial delay, and expiry. Once per second
the world coordinator calls the Guild-owned update function. At the due
time it reuses the normal live Guild-bot checks:

- in world
- alive
- not in combat
- current WorldSession
- no longer loading
- same Guild as the player

If no candidate is ready, the check repeats after
`LoginGreeting.RetryInterval` until
`LoginGreeting.ReadinessTimeout`. The attempt then expires silently;
it never surprises the player with a very late greeting.

### Initial timing

The first greeting uses weighted delay bands:

| Band | Default weight | Delay |
|------|---------------:|------:|
| Quick | 20% | 2-5 seconds |
| Ordinary | 55% | 8-20 seconds |
| Busy | 25% | 25-45 seconds |

`QuickChance` and `BusyChance` are configurable. The ordinary weight is
the remaining percentage. The server clamps BusyChance so the two
configured weights cannot exceed 100.

This pending timer is the first message's human-response delay. The
bridge inserts the first generated greeting with no second artificial
delay; normal LLM latency may still make it appear slightly later.
Additional greeters use the shared reading, typing, and distraction
pacing.

### Responder and prompt behavior

One high-priority `guild_login_greeting` event carries the current
session, target player, delay band, and shuffled live candidates.
`chatter_guild_login.py` normally selects one responder. On a
`LoginGreeting.MultiReplyChance` success, it selects two or three,
bounded by `LoginGreeting.MaxResponders` and available candidates.

One LLM request generates the complete greeting sequence. Prompts:

- ask for one distinct 3-12-word greeting per selected bot
- forbid bot-to-bot conversation
- forbid invented absence length, destination, or player intent
- retain the normal Guild cross-zone and RP-only rules
- allow only the primary greeter to be asked to use the player's name
- enforce `LoginGreeting.MaxCharacters` after cleanup

Malformed multi-message JSON receives one repair attempt and then falls
back to a single greeter. The same configured provider and model used by
all chatter is used for greetings.

### Cancellation and session safety

A greeting belongs to the fresh login session at `turn_id=0`. It is
cancelled when:

- the player speaks in Guild Chat before it arrives
- the player logs out or fully logs in again
- the player changes Guild
- its Guild session or turn becomes stale
- the module, Guild chatter, or login greeting toggle is disabled
- no Guild bot becomes ready before timeout

The bridge checks the session and turn before the LLM call and again
before inserting messages. Successful delivery records each greeting as
`source_kind='reply'`, so later player-driven Guild prompts can see what
the guildmates said during this login session.

A fast network reconnect to a character that never left the world does
not fire AzerothCore's full login hook and therefore does not create a
duplicate greeting.

### Login-greeting configuration

| Key | Default | Owner | Purpose |
|-----|---------|-------|---------|
| `LoginGreeting.Enable` | 1 | Server/Bridge | Login greeting toggle |
| `LoginGreeting.Chance` | 100 | Server | Chance to schedule an attempt |
| `LoginGreeting.QuickChance` | 20 | Server | Weight of 2-5s delay |
| `LoginGreeting.BusyChance` | 25 | Server | Weight of 25-45s delay |
| `LoginGreeting.RetryInterval` | 5 | Server | Bot readiness retry |
| `LoginGreeting.ReadinessTimeout` | 90 | Server | Total bounded wait |
| `LoginGreeting.MaxCandidates` | 12 | Server | Live candidate cap |
| `LoginGreeting.MultiReplyChance` | 20 | Bridge | Multiple-greeter chance |
| `LoginGreeting.MaxResponders` | 3 | Bridge | Greeter cap |
| `LoginGreeting.PlayerNameChance` | 60 | Bridge | Primary name chance |
| `LoginGreeting.MaxCharacters` | 100 | Bridge | Per-greeting hard cap |

Existing installations must also apply
`data/sql/characters/updates/20260725_guild_login_greeting.sql` because
`llm_chatter_events.event_type` is an SQL enum.

---

## 14. JSON and Queue Contracts

### `QueueChatterEvent()`

Shared C++ insert helper:

- implemented in `LLMChatterShared.cpp`
- declared in `LLMChatterShared.h`

Critical rule:

- direct callers must pass JSON text that is already SQL-safe

Wrappers like world-private `QueueEvent()` handle that escaping
internally.

The nearby-object direct world path now explicitly escapes `extraJson`
before calling `QueueChatterEvent()`.

### Statement response contract

Typical single-message JSON shape:

```json
{"message": "...", "emote": null, "action": null}
```

### Conversation response contract

Typical multi-message JSON shape:

```json
[
  {"speaker": "BotA", "message": "...", "emote": null, "action": null},
  {"speaker": "BotB", "message": "...", "emote": null, "action": null}
]
```

---

## 15. Database Tables

| Table | Producer | Consumer | Purpose |
|---|---|---|---|
| `llm_chatter_events` | C++ | Python | Event queue |
| `llm_chatter_queue` | C++ | Python | Ambient request queue |
| `llm_chatter_messages` | Python | C++ | Outbound delivery queue (includes `npc_spawn_id` for NPC speakers and `player_guid` for proximity scene tracking) |
| `llm_group_cached_responses` | Python | C++ | Pre-cached instant reactions |
| `llm_group_bot_traits` | Python + C++ travel refresh | Python | Group personality, location, live travel state, and `is_altbot` (player-owned bot flag; gates memory generation) |
| `llm_group_chat_history` | Python | Python | Group anti-repetition history |
| `llm_general_chat_history` | C++/Python read path | Python/C++ | General-channel history |
| `llm_bot_memories` | Python | Python | Per-bot-per-player memory journal (`active=1` persists). `importance_score` drives decay-aware retrieval, eviction and condensation candidacy, including the top-memory guard; `condensation_generation` caps digests-of-digests. No `memory_type` is exempt from deletion — `first_meeting` rows are immune only to the short-session discard, not to eviction/condensation/cap trim (see 13n, "Known gap") |
| `llm_bot_relationships` | Python | Python + C++ read | One running LLM-maintained relationship summary per bot–player pair, plus the `updated_through_created_at` timestamp watermark. Written by `_maybe_update_relationship()` under optimistic concurrency, rewound by condensation (see 13n); read by `.llmc memory` |
| `llm_group_vibe` | Python | Python | Persisted per-group ambient mood (`vibe`, `source_type`, `set_at`) so it survives bridge restarts and the session CLEANUP wipe; lazily expired on read, purged on group teardown (see 13n) |
| `llm_bot_identities` | Python | Python | Persistent bot personality traits; regenerated on IdentityVersion bump |

---

## 16. Important Editing Rules

### Separation of Concerns

New features or subsystems must go in their own file(s). Never dump
unrelated logic into an existing file. Shared utilities belong in the
dedicated shared layer (`LLMChatterShared.cpp/h` for C++,
`chatter_shared.py` / `chatter_constants.py` for Python). Each file
should have one clear ownership domain.

### `enabledHooks`

Any new or changed C++ hook override must add the correct enum to the
constructor's `enabledHooks` vector or it will silently never fire.

### C++ file routing

- `LLMChatterDelivery.cpp` for outbound delivery logic
- `LLMChatterAmbient.cpp` for ambient world/event logic
- `LLMChatterNearby.cpp` for nearby scan logic
- `LLMChatterProximity.cpp` for proximity chatter scan and scene logic
- `LLMChatterWorld.cpp` for world transport/dispatcher logic
- `LLMChatterGroup.cpp` for group shared helpers, cleanup, registration
- `LLMChatterGroupCombat.cpp` for group PlayerScript hooks and combat
  state
- `LLMChatterGroupJoin.cpp` for join batching and GroupScript
- `LLMChatterGroupEmote.cpp` for emote reaction system
- `LLMChatterGroupQuest.cpp` for quest accept batching and CreatureScript
- `LLMChatterProximity.cpp` for proximity chatter scan and scene logic
- `LLMChatterPlayer.cpp` for General-channel player logic
- `LLMChatterShared.cpp` for shared helper contracts
- `LLMChatterScript.cpp` is registration only — do not add features here

### Compile policy

Do not compile automatically.
Wait for explicit user approval before running build steps.

---

## 17. Known Gaps

- `memory_type = 'first_meeting'` rows are **not** exempt from
  `_evict_one_used()`, condensation candidate selection, or the
  orphan-recovery cap trim.
  Pre-existing gap surfaced by an upstream PR discussion; planned as a
  separate follow-up. See 13n, "Known gap: `first_meeting` rows are not
  protected from deletion"
- `.llmc memoryclean` is in-game only — the `.llmc` root is registered
  `Console::No`, so it cannot be run from the server console or over SOAP.
  The same cleanup runs unattended every 24 hours via
  `purge_orphaned_memories()`, so this is a convenience limitation, not a
  functional one
- Boss pull/kill/wipe events need live in-game testing via actual boss
  encounters
- Hostile multi-target spell-attribution edge case not fully covered
- Exhaustive in-game validation of every event path is ongoing

---

## 18. Related Docs

- `docs/mod-llm-chatter-architecture.md` — architecture reference,
  file map, dependency tree, data flow
