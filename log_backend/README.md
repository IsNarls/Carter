# WoW Combat Log Viewer

Windows-local encounter detection can now deliver compact events to
`POST /api/v1/live/events`, independently of bulk-log processing. See
[the companion protocol](COMPANION_PROTOCOL.md) for schemas, Windows bundle,
source selection, lifecycle safety, telemetry and reverse-proxy deployment.
Build the bundle from the project root with
`python -m log_backend.build_companion_bundle`.

A small Python desktop viewer for archived or actively written WoW combat logs.
No third-party packages are required; Python with Tkinter is enough.

For the combined GUI, run:

```powershell
python wow_log_gui.py
```

Choose a log and then open either the live/replay event viewer or the encounter
timeline exporter. Timeline scanning and analysis run in the background, so the
interface remains usable while reading large archives.

When this backend is inside the Carter checkout, the launcher's **Carter
service** panel can start, stop, and restart Carter. It honors Carter's existing
single-instance lock, refuses to stop an unverified PID, and writes subprocess
output to the ignored owner-only `.carter_data/gui-carter.log` file. Carter
normally starts, health-checks, and stops its own loopback analysis child, whose
output is kept in `.carter_data/analysis-backend.log`. A healthy API process
that was started outside Carter is reused and left running when Carter stops.
The launcher's copyable **Live diagnostics console** tails the Carter and backend
files in separate tabs, bounds the displayed history, and redacts common
credential shapes before display. Clearing a console view does not delete its
private log file. Carter started from a terminal still writes to that terminal.

The same panel can prepare the live-log access token in the default integrated
storage directory. Only the token fingerprint is displayed.

## Importing a Warcraft Logs report

Choose **Import Warcraft Logs URL** in the combined GUI to download a public or
unlisted report. Paste either the complete report URL or its report code. Use the
**+** button to add up to five reports to one batch; extra rows can be removed
with **-**. The reports download sequentially, and a failure in one report does
not stop later reports in the batch. A single successful report opens in the
encounter timeline. A batch with multiple successful reports opens the
cross-report timer learner. Enter a Warcraft Logs API client ID and client
secret. API clients can be created at <https://www.warcraftlogs.com/api/clients/>.
Credentials are held only for the current download and are never included in
the report caches.

For reusable credentials, edit the repository-root Carter `.env` one directory above this backend:

```dotenv
CARTER_WARCRAFT_LOGS_CLIENT_ID=your_client_id
CARTER_WARCRAFT_LOGS_CLIENT_SECRET=your_client_secret
```

The GUI and API importer read that root file when an import begins. Standard
process variables named `WARCRAFT_LOGS_CLIENT_ID` and
`WARCRAFT_LOGS_CLIENT_SECRET` take precedence. The root `.env` and downloaded
`*.wcl.json` caches are excluded by the included ignore rules.

To trace slow or repeated API pagination requests, set
`WARCRAFT_LOGS_DOWNLOAD_DEBUG=1` before starting the application. Each page writes
its report/fight, page number, requested cursor, event counts, and request
duration to stderr and, during GUI imports, to the **Warcraft Logs** tab in the
live diagnostics console. Imports queued through Discord's `/logs ingest` also
appear there, sourced from the analysis-backend log. The console shows an
indeterminate bar during configuration/download and encounter-based percentage
progress during analysis. The trace is disabled by default and does not include
credentials or event payloads.

When an imported cache is selected in the timeline window, choose **Rescan WCL
report** to download every fight again and refresh the encounter list. The cache
is replaced only after the complete download succeeds.

The application keeps the five most recently downloaded Warcraft Logs reports.
After a sixth distinct report finishes downloading, the oldest report cache and
its metadata and saved analyses are removed. Each completed download also writes
a small `.meta.json` index so report and fight lists never need to read the full
cache. An analyzed fight is written to a compressed
`warcraft-logs-CODE.analysis` sidecar directory. Opening that unchanged fight
again loads the saved analysis; rescanning the report automatically invalidates
the old result and writes a fresh one after the next analysis.

## Read-only analysis API

The API has no third-party dependencies. It reads only managed
`warcraft-logs-CODE.wcl.json` reports, their lightweight metadata, and completed
compressed analyses. It never accepts a filesystem path and never returns the
raw cache, credentials, or `.env` content. Analyze a fight once in the desktop
application before querying it; a missing result returns `ANALYSIS_REQUIRED`
without parsing the large report during the request.

Except for `GET /health`, every HTTP request must include
`X-Carter-Guild-ID` with a positive Discord guild ID. The server resolves that
scope to `STORAGE/guilds/GUILD_ID/`; reports, import jobs, five-report retention,
analysis sidecars, mechanic drafts, and live data are isolated by guild. The
header is a local routing scope, not internet-facing authentication, so keep the
API on loopback or behind the protected transport described by Carter's main
README.

For standalone backend development, start the localhost server directly:

```powershell
python analysis_api.py
```

An integrated Carter deployment does not need this separate command when
`CARTER_ANALYSIS_MANAGE_BACKEND=true`. The default address is
`http://127.0.0.1:8765/api/v1`. A different bind address
must be selected explicitly with `--host`; `--port` and `--storage` are also
available. Query logic is separately available through
`AnalysisService` in `analysis_service.py`, so another Python application does
not need to run HTTP.

Available endpoints are:

- `GET /health`
- `GET /reports` and `GET /reports/{report_id}`
- `GET /reports/{report_id}/fights` and `GET .../fights/{fight_id}`
- `GET .../fights/{fight_id}/summary`
- `GET .../fights/{fight_id}/players?query=name` and `GET .../players/{player_id}`
- `GET .../rankings/damage?limit=10`, `GET .../rankings/healing?limit=10`,
  `GET .../rankings/dps?limit=10`, and `GET .../rankings/hps?limit=10`
- `GET .../mechanics?start=0&end=60&ability_id=123`
- `GET /mechanic-drafts/{draft_id}/timer-export`
- `POST /reports/{report_id}/mechanics/compare`
- `GET .../fights/{fight_id}/deaths` and `GET .../deaths/{death_id}`
- `GET .../deaths/{death_id}/replay?before=8&after=2&resolution_ms=500`
- `GET .../deaths/{death_id}/answer?seconds_before=10`
- `POST /reports/{report_id}/deaths/compare`

Comparison requests use JSON such as:

```json
{"fight_ids":[6,7,8],"ability_id":1282512}
```

Example calls:

```powershell
$headers = @{ 'X-Carter-Guild-ID' = '123456789012345678' }
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/reports
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/fights
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/reports/ZPBVDXhxLKFYv2jC/fights/6/summary
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/reports/ZPBVDXhxLKFYv2jC/fights/6/deaths
```

Responses include schema version and stable report/fight/player/death IDs. The guild-scoped
`/fights` collection uses absolute encounter timestamps and is sorted newest first across reports.
Errors have a stable `{ "error": { "code": ..., "message": ... } }` shape.
Times are relative to pull and expressed in seconds. Rankings default to 10 and
cap at 50. Death context defaults to 10 seconds and caps at 30. Replays default
to 8 seconds before and 2 after, cap the combined window at 30 seconds, and cap
coordinate frequency at one sample per 100 milliseconds. Mechanics responses
cap at 500 events, death lists and death-context event types cap at 500 entries,
replays cap at 100 actors, and fight comparisons cap at 20 fights. Capped collections
include the applied limit or resolution and truncation metadata.

Death analysis keeps the reported killer and ability when Warcraft Logs supplies
them, correlates the lethal damage event, and independently chooses the strongest
recent event as the likely primary cause. Inferred conclusions carry provenance,
confidence, and a reason. Positions always use the newest sample at or before
death. Distances are computed only between actors on the same map; stale samples
remain labeled with their age and confidence.

For answer generation, use the `/answer` endpoint to receive the narrative and
visual together in one JSON payload. This also works at
`GET /api/v1/live/deaths/{death_id}/answer?seconds_before=10`.
The Python equivalents are `AnalysisService.get_death_answer(report_id, fight_id,
death_id)` and `get_live_death_answer(death_id)`. Live answers capture one timeline
snapshot for consistent analysis, replay, and image evidence.

The response includes `answer.text` (an evidence-based summary), `analysis` (full
death context, including provenance and confidence), `replay` (positions and
mechanics leading up to death), and `image`. When available, `image` contains
`mime_type: "image/svg+xml"`, `encoding: "base64"`, `data`, `width`, `height`,
`alt_text`, map/time, and victim position confidence/sample age. A client can
display it using `src="data:image/svg+xml;base64," + payload.image.data` alongside
`payload.answer.text`, or pass these fields into its answer-generation layer.
There is no external language-model call in this endpoint.

The image is a schematic of recorded movement and actor positions on the
victim's floor, with a red cross at the death position. It uses no samples after
death and omits other actors whose latest position is more than three seconds
old. It does not provide arena artwork or inferred mechanic shapes. If no usable
death position exists, `image.status` is `"unavailable"` with a `reason`; the
answer, analysis, and replay are still returned. The context window defaults to
10 seconds and caps at 30, with replay samples every 100 ms.

### Managed report imports

The server can import and prepare a report without blocking an HTTP request:

- `POST /api/v1/imports` with `{"report_url":"https://www.warcraftlogs.com/reports/CODE"}`
- `GET /api/v1/imports/{job_id}`

The POST returns `202 Accepted` with a bounded background job. Poll its GET route
until `status` is `succeeded`, `partial`, `failed`, or `rate_limited`. Progress, per-fight results,
and stable error codes are returned, but credentials, raw events, report URLs, and
filesystem paths are not. Imports are serialized because cache pruning and fight
analysis share the managed storage directory. At most four jobs may be queued or
running and the newest fifty job records are retained in memory.

Only the backend process reads the root `.env` credential entries; API clients
cannot submit credentials or a destination path. Standard process variables
`WARCRAFT_LOGS_CLIENT_ID` and `WARCRAFT_LOGS_CLIENT_SECRET` can override the
file. Job state is process-local and is not restored after a server restart, while
completed report caches and fight analyses remain on disk.

The optional boolean `refresh` request field defaults to `false`. With the default,
an existing valid cache is analyzed locally without loading credentials or contacting
Warcraft Logs. Use `refresh: true` only for a growing live report. Event downloads are
atomically checkpointed at a bounded interval and whenever a request fails in a private
`*.download.json` sidecar, avoiding a full growing-cache rewrite after every page. A 429
stops the job as `rate_limited`; the next import resumes from the saved fight/page cursor.
Other interrupted downloads retain the same checkpoint and also resume when retried.

### Live local log API

The same server can follow the locally written WoW combat log. The path is an
owner-controlled startup option and cannot be supplied through an HTTP request:

```powershell
python analysis_api.py --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
  --live-guild-id 123456789012345678
```

To load a custom placeholder schedule, add:

```powershell
python analysis_api.py --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
  --live-guild-id 123456789012345678 `
  --mechanic-scripts mechanic_scripts.example.json
```

The bundled generic script is deliberately fictional and every emitted event is
marked `placeholder: true`. Scripts can select an encounter ID and difficulty or
use `null`/`any` as a fallback. Mechanics have a unique ID, nonnegative scheduled
time, and message. This milestone does not claim that these timers describe a
real boss encounter.

By default, ingestion begins at the current end of the file so old encounters
are not replayed. Add `--live-from-start` to process an existing fixture or
archive from its beginning.

The follower runs outside HTTP request threads, retries partial final lines,
detects truncation and replacement, assigns monotonic cursors, and publishes
immutable analysis snapshots. Completed or interrupted pulls are written
atomically under the managed `live-analysis` directory. Requests never parse
the growing source file and responses expose only its filename, never its full
path or raw lines.

Use the desktop launcher's **Generate / verify live token** action for the
intended Discord guild before starting a remote sender. The cryptographically
random value is stored at
`STORAGE/guilds/GUILD_ID/live-analysis/.live-access-token`, reused across
restarts, restricted to owner-only permissions, and never logged or returned.
`/api/v1/live/status` exposes only `access_token_ready` and a fingerprint.
Remote ingestion requires the raw token, guild ID, stream ID, and byte offset on
every request. A failed authentication attempt does not provision a new guild
directory.

The game-machine client is `live_log_pipeline.py`. It has no third-party Python
dependencies and accepts either an HTTPS origin or `/api/v1` base URL. Keep its
token and cursor outside the checkout. It resumes acknowledged bytes, assigns a
new logical stream after file replacement or truncation, retries unavailable
servers, and refuses redirects. The backend restores an in-progress remote pull
from its durable spool after a restart.

```powershell
py -3 live_log_pipeline.py `
  --log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
  --url "https://carter.example" `
  --guild-id "123456789012345678" `
  --token-file "$env:LOCALAPPDATA\Carter\live-token.txt" `
  --state "$env:LOCALAPPDATA\Carter\live-log-state.json"
```

Omit `--from-start` for normal use so a newly selected existing log starts at
its current end. WoW must be actively writing combat-log lines for uploads to
appear.

Keep the API listener on loopback. Reach it through an SSH local forward or a
protected reverse proxy that exposes only `POST /api/v1/live/ingest`; the other
analysis routes are not independently internet-authenticated. Do not publish
port 8765 directly.

Live endpoints are:

- `POST /api/v1/live/ingest` (remote sender; live token required)
- `GET /api/v1/live/status`
- `GET /api/v1/live/timer-module`
- `POST /api/v1/live/timer-module/arm`
- `POST /api/v1/live/timer-module/disarm`
- `GET /api/v1/live/encounter`
- `GET /api/v1/live/summary`
- `GET /api/v1/live/timeline`
- `GET /api/v1/live/mechanic-events?status=TRIGGERED&limit=100`
- `GET /api/v1/live/completed`
- `GET /api/v1/live/completed/{attempt}/summary`
- `GET /api/v1/live/attempts`
- `GET /api/v1/live/attempts/{attempt}/timeline`
- `GET /api/v1/live/attempts/{attempt}/summary`
- `GET /api/v1/live/attempts/{attempt}/players?query=name`
- `GET /api/v1/live/attempts/{attempt}/players/{player_id}`
- `GET /api/v1/live/attempts/{attempt}/rankings/{damage|healing|dps|hps}`
- `GET /api/v1/live/attempts/{attempt}/deaths?player_name=name`
- `GET /api/v1/live/attempts/{attempt}/deaths/{death_id}?seconds_before=10`
- `GET /api/v1/live/attempts/{attempt}/deaths/{death_id}/answer?seconds_before=10`
- `GET /api/v1/live/players?query=name`
- `GET /api/v1/live/players/{player_id}`
- `GET /api/v1/live/rankings/{damage|healing|dps|hps}?limit=10`
- `GET /api/v1/live/mechanics?start=0&end=60&ability_id=123`
- `GET /api/v1/live/deaths?player_name=name`
- `GET /api/v1/live/deaths/{death_id}?seconds_before=10`
- `GET /api/v1/live/deaths/{death_id}/replay?before=5&after=0&resolution_ms=100`
- `GET /api/v1/live/updates?since=0&limit=100`

For example:

```powershell
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/live/status
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/live/summary
Invoke-RestMethod -Headers $headers "http://127.0.0.1:8765/api/v1/live/updates?since=120&limit=100"
```

Clients poll `updates` with the last received cursor and use the other endpoints
for normalized snapshots. Update history is bounded to 5,000 records; an older
cursor returns `CURSOR_EXPIRED` with the oldest available cursor. Update results
default to 100 and cap at 500. Live death replay defaults to the five seconds
before death. Stored and live APIs share the ranking, death-context, replay, and
mechanic limits documented above.

### Observed live callout profiles

The live backend preloads all reviewed `callout_profiles/*.callout.json` files
and any guild-specific files under `data/guilds/GUILD_ID/callout-profiles/`.
Selection is an exact in-memory lookup by the `ENCOUNTER_START` encounter ID and
difficulty. The catalog is tiny, so `ZONE_CHANGE` and `MAP_CHANGE` do not trigger
additional disk loading. Files are checked again when the next encounter starts,
which lets a newly reviewed boss profile take effect without restarting Carter.

Only observed combat-log triggers are supported. Each mechanic declares an exact
event type, spell ID, constrained source policy, duplicate/grouping window,
bounded spoken text, and maximum lateness. The backend emits a protected
`MECHANIC_CALLOUT` update; it never exposes profile evidence, raw combat-log
lines, or executable Lua. Carter drops late callouts both before queueing and
again before playback. Unknown encounters or unmatched difficulties emit no
mechanic callouts and continue using the generic voice lifecycle messages.

The packaged profiles are:

- Nek'zali the Soulcoiler, encounter 3470, difficulties 15 and 17
- Entombed Sentinels, encounter 3445, difficulty 15

Their wording remains provisional. Vashnik the Malignant and The Lost Explorers
currently have only raw DBM evidence, so they intentionally use the unknown-boss
fallback until reviewed JSON profiles are added. The unrelated
`wow-guide-profile/v1` files remain offline timer-learning context and are not
spoken at runtime.

See `callout_profiles/README.md` and copy an existing profile when adding a boss.
Invalid or conflicting files are skipped and listed under `live_callouts.errors`
in `/api/v1/live/status`; valid profiles remain available.

Live local logs usually do not identify a killing blow directly. The API marks
the last correlated damage event as `inferred`, keeps it separate from the
strongest recent primary cause, and includes the deterministic reason. Missing
positions, health snapshots, debuffs, or absorbs remain absent rather than being
invented.

Reviewed timer exports can be loaded when the live API starts:

```powershell
python analysis_api.py `
  --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
  --live-guild-id 123456789012345678 `
  --timer-modules timer-exports
```

The API also uses `timer-exports` under its storage directory automatically when
that directory exists. Only exports with `enabled: true` are loaded. Open
**Fight modules** from the main window and choose **Prepare selected for live
use** to create one explicitly. The timer draft review remains an analysis and
schema-export screen, and ordinary schema exports remain disabled.

Modules are indexed in memory by encounter ID and difficulty. `ENCOUNTER_START`
therefore performs no report analysis or disk loading: it verifies the live
difficulty, selects the matching module, emits the fight-engaged trigger, and
starts its clock. `ENCOUNTER_END` emits the result and cancels pending callouts.

An external voice layer can prepare a particular module before the pull without
being responsible for the authoritative start time:

```powershell
Invoke-RestMethod -Method Post `
  -Headers $headers `
  -ContentType application/json `
  -Body '{"encounter_id":3429,"difficulty":4}' `
  http://127.0.0.1:8765/api/v1/live/timer-module/arm
```

It can instead send `{"timer_id":"..."}`. Arming reloads the configured module
directory before selecting the module, reports `applies_to: next_encounter`, and
does not start the clock. A mismatched live encounter or difficulty cannot use
the armed module. Disarming returns selection to the automatic encounter and
difficulty match.

The placeholder timeline starts only after a valid `ENCOUNTER_START`. It advances
from both a monotonic processing clock and newer combat-log timestamps, which
allows timers to continue during quiet log periods while also catching up after
ingestion delay. Each event keeps scheduled/log time separate from processing
time. A matching `ENCOUNTER_END` records `KILL` or `WIPE`, cancels every remaining
timer, and prevents old-attempt events from firing during the next pull. A new
pull, source reset, or monitor shutdown interrupts and cancels the active script.
The timeline and its canceled diagnostics are persisted with the encounter.

To exercise the complete flow without running WoW, open two terminals. Start the
API on an empty test file in the first:

```powershell
New-Item -ItemType File -Force simulated-WoWCombatLog.txt
python analysis_api.py --live-log simulated-WoWCombatLog.txt `
  --live-guild-id 123456789012345678
```

Append a fast simulated pull in the second:

```powershell
python simulate_live_encounter.py simulated-WoWCombatLog.txt --duration 25 --delay 0.25
Invoke-RestMethod -Headers $headers http://127.0.0.1:8765/api/v1/live/timeline
```

Use `--kill` to produce a successful result. Without it, the simulator produces
a wipe. The timestamp advances in five-second steps, while `--delay` controls how
quickly the test is written in real time.

## Learning conservative mechanic timer drafts

`mechanic_timer_learner.py` is an offline analysis module for repeated Warcraft
Logs pulls. It reads the downloaded cache only when explicitly run; normal API
requests continue to use compact metadata and sidecars.

For several reports, choose **Cross-report timer learner** in the main GUI. The
window lists the selected report caches with their local modification timestamps,
newest first. Use **Add report files...** to add existing `*.wcl.json` files; the
picker only displays that supported cache type. Check the **Use** box for up to
five reports that should contribute pulls, or use **Select all** and **Select
none**. The available-file list can contain more than five reports. The window
remains idle until **Review checked reports** is selected. After scanning, choose
an encounter group and explicitly run **Learn merged
timer draft**. Each checked report is then interpreted in its own worker process,
up to five workers, and
the compact evidence is merged deterministically into one timer draft. If a
restricted runtime cannot create worker processes, the learner falls back to a
concurrent thread pool. Downloads remain sequential to avoid unnecessary API
rate-limit pressure.

### Timed-mechanic guide context

Guide text can be curated into the validated `wow-guide-profile/v1` contract
documented by `guide_profile.schema.json`. The included
`guide_profiles/zuljan-malacrass.guide.json` converts the supplied Zul'jan and
Hex Lord Malacrass guide into four phases and timer-relevant mechanic metadata.
It records static-timer candidates, phase triggers, conditional follow-ups, and
reactive mechanics. Guide damage numbers are deliberately excluded.

The Cross-report Timer Learner automatically selects the included profile when
it is the only available `*.guide.json` file. Use **Choose guide profile...** to
select another profile or **Clear guide** to learn from logs alone. Matching is
by normalized ability name. Guide context annotates accepted and rejected
evidence but cannot change acceptance thresholds, supply spell IDs, invent
pull-relative times, or enable a module. The timer review displays matched guide
context beside log evidence.

For guide mechanics marked as pull-relative static or phase candidates, the
learner uses repeated boss `begincast`/`cast` events even when the mechanic has
no direct damage event. Unguided raid-wide discovery can still use damage events
as evidence, but damage values never enter the generated timer contract.

Validate or use the profile from the command line:

```powershell
python guide_profile.py guide_profiles\zuljan-malacrass.guide.json
python mechanic_timer_learner.py report.wcl.json `
  --encounter-id 3429 `
  --difficulty 4 `
  --guide-profile guide_profiles\zuljan-malacrass.guide.json
```

Effect durations and tick intervals remain descriptive context. Only repeated
combat-log observations can become encounter-relative timer offsets. Runtime
`wow-fight-timer/v1` exports contain triggers, callout times, ability IDs, and
messages; they do not contain damage parameters.

Generate a draft for one encounter and difficulty:

```powershell
python mechanic_timer_learner.py `
  warcraft-logs-ZPBVDXhxLKFYv2jC.wcl.json `
  --encounter-id 3429 `
  --difficulty 4
```

Pass several report cache paths to merge them from the command line. The learner
uses at most five concurrent workers by default; `--workers 2` can lower the
limit on machines with less available memory.

Use repeated `--fight-id` options to restrict the selected pulls, or pass several
report caches to learn across raid nights. The default output is written
atomically under `mechanic-drafts`.

The learner considers only damage sourced by actors identified as bosses and
received by players. Damage events within two seconds are grouped into a burst.
By default, an ability is eligible only when:

- At least three pulls contain usable evidence.
- It appears in at least 80% of pulls that lived long enough to observe it.
- Its median burst affects at least 60% of the raid.
- Its median affected-player damage is at least 15% of maximum health.
- Its first occurrence stays within a two-second timing-deviation limit.
- It provides at least two seconds of actionable warning time.
- Repeated occurrences are emitted only when their interval deviation and
  coefficient of variation also pass conservative limits.

Short wipes are excluded from later-timer coverage when they ended before the
predicted occurrence. They still contribute evidence for mechanics they reached.
Stable overlaps between accepted high-damage abilities are reported separately.

The same workflow is available in the encounter GUI. Open an imported Warcraft
Logs report, scan it, and Ctrl-click or Shift-click at least three attempts from
the same encounter and difficulty. Choose **Learn timer draft** directly, or
choose **Compare mechanics** and then **Run conservative learner** from the
comparison window. Learning runs in the background, writes the draft under
`mechanic-drafts`, and opens a review window with accepted abilities, rejected
candidates and reasons, the proposed schedule, and stable overlaps.

Every output includes accepted and rejected abilities, numeric evidence, and
stable rejection codes. The generated script is marked `draft: true` and
`enabled: false`; it must be reviewed before being copied into an enabled live
script registry. This prevents three coincidental pulls from silently becoming
authoritative encounter instructions.

Learned scripts also include explicit encounter-boundary triggers. A normalized
`ENCOUNTER_START` log event starts the timeline and establishes 0:00. A matching
`ENCOUNTER_END` ends the timeline as a kill or wipe and cancels any warnings that
have not fired. These are dynamic log triggers, so the learner never predicts a
fixed fight end time. The draft records the observed Warcraft Logs fight start
and end boundaries for every source pull, and the review window shows their
evidence separately from scheduled mechanics.

Saved drafts are available through bounded, read-only endpoints:

```text
GET /api/v1/mechanic-drafts
GET /api/v1/mechanic-drafts/{draft_id}
GET /api/v1/mechanic-drafts/{draft_id}/timer-export
```

The timer-export endpoint returns the smaller `wow-fight-timer/v1` runtime
contract rather than the full learner evidence. The same disabled contract can
be written from the timer review window with **Export timer schema...**, or from
the command line:

```powershell
python mechanic_timer_learner.py report.wcl.json `
  --encounter-id 3429 --difficulty 4 `
  --timer-export timer-exports/boss.timer.json
```

The contract is documented by `timer_export.schema.json`. It contains encounter
and difficulty matching, `ENCOUNTER_START` and `ENCOUNTER_END` actions, engage/end
messages, the clock rules, relative callouts, and source provenance. A future bot
can match an incoming live-log start event, emit `triggers.start.emit.message`,
set that event to 0:00, and schedule `callouts[].offset_seconds`. On the matching
end event it emits the end message with `{result}` replaced by `KILL`, `WIPE`, or
`INTERRUPTED`, then cancels pending callouts. Exports retain `enabled: false`
until a consumer explicitly approves them.

These endpoints never initiate analysis of the large report cache. Timer learning
remains an explicit offline command because a report can contain hundreds of
megabytes of events.

The importer downloads every API event for every boss pull, following all event
pages, and writes `warcraft-logs-CODE.wcl.json` beside the application. Once the
download completes, the encounter list opens automatically. Imported encounters
support the same damage/DPS, effective healing/HPS, position replay, boss health,
mechanic display, mechanic comparison, and CSV exports as local text logs. The
cache can also be reopened later with **Browse**. Its data is already downloaded,
so later analysis does not require credentials or a network connection.

The client-credentials API cannot read private reports. Change the report to
public or unlisted before importing it. Imported API caches open in the encounter
timeline; the raw live/replay viewer continues to read local combat-log text.

To view player data, choose **Timeline / player analysis**, click **Scan
encounters**, select a pull, and click **View player analysis**. Use the **Damage**
or **Healing** ranking selector to switch between DPS and effective HPS. Effective
healing includes damage prevented by attributed absorb shields. Double-clicking
an encounter opens the same analysis. CSV export is optional.

Run from this directory:

```powershell
python combat_log_viewer.py
```

Or preselect a log:

```powershell
python combat_log_viewer.py "Archive-WoWCombatLog-040826_194045.txt"
```

Choose a file with **Browse**, select a mode, then click **Start / Restart**:

- **Replay** displays archived events according to their recorded timestamps.
  Speed can be changed during playback. **Skip idle gaps > 5s** jumps over long
  quiet periods; disable it for original timing. Pause and resume as needed.
- **Follow live** watches the selected file for appended lines. **Live: start at
  end** shows only new entries; uncheck it to read the existing contents first.
  For actual live combat, select the file WoW is currently writing, rather than
  one of these archives. Following handles truncation of the same file; if a log
  is renamed or replaced, select the new file and restart.
- The case-insensitive filter matches original incoming lines, including player
  names, spell names, and event types. It affects future entries only. Restart to
  replay from the beginning with a different filter.
- Select a row to inspect the full original line. Source, target, and ability are
  extracted from common combat events; other records remain available as raw text.

Files are streamed rather than loaded into memory. The table retains the latest
2,500 matching events, keeping it practical for the large logs in this folder.
Very dense logs or high speeds may run slower than the requested replay rate to
keep the interface responsive; events are never deliberately dropped from reading.

On this machine, `py` defaults to a missing `J:\python.exe`. Use `python` or
`py -3.12` to select the installed Python 3.12 instead.

## Encounter timelines

List the numbered boss attempts in a log:

```powershell
python encounter_parser.py "Archive-WoWCombatLog-040826_194045.txt"
```

Export one attempt as a one-row-per-second CSV:

```powershell
python encounter_parser.py "Archive-WoWCombatLog-040826_194045.txt" --attempt 3 --output timeline.csv
```

The export contains combined active-boss health, damage dealt during each second,
boss casts, player deaths, and separate health/damage columns for every inferred
boss unit. It handles council and phased encounters by treating a boss as active
while it has recent health observations. Boss units are inferred from hostile
creatures with at least 15% of the largest observed maximum health; this retains
large encounter entities while excluding normal adds.

Health comes from the advanced unit snapshot attached to damage events. It is
therefore an observation from the log rather than a value reconstructed by
subtracting damage. Seconds without recent observations are left blank, and the
damage column uses effective damage (reported amount minus positive overkill).
The field layout targets the `COMBAT_LOG_VERSION,22` format present in these logs.

The GUI and command-line exporter also create a `-players.csv` companion file.
It ranks players by total boss damage, calculates full-fight DPS, and provides
per-second damage, cumulative damage, X/Y coordinates, map ID, and the age of the
last position observation. Recognized pet and guardian damage is assigned to its
owning player. After a GUI export, a player-analysis window opens for browsing the
ranking and each player's second-by-second damage, effective healing, and position
without leaving the app.

Use **View 2D map** after selecting an encounter, or **Open 2D encounter map**
from the player-analysis window, to animate the pull.
Players appear in their WoW class colors and bosses appear as red dots. The map supports playback,
scrubbing, speed controls, player labels, map/floor selection, and an individual
boss-profile selector for council encounters. CSV export also creates a
`-boss-positions.csv` file containing each boss's observed X/Y/map profile.

Map playback interpolates between the surrounding one-second position samples at
roughly 30 frames per second. Player dots use the standard WoW class colors;
unknown or newly introduced specialization IDs use blue. Boss dots remain red.

The map's mechanics panel follows boss spell casts at the current replay time.
It shows casts in progress, recently completed or interrupted casts, the boss,
occurrence number, and the interval since that boss last used the same ability.
The history only reveals mechanics that have occurred by the current replay time
and follows the selected boss profile in multi-boss encounters.

To compare repeated pulls, Ctrl-click or Shift-click two or more attempts of the
same encounter in the encounter list, then choose **Compare mechanics**. The
comparison aligns pulls at 0:00 and draws each attempt only to its recorded kill,
wipe, or incomplete endpoint. Colored markers show mechanic cast starts and cast
lengths. Filters isolate a boss or ability, while the timing table reports pulls
observed, total casts, median/range of the first cast, and median/range of repeat
intervals. Short wipes therefore contribute their available early-fight data
without being treated as complete observations of later phases.
