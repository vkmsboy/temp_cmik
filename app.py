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
from textwrap import dedent

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

# --- CONFIGURATION (Globals) ---
TELEGRAM_TOKEN = None
ADMIN_USER_ID = None
CHANNEL_ID = None

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- IN-MEMORY DATA CACHE ---
MANGA_DATA = {}
MASTER_MESSAGE_ID = None
DATA_LOCK = threading.Lock()

# --- FLASK WEB APPLICATION ---
flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    with DATA_LOCK:
        mangas = sorted(MANGA_DATA.values(), key=lambda x: x['title'])
    return render_template("index.html", mangas=mangas)

@flask_app.route("/manga/<string:manga_slug>")
def manga_detail(manga_slug):
    with DATA_LOCK:
        manga_entry = MANGA_DATA.get(manga_slug)
    if not manga_entry: abort(404)
    try:
        chapters_sorted = sorted(manga_entry.get('chapters', {}).items(), key=lambda item: float(item[0]))
    except (ValueError, TypeError):
        chapters_sorted = sorted(manga_entry.get('chapters', {}).items())
    return render_template("manga_detail.html", manga=manga_entry, chapters=chapters_sorted, manga_slug=manga_slug)

@flask_app.route("/chapter/<string:manga_slug>/<string:chapter_num>")
def chapter_reader(manga_slug, chapter_num):
    with DATA_LOCK:
        manga_entry = MANGA_DATA.get(manga_slug)
    if not manga_entry or not manga_entry.get('chapters', {}).get(chapter_num): abort(404)
    chapter = {
        "manga_title": manga_entry['title'], "chapter_number": chapter_num,
        "pages": manga_entry['chapters'][chapter_num], "manga_slug": manga_slug
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
    except Exception as e:
        logger.error(f"Failed to get image {file_id}: {e}")
        abort(404)

# --- TELEGRAM BOT LOGIC ---
(SELECTING_ACTION, ADD_TITLE, ADD_DESC, ADD_COVER,
 SELECT_MANGA, ACTION_MENU, ADD_CHAPTER_METHOD,
 ADD_CHAPTER_ZIP, DELETE_CONFIRM) = range(9)

def admin_only(func):
    """Decorator to check for admin access. Ends conversation if unauthorized."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if not user or user.id != ADMIN_USER_ID:
            logger.warning(f"Unauthorized access attempt by user {user.id if user else 'Unknown'}.")
            if update.callback_query: await update.callback_query.answer("⛔️ Unauthorized.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapped

def slugify(text):
    return re.sub(r'[\W_]+', '-', text.lower()).strip('-')

async def save_data_to_channel(context: ContextTypes.DEFAULT_TYPE):
    """Saves the entire MANGA_DATA object to the single pinned master JSON message."""
    global MASTER_MESSAGE_ID
    with DATA_LOCK:
        if not MANGA_DATA:
            if MASTER_MESSAGE_ID:
                try:
                    await context.bot.unpin_chat_message(chat_id=CHANNEL_ID, message_id=MASTER_MESSAGE_ID)
                    await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=MASTER_MESSAGE_ID)
                    MASTER_MESSAGE_ID = None
                except telegram.error.TelegramError: MASTER_MESSAGE_ID = None
            return

        pretty_json = json.dumps(MANGA_DATA, indent=2)
        try:
            if MASTER_MESSAGE_ID:
                await context.bot.edit_message_text(chat_id=CHANNEL_ID, message_id=MASTER_MESSAGE_ID, text=f"<code>{pretty_json}</code>", parse_mode=ParseMode.HTML)
            else:
                message = await context.bot.send_message(chat_id=CHANNEL_ID, text=f"<code>{pretty_json}</code>", parse_mode=ParseMode.HTML)
                MASTER_MESSAGE_ID = message.message_id
                await context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=MASTER_MESSAGE_ID, disable_notification=True)
        except telegram.error.TelegramError as e:
            logger.error(f"Failed to save to channel, recreating message. Error: {e}")
            message = await context.bot.send_message(chat_id=CHANNEL_ID, text=f"<code>{pretty_json}</code>", parse_mode=ParseMode.HTML)
            MASTER_MESSAGE_ID = message.message_id
            await context.bot.pin_chat_message(chat_id=CHANNEL_ID, message_id=MASTER_MESSAGE_ID, disable_notification=True)

# --- Conversation Handlers ---
@admin_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the conversation and displays the main menu."""
    logger.info(f"User {update.effective_user.id} started conversation.")
    keyboard = [[InlineKeyboardButton("➕ Add New Comic", callback_data="add_manga")], [InlineKeyboardButton("📚 Manage Existing Comic", callback_data="manage_manga")]]
    text = "👋 Hello, Admin! Your Comic CMS is ready."
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECTING_ACTION

@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone help command, not part of a conversation."""
    logger.info(f"User {update.effective_user.id} requested help.")
    help_text = """
    *Comic CMS Bot Help*
    `/start` - Shows the main menu.
    `/help` - Shows this message.
    `/cancel` - Cancels any current operation (like adding a comic).
    """
    await update.message.reply_text(dedent(help_text), parse_mode=ParseMode.MARKDOWN)


async def add_manga_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: SELECTING_ACTION -> ADD_TITLE")
    await update.callback_query.edit_message_text("Enter the title for the new comic:")
    return ADD_TITLE

async def add_manga_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info(f"State: ADD_TITLE -> ADD_DESC. Title: '{update.message.text}'")
    context.user_data['title'] = update.message.text
    await update.message.reply_text("Great. Now enter a short description:")
    return ADD_DESC

async def add_manga_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: ADD_DESC -> ADD_COVER.")
    context.user_data['description'] = update.message.text
    await update.message.reply_text("Perfect. Now send me the cover image (as a photo or a file).")
    return ADD_COVER

async def add_manga_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: ADD_COVER. Received potential cover.")
    file_id = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        file_id = update.message.document.file_id
    
    if not file_id:
        await update.message.reply_text("That's not an image. Please send a photo or image file, or /cancel."); return ADD_COVER

    title = context.user_data['title']
    manga_slug = slugify(title)
    with DATA_LOCK:
        MANGA_DATA[manga_slug] = {
            "title": title, "slug": manga_slug, "description": context.user_data['description'],
            "cover_file_id": file_id, "chapters": {}
        }
    await save_data_to_channel(context)
    await update.message.reply_text(f"✅ Success! `{title}` has been created.", parse_mode=ParseMode.MARKDOWN)
    context.user_data.clear()
    logger.info("Add Comic conversation finished.")
    await start(update, context)
    return ConversationHandler.END

async def manage_manga_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: SELECTING_ACTION -> SELECT_MANGA")
    with DATA_LOCK:
        if not MANGA_DATA:
            await update.callback_query.answer("No comics found to manage.", show_alert=True)
            return SELECTING_ACTION
        mangas = sorted(MANGA_DATA.values(), key=lambda x: x['title'])
    
    keyboard = [[InlineKeyboardButton(m['title'], callback_data=f"manga_{m['slug']}")] for m in mangas]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")])
    await update.callback_query.edit_message_text("Select a comic to manage:", reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_MANGA

async def manage_action_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    manga_slug = query.data.split('_', 1)[1]
    context.user_data['manga_slug'] = manga_slug
    with DATA_LOCK: title = MANGA_DATA[manga_slug]['title']
    logger.info(f"State: SELECT_MANGA -> ACTION_MENU for '{title}'")
    keyboard = [
        [InlineKeyboardButton("➕ Add Chapter(s)", callback_data="add_chapter")],
        [InlineKeyboardButton("🗑️ Delete Comic", callback_data="delete_manga")],
        [InlineKeyboardButton("⬅️ Back to Comic List", callback_data="back_to_manage")],
    ]
    await query.edit_message_text(f"Managing `{title}`:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return ACTION_MENU

async def add_chapter_method(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: ACTION_MENU -> ADD_CHAPTER_METHOD")
    keyboard = [[InlineKeyboardButton("📦 ZIP Upload", callback_data="zip_upload")], [InlineKeyboardButton("⬅️ Back", callback_data=f"manga_{context.user_data['manga_slug']}")] ]
    await update.callback_query.edit_message_text("Please upload the ZIP file containing chapter folders:", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CHAPTER_ZIP

def extract_number(text):
    numbers = re.findall(r'(\d+\.?\d*)', text)
    return numbers[-1] if numbers else None

async def add_chapter_zip_process(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    logger.info("State: ADD_CHAPTER_ZIP. Received ZIP file.")
    doc = await update.message.document.get_file()
    manga_slug = context.user_data['manga_slug']
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "chapters.zip"
        await doc.download_to_drive(zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zf: zf.extractall(temp_dir)

        chapter_dirs = [d for d in Path(temp_dir).rglob('*') if d.is_dir() and any(f.suffix.lower() in ['.jpg', '.jpeg', '.png'] for f in d.iterdir())]
        if not chapter_dirs:
            await update.message.reply_text("❌ No image folders found in ZIP."); await start(update, context); return ConversationHandler.END

        await update.message.reply_text(f"Found {len(chapter_dirs)} chapters. Uploading...")
        sorted_chapters = sorted(chapter_dirs, key=lambda d: float(extract_number(d.name) or -1))

        with DATA_LOCK:
            for chap_dir in sorted_chapters:
                chapter_num = extract_number(chap_dir.name)
                if not chapter_num: continue
                image_files = sorted(list(chap_dir.glob('*.[jJ][pP][gG]')) + list(chap_dir.glob('*.[pP][nN][gG]')))
                if not image_files: continue
                await update.message.reply_text(f"Uploading Chapter {chapter_num} ({len(image_files)} pages)...")
                page_file_ids = [sent.photo[-1].file_id for img_path in image_files if (sent := await context.bot.send_photo(chat_id=update.effective_chat.id, photo=img_path.read_bytes()))]
                MANGA_DATA[manga_slug]['chapters'][chapter_num] = page_file_ids

    await save_data_to_channel(context)
    await update.message.reply_text("✅ All chapters saved!")
    context.user_data.clear()
    logger.info("ZIP upload finished.")
    await start(update, context)
    return ConversationHandler.END

async def delete_manga_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    manga_slug = context.user_data['manga_slug']
    logger.info(f"State: ACTION_MENU -> DELETE_CONFIRM for '{manga_slug}'.")
    keyboard = [[InlineKeyboardButton("YES, DELETE IT", callback_data=f"delmanga_yes_{manga_slug}")], [InlineKeyboardButton("NO, GO BACK", callback_data=f"manga_{manga_slug}")] ]
    await update.callback_query.edit_message_text("⚠️ **Are you sure?** This is permanent.", parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_CONFIRM

async def delete_manga_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    manga_slug = update.callback_query.data.split('_', 2)[2]
    logger.info(f"State: DELETE_CONFIRM. Deleting '{manga_slug}'.")
    with DATA_LOCK:
        MANGA_DATA.pop(manga_slug, None)
    await save_data_to_channel(context)
    await update.callback_query.edit_message_text(f"✅ Comic deleted.")
    return await manage_manga_start(update, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Generic cancel handler to end any conversation state."""
    logger.info("Conversation cancelled by user via /cancel command.")
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    await start(update, context)
    return ConversationHandler.END

async def unexpected_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles any input that is not expected in the current conversation state."""
    logger.warning("Received an unexpected input for the current conversation state.")
    await update.message.reply_text("I was not expecting that. Please provide the requested information, or type /cancel to go back to the main menu.")

def run_bot(token, admin_id, channel_id):
    """The main entry point for the bot thread."""
    global TELEGRAM_TOKEN, ADMIN_USER_ID, CHANNEL_ID, MASTER_MESSAGE_ID
    TELEGRAM_TOKEN, ADMIN_USER_ID, CHANNEL_ID = token, admin_id, channel_id

    async def main():
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        logger.info("Loading data from channel...")
        try:
            chat = await application.bot.get_chat(chat_id=CHANNEL_ID)
            if chat.pinned_message and chat.pinned_message.from_user and chat.pinned_message.from_user.id == application.bot.id:
                pinned_message = chat.pinned_message
                try:
                    with DATA_LOCK:
                        MANGA_DATA.update(json.loads(pinned_message.text))
                    MASTER_MESSAGE_ID = pinned_message.message_id
                    logger.info(f"Loaded data from pinned message ID: {MASTER_MESSAGE_ID}")
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Pinned message is not valid JSON.")
            else:
                logger.info("No valid pinned message found. Starting fresh.")
        except telegram.error.TelegramError as e:
            logger.error(f"Could not load data from channel. Is bot an admin? Error: {e}")

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("start", start)],
            states={
                SELECTING_ACTION: [
                    CallbackQueryHandler(add_manga_start, pattern="^add_manga$"),
                    CallbackQueryHandler(manage_manga_start, pattern="^manage_manga$"),
                ],
                ADD_TITLE: [MessageHandler(filters.TEXT, add_manga_title)],
                ADD_DESC: [MessageHandler(filters.TEXT, add_manga_desc)],
                ADD_COVER: [MessageHandler(filters.PHOTO | filters.Document.IMAGE, add_manga_cover)],
                SELECT_MANGA: [
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_"),
                    CallbackQueryHandler(start, pattern="^main_menu$")
                ],
                ACTION_MENU: [
                    CallbackQueryHandler(add_chapter_method, pattern="^add_chapter$"),
                    CallbackQueryHandler(delete_manga_confirm, pattern="^delete_manga$"),
                    CallbackQueryHandler(manage_manga_start, pattern="^back_to_manage$"),
                ],
                ADD_CHAPTER_METHOD: [
                    CallbackQueryHandler(add_chapter_zip_start, pattern="^zip_upload$"),
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_")
                ],
                ADD_CHAPTER_ZIP: [MessageHandler(filters.Document.ZIP, add_chapter_zip_process)],
                DELETE_CONFIRM: [
                    CallbackQueryHandler(delete_manga_execute, pattern=r"^delmanga_yes_"),
                    CallbackQueryHandler(manage_action_menu, pattern=r"^manga_")
                ],
            },
            fallbacks=[
                CommandHandler("cancel", cancel),
                CommandHandler("start", start), # Can restart from anywhere
            ],
            name="main_conv", persistent=False,
            per_message=False 
        )
        application.add_handler(conv_handler)
        application.add_handler(CommandHandler("help", help_command))
        
        try:
            logger.info("Bot initializing...")
            await application.initialize()
            await application.updater.start_polling()
            await application.start()
            logger.info("Telegram bot is now running.")
            await asyncio.Event().wait()
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
    load_dotenv()
    local_token, local_admin_id, local_channel_id = os.getenv("TELEGRAM_TOKEN"), int(os.getenv("ADMIN_USER_ID")), int(os.getenv("CHANNEL_ID"))
    if not all([local_token, local_admin_id, local_channel_id]):
        print("ERROR: For local run, set TELEGRAM_TOKEN, ADMIN_USER_ID, and CHANNEL_ID in .env file.")
    else:
        bot_thread = threading.Thread(target=run_bot, args=(local_token, local_admin_id, local_channel_id), daemon=True)
        bot_thread.start()
        flask_app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)

