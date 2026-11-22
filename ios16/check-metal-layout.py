#!/usr/bin/env python3
"""Read-only 20H392 OS/SystemOS Metal-layout preflight and optional Metal Data Seed check.

Examples:
  python3 ios16/check-metal-layout.py volumes --volumes-dir /path/iphone-volumes --out diagnostics/metal-layout.json
  python3 ios16/check-metal-layout.py seed --core /path/UniXNU --seed data-2g-skip-metal.dbr --out diagnostics/metal-seed.json
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import tomllib

EXPECTED = 'System/Library/Extensions/AGXMetalA11.bundle'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


@contextmanager
def attach_readonly(image: Path):
    """Use hdiutil's returned mount and device, never a guessed disk/host path."""
    with tempfile.TemporaryDirectory(prefix='ios16-metal-mount-') as td:
        target = Path(td) / 'volume'
        target.mkdir()
        info = plistlib.loads(subprocess.check_output([
            'hdiutil', 'attach', '-readonly', '-nobrowse', '-noverify',
            '-imagekey', 'diskimage-class=CRawDiskImage',
            '-mountpoint', str(target), '-plist', str(image),
        ]))
        entities = info.get('system-entities', [])
        devices = [e.get('dev-entry') for e in entities if e.get('dev-entry')]
        if not devices:
            raise RuntimeError('hdiutil returned no device')
        disk = next((d for d in devices if re.fullmatch(r'/dev/disk\d+', d)), devices[0])
        try:
            mounted = [Path(e['mount-point']) for e in entities if e.get('mount-point')]
            if not mounted:
                raise RuntimeError(f'{image.name}: no APFS volume mount (devices={devices})')
            root = target if target in mounted else mounted[0]
            if not root.is_dir():
                raise RuntimeError(f'Mount path disappeared: {root}')
            yield root
        finally:
            subprocess.run(['hdiutil', 'detach', disk], check=True, capture_output=True)


def entry(path: Path, root: Path) -> dict:
    record = {'path': '/' + str(path.relative_to(root)),
              'type': 'symlink' if path.is_symlink() else ('directory' if path.is_dir() else 'file')}
    if path.is_symlink():
        record['link_target'] = os.readlink(path)
    return record


def inspect_system(root: Path) -> dict:
    ext = root / 'System/Library/Extensions'
    expected = root / EXPECTED
    observed = []
    for dirname in (
        'System/Library/Extensions', 'System/Library/DriverExtensions',
        'Library/Extensions', 'System/Library/PrivateFrameworks',
    ):
        loc = root / dirname
        if loc.is_dir():
            for path in loc.iterdir():
                if path.name.casefold().startswith(('agx', 'appleagx', 'iogpu')) or 'metal' in path.name.casefold():
                    observed.append(entry(path, root))
    return {
        'extensions_dir_exists': ext.is_dir(),
        'expected_agxmetal_a11_exists': expected.exists(),
        'expected_agxmetal_a11_is_symlink': expected.is_symlink(),
        'observed_gpu_or_metal_entries': sorted(observed, key=lambda x: x['path']),
        'extensions_entry_count': sum(1 for _ in ext.iterdir()) if ext.is_dir() else None,
    }


def check_volumes(args) -> dict:
    pins = tomllib.loads((Path(__file__).with_name('volume-identity.toml')).read_text())
    report = {
        'target': 'iPhone10,3 / iOS 16.7.16 (20H392)',
        'mode': 'volumes', 'expected_visible_path': '/' + EXPECTED,
        'volumes': {}, 'errors': [],
    }
    for name in ('os', 'systemos'):
        path = args.volumes_dir / (name + '.img')
        pin = pins[name]
        if not path.is_file():
            report['errors'].append(f'{name}.img missing')
            continue
        size, digest = path.stat().st_size, sha256(path)
        item = {
            'image_size': size, 'image_sha256': digest,
            'expected_size': pin['bytes'], 'expected_sha256': pin['sha256'],
            'sha256_pass': digest == pin['sha256'] and size == pin['bytes'],
        }
        report['volumes'][name] = item
        if not item['sha256_pass']:
            report['errors'].append(f'{name}.img hash or size differs from pinned image')
            continue
        try:
            with attach_readonly(path) as root:
                item['layout'] = inspect_system(root)
        except Exception as exc:
            report['errors'].append(f'{name}.img read-only inspection failed: {exc}')
    report['original_agx_candidates'] = [
        {'volume': name, **candidate}
        for name, item in report['volumes'].items()
        for candidate in item.get('layout', {}).get('observed_gpu_or_metal_entries', [])
    ]
    found = [name for name, item in report['volumes'].items()
             if item.get('layout', {}).get('expected_agxmetal_a11_exists')]
    report['expected_agxmetal_a11_volumes'] = found
    if not found and not report['errors']:
        report['errors'].append('AGXMetalA11.bundle absent in both pinned volumes; select sentinel from observed paths')
    report['verdict'] = 'PASS' if not report['errors'] and found else 'NEEDS_REVIEW'
    return report


def check_seed(args) -> dict:
    """Inspect the real packed Data Seed without modifying its contents."""
    sys.path.insert(0, str(args.core.resolve() / 'drivers/DarwinBoot/Test'))
    import packed_ramdisk
    report = {'mode': 'seed', 'packed_data_seed': str(args.seed), 'checks': {}, 'errors': []}
    if not args.seed.is_file():
        report['errors'].append('Metal Data Seed missing')
    else:
        with tempfile.TemporaryDirectory(prefix='ios16-metal-seed-ro-') as td:
            raw = Path(td) / 'seed.raw'
            logical, extents = packed_ramdisk.regions(args.seed.read_bytes())
            with raw.open('wb') as f:
                f.truncate(logical)
                for offset, data in extents:
                    f.seek(offset)
                    f.write(data)
            with attach_readonly(raw) as root:
                paths = {
                    'bundle_info': root / 'ngpu/addons/NativeAdrenoMetal.bundle/Info.plist',
                    'bundle_executable': root / 'ngpu/addons/NativeAdrenoMetal.bundle/NativeAdrenoMetal',
                    'publish_marker': root / 'ngpu/addons/NativeAdrenoMetal.publish',
                    'daemon_executable': root / 'ngpu/bin/adrenoprobed',
                    'daemon_plist_directory': root / 'ngpu/daemons',
                }
                for key, path in paths.items():
                    report['checks'][key] = path.is_dir() if key.endswith('_directory') else path.is_file()
                if report['checks']['bundle_info']:
                    try:
                        info = plistlib.loads(paths['bundle_info'].read_bytes())
                        report['bundle_identifiers'] = {
                            key: info.get(key) for key in ('CFBundleIdentifier', 'CFBundleExecutable')
                        }
                    except Exception as exc:
                        report['errors'].append(f'Invalid plugin Info.plist: {exc}')
                if report['checks']['publish_marker']:
                    report['checks']['publish_marker_content'] = paths['publish_marker'].read_text().strip() == '1'
    missing = [key for key, valid in report['checks'].items() if valid is not True]
    if missing:
        report['errors'].append('Missing/invalid seed entries: ' + ', '.join(missing))
    report['verdict'] = 'PASS' if not report['errors'] else 'FAIL'
    return report


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='mode', required=True)
    v = sub.add_parser('volumes')
    v.add_argument('--volumes-dir', type=Path, required=True)
    v.add_argument('--out', type=Path, required=True)
    s = sub.add_parser('seed')
    s.add_argument('--core', type=Path, required=True)
    s.add_argument('--seed', type=Path, required=True)
    s.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    try:
        report = check_volumes(args) if args.mode == 'volumes' else check_seed(args)
    except Exception as exc:
        report = {'mode': args.mode, 'verdict': 'FAIL', 'errors': [repr(exc)]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['verdict'] == 'PASS' else 1


if __name__ == '__main__':
    sys.exit(main())
