
"""CourseVault Telegram bot entry point.

Python 3.13 compatible.
Requires aiogram 3.x, asyncpg, and pydantic-settings.

Payments remain disabled until the FamGateway integration is implemented
and verified. Uploaded ZIPs remain unpublished until the inspection workflow
marks them as validated.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal, InvalidOperation
from html import escape

import asyncpg
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


class Settings(BaseSettings):
    bot_token: str = Field(min_length=20)
    owner_telegram_id: int = Field(gt=0)
    database_url: str = Field(min_length=1)
    app_env: str = "production"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )


settings = Settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("coursevault")

dp = Dispatcher()
pool: asyncpg.Pool | None = None

# Conservative provisional upload limit. Confirm Telegram and host limits
# before production; ZIP content inspection is a separate required control.
MAX_ZIP_BYTES = 45 * 1024 * 1024
MAX_TITLE_LENGTH = 160
MAX_CATEGORY_LENGTH = 80
MAX_DESCRIPTION_LENGTH = 3500


class CourseDraft(StatesGroup):
    title = State()
    category = State()
    price = State()
    description = State()
    impact = State()
    prerequisites = State()
    zip_file = State()


def db() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database is not initialized")
    return pool


def is_owner(user_id: int) -> bool:
    return user_id == settings.owner_telegram_id


def money(price_paise: int) -> str:
    return f"₹{Decimal(price_paise) / Decimal(100):,.2f}"


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📚 Browse Courses",
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
                    text="🤖 AI Tutor",
                    callback_data="ai_info",
                )
            ],
        ]
    )


def course_keyboard(course_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Chat with AI",
                    callback_data=f"ai_course:{course_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 Buy Now",
                    callback_data=f"buy:{course_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔗 Share",
                    callback_data=f"share:{course_id}",
                ),
                InlineKeyboardButton(
                    text="❤️ Save",
                    callback_data=f"save:{course_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Main Menu",
                    callback_data="home",
                )
            ],
        ]
    )


async def show_courses(
    message: Message,
    category: str | None = None,
) -> None:
    rows = await database.list_courses(db(), category)

    if not rows:
        await message.answer(
            "No published courses found yet.",
            reply_markup=main_menu(),
        )
        return

    for row in rows:
        await message.answer(
            f"<b>{escape(row['title'])}</b>\n"
            f"Category: {escape(row['category'])}\n"
            f"Price: <b>{money(row['price_paise'])}</b>",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View Course",
                            callback_data=f"course:{row['id']}",
                        )
                    ]
                ]
            ),
        )


async def show_categories(message: Message) -> None:
    categories = await database.list_categories(db())

    if not categories:
        await message.answer("No categories are available yet.")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=category,
                    callback_data=f"category:{index}",
                )
            ]
            for index, category in enumerate(categories)
        ]
    )
    await message.answer("Choose a category:", reply_markup=keyboard)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    await database.upsert_user(
        db(),
        user.id,
        user.username,
        user.first_name or "",
    )

    await message.answer(
        "🎓 <b>Welcome to CourseVault!</b>\n\n"
        "Discover courses, explore categories, and save your favourites.\n\n"
        "Choose an option below to get started.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>CourseVault Help</b>\n\n"
        "/start — Main menu\n"
        "/courses — Browse courses\n"
        "/categories — Browse categories\n"
        "/wishlist — Saved courses\n\n"
        "<b>Owner commands</b>\n"
        "/addcourse — Create a course draft\n"
        "/drafts — List drafts\n"
        "/publish ID — Request publication review"
    )


@dp.message(Command("courses"))
async def courses_command(message: Message) -> None:
    await show_courses(message)


@dp.message(Command("categories"))
async def categories_command(message: Message) -> None:
    await show_categories(message)


@dp.message(Command("wishlist"))
async def wishlist_command(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    rows = await database.list_wishlist(db(), user.id)
    if not rows:
        await message.answer("Your wishlist is empty.")
        return

    for row in rows:
        await message.answer(
            f"❤️ <b>{escape(row['title'])}</b>\n"
            f"{escape(row['category'])} · {money(row['price_paise'])}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View Course",
                            callback_data=f"course:{row['id']}",
                        )
                    ]
                ]
            ),
        )


@dp.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "CourseVault main menu:",
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
    await callback.answer()

    if not callback.data or not callback.message:
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.message.answer("Invalid category selection.")
        return

    categories = await database.list_categories(db())
    if index < 0 or index >= len(categories):
        await callback.message.answer(
            "Categories changed. Please open Categories again."
        )
        return

    await show_courses(callback.message, categories[index])


@dp.callback_query(F.data.startswith("course:"))
async def course_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.data or not callback.message:
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.message.answer("Invalid course selection.")
        return

    course = await database.get_course(db(), course_id)
    if course is None:
        await callback.message.answer("This course is unavailable.")
        return

    text = (
        f"📘 <b>{escape(course['title'])}</b>\n\n"
        f"<b>Category:</b> {escape(course['category'])}\n"
        f"<b>Price:</b> {money(course['price_paise'])}\n\n"
        f"<b>About this course</b>\n"
        f"{escape(course['description'])}\n\n"
        f"<b>Impact on You</b>\n"
        f"{escape(course['impact'] or 'Not specified yet.')}\n\n"
        f"<b>Prerequisites</b>\n"
        f"{escape(course['prerequisites'] or 'Not specified.')}"
    )
    await callback.message.answer(
        text,
        reply_markup=course_keyboard(course_id),
    )


@dp.callback_query(F.data == "wishlist")
async def wishlist_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.message:
        return

    rows = await database.list_wishlist(db(), callback.from_user.id)
    if not rows:
        await callback.message.answer("Your wishlist is empty.")
        return

    for row in rows:
        await callback.message.answer(
            f"❤️ <b>{escape(row['title'])}</b>\n"
            f"{escape(row['category'])} · {money(row['price_paise'])}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="View Course",
                            callback_data=f"course:{row['id']}",
                        )
                    ]
                ]
            ),
        )


@dp.callback_query(F.data.startswith("save:"))
async def save_callback(callback: CallbackQuery) -> None:
    if not callback.data:
        await callback.answer("Invalid request.", show_alert=True)
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course.", show_alert=True)
        return

    course = await database.get_course(db(), course_id)
    if course is None:
        await callback.answer("Course unavailable.", show_alert=True)
        return

    saved = await database.add_to_wishlist(
        db(),
        callback.from_user.id,
        course_id,
    )

    await callback.answer(
        "Added to wishlist." if saved else "Already saved.",
        show_alert=False,
    )


@dp.callback_query(F.data.startswith("share:"))
async def share_callback(callback: CallbackQuery) -> None:
    if not callback.data or not callback.message:
        await callback.answer()
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course.", show_alert=True)
        return

    course = await database.get_course(db(), course_id)
    if course is None:
        await callback.answer("Course unavailable.", show_alert=True)
        return

    bot_info = await callback.bot.get_me()
    if not bot_info.username:
        await callback.answer("Bot username unavailable.", show_alert=True)
        return

    link = f"https://t.me/{bot_info.username}?start=course_{course_id}"
    await callback.answer()
    await callback.message.answer(
        f"Share <b>{escape(course['title'])}</b> with a friend:\n{link}"
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "🛒 Checkout is not enabled yet. No payment has been taken "
            "and no course access has been granted."
        )


@dp.callback_query(F.data == "ai_info")
async def ai_info_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🤖 The AI Tutor will be enabled after provider integration, "
            "content indexing, and usage limits are configured."
        )


@dp.callback_query(F.data.startswith("ai_course:"))
async def ai_course_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "The course-specific AI tutor is not enabled yet."
        )


@dp.message(Command("addcourse"))
async def add_course(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    await state.clear()
    await state.set_state(CourseDraft.title)
    await message.answer("Enter the course title.")


@dp.message(CourseDraft.title)
async def draft_title(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > MAX_TITLE_LENGTH:
        await message.answer(
            f"Enter a title between 1 and {MAX_TITLE_LENGTH} characters."
        )
        return

    await state.update_data(title=value)
    await state.set_state(CourseDraft.category)
    await message.answer("Enter the course category.")


@dp.message(CourseDraft.category)
async def draft_category(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > MAX_CATEGORY_LENGTH:
        await message.answer(
            f"Enter a category between 1 and {MAX_CATEGORY_LENGTH} characters."
        )
        return

    await state.update_data(category=value)
    await state.set_state(CourseDraft.price)
    await message.answer("Enter the price in INR, e.g. 199 or 199.50.")


@dp.message(CourseDraft.price)
async def draft_price(message: Message, state: FSMContext) -> None:
    try:
        price = Decimal((message.text or "").strip())
        if not price.is_finite() or price < 0 or price > Decimal("1000000"):
            raise InvalidOperation

        paise_decimal = price * 100
        if paise_decimal != paise_decimal.to_integral_value():
            raise InvalidOperation

        paise = int(paise_decimal)
    except (InvalidOperation, ValueError):
        await message.answer("Enter a valid INR amount with up to 2 decimals.")
        return

    await state.update_data(price_paise=paise)
    await state.set_state(CourseDraft.description)
    await message.answer("Enter the course description.")


@dp.message(CourseDraft.description)
async def draft_description(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > MAX_DESCRIPTION_LENGTH:
        await message.answer(
            f"Description must be between 1 and {MAX_DESCRIPTION_LENGTH} "
            "characters."
        )
        return

    await state.update_data(description=value)
    await state.set_state(CourseDraft.impact)
    await message.answer("Describe the realistic benefits for learners.")


@dp.message(CourseDraft.impact)
async def draft_impact(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 2000:
        await message.answer("Keep the impact section under 2,000 characters.")
        return

    await state.update_data(impact=value)
    await state.set_state(CourseDraft.prerequisites)
    await message.answer("Enter prerequisites, or send /skip.")


@dp.message(CourseDraft.prerequisites, Command("skip"))
async def skip_prerequisites(
    message: Message,
    state: FSMContext,
) -> None:
    await state.update_data(prerequisites="")
    await state.set_state(CourseDraft.zip_file)
    await message.answer("Upload the course ZIP as a document.")


@dp.message(CourseDraft.prerequisites)
async def draft_prerequisites(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 1500:
        await message.answer("Keep prerequisites under 1,500 characters.")
        return

    await state.update_data(prerequisites=value)
    await state.set_state(CourseDraft.zip_file)
    await message.answer("Upload the course ZIP as a document.")


@dp.message(CourseDraft.zip_file, F.document)
async def draft_zip(message: Message, state: FSMContext) -> None:
    user = message.from_user
    document = message.document

    if user is None or not is_owner(user.id):
        await state.clear()
        await message.answer("⛔ Not authorized.")
        return

    if document is None:
        await message.answer("Please upload a ZIP document.")
        return

    filename = (document.file_name or "").strip()
    if not filename.lower().endswith(".zip"):
        await message.answer("The uploaded filename must end in .zip.")
        return

    if document.file_size is None or document.file_size <= 0:
        await message.answer("Unable to verify the file size. Try again.")
        return

    if document.file_size > MAX_ZIP_BYTES:
        await message.answer(
            "The ZIP exceeds the current provisional upload limit."
        )
        return

    data = await state.get_data()

    # A ZIP filename and size do not prove the archive is valid or safe.
    # Store only as an unpublished, unverified draft.
    course_id = await db().fetchval(
        """
        INSERT INTO courses (
            title,
            category,
            price_paise,
            description,
            impact,
            prerequisites,
            zip_file_id,
            zip_size_bytes,
            zip_validation_status,
            status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'unverified', 'draft')
        RETURNING id
        """,
        data["title"],
        data["category"],
        data["price_paise"],
        data["description"],
        data["impact"],
        data["prerequisites"],
        document.file_id,
        document.file_size,
    )

    await database.write_audit_log(
        db(),
        user.id,
        "course_draft_created",
        "course",
        str(course_id),
        {"filename_extension": ".zip", "size_bytes": document.file_size},
    )

    await state.clear()
    await message.answer(
        "✅ <b>Draft created</b>\n\n"
        f"ID: <code>{course_id}</code>\n"
        f"Title: <b>{escape(data['title'])}</b>\n"
        f"Price: {money(data['price_paise'])}\n"
        f"ZIP size: {document.file_size:,} bytes\n\n"
        "Status: <b>Unverified draft</b>\n"
        "The ZIP must pass archive inspection before it can be published.\n"
        f"Review with /drafts. Publication request: /publish {course_id}"
    )


@dp.message(CourseDraft.zip_file)
async def draft_zip_invalid(message: Message) -> None:
    await message.answer("Please upload the ZIP as a document attachment.")


@dp.message(Command("drafts"))
async def drafts_command(message: Message) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    rows = await database.list_course_drafts(db())

    if not rows:
        await message.answer("There are no course drafts.")
        return

    for row in rows:
        await message.answer(
            f"ID: <code>{row['id']}</code>\n"
            f"<b>{escape(row['title'])}</b>\n"
            f"{escape(row['category'])} · {money(row['price_paise'])}\n"
            f"ZIP validation: <code>"
            f"{escape(row['zip_validation_status'])}</code>"
        )


@dp.message(Command("publish"))
async def publish_command(message: Message) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Usage: /publish COURSE_ID")
        return

    course_id = int(parts[1])
    course = await db().fetchrow(
        """
        SELECT id, title, status, zip_validation_status
        FROM courses
        WHERE id = $1
        """,
        course_id,
    )

    if course is None or course["status"] != "draft":
        await message.answer("Draft not found.")
        return

    if course["zip_validation_status"] != "passed":
        await message.answer(
            "⛔ Publication blocked: this ZIP has not passed archive "
            "inspection. The inspection workflow has not been implemented "
            "yet, so this draft cannot be published."
        )
        await database.write_audit_log(
            db(),
            message.from_user.id,
            "course_publish_blocked",
            "course",
            str(course_id),
            {"reason": "zip_not_validated"},
        )
        return

    await message.answer(
        f"Publish <b>{escape(course['title'])}</b>?\n\n"
        "Confirm only after checking distribution rights and the ZIP review.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Confirm publication",
                        callback_data=f"confirm_publish:{course_id}",
                    ),
                    InlineKeyboardButton(
                        text="Cancel",
                        callback_data="cancel_publish",
                    ),
                ]
            ]
        ),
    )


@dp.callback_query(F.data == "cancel_publish")
async def cancel_publish(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return

    await callback.answer("Cancelled.")
    if callback.message:
        await callback.message.answer("Publication cancelled; no changes made.")


@dp.callback_query(F.data.startswith("confirm_publish:"))
async def confirm_publish(callback: CallbackQuery) -> None:
    if not is_owner(callback.from_user.id):
        await callback.answer("Not authorized.", show_alert=True)
        return

    if not callback.data:
        await callback.answer("Invalid request.", show_alert=True)
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid course ID.", show_alert=True)
        return

    result = await database.publish_course(
        db(),
        callback.from_user.id,
        course_id,
    )

    if result == "published":
        await callback.answer("Published.")
        if callback.message:
            await callback.message.answer("✅ Course published successfully.")
    elif result == "zip_not_validated":
        await callback.answer(
            "ZIP validation required.",
            show_alert=True,
        )
    else:
        await callback.answer(
            "Draft unavailable or already processed.",
            show_alert=True,
        )


@dp.message()
async def fallback(message: Message) -> None:
    await message.answer(
        "Use the menu or /help to explore CourseVault.",
        reply_markup=main_menu(),
    )


async def main() -> None:
    global pool

    pool = await database.create_pool(settings.database_url)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    try:
        logger.info("Starting CourseVault")
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("CourseVault stopped due to an unexpected error")
        raise
    finally:
        await bot.session.close()
        if pool is not None:
            await database.close_pool(pool)
            pool = None


if __name__ == "__main__":
    asyncio.run(main())
