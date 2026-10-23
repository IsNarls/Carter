# Carter Discord voice prototype

Carter stays in the configured Discord voice channel and listens locally for
an authorized user to say "Carter". It then sends that user's microphone audio
to Gemini Live and plays generated speech in the channel.
It runs independently of Armory-Bot-Development and does not import that bot.

## Setup (Python 3.11+ on Debian/Linux)

```bash
cd /home/derek/Documents/Carter/carter
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python setup_wakeword.py
.venv/bin/python main.py
```

Configure your private `.env` using `.env.example` as the shareable reference.
Set `CARTER_ENABLED=true`, Carter's dedicated Discord token,
comma-separated `CARTER_ALLOWED_USER_IDS`, and `CARTER_GEMINI_API_KEY`. Voice and
logs channels are selected independently in each server with `/carter voicechannel`.
`CARTER_DISCORD_GUILD_ID` is optional and is used only once to remove Carter's
former guild-scoped command during migration to global commands.
Set `CARTER_AI_PROVIDER=gemini` and choose a Gemini Live model available to your
API key; the template uses `gemini-3.8-live`. The model name and key must both be
valid for activation to work. Text chat uses the standard generate-content API
and `CARTER_GEMINI_TEXT_MODEL` (template: `gemini-3.8-flash`) because the native
audio Live model cannot return text. Environment variables override `.env`.
Never paste tokens into chat or commit `.env`.

Wake listening is enabled by default and needs no additional API key. The setup
script downloads the 40 MB `vosk-model-small-en-us-0.15` model once from Vosk's
official server. [Vosk estimates about 300 MB runtime memory for small models](https://alphacephei.com/vosk/models).
Repeat setup on each deployment machine; the bot does not download models at runtime.
The default `CARTER_WAKE_MODEL_PATH=models/vosk-model-small-en-us-0.15` is relative
to Carter's directory. A missing model is reported by `/carter status`; manual
`/carter activate` remains available.

If Carter is already running, stop that process with Ctrl+C, install dependencies,
and restart it to load these changes. Do not launch a second instance.

Create a separate bot in the Discord Developer Portal and invite it using the
`bot` and `applications.commands` scopes. Grant View Channel, Connect, and Speak
in the configured channel. No Members privileged intent is needed. Message Content
is needed only when the optional Warcraft Logs monitor is enabled.
Enable Developer Mode in Discord to copy guild/channel/user IDs.

### Warcraft Logs channel summaries

Optional public-report summaries use the official Warcraft Logs v2 API. Create an
API client at `https://www.warcraftlogs.com/api/clients/`, then privately configure
`CARTER_WARCRAFT_LOGS_CLIENT_ID` and `CARTER_WARCRAFT_LOGS_CLIENT_SECRET`. Set
`CARTER_WARCRAFT_LOGS_ENABLED=true` and enable the **Message Content Intent** for
Carter in the Discord Developer Portal. This intent is only requested when the
feature is enabled.

In that channel, a Warcraft Logs report URL in a normal message, Discord embed,
webhook/bot message, or attachment link receives a
bounded report summary containing report duration and boss pull/kill results.
Carter accepts official Warcraft Logs subdomains but rejects lookalike domains.
The explicitly selected logs channel is the trust boundary; Carter ignores other
sites and channels. It fetches
public report metadata and fights; it does not scrape webpages or send raw combat
events to Gemini. Private reports requiring user authorization are not supported
by the client-credentials integration.
Terminal diagnostics identify scanned guild/channel/message IDs and recognized
public report codes, but never print complete message bodies, embed text, unrelated
URLs, or credential values.
For temporary parsing diagnosis, set `CARTER_LOG_MESSAGE_DEBUG=true`. This prints a
bounded view of message content, embeds, and attachment URLs from only the selected
logs channel, with common credential patterns redacted. Discord content can still
be sensitive, so return it to `false` after troubleshooting.

After channel selection, Carter scans the configured recent-message window
(`CARTER_WARCRAFT_LOGS_SCAN_MESSAGES`, default 5) and deduplicates every Warcraft
Logs report URL found in message content, embeds, components, or forwarded snapshots.
All discovered reports are stored; one private, invalid, or temporarily unavailable
report does not stop the remaining reports. New messages and message edits use the
same ingestion path. The bounded live AI context still keeps the five newest rich
reports, while normalized historical data is retained independently in SQLite.
The newest report remains the default live-report context.
The newest boss pull is the default when a voice question does not name a fight.
Before a voice session, Carter loads bounded Summary, Deaths, Interrupts, and
Dispels tables for that pull, plus pull/kill/duration metadata for up to 60 recent
boss pulls. Ask after waking Carter, for example: “Break down the latest fight.”
Posting the report link again after a minute refreshes an in-progress report.
While a report remains current, Carter also refreshes it every
`CARTER_WARCRAFT_LOGS_REFRESH_SECONDS` (default 60, allowed 30–600). Newly added
or completed fights automatically replace the bounded fight tables used by the
next voice conversation. Refreshes are silent in Discord and each server has its
own live-report monitor. Posting a different report link switches that server's
monitor to the new report.

An authorized user can change both runtime channels without editing `.env`:

```text
/carter voicechannel voice_channel:<voice channel> logs_channel:<text channel>
```

The user must already be in the selected voice channel. Carter validates voice
View/Connect/Speak permissions and log-channel View/Read Message History/Send
Messages permissions before changing anything. The logs-channel selection is
persisted per server; the voice-channel selection remains process-local and Carter
never autojoins voice after restart. Credentials remain only in `.env`. The
command connects and arms Carter but does not open a
Gemini session; say the wake name or use `/carter activate` when ready.

## Use

Only users in `CARTER_ALLOWED_USER_IDS` can run the guild's Carter commands.
The `/carter` command is registered globally and works in every server where the
application is installed with the `bot` and `applications.commands` scopes. Each
server has isolated voice, wake, AI-session, selected-channel, and latest-log state.

| Command | Behavior |
| --- | --- |
| `/carter join` | Join or retry the configured channel and arm local wake listening. You must be in that channel. |
| `/carter activate` | Start one AI session for your microphone. You must be in that channel. |
| `/carter stop` | Close AI and return to local wake listening while remaining in voice. |
| `/carter leave` | Stop AI and wake listening, then disconnect from voice. |
| `/carter status` | Show connection, wake state, AI state, provider, and usage without secrets. |
| `/carter ask prompt` | Send an authorized text prompt to Gemini and post Carter's text response in the channel. |
| `/carter logsdebug [refresh] [report] [fight]` | Show private parsed-data tiles. Select an exact report/fight and optionally refresh it from Warcraft Logs first. |
| `/carter setvoice channel` | Set this server's voice channel without joining it. |
| `/carter setlogs channel` | Persist this server's Warcraft Logs text channel and load its newest reports without joining voice. |
| `/carter voicechannel voice_channel logs_channel` | Select channels for this run, connect, restore the newest log, and arm wake listening. |

Historical Warcraft Logs commands are also global and use only this server's
stored data:

| Command | Behavior |
| --- | --- |
| `/logs scan [messages] [refresh_existing]` | Scan recent channel messages, ingest every unique report, and report safe success/failure counts. |
| `/logs sync` | Refresh the default recent window, including reports already known to Carter. |
| `/logs reports [limit]` | List recent stored reports and source links without rereading Discord. |
| `/logs history player [nights] [realm] [include_wipes] [boss_breakdown]` | Show duration-weighted DPS across raid nights with coverage, optional per-boss rows, trend, and a PNG chart. |
| `/logs compare player [bosses] [realm] [include_wipes]` | Compare the most recent qualifying raid night's bosses and distinguish repeated pulls. |
| `/logs boss player boss [nights] [realm] [difficulty] [include_wipes]` | Compare the same encounter ID and difficulty across raid nights. |

Scans above the normal recent-message window run as one tracked background
backfill per server. The command acknowledges immediately, then posts only safe
completion counts in the invoking channel (or the configured logs channel if
needed), so an expiring Discord interaction does not abandon a large import.
Changing the logs channel or shutting Carter down cancels and awaits that job.

The default comparison filter is kills only; set `include_wipes:true` for progression
analysis. If two stored characters share a name, provide `realm`. DPS summaries use
`sum(damage) / sum(eligible fight time)`, never an arithmetic mean of per-fight DPS.
Coverage includes stored pulls where the player was absent or where the API did not
provide usable damage metrics. Pet/NPC table rows are not treated as player rankings.
Boss-to-boss output is explicitly labeled because mechanics and target counts make
raw DPS values imperfect comparisons.

On every process start Carter remains disconnected from voice. Its saved logs
channel is restored automatically per server and monitoring resumes without a
command. Servers without a saved logs channel are reported in the terminal. Use
`/carter setvoice` when needed, followed by `/carter join` when ready.
`/carter voicechannel` remains a combined configure-and-connect shortcut.
After Carter joins, say "Carter", pause briefly,
and ask your question. The `armed` wake state means
the local listener is ready and AI is inactive. Only `CARTER_ALLOWED_USER_IDS`
can wake Carter; bots and unauthorized users are ignored. Waiting for the name
uses local Vosk recognition and sends no audio to Gemini or OpenAI.

One conversation may be active at a time. Only its initiating user's microphone
feeds AI, while everyone in the channel can hear replies. The name does not
start another session during an active conversation. When the session ends,
Carter remains in voice and returns to listening for its name; each wake starts
a new conversation. `/carter activate` remains a manual alternative.

`/carter ask` does not require Carter to join voice and does not speak its answer.
It creates one short-lived text request using Carter's configured Gemini text model. If a
Warcraft Logs report has been selected for that server, the same latest-report
context used by voice is available to the text prompt. Chat requests are not
stored as conversation history, and only one chat request per server runs at once.
When a chat prompt names an encounter or fight number from the current report,
Carter fetches and caches that specific pull's Summary, Deaths, Interrupts, and
Dispels tables before answering; otherwise the newest pull remains the default.
When a prompt gives a fight-relative timestamp with context—for example,
“What happened at 2:35 in the latest wipe on The Twin Fangs?”—Carter also posts
a PNG positional snapshot for that pull and time. The image is a clearly labeled
Carter schematic built from the official API's latest reported actor coordinates,
with a marker legend and a link to the exact timestamp in Warcraft Logs' interactive
replay. Warcraft Logs does not expose its replay map artwork through the API, so
the PNG uses a neutral grid rather than copying the website's map background.
If position data is unavailable, the text answer still succeeds and Carter posts
the exact interactive replay link. A bare timestamp defaults to the newest pull;
“latest wipe” selects the newest failed pull instead of a later kill.
For questions such as “What was the latest death of Exampleplayer?”, Carter loads
bounded official death events for the indexed reports, resolves the player through
report actor data, maps the newest event to its boss attempt, and reports the exact
UTC date/time plus report and fight IDs.

### Warcraft Logs disk cache and history database

Carter atomically stores each server's bounded public log analysis in
`.carter_data/guild-<server-id>.json`. The cache contains up to five report
metadata sets, the per-boss attempt framework, fetched fight tables, and bounded
death events. It never contains Discord/Gemini/Warcraft Logs credentials, and the
directory is excluded from Git. Cache directories use owner-only permissions and
cache files are written with mode `0600`.

Long-term normalized history is stored separately in
`.carter_data/warcraft-history.sqlite3`. It contains guild-scoped report,
encounter, character-alias, and player-performance rows with uniqueness constraints,
so repeated scans update records instead of duplicating them. It stores damage and
eligible duration separately and derives DPS/HPS during queries. Raid nights are
assigned fight-by-fight using `CARTER_WARCRAFT_LOGS_TIMEZONE` (default `UTC`) and
`CARTER_WARCRAFT_LOGS_RAID_DAY_CUTOFF_HOUR` (default `6`), keeping common
after-midnight pulls with the prior evening. Choose these settings before the first
ingest; Carter rejects silently regrouping an existing database under a different
policy. The database and SQLite sidecar files use owner-only permissions and contain
no Discord, Gemini, or Warcraft Logs credentials. It is excluded from Git.

`CARTER_WARCRAFT_LOGS_BACKFILL_MESSAGES` (default 100, maximum 500) bounds manual
backfills. Warcraft Logs requests use concurrency two, paginate death events, and
retry temporary network failures and 429/5xx responses with bounded backoff. Historical slash
commands query SQLite directly and do not require the original Discord messages or
another Warcraft Logs API request. A normal scan reuses completed stored reports;
partial report/detail/death syncs remain marked for retry and are not mistaken for
player absence. `refresh_existing:true` and `/logs sync` explicitly refresh completed
reports as well. Carter's chat question path can also retrieve a
bounded authoritative historical result before Gemini formats the English response.

At restart the persistent server mapping selects the logs channel and loads this
file immediately, then the live monitor checks Warcraft Logs for updates. Newly added or completed fights
refresh the report, death events, newest-fight tables, framework, and disk file.
Named-fight detail fetched for a chat question is also cached, so later questions
reuse disk-backed data without another detail request. Gemini does not receive
filesystem access; Carter reads the relevant bounded cached section and supplies
it as data context for the prompt.
When channel selection has just occurred, chat waits briefly for the newest-report
restore to finish. If no report context is loaded, Carter directs log questions to
the required channel-selection or report-link step instead of inventing an answer.
Chat retries one transient Gemini timeout, connection interruption, or upstream 5xx
failure; authentication, configuration, quota, and other permanent failures are not retried.

Use `/carter logsdebug` to inspect Carter's parsed state without sending it to
Gemini. The response is ephemeral and shows the selected logs channel, indexed
reports and bosses, recent attempts with report/fight IDs and UTC timestamps,
per-fight table shapes, representative player/metric rows, and death-event totals.
Every kill/wipe label links to its exact Warcraft Logs fight. For example,
`/carter logsdebug report:V8dLCwWZfTX91rYN fight:6 refresh:true` reloads that
report and renders fight 6. `/carter logsdebug refresh:true` refreshes the newest
report. API
credentials, access tokens, and raw API payloads are never included. Each API
parse also writes one bounded terminal summary containing only counts and IDs.

For every boss pull, Carter independently
caches the official API's Summary, Damage Done, Damage Taken, Healing, Casts,
Buffs, Debuffs, Resources, Survivability, Deaths, Interrupts, and Dispels tables,
plus player details and bounded actor/ability master data. Detail requests run
with concurrency two. Completed cached pulls are reused; a live in-progress pull
is refreshed when the report fingerprint changes. Explicit `?fight=6` and
`#fight=6` links select that exact report/fight for chat analysis instead of a
same-numbered fight from another report.

The AI receives a compact evidence representation from every returned module,
including original row counts and bounded top rows, rather than a blindly
truncated raw response. Missing modules stay missing and are not inferred. Carter
does not bulk-download every raw combat event for every pull; detailed pre-death
event timelines, position traces, and encounter-specific mechanic definitions
still require targeted event queries and verified encounter rules.

For chat questions, a deterministic question router selects a relevant evidence
subset before Gemini is called. It currently distinguishes death, wipe, DPS,
healing, tank, cooldown, mechanic, pull-comparison, and raid-summary questions.
Explicit report/fight URLs take precedence over repeated fight numbers or boss
names, preventing an older fight 6 from being confused with fight 6 in the newest
report. The model is instructed to distinguish verified table evidence from
hypotheses and to disclose when event-level causation is unavailable.

Damage rankings are calculated deterministically as encounter DPS
(`damage × 1000 / fightTimeMs`). Carter also retains active DPS
(`damage × 1000 / activeTimeMs`) as a separately labeled secondary rate. Questions
asking for “top DPS” use encounter DPS and return the rate rather than only a raw
damage total. Provider instructions require English-only replies while
preserving player, guild, boss, and ability proper names; Gemini speech synthesis
is also configured with the English language code.

The listener keeps up to one second of recent audio and five seconds while AI
connects, in memory only. A slow connection can clip the start of a long question.
There is a three-second cooldown after a conversation or `/carter stop`, and a
30-second cooldown after failed activation. Local speech recognition may miss
the name or falsely recognize it, especially with accents or background noise.
Set `CARTER_WAKE_WORD_ENABLED=false` for manual-only activation.

Carter joins with self-deafening disabled so it can receive audio.
Use headphones: bot accounts are filtered, but loudspeaker audio picked up by
your own microphone can still produce acoustic echo. Speak during Carter's reply
to interrupt it; provider interruption events clear queued playback.

The shared wake receiver keeps separate local recognition state for each Discord
user. After a wake or manual activation, the session is exclusively bound to the
activating user's Discord audio stream; speech from every other user, including
other authorized users, is dropped until that session ends. Carter's provider is
also instructed to ignore unrelated speech captured by the owner's microphone
unless it clearly addresses Carter or continues the active exchange.

## Providers and usage limits

Gemini is the primary provider. OpenAI Realtime fallback requires all of:
`CARTER_ALLOW_OPENAI_FALLBACK=true`, a valid `CARTER_OPENAI_API_KEY`, and access to
`CARTER_OPENAI_MODEL` (template: `gpt-realtime-2.1`). A Gemini quota or rate-limit
failure can trigger this fallback; invalid credentials, invalid models, and
unrelated errors do not. Carter reports a provider switch and starts fresh
conversation context. Fallback remains off by default.

The default inactivity timeout is 45 seconds (`CARTER_SESSION_TIMEOUT`, 5–300).
The absolute session limit is 180 seconds (`CARTER_MAX_SESSION_SECONDS`, 10–900),
including continuous speech. Both close the AI session while leaving voice
connected. Client-side speech detection uses `CARTER_INPUT_RMS_THRESHOLD=300`
(0–32767); increase it if background noise opens or holds turns, or reduce it if
speech is not sent. Carter sends explicit speech-start/end boundaries to Gemini
after 700 ms below this threshold. The threshold is separate from local wake-name
recognition.
Audio queues are bounded, and provider recovery allows at most one transient
retry instead of an unlimited reconnect loop.

Safe runtime diagnostics log the first accepted Discord microphone frame, local
speech-turn boundaries, the first Gemini server message, the first provider
event and output audio, retry snapshots, and final session counters. `/carter
status` also reports microphone-frame, speech-turn, provider-event, and Discord
receive-fault counters. These diagnostics never include audio, transcripts,
tokens, API keys, or credential values.

Usage includes the audio sent and generated, plus any provider context charges.
Audio duration and provider-reported counts are estimates, not a dollar balance.
Gemini free quota is not guaranteed; check your project's limits and billing.
OpenAI fallback can incur charges when enabled and triggered. Session time limits
bound each activation's duration; they are not a dollar spending cap.

The process serializes voice changes and releases sessions and connections on
shutdown (Ctrl+C). A local file lock prevents duplicate processes from this
directory; deploy one instance only, since the lock does not coordinate separate
machines or copied directories. Logs go to the Carter process's stderr without
API keys, tokens, or raw audio. There is no audio archive, tool execution,
website integration, or persistent conversation history.

## Verification

Run the offline checks from this directory:

```bash
.venv/bin/python -m compileall -q main.py session.py providers.py audio.py discord_receive.py wakeword.py wake_listener.py setup_wakeword.py warcraft_logs.py warcraft_history.py warcraft_chart.py warcraft_replay.py
.venv/bin/python -m unittest discover -p 'test_*.py'
```

Offline tests use fake providers and do not spend API quota. They cannot establish
Discord microphone reception, model access, encryption compatibility, or audible
playback. Those require the real bot, guild, allowed speaker, Gemini key/model,
and working microphone. OpenAI credentials are needed only for enabled fallback.

Live smoke check:

1. Start Carter and confirm it remains disconnected and does not read logs. Run
   `/carter voicechannel`, then confirm `/carter status` shows the wake listener
   `armed` with AI inactive.
2. Speak without the name; AI must remain inactive and no provider session starts.
3. Say "Carter", pause briefly, ask a question, and confirm audible Gemini output.
4. Confirm unauthorized users cannot wake Carter or control commands, and another
   user's audio does not feed the active conversation.
5. Run `/carter stop`; confirm AI ends, voice remains connected, and the listener
   returns to `armed`. Wait three seconds, then wake it again. Also check that
   inactivity and absolute time limits return it to local wake listening.
6. Check `/carter activate` as a manual alternative. Run `/carter leave` to confirm
   voice and wake listening stop; use `/carter join` to restore them.
7. Stop/restart Carter and confirm the terminal reports the saved logs channel and
   restores its cache without another `/carter setlogs`. Confirm it does not join
   voice automatically and Armory remains online independently.

Live wake accuracy, Discord voice, and provider calls have not been validated by
offline checks. Discord voice reception depends on the pinned receive extension and its
compatibility with Discord's voice encryption.

Official references: [Gemini Live SDK guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk),
[Gemini Live capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities),
[OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations),
[Discord receive extension](https://github.com/imayhaveborkedit/discord-ext-voice-recv),
and [discord.py interactions](https://discordpy.readthedocs.io/en/stable/interactions/api.html).
