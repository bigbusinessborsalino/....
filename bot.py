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

# Configuration (only these 4 needed now)
def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID = get_env_int("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PORT = get_env_int("PORT", 8000)

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- DUMMY WEB SERVER (keeps Render free tier alive) ---
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
        "✅ **Bot is online!**\n\n"
        "Usage in group:\n"
        "`/anime <name> -e <episode> -r <resolution|all>`\n\n"
        "✅ Files will be sent directly to your DM / Saved Messages."
    )

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    command_text = message.text.split(" ", 1)
    if len(command_text) < 2:
        await message.reply_text("Usage: /anime <name> -e <episode> -r <resolution|all>")
        return

    args = command_text[1]
    
    try:
        if "-e" not in args or "-r" not in args:
            await message.reply_text("Error: Missing -e or -r flags.")
            return

        parts = args.split("-e")
        anime_name = parts[0].strip()
        rest = parts[1].split("-r")
        episode = rest[0].strip()
        resolution_arg = rest[1].strip()

        resolutions = ["360", "720", "1080"] if resolution_arg.lower() == "all" else [resolution_arg]

        status_msg = await message.reply_text(f"Queueing **{anime_name}** Episode **{episode}**...")

        # --- SELF-REPAIR: Ensure script is executable ---
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
                await message.reply_text(f"❌ Failed to download {res}p. (Exit Code: {process.returncode})")
                continue

            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                await message.reply_text(f"❌ Download finished, but file not found for {res}p.")
                continue
            
            latest_file = max(files, key=os.path.getctime)
            
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            # Upload directly to user's DM
            await status_msg.edit_text(f"Uploading {final_filename} to your DM...")
            try:
                await app.send_document(
                    chat_id=message.from_user.id,
                    document=new_file_path,
                    caption=f"{final_filename}\n\n✅ Sent privately via Anime Bot",
                    force_document=True
                )
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Failed to send to your DM: {e}\nMake sure you started the bot in PM first.")

            # Cleanup
            try:
                os.remove(new_file_path)
                parent_dir = os.path.dirname(new_file_path)
                if not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass

        if success_count == len(resolutions):
            await status_msg.edit_text("✅ All done! Files sent to your DM.")
        else:
            await status_msg.edit_text(f"⚠️ Job finished. {success_count}/{len(resolutions)} sent.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")

if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
