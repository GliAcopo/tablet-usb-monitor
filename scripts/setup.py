#!/usr/bin/env python3
"""Download checksum-pinned local tools without privileged system changes."""
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / '.local'

def download(item, destination):
    if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == item['sha256']:
        return
    temporary = destination.with_suffix(destination.suffix + '.partial')
    urllib.request.urlretrieve(item['url'], temporary)
    if hashlib.sha256(temporary.read_bytes()).hexdigest() != item['sha256']:
        temporary.unlink()
        raise RuntimeError('Downloaded dependency did not match its pinned checksum')
    temporary.replace(destination)

def main():
    LOCAL.mkdir(exist_ok=True)
    manifest = json.loads((ROOT / 'dependencies.json').read_text())
    archive = LOCAL / 'platform-tools.zip'
    download(manifest['adb'], archive)
    with zipfile.ZipFile(archive) as zipped:
        for name in zipped.namelist():
            if not (LOCAL / name).resolve().is_relative_to(LOCAL.resolve()):
                raise RuntimeError('Invalid archive path')
        zipped.extractall(LOCAL)
    (LOCAL / 'platform-tools/adb').chmod(0o755)
    print('ADB is ready in .local/platform-tools; no system packages changed.')

if __name__ == '__main__':
    main()
