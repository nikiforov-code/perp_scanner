"""Качалки бирж, общие для бота и сканера.

Каждый модуль отдаёт dict {symbol: Rec} — формат описан в exchanges/types.py.
"""

from . import binance, bybit, gate, okx, types  # noqa: F401
