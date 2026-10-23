"""Local HTTP JSON API for persisted WoW encounter analyses."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from analysis_service import AnalysisService, ServiceError, API_SCHEMA_VERSION


class AnalysisRequestHandler(BaseHTTPRequestHandler):
    service: AnalysisService
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
            if length > 1_000_000:
                raise ServiceError('REQUEST_TOO_LARGE', 'Request body exceeds 1 MB.', 413)
            return json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError as error:
            raise ServiceError('INVALID_JSON', 'Request body is not valid JSON.') from error

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
        if path == '/api/v1/live/players':
            return self.service.find_live_players(self._value(query, 'query', ''))
        match = re.fullmatch(r'/api/v1/live/players/([^/]+)', path)
        if match:
            return self.service.get_live_player_performance(player_id=unquote(match.group(1)))
        match = re.fullmatch(r'/api/v1/live/rankings/(damage|healing)', path)
        if match:
            limit = self._value(query, 'limit', 10, int)
            return (self.service.get_live_damage_ranking(limit) if match.group(1) == 'damage'
                    else self.service.get_live_healing_ranking(limit))
        if path == '/api/v1/live/mechanics':
            return self.service.get_live_mechanics(
                self._value(query, 'start', None, float), self._value(query, 'end', None, float),
                self._value(query, 'ability_id', None, int))
        if path == '/api/v1/live/deaths':
            return self.service.list_live_deaths(self._value(query, 'player_id'),
                                                 self._value(query, 'player_name'))
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
        match = re.fullmatch(r'/api/v1/reports/([^/]+)/fights/(\d+)/rankings/(damage|healing)', path)
        if match:
            args = (unquote(match.group(1)), int(match.group(2)),
                    self._value(query, 'limit', 10, int))
            return (self.service.get_damage_ranking(*args) if match.group(3) == 'damage'
                    else self.service.get_healing_ranking(*args))
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
        body = self._body()
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
            payload = (self._dispatch_get(request.path, parse_qs(request.query))
                       if method == 'GET' else self._dispatch_post(request.path))
            self._send(200, payload)
        except ServiceError as error:
            self._send(error.status, error.response())
        except Exception:
            self._send(500, {'error': {'code': 'INTERNAL_ERROR',
                                      'message': 'The request could not be completed.'}})

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


def create_server(storage=None, host='127.0.0.1', port=8765, live=None):
    service = AnalysisService(storage, live=live)
    handler = type('ConfiguredAnalysisHandler', (AnalysisRequestHandler,), {'service': service})
    return ThreadingHTTPServer((host, int(port)), handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', type=Path, default=Path(__file__).parent)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--live-log', type=Path,
                        help='Owner-configured local WoWCombatLog.txt to follow')
    parser.add_argument('--live-from-start', action='store_true',
                        help='Read an existing live log from its beginning')
    parser.add_argument('--mechanic-scripts', type=Path,
                        help='Optional JSON file containing placeholder mechanic scripts')
    parser.add_argument('--timer-modules', type=Path,
                        help='Directory or file containing reviewed .timer.json modules')
    args = parser.parse_args()
    live = None
    if args.live_log:
        from live_log_service import LiveLogService
        from mechanic_timeline import MechanicScriptRegistry
        module_source = args.timer_modules or args.mechanic_scripts
        default_modules = args.storage / 'timer-exports'
        if module_source is None:
            default_modules.mkdir(parents=True, exist_ok=True)
            module_source = default_modules
        registry = MechanicScriptRegistry.from_file(module_source)
        live = LiveLogService(args.live_log, args.storage, from_end=not args.live_from_start,
                              mechanic_registry=registry)
        live.start()
    server = create_server(args.storage, args.host, args.port, live=live)
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
