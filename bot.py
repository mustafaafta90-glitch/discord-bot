import os
import re
import time
import random
import sqlite3
import asyncio
import datetime
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands

# ==========================================
# 1. RENDER KEEP-ALIVE WEB SERVER
# ==========================================
app = Flask(__name__)

@app.route("/")
def home():
    return "PCB BOT is online and running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    t = Thread(target=run_web, daemon=True)
    t.start()

# ==========================================
# 2. DATABASE SETUP (SQLite Persistence)
# ==========================================
conn = sqlite3.connect("bot_data.db", check_same_thread=False)
c = conn.cursor()

c.executescript("""
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id INTEGER PRIMARY KEY,
    prefix TEXT DEFAULT '!',
    modlog_channel INTEGER,
    welcome_channel INTEGER,
    leave_channel INTEGER,
    welcome_msg TEXT DEFAULT 'Welcome {user} to **{server}**! You are member **#{count}**.',
    leave_msg TEXT DEFAULT '**{user}** has left the server.',
    autorole_id INTEGER,
    starboard_channel INTEGER,
    starboard_threshold INTEGER DEFAULT 3,
    automod_invites INTEGER DEFAULT 0,
    automod_spam INTEGER DEFAULT 0,
    automod_mentions INTEGER DEFAULT 0,
    automod_caps INTEGER DEFAULT 0,
    automod_links INTEGER DEFAULT 0,
    sticky_channel INTEGER,
    sticky_content TEXT,
    sticky_last_msg INTEGER,
    jtc_channel INTEGER,
    membercount_channel INTEGER,
    is_locked_down INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS lockdown_backups (
    guild_id INTEGER, channel_id INTEGER, prev_send_perms INTEGER, PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS lockdown_ignored (
    guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id)
);
CREATE TABLE IF NOT EXISTS autopurge_channels (
    channel_id INTEGER PRIMARY KEY, guild_id INTEGER, delay_seconds INTEGER
);
CREATE TABLE IF NOT EXISTS triggers (
    guild_id INTEGER, keyword TEXT, response TEXT, PRIMARY KEY (guild_id, keyword)
);
CREATE TABLE IF NOT EXISTS mod_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, mod_id INTEGER, note TEXT, timestamp INTEGER
);
CREATE TABLE IF NOT EXISTS badwords (
    guild_id INTEGER, word TEXT, PRIMARY KEY (guild_id, word)
);
CREATE TABLE IF NOT EXISTS warnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, mod_id INTEGER, reason TEXT, timestamp INTEGER
);
CREATE TABLE IF NOT EXISTS tags (
    guild_id INTEGER, name TEXT, content TEXT, creator_id INTEGER, uses INTEGER DEFAULT 0, PRIMARY KEY (guild_id, name)
);
CREATE TABLE IF NOT EXISTS starboard_posts (
    orig_msg_id INTEGER PRIMARY KEY, star_msg_id INTEGER, guild_id INTEGER
);
CREATE TABLE IF NOT EXISTS button_roles (
    guild_id INTEGER, role_id INTEGER, label TEXT, PRIMARY KEY (guild_id, role_id)
);
CREATE TABLE IF NOT EXISTS emoji_roles (
    guild_id INTEGER, message_id INTEGER, emoji TEXT, role_id INTEGER, PRIMARY KEY (guild_id, message_id, emoji)
);
CREATE TABLE IF NOT EXISTS users (
    guild_id INTEGER, user_id INTEGER, balance INTEGER DEFAULT 500, bank INTEGER DEFAULT 0,
    xp INTEGER DEFAULT 0, level INTEGER DEFAULT 1, bounty INTEGER DEFAULT 0,
    last_daily INTEGER DEFAULT 0, last_work INTEGER DEFAULT 0, last_crime INTEGER DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS inventory (
    guild_id INTEGER, user_id INTEGER, item_name TEXT, quantity INTEGER DEFAULT 1,
    PRIMARY KEY (guild_id, user_id, item_name)
);
""")
conn.commit()

def get_config(guild_id: int):
    c.execute("INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,))
    conn.commit()
    c.execute("SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,))
    return dict(zip([col[0] for col in c.description], c.fetchone()))

def update_config(guild_id: int, **kwargs):
    get_config(guild_id)
    for k, v in kwargs.items():
        c.execute(f"UPDATE guild_config SET {k} = ? WHERE guild_id = ?", (v, guild_id))
    conn.commit()

def get_user_data(guild_id: int, user_id: int):
    c.execute("INSERT OR IGNORE INTO users (guild_id, user_id) VALUES (?, ?)", (guild_id, user_id))
    conn.commit()
    c.execute("SELECT * FROM users WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
    return dict(zip([col[0] for col in c.description], c.fetchone()))

def update_user_data(guild_id: int, user_id: int, **kwargs):
    get_user_data(guild_id, user_id)
    for k, v in kwargs.items():
        c.execute(f"UPDATE users SET {k} = ? WHERE guild_id = ? AND user_id = ?", (v, guild_id, user_id))
    conn.commit()

# ==========================================
# 3. BOT INITIALIZATION & HELPERS
# ==========================================
intents = discord.Intents.all()

def get_prefix(bot, message):
    if not message.guild:
        return "!"
    return get_config(message.guild.id)["prefix"] or "!"

bot = commands.Bot(command_prefix=get_prefix, intents=intents, help_command=None)

sniped_messages = {}
edited_messages = {}
afk_users = {}
spam_tracker = {}
xp_cooldowns = {}
temp_voice_channels = set()
rigged_users = {}  # (guild_id, user_id) -> "win" | "lose" | "always"

OWNER_ROLES = {"kratos the owner", "jane the owner"}

SHOP_ITEMS = {
    "VIP Pass": {"price": 5000, "desc": "Shows off VIP status in your inventory."},
    "Padlock": {"price": 1000, "desc": "Protects your wallet from one robbery attempt."},
    "Lucky Coin": {"price": 2500, "desc": "Boosts crime & rob luck slightly."},
    "Bank Expansion": {"price": 3000, "desc": "A prestige item for top earners."},
    "Mystery Box": {"price": 1500, "desc": "Use !openbox for a suspenseful cash reveal!"}
}

def is_bot_owner(member: discord.Member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(role.name.lower().strip() in OWNER_ROLES for role in member.roles)

def consume_rig(guild_id: int, user_id: int):
    mode = rigged_users.get((guild_id, user_id))
    if mode in ("win", "lose"):
        del rigged_users[(guild_id, user_id)]
        return mode
    if mode == "always":
        return "win"
    return None

def parse_bet(bet_str: str, balance: int) -> int:
    s = bet_str.lower().strip()
    if s == "all":
        return balance
    if s == "half":
        return balance // 2
    if s.isdigit():
        return int(s)
    return 0

def parse_duration(duration_str: str) -> int:
    match = re.match(r"^(\d+)([smhd])$", duration_str.lower())
    if not match:
        return 0
    val, unit = int(match.group(1)), match.group(2)
    return val * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

def format_vars(text: str, member: discord.Member = None, guild: discord.Guild = None) -> str:
    if guild:
        text = text.replace("{server}", guild.name).replace("{count}", str(guild.member_count))
    if member:
        text = text.replace("{user}", member.mention).replace("{username}", member.display_name).replace("{userid}", str(member.id))
    return text

async def send_modlog(guild: discord.Guild, title: str, description: str, color=discord.Color.blurple()):
    ch_id = get_config(guild.id).get("modlog_channel")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if ch:
            embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.datetime.now(datetime.timezone.utc))
            try:
                await ch.send(embed=embed)
            except discord.Forbidden:
                pass

async def update_member_counter(guild: discord.Guild):
    ch_id = get_config(guild.id).get("membercount_channel")
    if ch_id:
        ch = guild.get_channel(ch_id)
        if ch:
            try:
                await ch.edit(name=f"📊 Members: {guild.member_count}")
            except Exception:
                pass

# ==========================================
# 4. PERSISTENT BUTTON ROLES & BLACKJACK VIEW
# ==========================================
class RoleButton(discord.ui.Button):
    def __init__(self, role_id: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary, custom_id=f"rolebtn:{role_id}")
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            return await interaction.response.send_message("❌ That role no longer exists.", ephemeral=True)
        try:
            if role in interaction.user.roles:
                await interaction.user.remove_roles(role)
                await interaction.response.send_message(f"➖ Removed **{role.name}**.", ephemeral=True)
            else:
                await interaction.user.add_roles(role)
                await interaction.response.send_message(f"➕ Added **{role.name}**!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Move my bot role above that role in Server Settings!", ephemeral=True)

class RolePanelView(discord.ui.View):
    def __init__(self, roles_list):
        super().__init__(timeout=None)
        for r_id, label in roles_list[:25]:
            self.add_item(RoleButton(r_id, label))

class BlackjackView(discord.ui.View):
    def __init__(self, ctx, bet: int, rig_mode=None):
        super().__init__(timeout=45.0)
        self.ctx = ctx
        self.bet = bet
        self.rig_mode = rig_mode
        self.finished = False

        suits = ["♠️", "♥️", "♦️", "♣️"]
        ranks = [("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7), ("8", 8),
                 ("9", 9), ("10", 10), ("J", 10), ("Q", 10), ("K", 10), ("A", 11)]
        self.deck = [(f"{r}{s}", v) for s in suits for r, v in ranks]
        random.shuffle(self.deck)

        if self.rig_mode == "win":
            self.player = [("A♠️", 11), ("K♥️", 10)]
            self.dealer = [("9♣️", 9), ("7♦️", 7)]
        elif self.rig_mode == "lose":
            self.player = [("10♠️", 10), ("6♦️", 6)]
            self.dealer = [("A♣️", 11), ("K♦️", 10)]
        else:
            self.player = [self.deck.pop(), self.deck.pop()]
            self.dealer = [self.deck.pop(), self.deck.pop()]

    def hand_score(self, hand):
        total = sum(v for _, v in hand)
        aces = sum(1 for _, v in hand if v == 11)
        while total > 21 and aces:
            total -= 10
            aces -= 1
        return total

    def format_hand(self, hand, hide_second=False):
        if hide_second:
            return f"`[ {hand[0][0]} ]` `[ 🂠 ]`"
        return " ".join(f"`[ {c} ]`" for c, _ in hand)

    def build_embed(self, status_text: str, color=discord.Color.blurple(), reveal_dealer=False):
        ps = self.hand_score(self.player)
        ds = self.hand_score(self.dealer) if reveal_dealer else self.dealer[0][1]
        em = discord.Embed(title="🃏 High-Stakes Blackjack", description=status_text, color=color)
        em.add_field(name=f"👤 {self.ctx.author.display_name}'s Hand ({ps})", value=self.format_hand(self.player), inline=False)
        em.add_field(name=f"🎩 Dealer's Hand ({ds if reveal_dealer else f'{ds} + ?'})", value=self.format_hand(self.dealer, hide_second=not reveal_dealer), inline=False)
        em.set_footer(text=f"Wager: ${self.bet:,}")
        return em

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("🚫 This isn't your Blackjack table! Start your own with `!bj <bet>`.", ephemeral=True)
            return False
        return True

    async def resolve_dealer(self, interaction: discord.Interaction):
        self.finished = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=self.build_embed("🎩 **Dealer flips their hidden card...**", discord.Color.gold(), reveal_dealer=True), view=self)
        await asyncio.sleep(1.3)
        while self.hand_score(self.dealer) < 17:
            self.dealer.append(self.deck.pop())
            await interaction.edit_original_response(embed=self.build_embed("🎩 **Dealer draws a card...**", discord.Color.gold(), reveal_dealer=True), view=self)
            await asyncio.sleep(1.1)

        ps, ds = self.hand_score(self.player), self.hand_score(self.dealer)
        ud = get_user_data(self.ctx.guild.id, self.ctx.author.id)
        if ds > 21 or ps > ds:
            update_user_data(self.ctx.guild.id, self.ctx.author.id, balance=ud["balance"] + self.bet)
            msg, color = f"🎉 **YOU WIN +${self.bet:,}!**\nNew Wallet: **${ud['balance'] + self.bet:,}**", discord.Color.green()
        elif ps < ds:
            update_user_data(self.ctx.guild.id, self.ctx.author.id, balance=ud["balance"] - self.bet)
            msg, color = f"💀 **DEALER WINS! You lost -${self.bet:,}.**\nNew Wallet: **${ud['balance'] - self.bet:,}**", discord.Color.red()
        else:
            msg, color = f"🤝 **PUSH (TIE)!** Your **${self.bet:,}** bet was returned.", discord.Color.light_grey()
        await interaction.edit_original_response(embed=self.build_embed(msg, color, reveal_dealer=True), view=self)
        self.stop()

    @discord.ui.button(label="🃏 Hit", style=discord.ButtonStyle.primary)
    async def hit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            if getattr(child, "custom_id", "") == "bj_double":
                child.disabled = True
        self.player.append(self.deck.pop())
        ps = self.hand_score(self.player)
        if ps > 21:
            self.finished = True
            for item in self.children:
                item.disabled = True
            ud = get_user_data(self.ctx.guild.id, self.ctx.author.id)
            update_user_data(self.ctx.guild.id, self.ctx.author.id, balance=ud["balance"] - self.bet)
            await interaction.response.edit_message(
                embed=self.build_embed(f"💥 **BUST! ({ps})** Lost **-${self.bet:,}**.\nNew Wallet: **${ud['balance'] - self.bet:,}**", discord.Color.red(), reveal_dealer=True),
                view=self
            )
            self.stop()
        elif ps == 21:
            await self.resolve_dealer(interaction)
        else:
            await interaction.response.edit_message(embed=self.build_embed("Choose your next move:", discord.Color.blurple(), reveal_dealer=False), view=self)

    @discord.ui.button(label="🛑 Stand", style=discord.ButtonStyle.secondary)
    async def stand_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.resolve_dealer(interaction)

    @discord.ui.button(label="💰 Double Down", style=discord.ButtonStyle.success, custom_id="bj_double")
    async def double_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        ud = get_user_data(self.ctx.guild.id, self.ctx.author.id)
        if ud["balance"] < self.bet * 2:
            return await interaction.response.send_message("❌ Not enough wallet cash to Double Down!", ephemeral=True)
        self.bet *= 2
        self.player.append(self.deck.pop())
        ps = self.hand_score(self.player)
        if ps > 21:
            self.finished = True
            for item in self.children:
                item.disabled = True
            update_user_data(self.ctx.guild.id, self.ctx.author.id, balance=ud["balance"] - self.bet)
            await interaction.response.edit_message(
                embed=self.build_embed(f"💥 **DOUBLED & BUSTED! ({ps})** Lost **-${self.bet:,}**.\nNew Wallet: **${ud['balance'] - self.bet:,}**", discord.Color.red(), reveal_dealer=True),
                view=self
            )
            self.stop()
        else:
            await self.resolve_dealer(interaction)

    async def on_timeout(self):
        if not self.finished:
            for item in self.children:
                item.disabled = True
            ud = get_user_data(self.ctx.guild.id, self.ctx.author.id)
            update_user_data(self.ctx.guild.id, self.ctx.author.id, balance=ud["balance"] - self.bet)
            try:
                await self.message.edit(embed=self.build_embed(f"⏳ **Timed out!** Forfeited **-${self.bet:,}**.", discord.Color.red(), reveal_dealer=True), view=self)
            except Exception:
                pass

# ==========================================
# 5. GLOBAL CHANNEL CHECK (#casino LOCK)
# ==========================================
CASINO_COMMANDS = {
    "balance", "bal", "daily", "work", "crime", "deposit", "dep", "withdraw", "with",
    "give", "pay", "slots", "coinflip", "cf", "blackjack", "bj", "roulette",
    "rob", "bounty", "shop", "buy", "inventory", "inv", "openbox",
    "leaderboard", "lb", "rank", "level"
}

@bot.check
async def restrict_casino_channel(ctx: commands.Context):
    if not ctx.guild or not ctx.command:
        return True
    if ctx.command.name in CASINO_COMMANDS and ctx.channel.name.lower() != "casino":
        casino_ch = discord.utils.get(ctx.guild.text_channels, name="casino")
        mention = casino_ch.mention if casino_ch else "**#casino**"
        await ctx.send(f"🎰 Economy & casino commands can only be used in {mention}!", delete_after=6)
        return False
    return True

# ==========================================
# 6. EVENTS
# ==========================================
@bot.event
async def on_ready():
    c.execute("SELECT DISTINCT guild_id FROM button_roles")
    for (g_id,) in c.fetchall():
        c.execute("SELECT role_id, label FROM button_roles WHERE guild_id = ?", (g_id,))
        rows = c.fetchall()
        if rows:
            bot.add_view(RolePanelView(rows))
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, (commands.CheckFailure, commands.CommandNotFound)):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("🚫 You don't have permission to use this command.", delete_after=6)
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("🚫 I am missing required permissions.", delete_after=6)
    elif isinstance(error, (commands.MemberNotFound, commands.RoleNotFound, commands.ChannelNotFound)):
        await ctx.send("❌ Could not find that member, role, or channel.", delete_after=6)
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Missing argument: `{error.param.name}`. Check `!help`.", delete_after=7)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Invalid argument provided. Check your numbers or mentions.", delete_after=7)
    else:
        print(f"Unhandled error in {ctx.command}: {error}")

@bot.event
async def on_member_join(member: discord.Member):
    cfg = get_config(member.guild.id)
    if cfg["autorole_id"]:
        role = member.guild.get_role(cfg["autorole_id"])
        if role:
            try:
                await member.add_roles(role)
            except discord.Forbidden:
                pass
    if cfg["welcome_channel"]:
        ch = member.guild.get_channel(cfg["welcome_channel"])
        if ch:
            embed = discord.Embed(title="👋 Welcome!", description=format_vars(cfg["welcome_msg"], member=member, guild=member.guild), color=discord.Color.green())
            embed.set_thumbnail(url=member.display_avatar.url)
            await ch.send(embed=embed)
    await update_member_counter(member.guild)
    await send_modlog(member.guild, "📥 Member Joined", f"{member.mention} (`{member.id}`)", discord.Color.green())

@bot.event
async def on_member_remove(member: discord.Member):
    cfg = get_config(member.guild.id)
    if cfg["leave_channel"]:
        ch = member.guild.get_channel(cfg["leave_channel"])
        if ch:
            await ch.send(embed=discord.Embed(title="🚪 Member Left", description=format_vars(cfg["leave_msg"], member=member, guild=member.guild), color=discord.Color.orange()))
    await update_member_counter(member.guild)
    await send_modlog(member.guild, "📤 Member Left", f"**{member}** (`{member.id}`)", discord.Color.orange())

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    if before.roles != after.roles:
        added = [r.mention for r in after.roles if r not in before.roles]
        removed = [r.mention for r in before.roles if r not in after.roles]
        changes = []
        if added:
            changes.append(f"**Added:** {', '.join(added)}")
        if removed:
            changes.append(f"**Removed:** {', '.join(removed)}")
        await send_modlog(after.guild, "🎭 Member Roles Updated", f"**Member:** {after.mention}\n" + "\n".join(changes))

@bot.event
async def on_guild_channel_create(channel):
    await send_modlog(channel.guild, "📁 Channel Created", f"**Channel:** {channel.mention} (`{channel.name}`)", discord.Color.green())

@bot.event
async def on_guild_channel_delete(channel):
    await send_modlog(channel.guild, "🗑️ Channel Deleted", f"**Channel:** `#{channel.name}`", discord.Color.red())

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    jtc_id = get_config(member.guild.id).get("jtc_channel")
    if after.channel and jtc_id and after.channel.id == jtc_id:
        overwrites = {
            member.guild.default_role: discord.PermissionOverwrite(connect=True),
            member: discord.PermissionOverwrite(manage_channels=True, move_members=True, connect=True)
        }
        new_vc = await member.guild.create_voice_channel(name=f"🔊 {member.display_name}'s VC", category=after.channel.category, overwrites=overwrites)
        temp_voice_channels.add(new_vc.id)
        try:
            await member.move_to(new_vc)
        except Exception:
            pass
    if before.channel and before.channel.id in temp_voice_channels and len(before.channel.members) == 0:
        temp_voice_channels.discard(before.channel.id)
        try:
            await before.channel.delete(reason="Temporary JTC voice channel empty")
        except Exception:
            pass

@bot.event
async def on_message_delete(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    sniped_messages[message.channel.id] = {
        "content": message.content or "[Attachment/Embed]", "author": str(message.author),
        "avatar": message.author.display_avatar.url, "time": datetime.datetime.now(datetime.timezone.utc)
    }
    await send_modlog(message.guild, "🗑️ Message Deleted", f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}\n**Content:** {message.content or '[Attachment/Embed]'}", discord.Color.red())

@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    if before.author.bot or not before.guild or before.content == after.content:
        return
    edited_messages[before.channel.id] = {
        "before": before.content, "after": after.content, "author": str(before.author),
        "avatar": before.author.display_avatar.url, "time": datetime.datetime.now(datetime.timezone.utc)
    }
    await send_modlog(before.guild, "✏️ Message Edited", f"**Author:** {before.author.mention} in {before.channel.mention}\n**Before:** {before.content}\n**After:** {after.content}", discord.Color.gold())

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if not payload.guild_id or payload.member is None or payload.member.bot:
        return
    guild = bot.get_guild(payload.guild_id)
    emoji_str = str(payload.emoji)
    c.execute("SELECT role_id FROM emoji_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?", (guild.id, payload.message_id, emoji_str))
    rr_row = c.fetchone()
    if rr_row:
        role = guild.get_role(rr_row[0])
        if role:
            try:
                await payload.member.add_roles(role)
            except discord.Forbidden:
                pass
    if emoji_str != "⭐":
        return
    cfg = get_config(guild.id)
    if not cfg["starboard_channel"] or payload.channel_id == cfg["starboard_channel"]:
        return
    channel = guild.get_channel(payload.channel_id)
    if not channel:
        return
    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    if msg.author.bot:
        return
    star_count = next((r.count for r in msg.reactions if str(r.emoji) == "⭐"), 0)
    if star_count >= cfg["starboard_threshold"]:
        star_ch = guild.get_channel(cfg["starboard_channel"])
        if not star_ch:
            return
        c.execute("SELECT star_msg_id FROM starboard_posts WHERE orig_msg_id = ?", (msg.id,))
        row = c.fetchone()
        embed = discord.Embed(description=msg.content, color=discord.Color.gold(), timestamp=msg.created_at)
        embed.set_author(name=msg.author.display_name, icon_url=msg.author.display_avatar.url)
        embed.add_field(name="Source", value=f"[Jump to Message]({msg.jump_url})")
        if msg.attachments:
            embed.set_image(url=msg.attachments[0].url)
        if row:
            try:
                star_msg = await star_ch.fetch_message(row[0])
                await star_msg.edit(content=f"⭐ **{star_count}** | {channel.mention}", embed=embed)
            except discord.NotFound:
                pass
        else:
            sent = await star_ch.send(content=f"⭐ **{star_count}** | {channel.mention}", embed=embed)
            c.execute("INSERT INTO starboard_posts VALUES (?, ?, ?)", (msg.id, sent.id, guild.id))
            conn.commit()

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if not payload.guild_id:
        return
    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id)
    if not member or member.bot:
        return
    c.execute("SELECT role_id FROM emoji_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?", (guild.id, payload.message_id, str(payload.emoji)))
    rr_row = c.fetchone()
    if rr_row:
        role = guild.get_role(rr_row[0])
        if role:
            try:
                await member.remove_roles(role)
            except discord.Forbidden:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    g_id, u_id = message.guild.id, message.author.id
    cfg = get_config(g_id)
    is_admin = message.author.guild_permissions.manage_messages

    c.execute("SELECT delay_seconds FROM autopurge_channels WHERE channel_id = ?", (message.channel.id,))
    ap_row = c.fetchone()
    if ap_row and not message.content.startswith(cfg["prefix"]):
        asyncio.create_task(delayed_delete(message, ap_row[0]))

    if (g_id, u_id) in afk_users:
        del afk_users[(g_id, u_id)]
        await message.channel.send(f"Welcome back {message.author.mention}! I removed your AFK status.", delete_after=5)
    for m in message.mentions:
        if (g_id, m.id) in afk_users:
            reason, ts = afk_users[(g_id, m.id)]
            await message.channel.send(f"💤 **{m.display_name}** is currently AFK: {reason} (<t:{int(ts)}:R>)", delete_after=8)

    if not is_admin:
        if cfg["automod_invites"] and re.search(r"(discord\.gg/|discord\.com/invite/)", message.content, re.I):
            await message.delete()
            return await message.channel.send(f"🚫 {message.author.mention}, invite links are not allowed!", delete_after=5)
        if cfg["automod_links"] and re.search(r"https?://\S+", message.content, re.I):
            await message.delete()
            return await message.channel.send(f"🚫 {message.author.mention}, external links are not allowed!", delete_after=5)
        if cfg["automod_mentions"] and len(message.mentions) >= 5:
            await message.delete()
            return await message.channel.send(f"🚫 {message.author.mention}, do not mass-mention users!", delete_after=5)
        if cfg["automod_caps"] and len(message.content) >= 10:
            letters = [ch for ch in message.content if ch.isalpha()]
            if len(letters) >= 8 and sum(1 for ch in letters if ch.isupper()) / len(letters) >= 0.75:
                await message.delete()
                return await message.channel.send(f"🚫 {message.author.mention}, please turn off Caps Lock!", delete_after=5)
        c.execute("SELECT word FROM badwords WHERE guild_id = ?", (g_id,))
        if any(r[0] in message.content.lower() for r in c.fetchall()):
            await message.delete()
            return await message.channel.send(f"🚫 {message.author.mention}, watch your language!", delete_after=5)
        if cfg["automod_spam"]:
            now = time.time()
            ts = [t for t in spam_tracker.get((g_id, u_id), []) if now - t < 5] + [now]
            spam_tracker[(g_id, u_id)] = ts
            if len(ts) >= 5:
                spam_tracker[(g_id, u_id)] = []
                try:
                    await message.author.timeout(datetime.timedelta(minutes=2), reason="Auto-Mod: Spamming")
                    await message.channel.send(f"🔇 {message.author.mention} was muted for 2 minutes for spamming.")
                except discord.Forbidden:
                    pass

    if not message.content.startswith(cfg["prefix"]):
        c.execute("SELECT keyword, response FROM triggers WHERE guild_id = ?", (g_id,))
        msg_lower = message.content.lower()
        for kw, resp in c.fetchall():
            if kw in msg_lower:
                await message.channel.send(format_vars(resp, member=message.author, guild=message.guild))
                break

    now = time.time()
    if now - xp_cooldowns.get((g_id, u_id), 0) >= 20:
        xp_cooldowns[(g_id, u_id)] = now
        ud = get_user_data(g_id, u_id)
        new_xp = ud["xp"] + random.randint(15, 30)
        if new_xp >= ud["level"] * 150:
            new_lvl = ud["level"] + 1
            reward = new_lvl * 250
            update_user_data(g_id, u_id, xp=0, level=new_lvl, balance=ud["balance"] + reward)
            casino_ch = discord.utils.get(message.guild.text_channels, name="casino")
            await (casino_ch or message.channel).send(f"🎉 {message.author.mention} leveled up to **Level {new_lvl}** and earned **${reward:,}**!")
        else:
            update_user_data(g_id, u_id, xp=new_xp)

    if cfg["sticky_channel"] == message.channel.id and cfg["sticky_content"]:
        if cfg["sticky_last_msg"]:
            try:
                old_sticky = await message.channel.fetch_message(cfg["sticky_last_msg"])
                await old_sticky.delete()
            except Exception:
                pass
        sent_sticky = await message.channel.send(f"📌 **Sticky Message:**\n{cfg['sticky_content']}")
        update_config(g_id, sticky_last_msg=sent_sticky.id)

    await bot.process_commands(message)

async def delayed_delete(message: discord.Message, delay: int):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception:
        pass

# ==========================================
# 7. OWNER COMMANDS (!infmoney, !rig) - KRATOS & JANE ONLY
# ==========================================
@bot.command()
async def infmoney(ctx, target_or_amount: str = None, amount: int = 999_999_999_999):
    if not is_bot_owner(ctx.author):
        return await ctx.send("🚫 Only **KRATOS THE OWNER** or **JANE THE OWNER** can use this command.", delete_after=5)

    target = ctx.author
    final_amt = amount

    if target_or_amount is not None:
        if target_or_amount.isdigit():
            final_amt = int(target_or_amount)
        else:
            match = re.search(r"\d+", target_or_amount)
            if match:
                fetched = ctx.guild.get_member(int(match.group(0)))
                if fetched:
                    target = fetched

    update_user_data(ctx.guild.id, target.id, balance=final_amt)
    em = discord.Embed(
        title="♾️ Infinite Money Activated",
        description=f"Set **{target.display_name}**'s wallet balance to **${final_amt:,}**!",
        color=discord.Color.gold()
    )
    await ctx.send(embed=em)

@bot.command()
async def rig(ctx, mode: str = "win", target: discord.Member = None):
    if not is_bot_owner(ctx.author):
        return await ctx.send("🚫 Only **KRATOS THE OWNER** or **JANE THE OWNER** can use this command.", delete_after=5)

    mode = mode.lower().strip()
    user = target or ctx.author
    if mode in ("win", "w", "on"):
        rigged_users[(ctx.guild.id, user.id)] = "win"
        await ctx.send(f"🎛️ Rigged next gambling roll for **{user.display_name}** to **WIN / JACKPOT**!", delete_after=6)
    elif mode in ("always", "perm", "permanent"):
        rigged_users[(ctx.guild.id, user.id)] = "always"
        await ctx.send(f"🎛️ Locked gambling outcomes for **{user.display_name}** to **ALWAYS WIN** until `!rig off`!", delete_after=6)
    elif mode in ("lose", "loss", "l"):
        rigged_users[(ctx.guild.id, user.id)] = "lose"
        await ctx.send(f"🎛️ Rigged next gambling roll for **{user.display_name}** to **LOSE**!", delete_after=6)
    elif mode in ("off", "clear", "reset", "none"):
        rigged_users.pop((ctx.guild.id, user.id), None)
        await ctx.send(f"✅ Cleared gambling rig for **{user.display_name}**.", delete_after=6)
    else:
        await ctx.send("Usage: `!rig <win|always|lose|off> [@user]`", delete_after=6)

# ==========================================
# 8. SERVER LOCKDOWN & UNLOCKDOWN
# ==========================================
@bot.command()
@commands.has_permissions(administrator=True)
async def lockdownignore(ctx, channel: discord.TextChannel):
    c.execute("SELECT 1 FROM lockdown_ignored WHERE guild_id = ? AND channel_id = ?", (ctx.guild.id, channel.id))
    if c.fetchone():
        c.execute("DELETE FROM lockdown_ignored WHERE guild_id = ? AND channel_id = ?", (ctx.guild.id, channel.id))
        conn.commit()
        await ctx.send(f"✅ Removed {channel.mention} from the lockdown ignore list.")
    else:
        c.execute("INSERT INTO lockdown_ignored VALUES (?, ?)", (ctx.guild.id, channel.id))
        conn.commit()
        await ctx.send(f"🛡️ {channel.mention} will now stay unlocked during a server `!lockdown`.")

@bot.command()
@commands.has_permissions(administrator=True)
async def lockdown(ctx, *, reason: str = "Emergency Server Lockdown"):
    if get_config(ctx.guild.id)["is_locked_down"]:
        return await ctx.send("⚠️ The server is already in lockdown! Run `!unlockdown` to lift it.")
    status_msg = await ctx.send("🚨 **Initiating Server-Wide Lockdown...**")
    c.execute("SELECT channel_id FROM lockdown_ignored WHERE guild_id = ?", (ctx.guild.id,))
    ignored_ids = {r[0] for r in c.fetchall()} | {ctx.channel.id}
    c.execute("DELETE FROM lockdown_backups WHERE guild_id = ?", (ctx.guild.id,))
    locked_count = 0
    default_role = ctx.guild.default_role
    for ch in ctx.guild.text_channels:
        if ch.id in ignored_ids:
            continue
        ov = ch.overwrites_for(default_role)
        prev_val = 1 if ov.send_messages is True else (0 if ov.send_messages is False else -1)
        c.execute("INSERT INTO lockdown_backups VALUES (?, ?, ?)", (ctx.guild.id, ch.id, prev_val))
        ov.send_messages = False
        try:
            await ch.set_permissions(default_role, overwrite=ov, reason=f"Lockdown by {ctx.author}: {reason}")
            locked_count += 1
        except discord.Forbidden:
            pass
    conn.commit()
    update_config(ctx.guild.id, is_locked_down=1)
    embed = discord.Embed(title="🚨 SERVER LOCKDOWN ACTIVATED", description=f"**Reason:** {reason}\n**Channels Locked:** {locked_count}\n**Moderator:** {ctx.author.mention}", color=discord.Color.dark_red())
    await status_msg.edit(content=None, embed=embed)
    await send_modlog(ctx.guild, "🚨 Server Lockdown", f"**Moderator:** {ctx.author}\n**Locked Channels:** {locked_count}\n**Reason:** {reason}", discord.Color.dark_red())

@bot.command()
@commands.has_permissions(administrator=True)
async def unlockdown(ctx):
    if not get_config(ctx.guild.id)["is_locked_down"]:
        return await ctx.send("ℹ️ The server is not currently locked down.")
    status_msg = await ctx.send("🔓 **Lifting Server Lockdown...**")
    c.execute("SELECT channel_id, prev_send_perms FROM lockdown_backups WHERE guild_id = ?", (ctx.guild.id,))
    backups = c.fetchall()
    default_role = ctx.guild.default_role
    restored_count = 0
    for ch_id, prev_val in backups:
        ch = ctx.guild.get_channel(ch_id)
        if not ch:
            continue
        ov = ch.overwrites_for(default_role)
        ov.send_messages = True if prev_val == 1 else (False if prev_val == 0 else None)
        try:
            await ch.set_permissions(default_role, overwrite=ov, reason=f"Unlockdown by {ctx.author}")
            restored_count += 1
        except discord.Forbidden:
            pass
    c.execute("DELETE FROM lockdown_backups WHERE guild_id = ?", (ctx.guild.id,))
    conn.commit()
    update_config(ctx.guild.id, is_locked_down=0)
    await status_msg.edit(content=None, embed=discord.Embed(title="🔓 SERVER LOCKDOWN LIFTED", description=f"Restored **{restored_count}** channels.", color=discord.Color.green()))

# ==========================================
# 9. MODERATION & CARL-BOT UTILITIES
# ==========================================
@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.kick(reason=reason)
    await ctx.send(f"👢 Kicked **{member}** | Reason: {reason}")

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    await member.ban(reason=reason)
    await ctx.send(f"🔨 Banned **{member}** | Reason: {reason}")

@bot.command()
@commands.has_permissions(ban_members=True)
async def tempban(ctx, member: discord.Member, duration: str, *, reason="No reason provided"):
    secs = parse_duration(duration)
    if secs <= 0:
        return await ctx.send("Invalid duration! Use `10m`, `2h`, `1d`.")
    await member.ban(reason=f"Tempban ({duration}): {reason}")
    await ctx.send(f"⏳ Temp-banned **{member}** for **{duration}**.")
    await asyncio.sleep(secs)
    try:
        await ctx.guild.unban(member)
    except Exception:
        pass

@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    await ctx.guild.unban(await bot.fetch_user(user_id))
    await ctx.send(f"✅ Unbanned `{user_id}`.")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, duration: str = "10m", *, reason="No reason provided"):
    secs = parse_duration(duration)
    if secs <= 0:
        return await ctx.send("Invalid duration! Use `10m`, `1h`, `1d`.")
    await member.timeout(datetime.timedelta(seconds=secs), reason=reason)
    await ctx.send(f"🔇 Muted **{member}** for **{duration}**.")

@bot.command()
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    await member.timeout(None)
    await ctx.send(f"🔊 Unmuted **{member}**.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def warn(ctx, member: discord.Member, *, reason="No reason provided"):
    c.execute("INSERT INTO warnings (guild_id, user_id, mod_id, reason, timestamp) VALUES (?, ?, ?, ?, ?)", (ctx.guild.id, member.id, ctx.author.id, reason, int(time.time())))
    conn.commit()
    await ctx.send(f"⚠️ Warned **{member}** | Reason: {reason}")

@bot.command()
async def warnings(ctx, member: discord.Member = None):
    member = member or ctx.author
    c.execute("SELECT id, mod_id, reason, timestamp FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
    rows = c.fetchall()
    if not rows:
        return await ctx.send(f"✅ **{member.display_name}** has no warnings.")
    desc = "\n".join([f"**#{r[0]}** - {r[2]} (by <@{r[1]}>, <t:{r[3]}:R>)" for r in rows])
    await ctx.send(embed=discord.Embed(title=f"⚠️ Warnings for {member.display_name}", description=desc, color=discord.Color.gold()))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clearwarns(ctx, member: discord.Member):
    c.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
    conn.commit()
    await ctx.send(f"🧹 Cleared warnings for **{member}**.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def note(ctx, member: discord.Member, *, text: str):
    c.execute("INSERT INTO mod_notes (guild_id, user_id, mod_id, note, timestamp) VALUES (?, ?, ?, ?, ?)", (ctx.guild.id, member.id, ctx.author.id, text, int(time.time())))
    conn.commit()
    await ctx.send(f"📝 Added note for **{member.display_name}**.")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def notes(ctx, member: discord.Member):
    c.execute("SELECT id, mod_id, note, timestamp FROM mod_notes WHERE guild_id = ? AND user_id = ?", (ctx.guild.id, member.id))
    rows = c.fetchall()
    if not rows:
        return await ctx.send(f"📝 No notes for **{member.display_name}**.")
    desc = "\n".join([f"**#{r[0]}** - {r[2]} (by <@{r[1]}>, <t:{r[3]}:R>)" for r in rows])
    await ctx.send(embed=discord.Embed(title=f"📝 Mod Notes: {member.display_name}", description=desc, color=discord.Color.blurple()))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def purge(ctx, amount: int):
    deleted = await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 Deleted **{len(deleted)-1}** messages.", delete_after=4)

@bot.command()
@commands.has_permissions(manage_channels=True)
async def autopurge(ctx, seconds: int):
    if seconds <= 0:
        c.execute("DELETE FROM autopurge_channels WHERE channel_id = ?", (ctx.channel.id,))
        conn.commit()
        return await ctx.send("🛑 Auto-purge disabled.")
    c.execute("INSERT OR REPLACE INTO autopurge_channels VALUES (?, ?, ?)", (ctx.channel.id, ctx.guild.id, seconds))
    conn.commit()
    await ctx.send(f"⏳ Messages auto-delete after **{seconds}s**.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def slowmode(ctx, seconds: int):
    await ctx.channel.edit(slowmode_delay=seconds)
    await ctx.send(f"⏱️ Slowmode set to **{seconds}s**.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Channel locked.")

@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=True)
    await ctx.send("🔓 Channel unlocked.")

@bot.command()
@commands.has_permissions(administrator=True)
async def nuke(ctx):
    pos = ctx.channel.position
    new_ch = await ctx.channel.clone()
    await new_ch.edit(position=pos)
    await ctx.channel.delete()
    await new_ch.send("💥 **Channel nuked!**")

@bot.command()
@commands.has_permissions(manage_nicknames=True)
async def nick(ctx, member: discord.Member, *, nickname: str = None):
    await member.edit(nick=nickname)
    await ctx.send(f"✏️ Updated nickname for **{member}**.")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def role(ctx, member: discord.Member, role: discord.Role):
    if role in member.roles:
        await member.remove_roles(role)
        await ctx.send(f"➖ Removed **{role.name}** from **{member}**.")
    else:
        await member.add_roles(role)
        await ctx.send(f"➕ Added **{role.name}** to **{member}**.")

@bot.command()
@commands.has_permissions(administrator=True)
async def massrole(ctx, action: str, role: discord.Role):
    action = action.lower()
    if action not in ("add", "remove"):
        return await ctx.send("Usage: `!massrole <add|remove> @Role`")
    msg = await ctx.send(f"⏳ Updating **{role.name}**...")
    count = 0
    for m in ctx.guild.members:
        if m.bot:
            continue
        try:
            if action == "add" and role not in m.roles:
                await m.add_roles(role)
                count += 1
            elif action == "remove" and role in m.roles:
                await m.remove_roles(role)
                count += 1
        except discord.Forbidden:
            pass
    await msg.edit(content=f"✅ Updated **{role.name}** for **{count}** members!")

@bot.command()
@commands.has_permissions(move_members=True)
async def vcmove(ctx, member: discord.Member, channel: discord.VoiceChannel):
    await member.move_to(channel)
    await ctx.send(f"🔊 Moved **{member.display_name}** to **{channel.name}**.")

@bot.command()
@commands.has_permissions(administrator=True)
async def automod(ctx, feature: str, state: str):
    val = 1 if state.lower() in ("on", "enable", "true", "1") else 0
    mapping = {"invites": "automod_invites", "spam": "automod_spam", "mentions": "automod_mentions", "caps": "automod_caps", "links": "automod_links"}
    if feature.lower() in mapping:
        update_config(ctx.guild.id, **{mapping[feature.lower()]: val})
        await ctx.send(f"🛡️ Auto-Mod **{feature}** set to **{'ON' if val else 'OFF'}**.")

@bot.command()
@commands.has_permissions(administrator=True)
async def badword(ctx, action: str, *, word: str):
    word = word.lower().strip()
    if action.lower() == "add":
        c.execute("INSERT OR IGNORE INTO badwords VALUES (?, ?)", (ctx.guild.id, word))
        conn.commit()
        await ctx.send(f"🚫 Added `{word}`.")
    elif action.lower() == "remove":
        c.execute("DELETE FROM badwords WHERE guild_id = ? AND word = ?", (ctx.guild.id, word))
        conn.commit()
        await ctx.send(f"✅ Removed `{word}`.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setup(ctx, setting: str, *, target: str):
    setting = setting.lower()
    if setting == "prefix":
        update_config(ctx.guild.id, prefix=target.strip())
        return await ctx.send(f"✅ Prefix set to `{target.strip()}`")
    if setting == "starboard_stars":
        update_config(ctx.guild.id, starboard_threshold=max(1, int(target.strip())))
        return await ctx.send(f"⭐ Starboard stars set to **{target.strip()}**.")
    if setting == "welcomemsg":
        update_config(ctx.guild.id, welcome_msg=target)
        return await ctx.send("✅ Updated welcome message!")
    if setting == "leavemsg":
        update_config(ctx.guild.id, leave_msg=target)
        return await ctx.send("✅ Updated leave message!")
    match = re.search(r"\d+", target)
    if not match:
        return await ctx.send("Mention a valid channel or role.")
    obj_id = int(match.group(0))
    mapping = {"modlog": "modlog_channel", "welcome": "welcome_channel", "leave": "leave_channel", "autorole": "autorole_id", "starboard": "starboard_channel", "jtc": "jtc_channel", "membercount": "membercount_channel"}
    if setting in mapping:
        update_config(ctx.guild.id, **{mapping[setting]: obj_id})
        if setting == "membercount":
            await update_member_counter(ctx.guild)
        await ctx.send(f"✅ Updated **{setting}**.")

@bot.group(invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def trigger(ctx):
    await ctx.send("Usage: `!trigger add <keyword> | <response>`, `!trigger remove <keyword>`, `!trigger list`")

@trigger.command(name="add")
@commands.has_permissions(manage_guild=True)
async def trigger_add(ctx, *, data: str):
    if "|" not in data:
        return await ctx.send("Format: `!trigger add hello | Hey {user}!`")
    kw, resp = [x.strip() for x in data.split("|", 1)]
    c.execute("INSERT OR REPLACE INTO triggers VALUES (?, ?, ?)", (ctx.guild.id, kw.lower(), resp))
    conn.commit()
    await ctx.send(f"⚡ Added trigger `{kw.lower()}`!")

@trigger.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def trigger_remove(ctx, *, keyword: str):
    c.execute("DELETE FROM triggers WHERE guild_id = ? AND keyword = ?", (ctx.guild.id, keyword.lower()))
    conn.commit()
    await ctx.send(f"🗑️ Removed trigger `{keyword.lower()}`.")

@trigger.command(name="list")
async def trigger_list(ctx):
    c.execute("SELECT keyword FROM triggers WHERE guild_id = ?", (ctx.guild.id,))
    rows = c.fetchall()
    await ctx.send("⚡ **Triggers:** " + (", ".join([f"`{r[0]}`" for r in rows]) if rows else "None"))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def addrolebutton(ctx, role: discord.Role, *, label: str):
    c.execute("INSERT OR REPLACE INTO button_roles VALUES (?, ?, ?)", (ctx.guild.id, role.id, label))
    conn.commit()
    await ctx.send(f"✅ Added button **{label}** for **{role.name}**.")

@bot.command()
@commands.has_permissions(manage_roles=True)
async def rolepanel(ctx, *, title="Self-Assignable Roles"):
    c.execute("SELECT role_id, label FROM button_roles WHERE guild_id = ?", (ctx.guild.id,))
    rows = c.fetchall()
    if not rows:
        return await ctx.send("No buttons configured!")
    await ctx.send(embed=discord.Embed(title=title, description="Click below to toggle roles:", color=discord.Color.blurple()), view=RolePanelView(rows))

@bot.command()
@commands.has_permissions(manage_roles=True)
async def reactionrole(ctx, message_id: int, emoji: str, role: discord.Role):
    msg = await ctx.channel.fetch_message(message_id)
    await msg.add_reaction(emoji)
    c.execute("INSERT OR REPLACE INTO emoji_roles VALUES (?, ?, ?, ?)", (ctx.guild.id, message_id, emoji, role.id))
    conn.commit()
    await ctx.send(f"✅ Bound {emoji} to **{role.name}**!", delete_after=5)

@bot.group(invoke_without_command=True)
async def tag(ctx, *, name: str):
    c.execute("SELECT content FROM tags WHERE guild_id = ? AND name = ?", (ctx.guild.id, name.lower()))
    row = c.fetchone()
    if not row:
        return await ctx.send("❌ Tag not found.")
    c.execute("UPDATE tags SET uses = uses + 1 WHERE guild_id = ? AND name = ?", (ctx.guild.id, name.lower()))
    conn.commit()
    await ctx.send(format_vars(row[0], member=ctx.author, guild=ctx.guild))

@tag.command(name="create")
async def tag_create(ctx, name: str, *, content: str):
    c.execute("INSERT OR REPLACE INTO tags VALUES (?, ?, ?, ?, 0)", (ctx.guild.id, name.lower(), content, ctx.author.id))
    conn.commit()
    await ctx.send(f"✅ Tag `{name.lower()}` saved!")

@tag.command(name="delete")
async def tag_delete(ctx, name: str):
    c.execute("DELETE FROM tags WHERE guild_id = ? AND name = ?", (ctx.guild.id, name.lower()))
    conn.commit()
    await ctx.send(f"🗑️ Tag `{name.lower()}` deleted.")

@tag.command(name="list")
async def tag_list(ctx):
    c.execute("SELECT name, uses FROM tags WHERE guild_id = ?", (ctx.guild.id,))
    rows = c.fetchall()
    await ctx.send("🏷️ **Tags:** " + (", ".join([f"`{r[0]}` ({r[1]})" for r in rows]) if rows else "None"))

@bot.command()
@commands.has_permissions(manage_messages=True)
async def sticky(ctx, *, content: str):
    if content.lower() == "off":
        update_config(ctx.guild.id, sticky_channel=None, sticky_content=None)
        return await ctx.send("📌 Sticky disabled.")
    msg = await ctx.send(f"📌 **Sticky Message:**\n{content}")
    update_config(ctx.guild.id, sticky_channel=ctx.channel.id, sticky_content=content, sticky_last_msg=msg.id)

@bot.command()
async def snipe(ctx):
    data = sniped_messages.get(ctx.channel.id)
    if not data:
        return await ctx.send("Nothing to snipe!")
    em = discord.Embed(description=data["content"], color=discord.Color.red(), timestamp=data["time"])
    em.set_author(name=data["author"], icon_url=data["avatar"])
    await ctx.send(embed=em)

@bot.command()
async def editsnipe(ctx):
    data = edited_messages.get(ctx.channel.id)
    if not data:
        return await ctx.send("Nothing to editsnipe!")
    em = discord.Embed(color=discord.Color.gold(), timestamp=data["time"])
    em.set_author(name=data["author"], icon_url=data["avatar"])
    em.add_field(name="Before", value=data["before"], inline=False)
    em.add_field(name="After", value=data["after"], inline=False)
    await ctx.send(embed=em)

@bot.command()
async def poll(ctx, question: str, *options):
    if len(options) < 2 or len(options) > 9:
        return await ctx.send('Usage: `!poll "Question?" "Opt 1" "Opt 2"`')
    emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
    msg = await ctx.send(embed=discord.Embed(title=f"📊 {question}", description="\n".join([f"{emojis[i]} {o}" for i, o in enumerate(options)]), color=discord.Color.blurple()))
    for i in range(len(options)):
        await msg.add_reaction(emojis[i])

@bot.command()
@commands.has_permissions(manage_guild=True)
async def giveaway(ctx, duration: str, *, prize: str):
    secs = parse_duration(duration)
    if secs <= 0:
        return await ctx.send("Invalid duration!")
    msg = await ctx.send(embed=discord.Embed(title="🎉 GIVEAWAY 🎉", description=f"**Prize:** {prize}\nReact with 🎉!\nEnds <t:{int(time.time()) + secs}:R>", color=discord.Color.magenta()))
    await msg.add_reaction("🎉")
    await asyncio.sleep(secs)
    msg = await ctx.channel.fetch_message(msg.id)
    users = [u async for u in msg.reactions[0].users() if not u.bot]
    if users:
        await ctx.send(f"🎊 Congratulations {random.choice(users).mention}! You won **{prize}**!")

@bot.command()
async def remind(ctx, duration: str, *, text: str):
    secs = parse_duration(duration)
    if secs <= 0:
        return await ctx.send("Invalid duration!")
    await ctx.send(f"⏰ Reminder set for **{duration}**.")
    await asyncio.sleep(secs)
    await ctx.send(f"⏰ {ctx.author.mention}, Reminder: **{text}**")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def embed(ctx, title: str, *, description: str):
    await ctx.send(embed=discord.Embed(title=title, description=description, color=discord.Color.blurple()))
    await ctx.message.delete()

@bot.command()
@commands.has_permissions(manage_messages=True)
async def announce(ctx, channel: discord.TextChannel, *, message: str):
    await channel.send(embed=discord.Embed(title="📢 Announcement", description=message, color=discord.Color.gold()))
    await ctx.send(f"✅ Sent to {channel.mention}!", delete_after=4)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def schedule(ctx, delay: str, channel: discord.TextChannel, *, message: str):
    secs = parse_duration(delay)
    if secs <= 0:
        return await ctx.send("Invalid delay!")
    await ctx.send(f"🗓️ Scheduled in **{delay}**.")
    await asyncio.sleep(secs)
    await channel.send(message)

@bot.command()
async def afk(ctx, *, reason="AFK"):
    afk_users[(ctx.guild.id, ctx.author.id)] = (reason, time.time())
    await ctx.send(f"💤 {ctx.author.mention} is now AFK: **{reason}**")

@bot.command()
async def ping(ctx):
    await ctx.send(f"🏓 Pong! **{round(bot.latency * 1000)}ms**")

@bot.command(aliases=["av"])
async def avatar(ctx, member: discord.Member = None):
    m = member or ctx.author
    em = discord.Embed(title=f"{m.display_name}'s Avatar", color=m.color)
    em.set_image(url=m.display_avatar.url)
    await ctx.send(embed=em)

@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    em = discord.Embed(title=g.name, color=discord.Color.blurple())
    if g.icon:
        em.set_thumbnail(url=g.icon.url)
    em.add_field(name="Owner", value=str(g.owner))
    em.add_field(name="Members", value=str(g.member_count))
    em.add_field(name="Channels", value=str(len(g.channels)))
    await ctx.send(embed=em)

@bot.command()
async def userinfo(ctx, member: discord.Member = None):
    m = member or ctx.author
    em = discord.Embed(title=str(m), color=m.color)
    em.set_thumbnail(url=m.display_avatar.url)
    em.add_field(name="ID", value=str(m.id))
    em.add_field(name="Joined", value=f"<t:{int(m.joined_at.timestamp())}:R>")
    await ctx.send(embed=em)

@bot.command()
async def roleinfo(ctx, role: discord.Role):
    em = discord.Embed(title=f"Role: {role.name}", color=role.color)
    em.add_field(name="ID", value=str(role.id))
    em.add_field(name="Members", value=str(len(role.members)))
    await ctx.send(embed=em)

# ==========================================
# 10. ECONOMY & SUSPENSE CASINO (#casino ONLY)
# ==========================================
@bot.command(aliases=["bal"])
async def balance(ctx, member: discord.Member = None):
    m = member or ctx.author
    ud = get_user_data(ctx.guild.id, m.id)
    em = discord.Embed(title=f"💰 {m.display_name}'s Finances", color=discord.Color.green())
    em.set_thumbnail(url=m.display_avatar.url)
    em.add_field(name="💵 Wallet", value=f"**${ud['balance']:,}**", inline=True)
    em.add_field(name="🏦 Bank", value=f"**${ud['bank']:,}**", inline=True)
    em.add_field(name="🎯 Bounty", value=f"**${ud['bounty']:,}**", inline=True)
    em.set_footer(text=f"Net Worth: ${ud['balance'] + ud['bank']:,}")
    await ctx.send(embed=em)

@bot.command()
async def daily(ctx):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    now = int(time.time())
    if now - ud["last_daily"] < 86400:
        rem = 86400 - (now - ud["last_daily"])
        return await ctx.send(f"⏳ Come back in **{rem // 3600}h {(rem % 3600) // 60}m**!")
    reward = random.randint(500, 1000)
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + reward, last_daily=now)
    await ctx.send(embed=discord.Embed(title="💵 Daily Reward Claimed!", description=f"Collected **+${reward:,}**!\nNew Wallet: **${ud['balance'] + reward:,}**", color=discord.Color.green()))

@bot.command()
async def work(ctx):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    now = int(time.time())
    if now - ud["last_work"] < 1800:
        rem = 1800 - (now - ud["last_work"])
        return await ctx.send(f"⏳ Work again in **{rem // 60}m {rem % 60}s**.")
    earned = random.randint(150, 450)
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + earned, last_work=now)
    await ctx.send(embed=discord.Embed(title="💼 Shift Complete", description=f"You {random.choice(['coded a Discord bot', 'fixed a server outage', 'worked at the casino'])} and earned **+${earned:,}**!", color=discord.Color.green()))

@bot.command()
async def crime(ctx):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    now = int(time.time())
    if now - ud["last_crime"] < 3600:
        rem = 3600 - (now - ud["last_crime"])
        return await ctx.send(f"⏳ Lay low for **{rem // 60}m {rem % 60}s**.")
    msg = await ctx.send(embed=discord.Embed(title="🕵️ Breaking into the Vault...", description="Picking the lock... stay quiet...", color=discord.Color.dark_grey()))
    await asyncio.sleep(1.5)

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    c.execute("SELECT quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND item_name = 'Lucky Coin'", (ctx.guild.id, ctx.author.id))
    has_coin = (c.fetchone() or [0])[0] > 0
    won = True if rig_mode == "win" else (False if rig_mode == "lose" else (random.random() < (0.60 if has_coin else 0.45)))

    if won:
        loot = random.randint(400, 1200)
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + loot, last_crime=now)
        await msg.edit(embed=discord.Embed(title="💰 Heist Succeeded!", description=f"Escaped with **+${loot:,}**!", color=discord.Color.green()))
    else:
        fine = min(ud["balance"], random.randint(200, 500))
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - fine, last_crime=now)
        await msg.edit(embed=discord.Embed(title="🚓 BUSTED!", description=f"Paid a **-${fine:,}** fine.", color=discord.Color.red()))

@bot.command(aliases=["dep"])
async def deposit(ctx, amount: str):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(amount, ud["balance"])
    if amt <= 0 or amt > ud["balance"]:
        return await ctx.send("❌ Invalid deposit amount.")
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - amt, bank=ud["bank"] + amt)
    await ctx.send(f"🏦 Deposited **${amt:,}** into your bank.")

@bot.command(aliases=["with"])
async def withdraw(ctx, amount: str):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(amount, ud["bank"])
    if amt <= 0 or amt > ud["bank"]:
        return await ctx.send("❌ Invalid withdrawal amount.")
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + amt, bank=ud["bank"] - amt)
    await ctx.send(f"💵 Withdrew **${amt:,}** to your wallet.")

@bot.command(aliases=["pay"])
async def give(ctx, member: discord.Member, amount: int):
    if member.id == ctx.author.id or amount <= 0:
        return await ctx.send("❌ Invalid transfer.")
    sender = get_user_data(ctx.guild.id, ctx.author.id)
    if sender["balance"] < amount:
        return await ctx.send("❌ Insufficient funds.")
    receiver = get_user_data(ctx.guild.id, member.id)
    update_user_data(ctx.guild.id, ctx.author.id, balance=sender["balance"] - amount)
    update_user_data(ctx.guild.id, member.id, balance=receiver["balance"] + amount)
    await ctx.send(f"💸 Sent **${amount:,}** to **{member.display_name}**!")

@bot.command()
async def slots(ctx, bet: str):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(bet, ud["balance"])
    if amt <= 0 or amt > ud["balance"]:
        return await ctx.send("❌ Invalid bet amount. Usage: `!slots <amount|all|half>`")

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    emojis = ["🍒", "🍋", "🍇", "🔔", "💎", "7️⃣"]
    if rig_mode == "win":
        r = ["7️⃣", "7️⃣", "7️⃣"]
    elif rig_mode == "lose":
        r = ["🍒", "🍋", "🍇"]
    else:
        r = [random.choice(emojis) for _ in range(3)]

    def slot_embed(disp, status, color=discord.Color.gold()):
        em = discord.Embed(title="🎰 HIGH-ROLLER SLOTS", color=color)
        em.add_field(name="━━━━━━ REELS ━━━━━━", value=f"## ➔  | {disp[0]} | {disp[1]} | {disp[2]} |  ⬅️", inline=False)
        em.add_field(name="Status", value=status, inline=False)
        em.set_footer(text=f"Player: {ctx.author.display_name} • Bet: ${amt:,}")
        return em

    msg = await ctx.send(embed=slot_embed(["❓", "❓", "❓"], "🔄 **Spinning all reels...**", discord.Color.blurple()))
    await asyncio.sleep(0.9)
    await msg.edit(embed=slot_embed([r[0], "❓", "❓"], "🔒 **Reel 1 locked! Two left...**", discord.Color.blurple()))
    await asyncio.sleep(0.9)
    await msg.edit(embed=slot_embed([r[0], r[1], "❓"], "🔥 **Reel 2 locked! Holding breath for Reel 3...**", discord.Color.orange()))
    await asyncio.sleep(1.1)

    if r[0] == r[1] == r[2]:
        mult = 10 if r[0] == "7️⃣" else 5
        win = amt * mult
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + win)
        await msg.edit(embed=slot_embed(r, f"🎉 **MEGA JACKPOT ({mult}x)! You won +${win:,}!**\nNew Wallet: **${ud['balance'] + win:,}**", discord.Color.gold()))
    elif r[0] == r[1] or r[1] == r[2] or r[0] == r[2]:
        profit = int(amt * 1.5) - amt
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + profit)
        await msg.edit(embed=slot_embed(r, f"✨ **DOUBLE MATCH! You won +${profit:,}!**\nNew Wallet: **${ud['balance'] + profit:,}**", discord.Color.green()))
    else:
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - amt)
        await msg.edit(embed=slot_embed(r, f"💀 **NO MATCH! You lost -${amt:,}.**\nNew Wallet: **${ud['balance'] - amt:,}**", discord.Color.red()))

@bot.command(aliases=["cf"])
async def coinflip(ctx, bet: str, side: str = "heads"):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(bet, ud["balance"])
    side = side.lower()
    if side not in ("heads", "tails", "h", "t"):
        return await ctx.send("Choose `heads` or `tails`! Example: `!cf 200 heads`")
    if amt <= 0 or amt > ud["balance"]:
        return await ctx.send("❌ Invalid bet amount. Example: `!cf 200 heads`")
    choice = "heads" if side in ("heads", "h") else "tails"
    other = "tails" if choice == "heads" else "heads"

    msg = await ctx.send(embed=discord.Embed(title="🪙 Coin Flip", description=f"Wager: **${amt:,}** on **{choice.upper()}**\n\n🔄 *Tossing the coin high into the air...*", color=discord.Color.blurple()))
    await asyncio.sleep(1.0)
    await msg.edit(embed=discord.Embed(title="🪙 Coin Flip", description=f"Wager: **${amt:,}** on **{choice.upper()}**\n\n💫 *Spinning fast... about to hit the table...*", color=discord.Color.orange()))
    await asyncio.sleep(1.1)

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    result = choice if rig_mode == "win" else (other if rig_mode == "lose" else random.choice(["heads", "tails"]))

    if choice == result:
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + amt)
        em3 = discord.Embed(title=f"🎉 IT LANDED ON {result.upper()}!", description=f"You called **{choice.upper()}** and won **+${amt:,}**!\nNew Wallet: **${ud['balance'] + amt:,}**", color=discord.Color.green())
    else:
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - amt)
        em3 = discord.Embed(title=f"💀 IT LANDED ON {result.upper()}!", description=f"You called **{choice.upper()}** and lost **-${amt:,}**.\nNew Wallet: **${ud['balance'] - amt:,}**", color=discord.Color.red())
    await msg.edit(embed=em3)

@bot.command()
async def roulette(ctx, bet: str, color: str):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(bet, ud["balance"])
    color = color.lower()
    if color not in ("red", "black", "green", "r", "b", "g"):
        return await ctx.send("Choose `red`, `black`, or `green`! Example: `!roulette 200 red`")
    if amt <= 0 or amt > ud["balance"]:
        return await ctx.send("❌ Invalid bet amount. Example: `!roulette 200 red`")

    color_map = {"r": "red", "red": "red", "b": "black", "black": "black", "g": "green", "green": "green"}
    chosen = color_map[color]

    msg = await ctx.send(embed=discord.Embed(title="🎡 Casino Roulette", description=f"Betting **${amt:,}** on **{chosen.upper()}**\n\n🔄 `🔴 ➔ ⚫ ➔ 🔴 ➔ ⚫ ➔ 🟢` *Wheel spinning fast...*", color=discord.Color.blurple()))
    await asyncio.sleep(1.1)
    await msg.edit(embed=discord.Embed(title="🎡 Casino Roulette", description=f"Betting **${amt:,}** on **{chosen.upper()}**\n\n⏳ `⚫ ➔ 🔴 ➔ 🟢 ➔ ⚫` *Ball slowing down across the pockets...*", color=discord.Color.orange()))
    await asyncio.sleep(1.2)

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    if rig_mode == "win":
        roll = chosen
    elif rig_mode == "lose":
        roll = "black" if chosen != "black" else "red"
    else:
        roll = random.choices(["red", "black", "green"], weights=[48, 48, 4])[0]

    ball_icon = "🔴" if roll == "red" else ("⚫" if roll == "black" else "🟢")
    if chosen == roll:
        mult = 14 if roll == "green" else 2
        profit = (amt * mult) - amt
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + profit)
        em3 = discord.Embed(title=f"🎉 {ball_icon} LANDED ON {roll.upper()}!", description=f"Multiplier: **{mult}x** — You won **+${profit:,}**!\nNew Wallet: **${ud['balance'] + profit:,}**", color=discord.Color.green())
    else:
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - amt)
        em3 = discord.Embed(title=f"💀 {ball_icon} LANDED ON {roll.upper()}!", description=f"You bet on **{chosen.upper()}** and lost **-${amt:,}**.\nNew Wallet: **${ud['balance'] - amt:,}**", color=discord.Color.red())
    await msg.edit(embed=em3)

@bot.command(aliases=["bj"])
async def blackjack(ctx, bet: str):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    amt = parse_bet(bet, ud["balance"])
    if amt <= 0 or amt > ud["balance"]:
        return await ctx.send("❌ Invalid bet amount. Example: `!bj 200`")

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    view = BlackjackView(ctx, amt, rig_mode=rig_mode)
    if view.hand_score(view.player) == 21:
        payout = int(amt * 1.5)
        update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + payout)
        return await ctx.send(embed=view.build_embed(f"🃏 **NATURAL BLACKJACK! (21)**\nInstant 2.5x payout: **+${payout:,}**!\nNew Wallet: **${ud['balance'] + payout:,}**", discord.Color.gold(), reveal_dealer=True))

    view.message = await ctx.send(embed=view.build_embed("Click **Hit**, **Stand**, or **Double Down** below!", discord.Color.blurple(), reveal_dealer=False), view=view)

@bot.command()
async def rob(ctx, target: discord.Member):
    if target.id == ctx.author.id:
        return await ctx.send("You can't rob yourself.")
    robber = get_user_data(ctx.guild.id, ctx.author.id)
    victim = get_user_data(ctx.guild.id, target.id)
    if victim["balance"] < 200 and victim["bounty"] <= 0:
        return await ctx.send("They don't have enough cash or bounty worth robbing!")

    msg = await ctx.send(embed=discord.Embed(title="🦹 Sneaking up on target...", description=f"Reaching for **{target.display_name}**'s wallet...", color=discord.Color.dark_grey()))
    await asyncio.sleep(1.3)

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    c.execute("SELECT quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND item_name = 'Padlock'", (ctx.guild.id, target.id))
    lock_row = c.fetchone()
    if lock_row and lock_row[0] > 0 and rig_mode != "win":
        c.execute("UPDATE inventory SET quantity = quantity - 1 WHERE guild_id = ? AND user_id = ? AND item_name = 'Padlock'", (ctx.guild.id, target.id))
        conn.commit()
        return await msg.edit(embed=discord.Embed(title="🔒 PADLOCK TRIGGERED!", description=f"**{target.display_name}** had a **Padlock**! Your robbery failed.", color=discord.Color.orange()))

    c.execute("SELECT quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND item_name = 'Lucky Coin'", (ctx.guild.id, ctx.author.id))
    has_coin = (c.fetchone() or [0])[0] > 0
    won = True if rig_mode == "win" else (False if rig_mode == "lose" else (random.random() < (0.55 if has_coin else 0.45)))

    if won:
        stolen = random.randint(50, max(50, int(victim["balance"] * 0.4))) if victim["balance"] >= 200 else 0
        bounty_payout = victim["bounty"]
        update_user_data(ctx.guild.id, ctx.author.id, balance=robber["balance"] + stolen + bounty_payout)
        update_user_data(ctx.guild.id, target.id, balance=victim["balance"] - stolen, bounty=0)
        bounty_txt = f"\n🎯 **Bounty Claimed:** +${bounty_payout:,}" if bounty_payout > 0 else ""
        await msg.edit(embed=discord.Embed(title="🦹 ROBBERY SUCCESSFUL!", description=f"You stole **${stolen:,}** from **{target.display_name}**!{bounty_txt}", color=discord.Color.green()))
    else:
        fine = min(robber["balance"], 250)
        update_user_data(ctx.guild.id, ctx.author.id, balance=robber["balance"] - fine)
        await msg.edit(embed=discord.Embed(title="🚓 CAUGHT RED-HANDED!", description=f"You got caught and paid a **-${fine:,}** fine!", color=discord.Color.red()))

@bot.command()
async def bounty(ctx, target: discord.Member, amount: int):
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    if amount <= 0 or ud["balance"] < amount:
        return await ctx.send("❌ Insufficient wallet funds.")
    td = get_user_data(ctx.guild.id, target.id)
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - amount)
    update_user_data(ctx.guild.id, target.id, bounty=td["bounty"] + amount)
    await ctx.send(f"🎯 Placed a **${amount:,}** bounty on **{target.display_name}**!")

@bot.command()
async def shop(ctx):
    em = discord.Embed(title="🛒 Server Shop", color=discord.Color.gold())
    for name, info in SHOP_ITEMS.items():
        em.add_field(name=f"{name} — ${info['price']:,}", value=info["desc"], inline=False)
    em.set_footer(text="Buy an item with !buy <item name>")
    await ctx.send(embed=em)

@bot.command()
async def buy(ctx, *, item_name: str):
    matched = next((k for k in SHOP_ITEMS if k.lower() == item_name.lower()), None)
    if not matched:
        return await ctx.send("❌ Item not found in shop.")
    price = SHOP_ITEMS[matched]["price"]
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    if ud["balance"] < price:
        return await ctx.send("❌ Not enough wallet cash.")
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] - price)
    c.execute("INSERT INTO inventory VALUES (?, ?, ?, 1) ON CONFLICT(guild_id, user_id, item_name) DO UPDATE SET quantity = quantity + 1", (ctx.guild.id, ctx.author.id, matched))
    conn.commit()
    await ctx.send(f"🛍️ Purchased **{matched}** for **${price:,}**!")

@bot.command(aliases=["inv"])
async def inventory(ctx):
    c.execute("SELECT item_name, quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND quantity > 0", (ctx.guild.id, ctx.author.id))
    rows = c.fetchall()
    if not rows:
        return await ctx.send("🎒 Your inventory is empty.")
    await ctx.send(embed=discord.Embed(title=f"🎒 {ctx.author.display_name}'s Inventory", description="\n".join([f"• **{r[0]}** x{r[1]}" for r in rows]), color=discord.Color.blurple()))

@bot.command()
async def openbox(ctx):
    c.execute("SELECT quantity FROM inventory WHERE guild_id = ? AND user_id = ? AND item_name = 'Mystery Box'", (ctx.guild.id, ctx.author.id))
    row = c.fetchone()
    if not row or row[0] <= 0:
        return await ctx.send("📦 You don't have any Mystery Boxes! Buy one from `!shop`.")
    c.execute("UPDATE inventory SET quantity = quantity - 1 WHERE guild_id = ? AND user_id = ? AND item_name = 'Mystery Box'", (ctx.guild.id, ctx.author.id))
    conn.commit()

    msg = await ctx.send(embed=discord.Embed(title="📦 Opening Mystery Box...", description="🔄 *Shaking the box... something heavy is inside...*", color=discord.Color.blurple()))
    await asyncio.sleep(1.1)
    await msg.edit(embed=discord.Embed(title="✨ Breaking the Seal...", description="💫 *Golden light glowing through the cracks...*", color=discord.Color.orange()))
    await asyncio.sleep(1.1)

    rig_mode = consume_rig(ctx.guild.id, ctx.author.id)
    prize = 50000 if rig_mode == "win" else (100 if rig_mode == "lose" else random.randint(500, 4000))
    ud = get_user_data(ctx.guild.id, ctx.author.id)
    update_user_data(ctx.guild.id, ctx.author.id, balance=ud["balance"] + prize)
    await msg.edit(embed=discord.Embed(title="🎁 MYSTERY BOX OPENED!", description=f"You found **+${prize:,}** inside!\nNew Wallet: **${ud['balance'] + prize:,}**", color=discord.Color.gold()))

@bot.command(aliases=["lb"])
async def leaderboard(ctx):
    c.execute("SELECT user_id, (balance + bank) as net FROM users WHERE guild_id = ? ORDER BY net DESC LIMIT 10", (ctx.guild.id,))
    rows = c.fetchall()
    desc = "\n".join([f"**#{i+1}** <@{r[0]}> — **${r[1]:,}**" for i, r in enumerate(rows)])
    await ctx.send(embed=discord.Embed(title="🏆 Richest Players", description=desc or "No data yet.", color=discord.Color.gold()))

@bot.command(aliases=["level"])
async def rank(ctx, member: discord.Member = None):
    m = member or ctx.author
    ud = get_user_data(ctx.guild.id, m.id)
    await ctx.send(f"📊 **{m.display_name}** is **Level {ud['level']}** ({ud['xp']}/{ud['level']*150} XP)")

# ==========================================
# 11. HELP COMMAND (Role-Based UI)
# ==========================================
@bot.command()
async def help(ctx):
    if is_bot_owner(ctx.author):
        em = discord.Embed(
            title="👑 Owner & Admin Command Directory",
            description="Full server control, owner cheats, moderation, and casino commands.",
            color=discord.Color.gold()
        )
        em.add_field(
            name="⚡ Owner Commands (KRATOS & JANE ONLY)",
            value="`!infmoney [@user] [amount]`, `!rig <win|always|lose|off> [@user]`",
            inline=False
        )
        em.add_field(
            name="🚨 Lockdown & Moderation",
            value="`!lockdown`, `!unlockdown`, `!lockdownignore`, `!kick`, `!ban`, `!tempban`, `!unban`, `!mute`, `!unmute`, `!warn`, `!warnings`, `!clearwarns`, `!note`, `!notes`, `!purge`, `!autopurge`, `!slowmode`, `!lock`, `!unlock`, `!nuke`, `!nick`, `!role`, `!massrole`, `!vcmove`, `!automod`, `!badword`",
            inline=False
        )
        em.add_field(
            name="⚙️ Utilities, Panels & Automation",
            value="`!setup`, `!trigger`, `!addrolebutton`, `!rolepanel`, `!reactionrole`, `!tag`, `!sticky`, `!snipe`, `!editsnipe`, `!poll`, `!giveaway`, `!remind`, `!embed`, `!announce`, `!schedule`, `!afk`, `!ping`, `!avatar`, `!serverinfo`, `!userinfo`, `!roleinfo`",
            inline=False
        )
        em.add_field(
            name="🎰 Economy & Casino (#casino only)",
            value="`!balance`, `!daily`, `!work`, `!crime`, `!deposit`, `!withdraw`, `!give`, `!slots`, `!coinflip`, `!roulette`, `!blackjack`, `!rob`, `!bounty`, `!shop`, `!buy`, `!inventory`, `!openbox`, `!leaderboard`, `!rank`",
            inline=False
        )
        em.set_footer(text="Visible only to KRATOS THE OWNER & JANE THE OWNER")
    else:
        em = discord.Embed(
            title="🎰 Casino & Player Commands",
            description="Use all economy and gambling commands inside **#casino**!",
            color=discord.Color.blurple()
        )
        em.add_field(
            name="💵 Money & Banking",
            value="`!balance` (`!bal`), `!daily`, `!work`, `!crime`, `!deposit` (`!dep`), `!withdraw` (`!with`), `!give` (`!pay`)",
            inline=False
        )
        em.add_field(
            name="🎲 Casino Games",
            value="`!slots <bet>`, `!coinflip <bet> <heads|tails>`, `!roulette <bet> <red|black|green>`, `!blackjack <bet>`",
            inline=False
        )
        em.add_field(
            name="🛒 Shop, PvP & Progression",
            value="`!shop`, `!buy <item>`, `!inventory` (`!inv`), `!openbox`, `!rob @user`, `!bounty @user <amount>`, `!leaderboard` (`!lb`), `!rank`",
            inline=False
        )
        em.add_field(
            name="🛠️ General Utilities",
            value="`!afk`, `!remind`, `!poll`, `!tag`, `!snipe`, `!editsnipe`, `!avatar`, `!userinfo`, `!serverinfo`, `!ping`",
            inline=False
        )
    await ctx.send(embed=em)

# ==========================================
# 12. START BOT
# ==========================================
if __name__ == "__main__":
    keep_alive()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("ERROR: DISCORD_TOKEN environment variable not set!")
    else:
        bot.run(token)
