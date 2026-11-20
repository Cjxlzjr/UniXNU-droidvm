#!/usr/bin/env python3
"""Stream a package checkpoint between the staged builder and CI's age cipher."""

import argparse
import gzip
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path


STAMPS = tuple(f'build-all/{step}.done' for step in ('policies', 'data-base', 'reference', 'kernel', 'package'))
FILES = (
    '.config',
    'kernel/kernel.macho',
    'kernel/kernel.json',
    'package/Build/Out/AARCH64-DEBUG/UniXNU.efi',
    'package/Build/Out/AARCH64-DEBUG/DarwinBoot.efi',
    'tree/build/owner-data-fixture/seed.json',
    'tree/build/droidvm-data/data-2g-skip-app.dbr',
    'tree/build/original-static-system-trust/original-static-system.bundle',
    'tree/build/original-trust-extra/app.trustcache',
    *STAMPS,
)
OPTIONAL = ('tree/build/app-cryptex/app-trustcache.bin',)
KEY = 'checkpoint-key.txt'
MAX_BYTES = 512 * 1024 * 1024


def validate(work, names):
    for name in names:
        path = work / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError(f'Incomplete package state: {name}')
    seed = json.loads((work / 'tree/build/owner-data-fixture/seed.json').read_text())
    volume = str(uuid.UUID(seed['volume_uuid'])).upper()
    config = (work / '.config').read_text().upper().splitlines()
    if f'CONFIG_XNU_OWNER_DATA_VOLUME_UUID="{volume}"' not in config:
        raise ValueError('Package kernel configuration and Data seed UUID disagree')
    record = json.loads((work / 'kernel/kernel.json').read_text())
    with (work / 'kernel/kernel.macho').open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != record['candidate_sha256']:
            raise ValueError('Package kernel hash disagrees with kernel.json')


def save(work, key, output):
    names = (*FILES, *(name for name in OPTIONAL if (work / name).is_file()))
    validate(work, names)
    data = key.encode('ascii')
    if sum((work / name).stat().st_size for name in names) + len(data) > MAX_BYTES:
        raise ValueError('Package checkpoint exceeds its size limit')
    with gzip.GzipFile(fileobj=output, mode='wb', compresslevel=3) as compressed:
        with tarfile.open(fileobj=compressed, mode='w|') as archive:
            member = tarfile.TarInfo(KEY)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            for name in names:
                archive.add(work / name, arcname=name, recursive=False)


def restore(work, key, source):
    # Validate entirely in a staging directory. Stamps are installed last so
    # the regular builder never skips a step whose files were not restored.
    with tempfile.TemporaryDirectory(dir=work) as temporary:
        staging = Path(temporary)
        seen = set()
        size = 0
        with tarfile.open(source, mode='r|gz') as archive:
            for member in archive:
                name = member.name
                size += member.size
                if (name not in (*FILES, *OPTIONAL, KEY) or name in seen or not member.isfile()
                        or not 0 < member.size <= MAX_BYTES or size > MAX_BYTES):
                    raise ValueError(f'Unexpected checkpoint member: {name}')
                with archive.extractfile(member) as stream:
                    if name == KEY:
                        expected = key.encode('ascii')
                        if member.size != len(expected) or stream.read() != expected:
                            raise ValueError('Checkpoint was built with different inputs')
                    else:
                        target = staging / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with target.open('wb') as output:
                            shutil.copyfileobj(stream, output)
                seen.add(name)
        if not {*FILES, KEY} <= seen:
            raise ValueError('Checkpoint is missing package outputs')
        validate(staging, seen - {KEY})
        for name in FILES + OPTIONAL:
            if name not in seen:
                continue
            target = work / name
            target.parent.mkdir(parents=True, exist_ok=True)
            (staging / name).replace(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('save', 'restore'))
    parser.add_argument('work', type=Path)
    parser.add_argument('key')
    parser.add_argument('--archive', type=Path, help='the decrypted checkpoint, for restore only')
    args = parser.parse_args()
    try:
        if args.action == 'save':
            save(args.work, args.key, sys.stdout.buffer)
        else:
            if args.archive is None:
                parser.error('restore requires --archive')
            restore(args.work, args.key, args.archive)
    except (ValueError, KeyError, EOFError, OSError, tarfile.TarError) as exc:
        print(f'Package checkpoint: {exc}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
