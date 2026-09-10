"""Радар жирных отрицательных фандингов на Bybit.

Дайджест такую монету может не показать: он приходит раз в час и отсекает всё, что
начисляется дальше четырёх часов, а восьмичасовые контракты Bybit половину цикла
лежат вне этого окна. Радар закрывает именно эту дыру — показывает монету один раз,
дальше человек ведёт её сам.

Правила:
  • только Bybit и только отрицательная сторона — торгуется она;
  • пороги ставки и объёма персональные, по умолчанию -1,5% и 5M;
  • горизонт четырёх часов НЕ применяется, в этом весь смысл радара;
  • после оповещения пара «пользователь + монета» молчит 12 часов, поэтому дребезг
    ставки у порога повторов не даёт.

Отправка передаётся снаружи, как в notifier.py, — чтобы логику можно было проверить
без Telegram.
"""

import logging
from datetime import datetime, timezone, timedelta

from formatting import fmt_coin, fmt_rate, is_clean_symbol

log = logging.getLogger("funding-bot.radar")

EXCHANGE = "BYBIT"
EXCHANGE_TITLE = "Bybit"

COOLDOWN_SEC = 12 * 3600   # столько монета молчит после оповещения
KEEP_MARKS_SEC = 48 * 3600  # старше этого отметки чистятся при старте


def select_hits(items: list, s, now_ts: int, last_sent: dict) -> list:
    """Монеты Bybit, о которых этому пользователю пора сказать.

    last_sent — {symbol: время последнего оповещения} по этому пользователю.
    """
    hits = []
    for x in items:
        if (x.get("exchange") or "").strip().upper() != EXCHANGE:
            continue

        sym = (x.get("symbol") or "").strip().upper()
        if not is_clean_symbol(sym):
            continue

        if not x.get("next_funding_ms"):
            continue

        rate = float(x.get("funding_rate", 0.0))
        if rate > s.radar_rate:          # radar_rate отрицательный
            continue

        if float(x.get("vol_usdt_24h") or 0.0) < s.radar_vol:
            continue

        if now_ts - int(last_sent.get(sym, 0)) < COOLDOWN_SEC:
            continue

        hits.append(x)

    # чем глубже ставка, тем интереснее
    hits.sort(key=lambda x: float(x.get("funding_rate", 0.0)))
    return hits


def _time_left(next_ms: int, now_ms: int) -> str:
    minutes = max(0, int((next_ms - now_ms) / 60000))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours} ч {mins} мин"
    if hours:
        return f"{hours} ч"
    return f"{mins} мин"


def build_alert(item: dict, s, tz, now_ms: int) -> str:
    """Одна монета — одно сообщение, чтобы радар не сливался с дайджестом."""
    sym = item.get("symbol", "")
    rate = float(item.get("funding_rate", 0.0))
    vol = float(item.get("vol_usdt_24h") or 0.0)
    next_ms = int(item.get("next_funding_ms") or 0)
    url = item.get("url") or ""

    vol_txt = f"{vol/1e6:.1f}M".replace(".", ",")
    hhmm = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%H:%M")

    lines = [
        "📡 <b>Радар: жирный фандинг</b>",
        "",
        f"<b>{fmt_coin(sym)}</b> · {EXCHANGE_TITLE}",
        f"ставка <b>{fmt_rate(rate)}</b>",
        f"объём {vol_txt}",
        f"начисление в {hhmm} (через {_time_left(next_ms, now_ms)})",
    ]
    if url:
        lines += ["", f'<a href="{url}">Открыть на {EXCHANGE_TITLE}</a>']

    return "\n".join(lines)


async def radar_users(
    users: dict,
    items: list,
    now_ts: int,
    send,
    marks: dict,
    mark_sent,
    is_blocked=None,
    on_blocked=None,
) -> dict:
    """Обходит пользователей и шлёт то, что попало на радар.

    marks      — {(user_id, symbol): время последнего оповещения}
    mark_sent  — (user_id, symbol, ts) -> None, запись отметки насквозь в базу
    """
    stats = {"sent": [], "failed": [], "blocked": []}
    now_ms = now_ts * 1000

    for uid, s in list(users.items()):
        try:
            if not s.notify_enabled:
                continue

            last_sent = {sym: ts for (u, sym), ts in marks.items() if u == uid}
            hits = select_hits(items, s, now_ts, last_sent)
            if not hits:
                continue

            tz = timezone(timedelta(hours=int(s.utc_offset_hours)))
            sent_any = False

            for item in hits:
                sym = (item.get("symbol") or "").strip().upper()
                await send(uid, build_alert(item, s, tz, now_ms))
                # отметка ставится только после удачной отправки
                marks[(uid, sym)] = now_ts
                mark_sent(uid, sym, now_ts)
                sent_any = True
                log.info(
                    "radar sent uid=%s %s %.2f%% vol=%.0f",
                    uid, sym, float(item.get("funding_rate", 0.0)), float(item.get("vol_usdt_24h") or 0.0),
                )

            if sent_any:
                stats["sent"].append(uid)

        except Exception as e:
            if is_blocked is not None and is_blocked(e):
                stats["blocked"].append(uid)
                log.info("radar blocked uid=%s — выключаю уведомления", uid)
                if on_blocked is not None:
                    on_blocked(uid, s)
                continue

            stats["failed"].append(uid)
            log.exception("radar failed uid=%s: %r", uid, e)

    return stats
