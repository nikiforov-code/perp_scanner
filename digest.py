"""Отбор монет и сборка сообщения дайджеста.

Тем же кодом собирается и ответ на кнопку «🔥 ТОП Фандинги» — списки одинаковые,
отличается только заголовок.
"""

from datetime import datetime
from typing import Optional

from formatting import (
    EX_NAME,
    calc_coin_width,
    format_item_line,
    is_clean_symbol,
)
from user_settings import UserSettings

EXCHANGES = ["BINANCE", "BYBIT", "OKX", "GATE"]

HORIZON_HOURS = 4
MAX_PER_EXCHANGE = 20   # столько строк на биржу влезает в сообщение

# Сколько минут после целевой ещё имеет смысл слать дайджест. Раньше требовалось
# точное совпадение минуты, и подвисание цикла стоило пропуска целого часа.
GRACE_MINUTES = 10


def passes_threshold(fr: float, pos_thr: float, neg_thr: float) -> bool:
    return fr >= pos_thr or fr <= neg_thr


def hour_key(now_local: datetime) -> int:
    """Идентификатор текущего часа по локальному времени пользователя."""
    return int(now_local.replace(minute=0, second=0, microsecond=0).timestamp())


def target_minute(s: UserSettings) -> int:
    return (60 - int(s.digest_before_hour_minutes)) % 60


def due_hour_key(s: UserSettings, now_local: datetime, grace: int = GRACE_MINUTES) -> Optional[int]:
    """Ключ часа, если дайджест сейчас пора слать, иначе None.

    Окно вместо точной минуты: если цикл задержался, дайджест всё равно уйдёт.
    """
    target = target_minute(s)
    minute = now_local.minute
    if target <= minute <= target + grace:
        return hour_key(now_local)
    return None


def select_items(items: list, s: UserSettings, now_ms: int, horizon_hours: int = HORIZON_HOURS) -> dict:
    """Фильтрует монеты по настройкам пользователя и группирует по биржам."""
    max_ms = now_ms + horizon_hours * 60 * 60 * 1000
    groups = {ex: [] for ex in EXCHANGES}

    for x in items:
        ex = (x.get("exchange") or "").strip().upper()
        if ex not in groups:
            continue

        sym = (x.get("symbol") or "").strip().upper()
        if not is_clean_symbol(sym):
            continue

        next_ms = int(x.get("next_funding_ms") or 0)
        if not next_ms or next_ms < now_ms or next_ms > max_ms:
            continue

        fr = float(x.get("funding_rate", 0.0))
        if not passes_threshold(fr, s.pos_threshold, s.neg_threshold):
            continue

        if float(x.get("vol_usdt_24h") or 0.0) < s.vol_threshold_usdt:
            continue

        groups[ex].append(x)

    # сначала ближайшие по времени начисления, при равенстве — по модулю ставки
    for ex in groups:
        groups[ex].sort(
            key=lambda z: (int(z.get("next_funding_ms") or 0), -abs(float(z.get("funding_rate", 0.0))))
        )

    return groups


def total_count(groups: dict) -> int:
    return sum(len(v) for v in groups.values())


def build_message(groups: dict, s: UserSettings, title: str, now_local: datetime, tz, binance_prices=None) -> str:
    """Собирает HTML-сообщение с четырьмя секциями бирж."""
    offset = int(s.utc_offset_hours)
    offset_txt = f"UTC {'+' if offset >= 0 else ''}{offset}"
    vol_m = s.vol_threshold_usdt / 1_000_000

    lines = [
        f"{title} ({offset_txt})",
        (
            f"Ставка ≥{s.pos_threshold:.2f}% или ≤{s.neg_threshold:.2f}% | "
            f"Объём ≥{vol_m:g}M | "
            f"Горизонт {HORIZON_HOURS} часа"
        ),
        "",
    ]

    for ex in EXCHANGES:
        ex_title = EX_NAME.get(ex, ex)
        arr = groups.get(ex) or []

        if not arr:
            lines.append(f"<b>{ex_title}</b>: —")
            lines.append("")
            continue

        shown = arr[:MAX_PER_EXCHANGE]
        coin_w = calc_coin_width(shown)

        if len(arr) > len(shown):
            lines.append(f"<b>{ex_title}</b>  (показано {len(shown)} из {len(arr)}):")
        else:
            lines.append(f"<b>{ex_title}</b> (найдено {len(arr)}):")

        for x in shown:
            lines.append(format_item_line(x, coin_w, tz=tz, binance_prices=binance_prices or {}))
        lines.append("")

    return "\n".join(lines).strip()
