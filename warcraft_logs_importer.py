"""Download Warcraft Logs reports into a local cache used by the analyzer."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
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


class WarcraftLogsError(RuntimeError):
    pass


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


def prune_report_caches(directory: Path, maximum: int = MAX_STORED_REPORTS) -> list[Path]:
    """Keep only the newest Warcraft Logs report caches and their analyses."""
    directory = Path(directory).resolve()
    caches = sorted(
        (path for path in directory.glob('warcraft-logs-*.wcl.json') if path.is_file()),
        key=lambda path: (path.stat().st_mtime_ns, path.name.lower()), reverse=True,
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


def load_credentials(env_path: Path | None = None) -> tuple[str, str]:
    """Read Warcraft Logs credentials from process variables or a local .env file.

    Process environment variables take precedence. This intentionally implements
    only the small KEY=VALUE subset needed here, keeping the project dependency-free.
    """
    env_path = Path(env_path) if env_path is not None else Path(__file__).with_name('.env')
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
        'WARCRAFT_LOGS_CLIENT_ID', os.environ.get('WCL_CLIENT_ID',
                                                  values.get('WARCRAFT_LOGS_CLIENT_ID',
                                                             values.get('WCL_CLIENT_ID', ''))))
    client_secret = os.environ.get(
        'WARCRAFT_LOGS_CLIENT_SECRET', os.environ.get('WCL_CLIENT_SECRET',
                                                      values.get('WARCRAFT_LOGS_CLIENT_SECRET',
                                                                 values.get('WCL_CLIENT_SECRET', ''))))
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
                'User-Agent': 'WoW-Combat-Log-Tools/1.0',
            },
            method='POST',
        )
        payload = self._open_json(request)
        if payload.get('errors'):
            details = '; '.join(item.get('message', str(item)) for item in payload['errors'])
            raise WarcraftLogsError(details)
        return payload.get('data', {})

    def _open_json(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as error:
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
        """Download every event from every boss pull and atomically save a JSON cache."""
        code = extract_report_code(report_url)
        progress = progress or (lambda _message: None)
        progress('Authenticating with Warcraft Logs...')
        self.authenticate()
        progress(f'Reading report {code}...')
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
        data = self.graphql(metadata_query, {'code': code})
        report = (data.get('reportData') or {}).get('report')
        if not report:
            raise WarcraftLogsError('The report was not found or is not accessible.')
        fights = [fight for fight in report.get('fights', []) if fight.get('encounterID')]
        if not fights:
            raise WarcraftLogsError('This report contains no boss encounters.')

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
        events_by_fight: dict[str, list[dict]] = {}
        total = len(fights)
        for index, fight in enumerate(fights, 1):
            fight_id = int(fight['id'])
            # Warcraft Logs currently returns empty continuation pages when
            # fightIDs and startTime are combined. A fight's report-relative
            # time range yields the same encounter events and paginates correctly.
            start = float(fight.get('startTime') or 0)
            end = float(fight.get('endTime') or start)
            pages = 0
            collected: list[dict] = []
            while True:
                pages += 1
                progress(f'Downloading pull {index}/{total}: {fight.get("name", "Encounter")} '
                         f'({len(collected):,} events)...')
                page_data = self.graphql(event_query, {
                    'code': code, 'start': start, 'end': end,
                })
                paginator = (((page_data.get('reportData') or {}).get('report') or {})
                             .get('events') or {})
                batch = paginator.get('data') or []
                collected.extend(item for item in batch if isinstance(item, dict))
                next_timestamp = paginator.get('nextPageTimestamp')
                if next_timestamp is None:
                    break
                next_start = float(next_timestamp)
                if next_start <= start or pages > 10000:
                    raise WarcraftLogsError('Event pagination stopped making progress.')
                start = next_start
            events_by_fight[str(fight_id)] = collected

        cache = {
            'format': 'wow-log-tools-warcraft-logs-v1',
            'report': report,
            'eventsByFight': events_by_fight,
        }
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + '.part')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(cache, output, ensure_ascii=False, separators=(',', ':'))
        temporary.replace(destination)
        metadata = {
            'format': 'wow-log-tools-metadata-v1',
            'report': {key: report.get(key) for key in ('code', 'title', 'startTime', 'endTime')},
            'fights': fights,
        }
        meta_destination = metadata_path(destination)
        meta_temporary = meta_destination.with_suffix(meta_destination.suffix + '.part')
        with meta_temporary.open('w', encoding='utf-8') as output:
            json.dump(metadata, output, ensure_ascii=False, separators=(',', ':'))
        meta_temporary.replace(meta_destination)
        removed = prune_report_caches(destination.parent)
        progress(f'Imported {sum(map(len, events_by_fight.values())):,} events.')
        if removed:
            progress('Removed oldest stored report: ' + ', '.join(path.name for path in removed))
        return destination


def download_report_batch(client: WarcraftLogsClient, report_values, directory: Path,
                          progress: Callable[[str], None] | None = None
                          ) -> tuple[list[Path], list[dict]]:
    """Download up to five reports sequentially, continuing after per-report failures."""
    prepared = prepare_report_batch(report_values)
    directory = Path(directory)
    progress = progress or (lambda _message: None)
    completed = []
    failures = []
    total = len(prepared)
    for index, (report_value, code) in enumerate(prepared, 1):
        destination = directory / f'warcraft-logs-{code}.wcl.json'
        prefix = f'Report {index}/{total} ({code})'
        try:
            client.download_report(
                report_value, destination,
                lambda message, label=prefix: progress(f'{label}: {message}'))
            completed.append(destination)
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

