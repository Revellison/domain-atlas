import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fetch_domains import SOURCE_URL, download, validate_csv


class SourceTests(unittest.TestCase):
    def test_csv_bom_header_normalization_and_deduplication(self):
        domains = validate_csv('\ufeffdomain\nExample.COM\nwww.example.com\nexample.com\n'.encode(), 2)
        self.assertEqual(domains, ['example.com', 'www.example.com'])

    def test_empty_html_and_invalid_data_are_rejected(self):
        for raw in [b'', b'<!DOCTYPE html><html>Gateway error</html>',
                    b'{"error":"blocked"}', b'example.com\nnot a domain\n']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validate_csv(raw, 1)
        with self.assertRaises(ValueError):
            validate_csv(b'example.com\n', 100)

    def test_download_records_source_and_preserves_raw_csv(self):
        raw = b'domain\nExample.COM\nwww.example.com\nexample.com\n'
        def curl(command, **kwargs):
            self.assertEqual(command[-1], SOURCE_URL)
            self.assertIn('--retry-all-errors', command)
            self.assertEqual(kwargs['timeout'], 180)
            Path(command[command.index('--output') + 1]).write_bytes(raw)
        with tempfile.TemporaryDirectory() as directory, patch('fetch_domains.subprocess.run', side_effect=curl):
            path = Path(directory) / 'domains.csv'
            metadata = download(path, 2)
            self.assertEqual(path.read_text(), 'example.com\nwww.example.com\n')
            self.assertEqual(path.with_suffix('.source.csv').read_bytes(), raw)
            self.assertEqual(json.loads(path.with_suffix('.source.json').read_text())['domains'], 2)
            self.assertEqual(metadata['url'], SOURCE_URL)

    def test_network_failure_never_falls_back_to_an_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'domains.csv'
            path.write_text('old.example\n')
            error = subprocess.CalledProcessError(22, ['curl'], stderr='HTTP 503')
            with patch('fetch_domains.subprocess.run', side_effect=error), self.assertRaises(subprocess.CalledProcessError):
                download(path, 1)
            self.assertEqual(path.read_text(), 'old.example\n')
            self.assertFalse(path.with_suffix('.source.json').exists())


if __name__ == '__main__':
    unittest.main()
