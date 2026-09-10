"""Кэш рынка внутри бота.

Раньше бот ходил за данными по HTTP к сканеру (127.0.0.1:8000) и без него был мёртв.
Теперь он опрашивает биржи сам, двумя темпами:

  быстрый цикл (60 с)  — Binance, Bybit, Gate и ценовая часть OKX: 7 запросов;
  медленный цикл (180 с) — фандинги OKX поштучно по ликвидным монетам: ~260 запросов.

Итого около 1,6 запроса в секунду против 32 у старого сканера.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx

from exchanges import binance as ex_binance
from exchanges import bybit as ex_bybit
from exchanges import gate as ex_gate
from exchanges import okx as ex_okx

log = logging.getLogger("funding-bot.market")

BOT_EXCHANGES = ("BINANCE", "BYBIT", "OKX", "GATE")

FAST_PERIOD_SEC = 60
OKX_FUNDING_PERIOD_SEC = 180

# нижняя граница отбора монет OKX: защищает от пользователя, поставившего порог в ноль
OKX_MIN_VOL_FLOOR = 500_000


def okx_min_volume(user_thresholds) -> float:
    """Порог объёма для опроса OKX — самый мягкий среди пользователей, но не ниже пола."""
    values = [float(v) for v in user_thresholds if v is not None]
    if not values:
        return float(OKX_MIN_VOL_FLOOR)
    return float(max(OKX_MIN_VOL_FLOOR, min(values)))


class Market:
    def __init__(self):
        self.per_exchange = {}   # биржа -> {символ: Rec}
        self.status = {}         # биржа -> {ok, stale, err, updated_ts}
        self.updated_ts = 0.0
        self._okx_prices = {"inst_ids": [], "vol": {}, "mark": {}}
        self._okx_funding = {}
        self._okx_last_ts = 0.0

    # ---------- состояние ----------

    def merge(self, name: str, data: dict, err: Optional[str] = None) -> None:
        """Кладёт свежие данные биржи. Сбой не затирает прошлый снимок."""
        now = time.time()

        if not err and isinstance(data, dict) and data:
            self.per_exchange[name] = data
            self.status[name] = {"ok": True, "stale": False, "err": None, "updated_ts": now}
        else:
            prev = self.per_exchange.get(name) or {}
            self.per_exchange[name] = prev
            self.status[name] = {
                "ok": False,
                "stale": True,
                "err": err or "empty response",
                "updated_ts": (self.status.get(name) or {}).get("updated_ts", 0.0),
            }

        self.updated_ts = now

    def items(self) -> list:
        """Плоский список в том же виде, в каком его раньше отдавал /api/bot/all."""
        out = []
        for ex_name in BOT_EXCHANGES:
            for sym, rec in (self.per_exchange.get(ex_name) or {}).items():
                funding = rec.get("funding_pct")
                next_ms = rec.get("next_ms")
                if funding is None or not next_ms:
                    continue
                out.append({
                    "symbol": sym,
                    "funding_rate": float(funding),
                    "next_funding_ms": int(next_ms),
                    "exchange": ex_name,
                    "url": rec.get("link"),
                    "mark_px": float(rec.get("mark_px") or 0.0),
                    "vol_usdt_24h": float(rec.get("vol_usdt_24h") or 0.0),
                })
        return out

    def price_spread(self, symbol: str) -> Optional[dict]:
        """Разница цен Binance и Bybit по монете. None, если данных нет."""
        sym = (symbol or "").strip().upper()
        if not sym:
            return None
        if not sym.endswith("USDT"):
            sym += "USDT"

        a_rec = (self.per_exchange.get("BINANCE") or {}).get(sym)
        b_rec = (self.per_exchange.get("BYBIT") or {}).get(sym)
        if not a_rec or not b_rec:
            return None

        try:
            binance_price = float(a_rec.get("mark_px") or 0.0)
            bybit_price = float(b_rec.get("mark_px") or 0.0)
        except (TypeError, ValueError):
            return None

        if binance_price <= 0 or bybit_price <= 0:
            return None

        return {
            "symbol": sym,
            "binance_price": binance_price,
            "bybit_price": bybit_price,
            "spread_pct": ((binance_price / bybit_price) - 1) * 100,
        }

    def snapshot(self) -> dict:
        return {
            "updated_ts": self.updated_ts,
            "sources": {k: len(v or {}) for k, v in self.per_exchange.items()},
            "status": dict(self.status),
        }

    def is_ready(self) -> bool:
        """Есть ли хоть какие-то данные — чтобы не слать пустые ответы на старте."""
        return any((self.per_exchange.get(ex) or {}) for ex in BOT_EXCHANGES)

    # ---------- обновление ----------

    async def _safe(self, name: str, coro) -> None:
        try:
            self.merge(name, await coro)
        except Exception as e:
            log.warning("fetch %s failed: %r", name, e)
            self.merge(name, {}, err=repr(e))

    async def refresh_fast(self, client: httpx.AsyncClient) -> None:
        """Три биржи списком + ценовая часть OKX."""
        await asyncio.gather(
            self._safe("BINANCE", ex_binance.fetch(client)),
            self._safe("BYBIT", ex_bybit.fetch(client)),
            self._safe("GATE", ex_gate.fetch(client)),
            self._refresh_okx_prices(client),
        )

    async def _refresh_okx_prices(self, client: httpx.AsyncClient) -> None:
        try:
            self._okx_prices = await ex_okx.fetch_prices(client)
        except Exception as e:
            log.warning("fetch OKX prices failed: %r", e)
            return
        self._recompose_okx()

    async def refresh_okx_funding(self, client: httpx.AsyncClient, min_vol: float) -> None:
        """Поштучный опрос фандингов OKX по монетам, проходящим порог объёма."""
        inst_ids = ex_okx.select_instruments(
            self._okx_prices.get("inst_ids") or [],
            self._okx_prices.get("vol") or {},
            min_vol,
        )
        if not inst_ids:
            return

        started = time.time()
        try:
            funding = await ex_okx.fetch_funding(client, inst_ids)
        except Exception as e:
            log.warning("fetch OKX funding failed: %r", e)
            self.merge("OKX", {}, err=repr(e))
            return

        self._okx_funding = funding
        self._okx_last_ts = time.time()
        self._recompose_okx()
        log.info(
            "OKX funding refreshed: %s монет из %s инструментов за %.1f с",
            len(funding), len(inst_ids), time.time() - started,
        )

    def _recompose_okx(self) -> None:
        if not self._okx_funding:
            return
        self.merge("OKX", ex_okx.compose(self._okx_prices, self._okx_funding))

    async def run(self, min_vol_provider) -> None:
        """Вечный цикл обновления. min_vol_provider() отдаёт порог объёма для OKX."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, pool=60.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        ) as client:
            while True:
                try:
                    await self.refresh_fast(client)

                    if time.time() - self._okx_last_ts >= OKX_FUNDING_PERIOD_SEC:
                        try:
                            min_vol = float(min_vol_provider())
                        except Exception:
                            min_vol = float(OKX_MIN_VOL_FLOOR)
                        await self.refresh_okx_funding(client, min_vol)
                except Exception:
                    log.exception("market refresh failed")

                await asyncio.sleep(FAST_PERIOD_SEC)


MARKET = Market()
