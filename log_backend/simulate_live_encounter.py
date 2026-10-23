"""Append a deterministic synthetic encounter to a log watched by the live API."""

import argparse
import csv
from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import time


def record(moment, fields):
    payload = io.StringIO()
    csv.writer(payload, lineterminator='').writerow(fields)
    stamp = moment.strftime('%m/%d/%Y %H:%M:%S.%f')[:-3] + '+0'
    return f'{stamp}  {payload.getvalue()}\n'


def append(path, text):
    with path.open('a', encoding='utf-8', newline='') as output:
        output.write(text)
        output.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', type=Path, help='Test log file monitored by analysis_api.py')
    parser.add_argument('--duration', type=int, default=25)
    parser.add_argument('--step', type=int, default=5)
    parser.add_argument('--delay', type=float, default=.25,
                        help='Real seconds to wait between simulated log records')
    parser.add_argument('--kill', action='store_true', help='End as a kill instead of a wipe')
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.touch(exist_ok=True)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    append(args.log, record(start - timedelta(seconds=1), [
        'SPELL_CAST_SUCCESS', 'Player-1', 'Tester', 0, 0,
        'Creature-0', 'Training Target', 0, 0, 1, 'Pre-pull activity', 1]))
    time.sleep(max(0, args.delay))
    append(args.log, record(start, ['ENCOUNTER_START', 999001, 'Generic Test Encounter',
                                    15, 5, 0]))
    for elapsed in range(args.step, args.duration + 1, args.step):
        time.sleep(max(0, args.delay))
        append(args.log, record(start + timedelta(seconds=elapsed), [
            'SPELL_CAST_SUCCESS', 'Creature-999', 'Generic Test Encounter', 0, 0,
            'Player-1', 'Tester', 0, 0, 900000 + elapsed, 'Timeline heartbeat', 1]))
    time.sleep(max(0, args.delay))
    append(args.log, record(start + timedelta(seconds=args.duration), [
        'ENCOUNTER_END', 999001, 'Generic Test Encounter', 15, 5,
        1 if args.kill else 0, args.duration * 1000]))
    print(f'Wrote simulated {"kill" if args.kill else "wipe"} to {args.log.name}')


if __name__ == '__main__':
    main()
