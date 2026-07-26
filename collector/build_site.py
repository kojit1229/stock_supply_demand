"""Build the deployed JSDA issue index and time-series shards.

File contracts are fixed by ``design.md`` section 4.  Weekly inputs are
``supply_demand_weekly_v1`` documents; this module emits only the matching
issues, series, and meta contracts and fails before touching the output when
input validation fails.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_CODE_PATTERN = re.compile(r"[0-9A-Z]{4,5}")


WEEKLY_SCHEMA_VERSION = "supply_demand_weekly_v1"
ISSUES_SCHEMA_VERSION = "supply_demand_issues_v1"
SERIES_SCHEMA_VERSION = "supply_demand_series_v1"
META_SCHEMA_VERSION = "supply_demand_meta_v1"
SIGNALS_SCHEMA_VERSION = 1
MAX_WEEKS = 160

_SERIES_FIELDS = ("lend_qty", "own_qty", "ten_qty", "lend_amt")
# 増分11(2026-07-24確定、design.md §4): weekly/*.jsonのshinki(新規成約高)生値から
# 算出する後方互換の追加系列。既存の_SERIES_FIELDS(z=残高)とは別枠で扱う
_S_SERIES_FIELDS = ("s_borrow_qty", "s_lend_qty")
_TAISHAKU_FIELDS = (
    "lend_qty",
    "lend_amt",
    "own_qty",
    "own_amt",
    "ten_qty",
    "ten_amt",
)
_COLLATERAL_TYPES = ("yutanpo", "mutanpo")

# 増分13(2026-07-24確定、design.md §4 signals.json): index.htmlのcomputeSignal系
# 関数群と同一閾値(PWAのシグナルカードと判定を一致させるため、境界値も含め
# 完全に同じ規則をPython側で再実装する)
MIN_VALID_WEEKS = 8  # 借株残の有効週(非null週)数がこれ未満なら'insufficient'
BORROW_CHANGE_THRESHOLD = 0.10  # 借株残4週変化率 ±10%
SHORT_RATIO_THRESHOLD_PT = 0.5  # 空売り合計ratio 4週前比 ±0.5pt


class BuildSiteError(ValueError):
    """Raised when weekly data violates the deployed file contracts."""


def _validate_generated_at(generated_at: str) -> None:
    if not isinstance(generated_at, str) or not generated_at:
        raise BuildSiteError("generated_at must be a non-empty ISO 8601 string")
    candidate = generated_at[:-1] + "+00:00" if generated_at.endswith("Z") else generated_at
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BuildSiteError(
            f"generated_at must be an ISO 8601 timestamp: {generated_at!r}"
        ) from exc


def _validate_report_date(value: Any, path: Path) -> str:
    if not isinstance(value, str):
        raise BuildSiteError(f"report_date must be a string: {path}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BuildSiteError(f"invalid report_date in {path}: {value!r}") from exc
    if parsed.isoformat() != value:
        raise BuildSiteError(f"invalid report_date in {path}: {value!r}")
    return value


def _validate_measurements(
    measurements: Any, *, path: Path, code: str, collateral: str, kind: str = "taishaku"
) -> None:
    if not isinstance(measurements, dict):
        raise BuildSiteError(
            f"{kind}.{collateral} must be an object for {code} in {path}"
        )
    for field in _TAISHAKU_FIELDS:
        value = measurements.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise BuildSiteError(
                f"{kind}.{collateral}.{field} must be an integer "
                f"for {code} in {path}"
            )


def _validate_issues(value: Any, path: Path) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise BuildSiteError(f"empty issue set in {path}")

    for code, issue in value.items():
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            raise BuildSiteError(f"invalid issue code in {path}: {code!r}")
        if not isinstance(issue, dict):
            raise BuildSiteError(f"issue {code} must be an object in {path}")
        if not isinstance(issue.get("name"), str) or not issue["name"].strip():
            raise BuildSiteError(f"issue {code} has an invalid name in {path}")
        taishaku = issue.get("taishaku")
        if not isinstance(taishaku, dict):
            raise BuildSiteError(f"issue {code} has invalid taishaku data in {path}")
        shinki = issue.get("shinki")
        if not isinstance(shinki, dict):
            raise BuildSiteError(f"issue {code} has invalid shinki data in {path}")
        for collateral in _COLLATERAL_TYPES:
            if collateral in taishaku:
                _validate_measurements(
                    taishaku[collateral],
                    path=path,
                    code=code,
                    collateral=collateral,
                )
            # shinki(新規成約高)はtaishakuと同一の6フィールド構造(design.md §4)。
            # 増分11でs_borrow_qty/s_lend_qtyの算出に使うため、taishaku同様に検証する
            # (これまでは未使用のため未検証だった)
            if collateral in shinki:
                _validate_measurements(
                    shinki[collateral],
                    path=path,
                    code=code,
                    collateral=collateral,
                    kind="shinki",
                )
    return value


def _load_weekly_documents(weekly_dir: Path) -> list[dict[str, Any]]:
    if not weekly_dir.is_dir():
        raise BuildSiteError(f"weekly directory does not exist: {weekly_dir}")

    paths = sorted(path for path in weekly_dir.glob("*.json") if path.is_file())
    if not paths:
        raise BuildSiteError(f"empty issue set: no weekly JSON files in {weekly_dir}")
    # ファイル名=report_date(検証済み契約)なので名前順の末尾160件だけ読めば窓が確定する。
    # weekly/はgh-pages上で恒久累積するため、全件ロードすると長期運用で線形に重くなる
    paths = paths[-MAX_WEEKS:]

    documents: list[dict[str, Any]] = []
    seen_weeks: dict[str, Path] = {}
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as source:
                document = json.load(source)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildSiteError(f"cannot read weekly JSON: {path}") from exc

        if not isinstance(document, dict):
            raise BuildSiteError(f"weekly document must be an object: {path}")
        if document.get("schema_version") != WEEKLY_SCHEMA_VERSION:
            raise BuildSiteError(
                f"schema_version mismatch in {path}: "
                f"{document.get('schema_version')!r}"
            )
        source_files = document.get("source_files")
        if not isinstance(source_files, list) or not all(
            isinstance(item, str) and item for item in source_files
        ):
            raise BuildSiteError(f"invalid source_files in {path}")

        report_date = _validate_report_date(document.get("report_date"), path)
        if report_date in seen_weeks:
            raise BuildSiteError(
                f"duplicate week {report_date}: {seen_weeks[report_date]} and {path}"
            )
        if path.stem != report_date:
            raise BuildSiteError(
                f"report_date does not match filename: {report_date!r} != {path.name!r}"
            )

        _validate_issues(document.get("issues"), path)
        # ビルダーが使うのはtaishaku/name/shinki由来の合算2値のみ。生shinki(週次
        # 約4千銘柄×6値×2担保区分)を160週分保持するとピークメモリが倍増するため、
        # 検証直後にs_borrow_qty/s_lend_qty(増分11)へ圧縮してから生データは落とす
        for issue in document["issues"].values():
            shinki = issue.pop("shinki")
            issue["s_lend_qty"] = _combined_value(shinki, "lend_qty")
            issue["s_borrow_qty"] = _null_safe_sum(
                _combined_value(shinki, "own_qty"), _combined_value(shinki, "ten_qty")
            )
        seen_weeks[report_date] = path
        documents.append(document)

    documents.sort(key=lambda item: item["report_date"])
    return documents


def _combined_value(taishaku: dict[str, Any], field: str) -> int | None:
    values = [
        taishaku[collateral][field]
        for collateral in _COLLATERAL_TYPES
        if collateral in taishaku
    ]
    return sum(values) if values else None


def _null_safe_sum(*values: int | None) -> int | None:
    """Sum the non-null operands; null iff every operand is null.

    増分11のs_borrow_qty(借入(自己)+借入(転貸))合算規則(design.md §4「片方null
    片方数値なら数値を採用、両方nullならnull」)。_combined_valueと組み合わせて
    使う場合、担保区分の内側フィールドは常に揃って存在/不在になる(jsda_weekly.py
    が前週比列以外をNone許容しないため)ので、この汎用実装は_combined_value自身の
    「一部担保区分のみ存在」規則とも整合する。"""
    present = [value for value in values if value is not None]
    return sum(present) if present else None


# ---------------------------------------------------------------------------
# 増分13: signals.json(design.md §4)。判定ロジックはindex.htmlの
# computeBorrowIndicator/computeShortIndicator/computeSignalBadgeと同一閾値
# (境界値含む)で移植する。**順位・スコアは持たせない**契約なので、算出結果は
# badge/borrow_chg/short/priceの4項目のみ返す。
# ---------------------------------------------------------------------------


def _load_price_codes(price_list_path: Path) -> set[str]:
    """Load config/price_list.json's codes. Missing file -> empty set (no
    price list means every issue's ``price`` flag is False; not an error, so
    a local checkout without the file can still build signals.json). A
    present-but-malformed file still fails loud."""
    if not price_list_path.exists():
        return set()
    try:
        raw = price_list_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildSiteError(f"price_listを読めません: {price_list_path}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BuildSiteError(f"price_listがJSONとして不正です: {price_list_path}") from exc
    if not isinstance(document, dict):
        raise BuildSiteError(f"price_listがJSONオブジェクトではありません: {price_list_path}")
    codes = document.get("codes")
    if not isinstance(codes, list):
        raise BuildSiteError(f"price_listのcodesが不正です: {price_list_path}")
    validated: set[str] = set()
    for code in codes:
        if not isinstance(code, str) or _CODE_PATTERN.fullmatch(code) is None:
            raise BuildSiteError(f"price_listの銘柄コードが不正です: {code!r}")
        validated.add(code)
    return validated


def _load_short_events(short_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load every short/{XX}.json shard into a flat code->events map.

    Missing directory -> empty map (no short data means every issue's short
    indicator is 'none'/neutral; not an error, so a local checkout without a
    daily short/ pull can still build signals.json). A present-but-malformed
    shard file still fails loud; individual malformed *events* are instead
    filtered defensively inside the indicator functions below, mirroring
    index.html's own tolerant `events.filter(...)`.
    """
    if not short_dir.is_dir():
        return {}
    events_by_code: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(short_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildSiteError(f"shortシャードを読めません: {path}") from exc
        if not isinstance(document, dict):
            raise BuildSiteError(f"shortシャードがオブジェクトではありません: {path}")
        issues = document.get("issues")
        if not isinstance(issues, dict):
            raise BuildSiteError(f"shortシャードのissuesが不正です: {path}")
        for code, issue in issues.items():
            if not isinstance(issue, dict) or not isinstance(issue.get("events"), list):
                raise BuildSiteError(f"shortシャードの銘柄データが不正です: {path} {code!r}")
            events_by_code[code] = issue["events"]
    return events_by_code


def _borrow_series(series: dict[str, list[int | None]]) -> list[float | None]:
    """index.htmlのborrowBalanceSeries: own_qty+ten_qtyを週ごとに合算し、
    どちらかがnullならnull(0扱いにしない、欠測伝播)。"""
    own = series["own_qty"]
    ten = series["ten_qty"]
    return [
        (own[index] + ten[index]) if own[index] is not None and ten[index] is not None else None
        for index in range(len(own))
    ]


def _compute_borrow_indicator(borrow: list[float | None]) -> dict[str, Any]:
    """index.htmlのcomputeBorrowIndicatorと同一(境界値含む)。"""
    valid_count = sum(1 for value in borrow if value is not None)
    if valid_count < MIN_VALID_WEEKS:
        return {"status": "insufficient", "direction": "neutral", "change_ratio": None}
    latest_index = len(borrow) - 1
    compare_index = latest_index - 4
    latest = borrow[latest_index]
    compare = borrow[compare_index] if compare_index >= 0 else None
    if latest is None or compare is None or compare == 0:
        return {"status": "unavailable", "direction": "neutral", "change_ratio": None}
    change_ratio = (latest - compare) / compare
    if change_ratio >= BORROW_CHANGE_THRESHOLD:
        direction = "increase"
    elif change_ratio <= -BORROW_CHANGE_THRESHOLD:
        direction = "decrease"
    else:
        direction = "neutral"
    return {"status": "ok", "direction": direction, "change_ratio": change_ratio}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


_YMD_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_ymd(value: Any) -> bool:
    """True iff ``value`` is a genuinely valid YYYY-MM-DD date string.

    形だけの正規表現一致(例: "2026-07-99")は``date.fromisoformat``で弾く。
    """
    if not isinstance(value, str) or _YMD_PATTERN.match(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _sum_latest_ratio_as_of(
    events: list[dict[str, Any]], as_of_date: str
) -> float | None:
    """index.htmlのsumLatestRatioAsOf: 報告者ごとの「基準日以前の最新イベント」
    のratioを合算する(below_thresholdのratio 0.0もそのまま加算)。報告が
    1件も無ければNone。"""
    by_seller: dict[str, dict[str, Any]] = {}
    for event in events:
        if event["date"] > as_of_date:
            continue
        current = by_seller.get(event["seller"])
        if current is None or event["date"] > current["date"]:
            by_seller[event["seller"]] = event
    total = 0.0
    any_found = False
    for event in by_seller.values():
        ratio = event.get("ratio")
        if _is_number(ratio):
            total += ratio
            any_found = True
    return total if any_found else None


def _compute_short_indicator(events: list[dict[str, Any]]) -> dict[str, Any]:
    """index.htmlのcomputeShortIndicator(fetchFailed=falseの経路のみ。builder
    側はfetchではなくファイル読み込みなので'error'状態は無い)と同一。

    reviewer指摘B(2026-07-25): dateが``^\\d{4}-\\d{2}-\\d{2}$``の形をしていても
    暦として不正(例: "2026-07-99")だと``date.fromisoformat``が未捕捉
    ValueErrorになり得るため、有効イベントの寛容フィルタ(既存の「1イベント
    単位の不正は無視」方針)の時点でこのチェックも行い、以降の計算では
    valid_eventsのdateが常にfromisoformatで解釈可能であることを保証する。
    """
    valid_events = [
        event
        for event in events
        if isinstance(event, dict)
        and _is_valid_ymd(event.get("date"))
        and isinstance(event.get("seller"), str)
    ]
    if not valid_events:
        return {"status": "none", "direction": "neutral", "sum_latest": None}
    latest_date = max(event["date"] for event in valid_events)
    sum_latest = _sum_latest_ratio_as_of(valid_events, latest_date)
    if sum_latest is None:
        return {"status": "none", "direction": "neutral", "sum_latest": None}
    prior_date = (date.fromisoformat(latest_date) - timedelta(days=28)).isoformat()
    sum_prior = _sum_latest_ratio_as_of(valid_events, prior_date)
    if sum_prior is None:
        return {"status": "no-history", "direction": "neutral", "sum_latest": sum_latest}
    delta_pt = (sum_latest - sum_prior) * 100
    if delta_pt >= SHORT_RATIO_THRESHOLD_PT:
        direction = "increase"
    elif delta_pt <= -SHORT_RATIO_THRESHOLD_PT:
        direction = "decrease"
    else:
        direction = "neutral"
    return {"status": "ok", "direction": direction, "sum_latest": sum_latest}


def _compute_badge(borrow_indicator: dict[str, Any], short_indicator: dict[str, Any]) -> str:
    """index.htmlのcomputeSignalBadgeと同一の4値規則。"""
    if borrow_indicator["status"] == "insufficient":
        return "insufficient"
    d1 = borrow_indicator["direction"]
    d3 = short_indicator["direction"]
    if d1 == "increase" and d3 == "increase":
        return "pressure-up"
    if (d1, d3) in (("decrease", "decrease"), ("decrease", "neutral"), ("neutral", "decrease")):
        return "covering"
    return "neutral"


def _compute_signal(
    series: dict[str, list[int | None]],
    events: list[dict[str, Any]],
    price_codes: set[str],
    code: str,
) -> dict[str, Any]:
    borrow = _borrow_series(series)
    borrow_indicator = _compute_borrow_indicator(borrow)
    short_indicator = _compute_short_indicator(events)
    sum_latest = short_indicator["sum_latest"]
    return {
        "badge": _compute_badge(borrow_indicator, short_indicator),
        "borrow_chg": borrow_indicator["change_ratio"],
        "short": bool(sum_latest is not None and sum_latest > 0),
        "price": code in price_codes,
    }


def _assemble_outputs(
    documents: list[dict[str, Any]],
    generated_at: str,
    short_events: dict[str, list[dict[str, Any]]],
    price_codes: set[str],
) -> dict[str, dict[str, Any]]:
    retained = documents[-MAX_WEEKS:]
    weeks = [document["report_date"] for document in retained]

    latest_names: dict[str, str] = {}
    for document in retained:
        for code, issue in document["issues"].items():
            latest_names[code] = issue["name"]
    if not latest_names:
        raise BuildSiteError("empty issue set")

    outputs: dict[str, dict[str, Any]] = {}
    outputs["issues.json"] = {
        "schema_version": ISSUES_SCHEMA_VERSION,
        "issues": {
            code: {"name": latest_names[code], "shard": code[:2]}
            for code in sorted(latest_names)
        },
    }

    shard_issues: dict[str, dict[str, Any]] = {}
    signals_issues: dict[str, dict[str, Any]] = {}
    for code in sorted(latest_names):
        series = {field: [] for field in _SERIES_FIELDS}
        for field in _S_SERIES_FIELDS:
            series[field] = []
        for document in retained:
            issue = document["issues"].get(code)
            if issue is None:
                for field in _SERIES_FIELDS:
                    series[field].append(None)
                for field in _S_SERIES_FIELDS:
                    series[field].append(None)
                continue
            taishaku = issue["taishaku"]
            for field in _SERIES_FIELDS:
                series[field].append(_combined_value(taishaku, field))
            for field in _S_SERIES_FIELDS:
                series[field].append(issue[field])

        shard = code[:2]
        shard_issues.setdefault(shard, {})[code] = {
            "name": latest_names[code],
            **series,
        }
        # 増分13: series構築と同じループ内でown_qty/ten_qtyを再利用してsignalsも
        # 算出する(ファイル再読み込み無し)
        signals_issues[code] = _compute_signal(
            series, short_events.get(code, []), price_codes, code
        )

    for shard in sorted(shard_issues):
        outputs[f"series/{shard}.json"] = {
            "schema_version": SERIES_SCHEMA_VERSION,
            "weeks": weeks,
            "issues": shard_issues[shard],
        }

    outputs["signals.json"] = {
        "schema_version": SIGNALS_SCHEMA_VERSION,
        "week": weeks[-1],
        "issues": signals_issues,
    }

    outputs["meta.json"] = {
        "schema_version": META_SCHEMA_VERSION,
        "latest_week": weeks[-1],
        "generated_at": generated_at,
        "issue_count": len(latest_names),
        "weekly_count": len(weeks),
    }
    return outputs


def _write_outputs(out_dir: Path, outputs: dict[str, dict[str, Any]]) -> None:
    """Replace only builder-owned outputs, preserving sibling data directories.

    外部利用者あり: collector/weekly_update.py が原子的コミットのため直接呼ぶ。
    シグネチャ・対象(issues.json/meta.json/series/signals.json)を変える際は
    同ファイルも更新すること。
    """
    if out_dir.exists() and not out_dir.is_dir():
        raise BuildSiteError(f"output path is not a directory: {out_dir}")

    rendered = {
        relative: json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        for relative, document in outputs.items()
    }

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".build-site-", dir=out_dir.parent))
    created_out_dir = not out_dir.exists()
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for relative, text in rendered.items():
            staged_path = stage / "new" / relative
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            staged_path.write_text(text, encoding="utf-8")

        out_dir.mkdir(parents=True, exist_ok=True)
        targets = ("issues.json", "meta.json", "series", "signals.json")
        for name in targets:
            target = out_dir / name
            if target.exists() or target.is_symlink():
                backup = stage / "old" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
                backups.append((backup, target))

        for name in targets:
            staged_path = stage / "new" / name
            target = out_dir / name
            os.replace(staged_path, target)
            committed.append(target)
    except Exception:
        try:
            for target in reversed(committed):
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
            for backup, target in reversed(backups):
                if backup.exists() or backup.is_symlink():
                    os.replace(backup, target)
        except Exception as rollback_exc:
            # 復元自体に失敗したら、旧データ(stage/old)を消さずに残して
            # 復旧手段を保つ(この場合のみstageを削除しない)
            raise BuildSiteError(
                f"rollback failed; previous outputs preserved under {stage}"
            ) from rollback_exc
        if created_out_dir:
            try:
                out_dir.rmdir()
            except OSError:
                pass
        shutil.rmtree(stage, ignore_errors=True)
        raise
    else:
        shutil.rmtree(stage, ignore_errors=True)


def build_site(
    weekly_dir: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    generated_at: str,
    *,
    short_dir: str | os.PathLike[str] | None = None,
    price_list_path: str | os.PathLike[str] = "config/price_list.json",
) -> dict[str, dict[str, Any]]:
    """Validate weekly snapshots and write all deployed builder outputs.

    ``short_dir``(既定: ``out_dir/short``)と``price_list_path``(既定:
    ``config/price_list.json``、CWD相対でprices.pyのCLI既定と同じ流儀)は
    増分13のsignals.json算出用。どちらも欠落は許容(短絡してshort全false・
    price全false扱いにする、ローカルにdaily.ymlの生成物が無い環境でも週次
    ビルドが通るように)。存在するのに壊れている場合はフェイルラウドする。
    """
    _validate_generated_at(generated_at)
    documents = _load_weekly_documents(Path(weekly_dir))
    out_root = Path(out_dir)
    resolved_short_dir = Path(short_dir) if short_dir is not None else out_root / "short"
    short_events = _load_short_events(resolved_short_dir)
    price_codes = _load_price_codes(Path(price_list_path))
    outputs = _assemble_outputs(documents, generated_at, short_events, price_codes)
    _write_outputs(out_root, outputs)
    return outputs


def _default_generated_at() -> str:
    timestamp = datetime.fromtimestamp(time.time(), timezone.utc)
    return timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build JSDA issue metadata and 160-week series shards"
    )
    parser.add_argument("--weekly-dir", required=True, help="directory of weekly JSON files")
    parser.add_argument("--out-dir", required=True, help="output data directory")
    parser.add_argument("--generated-at", help="ISO 8601 generation timestamp")
    parser.add_argument(
        "--short-dir",
        help="JPX short-position shard directory for signals.json "
        "(default: <out-dir>/short; missing is OK, malformed is fatal)",
    )
    parser.add_argument(
        "--price-list",
        default="config/price_list.json",
        help="price_list.json for signals.json (missing is OK, malformed is fatal)",
    )
    args = parser.parse_args(argv)

    generated_at = args.generated_at if args.generated_at is not None else _default_generated_at()
    try:
        build_site(
            args.weekly_dir,
            args.out_dir,
            generated_at,
            short_dir=args.short_dir,
            price_list_path=args.price_list,
        )
    except (BuildSiteError, OSError) as exc:
        print(f"build_site: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
