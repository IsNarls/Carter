"""Stream and replay WoW combat logs using only Python's standard library."""

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


STAMP = re.compile(
    r'^(\d{1,2})/(\d{1,2})/(?:(\d{4}) )?(\d{1,2}):(\d{2}):(\d{2})'
    r'\.(\d+)([+-]\d{1,2}(?::\d{2})?)?\s{2,}(.*)$'
)
MAX_ROWS = 2500


def decode_log_line(raw):
    """WoW lines can mix UTF-8 names with isolated Windows-1252 bytes."""
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        mixed = raw.decode('utf-8', errors='surrogateescape')
        text = ''.join(
            bytes((ord(character) - 0xDC00,)).decode('cp1252', errors='replace')
            if 0xDC80 <= ord(character) <= 0xDCFF else character
            for character in mixed
        )
        return text.removeprefix('\ufeff')


@dataclass
class Event:
    timestamp: float | None
    clock: str
    kind: str
    source: str
    target: str
    ability: str
    raw: str
    fields: tuple[str, ...] = ()


def parse_event(line, year=None):
    """Keep unknown records visible; decode common combat-event headers."""
    match = STAMP.match(line)
    if not match:
        return Event(None, '', 'UNPARSED', '', '', '', line)
    month, day, stamp_year, hour, minute, second, fraction, offset, payload = match.groups()
    timestamp = None
    try:
        offset_parts = (offset or '+0').split(':')
        minutes = int(offset_parts[0]) * 60
        if len(offset_parts) == 2:
            minutes += int(offset_parts[1]) * (-1 if offset.startswith('-') else 1)
        timestamp = datetime(
            int(stamp_year or year or datetime.now().year), int(month), int(day),
            int(hour), int(minute), int(second), int((fraction + '000000')[:6]),
            tzinfo=timezone(timedelta(minutes=minutes)),
        ).timestamp()
    except ValueError:
        pass
    try:
        fields = next(csv.reader([payload]))
    except (csv.Error, StopIteration):
        fields = [payload]
    kind = fields[0] if fields else 'UNKNOWN'
    source = target = ability = ''
    # Common combat records: event, source GUID/name/flags/raid flags,
    # destination GUID/name/flags/raid flags, then event-specific fields.
    combat = kind.startswith(('SPELL_', 'RANGE_', 'SWING_', 'DAMAGE_', 'ENVIRONMENTAL_', 'UNIT_'))
    if combat and len(fields) >= 9:
        source = '' if fields[2] == 'nil' else fields[2]
        target = '' if fields[6] == 'nil' else fields[6]
        if kind.startswith(('SPELL_', 'RANGE_', 'DAMAGE_SHIELD', 'DAMAGE_SPLIT')) and len(fields) > 10:
            ability = fields[10]
        elif kind.startswith('SWING_'):
            ability = 'Melee'
        elif kind == 'ENVIRONMENTAL_DAMAGE' and len(fields) > 9:
            ability = fields[9]
    elif kind in ('ENCOUNTER_START', 'ENCOUNTER_END', 'ZONE_CHANGE', 'MAP_CHANGE') and len(fields) > 2:
        ability = fields[2]
    return Event(timestamp, f'{hour}:{minute}:{second}.{fraction}', kind,
                 source, target, ability, line, tuple(fields))


class LogReader:
    """Binary offsets allow retrying an incomplete line in a growing log."""

    def __init__(self, path, at_end=False):
        self.file = open(path, 'rb')
        if at_end:
            self.file.seek(0, 2)

    def read(self, follow=False):
        position = self.file.tell()
        line = self.file.readline()
        if not line:
            if follow:
                self.file.seek(0, 2)
                if self.file.tell() < position:
                    self.file.seek(0)
                else:
                    self.file.seek(position)
            return None
        if follow and not line.endswith(b'\n'):
            self.file.seek(position)
            return None
        return parse_event(decode_log_line(line).rstrip('\r\n'))

    def close(self):
        self.file.close()


class Viewer:
    def __init__(self, root, initial=None):
        self.root = root
        root.title('WoW Combat Log Viewer')
        root.geometry('1250x760')
        self.reader = None
        self.pending = None
        self.running = False
        self.virtual_time = None
        self.last_tick = time.monotonic()
        self.processed = 0
        self.records = {}
        self.path = tk.StringVar(value=str(initial or ''))
        self.mode = tk.StringVar(value='Replay')
        self.speed = tk.StringVar(value='1')
        self.query = tk.StringVar()
        self.from_end = tk.BooleanVar(value=True)
        self.skip_gaps = tk.BooleanVar(value=True)
        self.scroll = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='Choose a log, then press Start.')

        top = ttk.Frame(root, padding=8)
        top.pack(fill='x')
        ttk.Entry(top, textvariable=self.path).pack(side='left', fill='x', expand=True)
        ttk.Button(top, text='Browse…', command=self.browse).pack(side='left', padx=6)
        controls = ttk.Frame(root, padding=(8, 0, 8, 8))
        controls.pack(fill='x')
        ttk.Combobox(controls, textvariable=self.mode, values=('Replay', 'Follow live'),
                     state='readonly', width=12).pack(side='left')
        ttk.Label(controls, text='  Speed: ').pack(side='left')
        ttk.Combobox(controls, textvariable=self.speed, values=('0.25', '1', '2', '5', '10', '50'),
                     state='readonly', width=5).pack(side='left')
        ttk.Checkbutton(controls, text='Skip idle gaps > 5s', variable=self.skip_gaps).pack(side='left', padx=8)
        ttk.Checkbutton(controls, text='Live: start at end', variable=self.from_end).pack(side='left')
        ttk.Button(controls, text='Start / Restart', command=self.start).pack(side='left', padx=8)
        self.pause_button = ttk.Button(controls, text='Pause', command=self.pause)
        self.pause_button.pack(side='left')
        search = ttk.Frame(root, padding=(8, 0, 8, 8))
        search.pack(fill='x')
        ttk.Label(search, text='Filter incoming lines: ').pack(side='left')
        ttk.Entry(search, textvariable=self.query).pack(side='left', fill='x', expand=True)
        ttk.Checkbutton(search, text='Auto-scroll', variable=self.scroll).pack(side='left', padx=8)

        pane = ttk.Panedwindow(root, orient='vertical')
        pane.pack(fill='both', expand=True, padx=8)
        table_frame = ttk.Frame(pane)
        columns = ('time', 'event', 'source', 'target', 'ability')
        self.table = ttk.Treeview(table_frame, columns=columns, show='headings', selectmode='browse')
        for name, width in zip(columns, (130, 245, 240, 240, 260)):
            self.table.heading(name, text=name.title())
            self.table.column(name, width=width, minwidth=80)
        vertical = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.table.grid(row=0, column=0, sticky='nsew')
        vertical.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        pane.add(table_frame, weight=4)
        detail_frame = ttk.LabelFrame(pane, text='Selected event — original log line')
        self.detail = tk.Text(detail_frame, height=6, wrap='word', state='disabled')
        self.detail.pack(fill='both', expand=True)
        pane.add(detail_frame, weight=1)
        self.table.bind('<<TreeviewSelect>>', self.show_detail)
        ttk.Label(root, textvariable=self.status, padding=8).pack(fill='x')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(30, self.tick)

    def browse(self):
        path = filedialog.askopenfilename(initialdir=str(Path(__file__).parent),
                                          filetypes=[('Combat logs', '*.txt'), ('All files', '*')])
        if path:
            self.path.set(path)

    def start(self):
        follow = self.mode.get() == 'Follow live'
        try:
            reader = LogReader(self.path.get(), at_end=follow and self.from_end.get())
        except OSError as error:
            messagebox.showerror('Cannot open log', str(error))
            return
        if self.reader:
            self.reader.close()
        self.reader = reader
        self.active_follow = follow
        self.pending = None
        self.virtual_time = None
        self.processed = 0
        self.records.clear()
        self.table.delete(*self.table.get_children())
        self.set_detail('')
        self.running = True
        self.last_tick = time.monotonic()
        self.pause_button.configure(text='Pause')

    def pause(self):
        if self.reader:
            self.running = not self.running
            self.last_tick = time.monotonic()
            self.pause_button.configure(text='Pause' if self.running else 'Resume')
            if not self.running:
                self.status.set(f'Paused | {self.processed:,} events read')

    def tick(self):
        now = time.monotonic()
        elapsed = now - self.last_tick
        self.last_tick = now
        if self.running and self.reader:
            try:
                self.pump(elapsed)
            except (OSError, ValueError) as error:
                self.running = False
                self.status.set(f'Read failed: {error}')
        self.root.after(30, self.tick)

    def pump(self, elapsed):
        if self.virtual_time is not None:
            self.virtual_time += elapsed * float(self.speed.get())
        query = self.query.get().casefold()
        added = []
        state = 'Watching for new lines' if self.active_follow else 'Replaying'
        # A bounded batch keeps the UI responsive even during a dense raid pull.
        for _ in range(200):
            if self.pending is None:
                self.pending = self.reader.read(follow=self.active_follow)
            event = self.pending
            if event is None:
                if not self.active_follow:
                    state = 'Replay finished'
                    self.running = False
                break
            if not self.active_follow and event.timestamp is not None:
                if self.virtual_time is None:
                    self.virtual_time = event.timestamp
                if self.skip_gaps.get() and event.timestamp - self.virtual_time > 5:
                    self.virtual_time = event.timestamp
                if event.timestamp > self.virtual_time:
                    break
            self.pending = None
            self.processed += 1
            if query and query not in event.raw.casefold():
                continue
            iid = self.table.insert('', 'end', values=(event.clock, event.kind, event.source,
                                                        event.target, event.ability))
            self.records[iid] = event.raw
            added.append(iid)
        rows = self.table.get_children()
        excess = len(rows) - MAX_ROWS
        if excess > 0:
            for iid in rows[:excess]:
                self.records.pop(iid, None)
            self.table.delete(*rows[:excess])
        if added and self.scroll.get():
            self.table.see(added[-1])
        self.status.set(f'{state} | {self.processed:,} events read | '
                        f'{len(self.records):,} displayed (latest {MAX_ROWS:,} matching events)')

    def set_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def show_detail(self, _event=None):
        selected = self.table.selection()
        if selected:
            self.set_detail(self.records.get(selected[0], ''))

    def close(self):
        if self.reader:
            self.reader.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', nargs='?', type=Path, help='Log file to preselect')
    args = parser.parse_args()
    root = tk.Tk()
    Viewer(root, args.log)
    root.mainloop()


if __name__ == '__main__':
    main()
