"""Incrementally update deployed JSDA weekly data.

The weekly contract is defined in ``design.md`` section 5: discover report
dates from the JSDA index, require both z and s workbooks, validate every new
week, and rebuild the site data only after the complete batch validates.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlsplit, urlunsplit

if __package__:
    from . import backfill_jsda, build_site, jsda_weekly
else:  # ``python collector/weekly_update.py``
    import backfill_jsda  # type: ignore[no-redef]
    import build_site  # type: ignore[no-redef]
    import jsda_weekly  # type: ignore[no-redef]


UPDATED_MARKER = "UPDATED=1"
_S_FILENAME = re.compile(r"^(?P<day>\d{8})s", re.IGNORECASE)


class WeeklyUpdateError(RuntimeError):
    """Raised when incremental discovery or publication cannot complete."""


@dataclass(frozen=True)
class WeeklyUpdateResult:
    updated_weeks: tuple[str, ...]
    skipped_weeks: tuple[str, ...]

    @property
    def updated(self) -> bool:
        return bool(self.updated_weeks)


def _read_latest_week(meta_path: Path) -> date | None:
    if not meta_path.exists():
        return None
    if not meta_path.is_file():
        raise WeeklyUpdateError(f"meta.jsonが通常ファイルではありません: {meta_path}")
    try:
        document = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WeeklyUpdateError(f"meta.jsonを読めません: {meta_path}") from exc
    if not isinstance(document, dict):
        raise WeeklyUpdateError(f"meta.jsonがJSONオブジェクトではありません: {meta_path}")
    value = document.get("latest_week")
    if not isinstance(value, str):
        raise WeeklyUpdateError(f"meta.jsonのlatest_weekが文字列ではありません: {value!r}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WeeklyUpdateError(
            f"meta.jsonのlatest_weekがYYYY-MM-DD形式ではありません: {value!r}"
        ) from exc
    if parsed.isoformat() != value:
        raise WeeklyUpdateError(
            f"meta.jsonのlatest_weekがYYYY-MM-DD形式ではありません: {value!r}"
        )
    return parsed


def _s_source(z_url: str) -> tuple[str, str]:
    """Return the s URL and filename obtained by replacing z in a z basename."""
    parsed = urlsplit(z_url)
    z_filename = Path(unquote(parsed.path)).name
    z_name = backfill_jsda.parse_z_name(z_filename)
    if z_name is None:
        raise WeeklyUpdateError(f"発見URLがzファイルではありません: {z_url}")
    s_filename = z_name.filename[:8] + "s" + z_name.filename[9:]
    if _S_FILENAME.match(s_filename) is None:
        raise WeeklyUpdateError(f"sファイル名を構成できません: {z_name.filename}")
    parent = parsed.path.rsplit("/", 1)[0] if "/" in parsed.path else ""
    s_path = f"{parent}/{quote(s_filename, safe='().-_')}"
    return urlunsplit(parsed._replace(path=s_path)), s_filename


def _is_not_published(error: BaseException) -> bool:
    """Recognize only an HTTP not-found response as an unpublished s file."""
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPError) and current.code in {404, 410}:
            return True
        current = current.__cause__ or current.__context__
    return False


def _generated_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _write_document(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _commit_weekly_and_outputs(
    output_root: Path,
    staged_weekly: Path,
    weeks: list[str],
    outputs: dict[str, dict[str, object]],
    rollback_root: Path,
) -> None:
    """Commit staged weeks, rolling them back if the atomic site write fails."""
    weekly_dir = output_root / "weekly"
    weekly_dir.mkdir(parents=True, exist_ok=True)
    rollback_root.mkdir(parents=True, exist_ok=True)
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for week in weeks:
            destination = weekly_dir / f"{week}.json"
            if destination.exists() or destination.is_symlink():
                backup = rollback_root / f"{week}.json"
                os.replace(destination, backup)
                backups.append((backup, destination))
            os.replace(staged_weekly / f"{week}.json", destination)
            committed.append(destination)
        build_site._write_outputs(output_root, outputs)
    except Exception:
        for destination in reversed(committed):
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for backup, destination in reversed(backups):
            os.replace(backup, destination)
        raise


