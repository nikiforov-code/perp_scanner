from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from datetime import datetime, timezone, timedelta
import asyncio
import time
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Funding Spread Scanner")

# =========================
# НАСТРОЙКИ (тут ты меняешь параметры, не трогая код ниже)
# =========================
MSK = timezone(timedelta(hours=3))

REFRESH_SECONDS = 30  # обновление данных (как ты решил)

NEAR_HOURS = 2
NEAR_MAX_MINUTES = NEAR_HOURS * 60

# =========================
# ADMIN (для будущей админки UI)
# =========================
from admin.auth import is_admin
from routes.api import router as api_router

app.include_router(api_router)

# OKX (добавляем аккуратно, чтобы было стабильно)
ENABLE_OKX = True
OKX_MAX_SYMBOLS_PER_REFRESH = 0  # не используется: отбор монет OKX идёт по объёму

# Gate.io
ENABLE_GATE = True

# Bitget
ENABLE_BITGET = True

# BingX
ENABLE_BINGX = True

# KuCoin
ENABLE_KUCOIN = True

# =========================
# UI SETTINGS (SQLite) — для админки UI-фильтров (НЕ влияет на bot endpoints)
# =========================
from admin.ui_settings_store import ui_db_init, ui_get, ui_set

from state import STATE

# Качалки четырёх бирж вынесены в общий пакет — их же использует бот.
from exchanges import binance as ex_binance
from exchanges import bybit as ex_bybit
from exchanges import gate as ex_gate
from exchanges import okx as ex_okx
from exchanges.types import to_percent, to_int_ms, okx_instid_to_symbol

# Сканер опрашивает фандинги OKX поштучно, поэтому берём только ликвидные монеты.
OKX_MIN_VOL_USDT = 1_000_000

def msk_hhmm(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK)
    return dt.strftime("%H:%M")

def minutes_until(ms: int) -> int:
    now = int(time.time() * 1000)
    return max(0, int((ms - now) / 60000))


def make_bitget_link(symbol: str) -> str:
    return f"https://www.bitget.com/futures/usdt/{symbol}"

def make_bingx_link(symbol: str) -> str:
    # BTCUSDT -> https://bingx.com/en/perpetual/BTC-USDT
    if symbol.endswith("USDT"):
        coin = symbol[:-4]
        return f"https://bingx.com/en/perpetual/{coin}-USDT"
    return "https://bingx.com"

def make_kucoin_link(symbol: str) -> str:
    # В KuCoin Futures контракт обычно имеет суффикс M: BTCUSDT -> BTCUSDTM
    if symbol.endswith("USDT"):
        return f"https://www.kucoin.com/futures/trade/{symbol}M"
    return "https://www.kucoin.com/futures"

def get_mode_from_request(request: Request) -> str:
    mode = request.query_params.get("mode", "").strip().lower()
    if mode in ("all", "near"):
        return mode
    return "near"

# --- BITGET contracts cache ---
_BITGET_CONTRACTS_CACHE = {"ts": 0.0, "set": set()}
_BITGET_CONTRACTS_TTL_SEC = 3 * 60 * 60  # 3 часа

async def _get_bitget_usdt_active_symbols(client: httpx.AsyncClient) -> set:
    now = time.time()
    if _BITGET_CONTRACTS_CACHE["set"] and (now - _BITGET_CONTRACTS_CACHE["ts"] < _BITGET_CONTRACTS_TTL_SEC):
        return _BITGET_CONTRACTS_CACHE["set"]

    # Список контрактов (активных) Bitget USDT-M
    url = "https://api.bitget.com/api/v2/mix/market/contracts"
    r = await client.get(url, params={"productType": "usdt-futures"}, timeout=10)
    r.raise_for_status()
    js = r.json()

    data = js.get("data") or []
    active = set()

    for it in data:
        # В разных ответах Bitget бывает:
        # - symbol: "BTCUSDT"
        # - symbol: "BTCUSDT_UMCBL"
        # - baseCoin/quoteCoin
        sym = (it.get("symbol") or "").strip()
        if not sym:
            continue

        # нормализуем к BTCUSDT (без _UMCBL)
        sym_norm = sym.split("_", 1)[0].upper()

        if not sym_norm.endswith("USDT"):
            continue

        # если есть явный статус — берём только активные
        status = it.get("status")
        if status is not None:
            # у Bitget встречаются разные значения, но активные обычно "normal"/"online"/"1"
            s = str(status).lower()
            if s in ("offline", "delisted", "0", "false"):
                continue

        active.add(sym_norm)

    _BITGET_CONTRACTS_CACHE["ts"] = now
    _BITGET_CONTRACTS_CACHE["set"] = active
    return active

async def fetch_bitget(client: httpx.AsyncClient) -> dict:
    """
    Bitget USDT-M funding:
    1) contracts whitelist (активные)
    2) current-fund-rate -> фильтруем только активные
    """
    active = await _get_bitget_usdt_active_symbols(client)

    # --- BITGET 24h volumes (USDT) ---
    vol_map = {}
    mark_map = {}
    try:
        tick_url = "https://api.bitget.com/api/v2/mix/market/tickers"
        r_tick = await client.get(tick_url, params={"productType": "USDT-FUTURES"}, timeout=10)
        r_tick.raise_for_status()
        tick_js = r_tick.json()

        for t in (tick_js.get("data") or []):
            raw = (t.get("symbol") or "").strip()
            if not raw:
                continue
            sym_norm = raw.split("_", 1)[0].upper()
            if not sym_norm.endswith("USDT"):
                continue

            # в документации есть quoteVolume и usdtVolume; для USDT-M логичнее usdtVolume
            v = t.get("usdtVolume") or t.get("quoteVolume")
            if v is None:
                continue
            try:
                vol_map[sym_norm] = float(v)
            except Exception:
                continue

            px = t.get("markPrice") or t.get("markPx") or t.get("lastPr") or t.get("last") or t.get("price")
            if px is None:
                continue
            try:
                mark_map[sym_norm] = float(px)
            except Exception:
                continue
    except Exception:
        # объём — не критичный, не ломаем биржу если тикеры временно недоступны
        vol_map = {}
        mark_map = {}

    url = "https://api.bitget.com/api/v2/mix/market/current-fund-rate"
    r = await client.get(url, params={"productType": "usdt-futures"}, timeout=10)
    r.raise_for_status()
    js = r.json()

    data = js.get("data", []) or []
    out = {}

    for item in data:
        raw_sym = (item.get("symbol") or "").strip()
        if not raw_sym:
            continue

        sym = raw_sym.split("_", 1)[0].upper()
        if not sym.endswith("USDT"):
            continue

        # ключевая фильтрация: только реально активные контракты
        if sym not in active:
            continue

        fr = item.get("fundingRate")
        nu = item.get("nextUpdate")  # ms
        if fr is None or nu is None:
            continue

        out[sym] = {
            "exchange": "BITGET",
            "funding_pct": to_percent(fr),
            "next_ms": int(nu),
            "link": make_bitget_link(sym),
            "mark_px": float(mark_map.get(sym, 0.0)),
            "vol_usdt_24h": float(vol_map.get(sym, 0.0)),
        }

    return out

# --- BINGX contracts cache ---
_BINGX_CONTRACTS_CACHE = {"ts": 0.0, "set": set()}
_BINGX_CONTRACTS_TTL_SEC = 3 * 60 * 60  # 3 часа

async def _get_bingx_usdt_active_symbols(client: httpx.AsyncClient) -> set:
    now = time.time()
    if _BINGX_CONTRACTS_CACHE["set"] and (now - _BINGX_CONTRACTS_CACHE["ts"] < _BINGX_CONTRACTS_TTL_SEC):
        return _BINGX_CONTRACTS_CACHE["set"]

    # Список контрактов BingX Swap (USDT Perpetual)
    url = "https://open-api.bingx.com/openApi/swap/v2/quote/contracts"
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    js = r.json()

    data = js.get("data")
    if isinstance(data, dict):
        data = data.get("list")

    if not isinstance(data, list):
        # если API неожиданно поменялся — не режем всё, просто вернём пустой set
        _BINGX_CONTRACTS_CACHE["ts"] = now
        _BINGX_CONTRACTS_CACHE["set"] = set()
        return set()

    active = set()
    for it in data:
        sym = (it.get("symbol") or it.get("s") or "").strip()
        if not sym:
            continue

        # обычно "BTC-USDT" -> BTCUSDT
        sym_norm = sym.replace("-", "").upper()
        if not sym_norm.endswith("USDT"):
            continue

        # если есть статус/флаг — оставляем только активные (мягко)
        status = it.get("status") or it.get("st")
        if status is not None:
            s = str(status).lower()
            if s in ("0", "false", "offline", "delisted", "suspend", "closed"):
                continue

        active.add(sym_norm)

    _BINGX_CONTRACTS_CACHE["ts"] = now
    _BINGX_CONTRACTS_CACHE["set"] = active
    return active

async def fetch_bingx(client: httpx.AsyncClient) -> dict:
    """
    BingX Swap V2 (public):
    GET https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex
    Обычно возвращает список по всем контрактам с funding и временем следующего funding.
    """
    active = await _get_bingx_usdt_active_symbols(client)

    # --- BINGX 24h volumes (USDT turnover) ---
    vol_map = {}
    mark_map = {}
    try:
        tick_url = "https://open-api.bingx.com/openApi/swap/v2/quote/ticker"
        r_tick = await client.get(tick_url, timeout=10)
        r_tick.raise_for_status()
        tick_js = r_tick.json()

        tdata = tick_js.get("data")
        if isinstance(tdata, dict):
            tdata = tdata.get("list")
        if not isinstance(tdata, list):
            tdata = []

        for t in tdata:
            raw = (t.get("symbol") or t.get("s") or "").strip()
            if not raw:
                continue

            # "BTC-USDT" -> "BTCUSDT"
            sym_norm = raw.replace("-", "").upper()
            if not sym_norm.endswith("USDT"):
                continue

            # у BingX turnover в USDT обычно в поле quoteVolume
            qv = t.get("quoteVolume") or t.get("quote_volume") or t.get("q")
            if qv is None:
                continue

            try:
                vol_map[sym_norm] = float(qv)
            except Exception:
                continue
            px = (
                t.get("markPrice")
                or t.get("mark_price")
                or t.get("lastPrice")
                or t.get("last")
                or t.get("close")
                or t.get("c")
            )
            if px is not None:
                try:
                    mark_map[sym_norm] = float(px)
                except Exception:
                    pass
    except Exception:
        vol_map = {}
        mark_map = {}

    url = "https://open-api.bingx.com/openApi/swap/v2/quote/premiumIndex"
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    js = r.json()

    # В разных версиях/обёртках может быть data как list или dict с list
    data = js.get("data")
    if isinstance(data, dict):
        # иногда бывает {"data": {"list": [...]}}
        data = data.get("list")

    if not isinstance(data, list):
        return {}

    out = {}
    for item in data:
        sym = item.get("symbol") or item.get("s")
        if not sym:
            continue

        # BingX часто: "BTC-USDT" (с дефисом). Приводим к "BTCUSDT"
        sym_norm = sym.replace("-", "").upper()

        if not sym_norm.endswith("USDT"):
            continue

        if active and sym_norm not in active:
            continue

        fr = (
            item.get("lastFundingRate")
            or item.get("fundingRate")
            or item.get("funding_rate")
            or item.get("r")
        )
        nft = (
            item.get("nextFundingTime")
            or item.get("nextFundingTimestamp")
            or item.get("nextFundingTimeStamp")
            or item.get("next_funding_time")
            or item.get("T")
        )

        if fr is None or nft is None:
            continue

        # next funding time: бывает ms, бывает sec — нормализуем в ms
        try:
            t = int(float(nft))
            if t < 10_000_000_000:  # похоже на секунды
                t = t * 1000
        except Exception:
            continue

        out[sym_norm] = {
            "exchange": "BINGX",
            "funding_pct": to_percent(fr),
            "next_ms": t,
            "link": make_bingx_link(sym_norm),
            "mark_px": float(mark_map.get(sym_norm, 0.0)),
            "vol_usdt_24h": float(vol_map.get(sym_norm, 0.0)),
        }

    return out

# --- KUCOIN contracts cache ---
_KUCOIN_CONTRACTS_CACHE = {"ts": 0.0, "set": set()}
_KUCOIN_CONTRACTS_TTL_SEC = 3 * 60 * 60  # 3 часа

async def _get_kucoin_active_contracts(client: httpx.AsyncClient) -> set:
    """
    KuCoin Futures: active contracts list.
    GET https://api-futures.kucoin.com/api/v1/contracts/active
    Returns a set of contract symbols like {"XBTUSDTM", "ETHUSDTM", ...}
    """
    now = time.time()
    if _KUCOIN_CONTRACTS_CACHE["set"] and (now - _KUCOIN_CONTRACTS_CACHE["ts"] < _KUCOIN_CONTRACTS_TTL_SEC):
        return _KUCOIN_CONTRACTS_CACHE["set"]

    url = "https://api-futures.kucoin.com/api/v1/contracts/active"
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    js = r.json()

    active = set()
    data = js.get("data") or []
    for it in data:
        sym = (it.get("symbol") or "").strip().upper()  # e.g. XBTUSDTM
        if sym:
            active.add(sym)

    _KUCOIN_CONTRACTS_CACHE["ts"] = now
    _KUCOIN_CONTRACTS_CACHE["set"] = active
    return active

async def fetch_kucoin_funding(client: httpx.AsyncClient, coins: list[str]) -> dict:
    """
    KuCoin Unified API (public):
    GET https://api.kucoin.com/api/ua/v1/market/funding-rate?symbol=...
    Важно: не даём одной плохой монете ломать весь refresh_loop.
    """
    out = {}
    sem = asyncio.Semaphore(8)
    active_contracts = await _get_kucoin_active_contracts(client)

    # --- KUCOIN 24h volumes (USDT turnover) ---
    vol_map = {}
    mark_map = {}
    try:
        # KuCoin Futures: active contracts include 24h turnover
        tick_url = "https://api-futures.kucoin.com/api/v1/contracts/active"
        r_tick = await client.get(tick_url, timeout=10)
        r_tick.raise_for_status()
        tick_js = r_tick.json()

        data = tick_js.get("data") or []
        if not isinstance(data, list):
            data = []

        for t in data:
            raw = (t.get("symbol") or "").strip().upper()  # например: XBTUSDTM
            if not raw:
                continue

            # "XBTUSDTM" -> "XBTUSDT"
            sym_norm = raw.replace("USDTM", "USDT")
            if not sym_norm.endswith("USDT"):
                continue

            tv = t.get("turnoverOf24h")  # 24h turnover (USDT)
            if tv is None:
                continue

            try:
                vol_map[sym_norm] = float(tv)
            except Exception:
                continue
            px = t.get("markPrice") or t.get("indexPrice") or t.get("lastTradePrice")
            if px is not None:
                try:
                    mark_map[sym_norm] = float(px)
                except Exception:
                    pass
    except Exception:
        vol_map = {}

    def is_valid_coin(coin: str) -> bool:
        # KuCoin контракты ожидают латиницу/цифры (типа BTC, XBT, 1000PEPE и т.п.)
        # Пропускаем любые экзотические/иероглифы/пробелы.
        if not coin:
            return False
        for ch in coin:
            if not (("A" <= ch <= "Z") or ("0" <= ch <= "9")):
                return False
        return True

    # минимальный маппинг тикеров KuCoin (BTC -> XBT)
    def to_kucoin_coin(coin: str) -> str:
        if coin == "BTC":
            return "XBT"
        return coin

    async def one(coin: str):
        async with sem:
            try:
                coin = (coin or "").upper().strip()
                if not is_valid_coin(coin):
                    return

                kc_coin = to_kucoin_coin(coin)
                contract = f"{kc_coin}USDTM"

                if contract not in active_contracts:
                    return


                url = "https://api.kucoin.com/api/ua/v1/market/funding-rate"
                r = await client.get(url, params={"symbol": contract}, timeout=10)
                r.raise_for_status()
                js = r.json()

                data = js.get("data") or {}
                fr = data.get("nextFundingRate")
                ft = data.get("fundingTime")  # ms
                if fr is None or ft is None:
                    return

                sym = f"{coin}USDT"
                out[sym] = {
                    "exchange": "KUCOIN",
                    "funding_pct": float(fr) * 100.0,
                    "next_ms": int(ft),
                    "link": make_kucoin_link(sym),
                    "mark_px": float(mark_map.get(f"{kc_coin}USDT", 0.0)),
                    "vol_usdt_24h": float(vol_map.get(f"{kc_coin}USDT", 0.0)),
                }

            except Exception:
                # Любая ошибка по конкретной монете — просто пропускаем,
                # чтобы не ломать весь refresh loop.
                return

    await asyncio.gather(*(one(c) for c in coins))
    return out

# --- MEXC contracts cache ---
_MEXC_CONTRACTS_CACHE = {"ts": 0.0, "map": {}}
_MEXC_CONTRACTS_TTL_SEC = 2 * 60 * 60  # 2 часа


async def _get_mexc_usdt_symbol_map(client: httpx.AsyncClient) -> dict:
    now = time.time()
    if _MEXC_CONTRACTS_CACHE["map"] and (now - _MEXC_CONTRACTS_CACHE["ts"] < _MEXC_CONTRACTS_TTL_SEC):
        return _MEXC_CONTRACTS_CACHE["map"]

    url = "https://contract.mexc.com/api/v1/contract/detail"
    r = await client.get(url, timeout=10)
    r.raise_for_status()
    js = r.json()

    mp = {}
    data = js.get("data") or []

    for it in data:
        sym = (it.get("symbol") or "").strip()          # "BTC_USDT", "LUNA2_USDT"
        if not sym or "_USDT" not in sym:
            continue

        quote = (it.get("quoteCoin") or "").strip().upper()
        if quote and quote != "USDT":
            continue

        # state у MEXC может быть разным по версиям API (0/1/2/...) — НЕ режем по нему жёстко
        # но если есть явный флаг "offline"/"delisted", тогда можно резать — тут его нет, поэтому пропускаем.

        base = (it.get("baseCoin") or "").strip().upper()
        if not base:
            base = sym.split("_", 1)[0].upper()

        sym = sym.upper()

        # записываем первую встреченную версию
        if base and base not in mp:
            mp[base] = sym

    _MEXC_CONTRACTS_CACHE["ts"] = now
    _MEXC_CONTRACTS_CACHE["map"] = mp
    return mp

async def fetch_mexc_funding(client: httpx.AsyncClient, symbols: list[str]) -> dict:
    out = {}
    sem = asyncio.Semaphore(8)

    mexc_map = await _get_mexc_usdt_symbol_map(client)

    # --- MEXC 24h volumes (USDT turnover) ---
    vol_map = {}
    mark_map = {}
    try:
        tick_url = "https://contract.mexc.com/api/v1/contract/ticker"
        r_tick = await client.get(tick_url, timeout=10)
        r_tick.raise_for_status()
        tick_js = r_tick.json()

        data = tick_js.get("data")
        # В зависимости от режима API "data" может быть dict (если один символ) или list (если все)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            data = []

        for t in data:
            raw = (t.get("symbol") or "").strip().upper()  # пример: "BTC_USDT"
            if not raw or "_USDT" not in raw:
                continue

            # "BTC_USDT" -> "BTCUSDT"
            sym_norm = raw.replace("_", "")

            a24 = t.get("amount24")  # 24h turnover (USDT)
            if a24 is None:
                continue
            try:
                vol_map[sym_norm] = float(a24)
            except Exception:
                continue
            px = (
                t.get("markPrice")
                or t.get("fairPrice")
                or t.get("lastPrice")
                or t.get("last")
                or t.get("indexPrice")
            )
            if px is None:
                continue
            try:
                mark_map[sym_norm] = float(px)
            except Exception:
                continue            
    except Exception:
        vol_map = {}
        mark_map = {}

    def resolve_mexc_symbol(coin: str) -> str | None:
        coin = coin.upper()

        if coin in mexc_map:
            return mexc_map[coin]

        # COIN2, COIN3 и т.п.
        for d in ("2", "3", "4", "5"):
            if coin + d in mexc_map:
                return mexc_map[coin + d]

        # любые версии вида COIN<digits>
        for base, sym in mexc_map.items():
            if base.startswith(coin) and base[len(coin):].isdigit():
                return sym

        return None

    async def one(coin: str):
        async with sem:
            mexc_symbol = resolve_mexc_symbol(coin)
            if not mexc_symbol:
                return

            url = f"https://contract.mexc.com/api/v1/contract/funding_rate/{mexc_symbol}"
            r = await client.get(url, timeout=10)
            r.raise_for_status()
            j = r.json()

            if not j.get("success"):
                return

            data = j.get("data") or {}
            rate = data.get("fundingRate")
            next_ts = data.get("nextSettleTime")
            if rate is None or next_ts is None:
                return

            sym = f"{coin}USDT"
            out[sym] = {
                "exchange": "MEXC",
                "funding_pct": to_percent(rate),
                "next_ms": int(next_ts),
                "link": f"https://www.mexc.com/futures/{mexc_symbol}",
                "mark_px": float(mark_map.get(sym, 0.0)),
                "vol_usdt_24h": float(vol_map.get(sym, 0.0)),
            }

    await asyncio.gather(*(one(c) for c in symbols))
    return out


from builders.funding_rows import build_rows

async def safe_fetch(name: str, coro):
    try:
        data = await coro
        return {"ok": True, "data": data, "err": None}
    except Exception as e:
        return {"ok": False, "data": {}, "err": repr(e)}

async def refresh_loop():
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, pool=60.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    ) as client:

        while True:
            try:
                tasks = [
                    safe_fetch("BINANCE", ex_binance.fetch(client)),
                    safe_fetch("BYBIT", ex_bybit.fetch(client)),
                ]
                names = ["BINANCE", "BYBIT"]

                if ENABLE_OKX:
                    tasks.append(safe_fetch("OKX", ex_okx.fetch(client, OKX_MIN_VOL_USDT)))
                    names.append("OKX")

                if ENABLE_GATE:
                    tasks.append(safe_fetch("GATE", ex_gate.fetch(client)))
                    names.append("GATE")

                if ENABLE_BITGET:
                    tasks.append(safe_fetch("BITGET", fetch_bitget(client)))
                    names.append("BITGET")

                if ENABLE_BINGX:
                    tasks.append(safe_fetch("BINGX", fetch_bingx(client)))
                    names.append("BINGX")

                results = await asyncio.gather(*tasks)

                # берём прошлое состояние, чтобы не терять данные при ошибках
                prev_per_exchange = STATE.get("per_exchange", {}) or {}
                prev_ex_status = STATE.get("ex_status", {}) or {}

                per_exchange = dict(prev_per_exchange)  # стартуем с прошлого снапшота
                sources = {}
                ex_status = dict(prev_ex_status)

                now_ts = time.time()

                for name, res in zip(names, results):
                    if not isinstance(res, dict):
                        # защита от неожиданных значений (bool / None / etc)
                        res = {"ok": False, "data": {}, "err": f"invalid result type: {type(res)}"}

                    ok = bool(res.get("ok"))
                    data = res.get("data") or {}
                    err = res.get("err")

                    if ok and isinstance(data, dict) and len(data) > 0:
                        # Успешно обновили биржу — записываем новые данные
                        per_exchange[name] = data
                        ex_status[name] = {
                            "ok": True,
                            "stale": False,
                            "err": None,
                            "updated_ts": now_ts,
                        }
                        sources[name] = len(data)
                    else:
                        # Ошибка или пустой ответ — НЕ затираем прошлые данные
                        # Если прошлых данных нет — оставим пусто, но отметим stale
                        prev_data = prev_per_exchange.get(name) or {}
                        per_exchange[name] = prev_data

                        ex_status[name] = {
                            "ok": False,
                            "stale": True,
                            "err": err or "empty response",
                            "updated_ts": ex_status.get(name, {}).get("updated_ts", 0.0),
                        }
                        sources[name] = len(prev_data)

                # --- candidates: монеты, которые есть минимум на 2 биржах ---
                symbol_to_exchanges = {}

                for ex_name, ex_map in per_exchange.items():
                    if not isinstance(ex_map, dict):
                        continue

                    for sym in ex_map.keys():
                        if sym.endswith("USDT"):
                            coin = sym.replace("USDT", "")
                            symbol_to_exchanges.setdefault(coin, set()).add(ex_name)

                candidates = [coin for coin, exs in symbol_to_exchanges.items() if len(exs) >= 2]
                # сортировка candidates по "текущему спреду funding" среди уже загруженных бирж
                def current_spread(coin: str) -> float:
                    sym = f"{coin}USDT"
                    rates = []
                    for ex_map in per_exchange.values():
                        if not isinstance(ex_map, dict):
                            continue
                        rec = ex_map.get(sym)
                        if rec and ("funding_pct" in rec):
                            rates.append(rec["funding_pct"])
                    if len(rates) < 2:
                        return -1e9
                    return max(rates) - min(rates)

                candidates.sort(key=current_spread, reverse=True)

                # защитный лимит (теперь это НЕ рандом, а топ по спреду)
                candidates = candidates[:300]

                # --- KUCOIN (по тем же кандидатам, что и MEXC) ---
                if ENABLE_KUCOIN:
                    res = await safe_fetch("KUCOIN", fetch_kucoin_funding(client, candidates))
                    if not isinstance(res, dict):
                        res = {"ok": False, "data": {}, "err": f"invalid result type: {type(res)}"}

                    ok = bool(res.get("ok"))
                    data = res.get("data") or {}
                    err = res.get("err")

                    if ok and isinstance(data, dict) and len(data) > 0:
                        per_exchange["KUCOIN"] = data
                        ex_status["KUCOIN"] = {"ok": True, "stale": False, "err": None, "updated_ts": now_ts}
                        sources["KUCOIN"] = len(data)
                    else:
                        prev_data = prev_per_exchange.get("KUCOIN") or {}
                        per_exchange["KUCOIN"] = prev_data
                        ex_status["KUCOIN"] = {
                            "ok": False,
                            "stale": True,
                            "err": err or "empty response",
                            "updated_ts": ex_status.get("KUCOIN", {}).get("updated_ts", 0.0),
                        }
                        sources["KUCOIN"] = len(prev_data)

                coins_for_mexc = candidates

                # --- MEXC ---
                res = await safe_fetch("MEXC", fetch_mexc_funding(client, coins_for_mexc))
                if not isinstance(res, dict):
                    res = {"ok": False, "data": {}, "err": f"invalid result type: {type(res)}"}

                ok = bool(res.get("ok"))
                data = res.get("data") or {}
                err = res.get("err")

                if ok and isinstance(data, dict) and len(data) > 0:
                    per_exchange["MEXC"] = data
                    ex_status["MEXC"] = {"ok": True, "stale": False, "err": None, "updated_ts": now_ts}
                    sources["MEXC"] = len(data)
                else:
                    prev_data = prev_per_exchange.get("MEXC") or {}
                    per_exchange["MEXC"] = prev_data
                    ex_status["MEXC"] = {
                        "ok": False,
                        "stale": True,
                        "err": err or "empty response",
                        "updated_ts": ex_status.get("MEXC", {}).get("updated_ts", 0.0),
                    }
                    sources["MEXC"] = len(prev_data)

                rows = build_rows(per_exchange)

                STATE["rows"] = rows
                STATE["per_exchange"] = per_exchange
                STATE["updated_ts"] = time.time()
                STATE["error"] = None
                STATE["sources"] = sources
                STATE["ex_status"] = ex_status
            except Exception as e:
                STATE["error"] = repr(e)

            await asyncio.sleep(REFRESH_SECONDS)

@app.on_event("startup")
async def on_startup():
    ui_db_init()
    asyncio.create_task(refresh_loop())


def _normalize_price_spread_symbol(raw: str) -> str:
    s = (raw or "").strip().upper()
    if not s:
        return ""
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s


@app.get("/api/bot/price_spread")
def api_bot_price_spread(symbol: str = Query(...)):
    sym = _normalize_price_spread_symbol(symbol)
    if not sym:
        return JSONResponse({"detail": "invalid symbol"}, status_code=404)

    pe = STATE.get("per_exchange") or {}
    bn_map = pe.get("BINANCE") or {}
    by_map = pe.get("BYBIT") or {}
    a_rec = bn_map.get(sym)
    b_rec = by_map.get(sym)
    if not a_rec or not b_rec:
        return JSONResponse(
            {"detail": "symbol not found on Binance/Bybit or no data yet"},
            status_code=404,
        )

    try:
        binance_price = float(a_rec["mark_px"])
        bybit_price = float(b_rec["mark_px"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"detail": "mark_px missing"}, status_code=404)

    if binance_price <= 0:
        return JSONResponse({"detail": "invalid binance price"}, status_code=404)

    spread_pct = ((binance_price / bybit_price) - 1) * 100
    return {
        "symbol": sym,
        "binance_price": binance_price,
        "bybit_price": bybit_price,
        "spread_pct": spread_pct,
    }


@app.get("/api/admin/ui_settings")
def api_admin_ui_settings(request: Request):
    if not is_admin(request):
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    return {
        "ui": {
            "min_vol_usdt": ui_get("min_vol_usdt"),
            "min_spread_timing_yes": ui_get("min_spread_timing_yes"),
            "min_spread_timing_no": ui_get("min_spread_timing_no"),
            "min_price_spread_neg": ui_get("min_price_spread_neg"),
        }
    }

@app.post("/api/admin/ui_settings")
async def api_admin_ui_settings_set(request: Request):
    # защита токеном
    if not is_admin(request):
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "invalid json"}, status_code=400)

    updated = {}

    if "min_vol_usdt" in payload:
        try:
            v = float(payload["min_vol_usdt"])
            if v < 0:
                raise ValueError
            ui_set("min_vol_usdt", v)
            updated["min_vol_usdt"] = v
        except Exception:
            return JSONResponse({"detail": "invalid min_vol_usdt"}, status_code=400)

    if "min_spread_timing_yes" in payload:
        try:
            v = float(payload["min_spread_timing_yes"])
            ui_set("min_spread_timing_yes", v)
            updated["min_spread_timing_yes"] = v
        except Exception:
            return JSONResponse({"detail": "invalid min_spread_timing_yes"}, status_code=400)

    if "min_spread_timing_no" in payload:
        try:
            v = float(payload["min_spread_timing_no"])
            ui_set("min_spread_timing_no", v)
            updated["min_spread_timing_no"] = v
        except Exception:
            return JSONResponse({"detail": "invalid min_spread_timing_no"}, status_code=400)

    if "min_price_spread_neg" in payload:
        try:
            v = float(payload["min_price_spread_neg"])
            if v < 0:
                raise ValueError
            ui_set("min_price_spread_neg", v)
            updated["min_price_spread_neg"] = v
        except Exception:
            return JSONResponse({"detail": "invalid min_price_spread_neg"}, status_code=400)

    if not updated:
        return JSONResponse({"detail": "nothing to update"}, status_code=400)
    
    # применяем настройки сразу к UI-таблице (на бота не влияет)
    try:
        STATE["rows"] = build_rows(STATE.get("per_exchange") or {})
        STATE["updated_ts"] = time.time()
    except Exception:
        pass

    return {"status": "ok", "updated": updated}

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    mode = get_mode_from_request(request)
    token = (request.query_params.get("token") or "").strip()

    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Funding Spread Scanner</title>
  <style>
    body {{ font-family: Arial, sans-serif; padding: 16px; }}
    h1 {{ margin: 0 0 10px 0; }}
    .meta {{ margin: 0 0 12px 0; color: #444; }}
    .err {{ color: #b00020; font-weight: 700; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; font-size: 14px; }}
    th {{ background: #f3f3f3; text-align: center; position: sticky; top: 0; }}
    .yes {{ font-weight: 800; }}
    .btn {{ padding:6px 12px; border:1px solid #aaa; border-radius:6px; text-decoration:none; margin-right:8px; }}
    .active {{ background:#111; color:#fff; }}
    .long {{ background: #e6f4ea; }}   /* светло-зеленый */
    .short {{ background: #fce8e6; }}  /* светло-красный */

  </style>
</head>
<body>
  <h1>Funding Spread Scanner</h1>

  <div class="meta">
    <a href="/?mode=all{('&token=' + token) if token else ''}" class="btn {"active" if mode == "all" else ""}">ALL</a>
    <a href="/?mode=near{('&token=' + token) if token else ''}" class="btn {"active" if mode == "near" else ""}">NEAR</a>
  </div>

  <div id="adminPanel" class="meta" style="display:none; padding:10px; border:1px solid #ddd; border-radius:8px; background:#fafafa;">
    <b>UI фильтры (админ)</b>
    <div style="margin-top:8px; display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end;">
      <label style="display:flex; flex-direction:column; gap:4px;">
        Min vol (M USDT)
        <input id="f_min_vol" type="number" min="0" step="100000" style="padding:6px; width:180px;">
      </label>

      <label style="display:flex; flex-direction:column; gap:4px;">
        Min spread Timing=YES (%)
        <input id="f_yes" type="number" step="0.01" style="padding:6px; width:180px;">
      </label>

      <label style="display:flex; flex-direction:column; gap:4px;">
        Min spread Timing=NO (%)
        <input id="f_no" type="number" step="0.01" style="padding:6px; width:180px;">
      </label>
      <label style="display:flex; flex-direction:column; gap:4px;">
        Max negative Price Δ%
        <input id="f_price_neg" type="number" step="0.01" style="padding:6px; width:180px;">
      </label>      
      <button id="f_save" class="btn" style="cursor:pointer;">Save</button>
    </div>

    <div id="adminMsg" style="margin-top:8px; color:#444;"></div>
  </div>
 
  <div id="status" class="meta">Загрузка…</div>
  <div id="error" class="meta err"></div>

  <table>
    <thead>
      <tr>
        <th>Symbol</th>
        <th>Spread %</th>
        <th>Min %</th>
        <th>Min Ex</th>
        <th>Min Funding</th>
        <th>Max %</th>
        <th>Max Ex</th>
        <th>Max Funding</th>
        <th>Price Δ%</th>
        <th>Timing</th>
      </tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>

<script>
function getAdminToken() {{
  const p = new URLSearchParams(window.location.search);
  return (p.get('token') || '').trim();
}}

async function loadUiSettings(token) {{
  const res = await fetch('/api/admin/ui_settings?token=' + encodeURIComponent(token));
  if (!res.ok) throw new Error('GET ui_settings: ' + res.status);
  return await res.json();
}}

async function saveUiSettings(token, payload) {{
  const res = await fetch('/api/admin/ui_settings?token=' + encodeURIComponent(token), {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload),
  }});
  if (!res.ok) {{
    const t = await res.text();
    throw new Error('POST ui_settings: ' + res.status + ' ' + t);
  }}
  return await res.json();
}}

(function initAdminPanel() {{
  const token = getAdminToken();
  if (!token) return; // без token=... панель не показываем

  const panel = document.getElementById('adminPanel');
  const msg = document.getElementById('adminMsg');
  const iVol = document.getElementById('f_min_vol');
  const iYes = document.getElementById('f_yes');
  const iNo  = document.getElementById('f_no');
  const iPriceNeg = document.getElementById('f_price_neg');
  const btn  = document.getElementById('f_save');

  panel.style.display = 'block';
  msg.textContent = 'Загрузка настроек…';

  loadUiSettings(token).then((data) => {{
    const ui = (data && data.ui) || {{}};
    iVol.value = (ui.min_vol_usdt ?? 0) ? (Number(ui.min_vol_usdt) / 1e6) : '';
    iYes.value = ui.min_spread_timing_yes ?? '';
    iNo.value  = ui.min_spread_timing_no ?? '';
    iPriceNeg.value = ui.min_price_spread_neg ?? '';
    const vVol = Number(ui.min_vol_usdt ?? 0);
    const vYes = Number(ui.min_spread_timing_yes ?? 0);
    const vNo  = Number(ui.min_spread_timing_no ?? 0);
    const vPxN = Number(ui.min_price_spread_neg ?? 0);

    const parts = [];
    if (vVol > 0) parts.push(`vol ≥ ${{(vVol/1e6).toFixed(1)}}M`);
    if (vYes > 0) parts.push(`spread YES ≥ ${{vYes}}%`);
    if (vNo  > 0) parts.push(`spread NO ≥ ${{vNo}}%`);
    if (vPxN > 0) parts.push(`Price Δ%: hide if < −${{vPxN}}%`);

    msg.textContent = parts.length ? ('Активные фильтры: ' + parts.join(', ')) : 'Фильтры выключены. (Таблица обновляется раз в 30 сек.)';
  }}).catch((e) => {{
    msg.textContent = 'Ошибка загрузки настроек: ' + e.message;
  }});

  btn.addEventListener('click', async () => {{
    try {{
      msg.textContent = 'Сохраняю…';
      const payload = {{
        min_vol_usdt: Math.round(Number(iVol.value || 0) * 1e6),
        min_spread_timing_yes: Number(iYes.value || 0),
        min_spread_timing_no: Number(iNo.value || 0),
        min_price_spread_neg: Number(iPriceNeg.value || 0),
      }};
      await saveUiSettings(token, payload);
      msg.textContent = 'Сохранено.';
    }} catch (e) {{
      msg.textContent = 'Ошибка сохранения: ' + e.message;
    }}
  }});
}})();

async function refresh() {{
  const res = await fetch('/api/table?mode={mode}');
  const data = await res.json();

  const status = document.getElementById('status');
  const error = document.getElementById('error');
  const tbody = document.getElementById('tbody');

  status.textContent = 'Обновлено: ' + (data.updated_ts ? new Date(data.updated_ts * 1000).toLocaleTimeString() : '—');
  error.textContent = data.error ? ('Ошибка: ' + data.error) : '';

  tbody.innerHTML = '';

    for (const r of data.rows) {{
      const tr = document.createElement('tr');
      const edgeClass = (r.timing_edge === 'YES') ? 'yes' : '';
      const coin = r.symbol.replace('USDT', '');

      // классы для раскраски min/max процентов
      let minPctClass = '';
      let maxPctClass = '';

      if (r.timing_edge === 'NO') {{
        // Timing NO: long = Min%, short = Max%
        minPctClass = 'long';
        maxPctClass = 'short';
      }} else {{
        // Timing YES: long = ближайшее начисление, short = более позднее
        const minIsSoon = (r.min_next_msk === r.spread_next_msk);
        const maxIsSoon = (r.max_next_msk === r.spread_next_msk);

        if (minIsSoon) {{
          minPctClass = 'long';
          maxPctClass = 'short';
        }} else if (maxIsSoon) {{
          maxPctClass = 'long';
          minPctClass = 'short';
        }} else {{
          // fallback если не совпало по HH:MM
          minPctClass = 'long';
          maxPctClass = 'short';
        }}
     }}

      const minVolM = (Number(r.min_vol_usdt_24h || 0) / 1e6).toFixed(1).replace('.', ',');
      const maxVolM = (Number(r.max_vol_usdt_24h || 0) / 1e6).toFixed(1).replace('.', ',');
      const pxSpread = (r.price_spread_pct === null || r.price_spread_pct === undefined)
        ? '—'
        : Number(r.price_spread_pct).toFixed(3).replace('.', ',');
      

    tr.innerHTML = `
      <td>${{coin}}</td>
      <td>${{r.spread_pct}}</td>
      <td class="${{minPctClass}}">${{r.min_funding_pct}}</td>
      <td>
        <span style="display:inline-block; width:40px; text-align:right; font-weight:700; color:#000;">${{minVolM}}</span>
        <span style="margin:0 6px; color:#999;">|</span>
        <a href="${{r.min_link}}" target="_blank">${{r.min_exchange}}</a>
      </td>
      <td>${{r.min_next_msk}}</td>
      <td class="${{maxPctClass}}">${{r.max_funding_pct}}</td>
      <td>
        <span style="display:inline-block; width:40px; text-align:right; font-weight:700; color:#000;">${{maxVolM}}</span>
        <span style="margin:0 6px; color:#999;">|</span>
        <a href="${{r.max_link}}" target="_blank">${{r.max_exchange}}</a>
      </td>
      <td>${{r.max_next_msk}}</td>
      <td>${{pxSpread}}</td>
      <td class="${{edgeClass}}">${{r.timing_edge}}</td>
    `;
    tbody.appendChild(tr);
  }}
}}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>
"""
    return HTMLResponse(html)
