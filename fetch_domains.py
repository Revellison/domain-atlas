"""Download and validate the current Cheburcheck domain list before scanning."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from domain_report import read_input

SOURCE_URL = 'https://cheburcheck.ru/whitelist/domains.csv'
MAX_BYTES = 10_000_000


def validate_csv(raw, minimum=100):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError('CSV пуст или превышает 10 MB')
    text = raw.decode('utf-8-sig')
    if text.lstrip().lower().startswith(('<!doctype', '<html', '<head', '<body')):
        raise ValueError('Источник вернул HTML вместо CSV')
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'domains.csv'
        path.write_bytes(raw)
        domains, invalid = read_input(path, 1, 0)
    if invalid:
        raise ValueError(f'CSV содержит некорректные значения: {len(invalid)}')
    if len(domains) < minimum:
        raise ValueError(f'В CSV только {len(domains)} доменов, минимум для запуска: {minimum}')
    return domains


def atomic_write(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(data)
    temporary.replace(path)


def download(output, minimum=100):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        incoming = Path(directory) / 'download.csv'
        # curl respects Retry-After and detects truncated Content-Length responses.
        # The parent timeout also bounds all retries and backoff combined.
        command = ['curl', '--fail', '--silent', '--show-error', '--location',
                   '--proto', '=https', '--proto-redir', '=https',
                   '--retry', '2', '--retry-all-errors', '--retry-delay', '3',
                   '--retry-max-time', '150', '--connect-timeout', '10', '--max-time', '45',
                   '--max-filesize', str(MAX_BYTES), '--user-agent', 'DomainAtlas/1.0',
                   '--output', str(incoming), SOURCE_URL]
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=180)
        raw = incoming.read_bytes()
    domains = validate_csv(raw, minimum)
    metadata = {'url': SOURCE_URL, 'downloaded_at_utc': datetime.now(timezone.utc).isoformat(),
                'domains': len(domains), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    atomic_write(output.with_suffix('.source.csv'), raw)
    atomic_write(output.with_suffix('.source.json'), json.dumps(metadata, ensure_ascii=False, indent=2).encode('utf-8'))
    atomic_write(output, ('\n'.join(domains) + '\n').encode('utf-8'))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('build/input/domains.csv'))
    parser.add_argument('--min-domains', type=int, default=100)
    args = parser.parse_args()
    if args.min_domains < 1:
        parser.error('--min-domains must be at least 1')
    try:
        print(json.dumps(download(args.output, args.min_domains), ensure_ascii=False))
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        parser.exit(1, 'Не удалось получить свежий список доменов: ' + detail + '\n')


if __name__ == '__main__':
    main()
