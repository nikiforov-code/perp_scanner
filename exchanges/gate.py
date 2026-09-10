"""Gate.io Futures (USDT settle).

contracts -> funding_rate + funding_next_apply (unix seconds), плюс whitelist
неделистнутых контрактов; tickers -> mark price и объём в USDT.
"""

import time

import httpx

from .types import to_percent, make_gate_link

CONTRACTS_URL = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
FX_CONTRACTS_URL = "https://fx-api.gateio.ws/api/v4/futures/usdt/contracts"
FX_TICKERS_URL = "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers"

ACTIVE_CONTRACTS_TTL = 3 * 60 * 60  # 3 часа
_active_contracts_cache = {"ts": 0.0, "symbols": set()}


async def get_active_contracts(client: httpx.AsyncClient) -> set:
    now = time.time()
    if now - _active_contracts_cache["ts"] < ACTIVE_CONTRACTS_TTL:
        return _active_contracts_cache["symbols"]

    r = await client.get(CONTRACTS_URL, timeout=10)
    r.raise_for_status()
    data = r.json()

    active = set()
    for c in data:
        # active + perpetual + USDT
        if c.get("in_delisting") is False and c.get("name", "").endswith("_USDT"):
            active.add(c["name"].replace("_USDT", ""))

    _active_contracts_cache["ts"] = now
    _active_contracts_cache["symbols"] = active
    return active


async def fetch(client: httpx.AsyncClient) -> dict:
    active_contracts = await get_active_contracts(client)

    r = await client.get(FX_CONTRACTS_URL, timeout=10)
    r.raise_for_status()
    data = r.json()

    r_tick = await client.get(FX_TICKERS_URL, timeout=10)
    r_tick.raise_for_status()
    tickers = r_tick.json()

    vol_map = {}
    mark_map = {}
    for t in tickers:
        name = t.get("contract")  # пример: BTC_USDT
        if not name or not name.endswith("_USDT"):
            continue

        sym = f"{name.replace('_USDT', '')}USDT"

        mp = t.get("mark_price")
        if mp is None:
            mp = t.get("markPrice")
        if mp is None:
            mp = t.get("mark")
        if mp is None:
            mp = t.get("last")  # fallback, если mark нет (лучше чем ничего)

        if mp is not None:
            try:
                mark_map[sym] = float(mp)
            except Exception:
                pass

        qv = t.get("volume_24h_quote")
        if qv is None:
            continue
        try:
            vol_map[sym] = float(qv)
        except Exception:
            continue

    out = {}
    for item in data:
        name = item.get("name")  # пример: BTC_USDT
        if not name or not name.endswith("_USDT"):
            continue

        fr = item.get("funding_rate")
        nft = item.get("funding_next_apply")  # unix seconds
        if fr is None or nft is None:
            continue

        coin = name.replace("_USDT", "")
        if coin not in active_contracts:
            continue

        sym = f"{coin}USDT"
        out[sym] = {
            "exchange": "GATE",
            "funding_pct": to_percent(fr),
            "next_ms": int(nft) * 1000,
            "mark_px": float(mark_map.get(sym, 0.0)),
            "link": make_gate_link(name),
            "vol_usdt_24h": float(vol_map.get(sym, 0.0)),
        }

    return out
