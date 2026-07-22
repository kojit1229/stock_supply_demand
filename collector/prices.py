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
        else:
            dates.append(day)
            values.append(value)

    for previous, current in zip(dates, dates[1:]):
        if current <= previous:
            raise PriceCollectorError(f"{context}: 日付が昇順ではありません")

    if len(dates) > window:
        dates = dates[-window:]
        values = values[-window:]
    return dates, values


def _request_json(opener: Callable[..., Any], url: str, *, context: str) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request, timeout=REQUEST_TIMEOUT) as response:
        raw = response.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PriceCollectorError(f"{context}: レスポンスのJSONが不正です") from exc


def _fetch_series(
    opener: Callable[..., Any],
    limiter: _RateLimiter,
    sleep: Callable[[float], None],
    symbol: str,
    kind: str,
    *,
    range_: str,
    interval: str,
    window: int,
) -> tuple[list[str], list[float | None]]:
    url = f"{CHART_URL.format(symbol=symbol)}?range={range_}&interval={interval}"
    context = f"{symbol} {kind}"
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        limiter.wait()
        try:
            payload = _request_json(opener, url, context=context)
            return parse_chart_series(payload, window=window, context=context)
        except Exception as exc:  # retried uniformly; last attempt re-raises below
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                sleep(MIN_REQUEST_INTERVAL * (2**attempt))
    raise PriceCollectorError(
        f"{context}: 取得に{MAX_ATTEMPTS}回失敗しました"
    ) from last_error


def _collect_one(
    opener: Callable[..., Any],
    limiter: _RateLimiter,
    sleep: Callable[[float], None],
    code: str,
) -> tuple[dict[str, Any], str]:
    symbol = f"{code}.T"
    weekly_dates, weekly_close = _fetch_series(
        opener, limiter, sleep, symbol, "weekly",
        range_="3y", interval="1wk", window=WEEKLY_WINDOW,
    )
    daily_dates, daily_close = _fetch_series(
        opener, limiter, sleep, symbol, "daily",
        range_="1mo", interval="1d", window=DAILY_WINDOW,
    )
    document = {
        "schema_version": PRICES_SCHEMA_VERSION,
        "code": code,
        "weekly": {"dates": weekly_dates, "close": weekly_close},
        "daily": {"dates": daily_dates, "close": daily_close},
    }
    latest = max(weekly_dates[-1:] + daily_dates[-1:])
    return document, latest


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _reconcile_removed_codes(prices_dir: Path, codes: list[str]) -> tuple[str, ...]:
    """Delete prices/{code}.json for codes no longer in the (full) watchlist.

    Only meaningful for a full run (no ``--limit``): a code dropped from
    ``config/price_list.json`` would otherwise keep being served forever,
    because ``weekly.yml`` restores the previous ``data/`` from gh-pages
    before this script runs and this script never touches files it doesn't
    know about. A code that is still in ``codes`` but failed this run is
    preserved as before (it is never a candidate here, since it is present
    in ``codes``).
    """
    if not prices_dir.is_dir():
        return ()
    keep = set(codes)
    removed = []
    for path in sorted(prices_dir.glob("*.json")):
        if path.stem not in keep:
            path.unlink()
            removed.append(path.stem)
    return tuple(removed)


def run_collect(
    out_dir: str | os.PathLike[str],
    list_path: str | os.PathLike[str],
    *,
    limit: int | None = None,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
    generated_at: str | None = None,
) -> CollectResult:
    """Fetch weekly/daily closes for every listed code and write prices/ + prices_meta.json.

    A per-code failure is tolerated (existing ``prices/{code}.json`` is left in
    place, or the code is skipped if there is none yet). If failures exceed
    ``FAILURE_THRESHOLD`` of the requested codes, raises ``PriceCollectorError``
    without writing ``prices_meta.json`` so the caller does not deploy.

    Note: each successful code is written to ``prices/{code}.json`` as soon as
    it is fetched, before the threshold check below runs. So when the
    threshold check does raise, the healthy codes' files from this same run
    are still left on disk. That is fine: this script exits non-zero in that
    case, which fails the CI step and prevents weekly.yml's later Deploy step
    from running, so nothing here gets published regardless.

    Reconciliation: when ``limit`` is ``None`` (a full run) and the threshold
    check above passes, any ``prices/{code}.json`` whose code is no longer in
    ``config/price_list.json`` is deleted, so a code removed from the
    watchlist eventually stops being served (``weekly.yml`` restores the
    previous ``data/`` from gh-pages before every run, so nothing else would
    ever remove it). A ``--limit`` run never deletes anything, since it only
    ever sees a partial watchlist and would otherwise wipe out everything
    outside that prefix.

    ``opener``/``sleep``/``monotonic`` default to ``None`` (rather than
    binding ``urlopen``/``time.sleep``/``time.monotonic`` directly as default
    values) so tests can install a network guard or a fake clock by patching
    this module's names; the real functions are looked up here, at call time,
    exactly like ``collector.backfill_jsda.CachedDownloader`` does.
    """
    output_root = Path(out_dir)
    prices_dir = output_root / "prices"
    codes = _load_price_list(Path(list_path))
    if limit is not None:
        codes = codes[:limit]
    if not codes:
        raise PriceCollectorError("処理対象の銘柄コードがありません")

    fetch_opener = opener or urlopen
    fetch_sleep = sleep or time.sleep
    fetch_monotonic = monotonic or time.monotonic
    limiter = _RateLimiter(sleep=fetch_sleep, monotonic=fetch_monotonic)
    failures: list[str] = []
    latest_dates: list[str] = []
    for code in codes:
        try:
            document, latest = _collect_one(fetch_opener, limiter, fetch_sleep, code)
        except Exception as exc:
            failures.append(code)
            print(f"prices: {code}の取得に失敗しました: {exc}", file=sys.stderr)
            continue
        _write_json_atomic(prices_dir / f"{code}.json", document)
        latest_dates.append(latest)

    failure_ratio = len(failures) / len(codes)
    if failure_ratio > FAILURE_THRESHOLD:
        raise PriceCollectorError(
            f"失敗銘柄が{len(failures)}/{len(codes)}件"
            f"({failure_ratio:.0%})で許容閾値{FAILURE_THRESHOLD:.0%}を超えました: "
            f"{', '.join(failures)}"
        )

    removed: tuple[str, ...] = ()
    if limit is None:
        removed = _reconcile_removed_codes(prices_dir, codes)
        if removed:
            print(
                f"prices: リストから外れた{len(removed)}銘柄のファイルを削除しました: "
                f"{', '.join(removed)}",
                file=sys.stderr,
            )

    latest_price_date = max(latest_dates) if latest_dates else None
    meta = {
        "schema_version": PRICES_META_SCHEMA_VERSION,
        "latest_price_date": latest_price_date,
        "generated_at": generated_at or _generated_at(),
        "price_count": len(codes) - len(failures),
    }
    _write_json_atomic(output_root / "prices_meta.json", meta)
    return CollectResult(
        total=len(codes),
        failures=tuple(failures),
        latest_price_date=latest_price_date,
        removed=removed,
    )


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="price_list.json記載銘柄の株価(週次3年+直近日次)を取得します"
    )
    parser.add_argument("--out", default="data", help="出力dataディレクトリ")
    parser.add_argument(
        "--list", default="config/price_list.json", help="対象銘柄リストJSON"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="先頭N銘柄だけ処理する(テスト・手動確認用)"
    )
    args = parser.parse_args(argv)

    try:
        result = run_collect(args.out, args.list, limit=args.limit)
    except Exception as exc:
        print(f"prices: {exc}", file=sys.stderr)
        return 1

    if result.failures:
        print(
            f"prices: {len(result.failures)}銘柄の取得に失敗しました: "
            f"{', '.join(result.failures)}",
            file=sys.stderr,
        )
    print(
        f"prices: {result.success_count}/{result.total}銘柄を更新しました "
        f"(latest_price_date={result.latest_price_date})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
