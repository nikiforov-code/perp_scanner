"""Binance USD-M Futures.

1) exchangeInfo -> whitelist реально торгуемых PERPETUAL (status=TRADING)
2) premiumIndex -> funding + nextFundingTime
3) ticker/24hr  -> объём в USDT
"""

import httpx

from .types import to_percent, make_binance_link

BASE = "https://fapi.binance.com"


async def fetch(client: httpx.AsyncClient) -> dict:
    # 1) whitelist TRADING символов
    r1 = await client.get(f"{BASE}/fapi/v1/exchangeInfo", timeout=10)
    r1.raise_for_status()
    info = r1.json()

    tradable = set()
    for s in (info.get("symbols") or []):
        sym = s.get("symbol")
        if not sym or not sym.endswith("USDT"):
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("status") != "TRADING":
            continue
        tradable.add(sym)

    # 2) funding из premiumIndex
    r2 = await client.get(f"{BASE}/fapi/v1/premiumIndex", timeout=10)
    r2.raise_for_status()
    data = r2.json()

    # 3) 24h объёмы (quoteVolume) — для USDT-пар это объём в USDT
    r3 = await client.get(f"{BASE}/fapi/v1/ticker/24hr", timeout=10)
    r3.raise_for_status()
    vol_data = r3.json()

    vol_map = {}
    for v in vol_data:
        sym = v.get("symbol")
        if not sym or sym not in tradable:
            continue
        try:
            vol_map[sym] = float(v.get("quoteVolume"))
        except Exception:
            continue

    out = {}
    for item in data:
        sym = item.get("symbol")
        if not sym or sym not in tradable:
            continue

        out[sym] = {
            "exchange": "BINANCE",
            "funding_pct": to_percent(item["lastFundingRate"]),
            "next_ms": int(item["nextFundingTime"]),
            "mark_px": float(item.get("markPrice") or 0.0),
            "link": make_binance_link(sym),
            "vol_usdt_24h": float(vol_map.get(sym, 0.0)),
        }
    return out
