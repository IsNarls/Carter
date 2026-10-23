"""One desktop entry point for the combat-log viewer and encounter timelines."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import queue
import statistics
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from combat_log_viewer import Viewer
from encounter_parser import (analyze_encounter, scan_encounters, write_boss_position_csv,
                              write_csv, write_player_csv, Position, WOW_CLASS_COLORS)
from mechanic_timer_learner import learn_timer_script, save_draft
from timer_export import TIMER_EXPORT_FORMAT, build_timer_export, save_timer_export
from warcraft_logs_importer import (WarcraftLogsClient, download_report_batch,
                                    load_cache, load_credentials, prepare_report_batch)


def default_log() -> Path | None:
    logs = list(Path(__file__).parent.glob('*.txt'))
    return max(logs, key=lambda item: item.stat().st_mtime) if logs else None


def validate_learner_selection(path, encounters):
    """Return the exact learner inputs for a compatible GUI fight selection."""
    path = Path(path)
    encounters = list(encounters)
    if not str(path).lower().endswith('.wcl.json'):
        raise ValueError('Timer learning requires an imported Warcraft Logs .wcl.json report.')
    if len(encounters) < 3:
        raise ValueError('Select at least three attempts of the same encounter.')
    encounter_ids = {item.encounter_id for item in encounters}
    if len(encounter_ids) != 1:
        raise ValueError('All selected attempts must be from the same boss encounter.')
    difficulties = {item.difficulty for item in encounters}
    if len(difficulties) != 1:
        raise ValueError('All selected attempts must use the same difficulty.')
    if any(item.external_id is None for item in encounters):
        raise ValueError('One or more selected attempts has no Warcraft Logs fight ID.')
    return {
        'encounter_id': next(iter(encounter_ids)),
        'difficulty': next(iter(difficulties)),
        'fight_ids': [item.external_id for item in encounters],
    }


def group_cross_report_encounters(paths):
    """Group compatible pulls from several report caches without loading event data."""
    groups = {}
    for path in (Path(item) for item in paths):
        if not path.is_file() or not str(path).lower().endswith('.wcl.json'):
            continue
        for encounter in scan_encounters(path):
            key = (encounter.encounter_id, encounter.difficulty)
            group = groups.setdefault(key, {
                'encounter_id': encounter.encounter_id,
                'difficulty': encounter.difficulty,
                'name': encounter.name,
                'pulls': [],
                'paths': set(),
            })
            group['pulls'].append(encounter)
            group['paths'].add(path)
    result = []
    for group in groups.values():
        group['paths'] = sorted(group['paths'], key=lambda path: path.name.casefold())
        result.append(group)
    return sorted(result, key=lambda item: (item['name'].casefold(), item['difficulty']))


def report_path_key(path):
    """Return a stable local identity for a report cache path."""
    path = Path(path)
    try:
        path = path.resolve()
    except OSError:
        path = path.absolute()
    return str(path).casefold()


def filter_checked_report_paths(paths, checked_keys):
    """Return report paths whose visible checkbox is selected."""
    return [Path(path) for path in paths if report_path_key(path) in checked_keys]


def selectable_report_paths(paths):
    """Return unique existing Warcraft Logs caches, newest first."""
    reports = []
    seen = set()
    for value in paths:
        path = Path(value)
        if not path.is_file() or not path.name.lower().endswith('.wcl.json'):
            continue
        key = report_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        reports.append(path)

    def modified(path):
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return -1

    return sorted(reports, key=lambda path: (modified(path), path.name.casefold()), reverse=True)


def report_modified_text(path):
    """Format a report's local modification time for display."""
    try:
        return datetime.fromtimestamp(Path(path).stat().st_mtime).astimezone().strftime(
            '%Y-%m-%d %H:%M:%S %Z')
    except OSError:
        return 'Unavailable'


def _timer_module_filename(timer_id):
    """Return a path-safe, predictable filename for one managed timer module."""
    safe = ''.join(character if character.isalnum() or character in '-_.' else '-'
                   for character in str(timer_id)).strip('-.')
    return f'{(safe or "timer-module")[:150]}.timer.json'


def fight_module_catalog(draft_directory, module_directory):
    """Describe saved learner drafts and whether each has an enabled live export."""
    draft_directory = Path(draft_directory)
    module_directory = Path(module_directory)
    enabled_modules = {}
    if module_directory.is_dir():
        for path in module_directory.glob('*.json'):
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if (payload.get('format') == TIMER_EXPORT_FORMAT and payload.get('enabled') and
                    payload.get('timer_id')):
                enabled_modules[str(payload['timer_id'])] = path

    items = []
    if not draft_directory.is_dir():
        return items
    for path in draft_directory.glob('*.json'):
        try:
            draft = json.loads(path.read_text(encoding='utf-8'))
            preview = build_timer_export(draft, enabled=False)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            items.append({
                'draft_path': path,
                'draft_id': path.stem,
                'encounter_name': path.stem,
                'difficulty': '-',
                'callout_count': 0,
                'pull_count': 0,
                'ready': False,
                'module_path': None,
                'error': str(error),
            })
            continue
        timer_id = str(preview['timer_id'])
        module_path = enabled_modules.get(timer_id)
        items.append({
            'draft_path': path,
            'draft_id': timer_id,
            'encounter_name': preview['encounter']['name'],
            'difficulty': preview['encounter']['difficulty'],
            'callout_count': len(preview['callouts']),
            'pull_count': preview['provenance']['pull_count'],
            'ready': module_path is not None,
            'module_path': module_path,
            'error': None,
        })
    return sorted(items, key=lambda item: (
        str(item['encounter_name']).casefold(), str(item['difficulty']), item['draft_id']))


def prepare_fight_module(draft_path, module_directory):
    """Promote one reviewed learner draft into the managed live-module directory."""
    draft_path = Path(draft_path)
    try:
        draft = json.loads(draft_path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Could not read timer draft {draft_path.name}: {error}') from error
    payload = build_timer_export(draft, enabled=True)
    destination = Path(module_directory) / _timer_module_filename(payload['timer_id'])
    save_timer_export(payload, destination)
    return destination, payload


class TimelineWindow:
    def __init__(self, root, initial_path: str):
        self.root = root
        root.title('Encounter Timeline Exporter')
        root.geometry('1120x620')
        self.path = tk.StringVar(value=initial_path)
        self.status = tk.StringVar(value='Choose a log and scan for encounters.')
        self.encounters = []
        self.messages = queue.SimpleQueue()
        self.busy = False

        file_row = ttk.Frame(root, padding=10)
        file_row.pack(fill='x')
        ttk.Entry(file_row, textvariable=self.path).pack(side='left', fill='x', expand=True)
        ttk.Button(file_row, text='Browse...', command=self.browse).pack(side='left', padx=(6, 0))
        self.scan_button = ttk.Button(file_row, text='Scan encounters', command=self.scan)
        self.scan_button.pack(side='left', padx=(6, 0))
        self.rescan_button = ttk.Button(file_row, text='Rescan WCL report',
                                        command=self.rescan_wcl)
        self.rescan_button.pack(side='left', padx=(6, 0))
        self.path.trace_add('write', lambda *_args: self.update_rescan_button())
        self.update_rescan_button()

        columns = ('attempt', 'result', 'duration', 'difficulty', 'size', 'encounter')
        frame = ttk.Frame(root, padding=(10, 0))
        frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(frame, columns=columns, show='headings', selectmode='extended')
        settings = {
            'attempt': ('Attempt', 70), 'result': ('Result', 80),
            'duration': ('Duration', 90), 'difficulty': ('Difficulty', 90),
            'size': ('Players', 75), 'encounter': ('Encounter', 410),
        }
        for column, (label, width) in settings.items():
            self.table.heading(column, text=label)
            self.table.column(column, width=width, anchor='center' if column != 'encounter' else 'w')
        scrollbar = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scrollbar.grid(row=0, column=1, sticky='ns')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)
        self.table.bind('<Double-1>', lambda _event: self.view_players())

        bottom = ttk.Frame(root, padding=10)
        bottom.pack(fill='x')
        self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=150)
        self.progress.pack(side='left')
        ttk.Label(bottom, textvariable=self.status).pack(side='left', padx=10, fill='x', expand=True)
        self.export_button = ttk.Button(bottom, text='Export timeline CSV',
                                        command=self.export, state='disabled')
        self.export_button.pack(side='right')
        self.view_button = ttk.Button(bottom, text='View player analysis',
                                      command=self.view_players, state='disabled')
        self.view_button.pack(side='right', padx=(0, 8))
        self.map_button = ttk.Button(bottom, text='View 2D map',
                                     command=self.view_map, state='disabled')
        self.map_button.pack(side='right', padx=(0, 8))
        self.compare_button = ttk.Button(bottom, text='Compare mechanics',
                                         command=self.compare_mechanics, state='disabled')
        self.compare_button.pack(side='right', padx=(0, 8))
        self.learn_button = ttk.Button(bottom, text='Learn timer draft',
                                       command=self.learn_timer, state='disabled')
        self.learn_button.pack(side='right', padx=(0, 8))
        root.after(100, self.poll_messages)

    def browse(self):
        chosen = filedialog.askopenfilename(
            parent=self.root, initialdir=str(Path(__file__).parent),
            filetypes=[('Supported logs', '*.txt *.wcl.json'), ('Combat logs', '*.txt'),
                       ('Warcraft Logs cache', '*.wcl.json'), ('All files', '*')])
        if chosen:
            self.path.set(chosen)
            self.encounters = []
            self.table.delete(*self.table.get_children())
            self.view_button.configure(state='disabled')
            self.map_button.configure(state='disabled')
            self.compare_button.configure(state='disabled')
            self.learn_button.configure(state='disabled')
            self.export_button.configure(state='disabled')
            self.update_rescan_button()
            self.status.set('New file selected. Scan for encounters.')

    def is_wcl_path(self, path=None):
        value = str(path if path is not None else self.path.get()).lower()
        return value.endswith('.wcl.json')

    def update_rescan_button(self):
        self.rescan_button.configure(
            state='normal' if not self.busy and self.is_wcl_path() else 'disabled')

    def valid_path(self):
        path = Path(self.path.get().strip())
        if not path.is_file():
            messagebox.showerror('Log not found', 'Choose an existing combat-log file.', parent=self.root)
            return None
        return path

    def set_busy(self, busy, message):
        self.busy = busy
        self.status.set(message)
        self.scan_button.configure(state='disabled' if busy else 'normal')
        self.update_rescan_button()
        selected = bool(self.table.selection())
        self.view_button.configure(state='disabled' if busy or not selected else 'normal')
        self.map_button.configure(state='disabled' if busy or not selected else 'normal')
        compare_ready = len(self.table.selection()) >= 2
        self.compare_button.configure(state='disabled' if busy or not compare_ready else 'normal')
        self.learn_button.configure(
            state='disabled' if busy or not self.learner_selection_ready() else 'normal')
        self.export_button.configure(state='disabled' if busy or not selected else 'normal')
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()

    def scan(self):
        if self.busy:
            return
        path = self.valid_path()
        if path is None:
            return
        self.set_busy(True, f'Scanning {path.name}...')

        def work():
            try:
                self.messages.put(('scan_ok', path, scan_encounters(path)))
            except Exception as error:
                self.messages.put(('error', f'Could not scan the log:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def rescan_wcl(self):
        """Re-download the selected Warcraft Logs cache and refresh its fights."""
        if self.busy:
            return
        path = self.valid_path()
        if path is None:
            return
        if not self.is_wcl_path(path):
            messagebox.showinfo(
                'Warcraft Logs cache required',
                'Select an imported .wcl.json report before rescanning Warcraft Logs.',
                parent=self.root)
            return
        client_id, client_secret = load_credentials()
        if not client_id or not client_secret:
            messagebox.showerror(
                'Credentials required',
                'Add WARCRAFT_LOGS_CLIENT_ID and WARCRAFT_LOGS_CLIENT_SECRET to the '
                'project .env file, then try again.', parent=self.root)
            return
        filename = path.name
        prefix, suffix = 'warcraft-logs-', '.wcl.json'
        if filename.lower().startswith(prefix) and filename.lower().endswith(suffix):
            code = filename[len(prefix):-len(suffix)]
        else:
            try:
                code = str(load_cache(path).get('report', {}).get('code') or '')
            except Exception as error:
                messagebox.showerror('Invalid cache', f'Could not read report code:\n{error}',
                                     parent=self.root)
                return
        if not code:
            messagebox.showerror('Invalid cache', 'The cache has no Warcraft Logs report code.',
                                 parent=self.root)
            return
        self.set_busy(True, f'Rescanning Warcraft Logs report {code}...')

        def work():
            try:
                client = WarcraftLogsClient(client_id, client_secret)
                client.download_report(
                    code, path,
                    lambda text: self.messages.put(('wcl_progress', text)))
                self.messages.put(('scan_ok', path, scan_encounters(path)))
            except Exception as error:
                self.messages.put(('error', f'Could not rescan Warcraft Logs:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def selection_changed(self, _event=None):
        if self.busy:
            return
        selected = bool(self.table.selection())
        self.view_button.configure(state='normal' if selected else 'disabled')
        self.map_button.configure(state='normal' if selected else 'disabled')
        self.compare_button.configure(
            state='normal' if len(self.table.selection()) >= 2 else 'disabled')
        self.learn_button.configure(
            state='normal' if self.learner_selection_ready() else 'disabled')
        self.export_button.configure(state='normal' if selected else 'disabled')

    def learner_selection_ready(self, path=None, encounters=None):
        if self.busy:
            return False
        path = Path(path) if path is not None else Path(self.path.get().strip())
        encounters = list(encounters) if encounters is not None else self.selected_encounters()
        try:
            validate_learner_selection(path, encounters)
        except (OSError, ValueError):
            return False
        return True

    def selected_encounter(self):
        selected = self.table.selection()
        if not selected:
            return None
        return self.encounters[int(selected[0])]

    def selected_encounters(self):
        return [self.encounters[int(item)] for item in self.table.selection()]

    def view_players(self):
        self.analyze(export=False, view='players')

    def view_map(self):
        self.analyze(export=False, view='map')

    def compare_mechanics(self):
        if self.busy:
            return
        path = self.valid_path()
        encounters = self.selected_encounters()
        if path is None or len(encounters) < 2:
            return
        encounter_ids = {encounter.encounter_id for encounter in encounters}
        if len(encounter_ids) != 1:
            messagebox.showerror(
                'Select one fight',
                'Select two or more attempts of the same boss encounter.', parent=self.root)
            return
        self.set_busy(True, f'Comparing {len(encounters)} attempts of {encounters[0].name}...')

        def work():
            try:
                timelines = [analyze_encounter(path, encounter) for encounter in encounters]
                self.messages.put(('compare_ok', timelines, path, encounters))
            except Exception as error:
                self.messages.put(('error', f'Could not compare the encounters:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def learn_timer(self, path=None, encounters=None):
        """Learn a disabled timer draft from the selected imported fights."""
        if self.busy:
            return
        path = Path(path) if path is not None else self.valid_path()
        encounters = list(encounters) if encounters is not None else self.selected_encounters()
        if path is None:
            return
        try:
            inputs = validate_learner_selection(path, encounters)
        except ValueError as error:
            messagebox.showerror('Cannot learn timer draft', str(error), parent=self.root)
            return
        encounter_name = encounters[0].name
        self.set_busy(True, f'Learning conservative timers from {len(encounters)} '
                            f'{encounter_name} attempts...')

        def work():
            try:
                result = learn_timer_script(
                    [path], inputs['encounter_id'], inputs['difficulty'], inputs['fight_ids'])
                destination = (Path(__file__).parent / 'mechanic-drafts' /
                               f"{result['draft_id']}.json")
                save_draft(result, destination)
                self.messages.put(('learner_ok', result, destination))
            except Exception as error:
                self.messages.put(('error', f'Could not learn the timer draft:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def export(self):
        self.analyze(export=True)

    def analyze(self, export=False, view='players'):
        if self.busy:
            return
        encounter = self.selected_encounter()
        path = self.valid_path()
        if encounter is None or path is None:
            return
        output = None
        if export:
            safe_name = ''.join(character if character.isalnum() else '-'
                                for character in encounter.name).strip('-').lower()
            suggested = f'timeline-{encounter.attempt}-{safe_name}.csv'
            destination = filedialog.asksaveasfilename(
                parent=self.root, initialdir=str(path.parent), initialfile=suggested,
                defaultextension='.csv', filetypes=[('CSV files', '*.csv')])
            if not destination:
                return
            output = Path(destination)
        self.set_busy(True, f'Analyzing attempt {encounter.attempt}: {encounter.name}...')

        def work():
            try:
                timeline = analyze_encounter(path, encounter)
                if output is None:
                    self.messages.put(('analysis_ok', timeline, view))
                else:
                    write_csv(timeline, output)
                    player_output = output.with_name(f'{output.stem}-players.csv')
                    write_player_csv(timeline, player_output)
                    boss_output = output.with_name(f'{output.stem}-boss-positions.csv')
                    write_boss_position_csv(timeline, boss_output)
                    bosses = ', '.join(unit.name for unit in timeline.bosses) or 'none detected'
                    self.messages.put(('export_ok', output, player_output, boss_output,
                                       timeline, bosses))
            except Exception as error:
                self.messages.put(('error', f'Could not build the timeline:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def poll_messages(self):
        try:
            while True:
                message = self.messages.get_nowait()
                kind = message[0]
                if kind == 'wcl_progress':
                    self.status.set(message[1])
                elif kind == 'scan_ok':
                    _kind, path, encounters = message
                    # Discard an old scan if the path changed while it ran.
                    if Path(self.path.get().strip()) != path:
                        continue
                    self.encounters = encounters
                    self.table.delete(*self.table.get_children())
                    for index, encounter in enumerate(encounters):
                        result = 'Kill' if encounter.success else ('Wipe' if encounter.end else 'Open')
                        duration = f'{encounter.duration:.1f}s' if encounter.duration is not None else '-'
                        self.table.insert('', 'end', iid=str(index), values=(
                            encounter.attempt, result, duration, encounter.difficulty,
                            encounter.group_size, encounter.name))
                    self.set_busy(False, f'Found {len(encounters)} encounters. '
                                         'Select 2+ to compare or 3+ WCL pulls to learn timers.')
                    if encounters:
                        self.table.selection_set('0')
                        self.table.focus('0')
                        self.view_button.configure(state='normal')
                        self.map_button.configure(state='normal')
                        self.compare_button.configure(state='disabled')
                        self.learn_button.configure(state='disabled')
                        self.export_button.configure(state='normal')
                elif kind == 'analysis_ok':
                    timeline, view = message[1], message[2]
                    self.set_busy(False, f'Analyzed {len(timeline.rows)} seconds and '
                                         f'{len(timeline.players)} players.')
                    if view == 'map':
                        EncounterMapWindow(self.root, timeline)
                    else:
                        PlayerAnalysisWindow(self.root, timeline)
                elif kind == 'export_ok':
                    _kind, output, player_output, boss_output, timeline, bosses = message
                    self.set_busy(False, f'Analyzed {len(timeline.rows)} seconds and '
                                         f'{len(timeline.players)} players.')
                    PlayerAnalysisWindow(self.root, timeline)
                    messagebox.showinfo('Timeline exported',
                                        f'Saved encounter timeline:\n{output}\n\n'
                                        f'Saved player timeline:\n{player_output}\n\n'
                                        f'Saved boss position profiles:\n{boss_output}\n\n'
                                        f'Boss units: {bosses}', parent=self.root)
                elif kind == 'compare_ok':
                    timelines, path, encounters = message[1], message[2], message[3]
                    self.set_busy(False, f'Compared {len(timelines)} attempts of '
                                         f'{timelines[0].encounter.name}.')
                    on_learn = None
                    if self.learner_selection_ready(path, encounters):
                        on_learn = lambda p=path, items=encounters: self.learn_timer(p, items)
                    MechanicComparisonWindow(self.root, timelines, on_learn=on_learn)
                elif kind == 'learner_ok':
                    result, destination = message[1], message[2]
                    accepted = len(result['accepted_abilities'])
                    rejected = len(result['rejected_abilities'])
                    self.set_busy(False, f'Learned a disabled draft: {accepted} accepted, '
                                         f'{rejected} rejected candidates.')
                    MechanicDraftReviewWindow(self.root, result, destination)
                elif kind == 'error':
                    self.set_busy(False, 'Operation failed.')
                    messagebox.showerror('Error', message[1], parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self.poll_messages)


class PlayerAnalysisWindow:
    def __init__(self, parent, timeline):
        self.timeline = timeline
        self.players_by_id = {player.guid: player for player in timeline.players}
        self.root = tk.Toplevel(parent)
        self.root.title(f'Player Analysis - {timeline.encounter.name}')
        self.root.geometry('1120x720')
        result = 'Kill' if timeline.encounter.success else 'Wipe'
        duration = timeline.encounter.duration or 0
        ttk.Label(
            self.root,
            text=f'{timeline.encounter.name}  |  Attempt {timeline.encounter.attempt}  |  '
                 f'{result}  |  {duration:.1f} seconds',
            font=('', 12, 'bold'), padding=10,
        ).pack(fill='x')
        metric_row = ttk.Frame(self.root, padding=(10, 0, 10, 8))
        metric_row.pack(fill='x')
        ttk.Label(metric_row, text='Ranking:').pack(side='left')
        self.metric = tk.StringVar(value='Damage')
        metric_picker = ttk.Combobox(metric_row, textvariable=self.metric,
                                     values=('Damage', 'Healing'), state='readonly', width=12)
        metric_picker.pack(side='left', padx=(6, 0))
        metric_picker.bind('<<ComboboxSelected>>', self.refresh_players)
        ttk.Button(metric_row, text='Open 2D encounter map', command=self.open_map)\
            .pack(side='right')

        pane = ttk.Panedwindow(self.root, orient='vertical')
        pane.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.players_frame = ttk.LabelFrame(pane, text='Player damage ranking')
        player_columns = ('rank', 'player', 'class', 'total', 'rate', 'samples', 'last_position')
        self.player_table = ttk.Treeview(self.players_frame, columns=player_columns,
                                         show='headings', selectmode='browse', height=10)
        player_settings = {
            'rank': ('Rank', 60), 'player': ('Player', 240),
            'class': ('Class', 105),
            'total': ('Total damage', 150), 'rate': ('Fight DPS', 120),
            'samples': ('Position samples', 130), 'last_position': ('Last recorded position', 300),
        }
        for column, (label, width) in player_settings.items():
            self.player_table.heading(column, text=label)
            self.player_table.column(column, width=width,
                                     anchor='w' if column in ('player', 'last_position') else 'e')
        player_scroll = ttk.Scrollbar(self.players_frame, orient='vertical', command=self.player_table.yview)
        self.player_table.configure(yscrollcommand=player_scroll.set)
        self.player_table.grid(row=0, column=0, sticky='nsew')
        player_scroll.grid(row=0, column=1, sticky='ns')
        self.players_frame.rowconfigure(0, weight=1)
        self.players_frame.columnconfigure(0, weight=1)
        pane.add(self.players_frame, weight=1)

        seconds_frame = ttk.LabelFrame(pane, text='Selected player by second')
        second_columns = ('time', 'damage', 'cumulative_damage', 'healing',
                          'cumulative_healing', 'x', 'y', 'map', 'age')
        self.second_table = ttk.Treeview(seconds_frame, columns=second_columns,
                                         show='headings', selectmode='browse')
        second_settings = {
            'time': ('Time', 75), 'damage': ('Damage', 120),
            'cumulative_damage': ('Cumulative damage', 145),
            'healing': ('Healing', 120), 'cumulative_healing': ('Cumulative healing', 145),
            'x': ('X', 90), 'y': ('Y', 90),
            'map': ('Map', 90), 'age': ('Position age', 110),
        }
        for column, (label, width) in second_settings.items():
            self.second_table.heading(column, text=label)
            self.second_table.column(column, width=width, anchor='e')
        second_scroll = ttk.Scrollbar(seconds_frame, orient='vertical', command=self.second_table.yview)
        self.second_table.configure(yscrollcommand=second_scroll.set)
        self.second_table.grid(row=0, column=0, sticky='nsew')
        second_scroll.grid(row=0, column=1, sticky='ns')
        seconds_frame.rowconfigure(0, weight=1)
        seconds_frame.columnconfigure(0, weight=1)
        pane.add(seconds_frame, weight=2)

        self.player_table.bind('<<TreeviewSelect>>', self.show_player)
        self.refresh_players()

    def last_position_text(self, player):
        last_position = next((row.player_positions[player.guid]
                              for row in reversed(self.timeline.rows)
                              if player.guid in row.player_positions), None)
        return '' if last_position is None else (
            f'{last_position.x:.2f}, {last_position.y:.2f}  map {last_position.map_id} '
            f'({last_position.age}s old)')

    def refresh_players(self, _event=None):
        self.player_table.delete(*self.player_table.get_children())
        if self.metric.get() == 'Healing':
            players = sorted((player for player in self.timeline.players if player.healing),
                             key=lambda player: player.healing_rank)
            self.players_frame.configure(text='Player effective-healing ranking')
            self.player_table.heading('total', text='Effective healing')
            self.player_table.heading('rate', text='Fight HPS')
            values = lambda player: (player.healing_rank, player.name,
                                     player.class_name, f'{player.healing:,}', f'{player.hps:,.1f}',
                                     player.position_samples, self.last_position_text(player))
        else:
            players = sorted((player for player in self.timeline.players if player.damage),
                             key=lambda player: player.rank)
            self.players_frame.configure(text='Player boss-damage ranking')
            self.player_table.heading('total', text='Total boss damage')
            self.player_table.heading('rate', text='Fight DPS')
            values = lambda player: (player.rank, player.name,
                                     player.class_name, f'{player.damage:,}', f'{player.dps:,.1f}',
                                     player.position_samples, self.last_position_text(player))
        for player in players:
            self.player_table.insert('', 'end', iid=player.guid, values=(
                values(player)))
        if players:
            first = players[0].guid
            self.player_table.selection_set(first)
            self.player_table.focus(first)
            self.show_player()
        else:
            self.second_table.delete(*self.second_table.get_children())

    def show_player(self, _event=None):
        selected = self.player_table.selection()
        if not selected:
            return
        guid = selected[0]
        self.second_table.delete(*self.second_table.get_children())
        cumulative_damage = 0
        cumulative_healing = 0
        for row in self.timeline.rows:
            dealt = row.player_damage.get(guid, 0)
            healed = row.player_healing.get(guid, 0)
            cumulative_damage += dealt
            cumulative_healing += healed
            position = row.player_positions.get(guid)
            minute, second = divmod(row.second, 60)
            self.second_table.insert('', 'end', values=(
                f'{minute}:{second:02d}', f'{dealt:,}', f'{cumulative_damage:,}',
                f'{healed:,}', f'{cumulative_healing:,}',
                '' if position is None else f'{position.x:.2f}',
                '' if position is None else f'{position.y:.2f}',
                '' if position is None else position.map_id,
                '' if position is None else f'{position.age}s',
            ))

    def open_map(self):
        EncounterMapWindow(self.root, self.timeline)


class EncounterMapWindow:
    BOSS_COLOR = '#ff4d4d'
    BACKGROUND = '#111820'
    GRID_COLOR = '#293541'

    def __init__(self, parent, timeline):
        self.timeline = timeline
        self.root = tk.Toplevel(parent)
        self.root.title(f'2D Encounter Map - {timeline.encounter.name}')
        self.root.geometry('1050x780')
        self.playing = False
        self.frame = tk.DoubleVar(value=0.0)
        self.speed = tk.StringVar(value='1')
        self.show_names = tk.BooleanVar(value=False)
        self.time_text = tk.StringVar()
        self.bounds = (0.0, 1.0, 0.0, 1.0)
        self.last_animation_tick = None

        maps = sorted({position.map_id for row in timeline.rows
                       for position in (*row.player_positions.values(),
                                        *row.boss_positions.values())})
        self.map_id = tk.StringVar(value=str(maps[0]) if maps else '')
        self.profile_guids = {'All bosses': None}
        for profile in timeline.boss_profiles:
            label = profile.name
            if label in self.profile_guids:
                label = f'{label} ({profile.guid[-8:]})'
            self.profile_guids[label] = profile.guid
        self.profile = tk.StringVar(value='All bosses')

        controls = ttk.Frame(self.root, padding=8)
        controls.pack(fill='x')
        self.play_button = ttk.Button(controls, text='Play', command=self.toggle_play)
        self.play_button.pack(side='left')
        ttk.Button(controls, text='|<', command=lambda: self.set_frame(0)).pack(side='left', padx=(6, 0))
        ttk.Label(controls, text='Speed:').pack(side='left', padx=(14, 4))
        ttk.Combobox(controls, textvariable=self.speed, values=('0.5', '1', '2', '5', '10'),
                     state='readonly', width=5).pack(side='left')
        ttk.Label(controls, text='Map:').pack(side='left', padx=(14, 4))
        map_picker = ttk.Combobox(controls, textvariable=self.map_id,
                                  values=tuple(str(item) for item in maps),
                                  state='readonly', width=8)
        map_picker.pack(side='left')
        map_picker.bind('<<ComboboxSelected>>', self.map_changed)
        ttk.Label(controls, text='Boss profile:').pack(side='left', padx=(14, 4))
        profile_picker = ttk.Combobox(controls, textvariable=self.profile,
                                      values=tuple(self.profile_guids),
                                      state='readonly', width=28)
        profile_picker.pack(side='left')
        profile_picker.bind('<<ComboboxSelected>>', lambda _event: self.draw())
        ttk.Checkbutton(controls, text='Player names', variable=self.show_names,
                        command=self.draw).pack(side='left', padx=(14, 0))

        content = ttk.Panedwindow(self.root, orient='horizontal')
        content.pack(fill='both', expand=True, padx=8)
        map_frame = ttk.Frame(content)
        self.canvas = tk.Canvas(map_frame, background=self.BACKGROUND, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _event: self.draw())
        content.add(map_frame, weight=3)

        mechanics_frame = ttk.LabelFrame(content, text='Boss mechanics')
        self.current_mechanics = tk.Text(mechanics_frame, height=9, width=34, wrap='word',
                                         background='#171f28', foreground='#f5d76e',
                                         relief='flat', padx=8, pady=8, state='disabled')
        self.current_mechanics.pack(fill='x', padx=6, pady=6)
        ttk.Label(mechanics_frame, text='Mechanics seen so far').pack(anchor='w', padx=6)
        mechanic_columns = ('time', 'boss', 'ability', 'occurrence', 'interval')
        self.mechanic_history = ttk.Treeview(mechanics_frame, columns=mechanic_columns,
                                             show='headings', selectmode='browse')
        mechanic_settings = {
            'time': ('Time', 55), 'boss': ('Boss', 105), 'ability': ('Mechanic', 145),
            'occurrence': ('#', 35), 'interval': ('Gap', 55),
        }
        for column, (label, width) in mechanic_settings.items():
            self.mechanic_history.heading(column, text=label)
            self.mechanic_history.column(column, width=width, minwidth=30)
        mechanic_scroll = ttk.Scrollbar(mechanics_frame, orient='vertical',
                                        command=self.mechanic_history.yview)
        self.mechanic_history.configure(yscrollcommand=mechanic_scroll.set)
        self.mechanic_history.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=(2, 6))
        mechanic_scroll.pack(side='right', fill='y', padx=(0, 6), pady=(2, 6))
        content.add(mechanics_frame, weight=1)
        self.last_mechanic_signature = None
        bottom = ttk.Frame(self.root, padding=8)
        bottom.pack(fill='x')
        self.slider = ttk.Scale(bottom, from_=0, to=max(0, len(timeline.rows) - 1),
                                variable=self.frame, command=self.scrub)
        self.slider.pack(side='left', fill='x', expand=True)
        ttk.Label(bottom, textvariable=self.time_text, width=30, anchor='e').pack(side='right', padx=(8, 0))
        self.recalculate_bounds()
        self.draw()

    def current_map(self):
        try:
            return int(self.map_id.get())
        except ValueError:
            return None

    def map_changed(self, _event=None):
        self.recalculate_bounds()
        self.draw()

    def recalculate_bounds(self):
        map_id = self.current_map()
        points = [(position.x, position.y) for row in self.timeline.rows
                  for position in (*row.player_positions.values(), *row.boss_positions.values())
                  if position.map_id == map_id]
        if not points:
            self.bounds = (0.0, 1.0, 0.0, 1.0)
            return
        xs, ys = zip(*points)
        x_pad = max(1.0, (max(xs) - min(xs)) * 0.05)
        y_pad = max(1.0, (max(ys) - min(ys)) * 0.05)
        self.bounds = (min(xs) - x_pad, max(xs) + x_pad,
                       min(ys) - y_pad, max(ys) + y_pad)

    def project(self, x, y):
        x_min, x_max, y_min, y_max = self.bounds
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        margin = 42
        px = margin + (x - x_min) / max(x_max - x_min, 1) * max(width - margin * 2, 1)
        py = height - margin - (y - y_min) / max(y_max - y_min, 1) * max(height - margin * 2, 1)
        return px, py

    def draw(self):
        if not self.timeline.rows:
            return
        self.canvas.delete('all')
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        for step in range(1, 5):
            x = width * step / 5
            y = height * step / 5
            self.canvas.create_line(x, 0, x, height, fill=self.GRID_COLOR)
            self.canvas.create_line(0, y, width, y, fill=self.GRID_COLOR)
        moment = min(max(float(self.frame.get()), 0.0), len(self.timeline.rows) - 1)
        index = int(moment)
        fraction = moment - index
        row = self.timeline.rows[index]
        next_row = self.timeline.rows[min(index + 1, len(self.timeline.rows) - 1)]
        map_id = self.current_map()
        player_count = 0
        for player in self.timeline.players:
            position = self.interpolate_position(row.player_positions, next_row.player_positions,
                                                 player.guid, fraction)
            if position is None or position.map_id != map_id or position.age > 3:
                continue
            x, y = self.project(position.x, position.y)
            color = WOW_CLASS_COLORS.get(player.class_name, WOW_CLASS_COLORS['Unknown'])
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5,
                                    fill=color, outline='#eef7ff')
            if self.show_names.get():
                self.canvas.create_text(x + 8, y, text=player.name, fill=color, anchor='w')
            player_count += 1
        selected_guid = self.profile_guids.get(self.profile.get())
        boss_count = 0
        boss_names = {profile.guid: profile.name for profile in self.timeline.boss_profiles}
        boss_guids = set(row.boss_positions) | set(next_row.boss_positions)
        for guid in boss_guids:
            position = self.interpolate_position(row.boss_positions, next_row.boss_positions,
                                                 guid, fraction)
            if position is None:
                continue
            if position.map_id != map_id or (selected_guid and guid != selected_guid):
                continue
            x, y = self.project(position.x, position.y)
            self.canvas.create_oval(x - 9, y - 9, x + 9, y + 9,
                                    fill=self.BOSS_COLOR, outline='#ffe1e1', width=2)
            self.canvas.create_text(x + 12, y, text=boss_names.get(guid, 'Boss'),
                                    fill='#ffe1e1', anchor='w')
            boss_count += 1
        legend_x, legend_y = 14, 18
        self.canvas.create_oval(legend_x, legend_y - 8, legend_x + 16, legend_y + 8,
                                fill=self.BOSS_COLOR, outline='')
        self.canvas.create_text(legend_x + 22, legend_y, text='Boss', fill='#ffe1e1', anchor='w')
        legend_x += 76
        present_classes = sorted({player.class_name for player in self.timeline.players})
        for class_name in present_classes:
            if legend_x > width - 110:
                legend_x, legend_y = 14, legend_y + 22
            color = WOW_CLASS_COLORS.get(class_name, WOW_CLASS_COLORS['Unknown'])
            self.canvas.create_oval(legend_x, legend_y - 6, legend_x + 12, legend_y + 6,
                                    fill=color, outline='')
            self.canvas.create_text(legend_x + 17, legend_y, text=class_name,
                                    fill=color, anchor='w')
            legend_x += 17 + max(58, len(class_name) * 7)
        display_seconds = row.second + fraction
        minute = int(display_seconds // 60)
        second = display_seconds - minute * 60
        self.time_text.set(f'{minute}:{second:04.1f}  |  {player_count} players  |  {boss_count} bosses')
        self.update_mechanics(display_seconds, selected_guid)

    def update_mechanics(self, moment, selected_guid):
        mechanics = [mechanic for mechanic in self.timeline.mechanics
                     if selected_guid is None or mechanic.boss_guid == selected_guid]
        active = [mechanic for mechanic in mechanics
                  if mechanic.start <= moment <= max(mechanic.end + 1.5,
                                                     mechanic.start + 2.0)]
        lines = []
        for mechanic in active:
            ability = mechanic.ability or f'Spell {mechanic.spell_id}'
            if mechanic.end > moment and mechanic.end > mechanic.start:
                state = f'CASTING — {mechanic.end - moment:.1f}s remaining'
            elif mechanic.completed:
                state = 'CAST COMPLETE'
            else:
                state = 'CAST ENDED / INTERRUPTED'
            interval = '' if mechanic.since_previous is None else (
                f' • {mechanic.since_previous:.1f}s since previous')
            lines.append(f'{state}\n{ability}\n{mechanic.boss_name} • occurrence '
                         f'#{mechanic.occurrence}{interval}')
        current_text = '\n\n'.join(lines) if lines else 'No boss mechanic active'
        self.current_mechanics.configure(state='normal')
        self.current_mechanics.delete('1.0', 'end')
        self.current_mechanics.insert('1.0', current_text)
        self.current_mechanics.configure(state='disabled')

        occurred = [mechanic for mechanic in mechanics if mechanic.start <= moment]
        signature = (selected_guid, len(occurred), occurred[-1].start if occurred else None)
        if signature == self.last_mechanic_signature:
            return
        self.last_mechanic_signature = signature
        self.mechanic_history.delete(*self.mechanic_history.get_children())
        for mechanic in reversed(occurred[-30:]):
            minute = int(mechanic.start // 60)
            second = mechanic.start - minute * 60
            interval = '' if mechanic.since_previous is None else f'{mechanic.since_previous:.1f}s'
            self.mechanic_history.insert('', 'end', values=(
                f'{minute}:{second:04.1f}', mechanic.boss_name,
                mechanic.ability or f'Spell {mechanic.spell_id}',
                mechanic.occurrence, interval,
            ))

    @staticmethod
    def interpolate_position(current, following, guid, fraction):
        first = current.get(guid)
        if first is None:
            return None
        second = following.get(guid)
        if second is None or first.map_id != second.map_id or fraction <= 0:
            return first
        return Position(
            first.x + (second.x - first.x) * fraction,
            first.y + (second.y - first.y) * fraction,
            first.map_id,
            first.age + (second.age - first.age) * fraction,
        )

    def scrub(self, value):
        self.frame.set(float(value))
        self.draw()

    def set_frame(self, value):
        self.frame.set(value)
        self.draw()

    def toggle_play(self):
        self.playing = not self.playing
        if self.playing:
            if self.frame.get() >= len(self.timeline.rows) - 1:
                self.frame.set(0.0)
            self.last_animation_tick = time.monotonic()
            self.play_button.configure(text='Pause')
            self.advance()
        else:
            self.play_button.configure(text='Play')

    def advance(self):
        if not self.playing or not self.root.winfo_exists():
            return
        now = time.monotonic()
        elapsed = now - (self.last_animation_tick or now)
        self.last_animation_tick = now
        next_frame = float(self.frame.get()) + elapsed * float(self.speed.get())
        if next_frame >= len(self.timeline.rows):
            self.playing = False
            self.frame.set(float(len(self.timeline.rows) - 1))
            self.play_button.configure(text='Play')
            self.draw()
            return
        self.set_frame(next_frame)
        self.root.after(33, self.advance)


class MechanicComparisonWindow:
    COLORS = ('#3FC7EB', '#FF7C0A', '#A330C9', '#00FF98', '#F48CBA',
              '#FFF468', '#C41E3A', '#AAD372', '#8788EE', '#C69B6D',
              '#2DD4BF', '#F59E0B', '#E879F9', '#60A5FA', '#FB7185')

    def __init__(self, parent, timelines, on_learn=None):
        self.timelines = sorted(timelines, key=lambda item: item.encounter.attempt)
        encounter = self.timelines[0].encounter
        self.root = tk.Toplevel(parent)
        self.root.title(f'Mechanic Comparison - {encounter.name}')
        self.root.geometry('1260x820')
        self.boss = tk.StringVar(value='All bosses')
        self.ability = tk.StringVar(value='All mechanics')
        self.ability_labels = {}
        self.summary_keys = {}

        header = ttk.Frame(self.root, padding=10)
        header.pack(fill='x')
        if on_learn is not None:
            ttk.Button(header, text='Run conservative learner', command=on_learn).pack(side='right')
        ttk.Label(header, text=f'{encounter.name} mechanic comparison',
                  font=('', 14, 'bold')).pack(side='left')
        ttk.Label(header, text=f'{len(self.timelines)} attempts aligned at pull',
                  padding=(12, 0)).pack(side='left')
        ttk.Label(header, text='Boss:').pack(side='left', padx=(24, 4))
        bosses = sorted({mechanic.boss_name for timeline in self.timelines
                         for mechanic in timeline.mechanics})
        boss_picker = ttk.Combobox(header, textvariable=self.boss,
                                   values=('All bosses', *bosses), state='readonly', width=25)
        boss_picker.pack(side='left')
        boss_picker.bind('<<ComboboxSelected>>', self.boss_changed)
        ttk.Label(header, text='Mechanic:').pack(side='left', padx=(16, 4))
        self.ability_picker = ttk.Combobox(header, textvariable=self.ability,
                                           state='readonly', width=32)
        self.ability_picker.pack(side='left')
        self.ability_picker.bind('<<ComboboxSelected>>', lambda _event: self.refresh())

        chart_frame = ttk.LabelFrame(self.root, text='Static mechanic timelines')
        chart_frame.pack(fill='both', expand=True, padx=10)
        self.canvas = tk.Canvas(chart_frame, background='#111820', highlightthickness=0,
                                height=max(280, len(self.timelines) * 52 + 70))
        vertical = ttk.Scrollbar(chart_frame, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vertical.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        vertical.pack(side='right', fill='y')
        self.canvas.bind('<Configure>', lambda _event: self.draw_chart())

        summary_frame = ttk.LabelFrame(self.root, text='Observed timing summary')
        summary_frame.pack(fill='both', padx=10, pady=10)
        columns = ('marker', 'mechanic', 'boss', 'pulls', 'occurrences', 'first', 'repeat')
        self.summary = ttk.Treeview(summary_frame, columns=columns, show='headings',
                                    selectmode='browse', height=8)
        settings = {
            'marker': ('Key', 45), 'mechanic': ('Mechanic', 230), 'boss': ('Boss', 180),
            'pulls': ('Pulls seen', 80), 'occurrences': ('Total casts', 85),
            'first': ('First cast: median (range)', 190),
            'repeat': ('Repeat gap: median (range)', 200),
        }
        for column, (label, width) in settings.items():
            self.summary.heading(column, text=label)
            self.summary.column(column, width=width,
                                anchor='w' if column in ('mechanic', 'boss') else 'center')
        summary_scroll = ttk.Scrollbar(summary_frame, orient='vertical', command=self.summary.yview)
        self.summary.configure(yscrollcommand=summary_scroll.set)
        self.summary.pack(side='left', fill='both', expand=True)
        summary_scroll.pack(side='right', fill='y')
        self.summary.bind('<Double-1>', self.select_summary_mechanic)
        self.rebuild_abilities()

    @staticmethod
    def mechanic_key(mechanic):
        return mechanic.boss_name, mechanic.spell_id, mechanic.ability

    @staticmethod
    def clock(seconds):
        minute = int(seconds // 60)
        return f'{minute}:{seconds - minute * 60:04.1f}'

    @staticmethod
    def timing_summary(values):
        if not values:
            return '-'
        middle = statistics.median(values)
        if len(values) == 1 or max(values) - min(values) < 0.05:
            return f'{middle:.1f}s'
        return f'{middle:.1f}s ({min(values):.1f}-{max(values):.1f})'

    def mechanics_for(self, timeline, apply_ability=True):
        mechanics = timeline.mechanics
        if self.boss.get() != 'All bosses':
            mechanics = [item for item in mechanics if item.boss_name == self.boss.get()]
        selected_key = self.ability_labels.get(self.ability.get())
        if apply_ability and selected_key:
            mechanics = [item for item in mechanics if self.mechanic_key(item) == selected_key]
        return mechanics

    def boss_changed(self, _event=None):
        self.rebuild_abilities()

    def rebuild_abilities(self):
        keys = sorted({self.mechanic_key(item) for timeline in self.timelines
                       for item in self.mechanics_for(timeline, apply_ability=False)},
                      key=lambda key: (key[0], key[2], key[1]))
        self.ability_labels = {}
        labels = ['All mechanics']
        for boss, spell_id, ability in keys:
            label = f'{ability or f"Spell {spell_id}"} - {boss}'
            self.ability_labels[label] = (boss, spell_id, ability)
            labels.append(label)
        self.ability_picker.configure(values=labels)
        self.ability.set('All mechanics')
        self.refresh()

    def refresh(self):
        self.draw_chart()
        self.draw_summary()

    def current_keys(self):
        return sorted({self.mechanic_key(item) for timeline in self.timelines
                       for item in self.mechanics_for(timeline)},
                      key=lambda key: (key[0], key[2], key[1]))

    def draw_chart(self):
        self.canvas.delete('all')
        width = max(self.canvas.winfo_width(), 900)
        left, right, top, row_height = 165, 45, 48, 52
        chart_width = max(1, width - left - right)
        maximum = max((timeline.encounter.duration or 0 for timeline in self.timelines), default=1)
        maximum = max(maximum, 1)
        grid_step = 30 if maximum <= 600 else 60
        for elapsed in range(0, int(maximum) + grid_step, grid_step):
            x = left + elapsed / maximum * chart_width
            self.canvas.create_line(x, top - 22, x, top + len(self.timelines) * row_height,
                                    fill='#293541')
            self.canvas.create_text(x, top - 30, text=self.clock(elapsed),
                                    fill='#9fb0c0', anchor='center')
        keys = self.current_keys()
        key_numbers = {key: index + 1 for index, key in enumerate(keys)}
        key_colors = {key: self.COLORS[index % len(self.COLORS)]
                      for index, key in enumerate(keys)}
        for row_index, timeline in enumerate(self.timelines):
            encounter = timeline.encounter
            y = top + row_index * row_height
            duration = encounter.duration or (timeline.rows[-1].second if timeline.rows else 0)
            end_x = left + duration / maximum * chart_width
            result = 'KILL' if encounter.success else ('WIPE' if encounter.end else 'OPEN')
            result_color = '#35d07f' if encounter.success else '#ff6262'
            self.canvas.create_text(10, y, text=f'#{encounter.attempt}  {result}',
                                    fill=result_color, anchor='w', font=('', 10, 'bold'))
            self.canvas.create_text(105, y, text=self.clock(duration), fill='#b8c5d1', anchor='w')
            self.canvas.create_line(left, y, end_x, y, fill='#647687', width=2)
            self.canvas.create_oval(end_x - 4, y - 4, end_x + 4, y + 4,
                                    fill=result_color, outline='')
            for mechanic in self.mechanics_for(timeline):
                key = self.mechanic_key(mechanic)
                x = left + mechanic.start / maximum * chart_width
                cast_end = left + min(mechanic.end, duration) / maximum * chart_width
                color = key_colors[key]
                self.canvas.create_line(x, y - 11, x, y + 11, fill=color, width=3)
                if cast_end > x:
                    self.canvas.create_line(x, y, cast_end, y, fill=color, width=5)
                self.canvas.create_text(x, y - 14, text=str(key_numbers[key]),
                                        fill=color, anchor='s', font=('', 8, 'bold'))
        total_height = top + len(self.timelines) * row_height + 20
        self.canvas.configure(scrollregion=(0, 0, width, total_height))

    def draw_summary(self):
        self.summary.delete(*self.summary.get_children())
        self.summary_keys = {}
        keys = self.current_keys()
        for index, key in enumerate(keys):
            boss, spell_id, ability = key
            first_casts = []
            repeat_gaps = []
            occurrences = 0
            pulls_seen = 0
            for timeline in self.timelines:
                events = sorted((item for item in self.mechanics_for(timeline)
                                 if self.mechanic_key(item) == key), key=lambda item: item.start)
                if not events:
                    continue
                pulls_seen += 1
                occurrences += len(events)
                first_casts.append(events[0].start)
                repeat_gaps.extend(events[item].start - events[item - 1].start
                                   for item in range(1, len(events)))
            iid = str(index)
            color = self.COLORS[index % len(self.COLORS)]
            self.summary.tag_configure(iid, foreground=color)
            self.summary.insert('', 'end', iid=iid, tags=(iid,), values=(
                index + 1, ability or f'Spell {spell_id}', boss,
                f'{pulls_seen}/{len(self.timelines)}', occurrences,
                self.timing_summary(first_casts), self.timing_summary(repeat_gaps),
            ))
            self.summary_keys[iid] = key

    def select_summary_mechanic(self, _event=None):
        selected = self.summary.selection()
        if not selected:
            return
        key = self.summary_keys.get(selected[0])
        label = next((label for label, candidate in self.ability_labels.items()
                      if candidate == key), None)
        if label:
            self.ability.set(label)
            self.refresh()


class FightModuleWindow:
    """Prepare reviewed fight timers for live encounter matching."""

    def __init__(self, parent):
        self.root = tk.Toplevel(parent)
        self.root.title('Fight Modules')
        self.root.geometry('900x520')
        self.root.minsize(760, 420)
        self.draft_directory = Path(__file__).parent / 'mechanic-drafts'
        self.module_directory = Path(__file__).parent / 'timer-exports'
        self.items = []
        self.status = tk.StringVar()

        header = ttk.Frame(self.root, padding=14)
        header.pack(fill='x')
        ttk.Label(header, text='Fight modules', font=('', 15, 'bold')).pack(anchor='w')
        ttk.Label(
            header,
            text=('Prepare reviewed timer drafts for live use. Preparing a module does not '
                  'start its clock; the matching ENCOUNTER_START event starts the fight timer.'),
            wraplength=850,
            justify='left',
        ).pack(anchor='w', pady=(4, 0))

        frame = ttk.Frame(self.root, padding=(14, 0))
        frame.pack(fill='both', expand=True)
        columns = ('encounter', 'difficulty', 'callouts', 'pulls', 'status')
        self.table = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        settings = {
            'encounter': ('Encounter', 340, 'w'),
            'difficulty': ('Difficulty', 100, 'center'),
            'callouts': ('Callouts', 85, 'center'),
            'pulls': ('Source pulls', 100, 'center'),
            'status': ('Live status', 150, 'center'),
        }
        for column, (label, width, anchor) in settings.items():
            self.table.heading(column, text=label)
            self.table.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)

        controls = ttk.Frame(self.root, padding=14)
        controls.pack(fill='x')
        ttk.Label(controls, textvariable=self.status).pack(side='left', fill='x', expand=True)
        ttk.Button(controls, text='Refresh', command=self.refresh).pack(side='right')
        self.prepare_button = ttk.Button(
            controls, text='Prepare selected for live use', command=self.prepare_selected,
            state='disabled')
        self.prepare_button.pack(side='right', padx=(0, 8))
        self.refresh()

    def refresh(self, selected_draft_id=None):
        self.items = fight_module_catalog(self.draft_directory, self.module_directory)
        self.table.delete(*self.table.get_children())
        selected_iid = None
        for index, item in enumerate(self.items):
            iid = str(index)
            if item['error']:
                state = 'Invalid draft'
            elif item['ready']:
                state = 'Ready'
            else:
                state = 'Draft only'
            difficulty = item['difficulty'] if item['difficulty'] is not None else 'Any'
            self.table.insert('', 'end', iid=iid, values=(
                item['encounter_name'], difficulty, item['callout_count'],
                item['pull_count'], state,
            ))
            if item['draft_id'] == selected_draft_id:
                selected_iid = iid
        ready = sum(1 for item in self.items if item['ready'])
        invalid = sum(1 for item in self.items if item['error'])
        if not self.items:
            self.status.set('No learned drafts found. Run the timer learner first.')
        else:
            suffix = f'  |  {invalid} invalid' if invalid else ''
            self.status.set(f'{len(self.items)} fight drafts  |  {ready} ready for live use{suffix}')
        if selected_iid is not None:
            self.table.selection_set(selected_iid)
            self.table.focus(selected_iid)
        self.selection_changed()

    def selected_item(self):
        selected = self.table.selection()
        return self.items[int(selected[0])] if selected else None

    def selection_changed(self, _event=None):
        item = self.selected_item()
        self.prepare_button.configure(
            state='normal' if item is not None and not item['error'] else 'disabled')
        if item is not None and item['error']:
            self.status.set(f"Invalid draft: {item['error']}")

    def prepare_selected(self):
        item = self.selected_item()
        if item is None or item['error']:
            return
        try:
            destination, payload = prepare_fight_module(
                item['draft_path'], self.module_directory)
        except (OSError, TypeError, ValueError) as error:
            messagebox.showerror('Could not prepare fight module', str(error), parent=self.root)
            return
        self.refresh(selected_draft_id=item['draft_id'])
        messagebox.showinfo(
            'Fight module ready',
            f"{payload['encounter']['name']} is ready for live encounter matching.\n\n"
            f'Saved to:\n{destination}\n\n'
            'Its timer will start only when the matching encounter begins.',
            parent=self.root,
        )


class MechanicDraftReviewWindow:
    """Show a learned draft and its evidence without enabling the timer script."""

    def __init__(self, parent, result, destination):
        self.result = result
        self.destination = Path(destination)
        self.root = tk.Toplevel(parent)
        self.root.title(f"Timer Draft Review - {result['encounter_name']}")
        self.root.geometry('1220x780')

        header = ttk.Frame(self.root, padding=10)
        header.pack(fill='x')
        ttk.Button(header, text='Export timer schema...',
                   command=self.export_timer).pack(side='right')
        ttk.Label(header, text=f"{result['encounter_name']} timer draft",
                  font=('', 14, 'bold')).pack(anchor='w')
        ttk.Label(
            header,
            text=(f"{result['pull_count']} selected pulls  |  Difficulty {result['difficulty']}  |  "
                  f"{len(result['accepted_abilities'])} accepted  |  "
                  f"{len(result['rejected_abilities'])} rejected"),
        ).pack(anchor='w', pady=(3, 0))
        analysis = result.get('analysis') or {}
        if analysis:
            ttk.Label(
                header,
                text=(f"Analysis mode: {analysis.get('mode', 'unknown')}  |  "
                      f"Workers: {analysis.get('worker_count', 1)}"),
            ).pack(anchor='w', pady=(3, 0))
        source_ids = ', '.join(
            f"{item['report_id']} #{item['fight_id']}" for item in result.get('source_fights', []))
        if source_ids:
            ttk.Label(header, text=f'Source fights: {source_ids}', wraplength=1170).pack(
                anchor='w', pady=(3, 0))
        ttk.Label(
            header,
            text='Review required. This draft is saved with enabled=false and is not used for callouts.',
            foreground='#a45b00',
        ).pack(anchor='w', pady=(3, 0))
        ttk.Label(header, text=f'Saved to: {self.destination}', wraplength=1170).pack(
            anchor='w', pady=(3, 0))

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill='both', expand=True, padx=10, pady=(0, 10))

        accepted_frame = ttk.Frame(notebook, padding=6)
        rejected_frame = ttk.Frame(notebook, padding=6)
        schedule_frame = ttk.Frame(notebook, padding=6)
        overlap_frame = ttk.Frame(notebook, padding=6)
        trigger_frame = ttk.Frame(notebook, padding=6)
        notebook.add(accepted_frame, text=f"Accepted ({len(result['accepted_abilities'])})")
        notebook.add(rejected_frame, text=f"Rejected ({len(result['rejected_abilities'])})")
        notebook.add(trigger_frame, text='Fight triggers (2)')
        notebook.add(schedule_frame, text=f"Draft schedule ({len(result['script']['mechanics'])})")
        notebook.add(overlap_frame, text=f"Stable overlaps ({len(result['stable_overlaps'])})")

        self._accepted_table(accepted_frame)
        self._rejected_table(rejected_frame)
        self._trigger_table(trigger_frame)
        self._schedule_table(schedule_frame)
        self._overlap_table(overlap_frame)

    @staticmethod
    def _percent(value):
        return '-' if value is None else f'{float(value) * 100:.0f}%'

    @staticmethod
    def _timing(value):
        if not value:
            return '-'
        median = value.get('median')
        deviation = value.get('p90_absolute_deviation')
        if median is None:
            return '-'
        return f'{median:.1f}s ±{float(deviation or 0):.1f}s p90'

    @staticmethod
    def _tree(parent, columns, settings):
        table = ttk.Treeview(parent, columns=columns, show='headings', selectmode='browse')
        for column, (label, width, anchor) in settings.items():
            table.heading(column, text=label)
            table.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(parent, orient='vertical', command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        return table

    def _accepted_table(self, parent):
        columns = ('ability', 'confidence', 'pulls', 'coverage', 'targets', 'health',
                   'first', 'repeat')
        settings = {
            'ability': ('Ability', 210, 'w'), 'confidence': ('Confidence', 85, 'center'),
            'pulls': ('Observed pulls', 105, 'center'), 'coverage': ('Coverage', 80, 'center'),
            'targets': ('Raid hit', 75, 'center'), 'health': ('Median health', 100, 'center'),
            'first': ('First occurrence', 150, 'center'),
            'repeat': ('Repeat interval', 155, 'center'),
        }
        table = self._tree(parent, columns, settings)
        for candidate in self.result['accepted_abilities']:
            evidence = candidate['evidence']
            table.insert('', 'end', values=(
                candidate['ability'], candidate.get('confidence', '-'),
                f"{evidence.get('observed_pulls', 0)}/{evidence.get('selected_pulls', 0)}",
                self._percent(evidence.get('pull_coverage')),
                self._percent(evidence.get('median_target_fraction')),
                self._percent(evidence.get('median_target_health_fraction')),
                self._timing(evidence.get('first_occurrence')),
                self._timing(evidence.get('repeat_interval')),
            ))

    def _rejected_table(self, parent):
        columns = ('ability', 'reasons', 'pulls', 'coverage', 'targets', 'health', 'first')
        settings = {
            'ability': ('Ability', 200, 'w'), 'reasons': ('Rejection reasons', 390, 'w'),
            'pulls': ('Observed pulls', 100, 'center'), 'coverage': ('Coverage', 75, 'center'),
            'targets': ('Raid hit', 75, 'center'), 'health': ('Median health', 100, 'center'),
            'first': ('First occurrence', 155, 'center'),
        }
        table = self._tree(parent, columns, settings)
        for candidate in self.result['rejected_abilities']:
            evidence = candidate.get('evidence') or {}
            reasons = ', '.join(reason.replace('_', ' ') for reason in candidate.get('reasons', []))
            table.insert('', 'end', values=(
                candidate['ability'], reasons or 'No accepted prediction',
                f"{evidence.get('observed_pulls', 0)}/{evidence.get('selected_pulls', self.result['pull_count'])}",
                self._percent(evidence.get('pull_coverage')),
                self._percent(evidence.get('median_target_fraction')),
                self._percent(evidence.get('median_target_health_fraction')),
                self._timing(evidence.get('first_occurrence')),
            ))

    def _schedule_table(self, parent):
        columns = ('time', 'kind', 'ability', 'message')
        settings = {
            'time': ('At', 85, 'center'), 'kind': ('Type', 165, 'center'),
            'ability': ('Ability ID', 100, 'center'), 'message': ('Callout', 720, 'w'),
        }
        table = self._tree(parent, columns, settings)
        for item in self.result['script']['mechanics']:
            seconds = float(item['time'])
            minute = int(seconds // 60)
            table.insert('', 'end', values=(
                f'{minute}:{seconds - minute * 60:04.1f}', item['kind'],
                item.get('ability_id') or ', '.join(map(str, item.get('ability_ids', []))),
                item['message'],
            ))

    def _trigger_table(self, parent):
        columns = ('phase', 'event', 'action', 'match', 'source', 'evidence')
        settings = {
            'phase': ('Boundary', 85, 'center'), 'event': ('Log event', 145, 'center'),
            'action': ('Action', 135, 'center'), 'match': ('Match', 180, 'w'),
            'source': ('Learned from', 235, 'w'), 'evidence': ('Evidence', 270, 'w'),
        }
        table = self._tree(parent, columns, settings)
        triggers = self.result['script'].get('encounter_triggers') or {}
        for phase in ('start', 'end'):
            trigger = triggers.get(phase) or {}
            match = trigger.get('match') or {}
            evidence = trigger.get('evidence') or {}
            counts = (f"{evidence.get('start_boundaries_observed', 0)} starts, "
                      f"{evidence.get('end_boundaries_observed', 0)} ends, "
                      f"{evidence.get('kills', 0)} kills, {evidence.get('wipes', 0)} wipes")
            table.insert('', 'end', values=(
                phase.title(), trigger.get('event_type', '-'), trigger.get('action', '-'),
                f"Encounter {match.get('encounter_id', '-')} / Difficulty "
                f"{match.get('difficulty', '-')}",
                trigger.get('observed_source', '-'), counts,
            ))

    def _overlap_table(self, parent):
        columns = ('abilities', 'pulls', 'coverage', 'separation')
        settings = {
            'abilities': ('Abilities', 500, 'w'), 'pulls': ('Common pulls', 120, 'center'),
            'coverage': ('Overlap coverage', 140, 'center'),
            'separation': ('Median separation', 150, 'center'),
        }
        table = self._tree(parent, columns, settings)
        for overlap in self.result['stable_overlaps']:
            table.insert('', 'end', values=(
                ' + '.join(overlap['abilities']), overlap['common_pulls'],
                self._percent(overlap['overlap_coverage']),
                f"{overlap['median_separation_seconds']:.1f}s",
            ))

    def export_timer(self):
        export_directory = Path(__file__).parent / 'timer-exports'
        export_directory.mkdir(parents=True, exist_ok=True)
        destination = filedialog.asksaveasfilename(
            parent=self.root,
            title='Export portable timer schema',
            initialdir=str(export_directory),
            initialfile=f"{self.result['draft_id']}.draft.timer.json",
            defaultextension='.timer.json',
            filetypes=[('Timer schema', '*.timer.json'), ('JSON files', '*.json')],
        )
        if not destination:
            return
        try:
            payload = build_timer_export(self.result, enabled=False)
            save_timer_export(payload, destination)
        except (OSError, TypeError, ValueError) as error:
            messagebox.showerror('Timer export failed', str(error), parent=self.root)
            return
        messagebox.showinfo(
            'Timer schema exported',
            f'Saved portable timer schema:\n{destination}\n\n'
            'The export remains disabled. Use Fight Modules from the main window to prepare '
            'a reviewed draft for live use.',
            parent=self.root)


class CrossReportLearnerWindow:
    """Choose one encounter across stored reports and learn one merged timer draft."""

    def __init__(self, parent, paths=None):
        self.root = tk.Toplevel(parent)
        self.root.title('Cross-report Timer Learner')
        self.root.geometry('1040x780')
        self.root.minsize(880, 650)
        self.paths = selectable_report_paths(paths or [])[:5]
        self.checked_report_keys = {report_path_key(path) for path in self.paths}
        self.scan_paths = []
        self.groups = []
        self.messages = queue.SimpleQueue()
        self.busy = False
        self.status = tk.StringVar()

        header = ttk.Frame(self.root, padding=12)
        header.pack(fill='x')
        ttk.Label(header, text='Cross-report timer learner', font=('', 15, 'bold')).pack(anchor='w')
        ttk.Label(
            header, wraplength=940, justify='left',
            text='Choose up to five Warcraft Logs report caches. Reports are shown newest '
                 'first. Scan when ready, then select an encounter group and run the learner.',
        ).pack(anchor='w', pady=(4, 0))

        reports = ttk.LabelFrame(self.root, text='Selected report caches', padding=8)
        reports.pack(fill='x', padx=12, pady=(0, 10))
        report_columns = ('use', 'report', 'modified', 'folder')
        self.report_table = ttk.Treeview(
            reports, columns=report_columns, show='headings', selectmode='extended', height=5)
        report_settings = {
            'use': ('Use', 55, 'center'),
            'report': ('Report file', 270, 'w'),
            'modified': ('Modified (newest first)', 205, 'center'),
            'folder': ('Folder', 420, 'w'),
        }
        for column, (label, width, anchor) in report_settings.items():
            self.report_table.heading(column, text=label)
            self.report_table.column(column, width=width, anchor=anchor)
        report_scroll = ttk.Scrollbar(reports, orient='vertical', command=self.report_table.yview)
        self.report_table.configure(yscrollcommand=report_scroll.set)
        self.report_table.grid(row=0, column=0, sticky='nsew')
        report_scroll.grid(row=0, column=1, sticky='ns')
        reports.columnconfigure(0, weight=1)
        self.report_table.bind('<<TreeviewSelect>>', self.report_selection_changed)
        self.report_table.bind('<Button-1>', self.toggle_report_checkbox, add='+')

        report_actions = ttk.Frame(reports)
        report_actions.grid(row=1, column=0, columnspan=2, sticky='ew', pady=(8, 0))
        self.add_report_button = ttk.Button(
            report_actions, text='Add report files...', command=self.add_report_files)
        self.add_report_button.pack(side='left')
        self.remove_report_button = ttk.Button(
            report_actions, text='Remove highlighted', command=self.remove_selected_reports,
            state='disabled')
        self.remove_report_button.pack(side='left', padx=(8, 0))
        self.select_all_button = ttk.Button(
            report_actions, text='Select all', command=self.select_all_reports,
            state='normal' if self.paths else 'disabled')
        self.select_all_button.pack(side='left', padx=(8, 0))
        self.select_none_button = ttk.Button(
            report_actions, text='Select none', command=self.select_no_reports,
            state='normal' if self.paths else 'disabled')
        self.select_none_button.pack(side='left', padx=(8, 0))
        self.clear_reports_button = ttk.Button(
            report_actions, text='Clear list', command=self.clear_reports,
            state='normal' if self.paths else 'disabled')
        self.clear_reports_button.pack(side='left', padx=(8, 0))
        ttk.Label(report_actions, text='Expected file type: *.wcl.json').pack(side='right')

        frame = ttk.LabelFrame(self.root, text='Matching encounter groups', padding=8)
        frame.pack(fill='both', expand=True)
        columns = ('encounter', 'difficulty', 'pulls', 'reports', 'kills', 'wipes')
        self.table = ttk.Treeview(frame, columns=columns, show='headings', selectmode='browse')
        settings = {
            'encounter': ('Encounter', 390, 'w'),
            'difficulty': ('Difficulty', 90, 'center'),
            'pulls': ('Pulls', 80, 'center'),
            'reports': ('Reports', 80, 'center'),
            'kills': ('Kills', 70, 'center'),
            'wipes': ('Wipes / open', 105, 'center'),
        }
        for column, (label, width, anchor) in settings.items():
            self.table.heading(column, text=label)
            self.table.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.selection_changed)

        controls = ttk.Frame(self.root, padding=12)
        controls.pack(fill='x')
        self.progress = ttk.Progressbar(controls, mode='indeterminate', length=170)
        self.progress.pack(side='left')
        ttk.Label(controls, textvariable=self.status, wraplength=560).pack(
            side='left', fill='x', expand=True, padx=10)
        self.scan_button = ttk.Button(controls, text='Scan reports', command=self.start_scan)
        self.scan_button.pack(side='right', padx=(8, 0))
        self.learn_button = ttk.Button(
            controls, text='Learn merged timer draft', command=self.learn, state='disabled')
        self.learn_button.pack(side='right')
        self.refresh_report_table()
        self.root.after(100, self.poll)

    def refresh_report_table(self):
        self.paths = selectable_report_paths(self.paths)
        valid_keys = {report_path_key(path) for path in self.paths}
        self.checked_report_keys.intersection_update(valid_keys)
        self.report_table.delete(*self.report_table.get_children())
        for index, path in enumerate(self.paths):
            checked = report_path_key(path) in self.checked_report_keys
            self.report_table.insert('', 'end', iid=str(index), values=(
                '☑' if checked else '☐', path.name, report_modified_text(path), str(path.parent),
            ))
        checked_count = len(self.checked_report_paths())
        self.scan_button.configure(
            state='normal' if checked_count and not self.busy else 'disabled')
        self.clear_reports_button.configure(
            state='normal' if self.paths and not self.busy else 'disabled')
        self.add_report_button.configure(
            state='normal' if len(self.paths) < 5 and not self.busy else 'disabled')
        self.select_all_button.configure(
            state='normal' if self.paths and not self.busy else 'disabled')
        self.select_none_button.configure(
            state='normal' if checked_count and not self.busy else 'disabled')
        self.report_selection_changed()
        if self.paths:
            self.status.set(f'{checked_count} of {len(self.paths)} reports selected. '
                            'Click a checkbox to include or exclude a report.')
        else:
            self.status.set('Add one or more Warcraft Logs .wcl.json report caches.')

    def checked_report_paths(self):
        return filter_checked_report_paths(self.paths, self.checked_report_keys)

    def clear_scan_results(self):
        self.groups = []
        self.scan_paths = []
        self.table.delete(*self.table.get_children())
        self.learn_button.configure(state='disabled')

    def reports_changed(self):
        self.clear_scan_results()
        self.refresh_report_table()

    def add_report_files(self):
        if self.busy or len(self.paths) >= 5:
            return
        initial_directory = self.paths[0].parent if self.paths else Path(__file__).parent
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title='Select Warcraft Logs report caches',
            initialdir=str(initial_directory),
            filetypes=[('Warcraft Logs report caches', '*.wcl.json')],
        )
        if not selected:
            return
        chosen = selectable_report_paths(selected)
        if len(chosen) != len(selected):
            messagebox.showerror(
                'Unsupported report file',
                'Only existing Warcraft Logs .wcl.json report caches can be selected.',
                parent=self.root,
            )
            return
        combined = selectable_report_paths([*self.paths, *chosen])
        if len(combined) > 5:
            messagebox.showerror(
                'Too many reports',
                'Select no more than five Warcraft Logs report caches.',
                parent=self.root,
            )
            return
        self.paths = combined
        self.checked_report_keys.update(report_path_key(path) for path in chosen)
        self.reports_changed()

    def toggle_report_checkbox(self, event):
        if self.busy or self.report_table.identify_region(event.x, event.y) != 'cell':
            return None
        if self.report_table.identify_column(event.x) != '#1':
            return None
        iid = self.report_table.identify_row(event.y)
        if not iid:
            return 'break'
        path = self.paths[int(iid)]
        key = report_path_key(path)
        if key in self.checked_report_keys:
            self.checked_report_keys.remove(key)
        else:
            self.checked_report_keys.add(key)
        self.clear_scan_results()
        self.refresh_report_table()
        return 'break'

    def select_all_reports(self):
        if self.busy:
            return
        self.checked_report_keys = {report_path_key(path) for path in self.paths}
        self.clear_scan_results()
        self.refresh_report_table()

    def select_no_reports(self):
        if self.busy:
            return
        self.checked_report_keys.clear()
        self.clear_scan_results()
        self.refresh_report_table()

    def report_selection_changed(self, _event=None):
        self.remove_report_button.configure(
            state='normal' if self.report_table.selection() and not self.busy else 'disabled')

    def remove_selected_reports(self):
        if self.busy:
            return
        selected = {int(iid) for iid in self.report_table.selection()}
        if not selected:
            return
        removed = [path for index, path in enumerate(self.paths) if index in selected]
        self.paths = [path for index, path in enumerate(self.paths) if index not in selected]
        self.checked_report_keys.difference_update(report_path_key(path) for path in removed)
        self.reports_changed()

    def clear_reports(self):
        if self.busy:
            return
        self.paths = []
        self.checked_report_keys.clear()
        self.reports_changed()

    def set_report_controls(self, enabled):
        self.report_table.configure(selectmode='extended' if enabled else 'none')
        self.add_report_button.configure(
            state='normal' if enabled and len(self.paths) < 5 else 'disabled')
        self.clear_reports_button.configure(
            state='normal' if enabled and self.paths else 'disabled')
        self.select_all_button.configure(
            state='normal' if enabled and self.paths else 'disabled')
        self.select_none_button.configure(
            state='normal' if enabled and self.checked_report_paths() else 'disabled')
        self.remove_report_button.configure(
            state='normal' if enabled and self.report_table.selection() else 'disabled')

    def start_scan(self):
        selected_paths = self.checked_report_paths()
        if self.busy or not selected_paths:
            return
        self.clear_scan_results()
        self.scan_paths = selected_paths
        selected = self.table.selection()
        if selected:
            self.table.selection_remove(*selected)
        self.busy = True
        self.set_report_controls(False)
        self.scan_button.configure(state='disabled')
        self.learn_button.configure(state='disabled')
        self.status.set(f'Reading encounter indexes from {len(self.scan_paths)} reports...')
        self.progress.start(10)
        threading.Thread(target=self.scan_groups, daemon=True).start()

    def scan_groups(self):
        try:
            self.messages.put(('groups', group_cross_report_encounters(self.scan_paths)))
        except Exception as error:
            self.messages.put(('error', f'Could not read the report indexes:\n{error}'))

    def selected_group(self):
        selected = self.table.selection()
        return self.groups[int(selected[0])] if selected else None

    def selection_changed(self, _event=None):
        group = self.selected_group()
        ready = group is not None and len(group['pulls']) >= 3 and not self.busy
        self.learn_button.configure(state='normal' if ready else 'disabled')
        if group is not None and len(group['pulls']) < 3:
            self.status.set('At least three matching pulls are required for conservative learning.')

    def set_busy(self, busy, message):
        self.busy = busy
        self.status.set(message)
        self.learn_button.configure(state='disabled')
        self.scan_button.configure(
            state='disabled' if busy or not self.checked_report_paths() else 'normal')
        self.set_report_controls(not busy)
        if busy:
            self.progress.start(10)
        else:
            self.progress.stop()
            self.selection_changed()

    def learn(self):
        if self.busy:
            return
        group = self.selected_group()
        if group is None or len(group['pulls']) < 3:
            return
        self.set_busy(True, f"Interpreting {len(group['paths'])} reports for {group['name']}...")

        def work():
            try:
                result = learn_timer_script(
                    group['paths'], group['encounter_id'], group['difficulty'],
                    progress=lambda text: self.messages.put(('progress', text)))
                destination = (Path(__file__).parent / 'mechanic-drafts' /
                               f"{result['draft_id']}.json")
                save_draft(result, destination)
                self.messages.put(('result', result, destination))
            except Exception as error:
                self.messages.put(('error', f'Could not learn the merged timer draft:\n{error}'))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == 'groups':
                    self.groups = message[1]
                    self.table.delete(*self.table.get_children())
                    for index, group in enumerate(self.groups):
                        kills = sum(1 for pull in group['pulls'] if pull.success)
                        self.table.insert('', 'end', iid=str(index), values=(
                            group['name'], group['difficulty'], len(group['pulls']),
                            len(group['paths']), kills, len(group['pulls']) - kills,
                        ))
                    self.busy = False
                    self.progress.stop()
                    self.scan_button.configure(
                        state='normal' if self.checked_report_paths() else 'disabled')
                    self.set_report_controls(True)
                    eligible = sum(1 for group in self.groups if len(group['pulls']) >= 3)
                    self.status.set(f'Found {len(self.groups)} encounter groups; '
                                    f'{eligible} have at least three pulls. Select one to learn.')
                elif message[0] == 'progress':
                    self.status.set(message[1])
                elif message[0] == 'result':
                    result, destination = message[1], message[2]
                    mode = result.get('analysis', {}).get('mode', 'analysis')
                    self.set_busy(False, f"Merged {result['pull_count']} pulls using {mode}.")
                    MechanicDraftReviewWindow(self.root, result, destination)
                elif message[0] == 'error':
                    self.set_busy(False, 'Cross-report learning failed.')
                    messagebox.showerror('Cross-report learner', message[1], parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self.poll)


class Launcher:
    def __init__(self, root, initial=None):
        self.root = root
        root.title('WoW Combat Log Tools')
        root.geometry('1020x470')
        root.minsize(900, 430)
        chosen = initial or default_log()
        self.path = tk.StringVar(value=str(chosen or ''))

        body = ttk.Frame(root, padding=22)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='WoW Combat Log Tools', font=('', 18, 'bold')).pack(anchor='w')
        ttk.Label(body, text='Choose a local log or import a Warcraft Logs report, then analyze its encounters.',
                  padding=(0, 4, 0, 18)).pack(anchor='w')
        path_row = ttk.Frame(body)
        path_row.pack(fill='x')
        ttk.Entry(path_row, textvariable=self.path).pack(side='left', fill='x', expand=True)
        ttk.Button(path_row, text='Browse...', command=self.browse).pack(side='left', padx=(8, 0))

        actions = ttk.Frame(body, padding=(0, 24, 0, 0))
        actions.pack(fill='both', expand=True)
        for column in range(3):
            actions.columnconfigure(column, weight=1, uniform='launcher-actions')
        actions.rowconfigure(0, weight=1)
        actions.rowconfigure(1, weight=1)
        viewer = ttk.Button(actions, text='Open live / replay viewer', command=self.open_viewer)
        viewer.grid(row=0, column=0, sticky='nsew', padx=(0, 6), pady=(0, 6))
        timeline = ttk.Button(actions, text='Timeline / player analysis', command=self.open_timeline)
        timeline.grid(row=0, column=1, sticky='nsew', padx=6, pady=(0, 6))
        importer = ttk.Button(actions, text='Import Warcraft Logs URL', command=self.open_importer)
        importer.grid(row=0, column=2, sticky='nsew', padx=(6, 0), pady=(0, 6))
        cross_report = ttk.Button(actions, text='Cross-report timer learner',
                                  command=self.open_cross_report_learner)
        cross_report.grid(row=1, column=0, sticky='nsew', padx=(0, 6), pady=(6, 0))
        fight_modules = ttk.Button(actions, text='Fight modules',
                                   command=self.open_fight_modules)
        fight_modules.grid(row=1, column=1, sticky='nsew', padx=6, pady=(6, 0))
        ttk.Label(body, text='View damage, healing, DPS/HPS, rank, and position, or export timeline CSV files.')\
            .pack(anchor='w', pady=(14, 0))

    def browse(self):
        chosen = filedialog.askopenfilename(
            parent=self.root, initialdir=str(Path(__file__).parent),
            filetypes=[('Supported logs', '*.txt *.wcl.json'), ('Combat logs', '*.txt'),
                       ('Warcraft Logs cache', '*.wcl.json'), ('All files', '*')])
        if chosen:
            self.path.set(chosen)

    def checked_path(self):
        path = Path(self.path.get().strip())
        if not path.is_file():
            messagebox.showerror('Log not found', 'Choose an existing combat-log file.', parent=self.root)
            return None
        return path

    def open_viewer(self):
        path = self.checked_path()
        if path is not None:
            if str(path).lower().endswith('.wcl.json'):
                messagebox.showinfo(
                    'Timeline analysis',
                    'Imported Warcraft Logs reports open in Timeline / player analysis. '
                    'The raw viewer is for local text combat logs.', parent=self.root)
                return
            Viewer(tk.Toplevel(self.root), path)

    def open_timeline(self):
        path = self.checked_path()
        if path is not None:
            TimelineWindow(tk.Toplevel(self.root), str(path))

    def open_importer(self):
        WarcraftLogsImportWindow(self.root, self.import_finished)

    def open_cross_report_learner(self):
        paths = sorted(
            Path(__file__).parent.glob('warcraft-logs-*.wcl.json'),
            key=lambda path: path.stat().st_mtime_ns, reverse=True)[:5]
        CrossReportLearnerWindow(self.root, paths)

    def open_fight_modules(self):
        FightModuleWindow(self.root)

    def import_finished(self, paths):
        paths = [Path(path) for path in paths]
        if not paths:
            return
        # Open the final report in the batch. Every earlier report remains available
        # through Browse and is included in the five-report cache retention policy.
        path = paths[-1]
        self.path.set(str(path))
        if len(paths) > 1:
            CrossReportLearnerWindow(self.root, paths)
        else:
            TimelineWindow(tk.Toplevel(self.root), str(path)).scan()


class WarcraftLogsImportWindow:
    """Collect API credentials and download a report without blocking Tk."""

    def __init__(self, parent, on_complete):
        self.root = tk.Toplevel(parent)
        self.root.title('Import Warcraft Logs Reports')
        self.root.geometry('700x560')
        self.root.minsize(650, 460)
        self.root.resizable(True, True)
        self.on_complete = on_complete
        self.messages = queue.SimpleQueue()
        self.busy = False
        client_id, client_secret = load_credentials()
        self.url_rows = []
        self.client_id = tk.StringVar(value=client_id)
        self.client_secret = tk.StringVar(value=client_secret)
        credential_status = ('Credentials loaded from .env or environment variables.'
                             if client_id and client_secret else
                             'Enter credentials below or add them to the project .env file.')
        self.status = tk.StringVar(value=credential_status)

        body = ttk.Frame(self.root, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='Import from Warcraft Logs', font=('', 15, 'bold')).pack(anchor='w')
        ttk.Label(
            body, wraplength=660, justify='left',
            text='Add up to five reports. The importer downloads them one at a time, including '
                 'every event for every boss pull, and saves local .wcl.json caches.',
            padding=(0, 5, 0, 14)).pack(anchor='w')
        form = ttk.Frame(body)
        form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='Report URL or code:').grid(row=0, column=0, sticky='nw', pady=5)
        self.report_rows = ttk.Frame(form)
        self.report_rows.grid(row=0, column=1, sticky='ew', padx=(10, 0), pady=2)
        self.report_rows.columnconfigure(0, weight=1)
        self.add_url_button = ttk.Button(form, text='+', width=3, command=self.add_url_row)
        self.add_url_button.grid(row=0, column=2, sticky='ne', padx=(6, 0), pady=4)
        self.add_url_row()
        ttk.Label(form, text='Client ID:').grid(row=1, column=0, sticky='w', pady=5)
        self.client_id_entry = ttk.Entry(form, textvariable=self.client_id)
        self.client_id_entry.grid(row=1, column=1, sticky='ew', padx=(10, 0), pady=5)
        ttk.Label(form, text='Client secret:').grid(row=2, column=0, sticky='w', pady=5)
        self.client_secret_entry = ttk.Entry(form, textvariable=self.client_secret, show='*')
        self.client_secret_entry.grid(row=2, column=1, sticky='ew', padx=(10, 0), pady=5)
        ttk.Label(
            body, wraplength=660, justify='left',
            text='Set WARCRAFT_LOGS_CLIENT_ID and WARCRAFT_LOGS_CLIENT_SECRET in .env, or '
                 'enter them above. Create a client at https://www.warcraftlogs.com/api/clients/ . '
                 'Client-credentials access works with public and unlisted reports; private reports '
                 'require user authorization and cannot be imported by this dialog.',
            padding=(0, 12, 0, 10)).pack(anchor='w')
        ttk.Label(body, textvariable=self.status, wraplength=660).pack(anchor='w', fill='x')
        controls = ttk.Frame(body, padding=(0, 12, 0, 0))
        controls.pack(fill='x')
        self.progress = ttk.Progressbar(controls, mode='indeterminate')
        self.progress.pack(side='left', fill='x', expand=True)
        self.button = ttk.Button(controls, text='Download and analyze', command=self.start)
        self.button.pack(side='right', padx=(12, 0))
        self.root.after(100, self.poll)

    def add_url_row(self):
        if self.busy or len(self.url_rows) >= 5:
            return
        variable = tk.StringVar()
        row = ttk.Frame(self.report_rows)
        row.pack(fill='x', pady=2)
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side='left', fill='x', expand=True)
        remove = None
        if self.url_rows:
            remove = ttk.Button(row, text='−', width=3,
                                command=lambda target=row: self.remove_url_row(target))
            remove.pack(side='left', padx=(5, 0))
        self.url_rows.append({'frame': row, 'variable': variable,
                              'entry': entry, 'remove': remove})
        self.add_url_button.configure(
            state='disabled' if len(self.url_rows) >= 5 else 'normal')
        entry.focus_set()

    def remove_url_row(self, target):
        if self.busy or len(self.url_rows) <= 1:
            return
        row = next((item for item in self.url_rows if item['frame'] is target), None)
        if row is None:
            return
        self.url_rows.remove(row)
        row['frame'].destroy()
        self.add_url_button.configure(state='normal')

    def set_input_state(self, enabled):
        state = 'normal' if enabled else 'disabled'
        for row in self.url_rows:
            row['entry'].configure(state=state)
            if row['remove'] is not None:
                row['remove'].configure(state=state)
        self.client_id_entry.configure(state=state)
        self.client_secret_entry.configure(state=state)
        self.add_url_button.configure(
            state='normal' if enabled and len(self.url_rows) < 5 else 'disabled')

    def start(self):
        if self.busy:
            return
        try:
            reports = prepare_report_batch(row['variable'].get() for row in self.url_rows)
        except ValueError as error:
            messagebox.showerror('Invalid report', str(error), parent=self.root)
            return
        if not self.client_id.get().strip() or not self.client_secret.get().strip():
            messagebox.showerror('Credentials required',
                                 'Enter the Warcraft Logs client ID and client secret.',
                                 parent=self.root)
            return
        client = WarcraftLogsClient(self.client_id.get(), self.client_secret.get())
        self.busy = True
        self.button.configure(state='disabled')
        self.set_input_state(False)
        self.progress.start(10)
        report_values = [value for value, _code in reports]

        def work():
            try:
                completed, failures = download_report_batch(
                    client, report_values, Path(__file__).parent,
                    lambda message: self.messages.put(('progress', message)))
                self.messages.put(('done', completed, failures))
            except Exception as error:
                self.messages.put(('error', str(error)))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                message = self.messages.get_nowait()
                if message[0] == 'progress':
                    self.status.set(message[1])
                elif message[0] == 'done':
                    completed, failures = message[1], message[2]
                    self.progress.stop()
                    if not completed:
                        self.busy = False
                        self.button.configure(state='normal')
                        self.set_input_state(True)
                        self.status.set('No reports were imported.')
                        details = '\n'.join(
                            f"{item['report_code']}: {item['error']}" for item in failures)
                        messagebox.showerror('Warcraft Logs import failed', details,
                                             parent=self.root)
                        continue
                    self.status.set(f'Imported {len(completed)} report(s).')
                    summary = '\n'.join(path.name for path in completed)
                    if failures:
                        failed = '\n'.join(
                            f"{item['report_code']}: {item['error']}" for item in failures)
                        next_step = ('The cross-report learner will open next.'
                                     if len(completed) > 1 else
                                     'The successful report will open next.')
                        messagebox.showwarning(
                            'Batch import completed with errors',
                            f'Imported:\n{summary}\n\nFailed:\n{failed}\n\n'
                            f'{next_step}', parent=self.root)
                    elif len(completed) > 1:
                        messagebox.showinfo(
                            'Batch import complete',
                            f'Imported {len(completed)} reports:\n{summary}\n\n'
                            'The cross-report timer learner will open next.',
                            parent=self.root)
                    self.client_secret.set('')
                    self.on_complete(completed)
                    self.root.destroy()
                    return
                elif message[0] == 'error':
                    self.busy = False
                    self.progress.stop()
                    self.button.configure(state='normal')
                    self.set_input_state(True)
                    self.status.set('Import failed.')
                    messagebox.showerror('Warcraft Logs import failed', message[1], parent=self.root)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self.poll)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', nargs='?', type=Path, help='Log file to preselect')
    args = parser.parse_args()
    root = tk.Tk()
    Launcher(root, args.log)
    root.mainloop()


if __name__ == '__main__':
    main()
