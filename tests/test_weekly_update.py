import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit

from collector import backfill_jsda, jsda_weekly, weekly_update


FIXTURES = Path(__file__).resolve().parent / "fixtures"
Z_FIXTURE = FIXTURES / "20260710z_sample.xlsx"
S_FIXTURE = FIXTURES / "20260710s_sample.xlsx"


class FixtureDownloader:
    def __init__(self, index_html, *, missing_s=None, invalid_s=None):
        self.index_html = index_html
        self.missing_s = set(missing_s or ())
        self.invalid_s = set(invalid_s or ())
        self.calls = []

    def fetch(self, url, destination):
        target = Path(destination)
        self.calls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        if url == backfill_jsda.INDEX_URL:
            target.write_text(self.index_html, encoding="utf-8")
            return target
        filename = Path(unquote(urlsplit(url).path)).name
        if filename in self.missing_s:
            cause = HTTPError(url, 404, "Not Found", {}, None)
            raise backfill_jsda.BackfillError("取得に失敗しました") from cause
        if filename[8:9].lower() == "z":
            shutil.copyfile(Z_FIXTURE, target)
        elif filename in self.invalid_s:
            shutil.copyfile(Z_FIXTURE, target)
        else:
            shutil.copyfile(S_FIXTURE, target)
        return target


def index_for(*days):
    return "\n".join(
        f'<a href="files/{day.replace("-", "")}z.xlsx">{day}</a>'
        for day in days
    )


class WeeklyUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.data = self.root / "data"
        self.cache = self.data / "_cache"
        guards = [
            mock.patch.object(
                backfill_jsda,
                "urlopen",
                side_effect=AssertionError("tests must not access the network"),
            ),
            mock.patch.object(
                jsda_weekly,
                "urlopen",
                side_effect=AssertionError("tests must not access the network"),
            ),
        ]
        for guard in guards:
            guard.start()
            self.addCleanup(guard.stop)

    def _write_meta(self, latest_week):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "meta.json").write_text(
            json.dumps({"latest_week": latest_week}), encoding="utf-8"
        )

    def test_no_new_week_exits_zero_without_updated_marker(self):
        self._write_meta("2026-07-10")
        downloader = FixtureDownloader(index_for("2026-07-10"))
        with mock.patch.object(
            weekly_update.backfill_jsda,
            "CachedDownloader",
            return_value=downloader,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = weekly_update._main(
                    ["--out", str(self.data), "--cache-dir", str(self.cache)]
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn(weekly_update.UPDATED_MARKER, stdout.getvalue())
        self.assertIn("対象なし", stdout.getvalue())
        self.assertEqual(downloader.calls, [backfill_jsda.INDEX_URL])

    def test_only_weeks_newer_than_meta_are_downloaded_and_built(self):
        self._write_meta("2026-07-03")
        downloader = FixtureDownloader(index_for("2026-07-03", "2026-07-10"))
        result = weekly_update.run_weekly_update(
            self.data, self.cache, downloader=downloader,
            generated_at="2026-07-22T00:00:00Z",
        )

        self.assertEqual(result.updated_weeks, ("2026-07-10",))
        source_calls = [call for call in downloader.calls if call != backfill_jsda.INDEX_URL]
        self.assertEqual(len(source_calls), 2)
        self.assertTrue(all("20260710" in call for call in source_calls))
        weekly = json.loads(
            (self.data / "weekly" / "2026-07-10.json").read_text(encoding="utf-8")
        )
        self.assertEqual(weekly["schema_version"], "supply_demand_weekly_v1")
        self.assertEqual(
            weekly["source_files"], ["20260710z.xlsx", "20260710s.xlsx"]
        )
        self.assertIn("285A", weekly["issues"])
        meta = json.loads((self.data / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["latest_week"], "2026-07-10")

    def test_first_run_uses_only_latest_discovered_week(self):
        downloader = FixtureDownloader(index_for("2026-07-03", "2026-07-10"))
        result = weekly_update.run_weekly_update(
            self.data, self.cache, downloader=downloader
        )
        self.assertEqual(result.updated_weeks, ("2026-07-10",))
        self.assertFalse((self.data / "weekly" / "2026-07-03.json").exists())

    def test_missing_s_week_is_skipped_as_no_target(self):
        downloader = FixtureDownloader(
            index_for("2026-07-10"), missing_s={"20260710s.xlsx"}
        )
        result = weekly_update.run_weekly_update(
            self.data, self.cache, downloader=downloader
        )

        self.assertFalse(result.updated)
        self.assertEqual(result.skipped_weeks, ("2026-07-10",))
        self.assertFalse((self.data / "weekly").exists())
        self.assertFalse((self.data / "meta.json").exists())

    def test_main_prints_updated_marker_line_on_success(self):
        # weekly.yml は `grep -qx 'UPDATED=1'` の行完全一致でdeploy可否を決める
        self._write_meta("2026-07-03")
        downloader = FixtureDownloader(index_for("2026-07-03", "2026-07-10"))
        with mock.patch.object(
            weekly_update.backfill_jsda,
            "CachedDownloader",
            return_value=downloader,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                exit_code = weekly_update._main(
                    ["--out", str(self.data), "--cache-dir", str(self.cache)]
                )
        self.assertEqual(exit_code, 0)
        self.assertIn("UPDATED=1", stdout.getvalue().splitlines())

    def test_commit_failure_rolls_back_existing_weekly_and_meta(self):
        # 既存データがある状態で本番反映(_write_outputs)が失敗しても旧データ無傷
        self._write_meta("2026-06-26")
        downloader0 = FixtureDownloader(index_for("2026-07-03"))
        weekly_update.run_weekly_update(
            self.data, self.cache, downloader=downloader0,
            generated_at="2026-07-15T00:00:00Z",
        )
        before_weekly = (self.data / "weekly" / "2026-07-03.json").read_bytes()
        before_meta = (self.data / "meta.json").read_bytes()

        downloader = FixtureDownloader(index_for("2026-07-03", "2026-07-10"))
        real_write = weekly_update.build_site._write_outputs
        calls = {"n": 0}

        def fail_on_commit(out_dir, outputs):
            # 1回目=stagedへの検証ビルドは通し、2回目=本番反映で失敗させる
            calls["n"] += 1
            if Path(out_dir) == self.data:
                raise OSError("simulated commit failure")
            return real_write(out_dir, outputs)

        with mock.patch.object(
            weekly_update.build_site, "_write_outputs", side_effect=fail_on_commit
        ):
            with self.assertRaises(OSError):
                weekly_update.run_weekly_update(
                    self.data, self.cache, downloader=downloader,
                    generated_at="2026-07-22T00:00:00Z",
                )

        self.assertGreaterEqual(calls["n"], 2)
        self.assertFalse((self.data / "weekly" / "2026-07-10.json").exists())
        self.assertEqual(
            (self.data / "weekly" / "2026-07-03.json").read_bytes(), before_weekly
        )
        self.assertEqual((self.data / "meta.json").read_bytes(), before_meta)

    def test_validation_failure_exits_one_without_output_files(self):
        downloader = FixtureDownloader(
            index_for("2026-07-10"), invalid_s={"20260710s.xlsx"}
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            weekly_update.backfill_jsda,
            "CachedDownloader",
            return_value=downloader,
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = weekly_update._main(
                ["--out", str(self.data), "--cache-dir", str(self.cache)]
            )

        self.assertEqual(exit_code, 1)
        self.assertNotIn(weekly_update.UPDATED_MARKER, stdout.getvalue())
        self.assertIn("対象シートがありません", stderr.getvalue())
        self.assertFalse((self.data / "weekly").exists())
        self.assertFalse((self.data / "meta.json").exists())


if __name__ == "__main__":
    unittest.main()
