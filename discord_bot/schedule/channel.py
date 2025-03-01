from discord.ext import tasks
from discord.utils import get

from config.settings import CHANNEL_SEND_PINGS
from discord_bot.logs import logger as log
from discord_bot.utils.time import get_hammertime, discord_countdown
from discord_bot.utils.views import add_persistent_view
from discord_bot.utils.games import async_get_game_from_message, get_game_id_from_message
from discord_bot.utils.channel import async_create_channel_hidden, async_channel_add_player, async_channel_add_dm
from discord_bot.utils.channel import async_get_all_game_channels_for_guild, async_get_channel_first_message
from discord_bot.components.channels import MusteringBanner, MusteringView
from core.utils.games import async_get_dm, async_get_player_list
from core.utils.channels import async_get_game_channels_pending_creation, async_set_game_channel_created
from core.utils.channels import async_get_game_channels_pending_destruction, async_destroy_game_channel
from core.utils.channels import async_get_game_channels_pending_reminder, _async_set_game_channel_reminded
from core.utils.channels import async_get_game_channels_pending_warning, async_set_game_channel_warned
from core.utils.channels import async_get_game_channel_for_game


class ChannelManager:
    """Manager class for performing channel based functions"""

    initialised = False
    guild = None
    parent_category = None

    def __init__(self, guild):
        """initialisation function"""
        self.guild = guild
        self.channel_event_loop.start()

    async def get_topic_text(self, game):
        """build the game channel topic header"""
        dm = await async_get_dm(game)
        topic_text = "This thread is for mustering for the following game: "
        topic_text += f"{game.module} ({game.name}) | DMed by {dm.name} | "
        topic_text += f"Game is scheduled for {get_hammertime(game.datetime)}"
        return topic_text

    async def get_ping_text(self, game):
        """Get text that will ping each of the users mentioned"""
        players = await async_get_player_list(game)
        dm = await async_get_dm(game)
        ping_text = f"DM: <@{dm.discord_id}>\n"
        ping_text += "Players: "
        ping_text += ",".join(f"<@{p.discord_id}>" for p in players if not p.standby)
        return ping_text

    async def get_flat_message_list(self, game):
        """Get a list of involved users, but in such a way as to not ping them"""
        players = await async_get_player_list(game)
        dm = await async_get_dm(game)
        text = f"DM: {dm.discord_name}\n"
        text += "Players: "
        text += ",".join(f"{p.discord_name}" for p in players if not p.standby)
        return text

    async def add_channel_users(self, channel, game):
        """Add the DM and players to the newly created channel"""
        dm = await async_get_dm(game)
        await async_channel_add_dm(channel, dm)

        players = await async_get_player_list(game)
        for player in players:
            await async_channel_add_player(channel, player)

    async def send_banner_message(self, channel, game):
        """send the welcome banner"""
        control_view = MusteringView(game)
        banner = MusteringBanner(game)
        await banner.build()

        if CHANNEL_SEND_PINGS:
            ping_text = await self.get_ping_text(game)
            message = await channel.send(ping_text, embed=banner, view=control_view)
        else:
            flat_text = await self.get_flat_message_list(game)
            message = await channel.send(flat_text, embed=banner, view=control_view)
        control_view.message = message
        add_persistent_view(control_view)

    async def check_and_create_channels(self):
        """Get outstanding channels needed and create them where missing"""
        pending_games = await async_get_game_channels_pending_creation()
        for upcoming_game in pending_games:
            log.info(f"Creating channel for game: {upcoming_game.name}")
            channel_name = upcoming_game.datetime.strftime("%Y%m%d-") + upcoming_game.module
            channel_topic = await self.get_topic_text(upcoming_game)
            channel = await async_create_channel_hidden(self.guild, self.parent_category, channel_name, channel_topic)
            if channel:
                game_channel = await async_set_game_channel_created(
                    upcoming_game, channel.id, channel.jump_url, channel.name
                )
                await self.add_channel_users(channel, upcoming_game)
                await self.send_banner_message(channel, upcoming_game)

    async def check_and_delete_channels(self):
        """Go through any outstanding channels and delete anything older than 3 days"""
        try:
            expired_game_channels = await async_get_game_channels_pending_destruction()
            for game_channel in expired_game_channels:
                log.info(f"Removing game channel: {game_channel.name}")
                channel = self.guild.get_channel(int(game_channel.discord_id))
                if channel:
                    await channel.delete()
                else:
                    log.info("Cannot retrieve the expected discord channel, assuming its been deleted manually...")
                await async_destroy_game_channel(game_channel)
        except Exception as e:
            log.error(e)

    async def check_and_remind_channels(self):
        """Remind players 24 hours before their game"""
        try:
            upcoming_games = await async_get_game_channels_pending_reminder()
            for game in upcoming_games:
                game_channel = await async_get_game_channel_for_game(game)
                log.info(f"Sending game reminder to channel: {game_channel.name}")
                channel = self.guild.get_channel(int(game_channel.discord_id))
                ping_text = await self.get_ping_text(game)
                await channel.send(f"Reminder: this game is coming up {discord_countdown(game.datetime)}!\n{ping_text}")
                await _async_set_game_channel_reminded(game_channel)
        except Exception as e:
            log.error(e)

    async def check_and_warn_channels(self):
        """Remind players 1 hour before their game"""
        try:
            upcoming_games = await async_get_game_channels_pending_warning()
            for game in upcoming_games:
                game_channel = await async_get_game_channel_for_game(game)
                log.info(f"Sending 1 hour start warning to channel: {game_channel.name}")
                channel = self.guild.get_channel(int(game_channel.discord_id))
                ping_text = await self.get_ping_text(game)
                await channel.send(
                    f"Game starting {discord_countdown(game.datetime)}, please ensure that you are ready\n{ping_text}"
                )
                await async_set_game_channel_warned(game_channel)
        except Exception as e:
            log.error(e)

    async def recover_channel_state(self):
        """Pull game postings from posting history and reconstruct a game/message status from it"""
        log.info("Reconnecting to existing mustering views")
        for channel in await async_get_all_game_channels_for_guild(self.guild):
            message = await async_get_channel_first_message(channel)
            game = await async_get_game_from_message(message)
            # Rebuild view handlers
            if game:
                control_view = MusteringView(game)
                control_view.message = message
                add_persistent_view(control_view)
                log.info(f"Reconnected mustering view for {game.name}")
            else:
                game_id = get_game_id_from_message(message)
                log.info(f"Identified potentially ophaned mustering channel (no game to match) for game ID: {game_id}")
                continue

    @tasks.loop(seconds=42)
    async def channel_event_loop(self):
        if not self.initialised:
            log.debug("Starting up the channel watcher")
            self.parent_category = get(self.guild.categories, name="Your Upcoming Games")
            await self.recover_channel_state()
            self.initialised = True

        await self.check_and_create_channels()
        await self.check_and_delete_channels()
        await self.check_and_remind_channels()
        await self.check_and_warn_channels()
