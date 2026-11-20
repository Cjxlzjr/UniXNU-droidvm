#!/usr/bin/env python3
"""Verify the donor APFS VFS import in the finished prelinked image."""
import hashlib
import json
from pathlib import Path
import struct
import sys

tree, candidate, original = map(Path, sys.argv[1:])
sys.path.insert(0, str(tree / 'kits/XnuPrelink'))
from native_prelink import backed_offset, exports, macho

native_name = 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/mach.development.bcm2837.unstripped'
native_data = (tree / native_name).read_bytes()
original_data = original.read_bytes()
final_data = candidate.read_bytes()
report = json.loads(candidate.with_suffix('.json').read_text())
module = 'com.apple.filesystems.apfs'
symbol = '_vfs_getvfs_with_vfsops'
if (report.get('native_sha256') != hashlib.sha256(native_data).hexdigest()
        or report.get('original_sha256') != hashlib.sha256(original_data).hexdigest()
        or not report['original_executable_sha256'].get(module)):
    sys.exit('APFS prelink report does not match the native and donor inputs')


def address(data):
    addresses = [va for va, names in exports(data, macho(data)).items() if symbol in names]
    if len(addresses) != 1:
        sys.exit(f'{symbol} needs one real kernel symbol, found {len(addresses)}')
    return addresses[0]


original_target, native_target = address(original_data), address(native_data)
bindings = [entry for entry in report['bindings'] if entry['module'] == module and entry['target'] == original_target]
if not bindings:
    sys.exit(f'The donor APFS import {symbol} was not found in the prelink report')
segments = macho(final_data)['segments']
for entry in bindings:
    offset = backed_offset(segments, entry['new_site'], 8, len(final_data))
    value, = struct.unpack_from('<Q', final_data, offset)
    if entry['new_target'] != native_target or value != native_target:
        sys.exit(f'{symbol} does not bind to the native VFS implementation at {entry["new_site"]:#x}')
print(f'APFS: verified {len(bindings)} real {symbol} pointer binding(s) in the finished kernel')
