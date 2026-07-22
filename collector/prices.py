"""Collect Yahoo Finance chart data for the ``config/price_list.json`` watchlist.

File contract (approved by the supervisor; do not change): ``data/prices/{code}.json``
is ``{schema_version: 1, code, weekly: {dates, close}, daily: {dates, close}}``
(weekly windowed to the most recent ``WEEKLY_WINDOW`` points, daily to
``DAILY_WINDOW``; dates are ``YYYY-MM-DD`` derived from the Yahoo response's
UNIX-second timestamps converted to JST; close values are numbers or
``null``). ``data/prices_meta.json`` is
``{schema_version: 1, latest_price_date, generated_at, price_count}``. This
module is the only writer of ``prices/`` and ``prices_meta.json``; it must
never touch ``data/meta.json`` or ``data/short_meta.json`` (one-writer-per-file
rule, design.md section 4).

Failure policy: a single stock's fetch failure is tolerated (the previous
``prices/{code}.json`` is left untouched, or the stock is skipped if there is
no previous file). If more than ``FAILURE_THRESHOLD`` of the requested stocks
fail, this is treated as a systemic outage and the whole run fails loudly
(non-zero exit, ``prices_meta.json`` is not written) so the caller does not
deploy stale-looking data.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
from urllib.request import Request, urlopen


PRICES_SCHEMA_VERSION = 1
PRICES_META_SCHEMA_VERSION = 1

USER_AGENT = "Mozilla/5.0 (compatible; jukyu-navi-updater/1.0)"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
MIN_REQUEST_INTERVAL = 0.5
MAX_ATTEMPTS = 3  # 1回目 + リトライ2回
REQUEST_TIMEOUT = 30
WEEKLY_WINDOW = 160
DAILY_WINDOW = 30
FAILURE_THRESHOLD = 0.2
# 固定オフセットのJST。zoneinfoのtzdataが無い実行環境(Windows等)でも動く。
JST = timezone(timedelta(hours=9))

# 4桁が通常銘柄、5桁は優先株・社債型種類株式(repo CLAUDE.md 絶対ルール2。例:
# 伊藤園優先25935)。どちらもYahooシンボルは末尾に ".T" を付けるだけで同形式。
_CODE_PATTERN = re.compile(r"^[0-9A-Z]{4,5}$")


class PriceCollectorError(RuntimeError):
    """Raised when the price list is invalid or the run is a systemic failure."""


@dataclass(frozen=True)
class CollectResult:
    total: int
    failures: tuple[str, ...]
    latest_price_date: str | None
    removed: tuple[str, ...] = ()

    @property
    def success_count(self) -> int:
        return self.total - len(self.failures)


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _load_price_list(list_path: Path) -> list[str]:
    try:
        raw = list_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PriceCollectorError(f"price_listを読めません: {list_path}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PriceCollectorError(f"price_listがJSONとして不正です: {list_path}") from exc
    if not isinstance(document, dict):
        raise PriceCollectorError(f"price_listがJSONオブジェクトではありません: {list_path}")
    codes = document.get("codes")
    if not isinstance(codes, list) or not codes:
        raise PriceCollectorError(f"price_listのcodesが空です: {list_path}")
    seen: set[str] = set()
    validated: list[str] = []
    for code in codes:
        if not isinstance(code, str) or _CODE_PATTERN.fullmatch(code) is None:
            raise PriceCollectorError(f"price_listの銘柄コードが不正です: {code!r}")
        if code in seen:
            raise PriceCollectorError(f"price_listに重複コードがあります: {code}")
        seen.add(code)
        validated.append(code)
    return validated


class _RateLimiter:
    """Enforces a minimum gap between the start of successive requests."""

    def __init__(
        self,
        *,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        min_interval: float = MIN_REQUEST_INTERVAL,
    ) -> None:
        self._sleep = sleep
        self._monotonic = monotonic
        self._min_interval = min_interval
        self._last_started: float | None = None

    def wait(self) -> None:
        if self._last_started is not None:
            remaining = self._min_interval - (self._monotonic() - self._last_started)
            if remaining > 0:
                self._sleep(remaining)
        self._last_started = self._monotonic()


def _to_jst_date(value: Any, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceCollectorError(f"{context}: timestampが数値ではありません: {value!r}")
    try:
        converted = datetime.fromtimestamp(int(value), tz=JST)
    except (OverflowError, OSError, ValueError) as exc:
        raise PriceCollectorError(f"{context}: timestampが不正です: {value!r}") from exc
    return converted.date().isoformat()


def _numeric_or_none(value: Any, context: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PriceCollectorError(f"{context}: closeが数値でもnullでもありません: {value!r}")
    if not math.isfinite(value):
        raise PriceCollectorError(f"{context}: closeが有限数ではありません: {value!r}")
    return float(value)


def parse_chart_series(
    payload: Any, *, window: int, context: str
) -> tuple[list[str], list[float | None]]:
    """Parse one Yahoo chart API response into (dates, close) windowed to the tail."""
    if not isinstance(payload, dict):
        raise PriceCollectorError(f"{context}: レスポンスがJSONオブジェクトではありません")
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise PriceCollectorError(f"{context}: chartオブジェクトがありません")
    error = chart.get("error")
    if error not in (None, {}):
        raise PriceCollectorError(f"{context}: chart.errorが設定されています: {error!r}")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise PriceCollectorError(f"{context}: chart.result[0]がありません")
    result = results[0]

    timestamps = result.get("timestamp")
    if not isinstance(timestamps, list) or not timestamps:
        raise PriceCollectorError(f"{context}: timestamp系列がありません")

    indicators = result.get("indicators")
    if not isinstance(indicators, dict):
        raise PriceCollectorError(f"{context}: indicatorsオブジェクトがありません")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
        raise PriceCollectorError(f"{context}: indicators.quote[0]がありません")
    closes = quotes[0].get("close")
    if not isinstance(closes, list):
        raise PriceCollectorError(f"{context}: close系列がありません")
    if len(closes) != len(timestamps):
        raise PriceCollectorError(f"{context}: closeとtimestampの長さが一致しません")

    raw_dates = [_to_jst_date(ts, context) for ts in timestamps]
    raw_values = [_numeric_or_none(value, context) for value in closes]

    # Yahoo appends a live quote for the still-in-progress bar (e.g. the
    # current week before Friday, or the current day before the close). That
    # live point shares its JST date with the previous element whenever the
    # collector runs before the bar's period has finished, so collapse
    # adjacent same-date pairs before the strict-ascending check below.
    # Collapsing prefers a non-null value over a null one rather than always
    # taking the later element: the live quote can itself be null (no trade
    # yet today), and blindly overwriting an already-fetched real close with
    # that null would silently destroy good data (fail-loud violation). A
    # duplicate that is NOT adjacent (or any reversed order) still fails as a
    # genuine contract violation.
    dates: list[str] = []
    values: list[float | None] = []
    for day, value in zip(raw_dates, raw_values):
        if dates and dates[-1] == day:
            if value is not None:
                values[-1] = value
