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
    measurements: Any, *, path: Path, code: str, collateral: str
) -> None:
    if not isinstance(measurements, dict):
        raise BuildSiteError(
            f"taishaku.{collateral} must be an object for {code} in {path}"
        )
    for field in _TAISHAKU_FIELDS:
        value = measurements.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise BuildSiteError(
                f"taishaku.{collateral}.{field} must be an integer "
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
        if not isinstance(issue.get("shinki"), dict):
            raise BuildSiteError(f"issue {code} has invalid shinki data in {path}")
        for collateral in _COLLATERAL_TYPES:
            if collateral in taishaku:
                _validate_measurements(
                    taishaku[collateral],
                    path=path,
                    code=code,
                    collateral=collateral,
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
        # ビルダーが使うのはtaishaku/nameのみ。shinki(週次約4千銘柄×6値)を
        # 160週分保持するとピークメモリが倍増するため、検証後に落とす
        for issue in document["issues"].values():
            issue.pop("shinki", None)
        seen_weeks[report_date] = path
        documents.append(document)

    documents.sort(key=lambda item: item["report_date"])
    return documents


def _combined_value(taishaku: dict[str, Any], field: str) -> int | None:
