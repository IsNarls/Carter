"""Independent Discord voice assistant with a separate raid-analysis backend."""

from __future__ import annotations

import asyncio
import fcntl
import io
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

from analysis_client import AnalysisClient, AnalysisClientError, AnalysisClientSettings
from discord_receive import CarterVoiceClient
from providers import ProviderError, ProviderSettings, generate_text
from raid_integration import (
    GuildSettingsStore, REPORT_URL, RaidAnalysis, RaidAnswer, analysis_tool_declaration,
    user_error,
)
from session import SessionLimits, VoiceSession
from wake_listener import WakeListener, WakeSettings


ROOT = Path(__file__).resolve().parent
DATA_ROOT = ROOT / ".carter_data"
INGEST_MESSAGE_SCAN_LIMIT = 100
INGEST_REPORT_LIMIT = 5
log = logging.getLogger("carter")


class ConfigurationError(ValueError):
    """Configuration errors with safe, constant messages for startup logs."""


def bounded_int(name, default, minimum, maximum):
    value = os.getenv(name, str(default)).strip()
    if not value.isdecimal() or not minimum <= int(value) <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return int(value)


def env_bool(name, default):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return value == "true"


def wake_configuration():
    model_path = Path(os.getenv("CARTER_WAKE_MODEL_PATH", WakeSettings.model_path))
    if not model_path.is_absolute():
        model_path = ROOT / model_path
    word = os.getenv("CARTER_WAKE_WORD", "Carter").strip()
    if not word or len(word) > 40 or not all(part.isalpha() for part in word.split()):
        raise ConfigurationError("CARTER_WAKE_WORD must contain only letters and spaces")
    return WakeSettings(
        enabled=env_bool("CARTER_WAKE_WORD_ENABLED", True), word=word,
        model_path=str(model_path), auto_join=False,
    )


def analysis_configuration():
    settings = AnalysisClientSettings(
        enabled=env_bool("CARTER_ANALYSIS_ENABLED", False),
        base_url=os.getenv(
            "CARTER_ANALYSIS_API_URL", "http://127.0.0.1:8765/api/v1",
        ).strip(),
        timeout=bounded_int("CARTER_ANALYSIS_TIMEOUT", 15, 1, 120),
        allow_insecure_remote=env_bool("CARTER_ANALYSIS_ALLOW_INSECURE_REMOTE", False),
    )
    try:
        settings.validated()
    except ValueError as error:
        raise ConfigurationError(str(error)) from None
    return settings, env_bool("CARTER_ANALYSIS_LOG_CHANNEL_INGEST", True)


def ai_configuration(*, analysis_tools=False):
    if os.getenv("CARTER_AI_PROVIDER", "gemini").strip().lower() != "gemini":
        raise ConfigurationError("CARTER_AI_PROVIDER must be gemini; OpenAI is opt-in fallback only")
    fallback = os.getenv("CARTER_ALLOW_OPENAI_FALLBACK", "false").strip().lower()
    if fallback not in {"true", "false"}:
        raise ConfigurationError("CARTER_ALLOW_OPENAI_FALLBACK must be true or false")
    settings = ProviderSettings(
        gemini_key=os.getenv("CARTER_GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("CARTER_GEMINI_MODEL", "gemini-3.8-live").strip(),
        gemini_text_model=os.getenv("CARTER_GEMINI_TEXT_MODEL", "gemini-3.8-flash").strip(),
        openai_key=os.getenv("CARTER_OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("CARTER_OPENAI_MODEL", "gpt-realtime-2.1").strip(),
        gemini_voice=os.getenv("CARTER_GEMINI_VOICE", "Kore").strip(),
        openai_voice=os.getenv("CARTER_OPENAI_VOICE", "marin").strip(),
        tool_declarations=(analysis_tool_declaration(),) if analysis_tools else (),
    )
    limits = SessionLimits(
        inactivity=bounded_int("CARTER_SESSION_TIMEOUT", 45, 5, 300),
        max_seconds=bounded_int("CARTER_MAX_SESSION_SECONDS", 180, 10, 900),
        rms_threshold=bounded_int("CARTER_INPUT_RMS_THRESHOLD", 300, 0, 32767),
        allow_fallback=fallback == "true",
    )
    return settings, limits


class GuildRuntime:
    """Independent voice, selection, and import state for one Discord guild."""

    def __init__(self, bot, guild_id, settings, limits, wake_settings, record):
        self.guild_id = int(guild_id)
        self.channel_id = _saved_id(record.get("voice_channel_id"))
        self.logs_channel_id = _saved_id(record.get("logs_channel_id"))
        self.analysis_state = GuildSettingsStore.analysis_state(record)
        self.voice_lock = asyncio.Lock()
        self.text_lock = asyncio.Lock()
        self.analysis_lock = asyncio.Lock()

        async def on_tool(call):
            return await bot._voice_analysis_tool(self, call)

        self.session = VoiceSession(settings, limits, tool_handler=on_tool)

        async def on_wake(user_id, voice, generation):
            return await bot._wake_activate(self, user_id, voice, generation)

        self.wake = WakeListener(self.session, bot.allowed_users, wake_settings, on_wake)
        self.want_voice = False
        self.import_tasks: set[asyncio.Task] = set()
        self.importing: dict[str, asyncio.Task] = {}
        self.history_import_task: asyncio.Task | None = None


class Carter(discord.Client):
    def __init__(self, legacy_guild_id, allowed_users, settings=None, limits=SessionLimits(),
                 wake_settings=None, analysis_settings=AnalysisClientSettings(), *,
                 monitor_log_channel=True):
        settings = settings or ProviderSettings()
        wake_settings = wake_settings or WakeSettings(auto_join=False)
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        # Explicit historical ingestion still needs message/embed visibility when
        # passive on-message monitoring is disabled.
        intents.message_content = bool(analysis_settings.enabled)
        super().__init__(intents=intents)
        self.legacy_guild_id = int(legacy_guild_id or 0)
        self.allowed_users = set(allowed_users)
        self.provider_settings = settings
        self.session_limits = limits
        self.wake_settings = wake_settings
        self.analysis_settings = analysis_settings
        self.monitor_log_channel = bool(monitor_log_channel)
        self.analysis_client = AnalysisClient(analysis_settings)
        self.raid = RaidAnalysis(self.analysis_client)
        self.settings_store = GuildSettingsStore(DATA_ROOT / "guild-settings.json")
        self.guilds_runtime: dict[int, GuildRuntime] = {}
        # The backend intentionally has one import worker. Serializing Carter's
        # submissions avoids filling its bounded queue from multiple guilds or commands.
        self.import_lock = asyncio.Lock()

        self.tree = app_commands.CommandTree(self)
        carter = app_commands.Group(name="carter", description="Control Carter")
        carter.command(name="join", description="Join this server's selected voice channel")(self.join)
        carter.command(name="leave", description="Leave voice")(self.leave)
        carter.command(name="status", description="Show Carter connection status")(self.status)
        carter.command(name="activate", description="Start a voice conversation")(self.activate)
        carter.command(name="stop", description="Stop AI speech and listening")(self.stop_ai)
        carter.command(name="ask", description="Ask Carter in text")(self.ask)
        carter.command(name="setvoice", description="Save this server's voice channel")(
            self.set_voice_channel,
        )
        carter.command(name="setlogs", description="Save this server's log-link channel")(
            self.set_logs_channel,
        )
        carter.command(name="voicechannel", description="Set both channels and connect")(
            self.set_channels,
        )
        self.tree.add_command(carter)

        logs = app_commands.Group(name="logs", description="Prepared raid-log analysis")
        logs.command(name="health", description="Check the analysis backend")(self.logs_health)
        logs.command(name="reports", description="List prepared reports")(self.logs_reports)
        logs.command(name="fights", description="List fights in a prepared report")(self.logs_fights)
        logs.command(name="select", description="Select a prepared report and optional fight")(
            self.logs_select,
        )
        logs.command(name="source", description="Select stored or live analysis")(
            self.logs_source,
        )
        logs.command(name="summary", description="Show the selected fight summary")(
            self.logs_summary,
        )
        logs.command(name="ranking", description="Show top DPS or effective HPS")(
            self.logs_ranking,
        )
        logs.command(name="player", description="Show one player's performance")(
            self.logs_player,
        )
        logs.command(name="death", description="Explain one player's death")(
            self.logs_death,
        )
        logs.command(name="deaths", description="List death timestamps for the selected pull")(
            self.logs_deaths,
        )
        logs.command(name="identity", description="Map your Discord account to a character")(
            self.logs_identity,
        )
        logs.command(name="ingest", description="Import reports from a selected channel")(
            self.logs_ingest,
        )
        logs.command(name="import", description="Queue a Warcraft Logs report in the backend")(
            self.logs_import,
        )
        logs.command(name="job", description="Check a backend import job")(self.logs_job)
        self.tree.add_command(logs)
        self.tree.on_error = self.command_error

    async def setup_hook(self):
        await self.tree.sync()
        if self.legacy_guild_id:
            guild = discord.Object(id=self.legacy_guild_id)
            self.tree.clear_commands(guild=guild)
            try:
                await self.tree.sync(guild=guild)
            except discord.Forbidden as error:
                if error.code != 50001:
                    raise
                log.info("Legacy guild command cleanup skipped (missing access)")

    def runtime(self, guild_id):
        guild_id = int(guild_id)
        if guild_id not in self.guilds_runtime:
            self.guilds_runtime[guild_id] = GuildRuntime(
                self, guild_id, self.provider_settings, self.session_limits,
                self.wake_settings, self.settings_store.guild(guild_id),
            )
        return self.guilds_runtime[guild_id]

    def save_runtime(self, state):
        self.settings_store.save(
            state.guild_id,
            voice_channel_id=state.channel_id,
            logs_channel_id=state.logs_channel_id,
            analysis={
                "source": state.analysis_state.source,
                "report_id": state.analysis_state.report_id,
                "fight_id": state.analysis_state.fight_id,
                "live_attempt": state.analysis_state.live_attempt,
                "identities": state.analysis_state.identities,
            },
        )

    async def on_ready(self):
        log.info("Connected as bot ID %s", self.user.id)
        for guild in self.guilds:
            state = self.runtime(guild.id)
            log.info(
                "Server configuration: guild=%s voice_channel=%s logs_channel=%s",
                guild.id, state.channel_id or "not_set", state.logs_channel_id or "not_set",
            )
        if self.analysis_settings.enabled:
            try:
                await self.analysis_client.health()
                log.info("Raid analysis backend ready")
            except AnalysisClientError as error:
                log.warning("Raid analysis backend unavailable: %s", error.code)

    async def authorize(self, interaction, *, ephemeral=True):
        authorized = interaction.guild_id is not None and interaction.user.id in self.allowed_users
        try:
            if not authorized:
                await interaction.response.send_message(
                    "You are not authorized to control Carter.", ephemeral=True,
                )
                return False
            await interaction.response.defer(ephemeral=ephemeral, thinking=not ephemeral)
        except discord.NotFound as error:
            if error.code != 10062:
                raise
            command = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")
            log.warning("Discord interaction expired before acknowledgement: command=%s", command)
            return False
        return True

    async def _arm_wake(self, state, voice):
        if (not state.wake.settings.enabled or state.session.active
                or not voice.channel or voice.channel.id != state.channel_id):
            return
        try:
            await state.wake.start(voice)
        except Exception as error:
            log.warning("Wake setup failed: %s; run .venv/bin/python setup_wakeword.py",
                        type(error).__name__)

    async def _connect_voice(self, state, guild, channel):
        voice = guild.voice_client
        if not voice or not voice.is_connected():
            if voice:
                await voice.disconnect(force=True)
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.connect and permissions.speak):
                raise ConfigurationError("Carter needs View Channel, Connect, and Speak permissions")
            voice = await channel.connect(
                cls=CarterVoiceClient, timeout=20, reconnect=True, self_deaf=False,
            )
        await self._arm_wake(state, voice)
        return voice

    async def _wake_activate(self, state, user_id, voice, generation):
        async with state.voice_lock:
            if (generation != state.wake.generation or not state.wake.running
                    or state.wake.voice is not voice or not voice.is_connected()
                    or voice.channel.id != state.channel_id or state.session.active
                    or user_id not in self.allowed_users):
                return False
            member = voice.guild.get_member(user_id)
            member_voice = getattr(member, "voice", None)
            if not member_voice or not member_voice.channel or member_voice.channel.id != state.channel_id:
                return False

            async def notice(message):
                log.info("%s", message)

            try:
                await state.session.start(voice, user_id, notice, external_capture=True)
            except Exception as error:
                code = error.code if isinstance(error, ProviderError) else type(error).__name__
                log.warning("Wake activation failed: %s", code)
                return False
            log.info("Wake word detected; conversation active for user ID %s", user_id)
            return True

    async def _voice_analysis_tool(self, state, call):
        if call.get("name") != "query_raid_analysis":
            return {"error": "unknown_tool"}
        owner_id = state.session.owner_id
        if owner_id not in self.allowed_users:
            return {"error": "unauthorized"}
        try:
            async with state.analysis_lock:
                answer = await self.raid.execute_tool(
                    state.analysis_state, call.get("arguments") or {}, owner_id,
                )
                self.save_runtime(state)
            if answer.image:
                await self._post_voice_analysis(state, answer)
            return answer.tool_context or {"text": answer.text}
        except AnalysisClientError as error:
            log.warning("Voice raid-analysis tool failed: %s", error.code)
            return {"error": error.code, "message": user_error(error)}

    async def _post_voice_analysis(self, state, answer):
        channel = self.get_channel(state.logs_channel_id) if state.logs_channel_id else None
        if not isinstance(channel, discord.TextChannel):
            log.info("Voice analysis image not posted: logs channel not set")
            return
        try:
            await self._send_channel_answer(channel, answer)
        except discord.HTTPException as error:
            log.warning("Voice analysis attachment delivery failed: %s", type(error).__name__)

    async def ask(self, interaction: discord.Interaction,
                  prompt: app_commands.Range[str, 1, 1800]):
        if not await self.authorize(interaction, ephemeral=False):
            return
        state = self.runtime(interaction.guild_id)
        if state.text_lock.locked():
            await interaction.followup.send(
                "Carter is already answering a chat prompt in this server. Try again shortly.",
                ephemeral=True,
            )
            return
        async with state.text_lock:
            if self.analysis_settings.enabled:
                try:
                    answer = await self.raid.answer_question(
                        state.analysis_state, prompt, interaction.user.id,
                    )
                    if answer is not None:
                        self.save_runtime(state)
                        await self._send_interaction_answer(interaction, answer)
                        return
                except AnalysisClientError as error:
                    log.warning("Chat raid-analysis request failed: %s", error.code)
                    await interaction.followup.send(user_error(error), ephemeral=True)
                    return
            try:
                answer = await generate_text(state.session.settings, prompt)
            except ProviderError as error:
                log.warning("Chat prompt failed: %s", error.code)
                await interaction.followup.send(
                    f"Carter could not answer that prompt ({error.code}). Try again later.",
                    ephemeral=True,
                )
                return
            await self._send_interaction_answer(interaction, RaidAnswer(answer))

    async def set_voice_channel(self, interaction: discord.Interaction,
                                channel: discord.VoiceChannel):
        if not await self.authorize(interaction):
            return
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.connect and permissions.speak):
            await interaction.followup.send(
                "Carter needs View Channel, Connect, and Speak in that voice channel.",
                ephemeral=True,
            )
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            state.wake.reset()
            await state.wake.stop()
            await state.session.stop()
            current = interaction.guild.voice_client
            if current:
                await current.disconnect(force=True)
            state.channel_id = channel.id
            state.want_voice = False
            self.save_runtime(state)
        await interaction.followup.send(
            "Voice channel saved. Use `/carter join` when you are ready.", ephemeral=True,
        )

    async def set_logs_channel(self, interaction: discord.Interaction,
                               channel: discord.TextChannel):
        if not await self.authorize(interaction):
            return
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.read_message_history
                and permissions.send_messages and permissions.attach_files):
            await interaction.followup.send(
                "Carter needs View Channel, Read Message History, Send Messages, and Attach Files "
                "in that channel.", ephemeral=True,
            )
            return
        state = self.runtime(interaction.guild_id)
        state.logs_channel_id = channel.id
        self.save_runtime(state)
        scanned = found = queued = skipped = 0
        batch_busy = False
        if self.analysis_settings.enabled and self.monitor_log_channel:
            try:
                scanned, found, queued, skipped, batch_busy = await self._ingest_channel_history(
                    state, channel,
                )
            except (discord.HTTPException, asyncio.TimeoutError):
                log.warning("Initial logs-channel history scan failed: guild=%s", state.guild_id)
        if batch_busy:
            message = (
                "Log-link channel saved, but another historical ingest batch is still running. "
                "Run `/logs ingest` after it completes."
            )
        else:
            message = (
                f"Log-link channel saved. Scanned {scanned} recent message(s), found {found} "
                f"unique report(s), queued {queued} for local storage, and skipped {skipped} "
                "already-running report(s)."
            )
        await interaction.followup.send(message, ephemeral=True)

    async def set_channels(self, interaction: discord.Interaction,
                           voice_channel: discord.VoiceChannel,
                           logs_channel: discord.TextChannel):
        if not await self.authorize(interaction):
            return
        member_voice = getattr(interaction.user, "voice", None)
        if not member_voice or member_voice.channel.id != voice_channel.id:
            await interaction.followup.send("Join the selected voice channel first.", ephemeral=True)
            return
        voice_permissions = voice_channel.permissions_for(interaction.guild.me)
        log_permissions = logs_channel.permissions_for(interaction.guild.me)
        if not (voice_permissions.view_channel and voice_permissions.connect
                and voice_permissions.speak):
            await interaction.followup.send(
                "Carter lacks voice permissions in the selected channel.", ephemeral=True,
            )
            return
        if not (log_permissions.view_channel and log_permissions.read_message_history
                and log_permissions.send_messages and log_permissions.attach_files):
            await interaction.followup.send(
                "Carter lacks history, message, or attachment permissions in the selected log channel.",
                ephemeral=True,
            )
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            state.wake.reset()
            await state.wake.stop()
            await state.session.stop()
            current = interaction.guild.voice_client
            if current:
                await current.disconnect(force=True)
            state.channel_id = voice_channel.id
            state.logs_channel_id = logs_channel.id
            state.want_voice = True
            self.save_runtime(state)
            await self._connect_voice(state, interaction.guild, voice_channel)
        await interaction.followup.send(
            "Channels saved. Carter joined voice and armed local wake listening. "
            "The AI remains inactive until Carter is addressed.", ephemeral=True,
        )

    async def join(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        if not state.channel_id:
            await interaction.followup.send("Use `/carter setvoice` first.", ephemeral=True)
            return
        async with state.voice_lock:
            channel = interaction.guild.get_channel(state.channel_id)
            member_voice = getattr(interaction.user, "voice", None)
            if not isinstance(channel, discord.VoiceChannel):
                await interaction.followup.send("The saved voice channel is unavailable.", ephemeral=True)
                return
            if not member_voice or member_voice.channel.id != channel.id:
                await interaction.followup.send("Join the saved voice channel first.", ephemeral=True)
                return
            state.want_voice = True
            voice = interaction.guild.voice_client
            if voice and voice.is_connected():
                await self._arm_wake(state, voice)
                message = "Carter is already connected."
            else:
                if voice:
                    await voice.disconnect(force=True)
                try:
                    await self._connect_voice(state, interaction.guild, channel)
                except Exception:
                    stale = interaction.guild.voice_client
                    if stale:
                        await stale.disconnect(force=True)
                    raise
                message = "Connected."
            if state.wake.state == "armed":
                message += f" Say '{state.wake.settings.word}' to wake Carter."
            elif state.wake.last_error:
                message += " Wake listening is unavailable; `/carter activate` still works."
            await interaction.followup.send(message, ephemeral=True)

    async def activate(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            voice = interaction.guild.voice_client
            member_voice = getattr(interaction.user, "voice", None)
            if not voice or not voice.is_connected() or not isinstance(voice, CarterVoiceClient):
                await interaction.followup.send("Use `/carter join` first.", ephemeral=True)
                return
            if (not member_voice or member_voice.channel.id != state.channel_id
                    or voice.channel.id != state.channel_id):
                await interaction.followup.send("Join the saved voice channel first.", ephemeral=True)
                return
            if state.session.active:
                await interaction.followup.send(
                    "A conversation is active. Use `/carter stop` first.", ephemeral=True,
                )
                return

            async def notice(message):
                await interaction.followup.send(message, ephemeral=True)

            try:
                state.wake.reset()
                await state.session.start(
                    voice, interaction.user.id, notice, external_capture=state.wake.running,
                )
            except Exception as error:
                code = error.code if isinstance(error, ProviderError) else type(error).__name__
                log.warning("Activation failed: %s", code)
                await notice(
                    f"AI activation failed ({code}). Check model access, credentials, and voice permissions."
                )
                return
            await notice(
                f"Carter is listening only to your microphone using {state.session.provider_name}. "
                "Others can hear replies. Use `/carter stop` to end."
            )

    async def stop_ai(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            state.wake.reset()
            await state.session.stop()
            message = "AI stopped. Carter remains connected to voice."
            if state.wake.running:
                message += f" Say '{state.wake.settings.word}' to wake Carter again."
            await interaction.followup.send(message, ephemeral=True)

    async def leave(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            state.want_voice = False
            await state.wake.stop()
            await state.session.stop()
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect(force=True)
            await interaction.followup.send("Carter is disconnected.", ephemeral=True)

    async def status(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        voice = interaction.guild.voice_client
        session = state.session
        model = (session.settings.gemini_model if session.provider_name == "gemini"
                 else session.settings.openai_model)
        backend = "disabled"
        if self.analysis_settings.enabled:
            try:
                await self.analysis_client.health()
                backend = "ready"
            except AnalysisClientError as error:
                backend = error.code
        tokens = session.usage.get("total_token_count", session.usage.get("total_tokens"))
        token_status = f"; last provider token total: {tokens}" if isinstance(tokens, int) else ""
        await interaction.followup.send(
            f"Voice connected: {bool(voice and voice.is_connected())}; "
            f"voice channel set: {bool(state.channel_id)}; log channel set: {bool(state.logs_channel_id)}; "
            f"wake listener: {state.wake.state}; AI: {session.state}; provider: {session.provider_name}; "
            f"model: {model}; analysis backend: {backend}; analysis source: "
            f"{state.analysis_state.source}; OpenAI fallback enabled: {session.limits.allow_fallback}; "
            f"audio sent/generated: {session.input_seconds:.1f}s/{session.output_seconds:.1f}s; "
            f"mic frames/turns/provider events/tools: {session.input_frames}/"
            f"{session.speech_turns}/{session.provider_events}/{session.tool_calls}; "
            f"other-speaker frames dropped: {state.wake.dropped_non_owner_frames}; "
            f"last stop: {session.reason}; last error: "
            f"{session.last_error or state.wake.last_error or 'none'}{token_status}.",
            ephemeral=True,
        )

    async def logs_health(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        try:
            result = await self.analysis_client.health()
            await interaction.followup.send(
                f"Analysis backend: {result.get('status', 'ready')}; schema "
                f"{result.get('schema_version', 'unknown')}.", ephemeral=True,
            )
        except AnalysisClientError as error:
            await interaction.followup.send(user_error(error), ephemeral=True)

    async def logs_reports(self, interaction: discord.Interaction,
                           limit: app_commands.Range[int, 1, 20] = 10):
        await self._analysis_command(interaction, lambda _state: self.raid.reports(limit))

    async def logs_fights(self, interaction: discord.Interaction, report_id: str = ""):
        await self._analysis_command(
            interaction, lambda state: self.raid.fights(state, report_id or None),
        )

    async def logs_select(self, interaction: discord.Interaction, report_id: str,
                          fight_id: app_commands.Range[int, 1] | None = None):
        async def select(state):
            answer = await self.raid.select_report(state, report_id)
            if fight_id is not None:
                answer = await self.raid.select_fight(state, fight_id)
            return answer
        await self._analysis_command(interaction, select, save=True)

    async def logs_source(self, interaction: discord.Interaction,
                          source: Literal["stored", "live"],
                          attempt: app_commands.Range[int, 1] | None = None):
        async def select(state):
            if source == "live":
                return await self.raid.select_live(state, attempt)
            state.source, state.live_attempt = "stored", None
            resolved = await self.raid.resolve_source(state, "")
            return RaidAnswer(f"Selected stored analysis: {resolved.label}.")
        await self._analysis_command(interaction, select, save=True)

    async def logs_summary(self, interaction: discord.Interaction):
        await self._analysis_command(interaction, lambda state: self.raid.summary(state))

    async def logs_ranking(self, interaction: discord.Interaction,
                           metric: Literal["dps", "hps"] = "dps",
                           limit: app_commands.Range[int, 1, 20] = 10):
        await self._analysis_command(
            interaction, lambda state: self.raid.ranking(state, metric, limit),
        )

    async def logs_player(self, interaction: discord.Interaction,
                          player: app_commands.Range[str, 1, 100]):
        await self._analysis_command(
            interaction, lambda state: self.raid.player(state, player),
        )

    async def logs_death(self, interaction: discord.Interaction, player: str = "",
                         death_number: app_commands.Range[int, 1] | None = None):
        async def explain(state):
            name = player.strip() or state.identities.get(str(interaction.user.id), "")
            if not name:
                raise AnalysisClientError("identity_required")
            return await self.raid.death(state, name, death_number)
        await self._analysis_command(interaction, explain)

    async def logs_deaths(self, interaction: discord.Interaction):
        await self._analysis_command(
            interaction, lambda state: self.raid.death_timeline(state),
        )

    async def logs_identity(self, interaction: discord.Interaction,
                            character: app_commands.Range[str, 1, 100]):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        state.analysis_state.identities[str(interaction.user.id)] = " ".join(character.split())
        self.save_runtime(state)
        await interaction.followup.send(
            "Your character mapping was saved for this server.", ephemeral=True,
        )

    async def logs_ingest(self, interaction: discord.Interaction,
                          channel: discord.TextChannel):
        """Save a log channel and queue up to five reports from its newest messages."""
        if not await self.authorize(interaction):
            return
        if not self.analysis_settings.enabled:
            await interaction.followup.send(
                "Raid-log analysis is disabled in Carter's configuration.", ephemeral=True,
            )
            return
        permissions = channel.permissions_for(interaction.guild.me)
        if not (permissions.view_channel and permissions.read_message_history
                and permissions.send_messages and permissions.attach_files):
            await interaction.followup.send(
                "Carter needs View Channel, Read Message History, Send Messages, and Attach Files "
                "in that channel.", ephemeral=True,
            )
            return

        state = self.runtime(interaction.guild_id)
        state.logs_channel_id = channel.id
        self.save_runtime(state)
        try:
            scanned, found, queued, skipped, batch_busy = await self._ingest_channel_history(
                state, channel,
            )
        except (discord.HTTPException, asyncio.TimeoutError):
            log.warning(
                "Requested logs-channel history scan failed: guild=%s channel=%s",
                state.guild_id, channel.id,
            )
            await interaction.followup.send(
                "The log channel was saved, but Carter could not read its recent history.",
                ephemeral=True,
            )
            return

        if batch_busy:
            await interaction.followup.send(
                "Log channel saved, but another historical ingest batch is still running. "
                "Run this command again after that batch completes.",
                ephemeral=True,
            )
            return
        log.info(
            "Discord log history ingest queued: guild=%s channel=%s messages=%s reports=%s "
            "queued=%s skipped=%s",
            state.guild_id, channel.id, scanned, found, queued, skipped,
        )
        if not found:
            await interaction.followup.send(
                f"Log channel saved, but no Warcraft Logs report URL was visible in the "
                f"{scanned} recent message(s) checked. Confirm Discord's Message Content Intent "
                "is enabled and that Carter can read the channel.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Log channel saved. Scanned {scanned} recent message(s), found {found} unique "
            f"report(s), queued {queued} for local backend download and storage, and skipped "
            f"{skipped} already-running report(s). Each run is limited to five reports.",
            ephemeral=True,
        )

    async def logs_import(self, interaction: discord.Interaction,
                          report_url: app_commands.Range[str, 1, 500]):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        match = REPORT_URL.search(report_url)
        if not match:
            await interaction.followup.send("Provide a valid Warcraft Logs report URL.", ephemeral=True)
            return
        report_id = match.group(1)
        existing = state.importing.get(report_id)
        if existing and not existing.done():
            await interaction.followup.send("That report is already being prepared.", ephemeral=True)
            return
        if self.import_lock.locked():
            await interaction.followup.send(
                "Another report is being prepared. Try this import again after it completes.",
                ephemeral=True,
            )
            return
        await self.import_lock.acquire()
        try:
            queued = await self.raid.submit_report(match.group(0))
        except AnalysisClientError as error:
            self.import_lock.release()
            await interaction.followup.send(user_error(error), ephemeral=True)
            return
        except BaseException:
            self.import_lock.release()
            raise
        try:
            job_id = str(queued.tool_context["job_id"])
        except (AttributeError, KeyError, TypeError):
            self.import_lock.release()
            await interaction.followup.send(
                "The analysis backend returned an invalid import response.", ephemeral=True,
            )
            return
        lock_guard = {"held": True}
        try:
            task = asyncio.create_task(
                self._poll_import_locked(
                    state, job_id, report_id, interaction.channel, lock_guard,
                ),
                name=f"carter-import-{state.guild_id}-{report_id}",
            )
        except BaseException:
            self.import_lock.release()
            raise
        self._remember_import_task(state, report_id, task)

        def release_unstarted_lock(_done_task):
            if lock_guard["held"]:
                lock_guard["held"] = False
                self.import_lock.release()

        task.add_done_callback(release_unstarted_lock)
        await interaction.followup.send(queued.text, ephemeral=True)

    async def logs_job(self, interaction: discord.Interaction,
                       job_id: app_commands.Range[str, 1, 100]):
        if not await self.authorize(interaction):
            return
        try:
            payload = await self.analysis_client.import_job(job_id)
            await interaction.followup.send(_format_job(payload), ephemeral=True)
        except AnalysisClientError as error:
            await interaction.followup.send(user_error(error), ephemeral=True)

    async def _analysis_command(self, interaction, operation, *, save=False):
        if not await self.authorize(interaction, ephemeral=False):
            return
        state = self.runtime(interaction.guild_id)
        try:
            async with state.analysis_lock:
                answer = await operation(state.analysis_state)
                if save:
                    self.save_runtime(state)
            await self._send_interaction_answer(interaction, answer)
        except AnalysisClientError as error:
            log.warning("Raid-analysis command failed: %s", error.code)
            message = user_error(error)
            candidates = error.details.get("candidates")
            if candidates:
                message += " Candidates: " + ", ".join(_candidate(value) for value in candidates[:10])
            await interaction.followup.send(message[:1900], ephemeral=True)

    async def _send_interaction_answer(self, interaction, answer):
        chunks = _chunks(answer.text)
        arguments = {"allowed_mentions": discord.AllowedMentions.none()}
        if answer.image:
            arguments["file"] = discord.File(
                io.BytesIO(answer.image.data), filename=answer.image.filename,
                description=answer.image.alt_text,
            )
        await interaction.followup.send(chunks[0], **arguments)
        for chunk in chunks[1:5]:
            await interaction.followup.send(
                chunk, allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _send_channel_answer(self, channel, answer):
        arguments = {"allowed_mentions": discord.AllowedMentions.none()}
        if answer.image:
            arguments["file"] = discord.File(
                io.BytesIO(answer.image.data), filename=answer.image.filename,
                description=answer.image.alt_text,
            )
        await channel.send(_chunks(answer.text)[0], **arguments)

    def _schedule_import(self, state, url, channel):
        match = REPORT_URL.search(url)
        if not match or not self.analysis_settings.enabled:
            return False
        report_id = match.group(1)
        existing = state.importing.get(report_id)
        if existing and not existing.done():
            return False
        task = asyncio.create_task(
            self._run_import(state, match.group(0), report_id, channel),
            name=f"carter-import-{state.guild_id}-{report_id}",
        )
        self._remember_import_task(state, report_id, task)
        return True

    async def _ingest_channel_history(self, state, channel):
        """Find five recent unique reports and queue one sequential import batch."""
        existing_batch = state.history_import_task
        if existing_batch and not existing_batch.done():
            return 0, 0, 0, 0, True
        scanned = 0
        report_ids = set()
        reports = []
        async for message in channel.history(
                limit=INGEST_MESSAGE_SCAN_LIMIT, oldest_first=False):
            if scanned >= INGEST_MESSAGE_SCAN_LIMIT or len(report_ids) >= INGEST_REPORT_LIMIT:
                break
            scanned += 1
            for url in _message_report_urls(message):
                match = REPORT_URL.search(url)
                if not match:
                    continue
                report_id = match.group(1)
                if report_id in report_ids:
                    continue
                report_ids.add(report_id)
                reports.append((report_id, match.group(0)))
                if len(report_ids) >= INGEST_REPORT_LIMIT:
                    break
        queued, skipped, batch_busy = self._schedule_import_batch(state, reports, channel)
        return scanned, len(report_ids), queued, skipped, batch_busy

    def _schedule_import_batch(self, state, reports, channel):
        """Reserve report IDs immediately and process the batch without backend queue overflow."""
        existing_batch = state.history_import_task
        if existing_batch and not existing_batch.done():
            return 0, 0, True
        pending = []
        skipped = 0
        for report_id, url in reports[:INGEST_REPORT_LIMIT]:
            existing = state.importing.get(report_id)
            if existing and not existing.done():
                skipped += 1
                continue
            pending.append((report_id, url))
        if not pending:
            return 0, skipped, False

        task = asyncio.create_task(
            self._run_import_batch(state, pending, channel),
            name=f"carter-import-batch-{state.guild_id}",
        )
        for report_id, _url in pending:
            state.importing[report_id] = task
        state.history_import_task = task
        state.import_tasks.add(task)

        def release_reservations(done_task):
            state.import_tasks.discard(done_task)
            if state.history_import_task is done_task:
                state.history_import_task = None
            for report_id, _url in pending:
                if state.importing.get(report_id) is done_task:
                    state.importing.pop(report_id, None)

        task.add_done_callback(release_reservations)
        return len(pending), skipped, False

    async def _run_import_batch(self, state, reports, channel):
        """Run oldest first so the newest historical report remains selected."""
        task = asyncio.current_task()
        try:
            for report_id, url in reversed(reports):
                try:
                    await self._run_import(state, url, report_id, channel)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning(
                        "Unexpected backend import failure: guild=%s report=%s cause=%s",
                        state.guild_id, report_id, type(error).__name__,
                    )
                    await self._report_import_failure(
                        state, report_id, channel, AnalysisClientError("import_failed"),
                    )
        finally:
            for report_id, _url in reports:
                if state.importing.get(report_id) is task:
                    state.importing.pop(report_id, None)

    @staticmethod
    def _remember_import_task(state, report_id, task):
        state.importing[report_id] = task
        state.import_tasks.add(task)

        def release_reservation(done_task):
            state.import_tasks.discard(done_task)
            if state.importing.get(report_id) is done_task:
                state.importing.pop(report_id, None)

        task.add_done_callback(release_reservation)

    async def _run_import(self, state, url, report_id, channel):
        try:
            async with self.import_lock:
                try:
                    queued = await self.raid.submit_report(url)
                    job_id = str(queued.tool_context["job_id"])
                    await self._poll_import(state, job_id, report_id, channel)
                except AnalysisClientError as error:
                    await self._report_import_failure(state, report_id, channel, error)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    log.warning(
                        "Unexpected backend import failure: guild=%s report=%s cause=%s",
                        state.guild_id, report_id, type(error).__name__,
                    )
                    await self._report_import_failure(
                        state, report_id, channel, AnalysisClientError("import_failed"),
                    )
        finally:
            state.importing.pop(report_id, None)

    async def _poll_import_locked(self, state, job_id, report_id, channel, lock_guard):
        """Keep Carter's global import slot until a directly submitted job finishes."""
        try:
            await self._poll_import(state, job_id, report_id, channel)
        finally:
            if lock_guard["held"]:
                lock_guard["held"] = False
                self.import_lock.release()

    async def _poll_import(self, state, job_id, report_id, channel):
        try:
            deadline = time.monotonic() + 1800
            payload = {}
            while time.monotonic() < deadline:
                payload = await self.analysis_client.import_job(job_id)
                status = str(payload.get("status") or payload.get("state") or "").casefold()
                if status in {"succeeded", "partial", "failed"}:
                    break
                await asyncio.sleep(2)
            else:
                raise AnalysisClientError("analysis_timeout")
            status = str(payload.get("status") or payload.get("state") or "").casefold()
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            first_error = errors[0] if errors and isinstance(errors[0], dict) else {}
            if status not in {"succeeded", "partial"}:
                raise AnalysisClientError(str(first_error.get("code") or "import_failed"))
            result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
            state.analysis_state.source = "stored"
            state.analysis_state.report_id = str(
                result.get("report_id") or payload.get("report_id") or report_id
            )
            state.analysis_state.fight_id = None
            self.save_runtime(state)
            if isinstance(channel, discord.abc.Messageable):
                await channel.send(
                    f"Prepared Warcraft Logs report `{state.analysis_state.report_id}`: "
                    f"{int(result.get('analyzed_fights') or 0)} of "
                    f"{int(result.get('fight_count') or 0)} fight analyses available"
                    f"{' (partial)' if status == 'partial' else ''}.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except AnalysisClientError as error:
            await self._report_import_failure(state, report_id, channel, error)
        finally:
            state.importing.pop(report_id, None)

    @staticmethod
    async def _report_import_failure(state, report_id, channel, error):
        log.warning(
            "Backend report import failed: guild=%s report=%s code=%s",
            state.guild_id, report_id, error.code,
        )
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(
                    f"Could not prepare report `{report_id}` ({error.code}).",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass

    async def on_message(self, message):
        if not self.monitor_log_channel or not self.analysis_settings.enabled or not message.guild:
            return
        state = self.runtime(message.guild.id)
        if not state.logs_channel_id or message.channel.id != state.logs_channel_id:
            return
        for url in _message_report_urls(message):
            self._schedule_import(state, url, message.channel)

    async def on_message_edit(self, _before, after):
        await self.on_message(after)

    async def on_voice_state_update(self, member, before, after):
        state = self.guilds_runtime.get(member.guild.id)
        if not state:
            return
        is_bot = self.user and member.id == self.user.id
        if (is_bot or member.id == state.session.owner_id) and (
                not after.channel or after.channel.id != state.channel_id):
            async with state.voice_lock:
                state.wake.reset()
                if is_bot:
                    await state.wake.stop()
                await state.session.stop()

    async def command_error(self, interaction, error):
        original = getattr(error, "original", error)
        command = getattr(getattr(interaction, "command", None), "qualified_name", "unknown")
        frames = traceback.extract_tb(original.__traceback__) if original.__traceback__ else []
        location = "unknown"
        if frames:
            frame = frames[-1]
            location = f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        code = original.code if isinstance(original, (ProviderError, AnalysisClientError)) \
            else type(original).__name__
        log.error(
            "Command failed: command=%s wrapper=%s cause=%s location=%s",
            command, type(error).__name__, code, location,
        )
        message = "Carter could not complete that command. Check permissions and configuration."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.NotFound as response_error:
            if response_error.code != 10062:
                raise
            log.warning("Command error response skipped because Discord interaction expired")

    async def close(self):
        try:
            for state in self.guilds_runtime.values():
                for task in list(state.import_tasks):
                    task.cancel()
                if state.import_tasks:
                    await asyncio.gather(*state.import_tasks, return_exceptions=True)
                await state.wake.stop()
                await state.session.stop()
            await self.analysis_client.close()
            for voice in list(self.voice_clients):
                try:
                    await voice.disconnect(force=True)
                except Exception as error:
                    log.warning("Voice cleanup failed: %s", type(error).__name__)
        finally:
            await super().close()


def _saved_id(value):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 0
    return value if value > 0 else 0


def _chunks(text):
    value = str(text or "Carter returned no text.")
    return [value[index:index + 1900] for index in range(0, len(value), 1900)] or [value]


def _candidate(value):
    if isinstance(value, dict):
        if value.get("death_number"):
            return f"death {value['death_number']} at {float(value.get('time_seconds') or 0):.2f}s"
        return str(value.get("name") or value.get("player_id") or "unknown")[:80]
    return str(value)[:80]


def _format_job(payload):
    status = str(payload.get("status") or payload.get("state") or "unknown")
    progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
    detail = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    report = str(detail.get("report_id") or payload.get("report_id") or "unknown")
    completed = int(progress.get("completed_fights") or 0)
    total = int(progress.get("total_fights") or detail.get("fight_count") or 0)
    errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
    first_error = errors[0] if errors and isinstance(errors[0], dict) else {}
    error = first_error.get("code")
    result = f"Job `{payload.get('job_id', 'unknown')}` for `{report}`: {status}; {completed}/{total} fights."
    if error:
        result += f" Error: {error}."
    return result


def _message_report_urls(message):
    """Extract bounded report URLs, including Discord forwarded-message snapshots."""
    roots = [getattr(message, "content", "")]
    for name in ("embeds", "attachments", "stickers", "components", "message_snapshots"):
        value = getattr(message, name, None)
        if value:
            roots.append(value)
    raw = getattr(message, "_raw_data", None)
    if isinstance(raw, dict):
        roots.append(raw)
    strings, seen = [], set()

    def walk(value, depth=0):
        if depth > 7 or len(strings) > 1000:
            return
        if isinstance(value, str):
            strings.append(value[:20000])
            return
        if value is None or isinstance(value, (bytes, int, float, bool)):
            return
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(value, dict):
            for key, item in list(value.items())[:200]:
                walk(key, depth + 1)
                walk(item, depth + 1)
            return
        if isinstance(value, (list, tuple, set)):
            for item in list(value)[:200]:
                walk(item, depth + 1)
            return
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                walk(to_dict(), depth + 1)
            except Exception:
                pass
        for attribute in (
                "url", "proxy_url", "description", "title", "content", "name", "value",
                "fields", "embeds", "attachments", "stickers", "components",
                "message_snapshots"):
            walk(getattr(value, attribute, None), depth + 1)

    for root in roots:
        walk(root)
    urls = []
    trailing_url_punctuation = "".join(
        (".", ",", ";", ":", "!", "?", "'", '\"', "]", ")", "}")
    )
    for text in strings:
        for match in REPORT_URL.finditer(text):
            url = match.group(0).rstrip(trailing_url_punctuation)
            if url not in urls:
                urls.append(url)
    return urls[:20]


def main():
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s carter %(levelname)s %(message)s")
    for namespace in ("discord", "google_genai", "httpx", "httpcore", "websockets"):
        logging.getLogger(namespace).setLevel(logging.CRITICAL)
    if os.getenv("CARTER_ENABLED", "false").lower() != "true":
        log.info("Disabled; set CARTER_ENABLED=true to start")
        return
    token = os.getenv("CARTER_DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("CARTER_DISCORD_TOKEN is required")
    legacy = os.getenv("CARTER_DISCORD_GUILD_ID", "").strip()
    legacy_guild_id = int(legacy) if legacy.isdecimal() and int(legacy) > 0 else 0
    raw_users = os.getenv("CARTER_ALLOWED_USER_IDS", "").split(",")
    if not raw_users or not all(user.strip().isdecimal() and int(user.strip()) > 0
                                for user in raw_users):
        raise ConfigurationError("CARTER_ALLOWED_USER_IDS requires positive Discord user IDs")
    users = {int(user.strip()) for user in raw_users}
    analysis_settings, monitor = analysis_configuration()
    settings, limits = ai_configuration(analysis_tools=analysis_settings.enabled)
    with (ROOT / ".carter.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Carter is already running from this directory") from None
        bot = Carter(
            legacy_guild_id, users, settings, limits, wake_configuration(),
            analysis_settings, monitor_log_channel=monitor,
        )
        bot.run(token, log_handler=None)


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as error:
        log.error("%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        log.error("Startup failed (%s). Check Carter configuration and connectivity.",
                  type(error).__name__)
        raise SystemExit(1) from None
