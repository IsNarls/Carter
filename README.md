# Carter

Carter is an independent Discord voice assistant with Warcraft Logs analysis. It keeps Discord, voice receive, wake-word handling, and Gemini/OpenAI sessions in the Carter process while a separate local analysis service owns Warcraft Logs downloads, storage, deterministic calculations, and image artifacts.

Armory is not a dependency and must not be imported, installed, or modified for this deployment.

## Architecture

Run the two processes separately:

1. `log_backend/analysis_api.py` listens on loopback and owns Warcraft Logs credentials and data.
2. `main.py` connects to Discord and calls the analysis API at `http://127.0.0.1:8765/api/v1`.

Keep the analysis API bound to `127.0.0.1`. Carter rejects an insecure remote analysis URL by default. Do not put Warcraft Logs credentials in Carter's root `.env`; they belong only in `log_backend/.env`.

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
test -e log_backend/.env || install -m 600 log_backend/.env.example log_backend/.env
chmod 600 .env log_backend/.env
```

Never print, paste into logs, or commit either `.env` file.

### Carter configuration

Set Discord and provider values privately in the root `.env`. At minimum, configure:

```dotenv
CARTER_ENABLED=true
CARTER_DISCORD_TOKEN=
CARTER_ALLOWED_USER_IDS=
CARTER_AI_PROVIDER=gemini
CARTER_GEMINI_API_KEY=
CARTER_GEMINI_MODEL=gemini-3.8-live
CARTER_WAKE_WORD_ENABLED=true
CARTER_WAKE_WORD=Carter
CARTER_ALLOW_OPENAI_FALLBACK=false

CARTER_AUTO_JOIN=false
CARTER_ANALYSIS_ENABLED=true
CARTER_ANALYSIS_API_URL=http://127.0.0.1:8765/api/v1
CARTER_ANALYSIS_TIMEOUT=15
CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE=false
CARTER_ANALYSIS_LOG_CHANNEL_INGEST=true
```

Slash commands are registered globally for every server that has installed Carter, and Discord may take time to propagate a new global command set. `CARTER_DISCORD_GUILD_ID` is optional and now serves only to clear stale commands that an older Carter version registered directly in that one guild.

Keep `CARTER_AUTO_JOIN=false`: a saved channel is a selection, not permission to connect automatically. Keep `CARTER_ALLOW_OPENAI_FALLBACK=false` unless OpenAI fallback is explicitly wanted and configured.

### Analysis service configuration

Put only the Warcraft Logs OAuth client credentials in `log_backend/.env`:

```dotenv
WARCRAFT_LOGS_CLIENT_ID=
WARCRAFT_LOGS_CLIENT_SECRET=
```

The backend importer reads those values from its process environment or `log_backend/.env`. The Carter process does not need them and the root `.env.example` intentionally does not define them.

## Start Carter

Start the analysis service first, from terminal 1:

```bash
cd ~/Carter/carter/log_backend
../.venv/bin/python analysis_api.py --storage ./data --host 127.0.0.1 --port 8765
```

Then start Carter from terminal 2:

```bash
cd ~/Carter/carter
.venv/bin/python main.py
```

Do not start a second `main.py` process if Carter is already running. Carter should connect to Discord without joining a voice channel. Use `/logs health` to verify the loopback service from Discord.

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
- `/carter activate` explicitly starts an authorized conversation.
- `/carter stop` ends the AI conversation while Carter remains in voice and rearms wake listening.
- `/carter leave` ends the session and disconnects from voice.
- `/carter ask` sends a text question and returns the answer in Discord chat.

Selections are isolated by guild and survive Carter restarts. Carter still remains disconnected until `/carter join` or the supported voice-session command is invoked.

### `/logs`

- `/logs health` checks Carter's connection to the local analysis service.
- `/logs reports` and `/logs fights` browse imported reports and encounters.
- `/logs select` stores the guild's current report/fight context.
- `/logs source` selects stored analysis or the current/an explicitly numbered archived live attempt.
- `/logs summary` returns a deterministic encounter summary.
- `/logs ranking` returns calculated DPS or HPS rankings.
- `/logs player` returns one player's data for the selected fight or pull.
- `/logs death` explains one selected player's death and attaches its matching position diagram when available.
- `/logs deaths` lists the selected pull's deaths in fight-relative timestamp order.
- `/logs identity` maps the invoking Discord user to a character name for “Why did I die?” requests.
- `/logs import` starts a report import or refresh job.
- `/logs job` reports the status and outcome of an import job.

Discord option descriptions are the source of truth for each command's required filters. Use `/logs reports` and `/logs fights` before selecting when a report, boss, difficulty, or player name is ambiguous.

## Report ingestion and import jobs

When `CARTER_ANALYSIS_LOG_CHANNEL_INGEST=true`, Carter scans messages from the guild's selected logs channel for `warcraftlogs.com/reports/...` URLs, including supported embed and forwarded-message content. It submits discovered report codes to the local service rather than downloading Warcraft Logs data itself.

Imports are jobs because a complete report can require several API requests. `/logs import` acknowledges the request, and `/logs job` exposes progress or a safe error. Importing the same report again refreshes its stored fights so a live raid log can gain new pulls without creating a separate logical report. Backend storage under `log_backend/data` persists across Carter restarts; do not commit or expose that runtime directory.

Warcraft Logs rate limits, an incomplete live upload, or missing table coverage can leave a job partial. Carter must report that state instead of treating missing rows as zero.

## Deterministic analysis semantics

Raid-log calculations are performed by the analysis service, not invented by Gemini:

- DPS is qualifying boss damage divided by the selected encounter duration.
- HPS is qualifying effective player healing divided by the selected encounter duration.
- A DPS ranking is ordered by DPS, not by raw damage done. Raw totals can be shown as supporting data.
- Pet contribution is attributed to its known player owner; NPCs are not ranked as players. Missing or partial source coverage is distinct from a real zero.
- Character identity is guild scoped and uses stable source identity where available. `/logs identity` is used when a name is ambiguous.

`/carter ask` resolves supported log questions through the deterministic backend and grounds the English response in the returned result. It must say when a query cannot be resolved; it must not estimate a ranking, death, fight, or timestamp. This path is suitable for chat even when no voice session is active.

During an authorized voice conversation, Gemini Live can call the same analysis operations as tools. The model handles natural-language intent and wording, while the backend selects stored data and performs calculations. Tool output remains guild scoped. Local wake listening does not open a Gemini conversation or consume Gemini usage until the wake word is detected or `/carter activate` is used.

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

- The backend starts on `127.0.0.1:8765`, `/logs health` succeeds, and it is not reachable from another host.
- Carter starts once, registers commands, and does not autojoin voice.
- `/carter setvoice` and `/carter setlogs` save different guilds' selections independently; a Carter restart restores the selections but remains disconnected.
- `/logs import` accepts a known report, `/logs job` reaches a successful or explicitly partial terminal state, and re-importing a live report adds new fights.
- `/logs reports`, `/logs fights`, `/logs source`, and `/logs summary` resolve the intended source and selected fight.
- `/logs ranking` reports top DPS for the selected encounter rather than sorting by raw damage; HPS uses the selected encounter duration.
- `/logs deaths` returns an ordered last-wipe timeline, and `/logs death` attaches a matching timestamped position diagram when the report supports one.
- `/carter ask` answers a supported raid-log question in English with deterministic stored data and declines unsupported/ambiguous requests safely.
- `/carter join` joins the saved channel and `/carter status` reports the wake listener armed.
- Ordinary speech without “Carter” does not start a Gemini conversation or trigger analysis tools.
- Saying “Carter” starts one authorized Gemini Live conversation, and a log question invokes the local analysis tool successfully.
- `/carter stop` ends AI activity, leaves Carter connected, and rearms wake listening; `/carter leave` disconnects.
- A user not listed in `CARTER_ALLOWED_USER_IDS` cannot configure, activate, query, or control Carter.

Do not create, install, or enable a systemd service until this live checklist succeeds. After it succeeds, service units can be prepared for review, but they should not be enabled automatically.

## Security notes

- Never commit `.env`, `log_backend/.env`, tokens, API keys, OAuth secrets, or backend runtime data.
- Keep the analysis API on loopback and keep `CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE=false`.
- Keep `CARTER_ALLOW_OPENAI_FALLBACK=false` unless fallback was explicitly approved.
- Rotate any credential that has appeared in a terminal transcript, chat, issue, or commit.
- Safe diagnostics include dependency status, test outcomes, process state, job state, and missing variable names—never secret values.
