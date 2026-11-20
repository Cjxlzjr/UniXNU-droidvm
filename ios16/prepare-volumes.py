#!/usr/bin/env python3
"""Extract and verify the iPhone X OS and SystemOS disks from the Apple IPSW."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tomllib
from types import SimpleNamespace

core, output = map(Path, sys.argv[1:])
sys.path.insert(0, str(core.resolve() / 'scripts'))
from unixnu.ipsw import Ipsw
from unixnu.udif import Image
from unixnu.userdata import attach, disk_blocks

profile = tomllib.loads(Path(__file__).with_name('profile.toml').read_text())
ctx = SimpleNamespace(board=profile, out_root=output / '.inputs')
source = Ipsw(ctx, profile['ipsw']['url'])
pins = tomllib.loads(Path(__file__).with_name('volume-identity.toml').read_text())


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def apfs_volumes(path):
    disk = attach(path)
    name = Path(disk).name
    store_pattern = re.compile(fr'{re.escape(name)}(?:s\d+)?')
    try:
        listing = plistlib.loads(subprocess.check_output(['diskutil', 'apfs', 'list', '-plist']))
        volumes = []
        for container in listing['Containers']:
            if any(store_pattern.fullmatch(store['DeviceIdentifier']) for store in container['PhysicalStores']):
                for volume in container['Volumes']:
                    volumes.append({'name': volume.get('Name'), 'uuid': volume['APFSVolumeUUID'],
                                    'roles': volume.get('Roles', [])})
        if not volumes:
            raise ValueError(f'No APFS volumes were observed on {path}')
        return volumes
    finally:
        subprocess.run(['hdiutil', 'detach', disk], check=True, capture_output=True)


output.mkdir(parents=True, exist_ok=True)
result = {}
for component, name in (('OS', 'os'), ('Cryptex1,SystemOS', 'systemos')):
    pin = pins[name]
    filename, size, expected = f'{name}.img', pin['bytes'], pin['sha256']
    path = output / filename
    if not (path.is_file() and path.stat().st_size == size and digest(path) == expected):
        member = source.extract(component)
        temporary = path.with_suffix('.partial')
        temporary.unlink(missing_ok=True)
        try:
            if Image(member).chunks is None:
                os.link(member, temporary)
            else:
                with temporary.open('wb') as stream:
                    for block in disk_blocks(member, size):
                        stream.write(block)
            if temporary.stat().st_size != size or digest(temporary) != expected:
                raise ValueError(f'{component} does not match the pinned raw size and SHA-256')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    identities = apfs_volumes(path)
    if {volume['uuid'].upper() for volume in identities} != {pin['uuid']}:
        raise ValueError(f'{filename}: APFS volume UUID differs from the pinned donor')
    result[filename] = {'component': component, 'bytes': size, 'sha256': expected, 'apfs_volumes': identities}
report = {'target': 'iPhone10,3 / iOS 16.7.16 (20H392)', 'files': result}
content = json.dumps(report, indent=2) + '\n'
(output / 'volumes.json').write_text(content)
print(content, end='')
