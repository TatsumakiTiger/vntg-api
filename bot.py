"""
Gateway bot — listens for guild member joins and grants the verified role
to users that already logged in via the website.

Runs as a separate Railway process (see Procfile `worker` entry).
"""
import os
import asyncio
import logging
import discord
import psycopg2

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vntg-bot")

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID", "1477669827547103365"))
VERIFIED_ROLE_ID = int(os.environ.get("DISCORD_VERIFIED_ROLE_ID", "1491125641226227742"))


def user_exists(discord_id: int) -> bool:
    conn = psycopg2.connect(DATABASE_URL)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM users WHERE discord_id = %s", (str(discord_id),))
        return cur.fetchone() is not None
    finally:
        conn.close()


intents = discord.Intents.default()
intents.members = True  # required for on_member_join — enable in Dev Portal
client = discord.Client(intents=intents)


async def grant_role(member: discord.Member):
    role = member.guild.get_role(VERIFIED_ROLE_ID)
    if role is None:
        log.warning("Verified role %s not found in guild %s", VERIFIED_ROLE_ID, member.guild.id)
        return
    if role in member.roles:
        return
    try:
        await member.add_roles(role, reason="auto-verify: logged in on website")
        log.info("Granted verified role to %s (%s)", member, member.id)
    except discord.Forbidden:
        log.error("Missing permissions to grant role to %s", member)
    except discord.HTTPException as e:
        log.error("Failed to grant role to %s: %s", member, e)


@client.event
async def on_ready():
    log.info("Logged in as %s", client.user)
    # Backfill pass: any existing member who logged in on the site but
    # doesn't have the role yet gets it now.
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        log.warning("Guild %s not visible yet, skipping backfill", GUILD_ID)
        return
    count = 0
    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        try:
            if user_exists(member.id):
                await grant_role(member)
                count += 1
        except Exception as e:
            log.error("Backfill error for %s: %s", member, e)
    log.info("Backfill complete: %d members checked", count)


@client.event
async def on_member_join(member: discord.Member):
    if member.guild.id != GUILD_ID or member.bot:
        return
    try:
        if user_exists(member.id):
            await grant_role(member)
        else:
            log.info("New join %s has no site account yet", member)
    except Exception as e:
        log.error("on_member_join error for %s: %s", member, e)


if __name__ == "__main__":
    client.run(BOT_TOKEN)
