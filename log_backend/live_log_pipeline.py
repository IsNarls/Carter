"""Reliably stream appended WoW combat-log lines to Carter's analysis backend."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_BATCH_BYTES = 256 * 1024
TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{40,128}$')
GUILD_PATTERN = re.compile(r'^[1-9][0-9]{0,19}$')
IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9._-]{8,128}$')
STATE_VERSION = 2


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep live credentials on the explicitly configured origin."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_HTTP_OPENER = build_opener(_NoRedirectHandler())


def _atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.part')
    temporary.write_text(json.dumps(payload, separators=(',', ':')), encoding='utf-8')
    temporary.replace(path)


def _load_state(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            return None
        stream_id = value.get('stream_id')
        offset = value.get('offset')
        if not IDENTIFIER_PATTERN.fullmatch(str(stream_id or '')):
            return None
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            return None
        if value.get('version') == STATE_VERSION:
            file_id = value.get('file_id')
            if not IDENTIFIER_PATTERN.fullmatch(str(file_id or '')):
                return None
            return {
                'version': STATE_VERSION,
                'file_id': file_id,
                'stream_id': stream_id,
                'offset': offset,
            }
        # Version 1 used the physical file identity as its stream identity.
        if value.get('version') == 1:
            return {'version': 1, 'stream_id': stream_id, 'offset': offset}
    except (OSError, ValueError, TypeError):
        pass
    return None


def _identity(path, stat=None):
    stat = stat or path.stat()
    material = f'{path.resolve()}:{getattr(stat, "st_dev", 0)}:{getattr(stat, "st_ino", 0)}'
    # Modern Python exposes a stable file index as st_ino on Windows. Keep a
    # creation-time fallback for filesystems that report zero instead.
    if not getattr(stat, 'st_ino', 0):
        created = getattr(stat, 'st_birthtime_ns', None)
        if created is None and os.name == 'nt':
            created = getattr(stat, 'st_ctime_ns', 0)
        material += f':{created or 0}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]


def _new_stream_id():
    return secrets.token_hex(16)


def _state_payload(file_id, stream_id, offset):
    return {
        'version': STATE_VERSION,
        'file_id': file_id,
        'stream_id': stream_id,
        'offset': offset,
    }


def _initial_cursor(log_path, state, from_start=False):
    """Return a resumable v2 cursor for the file currently at ``log_path``."""
    stat = log_path.stat()
    file_id = _identity(log_path, stat)
    size = stat.st_size
    if state:
        same_file = (
            state.get('file_id') == file_id if state.get('version') == STATE_VERSION
            else state.get('stream_id') == file_id
        )
        if same_file and state['offset'] <= size:
            return file_id, state['stream_id'], state['offset']
        # A persisted cursor plus a different or shorter file is a rotation,
        # including an in-place truncation whose physical ID did not change.
        return file_id, _new_stream_id(), 0
    return file_id, _new_stream_id(), 0 if from_start else size


def _read_token(token_file):
    if token_file:
        token = Path(token_file).read_text(encoding='ascii').strip()
    else:
        token = os.environ.get('CARTER_LIVE_ACCESS_TOKEN', '').strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError('Provide a valid token file or CARTER_LIVE_ACCESS_TOKEN.')
    return token


def _endpoint(base_url):
    value = str(base_url or '').strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('Provide an HTTP(S) origin URL or an /api/v1 base URL.')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('The Carter URL has an invalid port.') from error
    path = parsed.path.rstrip('/')
    if path in {'', '/'}:
        endpoint_path = '/api/v1/live/ingest'
    elif path in {'/api/v1', '/api/v1/live/ingest'}:
        endpoint_path = '/api/v1/live/ingest'
    else:
        raise ValueError('The Carter URL path must be empty or /api/v1.')
    return urlunsplit((parsed.scheme, parsed.netloc, endpoint_path, '', ''))


def _validate_transport(url, allow_insecure):
    endpoint = _endpoint(url)
    parsed = urlsplit(endpoint)
    if parsed.scheme == 'https':
        return endpoint
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.casefold() == 'localhost'
    if parsed.scheme == 'http' and loopback:
        return endpoint
    if parsed.scheme == 'http' and allow_insecure:
        return endpoint
    raise ValueError('Use HTTPS (or --allow-insecure only on a trusted private network).')


def send_batch(url, guild_id, token, stream_id, offset, payload, timeout=20):
    request = Request(_endpoint(url), data=payload, method='POST', headers={
        'Content-Type': 'application/octet-stream',
        'X-Carter-Guild-ID': guild_id,
        'X-Carter-Live-Token': token,
        'X-Carter-Live-Stream-ID': stream_id,
        'X-Carter-Live-Offset': str(offset),
    })
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        try:
            payload = json.loads(error.read())
        except (ValueError, OSError):
            payload = {}
        return error.code, payload


def _complete_batch(stream, limit=MAX_BATCH_BYTES):
    start = stream.tell()
    data = stream.read(limit)
    if not data:
        return b''
    newline = data.rfind(b'\n')
    if newline < 0:
        stream.seek(start)
        return b''
    complete = data[:newline + 1]
    stream.seek(start + len(complete))
    return complete


def run(args):
    log_path = args.log.resolve()
    if not GUILD_PATTERN.fullmatch(args.guild_id):
        raise ValueError('A valid Discord guild ID is required.')
    endpoint = _validate_transport(args.url, args.allow_insecure)
    token = _read_token(args.token_file)
    state_path = (args.state or log_path.with_name('.carter-live-state.json')).resolve()
    retry_delay = max(0.25, args.retry_delay)

    while not log_path.is_file():
        if args.once:
            raise FileNotFoundError(f'Combat log not found: {log_path}')
        print('Waiting for combat log...')
        time.sleep(retry_delay)

    state = _load_state(state_path)
    file_id, stream_id, offset = _initial_cursor(log_path, state, args.from_start)
    _atomic_json(state_path, _state_payload(file_id, stream_id, offset))
    print(f'Carter live pipeline ready; offset={offset}.')

    while True:
        try:
            stat = log_path.stat()
            current_id = _identity(log_path, stat)
            current_size = stat.st_size
        except OSError:
            if args.once:
                raise
            print('Combat log temporarily unavailable; waiting for it to return.')
            time.sleep(retry_delay)
            continue
        if current_id != file_id or current_size < offset:
            file_id, stream_id, offset = current_id, _new_stream_id(), 0
            _atomic_json(state_path, _state_payload(file_id, stream_id, offset))
        try:
            with log_path.open('rb') as source:
                # Do not read a replacement that raced with the preceding stat.
                if _identity(log_path, os.fstat(source.fileno())) != current_id:
                    continue
                source.seek(offset)
                batch = _complete_batch(source)
        except OSError:
            if args.once:
                raise
            print('Combat log temporarily unavailable; retrying.')
            time.sleep(retry_delay)
            continue
        if not batch:
            if args.once:
                return 0
            time.sleep(max(0.05, args.poll_interval))
            continue
        try:
            status, response = send_batch(endpoint, args.guild_id, token, stream_id,
                                          offset, batch, args.timeout)
        except (OSError, URLError, TimeoutError):
            print('Carter unavailable; retrying the same batch.')
            if args.once:
                return 1
            time.sleep(retry_delay)
            continue
        if status == 409 and isinstance(response.get('next_offset'), int):
            offset = response['next_offset']
        elif status == 200 and isinstance(response.get('next_offset'), int):
            offset = response['next_offset']
            print(f'Sent {response.get("accepted", 0)} lines; offset={offset}.')
        else:
            code = response.get('error', {}).get('code', f'HTTP_{status}')
            print(f'Upload rejected: {code}.')
            if args.once or status in {400, 401, 403, 413}:
                return 1
            time.sleep(retry_delay)
            continue
        _atomic_json(state_path, _state_payload(file_id, stream_id, offset))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, required=True, help='Path to WoWCombatLog.txt')
    parser.add_argument('--url', required=True, help='Carter analysis backend base URL')
    parser.add_argument('--guild-id', required=True, help='Discord guild owning this log')
    parser.add_argument('--token-file', type=Path,
                        help='File containing the guild live token (preferred)')
    parser.add_argument('--state', type=Path, help='Persistent cursor file')
    parser.add_argument('--from-start', action='store_true', help='Read an existing log from byte 0')
    parser.add_argument('--once', action='store_true', help='Send available complete lines and exit')
    parser.add_argument('--allow-insecure', action='store_true',
                        help='Allow HTTP to a non-loopback host on a trusted private network')
    parser.add_argument('--poll-interval', type=float, default=0.2)
    parser.add_argument('--retry-delay', type=float, default=3.0)
    parser.add_argument('--timeout', type=float, default=20.0)
    args = parser.parse_args()
    try:
        raise SystemExit(run(args))
    except (OSError, ValueError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()
