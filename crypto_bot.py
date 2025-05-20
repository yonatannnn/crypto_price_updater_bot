import asyncio
import os
from datetime import datetime, timedelta
import requests
from telethon import TelegramClient, events
from pymongo import MongoClient
from dotenv import load_dotenv

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

# Send prices every 30 minutes from the next aligned time
async def send_half_hourly_prices():
    now = datetime.now()
    minute = 30 if now.minute < 30 else 60
    next_mark = now.replace(minute=minute, second=0, microsecond=0)
    if minute == 60:
        next_mark += timedelta(hours=1)
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

# Main loop
async def main():
    await bot.start()
    print("✅ Bot started...")
    await asyncio.gather(
        bot.run_until_disconnected(),
        send_half_hourly_prices()
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
