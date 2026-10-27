"""Build a deterministic, checksummed Windows profile/protocol distribution."""

import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from .live_callouts import (CalloutProfileRegistry, DEFAULT_PROFILE_DIRECTORY,
                           MAX_PROFILE_BYTES, MAX_PROFILES, validate_callout_profile)


BUNDLE_VERSION = '1.0.0'
ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT.parent / '.carter_data' / 'companion-distribution'


def build_bundle(output=DEFAULT_OUTPUT, guild_storage=None):
    sources = [DEFAULT_PROFILE_DIRECTORY]
    if guild_storage is not None:
        sources.append(Path(guild_storage) / 'callout-profiles')
    registry = CalloutProfileRegistry(sources)
    if registry.status()['errors']:
        raise ValueError('Resolve profile validation errors before distributing the catalog.')
    files = {}
    profiles = []
    # Keep all validated candidates, including overrides; manifest source ordering
    # tells Windows to apply guild overrides after packaged profiles, just as Carter does.
    count = 0
    for source_index, directory in enumerate(sources):
        for path in sorted(directory.glob('*.callout.json')):
            count += 1
            if count > MAX_PROFILES or path.is_symlink() or path.stat().st_size > MAX_PROFILE_BYTES:
                raise ValueError('Unsafe or oversized profile catalog.')
            data = path.read_bytes()
            profile = validate_callout_profile(json.loads(data))
            name = f'profiles/{source_index}/{path.name}'
            files[name] = data
            profiles.append({'path': name, 'source_priority': source_index,
                             'profile_id': profile['profile_id'],
                             'profile_revision': profile['profile_revision'],
                             'encounter_id': profile['encounter_id'],
                             'difficulty_ids': profile['difficulty_ids']})
    for name in ('callout_profile.schema.json', 'companion_events.schema.json',
                 'companion_response.schema.json'):
        files[f'schemas/{name}'] = (ROOT / name).read_bytes()
    for name in ('live_callouts.py', 'combat_log_core.py'):
        files[f'reference/{name}'] = (ROOT / name).read_bytes()
    files['PROTOCOL.md'] = (ROOT / 'COMPANION_PROTOCOL.md').read_bytes()
    files['INSTALL.md'] = (
        '# Windows installation\n\n'
        'Verify the ZIP sidecar SHA-256 through a trusted transfer. Extract to a staging folder.\n'
        'Verify every manifest file length/hash before loading it. Validate all profiles with\n'
        'schemas/callout_profile.schema.json and reference/live_callouts.py. Load profile\n'
        'sources in manifest source_priority order (profiles/0, then profiles/1 if supplied).\n'
        'Activate atomically between fights and retain the last working bundle.\n\n'
        'reference/combat_log_core.py and reference/live_callouts.py are dependency-free\n'
        'examples of the exact server matcher. ObservedCalloutEngine.start accepts an object\n'
        'with encounter_id, difficulty, session_id, attempt_id, attempt, encounter_name and\n'
        'log_start_timestamp attributes. Map its MECHANIC_CALLOUT output to the wire\n'
        'MECHANIC_OBSERVED identifiers; never send its speech text. Read PROTOCOL.md.\n\n'
        'Configure HTTPS, guild ID and the existing private guild live token. No Discord\n'
        'credentials are needed. On Carter select /logs live mode:on source:companion and\n'
        'join voice with /carter join. The old bulk Test button does not test this feed.\n'
    ).encode()
    manifest = {'format': 'carter-companion-bundle/v1', 'bundle_version': BUNDLE_VERSION,
                'event_schema_version': 1, 'profiles': profiles,
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data in sorted(files.items())}}
    files['manifest.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f'carter-companion-{BUNDLE_VERSION}.zip'
    temporary = destination.with_suffix('.zip.part')
    with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    temporary.replace(destination)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix('.zip.sha256').write_text(
        f'{checksum}  {destination.name}\n', encoding='ascii')
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--guild-storage', type=Path)
    args = parser.parse_args()
    print(build_bundle(args.output, args.guild_storage))


if __name__ == '__main__':
    main()
