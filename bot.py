import asyncio
import io
import os
import re
import base64
import codecs
import mimetypes
import threading
import urllib.parse
import sqlite3
import logging
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from google import genai
from google.genai import types
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, TypeHandler, filters, ContextTypes

# --- CONFIGURATION ---
TELEGRAM_TOKEN = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
GEMINI_API_KEY = (os.environ.get("GEMINI_API_KEY") or "").strip()
ADMIN_ID_RAW = (os.environ.get("ADMIN_ID") or "0").strip()

if not TELEGRAM_TOKEN or "YOUR_" in TELEGRAM_TOKEN or "PASTE_" in TELEGRAM_TOKEN:
    raise RuntimeError("Invalid or missing TELEGRAM_TOKEN environment variable!")
if not GEMINI_API_KEY or "YOUR_" in GEMINI_API_KEY or "PASTE_" in GEMINI_API_KEY:
    raise RuntimeError("Invalid or missing GEMINI_API_KEY environment variable!")
if not ADMIN_ID_RAW.isdigit():
    raise RuntimeError(f"ADMIN_ID must be numeric, got: {ADMIN_ID_RAW!r}")

ADMIN_ID = int(ADMIN_ID_RAW)

ai_client = genai.Client(api_key=GEMINI_API_KEY)
chat_histories = {}
AVAILABLE_MODELS = []

TEXT_EXTENSIONS = (
    ".txt", ".py", ".c", ".cpp", ".h", ".hpp", ".js", ".ts", ".php", ".java", ".go",
    ".rs", ".sh", ".bash", ".json", ".xml", ".yaml", ".yml", ".md", ".log",
    ".sql", ".html", ".css", ".conf", ".ini", ".env", ".csv", ".asm", ".s"
)
MAX_EXTRACTED_CHARS = 100_000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ctf_team.db")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- RENDER HEALTH-CHECK WEB SERVER ---
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"CTF AI Bot v2.7 is alive")

    def log_message(self, format, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info(f"Health-check HTTP server listening on port {port}")

# --- PERSISTENT SQLITE DATABASE FOR TEAM NOTES & USER TRACKING ---
def init_db():
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                author TEXT,
                content TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                first_seen TEXT,
                last_seen TEXT,
                msg_count INTEGER DEFAULT 1
            )
        """)
        conn.commit()

def discover_models():
    """Fetches valid generateContent models for this API key, prioritizing active 3.x Flash models."""
    global AVAILABLE_MODELS
    preferred_order = [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
    ]
    try:
        all_valid = [
            m.name.replace("models/", "")
            for m in ai_client.models.list()
            if "generateContent" in (m.supported_actions or [])
            and "gemini" in m.name.lower()
            and "embedding" not in m.name.lower()
            and "image" not in m.name.lower()
            and "tts" not in m.name.lower()
            and "2.5" not in m.name.lower()
            and "2.0" not in m.name.lower()
        ]
        ordered = [m for m in preferred_order if m in all_valid]
        ordered += [m for m in all_valid if m not in ordered]
        AVAILABLE_MODELS = ordered if ordered else ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]
    except Exception as e:
        logging.warning(f"Could not auto-list models ({e}), using defaults.")
        AVAILABLE_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"]

    logging.info(f"Active Gemini fallback chain: {AVAILABLE_MODELS}")

# --- GLOBAL USER TRACKER & OWNER ALERT HANDLER ---
async def track_user_and_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Logs every interacting user to SQLite and sends real-time Telegram alerts to ADMIN_ID."""
    if not update.effective_user or update.effective_user.is_bot:
        return

    user = update.effective_user
    user_id = user.id
    username = f"@{user.username}" if user.username else "No username"
    full_name = user.full_name or "Unknown"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    is_new_user = False
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as conn:
            row = conn.execute("SELECT msg_count FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                is_new_user = True
                conn.execute(
                    "INSERT INTO users (user_id, username, full_name, first_seen, last_seen, msg_count) VALUES (?, ?, ?, ?, ?, 1)",
                    (user_id, username, full_name, now, now),
                )
            else:
                conn.execute(
                    "UPDATE users SET username = ?, full_name = ?, last_seen = ?, msg_count = msg_count + 1 WHERE user_id = ?",
                    (username, full_name, now, user_id),
                )
            conn.commit()
    except Exception as db_err:
        logging.error(f"User tracking DB error: {db_err}")

    if not ADMIN_ID or user_id == ADMIN_ID:
        return

    action_preview = "Interacted with bot"
    if update.message:
        if update.message.text:
            action_preview = update.message.text[:250]
        elif update.message.photo:
            cap = f" — Caption: {update.message.caption[:150]}" if update.message.caption else ""
            action_preview = f"[Sent a Photo{cap}]"
        elif update.message.document:
            fname = update.message.document.file_name or "file"
            cap = f" — Caption: {update.message.caption[:150]}" if update.message.caption else ""
            action_preview = f"[Sent File: {fname}{cap}]"

    chat_type = update.effective_chat.type if update.effective_chat else "private"
    badge = "🆕 NEW USER ALERT" if is_new_user else "📩 BOT ACTIVITY ALERT"
    alert_text = (
        f"{badge}\n"
        f"• Name: {full_name}\n"
        f"• Username: {username}\n"
        f"• User ID: {user_id}\n"
        f"• Chat: {chat_type}\n"
        f"• Message: {action_preview}"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=alert_text)
    except Exception as e:
        logging.warning(f"Could not send alert to ADMIN_ID ({ADMIN_ID}): {e}")

# --- ARCHIVE & DOCX TEXT EXTRACTORS (STDLIB ONLY) ---
def extract_docx_text(file_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        if "word/document.xml" not in zf.namelist():
            return ""
        xml_data = zf.read("word/document.xml")

    root = ET.fromstring(xml_data)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for p in root.findall(".//w:p", ns):
        texts = [t.text for t in p.findall(".//w:t", ns) if t.text]
        if texts:
            paragraphs.append("".join(texts))
    return "\n".join(paragraphs)[:MAX_EXTRACTED_CHARS]

def extract_zip_contents(file_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
        entries = zf.infolist()
        if not entries:
            return "Archive is empty."

        output_parts = ["=== ZIP ARCHIVE FILE LIST ==="]
        for info in entries:
            output_parts.append(f"- {info.filename} ({info.file_size} bytes)")

        total_chars = sum(len(line) for line in output_parts)
        output_parts.append("\n=== EXTRACTED TEXT / CODE FILES ===")

        for info in entries:
            if info.is_dir() or info.file_size == 0:
                continue
            if "__MACOSX/" in info.filename or info.file_size > 2 * 1024 * 1024:
                continue

            lower_name = info.filename.lower()
            try:
                raw = zf.read(info.filename)
            except RuntimeError:
                output_parts.append(f"\n--- {info.filename} [ENCRYPTED / PASSWORD PROTECTED] ---")
                continue

            extracted = ""
            if lower_name.endswith(".docx"):
                try:
                    extracted = extract_docx_text(raw)
                except Exception:
                    extracted = ""
            elif lower_name.endswith(TEXT_EXTENSIONS) or "." not in os.path.basename(lower_name):
                try:
                    decoded = raw.decode("utf-8")
                    sample = decoded[:1000]
                    printable_ratio = sum(ch.isprintable() or ch in "\n\r\t" for ch in sample) / max(len(sample), 1)
                    if printable_ratio > 0.85:
                        extracted = decoded
                except UnicodeDecodeError:
                    pass

            if extracted:
                remaining = MAX_EXTRACTED_CHARS - total_chars
                if remaining <= 200:
                    output_parts.append("\n[Truncated remaining files due to size limit]")
                    break
                snippet = extracted[:remaining]
                output_parts.append(f"\n--- FILE: {info.filename} ---\n{snippet}")
                total_chars += len(snippet)

        return "\n".join(output_parts)

# --- UPGRADED PRO CTF SYSTEM PROMPT ---
SYSTEM_PROMPT = """
You are an elite, battle-hardened CTF (Capture The Flag) Red Team & Security Engineer assisting a competitive team in Telegram.

STRICT RULES FOR EVERY RESPONSE:
1. NO FLUFF OR TEXTBOOK LECTURES: Never give generic beginner tutorials unless explicitly asked. Get straight to exploitation, enumeration, and solving.
2. ACTIONABLE ONE-LINERS: Provide exact, copy-pasteable commands, payloads, or Python exploit scripts (using pwntools, requests, etc.).
3. THINK IN ATTACK CHAINS: When given ports, services, code, images, PDFs, DOCX, ZIP archives, or logs, state:
  - What is vulnerable or hidden and why (1-2 lines).
  - Exact command/payload to test or exploit it right now.
  - What to check next if that fails.
4. TELEGRAM FORMATTING: Use Telegram Markdown (*bold*, `inline code`, and ```bash or ```python code blocks). NEVER use ### headers (Telegram does not render them; use *BOLD UPPERCASE* instead).
5. LANGUAGE: If asked in Khmer, explain concisely in Khmer while keeping all technical terms, commands, and payloads in English.
"""

def get_chat_history(chat_id: int):
    if chat_id not in chat_histories:
        chat_histories[chat_id] = deque(maxlen=15)
    return chat_histories[chat_id]

async def send_formatted_reply(update: Update, text: str):
    if not update.message or not text:
        return

    chunks = []
    remaining = text
    while len(remaining) > 4000:
        split_idx = remaining.rfind("\n", 0, 4000)
        if split_idx == -1:
            split_idx = 4000
        chunks.append(remaining[:split_idx])
        remaining = remaining[split_idx:].lstrip("\n")
    if remaining:
        chunks.append(remaining)

    for chunk in chunks:
        try:
            await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            try:
                await update.message.reply_text(chunk)
            except Exception as send_err:
                logging.error(f"Failed to send Telegram message chunk: {send_err}")

async def ask_gemini(
    chat_id: int,
    user_name: str,
    prompt: str,
    file_bytes: bytes = None,
    mime_type: str = None,
    file_label: str = None,
    memory_note: str = None,
) -> str:
    history = get_chat_history(chat_id)
    if memory_note:
        history.append(f"{user_name} [Uploaded {file_label}]: {prompt}\n[File Context]: {memory_note[:3000]}")
    elif file_label:
        history.append(f"{user_name} [Uploaded {file_label}]: {prompt}")
    else:
        history.append(f"{user_name}: {prompt}")

    conversation_context = "\n".join(history)

    if file_bytes and mime_type:
        contents = [
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
            conversation_context,
        ]
    else:
        contents = conversation_context

    models_to_try = list(AVAILABLE_MODELS[:6]) if AVAILABLE_MODELS else ["gemini-3.8-flash", "gemini-3.7-flash"]
    last_error = None

    for attempt, model_name in enumerate(models_to_try):
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        temperature=0.3,
                    )
                ),
                timeout=45.0
            )
            reply_text = response.text or "No response generated."
            history.append(f"AI Agent: {reply_text}")
            return reply_text
        except Exception as e:
            last_error = e
            err_str = str(e)
            if any(code in err_str for code in ("429", "404", "RESOURCE_EXHAUSTED", "NOT_FOUND")):
                if model_name in AVAILABLE_MODELS:
                    AVAILABLE_MODELS.remove(model_name)
                    AVAILABLE_MODELS.append(model_name)
            logging.warning(f"Attempt {attempt + 1}/{len(models_to_try)} on {model_name} failed ({type(e).__name__}), switching model...")
            await asyncio.sleep(1)

    raise last_error

# --- COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    help_text = (
        "*CTF AI Agent v2.7 Online*\n\n"
        "*Commands:*\n"
        "• `/ask <question>` - Ask for CTF attack vectors, payloads, or hints\n"
        "• `/analyze <code/nmap/log>` - Deep vulnerability & exploit analysis\n"
        "• `/decode <string>` - Auto-decode Base64, Hex, URL, and ROT13\n"
        "• `/save <note>` - Save a flag, port, or credential for the team\n"
        "• `/notes` - View all saved team notes\n"
        "• `/clear` - Reset AI conversation memory for a new challenge\n"
        "• `/users` - View all bot users (Admin only)\n\n"
        "*Multimodal File Analysis:*\n"
        "• Send any *Photo/Screenshot*, *PDF*, *DOCX*, *ZIP archive*, or *TXT/Code file* (add your question in the caption) and I will analyze it directly.\n"
        "_Tip: Reply directly to any of my messages to continue the discussion._"
    )
    await send_formatted_reply(update, help_text)

async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not ADMIN_ID or update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Unauthorized: Admin only command.")
        return

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute(
            "SELECT user_id, username, full_name, first_seen, last_seen, msg_count FROM users ORDER BY last_seen DESC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("No users recorded yet.")
        return

    lines = [f"*BOT USERS ({len(rows)} total):*\n"]
    for idx, (uid, uname, fname, fseen, lseen, count) in enumerate(rows, 1):
        lines.append(
            f"{idx}. *{fname}* ({uname})\n"
            f"   • ID: `{uid}` | Messages: `{count}`\n"
            f"   • Last active: `{lseen}`"
        )
    await send_formatted_reply(update, "\n".join(lines))

async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    chat_histories.pop(chat_id, None)
    await update.message.reply_text("Memory cleared. Ready for the next challenge.")

async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name if update.effective_user else "Player"
    user_query = " ".join(context.args or [])

    if not user_query:
        await send_formatted_reply(update, "Usage: `/ask <your question or target info>`")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        reply = await ask_gemini(chat_id, user_name, user_query)
        await send_formatted_reply(update, reply)
    except Exception as e:
        logging.error(f"AI Error: {e}")
        await update.message.reply_text(f"Gemini API Error: {type(e).__name__}. Check terminal logs.")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name if update.effective_user else "Player"
    target_data = " ".join(context.args or [])

    if not target_data and update.message.reply_to_message:
        target_data = update.message.reply_to_message.text or update.message.reply_to_message.caption or ""

    if not target_data:
        await send_formatted_reply(update, "Usage: `/analyze <paste code, Nmap scan, or error log>` (or reply to a message with `/analyze`)")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    structured_prompt = (
        "Analyze the following CTF artifact (code, scan output, or log). "
        "Identify exact vulnerabilities, misconfigurations, or hidden clues, and give the exact exploit payload or next command:\n\n"
        f"{target_data}"
    )
    try:
        reply = await ask_gemini(chat_id, user_name, structured_prompt)
        await send_formatted_reply(update, reply)
    except Exception as e:
        logging.error(f"AI Error: {e}")
        await update.message.reply_text("Error analyzing artifact.")

async def decode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    raw = " ".join(context.args or []).strip()
    if not raw and update.message.reply_to_message:
        raw = (update.message.reply_to_message.text or "").strip()

    if not raw:
        await send_formatted_reply(update, "Usage: `/decode <base64, hex, url-encoded, or rot13 string>`")
        return

    results = [f"*Input:* `{raw[:100]}`\n"]

    try:
        padded = raw + "=" * (-len(raw) % 4)
        b64_out = base64.b64decode(padded).decode("utf-8")
        if b64_out.isprintable():
            results.append(f"• *Base64:* `{b64_out}`")
    except Exception:
        pass

    try:
        clean_hex = re.sub(r'^(0x|\\x)', '', raw.replace(" ", ""))
        hex_out = bytes.fromhex(clean_hex).decode("utf-8")
        if hex_out.isprintable():
            results.append(f"• *Hex:* `{hex_out}`")
    except Exception:
        pass

    url_out = urllib.parse.unquote(raw)
    if url_out != raw:
        results.append(f"• *URL Decoded:* `{url_out}`")

    rot_out = codecs.decode(raw, "rot_13")
    if rot_out != raw:
        results.append(f"• *ROT13:* `{rot_out}`")

    if len(results) == 1:
        results.append("_No standard encoding matched. Use `/ask` for custom cipher analysis._")

    await send_formatted_reply(update, "\n".join(results))

async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    author = update.effective_user.first_name if update.effective_user else "Player"
    note_text = " ".join(context.args or [])

    if not note_text:
        await send_formatted_reply(update, "Usage: `/save <discovered port, credential, or flag>`")
        return

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        conn.execute("INSERT INTO notes (chat_id, author, content) VALUES (?, ?, ?)", (chat_id, author, note_text))
        conn.commit()
    await send_formatted_reply(update, f"Saved note: `{note_text}`")

async def list_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = update.effective_chat.id
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        rows = conn.execute("SELECT id, author, content FROM notes WHERE chat_id = ?", (chat_id,)).fetchall()

    if not rows:
        await update.message.reply_text("No notes saved for this group yet. Use /save <note> to add one.")
        return

    formatted = ["*TEAM CTF NOTES:*"]
    for row_id, author, content in rows:
        formatted.append(f"{row_id}. `{content}` _(by {author})_")
    await send_formatted_reply(update, "\n".join(formatted))

# --- MULTIMODAL HANDLERS ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name if update.effective_user else "Player"
    caption = update.message.caption or (
        "Analyze this image/screenshot for CTF clues, hidden text, terminal errors, "
        "or vulnerabilities, and provide the exact next command or solution."
    )

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = bytes(await photo_file.download_as_bytearray())

        reply = await ask_gemini(
            chat_id=chat_id,
            user_name=user_name,
            prompt=caption,
            file_bytes=photo_bytes,
            mime_type="image/jpeg",
            file_label="Photo",
        )
        await send_formatted_reply(update, reply)
    except Exception as e:
        logging.error(f"Photo Analysis Error: {e}")
        await update.message.reply_text(f"Error analyzing photo: {type(e).__name__}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    chat_id = update.effective_chat.id
    user_name = update.effective_user.first_name if update.effective_user else "Player"
    doc = update.message.document

    if doc.file_size and doc.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("File is larger than Telegram's 20 MB bot download limit.")
        return

    file_name = doc.file_name or "uploaded_file"
    lower_name = file_name.lower()
    mime_type = doc.mime_type or mimetypes.guess_type(file_name)[0] or "text/plain"

    caption = update.message.caption or (
        f"Analyze the attached file `{file_name}`. Summarize key findings, identify any "
        "vulnerabilities, hidden flags, or misconfigurations, and give the exact exploit or next step."
    )

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    try:
        tg_file = await doc.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())

        if lower_name.endswith(".docx") or mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            try:
                docx_text = extract_docx_text(file_bytes)
            except (zipfile.BadZipFile, ET.ParseError):
                await update.message.reply_text(f"Error: `{file_name}` is corrupted or not a valid DOCX file.")
                return

            if not docx_text.strip():
                await update.message.reply_text(f"`{file_name}` contained no readable text.")
                return

            full_prompt = f"{caption}\n\n=== DOCX CONTENT ({file_name}) ===\n{docx_text}"
            reply = await ask_gemini(
                chat_id=chat_id,
                user_name=user_name,
                prompt=full_prompt,
                file_label=file_name,
                memory_note=docx_text,
            )
            await send_formatted_reply(update, reply)
            return

        if lower_name.endswith(".zip") or mime_type in ("application/zip", "application/x-zip-compressed"):
            try:
                zip_summary = extract_zip_contents(file_bytes)
            except zipfile.BadZipFile:
                await update.message.reply_text(f"Error: `{file_name}` is corrupted or not a valid ZIP file.")
                return

            full_prompt = f"{caption}\n\n{zip_summary}"
            reply = await ask_gemini(
                chat_id=chat_id,
                user_name=user_name,
                prompt=full_prompt,
                file_label=file_name,
                memory_note=zip_summary,
            )
            await send_formatted_reply(update, reply)
            return

        if lower_name.endswith(TEXT_EXTENSIONS):
            mime_type = "text/plain"

        supported_mimes = (
            "application/pdf",
            "text/plain",
            "text/html",
            "text/css",
            "text/csv",
            "text/markdown",
            "application/json",
            "application/xml",
            "image/png",
            "image/jpeg",
            "image/webp",
        )
        if mime_type not in supported_mimes and not mime_type.startswith("text/"):
            await send_formatted_reply(
                update,
                f"Unsupported file type (`{mime_type}`). Send a *PDF*, *DOCX*, *ZIP*, *Image*, or *Text/Code* file."
            )
            return

        reply = await ask_gemini(
            chat_id=chat_id,
            user_name=user_name,
            prompt=caption,
            file_bytes=file_bytes,
            mime_type=mime_type,
            file_label=file_name,
        )
        await send_formatted_reply(update, reply)
    except Exception as e:
        logging.error(f"Document Analysis Error: {e}")
        await update.message.reply_text(f"Error analyzing `{file_name}`: {type(e).__name__}")

async def handle_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.message
        and update.message.text
        and update.message.reply_to_message
        and update.message.reply_to_message.from_user
        and update.message.reply_to_message.from_user.id == context.bot.id
    ):
        chat_id = update.effective_chat.id
        user_name = update.effective_user.first_name if update.effective_user else "Player"
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            reply = await ask_gemini(chat_id, user_name, update.message.text)
            await send_formatted_reply(update, reply)
        except Exception as e:
            logging.error(f"AI Error: {e}")

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Unhandled exception while processing update: {context.error}")

if __name__ == '__main__':
    init_db()
    discover_models()
    start_health_server()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(TypeHandler(Update, track_user_and_alert), group=-1)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("users", list_users_command))
    app.add_handler(CommandHandler("ask", ask_command))
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.add_handler(CommandHandler("decode", decode_command))
    app.add_handler(CommandHandler("save", save_note))
    app.add_handler(CommandHandler("notes", list_notes))
    app.add_handler(CommandHandler("clear", clear_memory))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reply))

    app.add_error_handler(global_error_handler)

    print(f"CTF AI Bot v2.7 is running (Admin ID: {ADMIN_ID})...")
    app.run_polling(drop_pending_updates=True)