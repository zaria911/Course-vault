import asyncio
import logging
import os
import uuid
from decimal import Decimal, InvalidOperation
from html import escape

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

import database


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("coursevault")


class Settings(BaseSettings):
    bot_token: str
    owner_telegram_id: int
    database_url: str
    app_env: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


class CourseDraft(StatesGroup):
    title = State()
    category = State()
    price = State()
    description = State()
    impact = State()
    prerequisites = State()
    zip_file = State()


settings = Settings()
dp = Dispatcher()
pool: asyncpg.Pool | None = None


def db() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("Database pool is not initialized")
    return pool


def is_owner(user_id: int) -> bool:
    return user_id == settings.owner_telegram_id


def money(price_paise: int) -> str:
    return f"₹{price_paise / 100:,.2f}"


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
                    text="🔗 Share with Friends",
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
            "No published courses found here yet.",
            reply_markup=main_menu(),
        )
        return

    for row in rows:
        text = (
            f"<b>{escape(row['title'])}</b>\n"
            f"Category: {escape(row['category'])}\n"
            f"Price: <b>{money(row['price_paise'])}</b>\n"
            f"Course ID: <code>{row['id']}</code>"
        )
        await message.answer(
            text,
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
        "Discover practical courses, explore learning paths, "
        "and keep your favourite courses in one place.\n\n"
        "Choose an option below to get started.",
        reply_markup=main_menu(),
    )


@dp.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "<b>CourseVault Help</b>\n\n"
        "/start — Main menu\n"
        "/courses — Browse published courses\n"
        "/categories — Browse categories\n"
        "/wishlist — View saved courses\n\n"
        "Owner commands:\n"
        "/addcourse — Create a course draft\n"
        "/drafts — List unpublished drafts\n"
        "/publish ID — Publish a reviewed draft\n"
    )


@dp.message(Command("courses"))
async def courses_command(message: Message) -> None:
    await show_courses(message)


@dp.message(Command("categories"))
async def categories_command(message: Message) -> None:
    categories = await database.list_categories(db())

    if not categories:
        await message.answer("No course categories are available yet.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                text=category,
                callback_data=f"category:{index}",
            )
        ]
        for index, category in enumerate(categories)
    ]

    # The selected category is resolved from the current database list
    # in the callback handler below.
    await message.answer(
        "Choose a category:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@dp.message(Command("wishlist"))
async def wishlist_command(message: Message) -> None:
    user = message.from_user
    if user is None:
        return

    rows = await database.list_wishlist(db(), user.id)

    if not rows:
        await message.answer("Your wishlist is empty. Save a course you like!")
        return

    for row in rows:
        await message.answer(
            f"<b>{escape(row['title'])}</b>\n"
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

    if not callback.message:
        return

    categories = await database.list_categories(db())
    if not categories:
        await callback.message.answer("No categories available yet.")
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
    await callback.message.answer("Choose a category:", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("category:"))
async def category_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.data or not callback.message:
        return

    try:
        index = int(callback.data.split(":", 1)[1])
    except ValueError:
        return

    categories = await database.list_categories(db())
    if index < 0 or index >= len(categories):
        await callback.message.answer(
            "That category has changed. Please open Categories again."
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
        f"{escape(course['impact'] or 'Course benefits will be added soon.')}\n\n"
        f"<b>Prerequisites</b>\n"
        f"{escape(course['prerequisites'] or 'No prerequisites specified.')}"
    )

    await callback.message.answer(
        text,
        reply_markup=course_keyboard(course_id),
    )


@dp.callback_query(F.data == "wishlist")
async def wishlist_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.message or not callback.from_user:
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
async def save_course_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.data:
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        return

    course = await database.get_course(db(), course_id)
    if course is None:
        if callback.message:
            await callback.message.answer("This course is unavailable.")
        return

    await database.add_to_wishlist(db(), callback.from_user.id, course_id)

    if callback.message:
        await callback.message.answer("❤️ Saved to your wishlist.")


@dp.callback_query(F.data.startswith("share:"))
async def share_course_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if not callback.data or not callback.message:
        return

    try:
        course_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        return

    course = await database.get_course(db(), course_id)
    if course is None:
        await callback.message.answer("This course is unavailable.")
        return

    bot_info = await callback.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=course_{course_id}"

    await callback.message.answer(
        f"Share <b>{escape(course['title'])}</b> with a friend:\n\n"
        f"{link}"
    )


@dp.callback_query(F.data.startswith("buy:"))
async def buy_course_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    # Do not create a fake checkout or grant course access.
    # The FamGateway adapter will replace this placeholder only after
    # official checkout and server-side verification are implemented.
    if callback.message:
        await callback.message.answer(
            "🛒 Payment checkout is being configured. "
            "No payment has been taken, and no course access has been granted."
        )


@dp.callback_query(F.data == "ai_info")
async def ai_info_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "🤖 The CourseVault AI Tutor will be available after the "
            "AI provider gateway and usage limits are configured."
        )


@dp.callback_query(F.data.startswith("ai_course:"))
async def ai_course_callback(callback: CallbackQuery) -> None:
    await callback.answer()

    if callback.message:
        await callback.message.answer(
            "The course-specific AI tutor is not enabled yet. "
            "It will be connected after course-content indexing and "
            "AI usage controls are implemented."
        )


@dp.message(Command("addcourse"))
async def add_course_command(
    message: Message,
    state: FSMContext,
) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    await state.clear()
    await state.set_state(CourseDraft.title)
    await message.answer("Enter the course title.")


@dp.message(CourseDraft.title)
async def draft_title(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 160:
        await message.answer("Enter a title between 1 and 160 characters.")
        return

    await state.update_data(title=value)
    await state.set_state(CourseDraft.category)
    await message.answer("Enter the course category.")


@dp.message(CourseDraft.category)
async def draft_category(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 80:
        await message.answer("Enter a category between 1 and 80 characters.")
        return

    await state.update_data(category=value)
    await state.set_state(CourseDraft.price)
    await message.answer("Enter the price in INR, for example: 199 or 199.50")


@dp.message(CourseDraft.price)
async def draft_price(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()

    try:
        price = Decimal(raw)
        if not price.is_finite() or price < 0 or price > Decimal("1000000"):
            raise InvalidOperation
        paise = int(price * 100)
        if price * 100 != paise:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        await message.answer("Enter a valid INR price with up to two decimals.")
        return

    await state.update_data(price_paise=paise)
    await state.set_state(CourseDraft.description)
    await message.answer("Enter the course description.")


@dp.message(CourseDraft.description)
async def draft_description(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not value or len(value) > 3500:
        await message.answer("Enter a description of up to 3,500 characters.")
        return

    await state.update_data(description=value)
    await state.set_state(CourseDraft.impact)
    await message.answer(
        "Describe the realistic benefits of this course for learners."
    )


@dp.message(CourseDraft.impact)
async def draft_impact(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 2000:
        await message.answer("Keep this section under 2,000 characters.")
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
    await message.answer("Now upload the course as a ZIP document.")


@dp.message(CourseDraft.prerequisites)
async def draft_prerequisites(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 1500:
        await message.answer("Keep prerequisites under 1,500 characters.")
        return

    await state.update_data(prerequisites=value)
    await state.set_state(CourseDraft.zip_file)
    await message.answer("Now upload the course as a ZIP document.")


@dp.message(CourseDraft.zip_file, F.document)
async def draft_zip(message: Message, state: FSMContext) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await state.clear()
        await message.answer("⛔ Not authorized.")
        return

    document = message.document
    filename = (document.file_name or "").strip()

    # Conservative provisional limit; verify the actual Bot API and
    # delivery limits before enabling public course sales.
    max_bytes = 45 * 1024 * 1024

    if not filename.lower().endswith(".zip"):
        await message.answer("Please upload a .zip archive.")
        return

    if document.file_size is None or document.file_size > max_bytes:
        await message.answer(
            "This archive is too large for the current provisional limit. "
            "Please provide a smaller ZIP."
        )
        return

    data = await state.get_data()

    course_id = await db().fetchval(
        """
        INSERT INTO courses (
            title, category, price_paise, description,
            impact, prerequisites, zip_file_id, status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'draft')
        RETURNING id
        """,
        data["title"],
        data["category"],
        data["price_paise"],
        data["description"],
        data["impact"],
        data["prerequisites"],
        document.file_id,
    )

    await state.clear()

    await message.answer(
        f"✅ Course draft created!\n\n"
        f"ID: <code>{course_id}</code>\n"
        f"Title: <b>{escape(data['title'])}</b>\n"
        f"Price: {money(data['price_paise'])}\n"
        f"Status: <b>Draft — not visible to students</b>\n\n"
        f"Inspect and validate the ZIP before publishing. "
        f"To publish after review, use /publish {course_id}."
    )


@dp.message(CourseDraft.zip_file)
async def draft_zip_invalid(message: Message) -> None:
    await message.answer("Please send the course as a ZIP document.")


@dp.message(Command("drafts"))
async def drafts_command(message: Message) -> None:
    if message.from_user is None or not is_owner(message.from_user.id):
        await message.answer("⛔ Not authorized.")
        return

    rows = await db().fetch(
        """
        SELECT id, title, category, price_paise
        FROM courses
        WHERE status = 'draft'
        ORDER BY created_at DESC
        LIMIT 50
        """
    )

    if not rows:
        await message.answer("There are no course drafts.")
        return

    for row in rows:
        await message.answer(
            f"ID: <code>{row['id']}</code>\n"
            f"<b>{escape(row['title'])}</b>\n"
            f"{escape(row['category'])} · {money(row['price_paise'])}"
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

    # This basic publication command is owner-only. Full ZIP safety
    # inspection and audit logging must be implemented before production.
    result = await db().execute(
        """
        UPDATE courses
        SET status = 'published'
        WHERE id = $1 AND status = 'draft'
        """,
        course_id,
    )

    if result.endswith(" 1"):
        await message.answer(
            f"Course {course_id} is published. "
            "Only publish content you have rights to distribute and have reviewed."
        )
    else:
        await message.answer(
            "No draft was published. Check the course ID and current status."
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
        logger.info("Starting CourseVault bot")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
