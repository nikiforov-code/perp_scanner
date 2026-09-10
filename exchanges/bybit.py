"""Bybit V5: GET /v5/market/tickers?category=linear

Один запрос отдаёт fundingRate, nextFundingTime, markPrice и turnover24h.
"""

import httpx

from .types import to_percent, make_bybit_link

URL = "https://api.bybit.com/v5/market/tickers"


async def fetch(client: httpx.AsyncClient) -> dict:
    r = await client.get(URL, params={"category": "linear"}, timeout=10)
    r.raise_for_status()
    js = r.json()

    items = (js.get("result", {}) or {}).get("list", []) or []

    out = {}
    for item in items:
        sym = (item.get("symbol") or "").strip()
        if not sym or not sym.endswith("USDT"):
            continue

        fr = item.get("fundingRate")
        nft = item.get("nextFundingTime")
        if fr is None or nft is None:
            continue

        try:
            vol = float(item.get("turnover24h"))
        except (TypeError, ValueError):
            vol = 0.0

        out[sym] = {
            "exchange": "BYBIT",
            "funding_pct": to_percent(fr),
            "next_ms": int(nft),
            "mark_px": float(item.get("markPrice") or 0.0),
            "link": make_bybit_link(sym),
            "vol_usdt_24h": vol,
        }

    return out
