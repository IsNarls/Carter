# Log retrieval and analysis API: voice and Discord integration review

Prepared: October 23, 2026. This document describes the current repository implementation and proposed integration work. Endpoint paths were checked against `analysis_api.py`; it does not establish that a server is currently running.

Repository context: GitHub `IsNarls/Carter` already has a Discord voice prototype on `main` (observed commit `eeb2e54`), including wake-word listening, Gemini Live sessions, and optional OpenAI fallback. The status descriptions below concern the log-analysis workspace on `log-analysis-api`; they do not mean the existing prototype lacks voice transport. The two branches currently have unrelated histories. Integrate the analysis service into the existing Carter bot deliberately; do not replace `main` or blindly merge the histories. Review `main.py`, `providers.py`, `session.py`, `discord_receive.py`, and the main-branch README before adding an adapter.

## Intended outcome

This codebase handles both log retrieval and log analysis. It retrieves Warcraft Logs reports and their event data, ingests local combat logs, analyzes encounters, persists the results, and exposes analysis and answer payloads. The voice/Discord integration should reuse this backend for that entire pipeline rather than implement a separate retrieval or analysis system.

A user can ask a question by voice or Discord text, such as "Why did Alex die on the last pull?" The integration resolves the encounter, player, and death, retrieves a combined answer payload, and presents the explanation with its supporting map image. Voice receives a concise spoken explanation; Discord receives text and an image attachment from the same evidence.

Generate the visual alongside the answer evidence so the transport layer receives one consistent payload. Preserve reported versus inferred causes, missing information, stale coordinates, and encounter identity throughout the flow.

## Implementation status

| Capability | Current state |
|---|---|
| Warcraft Logs report/event retrieval and local caches | Implemented in `warcraft_logs_importer.py`; available through Python/GUI workflows |
| Retrieval/import and analysis preparation over HTTP | Not implemented; current HTTP routes query existing analysis |
| Stored report metadata and persisted fight analysis queries | Implemented |
| Local combat-log following and live analysis snapshots | Implemented; requires startup configuration |
| Player performance, rankings, mechanics, and death evidence | Implemented |
| Combined death explanation, analysis, replay, and image | Implemented for stored fights and the current live snapshot |
| Position diagram | Inline base64 SVG; generated without a GUI or external model |
| Natural-language question routing and conversational context | Not implemented |
| Discord commands, identity mapping, and message delivery | Not implemented |
| SVG-to-PNG conversion and Discord image attachment adapter | Not implemented |
| Speech recognition, speech synthesis, voice connection, and playback | Not implemented |
| External language-model answer generation | Not implemented; current death summary is deterministic |
| WebSocket, SSE, or webhook event delivery | Not implemented; clients poll updates |
| API authentication | Not implemented; default bind is loopback |

## Code ownership and entry points

| File | Responsibility |
|---|---|
| `analysis_api.py` | HTTP routes, query parsing, JSON responses, startup configuration |
| `analysis_service.py` | Stored/live queries, limits, errors, combined death-answer assembly |
| `death_answer.py` | Evidence-based death summary and SVG position diagram |
| `encounter_parser.py` | Encounter analysis, death correlation, positions, persisted analysis |
| `warcraft_logs_importer.py` | Warcraft Logs authentication, report/event retrieval, paginated downloads, local caches, and metadata handling |
| `live_log_service.py` | Local log ingestion, snapshots, cursors, completed/archived attempts |
| `mechanic_timeline.py` | Scheduled mechanic events and timer-module behavior |
| `wow_log_gui.py` | Desktop analysis and interactive map; not required by the HTTP renderer |
| `test_analysis_api.py` | API/service tests including combined answers and image evidence |
| `test_live_log_service.py` | Live ingestion and API behavior tests |
| `README.md` | Startup instructions and API documentation |

## Running the API

Default base URL: `http://127.0.0.1:8765/api/v1`.

Stored analysis:

```powershell
python analysis_api.py
```

Live local analysis:

```powershell
python analysis_api.py --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt"
```

Optional arguments include `--storage`, `--host`, `--port`, `--live-from-start`, `--mechanic-scripts`, and `--timer-modules`.

By default, live following begins at the end of the log. Stored fight queries require an existing persisted analysis; the HTTP API does not import or analyze a report on demand. Live log paths are owner-configured at startup, not supplied by a request.

The bot can use loopback when it runs on the API machine. A remotely hosted bot needs a deliberately configured connection to that machine. Define access control and transport protection before exposing this unauthenticated API remotely.

All current successful HTTP responses are JSON, including images embedded inside JSON. Python callers can use `AnalysisService` directly instead of HTTP.

### Retrieval and analysis ownership

The existing retrieval path uses `WarcraftLogsClient.download_report(...)` and batch retrieval helpers in `warcraft_logs_importer.py`. The GUI accepts report URLs and coordinates downloading and analysis; `encounter_parser.py` provides encounter scanning and analysis. Local live logs are ingested through `LiveLogService`. These are existing backend capabilities, not work to recreate in the bot.

Distinguish backend functionality from the current HTTP surface: there is no report-download/import or analysis-start endpoint today. `/reports` discovers already stored reports; fight queries read persisted analyses and can return `ANALYSIS_REQUIRED`. For a bot command such as "Analyze this Warcraft Logs URL," expose a controlled service/job wrapper around the existing retrieval and analysis functions, report progress/completion/errors, and then use the current query endpoints. Keep retrieval credentials and managed cache destinations on the backend. Review existing cache retention before promising that a previously imported report will remain available indefinitely.

## Implemented endpoint inventory

All paths below are relative to `/api/v1`. Braces identify path parameters. Query parameters shown are optional unless otherwise stated.

### Health and report discovery

| Method | Path | Result |
|---|---|---|
| GET | `/health` | API status and schema version |
| GET | `/reports` | Locally stored reports |
| GET | `/reports/{report_id}` | Report metadata |
| GET | `/reports/{report_id}/fights` | Fights and available persisted analysis |

### Stored fight analysis

In this table, `F` means `/reports/{report_id}/fights/{fight_id}`.

| Method | Path | Result / parameters |
|---|---|---|
| GET | `F` | Encounter details |
| GET | `F/summary` | Fight summary |
| GET | `F/players?query=Name` | Player search |
| GET | `F/players/{player_id}` | Player performance |
| GET | `F/rankings/damage?limit=10` | Damage ranking |
| GET | `F/rankings/healing?limit=10` | Healing ranking |
| GET | `F/mechanics?start=0&end=60&ability_id=123` | Recorded mechanics; optional time/ability filters |
| GET | `F/deaths?player_name=Name` | Death list; also accepts `player_id` |
| GET | `F/deaths/{death_id}?seconds_before=10` | Detailed death evidence |
| GET | `F/deaths/{death_id}/replay?before=8&after=2&resolution_ms=500` | Position samples and mechanic/death events |
| GET | `F/deaths/{death_id}/answer?seconds_before=10` | Combined answer, evidence, replay, and image |
| POST | `/reports/{report_id}/deaths/compare` | Compare a player's deaths across fights |
| POST | `/reports/{report_id}/mechanics/compare` | Compare mechanics across fights |

Example comparison bodies:

```json
{"fight_ids":[6,7,8],"player_name":"Alex"}
```

```json
{"fight_ids":[6,7,8],"ability_id":123}
```

Death comparison also accepts `player_id`. Mechanic `ability_id` is optional.

### Live analysis and event polling

| Method | Path | Result / parameters |
|---|---|---|
| GET | `/live/status` | Source configuration, encounter state, and follower status |
| GET | `/live/encounter` | Current encounter identity and details |
| GET | `/live/summary` | Current fight summary |
| GET | `/live/players?query=Name` | Player search |
| GET | `/live/players/{player_id}` | Player performance |
| GET | `/live/rankings/damage?limit=10` | Damage ranking |
| GET | `/live/rankings/healing?limit=10` | Healing ranking |
| GET | `/live/mechanics?start=0&end=60&ability_id=123` | Recorded mechanics |
| GET | `/live/deaths?player_name=Name` | Death list; also accepts `player_id` |
| GET | `/live/deaths/{death_id}?seconds_before=10` | Detailed death evidence |
| GET | `/live/deaths/{death_id}/replay?before=5&after=0&resolution_ms=100` | Live death replay |
| GET | `/live/deaths/{death_id}/answer?seconds_before=10` | Combined answer from one captured timeline snapshot |
| GET | `/live/updates?since=0&limit=100` | Cursor-based updates for polling |
| GET | `/live/timeline` | Scheduled mechanic timeline |
| GET | `/live/mechanic-events?status=TRIGGERED&limit=100` | Mechanic events; status can be `TRIGGERED` or `CANCELED` |
| GET | `/live/completed` | Completed encounter list |
| GET | `/live/completed/{attempt}/summary` | Completed encounter summary |
| GET | `/live/attempts` | Archived attempt list |
| GET | `/live/attempts/{attempt}/timeline` | Archived attempt mechanic timeline |

Important gap: there is no implemented `/live/attempts/{attempt}/deaths/...` route. Archived attempt timelines are mechanic schedules, not position replays. Answering a death question about an earlier live pull after the current snapshot changes needs additional service/API support or a cached combined payload captured for that pull. Stored Warcraft Logs fights use the separate report/fight namespace.

### Timer modules and drafts

| Method | Path | Result / body |
|---|---|---|
| GET | `/mechanic-drafts` | Available drafts |
| GET | `/mechanic-drafts/{draft_id}` | Draft details |
| GET | `/mechanic-drafts/{draft_id}/timer-export` | Timer export payload |
| GET | `/live/timer-module` | Current timer module state |
| POST | `/live/timer-module/arm` | Body: `timer_id` or `encounter_id`, optionally `difficulty` |
| POST | `/live/timer-module/disarm` | Disarm module; no body fields required |

Timer arming and disarming change live state. Keep them separate from ordinary question-answer commands. Placeholder schedules are labeled; do not present them as verified boss predictions.

## Combined death-answer contract

Service entry points:

```python
service.get_death_answer(report_id, fight_id, death_id, seconds_before=10)
service.get_live_death_answer(death_id, seconds_before=10)
```

Representative structure; identifiers, text, and metadata below are illustrative:

```json
{
  "schema_version": 1,
  "death_id": "selected-death-id",
  "answer": {
    "text": "Alex died at 42.00s. Killing blow: Burn (inferred). Likely primary cause: Slam (inferred from recent damage)."
  },
  "analysis": {
    "player": {"id": "selected-player-id", "name": "Alex", "class": "Mage"},
    "time_seconds": 42.0,
    "position": {},
    "killing_blow": {},
    "primary_cause": {},
    "damage_before_death": [],
    "healing_before_death": []
  },
  "replay": {
    "start": 32.0,
    "end": 42.0,
    "resolution_ms": 100,
    "actors": {},
    "events": []
  },
  "image": {
    "status": "available",
    "mime_type": "image/svg+xml",
    "encoding": "base64",
    "data": "BASE64_SVG_BYTES",
    "width": 800,
    "height": 600,
    "alt_text": "Alex — death at 42.00s",
    "map_id": 7,
    "time_seconds": 42.0,
    "kind": "position_diagram",
    "position_confidence": "high",
    "sample_age_seconds": 0.1
  }
}
```

Actual responses also carry stored report/fight identifiers or live session/attempt/cursor metadata. `analysis` retains additional death evidence such as spatial context, debuffs, and active mechanics where recorded. Inspect the source/tests for full fields rather than treating the abbreviated example as a complete schema.

If no usable death position exists, the image object is:

```json
{"status":"unavailable","reason":"No usable position recorded at or before death."}
```

The text, analysis, and replay are still returned. No standalone image URL or binary image endpoint exists.

The diagram uses the victim's map/floor, a red cross at the death position, other actors in blue, and movement lines. The victim position comes from death evidence; replay samples are restricted to at or before death. Other actors with latest usable positions older than three seconds are omitted. Victim confidence and sample age remain in image metadata. The diagram has no arena artwork or inferred mechanic shapes.

## Evidence, limits, and errors

- Times are seconds relative to encounter start. Live metadata identifies distance as `log_coordinate_units`; do not assert yard distances without verifying the source coordinate scale.
- Keep the killing blow distinct from the inferred primary cause. Preserve confidence, provenance, and reasons in any generated explanation.
- Never infer an avoidable mistake, failed defensive, healer fault, or missing mechanic solely from absent data.
- Rankings default to 10 and cap at 50. Death context defaults to 10 seconds and caps at 30.
- Replays cap at 30 seconds, 100 actors, and a minimum 100 ms sample interval. Combined answers use a pre-death window and 100 ms replay sampling.
- Death lists and damage/healing context collections cap at 500; recorded mechanics cap at 500; fight comparisons cap at 20 fights. Observe truncation metadata.
- Live updates default to 100 and cap at 500; update history is bounded to 5,000 records. Scheduled timelines return up to 500 events.
- Errors use `{"error":{"code":"...","message":"..."}}`, sometimes with additional details.
- Handle `ANALYSIS_REQUIRED`, `REPORT_NOT_FOUND`, `FIGHT_NOT_FOUND`, `DEATH_NOT_FOUND`, `AMBIGUOUS_PLAYER`, `AMBIGUOUS_DEATH`, `LIVE_SOURCE_NOT_CONFIGURED`, `LIVE_SOURCE_UNAVAILABLE`, `NO_ACTIVE_ENCOUNTER`, `CURSOR_EXPIRED`, and `INVALID_PARAMETER` where applicable. Some ambiguity errors come from direct service selection rather than every HTTP route.
- HTTP death detail/answer routes require a death ID. Resolve player and death ambiguity before calling them.
- On `CURSOR_EXPIRED`, refresh current state and recover using the returned oldest cursor; define how skipped notifications are treated.
- Scope live cursor tracking and deduplication to the live session; a restarted service is a new context.

## Proposed shared voice/Discord flow

This section is proposed integration work, not implemented endpoints.

1. Receive Discord text or a voice transcript.
2. Resolve the requested source, report/fight or live session/attempt, player, and death. Persist a Discord-user-to-character mapping for questions such as "Why did I die?"
   When the user supplies a report URL that is not prepared, use the existing backend retrieval and analysis pipeline through a new adapter/job interface before querying it. This interface is proposed work; no such HTTP route currently exists.
3. If multiple candidates remain, ask a focused follow-up. Do not silently choose a different pull or similarly named player.
4. Fetch the combined `/answer` payload once. Use its encounter identity and evidence for both output channels.
5. Use `answer.text` directly for a deterministic baseline, or build a richer explanation from `analysis` while retaining uncertainty. Add speech-specific phrasing in the adapter; there is no `speech_text` field currently.
6. Decode the SVG and render it to PNG in an image adapter. Preserve alt text and identify unavailable or stale imagery. Keep base64 image data out of text-only prompts and logs.
7. Send text and the image together through the Discord adapter. Generate a concise spoken response from the same facts through the voice adapter.
8. Record the resolved encounter/death identity and delivery result so retries do not duplicate messages or speech.

For proactive notifications, poll `/live/updates` using cursors, deduplicate by session and event identity, and fetch detailed evidence only as needed. Define rate limits, delivery queues, cancellation behavior, and user opt-in for unsolicited voice callouts.

## Codex review and implementation checklist

Review the current code before implementing the adapters. Do not assume this document creates routes or transport capabilities.

- Verify every documented route and response field against `analysis_api.py`, `analysis_service.py`, and tests.
- Preserve this codebase's ownership of log retrieval and analysis. Reuse the existing importer, local follower, parser, persistence, and query services. Add retrieval/analysis job endpoints or direct service orchestration only where the Discord/voice workflow needs them, with progress, bounded concurrency, failure recovery, and tests; do not duplicate these systems in the bot.
- Review death correlation and spatial evidence in `encounter_parser.py`, including missing coordinates, stale samples, floor changes, post-death events, and coordinate scale.
- Review SVG bounds, overlapping labels, actor identity/class presentation, stale victim positions, and image accessibility. Determine whether PNG rendering belongs in the API or the bot adapter.
- Define a typed shared answer contract for text, voice, visual availability, evidence references, confidence, and source/session/attempt identity. Version additions deliberately.
- Add archived live-attempt death lookup/answer support if "last pull" must work after the next encounter begins. Keep stable encounter identity throughout resolution and delivery.
- Implement intent routing for death explanations, damage/healing rankings, player performance, fight summaries, and mechanic questions. These data endpoints do not currently provide generated narrative answers outside death answers.
- Implement Discord identity mapping, command handling, message formatting, attachments, and retry/deduplication behavior.
- Choose and integrate speech recognition and speech synthesis separately from log analysis. Confirm current provider and Discord voice requirements from official documentation during implementation.
- Define deployment connectivity, authentication for any remote access, secrets handling, channel permissions, and access to state-changing timer operations.
- Validate cursor rollover/session changes and distinguish unavailable live sources from an encounter that simply has not started.
- Test ambiguity, missing images, stale/inferred evidence, multiple deaths, previous pulls, partial data, connection failure, restart recovery, and text/voice consistency.
- Use end-to-end fixtures to verify one user request produces one explanation and the matching image without mixing pulls.

## Validation reference

The preceding implementation work passed 13 targeted tests with:

```powershell
python -m unittest test_analysis_api test_live_log_service
```

Coverage includes combined answer HTTP retrieval, image generation, missing positions, floor/stale/future sample filtering, escaped labels, and single-snapshot live answer assembly. This does not establish Discord delivery, voice behavior, PNG conversion, or remote deployment correctness. Re-run the relevant tests after implementation changes.
