import os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Handler).serve_forever(), daemon=True).start()

import discord 
from discord.ext import commands, tasks
import sqlite3
import random
import asyncio

# 1. Setup Discord Intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Secret tracking sets
rigged_users = set()
infinite_money_users = set()

# Tracks who has stolen from whom: {victim_id: {thief_id}}
ROBBERY_VICTIMS = {}

# AUTO-MEMORY BACKUP: Saves player data in code memory so it survives DB wipes/code updates!
USER_MEMORY_CACHE = {}

# Helper function for Owner / Co-Owner / Admin (used for infmoney and reseteco)
function_is_privileged = lambda ctx: (
    ctx.author.guild_permissions.administrator or 
    any(r.name.lower() in ["owner", "co-owner"] for r in ctx.author.roles)
)

# Helper function specifically for the "jackpott" role (used for !rig)
def has_jackpott_role(ctx):
    return any(r.name.lower() == "jackpott" for r in ctx.author.roles)

# 2. Setup SQLite Database & Tables
conn = sqlite3.connect("economy.db")
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id TEXT PRIMARY KEY,
        wallet INTEGER DEFAULT 0,
        bank INTEGER DEFAULT 0
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
        user_id TEXT,
        item_name TEXT,
        quantity INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, item_name)
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS bounties (
        target_id TEXT PRIMARY KEY,
        issuer_id TEXT,
        amount INTEGER
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS market (
        listing_id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id TEXT,
        item_name TEXT,
        price INTEGER
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_levels (
        user_id TEXT PRIMARY KEY,
        xp INTEGER DEFAULT 0,
        level INTEGER DEFAULT 1
    )
""")
conn.commit()

# Database Helper Functions with Auto-Memory Sync
def get_user_data(user_id):
    uid_str = str(user_id)
    cursor.execute("SELECT wallet, bank FROM users WHERE user_id = ?", (uid_str,))
    result = cursor.fetchone()
    
    if result is None:
        if uid_str in USER_MEMORY_CACHE:
            w, b = USER_MEMORY_CACHE[uid_str]["wallet"], USER_MEMORY_CACHE[uid_str]["bank"]
        else:
            w, b = 0, 0
            
        cursor.execute("INSERT INTO users (user_id, wallet, bank) VALUES (?, ?, ?)", (uid_str, w, b))
        conn.commit()
        return {"wallet": w, "bank": b}
    
    USER_MEMORY_CACHE[uid_str] = {"wallet": result[0], "bank": result[1]}
    return {"wallet": result[0], "bank": result[1]}

def get_balance(user_id):
    if user_id in infinite_money_users:
        return 999999999999999  
    return get_user_data(user_id)["wallet"]

def update_balance(user_id, amount):
    if user_id in infinite_money_users:
        return  
    
    uid_str = str(user_id)
    udata = get_user_data(uid_str)
    new_wallet = max(0, udata["wallet"] + amount)
    cursor.execute("UPDATE users SET wallet = ? WHERE user_id = ?", (new_wallet, uid_str))
    conn.commit()
    USER_MEMORY_CACHE[uid_str]["wallet"] = new_wallet

def get_item_qty(user_id, item_name):
    cursor.execute("SELECT quantity FROM inventory WHERE user_id = ? AND item_name = ?", (str(user_id), item_name))
    res = cursor.fetchone()
    return res[0] if res else 0

def add_item_qty(user_id, item_name, qty):
    current = get_item_qty(user_id, item_name)
    if current == 0 and qty > 0:
        cursor.execute("INSERT INTO inventory (user_id, item_name, quantity) VALUES (?, ?, ?)", (str(user_id), item_name, qty))
    else:
        new_qty = max(0, current + qty)
        cursor.execute("UPDATE inventory SET quantity = ? WHERE user_id = ? AND item_name = ?", (new_qty, str(user_id), item_name))
    conn.commit()

def consume_lucky_dice_boost(user_id):
    qty = get_item_qty(user_id, "lucky_dice")
    if qty > 0:
        add_item_qty(user_id, "lucky_dice", -1)
        return 0.15 # 15% luck boost
    return 0.0

def add_user_xp(user_id, amount):
    uid_str = str(user_id)
    cursor.execute("SELECT xp, level FROM user_levels WHERE user_id = ?", (uid_str,))
    res = cursor.fetchone()
    if not res:
        xp, level = 0, 1
        cursor.execute("INSERT INTO user_levels (user_id, xp, level) VALUES (?, ?, ?)", (uid_str, xp, level))
    else:
        xp, level = res
    
    xp += amount
    next_level_xp = level * 100
    leveled_up = False
    while xp >= next_level_xp:
        xp -= next_level_xp
        level += 1
        leveled_up = True
        next_level_xp = level * 100
        # Rocket Pass Tier Reward: $5,000 + 1 Free Crate Key!
        update_balance(user_id, 5000)
        add_item_qty(user_id, "crate_key", 1)
    
    cursor.execute("UPDATE user_levels SET xp = ?, level = ? WHERE user_id = ?", (xp, level, uid_str))
    conn.commit()
    return leveled_up, level

# Shop Catalog Items (with asset images)
SHOP_ITEMS = {
    "padlock": {"name": "Padlock", "price": 15000, "desc": "Protects your wallet from being stolen by other players.", "emoji": "🔒", "img": "https://i.imgur.com/K3y7RmW.png"},
    "camera": {"name": "Security Camera", "price": 35000, "desc": "Grants a +40% luck boost during robberies.", "emoji": "📷", "img": "https://i.imgur.com/8Qz4Y7z.png"},
    "cooldown_drink": {"name": "Energy Drink", "price": 10000, "desc": "Cuts your action cooldown timers down by 30%.", "emoji": "⚡", "img": "https://i.imgur.com/8xL1OaP.png"},
    "lottery_ticket": {"name": "Lottery Ticket", "price": 5000, "desc": "1% chance to win the $7,000,000 jackpot via !scratch!", "emoji": "🎟️", "img": "https://i.imgur.com/vH4S7iG.png"},
    "lucky_dice": {"name": "Lucky Dice", "price": 5000, "desc": "Consumable: Gives +15% win rate boost on your next gamble.", "emoji": "🎲", "img": "https://i.imgur.com/0Q4XqZy.png"},
    "crate_key": {"name": "Champion Crate Key", "price": 10000, "desc": "Opens a Champion Crate to drop Rocket League items!", "emoji": "🔑", "img": "https://i.imgur.com/mZ6P7xR.png"}
}

# Rocket League Crate Drops Catalog with Images
CRATE_DROPS = {
    "Zomba Wheels": {"rarity": "Exotic", "value": 25000, "emoji": "🛞", "img": "https://rocket-league.com/content/media/items/avatar/220px/wheels_zomba_hero.png"},
    "Octane Body": {"rarity": "Import", "value": 50000, "emoji": "🏎️", "img": "https://rocket-league.com/content/media/items/avatar/220px/body_octane_hero.png"},
    "Fennec Body": {"rarity": "Import", "value": 75000, "emoji": "🏎️", "img": "https://rocket-league.com/content/media/items/avatar/220px/body_fennec_hero.png"},
    "Dissolver Decal": {"rarity": "Black Market", "value": 150000, "emoji": "🔥", "img": "https://rocket-league.com/content/media/items/avatar/220px/decal_dissolver_hero.png"},
    "Dueling Dragons": {"rarity": "Black Market", "value": 300000, "emoji": "🐉", "img": "https://rocket-league.com/content/media/items/avatar/220px/explosion_duelingdragons_hero.png"},
    "Titanium White Octane": {"rarity": "Exotic Legend", "value": 1000000, "emoji": "👑", "img": "https://rocket-league.com/content/media/items/avatar/220px/body_octane_white_hero.png"}
}

# Background Task: Hourly Bank Interest (1% hourly passive income)
@tasks.loop(hours=1)
async def bank_interest():
    cursor.execute("SELECT user_id, bank FROM users WHERE bank > 0")
    rows = cursor.fetchall()
    for uid, bank_amt in rows:
        if int(uid) in infinite_money_users:
            continue
        interest = int(bank_amt * 0.01)
        if interest > 0:
            cursor.execute("UPDATE users SET bank = bank + ? WHERE user_id = ?", (interest, uid))
    conn.commit()

# 3. Dropdown Menu UI Classes (Upgraded with Rocket Pass & RL Commands)
class EconomyHelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Economy", description="Wallet, balance, work, crime, leaderboard, & giving", emoji="🪙"),
            discord.SelectOption(label="Casino", description="High suspense games (3% slots win rate, 1000x jackpot)", emoji="🎰"),
            discord.SelectOption(label="Rocket League", description="1v1 match sim, crate openings with pictures, & Rocket Pass!", emoji="⚽"),
            discord.SelectOption(label="Shop & Inventory", description="Browse items, keys, auto-buy lottery, & boosters", emoji="🛒"),
            discord.SelectOption(label="Bank & Bounties", description="Deposits, bank interest, and revenge bounties", emoji="🏦"),
        ]
        super().__init__(
            placeholder="Select a category to view commands...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Economy":
            embed = discord.Embed(
                title="🪙 Economy Commands",
                description=(
                    "`!balance [@user]` - Check wallet & wealth tier\n"
                    "`!leaderboard` (`!lb`) - View top 10 richest players\n"
                    "`!daily` - Claim reward ($500 every 15m)\n"
                    "`!work` - Work a job for quick cash (5m cooldown)\n"
                    "`!crime` - Commit a risky crime for big payouts\n"
                    "`!give <@user> <amount>` - Transfer money to someone\n"
                    "`!rob <@user>` - Attempt to steal money"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Economy Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Casino":
            embed = discord.Embed(
                title="🎰 Casino Commands (High Suspense)",
                description=(
                    "`!cf <bet> <heads/tails>` - Coinflip 50/50 with animated toss\n"
                    "`!slots <bet>` - Slot machine (3% win rate, 777 pays 1000x, 2 match pays 3x)\n"
                    "`!bj <bet>` - Blackjack vs dealer with card dealing\n"
                    "`!roulette <bet> <red/black/green>` - Spinning wheel roulette\n"
                    "`!poker <bet>` - Quick poker hand with river showdown"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Casino Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Rocket League":
            embed = discord.Embed(
                title="⚽ Rocket League & Pass Commands",
                description=(
                    "`!match <bet>` - Queue up for a 1v1 match simulation with live suspense\n"
                    "`!pass` (`!lvl`) - Check your Rocket Pass level, XP progress, & tier rewards\n"
                    "`!crate` - Use a Crate Key (`!buy crate_key`) to open crates & view item pictures!\n"
                    "`!tradein` - Trade up 3 lower crate items for a higher rarity item\n"
                    "`!sell <item_name> <price>` - List an item on the global marketplace\n"
                    "`!market` - Browse active player market listings\n"
                    "`!buyitem <listing_id>` - Purchase an item from the market"
                ),
                color=discord.Color.from_rgb(0, 191, 255)
            )
            embed.set_footer(text="Rocket League Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Shop & Inventory":
            embed = discord.Embed(
                title="🛒 Shop & Inventory",
                description=(
                    "`!shop` - View items for sale\n"
                    "`!buy <item_id>` - Purchase gear or crate keys\n"
                    "`!scratch` - Auto-buys ticket if needed & scratches for $7M!\n"
                    "`!inventory` - View your items and gear"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Shop Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Bank & Bounties":
            embed = discord.Embed(
                title="🏦 Bank & Bounties",
                description=(
                    "`!deposit <amount/all>` (`!dep`) - Deposit cash (Earns 1% hourly interest!)\n"
                    "`!withdraw <amount/all>` (`!with`) - Withdraw cash from bank\n"
                    "`!bounty <@user> <amount>` - Bounty someone who robbed you\n"
                    "`!bounties` - View active server bounties\n"
                    "`!reseteco` - Admin command to reset server economy"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Bank Menu")
            await interaction.response.edit_message(embed=embed)

class EconomyHelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(EconomyHelpSelect())

# 4. Bot Events & Base Commands
@bot.event
async def on_ready():
    bank_interest.start()
    print(f"Logged in successfully as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    # Give XP for chatting in the server (10 XP per message)
    leveled_up, new_lvl = add_user_xp(message.author.id, 10)
    if leveled_up:
        await message.channel.send(f"🎉 {message.author.mention}, your **Rocket Pass** leveled up to **Tier {new_lvl}**! You unlocked **$5,000** and a free **Champion Crate Key** (`crate_key`)! 🔑")
    
    await bot.process_commands(message)

@bot.command(name="help")
async def eco_help(ctx):
    embed = discord.Embed(
        title="🪙 Economy & Rocket League Help Menu",
        description="Select a category from the dropdown menu below to view available commands.",
        color=discord.Color.from_rgb(138, 43, 226)
    )
    embed.set_footer(text="Economy Menu")
    await ctx.send(embed=embed, view=EconomyHelpView())

# Balance UI
@bot.command(name="bal", aliases=["balance"])
async def bal(ctx, member: discord.Member = None):
    target = member or ctx.author
    udata = get_user_data(target.id)
    user_bal = get_balance(target.id)
    bank_bal = udata["bank"]

    if target.id in infinite_money_users:
        wallet_str = "∞ (Infinite)"
        bank_str = "∞ (Infinite)"
        rank_title, embed_color = "👑 Infinite Mogul", discord.Color.gold()
    else:
        wallet_str = f"${user_bal:,}"
        bank_str = f"${bank_bal:,}"
        total_wealth = user_bal + bank_bal
        if total_wealth >= 1000000000:
            rank_title, embed_color = "👑 Mogul", discord.Color.gold()
        elif total_wealth >= 1000000:
            rank_title, embed_color = "💎 High Roller", discord.Color.from_rgb(0, 191, 255)
        elif total_wealth >= 50000:
            rank_title, embed_color = "💼 Entrepreneur", discord.Color.from_rgb(50, 205, 50)
        elif total_wealth > 0:
            rank_title, embed_color = "🪙 Earner", discord.Color.from_rgb(138, 43, 226)
        else:
            rank_title, embed_color = "💸 Destitute", discord.Color.dark_embed()

    embed = discord.Embed(title=f"🏦 Financial Profile — {target.display_name}", color=embed_color)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="💳 Wallet Balance", value=f"**{wallet_str}**", inline=True)
    embed.add_field(name="🏛️ Bank Account", value=f"**{bank_str}** *(Earns 1% hourly interest!)*", inline=True)
    embed.add_field(name="🏆 Wealth Tier", value=f"**{rank_title}**", inline=False)
    embed.set_footer(text="Secure Memory-Backed Ledger")
    await ctx.send(embed=embed)

# --- LEADERBOARD COMMAND ---
@bot.command(name="leaderboard", aliases=["lb", "rich"])
async def leaderboard(ctx):
    cursor.execute("SELECT user_id, wallet, bank FROM users")
    rows = cursor.fetchall()

    if not rows:
        return await ctx.send("❌ No economy data found yet!")

    user_wealth = []
    for row in rows:
        uid, wallet, bank = row
        total = 999999999999999 if int(uid) in infinite_money_users else (wallet + bank)
        user_wealth.append((uid, total))

    user_wealth.sort(key=lambda x: x[1], reverse=True)

    embed = discord.Embed(
        title="🏆 Server Wealth Leaderboard — Top 10",
        description="Ranking the wealthiest players by total net worth (Wallet + Bank).",
        color=discord.Color.gold()
    )

    medals = ["🥇", "🥈", "🥉"]
    lb_text = []

    for idx, (uid, wealth) in enumerate(user_wealth[:10], start=1):
        try:
            member = await ctx.guild.fetch_member(int(uid))
            name = member.display_name
        except Exception:
            name = f"User ID: {uid}"

        wealth_str = "∞ (Infinite)" if wealth >= 999999999999999 else f"${wealth:,}"
        prefix = medals[idx - 1] if idx <= 3 else f"`#{idx}`"
        lb_text.append(f"{prefix} **{name}** — 💰 **{wealth_str}**")

    embed.add_field(name="Richest Players", value="\n".join(lb_text), inline=False)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}", icon_url=ctx.author.display_avatar.url)
    await ctx.send(embed=embed)

# --- WORK & CRIME COMMANDS ---
@bot.command(name="work")
@commands.cooldown(1, 300, commands.BucketType.user)
async def work(ctx):
    jobs = [
        ("Discord Moderator", random.randint(200, 600)),
        ("Roblox Game Developer", random.randint(400, 900)),
        ("Fast Food Cashier", random.randint(150, 400)),
        ("Python Bot Coder", random.randint(500, 1200))
    ]
    job_name, pay = random.choice(jobs)
    update_balance(ctx.author.id, pay)
    add_user_xp(ctx.author.id, 25) # Extra XP for working
    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    
    embed = discord.Embed(
        title="💼 Job Shift Completed!",
        description=f"You worked as a **{job_name}** and earned **${pay:,}** (+25 XP)!\nNew wallet: **{new_bal}**",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@work.error
async def work_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        mins, secs = divmod(int(error.retry_after), 60)
        await ctx.send(f"⏳ You are tired from working! Rest for **{mins}m {secs}s** before working again.")

@bot.command(name="crime")
@commands.cooldown(1, 600, commands.BucketType.user)
async def crime(ctx):
    thief_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and thief_bal < 2000:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ You need at least **$2,000** in your wallet to cover bail if your crime fails!")

    crimes = [
        ("bank ATM hacking", 1500, 4000),
        ("stealing a high-end gaming PC", 800, 2500),
        ("robbing a convenience store", 400, 1500)
    ]
    crime_name, min_p, max_p = random.choice(crimes)
    success = random.random() <= 0.50 or ctx.author.id in infinite_money_users

    if success:
        payout = random.randint(min_p, max_p)
        update_balance(ctx.author.id, payout)
        add_user_xp(ctx.author.id, 50)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        embed = discord.Embed(
            title="🥷 Crime Successful!",
            description=f"You successfully got away with **{crime_name}** and pocketed **${payout:,}** (+50 XP)!\nNew wallet: **{new_bal}**",
            color=discord.Color.green()
        )
    else:
        fine = random.randint(1000, 3000)
        if ctx.author.id not in infinite_money_users:
            update_balance(ctx.author.id, -fine)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        embed = discord.Embed(
            title="🚨 Busted by Police!",
            description=f"You failed your attempt at **{crime_name}** and paid a police fine of **${fine:,}**.\nNew wallet: **{new_bal}**",
            color=discord.Color.red()
        )
    await ctx.send(embed=embed)

@crime.error
async def crime_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        mins, secs = divmod(int(error.retry_after), 60)
        await ctx.send(f"⏳ The police are still looking for you! Lay low for **{mins}m {secs}s**.")

# --- BOUNTY SYSTEM ---
@bot.command(name="bounty")
async def bounty(ctx, target: discord.Member, amount: int):
    if target.id == ctx.author.id:
        return await ctx.send("❌ You cannot place a bounty on yourself!")
    if amount <= 0:
        return await ctx.send("❌ Bounty amount must be greater than $0.")

    user_thief_records = ROBBERY_VICTIMS.get(ctx.author.id, set())
    if target.id not in user_thief_records and ctx.author.id not in infinite_money_users:
        return await ctx.send(f"❌ You can only place a bounty on **{target.display_name}** if they have successfully robbed/stolen from you!")

    sender_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and sender_bal < amount:
        return await ctx.send(f"❌ You don't have enough money in your wallet to fund this bounty! Wallet: **${sender_bal:,}**")

    if ctx.author.id not in infinite_money_users:
        update_balance(ctx.author.id, -amount)

    cursor.execute("INSERT OR REPLACE INTO bounties (target_id, issuer_id, amount) VALUES (?, ?, ?)", 
                   (str(target.id), str(ctx.author.id), amount))
    conn.commit()

    await ctx.send(f"🎯 {ctx.author.mention} placed a revenge bounty of **${amount:,}** on {target.display_name} for stealing from them!")

@bot.command(name="bounties", aliases=["bountylist"])
async def bounties(ctx):
    cursor.execute("SELECT target_id, amount FROM bounties")
    rows = cursor.fetchall()
    if not rows:
        return await ctx.send("📋 There are currently no active bounties on anyone.")

    embed = discord.Embed(title="🎯 Active Server Bounties", color=discord.Color.dark_red())
    b_list = []
    for uid, amt in rows:
        try:
            member = await ctx.guild.fetch_member(int(uid))
            name = member.display_name
        except Exception:
            name = f"User ID: {uid}"
        b_list.append(f"👤 **{name}** — 💰 Reward: **${amt:,}**")

    embed.description = "\n".join(b_list)
    await ctx.send(embed=embed)

# --- DAILY REWARD ---
@bot.command(name="daily")
@commands.cooldown(1, 900, commands.BucketType.user)
async def daily(ctx):
    update_balance(ctx.author.id, 500)
    add_user_xp(ctx.author.id, 30)
    new_bal = get_balance(ctx.author.id)
    await ctx.send(f"{ctx.author.mention}, you claimed your daily reward of **$500** (+30 XP)! Wallet: **${new_bal:,}**")

@daily.error
async def daily_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        minutes, seconds = divmod(int(error.retry_after), 60)
        await ctx.send(f"⏳ You are on cooldown! Try again in **{minutes}m {seconds}s**.")

# --- BANKING: DEPOSIT & WITHDRAW ---
@bot.command(name="deposit", aliases=["dep"])
async def deposit(ctx, amount: str):
    uid_str = str(ctx.author.id)
    udata = get_user_data(uid_str)
    wallet = udata["wallet"]

    if wallet <= 0 and ctx.author.id not in infinite_money_users:
        return await ctx.send("❌ You don't have any cash in your wallet to deposit!")

    if amount.lower() == "all":
        deposit_amt = wallet
    else:
        try:
            deposit_amt = int(amount)
        except ValueError:
            return await ctx.send("❌ Please specify a valid number or `all`.")

    if deposit_amt <= 0:
        return await ctx.send("❌ Amount must be greater than $0.")

    if ctx.author.id not in infinite_money_users and deposit_amt > wallet:
        return await ctx.send(f"❌ You don't have that much cash! Your wallet has **${wallet:,}**.")

    if ctx.author.id not in infinite_money_users:
        cursor.execute("UPDATE users SET wallet = wallet - ?, bank = bank + ? WHERE user_id = ?", (deposit_amt, deposit_amt, uid_str))
    else:
        cursor.execute("UPDATE users SET bank = bank + ? WHERE user_id = ?", (deposit_amt, uid_str))
    conn.commit()

    updated = get_user_data(uid_str)
    USER_MEMORY_CACHE[uid_str] = {"wallet": updated["wallet"], "bank": updated["bank"]}

    w_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['wallet']:,}"
    b_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['bank']:,}"
    await ctx.send(f"🏦 {ctx.author.mention}, successfully deposited **${deposit_amt:,}** into your bank! (Wallet: {w_str} | Bank: {b_str})")

@bot.command(name="withdraw", aliases=["with"])
async def withdraw(ctx, amount: str):
    uid_str = str(ctx.author.id)
    udata = get_user_data(uid_str)
    bank = udata["bank"]

    if bank <= 0:
        return await ctx.send("❌ You don't have any money in your bank account!")

    if amount.lower() == "all":
        withdraw_amt = bank
    else:
        try:
            withdraw_amt = int(amount)
        except ValueError:
            return await ctx.send("❌ Please specify a valid number or `all`.")

    if withdraw_amt <= 0:
        return await ctx.send("❌ Amount must be greater than $0.")

    if withdraw_amt > bank:
        return await ctx.send(f"❌ You don't have that much in your bank! Bank balance: **${bank:,}**.")

    cursor.execute("UPDATE users SET wallet = wallet + ?, bank = bank - ? WHERE user_id = ?", (withdraw_amt, withdraw_amt, uid_str))
    conn.commit()

    updated = get_user_data(uid_str)
    USER_MEMORY_CACHE[uid_str] = {"wallet": updated["wallet"], "bank": updated["bank"]}

    w_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['wallet']:,}"
    b_str = f"${updated['bank']:,}"
    await ctx.send(f"💵 {ctx.author.mention}, successfully withdrew **${withdraw_amt:,}** from your bank! (Wallet: {w_str} | Bank: {b_str})")

# --- GIVE MONEY COMMAND ---
@bot.command(name="give", aliases=["pay", "send"])
async def give(ctx, recipient: discord.Member, amount_str: str):
    if recipient.id == ctx.author.id:
        return await ctx.send("❌ You cannot send money to yourself!")

    try:
        amount = int(amount_str)
    except ValueError:
        return await ctx.send("❌ Please specify a valid number amount.")

    if amount <= 0:
        return await ctx.send("❌ Please specify a valid amount greater than $0.")

    if amount > 999999999999999:
        return await ctx.send("❌ That amount is way too large! Keep it under 999 trillion.")

    sender_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and sender_bal < amount:
        return await ctx.send(f"❌ You don't have enough wallet money! Your wallet balance is **${sender_bal:,}**.")

    if ctx.author.id not in infinite_money_users:
        update_balance(ctx.author.id, -amount)
    
    update_balance(recipient.id, amount)
    
    new_sender_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    await ctx.send(f"💸 {ctx.author.mention} successfully sent **${amount:,}** to {recipient.mention}! Your new wallet balance: **{new_sender_bal}**")

# --- ADMIN / OWNER SERVER-WIDE ECONOMY RESET ---
@bot.command(name="reseteco", aliases=["reseteconomy"])
async def reseteco(ctx):
    if not function_is_privileged(ctx):
        return await ctx.send("❌ You do not have permission to use this command!")
        
    USER_MEMORY_CACHE.clear()
    ROBBERY_VICTIMS.clear()
    cursor.execute("DELETE FROM users")
    cursor.execute("DELETE FROM inventory")
    cursor.execute("DELETE FROM bounties")
    cursor.execute("DELETE FROM market")
    cursor.execute("DELETE FROM user_levels")
    conn.commit()
    await ctx.send(f"⚠️ {ctx.author.mention} has completely reset the server economy! All wallets, banks, inventories, market listings, bounties, and Rocket Pass levels have been wiped.")

# --- SHOP & INVENTORY ---
@bot.command(name="shop")
async def shop(ctx):
    embed = discord.Embed(title="🛒 Protection, Crate Keys & Booster Shop", color=discord.Color.teal())
    for item_id, info in SHOP_ITEMS.items():
        embed.add_field(
            name=f"{info['emoji']} {info['name']} (`{item_id}`)",
            value=f"Price: **${info['price']:,}**\n{info['desc']}",
            inline=False
        )
    embed.set_footer(text="Use !buy <item_id>!")
    await ctx.send(embed=embed)

@bot.command(name="buy")
async def buy(ctx, item_id: str):
    item_id = item_id.lower()
    if item_id not in SHOP_ITEMS:
        return await ctx.send("❌ Invalid item ID! Type `!shop` to see available items.")

    item = SHOP_ITEMS[item_id]
    user_bal = get_balance(ctx.author.id)

    if ctx.author.id not in infinite_money_users and user_bal < item["price"]:
        return await ctx.send(f"❌ You need **${item['price']:,}** in your wallet to buy a {item['name']}, but you only have **${user_bal:,}**!")

    if ctx.author.id not in infinite_money_users:
        update_balance(ctx.author.id, -item["price"])
        
    add_item_qty(ctx.author.id, item_id, 1)

    embed = discord.Embed(
        title="✅ Purchase Successful!",
        description=f"{ctx.author.mention}, successfully purchased **{item['emoji']} {item['name']}**!",
        color=discord.Color.green()
    )
    if "img" in item:
        embed.set_thumbnail(url=item["img"])
    await ctx.send(embed=embed)

@bot.command(name="inventory", aliases=["inv"])
async def inventory(ctx, member: discord.Member = None):
    target = member or ctx.author
    embed = discord.Embed(title=f"🎒 Inventory — {target.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)

    desc_list = []
    for item_id, info in SHOP_ITEMS.items():
        qty = get_item_qty(target.id, item_id)
        if qty > 0:
            desc_list.append(f"{info['emoji']} **{info['name']}** (x{qty})")

    for item_name, info in CRATE_DROPS.items():
        qty = get_item_qty(target.id, item_name)
        if qty > 0:
            desc_list.append(f"{info['emoji']} **{item_name}** (x{qty}) — *{info['rarity']}*")

    if not desc_list:
        embed.description = "Your inventory is completely empty!"
    else:
        embed.description = "\n".join(desc_list)

    await ctx.send(embed=embed)

# --- SCRATCH CARD ---
@bot.command(name="scratch", aliases=["lottery"])
async def scratch(ctx):
    tickets = get_item_qty(ctx.author.id, "lottery_ticket")
    auto_bought = False

    if tickets <= 0:
        ticket_price = SHOP_ITEMS["lottery_ticket"]["price"]
        user_bal = get_balance(ctx.author.id)

        if ctx.author.id not in infinite_money_users and user_bal < ticket_price:
            return await ctx.send(f"❌ You don't have a Lottery Ticket and can't afford one! Ticket price: **${ticket_price:,}** (Wallet: **${user_bal:,}**).")

        if ctx.author.id not in infinite_money_users:
            update_balance(ctx.author.id, -ticket_price)
        
        auto_bought = True
    else:
        add_item_qty(ctx.author.id, "lottery_ticket", -1)

    msg = await ctx.send("🎟️ **Scratching ticket...** `[ 🪙 Rubbing silver foil... ]`")
    await asyncio.sleep(0.8)

    roll = random.random()
    if roll <= 0.01: # 1% Chance
        prize = 7000000
        desc = f"🎟️ **JACKPOT! YOU HIT THE 1% CHANCE!**\n{ctx.author.mention}, you won **${prize:,}**!"
        color = discord.Color.gold()
    else:
        prize = 0
        desc = f"🎟️ **No luck!** Your scratch ticket was a loser. Better luck next time, {ctx.author.mention}!"
        color = discord.Color.red()

    if prize > 0:
        update_balance(ctx.author.id, prize)
        add_user_xp(ctx.author.id, 100)

    auto_text = " *(Auto-purchased ticket for $5,000)*" if auto_bought else ""
    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    embed = discord.Embed(
        title="🎟️ Lottery Scratch Card Result",
        description=f"{desc}{auto_text}\nNew wallet: **{new_bal}**",
        color=color
    )
    await msg.edit(content=None, embed=embed)

# --- STEAL / ROB SYSTEM ---
@bot.command(name="rob", aliases=["steal"])
@commands.cooldown(1, 60, commands.BucketType.user)
async def rob(ctx, victim: discord.Member):
    if victim.id == ctx.author.id:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ You cannot rob yourself!")

    if victim.guild_permissions.administrator:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("🛡️ **Admin Immunity Active!** You cannot rob server administrators.")

    thief_bal = get_balance(ctx.author.id)
    victim_bal = get_balance(victim.id)

    if ctx.author.id not in infinite_money_users and thief_bal < 5000:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send("❌ You need at least **$5,000** in your wallet to fund a robbery attempt!")

    if victim_bal <= 0:
        ctx.command.reset_cooldown(ctx)
        return await ctx.send(f"💀 **{victim.display_name}** is too broke! They have no cash in their wallet.")

    victim_locks = get_item_qty(victim.id, "padlock")
    if victim_locks > 0:
        add_item_qty(victim.id, "padlock", -1)
        if ctx.author.id not in infinite_money_users:
            update_balance(ctx.author.id, -2000)
        return await ctx.send(f"🔒 **Robbery Failed!** {victim.mention}'s **Padlock** snapped and blocked your attempt!")

    camera_qty = get_item_qty(ctx.author.id, "camera")
    success_chance = 0.45 + (0.40 if camera_qty > 0 else 0.0)

    roll = random.random()
    if roll <= success_chance or ctx.author.id in infinite_money_users:
        stolen_amount = int(victim_bal * random.uniform(0.15, 0.30))
        stolen_amount = max(1, stolen_amount)
        update_balance(victim.id, -stolen_amount)
        update_balance(ctx.author.id, stolen_amount)
        add_user_xp(ctx.author.id, 40)
        
        if victim.id not in ROBBERY_VICTIMS:
            ROBBERY_VICTIMS[victim.id] = set()
        ROBBERY_VICTIMS[victim.id].add(ctx.author.id)

        cursor.execute("SELECT amount FROM bounties WHERE target_id = ?", (str(ctx.author.id),))
        b_res = cursor.fetchone()
        bounty_bonus = 0
        if b_res:
            bounty_bonus = b_res[0]
            update_balance(ctx.author.id, bounty_bonus)
            cursor.execute("DELETE FROM bounties WHERE target_id = ?", (str(ctx.author.id),))
            conn.commit()

        bounty_text = f"\n🎯 **Bounty Claimed!** You collected a reward of **${bounty_bonus:,}**!" if bounty_bonus > 0 else ""
        await ctx.send(f"🥷 Success! You sneakily stole **${stolen_amount:,}** from {victim.mention} (+40 XP)!{bounty_text}")
    else:
        fine = random.randint(10000, 20000)
        actual_fine = min(thief_bal, fine)
        if ctx.author.id not in infinite_money_users:
            update_balance(ctx.author.id, -actual_fine)
        await ctx.send(f"🚨 **Busted!** You got caught trying to rob {victim.mention} and had to pay a police fine of **${actual_fine:,}**!")

@rob.error
async def rob_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ You're on cooldown! Try again in **{int(error.retry_after)}s**.")

# --- SECRET RIG & INF MONEY TOGGLES ---
@bot.command(name="rig")
async def rig(ctx):
    if not has_jackpott_role(ctx):
        return
    try: await ctx.message.delete()
    except Exception: pass
    
    if ctx.author.id in rigged_users:
        rigged_users.remove(ctx.author.id)
        await ctx.send("🤫 *Rigged mode DISABLED.*", delete_after=0.2)
    else:
        rigged_users.add(ctx.author.id)
        await ctx.send("🤫 *Rigged mode ENABLED.*", delete_after=0.2)

@bot.command(name="infmoney", aliases=["infinite"])
async def infmoney(ctx):
    if not function_is_privileged(ctx):
        return
    try: await ctx.message.delete()
    except Exception: pass
    
    if ctx.author.id in infinite_money_users:
        infinite_money_users.remove(ctx.author.id)
        await ctx.send("🤫 *Infinite money DISABLED.*", delete_after=0.2)
    else:
        infinite_money_users.add(ctx.author.id)
        await ctx.send("🤫 *Infinite money ENABLED.*", delete_after=0.2)

# --- ROCKET LEAGUE & PASS FEATURES ---
@bot.command(name="match", aliases=["rl"])
async def match(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("⚽ **Rocket League 1v1 Match**\n`[ 3... 2... 1... GO! Kickoff! 🏎️💨 ]`")
    await asyncio.sleep(0.8)
    await msg.edit(content="⚽ **Rocket League 1v1 Match**\n`[ 💥 Big hit mid-field! Opponent going for an aerial... ]`")
    await asyncio.sleep(0.8)
    await msg.edit(content="⚽ **Rocket League 1v1 Match**\n`[ 🛡️ EPIC SAVE! You clear it off the goal line! ]`")
    await asyncio.sleep(0.8)
    await msg.edit(content="⚽ **Rocket League 1v1 Match**\n`[ ⏱️ 0 SECONDS ON THE CLOCK! BALL IS STILL IN THE AIR! ]`")
    await asyncio.sleep(0.8)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    win_chance = 0.50 + luck_boost

    if ctx.author.id in rigged_users or ctx.author.id in infinite_money_users:
        won = True
    else:
        won = random.random() <= win_chance

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id) + (bet if won else -bet):,}"

    if won:
        update_balance(ctx.author.id, bet)
        add_user_xp(ctx.author.id, 35)
        embed = discord.Embed(
            title="⚽ Rocket League 1v1 — VICTORY!",
            description=f"🚨 **GOAL! Flip-reset shot in Overtime!**\n\n🎉 {ctx.author.mention}, you won your 1v1 and earned **${bet:,}** (+35 XP)!\nNew wallet: **{new_bal}**",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url="https://rocket-league.com/content/media/items/avatar/220px/explosion_duelingdragons_hero.png")
    else:
        update_balance(ctx.author.id, -bet)
        embed = discord.Embed(
            title="⚽ Rocket League 1v1 — DEFEAT",
            description=f"💀 **Open net missed at 0 seconds!** Opponent scored on the counter-attack.\n\n{ctx.author.mention}, you lost **${bet:,}**.\nNew wallet: **{new_bal}**",
            color=discord.Color.red()
        )

    await msg.edit(content=None, embed=embed)

@bot.command(name="pass", aliases=["level", "lvl", "rocketpass"])
async def rocket_pass(ctx, member: discord.Member = None):
    target = member or ctx.author
    uid_str = str(target.id)
    cursor.execute("SELECT xp, level FROM user_levels WHERE user_id = ?", (uid_str,))
    res = cursor.fetchone()
    xp, level = res if res else (0, 1)
    next_level_xp = level * 100

    embed = discord.Embed(
        title=f"🏎️ Rocket Pass — {target.display_name}",
        description=f"**Current Tier:** Tier {level}\n**XP Progress:** {xp} / {next_level_xp} XP\n\n*Earn XP by chatting, working, and playing games to level up your pass and unlock free Crate Keys and cash rewards!*",
        color=discord.Color.from_rgb(0, 191, 255)
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    await ctx.send(embed=embed)

@bot.command(name="crate", aliases=["open"])
async def crate(ctx):
    keys = get_item_qty(ctx.author.id, "crate_key")
    if keys <= 0:
        return await ctx.send("❌ You need a **Champion Crate Key** (`crate_key`)! Buy one from the shop (`!buy crate_key`) or level up your Rocket Pass (`!pass`).")

    add_item_qty(ctx.author.id, "crate_key", -1)

    msg = await ctx.send("🔑 **Inserting Key into Champion Crate...** `[ 📦 Unlocking latches... ]`")
    await asyncio.sleep(0.8)
    await msg.edit(content="📦 **Opening Champion Crate...** `[ 🛞 Spinning items: Exotic... Import... Black Market... ]`")
    await asyncio.sleep(0.8)

    items = list(CRATE_DROPS.keys())
    weights = [40, 25, 20, 10, 4, 1] 
    drop_name = random.choices(items, weights=weights)[0]
    drop_info = CRATE_DROPS[drop_name]

    add_item_qty(ctx.author.id, drop_name, 1)
    add_user_xp(ctx.author.id, 50)

    embed = discord.Embed(
        title=f"📦 Crate Opened — {drop_info['rarity']} Drop!",
        description=f"{ctx.author.mention}, you unlocked **{drop_info['emoji']} {drop_name}** (+50 XP)!\nEstimated Value: **${drop_info['value']:,}**",
        color=discord.Color.gold() if "White" in drop_name else discord.Color.purple()
    )
    embed.set_image(url=drop_info["img"])
    await msg.edit(content=None, embed=embed)

@bot.command(name="tradein")
async def tradein(ctx):
    user_inventory = []
    for drop_name in CRATE_DROPS.keys():
        qty = get_item_qty(ctx.author.id, drop_name)
        if qty > 0:
            user_inventory.append((drop_name, qty))

    total_crate_items = sum(qty for _, qty in user_inventory)

    if total_crate_items < 3:
        return await ctx.send("❌ You need at least 3 Rocket League crate items in your inventory to perform a Trade-In!")

    count = 0
    for name, qty in user_inventory:
        to_remove = min(qty, 3 - count)
        add_item_qty(ctx.author.id, name, -to_remove)
        count += to_remove
        if count >= 3:
            break

    rare_items = ["Dissolver Decal", "Dueling Dragons", "Titanium White Octane"]
    won_item = random.choices(rare_items, weights=[60, 30, 10])[0]
    drop_info = CRATE_DROPS[won_item]

    add_item_qty(ctx.author.id, won_item, 1)
    add_user_xp(ctx.author.id, 100)

    embed = discord.Embed(
        title="🔄 Trade-In Successful!",
        description=f"{ctx.author.mention}, you traded in 3 items and unlocked a **{drop_info['rarity']} {drop_info['emoji']} {won_item}** (+100 XP)!",
        color=discord.Color.green()
    )
    embed.set_image(url=drop_info["img"])
    await ctx.send(embed=embed)

@bot.command(name="sell")
async def sell(ctx, *, item_and_price: str):
    try:
        parts = item_and_price.rsplit(" ", 1)
        item_name = parts[0].strip()
        price = int(parts[1])
    except Exception:
        return await ctx.send("❌ Usage: `!sell <item_name> <price>` (e.g., `!sell Octane Body 60000`)")

    qty = get_item_qty(ctx.author.id, item_name)
    if qty <= 0:
        return await ctx.send(f"❌ You don't have a **{item_name}** in your inventory to sell!")

    add_item_qty(ctx.author.id, item_name, -1)
    cursor.execute("INSERT INTO market (seller_id, item_name, price) VALUES (?, ?, ?)", (str(ctx.author.id), item_name, price))
    conn.commit()

    await ctx.send(f"🏷️ {ctx.author.mention} listed **{item_name}** on the market for **${price:,}**!")

@bot.command(name="market")
async def market(ctx):
    cursor.execute("SELECT listing_id, seller_id, item_name, price FROM market")
    rows = cursor.fetchall()
    if not rows:
        return await ctx.send("📋 The global marketplace is currently empty!")

    embed = discord.Embed(title="🏷️ Global Marketplace", color=discord.Color.blue())
    desc = []
    for lid, sid, iname, price in rows:
        try:
            seller = await ctx.guild.fetch_member(int(sid))
            sname = seller.display_name
        except Exception:
            sname = f"User {sid}"
        desc.append(f"`ID: {lid}` — **{iname}** listed by **{sname}** for **${price:,}**")

    embed.description = "\n".join(desc)
    embed.set_footer(text="Use !buyitem <listing_id> to purchase!")
    await ctx.send(embed=embed)

@bot.command(name="buyitem")
async def buyitem(ctx, listing_id: int):
    cursor.execute("SELECT seller_id, item_name, price FROM market WHERE listing_id = ?", (listing_id,))
    res = cursor.fetchone()
    if not res:
        return await ctx.send("❌ Invalid listing ID!")

    seller_id, item_name, price = res
    if str(ctx.author.id) == seller_id:
        return await ctx.send("❌ You cannot buy your own market listing!")

    buyer_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and buyer_bal < price:
        return await ctx.send(f"❌ You don't have enough wallet money! Price: **${price:,}**")

    if ctx.author.id not in infinite_money_users:
        update_balance(ctx.author.id, -price)
    
    update_balance(int(seller_id), price)
    add_item_qty(ctx.author.id, item_name, 1)

    cursor.execute("DELETE FROM market WHERE listing_id = ?", (listing_id,))
    conn.commit()

    await ctx.send(f"✅ {ctx.author.mention} bought **{item_name}** from <@{seller_id}> for **${price:,}**!")

# 5. Gambling Games (3% Slots, 1000x Jackpot, 2-match small wins, Suspense)

@bot.command(name="cf", aliases=["coinflip"])
async def coinflip(ctx, bet: int, choice: str):
    choice = choice.lower()
    if choice not in ["heads", "tails"]:
        return await ctx.send("❌ Choose either `heads` or `tails`. Example: `!cf 500 heads`")
    
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🪙 **Coinflip Arena**\n`[ 🔄 Toss in progress... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🪙 **Coinflip Arena**\n`[ 🌀 Flipping high in the air... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🪙 **Coinflip Arena**\n`[ 🔍 The coin is about to land... ]`")
    await asyncio.sleep(0.6)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    dice_text = "\n🎲 *(Lucky Dice consumed: +15% Luck)*" if luck_boost > 0 else ""

    if ctx.author.id in rigged_users:
        outcome = choice
    else:
        if luck_boost > 0 and random.random() <= luck_boost:
            outcome = choice 
        else:
            outcome = random.choice(["heads", "tails"])

    if ctx.author.id in infinite_money_users:
        new_bal = "∞ (Infinite)"
    else:
        new_bal = f"${get_balance(ctx.author.id) + (bet if choice == outcome else -bet):,}"

    if choice == outcome:
        update_balance(ctx.author.id, bet)
        add_user_xp(ctx.author.id, 15)
        embed = discord.Embed(
            title="🪙 Coinflip — VICTORY!",
            description=f"The coin landed on **{outcome.upper()}**!\n\n🎉 {ctx.author.mention}, you won **${bet:,}** (+15 XP)!{dice_text}\nNew wallet: **{new_bal}**",
            color=discord.Color.green()
        )
    else:
        update_balance(ctx.author.id, -bet)
        embed = discord.Embed(
            title="🪙 Coinflip — DEFEAT",
            description=f"The coin landed on **{outcome.upper()}**!\n\n💀 {ctx.author.mention}, you lost **${bet:,}**.{dice_text}\nNew wallet: **{new_bal}**",
            color=discord.Color.red()
        )

    await msg.edit(content=None, embed=embed)

@bot.command(name="slots")
async def slots(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    emojis = ["🎰", "🍒", "🍋", "🔔", "💎", "7️⃣"]

    msg = await ctx.send("🎰 **Casino Slot Machine**\n`[ ❓ | ❓ | ❓ ]` — *Spinning 1st reel...*")
    await asyncio.sleep(0.7)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    dice_text = "\n🎲 *(Lucky Dice consumed: +15% Luck)*" if luck_boost > 0 else ""

    win_threshold = 0.03 + luck_boost

    if ctx.author.id in rigged_users:
        reel1, reel2, reel3 = "7️⃣", "7️⃣", "7️⃣"
    elif random.random() <= win_threshold:
        win_symbol = random.choices(["🍒", "🍋", "🔔", "💎", "7️⃣"], weights=[40, 30, 20, 9, 1])[0]
        reel1, reel2, reel3 = win_symbol, win_symbol, win_symbol
    else:
        reel1 = random.choice(emojis)
        reel2 = random.choice(emojis)
        reel3 = random.choice(emojis)
        if random.random() <= 0.15:
            match_sym = random.choice(emojis)
            reel1, reel2 = match_sym, match_sym
            reel3 = "🍒" if match_sym != "🍒" else "🍋"

    await msg.edit(content=f"🎰 **Casino Slot Machine**\n`[ {reel1} | ❓ | ❓ ]` — *Spinning 2nd reel...*")
    await asyncio.sleep(0.7)
    await msg.edit(content=f"🎰 **Casino Slot Machine**\n`[ {reel1} | {reel2} | ❓ ]` — *Spinning final reel...*")
    await asyncio.sleep(0.7)

    if reel1 == reel2 == reel3:
        if reel1 == "7️⃣":
            winnings = bet * 1000
            desc = f"🚨 **TRIPLE 7s MEGA JACKPOT!** (1000x)\n🎉 {ctx.author.mention}, you won an unbelievable **${winnings:,}**!"
            add_user_xp(ctx.author.id, 500)
        elif reel1 == "💎":
            winnings = bet * 25
            desc = f"💎 **DIAMOND JACKPOT!** (25x)\n🎉 {ctx.author.mention}, you won **${winnings:,}**!"
            add_user_xp(ctx.author.id, 200)
        else:
            winnings = bet * 10
            desc = f"🎉 **TRIPLE JACKPOT!** (10x)\n🎉 {ctx.author.mention}, you won **${winnings:,}**!"
            add_user_xp(ctx.author.id, 100)
        update_balance(ctx.author.id, winnings)
        embed_color = discord.Color.green()
        title_str = "🎰 Slots — JACKPOT WIN!"
    elif reel1 == reel2 or reel2 == reel3 or reel1 == reel3:
        winnings = bet * 3
        update_balance(ctx.author.id, winnings)
        add_user_xp(ctx.author.id, 25)
        desc = f"✨ **Small Win! (2 Matching Emojis)** (3x)\n🎉 {ctx.author.mention}, you won **${winnings:,}** (+25 XP)!"
        embed_color = discord.Color.gold()
        title_str = "🎰 Slots — SMALL WIN"
    else:
        update_balance(ctx.author.id, -bet)
        desc = f"❌ {ctx.author.mention}, you lost **${bet:,}**."
        embed_color = discord.Color.red()
        title_str = "🎰 Slots — LOSS"

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    embed = discord.Embed(
        title=title_str,
        description=f"**[ {reel1} | {reel2} | {reel3} ]**\n\n{desc}{dice_text}\nNew wallet: **{new_bal}**",
        color=embed_color
    )
    await msg.edit(content=None, embed=embed)

@bot.command(name="bj", aliases=["blackjack"])
async def blackjack(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🃏 **Blackjack Table**\n`[ 🎴 Shuffling deck... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🃏 **Blackjack Table**\n`[ 🎴 Dealing cards to players... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🃏 **Blackjack Table**\n`[ 🏛️ Dealer revealing house hand... ]`")
    await asyncio.sleep(0.6)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    dice_text = "\n🎲 *(Lucky Dice consumed: +15% Luck)*" if luck_boost > 0 else ""

    if ctx.author.id in rigged_users:
        player_score = 21
        dealer_score = random.randint(17, 20)
    else:
        player_score = random.randint(15, 21)
        dealer_score = random.randint(15, 21)
        if luck_boost > 0 and random.random() <= luck_boost:
            player_score = 21

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"

    if player_score > dealer_score:
        update_balance(ctx.author.id, bet)
        add_user_xp(ctx.author.id, 20)
        embed = discord.Embed(
            title="🃏 Blackjack — YOU WON!",
            description=f"Your Score: **{player_score}** | Dealer Score: **{dealer_score}**\n\n🎉 {ctx.author.mention}, you won **${bet:,}** (+20 XP)!{dice_text}\nWallet: **{new_bal}**",
            color=discord.Color.green()
        )
    elif player_score < dealer_score:
        update_balance(ctx.author.id, -bet)
        embed = discord.Embed(
            title="🃏 Blackjack — YOU LOST",
            description=f"Your Score: **{player_score}** | Dealer Score: **{dealer_score}**\n\n💀 {ctx.author.mention}, you lost **${bet:,}**.{dice_text}\nWallet: **{new_bal}**",
            color=discord.Color.red()
        )
    else:
        embed = discord.Embed(
            title="🃏 Blackjack — PUSH (TIE)",
            description=f"Both got **{player_score}**. Your bet of **${bet:,}** was returned.{dice_text}",
            color=discord.Color.gold()
        )

    await msg.edit(content=None, embed=embed)

@bot.command(name="roulette")
async def roulette(ctx, bet: int, choice: str):
    choice = choice.lower()
    if choice not in ["red", "black", "green"]:
        return await ctx.send("❌ Pick `red`, `black`, or `green`. Example: `!roulette 100 red`")

    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🎡 **Roulette Wheel**\n`[ 🔴 Spinning wheel... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🎡 **Roulette Wheel**\n`[ ⚫ Ball speeding across slots... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🎡 **Roulette Wheel**\n`[ 🟢 Ball slowing down... ]`")
    await asyncio.sleep(0.6)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    dice_text = "\n🎲 *(Lucky Dice consumed: +15% Luck)*" if luck_boost > 0 else ""

    if ctx.author.id in rigged_users:
        spin = choice
    else:
        if luck_boost > 0 and random.random() <= luck_boost:
            spin = choice
        else:
            spin = random.choices(["red", "black", "green"], weights=[47.3, 47.3, 5.4])[0]

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"

    if choice == spin:
        multiplier = 14 if choice == "green" else 2
        winnings = bet * multiplier
        update_balance(ctx.author.id, winnings)
        add_user_xp(ctx.author.id, 25)
        embed = discord.Embed(
            title="🎡 Roulette — WIN!",
            description=f"The ball landed on **{spin.upper()}**!\n\n🎉 {ctx.author.mention}, you won **${winnings:,}** (+25 XP)!{dice_text}\nWallet: **{new_bal}**",
            color=discord.Color.green()
        )
    else:
        update_balance(ctx.author.id, -bet)
        embed = discord.Embed(
            title="🎡 Roulette — LOSS",
            description=f"The ball landed on **{spin.upper()}**!\n\n💀 {ctx.author.mention}, you lost **${bet:,}**.{dice_text}\nWallet: **{new_bal}**",
            color=discord.Color.red()
        )

    await msg.edit(content=None, embed=embed)

@bot.command(name="poker")
async def poker(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("♠️ **Poker Table**\n`[ 🎴 Dealing pocket cards... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="♠️ **Poker Table**\n`[ 🃏 Revealing the flop and turn... ]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="♠️ **Poker Table**\n`[ 🃏 Dealing the final river card... ]`")
    await asyncio.sleep(0.6)

    luck_boost = consume_lucky_dice_boost(ctx.author.id)
    dice_text = "\n🎲 *(Lucky Dice consumed: +15%
