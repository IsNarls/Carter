# WoW Combat Log Viewer

A small Python desktop viewer for archived or actively written WoW combat logs.
No third-party packages are required; Python with Tkinter is enough.

For the combined GUI, run:

```powershell
python wow_log_gui.py
```

Choose a log and then open either the live/replay event viewer or the encounter
timeline exporter. Timeline scanning and analysis run in the background, so the
interface remains usable while reading large archives.

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

For reusable credentials, edit the blank `.env` file beside the scripts:

```dotenv
WARCRAFT_LOGS_CLIENT_ID=your_client_id
WARCRAFT_LOGS_CLIENT_SECRET=your_client_secret
```

The GUI reads this file when the import window opens. Process environment
variables with the same names take precedence. `.env` and downloaded
`*.wcl.json` caches are excluded by the included `.gitignore`.

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

Start the localhost server:

```powershell
python analysis_api.py
```

The default address is `http://127.0.0.1:8765/api/v1`. A different bind address
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
- `GET .../rankings/damage?limit=10` and `GET .../rankings/healing?limit=10`
- `GET .../mechanics?start=0&end=60&ability_id=123`
- `GET /mechanic-drafts/{draft_id}/timer-export`
- `POST /reports/{report_id}/mechanics/compare`
- `GET .../fights/{fight_id}/deaths` and `GET .../deaths/{death_id}`
- `GET .../deaths/{death_id}/replay?before=8&after=2&resolution_ms=500`
- `POST /reports/{report_id}/deaths/compare`

Comparison requests use JSON such as:

```json
{"fight_ids":[6,7,8],"ability_id":1282512}
```

Example calls:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/reports
Invoke-RestMethod http://127.0.0.1:8765/api/v1/reports/ZPBVDXhxLKFYv2jC/fights/6/summary
Invoke-RestMethod http://127.0.0.1:8765/api/v1/reports/ZPBVDXhxLKFYv2jC/fights/6/deaths
```

Responses include schema version and stable report/fight/player/death IDs.
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

### Live local log API

The same server can follow the locally written WoW combat log. The path is an
owner-controlled startup option and cannot be supplied through an HTTP request:

```powershell
python analysis_api.py --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt"
```

To load a custom placeholder schedule, add:

```powershell
python analysis_api.py --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
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

Live endpoints are:

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
- `GET /api/v1/live/players?query=name`
- `GET /api/v1/live/players/{player_id}`
- `GET /api/v1/live/rankings/damage?limit=10`
- `GET /api/v1/live/rankings/healing?limit=10`
- `GET /api/v1/live/mechanics?start=0&end=60&ability_id=123`
- `GET /api/v1/live/deaths?player_name=name`
- `GET /api/v1/live/deaths/{death_id}?seconds_before=10`
- `GET /api/v1/live/deaths/{death_id}/replay?before=5&after=0&resolution_ms=100`
- `GET /api/v1/live/updates?since=0&limit=100`

For example:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/v1/live/status
Invoke-RestMethod http://127.0.0.1:8765/api/v1/live/summary
Invoke-RestMethod "http://127.0.0.1:8765/api/v1/live/updates?since=120&limit=100"
```

Clients poll `updates` with the last received cursor and use the other endpoints
for normalized snapshots. Update history is bounded to 5,000 records; an older
cursor returns `CURSOR_EXPIRED` with the oldest available cursor. Update results
default to 100 and cap at 500. Live death replay defaults to the five seconds
before death. Stored and live APIs share the ranking, death-context, replay, and
mechanic limits documented above.

Live local logs usually do not identify a killing blow directly. The API marks
the last correlated damage event as `inferred`, keeps it separate from the
strongest recent primary cause, and includes the deterministic reason. Missing
positions, health snapshots, debuffs, or absorbs remain absent rather than being
invented.

Reviewed timer exports can be loaded when the live API starts:

```powershell
python analysis_api.py `
  --live-log "D:\World of Warcraft\_retail_\Logs\WoWCombatLog.txt" `
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
python analysis_api.py --live-log simulated-WoWCombatLog.txt
```

Append a fast simulated pull in the second:

```powershell
python simulate_live_encounter.py simulated-WoWCombatLog.txt --duration 25 --delay 0.25
Invoke-RestMethod http://127.0.0.1:8765/api/v1/live/timeline
```

Use `--kill` to produce a successful result. Without it, the simulator produces
a wipe. The timestamp advances in five-second steps, while `--delay` controls how
quickly the test is written in real time.

## Learning conservative mechanic timer drafts

`mechanic_timer_learner.py` is an offline analysis module for repeated Warcraft
Logs pulls. It reads the downloaded cache only when explicitly run; normal API
requests continue to use compact metadata and sidecars.

For several reports, choose **Cross-report timer learner** in the main GUI. The
window remains idle until **Scan reports** is selected. After scanning, choose an
encounter group and explicitly run **Learn merged timer draft**. Each selected
report is then interpreted in its own worker process, up to five workers, and
the compact evidence is merged deterministically into one timer draft. If a
restricted runtime cannot create worker processes, the learner falls back to a
concurrent thread pool. Downloads remain sequential to avoid unnecessary API
rate-limit pressure.

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
