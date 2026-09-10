"""Форматирование строк сообщений бота.

Здесь только чистые функции без сети и состояния — их покрывают тесты.
Вид строки менять нельзя: пользователи к нему привыкли.
"""

import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

MSK = timezone(timedelta(hours=3))

EX_NAME = {
    "BINANCE": "Binance",
    "BYBIT": "Bybit",
    "OKX": "OKX",
    "GATE": "Gate",
    "MEXC": "MEXC",
    "BITGET": "Bitget",
}

# биржи, для которых в строке показывается разница цены с Binance
DELTA_EXCHANGES = {"BYBIT", "OKX", "GATE"}

LATIN_SYMBOL_RE = re.compile(r"^[A-Z0-9._-]+$")

RATE_W = 7   # "-12.34%" помещается
TIME_W = 5   # "03:00"
VOL_W = 6


def fmt_coin(sym: str) -> str:
    return (sym or "").replace("USDT", "")


def fmt_rate(fr: float) -> str:
    # без плюса, но знак минус сохраняем
    return f"{fr:+.2f}%".replace("+", "")


def is_clean_symbol(sym: str) -> bool:
    """Убираем не-латиницу (иероглифы и любой мусор)."""
    if not sym:
        return False
    return bool(LATIN_SYMBOL_RE.match(sym.upper().strip()))


def calc_coin_width(items: list, min_w: int = 4, max_w: int = 10) -> int:
    """Ширина колонки = длина самого длинного названия монеты в сообщении."""
    coins = [fmt_coin(x.get("symbol", "")) for x in items]
    w = max((len(c) for c in coins), default=min_w)
    return max(min_w, min(w, max_w))


def _to_positive_float_or_none(v) -> Optional[float]:
    try:
        num = float(v)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def build_binance_price_map(items: list) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for x in items:
        if ((x.get("exchange") or "").strip().upper()) != "BINANCE":
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
    if ex_u == "BINANCE" or ex_u not in DELTA_EXCHANGES:
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
    ex_u = (x.get("exchange") or "").strip().upper()
    url = x.get("url", "")
    next_ms = int(x.get("next_funding_ms") or 0)
    fr = float(x.get("funding_rate", 0.0))
    vol = float(x.get("vol_usdt_24h") or 0.0)
    mark_px = _to_positive_float_or_none(x.get("mark_px"))

    vol_m_txt = f"{vol/1e6:.1f}M".replace(".", ",") if vol > 0 else "--"
    hhmm = (
        datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%H:%M")
        if next_ms
        else "--:--"
    )

    # 🟢 отрицательный, 🔴 положительный
    dot = "🟢" if fr < 0 else "🔴"

    coin_col = fmt_coin(sym)[:coin_w].ljust(coin_w)
    rate_col = fmt_rate(fr)[:RATE_W].rjust(RATE_W)
    time_col = hhmm.ljust(TIME_W)
    vol_col = vol_m_txt[:VOL_W].rjust(VOL_W)

    mono = f"{coin_col} {rate_col} {time_col} {vol_col}"

    if ex_u == "BINANCE":
        link_html = f' <a href="{url}">BIN</a>' if url else ""
        return f"{dot} <code>{mono}</code>{link_html}"

    if ex_u in DELTA_EXCHANGES:
        delta_txt = format_delta_vs_binance(ex_u, (sym or "").strip().upper(), mark_px, binance_prices or {})
        delta_label = f"Δ{delta_txt}" if delta_txt is not None else "ΔНЕТ"
        delta_html = f' <a href="{url}">{delta_label}</a>' if url else f" {delta_label}"
        return f"{dot} <code>{mono}</code>{delta_html}"

    return f"{dot} <code>{mono}</code>"


def split_html_chunks(text: str, limit: int) -> list:
    """Режет длинный HTML-текст на части по границам строк.

    Telegram отбивает сообщения длиннее 4096 символов, а рвать текст посреди
    <code> или <a> нельзя — поэтому режем только между строками. Строка, которая
    сама длиннее лимита, уезжает отдельным куском: потерять её хуже, чем отправить
    длинной.
    """
    chunks = []
    buf = ""

    for ln in text.split("\n"):
        candidate = f"{buf}{ln}\n"
        if len(candidate) > limit and buf:
            chunks.append(buf.rstrip())
            buf = ""
        buf += ln + "\n"

    if buf.strip():
        chunks.append(buf.rstrip())

    return [c for c in chunks if c.strip()]
