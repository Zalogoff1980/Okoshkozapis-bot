"""Telegram-бот для записи клиентов к мастеру."""
import asyncio
import html
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import config

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "bookings.db")
TZ = ZoneInfo(config.TIMEZONE)
FMT = "%Y-%m-%d %H:%M"
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

router = Router()
MASTER_ID = int(os.getenv("MASTER_ID", "0") or 0)


# ---------- База данных ----------

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                service TEXT NOT NULL,
                start TEXT NOT NULL,
                duration INTEGER NOT NULL,
                price INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                reminded_day INTEGER NOT NULL DEFAULT 0,
                reminded_2h INTEGER NOT NULL DEFAULT 0
            )"""
        )


def now_local() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def parse_hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def pretty_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month - 1]}"


def pretty_dt(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} ({WEEKDAYS[dt.weekday()]}) в {dt:%H:%M}"


def busy_ranges(day: date) -> list[tuple[datetime, datetime]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT start, duration FROM bookings WHERE status='active' AND start LIKE ?",
            (f"{day.isoformat()}%",),
        ).fetchall()
    result = []
    for r in rows:
        s = datetime.strptime(r["start"], FMT)
        result.append((s, s + timedelta(minutes=r["duration"])))
    return result


def free_slots(day: date, duration: int) -> list[str]:
    """Список свободного времени на день для услуги нужной длительности."""
    sh, sm = parse_hm(config.WORK_START)
    eh, em = parse_hm(config.WORK_END)
    t = datetime.combine(day, datetime.min.time()).replace(hour=sh, minute=sm)
    end = t.replace(hour=eh, minute=em)
    now = now_local()
    busy = busy_ranges(day)
    slots = []
    while t + timedelta(minutes=duration) <= end:
        t_end = t + timedelta(minutes=duration)
        in_future = t > now + timedelta(minutes=30)
        no_overlap = all(t_end <= bs or t >= be for bs, be in busy)
        if in_future and no_overlap:
            slots.append(t.strftime("%H:%M"))
        t += timedelta(minutes=config.SLOT_STEP)
    return slots


def available_days(duration: int) -> list[date]:
    today = now_local().date()
    days = []
    for i in range(config.DAYS_AHEAD + 1):
        d = today + timedelta(days=i)
        if d.weekday() in config.DAYS_OFF:
            continue
        if free_slots(d, duration):
            days.append(d)
    return days


# ---------- Клавиатуры ----------

def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data="menu|book")
    kb.button(text="📋 Мои записи", callback_data="menu|my")
    kb.button(text="💰 Услуги и цены", callback_data="menu|prices")
    kb.adjust(1)
    return kb.as_markup()


def back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="⬅️ В меню", callback_data="menu|home")


async def show(call: CallbackQuery, text: str, markup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        await call.message.answer(text, reply_markup=markup)


def welcome_text() -> str:
    return (f"Здравствуйте! Это онлайн-запись «{html.escape(config.SALON_NAME)}».\n"
            f"Выберите, что нужно:")


# ---------- Клиент ----------

@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(welcome_text(), reply_markup=main_menu())


@router.callback_query(F.data == "menu|home")
async def menu_home(call: CallbackQuery) -> None:
    await show(call, welcome_text(), main_menu())
    await call.answer()


@router.callback_query(F.data == "menu|prices")
async def menu_prices(call: CallbackQuery) -> None:
    lines = ["<b>Услуги и цены</b>\n"]
    for name, minutes, price in config.SERVICES:
        lines.append(f"• {html.escape(name)} — {price} ₽ ({minutes} мин)")
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 Записаться", callback_data="menu|book")
    kb.add(back_button())
    kb.adjust(1)
    await show(call, "\n".join(lines), kb.as_markup())
    await call.answer()


@router.callback_query(F.data == "menu|book")
async def menu_book(call: CallbackQuery) -> None:
    kb = InlineKeyboardBuilder()
    for i, (name, minutes, price) in enumerate(config.SERVICES):
        kb.button(text=f"{name} — {price} ₽", callback_data=f"svc|{i}")
    kb.add(back_button())
    kb.adjust(1)
    await show(call, "Выберите услугу:", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("svc|"))
async def choose_service(call: CallbackQuery) -> None:
    i = int(call.data.split("|")[1])
    name, minutes, price = config.SERVICES[i]
    days = available_days(minutes)
    kb = InlineKeyboardBuilder()
    if not days:
        kb.button(text="⬅️ Назад", callback_data="menu|book")
        await show(call, "К сожалению, свободных дней пока нет. Загляните позже.", kb.as_markup())
        await call.answer()
        return
    for d in days:
        kb.button(text=pretty_day(d), callback_data=f"day|{i}|{d.isoformat()}")
    kb.button(text="⬅️ Назад", callback_data="menu|book")
    kb.adjust(2)
    await show(call, f"<b>{html.escape(name)}</b>\nВыберите день:", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("day|"))
async def choose_day(call: CallbackQuery) -> None:
    _, i, day_str = call.data.split("|")
    i = int(i)
    name, minutes, price = config.SERVICES[i]
    day = date.fromisoformat(day_str)
    slots = free_slots(day, minutes)
    kb = InlineKeyboardBuilder()
    if not slots:
        kb.button(text="⬅️ Выбрать другой день", callback_data=f"svc|{i}")
        await show(call, "На этот день время уже занято. Выберите другой день.", kb.as_markup())
        await call.answer()
        return
    for s in slots:
        kb.button(text=s, callback_data=f"time|{i}|{day_str}|{s}")
    kb.button(text="⬅️ Другой день", callback_data=f"svc|{i}")
    sizes = [4] * (len(slots) // 4) + ([len(slots) % 4] if len(slots) % 4 else []) + [1]
    kb.adjust(*sizes)
    await show(call, f"<b>{html.escape(name)}</b>, {pretty_day(day)}\nВыберите время:", kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("time|"))
async def choose_time(call: CallbackQuery, bot: Bot) -> None:
    _, i, day_str, hm = call.data.split("|")
    i = int(i)
    name, minutes, price = config.SERVICES[i]
    day = date.fromisoformat(day_str)

    # Проверяем ещё раз: вдруг время только что заняли
    if hm not in free_slots(day, minutes):
        kb = InlineKeyboardBuilder()
        kb.button(text="Выбрать другое время", callback_data=f"day|{i}|{day_str}")
        await show(call, "Это время только что заняли. Выберите другое.", kb.as_markup())
        await call.answer()
        return

    start = datetime.strptime(f"{day_str} {hm}", FMT)
    delta = start - now_local()
    user = call.from_user
    user_name = user.full_name
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO bookings
               (user_id, user_name, service, start, duration, price, reminded_day, reminded_2h)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user.id, user_name, name, start.strftime(FMT), minutes, price,
             1 if delta <= timedelta(hours=24) else 0,
             1 if delta <= timedelta(hours=2) else 0),
        )
        booking_id = cur.lastrowid

    kb = InlineKeyboardBuilder()
    kb.button(text="📋 Мои записи", callback_data="menu|my")
    kb.add(back_button())
    kb.adjust(1)
    await show(
        call,
        f"✅ Вы записаны!\n\n<b>{html.escape(name)}</b>\n{pretty_dt(start)}\nСтоимость: {price} ₽\n\n"
        f"Накануне пришлю напоминание.",
        kb.as_markup(),
    )
    await call.answer()

    if MASTER_ID:
        mention = f'<a href="tg://user?id={user.id}">{html.escape(user_name)}</a>'
        try:
            await bot.send_message(
                MASTER_ID,
                f"🔔 Новая запись #{booking_id}\n{mention}\n"
                f"{html.escape(name)}\n{pretty_dt(start)}",
            )
        except Exception:
            logging.exception("Не удалось уведомить мастера")


@router.callback_query(F.data == "menu|my")
async def menu_my(call: CallbackQuery) -> None:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE user_id=? AND status='active' AND start>=? ORDER BY start",
            (call.from_user.id, now_local().strftime(FMT)),
        ).fetchall()
    kb = InlineKeyboardBuilder()
    if not rows:
        text = "У вас пока нет записей."
        kb.button(text="📅 Записаться", callback_data="menu|book")
    else:
        lines = ["<b>Ваши записи</b>\n"]
        for r in rows:
            start = datetime.strptime(r["start"], FMT)
            lines.append(f"• {html.escape(r['service'])}, {pretty_dt(start)}")
            kb.button(text=f"❌ Отменить: {start:%d.%m %H:%M}", callback_data=f"cancel|{r['id']}")
        text = "\n".join(lines)
    kb.add(back_button())
    kb.adjust(1)
    await show(call, text, kb.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("cancel|"))
async def cancel_booking(call: CallbackQuery, bot: Bot) -> None:
    booking_id = int(call.data.split("|")[1])
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM bookings WHERE id=? AND user_id=? AND status='active'",
            (booking_id, call.from_user.id),
        ).fetchone()
        if row:
            conn.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
    if not row:
        await call.answer("Запись уже отменена", show_alert=True)
        return
    await call.answer("Запись отменена")
    await menu_my(call)
    if MASTER_ID:
        start = datetime.strptime(row["start"], FMT)
        mention = f'<a href="tg://user?id={row["user_id"]}">{html.escape(row["user_name"])}</a>'
        try:
            await bot.send_message(
                MASTER_ID,
                f"❌ Отмена записи #{booking_id}\n{mention}\n"
                f"{html.escape(row['service'])}\n{pretty_dt(start)}",
            )
        except Exception:
            logging.exception("Не удалось уведомить мастера об отмене")


# ---------- Мастер ----------

def is_master(message: Message) -> bool:
    return MASTER_ID != 0 and message.from_user.id == MASTER_ID


def format_schedule(rows, title: str) -> str:
    if not rows:
        return f"{title}\n\nЗаписей нет."
    lines = [f"<b>{title}</b>\n"]
    for r in rows:
        start = datetime.strptime(r["start"], FMT)
        mention = f'<a href="tg://user?id={r["user_id"]}">{html.escape(r["user_name"])}</a>'
        lines.append(f"{start:%d.%m %H:%M} — {html.escape(r['service'])}, {mention}")
    return "\n".join(lines)


async def send_schedule(message: Message, days: int, title: str) -> None:
    today = now_local().date()
    first = today.isoformat()
    last = (today + timedelta(days=days)).isoformat()
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM bookings WHERE status='active' AND start>=? AND start<? ORDER BY start",
            (f"{first} 00:00", f"{last} 00:00"),
        ).fetchall()
    await message.answer(format_schedule(rows, title))


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if is_master(message):
        await send_schedule(message, 1, "Записи на сегодня")


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    if is_master(message):
        await send_schedule(message, 7, "Записи на 7 дней")


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: <code>{message.from_user.id}</code>")


# ---------- Напоминания ----------

async def reminder_loop(bot: Bot) -> None:
    while True:
        try:
            now = now_local()
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM bookings WHERE status='active' AND start>? "
                    "AND (reminded_day=0 OR reminded_2h=0)",
                    (now.strftime(FMT),),
                ).fetchall()
                for r in rows:
                    start = datetime.strptime(r["start"], FMT)
                    left = start - now
                    if left <= timedelta(hours=2) and not r["reminded_2h"]:
                        text = f"⏰ Напоминаю: сегодня в {start:%H:%M} вас ждут — {html.escape(r['service'])}."
                        flag = "reminded_2h"
                        also_day = True
                    elif left <= timedelta(hours=24) and not r["reminded_day"]:
                        text = f"🔔 Напоминаю: завтра, {pretty_dt(start)} — {html.escape(r['service'])}."
                        flag = "reminded_day"
                        also_day = False
                    else:
                        continue
                    try:
                        await bot.send_message(r["user_id"], text)
                    except Exception:
                        logging.warning("Не удалось отправить напоминание %s", r["id"])
                    conn.execute(f"UPDATE bookings SET {flag}=1 WHERE id=?", (r["id"],))
                    if also_day:
                        conn.execute("UPDATE bookings SET reminded_day=1 WHERE id=?", (r["id"],))
        except Exception:
            logging.exception("Ошибка в цикле напоминаний")
        await asyncio.sleep(60)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Не задан BOT_TOKEN. Смотрите README.md")
    init_db()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
