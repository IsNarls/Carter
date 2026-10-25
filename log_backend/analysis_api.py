"""Local HTTP JSON API for persisted WoW encounter analyses."""

from __future__ import annotations

import argparse
import hmac
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, unquote, urlsplit

if __package__:
    from .analysis_service import AnalysisService, ServiceError, API_SCHEMA_VERSION
    from .import_jobs import ImportJobError, ReportImportJobManager
    from .live_log_service import (LIVE_ACCESS_TOKEN_FILENAME,
                                   LIVE_ACCESS_TOKEN_PATTERN, LiveLogService)
else:  # Support ``python analysis_api.py`` from this directory.
    from analysis_service import AnalysisService, ServiceError, API_SCHEMA_VERSION
    from import_jobs import ImportJobError, ReportImportJobManager
    from live_log_service import (LIVE_ACCESS_TOKEN_FILENAME,
                                  LIVE_ACCESS_TOKEN_PATTERN, LiveLogService)


GUILD_ID = re.compile(r'^[1-9][0-9]{0,19}$')
GUILD_HEADER = 'X-Carter-Guild-ID'
LIVE_TOKEN_HEADER = 'X-Carter-Live-Token'
MAX_LIVE_BATCH_BYTES = 512 * 1024


def guild_storage_directory(storage, guild_id):
    """Resolve one guild's private storage without accepting path-like identifiers."""
    value = str(guild_id or '').strip()
    if not GUILD_ID.fullmatch(value):
        raise ServiceError('GUILD_SCOPE_REQUIRED', 'A valid Discord guild scope is required.', 400)
    root = Path(storage).resolve()
    directory = (root / 'guilds' / value).resolve()
    if directory.parent != (root / 'guilds').resolve():
        raise ServiceError('GUILD_SCOPE_REQUIRED', 'A valid Discord guild scope is required.', 400)
    return directory


class GuildAnalysisRegistry:
    """Lazily create isolated analysis and import services for Discord guilds."""

    def __init__(self, storage=None, *, live=None, live_guild_id=None,
                 import_jobs_by_guild=None):
        self.storage = (Path(storage) if storage else Path(__file__).parent).resolve()
        self.live = live
        self.live_guild_id = None
        if live is not None:
            self.live_guild_id = str(live_guild_id or '').strip()
            guild_storage_directory(self.storage, self.live_guild_id)
        self._services = {}
        self._jobs = {str(key): value for key, value in (import_jobs_by_guild or {}).items()}
        self._owned_jobs = set()
        self._owned_live = set()
        self._lock = threading.Lock()

    def authenticate_live_ingest(self, guild_id, supplied):
        """Authenticate without provisioning attacker-selected guild storage."""
        directory = guild_storage_directory(self.storage, guild_id)
        token_path = directory / 'live-analysis' / LIVE_ACCESS_TOKEN_FILENAME
        try:
            if token_path.is_symlink():
                raise OSError('symbolic token path')
            expected = token_path.read_text(encoding='ascii').strip()
        except (OSError, UnicodeError):
            expected = ''
        supplied = str(supplied or '')
        if (not LIVE_ACCESS_TOKEN_PATTERN.fullmatch(expected)
                or not LIVE_ACCESS_TOKEN_PATTERN.fullmatch(supplied)
                or not hmac.compare_digest(supplied, expected)):
            raise ServiceError(
                'LIVE_AUTH_REQUIRED', 'Live ingestion authentication failed.', 401,
            )

    def scope(self, guild_id, *, require_jobs=False, require_live=False):
        guild_id = str(guild_id or '').strip()
        directory = guild_storage_directory(self.storage, guild_id)
        with self._lock:
            service = self._services.get(guild_id)
            guild_live = service.live if service is not None else (
                self.live if guild_id == self.live_guild_id else None)
            if require_live and guild_live is None:
                guild_live = LiveLogService(None, directory, remote=True)
                self._owned_live.add(guild_live)
            if service is None:
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                if hasattr(os, 'chmod'):
                    os.chmod(directory, 0o700)
                service = AnalysisService(directory, live=guild_live)
                self._services[guild_id] = service
            elif require_live and service.live is None:
                service.live = guild_live
            jobs = self._jobs.get(guild_id)
            if require_jobs and jobs is None:
                jobs = ReportImportJobManager(directory)
                self._jobs[guild_id] = jobs
                self._owned_jobs.add(jobs)
            return service, jobs

    def close(self):
        for jobs in list(self._owned_jobs):
            jobs.close()
        self._owned_jobs.clear()
        for live in list(self._owned_live):
            live.stop()
        self._owned_live.clear()


class AnalysisRequestHandler(BaseHTTPRequestHandler):
    registry: GuildAnalysisRegistry
    server_version = 'WoWAnalysisAPI/1.0'

    def log_message(self, format, *args):
        return

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _value(query, name, default=None, cast=str):
        values = query.get(name)
        if not values:
            return default
        try:
            return cast(values[-1])
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_PARAMETER', f'{name} has an invalid value.') from error

    def _body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_CONTENT_LENGTH',
                               'Content-Length must be a nonnegative integer.') from error
        if length < 0:
            raise ServiceError('INVALID_CONTENT_LENGTH',
                               'Content-Length must be a nonnegative integer.')
        try:
            if length > 1_000_000:
                raise ServiceError('REQUEST_TOO_LARGE', 'Request body exceeds 1 MB.', 413)
            body = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError as error:
            raise ServiceError('INVALID_JSON', 'Request body is not valid JSON.') from error
        if not isinstance(body, dict):
            raise ServiceError('INVALID_JSON', 'Request body must be a JSON object.')
        return body

    def _live_body(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_CONTENT_LENGTH',
                               'Content-Length must be a nonnegative integer.') from error
        if length < 1 or length > MAX_LIVE_BATCH_BYTES:
            raise ServiceError('REQUEST_TOO_LARGE',
                               'Live batches must be between 1 byte and 512 KiB.', 413)
        return self.rfile.read(length)

    def _dispatch_get(self, path, query):
        if path == '/api/v1/health':
            return {'schema_version': API_SCHEMA_VERSION, 'status': 'ok'}
        if path == '/api/v1/live/status':
            return self.service.get_live_status()
        if path == '/api/v1/live/timer-module':
            return self.service.get_live_timer_module()
        if path == '/api/v1/live/encounter':
            return self.service.get_live_encounter()
        if path == '/api/v1/live/summary':
            return self.service.get_live_fight_summary()
        if path == '/api/v1/live/timeline':
            return self.service.get_live_timeline()
        if path == '/api/v1/live/mechanic-events':
            return self.service.get_live_mechanic_events(
                self._value(query, 'status'), self._value(query, 'limit', 100, int))
        if path == '/api/v1/live/completed':
            return self.service.list_live_completed_encounters()
        match = re.fullmatch(r'/api/v1/live/completed/(\d+)/summary', path)
        if match:
            return self.service.get_live_completed_summary(int(match.group(1)))
        if path == '/api/v1/live/attempts':
            return self.service.list_live_attempts()
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/timeline', path)
        if match:
            return self.service.get_live_attempt_timeline(int(match.group(1)))
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/summary', path)
        if match:
            return self.service.get_live_attempt_summary(int(match.group(1)))
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/players', path)
        if match:
            return self.service.find_live_attempt_players(
                int(match.group(1)), self._value(query, 'query', ''))
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/players/([^/]+)', path)
        if match:
            return self.service.get_live_attempt_player_performance(
                int(match.group(1)), player_id=unquote(match.group(2)))
        match = re.fullmatch(
            r'/api/v1/live/attempts/(\d+)/rankings/(damage|healing|dps|hps)', path)
        if match:
            attempt, metric = int(match.group(1)), match.group(2)
            limit = self._value(query, 'limit', 10, int)
            methods = {
                'damage': self.service.get_live_attempt_damage_ranking,
                'healing': self.service.get_live_attempt_healing_ranking,
                'dps': self.service.get_live_attempt_dps_ranking,
                'hps': self.service.get_live_attempt_hps_ranking,
            }
            return methods[metric](attempt, limit)
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/deaths', path)
        if match:
            return self.service.list_live_attempt_deaths(
                int(match.group(1)), self._value(query, 'player_id'),
                self._value(query, 'player_name'))
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/deaths/([^/]+)/answer', path)
        if match:
            return self.service.get_live_attempt_death_answer(
                int(match.group(1)), unquote(match.group(2)),
                self._value(query, 'seconds_before', 10, float))
        match = re.fullmatch(r'/api/v1/live/attempts/(\d+)/deaths/([^/]+)', path)
        if match:
            return self.service.get_live_attempt_death_analysis(
                int(match.group(1)), death_id=unquote(match.group(2)),
                seconds_before=self._value(query, 'seconds_before', 10, float))
        if path == '/api/v1/live/players':
            return self.service.find_live_players(self._value(query, 'query', ''))
        match = re.fullmatch(r'/api/v1/live/players/([^/]+)', path)
        if match:
            return self.service.get_live_player_performance(player_id=unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/live/rankings/(damage|healing|dps|hps)', path)
        if match:
            metric = match.group(1)
            limit = self._value(query, 'limit', 10, int)
            methods = {
                'damage': self.service.get_live_damage_ranking,
                'healing': self.service.get_live_healing_ranking,
                'dps': self.service.get_live_dps_ranking,
                'hps': self.service.get_live_hps_ranking,
            }
            return methods[metric](limit)
        if path == '/api/v1/live/mechanics':
            return self.service.get_live_mechanics(
                self._value(query, 'start', None, float), self._value(query, 'end', None, float),
                self._value(query, 'ability_id', None, int))
        if path == '/api/v1/live/deaths':
            return self.service.list_live_deaths(self._value(query, 'player_id'),
                                                 self._value(query, 'player_name'))
        match = re.fullmatch(r'/api/v1/live/deaths/([^/]+)/answer', path)
        if match:
            return self.service.get_live_death_answer(
                unquote(match.group(1)), self._value(query, 'seconds_before', 10, float))
        match = re.fullmatch(r'/api/v1/live/deaths/([^/]+)/replay', path)
        if match:
            return self.service.get_live_death_replay(
                unquote(match.group(1)), self._value(query, 'before', 5, float),
                self._value(query, 'after', 0, float),
                self._value(query, 'resolution_ms', 100, int))
        match = re.fullmatch(r'/api/v1/live/deaths/([^/]+)', path)
        if match:
            return self.service.get_live_death_analysis(
                death_id=unquote(match.group(1)),
                seconds_before=self._value(query, 'seconds_before', 10, float))
        if path == '/api/v1/live/updates':
            return self.service.get_live_updates(self._value(query, 'since', 0, int),
                                                 self._value(query, 'limit', 100, int))
        if path == '/api/v1/reports':
            return self.service.list_reports()
        if path == '/api/v1/fights':
            return self.service.list_fights()
        match = re.fullmatch(r'/api/v1/imports/([^/]+)', path)
        if match:
            return self.import_jobs.get(unquote(match.group(1)))
        if path == '/api/v1/mechanic-drafts':
            return self.service.list_mechanic_drafts()
        match = re.fullmatch(r'/api/v1/mechanic-drafts/([^/]+)/timer-export', path)
        if match:
            return self.service.get_timer_export(unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/mechanic-drafts/([^/]+)', path)
        if match:
            return self.service.get_mechanic_draft(unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)', path)
        if match:
            return self.service.get_report(unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights', path)
        if match:
            return self.service.list_encounters(unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)', path)
        if match:
            return self.service.get_encounter(unquote(match.group(1)), int(match.group(2)))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/summary', path)
        if match:
            return self.service.get_fight_summary(unquote(match.group(1)), int(match.group(2)))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/players', path)
        if match:
            return self.service.find_players(unquote(match.group(1)), int(match.group(2)),
                                             self._value(query, 'query', ''))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/players/([^/]+)', path)
        if match:
            return self.service.get_player_performance(
                unquote(match.group(1)), int(match.group(2)), player_id=unquote(match.group(3)))
        match = re.fullmatch(
            r'/api/v1/reports/([^/]+)/fights/(\d+)/rankings/(damage|healing|dps|hps)', path)
        if match:
            metric = match.group(3)
            args = (unquote(match.group(1)), int(match.group(2)),
                    self._value(query, 'limit', 10, int))
            methods = {
                'damage': self.service.get_damage_ranking,
                'healing': self.service.get_healing_ranking,
                'dps': self.service.get_dps_ranking,
                'hps': self.service.get_hps_ranking,
            }
            return methods[metric](*args)
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/mechanics', path)
        if match:
            return self.service.get_mechanics(
                unquote(match.group(1)), int(match.group(2)),
                self._value(query, 'start', None, float), self._value(query, 'end', None, float),
                self._value(query, 'ability_id', None, int))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/deaths', path)
        if match:
            return self.service.list_deaths(
                unquote(match.group(1)), int(match.group(2)),
                self._value(query, 'player_id'), self._value(query, 'player_name'))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/deaths/([^/]+)/answer', path)
        if match:
            return self.service.get_death_answer(
                unquote(match.group(1)), int(match.group(2)), unquote(match.group(3)),
                self._value(query, 'seconds_before', 10, float))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/deaths/([^/]+)/replay', path)
        if match:
            return self.service.get_death_replay(
                unquote(match.group(1)), int(match.group(2)), unquote(match.group(3)),
                self._value(query, 'before', 8, float), self._value(query, 'after', 2, float),
                self._value(query, 'resolution_ms', 500, int))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/deaths/([^/]+)', path)
        if match:
            return self.service.get_death_analysis(
                unquote(match.group(1)), int(match.group(2)), death_id=unquote(match.group(3)),
                seconds_before=self._value(query, 'seconds_before', 10, float))
        raise ServiceError('ENDPOINT_NOT_FOUND', 'The requested endpoint does not exist.', 404)

    def _dispatch_post(self, path):
        if path == '/api/v1/live/ingest':
            stream_id = self.headers.get('X-Carter-Live-Stream-ID')
            offset = self.headers.get('X-Carter-Live-Offset')
            try:
                result = self.service.live.ingest_remote_batch(
                    stream_id, offset, self._live_body())
            except ValueError as error:
                raise ServiceError('INVALID_LIVE_BATCH', str(error)) from error
            if result.get('offset_mismatch'):
                return 409, result
            return result
        body = self._body()
        if path == '/api/v1/imports/reset':
            if body:
                raise ServiceError('INVALID_PARAMETER', 'The reset request does not accept options.')
            return self.import_jobs.reset_report_data()
        if path == '/api/v1/imports':
            if set(body) - {'report_url', 'refresh'}:
                raise ServiceError(
                    'INVALID_PARAMETER',
                    'Only report_url and refresh are accepted; credentials and destination paths '
                    'are backend-owned.')
            return 202, self.import_jobs.submit(
                body.get('report_url'), refresh=body.get('refresh', False),
            )
        if path == '/api/v1/live/timer-module/arm':
            return self.service.arm_live_timer_module(
                body.get('timer_id'), body.get('encounter_id'), body.get('difficulty'))
        if path == '/api/v1/live/timer-module/disarm':
            return self.service.disarm_live_timer_module()
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/mechanics/compare', path)
        if match:
            return self.service.compare_mechanics(unquote(match.group(1)), body.get('fight_ids', []),
                                                  body.get('ability_id'))
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/deaths/compare', path)
        if match:
            return self.service.compare_player_deaths(
                unquote(match.group(1)), body.get('fight_ids', []), body.get('player_id'),
                body.get('player_name'))
        raise ServiceError('ENDPOINT_NOT_FOUND', 'The requested endpoint does not exist.', 404)

    def _handle(self, method):
        request = urlsplit(self.path)
        try:
            if request.path != '/api/v1/health':
                guild_id = self.headers.get(GUILD_HEADER)
                if request.path == '/api/v1/live/ingest':
                    self.registry.authenticate_live_ingest(
                        guild_id, self.headers.get(LIVE_TOKEN_HEADER),
                    )
                self.service, self.import_jobs = self.registry.scope(
                    guild_id,
                    require_jobs=request.path == '/api/v1/imports'
                    or request.path.startswith('/api/v1/imports/'),
                    require_live=request.path == '/api/v1/live/ingest',
                )
            result = (self._dispatch_get(request.path, parse_qs(request.query))
                      if method == 'GET' else self._dispatch_post(request.path))
            status, payload = result if isinstance(result, tuple) else (200, result)
            self._send(status, payload)
        except (ServiceError, ImportJobError) as error:
            self._send(error.status, error.response())
        except Exception:
            self._send(500, {'error': {'code': 'INTERNAL_ERROR',
                                      'message': 'The request could not be completed.'}})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


class AnalysisHTTPServer(ThreadingHTTPServer):
    """HTTP server that releases its owned background import worker."""

    def __init__(self, *args, registry=None, **kwargs):
        self.registry = registry
        super().__init__(*args, **kwargs)

    def server_close(self):
        try:
            super().server_close()
        finally:
            if self.registry is not None:
                self.registry.close()


def create_server(storage=None, host='127.0.0.1', port=8765, live=None,
                  live_guild_id=None, import_jobs_by_guild=None):
    registry = GuildAnalysisRegistry(
        storage, live=live, live_guild_id=live_guild_id,
        import_jobs_by_guild=import_jobs_by_guild,
    )
    handler = type('ConfiguredAnalysisHandler', (AnalysisRequestHandler,), {
        'registry': registry,
    })
    return AnalysisHTTPServer((host, int(port)), handler, registry=registry)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', type=Path, default=Path(__file__).parent)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--live-log', type=Path,
                        help='Owner-configured local WoWCombatLog.txt to follow')
    parser.add_argument('--live-guild-id',
                        help='Discord guild that owns --live-log data')
    parser.add_argument('--live-from-start', action='store_true',
                        help='Read an existing live log from its beginning')
    parser.add_argument('--mechanic-scripts', type=Path,
                        help='Optional JSON file containing placeholder mechanic scripts')
    parser.add_argument('--timer-modules', type=Path,
                        help='Directory or file containing reviewed .timer.json modules')
    args = parser.parse_args()
    live = None
    if args.live_log:
        if not args.live_guild_id:
            parser.error('--live-guild-id is required with --live-log')
        if __package__:
            from .live_log_service import LiveLogService
            from .mechanic_timeline import MechanicScriptRegistry
        else:
            from live_log_service import LiveLogService
            from mechanic_timeline import MechanicScriptRegistry
        module_source = args.timer_modules or args.mechanic_scripts
        live_storage = guild_storage_directory(args.storage, args.live_guild_id)
        default_modules = live_storage / 'timer-exports'
        if module_source is None:
            default_modules.mkdir(parents=True, exist_ok=True)
            module_source = default_modules
        registry = MechanicScriptRegistry.from_file(module_source)
        live = LiveLogService(args.live_log, live_storage, from_end=not args.live_from_start,
                              mechanic_registry=registry)
        live.start()
    server = create_server(
        args.storage, args.host, args.port, live=live, live_guild_id=args.live_guild_id,
    )
    print(f'WoW analysis API listening on http://{args.host}:{args.port}/api/v1')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if live:
            live.stop()


if __name__ == '__main__':
    main()
