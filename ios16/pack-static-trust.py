#!/usr/bin/env python3
"""Wrap the original Apple static trust module for DarwinBoot without changing its entries."""
import hashlib
from pathlib import Path
import struct
import sys
import tomllib


def main(core, donor, output):
    device = Path(__file__).resolve().parent
    profile = tomllib.loads((device / 'profile.toml').read_text())
    board = tomllib.loads((device / 'board.toml').read_text())
    source_hash = profile['ipsw']['sha256']['087-86698-044.dmg.trustcache']
    raw = donor.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source_hash:
        sys.exit('Static trust cache differs from the pinned 20H392 IPSW input')

    sys.path.insert(0, str(core / 'scripts'))
    from unixnu.ipsw import im4p_payload

    module = im4p_payload(raw)
    bundle = struct.pack('<II', 1, 8) + module
    if hashlib.sha256(bundle).hexdigest() not in board['payload']['trust_cache_sha256']:
        sys.exit('Wrapped trust cache differs from the pinned DarwinBoot bundle')
    output.write_bytes(bundle)


if __name__ == '__main__':
    main(*(Path(arg).resolve() for arg in sys.argv[1:]))
