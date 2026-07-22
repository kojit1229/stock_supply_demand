import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import date
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from collector import backfill_jsda, jsda_weekly


FIXTURES = Path(__file__).resolve().parent / "fixtures"
BACKFILL_FIXTURE = FIXTURES / "jsda_backfill_sample.zip"


class BackfillJSDATests(unittest.TestCase):
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

    def _extract(self, *names):
        paths = []
        with ZipFile(BACKFILL_FIXTURE) as archive:
            for name in names:
                archive.extract(name, self.root)
                paths.append(self.root / name)
        return paths

    def _cache_archive(self, name="202507-12.zip"):
        cache = self.root / "cache"
        cache.mkdir()
        shutil.copyfile(BACKFILL_FIXTURE, cache / name)
        return cache

    def test_completed_half_year_archives_cover_three_year_window(self):
        self.assertEqual(
            backfill_jsda.archive_names(
                date(2023, 7, 22), date(2026, 7, 22), date(2026, 7, 22)
            ),
            [
                "202307-12.zip",
                "202401-06.zip",
                "202407-12.zip",
                "202501-06.zip",
                "202507-12.zip",
                "202601-06.zip",
            ],
        )

    def test_revision_is_preferred_and_non_z_files_are_ignored(self):
        with ZipFile(BACKFILL_FIXTURE) as archive:
            names = archive.namelist() + ["20251017s.xlsx", "20251017j.xlsx"]
        selected = backfill_jsda.select_preferred_z_names(names)
        self.assertEqual(
            selected[date(2025, 10, 17)].filename,
            "20251017z(20251030r).xlsx",
        )

    def test_index_link_discovery_accepts_thursday_and_prefers_revision(self):
        html = """
        <a href="files/20260319z.xlsx">holiday week</a>
        <a href="files/20260501z.xlsx">original</a>
        <a href="files/20260501z(20260514r).xlsx">revised</a>
        <a href="files/20260319s.xlsx">out of scope</a>
        """
        links = backfill_jsda.discover_z_urls(html)
        self.assertEqual(date(2026, 3, 19).weekday(), 3)
        self.assertIn(date(2026, 3, 19), links)
        self.assertTrue(links[date(2026, 5, 1)].endswith("20260501z(20260514r).xlsx"))

    def test_xls_and_xlsx_engines_parse_real_derived_values(self):
        self.assertLess(BACKFILL_FIXTURE.stat().st_size, 500_000)
        xls_path, xlsx_path = self._extract("20250704z.xls", "20250926z.xlsx")
        self.assertEqual(jsda_weekly._workbook_format(xls_path), "xlrd")
        self.assertEqual(jsda_weekly._workbook_format(xlsx_path), "openpyxl")

        old_issues = jsda_weekly.parse_zandaka(xls_path)
        new_issues = jsda_weekly.parse_zandaka(xlsx_path)
        # The audited BIFF8 source returns numeric codes as floats (13010.0).
        self.assertIn("1301", old_issues)
        self.assertIn("1301", new_issues)
        self.assertIn("285A", old_issues)
        self.assertIn("25935", old_issues)
        self.assertIn("25935", new_issues)

    def test_z_only_weekly_contract_accepts_holiday_report_date(self):
        (holiday_path,) = self._extract("20260319z.xlsx")
        document = jsda_weekly.build_z_weekly(
            holiday_path, "2026-03-19", min_issue_count=3
        )
        self.assertEqual(document["report_date"], "2026-03-19")
        self.assertEqual(document["source_files"], ["20260319z.xlsx"])
        self.assertEqual(document["issues"]["285A"]["shinki"], {})

    def test_cached_download_skips_urlopen(self):
        destination = self.root / "cache" / "already.zip"
        destination.parent.mkdir()
        destination.write_bytes(b"cached")
        opener = mock.Mock(side_effect=AssertionError("network must not be used"))
        downloader = backfill_jsda.CachedDownloader(opener=opener)

        self.assertEqual(
            downloader.fetch("https://example.invalid/already.zip", destination),
            destination,
        )
        opener.assert_not_called()

    def test_downloader_retries_with_exponential_backoff_and_user_agent(self):
        calls = []
        sleeps = []
        clock = [0.0]

        def fake_sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        def fake_open(request, timeout):
            calls.append((request, timeout))
            if len(calls) < 3:
                raise OSError("temporary failure")
            return io.BytesIO(b"ok")

        destination = self.root / "download.bin"
        downloader = backfill_jsda.CachedDownloader(
            opener=fake_open, sleep=fake_sleep, monotonic=lambda: clock[0]
        )
        downloader.fetch("https://example.invalid/download.bin", destination)

        self.assertEqual(destination.read_bytes(), b"ok")
        self.assertEqual(sleeps, [5.0, 10.0, 5.0])
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][1], 60)
        self.assertEqual(
            calls[0][0].get_header("User-agent"), jsda_weekly.USER_AGENT
        )

    def test_backfill_builds_weekly_and_shards_with_revised_source(self):
        cache = self._cache_archive()
        output = self.root / "data"
        result = backfill_jsda.run_backfill(
            date(2025, 7, 4),
            date(2025, 10, 17),
            output,
            cache,
            today=date(2026, 7, 22),
            min_issue_count=3,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(
            result.processed_weeks,
            ["2025-07-04", "2025-09-26", "2025-10-17"],
        )
        revised = json.loads(
            (output / "weekly" / "2025-10-17.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            revised["source_files"], ["20251017z(20251030r).xlsx"]
        )
        self.assertEqual(
            revised["issues"]["1301"]["taishaku"]["yutanpo"]["lend_qty"],
            48_539,
        )
        meta = json.loads((output / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["latest_week"], "2025-10-17")
        self.assertTrue((output / "series" / "28.json").is_file())

    def test_current_half_uses_cached_index_discovery(self):
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "index-2026-03-20.html").write_text(
            '<a href="files/20260319z.xlsx">holiday week</a>',
            encoding="utf-8",
        )
        with ZipFile(BACKFILL_FIXTURE) as archive:
            (cache / "20260319z.xlsx").write_bytes(
                archive.read("20260319z.xlsx")
            )

        output = self.root / "data"
        result = backfill_jsda.run_backfill(
            date(2026, 3, 19),
            date(2026, 3, 19),
            output,
            cache,
            today=date(2026, 3, 20),
            min_issue_count=3,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.processed_weeks, ["2026-03-19"])
        self.assertTrue((output / "weekly" / "2026-03-19.json").is_file())

    def test_validation_failure_exits_one_keeps_good_week_and_skips_build(self):
        cache = self._cache_archive()
        output = self.root / "data"
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            exit_code = backfill_jsda._main(
                [
                    "--start",
                    "2025-07-04",
                    "--end",
                    "2025-09-26",
                    "--out",
                    str(output),
                    "--cache-dir",
                    str(cache),
                ]
            )

        self.assertEqual(exit_code, 1)
        self.assertTrue((output / "weekly" / "2025-07-04.json").is_file())
        self.assertFalse((output / "weekly" / "2025-09-26.json").exists())
        self.assertFalse((output / "meta.json").exists())
        message = stderr.getvalue()
        self.assertIn("処理済み週: 2025-07-04", message)
        self.assertIn("2025-09-26", message)
        self.assertIn("銘柄数が下限未満", message)


if __name__ == "__main__":
    unittest.main()
