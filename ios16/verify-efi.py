#!/usr/bin/env python3
"""Validate the actual PE/COFF fields of the two EFI loaders."""
from pathlib import Path
import struct
import sys

directory = Path(sys.argv[1])
for name, expected_subsystem in (('UniXNU.efi', 10), ('DarwinBoot.efi', 11)):
    image = (directory / name).read_bytes()
    if len(image) < 64 or image[:2] != b'MZ':
        sys.exit(f'{name}: missing DOS header')
    pe, = struct.unpack_from('<I', image, 0x3c)
    if pe + 94 > len(image) or image[pe:pe + 4] != b'PE\0\0':
        sys.exit(f'{name}: missing PE header')
    machine, = struct.unpack_from('<H', image, pe + 4)
    optional_size, magic = struct.unpack_from('<H2xH', image, pe + 20)
    subsystem, = struct.unpack_from('<H', image, pe + 92)
    if optional_size < 70 or (machine, magic, subsystem) != (0xaa64, 0x20b, expected_subsystem):
        sys.exit(f'{name}: unexpected machine {machine:#x}, optional header {magic:#x} or subsystem {subsystem}')
    print(f'{name}: AArch64 PE32+, EFI subsystem {subsystem}, {len(image):,} bytes')
