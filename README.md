# Python & Telegram Comic/Manga Website (v2)

This project contains a complete Python-based web application for a comic or manga website. It uses Flask for the web frontend and a Telegram Bot for all content management, including a powerful ZIP upload feature.

## Features

-   **Full Management via Bot:** A new `/start` menu on the bot allows you to add, update, and delete comics and chapters.
-   **ZIP Upload for Chapters:** Upload a single `.zip` file containing multiple chapter folders (e.g., "Chapter 1", "Chapter 2") to add them to a comic in bulk. The bot automatically extracts the chapter numbers.
-   **Two-Step Comic Creation:** Add your comic's information (title, cover) first, then add chapters at any time.
-   **Dual Reading Modes:** On the website, readers can now switch between a "Long Strip" view (for webtoons/manhwa) and a "Paged" view (for traditional manga).

## How to Use the Bot

1.  **Start the Bot:** Open a chat with your bot on Telegram and send the `/start` command. This will bring up the main menu.

2.  **Add a New Comic:**
    -   Choose "➕ Add New Comic".
    -   The bot will ask for the title, description, and cover image. This creates the comic series on your site.

3.  **Manage an Existing Comic:**
    -   Choose "📚 Manage Existing Comic".
    -   Select the comic you want to manage from the list.
    -   You will get a new menu with these options:
        -   **➕ Add Chapter(s):** This is where you upload new chapters. The bot will ask for your preferred upload method:
            -   **ZIP Upload:** For bulk uploads. Your `.zip` file should be structured like this:
                ```
                MyAwesomeManga.zip
                └── Chapters/
                    ├── Chapter 1/
                    │   ├── 01.jpg
                    │   └── 02.png
                    ├── Chapter 2.5/
                    │   ├── page01.jpg
                    │   └── page02.jpg
                    └── ...
                ```
        -   **Manual Upload:** For adding a single chapter by sending images one by one.
    -   **✏️ Update Info:** Change the comic's title, description, or cover image.
    -   **🗑️ Delete Chapter:** Select and delete a specific chapter.
    -   **❌ Delete Comic:** Permanently delete the entire comic series.

## Setup

The setup process is the same as before. If you are updating, simply replace the contents of your `app.py` and `templates/chapter_reader.html` with the new code provided. Ensure you have `python-dotenv`, `Flask`, `python-telegram-bot`, `SQLAlchemy`, and `requests` installed.
