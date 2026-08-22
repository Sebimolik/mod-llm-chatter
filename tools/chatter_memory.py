"""Bot persistent memory system.

Owns:
- Session tracking (which bots are grouped with
  which player)
- Background memory generation via dedicated
  ThreadPoolExecutor
- Memory flush on farewell (activate or discard)
- Orphan recovery and session rehydration on
  bridge restart
- Memory retrieval for reunion greetings
"""

import logging
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

from chatter_db import (
    get_db_connection, get_group_location,
)
from chatter_shared import (
    get_zone_name, get_dungeon_flavor,
    format_location_label,
    build_bot_identity,
    estimate_tokens,
    get_gender_label,
    append_json_instruction,
)
from chatter_text import (
    _trim_summary,
    extract_json_object,
    parse_single_response,
)
from chatter_llm import call_llm, get_llm_client

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTS
# ============================================================

MEMORY_MOODS = {
    'ambient': [
        'curious', 'nostalgic', 'wistful',
        'playful', 'contemplative',
    ],
    'boss_kill': [
        'triumphant', 'exhilarated', 'proud',
        'breathless', 'relieved',
    ],
    'wipe': [
        'grimly_amused', 'humbled', 'resilient',
        'rueful', 'determined',
    ],
    'rare_kill': [
        'delighted', 'surprised', 'pleased',
        'excited', 'satisfied',
    ],
    'dungeon': [
        'adventurous', 'focused', 'alert',
        'eager', 'cautious',
    ],
    'party_member': [
        'warm', 'fond', 'grateful',
        'affectionate', 'respectful',
    ],
    'player_message': [
        'thoughtful', 'engaged', 'amused',
        'intrigued', 'moved',
    ],
    'quest_complete': [
        'proud', 'satisfied', 'relieved',
        'accomplished', 'glad',
    ],
    'achievement': [
        'proud', 'excited', 'delighted',
        'impressed', 'cheerful',
    ],
    'level_up': [
        'proud', 'elated', 'inspired',
        'jubilant', 'nostalgic',
    ],
    'bg_win': [
        'triumphant', 'exhilarated', 'proud',
        'victorious', 'gleeful',
    ],
    'bg_loss': [
        'rueful', 'determined', 'humbled',
        'stoic', 'resilient',
    ],
    'discovery': [
        'awed', 'curious', 'wistful',
        'adventurous', 'reflective',
    ],
    'pvp_kill': [
        'fierce', 'satisfied', 'exhilarated',
        'proud', 'ruthless',
    ],
    'gear_change': [
        'impressed', 'admiring', 'envious',
        'curious', 'approving',
    ],
    'mount_change': [
        'impressed', 'admiring', 'delighted',
        'curious', 'approving',
    ],
}

MEMORY_EXPRESSION_STYLES = [
    'poetic', 'understated', 'vivid',
    'wry', 'sincere',
]

# memory_type -> one-line prompt description, shared by
# _call_llm_for_memory() and
# _generate_shared_event_memory().
_MEMORY_TYPE_DESCRIPTIONS = {
    'ambient': "a quiet moment during travel",
    'boss_kill': "defeating a powerful enemy together",
    'wipe': "a total party wipe",
    'rare_kill': "finding and slaying a rare creature",
    'dungeon': "entering a dungeon or raid",
    'party_member': "adventuring alongside a companion",
    'player_message': "something the player said in chat",
    'quest_complete': "completing a quest together",
    'achievement': "earning an achievement",
    'level_up': "reaching a new level",
    'bg_win': "winning a battleground",
    'bg_loss': "losing a battleground",
    'discovery': "discovering a new area",
    'pvp_kill': "defeating an enemy player in combat",
    'gear_change': "noticing the player's new gear",
    'mount_change': "noticing the player's new mount",
}

# 1-10 importance rubric appended to every memory prompt
# so single-bot and shared-event memories are rated on
# an identical scale.
_IMPORTANCE_RUBRIC = (
    "Also rate how important/memorable this "
    "moment is on a 1-10 scale:\n"
    "- 1-3 (Ambient): casual chat, minor zone"
    " banter\n"
    "- 4-6 (Narrative): personal preferences"
    " stated by the player, minor achievements\n"
    "- 7-8 (Milestones): leveling milestones,"
    " acquiring rare gear, wipe encounters\n"
    "- 9-10 (Core Bonds): defeating raid bosses"
    " together, major narrative turning points\n\n"
)

DEFAULT_DECAY_MAX_IMPORTANCE = 3
DEFAULT_DECAY_DAYS = 30


def _effective_score_sql(config=None):
    """Build the decay-aware "effective importance" SQL
    expression used by get_bot_memories() and
    _evict_one_used().

    Memories at or below DecayMaxImportance lose about
    one point per DecayDays days, floored at 1; higher
    scores never decay. Both tunables are coerced to
    int before interpolation.
    """
    cfg = config or {}
    max_importance = int(cfg.get(
        'LLMChatter.Memory.DecayMaxImportance',
        DEFAULT_DECAY_MAX_IMPORTANCE,
    ))
    decay_days = int(cfg.get(
        'LLMChatter.Memory.DecayDays',
        DEFAULT_DECAY_DAYS,
    ))
    if decay_days < 1:
        decay_days = DEFAULT_DECAY_DAYS
    return (
        "CASE"
        " WHEN importance_score <= %d THEN"
        "   GREATEST(1, importance_score -"
        "     TIMESTAMPDIFF(DAY, created_at, NOW())"
        " / %d)"
        " ELSE importance_score"
        " END"
    ) % (max_importance, decay_days)


# ============================================================
# BACKGROUND EXECUTOR
# ============================================================

memory_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="memory",
)

# Separate, lower-priority pool for relationship-summary
# condensation (see _maybe_update_relationship() below).
# Kept independent from memory_executor so this lower-
# frequency background work never competes with the
# latency-sensitive memory-generation jobs sharing that pool.
relationship_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="relationship",
)

# Input char budget for one relationship-condensation LLM
# call, mirroring the guild summarizer's SummaryMaxInputChars
# concept. Not itself a runtime config key (see
# LLMChatter.Memory.Relationship.* in the conf.dist for the
# tunables that are).
_RELATIONSHIP_MAX_INPUT_CHARS = 6000

# ============================================================
# THREAD-SAFE SESSION TRACKER
# ============================================================

_active_sessions: Dict[int, dict] = {}
# {group_id: {
#   "start": float,        # set ONCE at first bot
#   "player_guid": int,
#   "bots": set(),         # bot_guids still in session
#   "members": dict,       # {guid: {name, class, race, gender}}
#   "msg_count": int,      # player_message count
#   "party_memories_generated": bool,
# }}

_group_locks: Dict[int, threading.Lock] = {}
_group_locks_meta = threading.Lock()

def _get_group_lock(
    group_id: int, create: bool = True
) -> Optional[threading.Lock]:
    """Get the per-group lock for session operations.

    create=False returns None if no lock exists
    (session already cleaned up).
    """
    with _group_locks_meta:
        if group_id not in _group_locks:
            if not create:
                return None
            _group_locks[group_id] = threading.Lock()
        return _group_locks[group_id]


# ============================================================
# SESSION MANAGEMENT
# ============================================================

def start_session(
    group_id, bot_guid, player_guid,
    session_start, members,
):
    """Register a bot in the active session tracker.

    First bot in a group initializes the session with
    session_start timestamp. Late joiners inherit the
    existing clock.

    Args:
        group_id: group identifier
        bot_guid: bot character guid
        player_guid: real player guid
        session_start: time.time() float
        members: dict {guid: {name, class, race}}
    """
    lock = _get_group_lock(group_id)
    with lock:
        if group_id not in _active_sessions:
            _active_sessions[group_id] = {
                "start": session_start,
                "player_guid": player_guid,
                "bots": set(),
                "members": members or {},
                "msg_count": 0,
                "party_memories_generated": False,
            }
        _active_sessions[group_id]["bots"].add(
            bot_guid
        )
        # Merge new member data for late joiners
        if members:
            _active_sessions[group_id][
                "members"
            ].update(members)


def get_session_mood(group_id, config=None):
    """Return the group's current ambient mood, or None.

    Lazy decay, no background timer: a mood is written by
    _ensure_cap_and_insert() whenever a new memory's
    importance_score crosses
    LLMChatter.GroupChatter.MoodImportanceThreshold, and
    simply "expires" once
    LLMChatter.GroupChatter.MoodDurationSeconds have
    elapsed -- callers just re-check the timestamp on
    every read instead of anything clearing it eagerly.

    Used by idle-chatter tone selection to bias toward a
    recent emotionally-significant memory instead of a
    fully random tone roll.
    """
    session = _active_sessions.get(group_id)
    if not session:
        return None
    mood = session.get("mood")
    mood_set_at = session.get("mood_set_at")
    if not mood or not mood_set_at:
        return None
    duration = float((config or {}).get(
        'LLMChatter.GroupChatter'
        '.MoodDurationSeconds', 600,
    ))
    if time.time() - mood_set_at >= duration:
        return None
    # MEMORY_MOODS entries are occasionally
    # snake_case (e.g. "grimly_amused") -- normalize
    # to a plain phrase for use as a prompt tone.
    return mood.replace('_', ' ')


def teardown_group_session(group_id):
    """Clear in-memory session state for one group.

    Called by cleanup_stale_groups() when a single
    group's player goes offline while others remain.
    """
    with _group_locks_meta:
        _active_sessions.pop(group_id, None)
        _group_locks.pop(group_id, None)


def clear_all_sessions():
    """Clear all in-memory session state.

    Called when all players go offline to prevent
    stale _active_sessions from surviving a full
    DB wipe.
    """
    with _group_locks_meta:
        _active_sessions.clear()
        _group_locks.clear()


# ============================================================
# QUEUE MEMORY
# ============================================================

def _is_altbot(db, group_id, bot_guid):
    """Check whether a bot is player-owned (alt) vs.
    a random/ownerless bot.

    A missing connection, a missing row, and DB errors
    all fail open (treated as altbot) to match the
    is_altbot column's DEFAULT 1.

    Args:
        db: live DB connection (None = fail open)
        group_id: group identifier
        bot_guid: bot character guid
    """
    if db is None:
        return True
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT is_altbot FROM"
            " llm_group_bot_traits"
            " WHERE group_id = %s"
            "   AND bot_guid = %s",
            (group_id, bot_guid),
        )
        row = cursor.fetchone()
        if row is None:
            return True
        return bool(row[0])
    except Exception:
        logger.error(
            "is_altbot lookup failed for "
            "bot=%s group=%s",
            bot_guid, group_id,
            exc_info=True,
        )
        return True


def queue_memory(
    config, group_id, bot_guid, player_guid,
    memory_type, event_context,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="", player_gender="",
    db=None,
):
    """Validate eligibility and submit a memory
    generation task to the background executor.

    Location is resolved here at queue time from
    get_group_location() while traits still exist.

    Args:
        config: bridge config dict
        group_id: group identifier
        bot_guid: bot character guid
        player_guid: real player guid
        memory_type: one of MEMORY_MOODS keys
        event_context: brief description of the
            moment for the LLM prompt
        bot_name: bot's character name
        bot_class: bot's class name
        bot_race: bot's race name
        db: optional DB connection reused for the
            altbot/player/location lookups; one
            connection is opened here if omitted
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return
    with lock:
        session = _active_sessions.get(group_id)
        if not session:
            return
        if bot_guid not in session["bots"]:
            return
        session_start = session["start"]
        p_guid = session["player_guid"]

    own_db = False
    if db is None:
        try:
            db = get_db_connection(config)
            own_db = True
        except Exception:
            logger.error(
                "queue_memory could not open a DB "
                "connection for group %s", group_id,
                exc_info=True,
            )
            db = None
    try:
        # Persistent memories are only generated for
        # player-owned (alt) bots, never for random/
        # ownerless bots.
        if not _is_altbot(db, group_id, bot_guid):
            logger.debug(
                "skipping memory generation for "
                "non-altbot bot_guid=%s group=%s",
                bot_guid, group_id,
            )
            return

        # Use the session's player_guid if caller
        # didn't provide one
        if not player_guid:
            player_guid = p_guid

        # Last resort: resolve from group members
        # (handles bridge restart mid-session where
        # session player_guid was lost)
        if not player_guid:
            try:
                from chatter_db import (
                    get_real_player_guid_for_group,
                )
                player_guid = (
                    get_real_player_guid_for_group(
                        db, group_id
                    )
                )
            except Exception:
                logger.error(
                    "player_guid DB fallback failed "
                    "for group %s", group_id,
                    exc_info=True,
                )

        if not player_guid:
            return  # can't create orphaned memory

        # Resolve location NOW while traits exist
        location, zone_id = _resolve_location(
            db, config, group_id
        )
    finally:
        if own_db and db:
            try:
                db.close()
            except Exception:
                pass

    memory_executor.submit(
        _execute_generate_memory,
        config=config,
        group_id=group_id,
        bot_guid=bot_guid,
        player_guid=player_guid,
        memory_type=memory_type,
        event_context=event_context,
        bot_name=bot_name,
        bot_class=bot_class,
        bot_race=bot_race,
        bot_gender=bot_gender,
        player_name=player_name,
        player_gender=player_gender,
        location=location,
        zone_id=zone_id,
        session_start=session_start,
        insert_active=False,
    )


def queue_shared_event_memory(
    config, group_id, memory_type, event_context,
    bot_guids, db=None,
):
    """Submit ONE memory-generation task whose result is
    inserted for every eligible bot in bot_guids.

    Used for party-wide events (boss/rare kills, wipes):
    a single LLM call replaces one call per bot. Callers
    are responsible for passing altbots only.

    Args:
        config: bridge config dict
        group_id: group identifier
        memory_type: one of MEMORY_MOODS keys
        event_context: brief description of the moment
        bot_guids: iterable of altbot character guids
        db: optional DB connection for the player_guid
            fallback lookup
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return
    with lock:
        session = _active_sessions.get(group_id)
        if not session:
            return
        eligible_bots = (
            set(bot_guids) & session["bots"]
        )
        if not eligible_bots:
            return
        session_start = session["start"]
        player_guid = session["player_guid"]

    # Last resort: resolve from group members
    # (handles bridge restart mid-session where
    # session player_guid was lost)
    if not player_guid:
        try:
            from chatter_db import (
                get_real_player_guid_for_group,
            )
            player_guid = (
                get_real_player_guid_for_group(
                    db, group_id
                )
            )
        except Exception:
            logger.error(
                "player_guid DB fallback failed "
                "for group %s", group_id,
                exc_info=True,
            )

    if not player_guid:
        return  # can't create orphaned memory

    memory_executor.submit(
        _execute_shared_event_memory,
        config=config,
        group_id=group_id,
        bot_guids=eligible_bots,
        player_guid=player_guid,
        memory_type=memory_type,
        event_context=event_context,
        session_start=session_start,
    )


# ============================================================
# MEMORY GENERATION (runs in background thread)
# ============================================================

def _resolve_location(db, config, group_id):
    """Resolve player-centric location label and its
    numeric zone_id.

    Uses get_group_location() for zone/area/map,
    then get_dungeon_flavor() for instances or
    format_location_label() for open world.

    Must be called while llm_group_bot_traits
    rows still exist (before group cleanup).

    Args:
        db: DB connection (None = open one)
        config: bridge config dict
        group_id: group identifier

    Returns (location_str, zone_id) where location_str
    is a human-readable string like "Teldrassil >
    Dolanaar" or "The Deadmines" (empty string on
    failure), and zone_id is the numeric zone id or
    None if unknown.
    """
    own_db = False
    try:
        if db is None:
            db = get_db_connection(config)
            own_db = True
        z, a, m = get_group_location(db, group_id)
        zone_id = z or None
        if not z and not m:
            return "", zone_id
        # Dungeons/raids: use flavour name
        df = get_dungeon_flavor(m)
        if df:
            return df.split(':')[0], zone_id
        # Open world: "Zone > Subzone" or "Zone"
        if z:
            return format_location_label(z, a), zone_id
        return "", zone_id
    except Exception:
        return "", None
    finally:
        if own_db and db:
            try:
                db.close()
            except Exception:
                pass


def _count_active_memories(cursor, bot_guid, player_guid):
    """Count active memories for a bot-player pair."""
    cursor.execute(
        "SELECT COUNT(*) FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1",
        (bot_guid, player_guid),
    )
    row = cursor.fetchone()
    return row[0] if row else 0


def _evict_one_used(
    cursor, conn, bot_guid, player_guid, config=None,
):
    """Evict the least valuable memory to make room.

    Prefers the lowest decay-aware effective_score
    used=1 row (created_at ASC breaks ties), falling
    back to the lowest-scoring row regardless of used
    status so a pool full of unread memories can't
    deadlock the cap. Both queries exclude the pair's
    single highest-scoring row, so a bot never forgets
    its most valuable memory about a player.

    Returns True if a row was deleted.
    """
    score_sql = _effective_score_sql(config)
    top_row_subquery = (
        " AND id != (SELECT id FROM ("
        "SELECT id FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1"
        " ORDER BY "
        + score_sql +
        " DESC, created_at DESC"
        " LIMIT 1"
        ") t)"
    )
    cursor.execute(
        "DELETE FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1"
        "   AND used = 1"
        + top_row_subquery +
        " ORDER BY "
        + score_sql +
        " ASC, created_at ASC"
        " LIMIT 1",
        (
            bot_guid, player_guid,
            bot_guid, player_guid,
        ),
    )
    conn.commit()
    if cursor.rowcount > 0:
        return True

    # Fallback: no used=1 row was eligible. Pool is
    # under generation pressure (filling up with
    # memories that haven't been recalled yet) --
    # evict the lowest-value row regardless of used
    # status so the cap never gets permanently stuck.
    cursor.execute(
        "DELETE FROM llm_bot_memories"
        " WHERE bot_guid = %s"
        "   AND player_guid = %s"
        "   AND active = 1"
        + top_row_subquery +
        " ORDER BY "
        + score_sql +
        " ASC, created_at ASC"
        " LIMIT 1",
        (
            bot_guid, player_guid,
            bot_guid, player_guid,
        ),
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.info(
            "Memory pool for bot=%s player=%s had no"
            " used=1 rows to evict (generation"
            " pressure: pool filled with unread"
            " memories); evicted lowest-value unused"
            " row instead",
            bot_guid, player_guid,
        )
        return True
    return False


def _ensure_cap_and_insert(
    conn, config, bot_guid, player_guid, group_id,
    memory_type, memory_text, mood, emote,
    session_start, active, max_per,
    importance=5, zone_id=None,
):
    """Check memory cap, evict if needed, insert.

    Returns True if inserted, False if pool full.
    """
    cursor = conn.cursor()
    cnt = _count_active_memories(
        cursor, bot_guid, player_guid
    )
    if cnt >= max_per:
        if not _evict_one_used(
            cursor, conn, bot_guid, player_guid,
            config,
        ):
            logger.debug(
                "Memory pool full, no used"
                " memories to evict for"
                " bot %s", bot_guid,
            )
            return False
    cursor.execute(
        "INSERT INTO llm_bot_memories"
        " (bot_guid, player_guid,"
        "  group_id, memory_type,"
        "  memory, mood, emote,"
        "  active, session_start,"
        "  importance_score, zone_id)"
        " VALUES"
        " (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            bot_guid, player_guid,
            group_id, memory_type,
            memory_text, mood, emote,
            active, session_start,
            importance, zone_id,
        ),
    )
    conn.commit()

    # Session mood: a sufficiently important memory
    # colors the group's ambient idle-chatter tone for
    # a while (see get_session_mood() for the lazy-decay
    # read side). Skip silently if the session already
    # ended -- never create an entry just for this.
    # Not lock-protected: this call can already run
    # inside the per-group lock (see
    # _execute_generate_memory's insert_active=False
    # path re-check block), and that lock is a plain
    # (non-reentrant) threading.Lock, so acquiring it
    # again here would deadlock. A couple of plain field
    # writes are safe enough for a best-effort mood cue.
    mood_threshold = int(config.get(
        'LLMChatter.GroupChatter'
        '.MoodImportanceThreshold', 7,
    ))
    if importance >= mood_threshold:
        session = _active_sessions.get(group_id)
        if session is not None:
            session["mood"] = mood
            session["mood_set_at"] = time.time()

    return True


def insert_first_meeting_memory(
    db, config, bot_guid, player_guid, group_id,
    memory_text, importance=5,
):
    """Insert a bot's first-meeting memory of a player.

    Routes through _ensure_cap_and_insert() so this memory
    type gets the same cap/eviction guarantees as every
    other memory type instead of bypassing them with a raw
    INSERT. Preserves the original hand-rolled behavior:
    mood is always 'warm', there's no emote, the memory is
    active immediately (active=1, not the pending-until-
    farewell active=0 lifecycle other event types use), and
    a duplicate is never created for a bot/player pair that
    already has a first_meeting memory.

    Returns True if inserted, False if a first_meeting
    memory already existed or the cap was full with nothing
    evictable.
    """
    cursor = db.cursor()
    cursor.execute(
        "SELECT 1 FROM llm_bot_memories"
        " WHERE bot_guid = %s AND player_guid = %s"
        "   AND memory_type = 'first_meeting'"
        " LIMIT 1",
        (bot_guid, player_guid),
    )
    already_exists = cursor.fetchone() is not None
    cursor.close()
    if already_exists:
        return False

    max_per = int(config.get(
        'LLMChatter.Memory.MaxPerBotPlayer', 30
    ))
    return _ensure_cap_and_insert(
        db, config, bot_guid, player_guid, group_id,
        'first_meeting', memory_text, 'warm', None,
        time.time(), active=1, max_per=max_per,
        importance=importance,
    )


def _execute_generate_memory(
    config, group_id, bot_guid, player_guid,
    memory_type, event_context,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="", player_gender="",
    location="",
    zone_id=None,
    session_start=0.0, insert_active=False,
):
    """Generate a memory via LLM and insert it.

    For insert_active=False (normal path):
      - Fast bailout before LLM call
      - Re-check + INSERT under per-group lock
    For insert_active=True (party_member at flush):
      - INSERT as active=1, then self-prune
    """
    # Fast bailout before expensive LLM call
    if not insert_active:
        lock = _get_group_lock(
            group_id, create=False
        )
        if lock is None:
            return
        with lock:
            session = _active_sessions.get(group_id)
            if (
                session is None
                or session["start"] != session_start
                or bot_guid not in session["bots"]
            ):
                return

    conn = None
    try:
        conn = get_db_connection(config)

        # Resolve player_name/player_gender from DB
        # if not provided but player_guid is known
        if (
            (not player_name or not player_gender)
            and player_guid
        ):
            try:
                pc = conn.cursor(dictionary=True)
                pc.execute(
                    "SELECT name, gender FROM"
                    " characters WHERE guid = %s",
                    (player_guid,),
                )
                pr = pc.fetchone()
                if pr:
                    if not player_name:
                        player_name = pr['name']
                    if (
                        not player_gender
                        and pr.get('gender') is not None
                    ):
                        player_gender = (
                            get_gender_label(
                                pr['gender']
                            )
                        )
                pc.close()
            except Exception:
                logger.debug(
                    "Could not resolve player_name/"
                    "player_gender for guid=%s",
                    player_guid,
                )

        # Pick mood and expression style
        moods = MEMORY_MOODS.get(
            memory_type,
            ['contemplative'],
        )
        mood = random.choice(moods)
        style = random.choice(
            MEMORY_EXPRESSION_STYLES
        )

        # Generate via LLM
        memory_text, emote, importance = (
            _call_llm_for_memory(
                config,
                bot_name=bot_name,
                bot_class=bot_class,
                bot_race=bot_race,
                bot_gender=bot_gender,
                player_name=player_name,
                player_gender=player_gender,
                memory_type=memory_type,
                event_context=event_context,
                mood=mood,
                style=style,
                location=location,
            )
        )

        if not memory_text:
            return

        max_per = int(config.get(
            'LLMChatter.Memory.MaxPerBotPlayer', 30
        ))

        if not insert_active:
            # Re-check session under per-group lock
            lock = _get_group_lock(
                group_id, create=False
            )
            if lock is None:
                return
            with lock:
                session = _active_sessions.get(
                    group_id
                )
                if (
                    session is None
                    or session["start"]
                        != session_start
                    or bot_guid
                        not in session["bots"]
                ):
                    return
                _ensure_cap_and_insert(
                    conn, config, bot_guid,
                    player_guid, group_id,
                    memory_type,
                    memory_text, mood, emote,
                    session_start, active=0,
                    max_per=max_per,
                    importance=importance,
                    zone_id=zone_id,
                )
        else:
            _ensure_cap_and_insert(
                conn, config, bot_guid,
                player_guid, group_id,
                memory_type,
                memory_text, mood, emote,
                session_start, active=1,
                max_per=max_per,
                importance=importance,
                zone_id=zone_id,
            )

    except Exception:
        logger.error(
            "Memory generation failed for "
            f"bot={bot_guid} group={group_id} "
            f"type={memory_type}",
            exc_info=True,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _execute_shared_event_memory(
    config, group_id, bot_guids, player_guid,
    memory_type, event_context, session_start=0.0,
):
    """Generate ONE shared memory via a single LLM call
    and insert it for every bot in bot_guids.

    Rows are inserted as active=0 (pending); they go
    active at farewell (flush_session_memories()) or via
    orphan recovery, like any other in-session memory.
    """
    # Fast bailout before expensive LLM call
    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return
    with lock:
        session = _active_sessions.get(group_id)
        if (
            session is None
            or session["start"] != session_start
        ):
            return
        live_bots = set(bot_guids) & session["bots"]
    if not live_bots:
        return

    conn = None
    try:
        conn = get_db_connection(config)

        # Resolve current zone for zone-aware recall
        # tie-breaking (see get_bot_memories()).
        try:
            zone_id, _a, _m = get_group_location(
                conn, group_id
            )
            zone_id = zone_id or None
        except Exception:
            zone_id = None

        moods = MEMORY_MOODS.get(
            memory_type, ['contemplative'],
        )
        mood = random.choice(moods)

        memory_texts, emote, importance = (
            _generate_shared_event_memory(
                config, memory_type, event_context,
                mood_hint=mood,
            )
        )

        if not memory_texts:
            return

        max_per = int(config.get(
            'LLMChatter.Memory.MaxPerBotPlayer', 30
        ))

        # Re-check session under per-group lock; bots
        # that left mid-LLM-call are dropped from the
        # insert.
        lock = _get_group_lock(
            group_id, create=False
        )
        if lock is None:
            return
        with lock:
            session = _active_sessions.get(group_id)
            if (
                session is None
                or session["start"]
                    != session_start
            ):
                return
            live_bots = (
                set(bot_guids) & session["bots"]
            )
            if not live_bots:
                return
            inserted_count = 0
            # Cycle through the available phrasings so
            # bots present for the same event don't all
            # get byte-identical stored text -- the
            # underlying facts/importance stay shared,
            # only the wording varies.
            for i, bot_guid in enumerate(live_bots):
                bot_memory_text = memory_texts[
                    i % len(memory_texts)
                ]
                if _ensure_cap_and_insert(
                    conn, config, bot_guid,
                    player_guid, group_id,
                    memory_type, bot_memory_text,
                    mood, emote, session_start,
                    active=0, max_per=max_per,
                    importance=importance,
                    zone_id=zone_id,
                ):
                    inserted_count += 1

            logger.info(
                "Shared memory generated for group=%s "
                "type=%s: %d altbot(s)",
                group_id, memory_type, inserted_count,
            )

    except Exception:
        logger.error(
            "Shared memory generation failed for "
            f"group={group_id} type={memory_type}",
            exc_info=True,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def _coerce_importance(raw_importance, default=5):
    """Coerce a parsed `importance` field to an int in
    [1, 10].

    Always returns a usable value: `default` when the
    field is missing, non-numeric, or wildly out of
    range (a garbled misparse), otherwise clamped into
    [1, 10].
    """
    try:
        importance = int(raw_importance)
    except (TypeError, ValueError):
        return default
    if importance < -1000 or importance > 1000:
        return default
    return max(1, min(10, importance))


def _call_llm_for_memory(
    config,
    bot_name="", bot_class="", bot_race="",
    bot_gender="",
    player_name="", player_gender="",
    memory_type="ambient", event_context="",
    mood="contemplative", style="sincere",
    location="",
):
    """Call LLM to generate a memory entry.

    Returns (memory_text, emote, importance) or
    (None, None, None) on failure. Only `memory` is
    required; a missing/malformed `importance` or
    `emote` falls back to a default instead of failing.
    """
    client = get_llm_client(config)

    type_desc = _MEMORY_TYPE_DESCRIPTIONS.get(
        memory_type, "a shared moment"
    )

    prompt = (
        f"{build_bot_identity(bot_name, bot_race, bot_class, bot_gender)} "
        f"in World of Warcraft.\n"
    )
    if player_name:
        prompt += (
            f"Player companion: {player_name}"
            + (
                f" (gender: {player_gender})"
                if player_gender else ""
            )
            + "\n"
        )
    if location:
        prompt += f"Location: {location}\n"
    prompt += (
        f"\nContext: {type_desc}\n"
    )
    if event_context:
        prompt += f"What happened: {event_context}\n"
    prompt += (
        f"Mood: {mood}\n"
        f"Expression style: {style}\n\n"
        f"Write a 1-2 sentence first-person memory "
        f"from your perspective about this moment. "
        f"This is a private journal entry, not "
        f"spoken aloud. Be specific about what "
        f"happened.\n\n"
        + _IMPORTANCE_RUBRIC +
        f"Respond in JSON:\n"
        f'{{"memory": "your memory text", '
        f'"emote": "one_word_emote", '
        f'"importance": 5}}\n\n'
        f"Rules:\n"
        f"- Memory must be 1-2 sentences\n"
        f"- First person perspective\n"
        f"- No quotes inside the memory text\n"
        f"- Only reference the location given above"
        f" — never invent or guess a location\n"
        f"- Emote is optional (null if none)\n"
        f"- Importance is an integer from 1 to 10"
        f" using the rubric above\n"
    )
    if player_name:
        prompt += (
            f"- When the memory involves the player,"
            f" refer to them by name"
            f" ({player_name}) — never use generic"
            f" terms like 'a traveler' or 'someone'"
            f" or 'a stranger'\n"
        )
    if player_name and player_gender:
        prompt += (
            f"- The player ({player_name}) is"
            f" grammatically {player_gender} — use"
            f" correct gender agreement for any"
            f" pronouns and past-tense verbs"
            f" referring to them\n"
        )
    prompt += (
        f"- Just the JSON, nothing else"
    )

    # Plain-string prompt path — not routed through
    # append_json_instruction, so inject the language
    # rule and lore guardrail directly.
    from chatter_shared import (
        get_language_rule, get_lore_guardrail_rule,
    )
    lang_rule = get_language_rule()
    if lang_rule:
        prompt += lang_rule
    lore_rule = get_lore_guardrail_rule()
    if lore_rule:
        prompt += lore_rule

    try:
        response = call_llm(
            client, prompt, config,
            max_tokens_override=120,
            context=f"memory:{bot_name}:{memory_type}",
            label='memory_generation',
        )
        if not response:
            return None, None, None

        # Robust JSON extraction (markdown-fence
        # stripping + regex-matched embedded {...}
        # fallback), keyed on 'memory'.
        data = extract_json_object(
            response, required_key='memory'
        )
        if data is None:
            return None, None, None

        memory = data.get('memory', '')
        if isinstance(memory, str):
            memory = memory.strip()
        else:
            return None, None, None

        if not memory or len(memory) > 500:
            return None, None, None

        emote = data.get('emote')
        if isinstance(emote, str):
            emote = emote.strip()[:32] or None
        else:
            emote = None

        importance = _coerce_importance(
            data.get('importance')
        )

        return memory, emote, importance

    except Exception:
        logger.error(
            f"LLM memory call failed for "
            f"{bot_name}:{memory_type}",
            exc_info=True,
        )
        return None, None, None


def _generate_shared_event_memory(
    config, memory_type, event_context,
    mood_hint=None,
):
    """Call the LLM ONCE to generate a shared memory
    for a party-wide event (boss/rare kill, wipe).

    Bot-identity-agnostic on purpose: the prompt asks
    for a first-person-plural ("we"/"our party") memory
    with no per-bot substitution. To avoid storing
    byte-identical text for every present altbot, the
    same call also asks for a couple of alternate
    phrasings of the same event; callers cycle through
    the returned list per bot.

    Returns (memory_texts, emote, importance) where
    memory_texts is a non-empty list of equivalent
    phrasings (always at least [primary_text]), or
    (None, None, None) on failure.
    """
    client = get_llm_client(config)
    type_desc = _MEMORY_TYPE_DESCRIPTIONS.get(
        memory_type, "a shared moment"
    )
    mood = mood_hint or 'contemplative'

    prompt = (
        "A full party of adventurers in World of "
        "Warcraft just shared a moment together.\n"
        f"\nContext: {type_desc}\n"
    )
    if event_context:
        prompt += f"What happened: {event_context}\n"
    prompt += f"Mood: {mood}\n\n"
    prompt += (
        "Write a 1-2 sentence first-person-plural "
        "memory (\"we\"/\"our party\") from the "
        "group's shared perspective about this "
        "moment, suitable to be stored as a "
        "private journal entry for a party member. "
        "This is a private journal entry, not "
        "spoken aloud. Be specific about what "
        "happened.\n\n"
    )
    prompt += _IMPORTANCE_RUBRIC
    prompt += (
        "Respond in JSON:\n"
        '{"memory": "your memory text", '
        '"variations": ["alternate phrasing 1", '
        '"alternate phrasing 2"], '
        '"emote": "one_word_emote", '
        '"importance": 5}\n\n'
        "Rules:\n"
        "- Memory must be 1-2 sentences\n"
        "- First person plural (\"we\"/\"our\") "
        "perspective -- never use a single "
        "character's name or \"I\"\n"
        "- No quotes inside the memory text\n"
        "- 'variations' holds 2 alternate ways to "
        "phrase the SAME memory (same facts, "
        "different wording/opening), since this "
        "text will be stored as a separate journal "
        "entry for each of several party members "
        "and they should not all read identically\n"
        "- Emote is optional (null if none)\n"
        "- Importance is an integer from 1 to 10"
        " using the rubric above\n"
        "- Just the JSON, nothing else"
    )

    from chatter_shared import (
        get_language_rule, get_lore_guardrail_rule,
    )
    lang_rule = get_language_rule()
    if lang_rule:
        prompt += lang_rule
    lore_rule = get_lore_guardrail_rule()
    if lore_rule:
        prompt += lore_rule

    try:
        response = call_llm(
            client, prompt, config,
            max_tokens_override=260,
            context=f"shared-memory:{memory_type}",
            label='shared_memory_generation',
        )
        if not response:
            return None, None, None

        data = extract_json_object(
            response, required_key='memory'
        )
        if data is None:
            return None, None, None

        memory = data.get('memory', '')
        if isinstance(memory, str):
            memory = memory.strip()
        else:
            return None, None, None

        if not memory or len(memory) > 500:
            return None, None, None

        # Optional alternate phrasings of the same
        # memory, so identical stored text isn't handed
        # to every altbot present. Best-effort: any
        # malformed/oversized/empty entry is dropped,
        # and the primary `memory` text is always first
        # so callers with no valid variations still get
        # a working single-item list.
        memory_texts = [memory]
        raw_variations = data.get('variations')
        if isinstance(raw_variations, list):
            for variation in raw_variations:
                if (
                    isinstance(variation, str)
                    and variation.strip()
                    and len(variation) <= 500
                ):
                    memory_texts.append(
                        variation.strip()
                    )

        emote = data.get('emote')
        if isinstance(emote, str):
            emote = emote.strip()[:32] or None
        else:
            emote = None

        importance = _coerce_importance(
            data.get('importance')
        )

        return memory_texts, emote, importance

    except Exception:
        logger.error(
            "Shared LLM memory call failed for "
            f"type={memory_type}",
            exc_info=True,
        )
        return None, None, None


# ============================================================
# FLUSH SESSION MEMORIES
# ============================================================

def _filter_altbot_guids(db, group_id, bot_guids):
    """Filter a set of bot guids down to altbots only.

    flush_session_memories() submits party_member jobs
    straight to the executor, bypassing queue_memory()'s
    per-bot altbot guard, so it needs its own filter.
    DB errors fail open (keep the full set).
    """
    bot_guids = list(bot_guids)
    if not bot_guids:
        return set()
    try:
        cursor = db.cursor()
        placeholders = ','.join(
            ['%s'] * len(bot_guids)
        )
        cursor.execute(
            "SELECT bot_guid FROM"
            " llm_group_bot_traits"
            " WHERE group_id = %s"
            f"   AND bot_guid IN ({placeholders})"
            "   AND is_altbot = 1",
            [group_id] + bot_guids,
        )
        eligible = {
            row[0] for row in cursor.fetchall()
        }
        logger.debug(
            "Altbot filter: %d/%d bots eligible for "
            "group=%s",
            len(eligible), len(bot_guids), group_id,
        )
        return eligible
    except Exception:
        logger.error(
            "altbot filter failed for group=%s",
            group_id, exc_info=True,
        )
        return set(bot_guids)


def flush_session_memories(
    db, group_id, player_guid, bot_guid, config,
):
    """Flush memories for a departing bot.

    Called from process_group_farewell_event.

    Steps:
    1. Under lock: capture session_start, snapshot
       bots for party_member, discard bot
    2. If qualifying: submit party_member for all
    3. UPDATE active=1 for this bot's session rows
    4. Prune to MaxPerBotPlayer cap
    5. If too short: DELETE inactive rows
    6. If last bot: clean up session
    """
    if not int(config.get(
        'LLMChatter.Memory.Enable', 1
    )):
        return

    session_minutes = int(config.get(
        'LLMChatter.Memory.SessionMinutes', 15
    ))
    max_per = int(config.get(
        'LLMChatter.Memory.MaxPerBotPlayer', 30
    ))

    do_commit = False
    do_party = False
    all_bots_snapshot = set()
    members_snapshot = {}
    session_start = 0.0

    lock = _get_group_lock(group_id, create=False)
    if lock is None:
        return

    with lock:
        session = _active_sessions.get(group_id)
        if not session:
            return

        session_start = session["start"]
        elapsed = time.time() - session_start
        do_commit = (
            elapsed >= session_minutes * 60
        )
        do_party = (
            do_commit
            and not session[
                "party_memories_generated"
            ]
        )

        if do_party:
            session[
                "party_memories_generated"
            ] = True
            # Snapshot ALL bots including departing;
            # only generate party_member memories
            # when 2+ bots were present (solo bots
            # cannot reflect on inter-bot bonding)
            all_bots_snapshot = (
                session["bots"] | {bot_guid}
            )
            if len(all_bots_snapshot) < 2:
                do_party = False
                all_bots_snapshot = set()
            members_snapshot = dict(
                session["members"]
            )

        # Remove bot BEFORE UPDATE
        session["bots"].discard(bot_guid)
        last_bot = len(session["bots"]) == 0

        if last_bot:
            del _active_sessions[group_id]
            # Clean up per-group lock entry
            with _group_locks_meta:
                _group_locks.pop(group_id, None)

    if do_commit:
        # Submit party_member memories for all bots
        if do_party:
            party_chance = int(config.get(
                'LLMChatter.Memory'
                '.PartyMemberGenerationChance',
                50
            ))
            flush_loc, flush_zone_id = (
                _resolve_location(
                    db, config, group_id
                )
            )
            # Only player-owned (alt) bots get
            # party_member memories.
            altbot_snapshot = _filter_altbot_guids(
                db, group_id, all_bots_snapshot
            )
            for target_guid in altbot_snapshot:
                if (random.random() * 100
                        >= party_chance):
                    continue
                member = members_snapshot.get(
                    target_guid, {}
                )
                context = (
                    "Reflecting on time spent with "
                    "party companions"
                )
                memory_executor.submit(
                    _execute_generate_memory,
                    config=config,
                    group_id=group_id,
                    bot_guid=target_guid,
                    player_guid=player_guid,
                    memory_type="party_member",
                    event_context=context,
                    bot_name=member.get('name', ''),
                    bot_class=member.get('class', ''),
                    bot_race=member.get('race', ''),
                    bot_gender=member.get(
                        'gender', ''
                    ),
                    location=flush_loc,
                    zone_id=flush_zone_id,
                    session_start=session_start,
                    insert_active=True,
                )

        # Activate rows from THIS session
        try:
            cursor = db.cursor()
            cursor.execute(
                "UPDATE llm_bot_memories"
                " SET active = 1"
                " WHERE group_id = %s"
                "   AND bot_guid = %s"
                "   AND active = 0"
                "   AND session_start = %s",
                (group_id, bot_guid, session_start),
            )
            rows_activated = cursor.rowcount
            db.commit()
            if rows_activated > 0:
                logger.debug(
                    "Activated %d memories for"
                    " bot %s / player %s",
                    rows_activated, bot_guid,
                    player_guid,
                )
                _maybe_queue_relationship_update(
                    cursor, config, bot_guid,
                    player_guid,
                )

            # Prune to cap
            cnt = _count_active_memories(
                cursor, bot_guid, player_guid
            )
            while cnt > max_per:
                if not _evict_one_used(
                    cursor, db,
                    bot_guid, player_guid, config,
                ):
                    break  # no used left
                cnt -= 1
        except Exception:
            logger.error(
                "Memory activation failed for "
                f"bot={bot_guid} group={group_id}",
                exc_info=True,
            )
    else:
        # Session too short: discard inactive rows
        try:
            cursor = db.cursor()
            cursor.execute(
                "DELETE FROM llm_bot_memories"
                " WHERE group_id = %s"
                "   AND bot_guid = %s"
                "   AND active = 0"
                "   AND session_start = %s",
                (group_id, bot_guid, session_start),
            )
            rows_discarded = cursor.rowcount
            db.commit()
            if rows_discarded > 0:
                logger.debug(
                    "Discarded %d memories for"
                    " bot %s (session too short)",
                    rows_discarded, bot_guid,
                )
        except Exception:
            logger.error(
                "Memory discard failed for "
                f"bot={bot_guid} group={group_id}",
                exc_info=True,
            )


# ============================================================
# RELATIONSHIP TRACKING
# ============================================================
#
# A running, LLM-maintained per-(bot_guid, player_guid)
# description of how a bot generally FEELS about a specific
# player -- a standing disposition, distinct from the
# individual llm_bot_memories journal entries it is
# periodically condensed from. Updated in the background
# (relationship_executor) after a farewell activates enough
# new memories, mirroring the guild session summarizer's
# _maybe_summarize_session() pattern in
# chatter_guild_player.py.
# ============================================================

def _maybe_queue_relationship_update(
    cursor, config, bot_guid, player_guid,
):
    """Decide whether enough new active memories have
    accumulated since the last relationship update to
    warrant submitting a background condensation job.

    Called from flush_session_memories() right after that
    bot's session rows are activated, reusing its already-
    open cursor. Only decides WHETHER to submit; the actual
    job (_maybe_update_relationship()) opens its own DB
    connection and runs independently on
    relationship_executor, so this check can never block or
    slow down the farewell flow. Any failure here is caught
    and logged -- it must never disrupt the farewell it's
    piggybacking on.
    """
    if not int(config.get(
        'LLMChatter.Memory.Relationship.Enable', 1
    )):
        return
    try:
        cursor.execute(
            "SELECT updated_through_memory_id FROM"
            " llm_bot_relationships"
            " WHERE bot_guid = %s AND player_guid = %s",
            (bot_guid, player_guid),
        )
        row = cursor.fetchone()
        watermark = int(row[0]) if row else 0

        cursor.execute(
            "SELECT COUNT(*) FROM llm_bot_memories"
            " WHERE bot_guid = %s"
            "   AND player_guid = %s"
            "   AND active = 1"
            "   AND id > %s",
            (bot_guid, player_guid, watermark),
        )
        count_row = cursor.fetchone()
        new_count = count_row[0] if count_row else 0

        threshold = int(config.get(
            'LLMChatter.Memory.Relationship'
            '.UpdateThreshold', 5,
        ))
        if new_count < threshold:
            return

        relationship_executor.submit(
            _maybe_update_relationship,
            config, bot_guid, player_guid,
        )
    except Exception:
        logger.error(
            "Relationship update trigger check failed "
            f"for bot={bot_guid} player={player_guid}",
            exc_info=True,
        )


def _maybe_update_relationship(
    config, bot_guid, player_guid,
):
    """Condense new memories into the running relationship
    summary for a bot-player pair. Runs in
    relationship_executor (background thread) -- opens its
    own DB connection rather than reusing the caller's, since
    a single mysql-connector connection is not safe to share
    across threads.

    Fetches the current summary + watermark (absent row =
    first-time pair, watermark 0), pulls active memories
    newer than the watermark (capped by
    _RELATIONSHIP_MAX_INPUT_CHARS), asks the LLM to fold them
    into an updated summary, hard-truncates to
    LLMChatter.Memory.Relationship.MaxChars, and writes it
    back with the new watermark via INSERT ... ON DUPLICATE
    KEY UPDATE.

    On any failure (no new memories, LLM call failure, empty/
    unparseable response) does nothing and leaves the
    watermark untouched, so the next qualifying farewell
    retries -- mirroring the guild summarizer's fail-safe:
    never a partial/corrupt overwrite.
    """
    conn = None
    try:
        conn = get_db_connection(config)
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT summary, updated_through_memory_id"
            " FROM llm_bot_relationships"
            " WHERE bot_guid = %s AND player_guid = %s",
            (bot_guid, player_guid),
        )
        existing = cursor.fetchone()
        previous_summary = (
            str(existing.get('summary') or '').strip()
            if existing else ''
        )
        watermark = (
            int(existing.get(
                'updated_through_memory_id'
            ) or 0)
            if existing else 0
        )

        cursor.execute(
            "SELECT id, memory FROM llm_bot_memories"
            " WHERE bot_guid = %s"
            "   AND player_guid = %s"
            "   AND active = 1"
            "   AND id > %s"
            " ORDER BY id ASC",
            (bot_guid, player_guid, watermark),
        )
        rows = cursor.fetchall()
        if not rows:
            return  # nothing new to fold in

        candidates = []
        char_count = 0
        for row in rows:
            text = str(row.get('memory') or '')
            row_chars = len(text) + 3
            if (
                candidates
                and char_count + row_chars
                    > _RELATIONSHIP_MAX_INPUT_CHARS
            ):
                break
            candidates.append(row)
            char_count += row_chars
        if not candidates:
            return

        # Resolve display names for the prompt
        cursor.execute(
            "SELECT guid, name FROM characters"
            " WHERE guid IN (%s, %s)",
            (bot_guid, player_guid),
        )
        bot_name = ''
        player_name = ''
        for name_row in cursor.fetchall():
            guid = int(name_row.get('guid') or 0)
            if guid == bot_guid:
                bot_name = name_row.get('name') or ''
            elif guid == player_guid:
                player_name = (
                    name_row.get('name') or ''
                )
        if not bot_name or not player_name:
            logger.error(
                "Relationship update aborted: could "
                f"not resolve names for bot={bot_guid}"
                f" player={player_guid}",
            )
            return

        memory_list = '\n'.join(
            f"  - {sanitize_memory_for_prompt(row['memory'])}"
            for row in candidates
        )

        max_chars = int(config.get(
            'LLMChatter.Memory.Relationship.MaxChars',
            400,
        ))

        prompt = (
            f"Update a compact description of how "
            f"{bot_name} feels about {player_name}, "
            f"based on shared memories.\n"
            f"Preserve established sentiment, inside "
            f"jokes, notable moments, and any tension "
            f"or warmth. Discard one-off trivia. Never "
            f"invent feelings not supported by the "
            f"memories.\n"
            f"Hard limit: {max_chars} characters.\n\n"
            f"Previous relationship:\n"
            f"{previous_summary or '(just met)'}\n\n"
            f"New memories to fold in:\n"
            f"{memory_list}\n\n"
            f"Return the updated relationship "
            f"description."
        )
        # message_only=True routes through
        # append_json_instruction's own language-rule +
        # lore-guardrail injection (see chatter_shared.py),
        # matching _maybe_summarize_session()'s call in
        # chatter_guild_player.py exactly -- do not also
        # inject get_lore_guardrail_rule()/
        # get_language_rule() here, that would duplicate
        # both rules in the final prompt.
        prompt = append_json_instruction(
            prompt, allow_action=False,
            message_only=True,
        )

        client = get_llm_client(config)
        response = call_llm(
            client, prompt, config,
            max_tokens_override=int(config.get(
                'LLMChatter.Memory.Relationship'
                '.MaxTokens', 300,
            )),
            context=(
                f"relationship:{bot_guid}:"
                f"{player_guid}"
            ),
            label='relationship_update',
            metadata={
                'bot_guid': bot_guid,
                'player_guid': player_guid,
                'relationship_input_lines':
                    len(candidates),
                'relationship_input_chars': char_count,
            },
        )
        parsed = parse_single_response(response or '')

        # Reuse the exact same truncation helper as the
        # guild session summarizer rather than inventing a
        # third near-identical one (now shared via
        # chatter_text.py rather than a cross-domain import).
        summary = _trim_summary(
            parsed.get('message', ''), max_chars,
        )
        if not summary:
            logger.error(
                "Relationship update produced an empty/"
                f"unparseable summary for bot={bot_guid}"
                f" player={player_guid}; watermark left"
                " untouched for retry",
            )
            return

        new_watermark = candidates[-1]['id']
        write_cursor = conn.cursor()
        write_cursor.execute(
            "INSERT INTO llm_bot_relationships"
            " (bot_guid, player_guid, summary,"
            "  updated_through_memory_id, updated_at)"
            " VALUES (%s, %s, %s, %s, NOW())"
            " ON DUPLICATE KEY UPDATE"
            "   summary = VALUES(summary),"
            "   updated_through_memory_id ="
            "     VALUES(updated_through_memory_id),"
            "   updated_at = NOW()",
            (
                bot_guid, player_guid, summary,
                new_watermark,
            ),
        )
        conn.commit()
        logger.info(
            "Relationship summary updated bot=%s "
            "player=%s lines=%s chars=%s out=%s",
            bot_guid, player_guid, len(candidates),
            char_count, len(summary),
        )
    except Exception:
        logger.error(
            "Relationship update failed for "
            f"bot={bot_guid} player={player_guid}",
            exc_info=True,
        )
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def get_relationship_summary(db, bot_guid, player_guid):
    """Fetch the current relationship summary for a
    bot-player pair.

    Returns the summary string, or None if no row exists
    yet (first-time pair / never reached the update
    threshold) or on any DB error. Callers must treat None
    as "no standing relationship text to inject" rather
    than an error condition.
    """
    try:
        cursor = db.cursor()
        cursor.execute(
            "SELECT summary FROM llm_bot_relationships"
            " WHERE bot_guid = %s AND player_guid = %s",
            (bot_guid, player_guid),
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] else None
    except Exception:
        logger.error(
            "Relationship summary lookup failed for "
            f"bot={bot_guid} player={player_guid}",
            exc_info=True,
        )
        return None


# ============================================================
# STARTUP RECOVERY
# ============================================================

def activate_orphaned_memories(
    db, session_minutes,
):
    """Promote orphaned inactive memories from
    sessions that ended without a clean farewell
    (bridge crash, server restart).

    Skips group_ids that still exist in
    llm_group_bot_traits — those are live sessions
    that will be rehydrated and should not be
    touched here.

    Uses UNIX_TIMESTAMP arithmetic since
    session_start is DOUBLE (not TIMESTAMP).
    """
    session_seconds = int(session_minutes) * 60
    try:
        cursor = db.cursor(dictionary=True)
        # Identify live groups — skip them so we
        # don't prematurely promote or discard rows
        # belonging to sessions still in progress
        cursor.execute(
            "SELECT DISTINCT group_id"
            " FROM llm_group_bot_traits"
        )
        live_groups = {
            int(r['group_id'])
            for r in cursor.fetchall()
        }

        # Find truly dead groups with inactive rows
        cursor.execute("""
            SELECT group_id, bot_guid, player_guid,
                MIN(session_start) AS min_start,
                UNIX_TIMESTAMP(MAX(created_at))
                    AS max_created
            FROM llm_bot_memories
            WHERE active = 0
            GROUP BY group_id, bot_guid, player_guid
        """)
        rows = cursor.fetchall()
        promoted = 0
        discarded = 0
        for row in rows:
            g_id = int(row['group_id'])
            # Skip live sessions — rehydration
            # will handle their pending rows
            if g_id in live_groups:
                continue
            b_guid = int(row['bot_guid'])
            p_guid = int(row['player_guid'])
            min_start = float(
                row['min_start'] or 0
            )
            max_created = float(
                row['max_created'] or 0
            )
            elapsed = max_created - min_start
            if elapsed >= session_seconds:
                cursor.execute(
                    "UPDATE llm_bot_memories"
                    " SET active = 1"
                    " WHERE group_id = %s"
                    "   AND bot_guid = %s"
                    "   AND player_guid = %s"
                    "   AND active = 0",
                    (g_id, b_guid, p_guid),
                )
                promoted += cursor.rowcount
            else:
                cursor.execute(
                    "DELETE FROM llm_bot_memories"
                    " WHERE group_id = %s"
                    "   AND bot_guid = %s"
                    "   AND player_guid = %s"
                    "   AND active = 0",
                    (g_id, b_guid, p_guid),
                )
                discarded += cursor.rowcount
        db.commit()
        if promoted or discarded:
            logger.info(
                f"Orphaned memories: promoted="
                f"{promoted}, discarded={discarded}"
            )
    except Exception:
        logger.error(
            "Orphan memory recovery failed",
            exc_info=True,
        )


def rehydrate_active_sessions(db):
    """Rebuild _active_sessions from live groups
    after a bridge restart.

    No lock needed: runs synchronously at startup
    before event loop and background executor.
    """
    try:
        cursor = db.cursor(dictionary=True)
        cursor.execute("""
            SELECT DISTINCT group_id, bot_guid
            FROM llm_group_bot_traits
        """)
        rows = cursor.fetchall()
        if not rows:
            return

        for row in rows:
            g_id = int(row['group_id'])
            b_guid = int(row['bot_guid'])
            if g_id not in _active_sessions:
                _active_sessions[g_id] = {
                    "start": time.time(),
                    "player_guid": 0,
                    "bots": set(),
                    "members": {},
                    "msg_count": 0,
                    "party_memories_generated": True,
                }
                # Create the per-group lock so that
                # queue_memory() and flush_session_memories()
                # (which use create=False) can find it
                # immediately after restart
                _get_group_lock(g_id, create=True)
            _active_sessions[g_id]["bots"].add(
                b_guid
            )

        if _active_sessions:
            logger.info(
                f"Rehydrated {len(_active_sessions)}"
                f" active sessions"
            )
    except Exception:
        logger.error(
            "Session rehydration failed",
            exc_info=True,
        )


# ============================================================
# GARBAGE COLLECTION (GM-triggered)
# ============================================================

def purge_orphaned_memories(db):
    """Delete llm_bot_memories and llm_bot_relationships
    rows whose bot_guid or player_guid no longer exists
    in characters (e.g. deleted characters).

    Runs on the bridge's 24-hour periodic maintenance
    pass; the '.llm memory clean' GM command issues the
    same two DELETEs from C++. Returns the number of
    llm_bot_memories rows deleted (unchanged return
    contract for existing callers/logging).
    """
    cursor = db.cursor()
    cursor.execute("""
        DELETE m FROM llm_bot_memories m
        LEFT JOIN characters c1
            ON m.bot_guid = c1.guid
        LEFT JOIN characters c2
            ON m.player_guid = c2.guid
        WHERE c1.guid IS NULL
           OR c2.guid IS NULL
    """)
    deleted = cursor.rowcount
    db.commit()

    cursor.execute("""
        DELETE m FROM llm_bot_relationships m
        LEFT JOIN characters c1
            ON m.bot_guid = c1.guid
        LEFT JOIN characters c2
            ON m.player_guid = c2.guid
        WHERE c1.guid IS NULL
           OR c2.guid IS NULL
    """)
    deleted_relationships = cursor.rowcount
    db.commit()
    cursor.close()

    if deleted:
        logger.info(
            "[MEMORY] purged %d orphaned "
            "llm_bot_memories row(s)",
            deleted,
        )
    if deleted_relationships:
        logger.info(
            "[MEMORY] purged %d orphaned "
            "llm_bot_relationships row(s)",
            deleted_relationships,
        )
    return deleted


# ============================================================
# MEMORY RETRIEVAL
# ============================================================

def get_bot_memories(
    db, bot_guid, player_guid, config=None, count=3,
    exclude_first_meeting=False, current_zone_id=None,
    mark_used=True,
):
    """Retrieve decay-ranked active memories for a
    bot-player pair.

    Over-fetches candidates ordered by the decay-aware
    effective_score (see _effective_score_sql()), then
    accumulates them in that order until either `count`
    rows or the LLMChatter.Memory.MaxInjectTokens budget
    is reached. Only the rows actually returned are
    marked used=1 (unless mark_used=False, e.g. when a
    memory is being fetched for secondhand reference by
    a DIFFERENT bot than the one it belongs to — that
    should not count toward this memory's own eviction
    priority).

    When current_zone_id is given, it is used as a
    TIE-BREAKER ONLY (after effective_score) so a
    same-zone memory can win among comparably-important
    candidates without ever outranking a genuinely more
    important memory from elsewhere. When omitted
    (default), ordering is byte-identical to before this
    parameter existed.

    Returns list of memory strings (may be empty).
    """
    try:
        extra = (
            " AND memory_type != 'first_meeting'"
            if exclude_first_meeting else ""
        )
        candidate_limit = max(count * 3, 15)
        cursor = db.cursor(dictionary=True)
        if current_zone_id is not None:
            cursor.execute(
                "SELECT id, memory, "
                + _effective_score_sql(config) +
                " AS effective_score"
                " FROM llm_bot_memories"
                " WHERE bot_guid = %s"
                "   AND player_guid = %s"
                "   AND active = 1"
                + extra +
                " ORDER BY effective_score DESC,"
                "   (zone_id = %s) DESC,"
                "   created_at DESC"
                " LIMIT %s",
                (
                    bot_guid, player_guid,
                    current_zone_id, candidate_limit,
                ),
            )
        else:
            cursor.execute(
                "SELECT id, memory, "
                + _effective_score_sql(config) +
                " AS effective_score"
                " FROM llm_bot_memories"
                " WHERE bot_guid = %s"
                "   AND player_guid = %s"
                "   AND active = 1"
                + extra +
                " ORDER BY effective_score DESC,"
                "   created_at DESC"
                " LIMIT %s",
                (bot_guid, player_guid, candidate_limit),
            )
        candidates = cursor.fetchall()
        if not candidates:
            return []

        max_tokens = int((config or {}).get(
            'LLMChatter.Memory.MaxInjectTokens', 400
        ))

        # Trim the (already effective_score-ordered)
        # candidates down to count + token budget. The
        # first pick is always kept even if it alone
        # would exceed budget, so a single oversized
        # memory can't starve the result down to empty.
        selected = []
        token_sum = 0
        for row in candidates:
            if len(selected) >= count:
                break
            cost = estimate_tokens(row['memory'])
            if selected and token_sum + cost > max_tokens:
                break
            selected.append(row)
            token_sum += cost

        if not selected:
            return []

        if mark_used:
            ids = [row['id'] for row in selected]
            placeholders = ','.join(
                ['%s'] * len(ids)
            )
            cursor.execute(
                "UPDATE llm_bot_memories"
                " SET used = 1,"
                " last_used_at = NOW()"
                " WHERE id IN (%s)"
                % placeholders,
                tuple(ids),
            )
            db.commit()
        return [row['memory'] for row in selected]
    except Exception:
        logger.error(
            f"Memory retrieval failed for "
            f"bot={bot_guid} player={player_guid}",
            exc_info=True,
        )
        return []


# ============================================================
# SANITIZATION
# ============================================================

def sanitize_memory_for_prompt(memory: str) -> str:
    """Sanitize a memory string for safe inclusion
    in an LLM prompt.

    Strips control characters, normalizes whitespace,
    caps at 200 characters.
    """
    if not memory or not isinstance(memory, str):
        return ""
    # Strip control characters
    text = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', memory)
    # Normalize whitespace
    text = ' '.join(text.split())
    # Cap length
    if len(text) > 200:
        text = text[:197] + "..."
    return text
