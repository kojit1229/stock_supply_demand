"""Parse JSDA weekly stock lending workbooks.

File contract (approved by the supervisor; do not change): the weekly output is
``{schema_version, report_date, source_files, issues}``, and each issue stores
the six raw quantity/amount values defined in design.md section 4.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from openpyxl import load_workbook


SCHEMA_VERSION = "supply_demand_weekly_v1"
USER_AGENT = "stock_supply_demand/1.0 (+https://github.com/kojit1229/stock_supply_demand)"

_ZANDAKA_SHEET = "残高（株券等（上場））"
_SHINKI_SHEET = "新規（株券等（上場））"
_HEADER_ROW_7 = (
    None,
    None,
    None,
    "数量",
    "前週比",
    "金額",
    "前週比",
    "数量",
    "前週比",
    "金額",
    "前週比",
    "数量",
    "前週比",
    "金額",
    "前週比",
)
_HEADER_ROW_6 = {
    "taishaku": (
        "銘柄名",
        "コード",
        "担保",
        "貸付残高",
        None,
        None,
        None,
        "借入残高（自己）",
        None,
        None,
        None,
        "借入残高（転貸）",
        None,
        None,
        None,
    ),
    "shinki": (
        "銘柄名",
        "コード",
        "担保",
        "新規貸付成約高",
        None,
        None,
        None,
        "新規借入成約高（自己）",
        None,
        None,
        None,
        "新規借入成約高（転貸）",
        None,
        None,
        None,
    ),
}
_SHEETS = {"taishaku": _ZANDAKA_SHEET, "shinki": _SHINKI_SHEET}
_COLLATERAL_KEYS = {"有担保": "yutanpo", "無担保": "mutanpo"}
_OUTPUT_COLUMNS = {
    "lend_qty": 3,
    "lend_amt": 5,
    "own_qty": 7,
    "own_amt": 9,
    "ten_qty": 11,
    "ten_amt": 13,
}
_QUANTITY_COLUMNS = (3, 7, 11)
_NUMERIC_COLUMNS = tuple(range(3, 15))
_CHANGE_COLUMNS = (4, 6, 8, 10, 12, 14)


class JSDAParseError(ValueError):
    """Raised when a workbook violates the audited JSDA format."""


def _normalize_name(value: Any) -> str:
    if value is None:
        raise JSDAParseError("銘柄名が空です")
    name = re.sub(r"\s+", " ", str(value).strip())
    if not name:
        raise JSDAParseError("銘柄名が空です")
    return name


def _normalize_code(value: Any) -> str:
    if value is None or not str(value).strip():
        raise JSDAParseError("コードが空です")
    # 旧.xls(xlrd)は数値セルをfloatで返す(13010.0)ため、整数値は先にintへ落とす
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    code = str(value).strip()
    # 統一コード5桁の末尾'0'は普通株の付加桁なので落とす。末尾が'0'以外
    # (優先株・社債型種類株式など、例 25935)は独立銘柄なので5桁のまま保持する
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]
    if not re.fullmatch(r"[0-9A-Z]{4,5}", code):
        raise JSDAParseError(f"不正な銘柄コードです: {value!r}")
    return code


def _to_int_or_none(value: Any, *, allow_none: bool, context: str) -> int | None:
    """Convert an Excel number to int; a previous-week '-' becomes None."""
    if value == "-":
        if allow_none:
            return None
        raise JSDAParseError(f"{context}に'-'は使用できません")
    if value is None:
        # 監査済みの欠損表記は前週比列の'-'のみ。空セルは欠損ではなく破損として扱う
        raise JSDAParseError(f"{context}が空です")
    if isinstance(value, bool):
        raise JSDAParseError(f"{context}が数値ではありません: {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise JSDAParseError(f"{context}が整数ではありません: {value!r}")


def _row_tuple(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """Return all 15 audited columns, preserving extra columns for detection."""
    if len(row) >= 15:
        return tuple(row)
    return tuple(row) + (None,) * (15 - len(row))


def _validate_header(row_number: int, row: tuple[Any, ...], kind: str) -> None:
    expected = _HEADER_ROW_6[kind] if row_number == 6 else _HEADER_ROW_7
    actual = _row_tuple(row)
    if actual != expected:
        raise JSDAParseError(
            f"{row_number}行目のヘッダが期待値と一致しません: "
            f"expected={expected!r}, actual={actual!r}"
        )


def _parse_workbook(path: str | os.PathLike[str], kind: str) -> dict[str, dict[str, Any]]:
    workbook_path = Path(path)
    try:
        workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    except Exception as exc:
        raise JSDAParseError(f"xlsxを開けません: {workbook_path}") from exc

    sheet_name = _SHEETS[kind]
    try:
        if sheet_name not in workbook.sheetnames:
            raise JSDAParseError(f"対象シートがありません: {sheet_name}")
        worksheet = workbook[sheet_name]
        issues: dict[str, dict[str, Any]] = {}
        source_names: dict[str, str] = {}
        bucket_sources: dict[tuple[str, str], set[str]] = {}
        quantity_sums = [0, 0, 0]
        total_quantities: tuple[int, int, int] | None = None
        data_count = 0
        saw_total = False

        for row_number, raw_row in enumerate(worksheet.iter_rows(values_only=True), 1):
            row = _row_tuple(raw_row)
            if row_number in (6, 7):
                _validate_header(row_number, row, kind)
                continue
            if row_number < 8:
                continue
            if not any(value is not None for value in row):
                continue

            if _normalize_name(row[0]) == "合計":
                if saw_total:
                    raise JSDAParseError("合計行が複数あります")
                if row[1] is not None and str(row[1]).strip():
                    raise JSDAParseError("合計行のコードが空ではありません")
                total_quantities = tuple(
                    _to_int_or_none(
                        row[column],
                        allow_none=False,
