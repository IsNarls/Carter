"""Read-only, JSON-friendly queries over persisted Warcraft Logs analyses."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json
import re

if __package__:
    from .encounter_parser import EncounterRef, EncounterTimeline, load_saved_analysis
    from .timer_export import build_timer_export
    from .death_answer import build_death_answer
    from .warcraft_logs_importer import metadata_path
else:  # Support direct execution from this directory.
    from encounter_parser import EncounterRef, EncounterTimeline, load_saved_analysis
    from timer_export import build_timer_export
    from death_answer import build_death_answer
    from warcraft_logs_importer import metadata_path


API_SCHEMA_VERSION = 1
DEFAULT_RANKING_LIMIT = 10
MAX_RANKING_LIMIT = 50
MAX_DEATH_CONTEXT = 30.0
MAX_REPLAY_WINDOW = 30.0
MIN_REPLAY_RESOLUTION_MS = 100
MAX_MECHANICS = 500
MAX_COMPARISON_FIGHTS = 20
MAX_DEATHS = 500
MAX_DEATH_CONTEXT_EVENTS = 500
MAX_REPLAY_ACTORS = 100
TANK_SPECIALIZATIONS = frozenset({'blood', 'brewmaster', 'guardian', 'protection', 'vengeance'})
REPORT_ID = re.compile(r'^[A-Za-z0-9]{8,32}$')
DRAFT_ID = re.compile(r'^[A-Za-z0-9._-]{1,160}$')


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

    def response(self):
        return {'error': {'code': self.code, 'message': self.message, **self.details}}


class AnalysisService:
    def __init__(self, storage: Path | None = None, live=None):
        self.storage = (Path(storage) if storage else Path(__file__).parent).resolve()
        self.live = live

    def _cache(self, report_id: str) -> Path:
        if not REPORT_ID.fullmatch(str(report_id)):
            raise ServiceError('INVALID_REPORT_ID', 'The report ID is invalid.')
        expected = f'warcraft-logs-{report_id}.wcl.json'.casefold()
        path = next((item for item in self.storage.glob('warcraft-logs-*.wcl.json')
                     if item.name.casefold() == expected), None)
        if path is None or path.resolve().parent != self.storage:
            raise ServiceError('REPORT_NOT_FOUND', 'The requested report is not stored.', 404)
        return path

    def _metadata(self, report_id: str) -> tuple[Path, dict]:
        cache = self._cache(report_id)
        path = metadata_path(cache)
        if not path.is_file():
            raise ServiceError('METADATA_REQUIRED',
                               'Rescan this report once to create lightweight API metadata.', 409)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ServiceError('INVALID_METADATA', 'The report metadata cannot be read.', 500) from error
        return cache, data

    @staticmethod
    def _ref(fight: dict, attempt: int) -> EncounterRef:
        start = float(fight.get('startTime') or 0) / 1000.0
        end = float(fight.get('endTime') or 0) / 1000.0
        return EncounterRef(attempt, int(fight.get('encounterID') or 0),
                            str(fight.get('name') or 'Encounter'),
                            int(fight.get('difficulty') or 0), int(fight.get('size') or 0),
                            start, end, bool(fight.get('kill')),
                            external_id=int(fight.get('id') or 0))

    def _fight(self, report_id: str, fight_id: int) -> tuple[Path, dict, EncounterRef]:
        cache, metadata = self._metadata(report_id)
        for attempt, fight in enumerate(metadata.get('fights', []), 1):
            if int(fight.get('id') or 0) == int(fight_id):
                return cache, metadata, self._ref(fight, attempt)
        raise ServiceError('FIGHT_NOT_FOUND', 'The requested fight is not stored.', 404)

    def _timeline(self, report_id: str, fight_id: int) -> EncounterTimeline:
        cache, _metadata, ref = self._fight(report_id, fight_id)
        timeline = load_saved_analysis(cache, ref)
        if timeline is None:
            raise ServiceError('ANALYSIS_REQUIRED',
                               'Analyze this fight in the desktop application first.', 409)
        return timeline

    @staticmethod
    def _base(report_id, fight_id=None):
        result = {'schema_version': API_SCHEMA_VERSION, 'report_id': report_id}
        if fight_id is not None:
            result['fight_id'] = int(fight_id)
        return result

    def list_reports(self):
        reports = []
        for cache in self.storage.glob('warcraft-logs-*.wcl.json'):
            report_id = cache.name[len('warcraft-logs-'):-len('.wcl.json')]
            meta = metadata_path(cache)
            record = {'report_id': report_id, 'downloaded_at_ns': cache.stat().st_mtime_ns,
                      'metadata_available': meta.is_file()}
            if meta.is_file():
                try:
                    data = json.loads(meta.read_text(encoding='utf-8'))
                    record.update({key: data.get('report', {}).get(key)
                                   for key in ('title', 'startTime', 'endTime')})
                    record['fight_count'] = len(data.get('fights', []))
                except (OSError, ValueError):
                    record['metadata_available'] = False
            reports.append(record)
        reports.sort(
            key=lambda item: (
                int(item.get('endTime') or item.get('startTime') or 0),
                int(item.get('downloaded_at_ns') or 0),
                str(item.get('report_id') or ''),
            ),
            reverse=True,
        )
        return {'schema_version': API_SCHEMA_VERSION, 'reports': reports}

    def list_fights(self):
        """Return every stored fight in encounter-time order, newest first."""
        fights = []
        for report in self.list_reports()['reports']:
            if not report.get('metadata_available'):
                continue
            report_id = str(report.get('report_id') or '')
            try:
                payload = self.list_encounters(report_id)
            except ServiceError:
                continue
            fights.extend(payload.get('fights', []))
        fights.sort(
            key=lambda item: (
                int(item.get('start_time_ms') or 0),
                int(item.get('end_time_ms') or 0),
                str(item.get('report_id') or ''),
                int(item.get('fight_id') or 0),
            ),
            reverse=True,
        )
        return {'schema_version': API_SCHEMA_VERSION, 'fights': fights}

    def get_report(self, report_id):
        _cache, data = self._metadata(report_id)
        return {**self._base(report_id), **data.get('report', {}),
                'fight_count': len(data.get('fights', []))}

    def list_mechanic_drafts(self):
        directory = self.storage / 'mechanic-drafts'
        drafts = []
        if directory.is_dir():
            for path in sorted(directory.glob('*.json'),
                               key=lambda item: item.stat().st_mtime_ns, reverse=True)[:50]:
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                    drafts.append({'draft_id': str(data.get('draft_id') or path.stem),
                                   'generated_at': data.get('generated_at'),
                                   'encounter_id': data.get('encounter_id'),
                                   'encounter_name': data.get('encounter_name'),
                                   'pull_count': data.get('pull_count'),
                                   'accepted_abilities': len(data.get('accepted_abilities', [])),
                                   'status': data.get('status')})
                except (OSError, ValueError, TypeError):
                    continue
        return {'schema_version': API_SCHEMA_VERSION, 'limit': 50,
                'truncated': bool(directory.is_dir() and
                                  sum(1 for _ in directory.glob('*.json')) > 50),
                'drafts': drafts}

    def get_mechanic_draft(self, draft_id):
        if not DRAFT_ID.fullmatch(str(draft_id)):
            raise ServiceError('INVALID_DRAFT_ID', 'The mechanic draft ID is invalid.')
        directory = (self.storage / 'mechanic-drafts').resolve()
        path = (directory / f'{draft_id}.json').resolve()
        if path.parent != directory or not path.is_file():
            raise ServiceError('DRAFT_NOT_FOUND', 'The mechanic timer draft was not found.', 404)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ServiceError('INVALID_DRAFT', 'The mechanic timer draft cannot be read.', 500) from error
        return data

    def get_timer_export(self, draft_id):
        """Return the portable runtime contract for a stored learner draft."""
        return build_timer_export(self.get_mechanic_draft(draft_id))

    def list_encounters(self, report_id):
        _cache, data = self._metadata(report_id)
        report_start = int(data.get('report', {}).get('startTime') or 0)
        fights = []
        for attempt, fight in enumerate(data.get('fights', []), 1):
            ref = self._ref(fight, attempt)
            relative_start = int(fight.get('startTime') or 0)
            relative_end = int(fight.get('endTime') or 0)
            fights.append({'report_id': report_id,
                           'fight_id': ref.external_id, 'attempt': attempt,
                           'encounter_id': ref.encounter_id, 'name': ref.name,
                           'difficulty': ref.difficulty, 'size': ref.group_size,
                           'duration_seconds': ref.duration, 'kill': ref.success,
                           'start_time_ms': report_start + relative_start,
                           'end_time_ms': report_start + relative_end})
        return {**self._base(report_id), 'fights': fights}

    def get_encounter(self, report_id, fight_id):
        _cache, _meta, ref = self._fight(report_id, fight_id)
        return {**self._base(report_id, fight_id), 'attempt': ref.attempt,
                'encounter_id': ref.encounter_id, 'name': ref.name,
                'difficulty': ref.difficulty, 'size': ref.group_size,
                'duration_seconds': ref.duration, 'kill': ref.success}

    def get_fight_summary(self, report_id, fight_id):
        timeline = self._timeline(report_id, fight_id)
        result = self.get_encounter(report_id, fight_id)
        result.update({'players': len(timeline.players), 'deaths': len(timeline.deaths),
                       'total_damage': sum(player.damage for player in timeline.players),
                       'raid_dps': sum(player.dps for player in timeline.players),
                       'total_effective_healing': sum(player.healing for player in timeline.players),
                       'raid_hps': sum(player.hps for player in timeline.players),
                       'mechanics': len(timeline.mechanics),
                       'bosses': [asdict(boss) for boss in timeline.bosses]})
        return result

    def find_players(self, report_id, fight_id, query):
        timeline = self._timeline(report_id, fight_id)
        folded = str(query).casefold()
        matches = [player for player in timeline.players if folded in player.name.casefold()]
        return {**self._base(report_id, fight_id), 'players': [asdict(p) for p in matches]}

    def _player(self, timeline, player_id=None, player_name=None):
        if player_id:
            matches = [player for player in timeline.players if player.guid == str(player_id)]
        elif player_name:
            folded = str(player_name).casefold()
            exact = [player for player in timeline.players if player.name.casefold() == folded]
            matches = exact or [player for player in timeline.players if folded in player.name.casefold()]
        else:
            raise ServiceError('PLAYER_REQUIRED', 'A player ID or name is required.')
        if not matches:
            raise ServiceError('PLAYER_NOT_FOUND', 'No matching player was found.', 404)
        if len(matches) > 1:
            raise ServiceError('AMBIGUOUS_PLAYER', 'More than one player matches.',
                               candidates=[{'player_id': p.guid, 'name': p.name} for p in matches])
        return matches[0]

    def get_player_performance(self, report_id, fight_id, player_id=None, player_name=None):
        timeline = self._timeline(report_id, fight_id)
        player = self._player(timeline, player_id, player_name)
        return {**self._base(report_id, fight_id), 'player': asdict(player)}

    def _ranking(self, report_id, fight_id, metric, limit):
        timeline = self._timeline(report_id, fight_id)
        applied = min(max(1, int(limit)), MAX_RANKING_LIMIT)
        ordered = sorted(timeline.players, key=lambda p: (-getattr(p, metric), p.name.casefold()))
        return {**self._base(report_id, fight_id), 'metric': metric,
                'limit': applied, 'truncated': len(ordered) > applied,
                'players': [asdict(player) for player in ordered[:applied]]}

    def get_damage_ranking(self, report_id, fight_id, limit=DEFAULT_RANKING_LIMIT):
        return self._ranking(report_id, fight_id, 'damage', limit)

    def get_healing_ranking(self, report_id, fight_id, limit=DEFAULT_RANKING_LIMIT):
        return self._ranking(report_id, fight_id, 'healing', limit)

    def get_dps_ranking(self, report_id, fight_id, limit=DEFAULT_RANKING_LIMIT):
        return self._ranking(report_id, fight_id, 'dps', limit)

    def get_hps_ranking(self, report_id, fight_id, limit=DEFAULT_RANKING_LIMIT):
        return self._ranking(report_id, fight_id, 'hps', limit)

    def get_mechanics(self, report_id, fight_id, start_seconds=None, end_seconds=None,
                      ability_id=None):
        timeline = self._timeline(report_id, fight_id)
        start = 0.0 if start_seconds is None else max(0.0, float(start_seconds))
        end = float('inf') if end_seconds is None else max(start, float(end_seconds))
        items = [m for m in timeline.mechanics if start <= m.start <= end
                 and (ability_id is None or m.spell_id == int(ability_id))]
        truncated = len(items) > MAX_MECHANICS
        return {**self._base(report_id, fight_id), 'start_seconds': start,
                'end_seconds': None if end == float('inf') else end,
                'truncated': truncated,
                'mechanics': [asdict(item) for item in items[:MAX_MECHANICS]]}

    def compare_mechanics(self, report_id, fight_ids, ability_id=None):
        selected = self._fight_ids(fight_ids)
        comparisons = [self.get_mechanics(report_id, fight_id, ability_id=ability_id)
                       for fight_id in selected]
        return {**self._base(report_id), 'fight_ids': selected,
                'limit': MAX_COMPARISON_FIGHTS, 'truncated': len(fight_ids) > len(selected),
                'comparisons': comparisons}

    @staticmethod
    def _fight_ids(fight_ids):
        if not isinstance(fight_ids, list) or not fight_ids:
            raise ServiceError('FIGHT_IDS_REQUIRED', 'fight_ids must be a non-empty JSON array.')
        try:
            return [int(item) for item in fight_ids[:MAX_COMPARISON_FIGHTS]]
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_FIGHT_ID', 'Every fight ID must be an integer.') from error

    def list_deaths(self, report_id, fight_id, player_id=None, player_name=None):
        timeline = self._timeline(report_id, fight_id)
        return self._death_list_payload(
            timeline, {**self._base(report_id, fight_id),
                       'units': {'time': 'seconds'}}, player_id, player_name)

    def _death_list_payload(self, timeline, base, player_id=None, player_name=None):
        deaths = timeline.deaths
        if player_id or player_name:
            player = self._player(timeline, player_id, player_name)
            deaths = [death for death in deaths if death.player_guid == player.guid]
        deaths = sorted(deaths, key=lambda death: (death.time, death.death_id))
        summaries = []
        for death in deaths[:MAX_DEATHS]:
            primary = dict(death.primary_cause) if death.primary_cause else None
            if primary:
                primary.pop('contributing_events', None)
            summaries.append({
                'death_id': death.death_id, 'player_id': death.player_guid,
                'player_name': death.player_name, 'class': death.class_name,
                'death_number': death.death_number, 'time_seconds': death.time,
                'killing_blow': death.killing_blow, 'primary_cause': primary,
            })
        return {**base, 'limit': MAX_DEATHS, 'truncated': len(deaths) > MAX_DEATHS,
                'deaths': summaries}

    def _death(self, timeline, death_id=None, player_id=None, player_name=None, death_number=None):
        if death_id:
            matches = [death for death in timeline.deaths if death.death_id == death_id]
        else:
            player = self._player(timeline, player_id, player_name)
            matches = [death for death in timeline.deaths if death.player_guid == player.guid]
            if death_number is not None:
                matches = [death for death in matches if death.death_number == int(death_number)]
        if not matches:
            raise ServiceError('DEATH_NOT_FOUND', 'No matching death was found.', 404)
        if len(matches) > 1:
            raise ServiceError('AMBIGUOUS_DEATH', 'Specify a death ID or death number.',
                               candidates=[{'death_id': d.death_id, 'time': d.time} for d in matches])
        return matches[0]

    def get_death_analysis(self, report_id, fight_id, death_id=None, player_id=None,
                           player_name=None, death_number=None, seconds_before=10):
        timeline = self._timeline(report_id, fight_id)
        death = self._death(timeline, death_id, player_id, player_name, death_number)
        return self._death_analysis_payload(death, self._base(report_id, fight_id), seconds_before)

    @staticmethod
    def _death_analysis_payload(death, base, seconds_before):
        window = min(max(0.0, float(seconds_before)), MAX_DEATH_CONTEXT)
        result = asdict(death)
        damage = [event for event in result['damage_before_death']
                  if death.time - event['time'] <= window]
        healing = [event for event in result['healing_before_death']
                   if death.time - event['time'] <= window]
        result['damage_before_death'] = damage[-MAX_DEATH_CONTEXT_EVENTS:]
        result['healing_before_death'] = healing[-MAX_DEATH_CONTEXT_EVENTS:]
        if result.get('primary_cause'):
            contributing = result['primary_cause'].get('contributing_events', [])
            result['primary_cause']['contributing_events'] = contributing[-100:]
        result['player'] = {'id': result.pop('player_guid'),
                            'name': result.pop('player_name'),
                            'class': result.pop('class_name')}
        result['time_seconds'] = result.pop('time')
        return {**base, 'units': {**base.get('units', {}), 'time': 'seconds'},
                'seconds_before': window, 'event_limit': MAX_DEATH_CONTEXT_EVENTS,
                'truncated': {'damage': len(damage) > MAX_DEATH_CONTEXT_EVENTS,
                              'healing': len(healing) > MAX_DEATH_CONTEXT_EVENTS}, **result}

    @staticmethod
    def _replay_positions(timeline, selected_guid, start, end, resolution):
        history = timeline.position_history
        if not history:
            history = {}
            for row in timeline.rows:
                for guid, position in {**row.player_positions, **row.boss_positions}.items():
                    history.setdefault(guid, []).append({
                        'time': float(row.second), 'x': position.x, 'y': position.y,
                        'map_id': position.map_id})
        allowed = sorted(history, key=lambda guid: (guid != selected_guid, guid))[
            :MAX_REPLAY_ACTORS]
        indices = {guid: -1 for guid in allowed}
        actors = {guid: [] for guid in allowed}
        moment = start
        step = resolution / 1000.0
        while moment <= end + 1e-9:
            for guid in allowed:
                samples = history[guid]
                index = indices[guid]
                while index + 1 < len(samples) and samples[index + 1]['time'] <= moment + 1e-9:
                    index += 1
                indices[guid] = index
                if index < 0:
                    continue
                sample = samples[index]
                age = max(0.0, moment - sample['time'])
                actors[guid].append({'t': round(moment, 3), 'x': sample['x'],
                                     'y': sample['y'], 'map_id': sample['map_id'],
                                     'sample_age_seconds': age,
                                     'position_provenance': ('observed' if age < 1e-6
                                                             else 'carried_forward')})
            moment += step
        return {guid: points for guid, points in actors.items() if points}, len(history) > len(allowed)

    def get_death_replay(self, report_id, fight_id, death_id, seconds_before=8,
                         seconds_after=2, resolution_ms=500):
        timeline = self._timeline(report_id, fight_id)
        death = self._death(timeline, death_id=death_id)
        return self._death_replay_payload(timeline, death, self._base(report_id, fight_id),
                                          seconds_before, seconds_after, resolution_ms)

    def _death_replay_payload(self, timeline, death, base, seconds_before,
                              seconds_after, resolution_ms):
        before, after = max(0.0, float(seconds_before)), max(0.0, float(seconds_after))
        if before + after > MAX_REPLAY_WINDOW:
            scale = MAX_REPLAY_WINDOW / (before + after)
            before, after = before * scale, after * scale
        resolution = max(MIN_REPLAY_RESOLUTION_MS, int(resolution_ms))
        start, end = max(0.0, death.time - before), min(
            timeline.encounter.duration or death.time + after, death.time + after)
        actors, actors_truncated = self._replay_positions(
            timeline, death.player_guid, start, end, resolution)
        events = [{'t': m.start, 'type': 'mechanic_start', 'ability': m.ability,
                   'ability_id': m.spell_id} for m in timeline.mechanics if start <= m.start <= end]
        events.append({'t': death.time, 'type': 'death', 'player': death.player_name,
                       'death_id': death.death_id})
        events.sort(key=lambda item: item['t'])
        return {**base, 'death_id': death.death_id,
                'start': start, 'end': end, 'resolution_ms': resolution,
                'actor_limit': MAX_REPLAY_ACTORS,
                'truncated': actors_truncated,
                'actors': actors, 'events': events}

    def get_death_answer(self, report_id, fight_id, death_id, seconds_before=10):
        timeline = self._timeline(report_id, fight_id)
        tank_guids = self._stored_tank_guids(report_id, fight_id)
        return self._death_answer_payload(
            timeline, self._base(report_id, fight_id), death_id, seconds_before, tank_guids,
        )

    def get_live_death_answer(self, death_id, seconds_before=10):
        timeline, status = self._live_snapshot()
        return self._death_answer_payload(timeline, self._live_base(timeline, status),
                                          death_id, seconds_before)

    @staticmethod
    def _focused_map_roles(timeline, victim_guid, tank_guids=()):
        roles = {str(victim_guid): 'player'}
        for guid in tank_guids:
            roles.setdefault(str(guid), 'tank')
        # A multi-target encounter can expose many hostile units. Keep the primary
        # parsed boss only so the positional diagram stays readable.
        if timeline.boss_profiles:
            encounter_name = str(timeline.encounter.name or '').casefold()
            primary = next(
                (boss for boss in timeline.boss_profiles
                 if str(boss.name or '').casefold() == encounter_name),
                timeline.boss_profiles[0],
            )
            roles.setdefault(str(primary.guid), 'boss')
        return roles

    def _stored_tank_guids(self, report_id, fight_id):
        _cache, metadata, _ref = self._fight(report_id, fight_id)
        fight = next(
            (item for item in metadata.get('fights', [])
             if int(item.get('id') or 0) == int(fight_id)),
            {},
        )
        players = fight.get('friendlyPlayers') if isinstance(fight, dict) else []
        specs = fight.get('friendlySpecs') if isinstance(fight, dict) else []
        if not isinstance(players, list) or not isinstance(specs, list):
            return []
        return [
            f'WCL-{int(player_id)}'
            for player_id, spec in zip(players, specs)
            if str(spec or '').casefold() in TANK_SPECIALIZATIONS
        ][:2]

    def _death_answer_payload(self, timeline, base, death_id, seconds_before,
                              tank_guids=()):
        # Capture one timeline so live analysis, replay, and image agree.
        death = self._death(timeline, death_id=death_id)
        analysis = self._death_analysis_payload(death, base, seconds_before)
        replay = self._death_replay_payload(timeline, death, base,
                                             analysis['seconds_before'], 0, 100)
        names = {p.guid: p.name for p in timeline.players}
        names.update({p.guid: p.name for p in timeline.boss_profiles})
        roles = self._focused_map_roles(timeline, analysis['player']['id'], tank_guids)
        return {**base, **build_death_answer(analysis, replay, names, roles)}

    def compare_player_deaths(self, report_id, fight_ids, player_id=None, player_name=None):
        selected = self._fight_ids(fight_ids)
        comparisons = []
        for fight_id in selected:
            result = self.list_deaths(report_id, fight_id, player_id, player_name)
            comparisons.append({'fight_id': int(fight_id), 'deaths': result['deaths']})
        return {**self._base(report_id), 'limit': MAX_COMPARISON_FIGHTS,
                'truncated': len(fight_ids) > len(selected), 'comparisons': comparisons}

    def _live_snapshot(self):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        timeline, status = self.live.snapshot()
        if timeline is None:
            if not status['available']:
                raise ServiceError('LIVE_SOURCE_UNAVAILABLE',
                                   'The configured live combat log is unavailable.', 503)
            raise ServiceError('NO_ACTIVE_ENCOUNTER', 'No live encounter is available.', 404)
        return timeline, status

    @staticmethod
    def _live_base(timeline, status):
        encounter = timeline.encounter
        return {
            'schema_version': API_SCHEMA_VERSION,
            'source_type': status.get('source_type', 'local_live_log'),
            'session_id': status['session_id'], 'encounter_id': encounter.encounter_id,
            'attempt': encounter.attempt, 'encounter_state': status['encounter_state'],
            'cursor': status['cursor'], 'generated_at': status['generated_at'],
            'fight_time_seconds': status['fight_time_seconds'],
            'units': {'time': 'seconds', 'distance': 'log_coordinate_units'},
            'complete': status['complete'], 'stale': status['stale'],
        }

    def get_live_status(self):
        if self.live is None:
            return {'schema_version': API_SCHEMA_VERSION, 'source_type': 'local_live_log',
                    'configured': False, 'encounter_state': 'error',
                    'error_code': 'LIVE_SOURCE_NOT_CONFIGURED'}
        return self.live.status()

    def get_live_timer_module(self):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        return {**self.live.timer_module_status(), 'schema_version': API_SCHEMA_VERSION,
                'source_type': 'local_live_log'}

    def arm_live_timer_module(self, timer_id=None, encounter_id=None, difficulty=None):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        if not timer_id and encounter_id is None:
            raise ServiceError('INVALID_PARAMETER',
                               'timer_id or encounter_id is required.')
        try:
            result = self.live.arm_timer_module(timer_id, encounter_id, difficulty)
        except (TypeError, ValueError) as error:
            raise ServiceError('TIMER_MODULE_NOT_FOUND', str(error), 404) from error
        return {**result, 'schema_version': API_SCHEMA_VERSION,
                'source_type': 'local_live_log'}

    def disarm_live_timer_module(self):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        return {**self.live.disarm_timer_module(), 'schema_version': API_SCHEMA_VERSION,
                'source_type': 'local_live_log'}

    def get_live_timeline(self):
        timeline, status = self._live_snapshot()
        scheduled = self.live.timeline_snapshot()
        events = scheduled.pop('events', [])
        return {**self._live_base(timeline, status), **scheduled,
                'event_limit': 500, 'truncated': len(events) > 500,
                'events': events[-500:]}

    def get_live_mechanic_events(self, status_filter=None, limit=100):
        timeline, status = self._live_snapshot()
        try:
            applied = min(max(1, int(limit)), 500)
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_PARAMETER', 'limit must be an integer.') from error
        events = [event for event in self.live.timeline_snapshot()['events']
                  if event.get('type') == 'MECHANIC_EVENT']
        if status_filter:
            wanted = str(status_filter).upper()
            if wanted not in ('TRIGGERED', 'CANCELED'):
                raise ServiceError('INVALID_PARAMETER',
                                   'status must be TRIGGERED or CANCELED.')
            events = [event for event in events if event.get('status') == wanted]
        return {**self._live_base(timeline, status), 'limit': applied,
                'truncated': len(events) > applied, 'events': events[-applied:]}

    def get_live_encounter(self):
        timeline, status = self._live_snapshot()
        ref = timeline.encounter
        return {**self._live_base(timeline, status), 'name': ref.name,
                'difficulty': ref.difficulty, 'size': ref.group_size,
                'kill': ref.success if ref.end is not None else None}

    def get_live_fight_summary(self):
        timeline, status = self._live_snapshot()
        return {**self._live_base(timeline, status), 'name': timeline.encounter.name,
                'players': len(timeline.players), 'deaths': len(timeline.deaths),
                'total_damage': sum(player.damage for player in timeline.players),
                'raid_dps': sum(player.dps for player in timeline.players),
                'total_effective_healing': sum(player.healing for player in timeline.players),
                'raid_hps': sum(player.hps for player in timeline.players),
                'mechanics': len(timeline.mechanics),
                'bosses': [asdict(boss) for boss in timeline.bosses]}

    def list_live_completed_encounters(self):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        status = self.live.status()
        encounters = []
        for timeline in self.live.completed():
            ref = timeline.encounter
            encounters.append({'attempt': ref.attempt, 'encounter_id': ref.encounter_id,
                               'name': ref.name, 'duration_seconds': ref.duration,
                               'kill': ref.success, 'deaths': len(timeline.deaths)})
        return {**status, 'encounters': sorted(encounters, key=lambda item: item['attempt'])}

    def get_live_completed_summary(self, attempt):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        timeline = next((item for item in self.live.completed()
                         if item.encounter.attempt == int(attempt)), None)
        if timeline is None:
            raise ServiceError('FIGHT_NOT_FOUND', 'The completed live encounter was not found.', 404)
        ref = timeline.encounter
        return {'schema_version': API_SCHEMA_VERSION, 'source_type': 'local_live_log',
                'session_id': self.live.session_id, 'encounter_id': ref.encounter_id,
                'attempt': ref.attempt, 'encounter_state': 'completed', 'complete': True,
                'name': ref.name, 'duration_seconds': ref.duration, 'kill': ref.success,
                'players': len(timeline.players), 'deaths': len(timeline.deaths),
                'mechanics': len(timeline.mechanics),
                'total_damage': sum(player.damage for player in timeline.players),
                'total_effective_healing': sum(player.healing for player in timeline.players)}

    def list_live_attempts(self):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        attempts = []
        for attempt, record in self.live.archived().items():
            timeline = record['timeline']
            attempts.append({'attempt': attempt, 'encounter_id': timeline.encounter.encounter_id,
                             'name': timeline.encounter.name, 'state': record['state'],
                             'result': (record.get('scheduled_timeline') or {}).get('result'),
                             'duration_seconds': timeline.encounter.duration})
        return {**self.live.status(),
                'attempts': sorted(attempts, key=lambda item: item['attempt'])}

    def _live_attempt_snapshot(self, attempt):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        try:
            selected = int(attempt)
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_PARAMETER', 'attempt must be an integer.') from error
        record = self.live.archived().get(selected)
        if record is None:
            raise ServiceError('FIGHT_NOT_FOUND', 'The live attempt was not found.', 404)
        return record['timeline'], record, selected

    @staticmethod
    def _live_attempt_base(timeline, session_id, attempt, state):
        ref = timeline.encounter
        return {
            'schema_version': API_SCHEMA_VERSION, 'source_type': 'local_live_log',
            'session_id': session_id, 'encounter_id': ref.encounter_id,
            'attempt': attempt, 'encounter_state': state,
            'fight_time_seconds': ref.duration, 'complete': state == 'completed',
            'units': {'time': 'seconds', 'distance': 'log_coordinate_units'},
        }

    def get_live_attempt_timeline(self, attempt):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        scheduled = record.get('scheduled_timeline') or {}
        events = scheduled.get('events', [])
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        return {**base, 'script_id': scheduled.get('script_id'),
                'placeholder': scheduled.get('placeholder'), 'result': scheduled.get('result'),
                'event_limit': 500, 'truncated': len(events) > 500,
                'events': events[-500:]}

    def get_live_attempt_summary(self, attempt):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        ref = timeline.encounter
        return {**base, 'name': ref.name, 'difficulty': ref.difficulty,
                'size': ref.group_size, 'duration_seconds': ref.duration,
                'kill': ref.success if ref.end is not None else None,
                'players': len(timeline.players), 'deaths': len(timeline.deaths),
                'total_damage': sum(player.damage for player in timeline.players),
                'raid_dps': sum(player.dps for player in timeline.players),
                'total_effective_healing': sum(player.healing for player in timeline.players),
                'raid_hps': sum(player.hps for player in timeline.players),
                'mechanics': len(timeline.mechanics),
                'bosses': [asdict(boss) for boss in timeline.bosses]}

    def find_live_attempt_players(self, attempt, query=''):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        folded = str(query).casefold()
        players = sorted((player for player in timeline.players
                          if folded in player.name.casefold()),
                         key=lambda player: (player.name.casefold(), player.guid))
        return {**base, 'players': [asdict(player) for player in players]}

    def get_live_attempt_player_performance(self, attempt, player_id=None,
                                            player_name=None):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        player = self._player(timeline, player_id, player_name)
        return {**base, 'player': asdict(player)}

    def _live_attempt_ranking(self, attempt, metric, limit):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        try:
            applied = min(max(1, int(limit)), MAX_RANKING_LIMIT)
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_PARAMETER', 'limit must be an integer.') from error
        players = sorted(timeline.players,
                         key=lambda player: (-getattr(player, metric),
                                             player.name.casefold()))
        return {**base, 'metric': metric, 'limit': applied,
                'truncated': len(players) > applied,
                'players': [asdict(player) for player in players[:applied]]}

    def get_live_attempt_damage_ranking(self, attempt, limit=DEFAULT_RANKING_LIMIT):
        return self._live_attempt_ranking(attempt, 'damage', limit)

    def get_live_attempt_healing_ranking(self, attempt, limit=DEFAULT_RANKING_LIMIT):
        return self._live_attempt_ranking(attempt, 'healing', limit)

    def get_live_attempt_dps_ranking(self, attempt, limit=DEFAULT_RANKING_LIMIT):
        return self._live_attempt_ranking(attempt, 'dps', limit)

    def get_live_attempt_hps_ranking(self, attempt, limit=DEFAULT_RANKING_LIMIT):
        return self._live_attempt_ranking(attempt, 'hps', limit)

    def list_live_attempt_deaths(self, attempt, player_id=None, player_name=None):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        return self._death_list_payload(timeline, base, player_id, player_name)

    def get_live_attempt_death_analysis(self, attempt, death_id=None, player_id=None,
                                        player_name=None, death_number=None,
                                        seconds_before=10):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        death = self._death(timeline, death_id, player_id, player_name, death_number)
        return self._death_analysis_payload(death, base, seconds_before)

    def get_live_attempt_death_answer(self, attempt, death_id, seconds_before=10):
        timeline, record, selected = self._live_attempt_snapshot(attempt)
        base = self._live_attempt_base(
            timeline, self.live.session_id, selected, record['state'])
        return self._death_answer_payload(timeline, base, death_id, seconds_before)

    def find_live_players(self, query=''):
        timeline, status = self._live_snapshot()
        folded = str(query).casefold()
        players = sorted((p for p in timeline.players if folded in p.name.casefold()),
                         key=lambda p: (p.name.casefold(), p.guid))
        return {**self._live_base(timeline, status),
                'players': [asdict(player) for player in players]}

    def get_live_player_performance(self, player_id=None, player_name=None):
        timeline, status = self._live_snapshot()
        player = self._player(timeline, player_id, player_name)
        return {**self._live_base(timeline, status), 'player': asdict(player)}

    def _live_ranking(self, metric, limit):
        timeline, status = self._live_snapshot()
        applied = min(max(1, int(limit)), MAX_RANKING_LIMIT)
        players = sorted(timeline.players,
                         key=lambda player: (-getattr(player, metric), player.name.casefold()))
        return {**self._live_base(timeline, status), 'metric': metric, 'limit': applied,
                'truncated': len(players) > applied,
                'players': [asdict(player) for player in players[:applied]]}

    def get_live_damage_ranking(self, limit=DEFAULT_RANKING_LIMIT):
        return self._live_ranking('damage', limit)

    def get_live_healing_ranking(self, limit=DEFAULT_RANKING_LIMIT):
        return self._live_ranking('healing', limit)

    def get_live_dps_ranking(self, limit=DEFAULT_RANKING_LIMIT):
        return self._live_ranking('dps', limit)

    def get_live_hps_ranking(self, limit=DEFAULT_RANKING_LIMIT):
        return self._live_ranking('hps', limit)

    def get_live_mechanics(self, start_seconds=None, end_seconds=None, ability_id=None):
        timeline, status = self._live_snapshot()
        start = 0.0 if start_seconds is None else max(0.0, float(start_seconds))
        end = float('inf') if end_seconds is None else max(start, float(end_seconds))
        items = [item for item in timeline.mechanics if start <= item.start <= end and
                 (ability_id is None or item.spell_id == int(ability_id))]
        return {**self._live_base(timeline, status), 'start_seconds': start,
                'end_seconds': None if end == float('inf') else end,
                'limit': MAX_MECHANICS, 'truncated': len(items) > MAX_MECHANICS,
                'mechanics': [asdict(item) for item in items[:MAX_MECHANICS]]}

    def list_live_deaths(self, player_id=None, player_name=None):
        timeline, status = self._live_snapshot()
        deaths = timeline.deaths
        if player_id or player_name:
            player = self._player(timeline, player_id, player_name)
            deaths = [death for death in deaths if death.player_guid == player.guid]
        summaries = []
        for death in sorted(deaths, key=lambda d: (d.time, d.death_id))[:MAX_DEATHS]:
            primary = dict(death.primary_cause) if death.primary_cause else None
            if primary:
                primary.pop('contributing_events', None)
            summaries.append({'death_id': death.death_id, 'player_id': death.player_guid,
                              'player_name': death.player_name, 'class': death.class_name,
                              'death_number': death.death_number, 'time_seconds': death.time,
                              'killing_blow': death.killing_blow, 'primary_cause': primary})
        return {**self._live_base(timeline, status), 'limit': MAX_DEATHS,
                'truncated': len(deaths) > MAX_DEATHS, 'deaths': summaries}

    def get_live_death_analysis(self, death_id=None, player_id=None, player_name=None,
                                death_number=None, seconds_before=10):
        timeline, status = self._live_snapshot()
        death = self._death(timeline, death_id, player_id, player_name, death_number)
        window = min(max(0.0, float(seconds_before)), MAX_DEATH_CONTEXT)
        result = asdict(death)
        damage = [event for event in result['damage_before_death']
                  if death.time - event['time'] <= window]
        healing = [event for event in result['healing_before_death']
                   if death.time - event['time'] <= window]
        result['damage_before_death'] = damage[-MAX_DEATH_CONTEXT_EVENTS:]
        result['healing_before_death'] = healing[-MAX_DEATH_CONTEXT_EVENTS:]
        result['player'] = {'id': result.pop('player_guid'),
                            'name': result.pop('player_name'),
                            'class': result.pop('class_name')}
        result['time_seconds'] = result.pop('time')
        return {**self._live_base(timeline, status), 'seconds_before': window,
                'event_limit': MAX_DEATH_CONTEXT_EVENTS,
                'truncated': {'damage': len(damage) > MAX_DEATH_CONTEXT_EVENTS,
                              'healing': len(healing) > MAX_DEATH_CONTEXT_EVENTS}, **result}

    def get_live_death_replay(self, death_id, seconds_before=5, seconds_after=0,
                              resolution_ms=100):
        timeline, status = self._live_snapshot()
        death = self._death(timeline, death_id=death_id)
        before, after = max(0.0, float(seconds_before)), max(0.0, float(seconds_after))
        if before + after > MAX_REPLAY_WINDOW:
            scale = MAX_REPLAY_WINDOW / (before + after)
            before, after = before * scale, after * scale
        resolution = max(MIN_REPLAY_RESOLUTION_MS, int(resolution_ms))
        start = max(0.0, death.time - before)
        end = min(timeline.encounter.duration or death.time + after, death.time + after)
        actors, actors_truncated = self._replay_positions(
            timeline, death.player_guid, start, end, resolution)
        events = [{'t': m.start, 'type': 'mechanic_start', 'ability': m.ability,
                   'ability_id': m.spell_id} for m in timeline.mechanics
                  if start <= m.start <= end]
        events.append({'t': death.time, 'type': 'death', 'player': death.player_name,
                       'death_id': death.death_id})
        return {**self._live_base(timeline, status), 'death_id': death.death_id,
                'start': start, 'end': end, 'resolution_ms': resolution,
                'actor_limit': MAX_REPLAY_ACTORS,
                'truncated': actors_truncated,
                'actors': actors,
                'events': sorted(events, key=lambda item: item['t'])}

    def get_live_updates(self, since_cursor=0, limit=100):
        if self.live is None or not self.live.configured:
            raise ServiceError('LIVE_SOURCE_NOT_CONFIGURED',
                               'No live combat log was configured.', 409)
        try:
            since = max(0, int(since_cursor))
            applied = min(max(1, int(limit)), 500)
        except (TypeError, ValueError) as error:
            raise ServiceError('INVALID_PARAMETER', 'Cursor and limit must be integers.') from error
        updates, oldest = self.live.updates(since, applied)
        if updates is None:
            raise ServiceError('CURSOR_EXPIRED', 'The requested cursor is no longer retained.',
                               409, oldest_cursor=oldest)
        status = self.live.status()
        return {**status, 'limit': applied, 'since_cursor': since,
                'truncated': len(updates) == applied, 'updates': updates}
