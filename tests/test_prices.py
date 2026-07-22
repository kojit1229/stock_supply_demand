import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from collector import prices


JST = timezone(timedelta(hours=9))
GENERATED_AT = "2026-07-22T09:00:00Z"


def _ts(year, month, day):
    return int(datetime(year, month, day, tzinfo=JST).timestamp())


def _chart_payload(timestamps, closes):
    return {
        "chart": {
            "result": [
                {
                    "meta": {},
                    "timestamp": timestamps,
                    "indicators": {"quote": [{"close": closes}]},
                }
            ],
            "error": None,
        }
    }


def _weekly_series(count, *, start_year=2023, start_month=7, start_day=21):
    start = datetime(start_year, start_month, start_day, tzinfo=JST)
    timestamps = [int((start + timedelta(weeks=i)).timestamp()) for i in range(count)]
    closes = [1000.0 + i for i in range(count)]
    return timestamps, closes


def _daily_series(count):
    start = datetime(2026, 6, 1, tzinfo=JST)
    timestamps = [int((start + timedelta(days=i)).timestamp()) for i in range(count)]
    closes = [2000.0 + i for i in range(count)]
    return timestamps, closes


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FailingResponse:
    """Raised in place of a response to simulate a network/HTTP failure."""


def _symbol_kind(url):
    path = url.split("?", 1)[0]
    symbol = path.rsplit("/", 1)[-1]
    if "interval=1wk" in url:
        return symbol, "weekly"
    if "interval=1d" in url:
        return symbol, "daily"
    raise AssertionError(f"unrecognized chart request: {url}")


class FakeOpener:
    """Serves canned payloads per (symbol, kind), tracking call counts."""

    def __init__(self, outcomes):
        # outcomes: dict[(symbol, kind)] -> payload dict, or list of
        # payload/Exception consumed one per call (last item repeats).
        self.outcomes = outcomes
        self.calls = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.calls.append(url)
        key = _symbol_kind(url)
        outcome = self.outcomes[key]
        if isinstance(outcome, list):
            index = min(
                sum(1 for called in self.calls if _symbol_kind(called) == key) - 1,
                len(outcome) - 1,
            )
            outcome = outcome[index]
        if isinstance(outcome, Exception):
            raise outcome
        return _FakeResponse(outcome)


def _ok_outcomes(codes, *, weekly_count=8, daily_count=5):
    outcomes = {}
    for code in codes:
        symbol = f"{code}.T"
        w_ts, w_close = _weekly_series(weekly_count)
        d_ts, d_close = _daily_series(daily_count)
        outcomes[(symbol, "weekly")] = _chart_payload(w_ts, w_close)
        outcomes[(symbol, "daily")] = _chart_payload(d_ts, d_close)
    return outcomes


class Clock:
    """Instant fake sleep/monotonic so retry backoff never really waits."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


class PricesParsingTests(unittest.TestCase):
    def test_parses_and_windows_weekly_series_with_jst_dates(self):
        timestamps, closes = _weekly_series(165)
        dates, values = prices.parse_chart_series(
            _chart_payload(timestamps, closes),
            window=prices.WEEKLY_WINDOW,
            context="test",
        )
        self.assertEqual(len(dates), prices.WEEKLY_WINDOW)
        self.assertEqual(len(values), prices.WEEKLY_WINDOW)
        expected_last_date = datetime.fromtimestamp(
            timestamps[-1], tz=JST
        ).date().isoformat()
        self.assertEqual(dates[-1], expected_last_date)
        self.assertEqual(values[-1], 1000.0 + 164)
        # dropped the oldest 5 points to fit the 160-point window
        self.assertNotIn(
            datetime.fromtimestamp(timestamps[0], tz=JST).date().isoformat(), dates
        )
        # ascending and within range
        self.assertEqual(dates, sorted(dates))

    def test_close_missing_becomes_null(self):
        timestamps, closes = _weekly_series(3)
        closes[1] = None
        dates, values = prices.parse_chart_series(
            _chart_payload(timestamps, closes), window=prices.WEEKLY_WINDOW, context="t"
        )
        self.assertEqual(values, [1000.0, None, 1002.0])

    def test_non_ascending_dates_are_rejected(self):
        timestamps, closes = _weekly_series(3)
        timestamps[1], timestamps[2] = timestamps[2], timestamps[1]
        with self.assertRaises(prices.PriceCollectorError):
            prices.parse_chart_series(
                _chart_payload(timestamps, closes),
                window=prices.WEEKLY_WINDOW,
                context="t",
            )

    def test_trailing_live_quote_on_same_day_collapses_into_one_point(self):
        # Simulates a Tuesday 08:30 JST cron run before market open: the
        # in-progress weekly bar (period-start timestamp) and Yahoo's
        # appended live quote both land on the same still-open Monday in
        # JST, exactly like the pattern seen in the real --limit 2 smoke
        # test. The later (live) value must win and the strict-ascending
        # check must see only one point for that date, not a duplicate.
        def jst(year, month, day, hour=0):
            return int(datetime(year, month, day, hour, tzinfo=JST).timestamp())

        timestamps = [jst(2026, 7, 6), jst(2026, 7, 13), jst(2026, 7, 20), jst(2026, 7, 20, 23)]
        closes = [1200.0, 1250.0, 1266.0, 1300.0]
        dates, values = prices.parse_chart_series(
            _chart_payload(timestamps, closes), window=prices.WEEKLY_WINDOW, context="t"
        )
        self.assertEqual(dates, ["2026-07-06", "2026-07-13", "2026-07-20"])
        self.assertEqual(values, [1200.0, 1250.0, 1300.0])

    def test_trailing_live_quote_with_null_close_does_not_erase_real_value(self):
        # If the appended live point has a null close (e.g. no trade yet
        # today), collapsing must NOT overwrite the already-fetched real
        # close for that date with null -- that would silently destroy good
        # data (reviewer-observed regression: 1266.0 -> None).
        def jst(year, month, day, hour=0):
            return int(datetime(year, month, day, hour, tzinfo=JST).timestamp())

        timestamps = [jst(2026, 7, 6), jst(2026, 7, 13), jst(2026, 7, 20), jst(2026, 7, 20, 23)]
        closes = [1200.0, 1250.0, 1266.0, None]
        dates, values = prices.parse_chart_series(
            _chart_payload(timestamps, closes), window=prices.WEEKLY_WINDOW, context="t"
        )
        self.assertEqual(dates, ["2026-07-06", "2026-07-13", "2026-07-20"])
        self.assertEqual(values, [1200.0, 1250.0, 1266.0])

