# Bot Standalone Implementation Plan

**Goal:** Отвязать Telegram-бот от сканера и починить рассылку, найденную аудитом.

**Architecture:** Общий пакет `exchanges/` с качалками четырёх бирж; бот держит собственный
кэш рынка (`market.py`) и обновляет его двумя темпами; чистые функции вынесены из `bot.py`
в отдельные модули, чтобы их можно было покрыть тестами. Сканер (`web.py`) остаётся рабочим
на том же пакете, его служба останавливается.

**Tech Stack:** Python 3.10 (сервер), aiogram 3, httpx, sqlite3, unittest (stdlib).

## Global Constraints

- Формат `Rec` неизменен: `exchange, symbol, funding_pct, next_ms, mark_px, vol_usdt_24h, link`.
- Формат сообщений бота не меняется — пользователи к нему привыкли.
- Разбор ответов бирж переносится дословно, без переписывания.
- Новых зависимостей не добавляем; тесты на stdlib `unittest`.
- Биржи бота: BINANCE, BYBIT, OKX, GATE.
- OKX: отбор `vol_usdt_24h >= max(500_000, min(пороги пользователей))`, период 180 с.
- Быстрая группа: период 60 с.

---

### Task 1: Пакет `exchanges/`

**Files:**
- Create: `exchanges/__init__.py`, `exchanges/types.py`, `exchanges/binance.py`,
  `exchanges/bybit.py`, `exchanges/gate.py`, `exchanges/okx.py`
- Modify: `web.py` — импорт четырёх качалок вместо локальных определений
- Test: `tests/test_exchanges.py`

**Produces:** `fetch(client) -> dict[str, Rec]` в каждом модуле; у OKX
`fetch(client, min_vol_usdt: float) -> dict[str, Rec]` и чистая функция
`select_instruments(vol_map, inst_ids, min_vol_usdt) -> list[str]`.

- [ ] Тест `select_instruments`: отбирает по объёму, без обрезки по количеству
- [ ] Тест: `to_percent`, `to_int_ms` из `types.py`
- [ ] Перенос кода качалок из `web.py` дословно
- [ ] `web.py` импортирует их, свои 4 биржи оставляет у себя
- [ ] `python3 -m unittest discover -s tests` зелёный

### Task 2: `formatting.py`

**Files:**
- Create: `formatting.py`
- Test: `tests/test_formatting.py`

**Produces:** `fmt_coin`, `fmt_rate`, `is_clean_symbol`, `calc_coin_width`,
`build_binance_price_map`, `format_delta_vs_binance`, `format_item_line`,
`split_html_chunks(text: str, limit: int) -> list[str]`.

- [ ] Тест: `split_html_chunks` режет по строкам, каждая часть ≤ лимита, теги не рвутся
- [ ] Тест: одна строка длиннее лимита возвращается отдельной частью, а не теряется
- [ ] Тест: `format_item_line` без висячего пробела, мёртвый `delta_block` убран
- [ ] Тест: `calc_coin_width`, `is_clean_symbol`, `fmt_rate`
- [ ] Перенос функций из `bot.py`, `bot.py` импортирует их

### Task 3: `user_settings.py`

**Files:**
- Create: `user_settings.py`
- Test: `tests/test_user_settings.py`

**Produces:** `UserSettings`, `sanitize(s) -> UserSettings`, `db_init`, `db_load_user`,
`db_load_all -> dict[int, UserSettings]`, `db_save_user`, `DEFAULT_UTC_OFFSET = 3`.

- [ ] Тест: `sanitize` превращает пороги `+1.0 / +1.0` в `+1.0 / -1.0`
- [ ] Тест: `sanitize` подставляет UTC+3 вместо `None`
- [ ] Тест: `db_load_all` возвращает всех пользователей из временной базы
- [ ] Перенос кода работы с SQLite из `bot.py`

### Task 4: `market.py`

**Files:**
- Create: `market.py`
- Test: `tests/test_market.py`

**Produces:** `Market` с `snapshot()`, `merge(name, data, err)`, `price_spread(symbol)`,
`refresh_fast(client)`, `refresh_okx_funding(client, min_vol)`, `run(min_vol_provider)`.

- [ ] Тест: `merge` при ошибке биржи сохраняет прошлые данные и ставит `stale`
- [ ] Тест: `merge` при пустом ответе не затирает прошлый снимок
- [ ] Тест: `price_spread` считает Binance/Bybit и возвращает `None`, если цены нет
- [ ] Тест: `price_spread` не делит на ноль

### Task 5: `digest.py`

**Files:**
- Create: `digest.py`
- Test: `tests/test_digest.py`

**Produces:** `select_items(items, s, now_ms, horizon_h=4) -> dict[ex, list]`,
`build_message(groups, s, header, now_local) -> str`, `MAX_PER_EXCHANGE = 20`,
`due_hours(s, now_local, last_hour_key) -> int | None`.

- [ ] Тест: `due_hours` срабатывает в целевую минуту и не срабатывает повторно в тот же час
- [ ] Тест: `due_hours` срабатывает, если минуту проспали (время ушло за цель)
- [ ] Тест: `select_items` режет по порогам, объёму и горизонту 4 часа
- [ ] Тест: `build_message` не отдаёт больше 20 строк на биржу
- [ ] Тест: пользователь с порогами `+1/-1` после `sanitize` даёт короткое сообщение

### Task 6: `bot.py` на новых модулях

**Files:**
- Modify: `bot.py`
- Test: `tests/test_notifier.py`

**Consumes:** всё из задач 2–5.

- [ ] Тест: ошибка отправки одному пользователю не мешает следующим получить своё
- [ ] Тест: `TelegramForbiddenError` выключает `notify_enabled` у пользователя
- [ ] `notifier_loop` переписан: цикл по пользователям с `try/except` внутри
- [ ] Дайджест уходит через `split_html_chunks`
- [ ] Пользователи грузятся из базы при старте
- [ ] Лог-шум убран: `wrong_minute` и HTTP-логи httpx
- [ ] `fetch_all` и `fetch_price_spread` читают `market`, а не HTTP

### Task 7: Зависимости и выкат

**Files:**
- Modify: `requirements.txt`
- Server: юниты systemd, база на проде

- [ ] `requirements.txt`: `aiogram`, `python-dotenv`
- [ ] Полный прогон `python3 -m unittest discover -s tests`
- [ ] Показать владельцу diff, дождаться «ок»
- [ ] `git pull` на сервере, `systemctl stop scanner-web && systemctl disable scanner-web`
- [ ] Разовая правка базы: UTC+3 четверым, санитайз порогов
- [ ] `systemctl restart scanner-bot`, проверить журнал
- [ ] `journald`: `SystemMaxUse=500M`
