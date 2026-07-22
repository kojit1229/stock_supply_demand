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

    def test_non_adjacent_duplicate_date_is_still_rejected(self):
        # A duplicate date that is NOT adjacent to its first occurrence (or
        # any reversed order) must remain a genuine contract violation; only
        # the live-quote collapse above is special-cased.
        def jst(year, month, day):
            return int(datetime(year, month, day, tzinfo=JST).timestamp())

        timestamps = [jst(2026, 7, 6), jst(2026, 7, 13), jst(2026, 7, 6)]
        closes = [1200.0, 1250.0, 1199.0]
        with self.assertRaises(prices.PriceCollectorError):
            prices.parse_chart_series(
                _chart_payload(timestamps, closes),
                window=prices.WEEKLY_WINDOW,
                context="t",
            )

    def test_non_numeric_close_is_rejected(self):
        timestamps, closes = _weekly_series(2)
        closes[0] = "not-a-number"
        with self.assertRaises(prices.PriceCollectorError):
            prices.parse_chart_series(
                _chart_payload(timestamps, closes),
                window=prices.WEEKLY_WINDOW,
                context="t",
            )

    def test_missing_chart_result_is_rejected(self):
        with self.assertRaises(prices.PriceCollectorError):
            prices.parse_chart_series(
                {"chart": {"result": None, "error": {"code": "Not Found"}}},
                window=prices.WEEKLY_WINDOW,
                context="t",
            )


class PricesRunCollectTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        network_guard = mock.patch.object(
            prices,
            "urlopen",
            side_effect=AssertionError("tests must not access the network"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def _write_list(self, codes):
        path = self.root / "price_list.json"
        path.write_text(
            json.dumps({"note": "test", "codes": codes}), encoding="utf-8"
        )
        return path

    def test_all_succeed_writes_prices_and_meta(self):
        codes = ["1101", "1102", "1103"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        result = prices.run_collect(
            out,
            list_path,
            opener=opener,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
            generated_at=GENERATED_AT,
        )

        self.assertEqual(result.total, 3)
        self.assertEqual(result.failures, ())
        for code in codes:
            document = json.loads((out / "prices" / f"{code}.json").read_text())
            self.assertEqual(document["schema_version"], 1)
            self.assertEqual(document["code"], code)
            self.assertEqual(
                len(document["weekly"]["dates"]), len(document["weekly"]["close"])
            )
            self.assertEqual(
                len(document["daily"]["dates"]), len(document["daily"]["close"])
            )
        meta = json.loads((out / "prices_meta.json").read_text())
        self.assertEqual(meta["schema_version"], 1)
        self.assertEqual(meta["generated_at"], GENERATED_AT)
        self.assertEqual(meta["price_count"], 3)
        self.assertEqual(meta["latest_price_date"], result.latest_price_date)

    def test_limit_processes_only_the_leading_n_codes(self):
        codes = ["1101", "1102", "1103", "1104"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        result = prices.run_collect(
            out, list_path, limit=2, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.total, 2)
        self.assertTrue((out / "prices" / "1101.json").exists())
        self.assertTrue((out / "prices" / "1102.json").exists())
        self.assertFalse((out / "prices" / "1103.json").exists())

    def test_full_run_deletes_files_for_codes_removed_from_the_list(self):
        codes = ["1101", "1102"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        prices_dir = out / "prices"
        prices_dir.mkdir(parents=True)
        # A code that used to be in price_list.json but was removed; this
        # file came back via weekly.yml's gh-pages restore step and would
        # otherwise be served forever.
        (prices_dir / "9999.json").write_text(
            json.dumps({"schema_version": 1, "code": "9999"}), encoding="utf-8"
        )
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        result = prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.removed, ("9999",))
        self.assertFalse((prices_dir / "9999.json").exists())
        self.assertTrue((prices_dir / "1101.json").exists())

    def test_limit_run_never_deletes_files_outside_the_processed_prefix(self):
        codes = ["1101", "1102", "1103"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        prices_dir = out / "prices"
        prices_dir.mkdir(parents=True)
        (prices_dir / "9999.json").write_text(
            json.dumps({"schema_version": 1, "code": "9999"}), encoding="utf-8"
        )
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        result = prices.run_collect(
            out, list_path, limit=1, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.removed, ())
        # 9999 is outside the --limit 1 prefix (and no longer in the full
        # list either); a --limit run must leave it untouched regardless.
        self.assertTrue((prices_dir / "9999.json").exists())

    def test_reconcile_preserves_a_failing_codes_existing_file(self):
        # A code that is still IN the list but failed this run must never be
        # treated as "removed from the list" -- only codes absent from
        # price_list.json altogether are reconciliation candidates.
        codes = ["1101", "1102", "1103", "1104", "1105"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        prices_dir = out / "prices"
        prices_dir.mkdir(parents=True)
        sentinel = json.dumps({"schema_version": 1, "code": "1105", "sentinel": True})
        (prices_dir / "1105.json").write_text(sentinel, encoding="utf-8")

        outcomes = _ok_outcomes(codes)
        outcomes[("1105.T", "weekly")] = [RuntimeError("boom")] * prices.MAX_ATTEMPTS
        opener = FakeOpener(outcomes)
        clock = Clock()

        result = prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.removed, ())
        self.assertEqual(
            (prices_dir / "1105.json").read_text(encoding="utf-8"), sentinel
        )

    def test_failure_at_boundary_preserves_existing_file_and_still_writes_meta(self):
        # 1 failure out of 5 codes = 20%, which must NOT exceed the threshold.
        codes = ["1101", "1102", "1103", "1104", "1105"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        prices_dir = out / "prices"
        prices_dir.mkdir(parents=True)
        sentinel = json.dumps({"schema_version": 1, "code": "1105", "sentinel": True})
        (prices_dir / "1105.json").write_text(sentinel, encoding="utf-8")

        outcomes = _ok_outcomes(codes)
        outcomes[("1105.T", "weekly")] = [RuntimeError("boom")] * prices.MAX_ATTEMPTS
        opener = FakeOpener(outcomes)
        clock = Clock()

        result = prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.failures, ("1105",))
        self.assertEqual(
            (prices_dir / "1105.json").read_text(encoding="utf-8"), sentinel
        )
        meta = json.loads((out / "prices_meta.json").read_text())
        self.assertEqual(meta["price_count"], 4)
        # retried MAX_ATTEMPTS times for the failing code's weekly request
        weekly_calls = [c for c in opener.calls if "1105.T" in c and "1wk" in c]
        self.assertEqual(len(weekly_calls), prices.MAX_ATTEMPTS)

    def test_failure_above_threshold_raises_and_skips_meta(self):
        # 2 failures out of 5 codes = 40%, over the 20% threshold.
        codes = ["1101", "1102", "1103", "1104", "1105"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        outcomes = _ok_outcomes(codes)
        outcomes[("1104.T", "weekly")] = [RuntimeError("boom")] * prices.MAX_ATTEMPTS
        outcomes[("1105.T", "weekly")] = [RuntimeError("boom")] * prices.MAX_ATTEMPTS
        opener = FakeOpener(outcomes)
        clock = Clock()

        with self.assertRaises(prices.PriceCollectorError):
            prices.run_collect(
                out, list_path, opener=opener, sleep=clock.sleep,
                monotonic=clock.monotonic, generated_at=GENERATED_AT,
            )

        # The three healthy codes were still written per-code before the
        # threshold check aborted the run (no deploy happens because the
        # step itself now exits non-zero).
        self.assertTrue((out / "prices" / "1101.json").exists())
        self.assertFalse((out / "prices_meta.json").exists())

    def test_never_writes_weekly_or_short_meta_files(self):
        codes = ["1101", "1102", "1103"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        out.mkdir(parents=True)
        (out / "meta.json").write_text('{"sentinel": "weekly"}', encoding="utf-8")
        (out / "short_meta.json").write_text('{"sentinel": "short"}', encoding="utf-8")
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(
            json.loads((out / "meta.json").read_text())["sentinel"], "weekly"
        )
        self.assertEqual(
            json.loads((out / "short_meta.json").read_text())["sentinel"], "short"
        )

    def test_transient_failure_recovers_within_retry_budget(self):
        codes = ["1101"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        outcomes = _ok_outcomes(codes)
        outcomes[("1101.T", "daily")] = [
            RuntimeError("temporary"),
            outcomes[("1101.T", "daily")],
        ]
        opener = FakeOpener(outcomes)
        clock = Clock()

        result = prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.failures, ())
        self.assertTrue((out / "prices" / "1101.json").exists())
        self.assertTrue(clock.sleeps)  # backoff actually slept once

    def test_empty_price_list_is_rejected(self):
        list_path = self._write_list([])
        with self.assertRaises(prices.PriceCollectorError):
            prices.run_collect(self.root / "data", list_path)

    def test_duplicate_code_is_rejected(self):
        list_path = self._write_list(["1101", "1101"])
        with self.assertRaises(prices.PriceCollectorError):
            prices.run_collect(self.root / "data", list_path)

    def test_invalid_code_shape_is_rejected(self):
        list_path = self._write_list(["12"])
        with self.assertRaises(prices.PriceCollectorError):
            prices.run_collect(self.root / "data", list_path)

    def test_five_digit_code_is_accepted(self):
        # 5-digit codes are valid for preferred/class shares under repo
        # CLAUDE.md absolute rule 2 (e.g. Itoen preferred 25935); rejecting
        # them here would abort the whole weekly run over a single valid
        # code addition to price_list.json.
        codes = ["1101", "25935"]
        list_path = self._write_list(codes)
        out = self.root / "data"
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()

        result = prices.run_collect(
            out, list_path, opener=opener, sleep=clock.sleep,
            monotonic=clock.monotonic, generated_at=GENERATED_AT,
        )

        self.assertEqual(result.failures, ())
        document = json.loads((out / "prices" / "25935.json").read_text())
        self.assertEqual(document["code"], "25935")


class PricesCLITests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_main_returns_1_when_run_collect_raises(self):
        list_path = self.root / "price_list.json"
        list_path.write_text(json.dumps({"codes": []}), encoding="utf-8")
        exit_code = prices._main(
            ["--out", str(self.root / "data"), "--list", str(list_path)]
        )
        self.assertEqual(exit_code, 1)

    def test_main_returns_0_on_success(self):
        codes = ["1101"]
        list_path = self.root / "price_list.json"
        list_path.write_text(
            json.dumps({"codes": codes}), encoding="utf-8"
        )
        opener = FakeOpener(_ok_outcomes(codes))
        clock = Clock()
        with mock.patch.object(prices.time, "sleep", clock.sleep), mock.patch.object(
            prices.time, "monotonic", clock.monotonic
        ), mock.patch.object(prices, "urlopen", opener):
            exit_code = prices._main(
                [
                    "--out", str(self.root / "data"),
                    "--list", str(list_path),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertTrue((self.root / "data" / "prices" / "1101.json").exists())


if __name__ == "__main__":
    unittest.main()
