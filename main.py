"""Independent Discord voice assistant with a separate raid-analysis backend."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
import fcntl
import io
import logging
import math
import os
import time
import traceback
from pathlib import Path
from typing import Literal

import discord
from discord import app_commands
from dotenv import load_dotenv

from analysis_backend_control import AnalysisBackendController
from analysis_client import AnalysisClient, AnalysisClientError, AnalysisClientSettings
from audio import PCMPlayback
from discord_receive import CarterVoiceClient
from providers import ProviderError, ProviderSettings, generate_text
from raid_integration import (
    GuildSettingsStore, REPORT_URL, RaidAnalysis, RaidAnswer, analysis_tool_declaration,
    user_error,
)
from session import FixedSpeechPlayer, SessionLimits, VoiceSession
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


def analysis_backend_configuration(settings):
    """Return Carter-owned backend lifecycle settings without exposing credentials."""
    managed = env_bool("CARTER_ANALYSIS_MANAGE_BACKEND", True)
    startup_timeout = bounded_int("CARTER_ANALYSIS_STARTUP_TIMEOUT", 15, 1, 120)
    health_interval = bounded_int("CARTER_ANALYSIS_HEALTH_INTERVAL", 15, 5, 300)
    return bool(settings.enabled and managed), startup_timeout, health_interval


def live_announcement_configuration():
    """Return bounded polling and combat-log freshness settings."""
    max_age = bounded_int("CARTER_LIVE_ANNOUNCEMENT_MAX_AGE_SECONDS", 15, 1, 300)
    poll_ms = bounded_int("CARTER_LIVE_ANNOUNCEMENT_POLL_MS", 100, 100, 5000)
    return max_age, poll_ms / 1000


def ai_configuration(*, analysis_tools=False):
    if os.getenv("CARTER_AI_PROVIDER", "gemini").strip().lower() != "gemini":
        raise ConfigurationError("CARTER_AI_PROVIDER must be gemini; OpenAI is opt-in fallback only")
    fallback = os.getenv("CARTER_ALLOW_OPENAI_FALLBACK", "false").strip().lower()
    if fallback not in {"true", "false"}:
        raise ConfigurationError("CARTER_ALLOW_OPENAI_FALLBACK must be true or false")
    text_thinking = os.getenv(
        "CARTER_GEMINI_TEXT_THINKING_LEVEL", "minimal",
    ).strip().lower()
    if text_thinking not in {"minimal", "low", "medium", "high"}:
        raise ConfigurationError(
            "CARTER_GEMINI_TEXT_THINKING_LEVEL must be minimal, low, medium, or high"
        )
    settings = ProviderSettings(
        gemini_key=os.getenv("CARTER_GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("CARTER_GEMINI_MODEL", "gemini-3.8-live").strip(),
        gemini_text_model=os.getenv(
            "CARTER_GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite",
        ).strip(),
        gemini_text_thinking_level=text_thinking,
        gemini_text_timeout=bounded_int("CARTER_GEMINI_TEXT_TIMEOUT", 20, 5, 120),
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
        self.raid = RaidAnalysis(bot.analysis_client.for_guild(self.guild_id))
        self.voice_lock = asyncio.Lock()
        self.text_lock = asyncio.Lock()
        self.analysis_lock = asyncio.Lock()

        async def on_tool(call):
            return await bot._voice_analysis_tool(self, call)

        self.session = VoiceSession(settings, limits, tool_handler=on_tool)
        self.live_announcer = FixedSpeechPlayer(settings)

        async def on_wake(user_id, voice, generation):
            return await bot._wake_activate(self, user_id, voice, generation)

        self.wake = WakeListener(self.session, bot.allowed_users, wake_settings, on_wake)

        async def rearm_after_exchange():
            await bot._rearm_after_exchange(self)

        self.session.completion_handler = rearm_after_exchange
        self.want_voice = False
        self.import_tasks: set[asyncio.Task] = set()
        self.importing: dict[str, asyncio.Task] = {}
        self.history_import_task: asyncio.Task | None = None
        self.replay_active = False
        self.live_logging_enabled = False
        self.live_monitor_task: asyncio.Task | None = None
        self.live_announcement_tasks: set[asyncio.Task] = set()
        self.live_announcement_metadata: dict[asyncio.Task, tuple[str, str]] = {}
        self.live_announcement_lock = asyncio.Lock()
        self.live_announcement_active = False
        self.live_cursor = 0
        self.live_session_id = None
        self.live_feed_id = None
        self.live_last_error = None
        self.live_seen = set()
        self.live_seen_order = deque()
        self.live_announcement_source = "raw_log"
        self.live_closed_attempts = deque(maxlen=500)


class Carter(discord.Client):
    def __init__(self, legacy_guild_id, allowed_users, settings=None, limits=SessionLimits(),
                 wake_settings=None, analysis_settings=AnalysisClientSettings(), *,
                 monitor_log_channel=True, backend_controller=None,
                 backend_health_interval=15, live_announcement_max_age=15,
                 live_announcement_poll_interval=0.5):
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
        self.backend_controller = backend_controller
        self.backend_health_interval = max(5, int(backend_health_interval))
        self.live_announcement_max_age = max(1, int(live_announcement_max_age))
        self.live_announcement_poll_interval = min(
            5.0, max(0.1, float(live_announcement_poll_interval)),
        )
        self.backend_supervisor_task = None
        self.backend_recovery_lock = asyncio.Lock()
        self.analysis_client = AnalysisClient(analysis_settings)
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
        carter.command(name="activate", description="Start one voice request and response")(self.activate)
        carter.command(name="stop", description="Stop AI speech and listening")(self.stop_ai)
        carter.command(name="replay", description="Replay Carter's latest voice response")(
            self.replay,
        )
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
        logs.command(name="live", description="Control live encounter voice announcements")(
            self.logs_live,
        )
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
        logs.command(name="reset", description="Clear this server's Warcraft Logs import data")(
            self.logs_reset,
        )
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
        if self.backend_controller is not None and self.backend_supervisor_task is None:
            self.backend_supervisor_task = asyncio.create_task(
                self._supervise_analysis_backend(), name="carter-analysis-backend",
            )

    async def _supervise_analysis_backend(self):
        """Restart Carter's local backend if its health endpoint stops responding."""
        while True:
            await asyncio.sleep(self.backend_health_interval)
            try:
                await self.analysis_client.health()
                continue
            except AnalysisClientError as error:
                log.warning("Raid analysis backend health check failed: %s", error.code)
            result = await self._recover_analysis_backend()
            if result.ok:
                log.info("Raid analysis backend recovered: %s", result.code)
            else:
                log.error("Raid analysis backend recovery failed: %s", result.code)

    async def _recover_analysis_backend(self):
        """Serialize local backend recovery so Discord and the supervisor cannot race."""
        if self.backend_controller is None:
            return None
        async with self.backend_recovery_lock:
            return await asyncio.to_thread(self.backend_controller.ensure_running)

    async def _analysis_request(self, operation):
        """Retry one request after recovering Carter's managed loopback backend."""
        try:
            return await operation()
        except AnalysisClientError as error:
            if error.code != "analysis_unavailable" or self.backend_controller is None:
                raise
            result = await self._recover_analysis_backend()
            if result is None or not result.ok:
                code = result.code if result is not None else "not_managed"
                log.error("On-demand raid analysis recovery failed: %s", code)
                raise
            log.info("On-demand raid analysis backend recovery: %s", result.code)
            return await operation()

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

    async def _rearm_after_exchange(self, state):
        """Return a completed one-response exchange to local wake-only mode."""
        async with state.voice_lock:
            guild = self.get_guild(state.guild_id)
            voice = (getattr(state.session, "voice", None)
                     or getattr(state.wake, "voice", None)
                     or (getattr(guild, "voice_client", None) if guild else None))
            if (not state.want_voice or not voice or not voice.is_connected()
                    or not voice.channel or voice.channel.id != state.channel_id):
                return
            if state.wake.running and state.wake.voice is voice:
                state.wake.reset()
                log.info(
                    "One-response exchange finished; wake listener rearmed for '%s'",
                    state.wake.settings.word,
                )
                return
            await self._arm_wake(state, voice)
            if state.wake.state == "armed":
                log.info(
                    "One-response exchange finished; wake listener restarted for '%s'",
                    state.wake.settings.word,
                )

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
                    or state.replay_active or state.live_announcement_active
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
                answer = await self._analysis_request(
                    lambda: state.raid.execute_tool(
                        state.analysis_state, call.get("arguments") or {}, owner_id,
                    ),
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
                    answer = await self._analysis_request(
                        lambda: state.raid.answer_question(
                            state.analysis_state, prompt, interaction.user.id,
                        ),
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
            if state.session.active or state.live_announcement_active:
                await interaction.followup.send(
                    "Carter is already speaking or listening. Try again shortly.", ephemeral=True,
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
                "Others can hear the one reply; then wake listening rearms automatically."
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

    async def replay(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            voice = interaction.guild.voice_client
            member_voice = getattr(interaction.user, "voice", None)
            if not voice or not voice.is_connected() or not isinstance(voice, CarterVoiceClient):
                await interaction.followup.send("Use `/carter join` first.", ephemeral=True)
                return
            if (not member_voice or not member_voice.channel
                    or member_voice.channel.id != state.channel_id
                    or voice.channel.id != state.channel_id):
                await interaction.followup.send(
                    "Join the saved voice channel first.", ephemeral=True,
                )
                return
            if (state.session.active or state.replay_active
                    or state.live_announcement_active):
                await interaction.followup.send(
                    "Carter is already speaking or listening.", ephemeral=True,
                )
                return
            pcm = state.session.replay_audio()
            if not pcm:
                await interaction.followup.send(
                    "There is no recent Carter response to replay.", ephemeral=True,
                )
                return

            state.replay_active = True
            state.wake.reset(cooldown=30)
            playback = PCMPlayback()
            try:
                voice.play(playback)
                playback.push(pcm)
                playback.finish_turn()
                await interaction.followup.send(
                    "Replaying Carter's latest response without using AI tokens.",
                    ephemeral=True,
                )
                deadline = time.monotonic() + 25
                while (voice.is_connected() and not playback.is_idle()
                       and time.monotonic() < deadline):
                    await asyncio.sleep(0.05)
            finally:
                voice.stop()
                playback.cleanup()
                state.replay_active = False
                state.wake.reset(cooldown=1)

    async def leave(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        async with state.voice_lock:
            state.want_voice = False
            await state.wake.stop()
            await state.session.stop()
            state.session.clear_replay()
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
            f"{state.analysis_state.source}; live logging: "
            f"{'on' if state.live_logging_enabled else 'off'}; "
            f"OpenAI fallback enabled: {session.limits.allow_fallback}; "
            f"audio sent/generated: {session.input_seconds:.1f}s/{session.output_seconds:.1f}s; "
            f"mic frames/turns/provider events/tools: {session.input_frames}/"
            f"{session.speech_turns}/{session.provider_events}/{session.tool_calls}; "
            f"other-speaker frames dropped: {state.wake.dropped_non_owner_frames}; "
            f"last stop: {session.reason}; last error: "
            f"{session.last_error or state.wake.last_error or 'none'}{token_status}.",
            ephemeral=True,
        )

    async def logs_live(self, interaction: discord.Interaction,
                        mode: Literal["on", "off", "status"] = "status",
                        source: Literal["raw_log", "companion"] | None = None):
        """Opt in one guild to fresh live encounter announcements."""
        if not await self.authorize(interaction):
            return
        state = self.runtime(interaction.guild_id)
        if source is not None:
            if not self.analysis_settings.enabled:
                await interaction.followup.send("Raid-log analysis is disabled.", ephemeral=True)
                return
            try:
                selected = await self._analysis_request(
                    lambda: state.raid.client.live_announcement_source(source))
            except AnalysisClientError as error:
                await interaction.followup.send(user_error(error), ephemeral=True)
                return
            was_enabled = state.live_logging_enabled
            await self._stop_live_logging(state)
            state.live_announcement_source = selected["announcement_source"]
            if was_enabled and mode == "status":
                mode = "on"
        if mode == "status":
            if source is None and self.analysis_settings.enabled:
                try:
                    selected = await self._analysis_request(
                        state.raid.client.live_announcement_source)
                    state.live_announcement_source = selected["announcement_source"]
                except AnalysisClientError as error:
                    await interaction.followup.send(user_error(error), ephemeral=True)
                    return
            await interaction.followup.send(
                self._live_logging_status(state), ephemeral=True,
            )
            return
        if mode == "off":
            await self._stop_live_logging(state)
            await interaction.followup.send(
                "Live logging is off for this server. Encounter announcements are disabled.",
                ephemeral=True,
            )
            return
        if not self.analysis_settings.enabled:
            await interaction.followup.send(
                "Live logging cannot start because raid-log analysis is disabled.",
                ephemeral=True,
            )
            return
        if state.live_logging_enabled:
            await interaction.followup.send(
                self._live_logging_status(state), ephemeral=True,
            )
            return
        try:
            status = await self._analysis_request(state.raid.client.live_announcement_status)
        except AnalysisClientError as error:
            log.warning(
                "Live logging enable failed: guild=%s reason=%s",
                state.guild_id, error.code,
            )
            await interaction.followup.send(user_error(error), ephemeral=True)
            return
        if not status.get("configured") or not status.get("available"):
            await interaction.followup.send(
                "The live combat-log source is not available, so live logging was not enabled.",
                ephemeral=True,
            )
            return
        state.live_cursor = max(0, int(status.get("cursor") or 0))
        state.live_session_id = str(status.get("session_id") or "") or None
        state.live_feed_id = (
            str(status.get("feed_id") or "") or state.live_session_id
        )
        state.live_last_error = None
        state.live_announcement_source = status.get("announcement_source", "raw_log")
        state.live_closed_attempts.clear()
        state.live_seen.clear()
        state.live_seen_order.clear()
        state.live_logging_enabled = True
        state.live_monitor_task = asyncio.create_task(
            self._monitor_live_encounters(state),
            name=f"carter-live-log-{state.guild_id}",
        )
        voice = interaction.guild.voice_client
        connected = bool(
            voice and voice.is_connected() and voice.channel
            and voice.channel.id == state.channel_id
        )
        log.info(
            "Live logging enabled: guild=%s session=%s cursor=%s voice_connected=%s",
            state.guild_id, state.live_session_id or "unknown", state.live_cursor, connected,
        )
        message = (
            "Live logging is on for this server. Carter will announce fresh encounter starts, "
            "ends, and any matched guide callouts "
            f"(up to {self.live_announcement_max_age} seconds old)."
        )
        if not connected:
            message += " Carter is not in the saved voice channel; use `/carter join` when ready."
        await interaction.followup.send(message, ephemeral=True)

    def _live_logging_status(self, state):
        value = "on" if state.live_logging_enabled else "off"
        detail = f" Last monitor error: {state.live_last_error}." if state.live_last_error else ""
        return (
            f"Live logging is {value} for this server; announcement source: "
            f"{state.live_announcement_source}. The freshness limit is "
            f"{self.live_announcement_max_age} seconds.{detail}"
        )

    async def _stop_live_logging(self, state):
        state.live_logging_enabled = False
        current = asyncio.current_task()
        monitor = state.live_monitor_task
        state.live_monitor_task = None
        if monitor is not None and monitor is not current and not monitor.done():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        tasks = [task for task in state.live_announcement_tasks
                 if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        state.live_announcement_tasks.clear()
        state.live_announcement_metadata.clear()
        state.live_announcement_active = False

    async def _monitor_live_encounters(self, state):
        """Poll only this guild's private loopback feed and dispatch lifecycle events."""
        while state.live_logging_enabled:
            try:
                payload = await self._analysis_request(
                    lambda: state.raid.client.live_announcement_updates(state.live_cursor, 100),
                )
            except AnalysisClientError as error:
                if error.code == "CURSOR_EXPIRED":
                    state.live_cursor = max(0, int(error.details.get("oldest_cursor") or 1) - 1)
                    reason = "cursor_expired"
                else:
                    reason = error.code
                if state.live_last_error != reason:
                    log.warning(
                        "Live logging monitor unavailable: guild=%s reason=%s",
                        state.guild_id, reason,
                    )
                state.live_last_error = reason
                await asyncio.sleep(self.live_announcement_poll_interval)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reason = type(error).__name__
                if state.live_last_error != reason:
                    log.warning(
                        "Live logging monitor failed: guild=%s reason=%s",
                        state.guild_id, reason,
                    )
                state.live_last_error = reason
                await asyncio.sleep(self.live_announcement_poll_interval)
                continue

            state.live_last_error = None
            session_id = str(payload.get("session_id") or "") or None
            feed_id = str(payload.get("feed_id") or "") or session_id
            payload_cursor = max(0, int(payload.get("cursor") or 0))
            if (session_id != state.live_session_id
                    or feed_id != state.live_feed_id
                    or payload_cursor < state.live_cursor):
                # Feed/source changes invalidate all pending speech from the old producer.
                for pending in list(state.live_announcement_tasks):
                    pending.cancel()
                state.live_closed_attempts.clear()
                state.live_announcement_source = payload.get("announcement_source", "raw_log")
                log.info(
                    "Live logging stream reset: guild=%s old_session=%s new_session=%s "
                    "old_feed=%s new_feed=%s old_cursor=%s new_cursor=%s",
                    state.guild_id, state.live_session_id or "unknown",
                    session_id or "unknown", state.live_feed_id or "unknown",
                    feed_id or "unknown", state.live_cursor, payload_cursor,
                )
                state.live_session_id = session_id
                state.live_feed_id = feed_id
                state.live_cursor = 0
                state.live_seen.clear()
                state.live_seen_order.clear()
                continue

            updates = payload.get("updates")
            if not isinstance(updates, list):
                updates = []
            for event in updates:
                if not isinstance(event, dict):
                    continue
                state.live_cursor = max(state.live_cursor, int(event.get("cursor") or 0))
                self._queue_live_lifecycle(state, event)
            if payload.get("truncated"):
                continue
            await asyncio.sleep(self.live_announcement_poll_interval)

    def _queue_live_lifecycle(self, state, event):
        """Validate and queue lifecycle, test, or curated guide speech."""
        event_type = str(event.get("type") or "")
        is_test = event_type == "ANNOUNCEMENT_TEST_REQUESTED"
        is_callout = event_type == "MECHANIC_CALLOUT"
        if event_type not in {
                "ENCOUNTER_STARTED", "ENCOUNTER_ENDED", "ANNOUNCEMENT_TEST_REQUESTED",
                "MECHANIC_CALLOUT"}:
            return
        attempt_id = str(event.get("attempt_id") or event.get("request_id") or "")
        attempt_key = (str(event.get("session_id") or ""), attempt_id)
        if event_type == "ENCOUNTER_ENDED":
            # Close even an expired result: queued mechanics must never outlive the fight.
            if attempt_key not in state.live_closed_attempts:
                state.live_closed_attempts.append(attempt_key)
            for pending, metadata in list(state.live_announcement_metadata.items()):
                if metadata == (attempt_id, "MECHANIC_CALLOUT") and not pending.done():
                    pending.cancel()
        elif is_callout and attempt_key in state.live_closed_attempts:
            return
        elif event_type == "ENCOUNTER_STARTED" and event.get("announcement_source") == "companion":
            for pending, metadata in list(state.live_announcement_metadata.items()):
                if metadata[1] == "MECHANIC_CALLOUT" and metadata[0] != attempt_id:
                    pending.cancel()
        boss_name = " ".join(str(event.get("encounter_name") or "").split())[:120]
        encounter_id = event.get("encounter_id")
        result = str(event.get("result") or "").upper() or None
        mechanic_id = str(event.get("mechanic_id") or "")[:80]
        try:
            occurrence = int(event.get("occurrence")) if is_callout else None
        except (TypeError, ValueError):
            occurrence = None
        callout_text = " ".join(str(event.get("message") or "").split())[:240]
        callout_text_valid = bool(callout_text) and all(
            ord(character) >= 32 for character in callout_text)
        # Prefer the combat-log timestamp and calculate age at dispatch time.
        # The backend's event_age_seconds is only an ingest-time snapshot and
        # must not keep a retained update artificially fresh while it waits to
        # be polled.  Events without a log timestamp (the authenticated test
        # button) accrue elapsed time from their backend receipt timestamp.
        try:
            timestamp_field = (
                "log_end_timestamp"
                if event_type == "ENCOUNTER_ENDED" else "effective_log_timestamp"
            )
            event_timestamp = float(event[timestamp_field])
            if not math.isfinite(event_timestamp):
                raise ValueError
            age = max(0.0, time.time() - event_timestamp)
        except (KeyError, TypeError, ValueError):
            try:
                age = float(event.get("event_age_seconds"))
                if not math.isfinite(age):
                    raise ValueError
                age = max(0.0, age)
                received_at = event.get("received_at")
                if received_at:
                    received = datetime.fromisoformat(
                        str(received_at).replace("Z", "+00:00"))
                    if received.tzinfo is None:
                        raise ValueError
                    age += max(0.0, time.time() - received.timestamp())
            except (TypeError, ValueError, OverflowError):
                age = None
        allowed_age = float(self.live_announcement_max_age)
        if is_callout:
            try:
                profile_age = float(event.get("max_lateness_seconds"))
                if not math.isfinite(profile_age) or profile_age <= 0:
                    raise ValueError
                allowed_age = min(allowed_age, profile_age)
            except (TypeError, ValueError):
                allowed_age = 0.0
        if is_test:
            log.info(
                "Live announcement test received: guild=%s request=%s event_age_seconds=%s",
                state.guild_id, attempt_id or "missing",
                f"{age:.3f}" if age is not None else "unknown",
            )
        elif is_callout:
            log.info(
                "Live guide callout received: guild=%s attempt=%s boss=%s "
                "encounter_id=%s profile=%s mechanic=%s occurrence=%s "
                "event_age_seconds=%s",
                state.guild_id, attempt_id or "missing", boss_name or "missing",
                encounter_id if encounter_id is not None else "missing",
                str(event.get("profile_id") or "missing")[:80], mechanic_id or "missing",
                occurrence if occurrence is not None else "missing",
                f"{age:.3f}" if age is not None else "unknown",
            )
        else:
            log.info(
                "Live encounter lifecycle received: guild=%s attempt=%s boss=%s "
                "encounter_id=%s result=%s event_age_seconds=%s",
                state.guild_id, attempt_id or "missing", boss_name or "missing",
                encounter_id if encounter_id is not None else "missing", result or "n/a",
                f"{age:.3f}" if age is not None else "unknown",
            )
        identity = ((mechanic_id, occurrence) if is_callout else event_type)
        key = (str(event.get("session_id") or state.live_session_id or ""),
               attempt_id, event_type, identity)
        if key in state.live_seen:
            log.info(
                "Live encounter announcement suppressed: guild=%s attempt=%s event=%s "
                "reason=duplicate",
                state.guild_id, attempt_id or "missing", event_type,
            )
            return
        if len(state.live_seen_order) >= 500:
            state.live_seen.discard(state.live_seen_order.popleft())
        state.live_seen.add(key)
        state.live_seen_order.append(key)
        if not state.live_logging_enabled:
            reason = "live_logging_disabled"
        elif not attempt_id:
            reason = "invalid_lifecycle_event"
        elif not is_test and (not boss_name or encounter_id is None):
            reason = "invalid_lifecycle_event"
        elif is_callout and (not mechanic_id or occurrence is None or occurrence < 1
                             or not callout_text_valid or allowed_age <= 0):
            reason = "invalid_guide_callout"
        elif age is None:
            reason = "event_age_unavailable"
        elif age > allowed_age:
            reason = "stale_event"
        elif event_type == "ENCOUNTER_ENDED" and result not in {"KILL", "WIPE"}:
            reason = "invalid_encounter_result"
        else:
            reason = None
        if reason:
            log.info(
                "Live encounter announcement suppressed: guild=%s attempt=%s event=%s "
                "reason=%s event_age_seconds=%s",
                state.guild_id, attempt_id or "missing", event_type, reason,
                f"{age:.3f}" if age is not None else "unknown",
            )
            return
        if is_test:
            text = "I have received the test prompt"
        elif is_callout:
            text = callout_text
        else:
            text = (f"Good luck on {boss_name}!" if event_type == "ENCOUNTER_STARTED"
                    else "Good job!" if result == "KILL" else "That sucks!")
        if event_type == "ENCOUNTER_ENDED":
            # A result is authoritative for the attempt.  Stop any tactical
            # guidance that is queued or currently playing so it cannot trail
            # the kill/wipe announcement.
            for pending, metadata in list(state.live_announcement_metadata.items()):
                pending_attempt, pending_type = metadata
                if (pending_attempt == attempt_id
                        and pending_type == "MECHANIC_CALLOUT"
                        and not pending.done()):
                    pending.cancel()
        expires_at = time.monotonic() + max(0.0, allowed_age - (age or 0.0))
        task_suffix = f"{attempt_id}-{mechanic_id}-{occurrence}" if is_callout else attempt_id
        task = asyncio.create_task(
            self._deliver_live_announcement(
                state, attempt_id, event_type, text, expires_at=expires_at),
            name=f"carter-live-announcement-{state.guild_id}-{task_suffix}",
        )
        state.live_announcement_tasks.add(task)
        state.live_announcement_metadata[task] = (attempt_id, event_type)

        def forget(completed):
            state.live_announcement_tasks.discard(completed)
            state.live_announcement_metadata.pop(completed, None)

        task.add_done_callback(forget)
        log.info(
            "Live encounter announcement queued: guild=%s attempt=%s event=%s",
            state.guild_id, attempt_id, event_type,
        )

    async def _deliver_live_announcement(self, state, attempt_id, event_type, text,
                                         *, expires_at=None):
        try:
            async with state.live_announcement_lock:
                async with state.voice_lock:
                    if expires_at is not None and time.monotonic() > expires_at:
                        reason = "expired_in_queue"
                    elif not state.live_logging_enabled:
                        reason = "live_logging_disabled"
                    else:
                        guild = self.get_guild(state.guild_id)
                        voice = getattr(guild, "voice_client", None) if guild else None
                        if (not state.want_voice or not voice or not voice.is_connected()
                                or not voice.channel or voice.channel.id != state.channel_id):
                            reason = "voice_unavailable"
                        elif (state.session.active or state.replay_active
                              or state.live_announcement_active or voice.is_playing()):
                            reason = "voice_busy"
                        else:
                            reason = None
                    if reason:
                        method = log.info if reason == "expired_in_queue" else log.warning
                        method(
                            "Live encounter announcement %s: guild=%s attempt=%s event=%s "
                            "reason=%s",
                            "suppressed" if reason == "expired_in_queue" else "failed",
                            state.guild_id, attempt_id, event_type, reason)
                        return
                    state.live_announcement_active = True
                    try:
                        await state.live_announcer.speak(voice, text)
                    finally:
                        state.live_announcement_active = False
            log.info(
                "Live encounter announcement completed: guild=%s attempt=%s event=%s",
                state.guild_id, attempt_id, event_type,
            )
        except asyncio.CancelledError:
            log.info(
                "Live encounter announcement suppressed: guild=%s attempt=%s event=%s "
                "reason=cancelled",
                state.guild_id, attempt_id, event_type,
            )
            raise
        except Exception as error:
            code = error.code if isinstance(error, ProviderError) else type(error).__name__
            log.warning(
                "Live encounter announcement failed: guild=%s attempt=%s event=%s reason=%s",
                state.guild_id, attempt_id, event_type, code,
            )

    async def logs_health(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        try:
            result = await self._analysis_request(self.analysis_client.health)
            await interaction.followup.send(
                f"Analysis backend: {result.get('status', 'ready')}; schema "
                f"{result.get('schema_version', 'unknown')}.", ephemeral=True,
            )
        except AnalysisClientError as error:
            await interaction.followup.send(user_error(error), ephemeral=True)

    async def logs_reports(self, interaction: discord.Interaction,
                           limit: app_commands.Range[int, 1, 20] = 10):
        await self._analysis_command(interaction, lambda state: state.raid.reports(limit))

    async def logs_fights(self, interaction: discord.Interaction, report_id: str = ""):
        await self._analysis_command(
            interaction,
            lambda state: state.raid.fights(state.analysis_state, report_id or None),
        )

    async def logs_select(self, interaction: discord.Interaction, report_id: str,
                          fight_id: app_commands.Range[int, 1] | None = None):
        async def select(state):
            answer = await state.raid.select_report(state.analysis_state, report_id)
            if fight_id is not None:
                answer = await state.raid.select_fight(state.analysis_state, fight_id)
            return answer
        await self._analysis_command(interaction, select, save=True)

    async def logs_source(self, interaction: discord.Interaction,
                          source: Literal["stored", "live"],
                          attempt: app_commands.Range[int, 1] | None = None):
        async def select(state):
            if source == "live":
                return await state.raid.select_live(state.analysis_state, attempt)
            state.analysis_state.source, state.analysis_state.live_attempt = "stored", None
            resolved = await state.raid.resolve_source(state.analysis_state, "")
            return RaidAnswer(f"Selected stored analysis: {resolved.label}.")
        await self._analysis_command(interaction, select, save=True)

    async def logs_summary(self, interaction: discord.Interaction):
        await self._analysis_command(
            interaction, lambda state: state.raid.summary(state.analysis_state),
        )

    async def logs_ranking(self, interaction: discord.Interaction,
                           metric: Literal["dps", "hps"] = "dps",
                           limit: app_commands.Range[int, 1, 20] = 10):
        await self._analysis_command(
            interaction,
            lambda state: state.raid.ranking(state.analysis_state, metric, limit),
        )

    async def logs_player(self, interaction: discord.Interaction,
                          player: app_commands.Range[str, 1, 100]):
        await self._analysis_command(
            interaction, lambda state: state.raid.player(state.analysis_state, player),
        )

    async def logs_death(self, interaction: discord.Interaction, player: str = "",
                         death_number: app_commands.Range[int, 1] | None = None):
        async def explain(state):
            analysis = state.analysis_state
            name = player.strip() or analysis.identities.get(str(interaction.user.id), "")
            if not name:
                raise AnalysisClientError("identity_required")
            return await state.raid.death(analysis, name, death_number)
        await self._analysis_command(interaction, explain)

    async def logs_deaths(self, interaction: discord.Interaction):
        await self._analysis_command(
            interaction, lambda state: state.raid.death_timeline(state.analysis_state),
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
                          report_url: app_commands.Range[str, 1, 500],
                          refresh: bool = False):
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
            queued = await state.raid.submit_report(match.group(0), refresh=refresh)
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
        state = self.runtime(interaction.guild_id)
        try:
            payload = await state.raid.client.import_job(job_id)
            await interaction.followup.send(_format_job(payload), ephemeral=True)
        except AnalysisClientError as error:
            await interaction.followup.send(user_error(error), ephemeral=True)

    async def logs_reset(self, interaction: discord.Interaction, confirm: bool):
        """Clear only this guild's downloaded reports and import history."""
        is_admin = bool(getattr(
            getattr(interaction.user, "guild_permissions", None), "administrator", False,
        ))
        if interaction.guild_id is None or not is_admin:
            await interaction.response.send_message(
                "Only a server administrator can reset Warcraft Logs data.",
                ephemeral=True,
            )
            return
        if not confirm:
            await interaction.response.send_message(
                "No data was deleted. Run `/logs reset confirm:True` to remove this server's "
                "downloaded report caches, analyses, partial-download checkpoints, and import "
                "history. Other servers and Carter settings are not affected.",
                ephemeral=True,
            )
            return

        state = self.runtime(interaction.guild_id)
        if (self.import_lock.locked()
                or any(not task.done() for task in state.import_tasks)):
            await interaction.response.send_message(
                "A report import is still active or queued. Wait for it to finish before resetting.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await state.raid.client.reset_import_data()
        except AnalysisClientError as error:
            if error.code == "IMPORTS_ACTIVE":
                message = (
                    "A report import is still active or queued on the backend. "
                    "Wait for it to finish before resetting."
                )
            else:
                message = user_error(error)
            await interaction.followup.send(message, ephemeral=True)
            return

        await interaction.followup.send(
            f"Reset complete for this server: removed data for "
            f"{int(result.get('reports_removed') or 0)} report(s), including cached reports, "
            f"analysis files, partial-download checkpoints, and retained import history. "
            "Carter settings and other servers' data were not changed.",
            ephemeral=True,
        )

    async def _analysis_command(self, interaction, operation, *, save=False):
        if not await self.authorize(interaction, ephemeral=False):
            return
        state = self.runtime(interaction.guild_id)
        try:
            async with state.analysis_lock:
                answer = await self._analysis_request(lambda: operation(state))
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
                    queued = await state.raid.submit_report(url)
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
                payload = await state.raid.client.import_job(job_id)
                status = str(payload.get("status") or payload.get("state") or "").casefold()
                if status in {"succeeded", "partial", "failed", "rate_limited"}:
                    break
                await asyncio.sleep(2)
            else:
                raise AnalysisClientError("analysis_timeout")
            status = str(payload.get("status") or payload.get("state") or "").casefold()
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            first_error = errors[0] if errors and isinstance(errors[0], dict) else {}
            if status not in {"succeeded", "partial"}:
                details = {key: first_error[key] for key in (
                    "checkpoint_saved", "retryable", "retry_after_seconds",
                ) if key in first_error}
                raise AnalysisClientError(
                    str(first_error.get("code") or "import_failed"), details=details,
                )
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
                    f"{' (local cache reused)' if result.get('cache_hit') else ''}"
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
                message = (
                    (f"Warcraft Logs rate limit reached for `{report_id}`. Downloaded pages were "
                     "saved; run the import again later to resume.")
                    if error.code == "RATE_LIMITED" and error.details.get("checkpoint_saved") else
                    (f"Warcraft Logs rate limit reached for `{report_id}` before a page could be "
                     "saved; retry the import later.")
                    if error.code == "RATE_LIMITED" else
                    (f"Import interrupted for `{report_id}`. Downloaded pages were saved; run the "
                     "import again to resume.")
                    if error.code == "DOWNLOAD_FAILED" and error.details.get("checkpoint_saved") else
                    f"Could not prepare report `{report_id}` ({error.code})."
                )
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
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
            if self.backend_supervisor_task is not None:
                self.backend_supervisor_task.cancel()
                await asyncio.gather(self.backend_supervisor_task, return_exceptions=True)
                self.backend_supervisor_task = None
            for state in self.guilds_runtime.values():
                await self._stop_live_logging(state)
                for task in list(state.import_tasks):
                    task.cancel()
                if state.import_tasks:
                    await asyncio.gather(*state.import_tasks, return_exceptions=True)
                await state.wake.stop()
                await state.session.stop()
            await self.analysis_client.close()
            if self.backend_controller is not None:
                result = await asyncio.to_thread(self.backend_controller.stop)
                if not result.ok:
                    log.warning("Raid analysis backend cleanup failed: %s", result.code)
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
    manage_backend, backend_startup_timeout, backend_health_interval = \
        analysis_backend_configuration(analysis_settings)
    settings, limits = ai_configuration(analysis_tools=analysis_settings.enabled)
    live_max_age, live_poll_interval = live_announcement_configuration()
    with (ROOT / ".carter.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Carter is already running from this directory") from None
        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        backend_controller = None
        try:
            if manage_backend:
                try:
                    backend_controller = AnalysisBackendController(
                        analysis_settings.base_url, ROOT,
                        startup_timeout=backend_startup_timeout,
                    )
                except ValueError as error:
                    raise ConfigurationError(str(error)) from None
                backend_result = backend_controller.ensure_running()
                if not backend_result.ok:
                    raise RuntimeError(
                        f"Raid analysis backend startup failed: {backend_result.code}"
                    )
                log.info("Raid analysis backend lifecycle ready: %s", backend_result.code)
            bot = Carter(
                legacy_guild_id, users, settings, limits, wake_configuration(),
                analysis_settings, monitor_log_channel=monitor,
                backend_controller=backend_controller,
                backend_health_interval=backend_health_interval,
                live_announcement_max_age=live_max_age,
                live_announcement_poll_interval=live_poll_interval,
            )
            bot.run(token, log_handler=None)
        finally:
            if backend_controller is not None:
                backend_result = backend_controller.stop()
                if not backend_result.ok:
                    log.warning("Raid analysis backend cleanup failed: %s", backend_result.code)
            lock.seek(0)
            lock.truncate()
            lock.flush()


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
