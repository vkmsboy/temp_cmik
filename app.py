import os
import threading
import logging
import requests
import asyncio

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
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
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
        session.close()
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
        session.close()
        abort(404)
    session.close()
    return render_template("chapter_reader.html", chapter=chapter)

@flask_app.route("/image/<file_id>")
def get_telegram_image(file_id):
    if not TELEGRAM_TOKEN:
        logger.error("Cannot fetch image: TELEGRAM_TOKEN is not configured.")
        abort(500)
    try:
        api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getFile"
        params = {'file_id': file_id}
        response = requests.get(api_url, params=params)
        response.raise_for_status()
        
        file_path = response.json()['result']['file_path']
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        
        return redirect(image_url)
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching image from Telegram API for file_id {file_id}: {e}")
        abort(404)
    except KeyError:
        logger.error(f"Unexpected JSON response from Telegram API for file_id {file_id}")
        abort(500)

# --- TELEGRAM BOT LOGIC ---
(CHOOSE_ACTION, ASK_MANGA_TITLE, ASK_MANGA_DESC, ASK_MANGA_COVER, CHOOSE_MANGA_FOR_CHAPTER, ASK_CHAPTER_NUMBER, UPLOAD_PAGES) = range(7)

async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not ADMIN_USER_ID or update.effective_user.id != ADMIN_USER_ID:
        if update.message:
            await update.message.reply_text("Sorry, you are not authorized to use this command.")
        return False
    return True

async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_only(update, context):
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton("Add New Manga", callback_data="add_manga")], [InlineKeyboardButton("Add New Chapter", callback_data="add_chapter")]]
    await update.message.reply_text("What would you like to do?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_ACTION

async def ask_manga_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="Please send me the title of the new manga.")
    return ASK_MANGA_TITLE

async def save_manga_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["manga_title"] = update.message.text
    await update.message.reply_text("Got it. Now, send me a short description for the manga.")
    return ASK_MANGA_DESC

async def save_manga_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["manga_desc"] = update.message.text
    await update.message.reply_text("Great. Please send the cover image for this manga.")
    return ASK_MANGA_COVER

async def save_manga_cover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("That doesn't look like an image. Please send a photo.")
        return ASK_MANGA_COVER
    photo = update.message.photo[-1]
    context.user_data["manga_cover_id"] = photo.file_id
    session = get_db_session()
    try:
        new_manga = Manga(title=context.user_data["manga_title"], description=context.user_data["manga_desc"], cover_file_id=context.user_data["manga_cover_id"])
        session.add(new_manga)
        session.commit()
        await update.message.reply_text(f"✅ Successfully added manga: {new_manga.title}")
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving new manga: {e}")
        await update.message.reply_text("❌ Error saving manga. A manga with this title may already exist.")
    finally:
        session.close()
    context.user_data.clear()
    return ConversationHandler.END

async def choose_manga_for_chapter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    session = get_db_session()
    all_manga = session.query(Manga).order_by(Manga.title).all()
    session.close()
    if not all_manga:
        await query.edit_message_text(text="No manga found. Please add a manga first via the 'Add New Manga' option.")
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(m.title, callback_data=f"manga_{m.id}")] for m in all_manga]
    await query.edit_message_text(text="Which manga are you adding a chapter to?", reply_markup=InlineKeyboardMarkup(keyboard))
    return CHOOSE_MANGA_FOR_CHAPTER

async def ask_chapter_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data["manga_id"] = int(query.data.split("_")[1])
    await query.edit_message_text(text="Please send me the chapter number (e.g., '1', '25.5', '103').")
    return ASK_CHAPTER_NUMBER

async def start_page_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["chapter_number"] = update.message.text
    context.user_data["pages"] = []
    await update.message.reply_text("OK. Now, send me all the page images for this chapter, one by one.\nWhen you are finished, send the /done command.")
    return UPLOAD_PAGES

async def collect_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.photo:
        await update.message.reply_text("That is not an image. Please send photos or type /done to finish.")
        return UPLOAD_PAGES
    photo = update.message.photo[-1]
    context.user_data.setdefault("pages", []).append(photo.file_id)
    await update.message.reply_text(f"Page {len(context.user_data['pages'])} received. Send the next page or /done.")
    return UPLOAD_PAGES

async def save_chapter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data.get("pages"):
        await update.message.reply_text("No pages were uploaded. Cancelling operation.")
        context.user_data.clear()
        return ConversationHandler.END
    session = get_db_session()
    try:
        new_chapter = Chapter(manga_id=context.user_data["manga_id"], chapter_number=context.user_data["chapter_number"])
        session.add(new_chapter)
        session.flush()
        for i, file_id in enumerate(context.user_data["pages"]):
            session.add(Page(chapter_id=new_chapter.id, page_number=i + 1, file_id=file_id))
        session.commit()
        manga = session.query(Manga).get(context.user_data["manga_id"])
        await update.message.reply_text(f"✅ Success! Chapter {new_chapter.chapter_number} of '{manga.title}' has been added with {len(context.user_data['pages'])} pages.")
    except Exception as e:
        session.rollback()
        logger.error(f"Error saving chapter: {e}")
        await update.message.reply_text("❌ An error occurred while saving the chapter.")
    finally:
        session.close()
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message_source = update.message or update.callback_query.message
    await message_source.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

def run_bot():
    """Run the bot."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    if not TELEGRAM_TOKEN:
        logger.error("Telegram bot cannot start: TELEGRAM_TOKEN is not set.")
        return
    
    # --- CHANGE 1: REMOVED .shutdown_signals([]) FROM HERE ---
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("upload", start_upload)],
        states={
            CHOOSE_ACTION: [CallbackQueryHandler(ask_manga_title, pattern="^add_manga$"), CallbackQueryHandler(choose_manga_for_chapter, pattern="^add_chapter$")],
            ASK_MANGA_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_manga_title)],
            ASK_MANGA_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_manga_desc)],
            ASK_MANGA_COVER: [MessageHandler(filters.PHOTO, save_manga_cover)],
            CHOOSE_MANGA_FOR_CHAPTER: [CallbackQueryHandler(ask_chapter_number, pattern="^manga_")],
            ASK_CHAPTER_NUMBER: [MessageHandler(filters.TEXT & ~filters.COMMAND, start_page_upload)],
            UPLOAD_PAGES: [MessageHandler(filters.PHOTO, collect_page), CommandHandler("done", save_chapter)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel)],
    )

    application.add_handler(conv_handler)
    logger.info("Telegram bot is starting polling...")
    
    # --- CHANGE 2: ADDED shutdown_signals=[] TO THE CORRECT PLACE ---
    application.run_polling(shutdown_signals=[])


if __name__ == "__main__":
    # This part is now primarily for local testing, Colab starts the threads.
    if not all([TELEGRAM_TOKEN, ADMIN_USER_ID]):
        print("="*60)
        print("ERROR: Not intended to be run directly without a .env file.")
        print("Please run via the Colab notebook or a local .env setup.")
        print("="*60)
    else:
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        print("Starting Flask web server on http://127.0.0.1:5000")
        flask_app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

