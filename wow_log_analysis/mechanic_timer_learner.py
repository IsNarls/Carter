"""Learn conservative draft mechanic timers from repeated Warcraft Logs pulls."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import itertools
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

from timer_export import build_timer_export, save_timer_export
from warcraft_logs_importer import load_cache


LEARNER_SCHEMA_VERSION = 2
SAFE_ID = re.compile(r'[^A-Za-z0-9._-]+')
MAX_ANALYSIS_WORKERS = 5


@dataclass
class LearnerConfig:
    minimum_pulls: int = 3
    minimum_pull_coverage: float = 0.80
    minimum_target_fraction: float = 0.60
    minimum_health_fraction: float = 0.15
    maximum_timing_deviation: float = 2.0
    maximum_interval_cv: float = 0.10
    burst_gap_seconds: float = 2.0
    warning_lead_seconds: float = 5.0
    minimum_actionable_warning_seconds: float = 2.0
    overlap_seconds: float = 2.0
    maximum_fight_seconds: float = 900.0

    def validate(self):
        if self.minimum_pulls < 3:
            raise ValueError('minimum_pulls must be at least 3 for conservative learning.')
        for name in ('minimum_pull_coverage', 'minimum_target_fraction',
                     'minimum_health_fraction', 'maximum_interval_cv'):
            value = float(getattr(self, name))
            if not 0 <= value <= 1:
                raise ValueError(f'{name} must be between 0 and 1.')
        if min(self.maximum_timing_deviation, self.burst_gap_seconds,
               self.warning_lead_seconds, self.minimum_actionable_warning_seconds,
               self.overlap_seconds) < 0:
            raise ValueError('Time thresholds cannot be negative.')


def percentile(values, fraction):
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lower, upper = math.floor(index), math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - index) + ordered[upper] * (index - lower)


def robust_timing(values):
    median = statistics.median(values)
    deviations = [abs(value - median) for value in values]
    return {
        'median': median,
        'minimum': min(values), 'maximum': max(values),
        'median_absolute_deviation': statistics.median(deviations),
        'p90_absolute_deviation': percentile(deviations, .90),
    }


def _burst_events(events, gap):
    bursts = []
    current = []
    for event in sorted(events, key=lambda item: item['time']):
        if current and event['time'] - current[-1]['time'] > gap:
            bursts.append(current)
            current = []
        current.append(event)
    if current:
        bursts.append(current)
    return bursts


def _summarize_report(path, encounter_id, difficulty, selected_fights, config):
    payload = load_cache(path)
    report = payload['report']
    master = report.get('masterData') or {}
    actors = {int(actor.get('id') or 0): actor for actor in master.get('actors', [])}
    abilities = {int(item.get('gameID') or 0): str(item.get('name') or '')
                 for item in master.get('abilities', [])}
    player_ids = {actor_id for actor_id, actor in actors.items()
                  if actor.get('type') == 'Player'}
    boss_ids = {actor_id for actor_id, actor in actors.items()
                if str(actor.get('subType') or '').casefold() == 'boss'}
    fights = [fight for fight in report.get('fights', [])
              if int(fight.get('encounterID') or 0) == int(encounter_id)
              and (difficulty is None or int(fight.get('difficulty') or 0) == int(difficulty))
              and (not selected_fights or int(fight.get('id') or 0) in selected_fights)]
    summaries = []
    for fight in fights:
        fight_id = int(fight.get('id') or 0)
        has_start = fight.get('startTime') is not None
        has_end = fight.get('endTime') is not None
        start = float(fight.get('startTime') or 0)
        end = float(fight.get('endTime') if has_end else start)
        duration = max(0.0, (end - start) / 1000)
        group_size = max(1, int(fight.get('size') or len(fight.get('friendlyPlayers') or []) or 1))
        by_ability = {}
        casts = []
        for event in payload.get('eventsByFight', {}).get(str(fight_id), []):
            timestamp = float(event.get('timestamp') or start)
            elapsed = max(0.0, (timestamp - start) / 1000)
            if elapsed > config.maximum_fight_seconds:
                continue
            kind = str(event.get('type') or '').casefold()
            source_id = int(event.get('sourceID') or 0)
            target_id = int(event.get('targetID') or 0)
            ability_id = int(event.get('abilityGameID', event.get('abilityID')) or 0)
            if source_id in boss_ids and kind in ('begincast', 'cast'):
                casts.append({'time': elapsed, 'ability_id': ability_id,
                              'ability': abilities.get(ability_id, f'Spell {ability_id}'),
                              'type': kind})
            if (kind != 'damage' or source_id not in boss_ids or target_id not in player_ids or
                    ability_id <= 0):
                continue
            amount = max(0, int(event.get('amount') or 0) - max(0, int(event.get('overkill') or 0)))
            resources = event.get('targetResources') if isinstance(
                event.get('targetResources'), dict) else {}
            if not resources and int(event.get('resourceActor') or 0) == 2:
                resources = event
            maximum = int(resources.get('maxHitPoints') or 0)
            by_ability.setdefault(ability_id, []).append({
                'time': elapsed, 'target_id': target_id, 'amount': amount,
                'max_health': maximum,
            })
        abilities_in_fight = {}
        for ability_id, events in by_ability.items():
            bursts = []
            for cluster in _burst_events(events, config.burst_gap_seconds):
                by_target = {}
                health = {}
                for item in cluster:
                    by_target[item['target_id']] = by_target.get(item['target_id'], 0) + item['amount']
                    if item['max_health'] > 0:
                        health[item['target_id']] = max(health.get(item['target_id'], 0),
                                                        item['max_health'])
                fractions = [damage / health[target] for target, damage in by_target.items()
                             if health.get(target)]
                burst_start = min(item['time'] for item in cluster)
                preceding = [cast for cast in casts if 0 <= burst_start - cast['time'] <= 10]
                cast = min(preceding, key=lambda item: burst_start - item['time']) if preceding else None
                bursts.append({
                    'time': burst_start, 'targets': len(by_target),
                    'target_fraction': len(by_target) / group_size,
                    'total_damage': sum(by_target.values()),
                    'median_target_health_fraction': (statistics.median(fractions)
                                                      if fractions else None),
                    'preceding_cast': cast,
                })
            abilities_in_fight[ability_id] = bursts
        summaries.append({
            'report_id': str(report.get('code') or path.stem), 'fight_id': fight_id,
            'name': str(fight.get('name') or 'Encounter'), 'duration': duration,
            'kill': bool(fight.get('kill')), 'group_size': group_size,
            'start_time_ms': start if has_start else None,
            'end_time_ms': end if has_end else None,
            'abilities': abilities_in_fight, 'ability_names': abilities,
        })
    return summaries


def _summarize_report_job(index, path, encounter_id, difficulty, selected_fights, config):
    """Pickle-safe process worker for one complete report cache."""
    summaries = _summarize_report(
        Path(path), encounter_id, difficulty, selected_fights, config)
    return index, Path(path).name, summaries


def _collect_report_summaries(paths, encounter_id, difficulty, selected_fights, config,
                              max_workers, progress):
    paths = [Path(path) for path in paths]
    if not paths:
        raise ValueError('At least one Warcraft Logs report is required.')
    normalized = [str(path.resolve()).casefold() for path in paths]
    if len(normalized) != len(set(normalized)):
        raise ValueError('Each Warcraft Logs report may be included only once.')
    progress = progress or (lambda _message: None)
    worker_count = min(len(paths), MAX_ANALYSIS_WORKERS, max(1, int(max_workers)))
    if len(paths) == 1:
        progress(f'Interpreting {paths[0].name}...')
        summaries = _summarize_report(
            paths[0], encounter_id, difficulty, selected_fights, config)
        progress(f'Interpreted {paths[0].name}: {len(summaries)} matching pulls.')
        return summaries, 1, 'single_process'

    progress(f'Interpreting {len(paths)} reports with {worker_count} worker processes...')
    def execute(executor):
        ordered = [None] * len(paths)
        futures = [executor.submit(
            _summarize_report_job, index, path, encounter_id, difficulty,
            selected_fights, config)
            for index, path in enumerate(paths)]
        for future in as_completed(futures):
            index, filename, summaries = future.result()
            ordered[index] = summaries
            progress(f'Interpreted {filename}: {len(summaries)} matching pulls.')
        return [fight for summaries in ordered for fight in summaries]

    try:
        with ProcessPoolExecutor(max_workers=worker_count) as executor:
            fights = execute(executor)
        mode = 'parallel_processes'
    except PermissionError:
        progress('Process workers are unavailable; using concurrent threads instead.')
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            fights = execute(executor)
        mode = 'parallel_threads'
    return fights, worker_count, mode


def _ability_evidence(ability_id, fights, config):
    observed = [(fight, fight['abilities'][ability_id]) for fight in fights
                if fight['abilities'].get(ability_id)]
    name = next((fight['ability_names'].get(ability_id) for fight, _bursts in observed),
                f'Spell {ability_id}')
    result = {'ability_id': ability_id, 'ability': name, 'accepted': False,
              'reasons': [], 'evidence': {}}
    if len(observed) < config.minimum_pulls:
        result['reasons'].append('fewer_than_minimum_observed_pulls')
        return result
    first_times = [bursts[0]['time'] for _fight, bursts in observed]
    first_timing = robust_timing(first_times)
    eligible = [fight for fight in fights
                if fight['duration'] + config.maximum_timing_deviation >= first_timing['median']]
    eligible_observed = [(fight, fight['abilities'][ability_id]) for fight in eligible
                         if fight['abilities'].get(ability_id)]
    pull_coverage = len(eligible_observed) / max(1, len(eligible))
    all_bursts = [burst for _fight, bursts in eligible_observed for burst in bursts]
    target_fraction = statistics.median(burst['target_fraction'] for burst in all_bursts)
    health_fractions = [burst['median_target_health_fraction'] for burst in all_bursts
                        if burst['median_target_health_fraction'] is not None]
    health_fraction = statistics.median(health_fractions) if health_fractions else None
    result['evidence'].update({
        'selected_pulls': len(fights), 'observed_pulls': len(observed),
        'eligible_pulls': len(eligible), 'eligible_observed_pulls': len(eligible_observed),
        'pull_coverage': pull_coverage, 'median_target_fraction': target_fraction,
        'median_target_health_fraction': health_fraction, 'first_occurrence': first_timing,
    })
    if pull_coverage < config.minimum_pull_coverage:
        result['reasons'].append('insufficient_pull_coverage')
    if target_fraction < config.minimum_target_fraction:
        result['reasons'].append('not_raid_wide')
    if health_fraction is None:
        result['reasons'].append('missing_health_based_damage_evidence')
    elif health_fraction < config.minimum_health_fraction:
        result['reasons'].append('damage_below_health_fraction_threshold')
    if first_timing['p90_absolute_deviation'] > config.maximum_timing_deviation:
        result['reasons'].append('inconsistent_first_occurrence')
    if first_timing['median'] < config.minimum_actionable_warning_seconds:
        result['reasons'].append('insufficient_actionable_warning_time')

    intervals = []
    repeating_pulls = 0
    for _fight, bursts in eligible_observed:
        if len(bursts) > 1:
            repeating_pulls += 1
            intervals.extend(bursts[index]['time'] - bursts[index - 1]['time']
                             for index in range(1, len(bursts)))
    interval_evidence = None
    stable_repeat = False
    if repeating_pulls >= config.minimum_pulls and intervals:
        timing = robust_timing(intervals)
        mean = statistics.mean(intervals)
        cv = statistics.pstdev(intervals) / mean if mean > 0 and len(intervals) > 1 else 0.0
        interval_evidence = {**timing, 'coefficient_of_variation': cv,
                             'pulls_with_repeats': repeating_pulls}
        stable_repeat = (timing['p90_absolute_deviation'] <= config.maximum_timing_deviation
                         and cv <= config.maximum_interval_cv)
        if not stable_repeat:
            result['reasons'].append('inconsistent_repeat_interval')
    result['evidence']['repeat_interval'] = interval_evidence

    occurrence_predictions = []
    maximum_occurrences = max(len(bursts) for _fight, bursts in eligible_observed)
    for occurrence in range(maximum_occurrences):
        pairs = [(fight, bursts[occurrence]['time']) for fight, bursts in eligible_observed
                 if len(bursts) > occurrence]
        values = [value for _fight, value in pairs]
        if len(values) < config.minimum_pulls:
            break
        timing = robust_timing(values)
        eligible_for_occurrence = [fight for fight in eligible
                                   if fight['duration'] + config.maximum_timing_deviation >=
                                   timing['median']]
        eligible_keys = {(fight['report_id'], fight['fight_id'])
                         for fight in eligible_for_occurrence}
        supported_values = [value for fight, value in pairs
                            if (fight['report_id'], fight['fight_id']) in eligible_keys]
        if len(supported_values) < config.minimum_pulls:
            break
        timing = robust_timing(supported_values)
        coverage = len(supported_values) / max(1, len(eligible_for_occurrence))
        if (coverage < config.minimum_pull_coverage or
                timing['p90_absolute_deviation'] > config.maximum_timing_deviation):
            break
        if occurrence > 0 and not stable_repeat:
            break
        occurrence_predictions.append({'occurrence': occurrence + 1, 'time': timing['median'],
                                       'pulls': len(supported_values), 'coverage': coverage,
                                       'timing': timing})
    result['evidence']['predicted_occurrences'] = occurrence_predictions
    blocking = [reason for reason in result['reasons'] if reason != 'inconsistent_repeat_interval']
    if not blocking and occurrence_predictions:
        result['accepted'] = True
        result['confidence'] = ('high' if len(eligible_observed) >= 5 and
                                (maximum_occurrences == 1 or stable_repeat) else 'moderate')
        if not stable_repeat and maximum_occurrences > 1:
            result['notes'] = ['Only the stable first occurrence was learned; repeat timers were rejected.']
    return result


def _stable_overlaps(accepted, fights, config):
    groups = []
    for left, right in itertools.combinations(accepted, 2):
        common = []
        for fight in fights:
            left_bursts = fight['abilities'].get(left['ability_id'], [])
            right_bursts = fight['abilities'].get(right['ability_id'], [])
            if not left_bursts or not right_bursts:
                continue
            difference = min(abs(a['time'] - b['time']) for a in left_bursts for b in right_bursts)
            common.append(difference)
        if len(common) < config.minimum_pulls:
            continue
        overlapping = [value for value in common if value <= config.overlap_seconds]
        if len(overlapping) / len(common) >= config.minimum_pull_coverage:
            groups.append({'ability_ids': [left['ability_id'], right['ability_id']],
                           'abilities': [left['ability'], right['ability']],
                           'common_pulls': len(common),
                           'overlap_coverage': len(overlapping) / len(common),
                           'median_separation_seconds': statistics.median(overlapping)})
    return groups


def learn_timer_script(paths, encounter_id, difficulty=None, fight_ids=None, config=None,
                       max_workers=MAX_ANALYSIS_WORKERS, progress=None):
    config = config or LearnerConfig()
    config.validate()
    selected = set(int(item) for item in (fight_ids or []))
    paths = [Path(path) for path in paths]
    if selected and len(paths) > 1:
        raise ValueError('Fight IDs can be selected only for a single report; '
                         'cross-report learning uses all matching pulls.')
    fights, worker_count, analysis_mode = _collect_report_summaries(
        paths, encounter_id, difficulty, selected, config, max_workers, progress)
    if len(fights) < config.minimum_pulls:
        raise ValueError(f'At least {config.minimum_pulls} matching pulls are required; found {len(fights)}.')
    encounter_names = sorted({fight['name'] for fight in fights})
    ability_ids = sorted({ability_id for fight in fights for ability_id in fight['abilities']})
    candidates = [_ability_evidence(ability_id, fights, config) for ability_id in ability_ids]
    accepted = [candidate for candidate in candidates if candidate['accepted']]
    stable_overlaps = _stable_overlaps(accepted, fights, config)
    mechanics = []
    for candidate in accepted:
        for prediction in candidate['evidence']['predicted_occurrences']:
            occurrence = prediction['occurrence']
            activation = prediction['time']
            warning = max(0.0, activation - config.warning_lead_seconds)
            actual_lead = activation - warning
            base = f"ability-{candidate['ability_id']}-occurrence-{occurrence}"
            if warning < activation:
                mechanics.append({'id': base + '-warning', 'time': round(warning, 3),
                                  'message': (f"{candidate['ability']} expected in "
                                              f"{actual_lead:g} seconds."),
                                  'ability_id': candidate['ability_id'], 'kind': 'WARNING'})
            mechanics.append({'id': base, 'time': round(activation, 3),
                              'message': f"{candidate['ability']} expected now.",
                              'ability_id': candidate['ability_id'], 'kind': 'EXPECTED_ACTIVATION'})
    accepted_by_id = {candidate['ability_id']: candidate for candidate in accepted}
    overlap_callouts = []
    for overlap in stable_overlaps:
        left, right = (accepted_by_id[ability_id] for ability_id in overlap['ability_ids'])
        left_times = [item['time'] for item in left['evidence']['predicted_occurrences']]
        right_times = [item['time'] for item in right['evidence']['predicted_occurrences']]
        for index, (left_time, right_time) in enumerate(
                ((a, b) for a in left_times for b in right_times
                 if abs(a - b) <= config.overlap_seconds), 1):
            activation = min(left_time, right_time)
            warning = max(0.0, activation - config.warning_lead_seconds)
            callout = {
                'id': f"overlap-{'-'.join(map(str, overlap['ability_ids']))}-{index}",
                'time': round(warning, 3),
                'message': (f"Overlapping raid damage expected in "
                            f"{activation - warning:g} seconds: "
                            f"{left['ability']} and {right['ability']}."),
                'ability_ids': overlap['ability_ids'], 'kind': 'OVERLAP_WARNING',
                'expected_times': [round(left_time, 3), round(right_time, 3)],
            }
            mechanics.append(callout)
            overlap_callouts.append(callout)
    mechanics.sort(key=lambda item: (item['time'], item['id']))
    report_ids = sorted({fight['report_id'] for fight in fights})
    source_fights = sorted(({
        'report_id': fight['report_id'], 'fight_id': fight['fight_id'],
        'duration': round(fight['duration'], 3), 'kill': fight['kill'],
        'start_time_ms': fight['start_time_ms'], 'end_time_ms': fight['end_time_ms'],
    } for fight in fights), key=lambda item: (item['report_id'], item['fight_id']))
    trigger_evidence = {
        'selected_pulls': len(fights),
        'start_boundaries_observed': sum(
            fight['start_time_ms'] is not None for fight in fights),
        'end_boundaries_observed': sum(
            fight['end_time_ms'] is not None for fight in fights),
        'kills': sum(fight['kill'] for fight in fights),
        'wipes': sum(not fight['kill'] for fight in fights),
    }
    encounter_triggers = {
        'start': {
            'id': 'fight-begin', 'event_type': 'ENCOUNTER_START',
            'action': 'START_TIMELINE', 'establishes_time_zero': True,
            'message': f'{encounter_names[0]} engaged. Timer started.',
            'match': {'encounter_id': int(encounter_id),
                      'difficulty': difficulty if difficulty is not None else 'any'},
            'observed_source': 'warcraft_logs_report_fight_start',
            'evidence': dict(trigger_evidence),
        },
        'end': {
            'id': 'fight-end', 'event_type': 'ENCOUNTER_END',
            'action': 'END_TIMELINE', 'cancel_pending_mechanics': True,
            'message': f'{encounter_names[0]} ended: {{result}}.',
            'result_mapping': {'true': 'KILL', 'false': 'WIPE'},
            'match': {'encounter_id': int(encounter_id),
                      'difficulty': difficulty if difficulty is not None else 'any'},
            'observed_source': 'warcraft_logs_report_fight_end',
            'evidence': dict(trigger_evidence),
        },
    }
    generated_at = datetime.now(timezone.utc).isoformat()
    raw_id = SAFE_ID.sub('-', f"{'-'.join(report_ids)}-{encounter_id}-"
                              f"{difficulty if difficulty is not None else 'any'}-draft-v1").strip('-')
    draft_id = (raw_id if len(raw_id) <= 150 else
                f'{raw_id[:133]}-{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}')
    return {
        'schema_version': LEARNER_SCHEMA_VERSION, 'draft_id': draft_id,
        'generated_at': generated_at, 'status': 'DRAFT_REQUIRES_REVIEW',
        'provenance': 'deterministic_observed_pull_analysis',
        'analysis': {'mode': analysis_mode, 'worker_count': worker_count},
        'report_ids': report_ids, 'encounter_id': int(encounter_id),
        'encounter_name': encounter_names[0] if len(encounter_names) == 1 else encounter_names,
        'difficulty': difficulty, 'pull_count': len(fights), 'source_fights': source_fights,
        'config': asdict(config),
        'accepted_abilities': accepted,
        'rejected_abilities': [candidate for candidate in candidates if not candidate['accepted']],
        'stable_overlaps': stable_overlaps,
        'script': {
            'script_id': draft_id, 'encounter_id': int(encounter_id),
            'encounter_name': encounter_names[0] if encounter_names else f'Encounter {encounter_id}',
            'difficulty': difficulty if difficulty is not None else 'any',
            'placeholder': False, 'draft': True, 'enabled': False,
            'encounter_triggers': encounter_triggers,
            'warning': ('Generated from repeated pulls. Review before enabling; timers describe '
                        'observed patterns and may change with strategy, phase timing, or game updates.'),
            'overlap_callouts': overlap_callouts, 'mechanics': mechanics,
        },
    }


def save_draft(result, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + '.part')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(destination)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reports', nargs='+', type=Path,
                        help='One or more managed Warcraft Logs .wcl.json caches')
    parser.add_argument('--encounter-id', required=True, type=int)
    parser.add_argument('--difficulty', type=int)
    parser.add_argument('--fight-id', action='append', type=int, default=[])
    parser.add_argument('--warning-lead', type=float, default=5.0)
    parser.add_argument('--workers', type=int, default=MAX_ANALYSIS_WORKERS,
                        help='Maximum concurrent report-analysis workers (1-5)')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--timer-export', type=Path,
                        help='Also write a portable wow-fight-timer/v1 JSON file')
    args = parser.parse_args()
    config = LearnerConfig(warning_lead_seconds=args.warning_lead)
    result = learn_timer_script(args.reports, args.encounter_id, args.difficulty,
                                args.fight_id, config, max_workers=args.workers)
    output = args.output or Path('mechanic-drafts') / f"{result['draft_id']}.json"
    save_draft(result, output)
    print(f"Wrote {len(result['accepted_abilities'])} accepted and "
          f"{len(result['rejected_abilities'])} rejected abilities to {output}")
    if args.timer_export:
        save_timer_export(build_timer_export(result), args.timer_export)
        print(f'Wrote portable timer schema to {args.timer_export}')


if __name__ == '__main__':
    main()
