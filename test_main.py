"""Discord command authorization and lifecycle tests with no credentials/network."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from main import Carter, CarterVoiceClient, ai_configuration, wake_configuration
from wake_listener import WakeSettings


class ConfigurationTests(unittest.TestCase):
    def test_defaults_gemini_and_no_fallback(self):
        with patch.dict("os.environ", {}, clear=True):
            settings, limits = ai_configuration()
        self.assertEqual(settings.gemini_model, "gemini-3.8-live")
        self.assertFalse(limits.allow_fallback)
        self.assertEqual(limits.inactivity, 45)

    def test_invalid_provider_fallback_and_limits_rejected(self):
        for setting in ({"CARTER_AI_PROVIDER": "openai"},
                        {"CARTER_ALLOW_OPENAI_FALLBACK": "yes"},
                        {"CARTER_SESSION_TIMEOUT": "0"},
                        {"CARTER_MAX_SESSION_SECONDS": "999999"}):
            with patch.dict("os.environ", setting, clear=True):
                with self.assertRaises(ValueError):
                    ai_configuration()

    def test_wake_defaults_and_disable(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = wake_configuration()
        self.assertTrue(settings.enabled)
        self.assertTrue(settings.auto_join)
        self.assertEqual(settings.word, "Carter")
        with patch.dict("os.environ", {"CARTER_WAKE_WORD_ENABLED": "false",
                                      "CARTER_AUTO_JOIN": "false"}, clear=True):
            settings = wake_configuration()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.auto_join)


class CommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Carter(10, 20, {30}, wake_settings=WakeSettings(enabled=False, auto_join=False))
        self.fake_session = SimpleNamespace(active=False, owner_id=30,
            start=AsyncMock(), stop=AsyncMock(), provider_name="gemini")
        self.bot.session = self.fake_session
        self.channel = MagicMock()
        self.channel.id = 20
        self.channel.permissions_for.return_value = SimpleNamespace(
            view_channel=True, connect=True, speak=True,
        )
        self.guild = SimpleNamespace(voice_client=None, me=object(),
            get_channel=MagicMock(return_value=self.channel))
        self.interaction = SimpleNamespace(guild_id=10, guild=self.guild,
            user=SimpleNamespace(id=30, voice=SimpleNamespace(channel=self.channel)),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()))

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_command_registration_preserves_existing_names(self):
        group = self.bot.tree.get_command("carter", guild=SimpleNamespace(id=10))
        self.assertEqual({command.name for command in group.commands},
                         {"join", "leave", "status", "activate", "stop"})

    async def test_authorization_denies_wrong_user_and_guild(self):
        self.interaction.user.id = 999
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()
        self.interaction.response.send_message.assert_awaited_once()
        self.interaction.user.id = 30
        self.interaction.guild_id = 999
        await self.bot.leave(self.interaction)
        self.fake_session.stop.assert_not_awaited()

    async def test_join_uses_receive_client_and_stays_inactive(self):
        self.channel.connect = AsyncMock()
        with patch("main.discord.VoiceChannel", type(self.channel)):
            await self.bot.join(self.interaction)
        self.channel.connect.assert_awaited_once_with(
            cls=CarterVoiceClient, timeout=20, reconnect=True, self_deaf=False,
        )
        self.fake_session.start.assert_not_awaited()

    async def test_activate_requires_join_and_same_channel(self):
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()
        voice = MagicMock(spec=CarterVoiceClient)
        voice.is_connected.return_value = True
        voice.channel = self.channel
        self.guild.voice_client = voice
        self.interaction.user.voice = None
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_not_awaited()

    async def test_activate_accepts_owner_then_rejects_duplicate(self):
        voice = MagicMock(spec=CarterVoiceClient)
        voice.is_connected.return_value = True
        voice.channel = self.channel
        self.guild.voice_client = voice
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_awaited_once()
        self.assertEqual(self.fake_session.start.await_args.args[:2], (voice, 30))
        self.fake_session.active = True
        await self.bot.activate(self.interaction)
        self.fake_session.start.assert_awaited_once()

    async def test_leave_closes_ai_before_disconnecting(self):
        calls = []
        self.fake_session.stop.side_effect = lambda: calls.append("stop")
        voice = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda **kw: calls.append("disconnect")))
        self.guild.voice_client = voice
        await self.bot.leave(self.interaction)
        self.assertEqual(calls, ["stop", "disconnect"])

    async def test_owner_leaving_closes_ai(self):
        member = SimpleNamespace(id=30, guild=SimpleNamespace(id=10))
        await self.bot.on_voice_state_update(member, None, SimpleNamespace(channel=None))
        self.fake_session.stop.assert_awaited_once()

    async def test_stop_ai_keeps_voice_connected(self):
        voice = SimpleNamespace(disconnect=AsyncMock())
        self.guild.voice_client = voice
        await self.bot.stop_ai(self.interaction)
        self.fake_session.stop.assert_awaited_once()
        voice.disconnect.assert_not_awaited()

    async def test_wake_activates_authorized_member_with_shared_receiver(self):
        voice = SimpleNamespace(channel=self.channel, is_connected=lambda: True,
            guild=SimpleNamespace(get_member=lambda user_id: self.interaction.user))
        self.bot.wake.running = True
        self.bot.wake.voice = voice
        generation = self.bot.wake.generation
        self.assertTrue(await self.bot._wake_activate(30, voice, generation))
        self.fake_session.start.assert_awaited_once()
        self.assertTrue(self.fake_session.start.await_args.kwargs["external_capture"])
        self.fake_session.start.reset_mock()
        self.bot.wake.reset()
        self.assertFalse(await self.bot._wake_activate(30, voice, generation))
        self.fake_session.start.assert_not_awaited()
        self.bot.wake.voice = None
        self.bot.wake.running = False

    async def test_wake_rechecks_speaker_membership_before_opening_ai(self):
        voice = SimpleNamespace(channel=self.channel, is_connected=lambda: True,
            guild=SimpleNamespace(get_member=lambda user_id: None))
        self.bot.wake.running = True
        self.bot.wake.voice = voice
        self.assertFalse(await self.bot._wake_activate(30, voice, self.bot.wake.generation))
        self.fake_session.start.assert_not_awaited()
        self.bot.wake.voice = None
        self.bot.wake.running = False

    async def test_stop_rearms_local_listener_and_leave_disarms_it(self):
        calls = []
        wake = SimpleNamespace(running=True, reset=MagicMock(),
            stop=AsyncMock(side_effect=lambda: calls.append("wake_stop")),
            settings=WakeSettings(), last_error=None)
        self.bot.wake = wake
        self.fake_session.stop.side_effect = lambda: calls.append("ai_stop")
        voice = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda **kw: calls.append("disconnect")))
        self.guild.voice_client = voice
        await self.bot.stop_ai(self.interaction)
        wake.reset.assert_called_once()
        wake.stop.assert_not_awaited()
        voice.disconnect.assert_not_awaited()
        calls.clear()
        await self.bot.leave(self.interaction)
        self.assertEqual(calls, ["wake_stop", "ai_stop", "disconnect"])
        self.assertFalse(self.bot.want_voice)

    async def test_ready_autojoins_and_manual_leave_prevents_rejoin(self):
        bot_user = SimpleNamespace(id=123)
        self.bot._connection.user = bot_user
        self.bot.want_voice = True
        self.bot.get_guild = MagicMock(return_value=self.guild)
        self.bot._connect_voice = AsyncMock()
        with patch("main.discord.VoiceChannel", type(self.channel)):
            await self.bot.on_ready()
            self.bot._connect_voice.assert_awaited_once_with(self.guild, self.channel)
            self.bot._connect_voice.reset_mock()
            self.bot.want_voice = False
            await self.bot.on_ready()
            self.bot._connect_voice.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
