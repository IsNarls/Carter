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
Set `CARTER_ENABLED=true`, Carter's dedicated Discord token, guild and voice channel
IDs, comma-separated `CARTER_ALLOWED_USER_IDS`, and `CARTER_GEMINI_API_KEY`.
Set `CARTER_AI_PROVIDER=gemini` and choose a Gemini Live model available to your
API key; the template uses `gemini-3.8-live`. The model name and key must both be
valid for activation to work. Environment variables override `.env`.
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
in the configured channel. No Message Content or Members privileged intent is needed.
Enable Developer Mode in Discord to copy guild/channel/user IDs.

## Use

Only users in `CARTER_ALLOWED_USER_IDS` can run the guild's Carter commands.

| Command | Behavior |
| --- | --- |
| `/carter join` | Join or retry the configured channel and arm local wake listening. You must be in that channel. |
| `/carter activate` | Start one AI session for your microphone. You must be in that channel. |
| `/carter stop` | Close AI and return to local wake listening while remaining in voice. |
| `/carter leave` | Stop AI and wake listening, then disconnect from voice. |
| `/carter status` | Show connection, wake state, AI state, provider, and usage without secrets. |

With `CARTER_AUTO_JOIN=true` (default), Carter joins on startup. Join the channel,
say "Carter", pause briefly, then ask your question. The `armed` wake state means
the local listener is ready and AI is inactive. Only `CARTER_ALLOWED_USER_IDS`
can wake Carter; bots and unauthorized users are ignored. Waiting for the name
uses local Vosk recognition and sends no audio to Gemini or OpenAI.

One conversation may be active at a time. Only its initiating user's microphone
feeds AI, while everyone in the channel can hear replies. The name does not
start another session during an active conversation. When the session ends,
Carter remains in voice and returns to listening for its name; each wake starts
a new conversation. `/carter activate` remains a manual alternative.

The listener keeps up to one second of recent audio and five seconds while AI
connects, in memory only. A slow connection can clip the start of a long question.
There is a three-second cooldown after a conversation or `/carter stop`, and a
30-second cooldown after failed activation. Local speech recognition may miss
the name or falsely recognize it, especially with accents or background noise.
Set `CARTER_WAKE_WORD_ENABLED=false` for manual-only activation, or
`CARTER_AUTO_JOIN=false` to require `/carter join` after startup.

Carter joins with self-deafening disabled so it can receive audio.
Use headphones: bot accounts are filtered, but loudspeaker audio picked up by
your own microphone can still produce acoustic echo. Speak during Carter's reply
to interrupt it; provider interruption events clear queued playback.

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
connected. Local activity detection uses `CARTER_INPUT_RMS_THRESHOLD=300`
(0–32767); increase it if quiet noise keeps the session alive or reduce it if
speech does not reset the timer. This only controls the inactivity timer: all
activating-user audio received during an active session goes to the provider.
The activity timer is separate from local wake-name recognition.
Audio queues are bounded, and provider recovery allows at most one transient
retry instead of an unlimited reconnect loop.

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
.venv/bin/python -m compileall -q main.py wake_listener.py wakeword.py setup_wakeword.py
.venv/bin/python -m unittest discover -p 'test_*.py'
```

Offline tests use fake providers and do not spend API quota. They cannot establish
Discord microphone reception, model access, encryption compatibility, or audible
playback. Those require the real bot, guild, allowed speaker, Gemini key/model,
and working microphone. OpenAI credentials are needed only for enabled fallback.

Live smoke check:

1. Start Carter and confirm automatic voice join and `/carter status` showing
   wake listener `armed` with AI inactive. Use `/carter join` if joining fails.
2. Speak without the name; AI must remain inactive and no provider session starts.
3. Say "Carter", pause briefly, ask a question, and confirm audible Gemini output.
4. Confirm unauthorized users cannot wake Carter or control commands, and another
   user's audio does not feed the active conversation.
5. Run `/carter stop`; confirm AI ends, voice remains connected, and the listener
   returns to `armed`. Wait three seconds, then wake it again. Also check that
   inactivity and absolute time limits return it to local wake listening.
6. Check `/carter activate` as a manual alternative. Run `/carter leave` to confirm
   voice and wake listening stop; use `/carter join` to restore them.
7. Stop/restart Carter to check cleanup and automatic joining. Confirm Armory
   remains online independently.

Live wake accuracy, Discord voice, and provider calls have not been validated by
offline checks. Discord voice reception depends on the pinned receive extension and its
compatibility with Discord's voice encryption.

Official references: [Gemini Live SDK guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk),
[Gemini Live capabilities](https://ai.google.dev/gemini-api/docs/live-api/capabilities),
[OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations),
[Discord receive extension](https://github.com/imayhaveborkedit/discord-ext-voice-recv),
and [discord.py interactions](https://discordpy.readthedocs.io/en/stable/interactions/api.html).
