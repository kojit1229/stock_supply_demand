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


def run_weekly_update(
    out_dir: str | os.PathLike[str],
    cache_dir: str | os.PathLike[str],
    *,
    meta_path: str | os.PathLike[str] | None = None,
    downloader: backfill_jsda.CachedDownloader | None = None,
    generated_at: str | None = None,
) -> WeeklyUpdateResult:
    """Discover, validate, and atomically add all eligible complete weeks."""
    output_root = Path(out_dir)
    cache_root = Path(cache_dir)
    meta = Path(meta_path) if meta_path is not None else output_root / "meta.json"
    latest_week = _read_latest_week(meta)
    fetcher = downloader or backfill_jsda.CachedDownloader()

    index_path = fetcher.fetch(backfill_jsda.INDEX_URL, cache_root / "index.html")
    try:
        index_html = index_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        raise WeeklyUpdateError(f"JSDA indexキャッシュを読めません: {index_path}") from exc
    discovered = backfill_jsda.discover_z_urls(index_html)

    if latest_week is None:
        target_days = sorted(discovered)[-1:]
    else:
        target_days = sorted(day for day in discovered if day > latest_week)
    if not target_days:
        return WeeklyUpdateResult((), ())

    documents: list[tuple[str, dict[str, object]]] = []
    skipped: list[str] = []
    for report_day in target_days:
        week = report_day.isoformat()
        z_url = discovered[report_day]
        z_filename = backfill_jsda.parse_z_name(
            Path(unquote(urlsplit(z_url).path)).name
        )
        if z_filename is None:
            raise WeeklyUpdateError(f"発見URLがzファイルではありません: {z_url}")
        s_url, s_filename = _s_source(z_url)
        z_path = fetcher.fetch(z_url, cache_root / z_filename.filename)
        try:
            s_path = fetcher.fetch(s_url, cache_root / s_filename)
        except Exception as exc:
            if _is_not_published(exc):
                skipped.append(week)
                print(
                    f"weekly_update: {week} はsファイル未掲載のためスキップします",
                    file=sys.stderr,
                )
                continue
            raise WeeklyUpdateError(f"{week}のsファイル取得に失敗しました") from exc
        document = jsda_weekly.build_weekly(z_path, s_path, week)
        documents.append((week, document))

    if not documents:
        return WeeklyUpdateResult((), tuple(skipped))

    output_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".weekly-update-", dir=output_root.parent
    ) as temporary_name:
        stage = Path(temporary_name)
        staged_weekly = stage / "weekly"
        existing_weekly = output_root / "weekly"
        if existing_weekly.is_dir():
            shutil.copytree(existing_weekly, staged_weekly)
        elif existing_weekly.exists():
            raise WeeklyUpdateError(
                f"weekly出力先がディレクトリではありません: {existing_weekly}"
            )
        else:
            staged_weekly.mkdir()

        weeks = []
        for week, document in documents:
            _write_document(staged_weekly / f"{week}.json", document)
            weeks.append(week)

        staged_output = stage / "built"
        outputs = build_site.build_site(
            staged_weekly, staged_output, generated_at or _generated_at()
        )
        _commit_weekly_and_outputs(
            output_root,
            staged_weekly,
            weeks,
            outputs,
            stage / "rollback-weekly",
        )
    return WeeklyUpdateResult(tuple(weeks), tuple(skipped))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JSDA indexから新規週を検出して週次dataを増分更新します"
    )
    parser.add_argument("--out", default="data", help="出力dataディレクトリ")
    parser.add_argument(
        "--cache-dir", default="data/_cache", help="ダウンロードキャッシュ"
    )
    parser.add_argument(
        "--meta",
        help="latest_weekを読むmeta.json (省略時は--out配下のmeta.json)",
    )
    args = parser.parse_args(argv)

    try:
        result = run_weekly_update(
            args.out, args.cache_dir, meta_path=args.meta
        )
    except Exception as exc:
        print(f"weekly_update: {exc}", file=sys.stderr)
        return 1

    if result.updated:
        print(UPDATED_MARKER)
        print(
            f"weekly_update: {len(result.updated_weeks)}週を更新しました "
            f"({', '.join(result.updated_weeks)})",
            file=sys.stderr,
        )
    else:
        print("weekly_update: 対象なし")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
