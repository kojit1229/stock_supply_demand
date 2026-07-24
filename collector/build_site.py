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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_CODE_PATTERN = re.compile(r"[0-9A-Z]{4,5}")


WEEKLY_SCHEMA_VERSION = "supply_demand_weekly_v1"
ISSUES_SCHEMA_VERSION = "supply_demand_issues_v1"
SERIES_SCHEMA_VERSION = "supply_demand_series_v1"
META_SCHEMA_VERSION = "supply_demand_meta_v1"
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


def _assemble_outputs(
    documents: list[dict[str, Any]], generated_at: str
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

    for shard in sorted(shard_issues):
        outputs[f"series/{shard}.json"] = {
            "schema_version": SERIES_SCHEMA_VERSION,
            "weeks": weeks,
            "issues": shard_issues[shard],
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
    シグネチャ・対象(issues.json/meta.json/series)を変える際は同ファイルも更新すること。
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
        targets = ("issues.json", "meta.json", "series")
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
) -> dict[str, dict[str, Any]]:
    """Validate weekly snapshots and write all deployed builder outputs."""
    _validate_generated_at(generated_at)
    documents = _load_weekly_documents(Path(weekly_dir))
    outputs = _assemble_outputs(documents, generated_at)
    _write_outputs(Path(out_dir), outputs)
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
    args = parser.parse_args(argv)

    generated_at = args.generated_at if args.generated_at is not None else _default_generated_at()
    try:
        build_site(args.weekly_dir, args.out_dir, generated_at)
    except (BuildSiteError, OSError) as exc:
        print(f"build_site: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
