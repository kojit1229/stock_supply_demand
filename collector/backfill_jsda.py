"""Rebuild JSDA weekly lending balances from half-year archives.

Only z (week-end balance) workbooks are in scope.  Completed half-years come
from JSDA zip archives; the current half-year is discovered from the index so
holiday-shifted report dates never have to be guessed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Callable, Iterable
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

if __package__:
    from . import build_site, jsda_weekly
else:  # ``python collector/backfill_jsda.py``
    import build_site  # type: ignore[no-redef]
    import jsda_weekly  # type: ignore[no-redef]


INDEX_URL = "https://www.jsda.or.jp/shiryoshitsu/toukei/kabu-taiw/index.html"
FILES_URL = urljoin(INDEX_URL, "files/")
MIN_REQUEST_INTERVAL = 5.0
MAX_ATTEMPTS = 3
MIN_ISSUE_COUNT = 1_000
DEFAULT_YEARS = 3

_Z_FILENAME = re.compile(
    r"^(?P<day>\d{8})z(?:\((?P<revision>\d{8})r\))?\.(?:xls|xlsx)$",
    re.IGNORECASE,
)


class BackfillError(RuntimeError):
    """Raised when source discovery or archive contents are invalid."""


@dataclass(frozen=True)
class ZName:
    filename: str
    report_date: date
    revision_date: str | None

    @property
    def priority(self) -> tuple[int, str]:
        return (1 if self.revision_date else 0, self.revision_date or "")


@dataclass(frozen=True)
class WeeklyCandidate:
    zname: ZName
    path: Path


@dataclass
class BackfillResult:
    processed_weeks: list[str]
    failures: list[str]

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)


class CachedDownloader:
    """Sequential, cached downloader with request spacing and bounded retries."""

    def __init__(
        self,
        *,
        opener: Callable[..., object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._opener = opener or urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._last_request_started: float | None = None

    def _wait_for_request_slot(self) -> None:
        if self._last_request_started is None:
            return
        remaining = MIN_REQUEST_INTERVAL - (
            self._monotonic() - self._last_request_started
        )
        if remaining > 0:
            self._sleep(remaining)

    def fetch(self, url: str, destination: str | os.PathLike[str]) -> Path:
        target = Path(destination)
        if target.is_file():
            return target
        if target.exists():
            raise BackfillError(f"キャッシュ先が通常ファイルではありません: {target}")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_request_slot()
            self._last_request_started = self._monotonic()
            try:
                request = Request(url, headers={"User-Agent": jsda_weekly.USER_AGENT})
                with self._opener(request, timeout=60) as response, temporary.open(
                    "wb"
                ) as output:
                    shutil.copyfileobj(response, output)
                os.replace(temporary, target)
                # Keep an explicit five-second gap after every successful file.
                # Retry failures use the exponential waits below.
                self._sleep(MIN_REQUEST_INTERVAL)
                return target
            except Exception as exc:
                last_error = exc
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                if attempt < MAX_ATTEMPTS - 1:
                    self._sleep(MIN_REQUEST_INTERVAL * (2**attempt))
        raise BackfillError(f"取得に{MAX_ATTEMPTS}回失敗しました: {url}") from last_error


def parse_z_name(filename: str) -> ZName | None:
    """Parse a plain/revised z filename; return None for j/s/unrelated files."""
    basename = Path(filename).name
    match = _Z_FILENAME.fullmatch(basename)
    if match is None:
        return None
    raw_day = match.group("day")
    try:
        report_date = date(
            int(raw_day[:4]), int(raw_day[4:6]), int(raw_day[6:8])
        )
    except ValueError as exc:
        raise BackfillError(f"zファイル名の日付が不正です: {basename}") from exc
    revision = match.group("revision")
    if revision is not None:
        try:
            date(int(revision[:4]), int(revision[4:6]), int(revision[6:8]))
        except ValueError as exc:
            raise BackfillError(f"訂正日が不正です: {basename}") from exc
    return ZName(basename, report_date, revision)


def select_preferred_z_names(filenames: Iterable[str]) -> dict[date, ZName]:
    """Select the newest revised file for each report week."""
    selected: dict[date, ZName] = {}
    for filename in sorted(filenames):
        candidate = parse_z_name(filename)
        if candidate is None:
            continue
        current = selected.get(candidate.report_date)
        if current is None or candidate.priority > current.priority:
            selected[candidate.report_date] = candidate
        elif candidate.priority == current.priority and candidate.filename != current.filename:
            raise BackfillError(
                f"同一優先度のzファイルが複数あります: "
                f"{current.filename}, {candidate.filename}"
            )
    return selected


def discover_z_urls(index_html: str, index_url: str = INDEX_URL) -> dict[date, str]:
    """Discover and revision-resolve z links from the JSDA index HTML."""
    parser = _HrefParser()
    parser.feed(index_html)
    index_host = urlparse(index_url).netloc
    urls_by_name: dict[str, str] = {}
    for href in parser.hrefs:
        absolute = urljoin(index_url, href)
        parsed = urlparse(absolute)
        if parsed.netloc != index_host:
            continue
        filename = Path(unquote(parsed.path)).name
        if parse_z_name(filename) is not None:
            urls_by_name[filename] = absolute
    preferred = select_preferred_z_names(urls_by_name)
    return {day: urls_by_name[zname.filename] for day, zname in preferred.items()}


def _half_start(day: date) -> date:
    return date(day.year, 1 if day.month <= 6 else 7, 1)


def _next_half(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 7 else date(day.year, 7, 1)


def archive_names(start: date, end: date, today: date) -> list[str]:
    """Return completed half-year archive names intersecting the requested range."""
    current_half = _half_start(today)
    cursor = _half_start(start)
    names: list[str] = []
    while cursor <= end:
        if cursor < current_half:
            last_month = 6 if cursor.month == 1 else 12
            names.append(f"{cursor.year}{cursor.month:02d}-{last_month:02d}.zip")
        cursor = _next_half(cursor)
    return names


def _extract_archive_candidates(
    archive_path: Path,
    extract_root: Path,
    start: date,
    end: date,
) -> list[WeeklyCandidate]:
    try:
        with ZipFile(archive_path) as archive:
            infos_by_name = {
                Path(info.filename).name: info
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).name
            }
            preferred = select_preferred_z_names(infos_by_name)
            candidates: list[WeeklyCandidate] = []
            destination_dir = extract_root / archive_path.stem
            destination_dir.mkdir(parents=True, exist_ok=True)
            for report_date, zname in sorted(preferred.items()):
                if not start <= report_date <= end:
                    continue
                destination = destination_dir / zname.filename
                with archive.open(infos_by_name[zname.filename]) as source, destination.open(
                    "wb"
                ) as output:
                    shutil.copyfileobj(source, output)
                candidates.append(WeeklyCandidate(zname, destination))
            return candidates
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise BackfillError(f"zipを展開できません: {archive_path}") from exc


def _merge_candidates(candidates: Iterable[WeeklyCandidate]) -> dict[date, WeeklyCandidate]:
    selected: dict[date, WeeklyCandidate] = {}
    for candidate in candidates:
        current = selected.get(candidate.zname.report_date)
        if current is None or candidate.zname.priority > current.zname.priority:
            selected[candidate.zname.report_date] = candidate
        elif (
            candidate.zname.priority == current.zname.priority
            and candidate.zname.filename != current.zname.filename
        ):
            raise BackfillError(
                f"同一週の候補が競合しています: {current.zname.filename}, "
                f"{candidate.zname.filename}"
            )
    return selected


def _write_weekly(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def run_backfill(
    start: date,
    end: date,
    out_dir: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str],
    *,
    today: date | None = None,
    downloader: CachedDownloader | None = None,
    min_issue_count: int | None = None,
) -> BackfillResult:
    """Download, parse, and write all discovered weeks; build shards on full success."""
    effective_today = today or date.today()
    if start > end:
        raise BackfillError(f"startがendより後です: {start} > {end}")
    if end > effective_today:
        raise BackfillError(f"endが未来です: {end} > {effective_today}")
    minimum = MIN_ISSUE_COUNT if min_issue_count is None else min_issue_count
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 1:
        raise BackfillError("min_issue_countは1以上の整数である必要があります")

    output_root = Path(out_dir)
