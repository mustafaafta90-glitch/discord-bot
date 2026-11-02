import os, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
threading.Thread(target=lambda: HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 10000))), Handler).serve_forever(), daemon=True).start()

import discord 
from discord.ext import commands
import sqlite3
import random
import asyncio

# 1. Setup Discord Intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Secret tracking sets (excluded from help/public views)
rigged_users = set()
infinite_money_users = set()

# Helper function for Owner / Co-Owner / Admin (used for infmoney)
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

# Quick migration check to ensure 'bank' column exists if upgrading from an older DB
try:
    cursor.execute("SELECT bank FROM users LIMIT 1")
except sqlite3.OperationalError:
    cursor.execute("ALTER TABLE users ADD COLUMN bank INTEGER DEFAULT 0")
    conn.commit()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
        user_id TEXT,
        item_name TEXT,
        quantity INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, item_name)
    )
""")
conn.commit()

# Database Helper Functions
def get_user_data(user_id):
    cursor.execute("SELECT wallet, bank FROM users WHERE user_id = ?", (str(user_id),))
    result = cursor.fetchone()
    if result is None:
        cursor.execute("INSERT INTO users (user_id, wallet, bank) VALUES (?, ?, ?)", (str(user_id), 0, 0))
        conn.commit()
        return {"wallet": 0, "bank": 0}
    return {"wallet": result[0], "bank": result[1]}

def get_balance(user_id):
    if user_id in infinite_money_users:
        return 999999999999999  # Return max/infinite display value
    return get_user_data(user_id)["wallet"]

def update_balance(user_id, amount):
    if user_id in infinite_money_users:
        return  # Infinite users never lose net worth
    
    get_user_data(user_id)
    cursor.execute("UPDATE users SET wallet = wallet + ? WHERE user_id = ?", (amount, str(user_id)))
    conn.commit()

def set_balance(user_id, amount):
    if user_id in infinite_money_users:
        return
    
    get_user_data(user_id)
    cursor.execute("UPDATE users SET wallet = ? WHERE user_id = ?", (amount, str(user_id)))
    conn.commit()

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

# Shop Catalog Items
SHOP_ITEMS = {
    "padlock": {"name": "Padlock", "price": 15000, "desc": "Protects your wallet from being stolen by other players.", "emoji": "🔒"},
    "camera": {"name": "Security Camera", "price": 35000, "desc": "Grants a +40% luck boost during robberies.", "emoji": "📷"},
    "cooldown_drink": {"name": "Energy Drink", "price": 10000, "desc": "Cuts your action cooldown timers down by 30%.", "emoji": "⚡"}
}

# 3. Dropdown Menu UI Classes
class EconomyHelpSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Economy", description="Wallet, balance, daily rewards, & giving", emoji="🪙"),
            discord.SelectOption(label="Casino", description="Coinflip, slots, blackjack, and roulette", emoji="🎰"),
            discord.SelectOption(label="Shop & Inventory", description="Browse defensive and booster items", emoji="🛒"),
            discord.SelectOption(label="Bank & Loans", description="Deposits, withdrawals, and loan options", emoji="🏦"),
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
                    "`!daily` - Claim daily reward ($500)\n"
                    "`!give <@user> <amount>` - Transfer money to someone\n"
                    "`!rob <@user>` - Attempt to steal money (Watch out for fines!)"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Economy Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Casino":
            embed = discord.Embed(
                title="🎰 Casino Commands",
                description=(
                    "`!cf <bet> <heads/tails>` - Coinflip 50/50\n"
                    "`!slots <bet>` - Slot machine (💎 5x, 777 10x)\n"
                    "`!bj <bet>` - Blackjack vs dealer\n"
                    "`!roulette <bet> <red/black/green>` - Roulette wheel\n"
                    "`!poker <bet>` - Quick poker hand"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Casino Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Shop & Inventory":
            embed = discord.Embed(
                title="🛒 Shop & Inventory",
                description=(
                    "`!shop` - View items for sale\n"
                    "`!buy <item_id>` - Purchase protection or boosters\n"
                    "`!inventory` - View your items and active gear"
                ),
                color=discord.Color.from_rgb(138, 43, 226)
            )
            embed.set_footer(text="Shop Menu")
            await interaction.response.edit_message(embed=embed)

        elif self.values[0] == "Bank & Loans":
            embed = discord.Embed(
                title="🏦 Bank & Loans",
                description=(
                    "`!deposit <amount/all>` (`!dep`) - Deposit cash into bank\n"
                    "`!withdraw <amount/all>` (`!with`) - Withdraw cash from bank\n"
                    "`!reseteco` - Admin reset command"
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
    print(f"Logged in successfully as {bot.user}")

@bot.command(name="help")
async def eco_help(ctx):
    embed = discord.Embed(
        title="🪙 Economy Help Menu",
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
    embed.add_field(name="🏛️ Bank Account", value=f"**{bank_str}**", inline=True)
    embed.add_field(name="🏆 Wealth Tier", value=f"**{rank_title}**", inline=False)
    embed.set_footer(text="Secure SQLite Ledger")
    await ctx.send(embed=embed)

@bot.command(name="daily")
async def daily(ctx):
    update_balance(ctx.author.id, 500)
    new_bal = get_balance(ctx.author.id)
    await ctx.send(f"{ctx.author.mention}, you claimed your daily reward of **$500**! Wallet: **${new_bal:,}**")

# --- BANKING: DEPOSIT & WITHDRAW ---
@bot.command(name="deposit", aliases=["dep"])
async def deposit(ctx, amount: str):
    udata = get_user_data(ctx.author.id)
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
        cursor.execute("UPDATE users SET wallet = wallet - ?, bank = bank + ? WHERE user_id = ?", (deposit_amt, deposit_amt, str(ctx.author.id)))
    else:
        cursor.execute("UPDATE users SET bank = bank + ? WHERE user_id = ?", (deposit_amt, str(ctx.author.id)))
    conn.commit()

    updated = get_user_data(ctx.author.id)
    w_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['wallet']:,}"
    b_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['bank']:,}"
    await ctx.send(f"🏦 {ctx.author.mention}, successfully deposited **${deposit_amt:,}** into your bank! (Wallet: {w_str} | Bank: {b_str})")

@bot.command(name="withdraw", aliases=["with"])
async def withdraw(ctx, amount: str):
    udata = get_user_data(ctx.author.id)
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

    cursor.execute("UPDATE users SET wallet = wallet + ?, bank = bank - ? WHERE user_id = ?", (withdraw_amt, withdraw_amt, str(ctx.author.id)))
    conn.commit()

    updated = get_user_data(ctx.author.id)
    w_str = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${updated['wallet']:,}"
    b_str = f"${updated['bank']:,}"
    await ctx.send(f"💵 {ctx.author.mention}, successfully withdrew **${withdraw_amt:,}** from your bank! (Wallet: {w_str} | Bank: {b_str})")

# --- GIVE MONEY COMMAND ---
@bot.command(name="give", aliases=["pay", "send"])
async def give(ctx, recipient: discord.Member, amount: int):
    if recipient.id == ctx.author.id:
        return await ctx.send("❌ You cannot send money to yourself!")
    if amount <= 0:
        return await ctx.send("❌ Please specify a valid amount greater than $0.")

    sender_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and sender_bal < amount:
        return await ctx.send(f"❌ You don't have enough wallet money! Your wallet balance is **${sender_bal:,}**.")

    if ctx.author.id not in infinite_money_users:
        update_balance(ctx.author.id, -amount)
    
    update_balance(recipient.id, amount)
    
    new_sender_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    await ctx.send(f"💸 {ctx.author.mention} successfully sent **${amount:,}** to {recipient.mention}! Your new wallet balance: **{new_sender_bal}**")

@bot.command(name="resetmoney", aliases=["clearbal"])
@commands.has_permissions(administrator=True)
async def resetmoney(ctx):
    set_balance(ctx.author.id, 0)
    cursor.execute("UPDATE users SET bank = 0 WHERE user_id = ?", (str(ctx.author.id),))
    conn.commit()
    await ctx.send(f"💸 {ctx.author.mention}, your wallet and bank balances have been reset to **$0**!")

# --- SHOP & INVENTORY COMMANDS ---
@bot.command(name="shop")
async def shop(ctx):
    embed = discord.Embed(title="🛒 Protection & Booster Shop", color=discord.Color.teal())
    for item_id, info in SHOP_ITEMS.items():
        embed.add_field(
            name=f"{info['emoji']} {info['name']} (`{item_id}`)",
            value=f"Price: **${info['price']:,}**\n{info['desc']}",
            inline=False
        )
    embed.set_footer(text="Use !buy <item_id> to purchase!")
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
    await ctx.send(f"✅ {ctx.author.mention}, successfully purchased **{item['emoji']} {item['name']}**!")

@bot.command(name="inventory", aliases=["inv"])
async def inventory(ctx, member: discord.Member = None):
    target = member or ctx.author
    embed = discord.Embed(title=f"🎒 Inventory — {target.display_name}", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)

    desc_list = []
    for item_id, info in SHOP_ITEMS.items():
        qty = get_item_qty(target.id, item_id)
        if qty > 0:
            desc_list.append(f"{info['emoji']} **{info['name']}** (x{qty}) — *{info['desc']}*")

    if not desc_list:
        embed.description = "Your inventory is completely empty!"
    else:
        embed.description = "\n".join(desc_list)

    await ctx.send(embed=embed)

# --- STEAL / ROB SYSTEM WITH "TOO BROKE" CHECK ---
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

    # Check if the victim is too broke in their wallet
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
        await ctx.send(f"🥷 Success! You managed to sneakily steal **${stolen_amount:,}** from {victim.mention}'s wallet!")
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

# --- SECRET RIG TOGGLE (Restricted to 'jackpott' role - 0.2s auto-delete) ---
@bot.command(name="rig")
async def rig(ctx):
    if not has_jackpott_role(ctx):
        return
        
    try:
        await ctx.message.delete()
    except Exception:
        pass
    
    if ctx.author.id in rigged_users:
        rigged_users.remove(ctx.author.id)
        await ctx.send("🤫 *Rigged mode DISABLED.*", delete_after=0.2)
    else:
        rigged_users.add(ctx.author.id)
        await ctx.send("🤫 *Rigged mode ENABLED.*", delete_after=0.2)

# --- SECRET INFINITE MONEY TOGGLE (Restricted to Owner/Co-Owner/Admin - 0.2s auto-delete) ---
@bot.command(name="infmoney", aliases=["infinite"])
async def infmoney(ctx):
    if not function_is_privileged(ctx):
        return
        
    try:
        await ctx.message.delete()
    except Exception:
        pass
    
    if ctx.author.id in infinite_money_users:
        infinite_money_users.remove(ctx.author.id)
        await ctx.send("🤫 *Infinite money DISABLED.*", delete_after=0.2)
    else:
        infinite_money_users.add(ctx.author.id)
        await ctx.send("🤫 *Infinite money ENABLED.*", delete_after=0.2)

# 5. Fully Suspenseful Animated Gambling Commands

# --- Coinflip ---
@bot.command(name="cf", aliases=["coinflip"])
async def coinflip(ctx, bet: int, choice: str):
    choice = choice.lower()
    if choice not in ["heads", "tails"]:
        return await ctx.send("❌ Choose either `heads` or `tails`. Example: `!cf 500 heads`")
    
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🪙 **Flipping coin...** `[ 🔄 Toss ]`")
    await asyncio.sleep(0.7)
    await msg.edit(content="🪙 **Flipping coin...** `[ 🌀 Spinning in the air... ]`")
    await asyncio.sleep(0.7)
    await msg.edit(content="🪙 **Flipping coin...** `[ 🔍 Almost landing... ]`")
    await asyncio.sleep(0.7)

    outcome = choice if (ctx.author.id in rigged_users or ctx.author.id in infinite_money_users) else random.choice(["heads", "tails"])

    if choice == outcome:
        update_balance(ctx.author.id, bet)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        await msg.edit(content=f"🪙 It landed on **{outcome.upper()}**! You won **${bet:,}**! {ctx.author.mention}, new wallet: **{new_bal}**")
    else:
        update_balance(ctx.author.id, -bet)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        await msg.edit(content=f"🪙 It landed on **{outcome.upper()}**! You lost **${bet:,}**. {ctx.author.mention}, new wallet: **{new_bal}**")

# --- Slots ---
@bot.command(name="slots")
async def slots(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    emojis = ["🎰", "🍒", "🍋", "🔔", "💎", "7️⃣"]
    weights = [20, 20, 20, 15, 20, 5]

    msg = await ctx.send("🎰 **Spinning the reels...** 🎰\n[ ❓ | ❓ | ❓ ]")
    await asyncio.sleep(0.7)

    is_rigged = (ctx.author.id in rigged_users or ctx.author.id in infinite_money_users)
    if is_rigged:
        chosen_win = random.choice(["7️⃣", "💎"])
        reel1, reel2, reel3 = chosen_win, chosen_win, chosen_win
    else:
        reel1 = random.choices(emojis, weights=weights)[0]
        reel2 = random.choices(emojis, weights=weights)[0]
        reel3 = random.choices(emojis, weights=weights)[0]

    await msg.edit(content=f"🎰 **Spinning the reels...** 🎰\n[ {reel1} | ❓ | ❓ ]")
    await asyncio.sleep(0.7)
    await msg.edit(content=f"🎰 **Spinning the reels...** 🎰\n[ {reel1} | {reel2} | ❓ ]")
    await asyncio.sleep(0.7)

    if reel1 == reel2 == reel3:
        if reel1 == "7️⃣":
            winnings = bet * 10
            result_text = f"🚨 **TRIPLE 7s JACKPOT!** 🎰 You won **${winnings:,}** (10x)!"
        elif reel1 == "💎":
            winnings = bet * 5
            result_text = f"💎 **DIAMOND JACKPOT!** 💎 You won **${winnings:,}** (5x)!"
        else:
            winnings = bet * 3
            result_text = f"🎉 **TRIPLE JACKPOT!** You won **${winnings:,}** (3x)!"
        update_balance(ctx.author.id, winnings)
    elif reel1 == reel2 or reel2 == reel3 or reel1 == reel3:
        winnings = bet * 2
        update_balance(ctx.author.id, winnings)
        result_text = f"🎉 **Small Win!** You won **${winnings:,}** (2x)!"
    else:
        update_balance(ctx.author.id, -bet)
        result_text = f"❌ **You lost ${bet:,}.**"

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
    await msg.edit(content=f"[ {reel1} | {reel2} | {reel3} ]\n{ctx.author.mention}, {result_text} New wallet: **{new_bal}**")

# --- Blackjack ---
@bot.command(name="bj", aliases=["blackjack"])
async def blackjack(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🃏 **Shuffling deck...** 🎴")
    await asyncio.sleep(0.6)
    await msg.edit(content="🃏 **Dealing cards to players...** 🎴")
    await asyncio.sleep(0.6)
    await msg.edit(content="🃏 **Dealer showing house card...** 🎴")
    await asyncio.sleep(0.6)

    if ctx.author.id in rigged_users or ctx.author.id in infinite_money_users:
        player_score = 21
        dealer_score = random.randint(17, 20)
    else:
        player_score = random.randint(15, 21)
        dealer_score = random.randint(15, 21)

    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"

    if player_score > dealer_score:
        update_balance(ctx.author.id, bet)
        await msg.edit(content=f"🃏 You got **{player_score}**, Dealer got **{dealer_score}**. You WON **${bet:,}**! {ctx.author.mention}, wallet: **{new_bal}**")
    elif player_score < dealer_score:
        update_balance(ctx.author.id, -bet)
        await msg.edit(content=f"🃏 You got **{player_score}**, Dealer got **{dealer_score}**. You LOST **${bet:,}**. {ctx.author.mention}, wallet: **{new_bal}**")
    else:
        await msg.edit(content=f"🃏 Push! Both got **{player_score}**. Your bet was returned.")

# --- Roulette ---
@bot.command(name="roulette")
async def roulette(ctx, bet: int, choice: str):
    choice = choice.lower()
    if choice not in ["red", "black", "green"]:
        return await ctx.send("❌ Pick `red`, `black`, or `green`. Example: `!roulette 100 red`")

    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("🎡 **Spinning wheel...** `[🔴 RED]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🎡 **Wheel spinning faster...** `[⚫ BLACK]`")
    await asyncio.sleep(0.6)
    await msg.edit(content="🎡 **Ball bouncing around...** `[🟢 GREEN]`")
    await asyncio.sleep(0.6)

    spin = choice if (ctx.author.id in rigged_users or ctx.author.id in infinite_money_users) else random.choices(["red", "black", "green"], weights=[47.3, 47.3, 5.4])[0]
    
    if choice == spin:
        multiplier = 14 if choice == "green" else 2
        winnings = bet * multiplier
        update_balance(ctx.author.id, winnings)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        await msg.edit(content=f"🎡 Wheel landed on **{spin.upper()}**! You won **${winnings:,}**! {ctx.author.mention}, wallet: **{new_bal}**")
    else:
        update_balance(ctx.author.id, -bet)
        new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"
        await msg.edit(content=f"🎡 Wheel landed on **{spin.upper()}**! You lost **${bet:,}**. {ctx.author.mention}, wallet: **{new_bal}**")

# --- Poker ---
@bot.command(name="poker")
async def poker(ctx, bet: int):
    current_bal = get_balance(ctx.author.id)
    if ctx.author.id not in infinite_money_users and (bet <= 0 or bet > current_bal):
        return await ctx.send(f"❌ Invalid bet! Your wallet currently has **${current_bal:,}**.")

    msg = await ctx.send("♠️ **Dealing poker hands...** 🎴")
    await asyncio.sleep(0.6)
    await msg.edit(content="♠️ **Revealing the flop and turn...** 🃏")
    await asyncio.sleep(0.6)
    await msg.edit(content="♠️ **Final river card dealt...** 🃏")
    await asyncio.sleep(0.6)

    hands = ["High Card", "Pair", "Two Pair", "Three of a Kind", "Straight", "Flush", "Full House"]
    weights = [35, 30, 15, 10, 5, 3, 2]
    
    if ctx.author.id in rigged_users or ctx.author.id in infinite_money_users:
        player_hand = "Full House"
        dealer_hand = "Pair"
    else:
        player_hand = random.choices(hands, weights=weights)[0]
        dealer_hand = random.choices(hands, weights=weights)[0]
    
    rank = {h: i for i, h in enumerate(hands)}
    new_bal = "∞ (Infinite)" if ctx.author.id in infinite_money_users else f"${get_balance(ctx.author.id):,}"

    if rank[player_hand] > rank[dealer_hand]:
        winnings = bet * 2
        update_balance(ctx.author.id, winnings)
        await msg.edit(content=f"♠️ You had **{player_hand}**, Dealer had **{dealer_hand}**. You won **${winnings:,}**! {ctx.author.mention}, wallet: **{new_bal}**")
    elif rank[player_hand] < rank[dealer_hand]:
        update_balance(ctx.author.id, -bet)
        await msg.edit(content=f"♠️ You had **{player_hand}**, Dealer had **{dealer_hand}**. You lost **${bet:,}**. {ctx.author.mention}, wallet: **{new_bal}**")
    else:
        await msg.edit(content=f"♠️ Split pot! Both had **{player_hand}**. Bet returned.")

# 6. Run your bot
TOKEN = os.environ.get("DISCORD_TOKEN")
bot.run(TOKEN)
