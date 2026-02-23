import time
from datetime import datetime, timezone, timedelta

from admin.ui_settings_store import ui_get

MSK = timezone(timedelta(hours=3))


def msk_hhmm(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(MSK)
    return dt.strftime("%H:%M")


def minutes_until(ms: int) -> int:
    now = int(time.time() * 1000)
    return max(0, int((ms - now) / 60000))


def build_rows(per_exchange: dict) -> list:
    # собираем монеты, которые есть хотя бы на 2 биржах
    symbols = {}
    for ex, m in per_exchange.items():
        for sym, rec in m.items():
            symbols.setdefault(sym, []).append(rec)

    rows = []
    for sym, recs in symbols.items():
        # оставляем только "живые" записи (есть валидное время следующего funding)
        recs = [r for r in recs if (r.get("next_ms") or 0) > 0]
        if len(recs) < 2:
            continue

        # перебираем ВСЕ пары бирж
        for i in range(len(recs)):
            for j in range(i + 1, len(recs)):
                a = recs[i]
                b = recs[j]

                # classic spread между конкретной парой бирж
                spread_classic = b["funding_pct"] - a["funding_pct"]

                # timing edge для ЭТОЙ пары
                a_mins = minutes_until(a["next_ms"])
                b_mins = minutes_until(b["next_ms"])

                # Timing YES для пары: времена начисления отличаются (значит "заберём" ближайшее начисление только на одной бирже)
                edge = (a["next_ms"] != b["next_ms"])

                # NEAREST: ближайшее начисление между ЭТИМИ ДВУМЯ
                soon_rec = a if a["next_ms"] <= b["next_ms"] else b
                spread_nearest = soon_rec["funding_pct"]

                spread_raw = spread_nearest if edge else spread_classic
                spread_profit = abs(spread_raw)

                # фильтр минимального спреда в зависимости от Timing
                if edge:
                    if spread_profit < float(ui_get("min_spread_timing_yes", 0.2) or 0.2):
                        continue
                else:
                    if spread_profit < float(ui_get("min_spread_timing_no", 0.35) or 0.35):
                        continue

                # --- UI volume filter (only affects UI rows) ---
                min_vol = float(ui_get("min_vol_usdt", 0.0) or 0.0)
                if min_vol > 0:
                    a_vol = float(a.get("vol_usdt_24h") or 0.0)
                    b_vol = float(b.get("vol_usdt_24h") or 0.0)
                    if a_vol < min_vol or b_vol < min_vol:
                        continue

                # определяем min / max для UI
                min_rec = a if a["funding_pct"] <= b["funding_pct"] else b
                max_rec = b if min_rec is a else a

                # --- price spread (mark) между LONG-side и SHORT-side ---
                # Логика 1B: sides такие же, как подсветка в UI:
                # Timing=NO  -> long=min_rec, short=max_rec
                # Timing=YES -> long=та биржа, у которой next == spread_next (soon_rec), short=другая
                long_rec = min_rec
                short_rec = max_rec
                if edge:
                    if min_rec["next_ms"] == soon_rec["next_ms"]:
                        long_rec = min_rec
                        short_rec = max_rec
                    elif max_rec["next_ms"] == soon_rec["next_ms"]:
                        long_rec = max_rec
                        short_rec = min_rec

                lp = long_rec.get("mark_px")
                sp = short_rec.get("mark_px")

                price_spread_pct = None
                try:
                    lp = float(lp) if lp is not None else None
                    sp = float(sp) if sp is not None else None
                    if lp and sp and lp > 0:
                        price_spread_pct = round((sp / lp - 1.0) * 100.0, 4)
                except Exception:
                    price_spread_pct = None
                # --- UI filter: Price Δ% отдельно для "+" и "-" ---
                neg_thr = float(ui_get("min_price_spread_neg", 0.0) or 0.0)

                if price_spread_pct is not None:
                    # "-" сторона (neg_thr хранится как модуль)
                    if neg_thr > 0 and price_spread_pct < 0 and price_spread_pct < -neg_thr:
                        continue

                rows.append({
                    "symbol": sym,

                    "min_funding_pct": round(min_rec["funding_pct"], 4),
                    "min_exchange": min_rec["exchange"],
                    "min_next_msk": msk_hhmm(min_rec["next_ms"]),
                    "min_link": min_rec["link"],
                    "min_vol_usdt_24h": float(min_rec.get("vol_usdt_24h") or 0.0),

                    "max_funding_pct": round(max_rec["funding_pct"], 4),
                    "max_exchange": max_rec["exchange"],
                    "max_next_msk": msk_hhmm(max_rec["next_ms"]),
                    "max_link": max_rec["link"],
                    "max_vol_usdt_24h": float(max_rec.get("vol_usdt_24h") or 0.0),

                    "spread_pct": round(spread_profit, 4),
                    "price_spread_pct": price_spread_pct,
                    "spread_raw": round(spread_raw, 6),

                    "spread_mode": "NEAREST" if edge else "CLASSIC",
                    "spread_exchange": soon_rec["exchange"],
                    "spread_next_ms": int(soon_rec["next_ms"]),
                    "spread_next_msk": msk_hhmm(soon_rec["next_ms"]),

                    "timing_edge": "YES" if edge else "NO",
                })

    # основная сортировка по спреду, вторично Timing=YES выше при равном спреде
    rows.sort(key=lambda r: (-r["spread_pct"], r["timing_edge"] != "YES"))
    return rows
