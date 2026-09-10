"""Обход пользователей и отправка часового дайджеста.

Отправка передаётся снаружи (`send`), поэтому логику можно проверить тестами
без Telegram. Главное правило модуля: сбой на одном пользователе не должен
мешать остальным — раньше исключение ловилось вокруг всего цикла, и один
человек с некорректными порогами обрывал рассылку всем, кто стоял в очереди
после него.
"""

import logging
from datetime import datetime, timezone, timedelta

from digest import select_items, build_message, due_hour_key, total_count
from formatting import build_binance_price_map

log = logging.getLogger("funding-bot.notifier")

DIGEST_TITLE = "🔔 <b>Актуальные фандинги</b>"


async def notify_users(
    users: dict,
    items: list,
    now_ms: int,
    send,
    sent_hours: dict,
    is_blocked=None,
    on_blocked=None,
    now_provider=None,
) -> dict:
    """Проходит по пользователям и шлёт тем, кому пора.

    users       — {user_id: UserSettings}
    items       — плоский список монет из market.items()
    send        — async (user_id, text) -> None
    sent_hours  — {user_id: hour_key} последнего отправленного дайджеста
    is_blocked  — предикат: это исключение означает «пользователь заблокировал бота»?
    on_blocked  — коллбэк, вызывается с user_id заблокировавшего
    now_provider — (tz) -> datetime; подменяется в тестах, чтобы не зависеть от часов
    """
    if now_provider is None:
        now_provider = lambda tz: datetime.now(tz=tz)
    binance_prices = build_binance_price_map(items)
    stats = {"sent": [], "empty": [], "failed": [], "blocked": []}

    for uid, s in list(users.items()):
        try:
            if not s.notify_enabled:
                continue

            user_tz = timezone(timedelta(hours=int(s.utc_offset_hours)))
            now_local = now_provider(user_tz)

            key = due_hour_key(s, now_local)
            if key is None or sent_hours.get(uid) == key:
                continue

            groups = select_items(items, s, now_ms)
            found = total_count(groups)

            if found == 0:
                sent_hours[uid] = key
                stats["empty"].append(uid)
                log.info("digest empty uid=%s now=%s", uid, now_local.strftime("%H:%M"))
                continue

            title = f"{DIGEST_TITLE} — дайджест {now_local.strftime('%H:%M')}"
            text = build_message(groups, s, title, now_local, user_tz, binance_prices)

            await send(uid, text)

            sent_hours[uid] = key
            stats["sent"].append(uid)
            log.info("digest sent uid=%s total=%s now=%s", uid, found, now_local.strftime("%H:%M"))

        except Exception as e:
            if is_blocked is not None and is_blocked(e):
                stats["blocked"].append(uid)
                log.info("digest blocked uid=%s — выключаю уведомления", uid)
                if on_blocked is not None:
                    on_blocked(uid, s)
                continue

            stats["failed"].append(uid)
            log.exception("digest failed uid=%s: %r", uid, e)

    return stats
