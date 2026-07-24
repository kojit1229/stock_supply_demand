"""Rebuild JSDA weekly lending balances from half-year archives.

Only z (week-end balance) workbooks are in scope.  Completed half-years come
from JSDA zip archives; the current half-year is discovered from the index so
holiday-shifted report dates never have to be guessed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
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
# 増分11.5: s(新規成約高)は半期zipにzと同梱されている(2026-07-24実測、design.md
# §2)。命名規則・訂正版括弧書きはzと同一パターンでkind文字だけが異なる
_S_FILENAME = re.compile(
    r"^(?P<day>\d{8})s(?:\((?P<revision>\d{8})r\))?\.(?:xls|xlsx)$",
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
    # 増分11.5: sが見つからずbuild_z_weekly(shinki空)にフォールバックした週。
    # フェイルラウドにはしない(sの欠落自体はfailuresへ入れない)ので、進捗可視化
    # 用に別枠で数える。デフォルト空リストで既存呼び出しに影響しない
    s_missing_weeks: list[str] = field(default_factory=list)
    # sの抽出・取得を試みて失敗した際の非致命的な理由(診断用、exit_codeには
    # 影響しない)。s_missing_weeksとの違い: こちらは「なぜ拾えなかったか」の
    # ログで、選外(意図的にs無し)と取得失敗の両方を含みうる
    s_notices: list[str] = field(default_factory=list)

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
        user_agent: str = jsda_weekly.USER_AGENT,
    ) -> None:
        self._opener = opener or urlopen
        self._sleep = sleep
        self._monotonic = monotonic
        self._user_agent = user_agent
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
                request = Request(url, headers={"User-Agent": self._user_agent})
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


def parse_s_name(filename: str) -> ZName | None:
    """Parse a plain/revised s filename; return None for j/z/unrelated files.

    増分11.5: zとまったく同じ形の revision-aware ファイル名なので ``ZName`` を
    そのまま再利用する(sを表す専用フィールド名は付けない=歴史的な"zname"の
    まま、s向けの呼び出し側では変数名で区別する)。
    """
    basename = Path(filename).name
    match = _S_FILENAME.fullmatch(basename)
    if match is None:
        return None
    raw_day = match.group("day")
    try:
        report_date = date(
            int(raw_day[:4]), int(raw_day[4:6]), int(raw_day[6:8])
        )
    except ValueError as exc:
        raise BackfillError(f"sファイル名の日付が不正です: {basename}") from exc
    revision = match.group("revision")
    if revision is not None:
        try:
            date(int(revision[:4]), int(revision[4:6]), int(revision[6:8]))
        except ValueError as exc:
            raise BackfillError(f"訂正日が不正です: {basename}") from exc
    return ZName(basename, report_date, revision)


def select_preferred_s_names(filenames: Iterable[str]) -> dict[date, ZName]:
    """Select the newest revised s file for each report week (訂正版優先)."""
    selected: dict[date, ZName] = {}
    for filename in sorted(filenames):
        candidate = parse_s_name(filename)
        if candidate is None:
            continue
        current = selected.get(candidate.report_date)
        if current is None or candidate.priority > current.priority:
            selected[candidate.report_date] = candidate
        elif candidate.priority == current.priority and candidate.filename != current.filename:
            raise BackfillError(
                f"同一優先度のsファイルが複数あります: "
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


def discover_s_urls(index_html: str, index_url: str = INDEX_URL) -> dict[date, str]:
    """Discover and revision-resolve s links from the JSDA index HTML.

    増分11.5: 現行半期のindexページはzと同じ並びでsもリンクしている
    (2026-07-24実測)。sはあくまで任意扱いなので、呼び出し側は見つからなくても
    フェイルラウドしない。
    """
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
        if parse_s_name(filename) is not None:
            urls_by_name[filename] = absolute
    preferred = select_preferred_s_names(urls_by_name)
    return {day: urls_by_name[sname.filename] for day, sname in preferred.items()}


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


def _extract_archive_s_candidates(
    archive_path: Path,
    extract_root: Path,
    start: date,
    end: date,
) -> tuple[list[WeeklyCandidate], list[str]]:
    """Best-effort extraction of s(新規成約高) files from the same half-year zip.

    増分11.5: 構造は_extract_archive_candidates(z)と同一だが、sはzと違い必須では
    ない。zipファイル自体が開けない(BadZipFile等)場合はここで例外を送出し、
    呼び出し側(run_backfill)がアーカイブ単位でs_noticesへ記録する。

    reviewer指摘A(2026-07-24)対応: メンバー単位の抽出(archive.open→copyfileobj)は
    個別にtry/exceptで囲む。zip内の特定週のsメンバー1本だけが破損(CRC不正等)
    していても、その週だけを ``notices`` (戻り値の2要素目、週を特定できる文言)
    へ記録して処理を継続し、他の週のsは正常に取り込む(以前は関数全体が
    1つのtry/exceptで、1メンバーの破損が半期全体のs欠落を招いていた)。
    """
    try:
        archive = ZipFile(archive_path)
    except (BadZipFile, OSError, RuntimeError) as exc:
        raise BackfillError(f"zip(sファイル)を展開できません: {archive_path}") from exc
    try:
        try:
            infos_by_name = {
                Path(info.filename).name: info
                for info in archive.infolist()
                if not info.is_dir() and Path(info.filename).name
            }
            preferred = select_preferred_s_names(infos_by_name)
            destination_dir = extract_root / (archive_path.stem + "-s")
            destination_dir.mkdir(parents=True, exist_ok=True)
        except (BadZipFile, OSError, RuntimeError) as exc:
            raise BackfillError(f"zip(sファイル)を展開できません: {archive_path}") from exc

        candidates: list[WeeklyCandidate] = []
        notices: list[str] = []
        for report_date, sname in sorted(preferred.items()):
            if not start <= report_date <= end:
                continue
            destination = destination_dir / sname.filename
            try:
                with archive.open(infos_by_name[sname.filename]) as source, destination.open(
                    "wb"
                ) as output:
                    shutil.copyfileobj(source, output)
            except (BadZipFile, OSError, RuntimeError) as exc:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                notices.append(
                    f"{archive_path.name} {report_date.isoformat()} "
                    f"({sname.filename}): sメンバーを展開できません: {exc}"
                )
                continue
            candidates.append(WeeklyCandidate(sname, destination))
        return candidates, notices
    finally:
        archive.close()


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
    cache_root = Path(cache_dir)
    weekly_dir = output_root / "weekly"
    cache_root.mkdir(parents=True, exist_ok=True)
    fetcher = downloader or CachedDownloader()
    failures: list[str] = []
    processed: list[str] = []
    candidates: list[WeeklyCandidate] = []
    # 増分11.5: sは任意扱い。抽出/取得の失敗はここに集めるだけで、failuresには
    # 混ぜない(sが本当に存在しない週を許容するため=フェイルラウドの対象外)
    s_candidates: list[WeeklyCandidate] = []
    s_notices: list[str] = []

    with tempfile.TemporaryDirectory(prefix="jsda-backfill-") as temp_name:
        extract_root = Path(temp_name)
        for archive_name in archive_names(start, end, effective_today):
            try:
                archive_path = fetcher.fetch(
                    urljoin(FILES_URL, archive_name), cache_root / archive_name
                )
                candidates.extend(
                    _extract_archive_candidates(archive_path, extract_root, start, end)
                )
            except Exception as exc:
                failures.append(f"{archive_name}: {exc}")
                continue
            try:
                new_s_candidates, new_s_notices = _extract_archive_s_candidates(
                    archive_path, extract_root, start, end
                )
                s_candidates.extend(new_s_candidates)
                s_notices.extend(new_s_notices)
            except Exception as exc:
                s_notices.append(f"{archive_name}: {exc}")

        current_half = _half_start(effective_today)
        if end >= current_half:
            try:
                index_path = fetcher.fetch(
                    INDEX_URL, cache_root / f"index-{effective_today.isoformat()}.html"
                )
                # Link targets are ASCII; replacement keeps discovery working even
                # if JSDA changes the surrounding Japanese page encoding.
                index_html = index_path.read_bytes().decode("utf-8", errors="replace")
                discovered_s = discover_s_urls(index_html)
                for report_date, url in sorted(discover_z_urls(index_html).items()):
                    if not (start <= report_date <= end and report_date >= current_half):
                        continue
                    filename = Path(unquote(urlparse(url).path)).name
                    try:
                        source_path = fetcher.fetch(url, cache_root / filename)
                        zname = parse_z_name(filename)
                        if zname is None:
                            raise BackfillError(f"zファイル名ではありません: {filename}")
                        candidates.append(WeeklyCandidate(zname, source_path))
                    except Exception as exc:
                        failures.append(f"{report_date.isoformat()} ({filename}): {exc}")
                    # sも同じ週について取得を試みる(JSDAは連続リクエストでブロック
                    # される実績があるため、CachedDownloaderの間隔制御に乗せて
                    # z取得の直後に1件ずつ取る。失敗してもfailuresには入れない)
                    s_url = discovered_s.get(report_date)
                    if s_url is None:
                        continue
                    s_filename = Path(unquote(urlparse(s_url).path)).name
                    try:
                        s_source_path = fetcher.fetch(s_url, cache_root / s_filename)
                        sname = parse_s_name(s_filename)
                        if sname is None:
                            raise BackfillError(f"sファイル名ではありません: {s_filename}")
                        s_candidates.append(WeeklyCandidate(sname, s_source_path))
                    except Exception as exc:
                        s_notices.append(f"{report_date.isoformat()} ({s_filename}): {exc}")
            except Exception as exc:
                failures.append(f"index: {exc}")

        try:
            selected = _merge_candidates(candidates)
        except Exception as exc:
            failures.append(f"候補選別: {exc}")
            selected = {}
        try:
            selected_s = _merge_candidates(s_candidates)
        except Exception as exc:
            s_notices.append(f"s候補選別: {exc}")
            selected_s = {}

        if not selected and not failures:
            failures.append(f"{start.isoformat()}..{end.isoformat()}: 対象zファイルがありません")

        s_missing_weeks: list[str] = []
        for report_date, candidate in sorted(selected.items()):
            week_text = report_date.isoformat()
            destination = weekly_dir / f"{week_text}.json"
            s_candidate = selected_s.get(report_date)
            try:
                if s_candidate is not None:
                    document = jsda_weekly.build_weekly(
                        candidate.path,
                        s_candidate.path,
                        week_text,
                        min_issue_count=minimum,
                    )
                else:
                    s_missing_weeks.append(week_text)
                    document = jsda_weekly.build_z_weekly(
                        candidate.path,
                        week_text,
                        min_issue_count=minimum,
                    )
                _write_weekly(destination, document)
                processed.append(week_text)
            except Exception as exc:
                failure = f"{week_text} ({candidate.zname.filename}): {exc}"
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                except OSError as remove_exc:
                    failure += f"; 既存出力も削除できません: {remove_exc}"
                failures.append(failure)

    if not failures:
        try:
            build_site.build_site(weekly_dir, output_root, _generated_at())
        except Exception as exc:
            failures.append(f"build_site: {exc}")
    return BackfillResult(processed, failures, s_missing_weeks, s_notices)


def _parse_cli_date(value: str, label: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise BackfillError(f"{label}はYYYY-MM-DD形式で指定してください: {value!r}") from exc
    if parsed.isoformat() != value:
        raise BackfillError(f"{label}はYYYY-MM-DD形式で指定してください: {value!r}")
    return parsed


def _three_years_before(day: date) -> date:
    try:
        return day.replace(year=day.year - DEFAULT_YEARS)
    except ValueError:
        # February 29 has no counterpart in most prior years.
        return day.replace(year=day.year - DEFAULT_YEARS, day=28)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="JSDA半期zipから週次貸借残高とseries shardを再構築します"
    )
    parser.add_argument("--start", help="開始報告日 (YYYY-MM-DD)")
    parser.add_argument("--end", help="終了報告日 (YYYY-MM-DD)")
    parser.add_argument("--out", default="data", help="出力dataディレクトリ")
    parser.add_argument(
        "--cache-dir", default="data/_cache", help="ダウンロードキャッシュ"
    )
    args = parser.parse_args(argv)

    try:
        end = _parse_cli_date(args.end, "end") if args.end else date.today()
        start = (
            _parse_cli_date(args.start, "start")
            if args.start
            else _three_years_before(end)
        )
        result = run_backfill(start, end, args.out, args.cache_dir)
    except (BackfillError, OSError) as exc:
        print(f"backfill_jsda: {exc}", file=sys.stderr)
        return 1

    if result.failures:
        processed = ", ".join(result.processed_weeks) if result.processed_weeks else "なし"
        print(f"backfill_jsda: 処理済み週: {processed}", file=sys.stderr)
        print("backfill_jsda: 失敗週/取得元:", file=sys.stderr)
        for failure in result.failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"backfill_jsda: {len(result.processed_weeks)}週を処理しました "
        f"({result.processed_weeks[0]}..{result.processed_weeks[-1]})"
    )
    # 増分11.5: sの欠落はフェイルラウド対象外だが、可視化のため件数だけ出す
    # (exit_codeには影響しない)
    if result.s_missing_weeks:
        print(
            f"backfill_jsda: sファイルが見つからずshinki空のままの週: "
            f"{len(result.s_missing_weeks)}件 ({', '.join(result.s_missing_weeks)})",
            file=sys.stderr,
        )
    for notice in result.s_notices:
        print(f"backfill_jsda: sファイル取得の非致命的な問題: {notice}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
