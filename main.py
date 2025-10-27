import os
import re
import math
import time
import json
import base64
import signal
import asyncio
import logging
import aiohttp
import urllib.parse
import sys
import psutil # For stats
from datetime import datetime, timedelta
from motor.motor_asyncio import AsyncIOMotorClient
from aiohttp import web, ClientConnectionError, ClientTimeout
from dotenv import load_dotenv
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, UserNotParticipant, AuthBytesInvalid, PeerIdInvalid, LimitInvalid, Timeout, FileReferenceExpired
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.session import Session, Auth
from pyrogram.file_id import FileId, FileType
from pyrogram import raw
from pyrogram.raw.types import InputPhotoFileLocation, InputDocumentFileLocation

# -------------------------------------------------------------------------------- #
# KeralaCaptain Bot - Pure Streaming Engine V4.1 (Cleaned)                         #
# -------------------------------------------------------------------------------- #

# Load configurations from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s - %(levelname)s] - %(message)s')
LOGGER = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

# --- NEW: Record bot start time ---
start_time = time.time()

class Config:
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    # REMOVED: BACKUP_BOT_TOKEN (no longer needed)
    
    # --- NEW: Admin and Domain Config ---
    ADMIN_IDS = list(int(admin_id) for admin_id in os.environ.get("ADMIN_IDS", "0").split())
    PROTECTED_DOMAIN = os.environ.get("PROTECTED_DOMAIN", "https://keralacaptain.rf.gd/").rstrip('/') + '/'
    
    MONGO_URI = os.environ.get("MONGO_URI", "")
    LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0))
    STREAM_URL = os.environ.get("STREAM_URL", "").rstrip('/')
    PORT = int(os.environ.get("PORT", 8080))
    
    PING_INTERVAL = int(os.environ.get("PING_INTERVAL", 1200))
    ON_HEROKU = 'DYNO' in os.environ

# --- VALIDATE ESSENTIAL CONFIGURATIONS ---
required_vars = [
    Config.API_ID, Config.API_HASH, Config.BOT_TOKEN,
    Config.MONGO_URI, Config.LOG_CHANNEL_ID, Config.STREAM_URL,
    Config.ADMIN_IDS
]
if not all(required_vars) or Config.ADMIN_IDS == [0]:
    LOGGER.critical("FATAL: One or more required variables (API_ID, API_HASH, BOT_TOKEN, MONGO_URI, LOG_CHANNEL_ID, STREAM_URL, ADMIN_IDS) are missing. Cannot start.")
    exit(1)

# --- NEW: Global variable for the protected domain ---
CURRENT_PROTECTED_DOMAIN = Config.PROTECTED_DOMAIN

# -------------------------------------------------------------------------------- #
# HELPER FUNCTIONS & CLASSES
# -------------------------------------------------------------------------------- #

# --- REMOVED: Base64 Encoding/Decoding (No longer needed) ---

# --- Human-readable formatters (Kept for new stats panel) ---
def humanbytes(size):
    if not size: return "0 B"
    power = 1024
    n = 0
    power_labels = {0: ' ', 1: 'K', 2: 'M', 3: 'G', 4: 'T'}
    while size > power:
        size /= power
        n += 1
    return f"{round(size, 2)} {power_labels[n]}B"

def get_readable_time(seconds: int) -> str:
    """Return a human-readable time format"""
    result = ""
    (days, remainder) = divmod(seconds, 86400)
    days = int(days)
    if days != 0:
        result += f"{days}d "
    (hours, remainder) = divmod(remainder, 3600)
    hours = int(hours)
    if hours != 0:
        result += f"{hours}h "
    (minutes, seconds) = divmod(remainder, 60)
    minutes = int(minutes)
    if minutes != 0:
        result += f"{minutes}m "
    seconds = int(seconds)
    result += f"{seconds}s"
    return result

# -------------------------------------------------------------------------------- #
# DATABASE OPERATIONS
# -------------------------------------------------------------------------------- #

# ================================================================================ #
# DATABASE OPERATIONS (Cleaned)
# ================================================================================ #

db_client = AsyncIOMotorClient(Config.MONGO_URI)
db = db_client['KeralaCaptainBotDB']

# Primary collection for all normal operations
media_collection = db['media']
# Backup collection
media_backup_collection = db['media_backup']
# User conversations (Kept for new settings panel)
user_conversations_col = db['conversations']
# --- NEW: Collection for bot settings ---
settings_collection = db['settings']


# --- Database Functions (Cleaned) ---

# --- REMOVED: check_duplicate (No longer needed) ---
# --- REMOVED: add_media_to_db (No longer needed) ---

async def get_media_by_post_id(post_id: int):
    """(Kept for FileRef logic) Reads media data only from the main collection."""
    return await media_collection.find_one({"wp_post_id": post_id})

async def update_media_links_in_db(post_id: int, new_message_ids: dict, new_stream_link: str):
    """(Kept for FileRef logic) Updates links in both the main and backup collections."""
    update_query = {
        "$set": {"message_ids": new_message_ids, "stream_link": new_stream_link}
    }
    await media_collection.update_one({"wp_post_id": post_id}, update_query)
    await media_backup_collection.update_one({"wp_post_id": post_id}, update_query) # Also update the backup

# --- REMOVED: delete_media_from_db (No longer needed) ---
# --- REMOVED: get_stats (No longer needed) ---
# --- REMOVED: get_all_media_for_library (No longer needed) ---

async def get_user_conversation(chat_id):
    """(Kept for new admin panel) Manages user conversation state."""
    return await user_conversations_col.find_one({"_id": chat_id})

async def update_user_conversation(chat_id, data):
    """(Kept for new admin panel) Manages user conversation state."""
    if data:
        await user_conversations_col.update_one({"_id": chat_id}, {"$set": data}, upsert=True)
    else:
        await user_conversations_col.delete_one({"_id": chat_id})

async def get_post_id_from_msg_id(msg_id: int):
    """(Kept for FileRef logic) Helper for stream refreshing."""
    doc = await media_collection.find_one({"message_ids": {"$in": [msg_id]}})
    return doc['wp_post_id'] if doc else None

# --- NEW: Database functions for Domain Settings ---

async def get_protected_domain() -> str:
    """Fetches the protected domain from settings, returns default if not found."""
    try:
        doc = await settings_collection.find_one({"_id": "bot_settings"})
        if doc and "protected_domain" in doc:
            return doc["protected_domain"]
    except Exception as e:
        LOGGER.error(f"Could not fetch domain from DB: {e}. Using default.")
    
    # Fallback to default from Config
    return Config.PROTECTED_DOMAIN

async def set_protected_domain(new_domain: str):
    """Saves the new protected domain to the database."""
    global CURRENT_PROTECTED_DOMAIN
    if not (new_domain.startswith("https://") or new_domain.startswith("http://")):
        new_domain = "https://" + new_domain
    if not new_domain.endswith('/'):
        new_domain += '/'
        
    await settings_collection.update_one(
        {"_id": "bot_settings"},
        {"$set": {"protected_domain": new_domain}},
        upsert=True
    )
    CURRENT_PROTECTED_DOMAIN = new_domain # Update global variable
    LOGGER.info(f"Protected domain updated in DB: {new_domain}")
    return new_domain

# ================================================================================ #
# END OF DATABASE OPERATIONS SECTION
# ================================================================================ #


# -------------------------------------------------------------------------------- #
# STREAMING ENGINE & WEB SERVER (UNCHANGED CORE LOGIC)
# -------------------------------------------------------------------------------- #

multi_clients = {}
work_loads = {}
class_cache = {}
processed_media_groups = {} # Kept for FileReference logic
next_client_idx = 0 
stream_errors = 0 
last_error_reset = time.time()

# Upgraded ByteStreamer (Kept As Is)
class ByteStreamer:
    def __init__(self, client: Client):
        self.client: Client = client
        self.cached_file_ids = {} # Cache for file properties
        self.session_cache = {} # {dc_id: (session, timestamp)} for TTL
        asyncio.create_task(self.clean_cache_regularly())

    async def clean_cache_regularly(self):
        while True:
            await asyncio.sleep(1200) # 20 min
            self.cached_file_ids.clear()
            self.session_cache.clear()  
            LOGGER.info("Cleared ByteStreamer's cached file properties and sessions.")

    async def get_file_properties(self, message_id: int):
        if message_id in self.cached_file_ids:
            return self.cached_file_ids[message_id]

        message = await self.client.get_messages(Config.LOG_CHANNEL_ID, message_id)
        if not message or message.empty or not (message.document or message.video):
            raise FileNotFoundError

        media = message.document or message.video
        file_id = FileId.decode(media.file_id)
        setattr(file_id, "file_size", media.file_size or 0)
        setattr(file_id, "mime_type", media.mime_type or "video/mp4")
        setattr(file_id, "file_name", media.file_name or "Unknown.mp4")

        self.cached_file_ids[message_id] = file_id
        return file_id

    async def generate_media_session(self, file_id: FileId) -> Session:
        media_session = self.client.media_sessions.get(file_id.dc_id)
        dc_id = file_id.dc_id

        if dc_id in self.session_cache:
            session, ts = self.session_cache[dc_id]
            if time.time() - ts < 300: # 5min TTL
                LOGGER.debug(f"Reusing TTL-cached media session for DC {dc_id}")
                return session

        if media_session:
            try:
                await media_session.send(raw.functions.help.GetConfig(), timeout=10)
                self.session_cache[dc_id] = (media_session, time.time()) # Cache on success
                LOGGER.debug(f"Reusing pinged media session for DC {dc_id}")
                return media_session
            except Exception as e:
                LOGGER.warning(f"Existing media session for DC {dc_id} is stale: {e}. Recreating.")
                try:
                    await media_session.stop()
                except: pass
                if dc_id in self.client.media_sessions:
                    del self.client.media_sessions[dc_id]
                media_session = None

        LOGGER.info(f"Creating new media session for DC {dc_id}")
        if dc_id != await self.client.storage.dc_id():
            media_session = Session(self.client, dc_id, await Auth(self.client, dc_id, await self.client.storage.test_mode()).create(), await self.client.storage.test_mode(), is_media=True)
            await media_session.start()
            for i in range(3):
                try:
                    exported_auth = await self.client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                    await media_session.send(raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
                    break
                except AuthBytesInvalid as e:
                    LOGGER.warning(f"AuthBytesInvalid on attempt {i+1}: {e}")
                    if i == 2: raise
                    await asyncio.sleep(1)
        else:
            media_session = Session(self.client, dc_id, await self.client.storage.auth_key(), await self.client.storage.test_mode(), is_media=True)
            await media_session.start()

        self.client.media_sessions[dc_id] = media_session
        self.session_cache[dc_id] = (media_session, time.time()) # Cache new
        return media_session

    @staticmethod
    def get_location(file_id: FileId):
        if file_id.file_type == FileType.PHOTO:
            return InputPhotoFileLocation(id=file_id.media_id, access_hash=file_id.access_hash, file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)
        else:
            return InputDocumentFileLocation(id=file_id.media_id, access_hash=file_id.access_hash, file_reference=file_id.file_reference, thumb_size=file_id.thumbnail_size)

    async def yield_file(self, file_id: FileId, offset: int, chunk_size: int, message_id: int):
        media_session = await self.generate_media_session(file_id)
        location = self.get_location(file_id)

        current_offset = offset
        retry_count = 0
        max_retries = 3

        while True:
            try:
                chunk = await media_session.send(
                    raw.functions.upload.GetFile(location=location, offset=current_offset, limit=chunk_size),
                    timeout=30
                )
                
                if isinstance(chunk, raw.types.upload.File) and chunk.bytes:
                    yield chunk.bytes
                    if len(chunk.bytes) < chunk_size:
                        break
                    current_offset += len(chunk.bytes)
                else:
                    break

            except FileReferenceExpired:
                # This logic is CRUCIAL and why we kept the DB functions
                retry_count += 1
                if retry_count > max_retries:
                    raise 
                LOGGER.warning(f"FileReferenceExpired for msg {message_id}, retry {retry_count}/{max_retries}. Refreshing...")
                
                original_msg = await self.client.get_messages(Config.LOG_CHANNEL_ID, message_id)
                if original_msg:
                    refreshed_msg = await forward_file_safely(original_msg) # Uses forward_file_safely
                    if refreshed_msg:
                        new_file_id = await self.get_file_properties(refreshed_msg.id)
                        self.cached_file_ids[message_id] = new_file_id 
                        
                        # Update DB
                        post_id = await get_post_id_from_msg_id(message_id) # Uses get_post_id_from_msg_id
                        if post_id:
                            media_doc = await get_media_by_post_id(post_id) # Uses get_media_by_post_id
                            if media_doc:
                                old_qualities = media_doc['message_ids']
                                # Find the key (quality) for the old message_id
                                quality_key = next((k for k, v in old_qualities.items() if v == message_id), None)
                                
                                new_qualities = old_qualities
                                if quality_key:
                                    new_qualities[quality_key] = refreshed_msg.id
                                else:
                                    # Fallback if key not found (shouldn't happen)
                                    new_qualities = {k: refreshed_msg.id if v == message_id else v for k, v in old_qualities.items()}

                                # Uses update_media_links_in_db
                                await update_media_links_in_db(post_id, new_qualities, media_doc['stream_link'])
                                
                        location = self.get_location(new_file_id) # Recreate location
                        await asyncio.sleep(2) 
                        continue # Retry fetch
                raise # Failed refresh

            except FloodWait as e:
                LOGGER.warning(f"FloodWait of {e.value} seconds on get_file. Waiting...")
                await asyncio.sleep(e.value)
                continue

routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.Response(text=f"Welcome to KeralaCaptain's Streaming Service!", content_type='text/html')

@routes.get("/health")
async def health_handler(request):
    global stream_errors, last_error_reset
    if time.time() - last_error_reset > 60: 
        stream_errors = 0
        last_error_reset = time.time()
    active_sessions = len(multi_clients)
    cache_size = 0
    if multi_clients:
        sample_client = list(multi_clients.values())[0]
        if sample_client in class_cache:
            cache_size = len(class_cache[sample_client].cached_file_ids)
    return web.json_response({
        "status": "ok",
        "active_clients": active_sessions,
        "cache_size": cache_size,
        "stream_errors_last_min": stream_errors,
        "workloads": work_loads,
    })

@routes.get("/favicon.ico")
async def favicon_handler(request):
    return web.Response(status=204) 

# --- REMOVED: Old stream_handler ---

# --- NEW: Download Handler (Replaced stream_handler as requested) ---
@routes.get(r"/download/{message_id:\d+}") # Route changed to /download/
async def download_handler(request: web.Request):
    client_index = None
    try:
        # --- REFERER CHECK REMOVED ---
        # No security check based on Referer header

        message_id = int(request.match_info['message_id'])
        range_header = request.headers.get("Range", 0) # Keep Range for resumable downloads

        # --- Load Balancing (Keep this logic) ---
        if not work_loads: # Handle case where workloads might be empty initially
             return web.Response(status=503, text="Service temporarily unavailable (no workers).")
        min_load = min(work_loads.values()) if work_loads else 0
        candidates = [cid for cid, load in work_loads.items() if load == min_load]
        if not candidates: # Should not happen if workloads is not empty, but safeguard
             # Fallback: pick the first available client if candidates calculation failed
             candidates = list(work_loads.keys())
             if not candidates:
                 return web.Response(status=503, text="Service temporarily unavailable (no workers identified).")

        global next_client_idx
        if len(candidates) > 1:
            client_index = candidates[next_client_idx % len(candidates)]
            next_client_idx += 1
        else:
            client_index = candidates[0]

        faster_client = multi_clients[client_index]
        work_loads[client_index] += 1
        # --- End Load Balancing ---

        if faster_client not in class_cache:
            class_cache[faster_client] = ByteStreamer(faster_client)
        tg_connect = class_cache[faster_client]

        file_id = await tg_connect.get_file_properties(message_id)
        file_size = file_id.file_size
        # --- Get filename for download ---
        file_name = file_id.file_name or f"keralacaptain_download_{message_id}.mp4"
        # Sanitize filename (optional but recommended)
        file_name = re.sub(r'[\\/*?:"<>|]', "", file_name) # Remove invalid characters


        from_bytes = 0
        to_bytes = file_size - 1 # Default range end

        if range_header:
            try:
                range_spec = range_header.strip().replace("bytes=", "")
                if "-" in range_spec:
                     start, end = range_spec.split("-", 1)
                     from_bytes = int(start) if start else 0
                     to_bytes = int(end) if end else file_size - 1
                else: # e.g. bytes=500 (single byte request, unusual)
                     from_bytes = int(range_spec)
                     to_bytes = from_bytes # Only one byte
            except ValueError:
                 return web.Response(status=400, text="Invalid Range header")

        if from_bytes >= file_size or to_bytes >= file_size or from_bytes > to_bytes:
             LOGGER.warning(f"Invalid range request for {message_id}: Range {range_header}, Size {file_size}")
             return web.Response(status=416, reason="Range Not Satisfiable", headers={'Content-Range': f'bytes */{file_size}'})

        # Calculate chunk size and offset for Telegram download
        chunk_size = 1024 * 1024 # 1MB chunks from Telegram
        offset = from_bytes - (from_bytes % chunk_size)
        first_part_cut = from_bytes - offset
        length = (to_bytes - from_bytes) + 1 # Bytes to send in this request

        # --- Prepare Response Headers ---
        status_code = 206 if range_header else 200
        headers = {
            "Content-Type": file_id.mime_type or "application/octet-stream", # Default to generic stream if unknown
            "Accept-Ranges": "bytes",
            # --- Tell browser to download ---
            "Content-Disposition": f"attachment; filename=\"{file_name}\"",
             # --- Allow download from any origin (your ad pages) ---
             "Access-Control-Allow-Origin": "*",
             "Access-Control-Allow-Headers": "Range", # Allow Range header
             "Access-Control-Expose-Headers": "Content-Range, Content-Length, Content-Disposition" # Expose necessary headers
        }

        if range_header:
            headers["Content-Range"] = f"bytes {from_bytes}-{to_bytes}/{file_size}"
            headers["Content-Length"] = str(length)
        else:
            headers["Content-Length"] = str(file_size) # Full file length

        resp = web.StreamResponse(status=status_code, headers=headers)
        await resp.prepare(request)

        # --- Stream the file chunks ---
        body_generator = tg_connect.yield_file(file_id, offset, chunk_size, message_id)
        bytes_sent_in_current_request = 0
        is_first_chunk = True

        try:
            async for chunk in body_generator:
                if bytes_sent_in_current_request >= length:
                     break # Stop sending if we've fulfilled the requested range length

                data_to_write = chunk
                if is_first_chunk and first_part_cut > 0:
                     data_to_write = chunk[first_part_cut:]
                     is_first_chunk = False

                # Ensure we don't send more bytes than requested in the range
                remaining_bytes_in_request = length - bytes_sent_in_current_request
                if len(data_to_write) > remaining_bytes_in_request:
                     data_to_write = data_to_write[:remaining_bytes_in_request]

                await resp.write(data_to_write)
                bytes_sent_in_current_request += len(data_to_write)

        except (ConnectionError, asyncio.CancelledError, ConnectionResetError) as e:
            LOGGER.warning(f"Client disconnected or connection reset for message {message_id}: {type(e).__name__}")
            # No need to return resp, connection is likely closed
            global stream_errors
            stream_errors += 1
            return # Simply stop processing
        except FileReferenceExpired as e:
            LOGGER.error(f"FATAL DOWNLOAD ERROR (FileReferenceExpired) for {message_id}. Client needs to refresh.")
            # Can't easily recover here for a download, best to let the client retry
            return web.Response(status=410, text="Download link expired, please go back and generate a new link.")
        except Exception as e:
            LOGGER.critical(f"Unhandled download error for {message_id}: {e}", exc_info=True)
            stream_errors += 1
            # Don't return a full response if headers already sent
            # Best we can do is log and hope the client handles the broken stream
            return

        finally:
            if client_index is not None and client_index in work_loads: # Check if index exists before decrementing
                work_loads[client_index] -= 1
                LOGGER.debug(f"Decremented workload for client {client_index}. Current workloads: {work_loads}")

        return resp # Return the completed StreamResponse if no errors during write


async def web_server():
    web_app = web.Application(client_max_size=30_000_000)
    web_app.add_routes(routes)
    return web_app
        
# -------------------------------------------------------------------------------- #
# BOT & CLIENT INITIALIZATION (CLEANED)
# -------------------------------------------------------------------------------- #

main_bot = Client("KeralaCaptainBot", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)
# REMOVED: backup_bot (no longer needed)

class TokenParser:
    def parse_from_env(self):
        return {c + 2: t for c, (_, t) in enumerate(filter(lambda n: n[0].startswith("MULTI_TOKEN"), sorted(os.environ.items())))}

async def initialize_clients():
    multi_clients[0] = main_bot
    work_loads[0] = 0
    # REMOVED: backup_bot logic (no longer needed)
    
    all_tokens = TokenParser().parse_from_env()
    if not all_tokens:
        LOGGER.info("No additional clients found.")
        return

    async def start_client(client_id, token):
        try:
            client = await Client(name=str(client_id), api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=token, no_updates=True, in_memory=True).start()
            work_loads[client_id] = 0
            return client_id, client
        except Exception as e:
            LOGGER.error(f"Failed to start Client {client_id}: {e}")
            return None

    clients = await asyncio.gather(*[start_client(i, token) for i, token in all_tokens.items()])
    multi_clients.update({cid: client for cid, client in clients if client is not None})
    
    if len(multi_clients) > 1:
        LOGGER.info(f"Successfully initialized {len(multi_clients)} clients. Multi-Client mode is ON.")

async def forward_file_safely(message_to_forward: Message):
    """
    Sends a file to the log channel using send_cached_media.
    (Kept for FileReferenceExpired logic)
    (REMOVED: Backup bot logic)
    """
    try:
        media = message_to_forward.document or message_to_forward.video
        if not media:
            LOGGER.error("Message has no media to send.")
            return None
            
        file_id = media.file_id
            
        LOGGER.info(f"Sending cached media for message {message_to_forward.id} using main bot...")
        return await main_bot.send_cached_media(
            chat_id=Config.LOG_CHANNEL_ID,
            file_id=file_id,
            caption=getattr(message_to_forward, 'caption', '')
        )
            
    except Exception as e:
        LOGGER.error(f"Main bot failed to send cached media: {e}")
        return None

# -------------------------------------------------------------------------------- #
# NEW BOT HANDLERS (ADMIN ONLY)
# -------------------------------------------------------------------------------- #

# --- NEW: Admin filter ---
admin_only = filters.user(Config.ADMIN_IDS)

@main_bot.on_message(filters.command("start") & filters.private & admin_only)
async def start_command(client, message):
    await message.reply_text(
        "**👋 Welcome, Admin!**\n\nThis is your streaming bot's control panel. What would you like to do?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
            [InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart")]
        ])
    )
    # Clear any old conversation state
    await update_user_conversation(message.chat.id, None)

@main_bot.on_callback_query(filters.regex("^admin_stats$") & admin_only)
async def stats_callback(client, cb: CallbackQuery):
    await cb.answer("Fetching stats...")
    
    # Uptime
    uptime = get_readable_time(time.time() - start_time)
    
    # System Stats
    try:
        cpu_usage = psutil.cpu_percent()
        ram_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage('/').percent
        ram_total = humanbytes(psutil.virtual_memory().total)
    except Exception as e:
        LOGGER.warning(f"Could not fetch system stats: {e}")
        cpu_usage = ram_usage = disk_usage = "N/A"
        ram_total = "N/A"

    # Bot Stats
    active_clients = len(multi_clients)
    workload_str = "\n".join([f"  - Client {cid}: {load} streams" for cid, load in work_loads.items()])

    text = f"**📊 Bot Statistics**\n\n" \
           f"**Uptime:** `{uptime}`\n\n" \
           f"**System:**\n" \
           f"  - CPU: `{cpu_usage}%`\n" \
           f"  - RAM: `{ram_usage}%` (Total: `{ram_total}`)\n" \
           f"  - Disk: `{disk_usage}%`\n\n" \
           f"**Streaming:**\n" \
           f"  - Active Clients: `{active_clients}`\n" \
           f"  - Stream Errors (last min): `{stream_errors}`\n" \
           f"  - Current Workloads:\n{workload_str}"
           
    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_main_menu")]])
    )

@main_bot.on_callback_query(filters.regex("^admin_settings$") & admin_only)
async def settings_callback(client, cb: CallbackQuery):
    await cb.answer()
    current_domain = await get_protected_domain() # Fetch fresh from DB
    
    text = f"**⚙️ Settings**\n\n" \
           f"**Protected Domain:**\n" \
           f"The bot will only allow streaming requests from this URL (Referer).\n\n" \
           f"Current Value: `{current_domain}`"
           
    await cb.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Set New Domain", callback_data="admin_set_domain")],
            [InlineKeyboardButton("⬅️ Back", callback_data="admin_main_menu")]
        ])
    )

@main_bot.on_callback_query(filters.regex("^admin_set_domain$") & admin_only)
async def set_domain_callback(client, cb: CallbackQuery):
    await cb.answer()
    await update_user_conversation(cb.message.chat.id, {"stage": "awaiting_domain"})
    await cb.message.edit_text(
        "**✏️ Set New Domain**\n\n"
        "Please send the new domain you want to protect.\n\n"
        "Example: `https://keralacaptain.in` or `keralacaptain.in`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel_conv")]])
    )

@main_bot.on_callback_query(filters.regex("^admin_restart$") & admin_only)
async def restart_callback(client, cb: CallbackQuery):
    await cb.answer()
    await cb.message.edit_text(
        "**⚠️ Are you sure?**\n\nThis will perform a full restart of the bot. This is required for some updates to take effect.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Yes, Restart", callback_data="admin_restart_confirm"),
                InlineKeyboardButton("❌ No, Go Back", callback_data="admin_main_menu")
            ]
        ])
    )

@main_bot.on_callback_query(filters.regex("^admin_restart_confirm$") & admin_only)
async def restart_confirm_callback(client, cb: CallbackQuery):
    await cb.answer("Restarting...")
    await cb.message.edit_text("✅ **Restarting...**\n\nBot will be back online shortly.")
    
    # --- Real Restart Logic ---
    # This replaces the current process with a new one
    try:
        LOGGER.info("RESTART triggered by admin.")
        # Clean up clients before exit
        if main_bot and main_bot.is_connected:
            await main_bot.stop()
        # REMOVED: backup_bot
    except Exception as e:
        LOGGER.error(f"Error during pre-restart cleanup: {e}")
    
    # Execute a new instance of the bot
    os.execl(sys.executable, sys.executable, *sys.argv)

@main_bot.on_callback_query(filters.regex("^(admin_main_menu|admin_cancel_conv)$") & admin_only)
async def main_menu_callback(client, cb: CallbackQuery):
    await cb.answer()
    await update_user_conversation(cb.message.chat.id, None)
    await cb.message.edit_text(
        "**👋 Welcome, Admin!**\n\nThis is your streaming bot's control panel. What would you like to do?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
            [InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
            [InlineKeyboardButton("🔄 Restart Bot", callback_data="admin_restart")]
        ])
    )

@main_bot.on_message(filters.private & filters.text & admin_only)
async def text_message_handler(client, message: Message):
    chat_id = message.chat.id
    conv = await get_user_conversation(chat_id)
    if not conv: return

    stage = conv.get("stage")
    
    if stage == "awaiting_domain":
        new_domain = message.text.strip()
        
        # Validate domain (simple check)
        if "." not in new_domain or " " in new_domain:
            return await message.reply_text("Invalid format. Please send a valid domain like `keralacaptain.in`.")
        
        try:
            status_msg = await message.reply_text("Saving...")
            saved_domain = await set_protected_domain(new_domain) # This also updates the global var
            
            await status_msg.edit_text(
                f"✅ **Success!**\n\nProtected domain has been updated to:\n`{saved_domain}`",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data="admin_settings")]])
            )
            await update_user_conversation(chat_id, None) # Clear state
            
        except Exception as e:
            await status_msg.edit_text(f"❌ **Error!**\nCould not save domain: `{e}`")


# -------------------------------------------------------------------------------- #
# APPLICATION LIFECYCLE (CLEANED)
# -------------------------------------------------------------------------------- #

async def ping_server():
    """Pings the server to keep it alive on platforms like Heroku."""
    while True:
        await asyncio.sleep(Config.PING_INTERVAL)
        try:
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=10)) as session:
                async with session.get(Config.STREAM_URL) as resp:
                    LOGGER.info(f"Pinged server with status: {resp.status}")
        except Exception as e:
            LOGGER.warning(f"Failed to ping server: {e}")

if __name__ == "__main__":
    async def main_startup_shutdown_logic():
        """Handles the graceful startup and shutdown of all services."""
        global CURRENT_PROTECTED_DOMAIN
        
        LOGGER.info(f"Application starting up...")
        
        # --- NEW: Fetch protected domain on startup ---
        LOGGER.info("Fetching protected domain from database...")
        CURRENT_PROTECTED_DOMAIN = await get_protected_domain()
        LOGGER.info(f"Domain loaded: {CURRENT_PROTECTED_DOMAIN}")
        
        # DB Indexing (Kept)
        await media_collection.create_index("tmdb_id", unique=True)
        await media_collection.create_index("wp_post_id", unique=True)
        LOGGER.info("DB indexes ensured.")
        
        try:
            await main_bot.start()
            bot_info = await main_bot.get_me()
            LOGGER.info(f"Main Bot @{bot_info.username} started.")
        except FloodWait as e:
            LOGGER.error(f"Telegram FloodWait on main bot startup. Waiting for {e.value} seconds.")
            await asyncio.sleep(e.value + 5)
            await main_bot.start()
            bot_info = await main_bot.get_me()
            LOGGER.info(f"Main Bot @{bot_info.username} started after wait.")
        except Exception as e:
            LOGGER.critical(f"Failed to start main bot: {e}", exc_info=True)
            raise

        # REMOVED: Backup bot startup logic
            
        await initialize_clients()
        
        if Config.ON_HEROKU:
            asyncio.create_task(ping_server())
        
        web_app = await web_server()
        
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
        await site.start()
        LOGGER.info(f"Web server started on port {Config.PORT}.")
        
        try:
            await main_bot.send_message(Config.ADMIN_IDS[0], "**✅ Bot has restarted and all services are online!**")
        except Exception as e:
            LOGGER.warning(f"Could not send startup message: {e}")
                
        await asyncio.Event().wait()

    loop = asyncio.get_event_loop()

    async def shutdown_handler(sig):
        LOGGER.info(f"Received exit signal {sig.name}... shutting down gracefully.")
        
        if main_bot and main_bot.is_connected:
            LOGGER.info("Stopping main bot...")
            await main_bot.stop()
        # REMOVED: backup_bot shutdown
        
        tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        if tasks:
            LOGGER.info(f"Cancelling {len(tasks)} outstanding tasks...")
            [task.cancel() for task in tasks]
            await asyncio.gather(*tasks, return_exceptions=True)
        
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown_handler(s))
        )

    try:
        LOGGER.info("Application starting up...")
        loop.run_until_complete(main_startup_shutdown_logic())
        loop.run_forever()
    except Exception as e:
        LOGGER.critical(f"A critical error forced the application to stop: {e}", exc_info=True)
    finally:
        LOGGER.info("Event loop stopped. Final cleanup.")
        if loop.is_running():
            loop.stop()
        if not loop.is_closed():
            loop.close()
        LOGGER.info("Shutdown complete. Goodbye!")
