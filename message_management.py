import asyncio
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from re import match

import discord
import requests
from discord import Message, Option
from discord.ext import commands

from constants import FILEPATH, reTitleGroup
from emoji_management import deemojify, emojify


class MessageManagement(commands.Cog):

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot

    @staticmethod
    def _get_blizzard_token() -> str:
        client_id = os.environ.get("BLIZZARD_API_ID")
        client_secret = os.environ.get("BLIZZARD_API_SECRET")
        response = requests.post(
            "https://oauth.battle.net/token",
            data={"grant_type": "client_credentials"},
            auth=(client_id, client_secret)
        )
        response.raise_for_status()
        return response.json()["access_token"]

    @staticmethod
    def fix_item_links(message: Message) -> None:
        # Match any wowhead URL containing item=<id> that is not already a markdown link
        unfixed_link_pattern = r'https://(?:www\.)?wowhead\.com/[^\s\)]*item=(\d+)[^\s\)]*'

        def get_item_name(item_id: str, token: str) -> str | None:
            try:
                response = requests.get(
                    f"https://eu.api.blizzard.com/data/wow/item/{item_id}",
                    params={"namespace": "static-eu", "locale": "ru_RU"},
                    headers={"Authorization": f"Bearer {token}"}
                )
                response.raise_for_status()
                return response.json()["name"]
            except Exception as e:
                print(f"Error fetching item name for id={item_id}: {e}")
                return None

        # Collect unique item IDs from unfixed links (skip already-fixed markdown links)
        fixed_link_re = re.compile(r'\[.*?\]\(https://(?:www\.)?wowhead\.com/[^\)]*item=\d+[^\)]*\)')
        unfixed_link_re = re.compile(unfixed_link_pattern)

        # Remove fixed links from consideration by blanking them, then find bare URLs
        blanked = fixed_link_re.sub(lambda m: ' ' * len(m.group()), message.content)
        item_ids = list(dict.fromkeys(unfixed_link_re.findall(blanked)))

        if not item_ids:
            return

        token = MessageManagement._get_blizzard_token()

        # Fetch all item names in parallel
        id_to_name = {}
        with ThreadPoolExecutor() as executor:
            future_to_id = {executor.submit(get_item_name, item_id, token): item_id for item_id in item_ids}
            for future in as_completed(future_to_id):
                item_id = future_to_id[future]
                name = future.result()
                if name:
                    id_to_name[item_id] = name

        # Replace bare wowhead item URLs with fixed markdown links
        def replace_match(m):
            item_id = m.group(1)
            name = id_to_name.get(item_id)
            if name:
                return f'[{name}](https://www.wowhead.com/ru/item={item_id})'
            return m.group(0)

        # Only replace URLs that are not already inside a markdown link
        def replace_unfixed(content: str) -> str:
            result = []
            pos = 0
            for fixed in fixed_link_re.finditer(content):
                chunk = content[pos:fixed.start()]
                result.append(unfixed_link_re.sub(replace_match, chunk))
                result.append(fixed.group(0))
                pos = fixed.end()
            result.append(unfixed_link_re.sub(replace_match, content[pos:]))
            return ''.join(result)

        message.content = replace_unfixed(message.content)

    @commands.slash_command(description="Dump messages to user id")
    @commands.is_owner()
    async def dump_messages(self, ctx: discord.ApplicationContext, id: Option(str)):
        user = self.bot.get_user(int(id))
        if user == None:
            await ctx.respond(f"Bad user name {user.name}")
            return
        await ctx.respond(f"Sending the messages to {user.name}", ephemeral=True)
        async for message in ctx.channel.history(oldest_first=True):
            await user.send(f"```{deemojify(message.content)}```", suppress=True)

    @commands.slash_command(description="Clears the channnel")
    @commands.is_owner()
    async def clear(self, ctx: discord.ApplicationContext, amount: Option(int, default=20)):
        cleared = await ctx.channel.purge(limit=amount)
        await ctx.respond(f"Done clearing {len(cleared)} messages", delete_after=10)

    @commands.slash_command(description="Republish")
    @commands.has_permissions(manage_messages=True)
    async def republish(self, ctx: discord.ApplicationContext):
        """Take every message in the channel, delete it and send their copies from bot account"""
        await ctx.respond("Starting the process", ephemeral=True)

        message: discord.Message
        async for message in ctx.channel.history(oldest_first=True):
            if message.content:
                if message.author == self.bot.user:
                    await ctx.channel.send(message.content, files=[await discord.Attachment.to_file(x) for x in message.attachments], embeds=message.embeds, suppress=False if message.embeds else True)
                else:
                    await ctx.channel.send(emojify(message.content), files=[await discord.Attachment.to_file(x) for x in message.attachments], embeds=message.embeds, suppress=False if message.embeds else True)
            else:
                if message.embeds and message.embeds[0].title == "Содержание":
                    await self.bot.get_command("summary").callback(self, ctx)
                else:
                    await ctx.channel.send("*** ***")
            await message.delete()

    @commands.message_command(name="Insert empty message")
    @commands.has_permissions(manage_messages=True)
    async def insert_message(self, ctx: discord.ApplicationContext, message: discord.Message):
        chnl_bot_msgs = list()
        chn = ctx.channel

        await ctx.respond(f"Inserted empty message at {message.jump_url}", ephemeral=True)

        # Create an empty message to move last message into
        await ctx.channel.send("*** ***")

        # Get list of bot messages from the end to the one being replaced
        async for msg in ctx.channel.history():
            if msg.author == self.bot.user:
                if message.id == msg.id:
                    chnl_bot_msgs.append(msg.id)
                    break
                chnl_bot_msgs.append(msg.id)

        # Start from the end, take the current message content and earlier message content, move
        # earlier message into current one. Walk along all message ids except the one being replaced
        #
        #               A <- Insert position    >     *** ***
        #               ↓                       >        |
        #               B                       >        A
        #               ↓                       >        |
        #   Created empty message "*** ***"     >        B

        for i in range(len(chnl_bot_msgs[0:-1])):
            msg_crt, msg_nxt = await asyncio.gather(chn.fetch_message(chnl_bot_msgs[i]),
                                                    chn.fetch_message(chnl_bot_msgs[i + 1]))
            # remove all current message attachments then add attachments from next message to this one
            await msg_crt.edit(content=msg_nxt.content, attachments=[], files=[await discord.Attachment.to_file(x) for x in msg_nxt.attachments], embeds=msg_nxt.embeds, suppress=False if msg_nxt.embeds else True)
        await message.edit(content="**[PH]**", suppress=True, embeds=[])

    @commands.message_command(name="Turn into embed")
    @commands.has_permissions(manage_messages=True)
    async def embedify_message(self, ctx: discord.ApplicationContext, message: discord.Message):

        if not (message.author.id == self.bot.user.id):
            await ctx.respond("Cannot edit non-bot message", ephemeral=True)
            return

        title = match(reTitleGroup, message.content).group(1)
        messageContent = message.content.replace(title, "").strip()
        embed2 = discord.Embed(title=None, description=messageContent)

        await message.edit(content=title, embed=embed2, suppress=False)
        # await message.edit(title, suppress=True, attachments=[], files=[await discord.Attachment.to_file(x) for x in message.attachments], embed=embed)
        # await orgnl_msg.edit(emojify(answ.content), suppress=True, attachments=[], files=[await discord.Attachment.to_file(x) for x in answ.attachments])
        await ctx.respond(f"Successfully changed message at {message.jump_url}", ephemeral=True)

    @commands.message_command(name="Edit message")
    @commands.has_permissions(manage_messages=True)
    async def edit_message(self, ctx: discord.ApplicationContext, message: discord.Message):

        if not (message.author.id == self.bot.user.id):
            await ctx.respond("Cannot edit non-bot message", ephemeral=True)
            return

        originalMessage = message
        await ctx.respond("Sent you a dm", ephemeral=True)

        if message.embeds:
            await ctx.user.send(f' ```{deemojify(originalMessage.content)}\nEMBED\n{originalMessage.embeds[0].description}``` \n Write "Cancel" to stop the process',
                                files=[await discord.Attachment.to_file(x) for x in originalMessage.attachments], suppress=True)
        else:
            await ctx.user.send(f' ```{deemojify(originalMessage.content)}``` \n Write "Cancel" to stop the process', files=[await discord.Attachment.to_file(x) for x in originalMessage.attachments], suppress=True)

        # Pick up only non-empty message from the author of ctx command requrest in the DM channel
        def empty_message_check_dm(m):
            return m.author == ctx.author and m.channel == m.author.dm_channel and m.content

        answ: discord.Message = await self.bot.wait_for("message", check=empty_message_check_dm, timeout=240.0)

        if answ.content.lower() == "cancel":
            await ctx.user.send("Cancelled")
            return

        self.fix_item_links(answ)

        if 'EMBED' in answ.content:
            messageContent, embedContent = [
                x.strip() for x in answ.content.split("EMBED")]
            embed = discord.Embed(description=embedContent)
            await originalMessage.edit(content=emojify(messageContent), embed=embed)
        else:
            await originalMessage.edit(content=emojify(answ.content), attachments=[], files=[await discord.Attachment.to_file(x) for x in answ.attachments])
        await ctx.user.send(f"Successfully changed message at {originalMessage.jump_url}")

    @commands.has_permissions(manage_messages=True)
    @commands.slash_command(description="Summary", name="summary")
    async def summary(self, ctx: discord.ApplicationContext):
        await ctx.respond("Starting the process...", ephemeral=True)

        description = ""
        async for msg in ctx.channel.history(oldest_first=True):
            rslt = match(reTitleGroup, msg.content)
            # rslt = match("(\*{2}.*\*{2})", msg.content)

            if rslt is not None:
                description += f"[{rslt.group(1).replace('#', '').strip()}]({msg.jump_url})\n"

        await ctx.channel.send(content=None, embed=discord.Embed(
            title="Содержание",
            description=description,
            color=discord.Colour.blurple(),
        ))

    @commands.slash_command(description="Save channel messages", )
    async def save_messages(self, ctx: discord.ApplicationContext, filename: Option(str, "Filename", default=None)):
        await ctx.respond("Saving", ephemeral=True)
        chn_id = ctx.channel_id
        chn_name = ctx.channel.name

        msgs = [{
            "chn_name": chn_name,
            "chn_id": chn_id

        }]

        if filename == None:
            from datetime import date
            dt = date.today()

            match str(ctx.channel_id):
                case "862009501036838952":
                    filename = "resto"
                case "780149018104954921":
                    filename = "feral"
                case "780458782181294131":
                    filename = "guardian"
                case "779796524275204107":
                    filename = "boomkin"
                case _:
                    filename = ctx.channel.name

            filename += f"-{dt.day}.{dt.month}"

        Path(f"{FILEPATH}/{filename}").mkdir(parents=True, exist_ok=True)

        async for msg in ctx.channel.history(oldest_first=True):
            content = deemojify(msg.content)

            images = []
            # Save images in {FILEPATH}/channel_name/image_id
            # save this as data in the message description

            # Embeds
            embeds = []
            if msg.embeds:
                for embed in msg.embeds:
                    embeds.append(embed.description)

            for attachment in msg.attachments:
                img_id = 0
                with open(f'{FILEPATH}/{filename}/{img_id}.png', 'wb') as f:
                    f.write(await attachment.read())
                    images.append(f"{filename}/{img_id}.png")
                    img_id += 1
            msgs.append({
                "content": content,
                "images": images,
                "embeds": embeds
            })

        with open(f"{FILEPATH}/{filename}.json", "w", encoding='utf-8') as f:
            json.dump(msgs, f, ensure_ascii=False, indent=4)
        await ctx.respond("Done", ephemeral=True)

    @commands.slash_command(description="Publish")
    @commands.is_owner()
    async def publish(self, ctx: discord.ApplicationContext, filename: Option(str, "Name of file to publish", required=True)):

        if not Path(f"{FILEPATH}/{filename}.json").is_file():
            await ctx.respond(f"Bad filename: {filename}", ephemeral=True)
            return

        await ctx.respond("Starting")

        with open(f"{FILEPATH}/{filename}.json", 'r', encoding='utf-8') as f:
            messages = json.load(f)

            for msg in messages[1:]:
                files = []

                for image in msg["images"]:
                    files.append(discord.File(
                        f"{FILEPATH}/{image}")
                    )

                embeds = []
                for embed in msg["embeds"]:
                    embeds.append(discord.Embed(description=embed))

                await ctx.send(emojify(msg["content"]), files=files, embeds=embeds)


# def copyMessageContent(message: discord.Message):


def setup(bot):
    bot.add_cog(MessageManagement(bot))
