"""Collect JSF (Japan Securities Finance / 日証金) daily taishaku snapshots.

The output file contract (design.md section 4, approved by the supervisor; do
not change) is::

    taishaku/YYYY-MM-DD.json = {
        schema_version, apply_date, settle_date, report_type ("速報"|"確報"),
        issue_count, issues: {"1301": {name, yushi_shin, yushi_hen, yushi_zan,
                                        kashikabu_shin, kashikabu_hen,
                                        kashikabu_zan, sashihiki_zan,
                                        seido_kai, seido_uri}},
    }
    taishaku_meta.json = {schema_version, latest_apply_date, generated_at,
                           snapshot_count}

This module is the only writer of ``taishaku/`` and ``taishaku_meta.json``; it
must never touch ``meta.json``, ``issues.json``, ``series/``, ``weekly/``,
``short/``, ``short_meta.json``, ``prices/``, or ``prices_meta.json`` (each of
those has its own dedicated writer elsewhere in ``collector/``).

Source: https://www.taisyaku.jp/data/zandaka.csv -- a fixed URL that always
serves the latest snapshot only (cp932, no historical backfill available), so
this collector must run daily and accumulate one file per ``apply_date``.
Audited format (audit-shinyou.md section G, 2026-07-23): 36 columns, cp932,
CRLF, quoted text fields.  Only rows whose ``取引所区分名`` is exactly
"東証およびＰＴＳ" are kept (~96% of listed issues; the remaining rows are
issues listed solely on regional exchanges, which are out of scope for v1).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.request import Request, urlopen


SOURCE_URL = "https://www.taisyaku.jp/data/zandaka.csv"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 30
MAX_ATTEMPTS = 3  # 初回 + リトライ2回(仕様どおり)
RETRY_BACKOFF_SECONDS = 5.0

SCHEMA_VERSION = 1
META_SCHEMA_VERSION = 1
TOKYO_EXCHANGE_LABEL = "東証およびＰＴＳ"
# 監査時点の実測(4,751行)より十分小さい下限。フォーマット破損・truncateの検知用
MIN_DATA_ROWS = 3_000
# daily.yml が `grep -qx 'TAISHAKU_UPDATED=1'` の行完全一致でdeploy可否を判定する
UPDATED_MARKER = "TAISHAKU_UPDATED=1"

EXPECTED_HEADER = (
    "申込日",
    "決済日",
    "銘柄コード",
    "銘柄名",
    "取引所区分名",
    "上場区分",
    "速報／確報",
    "融資新規株数",
    "融資返済株数",
    "融資残高株数",
    "貸株新規株数",
    "貸株返済株数",
    "貸株残高株数",
    "差引残高株数",
    "融資新規金額",
    "融資返済金額",
    "融資残高金額",
    "貸株新規金額",
    "貸株返済金額",
    "貸株残高金額",
    "差引残高金額",
    "制度信用・買残高株数",
    "制度信用・売残高株数",
    "融資権利落額",
    "貸株権利落額",
    "合計・更新差金融資値上り",
    "合計・更新差金融資値下り",
    "合計・更新差金貸株値下り",
    "合計・更新差金貸株値上り",
    "総合回転日数",
    "融資・新規回転日数",
    "融資・返済回転日数",
    "融資・残高回転日数",
    "貸株・新規回転日数",
    "貸株・返済回転日数",
    "貸株・残高回転日数",
)
_REQUIRED_QUANTITY_COLUMNS = (
    (7, "yushi_shin", "融資新規株数"),
    (8, "yushi_hen", "融資返済株数"),
    (9, "yushi_zan", "融資残高株数"),
    (10, "kashikabu_shin", "貸株新規株数"),
    (11, "kashikabu_hen", "貸株返済株数"),
    (12, "kashikabu_zan", "貸株残高株数"),
    (13, "sashihiki_zan", "差引残高株数"),
)
_OPTIONAL_QUANTITY_COLUMNS = (
    (21, "seido_kai", "制度信用・買残高株数"),
    (22, "seido_uri", "制度信用・売残高株数"),
)

_CODE_PATTERN = re.compile(r"[0-9A-Z]{4,5}")
_DATE_PATTERN = re.compile(r"(?P<y>\d{4})/(?P<m>\d{2})/(?P<d>\d{2})")
_INT_PATTERN = re.compile(r"-?\d+")
_ISO_DATE_STEM = re.compile(r"\d{4}-\d{2}-\d{2}")


class TaishakuError(ValueError):
    """Raised when the zandaka.csv source, or an existing snapshot, is invalid."""


def _normalize_code(value: str) -> str:
    # 日証金のコードは最初から最終形(監督者が実CSVで検証済み: 4桁4,344+5桁優先株
    # 7銘柄、5桁末尾'0'は0件)。JSDAの「285A0→285A」4桁化規則はここには存在しない
    # ため切り詰めない。5桁で末尾'0'が来たら未知の規則としてフェイルラウドする
    code = value.strip()
    if _CODE_PATTERN.fullmatch(code) is None:
        raise TaishakuError(f"不正な銘柄コードです: {value!r}")
    if len(code) == 5 and code.endswith("0"):
        raise TaishakuError(
            f"5桁末尾'0'のコードは監査対象外の未知フォーマットです: {value!r}"
        )
    return code


def _normalize_name(value: str) -> str:
    name = re.sub(r"\s+", " ", value.strip())
    if not name:
        raise TaishakuError("銘柄名が空です")
    return name


def _parse_date(value: str, context: str) -> str:
    match = _DATE_PATTERN.fullmatch(value)
    if match is None:
        raise TaishakuError(f"{context}の日付形式が不正です: {value!r}")
    try:
        parsed = date(int(match["y"]), int(match["m"]), int(match["d"]))
    except ValueError as exc:
        raise TaishakuError(f"{context}の日付が不正です: {value!r}") from exc
    return parsed.isoformat()


def _parse_required_int(value: str, context: str) -> int:
    if _INT_PATTERN.fullmatch(value) is None:
        raise TaishakuError(f"{context}が整数として解釈できません: {value!r}")
    return int(value)


def _parse_optional_int(value: str, context: str) -> int | None:
    if value == "":
        return None
    return _parse_required_int(value, context)


def parse_zandaka_text(
    text: str, *, min_data_rows: int = MIN_DATA_ROWS
) -> dict[str, Any]:
    """Parse the decoded zandaka.csv contents into the taishaku/ snapshot dict.

    ``min_data_rows`` defaults to the production sanity threshold (3,000); unit
    tests that exercise a deliberately small fixture pass a lower value so the
    row-count guard does not fire while still validating parsing behaviour.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise TaishakuError("CSVが空です") from exc
    if tuple(header) != EXPECTED_HEADER:
        raise TaishakuError(
            "ヘッダ列が監査済みフォーマットと一致しません"
            "(日証金側のフォーマット変更の可能性、要再監査)"
        )

    data_rows = [row for row in reader if row]
    if len(data_rows) <= min_data_rows:
        raise TaishakuError(
            f"データ行数が少なすぎます: {len(data_rows)}行"
            f"(下限 {min_data_rows}行超が必要)"
        )

    apply_dates: set[str] = set()
    settle_dates: set[str] = set()
    report_types: set[str] = set()
    issues: dict[str, dict[str, Any]] = {}
    for offset, row in enumerate(data_rows):
        row_number = offset + 2  # +1 for header, +1 for 1-index
        if len(row) != len(EXPECTED_HEADER):
            raise TaishakuError(f"{row_number}行目の列数が不正です: {len(row)}列")
        apply_dates.add(row[0])
        settle_dates.add(row[1])
        report_types.add(row[6])
        if row[4] != TOKYO_EXCHANGE_LABEL:
            continue
        code = _normalize_code(row[2])
        if code in issues:
            raise TaishakuError(f"{row_number}行目: コードが重複しています: {code}")
        issue: dict[str, Any] = {"name": _normalize_name(row[3])}
        for column, key, label in _REQUIRED_QUANTITY_COLUMNS:
            issue[key] = _parse_required_int(row[column], f"{row_number}行目 {label}")
        for column, key, label in _OPTIONAL_QUANTITY_COLUMNS:
            issue[key] = _parse_optional_int(row[column], f"{row_number}行目 {label}")
        issues[code] = issue

    if not issues:
        raise TaishakuError(f"{TOKYO_EXCHANGE_LABEL!r}行が見つかりません")
    if len(apply_dates) != 1:
        raise TaishakuError(f"申込日が複数種類あります: {sorted(apply_dates)!r}")
    if len(settle_dates) != 1:
        raise TaishakuError(f"決済日が複数種類あります: {sorted(settle_dates)!r}")
    if len(report_types) != 1:
        raise TaishakuError(f"速報／確報が複数種類あります: {sorted(report_types)!r}")
    report_type = report_types.pop()
    if report_type not in ("速報", "確報"):
        raise TaishakuError(f"不正な速報／確報区分です: {report_type!r}")

    return {
        "schema_version": SCHEMA_VERSION,
        "apply_date": _parse_date(apply_dates.pop(), "申込日"),
        "settle_date": _parse_date(settle_dates.pop(), "決済日"),
        "report_type": report_type,
        "issue_count": len(issues),
        "issues": issues,
    }


def _fetch_csv_bytes(
    url: str = SOURCE_URL,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = USER_AGENT,
) -> bytes:
    # opener はNoneを既定にし、呼び出し時にurlopenへ遅延解決する(collector/prices.py
    # のrun_collectと同じ流儀)。関数定義時にデフォルト引数として束縛すると、テストが
    # モジュールの`urlopen`をmock.patch.objectしても効かなくなるため
    fetch_opener = opener or urlopen
    request = Request(url, headers={"User-Agent": user_agent})
    last_error: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            with fetch_opener(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 -- fail loud once retries exhaust
            last_error = exc
            if attempt < MAX_ATTEMPTS - 1:
                sleep(RETRY_BACKOFF_SECONDS)
    raise TaishakuError(f"日証金CSVの取得に{MAX_ATTEMPTS}回失敗しました: {url}") from last_error


def read_source_text(
    source: str | os.PathLike[str] | None,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = DEFAULT_TIMEOUT,
    user_agent: str = USER_AGENT,
) -> str:
    """Read zandaka.csv bytes (local ``source`` for tests/manual, else network)."""
    if source is not None:
        raw = Path(source).read_bytes()
    else:
        raw = _fetch_csv_bytes(
            opener=opener, sleep=sleep, timeout=timeout, user_agent=user_agent
        )
    try:
        return raw.decode("cp932")
    except UnicodeDecodeError as exc:
        raise TaishakuError("CSVをcp932でデコードできません") from exc


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_generated_at(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise TaishakuError("generated_atは空でないISO 8601文字列である必要があります")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TaishakuError(f"不正なgenerated_atです: {value!r}") from exc


def _load_existing_snapshot(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaishakuError(f"既存スナップショットが読めません: {path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise TaishakuError(f"schema_versionが不一致です: {path}")
    if document.get("report_type") not in ("速報", "確報"):
        raise TaishakuError(f"既存report_typeが不正です: {path}")
    if document.get("apply_date") != path.stem:
        raise TaishakuError(f"apply_dateがファイル名と不一致です: {path}")
    return document


def _write_snapshot_and_meta(
    out_root: Path, snapshot: dict[str, Any], generated_at: str
) -> None:
    """Atomically write one taishaku/{apply_date}.json, then rebuild the meta
    file from a directory listing (self-healing; no cross-run state needed)."""
    taishaku_dir = out_root / "taishaku"
    taishaku_dir.mkdir(parents=True, exist_ok=True)
    target = taishaku_dir / f"{snapshot['apply_date']}.json"
    rendered = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    fd, tmp_name = tempfile.mkstemp(
        dir=taishaku_dir, prefix=".tmp-taishaku-", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(rendered)
        os.replace(tmp_name, target)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise

    dates = sorted(
        path.stem for path in taishaku_dir.glob("*.json") if _ISO_DATE_STEM.fullmatch(path.stem)
    )
    if not dates:
        raise TaishakuError(f"{taishaku_dir} にスナップショットがありません")
    meta = {
        "schema_version": META_SCHEMA_VERSION,
        "latest_apply_date": dates[-1],
        "generated_at": generated_at,
        "snapshot_count": len(dates),
    }
    meta_text = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    meta_fd, meta_tmp = tempfile.mkstemp(
        dir=out_root, prefix=".tmp-taishaku-meta-", suffix=".json"
    )
    try:
        with os.fdopen(meta_fd, "w", encoding="utf-8") as handle:
            handle.write(meta_text)
        os.replace(meta_tmp, out_root / "taishaku_meta.json")
    except Exception:
        Path(meta_tmp).unlink(missing_ok=True)
        raise


def run_update(
    out_dir: str | os.PathLike[str],
    *,
    source: str | os.PathLike[str] | None = None,
    generated_at: str | None = None,
    min_data_rows: int = MIN_DATA_ROWS,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Fetch/parse the latest snapshot; write it unless that would be a
    confirmed->preliminary downgrade. Returns True iff a write happened."""
    out_root = Path(out_dir)
    text = read_source_text(source, opener=opener, sleep=sleep)
    snapshot = parse_zandaka_text(text, min_data_rows=min_data_rows)

    target = out_root / "taishaku" / f"{snapshot['apply_date']}.json"
    existing = _load_existing_snapshot(target)
    if (
        existing is not None
        and existing.get("report_type") == "確報"
        and snapshot["report_type"] == "速報"
    ):
        return False  # 確報保存済みなら速報でダウングレードしない

    timestamp = generated_at or _generated_at()
    _validate_generated_at(timestamp)
    _write_snapshot_and_meta(out_root, snapshot, timestamp)
    return True


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="日証金 銘柄別残高一覧(zandaka.csv)からtaishakuスナップショットを増分更新します"
    )
    parser.add_argument("--out", default="data", help="出力dataディレクトリ")
