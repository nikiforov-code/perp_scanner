"""Общие помощники для качалок бирж.

Запись одной монеты (Rec) — обычный dict, ключ во внешнем словаре — символ вида BTCUSDT:

    {
        "exchange":      "BINANCE",
        "funding_pct":   -0.47,          # ставка в процентах, не в долях
        "next_ms":       1789041600000,  # время следующего начисления, unix ms
        "mark_px":       64123.5,
        "vol_usdt_24h":  982_000_000.0,
        "link":          "https://...",
    }

Формат зафиксирован: на него опираются и бот, и сканер.
"""


def to_percent(rate_str) -> float:
    """Ставка приходит долей (0.0001) — переводим в проценты."""
    return float(rate_str) * 100.0


def to_int_ms(v) -> int:
    try:
        if v is None:
            return 0
        s = str(v).strip()
        if not s:
            return 0
        return int(float(s))
    except Exception:
        return 0


def make_binance_link(symbol: str) -> str:
    return f"https://www.binance.com/en/futures/{symbol}"


def make_bybit_link(symbol: str) -> str:
    return f"https://www.bybit.com/trade/usdt/{symbol}"


def make_okx_link(inst_id: str) -> str:
    # OKX использует формат типа BTC-USDT-SWAP
    return f"https://www.okx.com/trade-swap/{inst_id}"


def make_gate_link(contract: str) -> str:
    # пример: BTC_USDT -> страница фьючерса Gate
    return f"https://www.gate.com/futures/USDT/{contract}"


def okx_instid_to_symbol(inst_id: str) -> str:
    # BTC-USDT-SWAP -> BTCUSDT
    parts = inst_id.split("-")
    if len(parts) >= 2 and parts[1] == "USDT":
        return f"{parts[0]}USDT"
    return ""
