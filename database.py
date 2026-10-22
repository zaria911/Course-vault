
"""CourseVault — Telegram course marketplace.
SQLite edition for BotKeep.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation
from html import escape

import asyncpg  # REMOVE THIS LINE IF PRESENT IN YOUR OLD FILE

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

import database


# --------------------------------------------------
# Configuration
# --------------------------------------------------

class Settings(BaseSettings):
    bot_token: str = Field(min_length=20)
    owner_telegram_id: int = Field(gt=0)
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("coursevault")

bot = Bot(
    token=settings.bot_token,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

db: database.SQLiteDatabase | None = None

MAX_ZIP_BYTES = 19 * 1024 * 1024
MAX_TITLE_LENGTH = 100
MAX_CATEGORY_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 2000
MAX_EXTRA_LENGTH = 1000


# --------------------------------------------------
# Course creation states
# --------------------------------------------------

class CourseDraft(StatesGroup):
    title = State()
    category = State()
    price = State()
    description = State()
    impact = State()
    prerequisites = State()
    zip_file = State()


# --------------------------------------------------
# Helpers
# --------------------------------------------------

def get_db() -> database.SQLiteDatabase:
    if db is None:
        raise RuntimeError("Database has not been initialized.")
    return db


def is_owner(user_id: int | None) -> bool:
    return user_id == settings.owner_telegram_id


def money(price_paise: int) -> str:
    return f"₹{price_paise / 100:,.2f}"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Browse courses",
                    callback_data="browse",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗂 Categories",
                    callback_data="categories",
                ),
                InlineKeyboardButton(
                    text="❤️ Wishlist",
                    callback_data="wishlist",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🤖 AI features",
                    callback_data="ai_info",
                )
            ],
        ]
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏠 Main menu",
                    callback_data="home",
                )
            ]
        ]
    )


def course_keyboard(course_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="❤️ Save to wishlist",
                    callback_data=f"save:{course_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 Buy course (coming soon)",
                    callback_data="buy_disabled",
                )
            ],
            [
                InlineKeyboardButton(
                    text="↩️ Browse courses",
                    callback_data="browse",
                )
            ],
        ]
    )


async def show_courses(
    message: Message,
    category: str | None = None,
) -> None:
    courses = await database.list_courses(get_db(), category)

    if not courses:
        await message.answer(
            "No published courses are available yet.",
            reply_markup=back_menu(),
        )
        return

    for course in courses[:30]:
        title = escape(course["title"])
        category_name = escape(course["category"])
        price = money(course["price_paise"])

        await message.answer(
            f"<b>{title}</b>\n"
            f"📂 {category_name}\n"
            f"💰 {price}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View course",
                            callback_data=f"course:{course['id']}",
                        )
                    ]
                ]
            ),
        )


async def show_categories(message: Message) -> None:
    categories = await database.list_categories(get_db())

    if not categories:
        await message.answer(
            "No course categories are available yet.",
            reply_markup=back_menu(),
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text=category[:60],
                callback_data=f"category:{index}",
            )
        ]
        for index, category in enumerate(categories[:50])
    ]
    rows.append(
        [InlineKeyboardButton(text="🏠 Home", callback_data="home")]
    )

    await message.answer(
        "<b>📂 Course categories</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


# --------------------------------------------------
# Basic commands
# --------------------------------------------------

@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return

    await database.upsert_user(
        get_db(),
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name or "",
    )

    await message.answer(
        "👋 Welcome to <b>CourseVault</b>!\n\n"
        "Explore courses, save your favourites, and discover "
        "learning opportunities.\n\n"
        "Course purchases are not available yet.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    text = (
        "<b>CourseVault commands</b>\n\n"
        "/start — Open the main menu\n"
        "/courses — Browse published courses\n"
        "/categories — Browse categories\n"
        "/wishlist — View saved courses\n"
    )

    if message.from_user and is_owner(message.from_user.id):
        text += (
            "\n<b>Owner commands</b>\n"
            "/addcourse — Create a course draft\n"
            "/drafts — List course drafts\n"
            "/publish ID — Request publication confirmation\n"
        )

    await message.answer(text)


@dp.message(Command("courses"))
async def courses_command(message: Message) -> None:
    await show_courses(message)


@dp.message(Command("categories"))
async def categories_command(message: Message) -> None:
    await show_categories(message)


@dp.message(Command("wishlist"))
async def wishlist_command(message: Message) -> None:
    if not message.from_user:
        return

    courses = await database.list_wishlist(
        get_db(),
        message.from_user.id,
    )

    if not courses:
        await message.answer(
            "Your wishlist is empty.",
            reply_markup=back_menu(),
        )
        return

    for course in courses[:30]:
        await message.answer(
            f"❤️ <b>{escape(course['title'])}</b>\n"
            f"💰 {money(course['price_paise'])}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View course",
                            callback_data=f"course:{course['id']}",
                        )
                    ]
                ]
            ),
        )


# --------------------------------------------------
# Main menu callbacks
# --------------------------------------------------

@dp.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "<b>CourseVault</b>\nChoose an option:",
            reply_markup=main_menu(),
        )


@dp.callback_query(F.data == "browse")
async def browse_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_courses(callback.message)


@dp.callback_query(F.data == "categories")
async def categories_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await show_categories(callback.message)


@dp.callback_query(F.data.startswith("category:"))
async def category_callback(callback: CallbackQuery) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    try:
        index = int(callback.data.split(":", 1)[1])
        categories = await database.list_categories(get_db())

        if index < 0 or index >= len(categories):
            await callback.answer("Category not found.", show_alert=True)
            return

        await callback.answer()
        await show_courses(callback.message, categories[index])

    except (ValueError, IndexError):
        await callback.answer("Invalid category.", show_alert=True)


@dp.callback_query(F.data.startswith("course:"))
async def course_callback(callback: CallbackQuery) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course.", show_alert=True)
        return

    course = await database.get_course(get_db(), course_id)

    if not course:
        await callback.answer("Course not found.", show_alert=True)
        return

    await callback.answer()

    description = escape(course["description"] or "No description provided.")
    impact = escape(course["impact"] or "Not specified")
    prerequisites = escape(course["prerequisites"] or "Not specified")

    await callback.message.answer(
        f"<b>{escape(course['title'])}</b>\n\n"
        f"📂 {escape(course['category'])}\n"
        f"💰 {money(course['price_paise'])}\n\n"
        f"<b>Description</b>\n{description}\n\n"
        f"<b>What you'll learn</b>\n{impact}\n\n"
        f"<b>Prerequisites</b>\n{prerequisites}",
        reply_markup=course_keyboard(course_id),
    )


@dp.callback_query(F.data == "wishlist")
async def wishlist_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.from_user or not callback.message:
        return

    courses = await database.list_wishlist(
        get_db(),
        callback.from_user.id,
    )

    if not courses:
        await callback.message.answer(
            "Your wishlist is empty.",
            reply_markup=back_menu(),
        )
        return

    for course in courses[:30]:
        await callback.message.answer(
            f"❤️ <b>{escape(course['title'])}</b>\n"
            f"💰 {money(course['price_paise'])}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View course",
                            callback_data=f"course:{course['id']}",
                        )
                    ]
                ]
            ),
        )


@dp.callback_query(F.data.startswith("save:"))
async def save_callback(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer()
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course.", show_alert=True)
        return

    if not callback.from_user:
        await callback.answer("Unable to identify your account.")
        return

    saved = await database.add_to_wishlist(
        get_db(),
        callback.from_user.id,
        course_id,
    )

    if saved:
        await callback.answer("Saved to your wishlist ❤️")
    else:
        await callback.answer(
            "Course unavailable. Try /start first.",
            show_alert=True,
        )


@dp.callback_query(F.data == "buy_disabled")
async def buy_disabled_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Purchases are not enabled yet.",
        show_alert=True,
    )


@dp.callback_query(F.data == "ai_info")
async def ai_info_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "🤖 <b>AI features are planned</b>\n\n"
            "AI tutoring and AI Course Studio are not enabled yet.",
            reply_markup=back_menu(),
        )


# --------------------------------------------------
# Owner-only course draft workflow
# --------------------------------------------------

@dp.message(Command("addcourse"))
async def add_course_command(
    message: Message,
    state: FSMContext,
) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("⛔ This command is owner-only.")
        return

    await state.clear()
    await state.set_state(CourseDraft.title)
    await message.answer("Enter the course title (maximum 100 characters).")


@dp.message(CourseDraft.title)
async def draft_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()

    if not title or len(title) > MAX_TITLE_LENGTH:
        await message.answer("Enter a title between 1 and 100 characters.")
        return

    await state.update_data(title=title)
    await state.set_state(CourseDraft.category)
    await message.answer("Enter the course category.")


@dp.message(CourseDraft.category)
async def draft_category(message: Message, state: FSMContext) -> None:
    category = (message.text or "").strip()

    if not category or len(category) > MAX_CATEGORY_LENGTH:
        await message.answer("Enter a category between 1 and 60 characters.")
        return

    await state.update_data(category=category)
    await state.set_state(CourseDraft.price)
    await message.answer("Enter the course price in INR, for example: 249.00")


@dp.message(CourseDraft.price)
async def draft_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()

    try:
        price = Decimal(raw)
        if not price.is_finite() or price < 0 or price > Decimal("1000000"):
            raise InvalidOperation
        price_paise = int(price * 100)
        if price * 100 != price_paise:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer(
            "Enter a valid non-negative INR amount with up to two decimal places."
        )
        return

    await state.update_data(price_paise=price_paise)
    await state.set_state(CourseDraft.description)
    await message.answer("Enter the course description (maximum 2000 characters).")


@dp.message(CourseDraft.description)
async def draft_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()

    if len(description) > MAX_DESCRIPTION_LENGTH:
        await message.answer("Description is too long. Maximum 2000 characters.")
        return

    await state.update_data(description=description)
    await state.set_state(CourseDraft.impact)
    await message.answer("What will students learn? (maximum 1000 characters)")


@dp.message(CourseDraft.impact)
async def draft_impact(message: Message, state: FSMContext) -> None:
    impact = (message.text or "").strip()

    if len(impact) > MAX_EXTRA_LENGTH:
        await message.answer("Please shorten this to 1000 characters or fewer.")
        return

    await state.update_data(impact=impact)
    await state.set_state(CourseDraft.prerequisites)
    await message.answer("Enter prerequisites, or type `None`.")


@dp.message(CourseDraft.prerequisites)
async def draft_prerequisites(message: Message, state: FSMContext) -> None:
    prerequisites = (message.text or "").strip()

    if len(prerequisites) > MAX_EXTRA_LENGTH:
        await message.answer("Please shorten this to 1000 characters or fewer.")
        return

    if prerequisites.lower() == "none":
        prerequisites = ""

    await state.update_data(prerequisites=prerequisites)
    await state.set_state(CourseDraft.zip_file)
    await message.answer(
        "Now upload the course ZIP as a document.\n"
        "It must have a .zip filename and be no larger than 19 MiB.\n"
        "The bot will save it as a draft; it will not publish automatically."
    )


@dp.message(CourseDraft.zip_file, F.document)
async def draft_zip(message: Message, state: FSMContext) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await state.clear()
        await message.answer("⛔ This command is owner-only.")
        return

    document = message.document
    if not document:
        await message.answer("Please upload the ZIP as a document.")
        return

    filename = document.file_name or ""

    if not filename.lower().endswith(".zip"):
        await message.answer("The filename must end in .zip. Try again.")
        return

    if document.file_size is None or document.file_size > MAX_ZIP_BYTES:
        await message.answer("The ZIP must be no larger than 19 MiB. Try again.")
        return

    data = await state.get_data()
    connection = get_db()._connect()

    try:
        cursor = connection.execute(
            """
            INSERT INTO courses (
                title, category, price_paise, description,
                impact, prerequisites, telegram_file_id,
                status, created_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (
                data["title"],
                data["category"],
                data["price_paise"],
                data["description"],
                data["impact"],
                data["prerequisites"],
                document.file_id,
                message.from_user.id,
            ),
        )
        course_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO audit_logs (
                actor_telegram_id, action, target_type,
                target_id, details
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message.from_user.id,
                "course_draft_created",
                "course",
                str(course_id),
                '{"source":"telegram_upload"}',
            ),
        )
        connection.commit()

    except Exception:
        connection.rollback()
        logger.exception("Failed to save course draft")
        await message.answer(
            "Could not save the draft. Check the server logs and try again."
        )
        return
    finally:
        connection.close()

    await state.clear()

    await message.answer(
        f"✅ Draft created successfully!\n"
        f"Course ID: <code>{course_id}</code>\n"
        f"Title: <b>{escape(data['title'])}</b>\n\n"
        f"Use /drafts to review drafts, then /publish {course_id} "
        f"to request publication confirmation."
    )


@dp.message(CourseDraft.zip_file)
async def draft_zip_wrong_type(message: Message) -> None:
    await message.answer("Please upload the course ZIP as a document.")


@dp.message(Command("drafts"))
async def drafts_command(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("⛔ This command is owner-only.")
        return

    drafts = await database.list_course_drafts(get_db())

    if not drafts:
        await message.answer("There are no course drafts.")
        return

    for course in drafts[:50]:
        await message.answer(
            f"<b>Draft #{course['id']}</b>\n"
            f"Title: {escape(course['title'])}\n"
            f"Category: {escape(course['category'])}\n"
            f"Price: {money(course['price_paise'])}\n"
            f"Publish with /publish {course['id']}"
        )


@dp.message(Command("publish"))
async def publish_command(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("⛔ This command is owner-only.")
        return

    parts = (message.text or "").split(maxsplit=1)

    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: /publish COURSE_ID")
        return

    course_id = int(parts[1])
    connection = get_db()._connect()

    try:
        course = connection.execute(
            "SELECT id, title FROM courses WHERE id = ? AND status = 'draft'",
            (course_id,),
        ).fetchone()
    finally:
        connection.close()

    if not course:
        await message.answer("Draft not found.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Confirm publish",
                    callback_data=f"publish_confirm:{course_id}",
                ),
                InlineKeyboardButton(
                    text="❌ Cancel",
                    callback_data="publish_cancel",
                ),
            ]
        ]
    )

    await message.answer(
        f"Publish this course?\n\n"
        f"<b>{escape(course['title'])}</b>\n"
        f"ID: <code>{course_id}</code>",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data.startswith("publish_confirm:"))
async def publish_confirm_callback(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Owner-only action.", show_alert=True)
        return

    if not callback.data:
        await callback.answer("Invalid request.", show_alert=True)
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course ID.", show_alert=True)
        return

    published = await database.publish_course(
        get_db(),
        course_id,
        callback.from_user.id,
    )

    if published:
        await callback.answer("Course published!")
        if callback.message:
            await callback.message.answer(
                f"✅ Course #{course_id} is now published."
            )
    else:
        await callback.answer(
            "Draft not found or already published.",
            show_alert=True,
        )


@dp.callback_query(F.data == "publish_cancel")
async def publish_cancel_callback(callback: CallbackQuery) -> None:
    await callback.answer("Publication cancelled.")


# --------------------------------------------------
# Fallback
# --------------------------------------------------

@dp.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "I'm not sure what you mean. Use /help to see the available commands.",
        reply_markup=main_menu(),
    )


# --------------------------------------------------
# Startup and shutdown
# --------------------------------------------------

async def main() -> None:
    global db

    db = await database.create_pool()
    logger.info("SQLite database initialized.")

    try:
        me = await bot.get_me()
        logger.info("Starting CourseVault as @%s", me.username)
        await dp.start_polling(bot)
    finally:
        await database.close_pool(db)
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
