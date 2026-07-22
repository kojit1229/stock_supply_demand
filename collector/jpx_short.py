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


def _normalize_name(value: Any) -> str:
    name = _normalize_text(value, "stock name")
    name = _NAME_SUFFIX.sub("", name).strip()
    if not name:
        raise JPXShortError("stock name is empty after suffix normalization")
    return name


def _excel_date(value: Any, datemode: int, context: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JPXShortError(f"{context} is not an Excel serial date: {value!r}")
    if not math.isfinite(float(value)):
        raise JPXShortError(f"{context} is not a finite Excel serial date: {value!r}")
    try:
        import xlrd

        converted = xlrd.xldate_as_datetime(value, datemode)
    except Exception as exc:
        raise JPXShortError(f"cannot convert {context}: {value!r}") from exc
    if converted.time() != datetime_time.min:
        raise JPXShortError(f"{context} contains a time component: {value!r}")
    return converted.date().isoformat()


def _ratio(value: Any, row_number: int) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JPXShortError(f"row {row_number}: ratio is not numeric: {value!r}")
    ratio = float(value)
    if not math.isfinite(ratio) or not 0 <= ratio <= 1:
        raise JPXShortError(f"row {row_number}: invalid ratio: {value!r}")
    return ratio


def _quantity(value: Any, row_number: int) -> int:
    if isinstance(value, bool):
        raise JPXShortError(f"row {row_number}: quantity is not an integer: {value!r}")
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        quantity = int(value)
    else:
        raise JPXShortError(f"row {row_number}: quantity is not an integer: {value!r}")
    if quantity < 0:
        raise JPXShortError(f"row {row_number}: quantity is negative: {quantity}")
    return quantity


def _rows_equal(actual: Iterable[Any], expected: tuple[Any, ...]) -> bool:
    return tuple(actual) == expected


def parse_short_workbook(
    path: str | os.PathLike[str],
) -> dict[str, dict[str, Any]]:
    """Parse one audited JPX BIFF8 snapshot into short issue objects."""
    workbook_path = Path(path)
    publication_date = _publication_date_from_name(workbook_path.name)
    try:
        if _workbook_format(workbook_path) != "xlrd":
            raise JPXShortError(f"JPX short workbook is not BIFF .xls: {workbook_path}")
    except JPXShortError:
        raise
    except Exception as exc:
        raise JPXShortError(f"cannot identify JPX workbook: {workbook_path}") from exc

    try:
        import xlrd

        workbook = xlrd.open_workbook(str(workbook_path), on_demand=True)
    except Exception as exc:
        raise JPXShortError(f"cannot open JPX workbook: {workbook_path}") from exc
    try:
        if len(workbook.sheet_names()) != 1:
            raise JPXShortError(
                f"JPX workbook must contain exactly one sheet: {workbook.sheet_names()}"
            )
        worksheet = workbook.sheet_by_index(0)
        expected_sheet = publication_date.strftime("%Y%m%d")
        if worksheet.name != expected_sheet:
            raise JPXShortError(
                f"sheet name does not match publication date: "
                f"{worksheet.name!r} != {expected_sheet!r}"
            )
        if worksheet.ncols != 16 or worksheet.nrows < 9:
            raise JPXShortError(
                f"unexpected JPX workbook dimensions: "
                f"{worksheet.nrows} rows x {worksheet.ncols} columns"
            )
        if not _rows_equal(worksheet.row_values(6), _HEADER_JA):
            raise JPXShortError("Japanese header row does not match audited format")
        if not _rows_equal(worksheet.row_values(7), _HEADER_EN):
            raise JPXShortError("English header row does not match audited format")

        disclosure = _excel_date(
            worksheet.cell_value(4, 2), workbook.datemode, "disclosure date"
        )
        if disclosure != publication_date.isoformat():
            raise JPXShortError(
                f"disclosure date does not match filename: "
                f"{disclosure} != {publication_date.isoformat()}"
            )

        issues: dict[str, dict[str, Any]] = {}
        keys: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row_index in range(8, worksheet.nrows):
            row = worksheet.row_values(row_index)
            if not any(value not in ("", None) for value in row):
                continue
            row_number = row_index + 1
            code = _normalize_code(row[2])
            name = _normalize_name(row[3])
            seller = _normalize_text(row[5], "short seller")
            event = {
                "date": _excel_date(
                    row[1], workbook.datemode, f"row {row_number} calculation date"
                ),
                "ratio": _ratio(row[10], row_number),
                "qty": _quantity(row[11], row_number),
                "seller": seller,
            }
            issue = issues.setdefault(code, {"name": name, "events": []})
            if issue["name"] != name:
                raise JPXShortError(
                    f"row {row_number}: conflicting names for {code}: "
                    f"{issue['name']!r} != {name!r}"
                )
            key = (code, seller, event["date"])
            previous = keys.get(key)
            if previous is None:
                keys[key] = event
                issue["events"].append(event)
            else:
                # A seller can report multiple investment funds for one stock on
                # one calculation date (observed for 402A on 2026-07-21).  The
                # deployed event contract has no fund field, so aggregate those
                # rows before the cross-snapshot key is applied.
                previous["ratio"] += event["ratio"]
                previous["qty"] += event["qty"]
        if not issues:
            raise JPXShortError("JPX workbook contains no data rows")
        for issue in issues.values():
            issue["events"].sort(key=lambda event: (event["date"], event["seller"]))
        return issues
    finally:
        workbook.release_resources()


def _validate_event(event: Any, code: str) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != {"date", "ratio", "qty", "seller"}:
        raise JPXShortError(f"invalid existing event for {code}: {event!r}")
    event_date = _parse_iso_date(event.get("date"), f"event date for {code}")
    seller = _normalize_text(event.get("seller"), f"event seller for {code}")
    ratio = _ratio(event.get("ratio"), 0)
    qty = _quantity(event.get("qty"), 0)
    return {"date": event_date, "ratio": ratio, "qty": qty, "seller": seller}


def _load_existing_shards(short_dir: Path) -> dict[str, dict[str, Any]]:
    if not short_dir.exists():
        return {}
    if not short_dir.is_dir():
        raise JPXShortError(f"short output is not a directory: {short_dir}")
    issues: dict[str, dict[str, Any]] = {}
    for path in sorted(short_dir.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise JPXShortError(f"cannot read existing short shard: {path}") from exc
        if not isinstance(document, dict) or document.get("schema_version") != SHORT_SCHEMA_VERSION:
            raise JPXShortError(f"schema_version mismatch in existing shard: {path}")
        shard_issues = document.get("issues")
        if not isinstance(shard_issues, dict):
            raise JPXShortError(f"invalid issues object in existing shard: {path}")
        for code, issue in shard_issues.items():
            normalized_code = _normalize_code(code)
            if normalized_code != code or code[:2] != path.stem:
                raise JPXShortError(f"code {code!r} is in the wrong shard: {path}")
            if code in issues or not isinstance(issue, dict):
                raise JPXShortError(f"duplicate or invalid existing issue: {code}")
            name = _normalize_text(issue.get("name"), f"existing name for {code}")
            events = issue.get("events")
            if not isinstance(events, list):
                raise JPXShortError(f"invalid existing events for {code}")
            seen: set[tuple[str, str]] = set()
            validated: list[dict[str, Any]] = []
            for event in events:
                clean = _validate_event(event, code)
                key = (clean["date"], clean["seller"])
                if key in seen:
                    raise JPXShortError(f"duplicate existing event for {code}: {key!r}")
                seen.add(key)
                validated.append(clean)
            validated.sort(key=lambda item: (item["date"], item["seller"]))
            issues[code] = {"name": name, "events": validated}
    return issues


def _load_latest_short_date(meta_path: Path) -> date | None:
    if not meta_path.exists():
        return None
    try:
        document = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JPXShortError(f"cannot read existing short meta: {meta_path}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SHORT_META_SCHEMA_VERSION:
        raise JPXShortError(f"schema_version mismatch in existing short meta: {meta_path}")
    value = _parse_iso_date(document.get("latest_short_date"), "latest_short_date")
    return date.fromisoformat(value)


def _merge_issues(
    destination: dict[str, dict[str, Any]],
    source: dict[str, dict[str, Any]],
) -> None:
    for code, incoming in source.items():
        current = destination.setdefault(code, {"name": incoming["name"], "events": []})
        current["name"] = incoming["name"]
        by_key = {
            (event["date"], event["seller"]): event for event in current["events"]
        }
        for event in incoming["events"]:
            by_key[(event["date"], event["seller"])] = event
        current["events"] = sorted(
            by_key.values(), key=lambda event: (event["date"], event["seller"])
        )


def _assemble_outputs(
    issues: dict[str, dict[str, Any]], latest_short_date: date, generated_at: str
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    shards: dict[str, dict[str, Any]] = {}
    for code in sorted(issues):
        shards.setdefault(code[:2], {})[code] = issues[code]
    documents = {
        shard: {
            "schema_version": SHORT_SCHEMA_VERSION,
            "issues": shard_issues,
        }
        for shard, shard_issues in sorted(shards.items())
    }
    meta = {
        "schema_version": SHORT_META_SCHEMA_VERSION,
        "latest_short_date": latest_short_date.isoformat(),
        "generated_at": generated_at,
    }
    return documents, meta


def _write_outputs(
    out_dir: Path,
    shards: dict[str, dict[str, Any]],
    meta: dict[str, Any],
) -> None:
    """Atomically replace only short/ and short_meta.json, with rollback."""
    if out_dir.exists() and not out_dir.is_dir():
        raise JPXShortError(f"output path is not a directory: {out_dir}")
    rendered_shards = {
        f"{shard}.json": json.dumps(
            document, ensure_ascii=False, separators=(",", ":")
        )
        for shard, document in shards.items()
    }
    rendered_meta = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".jpx-short-", dir=out_dir.parent))
    created_out_dir = not out_dir.exists()
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        staged_short = stage / "new" / "short"
        staged_short.mkdir(parents=True)
        for filename, text in rendered_shards.items():
            (staged_short / filename).write_text(text, encoding="utf-8")
        staged_meta = stage / "new" / "short_meta.json"
        staged_meta.write_text(rendered_meta, encoding="utf-8")

        out_dir.mkdir(parents=True, exist_ok=True)
        for name in ("short", "short_meta.json"):
            target = out_dir / name
            if target.exists() or target.is_symlink():
                backup = stage / "old" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, backup)
                backups.append((backup, target))
        for name in ("short", "short_meta.json"):
            staged = stage / "new" / name
            target = out_dir / name
            os.replace(staged, target)
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
            raise JPXShortError(
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


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def run_update(
    out_dir: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str],
    *,
    index_html_path: str | os.PathLike[str] | None = None,
    downloader: CachedDownloader | None = None,
    generated_at: str | None = None,
) -> tuple[str, ...]:
    """Discover and merge snapshots newer than short_meta.latest_short_date."""
    output_root = Path(out_dir)
    cache_root = Path(cache_dir)
    latest = _load_latest_short_date(output_root / "short_meta.json")
    fetcher = downloader or CachedDownloader(user_agent=JPX_USER_AGENT)

    if index_html_path is None:
        cache_root.mkdir(parents=True, exist_ok=True)
        today = datetime.now(timezone.utc).date().isoformat()
        index_path = fetcher.fetch(
            INDEX_URL, cache_root / f"jpx-short-index-{today}.html"
        )
    else:
        index_path = Path(index_html_path)
    try:
        index_html = index_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise JPXShortError(f"cannot read JPX index HTML: {index_path}") from exc

    discovered = discover_short_urls(index_html)
    candidates = [
        (publication_date, url)
        for publication_date, url in sorted(discovered.items())
        if latest is None or publication_date > latest
    ]
    if not candidates:
        return ()

    existing = _load_existing_shards(output_root / "short")
    parsed_snapshots: list[tuple[date, dict[str, dict[str, Any]]]] = []
    cache_root.mkdir(parents=True, exist_ok=True)
    for publication_date, url in candidates:
        filename = Path(unquote(urlparse(url).path)).name
        source = fetcher.fetch(url, cache_root / filename)
        parsed_snapshots.append((publication_date, parse_short_workbook(source)))

    for _, snapshot in parsed_snapshots:
        _merge_issues(existing, snapshot)
    latest_processed = parsed_snapshots[-1][0]
    timestamp = generated_at or _generated_at()
    _validate_generated_at(timestamp)
    shards, meta = _assemble_outputs(
        existing, latest_processed, timestamp
    )
    _write_outputs(output_root, shards, meta)
    return tuple(day.isoformat() for day, _ in parsed_snapshots)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JPX空売り残高報告からshort shardを増分更新します"
    )
    parser.add_argument("--out", default="data", help="出力dataディレクトリ")
    parser.add_argument(
        "--cache-dir", default="data/_cache", help="ダウンロードキャッシュ"
    )
    parser.add_argument(
        "--index-html", help="オフライン用JPX index HTML (指定時はindexを取得しない)"
    )
    args = parser.parse_args(argv)
    try:
        updated = run_update(
            args.out,
            args.cache_dir,
            index_html_path=args.index_html,
        )
    except Exception as exc:
        print(f"jpx_short: {exc}", file=sys.stderr)
        return 1
    if updated:
        print(
            f"jpx_short: {len(updated)}公表日を更新しました "
            f"({', '.join(updated)})"
        )
    else:
        print("jpx_short: 対象なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
