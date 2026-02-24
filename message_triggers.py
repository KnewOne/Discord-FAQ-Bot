import re
import sqlite3
from pathlib import Path

import discord
from discord import Option, SlashCommandGroup
from discord.ext import commands

DB_PATH = Path(__file__).parent / "database.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
                   CREATE TABLE IF NOT EXISTS message_triggers
                   (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       channel_id INTEGER NOT NULL,
                       guild_id INTEGER NOT NULL,
                       message_id INTEGER,
                       message_url TEXT,
                       patterns TEXT NOT NULL,
                       response_text TEXT,
                       created_by INTEGER NOT NULL,
                       UNIQUE (channel_id, message_id))
   """)
    
    cursor.execute("PRAGMA table_info(message_triggers)")
    col_info = {row[1]: row for row in cursor.fetchall()}

    # Migrate: if message_id or message_url are NOT NULL, recreate table to make them nullable
    # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk)
    needs_migration = (
        col_info.get("message_id", (None, None, None, 0))[3] == 1 or
        col_info.get("message_url", (None, None, None, 0))[3] == 1
    )
    if needs_migration:
        cursor.execute("ALTER TABLE message_triggers RENAME TO message_triggers_old")
        cursor.execute("""
            CREATE TABLE message_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                message_id INTEGER,
                message_url TEXT,
                patterns TEXT NOT NULL,
                response_text TEXT,
                created_by INTEGER NOT NULL,
                UNIQUE (channel_id, message_id)
            )
        """)
        cursor.execute("""
            INSERT INTO message_triggers (id, channel_id, guild_id, message_id, message_url, patterns, response_text, created_by)
            SELECT id, channel_id, guild_id, message_id, message_url, patterns, response_text, created_by
            FROM message_triggers_old
        """)
        cursor.execute("DROP TABLE message_triggers_old")

    conn.commit()
    conn.close()


class MessageTriggers(commands.Cog):
    triggers = SlashCommandGroup("triggers", "Manage message triggers in this channel")

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        init_db()

    def _get_conn(self):
        return sqlite3.connect(DB_PATH)

    @commands.has_permissions(manage_messages=True)
    @triggers.command(name="set", description="Set a message trigger")
    async def set_trigger(
            self,
            ctx: discord.ApplicationContext,
            patterns: Option(str, "Comma-separated regex patterns to match", required=True),
            response_text: Option(str, "Response text (use <> as placeholder for link)", required=True),
            message_url: Option(str, "The message URL to link to (optional)", required=False, default=None),
    ):
        message_id = None
        if message_url is not None:
            # Parse message URL to get message ID
            # Format: https://discord.com/channels/guild_id/channel_id/message_id
            url_pattern = r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
            match = re.match(url_pattern, message_url)
            if not match:
                await ctx.respond("Invalid message URL format.", ephemeral=True)
                return

            guild_id, target_channel_id, message_id = map(int, match.groups())

            # Verify the message exists and is accessible
            try:
                target_channel = self.bot.get_channel(target_channel_id)
                if target_channel is None:
                    target_channel = await self.bot.fetch_channel(target_channel_id)
                await target_channel.fetch_message(message_id)
            except discord.NotFound:
                await ctx.respond("Message not found.", ephemeral=True)
                return
            except discord.Forbidden:
                await ctx.respond("I don't have access to that message.", ephemeral=True)
                return

        # Validate regex patterns
        pattern_list = [p.strip() for p in patterns.split(",") if p.strip()]
        if not pattern_list:
            await ctx.respond("Please provide at least one pattern.", ephemeral=True)
            return

        for pattern in pattern_list:
            try:
                re.compile(pattern)
            except re.error as e:
                await ctx.respond(f"Invalid regex pattern `{pattern}`: {e}", ephemeral=True)
                return

        patterns_str = ",".join(pattern_list)

        conn = self._get_conn()
        cursor = conn.cursor()
        if message_id is not None:
            # Delete existing trigger for this channel/message
            cursor.execute(
                "DELETE FROM message_triggers WHERE channel_id = ? AND message_id = ?",
                (ctx.channel_id, message_id)
            )
        # Insert the new/updated trigger
        cursor.execute(
            "INSERT INTO message_triggers (channel_id, guild_id, message_id, message_url, patterns, response_text, created_by)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ctx.channel_id, ctx.guild_id, message_id, message_url, patterns_str, response_text, ctx.author.id)
        )
        conn.commit()
        conn.close()

        response_info = f"Trigger set.\nPatterns: `{patterns_str}`\nResponse: `{response_text}`"
        if message_url:
            response_info += f"\nURL: {message_url}"
        await ctx.respond(response_info, ephemeral=True)

    @commands.has_permissions(manage_messages=True)
    @triggers.command(name="delete", description="Delete a message trigger")
    async def delete_trigger(
            self,
            ctx: discord.ApplicationContext,
            message_url: Option(str, "The message URL to remove trigger for", required=True),
    ):

        # Parse message URL
        url_pattern = r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)"
        match = re.match(url_pattern, message_url)
        if not match:
            await ctx.respond("Invalid message URL format.", ephemeral=True)
            return

        _, _, message_id = map(int, match.groups())

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
                       DELETE
                           FROM message_triggers
                           WHERE channel_id = ?
                             AND message_id = ?
                       """, (ctx.channel_id, message_id))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()

        if deleted:
            await ctx.respond("Trigger deleted.", ephemeral=True)
        else:
            await ctx.respond("No trigger found for that message in this channel.", ephemeral=True)

    @commands.has_permissions(manage_messages=True)
    @triggers.command(name="list", description="List all message triggers in this channel")
    async def list_triggers(self, ctx: discord.ApplicationContext):

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT patterns, message_url, response_text
                           FROM message_triggers
                           WHERE channel_id = ?
                           ORDER BY id
                       """, (ctx.channel_id,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            await ctx.respond("No triggers set up in this channel.", ephemeral=True)
            return

        lines = []
        for i, (patterns, message_url, response_text) in enumerate(rows, 1):
            line = f"{i}. `{patterns}` -> {message_url}"
            if response_text:
                line += f" | `{response_text}`"
            lines.append(line)

        response = "\n".join(lines)
        if len(response) > 2000:
            response = response[:1997] + "..."

        await ctx.respond(response, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bot messages
        if message.author.bot:
            return

        # Ignore DMs
        if message.guild is None:
            return

        # Check if this channel has any triggers
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("""
                       SELECT patterns, message_url, response_text
                           FROM message_triggers
                           WHERE channel_id = ?
                       """, (message.channel.id,))
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return

        # Check each trigger
        content = message.content
        for patterns_str, message_url, response_text in rows:
            patterns = [p.strip() for p in patterns_str.split(",") if p.strip()]
            for pattern in patterns:
                try:
                    if re.search(pattern, content, re.IGNORECASE):
                        reply_content = response_text

                        if message_url:
                            if "<>" in response_text:
                                reply_content = response_text.replace("<>", message_url)
                            else:
                                reply_content = f"{response_text} {message_url}"

                        try:
                            await message.reply(reply_content, mention_author=True)
                        except discord.HTTPException:
                            pass
                        return
                except re.error:
                    pass


def setup(bot):
    bot.add_cog(MessageTriggers(bot))
