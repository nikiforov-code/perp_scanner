"""Telegram-бот по фандингам.

Данные бот берёт из собственного кэша рынка (market.py) — раньше он ходил за ними
по HTTP к сканеру и без запущенного сканера был мёртв.

Раскладка модулей:
    exchanges/      качалки бирж (общие со сканером)
    market.py       кэш рынка и цикл обновления
    formatting.py   форматирование строк сообщений
    digest.py       отбор монет и сборка сообщения
    notifier.py     обход пользователей и отправка дайджеста
    radar.py        разовые оповещения о жирных отрицательных фандингах Bybit
    user_settings.py настройки пользователей и SQLite
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Dict

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

import user_settings as store
from user_settings import UserSettings, sanitize, DEFAULT_UTC_OFFSET
from formatting import build_binance_price_map, split_html_chunks
from digest import select_items, build_message, total_count
from market import MARKET, okx_min_volume
from notifier import notify_users
import radar

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
# httpx логировал каждый запрос — в сутки это десятки тысяч строк ни о чём
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("funding-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found in .env")

POLL_SECONDS = 25       # как часто проверяем, кому пора слать дайджест
TG_MAX_LEN = 3800       # безопасный лимит длины Telegram-сообщения

# {user_id: UserSettings} — наполняется из базы при старте
USER_SETTINGS: Dict[int, UserSettings] = {}

# "ожидание ввода" (мини-FSM): "pos" | "neg" | "digest_before" | "vol" | "tz"
#                              | "radar_rate" | "radar_vol"
WAITING_INPUT: Dict[int, str] = {}

# {user_id: hour_key} — какой час уже отправлен, защита от дублей
SENT_HOURS: Dict[int, int] = {}

# {(user_id, symbol): ts} — когда радар последний раз показывал эту монету.
# Живёт и в памяти, и в базе: иначе деплой обнулял бы тишину.
RADAR_MARKS: Dict[tuple, int] = {}

bot = Bot(token=TOKEN)
dp = Dispatcher()


# =========================
# НАСТРОЙКИ ПОЛЬЗОВАТЕЛЯ
# =========================

def get_settings(user_id: int) -> UserSettings:
    if user_id in USER_SETTINGS:
        return USER_SETTINGS[user_id]

    loaded = store.db_load_user(user_id)
    if loaded is not None:
        USER_SETTINGS[user_id] = loaded
        return loaded

    s = sanitize(UserSettings())
    USER_SETTINGS[user_id] = s
    store.db_save_user(user_id, s)
    return s


def save_settings(user_id: int, s: UserSettings) -> None:
    sanitize(s)
    USER_SETTINGS[user_id] = s
    store.db_save_user(user_id, s)


def user_timezone(s: UserSettings) -> timezone:
    return timezone(timedelta(hours=int(s.utc_offset_hours)))


def offset_txt(s: UserSettings) -> str:
    offset = int(s.utc_offset_hours)
    return f"UTC {'+' if offset >= 0 else ''}{offset}"


# =========================
# КЛАВИАТУРЫ
# =========================

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🔥 ТОП Фандинги"), KeyboardButton(text="🔔 Фильтры")],
    ],
    resize_keyboard=True,
)


def filters_kb(user_id: int) -> ReplyKeyboardMarkup:
    s = get_settings(user_id)
    notify_btn = "✅ Уведомления ВКЛ" if s.notify_enabled else "⛔️ Уведомления ВЫКЛ"
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=f"Порог + (сейчас {s.pos_threshold:.2f}%)"),
                KeyboardButton(text=f"Порог - (сейчас {s.neg_threshold:.2f}%)"),
            ],
            [
                KeyboardButton(text=f"Дайджест за (сейчас {s.digest_before_hour_minutes} мин до часа)"),
                KeyboardButton(text=f"Объём ≥ (сейчас {int(s.vol_threshold_usdt):,} USDT)".replace(",", " ")),
            ],
            [
                KeyboardButton(text=f"📡 Радар ставка (сейчас {s.radar_rate:.2f}%)"),
                KeyboardButton(text=f"📡 Радар объём (сейчас {s.radar_vol / 1_000_000:g}M)"),
            ],
            [
                KeyboardButton(text=f"🕒 Таймзона (сейчас {offset_txt(s)})"),
                KeyboardButton(text=notify_btn),
            ],
            [
                KeyboardButton(text="⬅️ Назад"),
            ],
        ],
        resize_keyboard=True,
    )


# =========================
# ОТПРАВКА
# =========================

async def send_chunks(chat_id: int, text: str, reply_markup=None) -> None:
    """Шлёт длинный HTML-текст частями, не ломая разметку.

    Дайджест раньше уходил одним куском и на длинном списке отбивался ошибкой
    «text is too long» — за десять дней так потерялось 573 рассылки.
    """
    chunks = split_html_chunks(text, TG_MAX_LEN)
    for i, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup if i == len(chunks) - 1 else None,
        )


# =========================
# ХЕНДЛЕРЫ
# =========================

@dp.message(Command("start"))
async def start(message: Message):
    uid = message.from_user.id
    is_new = uid not in USER_SETTINGS and store.db_load_user(uid) is None
    get_settings(uid)
    WAITING_INPUT.pop(uid, None)

    if is_new:
        await message.answer(
            "Funding Spread Scanner Bot\n\n"
            f"Часовой пояс по умолчанию: UTC+{DEFAULT_UTC_OFFSET}. "
            "Поменять — кнопка «🕒 Таймзона» в разделе «🔔 Фильтры».\n\n"
            "Выбери действие:",
            reply_markup=MAIN_KB,
        )
        return

    await message.answer("Funding Spread Scanner Bot\n\nВыбери действие:", reply_markup=MAIN_KB)


@dp.message(Command("me"))
async def me(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)
    status = "ВКЛ" if s.notify_enabled else "ВЫКЛ"

    text = (
        "👤 *Твои текущие настройки*\n\n"
        f"🕒 Таймзона: {offset_txt(s)}\n"
        f"📈 Порог +: `{s.pos_threshold:.2f}%`\n"
        f"📉 Порог −: `{s.neg_threshold:.2f}%`\n"
        f"⏰ Дайджест за: `{s.digest_before_hour_minutes} мин`\n"
        f"💰 Объём ≥: `{s.vol_threshold_usdt / 1_000_000:.1f} млн USDT`\n"
        f"🔔 Уведомления: `{status}`\n\n"
        "📡 *Радар Bybit* (разовое оповещение о жирном минусе)\n"
        f"• ставка ниже: `{s.radar_rate:.2f}%`\n"
        f"• объём от: `{s.radar_vol / 1_000_000:g} млн USDT`"
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(lambda m: m.text == "🔥 ТОП Фандинги")
async def top_fundings(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)

    try:
        if not MARKET.is_ready():
            await message.answer(
                "Данные ещё загружаются, попробуй через минуту.",
                reply_markup=MAIN_KB,
            )
            return

        items = MARKET.items()
        now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        groups = select_items(items, s, now_ms)

        if total_count(groups) == 0:
            await message.answer(
                f"Нет фандингов по фильтру (≥{s.pos_threshold:.2f}% или ≤{s.neg_threshold:.2f}%) "
                "на выбранных биржах.",
                reply_markup=MAIN_KB,
            )
            return

        text = build_message(
            groups,
            s,
            "🔥 <b>ТОП фандинги</b>",
            datetime.now(tz=user_timezone(s)),
            user_timezone(s),
            build_binance_price_map(items),
        )
        await send_chunks(uid, text, reply_markup=MAIN_KB)

    except Exception as e:
        log.exception("top_fundings failed uid=%s", uid)
        await message.answer(f"Ошибка получения данных: {e}", reply_markup=MAIN_KB)


@dp.message(lambda m: m.text == "🔔 Фильтры")
async def filters_menu(message: Message):
    uid = message.from_user.id
    WAITING_INPUT.pop(uid, None)
    await message.answer("Настройки фильтров и уведомлений:", reply_markup=filters_kb(uid))


@dp.message(lambda m: m.text == "⬅️ Назад")
async def back_to_main(message: Message):
    uid = message.from_user.id
    WAITING_INPUT.pop(uid, None)
    await message.answer("Главное меню:", reply_markup=MAIN_KB)


@dp.message(lambda m: m.text and m.text.startswith("Порог +"))
async def ask_pos_threshold(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "pos"
    s = get_settings(uid)
    await message.answer(
        f"Введи новый порог для положительных (пример: 0.40)\nСейчас: {s.pos_threshold:.2f}%",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text and m.text.startswith("Порог -"))
async def ask_neg_threshold(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "neg"
    s = get_settings(uid)
    await message.answer(
        f"Введи новый порог для отрицательных (пример: -0.40)\nСейчас: {s.neg_threshold:.2f}%",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text and m.text.startswith("Дайджест за"))
async def ask_digest_before_hour(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "digest_before"
    s = get_settings(uid)
    await message.answer(
        "Введи за сколько минут до начала следующего часа присылать дайджест (пример: 30)\n"
        f"Сейчас: {s.digest_before_hour_minutes} мин",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text and m.text.startswith("Объём ≥"))
async def ask_volume_threshold(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "vol"
    s = get_settings(uid)
    await message.answer(
        "Введи минимальный объём 24h в миллионах USDT (пример: 5 = 5 000 000)\n"
        f"Сейчас: {s.vol_threshold_usdt / 1_000_000:g}M",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text and m.text.startswith("📡 Радар ставка"))
async def ask_radar_rate(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "radar_rate"
    s = get_settings(uid)
    await message.answer(
        "Радар ловит только отрицательные ставки и только на Bybit.\n"
        "Введи, ниже какой ставки монета интересна (пример: 1.5 или -1.5)\n"
        f"Сейчас: {s.radar_rate:.2f}%",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text and m.text.startswith("📡 Радар объём"))
async def ask_radar_vol(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "radar_vol"
    s = get_settings(uid)
    await message.answer(
        "Минимальный объём за сутки для радара, в миллионах USDT (пример: 10)\n"
        f"Сейчас: {s.radar_vol / 1_000_000:g}M",
        reply_markup=filters_kb(uid),
    )


@dp.message(lambda m: m.text in ("✅ Уведомления ВКЛ", "⛔️ Уведомления ВЫКЛ"))
async def toggle_notify(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)
    s.notify_enabled = not s.notify_enabled
    save_settings(uid, s)
    txt = "✅ Уведомления включены" if s.notify_enabled else "⛔️ Уведомления выключены"
    await message.answer(txt, reply_markup=filters_kb(uid))


@dp.message(lambda m: m.text and m.text.startswith("🕒 Таймзона"))
async def ask_timezone(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "tz"
    s = get_settings(uid)
    await message.answer(
        "⏱ Введи новый часовой пояс как UTC offset числом.\n"
        "Пример: -10, 0, 3, 12\n"
        "Диапазон: от -12 до +12.\n\n"
        f"Сейчас: {offset_txt(s)}\n"
        "Введи число одним сообщением:",
        reply_markup=filters_kb(uid),
    )


async def handle_ticker_query(message: Message, raw_text: str) -> bool:
    """Пользователь прислал тикер — отвечаем спредом цен Binance/Bybit."""
    if not re.fullmatch(r"[A-Za-z0-9]{2,20}", raw_text):
        return False

    data = MARKET.price_spread(raw_text)
    if not data:
        await message.answer(
            "Монета не найдена на Binance/Bybit или по ней ещё нет данных.",
            reply_markup=MAIN_KB,
        )
        return True

    sp = float(data["spread_pct"])
    lines = [
        f"📊 {data['symbol']}",
        f"Binance: {float(data['binance_price']):.5f}",
        f"Bybit: {float(data['bybit_price']):.5f}",
        f"Spread: {sp:+.3f}%",
        "",
    ]
    if sp < 0:
        lines.append("Bybit выше Binance")
    elif sp > 0:
        lines.append("Bybit ниже Binance")
    else:
        lines.append("Цены равны")

    await message.answer("\n".join(lines), reply_markup=MAIN_KB)
    return True


@dp.message()
async def any_text_handler(message: Message):
    uid = message.from_user.id
    mode = WAITING_INPUT.get(uid)

    if not mode:
        raw_text = (message.text or "").strip()
        if raw_text and await handle_ticker_query(message, raw_text):
            return
        await message.answer("Команда не распознана. Нажми кнопку в меню 👇", reply_markup=MAIN_KB)
        return

    raw = (message.text or "").strip().replace(",", ".")
    try:
        s = get_settings(uid)

        if mode == "tz":
            val = int(float(raw))  # чтобы "3.0" тоже принималось
            if val < -12 or val > 12:
                raise ValueError("utc_offset_hours должен быть от -12 до +12")
            s.utc_offset_hours = val
            save_settings(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer(
                f"Готово ✅ Таймзона установлена: {offset_txt(s)}",
                reply_markup=filters_kb(uid),
            )
            return

        if mode in ("pos", "neg"):
            val = float(raw)
            if abs(val) > 50:
                raise ValueError("слишком большое значение")
            if mode == "pos":
                s.pos_threshold = val
            else:
                s.neg_threshold = val
            save_settings(uid, s)  # sanitize приведёт знаки к нужным
            WAITING_INPUT.pop(uid, None)
            await message.answer(
                f"Готово ✅ Пороги: +{s.pos_threshold:.2f}% / {s.neg_threshold:.2f}%",
                reply_markup=filters_kb(uid),
            )
            return

        if mode == "digest_before":
            val = int(float(raw))
            if val < 1 or val > 59:
                raise ValueError("digest_before_hour_minutes должен быть 1..59")
            s.digest_before_hour_minutes = val
            save_settings(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer("Готово ✅", reply_markup=filters_kb(uid))
            return

        if mode == "radar_rate":
            val = float(raw)
            if abs(val) > 50 or val == 0:
                raise ValueError("ставка радара должна быть от 0 до 50")
            s.radar_rate = val
            save_settings(uid, s)  # sanitize приведёт знак к отрицательному
            WAITING_INPUT.pop(uid, None)
            await message.answer(
                f"Готово ✅ Радар ловит ставки ниже {s.radar_rate:.2f}%",
                reply_markup=filters_kb(uid),
            )
            return

        if mode == "radar_vol":
            val_m = float(raw)
            if val_m <= 0 or val_m > 100000:
                raise ValueError("объём радара должен быть >0 (в миллионах USDT)")
            s.radar_vol = val_m * 1_000_000
            save_settings(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer(
                f"Готово ✅ Радар смотрит монеты с объёмом от {s.radar_vol / 1_000_000:g}M",
                reply_markup=filters_kb(uid),
            )
            return

        if mode == "vol":
            val_m = float(raw)
            if val_m <= 0 or val_m > 100000:
                raise ValueError("Объём должен быть >0 (в миллионах USDT)")
            s.vol_threshold_usdt = val_m * 1_000_000
            save_settings(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer("Готово ✅", reply_markup=filters_kb(uid))
            return

    except Exception:
        await message.answer(
            "Не понял значение. Введи число (пример: 0.40 или -0.40 или 30).",
            reply_markup=filters_kb(uid),
        )


# =========================
# ФОНОВЫЕ УВЕДОМЛЕНИЯ
# =========================

def _disable_notifications(uid: int, s: UserSettings) -> None:
    """Пользователь заблокировал бота — перестаём к нему стучаться."""
    s.notify_enabled = False
    save_settings(uid, s)


async def _send_digest(uid: int, text: str) -> None:
    await send_chunks(uid, text)


async def notifier_loop():
    while True:
        try:
            if USER_SETTINGS and MARKET.is_ready():
                items = MARKET.items()
                now = datetime.now(tz=timezone.utc)

                await notify_users(
                    USER_SETTINGS,
                    items,
                    int(now.timestamp() * 1000),
                    _send_digest,
                    SENT_HOURS,
                    is_blocked=lambda e: isinstance(e, TelegramForbiddenError),
                    on_blocked=_disable_notifications,
                )

                await radar.radar_users(
                    USER_SETTINGS,
                    items,
                    int(now.timestamp()),
                    _send_digest,
                    RADAR_MARKS,
                    store.db_radar_mark,
                    is_blocked=lambda e: isinstance(e, TelegramForbiddenError),
                    on_blocked=_disable_notifications,
                )
        except Exception:
            log.exception("notifier_loop error")

        await asyncio.sleep(POLL_SECONDS)


def min_volume_for_okx() -> float:
    """Самый мягкий порог объёма среди пользователей — по нему отбираем монеты OKX."""
    return okx_min_volume([s.vol_threshold_usdt for s in USER_SETTINGS.values()])


async def main():
    store.db_init()

    # Грузим всех пользователей сразу: раньше словарь наполнялся только входящими
    # сообщениями, и после рестарта дайджест шёл лишь тем, кто сам написал боту.
    USER_SETTINGS.update(store.db_load_all())

    # Перезаписываем санированные значения: чинит кривые знаки порогов и
    # проставляет таймзону тем, кто её так и не ввёл.
    for uid, s in USER_SETTINGS.items():
        store.db_save_user(uid, s)

    # Отметки радара переживают рестарт, иначе деплой обнулял бы тишину
    # и люди получали бы повторы по тем же монетам.
    store.db_radar_purge(int(datetime.now(tz=timezone.utc).timestamp()) - radar.KEEP_MARKS_SEC)
    RADAR_MARKS.update(store.db_radar_load_all())

    log.info(
        "loaded %s users, %s radar marks, okx min volume=%.0f",
        len(USER_SETTINGS), len(RADAR_MARKS), min_volume_for_okx(),
    )

    asyncio.create_task(MARKET.run(min_volume_for_okx))
    asyncio.create_task(notifier_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
