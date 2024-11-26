import discord
from discord import app_commands
from dotenv import load_dotenv
from pymongo import MongoClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import os

load_dotenv()

TOKEN = os.getenv('TOKEN')

DATABASE = os.getenv('MONGO_DATABASE')
USERNAME = os.getenv('MONGO_USERNAME')
PASSWORD = os.getenv('MONGO_PASSWORD')
CLUSTER = os.getenv('MONGO_CLUSTER')

CONNECTION_STRING = f"mongodb+srv://{USERNAME}:{PASSWORD}@{CLUSTER}/{DATABASE}?retryWrites=true&w=majority"

db = MongoClient(CONNECTION_STRING)
scheduler = AsyncIOScheduler()

class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    
    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        try:
            synced = await self.tree.sync()
            print(f'Synced {synced} commands')
        except Exception as error:
            print(f'Failed to sync commands, error: {error}')
            exit(1)
        
        # grab all guilds from channel and schedule to run at 12 am
        guilds = db.get_database(DATABASE).get_collection("guilds").find()
        for guild in guilds:
            channel = self.get_channel(guild["channel_id"])
            if not channel:
                continue
            scheduler.add_job(self.post_quote, CronTrigger(hour=0, minute=0), args=[channel], id=guild["_id"])

    async def post_quote(self, channel: discord.TextChannel):
        # quotes table -> guild_id, channel_id, message_id, reaction_count
        message_id = db.get_database(DATABASE).get_collection("quotes").find_one({"guild_id": channel.guild.id})
        if not message_id:
            return
        message_channel = self.get_channel(message_id["channel_id"])
        if not message_channel:
            return
        message = await message_channel.fetch_message(message_id["message_id"])
        if not message:
            return
        embed = discord.Embed(title="Quote of the day", description=message.content)
        embed.set_author(name=message.author.display_name, icon_url=message.author.avatar_url)
        message = await channel.send(embed=embed)

        db.get_database(DATABASE).get_collection("quotes").delete_one({"guild_id": channel.guild.id})

        # start new timer
        # TODO: check if this works 
        scheduler.add_job(self.post_quote, CronTrigger(hour=0, minute=0), args=[channel])
        scheduler.start()
    
    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return
        
        # i think i can remove this
        await self.tree.process_message(message)

        # doesnt capture slash commands     
        
        # grab quote from guild
        quote = db.get_database(DATABASE).get_collection("quotes").find_one({"guild_id": message.guild.id})
        
        # find count of greatest reaction count in message
        reaction_count = max([reaction.count for reaction in message.reactions]) if message.reactions else 0

        if not quote or reaction_count > quote["reaction_count"]:
            if quote:
                db.get_database(DATABASE).get_collection("quotes").delete_one({"guild_id": message.guild.id})
            db.get_database(DATABASE).get_collection("quotes").insert_one({"guild_id": message.guild.id}, {"$set": {"message_id": message.id, "reaction_count": reaction_count, "channel_id" : message.channel.id}})

    

bot = Client()

@bot.tree.command(name="setup", description="Set channel to post quotes in")
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer()
    if not interaction.author.guild_permissions.administrator:
        await interaction.followup.send("You need to be an administrator to run this command", ephemeral=True)
        return
    
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("Invalid channel", ephemeral=True)
        return
    
    if channel.guild != interaction.guild:
        await interaction.followup.send("Channel must be in the same server as the bot", ephemeral=True)
        return

    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send("I don't have permission to send messages in that channel", ephemeral=True)
        return

    db.get_database(DATABASE).get_collection("guilds").update_one({"_id": interaction.guild.id}, {"$set": {"channel": channel.id}}, upsert=True)
    await interaction.followup.send(f"Set channel to {channel.mention}")

    scheduler.remove_job(interaction.guild.id)
    scheduler.add_job(bot.post_quote, CronTrigger(hour=0, minute=0), args=[channel], id=interaction.guild.id)
    
@bot.tree.command(name="quote", description="Quote a message")
async def quote(interaction: discord.Interaction):
    await interaction.response.defer()
    if not interaction.message.reference:
        await interaction.followup.send("You need to reply to a message to quote it", ephemeral=True)
        return

    message = await interaction.channel.fetch_message(interaction.message.reference.message_id)
    if not interaction.author.guild_permissions.administrator:
        await interaction.followup.send("You need to be an administrator to run this command", ephemeral=True)
        return
    embed = discord.Embed(title="Quote of the day", description=message.content)
    embed.set_author(name=message.author.display_name, icon_url=message.author.avatar_url)

    channel = db.get_database(DATABASE).get_collection("guilds").find_one({"_id": interaction.guild.id})
    if not channel:
        await interaction.followup.send("Channel not set", ephemeral=True)
        return

    channel = bot.get_channel(channel["channel"])
    if not channel:
        await interaction.followup.send("Channel not found", ephemeral=True)
        return
    
    if not channel.permissions_for(interaction.guild.me).send_messages:
        await interaction.followup.send("I don't have permission to send messages in that channel", ephemeral=True)
        return
    
    await channel.send(embed=embed)
    

if __name__ == "__main__":
    bot.run(TOKEN)
