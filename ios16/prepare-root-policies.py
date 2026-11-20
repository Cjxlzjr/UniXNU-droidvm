#!/usr/bin/env python3
"""Create and verify the signed root policies for the checked iPhone X disks."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib

core, volumes, output = (Path(arg).resolve() for arg in sys.argv[1:])
verified_root = core / 'kits/VerifiedRoot'
pins = tomllib.loads(Path(__file__).with_name('volume-identity.toml').read_text())
inputs = json.loads((volumes / 'volumes.json').read_text())
if inputs.get('target') != 'iPhone10,3 / iOS 16.7.16 (20H392)':
    sys.exit('Root policies require the pinned iPhone X image report')
for name, pin in pins.items():
    raw = volumes / f'{name}.img'
    record = inputs['files'][raw.name]
    if (record['bytes'] != pin['bytes'] or record['sha256'] != pin['sha256']
            or {v['uuid'].upper() for v in record['apfs_volumes']} != {pin['uuid']}
            or raw.stat().st_size != pin['bytes']):
        sys.exit(f'{raw.name}: size, digest or APFS identity does not match 20H392')

def run(*command):
    subprocess.run(command, check=True)

output.mkdir(parents=True, exist_ok=True)
with TemporaryDirectory(prefix='ios16-policies-') as folder:
    work = Path(folder)
    library = work / 'verified-root.dylib'
    prefix = Path(subprocess.check_output(['brew', '--prefix', 'openssl@3'], text=True).strip())
    run('xcrun', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror', '-dynamiclib',
        '-I', verified_root / 'include', '-I', prefix / 'include',
        verified_root / 'src/verified_root.c', verified_root / 'tests/openssl_provider.c',
        '-L', prefix / 'lib', '-lcrypto', '-o', library)
    key, public = work / 'owner.pem', work / 'owner-public.pem'
    run('openssl', 'genpkey', '-algorithm', 'RSA', '-pkeyopt', 'rsa_keygen_bits:3072', '-out', key)
    run('openssl', 'pkey', '-in', key, '-pubout', '-out', public)
    manifests, reports = [], {}
    trusted = None
    for name, pin in pins.items():
        raw = volumes / f'{name}.img'
        policy = work / name
        run(sys.executable, verified_root / 'tools/manifest.py',
            '--image', raw, '--private-key', key, '--public-key', public,
            '--volume-uuid', pin['uuid'], '--generation', '1', '--output', policy)
        run(sys.executable, verified_root / 'tools/verify_image.py',
            '--library', library, '--manifest', policy / 'manifest.bin', '--trusted-key', policy / 'trusted-key.bin',
            '--image', raw, '--volume-uuid', pin['uuid'], '--minimum-generation', '1', '--output', policy / 'verification.json')
        record = json.loads((policy / 'verification.json').read_text())
        if record['image_sha256'] != pin['sha256'] or record['image_bytes'] != pin['bytes']:
            sys.exit(f'{raw.name}: real verified digest does not match the pinned image')
        anchor = (policy / 'trusted-key.bin').read_bytes()
        if trusted is not None and anchor != trusted:
            sys.exit('The two root policies must carry the same owner trust anchor')
        trusted = anchor
        reports[name] = record
        manifests.append((policy / 'manifest.bin').read_bytes())

    cursor = 32 + 16 * len(manifests)
    windows = bytearray()
    for data in manifests:
        windows += struct.pack('<QQ', cursor, len(data))
        cursor += len(data)
    bundle = struct.pack('<8sQQQ', b'VROOTB01', 1, len(manifests), cursor) + windows + b''.join(manifests)
    (output / 'root-policies.bin').write_bytes(bundle)
    (output / 'trusted-key.bin').write_bytes(trusted)
    summary = {'target': inputs['target'], 'disks': reports, 'bundle_sha256': hashlib.sha256(bundle).hexdigest(),
               'trusted_key_sha256': hashlib.sha256(trusted).hexdigest(), 'boot_root_authenticated': False}
    (output / 'root-policies.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
