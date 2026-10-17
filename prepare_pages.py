"""Prepare GitHub Pages from one specific, already published release snapshot."""
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def prepare(payload_path, repository, tag, output=None):
    if not re.fullmatch(r'[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repository):
        raise ValueError('Expected a GitHub owner/repository')
    raw = Path(payload_path).read_bytes()
    payload = json.loads(raw)
    if (payload.get('schema_version') != 1 or not isinstance(payload.get('domains'), list)
            or not payload['domains'] or payload.get('summary', {}).get('domains') != len(payload['domains'])):
        raise ValueError('Invalid or empty release snapshot; refusing to deploy')
    output = Path(output) if output else ROOT / 'build' / 'pages'
    source = ROOT / 'site'
    if output.resolve() == source or source in output.resolve().parents or output.resolve() in source.parents:
        raise ValueError('Output must not overlap the site sources')
    shutil.copytree(source, output, dirs_exist_ok=True)
    (output / 'data' / 'domains.json').write_bytes(raw)
    config = {'dataUrl': './data/domains.json', 'demoUrl': './data/domains.json',
              'releaseTag': tag, 'releaseUrl': f'https://github.com/{repository}/releases/tag/{tag}'}
    (output / 'config.js').write_text('window.DOMAIN_CONFIG = ' + json.dumps(config, ensure_ascii=False) + ';\n', encoding='utf-8')
    (output / '.nojekyll').touch()
    return payload['summary']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--payload', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--tag', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args.payload, args.repository, args.tag), ensure_ascii=False))


if __name__ == '__main__':
    main()
