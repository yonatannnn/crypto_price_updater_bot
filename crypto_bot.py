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

# Initialize the Telethon bot
bot = TelegramClient('crypto_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# Handle /start command
@bot.on(events.NewMessage(pattern='/start'))
async def handle_start(event):
    user_id = event.sender_id
    if not users_col.find_one({"user_id": user_id}):
        users_col.insert_one({"user_id": user_id})
        print(f"✅ New user added: {user_id}")
    await event.respond(
        "You're now subscribed to crypto updates!\n\n"
        "You'll receive updates every <b>30 minutes</b>, "
        "starting from the next <b>hour or half hour</b> mark.\n\n"
        "✅ Updates include: BTC, ETH, SOL, ETHFI.",
        parse_mode='HTML'
    )

@bot.on(events.NewMessage(pattern=r'/set_alert (\w+) (\d+\.?\d*)'))
async def set_price_alert(event):
    user_id = event.sender_id
    symbol = event.pattern_match.group(1).upper()
    price = float(event.pattern_match.group(2))

    valid_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ETHFIUSDT"]
    if symbol not in valid_symbols:
        await event.respond(f"❌ Invalid symbol. Choose from: {', '.join(valid_symbols)}")
        return

    current_price = fetch_prices().get(symbol)
    if not current_price:
        await event.respond("⚠️ Could not fetch current price.")
        return

    direction = "above" if price > current_price else "below"
    db['alerts'].insert_one({
        "user_id": user_id,
        "symbol": symbol,
        "target_price": price,
        "direction": direction,
        "triggered": False,
        "created_at": datetime.utcnow()
    })

    arrow = "🔼" if direction == "above" else "🔽"
    await event.respond(
        f"📍 Alert set for <b>{symbol}</b> {arrow} <code>${price:,.2f}</code>\n"
        f"Current: <code>${current_price:,.2f}</code>",
        parse_mode='HTML'
    )


# Send prices every 30 minutes from the next aligned time
async def send_half_hourly_prices():
    now = datetime.now()
    if now.minute < 30:
        next_mark = now.replace(minute=30, second=0, microsecond=0)
    else:
        next_mark = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    wait_seconds = (next_mark - now).total_seconds()
    print(f"⏳ Waiting {wait_seconds:.0f} seconds until next 30-min mark ({next_mark.strftime('%H:%M:%S')})")
    await asyncio.sleep(wait_seconds)

    while True:
        prices = fetch_prices()
        msg = f"📊 <b>Crypto Price Update</b> <i>({datetime.now().strftime('%H:%M')})</i>\n\n"
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

        user_ids = {user["user_id"] for user in users_col.find()}
        for user_id in user_ids:
            try:
                await bot.send_message(user_id, msg, parse_mode='HTML')
            except Exception as e:
                print(f"❌ Could not send to {user_id}: {e}")

        await asyncio.sleep(1800)  # Sleep for 30 minutes

@bot.on(events.NewMessage(pattern='/list_alerts'))
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

@bot.on(events.CallbackQuery(pattern=b'cancel_(.*)'))
async def cancel_alert_button(event):
    alert_id = event.pattern_match.group(1)
    result = db['alerts'].delete_one({"_id": ObjectId(alert_id)})
    
    if result.deleted_count == 1:
        await event.answer("✅ Alert cancelled", alert=True)
        await event.edit("❌ Alert deleted.")
    else:
        await event.answer("⚠️ Could not find alert.", alert=True)


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
                    alerts_col.update_one({"_id": alert["_id"]}, {"$set": {"triggered": True}})
                except Exception as e:
                    print(f"❌ Failed to alert {user_id}: {e}")

        await asyncio.sleep(10)  # Poll every 10 seconds

@bot.on(events.NewMessage(pattern=r'/cancel_alert (\w+)'))
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


# Main loop
async def main():
    await bot.start()
    print("✅ Bot started...")
    await asyncio.gather(
        bot.run_until_disconnected(),
        send_half_hourly_prices(),
        watch_alerts()
    )

# Run the bot
if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.create_task(main())
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Bot stopped.")
    finally:
        loop.close()
