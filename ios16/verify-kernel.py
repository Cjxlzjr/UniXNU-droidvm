#!/usr/bin/env python3
"""Validate the prelinked iPhone X executable and its upstream report."""
import hashlib
import json
from pathlib import Path
import struct
import sys

directory = Path(sys.argv[1])
kernel = directory / 'kernel.macho'
with kernel.open('rb') as stream:
    header = stream.read(32)
    stream.seek(0)
    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
if len(header) != 32 or struct.unpack_from('<4I', header) != (0xfeedfacf, 0x100000c, 0, 2):
    sys.exit('Expected a plain ARM64 MH_EXECUTE kernel')
record = json.loads((directory / 'kernel.json').read_text())
if record.get('candidate_sha256') != digest or record.get('candidate_bytes') != kernel.stat().st_size:
    sys.exit('Kernel does not match its prelink report')
if not record.get('original_executable_sha256') or record.get('prelink_skipped'):
    sys.exit('The boot probe requires the prelinked kernel')
if record.get('original_sha256') != '2ec297bac95bcba01046fd0148e97e7e5885c05a6c18827f39f534ba375c1221':
    sys.exit('Kernel prelink report does not identify the pinned 20H392 iPhone X donor')
print(f'{digest}  {kernel} ({kernel.stat().st_size:,} bytes)')
