# --- download_worker.py (Version 1.1 - Fixed DB Connection Logic) ---

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
from pyrogram.errors import FloodWait, UserNotParticipant, AuthBytesInvalid, PeerIdInvalid, LimitInvalid, Timeout, FileReferenceExpired, MessageIdInvalid, MessageNotModified
from pyrogram.types import Message, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from pyrogram.session import Session, Auth
from pyrogram.file_id import FileId, FileType
from pyrogram import raw
from pyrogram.raw.types import InputPhotoFileLocation, InputDocumentFileLocation

# -------------------------------------------------------------------------------- #
# KeralaCaptain Bot - Download Worker Engine V1.1                                  #
# Fixed MongoDB connection check.                                                  #
# -------------------------------------------------------------------------------- #

# Load configurations from .env file
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s - %(levelname)s] - %(message)s')
LOGGER = logging.getLogger(__name__)
logging.getLogger("pyrogram").setLevel(logging.WARNING)
logging.getLogger("aiohttp.web").setLevel(logging.ERROR)

# --- Record bot start time ---
start_time = time.time()

class Config:
    # --- Telegram API Credentials ---
    API_ID = int(os.environ.get("API_ID", 0))
    API_HASH = os.environ.get("API_HASH", "")
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "") # Main bot token for this worker instance

    # --- Admin and Domain Config ---
    ADMIN_IDS = list(int(admin_id) for admin_id in os.environ.get("ADMIN_IDS", "0").split()) # IDs allowed to use bot commands
    PROTECTED_DOMAIN = os.environ.get("PROTECTED_DOMAIN", "https://keralacaptain.rf.gd/").rstrip('/') + '/' # Default domain for stream handler

    # --- Database and Channel ---
    MONGO_URI = os.environ.get("MONGO_URI", "") # MongoDB connection string
    LOG_CHANNEL_ID = int(os.environ.get("LOG_CHANNEL_ID", 0)) # Channel where files are stored

    # --- Web Server ---
    STREAM_URL = os.environ.get("STREAM_URL", "").rstrip('/') # Public URL of this worker
    PORT = int(os.environ.get("PORT", 8080)) # Port to run the web server on

    # --- Keep-Alive ---
    PING_INTERVAL = int(os.environ.get("PING_INTERVAL", 1200)) # Interval in seconds to ping self (e.g., for Heroku)
    ON_HEROKU = 'DYNO' in os.environ # Auto-detect Heroku environment


# --- VALIDATE ESSENTIAL CONFIGURATIONS ---
required_vars = [
    Config.API_ID, Config.API_HASH, Config.BOT_TOKEN,
    Config.MONGO_URI, Config.LOG_CHANNEL_ID, Config.STREAM_URL,
    Config.ADMIN_IDS
]
if not all(required_vars) or Config.ADMIN_IDS == [0]:
    LOGGER.critical("FATAL: One or more required variables (API_ID, API_HASH, BOT_TOKEN, MONGO_URI, LOG_CHANNEL_ID, STREAM_URL, ADMIN_IDS) are missing. Cannot start.")
    exit(1)

# --- Global variable for the protected domain (used by stream_handler if present) ---
CURRENT_PROTECTED_DOMAIN = Config.PROTECTED_DOMAIN

# -------------------------------------------------------------------------------- #
# HELPER FUNCTIONS & CLASSES
# -------------------------------------------------------------------------------- #

def humanbytes(size):
    """Converts bytes to human-readable format."""
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

# --- Database Setup ---
try:
    db_client = AsyncIOMotorClient(Config.MONGO_URI)
    # Try to get DB name from URI first
    db = db_client.get_database()
    # --- FIX: Check using 'is None' ---
    if db is None:
        # Fallback to a default name if not in URI
        LOGGER.warning("Database name not found in MONGO_URI, falling back to 'KeralaCaptainBotDB'. Ensure URI includes DB name like '/YourDBName'.")
        db = db_client['KeralaCaptainBotDB']
    # --- End FIX ---
    LOGGER.info(f"Successfully connected to MongoDB and using database: {db.name}") # Log the actual DB name being used
except Exception as e:
    # Log the specific exception for better debugging
    LOGGER.critical(f"FATAL: Could not connect to MongoDB: {e}", exc_info=True)
    exit(1)


# --- Collections ---
# Need media collection to find post_id and update message_ids on FileRefExpired
media_collection = db['media']
# Need user_conversations for admin panel state
user_conversations_col = db['conversations']
# Need settings collection for dynamic domain
settings_collection = db['settings']


# --- Database Functions (Only those needed for Worker/FileRef/Admin) ---

async def get_media_by_post_id(post_id: int):
    """Reads media data from the main collection."""
    try:
        return await media_collection.find_one({"wp_post_id": post_id})
    except Exception as e:
        LOGGER.error(f"Error fetching media by post_id {post_id}: {e}")
        return None

async def update_media_links_in_db(post_id: int, new_message_ids: dict, new_stream_link: str):
    """Updates message IDs and stream link in the main collection."""
    update_query = {
        "$set": {"message_ids": new_message_ids, "stream_link": new_stream_link}
    }
    try:
        result = await media_collection.update_one({"wp_post_id": post_id}, update_query)
        if result.modified_count > 0:
             LOGGER.info(f"Updated message IDs in DB for post {post_id} after FileRefExpired.")
        else:
             LOGGER.warning(f"Did not update message IDs in DB for post {post_id} (post not found or data unchanged).")
        # Optionally update backup collection if it exists - less critical for worker
        # await db['media_backup'].update_one({"wp_post_id": post_id}, update_query)
    except Exception as e:
        LOGGER.error(f"Failed to update message IDs in DB for post {post_id}: {e}")


async def get_user_conversation(chat_id):
    """Retrieves user conversation state for admin panel."""
    try:
        return await user_conversations_col.find_one({"_id": chat_id})
    except Exception as e:
        LOGGER.error(f"Error fetching user conversation for {chat_id}: {e}")
        return None

async def update_user_conversation(chat_id, data):
    """Manages user conversation state for admin panel."""
    try:
        if data:
            await user_conversations_col.update_one({"_id": chat_id}, {"$set": data}, upsert=True)
        else:
            await user_conversations_col.delete_one({"_id": chat_id})
    except Exception as e:
        LOGGER.error(f"Error updating user conversation for {chat_id}: {e}")

async def get_post_id_from_msg_id(msg_id: int):
    """Finds the wp_post_id associated with a Telegram message_id."""
    try:
        # Search within the 'message_ids' list of objects (new format)
        doc = await media_collection.find_one({"message_ids": {"$elemMatch": {"id": msg_id}}})
        if doc:
            return doc.get('wp_post_id')

        # Fallback: Search dictionary values directly (old {quality: id} format)
        cursor = media_collection.find({}) # Find all documents
        async for document in cursor:
            message_ids_data = document.get('message_ids', {})
            if isinstance(message_ids_data, dict):
                # Check values of the dictionary
                if msg_id in message_ids_data.values():
                    return document.get('wp_post_id')
            # Add check for list format here just in case $elemMatch failed? Less likely.

        LOGGER.warning(f"Could not find post_id for message_id {msg_id} in DB.")
        return None # Return None if not found in any format
    except Exception as e:
        LOGGER.error(f"Error searching for post_id from msg_id {msg_id}: {e}")
        return None

async def get_protected_domain() -> str:
    """Fetches the protected domain from settings, returns default if not found."""
    domain = Config.PROTECTED_DOMAIN # Start with default
    try:
        doc = await settings_collection.find_one({"_id": "bot_settings"})
        if doc and "protected_domain" in doc:
            domain = doc["protected_domain"]
    except Exception as e:
        LOGGER.error(f"Could not fetch domain from DB: {e}. Using default.")
    return domain

async def set_protected_domain(new_domain: str):
    """Saves the new protected domain to the database and updates global var."""
    global CURRENT_PROTECTED_DOMAIN
    try:
        if not (new_domain.startswith("https://") or new_domain.startswith("http://")):
            new_domain = "https://" + new_domain # Assume https if no protocol
        if not new_domain.endswith('/'):
            new_domain += '/' # Ensure trailing slash

        await settings_collection.update_one(
            {"_id": "bot_settings"},
            {"$set": {"protected_domain": new_domain}},
            upsert=True
        )
        CURRENT_PROTECTED_DOMAIN = new_domain # Update global variable immediately
        LOGGER.info(f"Protected domain updated in DB: {new_domain}")
        return new_domain
    except Exception as e:
        LOGGER.error(f"Failed to save protected domain {new_domain} to DB: {e}")
        return None # Indicate failure


# -------------------------------------------------------------------------------- #
# STREAMING/DOWNLOADING ENGINE & WEB SERVER
# -------------------------------------------------------------------------------- #

multi_clients = {}      # Stores active Pyrogram client instances {index: client}
work_loads = {}         # Tracks current stream/download count per client {index: count}
class_cache = {}        # Caches ByteStreamer instances {client: streamer_instance}
# REMOVED: processed_media_groups (Not strictly needed without Bot Handlers doing uploads)
next_client_idx = 0     # For round-robin load balancing
stream_errors = 0       # Counter for errors in the last minute (renamed from download_errors for clarity)
last_error_reset = time.time() # Timestamp for resetting error counter


# --- ByteStreamer Class (Handles Telegram File Downloading) ---
class ByteStreamer:
    """Core class for streaming/downloading files from Telegram."""
    def __init__(self, client: Client):
        self.client: Client = client
        self.cached_file_ids = {} # Cache for file properties {message_id: FileId_object}
        self.session_cache = {}   # Cache for media sessions {dc_id: (session, timestamp)}
        self.client_id_str = f"Client {client.me.id}" if client.is_connected else f"Client (starting...)"
        asyncio.create_task(self.clean_cache_regularly())
        LOGGER.info(f"ByteStreamer initialized for {self.client_id_str}")

    async def clean_cache_regularly(self):
        """Clears the file and session caches periodically."""
        while True:
            await asyncio.sleep(1200) # Clean every 20 minutes
            self.cached_file_ids.clear()
            self.session_cache.clear()
            LOGGER.info(f"({self.client_id_str}) Cleared ByteStreamer's cached file properties and media sessions.")

    async def get_file_properties(self, message_id: int):
        """Fetches file properties (size, mime, name) from the log channel message."""
        if message_id in self.cached_file_ids:
            return self.cached_file_ids[message_id]

        LOGGER.debug(f"({self.client_id_str}) Fetching properties for message_id: {message_id}")
        try:
            message = await self.client.get_messages(Config.LOG_CHANNEL_ID, message_id)
        except Exception as e:
            LOGGER.error(f"({self.client_id_str}) Failed to get message {message_id} from log channel {Config.LOG_CHANNEL_ID}: {e}")
            raise FileNotFoundError(f"Message {message_id} not found or inaccessible.")

        if not message or message.empty or not (message.document or message.video):
            LOGGER.warning(f"({self.client_id_str}) Message {message_id} is empty or not media.")
            raise FileNotFoundError(f"Message {message_id} is empty or not valid media.")

        media = message.document or message.video
        try:
            file_id_obj = FileId.decode(media.file_id)
            setattr(file_id_obj, "file_size", media.file_size or 0)
            setattr(file_id_obj, "mime_type", media.mime_type or "application/octet-stream") # Default MIME
            setattr(file_id_obj, "file_name", media.file_name or f"download_{message_id}.mp4") # Default filename

            self.cached_file_ids[message_id] = file_id_obj
            LOGGER.debug(f"({self.client_id_str}) Cached properties for message_id: {message_id}")
            return file_id_obj
        except Exception as e:
            LOGGER.error(f"({self.client_id_str}) Error decoding file_id or setting attributes for message {message_id}: {e}")
            raise ValueError(f"Could not process file properties for message {message_id}.")


    async def generate_media_session(self, file_id_obj: FileId) -> Session:
        """Generates or reuses a Pyrogram Session for the specific DC ID."""
        dc_id = file_id_obj.dc_id
        media_session = self.client.media_sessions.get(dc_id)

        # Check TTL cache first
        if dc_id in self.session_cache:
            session, ts = self.session_cache[dc_id]
            if time.time() - ts < 300: # 5-minute Time-To-Live
                LOGGER.debug(f"({self.client_id_str}) Reusing TTL-cached media session for DC {dc_id}")
                return session

        # Check existing session with a ping
        if media_session:
            try:
                await media_session.send(raw.functions.help.GetConfig(), timeout=10)
                self.session_cache[dc_id] = (media_session, time.time()) # Update cache timestamp
                LOGGER.debug(f"({self.client_id_str}) Reusing pinged media session for DC {dc_id}")
                return media_session
            except Exception as e:
                LOGGER.warning(f"({self.client_id_str}) Existing media session for DC {dc_id} is stale: {e}. Recreating.")
                try: await media_session.stop()
                except: pass
                if dc_id in self.client.media_sessions: del self.client.media_sessions[dc_id]
                media_session = None # Force recreation

        # Create new session if needed
        LOGGER.info(f"({self.client_id_str}) Creating new media session for DC {dc_id}")
        if dc_id != await self.client.storage.dc_id():
            # Create session for a different DC, requires auth export/import
            media_session = Session(self.client, dc_id, await Auth(self.client, dc_id, await self.client.storage.test_mode()).create(), await self.client.storage.test_mode(), is_media=True)
            await media_session.start()
            # Retry auth import multiple times
            for i in range(3):
                try:
                    exported_auth = await self.client.invoke(raw.functions.auth.ExportAuthorization(dc_id=dc_id))
                    await media_session.send(raw.functions.auth.ImportAuthorization(id=exported_auth.id, bytes=exported_auth.bytes))
                    LOGGER.info(f"({self.client_id_str}) Successfully imported authorization to DC {dc_id}")
                    break # Success
                except AuthBytesInvalid as e:
                    LOGGER.warning(f"({self.client_id_str}) AuthBytesInvalid on import attempt {i+1} for DC {dc_id}: {e}")
                    if i == 2: raise # Raise after final attempt
                    await asyncio.sleep(1) # Wait before retry
                except Exception as e:
                     LOGGER.error(f"({self.client_id_str}) Unexpected error during auth import for DC {dc_id}: {e}", exc_info=True)
                     raise # Re-raise unexpected errors
        else:
            # Create session for the primary DC (uses existing auth key)
            media_session = Session(self.client, dc_id, await self.client.storage.auth_key(), await self.client.storage.test_mode(), is_media=True)
            await media_session.start()

        self.client.media_sessions[dc_id] = media_session
        self.session_cache[dc_id] = (media_session, time.time()) # Cache the new session
        return media_session

    @staticmethod
    def get_location(file_id_obj: FileId):
        """Gets the raw Pyrogram InputFileLocation object needed for downloading."""
        if file_id_obj.file_type == FileType.PHOTO:
            # Photos use InputPhotoFileLocation
            return InputPhotoFileLocation(
                id=file_id_obj.media_id,
                access_hash=file_id_obj.access_hash,
                file_reference=file_id_obj.file_reference,
                thumb_size=file_id_obj.thumbnail_size
            )
        else:
            # Videos and Documents use InputDocumentFileLocation
            return InputDocumentFileLocation(
                id=file_id_obj.media_id,
                access_hash=file_id_obj.access_hash,
                file_reference=file_id_obj.file_reference,
                thumb_size=file_id_obj.thumbnail_size
            )

    async def yield_file(self, file_id_obj: FileId, offset: int, chunk_size: int, original_message_id: int):
        """Asynchronously generates file chunks from Telegram."""
        media_session = await self.generate_media_session(file_id_obj)
        location = self.get_location(file_id_obj)

        current_offset = offset
        retry_count = 0
        max_retries = 3 # Max retries for FileReferenceExpired

        while True:
            try:
                # Request a chunk from Telegram
                chunk_result = await media_session.send(
                    raw.functions.upload.GetFile(location=location, offset=current_offset, limit=chunk_size),
                    timeout=30 # 30-second timeout for the download request
                )

                if isinstance(chunk_result, raw.types.upload.File) and chunk_result.bytes:
                    yield chunk_result.bytes # Send the received bytes to the web handler
                    # Stop if this is the last chunk
                    if len(chunk_result.bytes) < chunk_size:
                        break
                    # Move to the next offset
                    current_offset += len(chunk_result.bytes)
                    retry_count = 0 # Reset retry count on success
                else:
                    # No more data or unexpected response
                    LOGGER.warning(f"({self.client_id_str}) Received unexpected response or empty chunk for msg {original_message_id} at offset {current_offset}.")
                    break

            except FileReferenceExpired:
                retry_count += 1
                if retry_count > max_retries:
                    LOGGER.error(f"({self.client_id_str}) FileReferenceExpired max retries exceeded for message {original_message_id}.")
                    raise # Propagate the error after retries

                LOGGER.warning(f"({self.client_id_str}) FileReferenceExpired for msg {original_message_id}, retry {retry_count}/{max_retries}. Refreshing...")

                try:
                    # Get the original message object from the log channel
                    # Use the main bot (client 0) to get message reliably
                    main_client = multi_clients.get(0)
                    if not main_client: raise Exception("Main client (0) not found for getting message.")
                    original_msg = await main_client.get_messages(Config.LOG_CHANNEL_ID, original_message_id)
                    if not original_msg: raise Exception("Original message not found in log channel.")

                    # Re-forward the message using the main bot instance to get a new file reference
                    refreshed_msg = await forward_file_safely(original_msg) # Uses client 0 implicitly
                    if not refreshed_msg: raise Exception("Failed to forward message for refreshing.")

                    # --- Update internal state with new file info ---
                    new_file_id_obj = await self.get_file_properties(refreshed_msg.id) # Get new FileId object using current client
                    self.cached_file_ids[original_message_id] = new_file_id_obj # Update cache (use original ID as key)
                    location = self.get_location(new_file_id_obj) # Update the location object for the next GetFile call
                    LOGGER.info(f"({self.client_id_str}) File reference refreshed for message {original_message_id}. New log message ID: {refreshed_msg.id}")
                    # -----------------------------------------------

                    # --- Update Database ---
                    # Find the corresponding wp_post_id using the original message ID
                    post_id = await get_post_id_from_msg_id(original_message_id)
                    if post_id:
                        media_doc = await get_media_by_post_id(post_id)
                        if media_doc:
                            old_qualities = media_doc.get('message_ids', {})
                            new_qualities = {}
                            found_and_updated = False

                            # Handle both list and dict formats in DB
                            if isinstance(old_qualities, list):
                                new_qualities = []
                                for item in old_qualities:
                                    # Create a copy to modify
                                    new_item = item.copy() if isinstance(item, dict) else item
                                    if isinstance(new_item, dict) and new_item.get('id') == original_message_id:
                                        new_item['id'] = refreshed_msg.id # Update ID in the copy
                                        found_and_updated = True
                                    new_qualities.append(new_item) # Append original or modified item
                            elif isinstance(old_qualities, dict):
                                new_qualities = old_qualities.copy()
                                for quality, identifier in old_qualities.items():
                                     current_id = identifier if isinstance(identifier, int) else identifier.get('id')
                                     if current_id == original_message_id:
                                         if isinstance(identifier, int):
                                             new_qualities[quality] = refreshed_msg.id
                                         elif isinstance(identifier, dict): # Should ideally not happen in old format, but handle
                                             new_item = identifier.copy()
                                             new_item['id'] = refreshed_msg.id
                                             new_qualities[quality] = new_item
                                         found_and_updated = True
                                         break

                            if found_and_updated:
                                await update_media_links_in_db(post_id, new_qualities, media_doc.get('stream_link', '')) # Update DB
                            else:
                                LOGGER.warning(f"({self.client_id_str}) Could not find message ID {original_message_id} in DB quality data for post {post_id} during FileRef update.")
                        else:
                             LOGGER.warning(f"({self.client_id_str}) Media document not found for post_id {post_id} during FileRef update.")
                    else:
                        LOGGER.warning(f"({self.client_id_str}) Could not find post_id for message {original_message_id} during FileRef update.")
                    # ----------------------

                    await asyncio.sleep(2) # Short delay before retrying GetFile
                    continue # Retry the GetFile call with the new file reference

                except Exception as refresh_err:
                     LOGGER.error(f"({self.client_id_str}) Failed to refresh file reference for message {original_message_id}: {refresh_err}", exc_info=True)
                     # Raise the original error if refresh fails, letting the handler decide response
                     raise FileReferenceExpired(f"Failed to automatically refresh file reference for {original_message_id}.")


            except FloodWait as e:
                LOGGER.warning(f"({self.client_id_str}) FloodWait of {e.value} seconds on get_file for message {original_message_id}. Waiting...")
                await asyncio.sleep(e.value + 2) # Wait a bit longer than required
                continue # Retry the GetFile call

            except Timeout:
                 LOGGER.warning(f"({self.client_id_str}) Timeout occurred while fetching chunk for message {original_message_id}. Retrying...")
                 await asyncio.sleep(1)
                 continue # Retry immediately

            except Exception as e:
                LOGGER.error(f"({self.client_id_str}) Unexpected error in yield_file for message {original_message_id}: {e}", exc_info=True)
                raise # Propagate unexpected errors


# --- aiohttp Web Server Routes ---
routes = web.RouteTableDef()

@routes.get("/", allow_head=True)
async def root_route_handler(request):
    """Handles requests to the root URL."""
    return web.Response(text="KeralaCaptain Download Worker is online!", content_type='text/html')

@routes.get("/health")
async def health_handler(request):
    """Provides health status and basic stats."""
    global stream_errors, last_error_reset
    # Reset error count every minute
    if time.time() - last_error_reset > 60:
        stream_errors = 0
        last_error_reset = time.time()

    active_client_count = len(multi_clients)
    cache_items = 0
    if multi_clients:
        # Get cache size from the streamer instance of the first client (as an estimate)
        first_client = next(iter(multi_clients.values()), None)
        if first_client and first_client in class_cache:
            cache_items = len(class_cache[first_client].cached_file_ids)

    return web.json_response({
        "status": "ok",
        "active_clients": active_client_count,
        "approx_cached_files": cache_items,
        "download_errors_last_minute": stream_errors,
        "current_workloads": work_loads, # Show load per client
    })

@routes.get("/favicon.ico")
async def favicon_handler(request):
    """Handles favicon requests (returns no content)."""
    return web.Response(status=204)

# --- NEW: Download Handler ---
@routes.get(r"/download/{message_id:\d+}") # Route changed to /download/
async def download_handler(request: web.Request):
    """Handles file download requests with range support."""
    client_index = None # Ensure index is defined for finally block
    request_start_time = time.time() # For logging duration
    message_id = int(request.match_info['message_id'])
    LOGGER.info(f"Download request received for message_id: {message_id} from {request.remote}")

    try:
        # --- REFERER CHECK REMOVED ---
        # No security check based on Referer header for the download route

        range_header = request.headers.get("Range") # Keep Range for resumable downloads

        # --- Load Balancing ---
        if not work_loads:
            LOGGER.error("Load balancing error: work_loads dictionary is empty.")
            return web.Response(status=503, text="Service temporarily unavailable (no workers).")

        # Find the minimum load value among available clients
        min_load = min(work_loads.values())
        # Get a list of client indices that have the minimum load
        candidates = [cid for cid, load in work_loads.items() if load == min_load]

        if not candidates:
            LOGGER.warning("Load balancing warning: No candidates found at min_load. Falling back.")
            candidates = list(work_loads.keys()) # Use all available clients as candidates
            if not candidates:
                LOGGER.error("Load balancing error: No clients available.")
                return web.Response(status=503, text="Service temporarily unavailable (no clients).")

        global next_client_idx
        if len(candidates) > 1:
            # If multiple clients have the same min load, use round-robin
            client_index = candidates[next_client_idx % len(candidates)]
            next_client_idx += 1
        else:
            # If only one client has the min load, select it
            client_index = candidates[0]

        faster_client = multi_clients[client_index]
        work_loads[client_index] += 1 # Increment load for the selected client
        LOGGER.debug(f"Assigned message {message_id} to client {client_index}. New workloads: {work_loads}")
        # --- End Load Balancing ---

        # Get or create ByteStreamer instance for the selected client
        if faster_client not in class_cache:
            class_cache[faster_client] = ByteStreamer(faster_client)
        tg_connect = class_cache[faster_client]

        # Get file properties (size, name, mime)
        file_id_obj = await tg_connect.get_file_properties(message_id)
        file_size = file_id_obj.file_size
        file_name = file_id_obj.file_name or f"download_{message_id}.mp4"
        # Sanitize filename
        file_name = re.sub(r'[\\/*?:"<>|]', "_", file_name) # Replace invalid chars with underscore

        # --- Parse Range Header ---
        from_bytes = 0
        to_bytes = file_size - 1

        if range_header:
            range_header = range_header.strip()
            if not range_header.startswith("bytes="):
                 LOGGER.warning(f"Malformed Range header for {message_id}: {range_header}")
                 return web.Response(status=400, text="Malformed Range header")
            try:
                range_spec = range_header.replace("bytes=", "")
                if "-" in range_spec:
                     start, end = range_spec.split("-", 1)
                     from_bytes = int(start) if start else 0
                     to_bytes = int(end) if end else file_size - 1
                else: # e.g., bytes=500 (uncommon)
                     from_bytes = int(range_spec)
                     to_bytes = from_bytes # Only requesting a single byte
            except ValueError:
                 LOGGER.warning(f"Invalid Range values for {message_id}: {range_header}")
                 return web.Response(status=400, text="Invalid Range values")

        # Validate range
        if from_bytes < 0 or to_bytes < 0 or to_bytes >= file_size or from_bytes > to_bytes:
             LOGGER.warning(f"Range Not Satisfiable for {message_id}: Range {range_header}, Size {file_size}")
             # Return 416 Range Not Satisfiable
             return web.Response(status=416, reason="Range Not Satisfiable", headers={'Content-Range': f'bytes */{file_size}'})
        # --- End Range Parsing ---

        # Calculate chunk details for Telegram download
        tg_chunk_size = 1024 * 1024 # Fetch 1MB chunks from Telegram
        # Start fetching from the beginning of the Telegram chunk containing `from_bytes`
        offset = (from_bytes // tg_chunk_size) * tg_chunk_size
        # How many bytes to discard from the first fetched chunk
        first_part_cut = from_bytes % tg_chunk_size
        # Total bytes to send in this HTTP response
        length = (to_bytes - from_bytes) + 1

        # --- Prepare Response ---
        status_code = 206 if range_header else 200
        headers = {
            "Content-Type": file_id_obj.mime_type or "application/octet-stream",
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"attachment; filename=\"{file_name}\"", # Force download
            "Access-Control-Allow-Origin": "*", # Allow cross-origin requests (from ad pages)
            "Access-Control-Allow-Headers": "Range, Origin, X-Requested-With, Content-Type, Accept", # Allow necessary headers
            "Access-Control-Expose-Headers": "Content-Range, Content-Length, Content-Disposition, Accept-Ranges" # Expose headers to JS
        }
        if status_code == 206: # Partial content
            headers["Content-Range"] = f"bytes {from_bytes}-{to_bytes}/{file_size}"
        headers["Content-Length"] = str(length)

        resp = web.StreamResponse(status=status_code, headers=headers)
        await resp.prepare(request) # Send headers to client
        LOGGER.info(f"Prepared response for {message_id}. Status: {status_code}, Range: {headers.get('Content-Range', 'Full')}")


        # --- Stream the file chunks from Telegram to Client ---
        body_generator = tg_connect.yield_file(file_id_obj, offset, tg_chunk_size, message_id)
        bytes_sent_in_current_request = 0
        is_first_chunk_yielded = True

        try:
            async for chunk in body_generator:
                
                # --- ✅ തിരുത്തിയ ഭാഗം (FIXED PART) ---
                # ആ 'if resp.transport...' എന്ന 3 വരികൾ ഇവിടെ നിന്ന് നീക്കം ചെയ്തു
                # The 3 'if resp.transport...' lines were removed from here
                
                if bytes_sent_in_current_request >= length:
                     LOGGER.debug(f"Finished sending range for {message_id}. Sent: {bytes_sent_in_current_request}, Requested length: {length}")
                     break # Stop if we've sent the requested number of bytes

                data_to_write = chunk
                # Discard beginning part of the first chunk if needed
                if is_first_chunk_yielded and first_part_cut > 0:
                     if len(chunk) > first_part_cut:
                         data_to_write = chunk[first_part_cut:]
                     else:
                         # This chunk is smaller than the part we need to cut, skip it entirely? Log this.
                         LOGGER.warning(f"First chunk for {message_id} smaller than cut offset. Skipping chunk.")
                         is_first_chunk_yielded = False # Still mark as processed
                         continue # Skip writing this chunk
                     is_first_chunk_yielded = False

                # Ensure we don't send more bytes than requested in the range
                remaining_bytes_in_request = length - bytes_sent_in_current_request
                if len(data_to_write) > remaining_bytes_in_request:
                     data_to_write = data_to_write[:remaining_bytes_in_request]

                # Write the (potentially modified) chunk to the client
                await resp.write(data_to_write)
                bytes_sent_in_current_request += len(data_to_write)

                # Optional: Add small sleep to prevent overwhelming network?
                # await asyncio.sleep(0.005) # 5 milliseconds

            # Check if we sent exactly the number of bytes requested after loop finishes
            if bytes_sent_in_current_request < length:
                 LOGGER.warning(f"Stream ended for {message_id} before fulfilling range. Sent: {bytes_sent_in_current_request}, Expected: {length}. Range: {range_header}")
                 # This might happen if the file is smaller than expected or TG stream ends early.

        except (ConnectionError, asyncio.CancelledError, ConnectionResetError) as e:
            # Client disconnected or network issue
            LOGGER.warning(f"Client connection error during download stream for message {message_id}: {type(e).__name__}")
            global stream_errors
            stream_errors += 1
            return resp # Stop processing, connection closed


        except FileReferenceExpired:
            LOGGER.error(f"Download failed for {message_id}: FileReferenceExpired and could not be refreshed.")
            if not resp.prepared:
                return web.Response(status=410, text="Download link expired, please generate a new link.")
            else:
                LOGGER.error(f"Cannot send 410, headers already prepared for {message_id}")
                return resp # Stop sending data

        except Exception as e:
            # Catch any other unexpected errors during streaming
            LOGGER.critical(f"Unhandled error during download stream for {message_id}: {e}", exc_info=True)
            stream_errors += 1
            if not resp.prepared:
                return web.Response(status=500, text="Internal Server Error during download")
            else:
                LOGGER.error(f"Cannot send 500, headers already prepared for {message_id}")
                return resp # Stop sending data

        finally:
            # Ensure workload is decremented even if errors occur
            if client_index is not None and client_index in work_loads:
                work_loads[client_index] -= 1
                LOGGER.debug(f"Decremented workload for client {client_index}. Current: {work_loads}")
            duration = time.time() - request_start_time
            LOGGER.info(f"Download request for {message_id} finished. Sent {bytes_sent_in_current_request} bytes. Duration: {duration:.2f}s")

        return resp # Return the completed stream response

    except FileNotFoundError:
        LOGGER.warning(f"Download request failed for message_id {message_id}: File not found or inaccessible.")
        return web.Response(status=404, text="File not found")
    except ValueError as e: # Catch potential errors from get_file_properties decoding etc.
        LOGGER.error(f"Download request failed for message_id {message_id} due to ValueError: {e}")
        return web.Response(status=500, text="Error processing file properties")
    except Exception as e:
        # Catch errors before load balancing or getting properties
        LOGGER.critical(f"Unhandled error processing download request for {message_id}: {e}", exc_info=True)
        stream_errors += 1
        return web.Response(status=500, text="Internal Server Error")


# -------------------------------------------------------------------------------- #
# BOT & CLIENT INITIALIZATION
# -------------------------------------------------------------------------------- #

main_bot = Client("KeralaCaptainWorker", api_id=Config.API_ID, api_hash=Config.API_HASH, bot_token=Config.BOT_TOKEN)

class TokenParser:
    """Parses multi-client tokens (MULTI_TOKEN_*) from environment variables."""
    def parse_from_env(self):
        # Start client index from 1 for additional tokens
        return {index + 1: token for index, (_, token) in enumerate(
            filter(lambda item: item[0].startswith("MULTI_TOKEN"), sorted(os.environ.items()))
        )}

async def initialize_clients():
    """Initializes the main bot client and any additional multi-clients."""
    multi_clients[0] = main_bot # Main bot is always client 0
    work_loads[0] = 0

    all_tokens = TokenParser().parse_from_env()
    if not all_tokens:
        LOGGER.info("No additional MULTI_TOKEN clients found.")
        return

    async def start_client(client_id, token):
        """Starts an individual client instance."""
        try:
            # Use unique session names for each client
            session_name = f"worker_client_{client_id}"
            # Use file-based sessions for stability in long-running workers
            client = await Client(
                name=session_name,
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=token,
                no_updates=True, # Worker bots don't need to process updates
                workdir="./sessions" # Store session files in a subdirectory
            ).start()
            work_loads[client_id] = 0
            LOGGER.info(f"Successfully started Client {client_id} (ID: {client.me.id})")
            return client_id, client
        except Exception as e:
            LOGGER.error(f"Failed to start Client {client_id}: {e}", exc_info=True)
            return None

    # Start all additional clients concurrently
    # Ensure sessions directory exists
    os.makedirs("./sessions", exist_ok=True)
    client_results = await asyncio.gather(*[start_client(i, token) for i, token in all_tokens.items()])
    # Add successfully started clients to the multi_clients dictionary
    multi_clients.update({cid: client for cid, client in client_results if client is not None})

    if len(multi_clients) > 1:
        LOGGER.info(f"Successfully initialized {len(multi_clients)} clients. Multi-Client mode is ON.")
    elif not multi_clients:
        LOGGER.critical("FATAL: No clients could be initialized. Exiting.")
        exit(1)


async def forward_file_safely(message_to_forward: Message):
    """
    Forwards a file to the log channel using send_cached_media.
    Needed for FileReferenceExpired logic. Uses the main bot instance (client 0).
    """
    try:
        media = message_to_forward.document or message_to_forward.video
        if not media:
            LOGGER.error("forward_file_safely: Message has no media.")
            return None

        file_id = media.file_id
        caption = getattr(message_to_forward, 'caption', '')

        # Always use the main bot (index 0) for forwarding consistency
        fwd_client = multi_clients.get(0)
        if not fwd_client:
             LOGGER.error("forward_file_safely: Main client (index 0) not available.")
             return None

        LOGGER.info(f"Attempting to forward message {message_to_forward.id} to log channel {Config.LOG_CHANNEL_ID}...")
        # Add retry logic for forwarding in case of temporary issues
        for attempt in range(3):
            try:
                sent_message = await fwd_client.send_cached_media(
                    chat_id=Config.LOG_CHANNEL_ID,
                    file_id=file_id,
                    caption=caption # Forward caption if present
                )
                if sent_message:
                    LOGGER.info(f"Successfully forwarded message {message_to_forward.id}, new message ID: {sent_message.id}")
                    return sent_message
            except FloodWait as fw:
                 LOGGER.warning(f"FloodWait ({fw.value}s) during forward attempt {attempt+1}. Retrying...")
                 await asyncio.sleep(fw.value + 2)
            except Exception as e:
                 LOGGER.error(f"Error during forward attempt {attempt+1}: {e}")
                 await asyncio.sleep(2) # Short delay before final retry

        LOGGER.error(f"forward_file_safely: Failed to send cached media after multiple attempts.")
        return None

    except Exception as e:
        LOGGER.error(f"forward_file_safely: Unexpected error: {e}", exc_info=True)
        return None

# -------------------------------------------------------------------------------- #
# BOT HANDLERS (Admin Commands for Worker)
# -------------------------------------------------------------------------------- #

# --- Admin filter ---
admin_only = filters.user(Config.ADMIN_IDS)

@main_bot.on_message(filters.command("start") & filters.private & admin_only)
async def start_command_admin(client, message):
    """Handles /start command for admins."""
    await message.reply_text(
        "**👋 Welcome, Admin!**\n\nThis is a KeralaCaptain Download Worker Bot control panel.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
            [InlineKeyboardButton("⚙️ Domain Setting", callback_data="admin_settings")],
            [InlineKeyboardButton("🔄 Restart Worker", callback_data="admin_restart")]
        ])
    )
    await update_user_conversation(message.chat.id, None)

@main_bot.on_callback_query(filters.regex("^admin_stats$") & admin_only)
async def stats_callback_admin(client, cb: CallbackQuery):
    """Displays worker statistics."""
    await cb.answer("Fetching stats...")

    uptime = get_readable_time(time.time() - start_time)
    try:
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        ram_total = humanbytes(psutil.virtual_memory().total)
        ram_used = humanbytes(psutil.virtual_memory().used)
    except Exception as e:
        LOGGER.warning(f"Could not fetch system stats: {e}")
        cpu = ram = disk = ram_total = ram_used = "N/A"

    active_clients_count = len(multi_clients)
    workload_lines = [f"  - Client {cid}: {load} downloads" for cid, load in work_loads.items()]
    workload_str = "\n".join(workload_lines) if workload_lines else "  - No clients active."

    text = f"""📊 **Worker Bot Statistics**

**Uptime:** `{uptime}`

**System:**
  - CPU: `{cpu}%`
  - RAM: `{ram}%` (`{ram_used}` / `{ram_total}`)
  - Disk: `{disk}%`

**Download Service:**
  - Active Clients: `{active_clients_count}`
  - Errors (last min): `{stream_errors}`
  - Current Workloads:
{workload_str}"""

    try:
        await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="admin_main_menu")]])
        )
    except MessageNotModified:
        pass # Ignore if message content is the same

@main_bot.on_callback_query(filters.regex("^admin_settings$") & admin_only)
async def settings_callback_admin(client, cb: CallbackQuery):
    """Shows current settings (Protected Domain)."""
    await cb.answer()
    current_domain = await get_protected_domain() # Fetch fresh from DB

    text = f"""⚙️ **Worker Settings**

**Protected Domain (for Stream Route):**
This worker uses this domain ONLY if the `/stream/` route is enabled to check the `Referer` header. The `/download/` route does NOT use this check.

Current Value: `{current_domain}`"""

    try:
        await cb.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✏️ Set New Domain", callback_data="admin_set_domain")],
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_main_menu")]
            ])
        )
    except MessageNotModified:
        pass

@main_bot.on_callback_query(filters.regex("^admin_set_domain$") & admin_only)
async def set_domain_callback_admin(client, cb: CallbackQuery):
    """Starts the process to set a new protected domain."""
    await cb.answer()
    await update_user_conversation(cb.message.chat.id, {"stage": "awaiting_domain"})
    try:
        await cb.message.edit_text(
            "**✏️ Set New Domain**\n\n"
            "Send the new domain for the stream route (e.g., `keralacaptain.in`). HTTPS will be added if missing. Include `www.` if needed.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel_conv")]])
        )
    except MessageNotModified:
        pass

@main_bot.on_callback_query(filters.regex("^admin_restart$") & admin_only)
async def restart_callback_admin(client, cb: CallbackQuery):
    """Asks for confirmation before restarting."""
    await cb.answer()
    try:
        await cb.message.edit_text(
            "**⚠️ Restart Worker?**\n\nThis will restart the current worker process.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Yes, Restart", callback_data="admin_restart_confirm"),
                    InlineKeyboardButton("❌ No, Go Back", callback_data="admin_main_menu")
                ]
            ])
        )
    except MessageNotModified:
        pass

@main_bot.on_callback_query(filters.regex("^admin_restart_confirm$") & admin_only)
async def restart_confirm_callback_admin(client, cb: CallbackQuery):
    """Confirms and triggers the restart."""
    await cb.answer("Restarting...")
    try:
        await cb.message.edit_text("✅ **Restarting worker process...**")
    except MessageNotModified: pass

    LOGGER.info("RESTART triggered by admin.")
    # Attempt graceful client stop before exiting
    try:
        await asyncio.sleep(1) # Short delay
        # Stop all clients managed by this worker
        for client_id, client_instance in multi_clients.items():
            if client_instance and client_instance.is_connected:
                 LOGGER.info(f"Stopping client {client_id}...")
                 await client_instance.stop()
        LOGGER.info("Stopped clients before restart.")
    except Exception as e:
        LOGGER.error(f"Error stopping clients during restart: {e}")

    # Replace the current process with a new one
    try:
         LOGGER.info(f"Executing: {sys.executable} {' '.join(sys.argv)}")
         os.execl(sys.executable, sys.executable, *sys.argv)
    except Exception as e:
         LOGGER.critical(f"Failed to execute restart: {e}", exc_info=True)
         # If execl fails, maybe just exit?
         sys.exit(1)


@main_bot.on_callback_query(filters.regex("^(admin_main_menu|admin_cancel_conv)$") & admin_only)
async def main_menu_callback_admin(client, cb: CallbackQuery):
    """Returns to the admin main menu."""
    await cb.answer()
    await update_user_conversation(cb.message.chat.id, None)
    try:
        await cb.message.edit_text(
             "**👋 Welcome, Admin!**\n\nKeralaCaptain Download Worker control panel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Statistics", callback_data="admin_stats")],
                [InlineKeyboardButton("⚙️ Domain Setting", callback_data="admin_settings")],
                [InlineKeyboardButton("🔄 Restart Worker", callback_data="admin_restart")]
            ])
        )
    except MessageNotModified:
        pass

@main_bot.on_message(filters.private & filters.text & admin_only)
async def text_message_handler_admin(client, message: Message):
    """Handles text input for admin commands (setting domain)."""
    chat_id = message.chat.id
    conv = await get_user_conversation(chat_id)
    if not conv: return

    stage = conv.get("stage")

    if stage == "awaiting_domain":
        new_domain = message.text.strip().lower()

        # Improved validation: check for protocol, ensure it's just domain.tld
        if "://" in new_domain or "/" in new_domain:
             return await message.reply_text("Invalid format. Send only the domain name like `mydomain.com` or `sub.mydomain.com`, without `http/https` or `/`.")
        if "." not in new_domain or " " in new_domain:
            return await message.reply_text("Invalid format. Please ensure it's a valid domain name.")

        try:
            status_msg = await message.reply_text("Saving new domain...")
            saved_domain = await set_protected_domain(new_domain) # Saves to DB and updates global var

            if saved_domain:
                await status_msg.edit_text(
                    f"✅ **Domain Updated**\n\nProtected domain for stream route set to:\n`{saved_domain}`",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Settings", callback_data="admin_settings")]])
                )
            else:
                 await status_msg.edit_text("❌ **Error!**\nCould not save domain to database.")

            await update_user_conversation(chat_id, None) # Clear state

        except Exception as e:
            await status_msg.edit_text(f"❌ **Error!**\nUnexpected error saving domain: `{e}`")


# -------------------------------------------------------------------------------- #
# APPLICATION LIFECYCLE
# -------------------------------------------------------------------------------- #

async def ping_server():
    """Periodically pings the server's public URL to keep it alive."""
    if not Config.STREAM_URL:
        LOGGER.warning("STREAM_URL not set. Skipping self-ping.")
        return
    ping_url = Config.STREAM_URL.rstrip('/') + "/health" # Ping health endpoint
    LOGGER.info(f"Self-ping target URL: {ping_url}")
    while True:
        await asyncio.sleep(Config.PING_INTERVAL)
        try:
            async with aiohttp.ClientSession(timeout=ClientTimeout(total=20)) as session: # Increased timeout
                async with session.get(ping_url) as resp:
                    LOGGER.info(f"Self-ping status: {resp.status}")
                    # Optionally log response body on non-200 status for debugging
                    # if resp.status != 200:
                    #     LOGGER.warning(f"Self-ping received non-200 status: {resp.status}, Body: {await resp.text()}")
        except asyncio.TimeoutError:
             LOGGER.warning(f"Self-ping to {ping_url} timed out.")
        except aiohttp.ClientError as e:
            LOGGER.warning(f"Self-ping failed: {e}")
        except Exception as e:
             LOGGER.error(f"Unexpected error during self-ping task: {e}", exc_info=True)


async def web_server():
    """Initializes the aiohttp web application."""
    app = web.Application()
    app.add_routes(routes)
    return app

if __name__ == "__main__":
    async def main_startup_shutdown_logic():
        """Handles the startup and shutdown sequence."""
        global CURRENT_PROTECTED_DOMAIN

        LOGGER.info("Download Worker starting up...")

        # Fetch protected domain from DB on startup
        LOGGER.info("Fetching protected domain setting...")
        CURRENT_PROTECTED_DOMAIN = await get_protected_domain()
        LOGGER.info(f"Protected domain for stream route loaded: {CURRENT_PROTECTED_DOMAIN}")

        # Ensure necessary DB indexes exist (Optional, uncomment if needed)
        # try:
        #     await media_collection.create_index("wp_post_id", background=True)
        #     # Add other indexes if your DB queries require them
        #     LOGGER.info("DB indexes ensured.")
        # except Exception as e:
        #      LOGGER.warning(f"Could not ensure DB indexes: {e}")

        LOGGER.info("DB connection established.")

        # Start the main Pyrogram client for this worker
        try:
            await main_bot.start()
            bot_info = await main_bot.get_me()
            LOGGER.info(f"Main client for worker @{bot_info.username} (ID: {bot_info.id}) started.")
        except FloodWait as e:
            LOGGER.error(f"FloodWait on worker startup. Waiting {e.value}s.")
            await asyncio.sleep(e.value + 5)
            await main_bot.start() # Retry start
            bot_info = await main_bot.get_me()
            LOGGER.info(f"Main client for worker @{bot_info.username} started after wait.")
        except Exception as e:
            LOGGER.critical(f"Failed to start main client for worker: {e}", exc_info=True)
            raise # Stop startup if main client fails

        # Initialize additional clients (MULTI_TOKENs)
        await initialize_clients()

        # Start self-ping task if configured
        if Config.PING_INTERVAL > 0 and (Config.ON_HEROKU or "RENDER" in os.environ): # Also ping on Render
             LOGGER.info(f"Starting self-ping task with interval {Config.PING_INTERVAL}s.")
             asyncio.create_task(ping_server())

        # Start the web server
        web_app = await web_server()
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", Config.PORT)
        try:
            await site.start()
            LOGGER.info(f"Web server started successfully on port {Config.PORT}.")
        except Exception as e:
             LOGGER.critical(f"FATAL: Failed to start web server on port {Config.PORT}: {e}", exc_info=True)
             # Attempt to stop clients before exiting
             for client_instance in multi_clients.values():
                 if client_instance and client_instance.is_connected: await client_instance.stop()
             exit(1)


        # Send startup message to first admin
        if Config.ADMIN_IDS:
            try:
                # Use html formatting for better readability in Telegram
                startup_message = (
                    f"✅ <b>Download Worker Bot Started!</b>\n\n"
                    f"<b>URL:</b> {Config.STREAM_URL}\n"
                    f"<b>Clients Initialized:</b> {len(multi_clients)}\n"
                    f"<b>Protected Domain (Stream):</b> <code>{CURRENT_PROTECTED_DOMAIN}</code>"
                )
                await main_bot.send_message(
                     Config.ADMIN_IDS[0],
                     startup_message,
                     parse_mode=enums.ParseMode.HTML
                )
            except Exception as e:
                LOGGER.warning(f"Could not send startup message to admin {Config.ADMIN_IDS[0]}: {e}")

        # Keep the application running indefinitely
        await asyncio.Event().wait()

    # --- Graceful Shutdown Logic ---
    loop = asyncio.get_event_loop()

    async def shutdown_handler(sig):
        """Handles shutdown signals (SIGINT, SIGTERM)."""
        LOGGER.info(f"Received exit signal {sig.name}. Shutting down worker gracefully...")

        # Stop all Pyrogram clients
        stopped_clients = 0
        tasks = []
        for client_id, client_instance in multi_clients.items():
             if client_instance and client_instance.is_connected:
                 LOGGER.info(f"Stopping client {client_id}...")
                 tasks.append(asyncio.create_task(client_instance.stop()))
                 stopped_clients += 1
        if tasks:
             await asyncio.gather(*tasks, return_exceptions=True) # Wait for stops to complete

        LOGGER.info(f"Stopped {stopped_clients} Pyrogram clients.")

        # Cancel any pending asyncio tasks (like ping_server)
        pending_tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        if pending_tasks:
            LOGGER.info(f"Cancelling {len(pending_tasks)} outstanding tasks...")
            [task.cancel() for task in pending_tasks]
            await asyncio.gather(*pending_tasks, return_exceptions=True) # Wait for tasks to cancel

        # Close MongoDB client
        if db_client:
            LOGGER.info("Closing MongoDB connection...")
            db_client.close()

        loop.stop() # Stop the event loop

    # Register signal handlers
    for sig_name in ('SIGINT', 'SIGTERM'):
         sig = getattr(signal, sig_name, None)
         if sig:
             try:
                 loop.add_signal_handler(
                     sig,
                     lambda s=sig: asyncio.create_task(shutdown_handler(s))
                 )
             except NotImplementedError: # Handle environments where signal handling is limited
                  LOGGER.warning(f"Signal handling for {sig_name} not supported on this platform.")


    # --- Run the Application ---
    try:
        loop.run_until_complete(main_startup_shutdown_logic())
        loop.run_forever() # Keep running until loop.stop() is called
    except KeyboardInterrupt:
         LOGGER.info("KeyboardInterrupt received.")
         if not loop.is_running(): # Start shutdown if loop isn't already stopping
             asyncio.run(shutdown_handler(signal.SIGINT))
    except Exception as e:
        LOGGER.critical(f"A critical error occurred: {e}", exc_info=True)
    finally:
        LOGGER.info("Event loop stopped. Final cleanup...")
        if not loop.is_closed():
             # Run pending tasks like session saving before closing
             loop.run_until_complete(loop.shutdown_asyncgens())
             loop.close()
        LOGGER.info("Download Worker shutdown complete.")
