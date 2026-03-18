# bot.py loading...import asyncio
import logging
import re
import sqlite3
from datetime import datetime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.enums import ParseMode

import os

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Database ────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("orders.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id    TEXT PRIMARY KEY,
            raw_text    TEXT,
            status      TEXT DEFAULT 'new',
            created_at  TEXT,
            updated_at  TEXT,
            notes       TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_order(order_id: str, raw_text: str):
    conn = sqlite3.connect("orders.db")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    conn.execute("""
        INSERT OR REPLACE INTO orders (order_id, raw_text, status, created_at, updated_at)
        VALUES (?, ?, 'new', ?, ?)
    """, (order_id, raw_text, now, now))
    conn.commit()
    conn.close()

def update_status(order_id: str, status: str):
    conn = sqlite3.connect("orders.db")
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    conn.execute("""
        UPDATE orders SET status=?, updated_at=? WHERE order_id=?
    """, (status, now, order_id))
    conn.commit()
    conn.close()

def get_order(order_id: str):
    conn = sqlite3.connect("orders.db")
    row = conn.execute(
        "SELECT * FROM orders WHERE order_id=?", (order_id,)
    ).fetchone()
    conn.close()
    return row

def get_all_orders(status_filter: str = None):
    conn = sqlite3.connect("orders.db")
    if status_filter:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status=? ORDER BY created_at DESC", (status_filter,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return rows

# ─── Parser ──────────────────────────────────────────────────────────────────

def parse_order(text: str) -> dict:
    """Parse raw order text into structured dict."""
    data = {
        "order_id": None,
        "status_text": None,
        "time": None,
        "description": None,
        "client_name": None,
        "address": None,
        "phone": None,
        "price": None,
        "date": None,
        "notes": [],
        "raw": text,
    }

    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # Order ID — alphanumeric code like BA0903119, LA1603243, FB1203093
    for line in lines:
        m = re.search(r'\b([A-Z]{2}\d{7,})\b', line)
        if m:
            data["order_id"] = m.group(1)
            break

    # Time — e.g. "10 AM", "10am", "2 pm", "3:30-4PM", "5-6pm"
    for line in lines:
        m = re.search(r'\b(\d{1,2}(?::\d{2})?(?:\s*[-–]\s*\d{1,2}(?::\d{2})?)?\s*[APap][Mm])\b', line)
        if m:
            data["time"] = m.group(1).upper().replace(" ", "")
            break

    # Status keywords
    status_keywords = ["Оформлен", "Нужно подтверждение", "Выполнен", "Отменён", "WEB", "TVMM", "CALL"]
    for line in lines:
        for kw in status_keywords:
            if kw.lower() in line.lower():
                data["status_text"] = line
                break
        if data["status_text"]:
            break

    # Phone — 10-digit US number, possibly with formatting
    for line in lines:
        m = re.search(r'[\+1\s\-\(]*(\d{3})[\s\-\)]*(\d{3})[\s\-]*(\d{4})', line)
        if m:
            data["phone"] = f"{m.group(1)}{m.group(2)}{m.group(3)}"
            break

    # Price — $XXX
    for line in lines:
        m = re.search(r'\$\s*(\d+)', line)
        if m:
            data["price"] = f"${m.group(1)}"
            break

    # Date — dd.mm.yyyy
    for line in lines:
        m = re.search(r'(\d{2}\.\d{2}\.\d{4})', line)
        if m:
            data["date"] = m.group(1)
            break

    # Address — line with "St,", "Ave,", "Rd,", "Dr,", "Ln,", "Blvd", "CA", "USA"
    for line in lines:
        if re.search(r'\b(St|Ave|Rd|Dr|Ln|Blvd|Way|Ct|Pl)\b.*CA', line, re.IGNORECASE):
            data["address"] = line
            break

    # TV description — lines with inch sizes like 55", 65", 75", 85", 98"
    for line in lines:
        if re.search(r"\d+['\"x]|inch|\bTV\b|\bmount\b|tilt|fixed|fireplace|drywall", line, re.IGNORECASE):
            if data["order_id"] and data["order_id"] not in line:
                data["description"] = line
                break

    # Client name — line after order_id that isn't status/address/price
    found_id = False
    for line in lines:
        if data["order_id"] and data["order_id"] in line:
            found_id = True
            continue
        if found_id:
            # skip if it's a status keyword line
            if any(kw.lower() in line.lower() for kw in status_keywords):
                continue
            # skip if it has digits that look like phone/price/address
            if re.search(r'\$|\d{5}|CA \d|📱', line):
                continue
            # skip if it's description
            if re.search(r"['\"x]|inch|tilt|mount|drywall", line, re.IGNORECASE):
                continue
            if len(line) > 2 and not re.match(r'^\d', line):
                data["client_name"] = line.title()
                break

    return data


def format_card(data: dict, status: str = "new") -> str:
    """Format parsed order into a Telegram HTML message."""
    status_emoji = {
        "new":       "🆕",
        "working":   "🔄",
        "done":      "✅",
        "cancelled": "❌",
        "postponed": "⏰",
        "failed":    "🚫",
    }
    status_label = {
        "new":       "Новый",
        "working":   "В процессе",
        "done":      "Выполнен",
        "cancelled": "Отменён",
        "postponed": "Перенесён",
        "failed":    "Не выполнен",
    }

    lines = []
    lines.append(f"{status_emoji.get(status, '🆕')} <b>Заказ</b> — статус: <b>{status_label.get(status, 'Новый')}</b>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if data.get("time"):
        lines.append(f"🕐 <b>{data['time']}</b>")
    if data.get("date"):
        lines.append(f"📅 {data['date']}")

    lines.append("")

    if data.get("description"):
        lines.append(f"📺 {data['description']}")
    if data.get("client_name"):
        lines.append(f"👤 {data['client_name']}")
    if data.get("address"):
        lines.append(f"📍 {data['address']}")
    if data.get("price"):
        lines.append(f"💰 <b>{data['price']}</b>")

    if data.get("status_text"):
        lines.append(f"📋 {data['status_text']}")

    lines.append("")
    lines.append("──────────────────────")

    # Copyable fields
    if data.get("order_id"):
        lines.append(f"🔢 <code>{data['order_id']}</code>")
    if data.get("phone"):
        lines.append(f"📱 <code>{data['phone']}</code>")

    lines.append("──────────────────────")

    return "\n".join(lines)


def make_keyboard(order_id: str, has_phone: bool = False, has_address: bool = False) -> InlineKeyboardMarkup:
    """Build inline keyboard for an order."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Выполнен",   callback_data=f"status:{order_id}:done"),
            InlineKeyboardButton(text="🔄 В процессе", callback_data=f"status:{order_id}:working"),
        ],
        [
            InlineKeyboardButton(text="❌ Отменён",    callback_data=f"status:{order_id}:cancelled"),
            InlineKeyboardButton(text="⏰ Перенесён",  callback_data=f"status:{order_id}:postponed"),
        ],
        [
            InlineKeyboardButton(text="🚫 Не выполнен", callback_data=f"status:{order_id}:failed"),
        ],
    ]

    # Add map / call buttons if data available
    action_row = []
    if has_address:
        action_row.append(
            InlineKeyboardButton(text="🗺 Карта", callback_data=f"map:{order_id}")
        )
    if has_phone:
        action_row.append(
            InlineKeyboardButton(text="📞 Позвонить", callback_data=f"call:{order_id}")
        )
    if action_row:
        buttons.append(action_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ─── Bot setup ───────────────────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

# In-memory cache: order_id -> parsed data (for map/call lookups)
order_cache: dict[str, dict] = {}


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет, Ильяс!</b>\n\n"
        "Я помогаю управлять заказами на установку ТВ.\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Скопируй текст заказа из Telegram-чата\n"
        "• Вставь сюда — я покажу карточку с кнопками\n\n"
        "<b>Команды:</b>\n"
        "/today — заказы, добавленные сегодня\n"
        "/history — все заказы\n"
        "/done — выполненные заказы\n"
        "/help — помощь"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Помощь</b>\n\n"
        "<b>Как добавить заказ:</b>\n"
        "Просто скопируй текст заказа из чата компании и отправь мне. "
        "Я автоматически распознаю номер, адрес, телефон и цену.\n\n"
        "<b>Кнопки на карточке:</b>\n"
        "✅ Выполнен — работа сделана\n"
        "🔄 В процессе — едешь / работаешь\n"
        "❌ Отменён — клиент отменил\n"
        "⏰ Перенесён — перенесли на другой день\n"
        "🚫 Не выполнен — не получилось\n"
        "🗺 Карта — открыть Google Maps\n"
        "📞 Позвонить — номер клиента\n\n"
        "<b>Команды:</b>\n"
        "/today — заказы сегодня\n"
        "/history — история всех заказов\n"
        "/done — выполненные"
    )


@dp.message(Command("today"))
async def cmd_today(message: Message):
    today = datetime.now().strftime("%d.%m.%Y")
    rows = get_all_orders()
    today_rows = [r for r in rows if r[3] and today in r[3]]  # created_at contains today

    if not today_rows:
        await message.answer("📭 Сегодня заказов пока нет.\n\nДобавь заказ — скопируй текст из чата и отправь мне!")
        return

    await message.answer(f"📋 <b>Заказы на сегодня ({len(today_rows)}):</b>")
    for row in today_rows:
        order_id, raw_text, status, created_at, updated_at, notes = row
        if order_id in order_cache:
            data = order_cache[order_id]
        else:
            data = parse_order(raw_text)
            order_cache[order_id] = data

        text = format_card(data, status)
        kb = make_keyboard(order_id, bool(data.get("phone")), bool(data.get("address")))
        await message.answer(text, reply_markup=kb)


@dp.message(Command("history"))
async def cmd_history(message: Message):
    rows = get_all_orders()
    if not rows:
        await message.answer("📭 История пуста. Добавь первый заказ!")
        return

    status_emoji = {"new": "🆕", "working": "🔄", "done": "✅", "cancelled": "❌", "postponed": "⏰", "failed": "🚫"}
    status_label = {"new": "Новый", "working": "В процессе", "done": "Выполнен", "cancelled": "Отменён", "postponed": "Перенесён", "failed": "Не выполнен"}

    lines = [f"📋 <b>Все заказы ({len(rows)}):</b>\n"]
    for row in rows:
        order_id, raw_text, status, created_at, *_ = row
        data = parse_order(raw_text)
        em = status_emoji.get(status, "🆕")
        sl = status_label.get(status, status)
        time_str = data.get("time") or ""
        price_str = data.get("price") or ""
        name_str = data.get("client_name") or ""
        lines.append(f"{em} <code>{order_id}</code>  {time_str}  {price_str}  {name_str}\n   └ {sl} · {created_at}")

    await message.answer("\n".join(lines))


@dp.message(Command("done"))
async def cmd_done(message: Message):
    rows = get_all_orders("done")
    if not rows:
        await message.answer("Выполненных заказов пока нет.")
        return
    total = sum(
        int(re.search(r'\$(\d+)', r[1]).group(1)) if re.search(r'\$(\d+)', r[1]) else 0
        for r in rows
    )
    lines = [f"✅ <b>Выполнено: {len(rows)} заказов</b>  💰 Итого: <b>${total}</b>\n"]
    for row in rows:
        order_id, raw_text, status, created_at, updated_at, *_ = row
        data = parse_order(raw_text)
        price = data.get("price") or ""
        name = data.get("client_name") or ""
        lines.append(f"• <code>{order_id}</code>  {price}  {name}  · {updated_at}")
    await message.answer("\n".join(lines))


@dp.message(F.text)
async def handle_order_text(message: Message):
    text = message.text.strip()

    # Must look like an order — contain an order code
    order_id_match = re.search(r'\b([A-Z]{2}\d{7,})\b', text)
    if not order_id_match:
        await message.answer(
            "🤔 Не похоже на заказ — не нашёл номер заказа (например, BA0903119).\n\n"
            "Скопируй текст заказа целиком из чата компании и отправь мне.\n"
            "Напиши /help для помощи."
        )
        return

    order_id = order_id_match.group(1)
    data = parse_order(text)
    order_cache[order_id] = data
    save_order(order_id, text)

    card_text = format_card(data, "new")
    kb = make_keyboard(order_id, bool(data.get("phone")), bool(data.get("address")))
    await message.answer(card_text, reply_markup=kb)


@dp.callback_query(F.data.startswith("status:"))
async def handle_status(callback: CallbackQuery):
    _, order_id, new_status = callback.data.split(":")
    update_status(order_id, new_status)

    row = get_order(order_id)
    if not row:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    _, raw_text, status, *_ = row
    if order_id in order_cache:
        data = order_cache[order_id]
    else:
        data = parse_order(raw_text)
        order_cache[order_id] = data

    card_text = format_card(data, new_status)
    kb = make_keyboard(order_id, bool(data.get("phone")), bool(data.get("address")))

    status_label = {
        "done": "✅ Выполнен",
        "working": "🔄 В процессе",
        "cancelled": "❌ Отменён",
        "postponed": "⏰ Перенесён",
        "failed": "🚫 Не выполнен",
    }
    await callback.answer(f"Статус обновлён: {status_label.get(new_status, new_status)}")
    await callback.message.edit_text(card_text, reply_markup=kb)


@dp.callback_query(F.data.startswith("map:"))
async def handle_map(callback: CallbackQuery):
    order_id = callback.data.split(":")[1]
    data = order_cache.get(order_id)
    if not data:
        row = get_order(order_id)
        if row:
            data = parse_order(row[1])
            order_cache[order_id] = data

    if not data or not data.get("address"):
        await callback.answer("Адрес не найден", show_alert=True)
        return

    address = data["address"]
    maps_url = f"https://www.google.com/maps/search/?api=1&query={address.replace(' ', '+')}"
    await callback.answer()
    await callback.message.answer(
        f"🗺 <b>Адрес:</b>\n{address}\n\n"
        f'<a href="{maps_url}">📍 Открыть в Google Maps</a>'
    )


@dp.callback_query(F.data.startswith("call:"))
async def handle_call(callback: CallbackQuery):
    order_id = callback.data.split(":")[1]
    data = order_cache.get(order_id)
    if not data:
        row = get_order(order_id)
        if row:
            data = parse_order(row[1])
            order_cache[order_id] = data

    if not data or not data.get("phone"):
        await callback.answer("Телефон не найден", show_alert=True)
        return

    phone = data["phone"]
    formatted = f"+1 ({phone[:3]}) {phone[3:6]}-{phone[6:]}"
    await callback.answer()
    await callback.message.answer(
        f"📞 <b>Клиент:</b>\n"
        f"Номер: <code>{phone}</code>\n\n"
        f'<a href="tel:+1{phone}">📲 Позвонить {formatted}</a>'
    )


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    init_db()
    logger.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
