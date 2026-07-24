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
        # short/prices(別ビルダー所有)は保全し、消滅shardのseriesは丸ごと入れ替わる
        (self.out_dir / "short").mkdir(parents=True)
        (self.out_dir / "short" / "keep.json").write_text("{}", encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main()
