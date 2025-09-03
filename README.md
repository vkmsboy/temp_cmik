# Python & Telegram Comic Website (v3 - Channel Database Edition)

This is a complete Python web application for a comic/manga website that uses a private Telegram channel as its database, offering a powerful, serverless, and free content management system.

## Features

- **Telegram Channel as a Database**: All comic data is stored as JSON messages in a private channel you control.
- **Full Management via Bot**: A `/start` menu on the bot allows you to add, update, and delete comics and chapters.
- **ZIP Upload for Chapters**: Bulk upload chapters in a single `.zip` file.
- **Dual Reading Modes**: Readers can switch between "Long Strip" and "Paged" views on the website.

## ⚠️ Important Setup Instructions

### 1. Create Your "Database" Channel

1.  In Telegram, create a **New Private Channel**.
2.  Go to the channel's info, select "Administrators," and **add your bot** as an admin. Ensure it has permissions to **Post, Edit, and Delete Messages**.

### 2. Get Your Channel ID

1.  Post a temporary message (e.g., "hello") in your new private channel.
2.  **Forward** that message to `@userinfobot`.
3.  The bot will reply with details. Copy the **Chat ID** (it will be a long negative number, like `-1001234567890`).

### 3. Update Your `.env` / Colab Secrets

You now need **three** secret variables:

-   `TELEGRAM_TOKEN`: Your bot's token from BotFather.
-   `ADMIN_USER_ID`: Your personal numeric Telegram ID.
-   `CHANNEL_ID`: The negative channel ID you just copied.

### 4. How to Use the Bot

-   Send `/start` to your bot to open the main menu.
-   **Add Comic**: Creates a new entry.
-   **Manage Comic**: Allows you to add chapters (via ZIP or manually), update info, or delete comics/chapters.
