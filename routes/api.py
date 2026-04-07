import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from state import STATE
from builders.funding_rows import minutes_until

router = APIRouter(prefix="/api", tags=["api"])


def get_mode_from_request(request: Request) -> str:
    mode = request.query_params.get("mode", "").strip().lower()
    if mode in ("all", "near"):
        return mode
    return "near"


@router.get("/table")
def api_table(request: Request):
    import web
    mode = get_mode_from_request(request)
    rows = STATE["rows"]

    if mode == "near":
        filtered = []
        for r in rows:
            next_ms = int(r.get("spread_next_ms") or 0)
            if next_ms <= 0:
                continue
            if minutes_until(next_ms) > web.NEAR_MAX_MINUTES:
                continue
            filtered.append(r)

        filtered.sort(key=lambda r: (int(r["spread_next_ms"]), -r["spread_pct"]))
        rows = filtered

    return JSONResponse({
        "updated_ts": STATE["updated_ts"],
        "pid": os.getpid(),
        "file": __file__,
        "error": STATE["error"],
        "rows": rows,
        "sources": STATE["sources"],
        "ex_status": STATE.get("ex_status", {}),
        "settings": {
            "refresh_seconds": web.REFRESH_SECONDS,
            "mode": mode,
            "near_hours": web.NEAR_HOURS,
            "near_max_minutes": web.NEAR_MAX_MINUTES,
            "enable_okx": web.ENABLE_OKX,
            "enable_gate": web.ENABLE_GATE,
            "enable_bitget": web.ENABLE_BITGET,
            "enable_bingx": web.ENABLE_BINGX,
            "enable_kucoin": web.ENABLE_KUCOIN,
            "okx_max_symbols_per_refresh": web.OKX_MAX_SYMBOLS_PER_REFRESH,
            "mexc_enabled": True,
        }
    })


@router.get("/bot/top")
def api_bot_top(limit: int = Query(20, ge=1, le=50)):
    """
    Top funding rates by absolute value.
    Используется Telegram-ботом.
    """
    allowed_exchanges = {"BINANCE", "BYBIT", "OKX", "GATE"}

    items = []
    per_exchange = STATE.get("per_exchange", {})

    for ex_name, ex_map in per_exchange.items():
        if ex_name not in allowed_exchanges:
            continue

        for sym, rec in ex_map.items():
            funding = rec.get("funding_pct")
            next_ms = rec.get("next_ms")
            link = rec.get("link")

            if funding is None or not next_ms:
                continue

            items.append({
                "symbol": sym,
                "funding_rate": float(funding),
                "next_funding_ms": int(next_ms),
                "exchange": ex_name,
                "url": link,
            })

    items.sort(key=lambda x: abs(x["funding_rate"]), reverse=True)
    return items[:limit]


@router.get("/bot/all")
def api_bot_all():
    """
    Full funding list for Telegram-bot (no top slicing).
    4 exchanges only: BINANCE / BYBIT / OKX / GATE
    """
    allowed_exchanges = {"BINANCE", "BYBIT", "OKX", "GATE"}

    items = []
    per_exchange = STATE.get("per_exchange", {})

    for ex_name, ex_map in per_exchange.items():
        if ex_name not in allowed_exchanges:
            continue

        for sym, rec in ex_map.items():
            funding = rec.get("funding_pct")
            next_ms = rec.get("next_ms")
            link = rec.get("link")

            if funding is None or not next_ms:
                continue

            items.append({
                "symbol": sym,
                "funding_rate": float(funding),
                "next_funding_ms": int(next_ms),
                "exchange": ex_name,
                "url": link,
                "mark_px": float(rec.get("mark_px") or 0.0),
                "vol_usdt_24h": float(rec.get("vol_usdt_24h") or 0.0),
            })

    return items


@router.get("/bot/debug")
def api_bot_debug():
    per_exchange = STATE.get("per_exchange", {})
    return {
        "rows_len": len(STATE.get("rows", [])),
        "per_exchange_keys": list(per_exchange.keys()),
        "sources": STATE.get("sources", {}),
        "ex_status": STATE.get("ex_status", {}),
    }
