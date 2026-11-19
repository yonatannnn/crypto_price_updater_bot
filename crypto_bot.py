import asyncio
import os
import time
from datetime import datetime
import requests
from telethon import TelegramClient, events, Button
import sqlite3
from dotenv import load_dotenv
import trades as tr

# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Coins shown to users who haven't built their own watchlist yet
DEFAULT_WATCHLIST = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ETHFIUSDT"]
MAX_WATCHLIST = 20

# SQLite setup
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    target_price REAL NOT NULL,
    direction TEXT NOT NULL,
    triggered INTEGER NOT NULL DEFAULT 0,
    repeat INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts (triggered, user_id, symbol);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (user_id, symbol)
);
""")
db.commit()
tr.init_db(db)

# ---------------------------------------------------------------------------
# Prices
# ---------------------------------------------------------------------------
# One Binance request returns every spot price (~3,700 symbols). We cache it for
# a few seconds; the same data is used both for prices and to validate symbols.
_price_cache = {"time": 0.0, "prices": {}}
PRICE_CACHE_SECONDS = 5

def _fetch_all_prices_sync():
    try:
        response = requests.get("https://api.binance.com/api/v3/ticker/price", timeout=10)
        response.raise_for_status()
        return {item["symbol"]: float(item["price"]) for item in response.json()}
    except Exception as e:
        print(f"⚠️ Price fetch failed: {e}")
        return None

async def get_all_prices():
    """All Binance prices, cached briefly. Falls back to the last good data on errors."""
    if time.time() - _price_cache["time"] > PRICE_CACHE_SECONDS:
        prices = await asyncio.to_thread(_fetch_all_prices_sync)
        if prices:
            _price_cache["prices"] = prices
            _price_cache["time"] = time.time()
    return _price_cache["prices"]

async def fetch_prices(symbols):
    prices = await get_all_prices()
    return {s: prices.get(s) for s in symbols}

async def resolve_symbol(raw):
    """Turn user input like 'btc', 'BTCUSDT' or 'eth/usdt' into a valid Binance symbol, or None."""
    s = raw.upper().replace("/", "").replace("-", "").strip()
    if not s:
        return None
    prices = await get_all_prices()
    if not prices:
        return None
    if s in prices:
        return s
    if s + "USDT" in prices:
        return s + "USDT"
    return None

def fmt_price(p):
    """Readable price for both BTC (63,795.67) and PEPE (0.00000381)."""
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:,.4f}"
    return f"{p:.10f}".rstrip("0").rstrip(".")

def coin_emoji(sym):
    return "🟢" if sym.startswith("BTC") else "🔵" if sym.startswith("ETH") else "🟣"

# ---------------------------------------------------------------------------
# Watchlist helpers
# ---------------------------------------------------------------------------
def get_watchlist(user_id):
    rows = db.execute(
        "SELECT symbol FROM watchlist WHERE user_id = ? ORDER BY rowid", (user_id,)
    ).fetchall()
    return [r["symbol"] for r in rows] or list(DEFAULT_WATCHLIST)

def ensure_watchlist_seeded(user_id):
    """Copy the defaults into the user's own list the first time they customise it."""
    has_rows = db.execute("SELECT 1 FROM watchlist WHERE user_id = ? LIMIT 1", (user_id,)).fetchone()
    if not has_rows:
        now = datetime.utcnow().isoformat()
        db.executemany(
            "INSERT OR IGNORE INTO watchlist (user_id, symbol, added_at) VALUES (?, ?, ?)",
            [(user_id, s, now) for s in DEFAULT_WATCHLIST],
        )
        db.commit()

async def add_to_watchlist(user_id, raw_symbols):
    """Returns a list of result lines for the user."""
    ensure_watchlist_seeded(user_id)
    lines = []
    for raw in raw_symbols:
        symbol = await resolve_symbol(raw)
        if not symbol:
            lines.append(f"❌ <b>{raw.upper()}</b> isn't a Binance symbol")
            continue
        count = db.execute("SELECT COUNT(*) FROM watchlist WHERE user_id = ?", (user_id,)).fetchone()[0]
        if count >= MAX_WATCHLIST:
            lines.append(f"⚠️ Watchlist is full ({MAX_WATCHLIST} coins). Remove one first.")
            break
        cur = db.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, symbol, added_at) VALUES (?, ?, ?)",
            (user_id, symbol, datetime.utcnow().isoformat()),
        )
        db.commit()
        lines.append(f"⭐ Added <b>{symbol}</b>" if cur.rowcount else f"ℹ️ <b>{symbol}</b> is already in your watchlist")
    return lines

def remove_from_watchlist(user_id, symbol):
    ensure_watchlist_seeded(user_id)
    cur = db.execute("DELETE FROM watchlist WHERE user_id = ? AND symbol = ?", (user_id, symbol))
    db.commit()
    return cur.rowcount == 1

# ---------------------------------------------------------------------------
# Alert helpers
# ---------------------------------------------------------------------------
async def create_alerts(user_id, symbol, price_strings, is_repeating):
    """Create alerts and return the confirmation message (HTML)."""
    current_price = (await fetch_prices([symbol])).get(symbol)
    if not current_price:
        return "⚠️ Could not fetch the current price. Try again in a moment."

    confirmations = []
    for price_str in price_strings:
        try:
            price = float(price_str.replace(",", ""))
            if price <= 0:
                raise ValueError
        except ValueError:
            confirmations.append(f"❌ Invalid price: {price_str}")
            continue
        direction = "above" if price > current_price else "below"
        db.execute(
            "INSERT INTO alerts (user_id, symbol, target_price, direction, triggered, repeat, created_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (user_id, symbol, price, direction, int(is_repeating), datetime.utcnow().isoformat()),
        )
        db.commit()
        arrow = "🔼" if direction == "above" else "🔽"
        confirmations.append(f"{arrow} <code>${fmt_price(price)}</code>")

    repeat_text = "\n🔁 Repeating alerts." if is_repeating else ""
    return (
        f"📍 Alerts for <b>{symbol}</b>:\n" + "\n".join(confirmations) +
        f"\n\nCurrent: <code>${fmt_price(current_price)}</code>" + repeat_text
    )

def parse_prices_and_repeat(tokens):
    is_repeating = bool(tokens) and tokens[-1].lower() == "u"
    return (tokens[:-1] if is_repeating else tokens), is_repeating

# ---------------------------------------------------------------------------
# Message builders (shared by commands and buttons)
# ---------------------------------------------------------------------------
MENU_BUTTONS = [
    [Button.inline("💰 Prices", b"m:prices"), Button.inline("🔔 My alerts", b"m:alerts")],
    [Button.inline("⭐ Watchlist", b"m:watch"), Button.inline("➕ Add alert", b"m:add")],
    [Button.inline("📈 New trade", b"t:new"), Button.inline("📒 My trades", b"t:list")],
    [Button.inline("📜 Trade history", b"t:hist"), Button.inline("❓ Help", b"m:help")],
]
BACK_BUTTON = [Button.inline("🏠 Menu", b"m:menu")]

def menu_text():
    return "🏠 <b>Main menu</b>\nChoose an option below:"

async def prices_view(user_id, single=None):
    symbols = [single] if single else get_watchlist(user_id)
    prices = await fetch_prices(symbols)
    msg = f"📊 <b>Crypto Price Snapshot</b> <i>({datetime.utcnow().strftime('%H:%M')} UTC)</i>\n\n"
    for sym in symbols:
        val = prices.get(sym)
        if val:
            msg += f"{coin_emoji(sym)} <b>{sym}</b>: <code>${fmt_price(val)}</code>\n"
        else:
            msg += f"❌ <b>{sym}</b>: <i>Error fetching price</i>\n"
    refresh_data = f"m:prices:{single}" if single else "m:prices"
    buttons = [[Button.inline("🔄 Refresh", refresh_data.encode())], BACK_BUTTON]
    return msg, buttons

def alerts_view(user_id):
    alerts = db.execute(
        "SELECT * FROM alerts WHERE user_id = ? AND triggered = 0 ORDER BY symbol, target_price",
        (user_id,),
    ).fetchall()
    if not alerts:
        return "📭 You have no active alerts.", [[Button.inline("➕ Add alert", b"m:add")], BACK_BUTTON]
    lines = ["🔔 <b>Your active alerts</b>\n"]
    buttons = []
    for a in alerts:
        arrow = "🔼" if a["direction"] == "above" else "🔽"
        repeat = " 🔁" if a["repeat"] else ""
        label = f"{a['symbol']} {arrow} ${fmt_price(a['target_price'])}{repeat}"
        lines.append(f"• {label}")
        buttons.append([Button.inline(f"❌ {label}", f"cancel_{a['id']}".encode())])
    buttons.append(BACK_BUTTON)
    return "\n".join(lines), buttons

def watchlist_view(user_id):
    symbols = get_watchlist(user_id)
    msg = (
        "⭐ <b>Your watchlist</b>\n"
        "These coins appear in 💰 Prices. Tap one to remove it.\n\n" +
        "\n".join(f"• {s}" for s in symbols)
    )
    buttons = [[Button.inline(f"❌ {s}", f"w:rm:{s}".encode())] for s in symbols]
    buttons.append([Button.inline("➕ Add coin", b"w:add")])
    buttons.append(BACK_BUTTON)
    return msg, buttons

def help_text():
    return (
        "<b>🛠 Available Commands:</b>\n\n"
        "<b>/menu</b> — Show the button menu\n"
        "<b>/price</b> — Prices for your watchlist\n"
        "<b>/price SYMBOL</b> — Price of any coin, e.g. <code>/price PEPE</code>\n"
        "<b>/watch SYMBOL ...</b> — Add coins to your watchlist\n"
        "<b>/unwatch SYMBOL</b> — Remove a coin from your watchlist\n"
        "<b>/watchlist</b> — Show and edit your watchlist\n"
        "<b>/sa SYMBOL PRICE [PRICE...] [u]</b> — Set alerts. Add 'u' to repeat.\n"
        "<code>/sa BTC 69000 u</code>\n"
        "<b>/la</b> — List your active alerts with cancel buttons\n"
        "<b>/ca SYMBOL</b> — Cancel all alerts for a coin\n\n"
        "<b>📈 Simulated trading</b>\n"
        "<b>/trade</b> — Open a paper trade step by step\n"
        "<code>/trade BTC long 100 10x entry 65000 tp 70000 sl 62000 target 50</code>\n"
        "  • <b>100</b> = margin in USDT, <b>10x</b> = leverage\n"
        "  • <b>entry</b> optional (default: market price)\n"
        "  • <b>tp</b>, <b>sl</b>, <b>target</b> optional; target = USDT or % (e.g. <code>20%</code>)\n"
        "<b>/trades</b> — Open trades with live PnL\n"
        "<b>/close ID</b> — Close a trade at market price\n"
        "<b>/history</b> — Closed trades and total PnL\n\n"
        "<b>/help</b> — Show this help message\n\n"
        "<i>💡 Any Binance coin works. 'BTC' means BTCUSDT. Trades are simulated, no real money.</i>"
    )

# ---------------------------------------------------------------------------
# Conversation state for button flows (in memory; cleared on restart)
# ---------------------------------------------------------------------------
pending = {}  # user_id -> ("alert", symbol) | ("watch", None) | ("trade", draft_dict)

# ---------------------------------------------------------------------------
# Trade wizard: coin -> side -> entry -> margin -> leverage -> tp -> sl -> target -> confirm
# ---------------------------------------------------------------------------
TRADE_STEPS = ["entry", "margin", "leverage", "tp", "sl", "target"]
CANCEL_ROW = [Button.inline("✖️ Cancel", b"t:x")]

def coin_picker(user_id, prefix):
    rows, row = [], []
    for s in get_watchlist(user_id):
        row.append(Button.inline(s, f"{prefix}{s}".encode()))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows

def draft_summary(d):
    parts = [f"<b>{d['symbol']}</b>"]
    if d.get("side"):
        parts.append(tr.side_label(d["side"]))
    if d.get("leverage") and "leverage" in d.get("_done", []):
        parts.append(tr.fmt_lev(d["leverage"]))
    lines = [" ".join(parts)]
    if d.get("entry"):
        lines.append(f"Entry: <code>${fmt_price(d['entry'])}</code>")
    if d.get("margin"):
        lines.append(f"Margin: <code>${d['margin']:,.2f}</code>")
    return "\n".join(lines)

async def trade_step_view(d):
    """Text + buttons asking for the draft's current step."""
    step = d["_step"]
    head = "📈 <b>New simulated trade</b>\n" + draft_summary(d) + "\n\n"
    if step == "entry":
        price = (await fetch_prices([d["symbol"]])).get(d["symbol"])
        d["_market"] = price
        now = f" (now <code>${fmt_price(price)}</code>)" if price else ""
        return head + f"Send your <b>entry price</b>{now}, or use the market price:", [
            [Button.inline("📍 Use market price", b"t:mkt")], CANCEL_ROW]
    if step == "margin":
        return head + "How much <b>margin</b> (USDT)? Send a number, e.g. <code>100</code>", [
            [Button.inline("$10", b"t:m:10"), Button.inline("$50", b"t:m:50"), Button.inline("$100", b"t:m:100")],
            CANCEL_ROW]
    if step == "leverage":
        return head + "Choose <b>leverage</b> (or send a number like <code>15</code>):", [
            [Button.inline(f"{x}x", f"t:lev:{x}".encode()) for x in (1, 3, 5)],
            [Button.inline(f"{x}x", f"t:lev:{x}".encode()) for x in (10, 20, 50)],
            CANCEL_ROW]
    skip = [Button.inline("⏭ Skip", b"t:skip")]
    if step == "tp":
        hint = "above" if d["side"] == "long" else "below"
        return head + f"Send your <b>take-profit</b> price ({hint} entry), or skip:", [skip, CANCEL_ROW]
    if step == "sl":
        hint = "below" if d["side"] == "long" else "above"
        liq = tr.calc_liq(d["side"], d["entry"], d["leverage"])
        liq_txt = f"\n💀 Est. liquidation: <code>${fmt_price(liq)}</code>" if liq else ""
        return head + f"Send your <b>stop-loss</b> price ({hint} entry), or skip:{liq_txt}", [skip, CANCEL_ROW]
    if step == "target":
        return head + ("💰 Want a reminder at a <b>profit target</b>?\n"
                       "Send USDT like <code>50</code> or % of margin like <code>20%</code>, or skip:"), [skip, CANCEL_ROW]
    raise ValueError(step)

def advance(d):
    """Move the wizard to the next step; returns False when all steps are done."""
    d.setdefault("_done", []).append(d["_step"])
    idx = TRADE_STEPS.index(d["_step"]) + 1
    if idx >= len(TRADE_STEPS):
        d["_step"] = "confirm"
        return False
    d["_step"] = TRADE_STEPS[idx]
    return True

def confirm_view(d):
    errors, warnings = tr.finalize_draft(d)
    if errors:
        return None, errors
    text = "📈 <b>Confirm simulated trade</b>\n\n" + tr.trade_card(d, price=d.get("_market"))
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    return (text, [[Button.inline("✅ Open trade", b"t:ok"), Button.inline("✖️ Cancel", b"t:x")]]), []

async def show_trade_step(event, d, edit):
    """Show the next wizard step, or the confirmation screen at the end."""
    if d["_step"] == "confirm":
        view, errors = confirm_view(d)
        if errors:
            # Restart at TP so the user can fix TP/SL/target
            d["_step"] = "tp"
            d["_done"] = [s for s in d["_done"] if s not in ("tp", "sl", "target")]
            for k in ("tp", "sl", "target_raw", "target_profit", "target_price"):
                d.pop(k, None)
            text, buttons = await trade_step_view(d)
            text = "❌ " + "\n❌ ".join(errors) + "\n\n" + text
        else:
            text, buttons = view
    else:
        text, buttons = await trade_step_view(d)
    if edit:
        await event.edit(text, parse_mode='HTML', buttons=buttons)
    else:
        await event.respond(text, parse_mode='HTML', buttons=buttons)

async def handle_trade_text(event, d):
    """A typed reply during the trade wizard."""
    text = event.raw_text.strip()
    step = d["_step"]
    if step == "coin":
        symbol = await resolve_symbol(text)
        if not symbol:
            pending[event.sender_id] = ("trade", d)
            await event.respond(f"❌ <b>{text.upper()}</b> isn't a Binance symbol. Try again:", parse_mode='HTML')
            return
        d["symbol"] = symbol
        d["_step"] = "side"
        pending[event.sender_id] = ("trade", d)
        text, buttons = side_view(d)
        await event.respond(text, parse_mode='HTML', buttons=buttons)
        return
    if step == "side":
        side = tr.SIDES.get(text.lower())
        pending[event.sender_id] = ("trade", d)
        if not side:
            await event.respond("Please tap 🟢 Long or 🔴 Short.")
            return
        d["side"] = side
        d["_step"] = "entry"
        await show_trade_step(event, d, edit=False)
        return
    try:
        if step == "entry":
            d["entry"] = tr.parse_number(text)
        elif step == "margin":
            d["margin"] = tr.parse_number(text)
        elif step == "leverage":
            d["leverage"] = tr.parse_leverage(text)
        elif step == "tp":
            d["tp"] = tr.parse_number(text)
        elif step == "sl":
            d["sl"] = tr.parse_number(text)
        elif step == "target":
            d["target_raw"] = text
            tr.parse_target(text, d["margin"])
        else:
            await event.respond("Use the buttons above to confirm or cancel.")
            pending[event.sender_id] = ("trade", d)
            return
    except ValueError:
        pending[event.sender_id] = ("trade", d)
        await event.respond("⚠️ That doesn't look right. Please send a positive number"
                            + (" (1–125)." if step == "leverage" else "."))
        return
    advance(d)
    pending[event.sender_id] = ("trade", d)
    await show_trade_step(event, d, edit=False)

def coin_view(user_id):
    return ("📈 <b>New simulated trade</b>\nPick a coin, or send any symbol (e.g. <code>PEPE</code>):",
            coin_picker(user_id, "t:c:") + [CANCEL_ROW])

def side_view(d):
    return ("📈 <b>New simulated trade</b>\n" + draft_summary(d) + "\n\nLong or short?",
            [[Button.inline("🟢 Long", b"t:side:long"), Button.inline("🔴 Short", b"t:side:short")], CANCEL_ROW])

def open_trades_view(user_id, prices):
    rows = db.execute(
        "SELECT * FROM trades WHERE user_id=? AND status='open' ORDER BY id", (user_id,)
    ).fetchall()
    if not rows:
        return "📒 No open trades.", [[Button.inline("📈 New trade", b"t:new")], BACK_BUTTON]
    cards, buttons, total = [], [], 0.0
    for t in rows:
        price = prices.get(t["symbol"])
        cards.append(tr.trade_card(t, price=price))
        if price:
            total += tr.calc_pnl(t["side"], t["entry"], t["qty"], price)
        buttons.append([Button.inline(f"✋ Close #{t['id']} {t['symbol']}", f"t:close:{t['id']}".encode())])
    buttons.append([Button.inline("🔄 Refresh", b"t:list"), Button.inline("📜 History", b"t:hist")])
    buttons.append(BACK_BUTTON)
    text = "📒 <b>Open trades</b>\n\n" + "\n\n".join(cards) + f"\n\nTotal open PnL: <b>{tr.fmt_usd(total)}</b>"
    return text, buttons

async def close_trade_now(user_id, trade_id):
    t = db.execute(
        "SELECT * FROM trades WHERE id=? AND user_id=? AND status='open'", (trade_id, user_id)
    ).fetchone()
    if not t:
        return None
    price = (await fetch_prices([t["symbol"]])).get(t["symbol"])
    if not price:
        return "⚠️ Could not fetch the current price. Try again."
    pnl = tr.close_trade(db, t, price, "manual")
    return tr.close_message(t, price, "manual", pnl)

# Initialize Telethon bot
bot = TelegramClient('crypto_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage(pattern=r'^/start\b'))
async def handle_start(event):
    user_id = event.sender_id
    pending.pop(user_id, None)
    cur = db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    db.commit()
    if cur.rowcount:
        print(f"✅ New user added: {user_id}")
    await event.respond(
        "You're now subscribed to crypto updates!\n\n"
        "starting from the next <b>hour or half hour</b> mark.\n\n"
        "✅ Track any Binance coin: build your own ⭐ watchlist and set 🔔 price alerts.\n\n"
        "Use the buttons below, or <b>/help</b> for all commands.",
        parse_mode='HTML',
        buttons=MENU_BUTTONS,
    )

@bot.on(events.NewMessage(pattern=r'^/menu\b'))
async def handle_menu(event):
    pending.pop(event.sender_id, None)
    await event.respond(menu_text(), parse_mode='HTML', buttons=MENU_BUTTONS)

@bot.on(events.NewMessage(pattern=r'^/help\b'))
async def handle_help(event):
    await event.respond(help_text(), parse_mode='HTML', buttons=[BACK_BUTTON])

@bot.on(events.NewMessage(pattern=r'^/price(?:@\w+)?(?:\s+(\S+))?\s*$'))
async def handle_price_request(event):
    raw = event.pattern_match.group(1)
    single = None
    if raw:
        single = await resolve_symbol(raw)
        if not single:
            await event.respond(f"❌ <b>{raw.upper()}</b> isn't a Binance symbol.", parse_mode='HTML')
            return
    msg, buttons = await prices_view(event.sender_id, single)
    await event.respond(msg, parse_mode='HTML', buttons=buttons)

@bot.on(events.NewMessage(pattern=r'^/watch(?:@\w+)?(?:\s+(.+))?$'))
async def handle_watch(event):
    args = (event.pattern_match.group(1) or "").split()
    if not args:
        await event.respond("Usage: <code>/watch SOL PEPE DOGE</code>", parse_mode='HTML')
        return
    lines = await add_to_watchlist(event.sender_id, args)
    await event.respond("\n".join(lines), parse_mode='HTML',
                        buttons=[[Button.inline("⭐ Watchlist", b"m:watch")], BACK_BUTTON])

@bot.on(events.NewMessage(pattern=r'^/unwatch(?:@\w+)?(?:\s+(\S+))?'))
async def handle_unwatch(event):
    raw = event.pattern_match.group(1)
    if not raw:
        await event.respond("Usage: <code>/unwatch PEPE</code>", parse_mode='HTML')
        return
    symbol = await resolve_symbol(raw) or raw.upper()
    if remove_from_watchlist(event.sender_id, symbol):
        await event.respond(f"🗑 Removed <b>{symbol}</b> from your watchlist.", parse_mode='HTML')
    else:
        await event.respond(f"⚠️ <b>{symbol}</b> isn't in your watchlist.", parse_mode='HTML')

@bot.on(events.NewMessage(pattern=r'^/watchlist\b'))
async def handle_watchlist(event):
    msg, buttons = watchlist_view(event.sender_id)
    await event.respond(msg, parse_mode='HTML', buttons=buttons)

@bot.on(events.NewMessage(pattern=r'^/sa(?:@\w+)?\b'))
async def set_price_alerts(event):
    parts = event.raw_text.split()
    if len(parts) < 3:
        await event.respond(
            "⚠️ Please provide a symbol and at least one price.\n"
            "Example: <code>/sa ETH 2800 2769 2390</code>", parse_mode='HTML')
        return
    symbol = await resolve_symbol(parts[1])
    if not symbol:
        await event.respond(f"❌ <b>{parts[1].upper()}</b> isn't a Binance symbol.", parse_mode='HTML')
        return
    price_values, is_repeating = parse_prices_and_repeat(parts[2:])
    if not price_values:
        await event.respond("⚠️ Please provide at least one price.")
        return
    msg = await create_alerts(event.sender_id, symbol, price_values, is_repeating)
    await event.respond(msg, parse_mode='HTML')

@bot.on(events.NewMessage(pattern=r'^/la\b'))
async def list_alerts(event):
    msg, buttons = alerts_view(event.sender_id)
    await event.respond(msg, parse_mode='HTML', buttons=buttons)

@bot.on(events.NewMessage(pattern=r'^/ca(?:@\w+)?\s+(\S+)'))
async def cancel_alert_by_symbol(event):
    user_id = event.sender_id
    raw = event.pattern_match.group(1)
    symbol = await resolve_symbol(raw) or raw.upper()
    cur = db.execute(
        "DELETE FROM alerts WHERE user_id = ? AND symbol = ? AND triggered = 0",
        (user_id, symbol)
    )
    db.commit()
    if cur.rowcount:
        await event.respond(f"✅ Cancelled {cur.rowcount} alert(s) for <b>{symbol}</b>", parse_mode='HTML')
    else:
        await event.respond(f"⚠️ No active alerts found for <b>{symbol}</b>", parse_mode='HTML')

@bot.on(events.NewMessage(pattern=r'^/trade(?:@\w+)?(?:\s+(.+))?$'))
async def handle_trade_command(event):
    user_id = event.sender_id
    args = (event.pattern_match.group(1) or "").split()
    if not args:
        pending[user_id] = ("trade", {"_step": "coin"})
        text, buttons = coin_view(user_id)
        await event.respond(text, parse_mode='HTML', buttons=buttons)
        return
    try:
        raw_symbol, d = tr.parse_trade_command(args)
    except ValueError as e:
        await event.respond(
            f"❌ {e}\n\nExample:\n<code>/trade BTC long 100 10x entry 65000 tp 70000 sl 62000 target 50</code>\n"
            "Or just send <b>/trade</b> to be guided step by step.", parse_mode='HTML')
        return
    symbol = await resolve_symbol(raw_symbol)
    if not symbol:
        await event.respond(f"❌ <b>{raw_symbol.upper()}</b> isn't a Binance symbol.", parse_mode='HTML')
        return
    d["symbol"] = symbol
    market = (await fetch_prices([symbol])).get(symbol)
    d["_market"] = market
    if "entry" not in d:
        if not market:
            await event.respond("⚠️ Could not fetch the market price. Add <code>entry 65000</code> or try again.",
                                parse_mode='HTML')
            return
        d["entry"] = market
    d["_step"] = "confirm"
    d["_done"] = ["leverage"]
    view, errors = confirm_view(d)
    if errors:
        await event.respond("❌ " + "\n❌ ".join(errors), parse_mode='HTML')
        return
    pending[user_id] = ("trade", d)
    text, buttons = view
    await event.respond(text, parse_mode='HTML', buttons=buttons)

@bot.on(events.NewMessage(pattern=r'^/trades\b'))
async def handle_trades(event):
    rows = db.execute("SELECT DISTINCT symbol FROM trades WHERE user_id=? AND status='open'",
                      (event.sender_id,)).fetchall()
    prices = await fetch_prices([r["symbol"] for r in rows])
    text, buttons = open_trades_view(event.sender_id, prices)
    await event.respond(text, parse_mode='HTML', buttons=buttons)

@bot.on(events.NewMessage(pattern=r'^/close(?:@\w+)?(?:\s+#?(\d+))?'))
async def handle_close(event):
    if not event.pattern_match.group(1):
        await event.respond("Usage: <code>/close 3</code> (see IDs in /trades)", parse_mode='HTML')
        return
    msg = await close_trade_now(event.sender_id, int(event.pattern_match.group(1)))
    await event.respond(msg or "⚠️ No open trade with that ID.", parse_mode='HTML')

@bot.on(events.NewMessage(pattern=r'^/history\b'))
async def handle_history(event):
    await event.respond(tr.history_text(db, event.sender_id), parse_mode='HTML', buttons=[BACK_BUTTON])

# ---------------------------------------------------------------------------
# Plain text replies for the button flows (Add alert / Add coin / New trade)
# ---------------------------------------------------------------------------
@bot.on(events.NewMessage(func=lambda e: e.is_private and e.raw_text and not e.raw_text.startswith("/")))
async def handle_pending_reply(event):
    state = pending.pop(event.sender_id, None)
    if not state:
        await event.respond("Use the menu below or /help.", buttons=MENU_BUTTONS)
        return
    kind, symbol = state
    if kind == "trade":
        await handle_trade_text(event, symbol)  # 'symbol' holds the trade draft here
        return
    tokens = event.raw_text.replace(",", " ").split()
    if kind == "alert":
        price_values, is_repeating = parse_prices_and_repeat(tokens)
        if not price_values:
            pending[event.sender_id] = state
            await event.respond("⚠️ Send at least one price, e.g. <code>65000</code>", parse_mode='HTML')
            return
        msg = await create_alerts(event.sender_id, symbol, price_values, is_repeating)
        await event.respond(msg, parse_mode='HTML',
                            buttons=[[Button.inline("🔔 My alerts", b"m:alerts")], BACK_BUTTON])
    elif kind == "watch":
        lines = await add_to_watchlist(event.sender_id, tokens)
        msg, buttons = watchlist_view(event.sender_id)
        await event.respond("\n".join(lines) + "\n\n" + msg, parse_mode='HTML', buttons=buttons)

# ---------------------------------------------------------------------------
# Button callbacks
# ---------------------------------------------------------------------------
@bot.on(events.CallbackQuery(pattern=rb'^m:'))
async def handle_menu_button(event):
    user_id = event.sender_id
    data = event.data.decode()
    parts = data.split(":")
    action = parts[1]

    if action != "add":
        pending.pop(user_id, None)

    if action == "menu":
        await event.edit(menu_text(), parse_mode='HTML', buttons=MENU_BUTTONS)
    elif action == "prices":
        single = parts[2] if len(parts) > 2 else None
        msg, buttons = await prices_view(user_id, single)
        try:
            await event.edit(msg, parse_mode='HTML', buttons=buttons)
        except Exception:
            pass  # message unchanged (refresh pressed twice in the same minute)
        await event.answer("🔄 Updated")
        return
    elif action == "alerts":
        msg, buttons = alerts_view(user_id)
        await event.edit(msg, parse_mode='HTML', buttons=buttons)
    elif action == "watch":
        msg, buttons = watchlist_view(user_id)
        await event.edit(msg, parse_mode='HTML', buttons=buttons)
    elif action == "help":
        await event.edit(help_text(), parse_mode='HTML', buttons=[BACK_BUTTON])
    elif action == "add":
        symbols = get_watchlist(user_id)
        rows, row = [], []
        for s in symbols:
            row.append(Button.inline(s, f"a:{s}".encode()))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(BACK_BUTTON)
        await event.edit(
            "➕ <b>New alert</b>\nPick a coin (add more coins via ⭐ Watchlist):",
            parse_mode='HTML', buttons=rows)
    await event.answer()

@bot.on(events.CallbackQuery(pattern=rb'^a:'))
async def handle_alert_coin_button(event):
    symbol = event.data.decode().split(":", 1)[1]
    price = (await fetch_prices([symbol])).get(symbol)
    pending[event.sender_id] = ("alert", symbol)
    current = f"\nCurrent price: <code>${fmt_price(price)}</code>" if price else ""
    await event.edit(
        f"🔔 <b>{symbol}</b>{current}\n\n"
        "Send the target price(s) as a message, e.g.\n"
        "<code>65000</code> or <code>65000 70000 u</code> (u = repeat)",
        parse_mode='HTML', buttons=[BACK_BUTTON])
    await event.answer()

@bot.on(events.CallbackQuery(pattern=rb'^w:'))
async def handle_watchlist_button(event):
    user_id = event.sender_id
    parts = event.data.decode().split(":")
    if parts[1] == "rm":
        symbol = parts[2]
        removed = remove_from_watchlist(user_id, symbol)
        msg, buttons = watchlist_view(user_id)
        await event.edit(msg, parse_mode='HTML', buttons=buttons)
        await event.answer(f"🗑 Removed {symbol}" if removed else "Already removed")
    elif parts[1] == "add":
        pending[user_id] = ("watch", None)
        await event.edit(
            "⭐ <b>Add coins</b>\nSend coin symbols as a message, e.g. <code>PEPE DOGE XRP</code>",
            parse_mode='HTML', buttons=[BACK_BUTTON])
        await event.answer()

@bot.on(events.CallbackQuery(pattern=rb'^t:'))
async def handle_trade_button(event):
    user_id = event.sender_id
    parts = event.data.decode().split(":")
    action = parts[1]
    state = pending.get(user_id)
    d = state[1] if state and state[0] == "trade" else None

    if action == "new":
        pending[user_id] = ("trade", {"_step": "coin"})
        text, buttons = coin_view(user_id)
        await event.edit(text, parse_mode='HTML', buttons=buttons)
    elif action == "list":
        rows = db.execute("SELECT DISTINCT symbol FROM trades WHERE user_id=? AND status='open'",
                          (user_id,)).fetchall()
        prices = await fetch_prices([r["symbol"] for r in rows])
        text, buttons = open_trades_view(user_id, prices)
        try:
            await event.edit(text, parse_mode='HTML', buttons=buttons)
        except Exception:
            pass  # nothing changed since last refresh
    elif action == "hist":
        await event.edit(tr.history_text(db, user_id), parse_mode='HTML', buttons=[BACK_BUTTON])
    elif action == "close":
        tid = int(parts[2])
        await event.edit(f"✋ Close trade #{tid} at the current market price?", buttons=[
            [Button.inline("✅ Yes, close it", f"t:cy:{tid}".encode()), Button.inline("↩️ Back", b"t:list")]])
    elif action == "cy":
        msg = await close_trade_now(user_id, int(parts[2]))
        await event.edit(msg or "⚠️ That trade is already closed.", parse_mode='HTML',
                         buttons=[[Button.inline("📒 My trades", b"t:list")], BACK_BUTTON])
    elif action == "x":
        pending.pop(user_id, None)
        await event.edit("✖️ Trade cancelled.\n\n" + menu_text(), parse_mode='HTML', buttons=MENU_BUTTONS)
    elif d is None:
        await event.answer("This step expired. Start again with 📈 New trade.", alert=True)
        return
    elif action == "c":
        d["symbol"] = parts[2]
        d["_step"] = "side"
        text, buttons = side_view(d)
        await event.edit(text, parse_mode='HTML', buttons=buttons)
    elif action == "side":
        d["side"] = parts[2]
        d["_step"] = "entry"
        await show_trade_step(event, d, edit=True)
    elif action == "mkt" and d.get("_step") == "entry":
        price = (await fetch_prices([d["symbol"]])).get(d["symbol"])
        if not price:
            await event.answer("Could not fetch the price, send it as a number.", alert=True)
            return
        d["entry"] = d["_market"] = price
        advance(d)
        await show_trade_step(event, d, edit=True)
    elif action == "m" and d.get("_step") == "margin":
        d["margin"] = float(parts[2])
        advance(d)
        await show_trade_step(event, d, edit=True)
    elif action == "lev" and d.get("_step") == "leverage":
        d["leverage"] = float(parts[2])
        advance(d)
        await show_trade_step(event, d, edit=True)
    elif action == "skip" and d.get("_step") in ("tp", "sl", "target"):
        advance(d)
        await show_trade_step(event, d, edit=True)
    elif action == "ok" and d.get("_step") == "confirm":
        errors, _ = tr.finalize_draft(d)
        if errors:
            await event.answer(errors[0], alert=True)
            return
        tid = tr.insert_trade(db, user_id, d)
        pending.pop(user_id, None)
        t = db.execute("SELECT * FROM trades WHERE id=?", (tid,)).fetchone()
        print(f"📈 Trade #{tid} opened by {user_id}: {t['symbol']} {t['side']} {t['leverage']}x")
        await event.edit(tr.trade_card(t, title="✅ <b>Simulated trade opened</b>\nI'll remind you at your TP / SL / target.\n"),
                         parse_mode='HTML', buttons=[[Button.inline("📒 My trades", b"t:list")], BACK_BUTTON])
    else:
        await event.answer("This button is from an older step.", alert=False)
        return
    await event.answer()

@bot.on(events.CallbackQuery(pattern=rb'^cancel_(\d+)$'))
async def cancel_alert_button(event):
    alert_id = int(event.pattern_match.group(1))
    cur = db.execute(
        "DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, event.sender_id)
    )
    db.commit()
    if cur.rowcount == 1:
        await event.answer("✅ Alert cancelled")
    else:
        await event.answer("⚠️ Could not find alert.", alert=True)
    msg, buttons = alerts_view(event.sender_id)
    try:
        await event.edit(msg, parse_mode='HTML', buttons=buttons)
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Background alert watcher
# ---------------------------------------------------------------------------
async def watch_alerts():
    print("🔍 Alert watcher started.")
    while True:
        try:
            active_alerts = db.execute("SELECT * FROM alerts WHERE triggered = 0").fetchall()
            open_trades = db.execute("SELECT * FROM trades WHERE status = 'open'").fetchall()
            prices = await get_all_prices() if (active_alerts or open_trades) else {}

            for t in open_trades:
                price = prices.get(t["symbol"])
                if price is None:
                    continue
                hit = tr.check_trade(t, price)
                if not hit:
                    continue
                kind, level = hit
                try:
                    if kind == "target":
                        db.execute("UPDATE trades SET target_notified = 1 WHERE id = ?", (t["id"],))
                        db.commit()
                        await bot.send_message(t["user_id"], tr.target_message(t, price), parse_mode='HTML',
                                               buttons=[[Button.inline("📒 My trades", b"t:list")]])
                    else:
                        pnl = tr.close_trade(db, t, level, kind)
                        await bot.send_message(t["user_id"], tr.close_message(t, level, kind, pnl), parse_mode='HTML',
                                               buttons=[[Button.inline("📜 History", b"t:hist")]])
                except Exception as e:
                    print(f"❌ Failed trade notification for {t['user_id']}: {e}")

            for alert in active_alerts:
                user_id = alert["user_id"]
                symbol = alert["symbol"]
                price = prices.get(symbol)
                if price is None:
                    continue

                direction = alert["direction"]
                threshold = alert["target_price"]
                crossed = price >= threshold if direction == "above" else price <= threshold

                if crossed:
                    arrow = "🔔🔼" if direction == "above" else "🔔🔽"
                    try:
                        await bot.send_message(
                            user_id,
                            f"{arrow} <b>{symbol}</b> just crossed <code>${fmt_price(threshold)}</code>!\n"
                            f"Current: <code>${fmt_price(price)}</code>",
                            parse_mode='HTML'
                        )
                        if not alert["repeat"]:
                            db.execute("UPDATE alerts SET triggered = 1 WHERE id = ?", (alert["id"],))
                            db.commit()
                    except Exception as e:
                        print(f"❌ Failed to alert {user_id}: {e}")
        except Exception as e:
            print(f"❌ Alert watcher error: {e}")

        await asyncio.sleep(10)

async def main():
    await bot.start()
    print("✅ Bot started...")
    await asyncio.gather(
        bot.run_until_disconnected(),
        watch_alerts()
    )

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Bot stopped.")
    finally:
        loop.close()
