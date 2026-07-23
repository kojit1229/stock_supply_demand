import csv
import io
import json
import tempfile
import unittest
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

        preliminary_text = _rewrite_report_type(self.fixture_text, "速報")
        preliminary_source = self._write_source(preliminary_text, "preliminary.csv")
        updated_second = jsf_taishaku.run_update(
            out,
            source=preliminary_source,
            generated_at="2026-07-23T01:00:00Z",
