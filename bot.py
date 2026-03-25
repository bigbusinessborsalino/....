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
    if pm_users_col is not None:
        try:
            # Short timeout to prevent hanging if DB is unreachable
            users = {doc["user_id"] for doc in pm_users_col.find({}, {"user_id": 1}).max_time_ms(5000)}
            logger.info(f"Loaded {len(users)} users from MongoDB")
        except Exception as e:
            logger.error(f"Could not load users from DB: {e}")
    else:
        logger.warning("No MONGO_URI — using memory only")

def add_user(user_id):
    global users
    # 1. Update memory immediately so the bot works in this session
    users.add(user_id) 
    
    # 2. Try to save to DB for persistence across restarts
    if pm_users_col is not None:
        try:
            pm_users_col.update_one(
                {"user_id": user_id}, 
                {"$set": {"user_id": user_id}}, 
                upsert=True
            )
        except Exception as e:
            logger.error(f"DB Error (User not saved to cloud): {e}")

@app.on_message(filters.command("start"))
async def start(client, message):
    if message.chat.type == "private":
        add_user(message.from_user.id)
        await message.reply_text(
            "✅ **PM started & Authorized!**\n"
            "You can now use the bot here or in groups.\n\n"
            f"Use `/{BOT_PREFIX} <name> ep<episode> <res>p` to download."
        )
    else:
        # Check if the user has ever started the bot in PM
        if message.from_user.id not in users:
            bot_user = (await app.get_me()).username
            await message.reply_text(
                "❌ Please start me in PM first to authorize your account!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Start in PM", url=f"https://t.me/{bot_user}")
                ]])
            )
        else:
            await message.reply_text(
                f"✅ **Bot {BOT_PREFIX.upper()} online!**\n"
                f"Command: `/{BOT_PREFIX} <name> ep<episode> <resolution>p`"
            )

# --- DUMMY WEB SERVER FOR RENDER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
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

@app.on_message(filters.command("stats") & filters.private & filters.user(ADMIN_ID))
async def stats_cmd(client, message):
    count = len(users)
    await message.reply_text(f"📊 Total active/authorized users: **{count}**")

@app.on_message(filters.command(BOT_PREFIX))
async def anime_download(client, message: Message):
    global is_busy
    user_id = message.from_user.id

    # Workflow: Check memory -> Check DB -> Block if neither
    if user_id not in users:
        found = False
        if pm_users_col is not None:
            try:
                if pm_users_col.find_one({"user_id": user_id}):
                    users.add(user_id)
                    found = True
            except:
                pass
        
        if not found:
            bot_user = (await app.get_me()).username
            await message.reply_text(
                "❌ Please start me in PM first!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Start in PM", url=f"https://t.me/{bot_user}")
                ]])
            )
            return

    # Force Sub Check (kept as requested)
    if not await check_force_sub(user_id):
        buttons = []
        for ch in FORCE_SUB_CHANNELS:
            url = f"https://t.me/{ch.strip('@')}"
            buttons.append([InlineKeyboardButton("Join Channel", url=url)])
        await message.reply_text("❌ You must join all channels first:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    command_text = message.text.split(" ", 1)
    if len(command_text) < 2:
        await message.reply_text(f"Usage: `/{BOT_PREFIX} <name> ep<ep> <res>p`")
        return
    
    args = command_text[1]
    anime_name, episode, resolution_arg = parse_command(args)
    if not anime_name:
        await message.reply_text("❌ Invalid format. Example: `solo leveling ep1 720p`")
        return

    if is_busy:
        await message.reply_text("❌ Bot is busy with another download. Please wait.")
        return
    
    is_busy = True
    try:
        status_msg = await message.reply_text(f"Processing **{anime_name}**...")
        
        if os.path.exists("./animepahe-dl.sh"):
            os.chmod("./animepahe-dl.sh", 0o755)

        cmd = f"./animepahe-dl.sh -d -t 2 -a '{anime_name}' -e {episode} -r {resolution_arg}"
        process = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        await process.wait()

        files = glob.glob("**/*.mp4", recursive=True)
        if files:
            latest_file = max(files, key=os.path.getctime)
            await app.send_document(
                chat_id=user_id, 
                document=latest_file, 
                caption=f"✅ **{anime_name}**\nEpisode: {episode}\nResolution: {resolution_arg}p"
            )
            os.remove(latest_file)
            await status_msg.edit_text("✅ Sent to your PM!")
        else:
            await status_msg.edit_text("❌ File not found. Check the name/episode and try again.")

    except Exception as e:
        logger.error(f"Download Error: {e}")
        await message.reply_text(f"⚠️ Error: {e}")
    finally:
        is_busy = False

if __name__ == "__main__":
    print("Bot Starting...")
    load_users()
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
