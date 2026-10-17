import json
import tempfile
import unittest
from pathlib import Path

from prepare_pages import prepare, ROOT


class PagesTests(unittest.TestCase):
    def test_exact_release_bytes_and_same_origin_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.dumps({'schema_version': 1, 'summary': {'domains': 1},
                              'domains': [{'domain': 'example.com'}]}).encode()
            payload = root / 'release.json'
            payload.write_bytes(raw)
            output = root / 'pages'
            prepare(payload, 'owner/repo', 'snapshot-1', output)
            self.assertEqual((output / 'data' / 'domains.json').read_bytes(), raw)
            config_text = (output / 'config.js').read_text(encoding='utf-8')
            config = json.loads(config_text.removeprefix('window.DOMAIN_CONFIG = ').strip().removesuffix(';'))
            self.assertEqual(config['dataUrl'], './data/domains.json')
            self.assertEqual(config['releaseUrl'], 'https://github.com/owner/repo/releases/tag/snapshot-1')
            self.assertTrue((output / '.nojekyll').exists())
            self.assertEqual((output / 'app.js').read_bytes(), (ROOT / 'site' / 'app.js').read_bytes())

    def test_invalid_snapshot_does_not_replace_good_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = root / 'release.json'
            payload.write_text('{"schema_version":1,"domains":[],"summary":{"domains":0}}')
            output = root / 'pages'
            output.mkdir()
            (output / 'index.html').write_text('existing site')
            with self.assertRaises(ValueError):
                prepare(payload, 'owner/repo', 'snapshot-1', output)
            self.assertEqual((output / 'index.html').read_text(), 'existing site')

    def test_source_directory_is_never_overwritten(self):
        with self.assertRaises(ValueError):
            prepare(ROOT / 'site/data/domains.json', 'owner/repo', 'snapshot-1', ROOT / 'site')


if __name__ == '__main__':
    unittest.main()
