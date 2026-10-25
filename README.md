# Carter

Carter is an independent Discord voice assistant with Warcraft Logs analysis. It keeps Discord, voice receive, wake-word handling, and Gemini/OpenAI sessions in the Carter process while a separate local analysis service owns Warcraft Logs downloads, storage, deterministic calculations, and image artifacts.

Armory is not a dependency and must not be imported, installed, or modified for this deployment.

## Architecture

The implementation remains split into two processes for isolation, but Carter owns
the normal lifecycle:

1. `main.py` starts and supervises `log_backend/analysis_api.py` on loopback.
2. Carter connects to Discord and uses that service's API at `http://127.0.0.1:8765/api/v1`.
3. The analysis child owns Warcraft Logs retrieval, credentials, persisted data, and calculations.

Every Carter data request includes the current Discord guild ID. The backend
requires that scope and stores each guild under
`log_backend/data/guilds/GUILD_ID/`; report catalogs, import jobs, retained
reports, analyses, and live-log data are not shared between guilds.

If a healthy service is already listening at the configured loopback endpoint,
Carter reuses it and does not claim ownership or stop it. Otherwise Carter starts
one child, waits for `/health`, restarts it after a failed health check, and stops
only that owned child during Carter shutdown.

Keep the analysis API bound to `127.0.0.1`. Carter rejects an insecure remote analysis URL by default. Carter and the importer use the repository-root `.env`; the importer reads only its Warcraft Logs credential names from that file.

If a Windows log follower and the Debian bot must run on separate hosts, do not expose the backend's unauthenticated HTTP listener directly. Put it behind a private VPN or an authenticated TLS reverse proxy, restrict it with host firewalls, use an `https://.../api/v1` Carter URL, and leave `CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE=false`. Loopback remains the recommended deployment.

The selected voice and logs channels are persisted per Discord guild. Restoring a selection does **not** make Carter join voice on boot. A user must explicitly invoke a join/session command after every process start.

## Debian prerequisites

Carter requires Python 3.11 or newer. Voice and rendered analysis artifacts also need FFmpeg, Opus, and Cairo runtime libraries.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libopus0 libcairo2
python3 --version
```

Create or repair the virtual environment from the repository root, then install the repository requirements:

```bash
cd ~/Carter/carter
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python setup_wakeword.py
```

If your checkout is elsewhere, use its actual path instead of `~/Carter/carter`.

## Private configuration

Create each private file only when it is absent. These commands do not overwrite an existing configuration:

```bash
test -e .env || install -m 600 .env.example .env
chmod 600 .env
```

Never print, paste into logs, or commit the `.env` file.

### Carter configuration

Set Discord and provider values privately in the root `.env`. At minimum, configure:

```dotenv
CARTER_ENABLED=true
CARTER_DISCORD_TOKEN=
CARTER_ALLOWED_USER_IDS=
CARTER_AI_PROVIDER=gemini
CARTER_GEMINI_API_KEY=
CARTER_GEMINI_MODEL=gemini-3.8-live
CARTER_GEMINI_TEXT_MODEL=gemini-3.5-flash-lite
CARTER_GEMINI_TEXT_THINKING_LEVEL=minimal
CARTER_GEMINI_TEXT_TIMEOUT=20
CARTER_WAKE_WORD_ENABLED=true
CARTER_WAKE_WORD=Carter
CARTER_ALLOW_OPENAI_FALLBACK=false

CARTER_AUTO_JOIN=false
CARTER_ANALYSIS_ENABLED=true
CARTER_ANALYSIS_API_URL=http://127.0.0.1:8765/api/v1
CARTER_ANALYSIS_TIMEOUT=15
CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE=false
CARTER_ANALYSIS_LOG_CHANNEL_INGEST=true
CARTER_ANALYSIS_MANAGE_BACKEND=true
CARTER_ANALYSIS_STARTUP_TIMEOUT=15
CARTER_ANALYSIS_HEALTH_INTERVAL=15
```

Voice and chat use separate Gemini models. `CARTER_GEMINI_MODEL` remains the
Live voice model. Generic `/carter ask` requests use the low-latency text model,
thinking level, and bounded timeout above. Text diagnostics log only the model,
attempt number, elapsed time, safe result code, and retry decision; prompts,
responses, and credentials are never logged.

Slash commands are registered globally for every server that has installed Carter, and Discord may take time to propagate a new global command set. `CARTER_DISCORD_GUILD_ID` is optional and now serves only to clear stale commands that an older Carter version registered directly in that one guild.

Keep `CARTER_AUTO_JOIN=false`: a saved channel is a selection, not permission to connect automatically. Keep `CARTER_ALLOW_OPENAI_FALLBACK=false` unless OpenAI fallback is explicitly wanted and configured.

### Analysis service configuration

Put the Warcraft Logs OAuth client credentials in the repository-root `.env`:

```dotenv
CARTER_WARCRAFT_LOGS_CLIENT_ID=
CARTER_WARCRAFT_LOGS_CLIENT_SECRET=
```

The backend importer reads those two values from the root file for both managed and standalone backend runs. Standard process variables named `WARCRAFT_LOGS_CLIENT_ID` and `WARCRAFT_LOGS_CLIENT_SECRET` remain supported and take precedence. No Discord token, AI key, or OAuth value is included in report caches or diagnostics.

## Start Carter

Start Carter with one command. With `CARTER_ANALYSIS_MANAGE_BACKEND=true`, Carter
starts the analysis service automatically before connecting to Discord:

```bash
cd ~/Carter/carter
.venv/bin/python main.py
```

Do not start a second `main.py` process if Carter is already running. Carter should connect to Discord without joining a voice channel. Use `/logs health` to verify the loopback service from Discord.

Manual backend deployment remains an explicit opt-out for development or remote
service layouts: set `CARTER_ANALYSIS_MANAGE_BACKEND=false`, start the configured
service separately, and keep its URL protected. The default Debian deployment
does not require a second command.

As an alternative to terminal 2, launch the desktop tools with:

```bash
.venv/bin/python log_backend/wow_log_gui.py
```

The **Carter service** panel can start, stop, or restart Carter and its owned
analysis child. It
uses the same single-instance lock as `main.py`, records only the process ID in
that lock, refuses to signal an unverified process, and writes Carter output to
the ignored private file `.carter_data/gui-carter.log`. Backend output is written
to `.carter_data/analysis-backend.log`, also with owner-only permissions. Stop
first requests graceful cleanup; Carter then stops only its owned analysis child.
If Carter remains alive after 15 seconds, the GUI controller terminates only the
already verified Carter PID.

The **Live diagnostics console** tails those two files in separate Carter and
analysis-backend tabs. It keeps a bounded recent window and provides **Copy
selected**, **Copy all**, **Clear view**, and **Reload recent** controls for
debugging. Clear view does not delete the files. Common token, API-key, secret,
password, authorization-header, and Discord-webhook credential shapes are
redacted before text is displayed or copied. Carter processes started outside
the GUI continue writing to their original terminal rather than the Carter tab.

The panel can also generate or verify the live-log pipeline access token after a
Discord guild ID is selected. Its raw value is stored only in that guild's
`log_backend/data/guilds/GUILD_ID/live-analysis/.live-access-token` with
owner-only permissions. The GUI and live status API show only a SHA-256
fingerprint. The authenticated pipeline endpoint requires the raw token but
never returns or logs it.

To stream a game client's appended combat-log lines into Carter, securely copy
that guild's token out of band into a private file outside the checkout on the
game machine. The sender uses only the Python standard library; the game machine
does not need Carter's full requirements, provider keys, Discord token, or
`.env`. Run:

```bash
python log_backend/live_log_pipeline.py --log "/path/to/WoWCombatLog.txt" \
  --url https://carter.example --guild-id DISCORD_GUILD_ID \
  --token-file "/private/path/carter-live-token" \
  --state "/private/path/carter-live-state.json"
```

The pipeline starts at the end of a new existing file by default, follows only
complete lines, batches uploads, and saves a non-secret versioned byte cursor.
It resumes the same logical stream after a restart and gives a replaced or
truncated log a new stream identity, so byte offsets cannot collide. It refuses
HTTP redirects rather than risk forwarding the token. Use `--from-start` only
for an intentional initial historical pass.

`--url` accepts either the HTTPS origin or its `/api/v1` base. Plain HTTP is
accepted only for loopback unless `--allow-insecure` is explicitly used on an
encrypted, trusted private network. An SSH local-forward to the backend's
loopback listener is also suitable. If a reverse proxy is used, expose only
`POST /api/v1/live/ingest`; the other analysis routes are not independently
internet-authenticated. Keep the backend bound to loopback, do not expose port
8765 directly, and do not disable TLS verification.

Each upload is scoped to one Discord guild, authenticated with that guild's
pre-provisioned token, and limited to 512 KiB. Invalid authentication cannot
create new guild storage. Carter feeds accepted lines into the existing live
encounter parser, persists the active pull across a backend restart, and makes
the result available to its normal live-analysis tools.

For a normal terminal run, press `Ctrl+C` once and allow cleanup to finish. If voice receive cleanup stalls, wait for the timeout before interrupting again.

## Discord setup and permissions

The Discord application needs the `bot` and `applications.commands` scopes. Grant only the permissions Carter uses, including viewing channels, sending messages, reading message history, connecting, speaking, and using voice activity.

Enable the privileged **Message Content Intent** when selected log channels contain report URLs in message text or forwarded/embed snapshots. Carter also needs `View Channel` and `Read Message History` in the selected logs channel.

Only IDs in `CARTER_ALLOWED_USER_IDS` may configure, activate, or control Carter. Keep that list explicit and test unauthorized access before deployment.

## Commands

### `/carter`

- `/carter setvoice` stores this guild's selected voice channel without requiring the caller to be in voice.
- `/carter setlogs` stores this guild's selected logs channel and enables that channel as the source of report links.
- `/carter join` joins the saved voice channel and arms local wake listening.
- `/carter voicechannel` is the convenience command that accepts both channels, saves them, and connects; the caller must already be in the selected voice channel.
- `/carter status` reports connection, selected channels, session state, and whether the wake listener is armed.
- `/carter activate` explicitly starts one authorized request/response exchange.
- `/carter stop` ends the current exchange while Carter remains in voice and rearms wake listening.
- `/carter replay` replays Carter's latest generated voice response for up to ten minutes without contacting Gemini or consuming AI tokens. Only Carter's output is retained in memory; microphone audio is never stored, and the replay is cleared on leave or restart.
- `/carter leave` ends the session and disconnects from voice.
- `/carter ask` sends a text question and returns the answer in Discord chat.

Selections are isolated by guild and survive Carter restarts. Carter still remains disconnected until `/carter join` or the supported voice-session command is invoked.

### `/logs`

- `/logs health` checks Carter's connection to the local analysis service.
- `/logs reports` and `/logs fights` browse only this server's imported reports and encounters. The unfiltered fight list is ordered by each encounter's actual UTC timestamp, newest first, across all retained reports.
- `/logs select` stores the guild's current report/fight context.
- `/logs source` selects stored analysis or the current/an explicitly numbered archived live attempt.
- `/logs summary` returns a deterministic encounter summary.
- `/logs ranking` returns calculated DPS or HPS rankings.
- `/logs player` returns one player's data for the selected fight or pull.
- `/logs death` explains one selected player's death and attaches its matching position diagram when available.
- `/logs deaths` lists the selected pull's deaths in fight-relative timestamp order.
- `/logs identity` maps the invoking Discord user to a character name for “Why did I die?” requests.
- `/logs ingest <channel>` saves the selected logs channel, scans recent history newest-first, and queues no more than five unique reports for local download and storage.
- `/logs import` reuses a complete local report by default. Set its `refresh` option to `true` only when a live report has gained new pulls and must be fetched again.
- `/logs job` reports the status and outcome of an import job.

Discord option descriptions are the source of truth for each command's required filters. Use `/logs reports` and `/logs fights` before selecting when a report, boss, difficulty, or player name is ambiguous.

## Report ingestion and import jobs

When `CARTER_ANALYSIS_LOG_CHANNEL_INGEST=true`, Carter scans new messages from the guild's selected logs channel for `warcraftlogs.com/reports/...` URLs, including supported embed and forwarded-message content. Use `/logs ingest <channel>` for an explicit historical scan even when passive monitoring is disabled. Each invocation checks at most 100 recent messages and stops after five unique reports. Carter serializes all of its report imports, processes a historical batch oldest-to-newest, and rejects a second historical batch until the first finishes. The selected channel is persisted per guild, and later report-link messages continue to be ingested automatically when passive monitoring is enabled.

Carter submits discovered report codes to its local analysis service with the
current Discord guild ID. That service downloads the Warcraft Logs data and
persists the prepared report under `log_backend/data/guilds/GUILD_ID`; the
Discord process does not maintain a second downloader or cache. Import
completion or failure is posted safely in the selected channel. A direct
`/logs import` is owned by the server where it was invoked, while passive and
historical channel ingestion are owned by the server containing that channel.

Imports are jobs because a complete report can require several API requests. `/logs import` acknowledges the request, and `/logs job` exposes progress or a safe error only in the same server. Re-ingesting a report that already has a valid complete cache performs local analysis without contacting Warcraft Logs. To update an actively growing report, use `/logs import` with `refresh:true`. Backend storage under `log_backend/data/guilds/GUILD_ID` persists across Carter restarts; do not commit or expose that runtime directory. Each guild retains the five reports with the newest Warcraft Logs raid timestamps, regardless of Discord message or download order.

Downloads checkpoint atomically at a bounded interval and immediately before propagating a network failure or API limit. This avoids repeatedly rewriting an increasingly large report after every event page while retaining resumable progress. Network failures and API limits leave the existing complete cache untouched and retain partial pages in a private `*.download.json` sidecar. A rate limit ends the job as `rate_limited` instead of repeatedly calling the API. Retry the same import later; Carter resumes from the saved fight and page cursor. A successful completion atomically replaces the cache and removes the checkpoint.

Server administrators can run `/logs reset confirm:True` to remove only their
guild's downloaded report caches, metadata, analyses, partial-download
checkpoints (including interrupted atomic temporary files), and retained import-job history. The command refuses to run while
an import is active or queued. Carter settings and other guilds' data are not
affected.

Flat cache files created by an older Carter version directly under
`log_backend/data` are intentionally not assigned to a guild automatically.
Run `/logs ingest` once in each server to establish ownership from that server's
configured logs channel and populate its isolated directory.

Warcraft Logs rate limits, an incomplete live upload, or missing table coverage can leave a job partial. Carter must report that state instead of treating missing rows as zero.

## Deterministic analysis semantics

Raid-log calculations are performed by the analysis service, not invented by Gemini:

- `GET /fights` lists the guild's retained encounters by absolute encounter timestamp, newest first.

- DPS is qualifying boss damage divided by the selected encounter duration.
- HPS is qualifying effective player healing divided by the selected encounter duration.
- A DPS ranking is ordered by DPS, not by raw damage done. Raw totals can be shown as supporting data.
- Pet contribution is attributed to its known player owner; NPCs are not ranked as players. Missing or partial source coverage is distinct from a real zero.
- Character identity is guild scoped and uses stable source identity where available. `/logs identity` is used when a name is ambiguous.

`/carter ask` resolves supported log questions through the deterministic backend and grounds the English response in the returned result. It must say when a query cannot be resolved; it must not estimate a ranking, death, fight, or timestamp. This path is suitable for chat even when no voice session is active.

During an authorized voice exchange, Gemini Live can call the same analysis operations as tools. The model handles natural-language intent and wording, while the backend selects stored data and performs calculations. Tool output remains guild scoped. A spoken request must begin with the configured wake phrase. Carter accepts one request, plays one completed response, closes that AI session, and automatically rearms local wake listening. Follow-up speech is ignored until a new utterance begins with the wake phrase. Local wake listening does not open Gemini or consume Gemini usage until that wake phrase is detected or `/carter activate` is used.

## Death timelines and images

`/logs deaths` returns recorded deaths in chronological order. Fight-relative timestamps identify when each death happened; stored-report answers include the report/fight Warcraft Logs URL. `/logs death` resolves one player and death number, then calls the backend's combined answer endpoint once for its text, evidence, replay, and image.

For a supported death question, the analysis service may also return a schematic position diagram at the death timestamp. Carter converts the matching SVG to PNG and attaches it with the explanation. It is recorded-position evidence, not arena artwork. If usable position data is unavailable, Carter still returns the deterministic explanation and reports why the image is absent; it does not fabricate a frame.

## Offline verification

These checks do not contact Discord, Gemini, OpenAI, or Warcraft Logs:

```bash
.venv/bin/python -m compileall -q main.py session.py providers.py audio.py discord_receive.py wakeword.py wake_listener.py setup_wakeword.py log_backend
.venv/bin/python -m unittest discover -p 'test_*.py'
.venv/bin/python -m unittest discover -s log_backend -p 'test_*.py'
```

Do not expose secrets in test output. Tests should use temporary backend storage, never `log_backend/data` from a live deployment.

## Live verification checklist

Live Discord, provider, and Warcraft Logs checks were **not performed as part of this documentation update**. Before treating the deployment as complete, verify all of the following in a test guild:

- Starting Carter also starts the backend on `127.0.0.1:8765`; `/logs health` succeeds, and the backend is not reachable from another host.
- Stopping Carter stops its owned backend; if Carter reused a pre-existing healthy backend, that external process remains running.
- Carter starts once, registers commands, and does not autojoin voice.
- `/carter setvoice` and `/carter setlogs` save different guilds' selections independently; a Carter restart restores the selections but remains disconnected.
- `/logs ingest` saves the specified channel, finds no more than five unique reports in the newest 100 messages, and leaves downloaded data in that guild's `log_backend/data/guilds/GUILD_ID` directory after restart.
- Reports imported in one guild do not appear in another guild's `/logs reports`, cannot be selected there by report ID, and cannot be queried through chat or voice there.
- `/logs import` accepts a known report, `/logs job` reaches a successful or explicitly partial terminal state, and re-importing a live report adds new fights.
- `/logs reports`, `/logs fights`, `/logs source`, and `/logs summary` resolve the intended source and selected fight.
- `/logs ranking` reports top DPS for the selected encounter rather than sorting by raw damage; HPS uses the selected encounter duration.
- `/logs deaths` returns an ordered last-wipe timeline, and `/logs death` attaches a matching timestamped position diagram when the report supports one.
- `/carter ask` answers a supported raid-log question in English with deterministic stored data and declines unsupported/ambiguous requests safely.
- `/carter join` joins the saved channel and `/carter status` reports the wake listener armed.
- Ordinary speech, or speech that mentions “Carter” after other words, does not start Gemini or trigger analysis tools.
- An utterance beginning with “Carter” starts one authorized Gemini Live exchange, and a log question invokes the local analysis tool successfully.
- After Carter plays its first completed response, Gemini closes and the wake listener rearms; follow-up speech cannot produce another response without a new utterance beginning with “Carter”.
- `/carter stop` ends AI activity, leaves Carter connected, and rearms wake listening; `/carter leave` disconnects.
- A user not listed in `CARTER_ALLOWED_USER_IDS` cannot configure, activate, query, or control Carter.

Do not create, install, or enable a systemd service until this live checklist succeeds. After it succeeds, service units can be prepared for review, but they should not be enabled automatically.

## Security notes

- Never commit `.env`, tokens, API keys, OAuth secrets, or backend runtime data.
- Keep the analysis API on loopback and keep `CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE=false`.
- Keep `CARTER_ALLOW_OPENAI_FALLBACK=false` unless fallback was explicitly approved.
- Rotate any credential that has appeared in a terminal transcript, chat, issue, or commit.
- Safe diagnostics include dependency status, test outcomes, process state, job state, and missing variable names—never secret values.
