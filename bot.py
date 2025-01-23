import discord
from discord import app_commands
from dotenv import load_dotenv
from pymongo import MongoClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
import os
import datetime
import pytz
import logging
import sys
import re

load_dotenv()

TOKEN = os.getenv("TOKEN")

DATABASE = os.getenv("MONGO_DATABASE")
USERNAME = os.getenv("MONGO_USERNAME")
PASSWORD = os.getenv("MONGO_PASSWORD")
CLUSTER = os.getenv("MONGO_CLUSTER")
APPNAME = os.getenv("MONGO_APPNAME")

CONNECTION_STRING = f"mongodb+srv://{USERNAME}:{PASSWORD}@{CLUSTER}/?retryWrites=true&w=majority&appName={APPNAME}"

db = MongoClient(CONNECTION_STRING)
scheduler = AsyncIOScheduler()

AUSTRALIAN_TIMEZONE = pytz.timezone("Australia/Sydney")
START_DATE = datetime.datetime.now(AUSTRALIAN_TIMEZONE).replace(
    hour=21, minute=0, second=0, microsecond=0
)

logging.basicConfig(level=logging.ERROR)

IMAGE_REGEX = re.compile(
    r"(https?://[^\s]+(?:\.(?:png|jpg|jpeg|gif|webp))(?:\?[^\s]*)?)"
)


class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        try:
            synced = await self.tree.sync()
            print(f"Synced {synced} commands")
        except Exception as error:
            print(f"Failed to sync commands, error: {error}")
            exit(1)

        scheduler.start()
        print("Scheduler started")

        # grab all guilds from channel and schedule to run at 12 am
        guilds = db.get_database(DATABASE).get_collection("guilds").find()
        for guild in guilds:
            channel = self.get_channel(guild["channel_id"])
            if not channel:
                continue
            scheduler.add_job(
                self.post_quote,
                trigger=CronTrigger(hour=21, minute=0, timezone=AUSTRALIAN_TIMEZONE),
                args=[channel],
                id=str(guild["_id"]),
            )

    async def post_quote(
        self, channel: discord.TextChannel, message: discord.Message = None, use_db=True
    ):
        print("POSTING QUOTE")
        # quotes table -> guild_id, channel_id, message_id, reaction_count
        message_id = (
            db.get_database(DATABASE)
            .get_collection("quotes")
            .find_one({"_id": channel.guild.id})
        )
        if not message_id and use_db:
            return
        message_channel = self.get_channel(message_id["channel_id"]) if use_db else None
        if not message_channel and use_db:
            return
        try:
            message = (
                await message_channel.fetch_message(message_id["message_id"])
                if use_db
                else message
            )
        except discord.NotFound:
            if use_db:
                db.get_database(DATABASE).get_collection("quotes").delete_one(
                    {"_id": channel.guild.id}
                )
            return
        
        if not message:
            return
        embed = discord.Embed(
            title="Quote of the day",
            description=f"{message.content}\n[Jump to message]({message.jump_url})",
            color=discord.Color.blue(),
        )
        reactions = message.reactions
        print("Reactions:", reactions)
        if reactions:
            reaction_count = {reaction.emoji: reaction.count for reaction in reactions}
            reaction_count = dict(
                sorted(reaction_count.items(), key=lambda item: item[1], reverse=True)
            )
            print(f"Reactions: {reaction_count}")
            for reaction, count in reaction_count.items():
                embed.add_field(name=reaction, value=f"{count}", inline=True)
        else:
            print("No reactions found.")
        embed.set_author(
            name=message.author.display_name,
            icon_url=(
                message.author.avatar.url if message.author.avatar is not None else None
            ),
        )
        image_links = IMAGE_REGEX.findall(message.content)
        if message.attachments and message.attachments[0].filename.endswith(
            (".png", ".jpg", ".jpeg", ".gif")
        ):
            embed.set_image(url=message.attachments[0].url)
        elif image_links:
            embed.set_image(url=image_links[0])
        message = await channel.send(embed=embed)

        # check if threads are enabled
        guild = (
            db.get_database(DATABASE)
            .get_collection("guilds")
            .find_one({"_id": channel.guild.id})
        )
        if guild and guild["threads"]:
            # open thread
            try:
                thread = await message.create_thread(name="Discussion")
                await thread.send(".")
            except Exception as e:
                print("Failed to create thread", e)

        if use_db:
            db.get_database(DATABASE).get_collection("quotes").delete_one(
                {"_id": channel.guild.id}
            )

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id:
            return

        message = await self.get_channel(payload.channel_id).fetch_message(
            payload.message_id
        )
        if message.author == self.user or message.content == "":
            return
        message_created_time = message.created_at.replace(
            tzinfo=datetime.timezone.utc
        ).astimezone(AUSTRALIAN_TIMEZONE)
        now_time = datetime.datetime.now(AUSTRALIAN_TIMEZONE)

        if (
            message_created_time.day != now_time.day
            or message_created_time.year != now_time.year
            or message_created_time.month != now_time.month
        ):
            return

        # grab quote from guild
        quote = (
            db.get_database(DATABASE)
            .get_collection("quotes")
            .find_one({"_id": message.guild.id})
        )
        
        # check if quote is outdated
        if quote:
            quote_created_time = datetime.datetime.fromisoformat(
                quote["created_at"]
            ).replace(tzinfo=datetime.timezone.utc)
            if quote_created_time.day != now_time.day:
                db.get_database(DATABASE).get_collection("quotes").delete_one(
                    {"_id": message.guild.id}
                )
                quote = None

        # find count of greatest reaction count in message
        reaction_count = (
            max([reaction.count for reaction in message.reactions])
            if message.reactions
            else 0
        )

        if not quote or reaction_count > quote["reaction_count"]:
            if quote:
                db.get_database(DATABASE).get_collection("quotes").delete_one(
                    {"_id": message.guild.id}
                )
            db.get_database(DATABASE).get_collection("quotes").insert_one(
                {
                    "_id": message.guild.id,
                    "channel_id": message.channel.id,
                    "message_id": message.id,
                    "reaction_count": reaction_count,
                    "created_at" : message.created_at,
                }
            )
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        message = await self.get_channel(payload.channel_id).fetch_message(
            payload.message_id
        )
        if message.author == self.user or message.content == "":
            return
        quote = (
            db.get_database(DATABASE)
            .get_collection("quotes")
            .find_one({"_id": message.guild.id})
        )
        if quote and quote["message_id"] == message.id:
            reaction_count = (
                max([reaction.count for reaction in message.reactions])
                if message.reactions
                else 0
            )
            if reaction_count < quote["reaction_count"]:
                if reaction_count == 0:
                    db.get_database(DATABASE).get_collection("quotes").delete_one(
                        {"_id": message.guild.id}
                    )
                else:
                    db.get_database(DATABASE).get_collection("quotes").update_one(
                        {"_id": message.guild.id},
                        {"$set": {"reaction_count": reaction_count}},
                    )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.guild_id is None:
            return
        quote = (
            db.get_database(DATABASE)
            .get_collection("quotes")
            .find_one({"_id": payload.guild_id})
        )
        if quote and quote["message_id"] == payload.message_id:
            db.get_database(DATABASE).get_collection("quotes").delete_one(
                {"_id": payload.guild_id}
            )


bot = Client()


@bot.tree.command(name="setup", description="Set channel to post quotes in")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "You need to be an administrator to run this command", ephemeral=True
        )
        return

    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("Invalid channel", ephemeral=True)
        return

    if channel.guild != interaction.guild:
        await interaction.followup.send(
            "Channel must be in the same server as the bot", ephemeral=True
        )
        return

    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send(
            "I don't have permission to send messages in that channel", ephemeral=True
        )
        return

    db.get_database(DATABASE).get_collection("guilds").update_one(
        {"_id": interaction.guild.id},
        {"$set": {"channel_id": channel.id, "threads": True}},
        upsert=True,
    )
    await interaction.followup.send(f"Set channel to {channel.mention}")

    # generate a interval
    scheduler.add_job(
        bot.post_quote,
        trigger=CronTrigger(hour=21, minute=0, timezone=AUSTRALIAN_TIMEZONE),
        args=[channel],
        id=str(interaction.guild.id),
        replace_existing=True,
    )


@bot.tree.command(name="toggle_threads", description="Toggle threads")
async def toggle_threads(interaction: discord.Interaction, toggle: bool):
    await interaction.response.defer()
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "You need to be an administrator to run this command", ephemeral=True
        )
        return

    channel = (
        db.get_database(DATABASE)
        .get_collection("guilds")
        .find_one({"_id": interaction.guild.id})
    )
    if not channel:
        await interaction.followup.send("Channel not set", ephemeral=True)
        return

    db.get_database(DATABASE).get_collection("guilds").update_one(
        {"_id": interaction.guild.id}, {"$set": {"threads": toggle}}, upsert=True
    )
    await interaction.followup.send(f"Threads set to {toggle}")


@bot.tree.command(name="quote", description="Quote a message")
async def quote(interaction: discord.Interaction, message_id: str):
    await interaction.response.defer()

    if not message_id.isdigit():
        await interaction.followup.send("Message not found", ephemeral=True)
        return

    message = await interaction.channel.fetch_message(int(message_id))
    print("msg obj", message)
    print("msg type", message.type)

    if not message:
        await interaction.followup.send("Message not found", ephemeral=True)
        return

    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "You need to be an administrator to run this command", ephemeral=True
        )
        return

    print(f"Quoting message: {message.content}")
    # if not message.content:
    #     await interaction.followup.send("Message has no content", ephemeral=True)
    #     return

    channel = (
        db.get_database(DATABASE)
        .get_collection("guilds")
        .find_one({"_id": interaction.guild.id})
    )
    if not channel:
        await interaction.followup.send("Channel not set", ephemeral=True)
        return

    channel = bot.get_channel(channel["channel_id"])
    if not channel:
        await interaction.followup.send("Channel not found", ephemeral=True)
        return

    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send(
            "I don't have permission to send messages in that channel", ephemeral=True
        )
        return

    await bot.post_quote(channel, message=message, use_db=False)

    await interaction.followup.send("Quoted message")


@bot.tree.command(name="force_quote", description="force scheduled quote")
async def force_quote(interaction: discord.Interaction):
    await interaction.response.defer()

    # check if user is admin
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "You need to be an administrator to run this command", ephemeral=True
        )
        return

    channel = (
        db.get_database(DATABASE)
        .get_collection("guilds")
        .find_one({"_id": interaction.guild.id})
    )
    if not channel:
        await interaction.followup.send("Channel not set", ephemeral=True)
        return

    channel = bot.get_channel(channel["channel_id"])
    if not channel:
        await interaction.followup.send("Channel not found", ephemeral=True)
        return

    await bot.post_quote(channel)

    await interaction.followup.send("Forced quote")

@bot.tree.command(name="next_quote", description="See next quote.")
async def next_quote(interaction: discord.Interaction):
    await interaction.response.defer()
    quote = (
        db.get_database(DATABASE).get_collection("quotes").find_one(
                {"_id": interaction.guild_id}
            )
        )
    if quote is None:
        await interaction.followup.send("No quotes found", ephemeral=True)
        return

    message_channel = bot.get_channel(quote["channel_id"])
    if not message_channel:
        await interaction.followup.send("Channel not found for this quote", ephemeral=True)
        return

    try:
        message = await message_channel.fetch_message(quote["message_id"])
    except discord.NotFound:
        await interaction.followup.send("Message not found for this quote", ephemeral=True)
        return

    await bot.post_quote(message_channel, message, use_db=False)


@bot.tree.command(name="debug_schedule", description="schedule quote")
async def debug_schedule(interaction: discord.Interaction, seconds: int):
    await interaction.response.defer()
    if not interaction.user.guild_permissions.administrator:
        await interaction.followup.send(
            "You need to be an administrator to run this command", ephemeral=True
        )
        return
    # get channel
    channel = (
        db.get_database(DATABASE)
        .get_collection("guilds")
        .find_one({"_id": interaction.guild.id})
    )
    if not channel:
        await interaction.followup.send("Channel not set", ephemeral=True)
        return

    channel = bot.get_channel(channel["channel_id"])
    if not channel:
        await interaction.followup.send("Channel not found", ephemeral=True)
        return

    scheduler.add_job(
        bot.post_quote,
        DateTrigger(
            run_date=datetime.datetime.now() + datetime.timedelta(seconds=seconds),
            timezone=AUSTRALIAN_TIMEZONE,
        ),
        args=[channel],
    )

    await interaction.followup.send("Scheduled quote")


def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        # Let KeyboardInterrupt through
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))


# Override the default exception handler
sys.excepthook = handle_exception


if __name__ == "__main__":
    bot.run(TOKEN)
