import os
import glob
import asyncio
import logging
import sys
import stat
from pyrogram import Client, filters
from pyrogram.types import Message
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

# Busy flag for single task only
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

@app.on_message(filters.command("start"))
async def start(client, message):
    await message.reply_text(
        "✅ **Bot A1 is online!**\n\n"
        "Easy command:\n"
        "`/a1 <anime name> ep<episode> <resolution>p`\n\n"
        "Example:\n"
        "`/a1 solo leveling ep1 720p`\n\n"
        "✅ File will be sent to your PM.\n"
        "Forward it to your Saved Messages for safety."
    )

def parse_a1_command(text):
    words = text.strip().split()
    if len(words) < 3:
        return None, None, None
    
    res_str = words[-1]
    if not res_str.endswith("p") or not res_str[:-1].isdigit():
        return None, None, None
    resolution = res_str[:-1]
    
    # Format 1: name ep1 720p
    if words[-2].lower().startswith("ep") and words[-2][2:].isdigit():
        episode = words[-2][2:]
        anime_name = " ".join(words[:-2])
        return anime_name, episode, resolution
    
    # Format 2: name episode 1 720p
    if len(words) >= 4 and words[-3].lower() == "episode" and words[-2].isdigit():
        episode = words[-2]
        anime_name = " ".join(words[:-3])
        return anime_name, episode, resolution
    
    # Format 3: name ep 1 720p
    if len(words) >= 4 and words[-3].lower() == "ep" and words[-2].isdigit():
        episode = words[-2]
        anime_name = " ".join(words[:-3])
        return anime_name, episode, resolution
    
    return None, None, None

@app.on_message(filters.command("a1"))
async def anime_download(client, message: Message):
    global is_busy
    
    if is_busy:
        await message.reply_text("❌ This bot is busy right now.\nTry another bot (e.g. /a2)")
        return
    
    is_busy = True
    
    try:
        command_text = message.text.split(" ", 1)
        if len(command_text) < 2:
            await message.reply_text("Usage: /a1 <anime name> ep<episode> <resolution>p\nExample: /a1 solo leveling ep1 720p")
            return

        args = command_text[1]
        anime_name, episode, resolution_arg = parse_a1_command(args)
        
        if not anime_name or not episode:
            await message.reply_text("Usage: /a1 <anime name> ep<episode> <resolution>p\nExample: /a1 solo leveling ep1 720p")
            return

        resolutions = [resolution_arg]

        status_msg = await message.reply_text(f"Queueing **{anime_name}** Episode **{episode}** [{resolution_arg}p]...")

        # Ensure script is executable
        script_path = "./animepahe-dl.sh"
        if os.path.exists(script_path):
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)
        else:
            await message.reply_text("❌ Critical Error: animepahe-dl.sh not found!")
            return

        success_count = 0

        for res in resolutions:
            await status_msg.edit_text(f"Processing **{anime_name}** - Episode {episode} [{res}p]...")
            
            cmd = f"./animepahe-dl.sh -d -t 1 -a '{anime_name}' -e {episode} -r {res}"
            logger.info(f"Executing: {cmd}")
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT 
            )

            while True:
                line = await process.stdout.readline()
                if not line: break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                if decoded_line:
                    print(f"[SCRIPT] {decoded_line}")

            await process.wait()
            
            if process.returncode != 0:
                await message.reply_text(f"❌ Failed to download {res}p.")
                continue

            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                await message.reply_text(f"❌ File not found for {res}p.")
                continue
            
            latest_file = max(files, key=os.path.getctime)
            
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            await status_msg.edit_text(f"Sending {final_filename} to your PM...")
            try:
                await app.send_document(
                    chat_id=message.from_user.id,
                    document=new_file_path,
                    caption=f"{final_filename}\n\n✅ Sent to your PM.\nForward this to Saved Messages for safety!",
                    force_document=True
                )
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Send failed: {e}\nStart me in PM first (/start)")

            # Cleanup
            try:
                os.remove(new_file_path)
                parent_dir = os.path.dirname(new_file_path)
                if not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass

        if success_count == len(resolutions):
            await status_msg.edit_text("✅ Download done! Check your PM.")
        else:
            await status_msg.edit_text(f"⚠️ {success_count}/{len(resolutions)} sent.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")
    
    finally:
        # Rest 60 seconds to protect Render free tier
        await asyncio.sleep(60)
        is_busy = False
        await message.reply_text("✅ I am free now!\nYou can use /a1 again.")

if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
