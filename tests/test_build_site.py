import copy
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from collector import build_site, jsda_weekly


FIXTURES = Path(__file__).resolve().parent / "fixtures"
Z_FIXTURE = FIXTURES / "20260710z_sample.xlsx"
S_FIXTURE = FIXTURES / "20260710s_sample.xlsx"
GENERATED_AT = "2026-07-22T12:34:56+09:00"


class BuildSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.weekly_template = jsda_weekly.build_weekly(
            Z_FIXTURE, S_FIXTURE, "2026-07-10"
        )

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.weekly_dir = self.root / "data" / "weekly"
        self.out_dir = self.root / "site" / "data"
        self.weekly_dir.mkdir(parents=True)

    def _write_week(self, report_date, mutate=None, filename=None):
        document = copy.deepcopy(self.weekly_template)
        document["report_date"] = report_date
        if mutate is not None:
            mutate(document)
        path = self.weekly_dir / (filename or f"{report_date}.json")
        path.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    def _read_output(self, relative):
        return json.loads((self.out_dir / relative).read_text(encoding="utf-8"))

    def test_builds_aligned_series_from_real_weekly_fixture(self):
        dates = ["2026-06-26", "2026-07-03", "2026-07-10"]
        self._write_week(dates[2])
        self._write_week(
            dates[0], lambda document: document["issues"]["285A"].update(name="旧キオクシア")
        )
        self._write_week(
            dates[1], lambda document: document["issues"].pop("1301")
        )

        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)

        series_files = sorted((self.out_dir / "series").glob("*.json"))
        self.assertTrue(series_files)
        for path in series_files:
            shard = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(shard["weeks"], dates)
            for issue in shard["issues"].values():
                # 増分11: 既存z系列(4項目)+新規s系列(2項目)いずれも配列長は
                # weeksと一致しなければならない
                for field in (
                    "lend_qty", "own_qty", "ten_qty", "lend_amt",
                    "s_borrow_qty", "s_lend_qty",
                ):
                    self.assertEqual(len(issue[field]), len(dates))

        shard_28 = self._read_output("series/28.json")
        kioxia = shard_28["issues"]["285A"]
        expected_lend_qty = 9_215_104 + 1_393_235
        self.assertEqual(kioxia["lend_qty"], [expected_lend_qty] * len(dates))
        # 増分11回帰なし確認: 既存z系列は上記のとおり不変。s系列は同フィクスチャの
        # shinki生値(yutanpo+mutanpo)から独立に算出される
        expected_s_lend_qty = 5_316_474 + 576_263
        expected_s_borrow_qty = (267_266 + 0) + (2_190_256 + 662_946)
        self.assertEqual(kioxia["s_lend_qty"], [expected_s_lend_qty] * len(dates))
        self.assertEqual(kioxia["s_borrow_qty"], [expected_s_borrow_qty] * len(dates))

        shard_13 = self._read_output("series/13.json")
        self.assertEqual(shard_13["issues"]["1301"]["lend_qty"][1], None)
        self.assertEqual(shard_13["issues"]["1301"]["s_lend_qty"][1], None)
        self.assertEqual(shard_13["issues"]["1301"]["s_borrow_qty"][1], None)

        issues = self._read_output("issues.json")
        self.assertEqual(issues["issues"]["285A"]["name"], "キオクシアホールディングス")
        self.assertEqual(issues["issues"]["285A"]["shard"], "28")
        meta = self._read_output("meta.json")
        self.assertEqual(meta["latest_week"], dates[-1])
        self.assertEqual(meta["weekly_count"], len(dates))
        self.assertEqual(meta["generated_at"], GENERATED_AT)

    def test_shinki_present_issue_sums_borrow_and_lend_across_collateral(self):
        # 増分11: 285Aはyutanpo+mutanpoの両方にshinkiがある(design.md §4の規則:
        # s_lend_qty=貸付新規、s_borrow_qty=借入自己+転貸、いずれも有担保+無担保合算)
        self._write_week("2026-07-10")
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        kioxia = self._read_output("series/28.json")["issues"]["285A"]
        self.assertEqual(kioxia["s_lend_qty"], [5_316_474 + 576_263])
        self.assertEqual(
            kioxia["s_borrow_qty"], [(267_266 + 0) + (2_190_256 + 662_946)]
        )

    def test_shinki_single_collateral_issue_sums_available_side(self):
        # 2560はフィクスチャ上yutanpoのみ(mutanpo不在)。z側の
        # test_single_collateral_week_sums_available_sideと対になる、s側の検証
        self._write_week("2026-07-10")
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        shard = self._read_output("series/25.json")
        issue = shard["issues"]["2560"]
        self.assertEqual(issue["s_lend_qty"], [20])
        self.assertEqual(issue["s_borrow_qty"], [0 + 10])

    def test_shinki_missing_for_issue_yields_null_series_values(self):
        # その週にshinki(s)が全く収録されなかった銘柄(design.md §4「両方nullなら
        # null」)。実運用でも起こりうる形(z側は正常にあるがshinkiだけ空)
        def clear_shinki(document):
            document["issues"]["1301"]["shinki"] = {}

        self._write_week("2026-07-10", clear_shinki)
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        issue = self._read_output("series/13.json")["issues"]["1301"]
        self.assertIsNone(issue["s_lend_qty"][0])
        self.assertIsNone(issue["s_borrow_qty"][0])
        # z側(taishaku)は影響を受けない(shinkiの欠測とtaishakuは無関係)
        self.assertIsNotNone(issue["lend_qty"][0])

    def test_shinki_invalid_measurement_fails_loudly(self):
        # 増分11で新たにshinkiの内部構造も検証対象になったため、taishaku同様に
        # フェイルラウドすることを確認する(design.md/CLAUDE.mdルール1)
        def corrupt_shinki(document):
            document["issues"]["285A"]["shinki"]["yutanpo"]["own_qty"] = "not-an-int"

        self._write_week("2026-07-10", corrupt_shinki)
        with self.assertRaisesRegex(build_site.BuildSiteError, "shinki.yutanpo.own_qty"):
            build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        self.assertFalse(self.out_dir.exists())

    def test_duplicate_week_raises_without_writing_output(self):
        self._write_week("2026-07-10")
        self._write_week("2026-07-10", filename="duplicate.json")

        with self.assertRaisesRegex(build_site.BuildSiteError, "duplicate week"):
            build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)

        self.assertFalse(self.out_dir.exists())

    def test_schema_version_mismatch_raises_without_writing_output(self):
        self._write_week(
            "2026-07-10",
            lambda document: document.update(schema_version="unexpected_schema"),
        )

        with self.assertRaisesRegex(build_site.BuildSiteError, "schema_version mismatch"):
            build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)

        self.assertFalse(self.out_dir.exists())

    def test_meta_has_exact_key_set(self):
        self._write_week("2026-07-10")
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        meta = self._read_output("meta.json")
        self.assertEqual(
            set(meta),
            {"schema_version", "latest_week", "generated_at", "issue_count", "weekly_count"},
        )

    def test_filename_mismatch_raises(self):
        self._write_week("2026-07-10", filename="2026-07-11.json")
        with self.assertRaisesRegex(build_site.BuildSiteError, "does not match filename"):
            build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)

    def test_single_collateral_week_sums_available_side(self):
        def drop_mutanpo(document):
            document["issues"]["285A"]["taishaku"].pop("mutanpo")

        self._write_week("2026-07-10", drop_mutanpo)
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        kioxia = self._read_output("series/28.json")["issues"]["285A"]
        self.assertEqual(kioxia["lend_qty"], [9_215_104])

    def test_issue_appearing_and_disappearing_within_window(self):
        dates = ["2026-06-26", "2026-07-03", "2026-07-10"]
        # 1301は最終週まで消滅、285Aは途中から登場
        self._write_week(dates[0], lambda d: d["issues"].pop("285A"))
        self._write_week(dates[1], lambda d: d["issues"].pop("1301"))
        self._write_week(dates[2], lambda d: d["issues"].pop("1301"))
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        s13 = self._read_output("series/13.json")["issues"]["1301"]
        self.assertIsNone(s13["lend_qty"][1])
        self.assertIsNone(s13["lend_qty"][2])
        s28 = self._read_output("series/28.json")["issues"]["285A"]
        self.assertIsNone(s28["lend_qty"][0])
        self.assertIsNotNone(s28["lend_qty"][1])

    def test_sibling_dirs_preserved_and_stale_shards_removed(self):
        # short/prices(別ビルダー所有)は保全し、消滅shardのseriesは丸ごと入れ替わる。
        # 増分13でbuild_siteがsignals.json算出のためshort/を読むようになったため、
        # sentinelも(空でよいので)最小限の妥当なshort shard形にしておく
        (self.out_dir / "short").mkdir(parents=True)
        (self.out_dir / "short" / "keep.json").write_text(
            json.dumps({"schema_version": "supply_demand_short_v1", "issues": {}}),
            encoding="utf-8",
        )
        (self.out_dir / "series").mkdir()
        (self.out_dir / "series" / "ZZ.json").write_text("{}", encoding="utf-8")
        self._write_week("2026-07-10")
        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        self.assertTrue((self.out_dir / "short" / "keep.json").exists())
        self.assertFalse((self.out_dir / "series" / "ZZ.json").exists())
        self.assertTrue((self.out_dir / "series" / "28.json").exists())

    def test_commit_failure_restores_previous_outputs(self):
        self._write_week("2026-07-10")
        build_site.build_site(self.weekly_dir, self.out_dir, "2026-07-01T00:00:00Z")
        original_meta = self._read_output("meta.json")

        real_replace = build_site.os.replace

        def failing_replace(src, dst):
            # commit段階(stage/new→out_dir)のmeta.json置換のみ失敗させる
            if str(dst).endswith("meta.json") and "new" in str(src):
                raise OSError("injected failure")
            return real_replace(src, dst)

        build_site.os.replace = failing_replace
        try:
            with self.assertRaises(OSError):
                build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)
        finally:
            build_site.os.replace = real_replace

        self.assertEqual(self._read_output("meta.json"), original_meta)
        self.assertTrue((self.out_dir / "issues.json").exists())
        self.assertTrue((self.out_dir / "series" / "28.json").exists())

    def test_more_than_160_weeks_drops_oldest(self):
        first = date(2023, 1, 6)
        all_dates = [(first + timedelta(weeks=index)).isoformat() for index in range(161)]
        for report_date in all_dates:
            self._write_week(report_date)

        build_site.build_site(self.weekly_dir, self.out_dir, GENERATED_AT)

        shard = self._read_output("series/28.json")
        self.assertEqual(len(shard["weeks"]), 160)
        self.assertEqual(shard["weeks"], all_dates[1:])
        self.assertEqual(len(shard["issues"]["285A"]["lend_qty"]), 160)
        meta = self._read_output("meta.json")
        self.assertEqual(meta["weekly_count"], 160)
        self.assertEqual(meta["latest_week"], all_dates[-1])

    # ---- 増分13: signals.json ----------------------------------------
    # index.htmlのcomputeBorrowIndicator/computeShortIndicator/computeSignalBadge
    # と同一閾値(境界値含む)であることを、まず純粋関数レベルで固定化する。

    def test_borrow_indicator_boundary_plus_10_percent_is_increase(self):
        borrow = [100, 100, 100, 100, 1000, 100, 100, 100, 1100]  # 9件=有効週8以上
        indicator = build_site._compute_borrow_indicator(borrow)
        self.assertEqual(indicator["status"], "ok")
        self.assertAlmostEqual(indicator["change_ratio"], 0.10)
        self.assertEqual(indicator["direction"], "increase")

    def test_borrow_indicator_boundary_minus_10_percent_is_decrease(self):
        borrow = [100, 100, 100, 100, 1000, 100, 100, 100, 900]
        indicator = build_site._compute_borrow_indicator(borrow)
        self.assertAlmostEqual(indicator["change_ratio"], -0.10)
        self.assertEqual(indicator["direction"], "decrease")

    def test_borrow_indicator_just_inside_threshold_is_neutral(self):
        # ±10%のすぐ内側(9.99%)はneutral(境界の反対側も固定化)
        borrow = [100, 100, 100, 100, 1000, 100, 100, 100, 1099]
        indicator = build_site._compute_borrow_indicator(borrow)
        self.assertEqual(indicator["direction"], "neutral")

    def test_borrow_indicator_insufficient_at_exactly_7_valid_weeks(self):
        borrow = [1, 2, 3, 4, 5, 6, 7]  # 有効週7 < MIN_VALID_WEEKS(8)
        indicator = build_site._compute_borrow_indicator(borrow)
        self.assertEqual(indicator["status"], "insufficient")
        self.assertEqual(indicator["direction"], "neutral")
        self.assertIsNone(indicator["change_ratio"])

    def test_borrow_indicator_ok_at_exactly_8_valid_weeks(self):
        # 7週の対になる境界(8週ちょうどはinsufficientから外れる)
        borrow = [1, 2, 3, 4, 100, 6, 7, 110]
        indicator = build_site._compute_borrow_indicator(borrow)
        self.assertNotEqual(indicator["status"], "insufficient")

    def test_short_indicator_boundary_plus_0_5pt_is_increase(self):
        # (0.020-0.015)*100 はIEEE754上0.5000000000000001になる(index.htmlの
        # JS実装も同じ浮動小数点演算なので、この境界挙動はポート元と一致させる
        # べき仕様。0.010/0.015のような組は(0.015-0.010)*100=0.49999999999999994
        # に丸め落ちして境界を跨がないため使わない)
        events = [
            {"date": "2026-06-20", "ratio": 0.015, "seller": "A"},  # prior基準日以前
            {"date": "2026-07-22", "ratio": 0.020, "seller": "A"},  # latest
        ]
        indicator = build_site._compute_short_indicator(events)
        self.assertEqual(indicator["status"], "ok")
        self.assertEqual(indicator["direction"], "increase")

    def test_short_indicator_boundary_minus_0_5pt_is_decrease(self):
        events = [
            {"date": "2026-06-20", "ratio": 0.020, "seller": "A"},
            {"date": "2026-07-22", "ratio": 0.015, "seller": "A"},
        ]
        indicator = build_site._compute_short_indicator(events)
        self.assertEqual(indicator["direction"], "decrease")

    def test_short_indicator_just_inside_threshold_is_neutral(self):
        events = [
            {"date": "2026-06-20", "ratio": 0.010, "seller": "A"},
            {"date": "2026-07-22", "ratio": 0.0149, "seller": "A"},  # +0.49pt
        ]
        indicator = build_site._compute_short_indicator(events)
        self.assertEqual(indicator["direction"], "neutral")

    def test_signal_below_threshold_only_sums_to_zero_short_is_false(self):
        series = {"own_qty": [1, 1, 1, 1, 1, 1, 1, 1], "ten_qty": [1, 1, 1, 1, 1, 1, 1, 1]}
        events = [
            {"date": "2026-07-22", "ratio": 0.0, "qty": None, "seller": "A", "below_threshold": True}
        ]
        signal = build_site._compute_signal(series, events, set(), "1301")
        self.assertFalse(signal["short"])

    def test_signal_no_short_reports_is_false_and_neutral(self):
        series = {"own_qty": [None] * 8, "ten_qty": [None] * 8}
        signal = build_site._compute_signal(series, [], set(), "1301")
        self.assertFalse(signal["short"])
        self.assertEqual(signal["badge"], "insufficient")  # borrowも欠測のため

    def test_signal_price_flag_reflects_price_list_membership(self):
        series = {"own_qty": [None] * 8, "ten_qty": [None] * 8}
        self.assertTrue(build_site._compute_signal(series, [], {"1301"}, "1301")["price"])
        self.assertFalse(build_site._compute_signal(series, [], {"9999"}, "1301")["price"])

    def test_load_price_codes_missing_file_returns_empty_set(self):
        self.assertEqual(build_site._load_price_codes(self.root / "no-such.json"), set())

    def test_load_price_codes_malformed_file_fails_loudly(self):
        path = self.root / "price_list.json"
        path.write_text("not json", encoding="utf-8")
        with self.assertRaises(build_site.BuildSiteError):
            build_site._load_price_codes(path)

    def test_load_short_events_missing_directory_returns_empty_map(self):
        self.assertEqual(build_site._load_short_events(self.root / "no-such-dir"), {})

    def test_load_short_events_malformed_shard_fails_loudly(self):
        short_dir = self.root / "short"
        short_dir.mkdir()
        (short_dir / "13.json").write_text('{"schema_version": "x"}', encoding="utf-8")
        with self.assertRaises(build_site.BuildSiteError):
            build_site._load_short_events(short_dir)

    # ---- 増分13: build_site()経由の配線確認(実fixture) ------------------

    def test_signals_json_schema_and_price_short_wiring(self):
        dates = ["2026-06-26", "2026-07-03", "2026-07-10"]
        for report_date in dates:
            self._write_week(report_date)

        price_list_path = self.root / "price_list.json"
        price_list_path.write_text(json.dumps({"codes": ["285A"]}), encoding="utf-8")
        short_dir = self.root / "short_data"
        short_dir.mkdir()
        (short_dir / "28.json").write_text(
            json.dumps(
                {
                    "schema_version": "supply_demand_short_v1",
                    "issues": {
                        "285A": {
                            "name": "キオクシアホールディングス",
                            "events": [],  # 報告履歴なし
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        build_site.build_site(
            self.weekly_dir,
            self.out_dir,
            GENERATED_AT,
            short_dir=short_dir,
            price_list_path=price_list_path,
        )

        signals = self._read_output("signals.json")
        self.assertEqual(signals["schema_version"], 1)
        self.assertEqual(signals["week"], dates[-1])
        kioxia = signals["issues"]["285A"]
        self.assertEqual(set(kioxia), {"badge", "borrow_chg", "short", "price"})
        self.assertTrue(kioxia["price"])
        self.assertFalse(kioxia["short"])  # events空=報告なし
        koyo = signals["issues"]["1301"]
        self.assertFalse(koyo["price"])
        # 順位・スコアは持たせない契約: 4フィールド以外何も無い
        for issue in signals["issues"].values():
            self.assertEqual(set(issue), {"badge", "borrow_chg", "short", "price"})

    def test_signals_all_short_false_when_short_dir_missing(self):
        self._write_week("2026-07-10")
        build_site.build_site(
            self.weekly_dir,
            self.out_dir,
            GENERATED_AT,
            price_list_path=self.root / "no-such-price-list.json",
        )  # short_dir未指定 -> out_dir/short(存在しない)に既定される

        signals = self._read_output("signals.json")
        self.assertTrue(signals["issues"])
        for issue in signals["issues"].values():
            self.assertFalse(issue["short"])
            self.assertFalse(issue["price"])

    def test_signals_writer_does_not_disturb_other_outputs(self):
        # 増分13追加後も既存出力(series/issues/meta)は無傷であることの回帰確認
        self._write_week("2026-07-10")
        build_site.build_site(
            self.weekly_dir,
            self.out_dir,
            GENERATED_AT,
            price_list_path=self.root / "no-such-price-list.json",
        )
        self.assertTrue((self.out_dir / "issues.json").is_file())
        self.assertTrue((self.out_dir / "meta.json").is_file())
        self.assertTrue((self.out_dir / "series" / "28.json").is_file())
        self.assertTrue((self.out_dir / "signals.json").is_file())

    # ---- reviewer指摘B(2026-07-25、差し戻し分): 不正日付イベントの寛容フィルタ

    def test_short_indicator_malformed_date_event_is_ignored_not_crashed(self):
        # "2026-07-99"は^\d{4}-\d{2}-\d{2}$の形には合致するが暦として存在しない。
        # 未捕捉ValueErrorで週次ビルド全体を落とさず、そのイベントだけ無視する
        events = [
            {"date": "2026-07-99", "ratio": 0.5, "seller": "Broken"},
            {"date": "2026-06-20", "ratio": 0.010, "seller": "A"},
            {"date": "2026-07-22", "ratio": 0.020, "seller": "A"},
        ]
        indicator = build_site._compute_short_indicator(events)  # 例外にならない
        self.assertEqual(indicator["status"], "ok")
        self.assertEqual(indicator["direction"], "increase")

    def test_short_indicator_only_malformed_dates_degrades_to_none(self):
        events = [
            {"date": "2026-07-99", "ratio": 0.5, "seller": "Broken"},
            {"date": "not-a-date", "ratio": 0.2, "seller": "AlsoBroken"},
        ]
        indicator = build_site._compute_short_indicator(events)
        self.assertEqual(indicator["status"], "none")
        self.assertEqual(indicator["direction"], "neutral")

    def test_is_valid_ymd_rejects_shape_only_matches(self):
        self.assertFalse(build_site._is_valid_ymd("2026-07-99"))
        self.assertFalse(build_site._is_valid_ymd("2026-13-01"))
        self.assertFalse(build_site._is_valid_ymd("not-a-date"))
        self.assertFalse(build_site._is_valid_ymd(20260722))
        self.assertTrue(build_site._is_valid_ymd("2026-07-22"))

    def test_build_site_does_not_crash_when_short_shard_has_malformed_event(self):
        # 統合確認: 壊れたイベントが1件混ざっていてもbuild_site全体は落ちない
        self._write_week("2026-07-10")
        short_dir = self.root / "short"
        short_dir.mkdir()
        (short_dir / "28.json").write_text(
            json.dumps(
                {
                    "schema_version": "supply_demand_short_v1",
                    "issues": {
                        "285A": {
                            "name": "キオクシアホールディングス",
                            "events": [
                                {
                                    "date": "2026-07-99",
                                    "ratio": 0.5,
                                    "qty": 1,
                                    "seller": "Broken",
                                },
                                {
                                    "date": "2026-07-08",
                                    "ratio": 0.012,
                                    "qty": 1000,
                                    "seller": "A",
                                },
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        build_site.build_site(
            self.weekly_dir,
            self.out_dir,
            GENERATED_AT,
            short_dir=short_dir,
            price_list_path=self.root / "no-such-price-list.json",
        )
        signals = self._read_output("signals.json")
        self.assertTrue(signals["issues"]["285A"]["short"])  # 有効イベント分は反映

    # ---- reviewer指摘C(2026-07-25、差し戻し分): バッジ組合せの直接検証 ------

    def _indicator(self, direction, status="ok"):
        return {"status": status, "direction": direction}

    def test_badge_pressure_up_requires_both_increase(self):
        badge = build_site._compute_badge(
            self._indicator("increase"), self._indicator("increase")
        )
        self.assertEqual(badge, "pressure-up")

    def test_badge_covering_decrease_decrease(self):
        badge = build_site._compute_badge(
            self._indicator("decrease"), self._indicator("decrease")
        )
        self.assertEqual(badge, "covering")

    def test_badge_covering_decrease_neutral(self):
        badge = build_site._compute_badge(
            self._indicator("decrease"), self._indicator("neutral")
        )
        self.assertEqual(badge, "covering")

    def test_badge_covering_neutral_decrease(self):
        badge = build_site._compute_badge(
            self._indicator("neutral"), self._indicator("decrease")
        )
        self.assertEqual(badge, "covering")

    def test_badge_neutral_for_mixed_or_all_neutral_combinations(self):
        mixed_combinations = [
            ("increase", "decrease"),
            ("increase", "neutral"),
            ("neutral", "increase"),
            ("decrease", "increase"),
            ("neutral", "neutral"),
        ]
        for d1, d3 in mixed_combinations:
            with self.subTest(d1=d1, d3=d3):
                badge = build_site._compute_badge(
                    self._indicator(d1), self._indicator(d3)
                )
                self.assertEqual(badge, "neutral")

    def test_badge_insufficient_overrides_short_direction(self):
        badge = build_site._compute_badge(
            {"status": "insufficient", "direction": "neutral"},
            self._indicator("increase"),
        )
        self.assertEqual(badge, "insufficient")


if __name__ == "__main__":
    unittest.main()
