"""Independent Discord voice assistant with local wake-name activation."""
import asyncio
import fcntl
import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from discord_receive import CarterVoiceClient
from providers import ProviderError, ProviderSettings
from session import SessionLimits, VoiceSession
from wake_listener import WakeListener, WakeSettings

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("carter")


class ConfigurationError(ValueError):
    """Configuration errors with safe, constant messages for startup logs."""


def positive_id(name):
    value = os.getenv(name, "").strip()
    if not value.isdecimal() or int(value) <= 0:
        raise ConfigurationError(f"{name} must be a positive Discord ID")
    return int(value)


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
        model_path=str(model_path), auto_join=env_bool("CARTER_AUTO_JOIN", True),
    )


def ai_configuration():
    if os.getenv("CARTER_AI_PROVIDER", "gemini").strip().lower() != "gemini":
        raise ConfigurationError("CARTER_AI_PROVIDER must be gemini; OpenAI is opt-in fallback only")
    fallback = os.getenv("CARTER_ALLOW_OPENAI_FALLBACK", "false").strip().lower()
    if fallback not in {"true", "false"}:
        raise ConfigurationError("CARTER_ALLOW_OPENAI_FALLBACK must be true or false")
    settings = ProviderSettings(
        gemini_key=os.getenv("CARTER_GEMINI_API_KEY", "").strip(),
        gemini_model=os.getenv("CARTER_GEMINI_MODEL", "gemini-3.8-live").strip(),
        openai_key=os.getenv("CARTER_OPENAI_API_KEY", "").strip(),
        openai_model=os.getenv("CARTER_OPENAI_MODEL", "gpt-realtime-2.1").strip(),
        gemini_voice=os.getenv("CARTER_GEMINI_VOICE", "Kore").strip(),
        openai_voice=os.getenv("CARTER_OPENAI_VOICE", "marin").strip(),
    )
    limits = SessionLimits(
        inactivity=bounded_int("CARTER_SESSION_TIMEOUT", 45, 5, 300),
        max_seconds=bounded_int("CARTER_MAX_SESSION_SECONDS", 180, 10, 900),
        rms_threshold=bounded_int("CARTER_INPUT_RMS_THRESHOLD", 300, 0, 32767),
        allow_fallback=fallback == "true",
    )
    return settings, limits


class Carter(discord.Client):
    def __init__(self, guild_id, channel_id, allowed_users, settings=None, limits=SessionLimits(),
                 wake_settings=None):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.allowed_users = allowed_users
        self.voice_lock = asyncio.Lock()
        self.session = VoiceSession(settings or ProviderSettings(), limits)
        self.wake = WakeListener(self.session, allowed_users, wake_settings or WakeSettings(), self._wake_activate)
        self.want_voice = self.wake.settings.auto_join
        self.tree = app_commands.CommandTree(self)
        group = app_commands.Group(name="carter", description="Control Carter")
        group.command(name="join", description="Join the configured voice channel")(self.join)
        group.command(name="leave", description="Leave voice")(self.leave)
        group.command(name="status", description="Show Carter connection status")(self.status)
        group.command(name="activate", description="Start a voice conversation using your microphone")(self.activate)
        group.command(name="stop", description="Stop AI speech and listening")(self.stop_ai)
        self.tree.add_command(group, guild=discord.Object(id=guild_id))
        self.tree.on_error = self.command_error

    async def setup_hook(self):
        await self.tree.sync(guild=discord.Object(id=self.guild_id))

    async def on_ready(self):
        log.info("Connected as bot ID %s", self.user.id)
        if self.want_voice:
            async with self.voice_lock:
                guild = self.get_guild(self.guild_id)
                channel = guild.get_channel(self.channel_id) if guild else None
                if isinstance(channel, discord.VoiceChannel):
                    try:
                        await self._connect_voice(guild, channel)
                    except Exception as error:
                        log.warning("Automatic voice join failed: %s; use /carter join to retry", type(error).__name__)

    async def _arm_wake(self, voice):
        if (not self.wake.settings.enabled or self.session.active
                or not voice.channel or voice.channel.id != self.channel_id):
            return
        # Main's lock serializes listener changes against activation and leave.
        try:
            await self.wake.start(voice)
        except Exception as error:
            log.warning("Wake setup failed: %s; run .venv/bin/python setup_wakeword.py", type(error).__name__)

    async def _connect_voice(self, guild, channel):
        voice = guild.voice_client
        if not voice or not voice.is_connected():
            if voice:
                await voice.disconnect(force=True)
            permissions = channel.permissions_for(guild.me)
            if not (permissions.view_channel and permissions.connect and permissions.speak):
                raise ConfigurationError("Carter needs View Channel, Connect, and Speak permissions")
            voice = await channel.connect(cls=CarterVoiceClient, timeout=20, reconnect=True, self_deaf=False)
        await self._arm_wake(voice)
        return voice

    async def _wake_activate(self, user_id, voice, generation):
        async with self.voice_lock:
            if (generation != self.wake.generation or not self.wake.running
                    or self.wake.voice is not voice or not voice.is_connected()
                    or voice.channel.id != self.channel_id or self.session.active
                    or user_id not in self.allowed_users):
                return False
            member = voice.guild.get_member(user_id)
            member_voice = getattr(member, "voice", None)
            if not member_voice or not member_voice.channel or member_voice.channel.id != self.channel_id:
                return False

            async def notice(message):
                log.info("%s", message)

            try:
                await self.session.start(voice, user_id, notice, external_capture=True)
            except Exception as error:
                code = error.code if isinstance(error, ProviderError) else type(error).__name__
                log.warning("Wake activation failed: %s", code)
                return False
            log.info("Wake word detected; conversation active for user ID %s", user_id)
            return True

    async def authorize(self, interaction):
        if interaction.guild_id != self.guild_id or interaction.user.id not in self.allowed_users:
            await interaction.response.send_message("You are not authorized to control Carter.", ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True)
        return True

    async def join(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        async with self.voice_lock:
            channel = interaction.guild.get_channel(self.channel_id)
            member_voice = getattr(interaction.user, "voice", None)
            if not isinstance(channel, discord.VoiceChannel):
                await interaction.followup.send("The configured voice channel is unavailable.", ephemeral=True)
                return
            if not member_voice or member_voice.channel.id != channel.id:
                await interaction.followup.send("Join the configured voice channel first.", ephemeral=True)
                return
            self.want_voice = True
            voice = interaction.guild.voice_client
            if voice and voice.is_connected():
                await self._arm_wake(voice)
                message = "Carter is already connected."
            else:
                if voice:
                    await voice.disconnect(force=True)
                try:
                    await self._connect_voice(interaction.guild, channel)
                except Exception:
                    stale = interaction.guild.voice_client
                    if stale:
                        await stale.disconnect(force=True)
                    raise
                message = "Connected."
            if self.wake.state == "armed":
                message += f" Say '{self.wake.settings.word}' to wake Carter."
            elif self.wake.last_error:
                message += " Local wake listener unavailable; check model setup. /carter activate still works."
            elif not self.wake.settings.enabled:
                message += " Use /carter activate to start a conversation."
            await interaction.followup.send(message, ephemeral=True)

    async def activate(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        async with self.voice_lock:
            voice = interaction.guild.voice_client
            member_voice = getattr(interaction.user, "voice", None)
            if not voice or not voice.is_connected() or not isinstance(voice, CarterVoiceClient):
                await interaction.followup.send("Use /carter join first.", ephemeral=True)
                return
            if (not member_voice or member_voice.channel.id != self.channel_id
                    or voice.channel.id != self.channel_id):
                await interaction.followup.send("Join the configured voice channel first.", ephemeral=True)
                return
            if self.session.active:
                await interaction.followup.send("A conversation is active. Use /carter stop before starting another.", ephemeral=True)
                return

            async def notice(message):
                await interaction.followup.send(message, ephemeral=True)

            try:
                self.wake.reset()
                await self.session.start(voice, interaction.user.id, notice, external_capture=self.wake.running)
            except Exception as error:
                code = error.code if isinstance(error, ProviderError) else type(error).__name__
                log.warning("Activation failed: %s", code)
                await notice(f"AI activation failed ({code}). Check model access, API credentials, and voice permissions.")
                return
            await notice(f"Carter is listening to your microphone using {self.session.provider_name}. "
                         "Others in this voice channel can hear replies. Use /carter stop to end.")

    async def stop_ai(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        async with self.voice_lock:
            self.wake.reset()
            await self.session.stop()
            message = "AI stopped. Carter remains connected to Discord voice."
            if self.wake.running:
                message += f" Say '{self.wake.settings.word}' to wake Carter again."
            await interaction.followup.send(message, ephemeral=True)

    async def leave(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        async with self.voice_lock:
            self.want_voice = False
            await self.wake.stop()
            await self.session.stop()
            if interaction.guild.voice_client:
                await interaction.guild.voice_client.disconnect(force=True)
            await interaction.followup.send("Carter is disconnected.", ephemeral=True)

    async def status(self, interaction: discord.Interaction):
        if not await self.authorize(interaction):
            return
        voice = interaction.guild.voice_client
        connected = bool(voice and voice.is_connected())
        session = self.session
        model = (session.settings.gemini_model if session.provider_name == "gemini"
                 else session.settings.openai_model)
        tokens = session.usage.get("total_token_count", session.usage.get("total_tokens"))
        token_status = f"; last provider token total: {tokens}" if isinstance(tokens, int) else ""
        await interaction.followup.send(
            f"Voice connected: {connected}; wake listener: {self.wake.state}; AI: {session.state}; "
            f"provider: {session.provider_name}; model: {model}; "
            f"OpenAI fallback enabled: {session.limits.allow_fallback}; "
            f"audio sent/generated: {session.input_seconds:.1f}s/{session.output_seconds:.1f}s; "
            f"last stop: {session.reason}; last error: {session.last_error or self.wake.last_error or 'none'}{token_status}.",
            ephemeral=True,
        )

    async def on_voice_state_update(self, member, before, after):
        if member.guild.id != self.guild_id:
            return
        is_bot = self.user and member.id == self.user.id
        if (is_bot or member.id == self.session.owner_id) and (
                not after.channel or after.channel.id != self.channel_id):
            async with self.voice_lock:
                self.wake.reset()
                if is_bot:
                    await self.wake.stop()
                await self.session.stop()

    async def command_error(self, interaction, error):
        # Log only type; third-party exception messages can include private data.
        log.error("Command failed: %s", type(error).__name__)
        message = "Carter could not complete that command. Check channel permissions and connection."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def close(self):
        try:
            await self.wake.stop()
            await self.session.stop()
            for voice in list(self.voice_clients):
                try:
                    await voice.disconnect(force=True)
                except Exception as error:
                    log.warning("Voice cleanup failed: %s", type(error).__name__)
        finally:
            await super().close()


def main():
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s carter %(levelname)s %(message)s")
    # Third-party receiver debug/error logs can contain RTP bytes or keys.
    for namespace in ("discord", "google_genai", "httpx", "httpcore", "websockets"):
        logging.getLogger(namespace).setLevel(logging.CRITICAL)
    if os.getenv("CARTER_ENABLED", "false").lower() != "true":
        log.info("Disabled; set CARTER_ENABLED=true to start")
        return
    token = os.getenv("CARTER_DISCORD_TOKEN", "").strip()
    if not token:
        raise ConfigurationError("CARTER_DISCORD_TOKEN is required")
    guild_id = positive_id("CARTER_DISCORD_GUILD_ID")
    channel_id = positive_id("CARTER_DISCORD_VOICE_CHANNEL_ID")
    raw_users = os.getenv("CARTER_ALLOWED_USER_IDS", "").split(",")
    if not all(user.strip().isdecimal() and int(user.strip()) > 0 for user in raw_users):
        raise ConfigurationError("CARTER_ALLOWED_USER_IDS requires comma-separated positive user IDs")
    users = {int(user.strip()) for user in raw_users}
    settings, limits = ai_configuration()
    # Debian/local-process singleton. Keep the file: unlinking creates lock races.
    with (ROOT / ".carter.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Carter is already running from this directory") from None
        bot = Carter(guild_id, channel_id, users, settings, limits, wake_configuration())
        bot.run(token, log_handler=None)


if __name__ == "__main__":
    try:
        main()
    except ConfigurationError as error:
        log.error("%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        log.error("Startup failed (%s). Check Carter configuration and connectivity.", type(error).__name__)
        raise SystemExit(1) from None
