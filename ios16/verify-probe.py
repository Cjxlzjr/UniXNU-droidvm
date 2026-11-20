#!/usr/bin/env python3
"""Check the boot probe files extracted from the actual FAT volume."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tomllib

helper = Path(__file__).resolve().parents[1] / 'scripts/make-droidvm-adt.py'
spec = importlib.util.spec_from_file_location('adt', helper)
adt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adt)

staged, disk, payload_record = map(Path, sys.argv[1:])
config = tomllib.loads((staged / 'unixnu.toml').read_text())
entry = next(entry for entry in config['entry'] if entry['id'] == config['boot']['default'])
if config['driver'] != [{'path': 'Payloads/DarwinBoot.efi'}] or entry['path'] != 'Payloads/kernel.macho':
    sys.exit('FAT configuration does not route the default entry through DarwinBoot')
blob = (staged / 'kernel.macho.adt').read_bytes()
if hashlib.sha256(blob).hexdigest() != json.loads(payload_record.read_text())['adt_sha256']:
    sys.exit('FAT device tree differs from the payload builder record')
(root, children), end = adt.read_adt(blob)
nodes = {props['name'].rstrip(b'\0'): props for props, _ in children}
product = nodes[b'product']
if (end != len(blob) or root['model'].rstrip(b'\0') != b'iPhone10,3'
        or root['target-type'].rstrip(b'\0').upper() != b'D22'
        or root['compatible'].split(b'\0')[0].upper() != b'D22AP'
        or product['product-id'].hex() != 'fd079b298144a7deb4d7c87fef3eb474d8f0e7eb'
        or b'buttons' in nodes):
    sys.exit('ADT does not carry the D22AP iPhone X identity')
if struct.unpack('<I', nodes[b'chosen']['darwinboot-fdt'])[0] != 7:
    sys.exit('ADT must enable the FDT memory, device and CPU updates')

files = {}
for item in [*sorted(staged.iterdir()), disk]:
    with item.open('rb') as stream:
        files[item.name] = {'bytes': item.stat().st_size, 'sha256': hashlib.file_digest(stream, 'sha256').hexdigest()}
print(json.dumps({'target': 'iPhone10,3 / iOS 16.7.16 (20H392)', 'scope': 'kernel handoff probe',
                  'device_verified': False, 'root_volumes_present': False, 'files': files}, indent=2))
