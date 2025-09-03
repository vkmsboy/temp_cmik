import os
import threading
import logging
import requests
import asyncio
import zipfile
import tempfile
import re
import json
from pathlib import Path
from functools import wraps

from flask import Flask, render_template, redirect, url_for, abort
from dotenv import load_dotenv

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.constants import ParseMode

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
    CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
except (ValueError, TypeError):
    ADMIN_USER_ID, CHANNEL_ID = None, None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- IN-MEMORY DATA CACHE ---
# This dictionary will hold all our comic data, loaded from the channel on startup.
# Structure: { "comic_slug": {"message_id": 123, "data": {...comic_json...}} }
MANGA_DATA = {}
DATA_LOCK = threading.Lock() # To prevent race conditions when updating data

# --- FLASK WEB APPLICATION ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    with DATA_LOCK:
        # Sort data by title for display
        mangas = sorted(MANGA_DATA.values(), key=lambda x: x['data']['title'])
    return render_template("index.html", mangas=mangas)

@flask_app.route("/manga/<string:manga_slug>")
def manga_detail(manga_slug):
    with DATA_LOCK:
        manga_entry = MANGA_DATA.get(manga_slug)
    if not manga_entry:
        abort(404)
    
    manga = manga_entry['data']
    # Sort chapters by chapter number (as float)
    try:
        chapters_sorted = sorted(
            manga.get('chapters', {}).items(), 
            key=lambda item: float(item[0])
        )
    except (ValueError, TypeError):
        chapters_sorted = sorted(manga.get('chapters', {}).items())
        
    return render_template("manga_detail.html", manga=manga, chapters=chapters_sorted, manga_slug=manga_slug)

@flask_app.route("/chapter/<string:manga_slug>/<string:chapter_num>")
def chapter_reader(manga_slug, chapter_num):
    with DATA_LOCK:
        manga_entry = MANGA_DATA.get(manga_slug)
    if not manga_entry:
        abort(404)
    
    chapter_pages = manga_entry['data'].get('chapters', {}).get(chapter_num)
    if not chapter_pages:
        abort(404)
        
    chapter = {
        "manga_title": manga_entry['data']['title'],
        "chapter_number": chapter_num,
        "pages": chapter_pages,
        "manga_slug": manga_slug
    }
    return render_template("chapter_reader.html", chapter=chapter)

@flask_app.route("/image/<file_id>")
def get_telegram_image(file_id):
    if not TELEGRAM_TOKEN: abort(500)
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile?file_id={file_id}"
        response = requests.get(api_url)
        response.raise_for_status()
        file_path = response.json()['result']['file_path']
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        return redirect(image_url)
    except Exception:
        abort(404)

# --- TELEGRAM BOT LOGIC ---
# Conversation states
(START_ROUTES, ADD_MANGA_TITLE, ADD_MANGA_DESC, ADD_MANGA_COVER,
 MANAGE_SELECT_MANGA, MANAGE_ACTION_MENU, ADD_CHAPTER_METHOD,
 ADD_CHAPTER_MANUAL_NUMBER, ADD_CHAPTER_MANUAL_PAGES, ADD_CHAPTER_ZIP,
 DELETE_CHAPTER_SELECT, DELETE_MANGA_CONFIRM
) = range(12)

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_USER_ID:
            if update.message:
                await update.message.reply_text("⛔️ Unauthorized.")
            elif update.callback_query:
                await update.callback_query.answer("⛔️ Unauthorized.", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def slugify(text):
    return re.sub(r'[\W_]+', '-', text.lower()).strip('-')

async def save_manga_data(context: ContextTypes.DEFAULT_TYPE, manga_slug: str, manga_json: dict):
    """Posts or edits a message in the channel and updates the in-memory cache."""
    with DATA_LOCK:
        entry = MANGA_DATA.get(manga_slug, {})
        message_id = entry.get('message_id')
        pretty_json = json.dumps(manga_json, indent=2)
        
        if message_id: # Edit existing message
            message = await context.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=message_id, text=f"<code>{pretty_json}</code>", parse_mode=ParseMode.HTML)
        else: # Post new message
            message = await context.bot.send_message(chat_id=CHANNEL_ID, text=f"<code>{pretty_json}</code>", parse_mode=ParseMode.HTML)
        
        # Update cache
        MANGA_DATA[manga_slug] = {"message_id": message.message_id, "data": manga_json}

async def delete_manga_data(context: ContextTypes.DEFAULT_TYPE, manga_slug: str):
    with DATA_LOCK:
        entry = MANGA_DATA.pop(manga_slug, None)
        if entry and entry.get('message_id'):
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=entry['message_id'])

# --- Bot Handlers ---
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [
        [InlineKeyboardButton("➕ Add New Comic", callback_data="add_manga")],
        [InlineKeyboardButton("📚 Manage Existing Comic", callback_data="manage_manga")],
    ]
    text = "👋 Hello, Admin! This is your Comic CMS Bot."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return START_ROUTES

@admin_only
async def add_manga_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.callback_query.edit_message_text("Enter the title for the new comic:")
    return ADD_MANGA_TITLE

async def add_manga_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['title'] = update.message.text
    await update.message.reply_text("Great. Now enter a short description:")
    return ADD_MANGA_DESC

async def add_manga_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['description'] = update.message.text
    await update.message.reply_text("Perfect. Now send me the cover image.")
    return ADD_MANGA_COVER

async def add_manga_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = context.user_data['title']
    manga_slug = slugify(title)
    
    manga_json = {
        "title": title,
        "slug": manga_slug,
        "description": context.user_data['description'],
        "cover_file_id": update.message.photo[-1].file_id,
        "chapters": {}
    }
    await save_manga_data(context, manga_slug, manga_json)
    await update.message.reply_text(f"✅ Success! `{title}` has been created.")
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

@admin_only
async def manage_manga_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with DATA_LOCK:
        if not MANGA_DATA:
            await update.callback_query.edit_message_text("No comics found. Add one first!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="main_menu")]]))
            return START_ROUTES
        mangas = sorted(MANGA_DATA.values(), key=lambda x: x['data']['title'])
    
    keyboard = [[InlineKeyboardButton(m['data']['title'], callback_data=f"manga_{m['data']['slug']}")] for m in mangas]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    await update.callback_query.edit_message_text("Select a comic to manage:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_SELECT_MANGA

async def manage_action_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    manga_slug = query.data.split('_', 1)[1]
    context.user_data['manga_slug'] = manga_slug
    
    with DATA_LOCK:
        title = MANGA_DATA[manga_slug]['data']['title']
    
    keyboard = [
        [InlineKeyboardButton("➕ Add Chapter(s)", callback_data="add_chapter")],
        [InlineKeyboardButton("🗑️ Delete Chapter", callback_data="delete_chapter")],
        [InlineKeyboardButton("❌ Delete Comic", callback_data="delete_manga")],
        [InlineKeyboardButton("⬅️ Back to Comic List", callback_data="back_to_manage")],
    ]
    await query.edit_message_text(f"Managing `{title}`:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return MANAGE_ACTION_MENU

async def add_chapter_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 ZIP Upload", callback_data="zip_upload")],
        [InlineKeyboardButton("🖼️ Manual Upload", callback_data="manual_upload")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"manga_{context.user_data['manga_slug']}")]
    ]
    await update.callback_query.edit_message_text("How to add chapters?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CHAPTER_METHOD

async def add_chapter_zip_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Please upload the `.zip` file now.")
    return ADD_CHAPTER_ZIP

def extract_number(text):
    numbers = re.findall(r'(\d+\.?\d*)', text)
    return numbers[-1] if numbers else None

async def add_chapter_zip_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await update.message.document.get_file()
    manga_slug = context.user_data['manga_slug']
    
    with tempfile.TemporaryDirectory() as temp_dir, DATA_LOCK:
        zip_path = Path(temp_dir) / "chapters.zip"
        await doc.download_to_drive(zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zf: zf.extractall(temp_dir)

        chapter_dirs = [d for d in Path(temp_dir).rglob('*') if d.is_dir() and any(f.suffix.lower() in ['.jpg', '.jpeg', '.png'] for f in d.iterdir())]
        if not chapter_dirs:
            await update.message.reply_text("❌ No folders with images found in ZIP.")
            await start(update, context); return ConversationHandler.END

        await update.message.reply_text(f"Found {len(chapter_dirs)} chapters. Uploading...")
        
        manga_json = MANGA_DATA[manga_slug]['data']
        sorted_chapters = sorted(chapter_dirs, key=lambda d: float(extract_number(d.name) or -1))

        for chap_dir in sorted_chapters:
            chapter_num = extract_number(chap_dir.name)
            if not chapter_num: continue
            
            image_files = sorted(list(chap_dir.glob('*.[jJ][pP][gG]')) + list(chap_dir.glob('*.[pP][nN][gG]')))
            if not image_files: continue
            
            await update.message.reply_text(f"Uploading Chapter {chapter_num} ({len(image_files)} pages)...")
            page_file_ids = []
            for img_path in image_files:
                sent = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=img_path.read_bytes())
                page_file_ids.append(sent.photo[-1].file_id)
            manga_json['chapters'][chapter_num] = page_file_ids

    await save_manga_data(context, manga_slug, manga_json)
    await update.message.reply_text("✅ All chapters uploaded and saved!")
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

async def delete_manga_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manga_slug = context.user_data['manga_slug']
    keyboard = [
        [InlineKeyboardButton("YES, DELETE IT", callback_data=f"delmanga_yes_{manga_slug}")],
        [InlineKeyboardButton("NO, GO BACK", callback_data=f"manga_{manga_slug}")]
    ]
    await update.callback_query.edit_message_text("⚠️ **Are you sure?** This is permanent.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_MANGA_CONFIRM

async def delete_manga_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manga_slug = update.callback_query.data.split('_', 2)[2]
    await delete_manga_data(context, manga_slug)
    await update.callback_query.edit_message_text(f"✅ Comic deleted.")
    await manage_manga_start(update, context)
    return MANAGE_SELECT_MANGA

# --- Fallback & Cancel ---
async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END


def run_bot():
    async def main():
        if not all([TELEGRAM_TOKEN, ADMIN_USER_ID, CHANNEL_ID]):
            logger.error("CRITICAL: Bot requires TELEGRAM_TOKEN, ADMIN_USER_ID, and CHANNEL_ID to run.")
            return

        bot = telegram.Bot(token=TELEGRAM_TOKEN)
        logger.info("Loading data from channel...")
        async for message in bot.get_chat_history(chat_id=CHANNEL_ID, limit=200):
            try:
                data = json.loads(message.text)
                if 'slug' in data:
                    with DATA_LOCK:
                        MANGA_DATA[data['slug']] = {"message_id": message.message_id, "data": data}
            except (json.JSONDecodeError, TypeError):
                continue # Ignore non-json messages
        logger.info(f"Loaded {len(MANGA_DATA)} comics from channel.")

        application = Application.builder().token(TELEGRAM_TOKEN).build()
        
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                START_ROUTES: [
                    CallbackQueryHandler(add_manga_start, pattern="^add_manga$"),
                    CallbackQueryHandler(manage_manga_start, pattern="^manage_manga$"),
                ],
                ADD_MANGA_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manga_title)],
                ADD_MANGA_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_manga_desc)],
                ADD_MANGA_COVER: [MessageHandler(filters.PHOTO, add_manga_cover)],
                MANAGE_SELECT_MANGA: [
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_"),
                    CallbackQueryHandler(start, pattern="^main_menu$")
                ],
                MANAGE_ACTION_MENU: [
                    CallbackQueryHandler(add_chapter_method, pattern="^add_chapter$"),
                    CallbackQueryHandler(delete_manga_confirm, pattern="^delete_manga$"),
                    CallbackQueryHandler(manage_manga_start, pattern="^back_to_manage$"),
                ],
                ADD_CHAPTER_METHOD: [
                    CallbackQueryHandler(add_chapter_zip_start, pattern="^zip_upload$"),
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_"),
                ],
                ADD_CHAPTER_ZIP: [MessageHandler(filters.Document.ZIP, add_chapter_zip_process)],
                DELETE_MANGA_CONFIRM: [
                    CallbackQueryHandler(delete_manga_execute, pattern=r"^delmanga_yes_"),
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_"),
                ],
            },
            fallbacks=[CommandHandler("cancel", end_conversation)],
            persistent=False, name="comic_cms_conversation"
        )
        application.add_handler(conv_handler)
        
        try:
            logger.info("Bot initializing...")
            await application.initialize()
            await application.updater.start_polling()
            await application.start()
            logger.info("Telegram bot is now running.")
            await asyncio.Event().wait() # Keep it running indefinitely
        finally:
            logger.info("Bot stopping...")
            if application.updater and application.updater.is_running: await application.updater.stop()
            if application.running: await application.stop()
            await application.shutdown()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except Exception as e:
        logger.error(f"Critical error in bot thread: {e}", exc_info=True)
    finally:
        loop.close()

if __name__ == "__main__":
    if not all([TELEGRAM_TOKEN, ADMIN_USER_ID, CHANNEL_ID]):
        print("ERROR: Set TELEGRAM_TOKEN, ADMIN_USER_ID, and CHANNEL_ID in .env or Colab secrets.")
    else:
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
