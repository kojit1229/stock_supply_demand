import io
import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
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

    def _seed_shard(self, out, shard, issues):
        (out / "short").mkdir(parents=True, exist_ok=True)
        (out / "short" / f"{shard}.json").write_text(
            json.dumps(
                {"schema_version": jpx_short.SHORT_SCHEMA_VERSION, "issues": issues},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

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

    def test_main_prints_updated_marker_line_on_success(self):
        # daily.yml は `grep -qx 'UPDATED=1'` の行完全一致でdeploy可否を決める
        out = self.root / "data"
        cache = self.root / "cache"
        self._seed_meta(out)
        downloader = FixtureDownloader()
        stdout = io.StringIO()
        with mock.patch.object(
            jpx_short, "CachedDownloader", return_value=downloader
        ), redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = jpx_short._main(
                [
                    "--out",
                    str(out),
                    "--cache-dir",
                    str(cache),
                    "--index-html",
                    str(INDEX),
                ]
            )
        self.assertEqual(exit_code, 0)
        self.assertIn(jpx_short.UPDATED_MARKER, stdout.getvalue().splitlines())

    def test_main_omits_updated_marker_when_no_new_dates(self):
        # 対象なし(=deploy不要)時にUPDATED=1が出ないことをdeployゲートの逆方向で固定
        out = self.root / "data"
        cache = self.root / "cache"
        self._seed_meta(out, latest="2026-07-21")
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            exit_code = jpx_short._main(
                ["--out", str(out), "--cache-dir", str(cache), "--index-html", str(INDEX)]
            )
        self.assertEqual(exit_code, 0)
        self.assertNotIn(jpx_short.UPDATED_MARKER, stdout.getvalue().splitlines())
        self.assertIn("対象なし", stdout.getvalue())

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

    # ---- 増分10.5: 空売り報告終了イベント -----------------------------

    def test_synthesize_report_endings_flags_seller_absent_from_source(self):
        destination = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {
                        "date": "2026-07-14",
                        "ratio": 0.02,
                        "qty": 1000,
                        "seller": "Foo Capital",
                    }
                ],
            }
        }
        source = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {
                        "date": "2026-07-21",
                        "ratio": 0.03,
                        "qty": 2000,
                        "seller": "Bar Capital",
                    }
                ],
            }
        }
        endings = jpx_short._synthesize_report_endings(
            destination, source, date(2026, 7, 21)
        )
        self.assertEqual(
            endings,
            {
                "1301": {
                    "name": "テスト銘柄",
                    "events": [
                        {
                            "date": "2026-07-21",
                            "ratio": 0.0,
                            "qty": None,
                            "seller": "Foo Capital",
                            "below_threshold": True,
                        }
                    ],
                }
            },
        )

    def test_synthesize_report_endings_is_idempotent_when_already_below_threshold(self):
        destination = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {
                        "date": "2026-07-14",
                        "ratio": 0.0,
                        "qty": None,
                        "seller": "Foo Capital",
                        "below_threshold": True,
                    }
                ],
            }
        }
        source = {}  # 引き続きFoo Capitalは当日スナップショットに不在
        endings = jpx_short._synthesize_report_endings(
            destination, source, date(2026, 7, 21)
        )
        self.assertEqual(endings, {})

    def test_synthesize_report_endings_skips_seller_still_present(self):
        destination = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {
                        "date": "2026-07-14",
                        "ratio": 0.02,
                        "qty": 1000,
                        "seller": "Foo Capital",
                    }
                ],
            }
        }
        source = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {
                        "date": "2026-07-21",
                        "ratio": 0.025,
                        "qty": 1200,
                        "seller": "Foo Capital",
                    }
                ],
            }
        }
        endings = jpx_short._synthesize_report_endings(
            destination, source, date(2026, 7, 21)
        )
        self.assertEqual(endings, {})

    def test_synthesize_report_endings_handles_whole_code_absent_from_source(self):
        destination = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {"date": "2026-07-14", "ratio": 0.02, "qty": 1000, "seller": "A"},
                    {"date": "2026-07-14", "ratio": 0.01, "qty": 500, "seller": "B"},
                ],
            }
        }
        source = {
            "9999": {
                "name": "別銘柄",
                "events": [
                    {"date": "2026-07-21", "ratio": 0.03, "qty": 3000, "seller": "C"}
                ],
            }
        }
        endings = jpx_short._synthesize_report_endings(
            destination, source, date(2026, 7, 21)
        )
        self.assertEqual(sorted(e["seller"] for e in endings["1301"]["events"]), ["A", "B"])
        for event in endings["1301"]["events"]:
            self.assertEqual(event["date"], "2026-07-21")
            self.assertEqual(event["ratio"], 0.0)
            self.assertIsNone(event["qty"])
            self.assertTrue(event["below_threshold"])

    def test_seller_reappearing_after_below_threshold_resumes_normal_events(self):
        # 再登場: below_threshold後に同じ報告者が通常イベントで復帰したら、
        # 最新イベントは通常のものに戻る(run_updateが日毎に行う
        # merge→synthesize の順序をそのまま踏襲して検証する)
        existing = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {"date": "2026-07-07", "ratio": 0.02, "qty": 1000, "seller": "Foo"}
                ],
            }
        }
        day1_snapshot = {"1301": {"name": "テスト銘柄", "events": []}}  # Fooが消える
        jpx_short._merge_issues(existing, day1_snapshot)
        endings1 = jpx_short._synthesize_report_endings(
            existing, day1_snapshot, date(2026, 7, 14)
        )
        jpx_short._merge_issues(existing, endings1)
        latest_after_day1 = max(
            existing["1301"]["events"], key=lambda event: event["date"]
        )
        self.assertTrue(latest_after_day1.get("below_threshold"))

        day2_snapshot = {
            "1301": {
                "name": "テスト銘柄",
                "events": [
                    {"date": "2026-07-21", "ratio": 0.015, "qty": 800, "seller": "Foo"}
                ],
            }
        }
        jpx_short._merge_issues(existing, day2_snapshot)
        endings2 = jpx_short._synthesize_report_endings(
            existing, day2_snapshot, date(2026, 7, 21)
        )
        self.assertEqual(endings2, {})  # 再登場したので合成されない
        latest_after_day2 = max(
            existing["1301"]["events"], key=lambda event: event["date"]
        )
        self.assertEqual(latest_after_day2["date"], "2026-07-21")
        self.assertFalse(latest_after_day2.get("below_threshold"))
        self.assertEqual(latest_after_day2["qty"], 800)

    def test_run_update_synthesizes_below_threshold_for_vanished_seller(self):
        out = self.root / "data"
        cache = self.root / "cache"
        self._seed_meta(out)
        self._seed_shard(
            out,
            "13",
            {
                "1375": {
                    "name": "ユキグニファクトリー",
                    "events": [
                        {
                            "date": "2026-07-10",
                            "ratio": 0.01,
                            "qty": 500,
                            "seller": "Vanished Fund LLC",
                        }
                    ],
                }
            },
        )
        downloader = FixtureDownloader()
        updated = jpx_short.run_update(
            out,
            cache,
            index_html_path=INDEX,
            downloader=downloader,
            generated_at=GENERATED_AT,
        )
        self.assertEqual(updated, ("2026-07-21",))
        shard = json.loads((out / "short" / "13.json").read_text(encoding="utf-8"))
        events = shard["issues"]["1375"]["events"]
        vanished_events = [e for e in events if e["seller"] == "Vanished Fund LLC"]
        self.assertEqual(len(vanished_events), 2)  # 元イベント+合成イベント
        synthesized = [e for e in vanished_events if e.get("below_threshold")]
        self.assertEqual(len(synthesized), 1)
        self.assertEqual(
            synthesized[0],
            {
                "date": "2026-07-21",
                "ratio": 0.0,
                "qty": None,
                "seller": "Vanished Fund LLC",
                "below_threshold": True,
            },
        )
        # 実データに残る報告者は通常どおり(below_thresholdなし)取り込まれる
        real_events = [e for e in events if e["seller"] == "Barclays Capital Securities Ltd"]
        self.assertEqual(len(real_events), 1)
        self.assertNotIn("below_threshold", real_events[0])

    def test_run_update_reloads_below_threshold_events_from_disk(self):
        # 既存shardにbelow_thresholdイベントが保存されていても
        # _load_existing_shards/_validate_eventが正しく再読込できることの確認
        # (増分10.5前は5キーのイベントを拒否していた)
        out = self.root / "data"
        self._seed_shard(
            out,
            "13",
            {
                "1375": {
                    "name": "ユキグニファクトリー",
                    "events": [
                        {
                            "date": "2026-07-10",
                            "ratio": 0.0,
                            "qty": None,
                            "seller": "Vanished Fund LLC",
                            "below_threshold": True,
                        }
                    ],
                }
            },
        )
        reloaded = jpx_short._load_existing_shards(out / "short")
        self.assertEqual(
            reloaded["1375"]["events"][0]["below_threshold"], True
        )
        self.assertIsNone(reloaded["1375"]["events"][0]["qty"])

    def test_below_threshold_event_with_nonzero_ratio_fails_loudly(self):
        with self.assertRaises(jpx_short.JPXShortError):
            jpx_short._validate_event(
                {
                    "date": "2026-07-10",
                    "ratio": 0.01,
                    "qty": None,
                    "seller": "Vanished Fund LLC",
                    "below_threshold": True,
                },
                "1375",
            )

    def test_below_threshold_event_with_qty_fails_loudly(self):
        with self.assertRaises(jpx_short.JPXShortError):
            jpx_short._validate_event(
                {
                    "date": "2026-07-10",
                    "ratio": 0.0,
                    "qty": 100,
                    "seller": "Vanished Fund LLC",
                    "below_threshold": True,
                },
                "1375",
            )

    def test_run_update_does_not_write_or_synthesize_when_any_candidate_day_fails(self):
        # 取得失敗日には合成しない: 候補2日のうち1日でも取得に失敗したら、
        # 成功していたはずのもう1日分も含めて何も書き出さない(既存の
        # フェイルラウド構造がそのまま合成イベントの安全網になっていることの確認)
        out = self.root / "data"
        cache = self.root / "cache"
        # 07-17より前をlatestにし、07-17と07-21の両方を候補にする
        self._seed_meta(out, latest="2026-07-10")

        class PartiallyFailingDownloader:
            def fetch(self, url, destination):
                if "20260717" in url or "2026-07-17" in url:
                    raise RuntimeError("simulated fetch failure for 2026-07-17")
                destination = Path(destination)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(WORKBOOK, destination)
                return destination

        with self.assertRaises(RuntimeError):
            jpx_short.run_update(
                out,
                cache,
                index_html_path=INDEX,
                downloader=PartiallyFailingDownloader(),
                generated_at=GENERATED_AT,
            )

        self.assertFalse((out / "short").exists())
        meta = json.loads((out / "short_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["latest_short_date"], "2026-07-10")  # 書き換わっていない


if __name__ == "__main__":
    unittest.main()
