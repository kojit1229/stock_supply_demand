import csv
import io
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from collector import jsf_taishaku


FIXTURES = Path(__file__).resolve().parent / "fixtures"
ZANDAKA_SAMPLE = FIXTURES / "zandaka_sample.csv"
GENERATED_AT = "2026-07-23T00:00:00Z"
# 実CSV(4,751行)を数十行に縮小したfixtureなので、本番の3,000行下限は
# 意図的に満たさない。パース単体テストではこの縮小後の行数を下限として渡す。
SMALL_MIN_ROWS = 25

_OTHER_WRITERS_SENTINELS = {
    "meta.json": b"weekly meta",
    "issues.json": b"weekly issues",
    "series/13.json": b"series shard",
    "weekly/2026-07-17.json": b"weekly snapshot",
    "short/13.json": b"short shard",
    "short_meta.json": b"short meta",
    "prices/1301.json": b"price series",
    "prices_meta.json": b"prices meta",
}


def _rewrite_report_type(text: str, report_type: str) -> str:
    """Return a copy of a zandaka.csv body with column 6 (速報／確報) replaced."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header, data = rows[0], rows[1:]
    for row in data:
        row[6] = report_type
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(data)
    return out.getvalue()


def _with_apply_date(text: str, apply_date: str, settle_date: str) -> str:
    """Return a copy of a zandaka.csv body with 申込日/決済日 rewritten for every
    row (JSDA slash format, e.g. '2026/07/23'). Used to synthesize a second
    (or later) day's snapshot from the single-day real fixture."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header, data = rows[0], rows[1:]
    for row in data:
        row[0] = apply_date
        row[1] = settle_date
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(data)
    return out.getvalue()


def _mutate_rows(text, mutate):
    """Apply ``mutate(data_rows)`` in place and re-render the CSV body."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header, data = rows[0], rows[1:]
    mutate(data)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(data)
    return out.getvalue()


def _padded_over_row_threshold(text: str) -> str:
    """Pad a zandaka.csv body with duplicated non-Tokyo filler rows so the
    real production row-count threshold (MIN_DATA_ROWS) is exceeded. Used only
    for CLI-level tests, which cannot override the threshold (the CLI has no
    such flag, matching the spec's fixed `--out [--source]` surface). Filler
    rows reuse an existing non-Tokyo row verbatim, so they carry the same
    apply_date/settle_date/report_type and are dropped by the Tokyo filter,
    keeping the resulting snapshot identical to the un-padded fixture."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    header, data = rows[0], rows[1:]
    filler = next(row for row in data if row[4] != jsf_taishaku.TOKYO_EXCHANGE_LABEL)
    padded = data + [list(filler) for _ in range(jsf_taishaku.MIN_DATA_ROWS)]
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(padded)
    return out.getvalue()


class JSFTaishakuTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        network_guard = mock.patch.object(
            jsf_taishaku,
            "urlopen",
            side_effect=AssertionError("tests must not access the network"),
        )
        network_guard.start()
        self.addCleanup(network_guard.stop)
        self.fixture_text = ZANDAKA_SAMPLE.read_bytes().decode("cp932")

    def _write_source(self, text: str, name: str = "zandaka.csv") -> Path:
        path = self.root / name
        path.write_bytes(text.encode("cp932"))
        return path

    # ---- parsing ---------------------------------------------------

    def test_parses_tokyo_rows_only_with_null_conversion_and_code_passthrough(self):
        snapshot = jsf_taishaku.parse_zandaka_text(
            self.fixture_text, min_data_rows=SMALL_MIN_ROWS
        )

        self.assertEqual(snapshot["schema_version"], 1)
        self.assertEqual(snapshot["apply_date"], "2026-07-22")
        self.assertEqual(snapshot["settle_date"], "2026-07-24")
        self.assertEqual(snapshot["report_type"], "確報")
        self.assertEqual(snapshot["issue_count"], len(snapshot["issues"]))
        self.assertEqual(snapshot["issue_count"], 29)

        koyo = snapshot["issues"]["1301"]
        self.assertEqual(koyo["name"], "極洋")
        self.assertEqual(koyo["yushi_zan"], 8200)
        self.assertIsNone(koyo["seido_kai"])
        self.assertIsNone(koyo["seido_uri"])

        # 日証金のコードは最初から最終形(JSDAの285A0→285A規則はここには存在しない)。
        # 5桁優先株コードはそのまま独立銘柄として収録される
        self.assertIn("25935", snapshot["issues"])
        self.assertEqual(
            snapshot["issues"]["25935"]["name"], "伊藤園第１種優先株式"
        )
        for code in ("50765", "75505", "92015", "92025", "94345", "94346"):
            self.assertIn(code, snapshot["issues"])

        # 全角スペースを含む銘柄名は連続空白圧縮で正規化される
        self.assertNotIn("　", snapshot["issues"]["1397"]["name"])

    def test_regional_exchange_rows_are_dropped_even_for_duplicate_codes(self):
        # フィクスチャの1319は東証+名証+福証+札証の4行で同一コード。
        # 東証行だけが採用され、地方単独上場のためのコード重複はエラーにならない
        snapshot = jsf_taishaku.parse_zandaka_text(
            self.fixture_text, min_data_rows=SMALL_MIN_ROWS
        )
        self.assertIn("1319", snapshot["issues"])
        self.assertEqual(snapshot["issues"]["1319"]["name"], "ＮＦ日経３００".replace("　", " "))

    def test_normalize_code_accepts_four_digit_codes_unchanged(self):
        self.assertEqual(jsf_taishaku._normalize_code("1301"), "1301")

    def test_normalize_code_accepts_five_digit_preferred_share_codes_unchanged(self):
        # 監督者が実CSVで検証済み: 5桁優先株7銘柄はいずれも末尾が'0'以外
        for code in ("25935", "50765", "75505", "92015", "92025", "94345", "94346"):
            with self.subTest(code=code):
                self.assertEqual(jsf_taishaku._normalize_code(code), code)

    def test_normalize_code_rejects_five_digit_trailing_zero_as_unknown_format(self):
        # JSDAの「285A0→285A」4桁化規則は日証金には存在しない(捏造仕様だった)。
        # 5桁末尾'0'が来た場合は切り詰めず、未知フォーマットとしてフェイルラウドする
        with self.assertRaises(jsf_taishaku.TaishakuError):
            jsf_taishaku._normalize_code("285A0")

    def test_header_mismatch_fails_loudly(self):
        reader = csv.reader(io.StringIO(self.fixture_text))
        rows = list(reader)
        rows[0][0] = "不正な列名"
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerows(rows)
        broken_text = out.getvalue()

        with self.assertRaisesRegex(jsf_taishaku.TaishakuError, "ヘッダ"):
            jsf_taishaku.parse_zandaka_text(broken_text, min_data_rows=SMALL_MIN_ROWS)

    def test_insufficient_row_count_fails_with_default_threshold(self):
        # 既定の下限(3,000)に対し、数十行に縮小したfixtureはそのまま失敗するはず
        with self.assertRaisesRegex(jsf_taishaku.TaishakuError, "行数"):
            jsf_taishaku.parse_zandaka_text(self.fixture_text)

    def test_numeric_parse_failure_fails_loudly(self):
        reader = csv.reader(io.StringIO(self.fixture_text))
        rows = list(reader)
        rows[1][9] = "N/A"  # 融資残高株数を壊す
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerows(rows)
        broken_text = out.getvalue()

        with self.assertRaises(jsf_taishaku.TaishakuError):
            jsf_taishaku.parse_zandaka_text(broken_text, min_data_rows=SMALL_MIN_ROWS)

    def test_mixed_report_type_within_one_file_fails_loudly(self):
        reader = csv.reader(io.StringIO(self.fixture_text))
        rows = list(reader)
        rows[1][6] = "速報"  # 他の行は確報のまま
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerows(rows)
        broken_text = out.getvalue()

        with self.assertRaisesRegex(jsf_taishaku.TaishakuError, "速報／確報"):
            jsf_taishaku.parse_zandaka_text(broken_text, min_data_rows=SMALL_MIN_ROWS)

    # ---- idempotent write / 速報・確報 -----------------------------

    def test_confirmed_then_preliminary_is_skipped_not_downgraded(self):
        out = self.root / "data"
        source = self._write_source(self.fixture_text)

        updated_first = jsf_taishaku.run_update(
            out,
            source=source,
            generated_at=GENERATED_AT,
            min_data_rows=SMALL_MIN_ROWS,
        )
        self.assertTrue(updated_first)
        series_before = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in (out / "taishaku_series").glob("*.json")
        }
        self.assertTrue(series_before)  # 前提: 増分14aで作られているはず

        preliminary_text = _rewrite_report_type(self.fixture_text, "速報")
        preliminary_source = self._write_source(preliminary_text, "preliminary.csv")
        updated_second = jsf_taishaku.run_update(
            out,
            source=preliminary_source,
            generated_at="2026-07-23T01:00:00Z",
            min_data_rows=SMALL_MIN_ROWS,
        )
        self.assertFalse(updated_second)

        snapshot = json.loads((out / "taishaku" / "2026-07-22.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["report_type"], "確報")
        meta = json.loads((out / "taishaku_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generated_at"], GENERATED_AT)  # metaは書き換わっていない
        # reviewer指摘A(増分14a): ダウングレードでスキップした経路はseriesにも
        # 触れない(_update_seriesがsnapshot書き込みと同じreturn Falseガードの
        # 内側にあることの固定化、バイト単位で無変更)
        series_after = {
            path.relative_to(out).as_posix(): path.read_bytes()
            for path in (out / "taishaku_series").glob("*.json")
        }
        self.assertEqual(series_before, series_after)

    def test_preliminary_then_confirmed_overwrites(self):
        out = self.root / "data"
        preliminary_text = _rewrite_report_type(self.fixture_text, "速報")
        preliminary_source = self._write_source(preliminary_text, "preliminary.csv")

        updated_first = jsf_taishaku.run_update(
            out,
            source=preliminary_source,
            generated_at=GENERATED_AT,
            min_data_rows=SMALL_MIN_ROWS,
        )
        self.assertTrue(updated_first)

        confirmed_source = self._write_source(self.fixture_text, "confirmed.csv")
        updated_second = jsf_taishaku.run_update(
            out,
            source=confirmed_source,
            generated_at="2026-07-23T01:00:00Z",
            min_data_rows=SMALL_MIN_ROWS,
        )
        self.assertTrue(updated_second)

        snapshot = json.loads((out / "taishaku" / "2026-07-22.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["report_type"], "確報")
        meta = json.loads((out / "taishaku_meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["generated_at"], "2026-07-23T01:00:00Z")
        self.assertEqual(meta["latest_apply_date"], "2026-07-22")
        self.assertEqual(meta["snapshot_count"], 1)

    def test_writes_only_taishaku_contract_and_preserves_other_writers_outputs(self):
        out = self.root / "data"
        for relative, content in _OTHER_WRITERS_SENTINELS.items():
            path = out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        source = self._write_source(self.fixture_text)
        updated = jsf_taishaku.run_update(
            out, source=source, generated_at=GENERATED_AT, min_data_rows=SMALL_MIN_ROWS
        )
        self.assertTrue(updated)

        for relative, content in _OTHER_WRITERS_SENTINELS.items():
            self.assertEqual((out / relative).read_bytes(), content)
        self.assertTrue((out / "taishaku" / "2026-07-22.json").is_file())
        self.assertTrue((out / "taishaku_meta.json").is_file())
        # 増分14a: taishaku_seriesも同じ書き手が作る(他の書き手の領域は侵さない)
        self.assertTrue((out / "taishaku_series" / "13.json").is_file())

    def test_atomic_write_leaves_no_tmp_files_behind(self):
        out = self.root / "data"
        source = self._write_source(self.fixture_text)
        jsf_taishaku.run_update(
            out, source=source, generated_at=GENERATED_AT, min_data_rows=SMALL_MIN_ROWS
        )
        leftovers = (
            list((out / "taishaku").glob(".tmp-*"))
            + list(out.glob(".tmp-*"))
            + list((out / "taishaku_series").glob(".tmp-*"))
        )
        self.assertEqual(leftovers, [])

    # ---- CLI / UPDATED marker --------------------------------------

    def test_main_prints_updated_marker_on_write(self):
        # CLIは--min-data-rows等の抜け道を持たないため、既定の3,000行下限を
        # 実際に満たすCSV(非東証の水増し行を追加)でCLI経路を検証する
        out = self.root / "data"
        source = self._write_source(_padded_over_row_threshold(self.fixture_text))
        stdout = io.StringIO()
        with mock.patch("sys.stdout", stdout):
            exit_code = jsf_taishaku._main(["--out", str(out), "--source", str(source)])
        self.assertEqual(exit_code, 0)
        self.assertIn(jsf_taishaku.UPDATED_MARKER, stdout.getvalue().splitlines())
        snapshot = json.loads((out / "taishaku" / "2026-07-22.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["issue_count"], 29)  # 水増し行はTokyo外なので混入しない

    def test_main_omits_updated_marker_when_downgrade_is_skipped(self):
        out = self.root / "data"
        padded_confirmed = _padded_over_row_threshold(self.fixture_text)
        confirmed_source = self._write_source(padded_confirmed, "confirmed.csv")
        stdout_first = io.StringIO()
        with mock.patch("sys.stdout", stdout_first):
            exit_code_first = jsf_taishaku._main(
                ["--out", str(out), "--source", str(confirmed_source)]
            )
        self.assertEqual(exit_code_first, 0)
        self.assertIn(jsf_taishaku.UPDATED_MARKER, stdout_first.getvalue().splitlines())

        padded_preliminary = _rewrite_report_type(padded_confirmed, "速報")
        preliminary_source = self._write_source(padded_preliminary, "preliminary.csv")
        stdout_second = io.StringIO()
        with mock.patch("sys.stdout", stdout_second):
            exit_code_second = jsf_taishaku._main(
                ["--out", str(out), "--source", str(preliminary_source)]
            )
        self.assertEqual(exit_code_second, 0)
        self.assertNotIn(jsf_taishaku.UPDATED_MARKER, stdout_second.getvalue().splitlines())
        snapshot = json.loads((out / "taishaku" / "2026-07-22.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["report_type"], "確報")  # ダウングレードされていない

    def test_main_header_mismatch_exits_one_without_writing_files(self):
        reader = csv.reader(io.StringIO(self.fixture_text))
        rows = list(reader)
        rows[0][0] = "不正な列名"
        out_buf = io.StringIO()
        writer = csv.writer(out_buf, lineterminator="\r\n")
        writer.writerows(rows)
        broken_source = self._write_source(out_buf.getvalue(), "broken.csv")

        out = self.root / "data"
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            exit_code = jsf_taishaku._main(
                ["--out", str(out), "--source", str(broken_source)]
            )

        self.assertEqual(exit_code, 1)
        self.assertIn("ヘッダ", stderr.getvalue())
        self.assertFalse((out / "taishaku").exists())
        self.assertFalse((out / "taishaku_meta.json").exists())

    # ---- network fetch (opener injected, never hits the real network) --

    def test_fetch_retries_then_succeeds_with_browser_user_agent(self):
        calls = []
        attempts = {"count": 0}

        class _FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc_info):
                return False

            def read(self_inner):
                return "ok".encode("cp932")

        def opener(request, timeout):
            calls.append((request, timeout))
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise OSError("simulated network failure")
            return _FakeResponse()

        sleeps = []
        text = jsf_taishaku.read_source_text(
            None,
            opener=opener,
            sleep=lambda seconds: sleeps.append(seconds),
        )

        self.assertEqual(text, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0].get_header("User-agent"), jsf_taishaku.USER_AGENT)
        self.assertEqual(calls[0][1], jsf_taishaku.DEFAULT_TIMEOUT)
        self.assertEqual(sleeps, [jsf_taishaku.RETRY_BACKOFF_SECONDS] * 2)

    def test_fetch_raises_taishaku_error_after_exhausting_retries(self):
        def always_fails(request, timeout):
            raise OSError("simulated network failure")

        with self.assertRaises(jsf_taishaku.TaishakuError):
            jsf_taishaku.read_source_text(
                None, opener=always_fails, sleep=lambda _seconds: None
            )

    def test_network_guard_actually_intercepts_default_opener_resolution(self):
        # B-1回帰防止: opener/sourceを指定しない呼び出しは、setUpのnetwork_guardが
        # 差し替えたjsf_taishaku.urlopenへ実際に到達しなければならない。opener引数が
        # 定義時のデフォルト値でurlopenを実体束縛していると、このガードは素通りされ
        # 本番のネットワークへ抜けてしまう(reviewer B-1指摘、実機で本番GET通過を確認
        # 済み)。sleepだけ無害化し、TaishakuErrorの__cause__がguardのAssertionError
        # であることまで確認することで、実際にurlopen経由でガードへ到達したことを検証する
        with self.assertRaises(jsf_taishaku.TaishakuError) as ctx:
            jsf_taishaku._fetch_csv_bytes(sleep=lambda _seconds: None)
        self.assertIsInstance(ctx.exception.__cause__, AssertionError)
        self.assertIn("must not access the network", str(ctx.exception.__cause__))

        with self.assertRaises(jsf_taishaku.TaishakuError) as ctx2:
            jsf_taishaku.read_source_text(None, sleep=lambda _seconds: None)
        self.assertIsInstance(ctx2.exception.__cause__, AssertionError)

        with self.assertRaises(jsf_taishaku.TaishakuError) as ctx3:
            jsf_taishaku.run_update(
                self.root / "data",
                generated_at=GENERATED_AT,
                sleep=lambda _seconds: None,
            )
        self.assertIsInstance(ctx3.exception.__cause__, AssertionError)

    # ---- 増分14a: taishaku_series/{XX}.json --------------------------

    def test_series_first_build_and_incremental_append(self):
        # 初回構築(1日目)+増分追加(2日目)
        out = self.root / "data"
        day1 = self._write_source(self.fixture_text, "day1.csv")
        jsf_taishaku.run_update(
            out, source=day1, generated_at=GENERATED_AT, min_data_rows=SMALL_MIN_ROWS
        )
        shard = json.loads((out / "taishaku_series" / "13.json").read_text(encoding="utf-8"))
        self.assertEqual(shard["schema_version"], 1)
        self.assertEqual(shard["dates"], ["2026-07-22"])
        self.assertEqual(shard["issues"]["1301"]["yushi_zan"], [8200])
        self.assertEqual(set(shard["issues"]["1301"]), set(jsf_taishaku.SERIES_FIELDS))

        day2_text = _with_apply_date(self.fixture_text, "2026/07/23", "2026/07/27")
        day2 = self._write_source(day2_text, "day2.csv")
        jsf_taishaku.run_update(
            out, source=day2, generated_at="2026-07-23T09:00:00Z", min_data_rows=SMALL_MIN_ROWS
        )
        shard = json.loads((out / "taishaku_series" / "13.json").read_text(encoding="utf-8"))
        self.assertEqual(shard["dates"], ["2026-07-22", "2026-07-23"])
        self.assertEqual(shard["issues"]["1301"]["yushi_zan"], [8200, 8200])
        for field in jsf_taishaku.SERIES_FIELDS:
            self.assertEqual(len(shard["issues"]["1301"][field]), 2)

    def test_series_same_apply_date_reprocess_replaces_last_value(self):
        # 速報→確報の同日差し替え: 二重追加せず末尾を新しい値で上書きする
        out = self.root / "data"
        preliminary_text = _rewrite_report_type(self.fixture_text, "速報")
        preliminary_text = _mutate_rows(
            preliminary_text,
            lambda rows: [row.__setitem__(9, "1000") for row in rows if row[2] == "1301"],
        )
        preliminary_source = self._write_source(preliminary_text, "preliminary.csv")
        jsf_taishaku.run_update(
            out,
            source=preliminary_source,
            generated_at=GENERATED_AT,
            min_data_rows=SMALL_MIN_ROWS,
        )
        shard = json.loads((out / "taishaku_series" / "13.json").read_text(encoding="utf-8"))
        self.assertEqual(shard["dates"], ["2026-07-22"])
        self.assertEqual(shard["issues"]["1301"]["yushi_zan"], [1000])

        confirmed_source = self._write_source(self.fixture_text, "confirmed.csv")  # yushi_zan=8200
        jsf_taishaku.run_update(
            out,
            source=confirmed_source,
            generated_at="2026-07-22T18:00:00Z",
            min_data_rows=SMALL_MIN_ROWS,
        )
        shard = json.loads((out / "taishaku_series" / "13.json").read_text(encoding="utf-8"))
        # 二重追加されず、同じ1日分のまま末尾(唯一の要素)が確報値に差し替わる
        self.assertEqual(shard["dates"], ["2026-07-22"])
        self.assertEqual(shard["issues"]["1301"]["yushi_zan"], [8200])

    def test_series_handles_new_and_vanished_issues_with_null_fill(self):
        # 銘柄の出現・消滅: 1301が2日目に消え、9001が2日目に新規出現する
        out = self.root / "data"
        day1 = self._write_source(self.fixture_text, "day1.csv")
        jsf_taishaku.run_update(
            out, source=day1, generated_at=GENERATED_AT, min_data_rows=SMALL_MIN_ROWS
        )

        def mutate_day2(rows):
            rows[:] = [row for row in rows if row[2] != "1301"]
            new_row = list(rows[0])
            new_row[2] = "9001"
            new_row[3] = "テスト新規銘柄"
            new_row[4] = jsf_taishaku.TOKYO_EXCHANGE_LABEL
            rows.append(new_row)

        day2_text = _with_apply_date(self.fixture_text, "2026/07/23", "2026/07/27")
        day2_text = _mutate_rows(day2_text, mutate_day2)
        day2 = self._write_source(day2_text, "day2.csv")
        jsf_taishaku.run_update(
            out, source=day2, generated_at="2026-07-23T09:00:00Z", min_data_rows=SMALL_MIN_ROWS
        )

        shard_13 = json.loads((out / "taishaku_series" / "13.json").read_text(encoding="utf-8"))
        self.assertEqual(shard_13["dates"], ["2026-07-22", "2026-07-23"])
        # 1301は2日目に不在なのでnullで埋まる(消滅)
        self.assertEqual(shard_13["issues"]["1301"]["yushi_zan"], [8200, None])

        shard_90 = json.loads((out / "taishaku_series" / "90.json").read_text(encoding="utf-8"))
        self.assertEqual(shard_90["dates"], ["2026-07-22", "2026-07-23"])
        # 9001は1日目に不在なので過去分がnullで埋まる(新規出現)
        self.assertIsNone(shard_90["issues"]["9001"]["yushi_zan"][0])
        self.assertIsNotNone(shard_90["issues"]["9001"]["yushi_zan"][1])

    def test_merge_snapshot_into_series_trims_to_500_day_window(self):
        # 500日窓: 直接_merge_snapshot_into_seriesを叩き、500件で先頭が
        # 落ちることを確認する(500回run_updateする実運用相当のテストは
        # 過剰なので、窓トリムのロジック単体を検証する)
        # _merge_snapshot_into_series自体は日付フォーマットを検証しない
        # (それは_load_existing_series_shards側の責務)ので、順序さえ保たれる
        # 単純な連番文字列で500件用意する(実日付である必要はない)
        dates = [f"D{i:04d}" for i in range(500)]
        shards = {
            "13": {
                "schema_version": 1,
                "dates": dates,
                "issues": {
                    "1301": {field: [i for i in range(500)] for field in jsf_taishaku.SERIES_FIELDS}
                },
            }
        }
        snapshot = {
            "apply_date": "D0500",
            "issues": {
                "1301": {field: 999 for field in jsf_taishaku.SERIES_FIELDS},
            },

if __name__ == "__main__":
    unittest.main()
