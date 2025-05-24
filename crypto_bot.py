import asyncio
import os
from datetime import datetime, timedelta
import requests
from telethon import TelegramClient, events, Button
from pymongo import MongoClient
from dotenv import load_dotenv
from bson import ObjectId

# Load environment variables
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")

# MongoDB setup
mongo_client = MongoClient(MONGO_URI)
db = mongo_client['crypto_bot']
users_col = db['users']

# Fetch prices from Binance
def fetch_prices():
    symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ETHFIUSDT"]
    prices = {}
    for symbol in symbols:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        try:
            response = requests.get(url)
            data = response.json()
            prices[symbol] = float(data['price'])
        except Exception:
            prices[symbol] = None
    return prices

# Initialize Telethon bot
bot = TelegramClient('crypto_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

@bot.on(events.NewMessage(pattern='/start'))
async def handle_start(event):
    user_id = event.sender_id
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id})
        print(f"✅ New user added: {user_id}")
    await event.respond(
        "You're now subscribed to crypto updates!\n\n"
        "starting from the next <b>hour or half hour</b> mark.\n\n"
        "✅ Updates include: BTC, ETH, SOL, ETHFI." \
        "\n\n"
        "Use <b>/help</b> to see available commands.",
        parse_mode='HTML'
    )

@bot.on(events.NewMessage(pattern=r'/sa (\w+)(?:\s+([\d\s.u]+))?'))
async def set_price_alerts(event):
    user_id = event.sender_id
    parts = event.raw_text.split()

    if len(parts) < 3:
        await event.respond("⚠️ Please provide a symbol and at least one price.\nExample: <code>/sa ETHUSDT 2800 2769 2390</code>", parse_mode='HTML')
        return

    symbol = parts[1].upper()
    price_values = parts[2:]

    is_repeating = False
    if price_values and price_values[-1].lower() == "u":
        is_repeating = True
        price_values = price_values[:-1]

    valid_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ETHFIUSDT"]
    if symbol not in valid_symbols:
        await event.respond(f"❌ Invalid symbol. Choose from: {', '.join(valid_symbols)}")
        return

    current_price = fetch_prices().get(symbol)
    if not current_price:
        await event.respond("⚠️ Could not fetch current price.")
        return

    confirmations = []
    for price_str in price_values:
        try:
            price = float(price_str)
            direction = "above" if price > current_price else "below"

            db['alerts'].insert_one({
                "user_id": user_id,
                "symbol": symbol,
                "target_price": price,
                "direction": direction,
                "triggered": False,
                "repeat": is_repeating,
                "created_at": datetime.utcnow()
            })

            arrow = "🔼" if direction == "above" else "🔽"
            confirmations.append(f"{arrow} <code>${price:,.2f}</code>")
        except ValueError:
            confirmations.append(f"❌ Invalid price: {price_str}")

    repeat_text = "\n🔁 Repeating alerts." if is_repeating else ""
    await event.respond(
        f"📍 Alerts for <b>{symbol}</b>:\n" + "\n".join(confirmations) +
        f"\n\nCurrent: <code>${current_price:,.2f}</code>" + repeat_text,
        parse_mode='HTML'
    )

@bot.on(events.NewMessage(pattern='/la'))
async def list_alerts(event):
    user_id = event.sender_id
    alerts = list(db['alerts'].find({"user_id": user_id, "triggered": False}))

    if not alerts:
        await event.respond("📭 You have no active alerts.")
        return

    for alert in alerts:
        msg = (
            f"📌 <b>{alert['symbol']}</b> "
            f"({alert['direction']}) <code>${alert['target_price']:.2f}</code>"
        )
        await bot.send_message(
            user_id,
            msg,
            buttons=[
                [Button.inline(f"❌ Cancel {alert['symbol']}", data=f"cancel_{alert['_id']}")]
            ],
            parse_mode='HTML'
        )

@bot.on(events.NewMessage(pattern='/help'))
async def handle_help(event):
    help_msg = (
        "<b>🛠 Available Commands:</b>\n\n"
        "<b>/price</b> — Get the latest crypto prices instantly\n"
        "<b>/sa SYMBOL PRICE [u]</b> — Set alert. Add 'u' to make it repeatable.\n"
        "<code>/sa BTCUSDT 69000 u</code>\n"
        "<b>/la</b> — List your active price alerts with cancel buttons\n"
        "<b>/ca SYMBOL</b> — Cancel all alerts for a symbol\n"
        "<b>/help</b> — Show this help message\n\n"
        "<i>💡 Supported: BTCUSDT, ETHUSDT, SOLUSDT, ETHFIUSDT</i>"
    )
    await event.respond(help_msg, parse_mode='HTML')

@bot.on(events.CallbackQuery(pattern=b'cancel_(.*)'))
async def cancel_alert_button(event):
    alert_id = event.pattern_match.group(1)
    result = db['alerts'].delete_one({"_id": ObjectId(alert_id)})

    if result.deleted_count == 1:
        await event.answer("✅ Alert cancelled", alert=True)
        await event.edit("❌ Alert deleted.")
    else:
        await event.answer("⚠️ Could not find alert.", alert=True)

@bot.on(events.NewMessage(pattern=r'/ca (\w+)'))
async def cancel_alert_by_symbol(event):
    user_id = event.sender_id
    symbol = event.pattern_match.group(1).upper()
    result = db['alerts'].delete_many({
        "user_id": user_id,
        "symbol": symbol,
        "triggered": False
    })
    if result.deleted_count:
        await event.respond(f"✅ Cancelled {result.deleted_count} alert(s) for <b>{symbol}</b>", parse_mode='HTML')
    else:
        await event.respond(f"⚠️ No active alerts found for <b>{symbol}</b>", parse_mode='HTML')

@bot.on(events.NewMessage(pattern='/price'))
async def handle_price_request(event):
    prices = fetch_prices()

    msg = f"📊 <b>Crypto Price Snapshot</b> <i>({datetime.now().strftime('%H:%M')})</i>\n\n"
    for sym, val in prices.items():
        if val:
            emoji = (
                "🟢" if sym.startswith("BTC") else
                "🔵" if sym.startswith("ETH") else
                "🟣"
            )
            msg += f"{emoji} <b>{sym}</b>: <code>${val:,.4f}</code>\n"
        else:
            msg += f"❌ <b>{sym}</b>: <i>Error fetching price</i>\n"

    await event.respond(msg, parse_mode='HTML')

async def watch_alerts():
    alerts_col = db['alerts']
    print("🔍 Alert watcher started.")
    while True:
        prices = fetch_prices()
        active_alerts = alerts_col.find({"triggered": False})

        for alert in active_alerts:
            user_id = alert["user_id"]
            symbol = alert["symbol"]
            price = prices.get(symbol)
            if price is None:
                continue

            direction = alert["direction"]
            threshold = alert["target_price"]

            crossed = (
                price >= threshold if direction == "above"
                else price <= threshold
            )

            if crossed:
                arrow = "🔔🔼" if direction == "above" else "🔔🔽"
                try:
                    await bot.send_message(
                        user_id,
                        f"{arrow} <b>{symbol}</b> just crossed <code>${threshold:,.2f}</code>!\n"
                        f"Current: <code>${price:,.2f}</code>",
                        parse_mode='HTML'
                    )
                    if not alert.get("repeat"):
                        alerts_col.update_one({"_id": alert["_id"]}, {"$set": {"triggered": True}})
                except Exception as e:
                    print(f"❌ Failed to alert {user_id}: {e}")

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
