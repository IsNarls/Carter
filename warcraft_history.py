"""Durable, deterministic Warcraft Logs history and performance analytics.

The live Carter cache deliberately keeps only a small amount of rich fight data.
This module stores the normalized facts needed for historical comparisons in a
guild-scoped SQLite database. It is synchronous by design and keeps each local
transaction bounded; Discord-facing callers serialize ingestion per guild.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
import unicodedata
from collections import OrderedDict, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 2
REPORT_CODE = re.compile(r"[A-Za-z0-9]{8,32}\Z")
VALID_RESULT_FILTERS = frozenset({"kills", "wipes", "all"})


class WarcraftHistoryError(RuntimeError):
    """A safe history-store or query failure."""


class CharacterNotFoundError(WarcraftHistoryError):
    pass


class AmbiguousCharacterError(WarcraftHistoryError):
    def __init__(self, choices: Iterable[Mapping[str, Any]]):
        self.choices = tuple(dict(item) for item in choices)
        super().__init__("ambiguous_character")


class AmbiguousEncounterError(WarcraftHistoryError):
    def __init__(self, choices: Iterable[Mapping[str, Any]]):
        self.choices = tuple(dict(item) for item in choices)
        super().__init__("ambiguous_encounter")


def _key(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def _realm_key(value: Any) -> str:
    # WCL responses have used both display and normalized realm spellings.
    return "".join(char for char in _key(value) if char.isalnum())


def _number(value: Any, *, nonnegative: bool = True) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value) or (nonnegative and value < 0):
        return None
    return value


def _integer(value: Any, *, positive: bool = False) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    result = int(number)
    if positive and result <= 0:
        return None
    return result


def _table_data(table: Any) -> dict[str, Any]:
    if not isinstance(table, dict):
        return {}
    data = table.get("data")
    return data if isinstance(data, dict) else table


def _entries(table: Any, *names: str) -> list[dict[str, Any]]:
    data = _table_data(table)
    for name in (*names, "entries"):
        rows = data.get(name)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _player_detail_rows(breakdown: Mapping[str, Any]) -> list[dict[str, Any]]:
    details = _table_data(breakdown.get("playerDetails")).get("playerDetails")
    if not isinstance(details, dict):
        details = _table_data(breakdown.get("summary")).get("playerDetails")
    if not isinstance(details, dict):
        return []
    result = []
    role_names = {
        "tanks": "tank", "tank": "tank", "healers": "healer", "healer": "healer",
        "dps": "dps",
    }
    for group, role in role_names.items():
        rows = details.get(group)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                item = dict(row)
                item["_role"] = role
                result.append(item)
    return result


def fight_detail_complete(breakdown: Any) -> bool:
    """Return whether a fight payload can authoritatively describe participation."""
    if not isinstance(breakdown, Mapping):
        return False
    return all(
        isinstance(breakdown.get(name), Mapping)
        for name in ("damageDone", "healing", "playerDetails")
    ) and bool(_player_detail_rows(breakdown))


def death_events_complete(payload: Any) -> bool:
    """Return whether a death-event payload contains its final page."""
    if not isinstance(payload, Mapping):
        return False
    death_table = payload.get("deaths")
    return (
        isinstance(death_table, Mapping)
        and isinstance(death_table.get("data"), list)
        and death_table.get("nextPageTimestamp") is None
    )


def _rate(total: Any, duration_ms: Any) -> float | None:
    total_number = _number(total)
    duration_number = _number(duration_ms)
    if total_number is None or duration_number is None or duration_number <= 0:
        return None
    return total_number * 1000.0 / duration_number


def _canonical_url(report_code: str, fight_id: int | None = None) -> str:
    url = f"https://www.warcraftlogs.com/reports/{report_code}"
    return f"{url}?fight={fight_id}" if fight_id is not None else url


@dataclass(frozen=True)
class RaidNightPolicy:
    """Assign fights to a local raid date using a post-midnight cutoff."""

    timezone_name: str = "UTC"
    cutoff_hour: int = 6

    def __post_init__(self) -> None:
        if isinstance(self.cutoff_hour, bool) or not 0 <= int(self.cutoff_hour) <= 23:
            raise WarcraftHistoryError("invalid_raid_night_cutoff")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise WarcraftHistoryError("invalid_raid_night_timezone") from None

    def key_for(self, timestamp_ms: int | float) -> str:
        value = _number(timestamp_ms)
        if value is None:
            raise WarcraftHistoryError("invalid_fight_timestamp")
        local = datetime.fromtimestamp(value / 1000.0, timezone.utc).astimezone(
            ZoneInfo(self.timezone_name)
        )
        return (local - timedelta(hours=int(self.cutoff_hour))).date().isoformat()


class WarcraftHistoryStore:
    """Guild-isolated normalized Warcraft Logs storage and analytics."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        timezone_name: str = "UTC",
        cutoff_hour: int = 6,
    ) -> None:
        self.path = Path(path)
        self.policy = RaidNightPolicy(timezone_name, cutoff_hour)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            self._secure_file()
            return connection
        except Exception:
            connection.close()
            raise

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _secure_file(self) -> None:
        for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                pass

    def _initialize(self) -> None:
        with self._connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise WarcraftHistoryError("history_schema_too_new")
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE history_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE reports (
                        guild_id INTEGER NOT NULL,
                        report_code TEXT NOT NULL,
                        canonical_url TEXT NOT NULL,
                        title TEXT,
                        zone_name TEXT,
                        start_ms INTEGER,
                        end_ms INTEGER,
                        fingerprint TEXT NOT NULL,
                        sync_status TEXT NOT NULL,
                        last_synced_ms INTEGER NOT NULL,
                        last_error_code TEXT,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (guild_id, report_code)
                    );
                    CREATE TABLE encounters (
                        guild_id INTEGER NOT NULL,
                        report_code TEXT NOT NULL,
                        fight_id INTEGER NOT NULL,
                        encounter_id INTEGER NOT NULL,
                        boss_name TEXT NOT NULL,
                        difficulty INTEGER,
                        kill INTEGER NOT NULL,
                        in_progress INTEGER NOT NULL,
                        size INTEGER,
                        start_offset_ms INTEGER NOT NULL,
                        end_offset_ms INTEGER NOT NULL,
                        start_ms INTEGER NOT NULL,
                        end_ms INTEGER NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        raid_night TEXT NOT NULL,
                        average_item_level REAL,
                        boss_percentage REAL,
                        fight_percentage REAL,
                        detail_status TEXT NOT NULL,
                        detail_synced_ms INTEGER,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY (guild_id, report_code, fight_id),
                        FOREIGN KEY (guild_id, report_code)
                            REFERENCES reports(guild_id, report_code) ON DELETE CASCADE
                    );
                    CREATE INDEX encounters_night_idx
                        ON encounters(guild_id, raid_night, start_ms);
                    CREATE INDEX encounters_boss_idx
                        ON encounters(guild_id, encounter_id, difficulty, raid_night);
                    CREATE TABLE characters (
                        character_id INTEGER PRIMARY KEY,
                        guild_id INTEGER NOT NULL,
                        identity_key TEXT NOT NULL,
                        wcl_canonical_id INTEGER,
                        current_name TEXT NOT NULL,
                        current_realm TEXT,
                        current_region TEXT,
                        created_ms INTEGER NOT NULL,
                        last_seen_ms INTEGER NOT NULL,
                        UNIQUE (guild_id, identity_key),
                        UNIQUE (guild_id, character_id)
                    );
                    CREATE UNIQUE INDEX characters_canonical_idx
                        ON characters(guild_id, wcl_canonical_id)
                        WHERE wcl_canonical_id IS NOT NULL;
                    CREATE INDEX characters_name_idx
                        ON characters(guild_id, current_name);
                    CREATE TABLE character_aliases (
                        guild_id INTEGER NOT NULL,
                        character_id INTEGER NOT NULL,
                        name_key TEXT NOT NULL,
                        realm_key TEXT NOT NULL,
                        region_key TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        display_realm TEXT,
                        display_region TEXT,
                        first_seen_ms INTEGER NOT NULL,
                        last_seen_ms INTEGER NOT NULL,
                        PRIMARY KEY (
                            guild_id,character_id,name_key,realm_key,region_key
                        ),
                        FOREIGN KEY (guild_id, character_id)
                            REFERENCES characters(guild_id, character_id) ON DELETE CASCADE
                    );
                    CREATE INDEX aliases_character_idx
                        ON character_aliases(guild_id, character_id);
                    CREATE INDEX aliases_lookup_idx
                        ON character_aliases(guild_id, name_key, realm_key, region_key);
                    CREATE TABLE player_performance (
                        guild_id INTEGER NOT NULL,
                        report_code TEXT NOT NULL,
                        fight_id INTEGER NOT NULL,
                        character_id INTEGER NOT NULL,
                        actor_id INTEGER,
                        actor_guid INTEGER,
                        name_snapshot TEXT NOT NULL,
                        realm_snapshot TEXT,
                        region_snapshot TEXT,
                        class_name TEXT,
                        spec_name TEXT,
                        role TEXT,
                        damage_total REAL,
                        damage_time_ms REAL,
                        active_time_ms REAL,
                        healing_total REAL,
                        healing_time_ms REAL,
                        overheal_total REAL,
                        death_count INTEGER,
                        identity_confidence TEXT NOT NULL,
                        metric_status TEXT NOT NULL,
                        PRIMARY KEY (guild_id, report_code, fight_id, character_id),
                        FOREIGN KEY (guild_id, report_code, fight_id)
                            REFERENCES encounters(guild_id, report_code, fight_id)
                            ON DELETE CASCADE,
                        FOREIGN KEY (guild_id, character_id)
                            REFERENCES characters(guild_id, character_id)
                            ON DELETE CASCADE
                    );
                    CREATE INDEX performance_character_idx
                        ON player_performance(guild_id, character_id, report_code, fight_id);
                    PRAGMA user_version=2;
                    COMMIT;
                    """
                )
            elif version == 1:
                # Version 1 allowed only one owner for a name/realm/region alias.
                # Preserve every row while allowing distinct WCL GUIDs to share a
                # historical alias without silently merging their performance.
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE character_aliases RENAME TO character_aliases_v1;
                    DROP INDEX aliases_character_idx;
                    CREATE TABLE character_aliases (
                        guild_id INTEGER NOT NULL,
                        character_id INTEGER NOT NULL,
                        name_key TEXT NOT NULL,
                        realm_key TEXT NOT NULL,
                        region_key TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        display_realm TEXT,
                        display_region TEXT,
                        first_seen_ms INTEGER NOT NULL,
                        last_seen_ms INTEGER NOT NULL,
                        PRIMARY KEY (
                            guild_id,character_id,name_key,realm_key,region_key
                        ),
                        FOREIGN KEY (guild_id,character_id)
                            REFERENCES characters(guild_id,character_id) ON DELETE CASCADE
                    );
                    INSERT INTO character_aliases(
                        guild_id,character_id,name_key,realm_key,region_key,display_name,
                        display_realm,display_region,first_seen_ms,last_seen_ms
                    )
                    SELECT guild_id,character_id,name_key,realm_key,region_key,display_name,
                        display_realm,display_region,first_seen_ms,last_seen_ms
                    FROM character_aliases_v1;
                    DROP TABLE character_aliases_v1;
                    CREATE INDEX aliases_character_idx
                        ON character_aliases(guild_id,character_id);
                    CREATE INDEX aliases_lookup_idx
                        ON character_aliases(guild_id,name_key,realm_key,region_key);
                    PRAGMA user_version=2;
                    COMMIT;
                    """
                )
            expected = {
                "timezone": self.policy.timezone_name,
                "cutoff_hour": str(int(self.policy.cutoff_hour)),
            }
            current = dict(connection.execute("SELECT key, value FROM history_meta"))
            if current and any(current.get(key) != value for key, value in expected.items()):
                raise WarcraftHistoryError("raid_night_policy_changed")
            connection.executemany(
                "INSERT OR IGNORE INTO history_meta(key,value) VALUES (?,?)",
                expected.items(),
            )
        self._secure_file()

    @staticmethod
    def _breakdown_map(report_code: str, breakdowns: Any) -> dict[int, dict[str, Any]]:
        result: dict[int, dict[str, Any]] = {}
        if isinstance(breakdowns, Mapping):
            for key, value in breakdowns.items():
                if not isinstance(value, dict):
                    continue
                fight_id = None
                if isinstance(key, tuple) and len(key) == 2 and str(key[0]) == report_code:
                    fight_id = _integer(key[1], positive=True)
                elif isinstance(key, (int, str)) and str(key).isdigit():
                    fight_id = _integer(int(key), positive=True)
                if fight_id is not None:
                    result[fight_id] = value
        elif isinstance(breakdowns, (list, tuple)):
            for item in breakdowns:
                if not isinstance(item, dict) or str(item.get("report_code")) != report_code:
                    continue
                fight_id = _integer(item.get("fight_id"), positive=True)
                if fight_id and isinstance(item.get("data"), dict):
                    result[fight_id] = item["data"]
        return result

    @staticmethod
    def _death_payload(report_code: str, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        if "deaths" in value or "masterData" in value:
            return value
        nested = value.get(report_code)
        return nested if isinstance(nested, dict) else None

    @staticmethod
    def _raw_deaths(payload: dict[str, Any] | None) -> tuple[bool, dict[tuple[int, int], int], dict[int, dict[str, Any]]]:
        if payload is None:
            return False, {}, {}
        death_table = payload.get("deaths")
        data = death_table.get("data") if isinstance(death_table, dict) else None
        # Never present an incomplete event page as a complete death count.
        if (not isinstance(data, list)
                or (isinstance(death_table, dict)
                    and death_table.get("nextPageTimestamp") is not None)):
            return False, {}, {}
        counts: dict[tuple[int, int], int] = defaultdict(int)
        for event in data:
            if not isinstance(event, dict) or event.get("type") not in (None, "death"):
                continue
            fight_id = _integer(event.get("fight"), positive=True)
            actor_id = _integer(event.get("targetID"), positive=True)
            if fight_id and actor_id:
                counts[(fight_id, actor_id)] += 1
        actors = {}
        master = payload.get("masterData")
        for actor in (master.get("actors") if isinstance(master, dict) else []) or []:
            if isinstance(actor, dict) and _integer(actor.get("id"), positive=True):
                actors[int(actor["id"])] = actor
        return True, dict(counts), actors

    @staticmethod
    def _player_rows(breakdown: dict[str, Any]) -> list[dict[str, Any]]:
        return _player_detail_rows(breakdown)

    @staticmethod
    def _row_token(row: Mapping[str, Any]) -> tuple[str, Any]:
        actor_id = _integer(row.get("id"), positive=True)
        if actor_id is not None:
            return "id", actor_id
        guid = _integer(row.get("guid"), positive=True)
        if guid is not None:
            return "guid", guid
        return "name", (_key(row.get("name")), _key(row.get("type")))

    @staticmethod
    def _spec(row: Mapping[str, Any]) -> str | None:
        specs = row.get("specs")
        if isinstance(specs, list):
            candidates = [item for item in specs if isinstance(item, dict) and item.get("spec")]
            if candidates:
                selected = max(candidates, key=lambda item: _number(item.get("count")) or 0)
                return str(selected["spec"])[:80]
        icon = row.get("icon")
        if isinstance(icon, str) and "-" in icon:
            return icon.split("-", 1)[1][:80]
        return None

    def _character(
        self,
        connection: sqlite3.Connection,
        guild_id: int,
        report_code: str,
        row: Mapping[str, Any],
        seen_ms: int,
    ) -> tuple[int, str]:
        name = str(row.get("name") or "Unknown")[:100]
        realm = str(row.get("server") or "").strip()[:100] or None
        region = str(row.get("region") or "").strip()[:40] or None
        name_key = _key(name)
        realm_key = _realm_key(realm)
        region_key = _key(region)
        guid = _integer(row.get("guid"), positive=True)
        existing = None
        alias_rows = []
        if name_key and realm_key and region_key:
            alias_rows = connection.execute(
                """SELECT c.character_id,c.wcl_canonical_id
                   FROM character_aliases a JOIN characters c
                     ON c.guild_id=a.guild_id AND c.character_id=a.character_id
                   WHERE a.guild_id=? AND a.name_key=? AND a.realm_key=?
                     AND a.region_key=?
                   ORDER BY c.character_id""",
                (guild_id, name_key, realm_key, region_key),
            ).fetchall()
        if guid is not None:
            existing = connection.execute(
                """SELECT character_id,wcl_canonical_id FROM characters
                   WHERE guild_id=? AND wcl_canonical_id=?""",
                (guild_id, guid),
            ).fetchone()
            # Upgrade exactly one legacy name-based identity. A different non-null
            # GUID is a different character even when a name/realm was reused.
            if (existing is None and len(alias_rows) == 1
                    and alias_rows[0]["wcl_canonical_id"] is None):
                existing = alias_rows[0]
        elif len(alias_rows) == 1:
            existing = alias_rows[0]
        if existing is not None:
            character_id = int(existing[0])
            connection.execute(
                """UPDATE characters SET
                       wcl_canonical_id=COALESCE(?,wcl_canonical_id),current_name=?,
                       current_realm=COALESCE(?,current_realm),
                       current_region=COALESCE(?,current_region),
                       last_seen_ms=MAX(last_seen_ms,?)
                   WHERE guild_id=? AND character_id=?""",
                (guid, name, realm, region, seen_ms, guild_id, character_id),
            )
            confidence = "wcl_guid" if guid is not None else "name_realm_region"
        elif guid is not None:
            identity = f"guid:{guid}"
            confidence = "wcl_guid"
        elif name_key and realm_key and region_key and not alias_rows:
            identity = f"nrs:{region_key}:{realm_key}:{name_key}"
            confidence = "name_realm_region"
        else:
            actor = _integer(row.get("id"), positive=True)
            suffix = f"id:{actor}" if actor is not None else (
                f"guid:{guid}" if guid is not None else f"name:{name_key}"
            )
            identity = f"report:{report_code}:{suffix}"
            confidence = "report_only"
        if existing is None:
            connection.execute(
                """
                INSERT INTO characters(
                    guild_id,identity_key,wcl_canonical_id,current_name,current_realm,
                    current_region,created_ms,last_seen_ms
                ) VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id,identity_key) DO UPDATE SET
                    wcl_canonical_id=COALESCE(excluded.wcl_canonical_id,characters.wcl_canonical_id),
                    current_name=excluded.current_name,
                    current_realm=COALESCE(excluded.current_realm,characters.current_realm),
                    current_region=COALESCE(excluded.current_region,characters.current_region),
                    last_seen_ms=MAX(characters.last_seen_ms,excluded.last_seen_ms)
                """,
                (guild_id, identity, guid, name, realm, region, seen_ms, seen_ms),
            )
            character_id = int(connection.execute(
                "SELECT character_id FROM characters WHERE guild_id=? AND identity_key=?",
                (guild_id, identity),
            ).fetchone()[0])
        if name_key and realm_key and region_key:
            connection.execute(
                """
                INSERT INTO character_aliases(
                    guild_id,character_id,name_key,realm_key,region_key,display_name,
                    display_realm,display_region,first_seen_ms,last_seen_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(guild_id,character_id,name_key,realm_key,region_key) DO UPDATE SET
                    display_name=excluded.display_name,
                    display_realm=excluded.display_realm,
                    display_region=excluded.display_region,
                    last_seen_ms=MAX(character_aliases.last_seen_ms,excluded.last_seen_ms)
                """,
                (guild_id, character_id, name_key, realm_key, region_key, name, realm, region,
                 seen_ms, seen_ms),
            )
        return character_id, confidence

    def _normalize_performance(
        self,
        connection: sqlite3.Connection,
        guild_id: int,
        report_code: str,
        fight_id: int,
        breakdown: dict[str, Any],
        raw_available: bool,
        raw_counts: Mapping[tuple[int, int], int],
        death_actors: Mapping[int, Mapping[str, Any]],
        synced_ms: int,
    ) -> int:
        participants: OrderedDict[tuple[str, Any], dict[str, Any]] = OrderedDict()
        for row in self._player_rows(breakdown):
            participants[self._row_token(row)] = dict(row)
        has_player_details = bool(participants)

        summary = _table_data(breakdown.get("summary"))
        damage_rows = _entries(breakdown.get("damageDone")) or _entries(
            {"data": {"damageDone": summary.get("damageDone")}}, "damageDone",
        )
        healing_rows = _entries(breakdown.get("healing")) or _entries(
            {"data": {"healingDone": summary.get("healingDone")}}, "healingDone",
        )
        damage = {self._row_token(row): row for row in damage_rows}
        healing = {self._row_token(row): row for row in healing_rows}
        for row in (*damage_rows, *healing_rows):
            token = self._row_token(row)
            actor_type = _key(row.get("type"))
            if actor_type in {"pet", "npc", "enemy", "boss"}:
                continue
            if has_player_details and token not in participants:
                continue
            participants.setdefault(token, dict(row))

        if raw_available:
            dead_actor_ids = {actor for (event_fight, actor), count in raw_counts.items()
                              if event_fight == fight_id and count > 0}
            for actor_id in dead_actor_ids:
                actor = death_actors.get(actor_id)
                if not isinstance(actor, Mapping) or actor.get("type") not in (None, "Player"):
                    continue
                row = dict(actor)
                row["id"] = actor_id
                row.setdefault("type", actor.get("subType"))
                participants.setdefault(("id", actor_id), row)

        damage_time = _number(_table_data(breakdown.get("damageDone")).get("totalTime"))
        healing_time = _number(_table_data(breakdown.get("healing")).get("totalTime"))
        if damage_time is None:
            damage_time = _number(summary.get("totalTime"))
        if healing_time is None:
            healing_time = _number(summary.get("totalTime"))
        fallback_deaths: dict[int, int] = defaultdict(int)
        fallback_deaths_available = (
            isinstance(breakdown.get("deaths"), dict) or "deathEvents" in summary
        )
        if not raw_available:
            death_rows = _entries(breakdown.get("deaths")) or _entries(
                {"data": {"deathEvents": summary.get("deathEvents")}}, "deathEvents",
            )
            for row in death_rows:
                actor_id = _integer(row.get("id"), positive=True)
                if actor_id:
                    fallback_deaths[actor_id] += 1

        # A temporary death-events failure must not erase counts captured by an
        # earlier successful sync while otherwise-final fight metrics refresh.
        previous_deaths_by_guid = {}
        previous_deaths_by_actor = {}
        for previous in connection.execute(
            """SELECT actor_id,actor_guid,death_count FROM player_performance
               WHERE guild_id=? AND report_code=? AND fight_id=?
                   AND death_count IS NOT NULL""",
            (guild_id, report_code, fight_id),
        ):
            if previous[1] is not None:
                previous_deaths_by_guid[int(previous[1])] = int(previous[2])
            if previous[0] is not None:
                previous_deaths_by_actor[int(previous[0])] = int(previous[2])

        connection.execute(
            "DELETE FROM player_performance WHERE guild_id=? AND report_code=? AND fight_id=?",
            (guild_id, report_code, fight_id),
        )
        written = 0
        for token, base in participants.items():
            damage_row = damage.get(token)
            healing_row = healing.get(token)
            # A playerDetails row has richer identity fields than metric rows. Merge
            # metrics without overwriting those fields with absent values.
            actor_id = _integer(base.get("id"), positive=True)
            actor_guid = _integer(base.get("guid"), positive=True)
            character_id, confidence = self._character(
                connection, guild_id, report_code, base, synced_ms,
            )
            damage_total = _number(damage_row.get("total")) if damage_row else None
            active_time = _number(damage_row.get("activeTime")) if damage_row else None
            healing_total = _number(healing_row.get("total")) if healing_row else None
            overheal = _number(healing_row.get("overheal")) if healing_row else None
            prior_deaths = previous_deaths_by_guid.get(actor_guid)
            if prior_deaths is None:
                prior_deaths = previous_deaths_by_actor.get(actor_id)
            if raw_available and actor_id is not None:
                deaths = int(raw_counts.get((fight_id, actor_id), 0))
            elif prior_deaths is not None:
                deaths = prior_deaths
            elif fallback_deaths_available and actor_id is not None:
                deaths = int(fallback_deaths.get(actor_id, 0))
            else:
                deaths = None
            if damage_row is not None and healing_row is not None:
                metric_status = "damage_and_healing"
            elif damage_row is not None:
                metric_status = "damage_only"
            elif healing_row is not None:
                metric_status = "healing_only"
            else:
                metric_status = "participation_only"
            connection.execute(
                """
                INSERT INTO player_performance(
                    guild_id,report_code,fight_id,character_id,actor_id,actor_guid,
                    name_snapshot,realm_snapshot,region_snapshot,class_name,spec_name,role,
                    damage_total,damage_time_ms,active_time_ms,healing_total,healing_time_ms,
                    overheal_total,death_count,identity_confidence,metric_status
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    guild_id, report_code, fight_id, character_id, actor_id, actor_guid,
                    str(base.get("name") or "Unknown")[:100],
                    str(base.get("server") or "")[:100] or None,
                    str(base.get("region") or "")[:40] or None,
                    str(base.get("type") or "")[:80] or None,
                    self._spec(base), base.get("_role"), damage_total,
                    damage_time if damage_row is not None else None, active_time,
                    healing_total, healing_time if healing_row is not None else None,
                    overheal, deaths, confidence, metric_status,
                ),
            )
            written += 1
        return written

    def upsert_report_bundle(
        self,
        guild_id: int,
        report: Mapping[str, Any],
        breakdowns_by_fight: Any = None,
        death_events: Any = None,
        *,
        synced_ms: int | None = None,
        sync_complete: bool | None = None,
    ) -> dict[str, Any]:
        """Atomically upsert one report, its boss fights, and available metrics."""
        if isinstance(guild_id, bool) or not isinstance(guild_id, int) or guild_id <= 0:
            raise WarcraftHistoryError("invalid_guild_id")
        if not isinstance(report, Mapping):
            raise WarcraftHistoryError("invalid_report")
        report_code = str(report.get("code") or "")
        if not REPORT_CODE.fullmatch(report_code):
            raise WarcraftHistoryError("invalid_report_code")
        report_start = _integer(report.get("startTime"))
        report_end = _integer(report.get("endTime"))
        if report_start is None:
            raise WarcraftHistoryError("invalid_report_start")
        synced_ms = synced_ms if synced_ms is not None else int(time.time() * 1000)
        if _integer(synced_ms) is None:
            raise WarcraftHistoryError("invalid_sync_timestamp")
        synced_ms = int(synced_ms)
        detail_map = self._breakdown_map(report_code, breakdowns_by_fight)
        death_payload = self._death_payload(report_code, death_events)
        raw_available, raw_counts, death_actors = self._raw_deaths(death_payload)
        fights = [fight for fight in report.get("fights") or [] if isinstance(fight, dict)]
        fingerprint = json.dumps([
            [fight.get("id"), fight.get("endTime"), fight.get("kill"), fight.get("inProgress")]
            for fight in fights
        ], ensure_ascii=True, separators=(",", ":"))
        zone = report.get("zone")
        zone_name = str(zone.get("name") or "")[:120] if isinstance(zone, dict) else ""
        metadata = json.dumps({
            "title": str(report.get("title") or "")[:240],
            "zone": zone_name,
            "fight_count": len(fights),
        }, ensure_ascii=True, separators=(",", ":"))
        in_progress = any(fight.get("inProgress") is True for fight in fights)
        if sync_complete is not None and not isinstance(sync_complete, bool):
            raise WarcraftHistoryError("invalid_sync_status")
        requested_status = (
            "in_progress" if in_progress
            else "partial" if sync_complete is False
            else "complete"
        )
        encounter_count = performance_count = 0
        connection = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO reports(
                    guild_id,report_code,canonical_url,title,zone_name,start_ms,end_ms,
                    fingerprint,sync_status,last_synced_ms,last_error_code,metadata_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)
                ON CONFLICT(guild_id,report_code) DO UPDATE SET
                    canonical_url=excluded.canonical_url,title=excluded.title,
                    zone_name=excluded.zone_name,start_ms=excluded.start_ms,end_ms=excluded.end_ms,
                    fingerprint=excluded.fingerprint,sync_status=excluded.sync_status,
                    last_synced_ms=excluded.last_synced_ms,last_error_code=NULL,
                    metadata_json=excluded.metadata_json
                """,
                (
                    guild_id, report_code, _canonical_url(report_code),
                    str(report.get("title") or "Untitled report")[:240], zone_name or None,
                    report_start, report_end, fingerprint,
                    requested_status, synced_ms, metadata,
                ),
            )
            for fight in fights:
                encounter_id = _integer(fight.get("encounterID"), positive=True)
                fight_id = _integer(fight.get("id"), positive=True)
                start_offset = _integer(fight.get("startTime"))
                end_offset = _integer(fight.get("endTime"))
                if not encounter_id or not fight_id or start_offset is None or end_offset is None:
                    continue
                absolute_start = report_start + start_offset
                absolute_end = report_start + end_offset
                duration = max(0, end_offset - start_offset)
                breakdown = detail_map.get(fight_id)
                detail_status = "missing"
                if isinstance(breakdown, dict):
                    detail_status = "complete" if fight_detail_complete(breakdown) else "partial"
                fight_metadata = json.dumps({
                    key: fight.get(key) for key in (
                        "friendlyPlayers", "friendlySpecs", "lastPhase", "phaseTransitions"
                    ) if key in fight
                }, ensure_ascii=True, separators=(",", ":"))
                connection.execute(
                    """
                    INSERT INTO encounters(
                        guild_id,report_code,fight_id,encounter_id,boss_name,difficulty,kill,
                        in_progress,size,start_offset_ms,end_offset_ms,start_ms,end_ms,duration_ms,
                        raid_night,average_item_level,boss_percentage,fight_percentage,
                        detail_status,detail_synced_ms,metadata_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(guild_id,report_code,fight_id) DO UPDATE SET
                        encounter_id=excluded.encounter_id,boss_name=excluded.boss_name,
                        difficulty=excluded.difficulty,kill=excluded.kill,
                        in_progress=excluded.in_progress,size=excluded.size,
                        start_offset_ms=excluded.start_offset_ms,end_offset_ms=excluded.end_offset_ms,
                        start_ms=excluded.start_ms,end_ms=excluded.end_ms,
                        duration_ms=excluded.duration_ms,raid_night=excluded.raid_night,
                        average_item_level=excluded.average_item_level,
                        boss_percentage=excluded.boss_percentage,
                        fight_percentage=excluded.fight_percentage,
                        detail_status=CASE
                            WHEN excluded.detail_status='missing' THEN encounters.detail_status
                            WHEN encounters.detail_status='complete'
                                 AND excluded.detail_status!='complete'
                                THEN encounters.detail_status
                            ELSE excluded.detail_status END,
                        detail_synced_ms=CASE
                            WHEN excluded.detail_status='missing' THEN encounters.detail_synced_ms
                            WHEN encounters.detail_status='complete'
                                 AND excluded.detail_status!='complete'
                                THEN encounters.detail_synced_ms
                            ELSE excluded.detail_synced_ms END,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        guild_id, report_code, fight_id, encounter_id,
                        str(fight.get("name") or "Unknown encounter")[:160],
                        _integer(fight.get("difficulty")), int(fight.get("kill") is True),
                        int(fight.get("inProgress") is True), _integer(fight.get("size")),
                        start_offset, end_offset, absolute_start, absolute_end, duration,
                        self.policy.key_for(absolute_start),
                        _number(fight.get("averageItemLevel")),
                        _number(fight.get("bossPercentage")),
                        _number(fight.get("fightPercentage")), detail_status,
                        synced_ms if breakdown is not None else None, fight_metadata,
                    ),
                )
                encounter_count += 1
                existing_performance = int(connection.execute(
                    """SELECT COUNT(*) FROM player_performance
                       WHERE guild_id=? AND report_code=? AND fight_id=?""",
                    (guild_id, report_code, fight_id),
                ).fetchone()[0])
                if (isinstance(breakdown, dict)
                        and (detail_status == "complete" or existing_performance == 0)):
                    performance_count += self._normalize_performance(
                        connection, guild_id, report_code, fight_id, breakdown,
                        raw_available, raw_counts, death_actors, synced_ms,
                    )
            incomplete_details = int(connection.execute(
                """SELECT COUNT(*) FROM encounters
                   WHERE guild_id=? AND report_code=? AND detail_status!='complete'""",
                (guild_id, report_code),
            ).fetchone()[0])
            final_status = (
                "in_progress" if in_progress
                else "partial" if sync_complete is False or incomplete_details
                else "complete"
            )
            connection.execute(
                """UPDATE reports SET sync_status=?
                   WHERE guild_id=? AND report_code=?""",
                (final_status, guild_id, report_code),
            )
            connection.execute(
                """DELETE FROM characters
                   WHERE guild_id=? AND NOT EXISTS (
                       SELECT 1 FROM player_performance p
                       WHERE p.guild_id=characters.guild_id
                           AND p.character_id=characters.character_id
                   )""",
                (guild_id,),
            )
            connection.commit()
        except sqlite3.Error as error:
            try:
                if connection is not None:
                    connection.rollback()
            except Exception:
                pass
            raise WarcraftHistoryError("history_write_failed") from error
        except WarcraftHistoryError:
            try:
                if connection is not None:
                    connection.rollback()
            except Exception:
                pass
            raise
        except Exception as error:
            try:
                if connection is not None:
                    connection.rollback()
            except Exception:
                pass
            raise WarcraftHistoryError("history_normalization_failed") from error
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
            self._secure_file()
        return {
            "report_code": report_code,
            "encounters": encounter_count,
            "performance_rows": performance_count,
            "sync_status": final_status,
        }

    def _character_choices(
        self, guild_id: int, player: str, realm: str | None, region: str | None,
    ) -> list[dict[str, Any]]:
        clauses = ["a.guild_id=?", "a.name_key=?"]
        parameters: list[Any] = [guild_id, _key(player)]
        if realm:
            clauses.append("a.realm_key=?")
            parameters.append(_realm_key(realm))
        if region:
            clauses.append("a.region_key=?")
            parameters.append(_key(region))
        query = f"""
            SELECT DISTINCT c.character_id,c.current_name,c.current_realm,c.current_region,
                c.identity_key
            FROM character_aliases a
            JOIN characters c ON c.guild_id=a.guild_id AND c.character_id=a.character_id
            WHERE {' AND '.join(clauses)}
            ORDER BY c.current_region,c.current_realm,c.current_name,c.character_id
        """
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
            if not rows and not realm and not region:
                rows = connection.execute(
                    """SELECT character_id,current_name,current_realm,current_region,identity_key
                       FROM characters WHERE guild_id=? AND lower(current_name)=lower(?)
                       ORDER BY current_region,current_realm,current_name,character_id""",
                    (guild_id, player.strip()),
                ).fetchall()
        return [dict(row) for row in rows]

    def resolve_character(
        self, guild_id: int, player: str, *, realm: str | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        choices = self._character_choices(guild_id, player, realm, region)
        if not choices:
            raise CharacterNotFoundError("character_not_found")
        if len(choices) > 1:
            raise AmbiguousCharacterError(choices)
        return choices[0]

    def resolve_encounter(
        self, guild_id: int, boss: str, *, difficulty: int | None = None,
    ) -> dict[str, Any]:
        """Resolve an encounter by exact name, then by one unique substring."""
        requested = _key(boss)
        if not requested:
            raise WarcraftHistoryError("encounter_not_found")
        parameters: list[Any] = [guild_id]
        clause = ""
        if difficulty is not None:
            clause = " AND difficulty IS ?"
            parameters.append(difficulty)
        with self._connection() as connection:
            rows = [dict(row) for row in connection.execute(
                f"""SELECT DISTINCT encounter_id,boss_name,difficulty FROM encounters
                    WHERE guild_id=? {clause}
                    ORDER BY boss_name,encounter_id,difficulty""",
                parameters,
            )]
        exact = [item for item in rows if _key(item["boss_name"]) == requested]
        matches = exact if exact else [
            item for item in rows if requested in _key(item["boss_name"])
        ]
        identities = {
            (item["encounter_id"], item["difficulty"]): item for item in matches
        }
        choices = list(identities.values())
        if not choices:
            raise WarcraftHistoryError("encounter_not_found")
        if len(choices) > 1:
            raise AmbiguousEncounterError(choices)
        return choices[0]

    @staticmethod
    def _filter_sql(result_filter: str) -> str:
        if result_filter not in VALID_RESULT_FILTERS:
            raise WarcraftHistoryError("invalid_result_filter")
        return " AND e.kill=1" if result_filter == "kills" else (
            " AND e.kill=0" if result_filter == "wipes" else ""
        )

    def _performance_rows(
        self,
        guild_id: int,
        character_id: int,
        nights: Iterable[str],
        result_filter: str,
        *,
        encounter_id: int | None = None,
        difficulty: int | None = None,
    ) -> list[dict[str, Any]]:
        nights = tuple(nights)
        if not nights:
            return []
        placeholders = ",".join("?" for _ in nights)
        extra = self._filter_sql(result_filter)
        # Begin with encounters and left-join this character so coverage includes
        # pulls where the player was absent or only has unavailable metrics. The
        # EXISTS clause limits coverage to reports in which the character appears,
        # avoiding unrelated raid groups that happened to log on the same date.
        parameters: list[Any] = [character_id, guild_id, *nights]
        if encounter_id is not None:
            extra += " AND e.encounter_id=?"
            parameters.append(encounter_id)
        if difficulty is not None:
            extra += " AND e.difficulty IS ?"
            parameters.append(difficulty)
        parameters.append(character_id)
        query = f"""
            SELECT e.raid_night,e.report_code,e.fight_id,e.encounter_id,e.boss_name,
                e.difficulty,e.kill,e.in_progress,e.start_ms,e.duration_ms,e.detail_status,
                p.damage_total,p.damage_time_ms,p.active_time_ms,p.healing_total,
                p.healing_time_ms,p.death_count,p.spec_name,p.role,p.metric_status
            FROM encounters e
            LEFT JOIN player_performance p ON p.guild_id=e.guild_id
                AND p.report_code=e.report_code AND p.fight_id=e.fight_id
                AND p.character_id=?
            WHERE e.guild_id=? AND e.raid_night IN ({placeholders})
                AND e.in_progress=0 {extra}
                AND EXISTS (
                    SELECT 1 FROM player_performance px
                    WHERE px.guild_id=e.guild_id AND px.report_code=e.report_code
                        AND px.character_id=?
                )
            ORDER BY e.start_ms,e.report_code,e.fight_id
        """
        with self._connection() as connection:
            return [dict(row) for row in connection.execute(query, parameters)]

    @staticmethod
    def _pull(row: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "report_code": row["report_code"], "fight_id": row["fight_id"],
            "boss_name": row["boss_name"], "encounter_id": row["encounter_id"],
            "difficulty": row["difficulty"], "kill": bool(row["kill"]),
            "start_ms": row["start_ms"], "duration_ms": row["duration_ms"],
            "damage_total": row["damage_total"], "damage_time_ms": row["damage_time_ms"],
            "dps": _rate(row["damage_total"], row["damage_time_ms"]),
            "active_dps": _rate(row["damage_total"], row["active_time_ms"]),
            "healing_total": row["healing_total"],
            "hps": _rate(row["healing_total"], row["healing_time_ms"]),
            "deaths": row["death_count"], "spec": row["spec_name"], "role": row["role"],
            "metric_status": row["metric_status"], "detail_status": row["detail_status"],
            "url": _canonical_url(row["report_code"], row["fight_id"]),
        }

    @staticmethod
    def _aggregate(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        rows = list(rows)
        eligible = [row for row in rows if _rate(row.get("damage_total"), row.get("damage_time_ms")) is not None]
        damage = sum(float(row["damage_total"]) for row in eligible)
        duration = sum(float(row["damage_time_ms"]) for row in eligible)
        healing_rows = [row for row in rows if _rate(row.get("healing_total"), row.get("healing_time_ms")) is not None]
        healing = sum(float(row["healing_total"]) for row in healing_rows)
        healing_duration = sum(float(row["healing_time_ms"]) for row in healing_rows)
        known_deaths = [int(row["death_count"]) for row in rows if row.get("death_count") is not None]
        absent = sum(
            row.get("metric_status") is None and row.get("detail_status") == "complete"
            for row in rows
        )
        unavailable = len(rows) - len(eligible)
        return {
            "pulls": len(rows), "eligible_encounters": len(eligible),
            "unavailable_encounters": unavailable,
            "absent_encounters": absent,
            "data_unavailable_encounters": max(0, unavailable - absent),
            "kills": sum(bool(row.get("kill")) for row in rows),
            "damage_total": damage if eligible else None,
            "damage_time_ms": duration if eligible else None,
            "dps": _rate(damage, duration) if eligible else None,
            "healing_total": healing if healing_rows else None,
            "healing_time_ms": healing_duration if healing_rows else None,
            "hps": _rate(healing, healing_duration) if healing_rows else None,
            "deaths": sum(known_deaths) if len(known_deaths) > 0 else None,
            "specs": sorted({str(row["spec_name"]) for row in rows if row.get("spec_name")}),
            "roles": sorted({str(row["role"]) for row in rows if row.get("role")}),
        }

    def _night_keys(
        self, guild_id: int, character_id: int, nights: int, result_filter: str,
        *, encounter_id: int | None = None, difficulty: int | None = None,
    ) -> list[str]:
        if isinstance(nights, bool) or not 1 <= int(nights) <= 100:
            raise WarcraftHistoryError("invalid_night_limit")
        extra = self._filter_sql(result_filter)
        if encounter_id is not None:
            parameters: list[Any] = [guild_id, encounter_id]
            difficulty_clause = ""
            if difficulty is not None:
                difficulty_clause = " AND e.difficulty IS ?"
                parameters.append(difficulty)
            parameters.extend((character_id, int(nights)))
            with self._connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT e.raid_night
                    FROM encounters e
                    WHERE e.guild_id=? AND e.encounter_id=? {difficulty_clause}
                        AND e.in_progress=0 {extra}
                        AND EXISTS (
                            SELECT 1 FROM player_performance px
                            WHERE px.guild_id=e.guild_id AND px.report_code=e.report_code
                                AND px.character_id=?
                        )
                    ORDER BY e.raid_night DESC LIMIT ?
                    """,
                    parameters,
                ).fetchall()
            return [str(row[0]) for row in reversed(rows)]
        parameters: list[Any] = [guild_id, character_id]
        if difficulty is not None:
            extra += " AND e.difficulty IS ?"
            parameters.append(difficulty)
        parameters.append(int(nights))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT e.raid_night
                FROM player_performance p JOIN encounters e
                  ON e.guild_id=p.guild_id AND e.report_code=p.report_code AND e.fight_id=p.fight_id
                WHERE p.guild_id=? AND p.character_id=? AND e.in_progress=0 {extra}
                ORDER BY e.raid_night DESC LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [str(row[0]) for row in reversed(rows)]

    @staticmethod
    def _trend(nights: list[dict[str, Any]], metric: str = "dps") -> dict[str, Any]:
        if metric not in {"dps", "hps"}:
            raise WarcraftHistoryError("invalid_trend_metric")
        comparable = [night for night in nights if night.get(metric) is not None]
        if len(comparable) < 2:
            return {"direction": "insufficient_data", "percent_change": None,
                    "comparable_nights": len(comparable), "metric": metric}
        first, last = comparable[0][metric], comparable[-1][metric]
        if first is None or first <= 0:
            return {"direction": "insufficient_data", "percent_change": None,
                    "comparable_nights": len(comparable), "metric": metric}
        change = (last - first) * 100.0 / first
        direction = "stable" if abs(change) <= 2.0 else ("increased" if change > 0 else "decreased")
        return {"direction": direction, "percent_change": change,
                "comparable_nights": len(comparable), "stable_threshold_percent": 2.0,
                "metric": metric}

    def player_nights(
        self,
        guild_id: int,
        player: str,
        *,
        nights: int = 5,
        realm: str | None = None,
        region: str | None = None,
        result_filter: str = "kills",
    ) -> dict[str, Any]:
        character = self.resolve_character(guild_id, player, realm=realm, region=region)
        keys = self._night_keys(guild_id, character["character_id"], nights, result_filter)
        rows = self._performance_rows(
            guild_id, character["character_id"], keys, result_filter,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["raid_night"]].append(row)
        summaries = []
        for night in keys:
            night_rows = grouped.get(night, [])
            summary = {"raid_night": night, **self._aggregate(night_rows)}
            bosses: OrderedDict[tuple[int, Any], list[dict[str, Any]]] = OrderedDict()
            for row in night_rows:
                bosses.setdefault((row["encounter_id"], row["difficulty"]), []).append(row)
            summary["bosses_analyzed"] = len(bosses)
            summary["bosses"] = [{
                "encounter_id": key[0], "difficulty": key[1],
                "boss_name": values[-1]["boss_name"], **self._aggregate(values),
            } for key, values in bosses.items()]
            summary["source_urls"] = list(dict.fromkeys(
                _canonical_url(row["report_code"]) for row in night_rows
            ))
            summaries.append(summary)
        return {
            "mode": "player_nights", "character": character,
            "result_filter": result_filter, "nights_requested": int(nights),
            "nights_returned": len(summaries), "nights": summaries,
            "trend": self._trend(summaries),
            "warning": "Raw DPS can vary with boss composition, difficulty, specialization, and fight length.",
        }

    def latest_raid_night_boss_comparison(
        self,
        guild_id: int,
        player: str,
        *,
        bosses: int = 5,
        raid_night: str | None = None,
        realm: str | None = None,
        region: str | None = None,
        result_filter: str = "kills",
    ) -> dict[str, Any]:
        if isinstance(bosses, bool) or not 1 <= int(bosses) <= 25:
            raise WarcraftHistoryError("invalid_boss_limit")
        character = self.resolve_character(guild_id, player, realm=realm, region=region)
        keys = self._night_keys(guild_id, character["character_id"], 100, result_filter)
        if raid_night is None:
            selected = keys[-1] if keys else None
        else:
            selected = raid_night if raid_night in keys else None
        rows = self._performance_rows(
            guild_id, character["character_id"], [selected] if selected else [], result_filter,
        )
        grouped: OrderedDict[tuple[int, Any], list[dict[str, Any]]] = OrderedDict()
        for row in rows:
            key = (row["encounter_id"], row["difficulty"])
            if key not in grouped and len(grouped) >= int(bosses):
                continue
            grouped.setdefault(key, []).append(row)
        comparisons = []
        for (encounter_id, difficulty), values in grouped.items():
            comparisons.append({
                "encounter_id": encounter_id, "difficulty": difficulty,
                "boss_name": values[-1]["boss_name"], **self._aggregate(values),
                "pull_details": [self._pull(row) for row in values],
            })
        eligible = [item for item in comparisons if item.get("dps") is not None]
        strongest = max(eligible, key=lambda item: item["dps"], default=None)
        weakest = min(eligible, key=lambda item: item["dps"], default=None)
        return {
            "mode": "raid_night_bosses", "character": character,
            "raid_night": selected, "result_filter": result_filter,
            "bosses_requested": int(bosses), "bosses": comparisons,
            "strongest": ({"encounter_id": strongest["encounter_id"],
                           "boss_name": strongest["boss_name"], "dps": strongest["dps"]}
                          if strongest else None),
            "weakest": ({"encounter_id": weakest["encounter_id"],
                         "boss_name": weakest["boss_name"], "dps": weakest["dps"]}
                        if weakest else None),
            "warning": "Boss DPS is not directly comparable when mechanics, target counts, or durations differ.",
        }

    # An explicit name matching the design/API language used by callers.
    player_raid_night = latest_raid_night_boss_comparison

    def player_boss_history(
        self,
        guild_id: int,
        player: str,
        encounter_id: int,
        *,
        difficulty: int | None,
        nights: int = 5,
        realm: str | None = None,
        region: str | None = None,
        result_filter: str = "kills",
    ) -> dict[str, Any]:
        encounter_id = _integer(encounter_id, positive=True)
        if encounter_id is None:
            raise WarcraftHistoryError("invalid_encounter_id")
        character = self.resolve_character(guild_id, player, realm=realm, region=region)
        if difficulty is None:
            with self._connection() as connection:
                options = [dict(row) for row in connection.execute(
                    """SELECT DISTINCT encounter_id,boss_name,difficulty FROM encounters
                       WHERE guild_id=? AND encounter_id=? ORDER BY difficulty""",
                    (guild_id, encounter_id),
                )]
            difficulty_values = {item["difficulty"] for item in options}
            if len(difficulty_values) != 1:
                raise AmbiguousEncounterError(options)
            difficulty = next(iter(difficulty_values))
        keys = self._night_keys(
            guild_id, character["character_id"], nights, result_filter,
            encounter_id=encounter_id, difficulty=difficulty,
        )
        rows = self._performance_rows(
            guild_id, character["character_id"], keys, result_filter,
            encounter_id=encounter_id, difficulty=difficulty,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[row["raid_night"]].append(row)
        summaries = []
        for night in keys:
            values = grouped.get(night, [])
            summaries.append({
                "raid_night": night, **self._aggregate(values),
                "pull_details": [self._pull(row) for row in values],
                "source_urls": list(dict.fromkeys(
                    _canonical_url(row["report_code"]) for row in values
                )),
            })
        return {
            "mode": "boss_history", "character": character,
            "encounter_id": encounter_id,
            "boss_name": rows[-1]["boss_name"] if rows else None,
            "difficulty": difficulty, "result_filter": result_filter,
            "nights_requested": int(nights), "nights_returned": len(summaries),
            "nights": summaries, "trend": self._trend(summaries),
        }

    boss_history = player_boss_history

    def recent_reports(self, guild_id: int, *, limit: int = 10) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not 1 <= int(limit) <= 100:
            raise WarcraftHistoryError("invalid_report_limit")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT r.guild_id,r.report_code,r.canonical_url,r.title,r.zone_name,
                    r.start_ms,r.end_ms,r.sync_status,r.last_synced_ms,
                    COUNT(DISTINCT e.fight_id) AS encounters,
                    COUNT(DISTINCT p.character_id) AS players
                FROM reports r
                LEFT JOIN encounters e ON e.guild_id=r.guild_id AND e.report_code=r.report_code
                LEFT JOIN player_performance p ON p.guild_id=e.guild_id
                    AND p.report_code=e.report_code AND p.fight_id=e.fight_id
                WHERE r.guild_id=? GROUP BY r.guild_id,r.report_code
                ORDER BY r.start_ms DESC,r.report_code LIMIT ?
                """,
                (guild_id, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def report_state(self, guild_id: int, report_code: str) -> dict[str, Any] | None:
        """Return minimal sync metadata for incremental-ingestion decisions."""
        if not REPORT_CODE.fullmatch(str(report_code or "")):
            raise WarcraftHistoryError("invalid_report_code")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT report_code,start_ms,end_ms,sync_status,last_synced_ms
                   FROM reports WHERE guild_id=? AND report_code=?""",
                (guild_id, report_code),
            ).fetchone()
        return dict(row) if row is not None else None

    def counts(self, guild_id: int) -> dict[str, int]:
        """Return safe guild-scoped storage counts for diagnostics."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM reports WHERE guild_id=?) AS reports,
                  (SELECT COUNT(*) FROM encounters WHERE guild_id=?) AS encounters,
                  (SELECT COUNT(*) FROM player_performance WHERE guild_id=?) AS performances,
                  (SELECT COUNT(*) FROM characters WHERE guild_id=?) AS characters,
                  (SELECT COUNT(DISTINCT raid_night) FROM encounters WHERE guild_id=?) AS raid_nights
                """,
                (guild_id, guild_id, guild_id, guild_id, guild_id),
            ).fetchone()
        return {key: int(row[key]) for key in row.keys()}

    def _prompt_player(self, prompt: str) -> str | None:
        patterns = (
            r"\b([^\W\d_][\w'\-]{1,39})[\'\u2019]s\s+(?:dps|damage|hps|healing|performance)",
            r"\b(?:player|for)\s+([^\W\d_][\w'\-]{1,39})\b",
        )
        for pattern in patterns:
            match = re.search(pattern, prompt, re.IGNORECASE)
            if match:
                return match.group(1)
        return None

    def question_context(
        self, guild_id: int, prompt: str, *, max_chars: int = 8000,
    ) -> str | None:
        """Route a narrow historical-performance question to authoritative data.

        This intentionally declines broad or ambiguous prose.  Slash commands can
        call the structured methods directly; Carter may use this best-effort
        adapter before asking a model to format the already-calculated result.
        """
        text = str(prompt or "")
        folded = text.casefold()
        historical = re.search(
            r"\b(history|historical|trend|over time|raid nights?|last\s+\d+\s+raids?|"
            r"last\s+(?:five|ten)\s+(?:raids?|nights?)|changed|improved|declined|"
            r"across\s+(?:\d+|five)\s+(?:bosses|raids?|nights?)|same boss)\b",
            folded,
        )
        if not historical or not re.search(r"\b(dps|damage|hps|healing|performance)\b", folded):
            return None
        player = self._prompt_player(text)
        if not player:
            return None
        count_match = re.search(r"\b(?:last|across|over)\s+(\d{1,2})\b", folded)
        requested = min(20, max(1, int(count_match.group(1)))) if count_match else (
            10 if re.search(r"\b(?:last|across|over)\s+ten\b", folded) else 5
        )
        result_filter = "wipes" if re.search(r"\bwipes?\s+only\b", folded) else (
            "all" if re.search(r"\b(?:include|including)\s+wipes?\b|\ball pulls?\b", folded)
            else "kills"
        )
        try:
            if re.search(r"\b(?:across\s+(?:\d+|five)\s+bosses|bosses\s+(?:in|from)\s+one)", folded):
                result = self.player_raid_night(
                    guild_id, player, bosses=requested, result_filter=result_filter,
                )
            elif "same boss" in folded or re.search(r"\bon\s+(?:the\s+)?[\w'\- ]+\s+changed\b", folded):
                with self._connection() as connection:
                    candidates = [dict(row) for row in connection.execute(
                        """SELECT DISTINCT encounter_id,boss_name,difficulty FROM encounters
                           WHERE guild_id=? ORDER BY length(boss_name) DESC,boss_name""",
                        (guild_id,),
                    )]
                named = [item for item in candidates if _key(item["boss_name"]) in _key(text)]
                identities = {(item["encounter_id"], item["difficulty"]) for item in named}
                if len(identities) != 1:
                    result = {
                        "mode": "boss_history", "error": "ambiguous_encounter",
                        "choices": named[:12],
                    }
                else:
                    encounter_id, difficulty = next(iter(identities))
                    result = self.player_boss_history(
                        guild_id, player, encounter_id, difficulty=difficulty,
                        nights=requested, result_filter=result_filter,
                    )
            else:
                result = self.player_nights(
                    guild_id, player, nights=requested, result_filter=result_filter,
                )
        except AmbiguousCharacterError as error:
            result = {"error": "ambiguous_character", "choices": error.choices}
        except CharacterNotFoundError:
            result = {"error": "character_not_found", "player": player}
        except AmbiguousEncounterError as error:
            result = {"error": "ambiguous_encounter", "choices": error.choices}
        if re.search(r"\b(hps|healing)\b", folded) and not result.get("error"):
            result["requested_metric"] = "hps"
            if isinstance(result.get("nights"), list):
                result["trend"] = self._trend(result["nights"], "hps")
            if isinstance(result.get("bosses"), list):
                comparable = [item for item in result["bosses"] if item.get("hps") is not None]
                result["strongest"] = max(
                    comparable, key=lambda item: item["hps"], default=None,
                )
                result["weakest"] = min(
                    comparable, key=lambda item: item["hps"], default=None,
                )
        return self.compact_context(result, max_chars=max_chars)

    @staticmethod
    def compact_context(result: Mapping[str, Any], *, max_chars: int = 8000) -> str:
        """Return bounded authoritative JSON suitable for Carter's model context."""
        if isinstance(max_chars, bool) or not 500 <= int(max_chars) <= 30000:
            raise WarcraftHistoryError("invalid_context_limit")
        prefix = "WARCRAFT LOGS HISTORICAL DATA (authoritative calculations):\n"
        budget = int(max_chars) - len(prefix)
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), default=str)
        if len(encoded) <= budget:
            return prefix + encoded
        # Keep the context valid JSON even when the complete structured result is
        # too large. Callers still retain the unabridged return value separately.
        preview_length = max(100, budget - 100)
        while preview_length > 100:
            compact = json.dumps({
                "_carter_truncated": True,
                "preview": encoded[:preview_length],
            }, ensure_ascii=True, separators=(",", ":"))
            if len(compact) <= budget:
                return prefix + compact
            preview_length -= 100
        return prefix + json.dumps({
            "_carter_truncated": True,
        }, separators=(",", ":"))


# Concise public name used by Carter's wiring.
HistoryStore = WarcraftHistoryStore
