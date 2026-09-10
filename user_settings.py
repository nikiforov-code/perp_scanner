"""Персональные настройки пользователей бота и их хранение в SQLite."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional

DB_PATH = "bot_settings.db"
SQLITE_TIMEOUT_SEC = 5.0  # сколько ждать, если база занята

DEFAULT_UTC_OFFSET = 3  # решение владельца: пользователям без таймзоны ставим UTC+3
MIN_UTC_OFFSET = -12
MAX_UTC_OFFSET = 12

# Радар жирных отрицательных фандингов на Bybit
DEFAULT_RADAR_RATE = -1.5        # ставка, ниже которой монета интересна
DEFAULT_RADAR_VOL = 5_000_000    # минимальный объём за сутки, USDT


@dataclass
class UserSettings:
    pos_threshold: float = 0.45              # сигнал если funding_rate >= +0.45
    neg_threshold: float = -0.45             # сигнал если funding_rate <= -0.45
    digest_before_hour_minutes: int = 20     # за сколько минут до смены часа шлём дайджест
    vol_threshold_usdt: float = 10_000_000   # минимальный объём 24h в USDT
    notify_enabled: bool = True
    utc_offset_hours: Optional[int] = None   # часовой пояс пользователя
    radar_rate: float = DEFAULT_RADAR_RATE   # радар: ставка (только отрицательная)
    radar_vol: float = DEFAULT_RADAR_VOL     # радар: минимальный объём


def sanitize(s: UserSettings) -> UserSettings:
    """Приводит настройки к осмысленному виду.

    Главное здесь — знаки порогов. Пользователь, задавший отрицательный порог
    как +1.00, ловил в дайджест весь рынок: проверка `fr >= 1.0 or fr <= 1.0`
    истинна для любой монеты. Такое сообщение не влезало в лимит Telegram и
    обрывало рассылку всем, кто стоял в очереди следом.
    """
    s.pos_threshold = abs(float(s.pos_threshold))
    s.neg_threshold = -abs(float(s.neg_threshold))

    if s.utc_offset_hours is None:
        s.utc_offset_hours = DEFAULT_UTC_OFFSET
    else:
        s.utc_offset_hours = max(MIN_UTC_OFFSET, min(MAX_UTC_OFFSET, int(s.utc_offset_hours)))

    s.digest_before_hour_minutes = max(1, min(59, int(s.digest_before_hour_minutes)))
    s.vol_threshold_usdt = max(0.0, float(s.vol_threshold_usdt))
    s.notify_enabled = bool(s.notify_enabled)

    # Радар торгует только отрицательную сторону, поэтому знак чинится так же.
    # Ноль означает «не задано»: у строк после миграции он превратился бы в фильтр
    # «любая отрицательная ставка», а это спам.
    s.radar_rate = -abs(float(s.radar_rate)) if float(s.radar_rate) else DEFAULT_RADAR_RATE
    s.radar_vol = float(s.radar_vol) if float(s.radar_vol) > 0 else DEFAULT_RADAR_VOL
    return s


REQUIRED_COLUMNS = {
    "pos_threshold": "REAL NOT NULL DEFAULT 0",
    "neg_threshold": "REAL NOT NULL DEFAULT 0",
    "digest_before_hour_minutes": "INTEGER NOT NULL DEFAULT 0",
    "vol_threshold_usdt": "REAL NOT NULL DEFAULT 0",
    "notify_enabled": "INTEGER NOT NULL DEFAULT 0",
    "updated_ts": "INTEGER NOT NULL DEFAULT 0",
    "utc_offset_hours": "INTEGER DEFAULT NULL",
    "radar_rate": f"REAL NOT NULL DEFAULT {DEFAULT_RADAR_RATE}",
    "radar_vol": f"REAL NOT NULL DEFAULT {DEFAULT_RADAR_VOL}",
}

_SELECT_COLUMNS = (
    "pos_threshold, neg_threshold, digest_before_hour_minutes, "
    "vol_threshold_usdt, notify_enabled, utc_offset_hours, radar_rate, radar_vol"
)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=SQLITE_TIMEOUT_SEC)


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _row_to_settings(row) -> UserSettings:
    return sanitize(
        UserSettings(
            pos_threshold=float(row[0]),
            neg_threshold=float(row[1]),
            digest_before_hour_minutes=int(row[2]),
            vol_threshold_usdt=float(row[3]),
            notify_enabled=bool(int(row[4])),
            utc_offset_hours=(int(row[5]) if row[5] is not None else None),
            radar_rate=float(row[6]) if row[6] is not None else DEFAULT_RADAR_RATE,
            radar_vol=float(row[7]) if row[7] is not None else DEFAULT_RADAR_VOL,
        )
    )


def db_init() -> None:
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                pos_threshold REAL NOT NULL,
                neg_threshold REAL NOT NULL,
                digest_before_hour_minutes INTEGER NOT NULL,
                vol_threshold_usdt REAL NOT NULL,
                notify_enabled INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL,
                utc_offset_hours INTEGER,
                radar_rate REAL NOT NULL DEFAULT {rate},
                radar_vol REAL NOT NULL DEFAULT {vol}
            )
            """.format(rate=DEFAULT_RADAR_RATE, vol=DEFAULT_RADAR_VOL)
        )

        cols = _table_columns(conn, "user_settings")
        for col, col_def in REQUIRED_COLUMNS.items():
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE user_settings ADD COLUMN {col} {col_def}")
                except sqlite3.OperationalError:
                    pass

        # Отметки радара: по паре «пользователь + монета» держим время последнего
        # оповещения. В памяти был бы риск повторов после каждого деплоя.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS radar_alerts (
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                sent_ts INTEGER NOT NULL,
                PRIMARY KEY (user_id, symbol)
            )
            """
        )

        missing = [c for c in REQUIRED_COLUMNS if c not in _table_columns(conn, "user_settings")]
        if missing:
            raise RuntimeError(f"DB schema mismatch: missing columns in user_settings: {missing}")
        conn.commit()
    finally:
        conn.close()


def db_load_user(user_id: int) -> Optional[UserSettings]:
    conn = _connect()
    try:
        row = conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM user_settings WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_settings(row) if row else None
    finally:
        conn.close()


def db_load_all() -> Dict[int, UserSettings]:
    """Все пользователи разом — вызывается при старте бота.

    Без этого словарь в памяти наполнялся только входящими сообщениями, и после
    рестарта дайджест уходил лишь тем, кто сам написал боту.
    """
    conn = _connect()
    try:
        rows = conn.execute(f"SELECT user_id, {_SELECT_COLUMNS} FROM user_settings").fetchall()
        return {int(r[0]): _row_to_settings(r[1:]) for r in rows}
    finally:
        conn.close()


def db_save_user(user_id: int, s: UserSettings) -> None:
    conn = _connect()
    try:
        now_ts = int(datetime.now(tz=timezone.utc).timestamp())
        conn.execute(
            """
            INSERT INTO user_settings (
                user_id, pos_threshold, neg_threshold, digest_before_hour_minutes,
                vol_threshold_usdt, notify_enabled, utc_offset_hours, updated_ts,
                radar_rate, radar_vol
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                pos_threshold = excluded.pos_threshold,
                neg_threshold = excluded.neg_threshold,
                digest_before_hour_minutes = excluded.digest_before_hour_minutes,
                vol_threshold_usdt = excluded.vol_threshold_usdt,
                notify_enabled = excluded.notify_enabled,
                utc_offset_hours = excluded.utc_offset_hours,
                updated_ts = excluded.updated_ts,
                radar_rate = excluded.radar_rate,
                radar_vol = excluded.radar_vol
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
                float(s.radar_rate),
                float(s.radar_vol),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- отметки радара ----------

def db_radar_load_all() -> Dict[tuple, int]:
    """{(user_id, symbol): sent_ts} — читается один раз при старте."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT user_id, symbol, sent_ts FROM radar_alerts").fetchall()
        return {(int(u), str(sym)): int(ts) for u, sym, ts in rows}
    finally:
        conn.close()


def db_radar_mark(user_id: int, symbol: str, sent_ts: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO radar_alerts(user_id, symbol, sent_ts) VALUES(?, ?, ?) "
            "ON CONFLICT(user_id, symbol) DO UPDATE SET sent_ts=excluded.sent_ts",
            (int(user_id), str(symbol), int(sent_ts)),
        )
        conn.commit()
    finally:
        conn.close()


def db_radar_purge(before_ts: int) -> int:
    """Чистит старые отметки, чтобы таблица не росла бесконечно."""
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM radar_alerts WHERE sent_ts < ?", (int(before_ts),))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
