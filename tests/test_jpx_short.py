import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest import mock

from collector import backfill_jsda, jpx_short


FIXTURES = Path(__file__).resolve().parent / "fixtures"
WORKBOOK = FIXTURES / "20260721_Short_Positions.xls"
INDEX = FIXTURES / "jpx_short_index_sample.html"
GENERATED_AT = "2026-07-22T09:00:00Z"


class FixtureDownloader:
    def __init__(self):
        self.calls = []

    def fetch(self, url, destination):
        self.calls.append(url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if Path(url).name != WORKBOOK.name:
            raise AssertionError(f"unexpected download: {url}")
        shutil.copyfile(WORKBOOK, destination)
        return destination


class JPXShortTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        network_guard = mock.patch.object(
            backfill_jsda,
            "urlopen",
            side_effect=AssertionError("tests must not access the network"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)

    def _seed_meta(self, out, latest="2026-07-17"):
        out.mkdir(parents=True, exist_ok=True)
        (out / "short_meta.json").write_text(
            json.dumps(
                {
                    "schema_version": jpx_short.SHORT_META_SCHEMA_VERSION,
                    "latest_short_date": latest,
                    "generated_at": "2026-07-18T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )

    def _run_new_date(self, out=None, cache=None):
        out = out or self.root / "data"
        cache = cache or self.root / "cache"
        self._seed_meta(out)
        downloader = FixtureDownloader()
        updated = jpx_short.run_update(
            out,
            cache,
            index_html_path=INDEX,
            downloader=downloader,
            generated_at=GENERATED_AT,
        )
        return out, cache, downloader, updated

    def test_discovers_random_token_links_and_rejects_external_host(self):
        links = jpx_short.discover_short_urls(INDEX.read_text(encoding="utf-8"))
        self.assertEqual(sorted(links), [date(2026, 7, 17), date(2026, 7, 21)])
        self.assertEqual(
            links[date(2026, 7, 21)],
            "https://www.jpx.co.jp/markets/public/short-selling/"
            "t13vrt000001joh3-att/20260721_Short_Positions.xls",
        )

    def test_parses_real_biff_float_and_string_codes_dates_and_values(self):
        self.assertLess(WORKBOOK.stat().st_size, 500_000)
        issues = jpx_short.parse_short_workbook(WORKBOOK)

        self.assertIn("1375", issues)
        self.assertIn("584A", issues)
        self.assertEqual(issues["1375"]["name"], "ユキグニファクトリー")
        first = issues["1375"]["events"][0]
        self.assertEqual(first["date"], "2026-07-16")
        self.assertAlmostEqual(first["ratio"], 0.0055)
        self.assertEqual(first["qty"], 222_007)
        self.assertEqual(first["seller"], "Barclays Capital Securities Ltd")
        self.assertIsInstance(first["ratio"], float)
        self.assertEqual(jpx_short._excel_date(46219.0, 0, "test"), "2026-07-16")
        grouped = [
            event
            for event in issues["402A"]["events"]
            if event["date"] == "2026-07-17"
            and event["seller"] == "Arrowstreet Capital, Limited Partnership"
        ]
        self.assertEqual(len(grouped), 1)
        self.assertAlmostEqual(grouped[0]["ratio"], 0.0142)
        self.assertEqual(grouped[0]["qty"], 951_600)

    def test_only_dates_newer_than_short_meta_are_downloaded(self):
        out, _, downloader, updated = self._run_new_date()

        self.assertEqual(updated, ("2026-07-21",))
        self.assertEqual(len(downloader.calls), 1)
        self.assertTrue(downloader.calls[0].endswith(WORKBOOK.name))
        meta = json.loads((out / "short_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["latest_short_date"], "2026-07-21")
        self.assertEqual(meta["generated_at"], GENERATED_AT)

    def test_second_import_is_idempotent_and_events_are_date_sorted(self):
        out, cache, downloader, _ = self._run_new_date()
        before = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in out.rglob("*.json")
        }

        updated = jpx_short.run_update(
            out,
            cache,
            index_html_path=INDEX,
            downloader=downloader,
            generated_at="2026-07-23T00:00:00Z",
        )
        after = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in out.rglob("*.json")
        }

        self.assertEqual(updated, ())
        self.assertEqual(before, after)
        shard = json.loads((out / "short" / "13.json").read_text(encoding="utf-8"))
        events = shard["issues"]["1375"]["events"]
        self.assertEqual(
            events,
            sorted(events, key=lambda event: (event["date"], event["seller"])),
        )

    def test_merge_same_snapshot_twice_does_not_duplicate_events(self):
        snapshot = jpx_short.parse_short_workbook(WORKBOOK)
        merged = {}
        jpx_short._merge_issues(merged, snapshot)
        once = json.dumps(merged, ensure_ascii=False, sort_keys=True)
        jpx_short._merge_issues(merged, snapshot)

        self.assertEqual(json.dumps(merged, ensure_ascii=False, sort_keys=True), once)

    def test_writes_only_short_contract_and_preserves_weekly_owned_outputs(self):
        out = self.root / "data"
        out.mkdir()
        sentinels = {
            "meta.json": b"weekly meta",
            "issues.json": b"weekly issues",
            "series/13.json": b"series shard",
            "weekly/2026-07-10.json": b"weekly snapshot",
        }
        for relative, content in sentinels.items():
            path = out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        self._run_new_date(out=out)

        for relative, content in sentinels.items():
            self.assertEqual((out / relative).read_bytes(), content)
        self.assertTrue((out / "short_meta.json").is_file())
        self.assertTrue((out / "short" / "13.json").is_file())

    def test_invalid_code_and_date_fail_loudly(self):
        for value in (1375.5, "13-5", None):
            with self.subTest(value=value), self.assertRaises(jpx_short.JPXShortError):
                jpx_short._normalize_code(value)
        with self.assertRaises(jpx_short.JPXShortError):
            jpx_short._excel_date("2026-07-16", 0, "test")

    def test_header_mismatch_exits_one_without_outputs(self):
        out = self.root / "data"
        cache = self.root / "cache"
        cache.mkdir()
        shutil.copyfile(WORKBOOK, cache / WORKBOOK.name)
        index = self.root / "one-index.html"
        index.write_text(
            '<a href="/random-token/20260721_Short_Positions.xls">file</a>',
            encoding="utf-8",
        )
        stderr = io.StringIO()
        with mock.patch.object(jpx_short, "_HEADER_JA", ("bad",)), redirect_stderr(stderr):
            exit_code = jpx_short._main(
                [
                    "--out",
                    str(out),
                    "--cache-dir",
                    str(cache),
                    "--index-html",
                    str(index),
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("header", stderr.getvalue())
        self.assertFalse((out / "short").exists())
        self.assertFalse((out / "short_meta.json").exists())

    def test_transaction_failure_restores_previous_short_outputs(self):
        out = self.root / "data"
        short = out / "short"
        short.mkdir(parents=True)
        (short / "old.json").write_bytes(b"old shard")
        (out / "short_meta.json").write_bytes(b"old meta")
        shards = {
            "13": {
                "schema_version": jpx_short.SHORT_SCHEMA_VERSION,
                "issues": {},
            }
        }
        meta = {
            "schema_version": jpx_short.SHORT_META_SCHEMA_VERSION,
            "latest_short_date": "2026-07-21",
            "generated_at": GENERATED_AT,
        }
        real_replace = jpx_short.os.replace

        def fail_meta_commit(source, destination):
            source_path = Path(source)
            destination_path = Path(destination)
            if source_path.parent.name == "new" and destination_path == out / "short_meta.json":
                raise OSError("simulated commit failure")
            return real_replace(source, destination)

        with mock.patch.object(jpx_short.os, "replace", side_effect=fail_meta_commit):
            with self.assertRaises(OSError):
                jpx_short._write_outputs(out, shards, meta)

        self.assertEqual((out / "short" / "old.json").read_bytes(), b"old shard")
        self.assertEqual((out / "short_meta.json").read_bytes(), b"old meta")

    def test_cached_downloader_accepts_browser_user_agent_override(self):
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            return io.BytesIO(b"ok")

        downloader = backfill_jsda.CachedDownloader(
            opener=opener,
            sleep=lambda _: None,
            user_agent=jpx_short.JPX_USER_AGENT,
        )
        downloader.fetch("https://example.invalid/file", self.root / "file")

        self.assertTrue(calls[0][0].get_header("User-agent").startswith("Mozilla/5.0"))


if __name__ == "__main__":
    unittest.main()
