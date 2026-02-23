import os
import sqlite3

UI_DB_PATH = os.getenv("UI_DB_PATH", "ui_settings.db")

UI_DEFAULTS = {
    "min_vol_usdt": 5_000_000,          # дефолт (потом будешь менять через админку)
    "min_spread_timing_yes": 0.2,       # дефолт для Timing=YES
    "min_spread_timing_no": 0.35,       # дефолт для Timing=NO
    "min_price_spread_neg": 0.0,        # фильтр Price Δ%: показывать только если <= -этого значения
}

# Текущие значения в памяти (загружаются из SQLite при ui_db_init)
UI_SETTINGS = dict(UI_DEFAULTS)


def ui_db_init() -> None:
    """
    Создаёт таблицу ui_settings и подтягивает настройки в UI_SETTINGS.
    Если каких-то ключей нет — создаёт их с дефолтами.
    """
    con = sqlite3.connect(UI_DB_PATH, timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=3000;")
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS ui_settings (
                k TEXT PRIMARY KEY,
                v REAL
            )
            """
        )

        # гарантируем наличие дефолтов в БД
        for k, v in UI_DEFAULTS.items():
            con.execute(
                "INSERT OR IGNORE INTO ui_settings(k, v) VALUES(?, ?)",
                (k, float(v)),
            )

        con.commit()

        # грузим в память
        cur = con.execute("SELECT k, v FROM ui_settings")
        rows = cur.fetchall()
        for k, v in rows:
            if k:
                UI_SETTINGS[k] = float(v)

    finally:
        con.close()


def ui_get(key: str, default=None):
    """
    Чтение из памяти (UI_SETTINGS). SQLite читаем один раз на старте (ui_db_init),
    дальше работаем из памяти.
    """
    return UI_SETTINGS.get(key, default)


def ui_set(key: str, value: float) -> None:
    """
    Запись в SQLite + обновление памяти.
    """
    key = (key or "").strip()
    if not key:
        return

    con = sqlite3.connect(UI_DB_PATH, timeout=5)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA busy_timeout=3000;")
        con.execute(
            "INSERT INTO ui_settings(k, v) VALUES(?, ?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, float(value)),
        )
        con.commit()
        UI_SETTINGS[key] = float(value)
    finally:
        con.close()
