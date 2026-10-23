"""Deterministic Carter routing and presentation for the analysis service."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from analysis_client import (
    AnalysisClient, AnalysisClientError, AnalysisImage, answer_without_image_bytes,
    render_answer_image,
)


REPORT_URL = re.compile(
    r"https?://(?:www\.)?warcraftlogs\.com/reports/([A-Za-z0-9]{8,32})[^\s<>()]*",
    re.IGNORECASE,
)


@dataclass
class GuildAnalysisState:
    source: str = "stored"
    report_id: str | None = None
    fight_id: int | None = None
    live_attempt: int | None = None
    identities: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedSource:
    kind: str
    report_id: str | None = None
    fight_id: int | None = None
    attempt: int | None = None
    label: str = ""


@dataclass(frozen=True)
class RaidAnswer:
    text: str
    image: AnalysisImage | None = None
    tool_context: dict[str, Any] = field(default_factory=dict)


class GuildSettingsStore:
    """Atomic, credential-free per-guild Discord and analysis selections."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            guilds = payload.get("guilds") if isinstance(payload, dict) else None
            return guilds if isinstance(guilds, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def guild(self, guild_id: int) -> dict[str, Any]:
        value = self._load().get(str(int(guild_id)), {})
        return dict(value) if isinstance(value, dict) else {}

    def save(self, guild_id: int, **changes):
        guilds = self._load()
        current = guilds.get(str(int(guild_id)))
        current = dict(current) if isinstance(current, dict) else {}
        current.update(changes)
        guilds[str(int(guild_id))] = current
        payload = json.dumps({"schema": 2, "guilds": guilds}, ensure_ascii=False,
                             separators=(",", ":"))
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

    @staticmethod
    def analysis_state(record: dict[str, Any]) -> GuildAnalysisState:
        raw = record.get("analysis") if isinstance(record.get("analysis"), dict) else {}
        source = raw.get("source") if raw.get("source") in {"stored", "live"} else "stored"
        report = raw.get("report_id") if isinstance(raw.get("report_id"), str) else None
        fight = _positive_or_none(raw.get("fight_id"))
        attempt = _positive_or_none(raw.get("live_attempt"))
        identities = raw.get("identities") if isinstance(raw.get("identities"), dict) else {}
        identities = {
            str(key): str(value)[:100] for key, value in identities.items()
            if str(key).isdecimal() and isinstance(value, str) and value.strip()
        }
        return GuildAnalysisState(source, report, fight, attempt, identities)

    def save_analysis(self, guild_id: int, state: GuildAnalysisState):
        self.save(guild_id, analysis=asdict(state))


class RaidAnalysis:
    def __init__(self, client: AnalysisClient):
        self.client = client

    async def select_report(self, state: GuildAnalysisState, report_id: str):
        fights = await self.client.fights(report_id)
        available = fights.get("fights") if isinstance(fights.get("fights"), list) else []
        if not available:
            raise AnalysisClientError("fight_not_found")
        latest = max(available, key=lambda item: int(item.get("attempt") or 0))
        state.source = "stored"
        state.report_id = str(fights.get("report_id") or report_id)
        state.fight_id = int(latest["fight_id"])
        state.live_attempt = None
        return RaidAnswer(
            f"Selected report `{state.report_id}`, fight {state.fight_id}: "
            f"{_clean(latest.get('name'), 100)} ({_result(latest)})."
        )

    async def select_fight(self, state: GuildAnalysisState, fight_id: int):
        if not state.report_id:
            await self._stored_source(state, "latest")
        fights = await self.client.fights(state.report_id)
        match = next((item for item in fights.get("fights", [])
                      if int(item.get("fight_id") or 0) == int(fight_id)), None)
        if not match:
            raise AnalysisClientError("fight_not_found")
        state.source, state.fight_id, state.live_attempt = "stored", int(fight_id), None
        return RaidAnswer(
            f"Selected fight {state.fight_id}: {_clean(match.get('name'), 100)} "
            f"({_result(match)})."
        )

    async def select_live(self, state: GuildAnalysisState, attempt: int | None = None):
        if attempt is None:
            status = await self.client.live_status()
            if not status.get("configured"):
                raise AnalysisClientError("live_source_not_configured")
            state.source, state.live_attempt = "live", None
            return RaidAnswer("Selected the current live encounter source.")
        attempts = await self.client.live_attempts()
        match = next((item for item in attempts.get("attempts", [])
                      if int(item.get("attempt") or 0) == int(attempt)), None)
        if not match:
            raise AnalysisClientError("fight_not_found")
        state.source, state.live_attempt = "live", int(attempt)
        return RaidAnswer(
            f"Selected archived live attempt {attempt}: {_clean(match.get('name'), 100)}."
        )

    async def reports(self, limit: int = 10):
        payload = await self.client.reports()
        reports = payload.get("reports") if isinstance(payload.get("reports"), list) else []
        if not reports:
            return RaidAnswer("The analysis backend has no prepared reports yet.")
        lines = ["**Prepared reports**"]
        for report in reports[:max(1, min(int(limit), 20))]:
            title = _clean(report.get("title") or "Untitled report", 90)
            lines.append(
                f"• `{report.get('report_id')}` — {title} — "
                f"{int(report.get('fight_count') or 0)} fights"
            )
        return RaidAnswer("\n".join(lines))

    async def fights(self, state: GuildAnalysisState, report_id: str | None = None,
                     limit: int = 20):
        report_id = report_id or state.report_id
        if not report_id:
            source = await self._stored_source(state, "latest")
            report_id = source.report_id
        payload = await self.client.fights(report_id)
        fights = payload.get("fights") if isinstance(payload.get("fights"), list) else []
        if not fights:
            return RaidAnswer(f"Report `{report_id}` contains no boss fights.")
        lines = [f"**Fights in `{report_id}`**"]
        for fight in fights[-max(1, min(int(limit), 25)):]:
            duration = _duration(fight.get("duration_seconds"))
            lines.append(
                f"• Fight {fight.get('fight_id')} — {_clean(fight.get('name'), 80)} — "
                f"{_result(fight)}, {duration}"
            )
        return RaidAnswer("\n".join(lines))

    async def summary(self, state: GuildAnalysisState, question: str = ""):
        source = await self.resolve_source(state, question)
        if source.kind == "stored":
            payload = await self.client.summary(source.report_id, source.fight_id)
        elif source.attempt is not None:
            payload = await self.client.archived_summary(source.attempt)
        else:
            payload = await self.client.live_summary()
        name = _clean(payload.get("name") or "Encounter", 100)
        result = _result(payload)
        text = (
            f"**{name}** — {result}, {_duration(payload.get('duration_seconds') or payload.get('fight_time_seconds'))}\n"
            f"Raid DPS: {_number(payload.get('raid_dps'))} · Raid HPS: {_number(payload.get('raid_hps'))} · "
            f"Deaths: {int(payload.get('deaths') or 0)} · Players: {int(payload.get('players') or 0)}\n"
            f"Source: {source.label}"
        )
        return RaidAnswer(text, tool_context={"intent": "summary", "source": source.label,
                                              "summary": _without_large_fields(payload)})

    async def ranking(self, state: GuildAnalysisState, metric: str, limit: int = 10,
                      question: str = ""):
        source = await self.resolve_source(state, question)
        if source.kind == "stored":
            payload = await self.client.ranking(source.report_id, source.fight_id, metric, limit)
        elif source.attempt is not None:
            payload = await self.client.archived_ranking(source.attempt, metric, limit)
        else:
            payload = await self.client.live_ranking(metric, limit)
        players = payload.get("players") if isinstance(payload.get("players"), list) else []
        value_key, total_key, label = (("dps", "damage", "DPS") if metric in {"damage", "dps"}
                                       else ("hps", "healing", "effective HPS"))
        if not players:
            return RaidAnswer(f"No {label} rows are available for {source.label}.")
        lines = [f"**Top {label} — {source.label}**"]
        for index, player in enumerate(players[:limit], 1):
            lines.append(
                f"{index}. {_clean(player.get('name'), 60)} — "
                f"{_number(player.get(value_key))} {label} "
                f"({_number(player.get(total_key))} total)"
            )
        return RaidAnswer("\n".join(lines), tool_context={
            "intent": f"{value_key}_ranking", "source": source.label,
            "players": [_player_context(item) for item in players[:limit]],
        })

    async def player(self, state: GuildAnalysisState, player_name: str, question: str = ""):
        source = await self.resolve_source(state, question)
        if source.kind == "stored":
            matches = await self.client.players(source.report_id, source.fight_id, player_name)
        elif source.attempt is not None:
            matches = await self.client.archived_players(source.attempt, player_name)
        else:
            matches = await self.client.live_players(player_name)
        players = matches.get("players") if isinstance(matches.get("players"), list) else []
        selected = _one_player(players, player_name)
        if source.kind == "stored":
            payload = await self.client.player(source.report_id, source.fight_id, selected["guid"])
        elif source.attempt is not None:
            payload = await self.client.archived_player(source.attempt, selected["guid"])
        else:
            payload = await self.client.live_player(selected["guid"])
        player = payload.get("player") if isinstance(payload.get("player"), dict) else {}
        text = (
            f"**{_clean(player.get('name'), 80)} — {source.label}**\n"
            f"DPS: {_number(player.get('dps'))} ({_number(player.get('damage'))} damage), "
            f"rank {int(player.get('rank') or 0)}.\n"
            f"Effective HPS: {_number(player.get('hps'))} "
            f"({_number(player.get('healing'))} healing), rank {int(player.get('healing_rank') or 0)}."
        )
        return RaidAnswer(text, tool_context={"intent": "player", "source": source.label,
                                              "player": _player_context(player)})

    async def death(self, state: GuildAnalysisState, player_name: str,
                    death_number: int | None = None, question: str = ""):
        source = await self.resolve_source(state, question)
        if source.kind == "stored":
            listing = await self.client.deaths(source.report_id, source.fight_id,
                                                player_name=player_name)
        elif source.attempt is not None:
            listing = await self.client.archived_deaths(source.attempt, player_name=player_name)
        else:
            listing = await self.client.live_deaths(player_name=player_name)
        deaths = listing.get("deaths") if isinstance(listing.get("deaths"), list) else []
        selected = _one_death(deaths, death_number)
        if source.kind == "stored":
            payload = await self.client.death_answer(
                source.report_id, source.fight_id, selected["death_id"],
            )
        elif source.attempt is not None:
            payload = await self.client.archived_death_answer(
                source.attempt, selected["death_id"],
            )
        else:
            payload = await self.client.live_death_answer(selected["death_id"])
        context = answer_without_image_bytes(payload)
        image_error = None
        try:
            image = render_answer_image(payload)
        except AnalysisClientError as error:
            if error.code not in {
                "unsupported_image", "invalid_image", "image_renderer_unavailable",
                "image_render_failed",
            }:
                raise
            image = None
            image_error = error.code
        text = str(context["text"])
        text += f"\nSource: {source.label}; death {selected.get('death_number')} at "
        text += f"{float(selected.get('time_seconds') or 0):.2f}s."
        if image is None:
            image_payload = payload.get("image") if isinstance(payload.get("image"), dict) else {}
            if image_payload.get("reason"):
                text += f" Position image unavailable: {_clean(image_payload['reason'], 180)}"
            elif image_error:
                text += f" Position image unavailable ({image_error})."
        source_url = _source_url(source)
        if source_url:
            text += f"\nWarcraft Logs: {source_url}"
        return RaidAnswer(text, image=image, tool_context={"intent": "death", **context})

    async def death_timeline(self, state: GuildAnalysisState, question: str = ""):
        """List bounded, fight-relative death timestamps without choosing a victim."""
        source = await self.resolve_source(state, question)
        if source.kind == "stored":
            payload = await self.client.deaths(source.report_id, source.fight_id)
        elif source.attempt is not None:
            payload = await self.client.archived_deaths(source.attempt)
        else:
            payload = await self.client.live_deaths()
        deaths = payload.get("deaths") if isinstance(payload.get("deaths"), list) else []
        ordered = sorted(
            deaths,
            key=lambda item: (float(item.get("time_seconds") or 0), str(item.get("death_id") or "")),
        )
        if not ordered:
            return RaidAnswer(f"No player deaths were recorded for {source.label}.")
        lines = [f"**Death timeline — {source.label}**"]
        for item in ordered[:50]:
            killing = item.get("killing_blow") if isinstance(item.get("killing_blow"), dict) else {}
            cause = _clean(killing.get("ability"), 80) if killing.get("ability") else "unknown cause"
            lines.append(
                f"• {float(item.get('time_seconds') or 0):.2f}s — "
                f"{_clean(item.get('player_name'), 80)} (death {int(item.get('death_number') or 0)}; "
                f"{cause})"
            )
        if len(ordered) > 50 or payload.get("truncated"):
            lines.append("Additional deaths were truncated by the bounded analysis response.")
        source_url = _source_url(source)
        if source_url:
            lines.append(f"Warcraft Logs: {source_url}")
        return RaidAnswer("\n".join(lines), tool_context={
            "intent": "death_timeline",
            "source": source.label,
            "deaths": [{
                "death_id": item.get("death_id"),
                "player_name": item.get("player_name"),
                "death_number": item.get("death_number"),
                "time_seconds": item.get("time_seconds"),
                "killing_blow": item.get("killing_blow"),
            } for item in ordered[:50]],
            "truncated": bool(len(ordered) > 50 or payload.get("truncated")),
        })

    async def submit_report(self, report_url: str):
        payload = await self.client.import_report(report_url)
        job = str(payload.get("job_id") or "")
        code = str(payload.get("report_id") or "")
        if not job:
            raise AnalysisClientError("invalid_import_response")
        return RaidAnswer(
            f"Queued report `{code or 'unknown'}` for backend retrieval and analysis "
            f"(job `{job}`).",
            tool_context={"intent": "import", "job_id": job, "report_id": code},
        )

    async def answer_question(self, state: GuildAnalysisState, question: str, user_id: int):
        text = str(question).strip()
        lowered = text.casefold()
        url = REPORT_URL.search(text)
        if url and re.search(r"\b(import|download|analy[sz]e|load|prepare)\b", lowered):
            return await self.submit_report(url.group(0))
        if (re.search(r"\b(death timeline|deaths|who died)\b", lowered)
                or re.search(r"\b(?:death breakdown|breakdown (?:of )?deaths?)\b", lowered)):
            return await self.death_timeline(state, text)
        if re.search(r"\b(die|died|death|killed|killing blow)\b", lowered):
            player = self._question_player(state, text, user_id)
            number = _death_number(lowered)
            return await self.death(state, player, number, text)
        if re.search(r"\b(heal(?:ing)?|hps)\b", lowered) and re.search(
                r"\b(top|rank|ranking|best|highest|who)\b", lowered):
            return await self.ranking(state, "hps", 10, text)
        if re.search(r"\b(dps|damage)\b", lowered):
            player = _named_player(text)
            if player and not re.search(r"\b(top|rank|ranking|best|highest|who)\b", lowered):
                return await self.player(state, player, text)
            return await self.ranking(state, "dps", 10, text)
        if re.search(r"\b(summary|summarize|breakdown|fight|pull|boss|raid|log)\b", lowered):
            return await self.summary(state, text)
        return None

    async def execute_tool(self, state: GuildAnalysisState, arguments: dict, user_id: int):
        if not isinstance(arguments, dict):
            raise AnalysisClientError("invalid_tool_arguments")
        working = GuildAnalysisState(
            source=state.source,
            report_id=state.report_id,
            fight_id=state.fight_id,
            live_attempt=state.live_attempt,
            identities=dict(state.identities),
        )
        intent = str(arguments.get("intent") or "").casefold()
        question = str(arguments.get("question") or "")[:500]
        if arguments.get("source") == "live":
            working.source = "live"
        elif arguments.get("source") == "stored":
            working.source = "stored"
        if arguments.get("report_id"):
            await self.select_report(working, str(arguments["report_id"]))
        if arguments.get("fight_id"):
            await self.select_fight(working, int(arguments["fight_id"]))
        if arguments.get("attempt"):
            await self.select_live(working, int(arguments["attempt"]))
        if intent == "summary":
            answer = await self.summary(working, question)
        elif intent in {"damage_ranking", "dps"}:
            answer = await self.ranking(
                working, "dps", int(arguments.get("limit") or 5), question,
            )
        elif intent in {"healing_ranking", "hps"}:
            answer = await self.ranking(
                working, "hps", int(arguments.get("limit") or 5), question,
            )
        elif intent == "player":
            name = self._argument_player(working, arguments.get("player_name"), user_id)
            answer = await self.player(working, name, question)
        elif intent == "death":
            name = self._argument_player(working, arguments.get("player_name"), user_id)
            number = _positive_or_none(arguments.get("death_number"))
            answer = await self.death(working, name, number, question)
        elif intent == "death_timeline":
            answer = await self.death_timeline(working, question)
        else:
            raise AnalysisClientError("unsupported_analysis_intent")
        state.source = working.source
        state.report_id = working.report_id
        state.fight_id = working.fight_id
        state.live_attempt = working.live_attempt
        return answer

    async def resolve_source(self, state: GuildAnalysisState, question: str = ""):
        if state.source == "live":
            attempt = state.live_attempt
            lowered = question.casefold()
            if re.search(r"\b(last|latest|newest|previous|prior)\s+(pull|attempt|wipe|fight)\b",
                         lowered):
                payload = await self.client.live_attempts()
                attempts = payload.get("attempts") if isinstance(payload.get("attempts"), list) else []
                if not attempts:
                    raise AnalysisClientError("fight_not_found")
                candidates = _named_encounters(attempts, lowered)
                if "wipe" in lowered:
                    candidates = [item for item in candidates if _live_attempt_result(item) == "wipe"]
                elif re.search(r"\bkill\b", lowered):
                    candidates = [item for item in candidates if _live_attempt_result(item) == "kill"]
                selected = _temporal_encounter(
                    candidates, lowered, "attempt", current_id=state.live_attempt,
                )
                attempt = int(selected["attempt"])
            if attempt is not None:
                return ResolvedSource("live", attempt=attempt,
                                      label=f"archived live attempt {attempt}")
            return ResolvedSource("live", label="current live encounter")
        return await self._stored_source(state, question)

    async def _stored_source(self, state: GuildAnalysisState, question: str):
        report_id = state.report_id
        if not report_id:
            payload = await self.client.reports()
            reports = payload.get("reports") if isinstance(payload.get("reports"), list) else []
            if not reports:
                raise AnalysisClientError("report_not_found")
            report_id = str(reports[0].get("report_id") or "")
            state.report_id = report_id
        payload = await self.client.fights(report_id)
        fights = payload.get("fights") if isinstance(payload.get("fights"), list) else []
        if not fights:
            raise AnalysisClientError("fight_not_found")
        fight_id = state.fight_id
        lowered = question.casefold()
        named = _named_encounters(fights, lowered)
        temporal = re.search(r"\b(latest|newest|last|previous|prior)\b", lowered)
        if fight_id is None or temporal or named is not fights:
            candidates = named
            if "wipe" in lowered:
                candidates = [item for item in candidates if item.get("kill") is False]
            elif re.search(r"\bkill\b", lowered):
                candidates = [item for item in candidates if item.get("kill") is True]
            selected = _temporal_encounter(
                candidates, lowered, "attempt", current_id=fight_id,
                identity_key="fight_id",
            )
            fight_id = int(selected["fight_id"])
            state.fight_id = fight_id
        selected = next((item for item in fights if int(item.get("fight_id") or 0) == fight_id), None)
        if selected is None:
            raise AnalysisClientError("fight_not_found")
        label = f"{_clean(selected.get('name'), 80)}, report {report_id}, fight {fight_id}"
        return ResolvedSource("stored", report_id, fight_id, label=label)

    @staticmethod
    def _argument_player(state, supplied, user_id):
        value = str(supplied or "").strip()
        if value.casefold() in {"", "me", "myself", "i"}:
            value = state.identities.get(str(user_id), "")
        if not value:
            raise AnalysisClientError("identity_required")
        return value[:100]

    def _question_player(self, state, question, user_id):
        lowered = question.casefold()
        if re.search(r"\b(why did i|my death|did i die|killed me)\b", lowered):
            return self._argument_player(state, None, user_id)
        player = _named_player(question)
        if not player:
            raise AnalysisClientError("player_required")
        return player


def analysis_tool_declaration():
    """Gemini Live declaration. The response never includes base64 image bytes."""
    return {
        "name": "query_raid_analysis",
        "description": (
            "Query Carter's prepared raid-log backend. Use for encounter summaries, DPS or HPS "
            "rankings, player performance, and death explanations. Never invent unavailable data."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "intent": {"type": "STRING", "enum": [
                    "summary", "damage_ranking", "healing_ranking", "player", "death",
                    "death_timeline",
                ]},
                "question": {"type": "STRING"},
                "source": {"type": "STRING", "enum": ["stored", "live"]},
                "report_id": {"type": "STRING"},
                "fight_id": {"type": "INTEGER"},
                "attempt": {"type": "INTEGER"},
                "player_name": {"type": "STRING"},
                "death_number": {"type": "INTEGER"},
                "limit": {"type": "INTEGER"},
            },
            "required": ["intent"],
        },
    }


def user_error(error: AnalysisClientError) -> str:
    messages = {
        "analysis_disabled": "Raid analysis is not enabled on this Carter deployment.",
        "analysis_unavailable": "The raid-analysis backend is unavailable.",
        "analysis_timeout": "The raid-analysis backend timed out.",
        "REPORT_NOT_FOUND": "That report is not prepared on the analysis backend.",
        "report_not_found": "No prepared reports are available yet.",
        "FIGHT_NOT_FOUND": "That fight or pull is not available.",
        "fight_not_found": "No matching fight or pull is available.",
        "ANALYSIS_REQUIRED": "The backend has the fight, but its analysis is not prepared yet.",
        "PLAYER_NOT_FOUND": "No matching player was found in that fight.",
        "player_required": "Specify a player name.",
        "identity_required": "Set your character first with `/logs identity`.",
        "ambiguous_player": "More than one player matches; use the exact character name.",
        "AMBIGUOUS_PLAYER": "More than one player matches; use the exact character name.",
        "DEATH_NOT_FOUND": "No matching death was recorded in that fight.",
        "ambiguous_death": "That player died more than once; specify the death number.",
        "LIVE_SOURCE_NOT_CONFIGURED": "The backend has no live combat-log source configured.",
        "live_source_not_configured": "The backend has no live combat-log source configured.",
        "NO_ACTIVE_ENCOUNTER": "There is no active live encounter.",
        "image_renderer_unavailable": "The explanation is available, but PNG rendering is not installed.",
    }
    return messages.get(error.code, f"Raid analysis could not complete the request ({error.code}).")


def _one_player(players, requested):
    exact = [item for item in players if str(item.get("name") or "").casefold() == requested.casefold()]
    candidates = exact or players
    if not candidates:
        raise AnalysisClientError("PLAYER_NOT_FOUND", status=404)
    if len(candidates) != 1:
        raise AnalysisClientError("ambiguous_player", details={
            "candidates": [str(item.get("name") or "") for item in candidates[:10]],
        })
    return candidates[0]


def _one_death(deaths, number):
    candidates = deaths
    if number is not None:
        candidates = [item for item in deaths if int(item.get("death_number") or 0) == int(number)]
    if not candidates:
        raise AnalysisClientError("DEATH_NOT_FOUND", status=404)
    if len(candidates) != 1:
        raise AnalysisClientError("ambiguous_death", details={
            "candidates": [{
                "death_number": item.get("death_number"),
                "time_seconds": item.get("time_seconds"),
            } for item in candidates[:20]],
        })
    return candidates[0]


def _named_player(text):
    patterns = (
        r"\bwhy did\s+([\w'’.-]{2,40})\s+(?:die|get killed)",
        r"\b(?:dps|damage|healing|hps|performance)\s+(?:for|of|by)\s+([\w'’.-]{2,40})",
        r"\bhow did\s+([\w'’.-]{2,40})\s+(?:do|perform)",
        r"\b([\w'’.-]{2,40})(?:'s|’s)\s+(?:death|dps|damage|healing|hps)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _death_number(text):
    match = re.search(r"\b(?:death|died)\s*(?:number|#)?\s*(\d{1,3})\b", text)
    return int(match.group(1)) if match else None


def _positive_or_none(value):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _clean(value, limit):
    text = str(value or "unknown").replace("`", "'").replace("@", "＠")
    return " ".join(text.split())[:limit]


def _number(value):
    try:
        return f"{float(value):,.0f}"
    except (TypeError, ValueError):
        return "0"


def _duration(value):
    try:
        seconds = max(0, int(float(value)))
    except (TypeError, ValueError):
        return "unknown duration"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _result(payload):
    if payload.get("kill") is True or str(payload.get("result") or "").upper() == "KILL":
        return "kill"
    if payload.get("kill") is False or str(payload.get("result") or "").upper() == "WIPE":
        return "wipe"
    return "in progress"


def _player_context(player):
    return {key: player.get(key) for key in (
        "guid", "name", "class_name", "damage", "dps", "rank", "healing", "hps",
        "healing_rank",
    )}


def _without_large_fields(payload):
    return {key: value for key, value in payload.items() if key not in {"image", "replay", "actors"}}


def _source_url(source):
    if source.kind == "stored" and source.report_id and source.fight_id:
        return f"https://www.warcraftlogs.com/reports/{source.report_id}#fight={source.fight_id}"
    return None


def _normalized_words(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _named_encounters(items, question):
    """Filter when a complete encounter name (optionally without leading 'The') is present."""
    normalized_question = _normalized_words(question)
    matches = []
    for item in items:
        name = _normalized_words(item.get("name"))
        variants = {name}
        if name.startswith("the "):
            variants.add(name[4:])
        if name and any(re.search(rf"\b{re.escape(value)}\b", normalized_question)
                        for value in variants if value):
            matches.append(item)
    return matches if matches else items


def _temporal_encounter(items, question, order_key, *, current_id=None,
                        identity_key=None):
    candidates = sorted(items, key=lambda item: int(item.get(order_key) or 0))
    if not candidates:
        raise AnalysisClientError("fight_not_found")
    if re.search(r"\b(previous|prior)\b", question) and current_id is not None:
        key = identity_key or order_key
        current = next((item for item in candidates
                        if int(item.get(key) or 0) == int(current_id)), None)
        if current is not None:
            current_order = int(current.get(order_key) or 0)
            earlier = [item for item in candidates
                       if int(item.get(order_key) or 0) < current_order]
            if earlier:
                return earlier[-1]
            raise AnalysisClientError("fight_not_found")
    if re.search(r"\b(previous|prior)\b", question) and len(candidates) > 1:
        return candidates[-2]
    return candidates[-1]


def _live_attempt_result(item):
    result = str(item.get("result") or "").casefold()
    if result == "kill":
        return "kill"
    if result == "wipe" or str(item.get("state") or "").casefold() == "abandoned":
        return "wipe"
    return "unknown"
