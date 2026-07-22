"""Collect JPX reported short positions and build their dedicated shards.

The file contracts in design.md section 4 are fixed.  This module is the only
writer of ``short/`` and ``short_meta.json``; it must never write the weekly
collector's ``meta.json``, ``issues.json``, ``series/``, or ``weekly/`` outputs.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, time as datetime_time, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlparse

if __package__:
    from .backfill_jsda import CachedDownloader, _HrefParser
    from .jsda_weekly import _workbook_format
else:  # ``python collector/jpx_short.py``
    from backfill_jsda import CachedDownloader, _HrefParser  # type: ignore[no-redef]
    from jsda_weekly import _workbook_format  # type: ignore[no-redef]


INDEX_URL = "https://www.jpx.co.jp/markets/public/short-selling/index.html"
JPX_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
SHORT_SCHEMA_VERSION = "supply_demand_short_v1"
SHORT_META_SCHEMA_VERSION = "supply_demand_short_meta_v1"

_SHORT_FILENAME = re.compile(r"^(?P<day>\d{8})_Short_Positions\.xls$")
_CODE_PATTERN = re.compile(r"[0-9A-Z]{4,5}")
_NAME_SUFFIX = re.compile(
    r"(?:\s+(?:普通株式|優先株式|種類株式|投資証券|受益証券))$"
)
_HEADER_JA = (
    "",
    "計算年月日",
    "銘柄コード",
    "銘柄名\n（日本語／英語）",
    "",
    "商号・名称・氏名",
    "住所・所在地",
    "委託者・投資一任契約の相手方の商号・名称・氏名",
    "委託者・投資一任契約の相手方の住所・所在地",
    "信託財産・運用財産の名称",
    "空売り残高割合",
    "空売り残高数量",
    "空売り残高売買単位数",
    "直近計算年月日",
    "直近空売り残高割合",
    "備考",
)
_HEADER_EN = (
    "",
    "Date of Calculation",
    "Code of Stock",
    "Name of Stock\n(Japanese / English)",
    "",
    "Name of Short Seller",
    "Address of Short Seller",
    "Name of Discretionary Investment Contractor ",
    "Address of Discretionary Investment Contractor",
    "Name of Investment Fund",
    "Ratio of Short Positions to Shares Outstanding",
    "Number of Short Positions in Shares",
    "Number of Short Positions in Trading Units",
    "Date of Calculation in Previous Reporting",
    "Ratio of Short Positions in Previous Reporting\xa0",
    "Notes",
)


class JPXShortError(ValueError):
    """Raised when JPX discovery, source data, or existing shards are invalid."""


def _parse_iso_date(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise JPXShortError(f"{context} must be a YYYY-MM-DD string: {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise JPXShortError(f"invalid {context}: {value!r}") from exc
    if parsed.isoformat() != value:
        raise JPXShortError(f"invalid {context}: {value!r}")
    return value


def _validate_generated_at(value: str) -> None:
    if not isinstance(value, str) or not value:
        raise JPXShortError("generated_at must be a non-empty ISO 8601 string")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise JPXShortError(f"invalid generated_at: {value!r}") from exc


def _publication_date_from_name(filename: str) -> date:
    match = _SHORT_FILENAME.fullmatch(filename)
    if match is None:
        raise JPXShortError(f"invalid JPX short filename: {filename}")
    raw = match.group("day")
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError as exc:
        raise JPXShortError(f"invalid date in JPX short filename: {filename}") from exc


def discover_short_urls(
    index_html: str, index_url: str = INDEX_URL
) -> dict[date, str]:
    """Discover tokenized JPX .xls links without constructing their paths."""
    parser = _HrefParser()
    parser.feed(index_html)
    expected_host = urlparse(index_url).netloc.lower()
    discovered: dict[date, str] = {}
    for href in parser.hrefs:
        absolute = urljoin(index_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != expected_host:
            continue
        filename = Path(unquote(parsed.path)).name
        if _SHORT_FILENAME.fullmatch(filename) is None:
            continue
        publication_date = _publication_date_from_name(filename)
        previous = discovered.get(publication_date)
        if previous is not None and previous != absolute:
            raise JPXShortError(
                f"multiple JPX links for {publication_date.isoformat()}: "
                f"{previous}, {absolute}"
            )
        discovered[publication_date] = absolute
    return discovered


def _normalize_code(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise JPXShortError(f"invalid stock code: {value!r}")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise JPXShortError(f"invalid stock code: {value!r}")
        value = int(value)
    code = str(value).strip()
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]
    if _CODE_PATTERN.fullmatch(code) is None:
        raise JPXShortError(f"invalid stock code: {value!r}")
    return code


def _normalize_text(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise JPXShortError(f"{context} must be text: {value!r}")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        raise JPXShortError(f"{context} is empty")
    return normalized


