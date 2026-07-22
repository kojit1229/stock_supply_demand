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
