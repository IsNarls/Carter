"""Download Warcraft Logs reports into a local cache used by the analyzer."""

from __future__ import annotations

import base64
import gzip
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


TOKEN_URL = 'https://www.warcraftlogs.com/oauth/token'
GRAPHQL_URL = 'https://www.warcraftlogs.com/api/v2/client'
REPORT_RE = re.compile(r'(?:warcraftlogs\.com/(?:reports|reports/compare)/|^)([A-Za-z0-9]{8,32})')
MAX_STORED_REPORTS = 5
MAX_BATCH_REPORTS = 5
DOWNLOAD_CHECKPOINT_FORMAT = 'wow-log-tools-download-checkpoint-v1'
# Checkpoints contain all events downloaded so far. Rewriting that growing JSON
# document after every API page makes large reports progressively slower. Keep
# periodic crash recovery while always forcing a checkpoint before propagating a
# download error (including a rate limit).
DOWNLOAD_CHECKPOINT_INTERVAL_SECONDS = 300.0


def _download_debug(message: str, progress: Callable[[str], None] | None = None) -> None:
    if os.environ.get('WARCRAFT_LOGS_DOWNLOAD_DEBUG') == '1':
        print(f'Warcraft Logs download: {message}', file=sys.stderr, flush=True)
        if progress is not None:
            progress(f'[debug] {message}')


class WarcraftLogsError(RuntimeError):
    pass


class WarcraftLogsRateLimitError(WarcraftLogsError):
    """Stable rate-limit signal that never includes response bodies or credentials."""

    def __init__(self, retry_after_seconds: int | None = None):
        self.retry_after_seconds = retry_after_seconds
        message = 'Warcraft Logs API rate limit reached; the download checkpoint was preserved.'
        if retry_after_seconds is not None:
            message += f' Retry after approximately {retry_after_seconds} seconds.'
        super().__init__(message)


def analysis_directory(cache_path: Path) -> Path:
    """Return the sidecar directory used for persisted fight analyses."""
    cache_path = Path(cache_path)
    name = cache_path.name
    stem = name[:-len('.wcl.json')] if name.lower().endswith('.wcl.json') else cache_path.stem
    return cache_path.with_name(f'{stem}.analysis')


def metadata_path(cache_path: Path) -> Path:
    cache_path = Path(cache_path)
    name = cache_path.name
    stem = name[:-len('.wcl.json')] if name.lower().endswith('.wcl.json') else cache_path.stem
    return cache_path.with_name(f'{stem}.meta.json')


def download_checkpoint_path(cache_path: Path) -> Path:
    """Return the private resumable-download sidecar for one report cache."""
    cache_path = Path(cache_path)
    return cache_path.with_name(cache_path.name + '.download.json')


def _atomic_json(path: Path, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump(payload, output, ensure_ascii=False, separators=(',', ':'))
    os.replace(temporary, path)


def _load_download_checkpoint(path: Path, code: str) -> dict | None:
    try:
        payload = json.loads(Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, OSError, ValueError):
        return None
    if (not isinstance(payload, dict)
            or payload.get('format') != DOWNLOAD_CHECKPOINT_FORMAT
            or str(payload.get('report_code') or '').casefold() != code.casefold()
            or not isinstance(payload.get('report'), dict)
            or not isinstance(payload.get('fights'), list)
            or not isinstance(payload.get('eventsByFight'), dict)):
        return None
    try:
        next_index = int(payload.get('next_fight_index') or 0)
    except (TypeError, ValueError):
        return None
    if not 0 <= next_index <= len(payload['fights']):
        return None
    current = payload.get('current')
    if current is not None and (not isinstance(current, dict)
            or not isinstance(current.get('events'), list)):
        return None
    payload['next_fight_index'] = next_index
    return payload


def prune_report_caches(directory: Path, maximum: int = MAX_STORED_REPORTS) -> list[Path]:
    """Keep reports with the newest raid timestamps, independent of download order."""
    directory = Path(directory).resolve()

    def chronology(path):
        try:
            payload = json.loads(metadata_path(path).read_text(encoding='utf-8'))
            report = payload.get('report') if isinstance(payload, dict) else None
            if isinstance(report, dict):
                timestamp = int(report.get('endTime') or report.get('startTime') or 0)
                if timestamp > 0:
                    return timestamp, path.stat().st_mtime_ns, path.name.lower()
        except (OSError, TypeError, ValueError):
            pass
        return 0, path.stat().st_mtime_ns, path.name.lower()

    caches = sorted(
        (path for path in directory.glob('warcraft-logs-*.wcl.json') if path.is_file()),
        key=chronology, reverse=True,
    )
    removed = []
    for cache_path in caches[max(0, maximum):]:
        resolved_cache = cache_path.resolve()
        if resolved_cache.parent != directory:
            continue
        sidecar = analysis_directory(resolved_cache).resolve()
        resolved_cache.unlink()
        if sidecar.parent == directory and sidecar.is_dir():
            shutil.rmtree(sidecar)
        metadata = metadata_path(resolved_cache).resolve()
        if metadata.parent == directory and metadata.is_file():
            metadata.unlink()
        removed.append(resolved_cache)
    return removed


PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / '.env'


def load_credentials(env_path: Path | None = None) -> tuple[str, str]:
    """Read Warcraft Logs credentials from process variables or Carter's root .env.

    Process environment variables take precedence. This intentionally implements
    only the small KEY=VALUE subset needed here, keeping the project dependency-free.
    """
    env_path = Path(env_path) if env_path is not None else PROJECT_ENV_FILE
    values: dict[str, str] = {}
    if env_path.is_file():
        for raw_line in env_path.read_text(encoding='utf-8-sig').splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            if '=' not in line:
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
                value = value[1:-1]
            values[key] = value
    client_id = os.environ.get(
        'WARCRAFT_LOGS_CLIENT_ID', os.environ.get(
            'CARTER_WARCRAFT_LOGS_CLIENT_ID', os.environ.get(
                'WCL_CLIENT_ID', values.get(
                    'WARCRAFT_LOGS_CLIENT_ID', values.get(
                        'CARTER_WARCRAFT_LOGS_CLIENT_ID', values.get('WCL_CLIENT_ID', ''),
                    ),
                ),
            ),
        ),
    )
    client_secret = os.environ.get(
        'WARCRAFT_LOGS_CLIENT_SECRET', os.environ.get(
            'CARTER_WARCRAFT_LOGS_CLIENT_SECRET', os.environ.get(
                'WCL_CLIENT_SECRET', values.get(
                    'WARCRAFT_LOGS_CLIENT_SECRET', values.get(
                        'CARTER_WARCRAFT_LOGS_CLIENT_SECRET',
                        values.get('WCL_CLIENT_SECRET', ''),
                    ),
                ),
            ),
        ),
    )
    return client_id.strip(), client_secret.strip()


def extract_report_code(value: str) -> str:
    """Extract a report code from a Warcraft Logs URL or accept a bare code."""
    value = value.strip()
    match = REPORT_RE.search(value)
    if not match:
        raise ValueError('Enter a Warcraft Logs report URL or report code.')
    return match.group(1)


def prepare_report_batch(values, maximum: int = MAX_BATCH_REPORTS) -> list[tuple[str, str]]:
    """Validate and de-duplicate a bounded list of report URLs or codes."""
    entries = [str(value).strip() for value in values if str(value).strip()]
    if not entries:
        raise ValueError('Enter at least one Warcraft Logs report URL or report code.')
    if len(entries) > maximum:
        raise ValueError(f'Import at most {maximum} Warcraft Logs reports at a time.')
    prepared = []
    seen = set()
    for index, value in enumerate(entries, 1):
        try:
            code = extract_report_code(value)
        except ValueError as error:
            raise ValueError(f'Report {index}: {error}') from error
        key = code.casefold()
        if key in seen:
            raise ValueError(f'Report {index}: report {code} is already in this batch.')
        seen.add(key)
        prepared.append((value, code))
    return prepared


class WarcraftLogsClient:
    def __init__(self, client_id: str, client_secret: str, timeout: int = 60):
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.timeout = timeout
        self.access_token = ''

    def authenticate(self):
        if not self.client_id or not self.client_secret:
            raise WarcraftLogsError('A Warcraft Logs client ID and client secret are required.')
        credentials = base64.b64encode(
            f'{self.client_id}:{self.client_secret}'.encode('utf-8')).decode('ascii')
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({'grant_type': 'client_credentials'}).encode('ascii'),
            headers={
                'Authorization': f'Basic {credentials}',
                'Content-Type': 'application/x-www-form-urlencoded',
                'Accept-Encoding': 'gzip',
                'User-Agent': 'WoW-Combat-Log-Tools/1.0',
            },
            method='POST',
        )
        payload = self._open_json(request)
        self.access_token = payload.get('access_token', '')
        if not self.access_token:
            raise WarcraftLogsError('Warcraft Logs did not return an access token.')

    def graphql(self, query: str, variables: dict) -> dict:
        if not self.access_token:
            self.authenticate()
        request = urllib.request.Request(
            GRAPHQL_URL,
            data=json.dumps({'query': query, 'variables': variables}).encode('utf-8'),
            headers={
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json',
                'Accept-Encoding': 'gzip',
                'User-Agent': 'WoW-Combat-Log-Tools/1.0',
            },
            method='POST',
        )
        payload = self._open_json(request)
        if payload.get('errors'):
            for item in payload['errors']:
                extensions = item.get('extensions') if isinstance(item, dict) else {}
                code = str((extensions or {}).get('code') or '').upper()
                message = str(item.get('message') or '') if isinstance(item, dict) else str(item)
                if 'RATE_LIMIT' in code or 'RATE LIMIT' in message.upper():
                    raise WarcraftLogsRateLimitError()
            details = '; '.join(
                str(item.get('message', item)) if isinstance(item, dict) else str(item)
                for item in payload['errors']
            )
            raise WarcraftLogsError(details)
        return payload.get('data', {})

    def _open_json(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                if str(response.headers.get('Content-Encoding') or '').casefold() == 'gzip':
                    body = gzip.decompress(body)
                return json.loads(body.decode('utf-8'))
        except urllib.error.HTTPError as error:
            if error.code == 429:
                retry_after = None
                try:
                    value = error.headers.get('Retry-After')
                    retry_after = max(0, int(value)) if value is not None else None
                except (AttributeError, TypeError, ValueError):
                    pass
                raise WarcraftLogsRateLimitError(retry_after) from error
            body = error.read().decode('utf-8', errors='replace')
            try:
                message = json.loads(body).get('error_description') or json.loads(body).get('error')
            except (ValueError, AttributeError):
                message = body[:300]
            if error.code in (401, 403):
                raise WarcraftLogsError(
                    'Authentication failed or this report is private. Check the client credentials '
                    'and use a public or unlisted report.') from error
            raise WarcraftLogsError(f'Warcraft Logs returned HTTP {error.code}: {message}') from error
        except urllib.error.URLError as error:
            raise WarcraftLogsError(f'Could not connect to Warcraft Logs: {error.reason}') from error
        except json.JSONDecodeError as error:
            raise WarcraftLogsError('Warcraft Logs returned an invalid response.') from error

    def download_report(self, report_url: str, destination: Path,
                        progress: Callable[[str], None] | None = None) -> Path:
        """Download a report with an atomic page checkpoint that survives failures."""
        code = extract_report_code(report_url)
        destination = Path(destination)
        checkpoint_path = download_checkpoint_path(destination)
        progress = progress or (lambda _message: None)
        progress('Authenticating with Warcraft Logs...')
        self.authenticate()
        checkpoint = _load_download_checkpoint(checkpoint_path, code)
        metadata_query = """
        query Report($code: String!) {
          reportData { report(code: $code) {
            code title startTime endTime
            fights {
              id encounterID name startTime endTime kill difficulty size
              friendlyPlayers friendlySpecs
              enemyNPCs { id gameID instanceCount groupCount petOwner }
            }
            masterData {
              logVersion gameVersion lang
              actors { id name type subType petOwner gameID }
              abilities { gameID name type }
            }
          } }
        }
        """
        if checkpoint is not None:
            report = checkpoint['report']
            fights = checkpoint['fights']
            events_by_fight = checkpoint['eventsByFight']
            next_fight_index = max(0, int(checkpoint.get('next_fight_index') or 0))
            current = checkpoint.get('current') if isinstance(checkpoint.get('current'), dict) else None
            progress(f'Resuming report {code} from its saved download checkpoint...')
        else:
            progress(f'Reading report {code}...')
            data = self.graphql(metadata_query, {'code': code})
            report = (data.get('reportData') or {}).get('report')
            if not report:
                raise WarcraftLogsError('The report was not found or is not accessible.')
            fights = [fight for fight in report.get('fights', []) if fight.get('encounterID')]
            if not fights:
                raise WarcraftLogsError('This report contains no boss encounters.')
            events_by_fight = {}
            next_fight_index = 0
            current = None

        last_checkpoint = time.monotonic()

        def save_checkpoint():
            nonlocal last_checkpoint
            _atomic_json(checkpoint_path, {
                'format': DOWNLOAD_CHECKPOINT_FORMAT,
                'report_code': code,
                'report': report,
                'fights': fights,
                'eventsByFight': events_by_fight,
                'next_fight_index': next_fight_index,
                'current': current,
            })
            last_checkpoint = time.monotonic()

        def save_periodic_checkpoint():
            if time.monotonic() - last_checkpoint >= DOWNLOAD_CHECKPOINT_INTERVAL_SECONDS:
                save_checkpoint()

        # A resumed checkpoint may already be very large. Do not rewrite it just
        # to begin the request; the next periodic checkpoint or any failure will
        # persist new progress.
        if checkpoint is None:
            save_checkpoint()

        event_query = """
        query Events($code: String!, $start: Float!, $end: Float!) {
          reportData { report(code: $code) {
            events(startTime: $start, endTime: $end, dataType: All,
                   limit: 10000, includeResources: true, useActorIDs: true) {
              data nextPageTimestamp
            }
          } }
        }
        """
        total = len(fights)
        for offset in range(next_fight_index, total):
            index = offset + 1
            fight = fights[offset]
            fight_id = int(fight['id'])
            # Warcraft Logs currently returns empty continuation pages when
            # fightIDs and startTime are combined. A fight's report-relative
            # time range yields the same encounter events and paginates correctly.
            initial_start = float(fight.get('startTime') or 0)
            end = float(fight.get('endTime') or initial_start)
            if current is not None and int(current.get('fight_id') or 0) == fight_id:
                start = float(current.get('next_start') or initial_start)
                pages = max(0, int(current.get('pages') or 0))
                collected = [item for item in current.get('events', [])
                             if isinstance(item, dict)]
            else:
                start = initial_start
                pages = 0
                collected = []
                current = {'fight_id': fight_id, 'next_start': start,
                           'pages': pages, 'events': collected}
            while True:
                pages += 1
                progress(f'Downloading pull {index}/{total}: {fight.get("name", "Encounter")} '
                         f'({len(collected):,} events)...')
                request_started = time.perf_counter()
                _download_debug(
                    f'report={code} fight={fight_id} pull={index}/{total} page={pages} '
                    f'start={start:g} end={end:g} events_before={len(collected)} request_started',
                    progress,
                )
                try:
                    page_data = self.graphql(event_query, {
                        'code': code, 'start': start, 'end': end,
                    })
                except Exception:
                    # Persist every successfully received page before returning a
                    # retryable failure to the import manager.
                    save_checkpoint()
                    raise
                paginator = (((page_data.get('reportData') or {}).get('report') or {})
                             .get('events') or {})
                batch = paginator.get('data') or []
                collected.extend(item for item in batch if isinstance(item, dict))
                next_timestamp = paginator.get('nextPageTimestamp')
                _download_debug(
                    f'report={code} fight={fight_id} pull={index}/{total} page={pages} '
                    f'request_seconds={time.perf_counter() - request_started:.2f} '
                    f'returned_events={len(batch)} collected_events={len(collected)} '
                    f'next_start={next_timestamp}',
                    progress,
                )
                if next_timestamp is None:
                    events_by_fight[str(fight_id)] = collected
                    next_fight_index = index
                    current = None
                    save_periodic_checkpoint()
                    break
                next_start = float(next_timestamp)
                if next_start <= start or pages > 10000:
                    raise WarcraftLogsError('Event pagination stopped making progress.')
                start = next_start
                current = {'fight_id': fight_id, 'next_start': start,
                           'pages': pages, 'events': collected}
                save_periodic_checkpoint()

        cache = {
            'format': 'wow-log-tools-warcraft-logs-v1',
            'report': report,
            'eventsByFight': events_by_fight,
        }
        _atomic_json(destination, cache)
        metadata = {
            'format': 'wow-log-tools-metadata-v1',
            'report': {key: report.get(key) for key in ('code', 'title', 'startTime', 'endTime')},
            'fights': fights,
        }
        _atomic_json(metadata_path(destination), metadata)
        try:
            checkpoint_path.unlink()
        except FileNotFoundError:
            pass
        removed = prune_report_caches(destination.parent)
        progress(f'Imported {sum(map(len, events_by_fight.values())):,} events.')
        if removed:
            progress('Removed oldest stored report: ' + ', '.join(path.name for path in removed))
        return destination


def download_report_batch(client: WarcraftLogsClient, report_values, directory: Path,
                          progress: Callable[[str], None] | None = None, *, refresh: bool = False
                          ) -> tuple[list[Path], list[dict]]:
    """Reuse complete caches and stop a batch cleanly when the API is rate-limited."""
    prepared = prepare_report_batch(report_values)
    directory = Path(directory)
    progress = progress or (lambda _message: None)
    completed = []
    failures = []
    total = len(prepared)
    for index, (report_value, code) in enumerate(prepared, 1):
        destination = directory / f'warcraft-logs-{code}.wcl.json'
        prefix = f'Report {index}/{total} ({code})'
        if (destination.is_file() and not refresh
                and not download_checkpoint_path(destination).is_file()):
            try:
                load_cache(destination)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            else:
                completed.append(destination)
                progress(f'{prefix}: reused existing local cache')
                continue
        try:
            client.download_report(
                report_value, destination,
                lambda message, label=prefix: progress(f'{label}: {message}'))
            completed.append(destination)
        except WarcraftLogsRateLimitError as error:
            failures.append({'report_code': code, 'error': str(error), 'retryable': True,
                             'checkpoint_saved': download_checkpoint_path(destination).is_file()})
            progress(f'{prefix}: API limit reached; batch stopped safely')
            break
        except Exception as error:
            failures.append({'report_code': code, 'error': str(error)})
            progress(f'{prefix}: failed: {error}')
    return completed, failures


def load_cache(path: Path) -> dict:
    with Path(path).open(encoding='utf-8') as source:
        payload = json.load(source)
    if payload.get('format') != 'wow-log-tools-warcraft-logs-v1':
        raise ValueError('This is not a Warcraft Logs cache created by this application.')
    return payload
