"""OKX SWAP.

Особенность биржи: фандинг отдаётся ТОЛЬКО поштучно — ни батч по нескольким instId,
ни выборка по instType не поддерживаются (проверено на живом API: батч отвечает
"Parameter instId error"). Поэтому качалка разделена надвое:

  fetch_prices()   — списком: инструменты, объёмы, mark-цены. Дёшево, можно часто.
  fetch_funding()  — поштучно по отобранным инструментам. Дорого, реже.

Отбор монет делает select_instruments() по объёму. Раньше здесь стоял срез
inst_ids[:350], из-за которого 113 монет из 463 не попадали в бота вообще.
"""

import asyncio
from datetime import datetime, timezone

import httpx

from .types import to_percent, to_int_ms, make_okx_link, okx_instid_to_symbol

INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
TICKERS_URL = "https://www.okx.com/api/v5/market/tickers"
MARK_PRICE_URL = "https://www.okx.com/api/v5/public/mark-price"
FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"

FUNDING_CONCURRENCY = 10


def select_instruments(inst_ids: list, vol_map: dict, min_vol_usdt: float) -> list:
    """Оставляет инструменты, объём которых дотягивает до порога.

    Монета без известного объёма отбрасывается: она всё равно не пройдёт
    пользовательский фильтр объёма.
    """
    out = []
    for inst_id in inst_ids:
        sym = okx_instid_to_symbol(inst_id)
        if not sym:
            continue
        vol = vol_map.get(sym)
        if vol is None:
            continue
        if float(vol) < float(min_vol_usdt):
            continue
        out.append(inst_id)
    return out


async def fetch_prices(client: httpx.AsyncClient) -> dict:
    """Списком: список инструментов, объёмы и mark-цены."""
    r = await client.get(INSTRUMENTS_URL, params={"instType": "SWAP"}, timeout=10)
    r.raise_for_status()
    data = r.json().get("data", []) or []

    inst_ids = [
        it.get("instId", "")
        for it in data
        if (it.get("instId") or "").endswith("-USDT-SWAP")
    ]

    vol_map = {}
    r_tick = await client.get(TICKERS_URL, params={"instType": "SWAP"}, timeout=10)
    r_tick.raise_for_status()
    for t in (r_tick.json().get("data") or []):
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        sym = okx_instid_to_symbol(inst_id)
        if not sym:
            continue
        try:
            vol_map[sym] = float(t.get("volCcy24h")) * float(t.get("last"))
        except Exception:
            continue

    mark_map = {}
    try:
        r_mp = await client.get(MARK_PRICE_URL, params={"instType": "SWAP"}, timeout=10)
        r_mp.raise_for_status()
        for t in (r_mp.json().get("data") or []):
            inst_id = t.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue
            sym = okx_instid_to_symbol(inst_id)
            if not sym:
                continue
            mp = t.get("markPx")
            if mp is None:
                continue
            try:
                mark_map[sym] = float(mp)
            except Exception:
                continue
    except Exception:
        mark_map = {}

    return {"inst_ids": inst_ids, "vol": vol_map, "mark": mark_map}


async def fetch_funding(client: httpx.AsyncClient, inst_ids: list) -> dict:
    """Поштучный опрос фандинга. Сбой по одной монете не роняет остальные."""
    out = {}
    sem = asyncio.Semaphore(FUNDING_CONCURRENCY)

    async def one(inst_id: str):
        async with sem:
            try:
                rr = await client.get(FUNDING_URL, params={"instId": inst_id}, timeout=10)
                rr.raise_for_status()
                d = (rr.json().get("data", []) or [])
                if not d:
                    return

                rec = d[0]
                funding_rate = rec.get("fundingRate")
                if funding_rate is None:
                    return

                ft_ms = to_int_ms(rec.get("fundingTime"))
                next_ft_ms = to_int_ms(rec.get("nextFundingTime"))

                now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
                cands = [t for t in (ft_ms, next_ft_ms) if t and t >= now_ms]
                next_ms = min(cands) if cands else (next_ft_ms or ft_ms)

                sym = okx_instid_to_symbol(inst_id)
                if not sym:
                    return

                out[sym] = {
                    "funding_pct": to_percent(funding_rate),
                    "next_ms": next_ms,
                    "link": make_okx_link(inst_id),
                }
            except Exception:
                # одна плохая монета не должна ломать весь проход
                return

    await asyncio.gather(*(one(i) for i in inst_ids))
    return out


def compose(prices: dict, funding: dict) -> dict:
    """Собирает записи Rec из ценовой части и фандинговой."""
    vol_map = prices.get("vol") or {}
    mark_map = prices.get("mark") or {}

    out = {}
    for sym, f in funding.items():
        out[sym] = {
            "exchange": "OKX",
            "funding_pct": f["funding_pct"],
            "next_ms": f["next_ms"],
            "mark_px": float(mark_map.get(sym, 0.0)),
            "link": f["link"],
            "vol_usdt_24h": float(vol_map.get(sym, 0.0)),
        }
    return out


async def fetch(client: httpx.AsyncClient, min_vol_usdt: float = 0.0) -> dict:
    """Полный проход одним вызовом — им пользуется сканер."""
    prices = await fetch_prices(client)
    inst_ids = select_instruments(prices["inst_ids"], prices["vol"], min_vol_usdt)
    funding = await fetch_funding(client, inst_ids)
    return compose(prices, funding)
