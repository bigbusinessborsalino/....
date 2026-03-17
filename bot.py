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
BOT_PREFIX = os.getenv("BOT_PREFIX", "a1")                    # ← set a2 / a3 etc. for other bots
FORCE_SUB_CHANNELS = [ch.strip() for ch in os.getenv("FORCE_SUBS", "").split(",") if ch.strip()]

# Busy flag
is_busy = False

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DUMMY WEB SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")
    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

async def check_pm_started(user_id):
    try:
        await app.send_chat_action(user_id, "typing")
        return True
    except:
        return False

async def check_force_sub(user_id):
    if not FORCE_SUB_CHANNELS:
        return True
    for ch in FORCE_SUB_CHANNELS:
        try:
            member = await app.get_chat_member(f"@{ch}", user_id)
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
    await message.reply_text(
        f"✅ **Bot {BOT_PREFIX.upper()} is online!**\n\n"
        f"Easy command:\n"
        f"`/{BOT_PREFIX} <anime name> ep<episode> <resolution>p`\n\n"
        f"Example:\n"
        f"`/{BOT_PREFIX} solo leveling ep1 720p`\n\n"
        "✅ File sent to your PM. Forward to Saved Messages."
    )

@app.on_message(filters.command(BOT_PREFIX))
async def anime_download(client, message: Message):
    global is_busy

    # PM check
    if not await check_pm_started(message.from_user.id):
        bot_user = (await app.get_me()).username
        await message.reply_text(
            "❌ Please start me in PM first!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Start in PM", url=f"https://t.me/{bot_user}")
            ]])
        )
        return

    # Force Sub check
    if not await check_force_sub(message.from_user.id):
        buttons = [[InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{ch}")] for ch in FORCE_SUB_CHANNELS]
        await message.reply_text("❌ You must join all channels first:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Parse command
    command_text = message.text.split(" ", 1)
    if len(command_text) < 2:
        await message.reply_text(f"Usage: /{BOT_PREFIX} <name> ep<ep> <res>p\nExample: /{BOT_PREFIX} solo leveling ep1 720p")
        return
    args = command_text[1]
    anime_name, episode, resolution_arg = parse_command(args)
    if not anime_name or not episode or not resolution_arg:
        await message.reply_text(f"Usage: /{BOT_PREFIX} <name> ep<ep> <res>p\nExample: /{BOT_PREFIX} solo leveling ep1 720p")
        return

    # Now it's a real task → set busy
    if is_busy:
        await message.reply_text("❌ This bot is busy right now.\nTry another bot.")
        return
    is_busy = True

    try:
        resolutions = [resolution_arg]
        status_msg = await message.reply_text(f"Queueing **{anime_name}** Episode **{episode}** [{resolution_arg}p]...")

        # Script ready
        script_path = "./animepahe-dl.sh"
        if os.path.exists(script_path):
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)

        success_count = 0
        start_time = time.time()

        # Status refresh every 30s
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

            # Faster download (-t 3 threads)
            cmd = f"./animepahe-dl.sh -d -t 3 -a '{anime_name}' -e {episode} -r {res}"
            logger.info(f"Executing: {cmd}")

            process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)

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
                    chat_id=message.from_user.id,
                    document=new_file_path,
                    caption=f"{final_filename}\n\n✅ Sent to PM.\nForward to Saved Messages!",
                    force_document=True
                )
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Send failed: {e}")

            # Cleanup (deletes file)
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
        # 60s rest to protect free tier
        await asyncio.sleep(60)
        is_busy = False
        await message.reply_text("✅ I am free now!\nYou can use me again.")

if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
