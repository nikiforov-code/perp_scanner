import os
import asyncio
import httpx
import re
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple

from aiogram import Bot, Dispatcher
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("funding-bot")


TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found in .env")

API_BASE = "http://127.0.0.1:8000"
MSK = timezone(timedelta(hours=3))
DB_PATH = "bot_settings.db"
SQLITE_TIMEOUT_SEC = 5.0  # сколько секунд ждать, если SQLite занят (database is locked)

EX_NAME = {
    "BINANCE": "Binance",
    "BYBIT": "Bybit",
    "OKX": "OKX",
    "GATE": "Gate",
    "MEXC": "MEXC",
    "BITGET": "Bitget",
}

EX_SHORT = {
    "BINANCE": "Bin",
    "BYBIT": "ByBit",
    "GATE": "Gate",
    "OKX": "OKX",
}


# =========================
# НАСТРОЙКИ БОТА (можешь менять)
# =========================
POLL_SECONDS = 25          # как часто проверяем уведомления
TOP_LIMIT = 15             # сколько брать из /api/bot/top для уведомлений/списков
SENT_TTL_SECONDS = 60 * 60 # 1 час держим ключи отправленных уведомлений
# =========================
# ТОП ФАНДИНГИ (кнопка)
# =========================
TOP_MAX_PER_EX = 20        # максимум строк на одну биржу в сообщении
TOP_EXCHANGES = ["BINANCE", "BYBIT", "OKX", "GATE"]
TG_MAX_LEN = 3800          # безопасный лимит длины Telegram-сообщения

# =========================
# Персональные настройки
# =========================
@dataclass
class UserSettings:
    pos_threshold: float = 0.45    # сигнал если funding_rate >= +0.45
    neg_threshold: float = -0.45   # сигнал если funding_rate <= -0.45
    digest_before_hour_minutes: int = 20  # за сколько минут до смены часа присылать дайджест (20 => в :40)
    vol_threshold_usdt: float = 10_000_000  # минимальный объём 24h в USDT
    notify_enabled: bool = True  # уведомления по умолчанию включены
    utc_offset_hours: Optional[int] = None  # часовой пояс пользователя (UTC offset, например -10..+14). None = не задан.

USER_SETTINGS: Dict[int, UserSettings] = {}

# "ожидание ввода" (простая мини-FSM)
WAITING_INPUT: Dict[int, str] = {}  # user_id -> "pos" | "neg" | "digest_before" | "vol"

# антидубль уведомлений
# key: (user_id, exchange, symbol, next_ms, sign) -> last_sent_epoch
SENT_CACHE: Dict[Tuple[int, str, str, int, str], int] = {}
LAST_WRONG_MINUTE_LOG: Dict[int, int] = {}  # user_id -> last logged minute for wrong_minute

REQUIRED_COLUMNS = {
    "pos_threshold": "REAL NOT NULL DEFAULT 0",
    "neg_threshold": "REAL NOT NULL DEFAULT 0",
    "digest_before_hour_minutes": "INTEGER NOT NULL DEFAULT 0",
    "vol_threshold_usdt": "REAL NOT NULL DEFAULT 0",
    "notify_enabled": "INTEGER NOT NULL DEFAULT 0",
    "updated_ts": "INTEGER NOT NULL DEFAULT 0",
    "utc_offset_hours": "INTEGER DEFAULT NULL",
}

def db_init():
    # создаём таблицу настроек, если её ещё нет
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SEC)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                pos_threshold REAL NOT NULL,
                neg_threshold REAL NOT NULL,
                digest_before_hour_minutes INTEGER NOT NULL,
                vol_threshold_usdt REAL NOT NULL,
                notify_enabled INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                utc_offset_hours INTEGER
            )
        """)
        # --- migrations: добавляем недостающие колонки в уже существующей БД ---
        cols = db_table_columns(conn, "user_settings")
        for col, col_def in REQUIRED_COLUMNS.items():
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    # если БД "старая/странная" или колонка уже появилась — не валим запуск
                    pass
        # проверка: все обязательные колонки на месте
        cols_after = db_table_columns(conn, "user_settings")
        missing = [c for c in REQUIRED_COLUMNS.keys() if c not in cols_after]
        if missing:
            raise RuntimeError(f"DB schema mismatch: missing columns in user_settings: {missing}")
        conn.commit()
    finally:
        conn.close()

def db_load_user(user_id: int) -> Optional[UserSettings]:
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SEC)
    try:
        cur = conn.execute(
            """
            SELECT pos_threshold, neg_threshold, digest_before_hour_minutes,
                   vol_threshold_usdt, notify_enabled, utc_offset_hours
            FROM user_settings
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        return UserSettings(
            pos_threshold=float(row[0]),
            neg_threshold=float(row[1]),
            digest_before_hour_minutes=int(row[2]),
            vol_threshold_usdt=float(row[3]),
            notify_enabled=bool(int(row[4])),
            utc_offset_hours=(int(row[5]) if row[5] is not None else None),
        )
    finally:
        conn.close()

def db_save_user(user_id: int, s: UserSettings):
    conn = sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SEC)
    try:
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        conn.execute(
            """
            INSERT INTO user_settings (
                user_id, pos_threshold, neg_threshold, digest_before_hour_minutes,
                vol_threshold_usdt, notify_enabled, utc_offset_hours, updated_ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pos_threshold = excluded.pos_threshold,
                neg_threshold = excluded.neg_threshold,
                digest_before_hour_minutes = excluded.digest_before_hour_minutes,
                vol_threshold_usdt = excluded.vol_threshold_usdt,
                notify_enabled = excluded.notify_enabled,
                utc_offset_hours = excluded.utc_offset_hours,
                updated_ts = excluded.updated_ts
            """,
            (
                user_id,
                float(s.pos_threshold),
                float(s.neg_threshold),
                int(s.digest_before_hour_minutes),
                float(s.vol_threshold_usdt),
                1 if s.notify_enabled else 0,
                (int(s.utc_offset_hours) if s.utc_offset_hours is not None else None),
                now_ts,
            ),
        )
        conn.commit()
    finally:
        conn.close()

def db_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}

def get_settings(user_id: int) -> UserSettings:
    # 1) если уже есть в памяти
    if user_id in USER_SETTINGS:
        return USER_SETTINGS[user_id]

    # 2) пробуем загрузить из SQLite
    loaded = db_load_user(user_id)
    if loaded is not None:
        USER_SETTINGS[user_id] = loaded
        return loaded

    # 3) дефолт + сразу сохраняем, чтобы после рестарта не было "пусто"
    s = UserSettings()
    USER_SETTINGS[user_id] = s
    db_save_user(user_id, s)
    return s

def now_utc_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)

def fmt_coin(sym: str) -> str:
    return (sym or "").replace("USDT", "")

def fmt_rate(fr: float) -> str:
    # без плюса, но знак минус сохраняем
    s = f"{fr:+.2f}%"
    return s.replace("+", "")

LATIN_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]+$")

def is_clean_symbol(sym: str) -> bool:
    """
    Фильтр как в таблице: убираем не-латиницу (иероглифы и любой мусор).
    Допускаем только A-Z, 0-9 и . _ -
    """
    if not sym:
        return False
    s = sym.upper().strip()
    return bool(LATIN_SYMBOL_RE.match(s))

def calc_coin_width(items: list[dict], min_w: int = 4, max_w: int = 8) -> int:
    # ширина колонки = длина самого длинного названия монеты в текущем сообщении
    coins = [fmt_coin(x.get("symbol", "")) for x in items]
    w = max((len(c) for c in coins), default=min_w)
    return max(min_w, min(w, max_w))


def _to_positive_float_or_none(v) -> Optional[float]:
    try:
        num = float(v)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def build_binance_price_map(items: list[dict]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for x in items:
        ex_u = ((x.get("exchange") or "").strip().upper())
        if ex_u != "BINANCE":
            continue
        sym = ((x.get("symbol") or "").strip().upper())
        if not sym:
            continue
        px = _to_positive_float_or_none(x.get("mark_px"))
        if px is None:
            continue
        out[sym] = px
    return out


def format_delta_vs_binance(
    ex_u: str,
    sym: str,
    exchange_price: Optional[float],
    binance_prices: Dict[str, float],
) -> Optional[str]:
    if ex_u == "BINANCE":
        return None
    if ex_u not in {"BYBIT", "OKX", "GATE"}:
        return None
    if exchange_price is None:
        return None
    binance_price = _to_positive_float_or_none(binance_prices.get(sym))
    if binance_price is None:
        return None
    delta_pct = ((binance_price / exchange_price) - 1.0) * 100.0
    return f"{delta_pct:+.1f}%".replace(".", ",")


def format_item_line(
    x: dict,
    coin_w: int,
    tz: timezone = MSK,
    binance_prices: Optional[Dict[str, float]] = None,
) -> str:
    sym = x.get("symbol", "")
    ex = x.get("exchange", "")
    url = x.get("url", "")
    ex_u = (ex or "").strip().upper()
    next_ms = int(x.get("next_funding_ms") or 0)
    fr = float(x.get("funding_rate", 0.0))
    vol = float(x.get("vol_usdt_24h") or 0.0)
    mark_px = _to_positive_float_or_none(x.get("mark_px"))
    vol_m_txt = f"{vol/1e6:.1f}M".replace(".", ",") if vol > 0 else "--"


    coin = fmt_coin(sym)
    fr_txt = fmt_rate(fr)
    hhmm = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%H:%M") if next_ms else "--:--"

    # 🟢 отрицательный, 🔴 положительный
    dot = "🟢" if fr < 0 else "🔴"

    # колонки: coin (динамическая), rate/time (фиксированные)
    RATE_W = 7  # "-1.47%" помещается
    TIME_W = 5  # "03:00"

    coin_col = (coin[:coin_w]).ljust(coin_w)
    rate_col = (fr_txt[:RATE_W]).rjust(RATE_W)
    time_col = hhmm.ljust(TIME_W)

    VOL_W = 6  # компактнее справа
    vol_col = (vol_m_txt[:VOL_W]).rjust(VOL_W)
    delta_txt = format_delta_vs_binance(ex_u, (sym or "").strip().upper(), mark_px, binance_prices or {})

    delta_block = ""
    if ex_u in {"BYBIT", "OKX", "GATE"}:
        delta_block = ""

    mono = f"{coin_col} {rate_col} {time_col} {vol_col}{delta_block}"

    if ex_u == "BINANCE":
        link_html = f' <a href="{url}">BIN</a>' if url else ""
        return f"{dot} <code>{mono}</code>{link_html}"

    if ex_u in {"BYBIT", "OKX", "GATE"}:
        delta_label = f"Δ{delta_txt}" if delta_txt is not None else "ΔНЕТ"
        delta_html = f' <a href="{url}">{delta_label}</a>' if url else f" {delta_label}"
        return f"{dot} <code>{mono}</code>{delta_html}"

    return f"{dot} <code>{mono}</code>"

def fmt_hhmm_msk(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK)
    return dt.strftime("%H:%M")

def purge_sent_cache():
    # чистим старые ключи, чтобы словарь не рос бесконечно
    now_ts = int(datetime.now(tz=timezone.utc).timestamp())
    dead = []
    for k, ts in SENT_CACHE.items():
        if now_ts - ts > SENT_TTL_SECONDS:
            dead.append(k)
    for k in dead:
        SENT_CACHE.pop(k, None)

async def send_split(message: Message, text: str, reply_markup=None):
    """
    Отправляет длинный HTML-текст частями, не ломая разметку.
    Режем по строкам, чтобы не порвать <code> и <a>.
    """
    lines = text.split("\n")
    buf = ""

    for ln in lines:
        chunk = ln + "\n"
        if len(buf) + len(chunk) > TG_MAX_LEN:
            await message.answer(
                buf.rstrip(),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup
            )
            buf = ""
        buf += chunk

    if buf.strip():
        await message.answer(
            buf.rstrip(),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup
        )

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
    tz_btn = f"🕒 Таймзона (сейчас UTC {'+' if s.utc_offset_hours >= 0 else ''}{s.utc_offset_hours})"
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
                KeyboardButton(text=tz_btn),
                KeyboardButton(text=notify_btn),
            ],
            [
                KeyboardButton(text="⬅️ Назад"),
            ],
        ],
        resize_keyboard=True,
    )

# =========================
# TELEGRAM BOT
# =========================
bot = Bot(token=TOKEN)
dp = Dispatcher()

async def fetch_all():
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{API_BASE}/api/bot/all")
        r.raise_for_status()
        return r.json()

async def fetch_price_spread(symbol: str):
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{API_BASE}/api/bot/price_spread",
            params={"symbol": symbol},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

def passes_threshold(fr: float, pos_thr: float, neg_thr: float) -> bool:
    return fr >= pos_thr or fr <= neg_thr

@dp.message(Command("start"))
async def start(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)

    # ЖЁСТКО: без таймзоны не даём пользоваться ботом
    if s.utc_offset_hours is None:
        WAITING_INPUT[uid] = "tz"
        await message.answer(
            "⏱ Укажи свой часовой пояс как UTC offset числом.\n"
            "Пример: -10, 0, 3, 12\n"
            "Допустимый диапазон: от -12 до +12.\n\n"
            "Введи число одним сообщением:"
        )
        return

    await message.answer(
        "Funding Spread Scanner Bot\n\nВыбери действие:",
        reply_markup=MAIN_KB
    )

@dp.message(Command("me"))
async def me(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)

    status = "ВКЛ" if s.notify_enabled else "ВЫКЛ"

    text = (
        "👤 *Твои текущие настройки*\n\n"
        f"🕒 Таймзона: UTC {'+' if s.utc_offset_hours >= 0 else ''}{s.utc_offset_hours}\n"
        f"📈 Порог +: `{s.pos_threshold:.2f}%`\n"
        f"📉 Порог −: `{s.neg_threshold:.2f}%`\n"
        f"⏰ Дайджест за: `{s.digest_before_hour_minutes} мин`\n"
        f"💰 Объём ≥: `{s.vol_threshold_usdt / 1_000_000:.1f} млн USDT`\n"
        f"🔔 Уведомления: `{status}`"
    )

    await message.answer(text, parse_mode="Markdown")

@dp.message(lambda m: m.text == "🔥 ТОП Фандинги")
async def top_fundings(message: Message):
    try:
        uid = message.from_user.id
        s = get_settings(uid)
        user_tz = timezone(timedelta(hours=int(s.utc_offset_hours)))

        # 1) берём много данных
        items = await fetch_all()
        binance_prices = build_binance_price_map(items)

        # 2) фильтруем по порогам
        filtered = []
        for x in items:
            fr = float(x.get("funding_rate", 0.0))
            ex = (x.get("exchange") or "").upper()
            sym = (x.get("symbol") or "").strip()

            next_ms = int(x.get("next_funding_ms") or 0)
            vol = float(x.get("vol_usdt_24h") or 0.0)
            now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            max_ms = now_ms + 4 * 60 * 60 * 1000
            
            if (
                ex in TOP_EXCHANGES
                and is_clean_symbol(sym)
                and next_ms
                and passes_threshold(fr, s.pos_threshold, s.neg_threshold)
                and vol >= s.vol_threshold_usdt
                and (now_ms <= next_ms <= max_ms)
            ):
                filtered.append(x)

        if not filtered:
            await message.answer(
                f"Нет фандингов по фильтру (≥{s.pos_threshold:.2f}% или ≤{s.neg_threshold:.2f}%) на выбранных биржах.",
                reply_markup=MAIN_KB
            )
            return

        # 3) группируем по биржам
        groups = {ex: [] for ex in TOP_EXCHANGES}
        for x in filtered:
            ex = (x.get("exchange") or "").upper()
            groups.setdefault(ex, []).append(x)

        # 4) сортировка внутри биржи:
        # сначала ближайшие по времени начисления, при равенстве — по модулю ставки (больше выше)
        for ex in groups:
            groups[ex].sort(
                key=lambda z: (
                    int(z.get("next_funding_ms") or 0),
                    -abs(float(z.get("funding_rate", 0.0)))
                )
            )

        lines_all = []

        vol_m = s.vol_threshold_usdt / 1_000_000
        utc_offset_hours = int(s.utc_offset_hours)  # уже обязателен, т.к. TZ mandatory
        utc_offset_txt = f"UTC {'+' if utc_offset_hours >= 0 else ''}{utc_offset_hours}"

        lines_all.append(
            f"🔥 <b>ТОП фандинги</b> ({utc_offset_txt})"
        )
        lines_all.append(
            f"Ставка ≥{s.pos_threshold:.2f}% или ≤{s.neg_threshold:.2f}% | "
            f"Объём ≥{vol_m:g}M | "
            f"Горизонт 4 часа"
        )
        lines_all.append("")  # пустая строка перед списками

        for ex in TOP_EXCHANGES:
            ex_title = EX_NAME.get(ex, ex)
            arr = groups.get(ex) or []
            if not arr:
                # пустые биржи можно скрывать, но ты просил 4 раздела — оставлю “нет данных”
                lines_all.append(f"<b>{ex_title}</b>: —")
                lines_all.append("")  # пустая строка
                continue

            # ограничим вывод (иначе можно утонуть)
            arr_view = arr[:TOP_MAX_PER_EX]

            coin_w = calc_coin_width(arr_view)

            lines_all.append(f"<b>{ex_title}</b>  (показано {len(arr_view)} из {len(arr)}):")
            for x in arr_view:
                lines_all.append(format_item_line(x, coin_w, tz=user_tz, binance_prices=binance_prices))
            lines_all.append("")  # пустая строка между разделами

        text = "\n".join(lines_all).strip()

        # 6) отправляем (с авто-разбиением если длинно)
        await send_split(message, text, reply_markup=MAIN_KB)

    except Exception as e:
        await message.answer(f"Ошибка получения данных: {e}", reply_markup=MAIN_KB)

@dp.message(lambda m: m.text == "🔔 Фильтры")
async def filters_menu(message: Message):
    uid = message.from_user.id
    WAITING_INPUT.pop(uid, None)
    await message.answer(
        "Настройки фильтров и уведомлений:",
        reply_markup=filters_kb(uid)
    )

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
        reply_markup=filters_kb(uid)
    )

@dp.message(lambda m: m.text and m.text.startswith("Порог -"))
async def ask_neg_threshold(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "neg"
    s = get_settings(uid)
    await message.answer(
        f"Введи новый порог для отрицательных (пример: -0.40)\nСейчас: {s.neg_threshold:.2f}%",
        reply_markup=filters_kb(uid)
    )

@dp.message(lambda m: m.text and m.text.startswith("Дайджест за"))
async def ask_digest_before_hour(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "digest_before"
    s = get_settings(uid)
    await message.answer(
        f"Введи за сколько минут до начала следующего часа присылать дайджест (пример: 30)\n"
        f"Сейчас: {s.digest_before_hour_minutes} мин",
        reply_markup=filters_kb(uid),
    )

@dp.message(lambda m: m.text and m.text.startswith("Объём ≥"))
async def ask_volume_threshold(message: Message):
    uid = message.from_user.id
    WAITING_INPUT[uid] = "vol"
    s = get_settings(uid)
    cur_m = s.vol_threshold_usdt / 1_000_000
    await message.answer(
        f"Введи минимальный объём 24h в миллионах USDT (пример: 5 = 5 000 000)\n"
        f"Сейчас: {cur_m:g}M",
        reply_markup=filters_kb(uid),
    )

@dp.message(lambda m: m.text in ("✅ Уведомления ВКЛ", "⛔️ Уведомления ВЫКЛ"))
async def toggle_notify(message: Message):
    uid = message.from_user.id
    s = get_settings(uid)
    s.notify_enabled = not s.notify_enabled
    db_save_user(uid, s)
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
        f"Сейчас: UTC {'+' if s.utc_offset_hours >= 0 else ''}{s.utc_offset_hours}\n"
        "Введи число одним сообщением:",
        reply_markup=filters_kb(uid)
    )

@dp.message()
async def any_text_handler(message: Message):
    uid = message.from_user.id
    mode = WAITING_INPUT.get(uid)

    # если мы не ждём ввод настроек — обычный fallback
    if not mode:
        raw_text = (message.text or "").strip()
        if raw_text and re.fullmatch(r"[A-Za-z0-9]{2,20}", raw_text):
            try:
                data = await fetch_price_spread(raw_text)
            except Exception:
                data = None
            if data:
                sp = float(data["spread_pct"])
                sym = data["symbol"]
                bp = float(data["binance_price"])
                yp = float(data["bybit_price"])
                lines = [
                    f"📊 {sym}",
                    f"Binance: {bp:.5f}",
                    f"Bybit: {yp:.5f}",
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
                return
            await message.answer(
                "Монета не найдена на Binance/Bybit или по ней ещё нет данных.",
                reply_markup=MAIN_KB,
            )
            return
        await message.answer("Команда не распознана. Нажми кнопку в меню 👇", reply_markup=MAIN_KB)
        return

    # ждём число
    raw = (message.text or "").strip().replace(",", ".")
    try:
        if mode == "tz":
            val = int(float(raw))  # чтобы "3.0" тоже принималось
            if val < -12 or val > 12:
                raise ValueError("utc_offset_hours должен быть от -12 до +12")

            s = get_settings(uid)
            s.utc_offset_hours = val
            db_save_user(uid, s)

            WAITING_INPUT.pop(uid, None)

            await message.answer(
                f"Готово ✅ Таймзона установлена: UTC {'+' if val >= 0 else ''}{val}\n\nВыбери действие:",
                reply_markup=MAIN_KB
            )
            return

        if mode in ("pos", "neg"):
            val = float(raw)
            # мягкая валидация
            if abs(val) > 50:
                raise ValueError("слишком большое значение")
            s = get_settings(uid)
            if mode == "pos":
                s.pos_threshold = val
            else:
                s.neg_threshold = val
            db_save_user(uid, s)    
            WAITING_INPUT.pop(uid, None)
            await message.answer("Готово ✅", reply_markup=filters_kb(uid))
            return

        if mode == "digest_before":
            val = int(float(raw))
            if val < 1 or val > 59:
                raise ValueError("digest_before_hour_minutes должен быть 1..59")
            s = get_settings(uid)
            s.digest_before_hour_minutes = val
            db_save_user(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer("Готово ✅", reply_markup=filters_kb(uid))
            return
        
        if mode == "vol":
            val_m = float(raw.replace(",", "."))
            if val_m <= 0 or val_m > 100000:
                raise ValueError("Объём должен быть >0 (в миллионах USDT)")
            s = get_settings(uid)
            s.vol_threshold_usdt = val_m * 1_000_000
            db_save_user(uid, s)
            WAITING_INPUT.pop(uid, None)
            await message.answer("Готово ✅", reply_markup=filters_kb(uid))
            return       

    except Exception:
        await message.answer(
            "Не понял значение. Введи число (пример: 0.40 или -0.40 или 30).",
            reply_markup=filters_kb(uid)
        )

# =========================
# ФОНОВЫЕ УВЕДОМЛЕНИЯ
# =========================
async def notifier_loop():
    while True:
        try:
            purge_sent_cache()

            # если нет пользователей — просто спим
            if not USER_SETTINGS:
                await asyncio.sleep(POLL_SECONDS)
                continue

            # берём полный список (нужен объём и чтобы не упускать монеты)
            try:
                items = await fetch_all()
                binance_prices = build_binance_price_map(items)
            except Exception as e:
                log.exception("digest fetch_all failed: %s", e)
                await asyncio.sleep(POLL_SECONDS)
                continue

            now_ts = int(datetime.now(tz=timezone.utc).timestamp())

            for uid, s in list(USER_SETTINGS.items()):
                if not s.notify_enabled:
                    log.info("digest skip uid=%s reason=notify_disabled", uid)
                    continue

                if s.utc_offset_hours is None:
                    log.info("digest skip uid=%s reason=tz_missing", uid)
                    continue    

                # персональный триггер: отправляем в минуту (60 - N) по ЛОКАЛЬНОМУ времени пользователя
                user_tz = timezone(timedelta(hours=int(s.utc_offset_hours)))
                now_local = datetime.now(tz=user_tz)
                hour_key = int(now_local.replace(minute=0, second=0, microsecond=0).timestamp())

                target_minute = (60 - int(s.digest_before_hour_minutes)) % 60
                if now_local.minute != target_minute:
                    last_min = LAST_WRONG_MINUTE_LOG.get(uid)
                    if last_min != now_local.minute:
                        log.info(
                            "digest skip uid=%s reason=wrong_minute now_minute=%s target_minute=%s",
                            uid, now_local.minute, target_minute
                        )
                        LAST_WRONG_MINUTE_LOG[uid] = now_local.minute
                    continue

                # антидубль: 1 дайджест на пользователя в час
                digest_key = (uid, "__DIGEST__", "__ALL__", hour_key, "batch")
                if digest_key in SENT_CACHE:
                    log.info(
                        "digest skip uid=%s reason=anti_duplicate hour_key=%s now=%s target_minute=%s",
                        uid, hour_key, now_local.strftime("%H:%M"), target_minute
                    )    
                    continue

                log.info(
                    "digest trigger uid=%s now=%s target_minute=%s pos>=%.2f neg<=%.2f vol>=%.0f",
                    uid, now_local.strftime("%H:%M"), target_minute, s.pos_threshold, s.neg_threshold, s.vol_threshold_usdt
                )

                # группируем отфильтрованные элементы по биржам (4 биржи всегда показываем)
                groups = {ex: [] for ex in TOP_EXCHANGES}
                # счётчики причин фильтрации (для дебага)
                c_total = 0
                c_ex = 0
                c_sym = 0
                c_next = 0
                c_thr = 0
                c_vol = 0
                c_time = 0
                c_ok = 0

                for x in items:
                    c_total += 1
                    ex = (x.get("exchange") or "").strip().upper()
                    if ex not in TOP_EXCHANGES:
                        c_ex += 1
                        continue

                    sym = (x.get("symbol") or "").strip().upper()
                    if not is_clean_symbol(sym):
                        c_sym += 1
                        continue

                    next_ms = int(x.get("next_funding_ms") or 0)
                    if not next_ms:
                        c_next += 1
                        continue

                    fr = float(x.get("funding_rate", 0.0))
                    if not passes_threshold(fr, s.pos_threshold, s.neg_threshold):
                        c_thr += 1
                        continue

                    # фильтр объёма 24h
                    vol = float(x.get("vol_usdt_24h") or 0.0)
                    if vol < s.vol_threshold_usdt:
                        c_vol += 1
                        continue

                    # фильтр по времени: от сейчас и до +4 часов вперёд (включительно)
                    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
                    max_ms = now_ms + 4 * 60 * 60 * 1000

                    if next_ms < now_ms or next_ms > max_ms:
                        c_time += 1
                        continue

                    groups[ex].append(x)
                    c_ok += 1

                # если вообще нет ни одной монеты по всем биржам — ничего не шлём
                total = sum(len(v) for v in groups.values())
                if total == 0:
                    log.info(
                        "digest skip uid=%s reason=no_items total=%s ex=%s sym=%s next=%s thr=%s vol=%s time=%s ok=%s now=%s target_minute=%s",
                        uid, c_total, c_ex, c_sym, c_next, c_thr, c_vol, c_time, c_ok, now_local.strftime("%H:%M"), target_minute
                    )
                    continue

                # сортировка внутри биржи: ближе по времени, затем по модулю ставки
                for ex in TOP_EXCHANGES:
                    groups[ex].sort(
                        key=lambda z: (
                            int(z.get("next_funding_ms") or 0),
                            -abs(float(z.get("funding_rate", 0.0)))
                        )
                    )

                # формируем сообщение с 4 секциями; если пусто — прочерк
                lines = []
                vol_m = s.vol_threshold_usdt / 1_000_000
                utc_offset_hours = int(s.utc_offset_hours)
                utc_offset_txt = f"UTC {'+' if utc_offset_hours >= 0 else ''}{utc_offset_hours}"

                lines.append(
                    f"🔔 <b>Актуальные фандинги</b> ({utc_offset_txt}) — дайджест {now_local.strftime('%H:%M')}"
                )
                lines.append(
                    f"Ставка ≥{s.pos_threshold:.2f}% или ≤{s.neg_threshold:.2f}% | "
                    f"Объём ≥{vol_m:g}M | "
                    f"Горизонт 4 часа"
                )
                lines.append("")  # пустая строка

                for ex in TOP_EXCHANGES:
                    ex_title = EX_NAME.get(ex, ex)
                    arr = groups.get(ex) or []
                    if not arr:
                        lines.append(f"<b>{ex_title}</b>: —")
                        lines.append("")
                        continue

                    coin_w = calc_coin_width(arr)
                    lines.append(f"<b>{ex_title}</b> (найдено {len(arr)}):")
                    for x in arr:
                        lines.append(format_item_line(x, coin_w, tz=user_tz, binance_prices=binance_prices))
                    lines.append("")

                text = "\n".join(lines).strip()

                await bot.send_message(
                    chat_id=uid,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                log.info(
                    "digest sent uid=%s total=%s now=%s target_minute=%s",
                    uid, total, now_local.strftime("%H:%M"), target_minute
                )

                SENT_CACHE[digest_key] = now_ts

        except Exception:
            log.exception("notifier_loop error")

        await asyncio.sleep(POLL_SECONDS)

async def main():
    db_init()
    # стартуем фоновую задачу уведомлений
    asyncio.create_task(notifier_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
