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
