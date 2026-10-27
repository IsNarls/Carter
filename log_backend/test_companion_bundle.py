import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from log_backend.build_companion_bundle import build_bundle
from log_backend.live_callouts import validate_callout_profile


class BundleTests(unittest.TestCase):
    def test_bundle_checksums_profiles_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory:
            path = build_bundle(directory)
            first = path.read_bytes()
            sidecar = path.with_suffix('.zip.sha256').read_text().split()[0]
            self.assertEqual(sidecar, hashlib.sha256(first).hexdigest())
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['event_schema_version'], 1)
                self.assertEqual(len(manifest['profiles']), 2)
                for name, info in manifest['files'].items():
                    data = archive.read(name)
                    self.assertEqual(len(data), info['bytes'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), info['sha256'])
                for profile in manifest['profiles']:
                    validated = validate_callout_profile(json.loads(archive.read(profile['path'])))
                    self.assertEqual(validated['profile_revision'], profile['profile_revision'])
                self.assertEqual(set(archive.namelist()), set(manifest['files']) | {'manifest.json'})
            self.assertEqual(build_bundle(directory).read_bytes(), first)

    def test_guild_overrides_are_included_in_source_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile_root = root / 'guild' / 'callout-profiles'
            profile_root.mkdir(parents=True)
            packaged = Path(__file__).parent / 'callout_profiles' / 'nekzali.callout.json'
            profile = json.loads(packaged.read_text())
            profile['profile_revision'] = 2
            (profile_root / 'override.callout.json').write_text(json.dumps(profile))
            path = build_bundle(root / 'out', root / 'guild')
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['profiles'][-1]['profile_revision'], 2)
                self.assertEqual(manifest['profiles'][-1]['source_priority'], 1)


if __name__ == '__main__':
    unittest.main()
