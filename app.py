import os
import threading
import logging
import requests
import asyncio
import zipfile
import tempfile
import re
from pathlib import Path

from flask import Flask, render_template, redirect, url_for, abort
from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

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

# --- CONFIGURATION ---
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
try:
    ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID"))
except (ValueError, TypeError):
    ADMIN_USER_ID = None

DATABASE_URL = "sqlite:///manga.db"
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE SETUP ---
Base = declarative_base()

class Manga(Base):
    __tablename__ = "manga"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False, unique=True)
    description = Column(Text, nullable=True)
    cover_file_id = Column(String(255), nullable=True)
    chapters = relationship("Chapter", back_populates="manga", cascade="all, delete-orphan")

class Chapter(Base):
    __tablename__ = "chapter"
    id = Column(Integer, primary_key=True)
    manga_id = Column(Integer, ForeignKey("manga.id"), nullable=False)
    chapter_number = Column(String(50), nullable=False)
    manga = relationship("Manga", back_populates="chapters")
    pages = relationship("Page", back_populates="chapter", cascade="all, delete-orphan", order_by="Page.page_number")

class Page(Base):
    __tablename__ = "page"
    id = Column(Integer, primary_key=True)
    chapter_id = Column(Integer, ForeignKey("chapter.id"), nullable=False)
    page_number = Column(Integer, nullable=False)
    file_id = Column(String(255), nullable=False)
    chapter = relationship("Chapter", back_populates="pages")

engine = create_engine(DATABASE_URL)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)

# --- FLASK WEB APPLICATION ---
flask_app = Flask(__name__)

def get_db_session():
    return Session()

@flask_app.route("/")
def index():
    session = get_db_session()
    all_manga = session.query(Manga).order_by(Manga.title).all()
    session.close()
    return render_template("index.html", mangas=all_manga)

@flask_app.route("/manga/<int:manga_id>")
def manga_detail(manga_id):
    session = get_db_session()
    manga = session.query(Manga).filter_by(id=manga_id).first()
    if not manga:
        abort(404)
    try:
        chapters_sorted = sorted(manga.chapters, key=lambda c: float(c.chapter_number))
    except (ValueError, TypeError):
        chapters_sorted = sorted(manga.chapters, key=lambda c: c.chapter_number)
    session.close()
    return render_template("manga_detail.html", manga=manga, chapters=chapters_sorted)

@flask_app.route("/chapter/<int:chapter_id>")
def chapter_reader(chapter_id):
    session = get_db_session()
    chapter = session.query(Chapter).filter_by(id=chapter_id).first()
    if not chapter:
        abort(404)
    session.close()
    return render_template("chapter_reader.html", chapter=chapter)

@flask_app.route("/image/<file_id>")
def get_telegram_image(file_id):
    if not TELEGRAM_TOKEN:
        abort(500)
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        params = {'file_id': file_id}
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        file_path = response.json()['result']['file_path']
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        return redirect(image_url)
    except Exception:
        abort(404)

# --- TELEGRAM BOT LOGIC ---
# Conversation states
(START_ROUTES,
 ADD_MANGA_TITLE, ADD_MANGA_DESC, ADD_MANGA_COVER,
 MANAGE_SELECT_MANGA, MANAGE_ACTION_MENU,
 ADD_CHAPTER_METHOD, ADD_CHAPTER_NUMBER, ADD_CHAPTER_PAGES, ADD_CHAPTER_ZIP,
 UPDATE_INFO_SELECT, UPDATE_INFO_PROMPT,
 DELETE_CHAPTER_SELECT, DELETE_MANGA_CONFIRM
) = range(14)

# Helper function to go back to main menu
async def to_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("➕ Add New Comic", callback_data="add_manga")],
        [InlineKeyboardButton("📚 Manage Existing Comic", callback_data="manage_manga")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.edit_message_text(
        text="Welcome! This bot helps you manage your comic website.\n\nWhat would you like to do?",
        reply_markup=reply_markup
    )
    return START_ROUTES

# -- Main Menu and Admin Check --
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if user.id != ADMIN_USER_ID:
        await update.message.reply_text("⛔️ Sorry, you are not authorized to use this bot.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("➕ Add New Comic", callback_data="add_manga")],
        [InlineKeyboardButton("📚 Manage Existing Comic", callback_data="manage_manga")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"👋 Hello, Admin! Welcome to your Comic CMS Bot.\n\n"
        "Use the buttons below to add new series or manage existing ones.",
        reply_markup=reply_markup
    )
    return START_ROUTES

# --- Add New Comic Flow ---
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
    photo = update.message.photo[-1]
    session = get_db_session()
    try:
        new_manga = Manga(
            title=context.user_data['title'],
            description=context.user_data['description'],
            cover_file_id=photo.file_id
        )
        session.add(new_manga)
        session.commit()
        await update.message.reply_text(f"✅ Success! `{new_manga.title}` has been created.")
    except Exception as e:
        session.rollback()
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        session.close()
        context.user_data.clear()

    await start(update, context)
    return ConversationHandler.END

# --- Manage Existing Comic Flow ---
async def manage_manga_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_db_session()
    mangas = session.query(Manga).order_by(Manga.title).all()
    session.close()
    if not mangas:
        await update.callback_query.edit_message_text("No comics found. Add one first!")
        return await to_main_menu(update, context)

    keyboard = [[InlineKeyboardButton(m.title, callback_data=f"manga_{m.id}")] for m in mangas]
    keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data="main_menu")])
    await update.callback_query.edit_message_text("Select a comic to manage:", reply_markup=InlineKeyboardMarkup(keyboard))
    return MANAGE_SELECT_MANGA

async def manage_action_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    manga_id = int(query.data.split('_')[1])
    context.user_data['manga_id'] = manga_id
    session = get_db_session()
    manga = session.query(Manga).get(manga_id)
    session.close()

    keyboard = [
        [InlineKeyboardButton("➕ Add Chapter(s)", callback_data="add_chapter")],
        [InlineKeyboardButton("✏️ Update Info", callback_data="update_info")],
        [InlineKeyboardButton("🗑️ Delete Chapter", callback_data="delete_chapter")],
        [InlineKeyboardButton("❌ Delete Comic", callback_data="delete_manga")],
        [InlineKeyboardButton("⬅️ Back to Comic List", callback_data="back_to_manage")],
    ]
    await query.edit_message_text(f"Managing `{manga.title}`:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return MANAGE_ACTION_MENU

# --- Add Chapter (Method Selection) ---
async def add_chapter_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 ZIP Upload", callback_data="zip_upload")],
        [InlineKeyboardButton("🖼️ Manual Upload", callback_data="manual_upload")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"manga_{context.user_data['manga_id']}")]
    ]
    await update.callback_query.edit_message_text("How would you like to add chapters?", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADD_CHAPTER_METHOD

# --- Manual Chapter Upload ---
async def add_chapter_manual_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Enter the chapter number (e.g., 1, 25.5):")
    return ADD_CHAPTER_NUMBER

async def add_chapter_pages_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['chapter_number'] = update.message.text
    context.user_data['pages'] = []
    await update.message.reply_text("OK. Now send all page images for this chapter.\nSend /done when finished.")
    return ADD_CHAPTER_PAGES

async def add_chapter_page_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.setdefault('pages', []).append(update.message.photo[-1].file_id)
    await update.message.reply_text(f"Page {len(context.user_data['pages'])} saved.")
    return ADD_CHAPTER_PAGES

async def add_chapter_manual_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_db_session()
    try:
        new_chapter = Chapter(manga_id=context.user_data['manga_id'], chapter_number=context.user_data['chapter_number'])
        session.add(new_chapter)
        session.flush()
        for i, file_id in enumerate(context.user_data['pages']):
            session.add(Page(chapter_id=new_chapter.id, page_number=i + 1, file_id=file_id))
        session.commit()
        await update.message.reply_text("✅ Chapter saved successfully!")
    except Exception as e:
        session.rollback()
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        session.close()
        context.user_data.clear()
    
    await start(update, context)
    return ConversationHandler.END

# --- ZIP Chapter Upload ---
def extract_number(text):
    numbers = re.findall(r'(\d+\.?\d*)', text)
    return float(numbers[-1]) if numbers else -1

async def add_chapter_zip_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.edit_message_text("Please upload the `.zip` file now.")
    return ADD_CHAPTER_ZIP

async def add_chapter_zip_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = await update.message.document.get_file()
    
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "chapters.zip"
        await doc.download_to_drive(zip_path)
        
        extract_path = Path(temp_dir) / "extracted"
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(extract_path)

        chapter_dirs = [d for d in extract_path.rglob('*') if d.is_dir() and any(f.suffix.lower() in ['.jpg', '.jpeg', '.png'] for f in d.iterdir())]
        
        if not chapter_dirs:
            await update.message.reply_text("❌ Error: Could not find any folders with images inside the ZIP file.")
            await start(update, context)
            return ConversationHandler.END

        await update.message.reply_text(f"Found {len(chapter_dirs)} chapter folders. Starting upload process...")

        sorted_chapters = sorted(chapter_dirs, key=lambda d: extract_number(d.name))
        
        session = get_db_session()
        uploaded_count = 0
        try:
            for chap_dir in sorted_chapters:
                chapter_num = str(extract_number(chap_dir.name))
                if float(chapter_num) < 0:
                    await update.message.reply_text(f"⚠️ Skipping folder `{chap_dir.name}` as no chapter number could be found.")
                    continue
                
                image_files = sorted(list(chap_dir.glob('*.[jJ][pP][gG]')) + list(chap_dir.glob('*.[pP][nN][gG]')))
                if not image_files:
                    continue
                
                await update.message.reply_text(f"Uploading Chapter {chapter_num} with {len(image_files)} pages...")
                
                new_chapter = Chapter(manga_id=context.user_data['manga_id'], chapter_number=chapter_num)
                session.add(new_chapter)
                session.flush()

                page_file_ids = []
                for img_path in image_files:
                    sent_photo = await context.bot.send_photo(chat_id=update.effective_chat.id, photo=open(img_path, 'rb'))
                    page_file_ids.append(sent_photo.photo[-1].file_id)
                
                for i, file_id in enumerate(page_file_ids):
                     session.add(Page(chapter_id=new_chapter.id, page_number=i + 1, file_id=file_id))
                
                session.commit()
                uploaded_count += 1

            await update.message.reply_text(f"✅ Finished! Successfully uploaded {uploaded_count} chapters.")
        except Exception as e:
            session.rollback()
            await update.message.reply_text(f"❌ An error occurred: {e}")
        finally:
            session.close()
            context.user_data.clear()

    await start(update, context)
    return ConversationHandler.END

# --- Delete Chapter Flow ---
async def delete_chapter_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manga_id = context.user_data['manga_id']
    session = get_db_session()
    manga = session.query(Manga).get(manga_id)
    chapters = manga.chapters
    session.close()
    
    if not chapters:
        await update.callback_query.edit_message_text("This comic has no chapters to delete.")
        return await manage_action_menu(update, context)

    try:
        chapters_sorted = sorted(chapters, key=lambda c: float(c.chapter_number))
    except (ValueError, TypeError):
        chapters_sorted = sorted(chapters, key=lambda c: c.chapter_number)

    keyboard = [[InlineKeyboardButton(f"Chapter {c.chapter_number}", callback_data=f"delchap_{c.id}")] for c in chapters_sorted]
    keyboard.append([InlineKeyboardButton("⬅️ Back", callback_data=f"manga_{manga_id}")])
    await update.callback_query.edit_message_text("Select a chapter to delete:", reply_markup=InlineKeyboardMarkup(keyboard))
    return DELETE_CHAPTER_SELECT

async def delete_chapter_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chapter_id = int(update.callback_query.data.split('_')[1])
    session = get_db_session()
    try:
        chapter = session.query(Chapter).get(chapter_id)
        if chapter:
            chapter_num = chapter.chapter_number
            session.delete(chapter)
            session.commit()
            await update.callback_query.edit_message_text(f"✅ Chapter {chapter_num} has been deleted.")
        else:
            await update.callback_query.edit_message_text("❌ Chapter not found.")
    except Exception as e:
        session.rollback()
        await update.callback_query.edit_message_text(f"❌ Error: {e}")
    finally:
        session.close()

    query = update.callback_query
    query.data = f"manga_{context.user_data['manga_id']}"
    return await manage_action_menu(update, context)

# --- Delete Comic Flow ---
async def delete_manga_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manga_id = context.user_data['manga_id']
    keyboard = [
        [InlineKeyboardButton("YES, DELETE IT", callback_data=f"delmanga_yes_{manga_id}")],
        [InlineKeyboardButton("NO, GO BACK", callback_data=f"manga_{manga_id}")]
    ]
    await update.callback_query.edit_message_text(
        "⚠️ **Are you sure?** This will delete the entire comic and all its chapters. This action cannot be undone.",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return DELETE_MANGA_CONFIRM

async def delete_manga_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    manga_id = int(update.callback_query.data.split('_')[2])
    session = get_db_session()
    try:
        manga = session.query(Manga).get(manga_id)
        if manga:
            title = manga.title
            session.delete(manga)
            session.commit()
            await update.callback_query.edit_message_text(f"✅ Comic `{title}` has been permanently deleted.")
        else:
            await update.callback_query.edit_message_text("❌ Comic not found.")
    except Exception as e:
        session.rollback()
        await update.callback_query.edit_message_text(f"❌ Error: {e}")
    finally:
        session.close()

    return await manage_manga_start(update, context)

# --- Update Info Flow ---
async def update_info_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("This feature is a work in progress!", show_alert=True)
    return MANAGE_ACTION_MENU

# --- Generic Cancel/End ---
async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

def run_bot():
    """Definitive startup sequence for the bot."""
    async def main():
        if not TELEGRAM_TOKEN:
            logger.error("Telegram bot cannot start: TELEGRAM_TOKEN is not set.")
            return

        Application._DEFAULT_SHUTDOWN_SIGNALS = ()
        
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
                    CallbackQueryHandler(manage_action_menu, pattern="^manga_"),
                    CallbackQueryHandler(to_main_menu, pattern="^main_menu$")
                ],
                MANAGE_ACTION_MENU: [
                    CallbackQueryHandler(add_chapter_method, pattern="^add_chapter$"),
                    CallbackQueryHandler(update_info_start, pattern="^update_info$"),
                    CallbackQueryHandler(delete_chapter_select, pattern="^delete_chapter$"),
                    CallbackQueryHandler(delete_manga_confirm, pattern="^delete_manga$"),
                    CallbackQueryHandler(manage_manga_start, pattern="^back_to_manage$"),
                ],
                ADD_CHAPTER_METHOD: [
                    CallbackQueryHandler(add_chapter_zip_start, pattern="^zip_upload$"),
                    CallbackQueryHandler(add_chapter_manual_start, pattern="^manual_upload$"),
                    CallbackQueryHandler(manage_action_menu, pattern="^manga_"),
                ],
                ADD_CHAPTER_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chapter_pages_start)],
                ADD_CHAPTER_PAGES: [
                    MessageHandler(filters.PHOTO, add_chapter_page_collect),
                    CommandHandler("done", add_chapter_manual_save)
                ],
                ADD_CHAPTER_ZIP: [MessageHandler(filters.Document.ZIP, add_chapter_zip_process)],
                DELETE_CHAPTER_SELECT: [
                    CallbackQueryHandler(delete_chapter_confirm, pattern="^delchap_"),
                    CallbackQueryHandler(manage_action_menu, pattern="^manga_"),
                ],
                DELETE_MANGA_CONFIRM: [
                    CallbackQueryHandler(delete_manga_execute, pattern="^delmanga_yes_"),
                    CallbackQueryHandler(manage_action_menu, pattern="^manga_"),
                ],
            },
            fallbacks=[CommandHandler("cancel", end_conversation)],
            persistent=False,
            name="comic_cms_conversation"
        )
        application.add_handler(conv_handler)
        
        try:
            logger.info("Bot is initializing...")
            await application.initialize()
            logger.info("Bot is starting to poll for updates...")
            await application.updater.start_polling()
            logger.info("Bot is starting to process updates...")
            await application.start()
            logger.info("Telegram bot is now running.")
            while True:
                await asyncio.sleep(3600)
        finally:
            logger.info("Bot is stopping...")
            if application.updater and application.updater.is_running:
                await application.updater.stop()
            if application.running:
                await application.stop()
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
    if not all([TELEGRAM_TOKEN, ADMIN_USER_ID]):
        print("ERROR: Not intended to be run directly without a .env file or Colab secrets.")
    else:
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
