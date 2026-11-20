#!/usr/bin/env python3
"""Generate a new read-only DroidVM test configuration for the iOS 16 handoff disk."""
import argparse
import json
from pathlib import Path, PurePosixPath
import uuid

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--boot-dir', type=PurePosixPath, required=True, help='absolute directory on the Android host')
parser.add_argument('--firmware', type=PurePosixPath, required=True, help='absolute Android path to the proven UEFI firmware')
parser.add_argument('--output', type=Path, required=True, help='new file for the probe VM')
args = parser.parse_args()
for path in (args.boot_dir, args.firmware):
    if not path.is_absolute() or '..' in path.parts:
        parser.error('Use an absolute Android path without parent traversals')

template = Path(__file__).resolve().parents[1] / 'tools/rig/unixnu-vm.template.json'
document = json.loads(template.read_text())
config = document['config']
config.update(id=str(uuid.uuid4()), name='UniXNU iOS 16 kernel probe', auto_up=False,
              gpu_mode='none', gpu_api='none', display_enabled=False, native_display_enabled=False,
              gpu_cgroup_enabled=False, gpu_host_pool_mb=0, gpu_drm2kgsl_pool_mb=0,
              motion_sensor=False, vpu_enabled=False, networks=[], peripherals=[],
              extra_options=['--dump-device-tree-blob', '/data/local/tmp/unixnu-ios16-probe-fdt.dtb'],
              disks=[{'path': str(args.boot_dir / 'ios16-boot-probe.img'), 'bus': 'virtio', 'readonly': True}])
for screen in config['screens'].values():
    screen.update(enabled=False, input_enabled=False)
config['boot']['uefi']['firmware'] = str(args.firmware)
with args.output.open('x', encoding='utf-8') as stream:
    json.dump(document, stream, indent=2)
    stream.write('\n')
print(f'{args.output}: {config["memory_mb"]} MiB, {config["cpu_count"]} vCPUs, new VM {config["id"]}')
