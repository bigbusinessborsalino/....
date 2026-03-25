import os
import glob
import asyncio
import logging
import sys
import stat
import time
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from aiohttp import web
from pymongo import MongoClient

# Load environment variables
load_dotenv()

# Configuration
def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID = get_env_int("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = get_env_int("PORT", 8000)
BOT_PREFIX = os.getenv("BOT_PREFIX", "a1")
FORCE_SUB_CHANNELS = [ch.strip() for ch in os.getenv("FORCE_SUBS", "").split(",") if ch.strip()]
ADMIN_ID = get_env_int("ADMIN_ID")
MONGO_URI = os.getenv("MONGO_URI")

# MongoDB + cache
client = MongoClient(MONGO_URI) if MONGO_URI else None
db = client["animebot"] if client is not None else None
pm_users_col = db["pm_users"] if db is not None else None
users = set()
is_busy = False

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

def load_users():
    global users
    # FIX: Use "is not None" for PyMongo objects
    if pm_users_col is not None:
        try:
            users = {doc["user_id"] for doc in pm_users_col.find({}, {"user_id": 1})}
            logger.info(f"Loaded {len(users)} users from MongoDB")
        except Exception as e:
            logger.error(f"Error loading users: {e}")
    else:
        logger.warning("No MONGO_URI — using memory only (lost on restart)")

def add_user(user_id):
    global users  # FIX: Ensure we update the global set
    users.add(user_id)
    # FIX: Use "is not None" for PyMongo objects
    if pm_users_col is not None:
        pm_users_col.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)

# --- DUMMY WEB SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
    # Filter out health check logs to keep Render logs clean
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def check_force_sub(user_id):
    if not FORCE_SUB_CHANNELS:
        return True
    for ch in FORCE_SUB_CHANNELS:
        try:
            chat_id = int(ch) if ch.startswith("-") else f"@{ch.strip('@')}"
            member = await app.get_chat_member(chat_id, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                return False
        except:
            return False
    return True

def parse_command(text):
    words = text.strip().split()
    if len(words) < 3:
        return None, None, None
    res_str = words[-1]
    if not res_str.endswith("p") or not res_str[:-1].isdigit():
        return None, None, None
    resolution = res_str[:-1]
    if words[-2].lower().startswith("ep") and words[-2][2:].isdigit():
        episode = words[-2][2:]
        anime_name = " ".join(words[:-2])
        return anime_name, episode, resolution
    if len(words) >= 4 and words[-3].lower() in ["episode", "ep"] and words[-2].isdigit():
        episode = words[-2]
        anime_name = " ".join(words[:-3])
        return anime_name, episode, resolution
    return None, None, None

@app.on_message(filters.command("start"))
async def start(client, message):
    if message.chat.type == "private":
        add_user(message.from_user.id)
        await message.reply_text("✅ PM started & saved! You can now use the bot in groups.")
    else:
        await message.reply_text(
            f"✅ **Bot {BOT_PREFIX.upper()} online!**\n"
            f"Command: /{BOT_PREFIX} <name> ep<episode> <resolution>p"
        )

@app.on_message(filters.command("stats") & filters.private & filters.user(ADMIN_ID))
async def stats_cmd(client, message):
    # FIX: Use "is not None" for PyMongo objects
    count = pm_users_col.count_documents({}) if pm_users_col is not None else len(users)
    await message.reply_text(f"📊 Total users who started the bot: **{count}**")

@app.on_message(filters.command("broadcast") & filters.private & filters.user(ADMIN_ID))
async def broadcast_cmd(client, message):
    if not message.reply_to_message:
        await message.reply_text("Reply to any message you want to broadcast, then type /broadcast")
        return
    sent = 0
    # FIX: Use "is not None" for PyMongo objects
    cursor = pm_users_col.find({}) if pm_users_col is not None else [{"user_id": uid} for uid in users]
    for doc in cursor:
        uid = doc["user_id"]
        try:
            await app.copy_message(uid, message.chat.id, message.reply_to_message.id)
            sent += 1
            await asyncio.sleep(0.3)
        except:
            pass
    await message.reply_text(f"✅ Broadcast done! Reached **{sent}** users.")

@app.on_message(filters.command(BOT_PREFIX))
async def anime_download(client, message: Message):
    global is_busy
    user_id = message.from_user.id

    # FIX: Fallback DB check if user is not in memory cache
    if user_id not in users:
        if pm_users_col is not None and pm_users_col.find_one({"user_id": user_id}):
            users.add(user_id)
        else:
            bot_user = (await app.get_me()).username
            await message.reply_text(
                "❌ Please start me in PM first!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Start in PM", url=f"https://t.me/{bot_user}")
                ]])
            )
            return

    if not await check_force_sub(user_id):
        buttons = []
        for ch in FORCE_SUB_CHANNELS:
            if ch.startswith("-"):
                clean = ch.replace("-100", "")
                url = f"https://t.me/c/{clean}"
            else:
                url = f"https://t.me/{ch}"
            buttons.append([InlineKeyboardButton("Join Channel", url=url)])
        await message.reply_text("❌ You must join all channels first:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    command_text = message.text.split(" ", 1)
    if len(command_text) < 2:
        await message.reply_text(f"Usage: /{BOT_PREFIX} <name> ep<ep> <res>p\nExample: /{BOT_PREFIX} solo leveling ep1 720p")
        return
    args = command_text[1]
    anime_name, episode, resolution_arg = parse_command(args)
    if not anime_name or not episode or not resolution_arg:
        await message.reply_text(f"Usage: /{BOT_PREFIX} <name> ep<ep> <res>p\nExample: /{BOT_PREFIX} solo leveling ep1 720p")
        return

    if is_busy:
        await message.reply_text("❌ This bot is busy right now.\nTry another bot.")
        return
    is_busy = True

    try:
        resolutions = [resolution_arg]
        status_msg = await message.reply_text(f"Queueing **{anime_name}** Episode **{episode}** [{resolution_arg}p]...")

        script_path = "./animepahe-dl.sh"
        if os.path.exists(script_path):
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)

        success_count = 0
        start_time = time.time()

        async def status_updater():
            while True:
                await asyncio.sleep(30)
                elapsed = int(time.time() - start_time)
                try:
                    await status_msg.edit_text(f"Still downloading... ({elapsed}s elapsed)")
                except:
                    break
        updater_task = asyncio.create_task(status_updater())

        for res in resolutions:
            await status_msg.edit_text(f"Processing **{anime_name}** - Episode {episode} [{res}p]...")
            cmd = f"./animepahe-dl.sh -d -t 3 -a '{anime_name}' -e {episode} -r {res}"
            logger.info(f"Executing: {cmd}")

            process = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )

            while True:
                line = await process.stdout.readline()
                if not line: break
                decoded = line.decode('utf-8', errors='ignore').strip()
                if decoded:
                    print(f"[SCRIPT] {decoded}")

            await process.wait()
            updater_task.cancel()

            if process.returncode != 0:
                await message.reply_text(f"❌ Failed {res}p.")
                continue

            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                continue
            latest_file = max(files, key=os.path.getctime)

            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            try:
                os.rename(latest_file, new_file_path)
            except:
                new_file_path = latest_file

            await status_msg.edit_text(f"Sending {final_filename} to your PM...")
            try:
                await app.send_document(
                    chat_id=user_id,
                    document=new_file_path,
                    caption=f"{final_filename}\n\n✅ Sent to PM.\nForward to Saved Messages!",
                    force_document=True
                )
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Send failed: {e}")

            # DELETE FILE AFTER SENDING
            try:
                os.remove(new_file_path)
                parent = os.path.dirname(new_file_path)
                if not os.listdir(parent):
                    os.rmdir(parent)
            except:
                pass

        if success_count == len(resolutions):
            await status_msg.edit_text("✅ Done! Check your PM.")
        else:
            await status_msg.edit_text(f"⚠️ {success_count} sent.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")
    finally:
        await asyncio.sleep(60)
        is_busy = False
        await message.reply_text("✅ I am free now!")

if __name__ == "__main__":
    print("Bot Starting...")
    load_users()
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
