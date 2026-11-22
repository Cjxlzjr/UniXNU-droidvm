#!/usr/bin/env python3
"""Build a separate DroidVM boot disk for 20H392 with the working 21H461 backend."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import re
import runpy
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import tomllib
from types import SimpleNamespace
from urllib.request import urlopen
import uuid

DEVICE = Path(__file__).resolve().parents[1]
IOS = DEVICE / 'ios16'
spec = importlib.util.spec_from_file_location('droidvm_build', DEVICE / 'scripts/build-droidvm-all.py')
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
CORE_REVISION = 'dba62c1ca53cd04c25b0e501afff31d5ffe2e20e'
KERNEL_DONOR = 'f18b4df865405f2a287b84977d7aca745e056d656d9fc9df1662970ce2969de7'
TRUST_SHA256 = '2d97b2a4e114c7303f5521814b8ae67bb85c66e4628fefbb13fe38a3fdba555d'
IPAD_OS_UUID = '04769D26-2CA0-4825-B443-E2E5F88300F2'
IPAD_SYSTEMOS_UUID = '8846FE78-2910-4297-AF97-D59BB6F8FC16'
EFI_PINS = {
    'UniXNU.efi': 'f63ae4b9d968288cfe44df40c4282221dbefe13fb5a86dcb55544cee6d55efe4',
    'DarwinBoot.efi': 'c25cdb24f0f3c1568308325764bbc10e64e12d3b4566473ae03ca442a4cb9958',
}
TRUST_COMPONENTS = ('StaticTrustCache', 'Cryptex1,SystemTrustCache', 'Cryptex1,AppTrustCache')
PINS = tomllib.loads((IOS / 'volume-identity.toml').read_text())
PROFILE = tomllib.loads((IOS / 'profile.toml').read_text())
IPHONE_OS, IPHONE_SYSTEM, IPHONE_APP = '087-86698-044.dmg', '087-86593-047.dmg', '087-86715-047.dmg'
SEED = 'data-2g-skip-app.dbr'
METAL_SEED = 'data-2g-skip-metal.dbr'
GPU_STAGES = ('off', '3d', 'metal')
PORT_PATCH = IOS / 'patches/legacy-mach-inherited-ports.patch'
HOST_CHECK_SOURCES = {
    'deferred_bo.cpp': (
        'https://raw.githubusercontent.com/UniXNU/Adreno740/6a3c9a8865e0feae9b3be72a3c8bb4f0595d93ba/tests/hostsim/deferred_bo.cpp',
        '35ba6f5e313ff21d14ba2884365181ebc871cfcf820e41567e8b1eaa11eb399e',
    ),
    'check-quartzcore-turnip-cache-host.py': (
        'https://raw.githubusercontent.com/UniXNU/UniXNU-sheng/516a780473a2ac59c8b438e132bb60204dda9e22/scripts/check-quartzcore-turnip-cache-host.py',
        'eae106179803091de6d065a57fd9af737184d1bae90927313255521c84fcc311',
    ),
}
QUARTZCORE_LIBRARY = 'System/Library/Frameworks/QuartzCore.framework/default.metallib'
QUARTZCORE_DONOR_SHA256 = '606f913adf520420f9b4bb666efb782a68c830f082f1e559e17b20d0d13adbc9'


def replace_field(text, field, value):
    text, count = re.subn(rf'^{re.escape(field)}\s*=.*$', lambda _: f'{field} = {value}', text, flags=re.M)
    if count != 1:
        raise ValueError(f'Expected one device field named {field}')
    return text


def replace_list(text, field, values):
    text, count = re.subn(rf'^{re.escape(field)} = \[\r?\n.*?^\]',
                          lambda _: f'{field} = {json.dumps(values)}', text, flags=re.M | re.S)
    if count != 1:
        raise ValueError(f'Expected one multiline device list named {field}')
    return text


def derived_board(text, gpu_stage='off'):
    if gpu_stage not in GPU_STAGES:
        raise ValueError(f'Invalid GPU experiment stage: {gpu_stage}')
    original = tomllib.loads(text)
    if original['ipsw']['build'] != '21H461' or original['kernel']['prelink']['kernelcache_sha256'] != KERNEL_DONOR:
        raise ValueError('The hybrid target requires the tested Darwin 23 backend')
    args = original['payload']['kernel_args'] + [
        'serial=3', 'owner-tools-overlay=0', 'owner-service-manager=0', 'owner-skip-launch-constraints=0',
        f'vgpu-3d={int(gpu_stage != "off")}',
        f'droidvm-gpu-metal={int(gpu_stage == "metal")}',
        f'sheng-gpu-addon={int(gpu_stage == "metal")}',
        f'vgpu-selftest={int(gpu_stage != "off")}',
    ]
    tool_args = original['payload']['adt']['tool_args'] + ['--identity-tree', 'build/iphone-x-identity.im4p']
    for key, value in (
        ('fetch', '["KernelCache"]'),
        ('volume', '"IOS16D23"'),
        ('title', '"DroidVM: iPhone X iOS 16.7.16"'),
        ('size_mib', '256'),
        ('root_match_volume_uuid', json.dumps(PINS['os']['uuid'])),
        ('ipad_identity', 'false'),
        ('tool_args', json.dumps(tool_args)),
    ):
        text = replace_field(text, key, value)
    # The [ipsw] chip_id is the kernel donor. Only the ADT chip_id is from D22AP.
    head, adt = text.split('[payload.adt]', 1)
    text = head + '[payload.adt]' + replace_field(adt, 'chip_id', '"8015"')
    text = replace_list(text, 'trust_cache_sha256', [TRUST_SHA256])
    text = replace_list(text, 'kernel_args', args)
    parsed = tomllib.loads(text)
    if parsed['kernel'] != original['kernel'] or parsed['ipsw']['chip_id'] != original['ipsw']['chip_id']:
        raise ValueError('The hybrid board changed the donor kernel selection')
    return text


def substitute(source, replacements):
    text = source.read_text()
    for old, new in replacements:
        if old not in text:
            raise ValueError(f'{source}: expected pinned source value {old}')
        text = text.replace(old, new)
    return text


def verify_kernel_volume_ids(image):
    with image.open('rb') as stream:
        contents = stream.read()
    for volume, previous in [('os', IPAD_OS_UUID), ('systemos', IPAD_SYSTEMOS_UUID)]:
        selected = PINS[volume]['uuid']
        if selected.encode() + b'\0' not in contents or previous.encode() + b'\0' in contents:
            raise ValueError(f'The linked kernel must select the iPhone {volume} APFS volume {selected}')


def verify_kernel_mach_ports(image):
    table = subprocess.check_output(['llvm-nm', '--defined-only', image], text=True)
    names = {line.split()[-1] for line in table.splitlines() if line.strip()}
    for call in ('mach_ports_register', 'mach_ports_lookup'):
        if {f'_{call}', f'__X{call}'} - names or f'__X_kernelrpc_{call}3' in names:
            raise ValueError(f'The linked task MIG server does not implement the iOS 16 {call} RPC')
    base.log('Verified the linked iOS 16 Mach inherited-port MIG handlers')


def stage_device(core, directory, gpu_stage='off'):
    shutil.copytree(DEVICE, directory, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('.git', '.cache', '__pycache__', 'build', 'unixnu.lock'))
    board = derived_board((DEVICE / 'board.toml').read_text(), gpu_stage)
    (directory / 'board.toml').write_text(board)
    defconfig = directory / 'configs' / tomllib.loads(board)['kernel']['defconfig']
    key = 'CONFIG_XNU_OWNER_CACHE_VOLUME_UUID'
    lines = [line for line in defconfig.read_text().splitlines()
             if not line.startswith((f'{key}=', f'# {key} is not set'))]
    # The generated owner-data-volume.h takes precedence over kern_exec.c's fallback UUID.
    lines.append(f'{key}="{PINS["systemos"]["uuid"]}"')
    defconfig.write_text('\n'.join(lines) + '\n')
    patches = directory / 'patches/xnu'
    shutil.copytree(core / 'patches/xnu', patches, dirs_exist_ok=True)
    shutil.copyfile(PORT_PATCH, patches / PORT_PATCH.name)
    old_uuids = [(IPAD_OS_UUID, PINS['os']['uuid']), (IPAD_SYSTEMOS_UUID, PINS['systemos']['uuid'])]
    for file in ('offline-owner-cache-carrier.patch', 'offline-owner-systemos-mount.patch'):
        target = patches / file
        target.write_text(substitute(target, old_uuids))
    probe = substitute(core / 'scripts/build-verified-root-probe.py', [
        (base.SYSTEMOS_DMG, IPHONE_SYSTEM),
        (base.OS_IMAGE_SHA256, PINS['os']['sha256']),
        ('b4908cde41d23e6bc05e6af51a2c9eeb00cf0f863a6a18b1600c96a3fd70414f', PINS['systemos']['sha256']),
        ('1483785', str((PINS['os']['bytes'] + 4095) // 4096)),
        ('806400', str((PINS['systemos']['bytes'] + 4095) // 4096)),
        ('3303014400', str(PINS['systemos']['bytes'])),
    ])
    (directory / 'scripts/build-verified-root-probe.py').write_text(probe)


class HybridBuild(base.Build):
    def __init__(self, args):
        core, out = args.unixnu.resolve(), args.output.resolve()
        if subprocess.check_output(['git', '-C', core, 'rev-parse', 'HEAD'], text=True).strip() != CORE_REVISION:
            raise ValueError(f'Expected the audited UniXNU revision {CORE_REVISION}')
        if out.is_relative_to(DEVICE):
            raise ValueError('Place the derived build tree outside the source repository')
        device = out / 'device'
        stage_device(core, device, args.gpu_stage)
        scratch = Path('/tmp') / ('droidvm-ios16-' + hashlib.sha256(str(out).encode()).hexdigest()[:12])
        super().__init__(args, device=device, out=out, scratch=scratch)
        self.reference_out = out / 'reference'
        self.raw = self.work / 'iphone-volumes'
        self.input_cache = self.raw / '.inputs'
        self.ipsw = self.input_cache / 'ipsw/20H392'
        self.os_dmg, self.systemos_dmg = IPHONE_OS, IPHONE_SYSTEM
        self.trust = self.tree / 'build/original-static-system-trust/original-static-system.bundle'
        self.gpu_stage = args.gpu_stage
        self.gpu_trust = self.tree / 'build/ios16-metal/owner-trust.bundle'

    def run_step(self, name, action):
        stamp = self.stamp(name)
        if not stamp.is_file():
            base.log(f'Hybrid step {name}')
            action()
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text('done\n')

    def configure(self):
        self.configure_sources()
        self.unixnu_cmd('tree', logfile=self.logs / 'tree.log')
        if self.gpu_stage == 'metal':
            self.patch_metal_deployment('ios16-metal-configure-patch.log')
        if (self.tree / 'patches/xnu').resolve() != (self.device / 'patches/xnu').resolve():
            raise ValueError('The resolved tree did not select the hybrid UUID patches')
        for name, pin in EFI_PINS.items():
            source = self.args.efi_dir / name
            if base.sha256(source) != pin:
                raise ValueError(f'Unexpected legacy SMP loader {name}')
            self.efi_dir().mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, self.efi_dir() / name)
        self.stamp('package').parent.mkdir(parents=True, exist_ok=True)
        self.stamp('package').write_text('verified loader reuse\n')

    def small_iphone_members(self):
        sys.path.insert(0, str(self.unixnu / 'scripts'))
        from unixnu.ipsw import Ipsw, im4p_payload
        source = Ipsw(SimpleNamespace(board=PROFILE, out_root=self.input_cache), PROFILE['ipsw']['url'])
        blobs = {}
        for name in ('DeviceTree', *TRUST_COMPONENTS, 'Cryptex1,AppOS'):
            path = source.extract(name)
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != PROFILE['ipsw']['sha256'][path.name]:
                raise ValueError(f'{name} differs from the pinned 20H392 member')
            blobs[name] = data
        identity = self.tree / 'build/iphone-x-identity.im4p'
        identity.write_bytes(blobs['DeviceTree'])
        modules = [im4p_payload(blobs[name]) for name in TRUST_COMPONENTS]
        offsets = [4 * (len(modules) + 1)]
        for module in modules[:-1]:
            offsets.append(offsets[-1] + len(module))
        bundle = struct.pack('<4I', len(modules), *offsets) + b''.join(modules)
        if hashlib.sha256(bundle).hexdigest() != TRUST_SHA256:
            raise ValueError('The original static, SystemOS and AppOS trust module bundle differs')
        self.trust.parent.mkdir(parents=True, exist_ok=True)
        self.trust.write_bytes(bundle)
        app = self.tree / 'build/original-trust-extra/app.trustcache'
        app.parent.mkdir(parents=True, exist_ok=True)
        app.write_bytes(blobs['Cryptex1,AppTrustCache'])

    def step_volumes(self):
        base.run([sys.executable, IOS / 'prepare-volumes.py', self.unixnu, self.raw],
                 env=self.env, logfile=self.logs / 'iphone-volumes.log')
        self.os_attachment()
        link = self.volumes / IPHONE_SYSTEM
        if not link.exists():
            link.symlink_to(self.raw / 'systemos.img')

    def step_policies(self):
        base.run([sys.executable, IOS / 'prepare-root-policies.py', self.unixnu, self.raw, self.work / 'policies',
                  '--kernel-inputs', self.tree / 'build'], env=self.env, logfile=self.logs / 'iphone-policies.log')

    def step_data_base(self):
        self.os_attachment()
        self.data.mkdir(parents=True, exist_ok=True)
        fixture = self.tree / 'build/owner-data-fixture'
        if not fixture.exists():
            launchd = base.sha256(self.volumes / 'os-mount/sbin/launchd')
            with TemporaryDirectory(dir=fixture.parent) as folder:
                prepared = Path(folder) / 'fixture'
                self.seed_script('prepare-owner-data-fixture.py', '--launchd-sha256', launchd, '--output', prepared)
                prepared.replace(fixture)
        source = fixture / 'owner-data-32.cdr'
        seed = json.loads((fixture / 'seed.json').read_text())
        if base.sha256(source) != seed['sha256']:
            raise ValueError('The copied APFS Data seed differs from its build report')
        self.pack(source, self.data / 'data-32.dbr')
        self.seed_chain(self.data / 'data-32.dbr', [
            ('data-2g.dbr', ['seed-data-grow.py', '--volatile', '--gib', '2']),
            ('data-2g-skip.dbr', ['seed-data-skip-setup.py']),
            (SEED, ['add-app-cryptex-data.py', '{base}', str(self.ipsw / IPHONE_APP), '{out}']),
        ])
        self.apply_data_uuid()

    def step_gpu_seed(self):
        self.seed_chain(self.data / SEED, [
            (METAL_SEED, ['add-gpu-plugin-data.py', '--publish', '--clients', base.GPU_CLIENTS]),
        ])

    def step_gpu_trust(self):
        self.gpu_trust.parent.mkdir(parents=True, exist_ok=True)
        base.run([sys.executable, IOS / 'metal-trust.py',
                  '--original', self.trust, '--expected-original', TRUST_SHA256,
                  '--plugin', self.tree / 'build/adreno-metal-plugin/trust.json',
                  '--output', self.gpu_trust], env=self.env, logfile=self.logs / 'ios16-metal-trust.log')

    def step_reference(self):
        super().step_reference()
        verify_kernel_mach_ports(self.tree / 'build/reference-candidate/kernel.macho')

    def run_kernel(self):
        self.unixnu_cmd('ipsw', 'fetch', 'KernelCache', logfile=self.logs / 'kernel-donor.log')
        self.run_step('reference', self.step_reference)
        self.small_iphone_members()
        for step, method in [('volumes', self.step_volumes), ('policies', self.step_policies),
                             ('data-base', self.step_data_base), ('kernel', self.step_kernel)]:
            self.run_step(step, method)
        self.verify_kernel()
        if self.gpu_stage == 'metal':
            # Older stamps can refer to an iOS 17 bundle and its seed/trust.
            self.run_step('gpu-plugin-ios16', self.step_plugin)
            self.run_step('gpu-seed-ios16', self.step_gpu_seed)
            self.run_step('gpu-trust-ios16', self.step_gpu_trust)

    def patch_metal_deployment(self, log):
        base.run([sys.executable, IOS / 'metal-experiment.py', 'patch', self.tree],
                 env=self.env, logfile=self.logs / log)

    def verify_metal_deployment(self):
        base.run([sys.executable, IOS / 'metal-experiment.py', 'audit',
                  self.tree / 'build/adreno-metal-plugin'],
                 env=self.env, logfile=self.logs / 'ios16-metal-artifacts.log')

    def step_plugin(self):
        self.prepare_turnip_host_checks()
        self.prepare_mesa()
        # Kernel assembly can copy upstream scripts over the configure-time
        # adaptations. Apply them after the final UnixNU tree operation.
        self.patch_metal_deployment('ios16-metal-patch.log')
        watchdog = runpy.run_path(str(self.tree / 'scripts/hostsim_watchdog_patch.py'))
        with watchdog['staged_watchdog_driver'](self.tree):
            self.build_checked_plugin()
        self.verify_metal_deployment()

    def build_checked_plugin(self):
        # Keep the host suites strict, but collect independent host and plugin
        # build results in the same CI attempt. Never stage an unverified plugin.
        self.script('build-turnip-darwin.py', '--target', 'ios', logfile=self.logs / 'turnip-ios.log')
        self.script('build-turnip-darwin.py', '--target', 'host', logfile=self.logs / 'turnip-host.log')
        host_status = self.script('check-turnip-darwin-host.py',
                                  logfile=self.logs / 'turnip-host-check.log', check=False)
        failed = sorted(p.stem for p in (self.tree / 'build/turnip-darwin-host').glob('*.log')
                        if 'FAIL' in p.read_text(errors='replace'))
        self.record_check('turnip-host', 'PASS' if host_status == 0
                          else 'FAIL ' + ' '.join(failed or ['(see the log)']))
        if not (self.tree / 'build/turnip-darwin-host/plugin_smoke').is_file():
            self.verify_turnip_host_check()
            raise ValueError('Host suite did not produce plugin_smoke binary')

        # Compilation is independent of the shader-cache regression verdict.
        # Diagnose link/API failures in this same CI run even if a host case
        # fails, while still refusing the payload/Release if either fails.
        plugin_status = self.script('build-adreno-metal-plugin.py',
                                    logfile=self.logs / 'plugin.log', check=False)
        self.record_check('gpu-plugin-build',
                          'PASS' if plugin_status == 0 else f'FAIL exit={plugin_status}')
        if plugin_status:
            log = self.logs / 'plugin.log'
            if log.is_file():
                base.log('Metal plugin compiler/linker tail:')
                print('\n'.join(log.read_text(errors='replace').splitlines()[-80:]), flush=True)
        if host_status and plugin_status:
            raise ValueError('Host regressions AND Metal plugin build failed; see turnip-host-check.log and plugin.log')
        self.verify_turnip_host_check()
        if plugin_status:
            raise ValueError('Metal plugin build failed; see plugin.log')

    def prepare_turnip_host_checks(self):
        inputs = self.tree / 'build/turnip-host-inputs'
        inputs.mkdir(parents=True, exist_ok=True)
        for name, (url, expected) in HOST_CHECK_SOURCES.items():
            with urlopen(url, timeout=60) as response:
                content = response.read(64 * 1024)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f'{name} differs from the pinned host test source')
            if name == 'deferred_bo.cpp':
                (inputs / name).write_bytes(content)
            else:
                # The upstream cache instrument is qualified with the original 21H461 library.
                # Give it that read-only test input without changing the active iPhone OS mount.
                original = f"LIBRARY = ROOT / '.cache/firmware/target-volumes/os-mount/{QUARTZCORE_LIBRARY}'"
                script = content.decode('utf-8')
                if script.count(original) != 1:
                    raise ValueError(f'{name}: expected one mounted-library path')
                script = script.replace(original, "LIBRARY = ROOT / 'build/turnip-host-inputs/default.metallib'")
                # Keep the original assertions unchanged. Surface the measured counters
                # and the failing command's logs before CI destroys its temporary workdir.
                anchor = "    values = dict(zip(fields, map(int, match.groups()[1:])))\n"
                if script.count(anchor) != 1:
                    raise ValueError('QuartzCore test counter anchor changed upstream')
                script = script.replace(anchor, anchor +
                    '    print("IOS16_QUARTZCORE_OBSERVED", mode, json.dumps(values, sort_keys=True), flush=True)\n')
                test_anchor = "    require(run.returncode == 0 and match and match[1] == mode, 'Actual UI compiler/cache control failed: ' + mode + '; see preserved log')\n"
                if script.count(test_anchor) != 1:
                    raise ValueError('QuartzCore test status anchor changed upstream')
                script = script.replace(test_anchor, test_anchor.replace('    require(', 
                    '    if run.returncode != 0 or not match or match[1] != mode:\n'
                    '        print("IOS16_QUARTZCORE_SUBPROCESS", mode, run.returncode, (run.stdout + run.stderr)[-14000:], flush=True)\n'
                    '    require('))
                # The pinned Turnip/IR3 backend probes 72 shader artifacts for
                # 24 QuartzCore recipes but writes only 48 distinct records.
                # Do not conflate misses with writes; still require all recipes,
                # real backend saves, a subsequently verified compile-forbidden
                # consumer hit, and negative UUID/empty-cache controls.
                producer_old = ("values['recipes'] == 24 and values['cold_required'] > 0 "
                                "and values['artifact_misses'] == values['artifact_writes'] == 72 "
                                "and values['driver_saves'] > 0")
                producer_new = ("values['recipes'] == 24 and values['cold_required'] > 0 "
                                "and values['artifact_misses'] == 72 and values['artifact_writes'] == 48 "
                                "and values['driver_saves'] > 0")
                if script.count(producer_old) != 1:
                    raise ValueError('QuartzCore cache producer invariant changed upstream')
                script = script.replace(producer_old, producer_new)
                # This test validates strict shader-cache persistence. The optional
                # NativeMetal warm-pass (flow bit 4) can append its own pipeline
                # records after a successful cache hit, changing the cache
                # directory for reasons unrelated to shader-cache writes.
                # Preserve flow bits 1+2 (render lowering and GPU submission).
                cache_env = ("env.update(DYLD_INSERT_LIBRARIES=str(PEER), "
                             "NMB_TEST_GRAPHICS_CACHE_LOADER=str(loader), "
                             "NMB_TEST_QUARTZCORE_LIBRARY=str(LIBRARY))")
                if script.count(cache_env) != 1:
                    raise ValueError('QuartzCore runtime environment anchor changed upstream')
                script = script.replace(cache_env, cache_env[:-1] + ", NATIVE_METAL_FLOW='3')")
                immutable_guard = "    require(manifest(Path(str(prefix) + '.cache')) == original_manifest, 'Consumed/negative controls preserve every producer cache byte')\n"
                if script.count(immutable_guard) != 1:
                    raise ValueError('QuartzCore immutable-cache assertion changed upstream')
                immutable_diagnostic = (
                    "    observed_manifest = manifest(Path(str(prefix) + '.cache'))\n"
                    "    if observed_manifest != original_manifest:\n"
                    "        before_keys, after_keys = set(original_manifest), set(observed_manifest)\n"
                    "        cache_diff = {'removed': sorted(before_keys - after_keys),\n"
                    "                      'added': sorted(after_keys - before_keys),\n"
                    "                      'changed': sorted(name for name in before_keys & after_keys\n"
                    "                                        if original_manifest[name] != observed_manifest[name])}\n"
                    "        print('IOS16_QUARTZCORE_CACHE_DIFF', mode, json.dumps(cache_diff, sort_keys=True), flush=True)\n"
                    "    require(observed_manifest == original_manifest, 'Consumed/negative controls preserve every producer cache byte')\n"
                )
                script = script.replace(immutable_guard, immutable_diagnostic)
                compile(script, str(self.tree / 'scripts' / name), 'exec')
                (self.tree / 'scripts' / name).write_text(script)

        library = inputs / 'default.metallib'
        if not library.is_file() or base.sha256(library) != QUARTZCORE_DONOR_SHA256:
            self.unixnu_cmd('ipsw', 'fetch', 'OS', logfile=self.logs / 'quartzcore-donor-os.log')
            donor_os = self.out / 'ipsw' / self.board['ipsw']['build'] / base.OS_DMG
            with TemporaryDirectory(dir=self.scratch) as folder:
                mount = Path(folder) / 'mount'
                mount.mkdir()
                attached = plistlib.loads(subprocess.check_output([
                    'hdiutil', 'attach', '-readonly', '-nobrowse', '-owners', 'off',
                    '-mountpoint', str(mount), '-plist', str(donor_os),
                ]))
                device = next(e['dev-entry'] for e in attached['system-entities']
                              if e.get('content-hint') == 'GUID_partition_scheme')
                try:
                    source = mount / QUARTZCORE_LIBRARY
                    if base.sha256(source) != QUARTZCORE_DONOR_SHA256:
                        raise ValueError('QuartzCore library differs from the qualified 21H461 input')
                    shutil.copyfile(source, library)
                finally:
                    base.run(['hdiutil', 'detach', device], env=self.env)
        # The upstream priming checker assumes every run makes exactly one
        # compute creation and one software submission. That exact cold-miss
        # shape must remain enforced, while a successful device-open producer
        # may create additional initialization pipelines and submit barriers.
        # Keep all positive Vulkan feedback, cache-hit, negative-refusal, UUID,
        # file-integrity and live-object assertions intact.
        # Keep the patch in one testable helper; the same pinned checker is
        # verified in the cheap Actions preflight before any system build.
        from host_check_integration import patch_priming_checker
        priming_path = self.tree / 'scripts/check-priming-cache-host.py'
        priming_path.write_text(patch_priming_checker(priming_path.read_text()))
        self.env['DROIDVM_IOS16_METAL_HOST_CHECKS'] = '1'

    def verify_turnip_host_check(self):
        checks = self.stamps / 'checks.json'
        status = json.loads(checks.read_text()).get('turnip-host') if checks.is_file() else None
        if status != 'PASS':
            raise ValueError(f'Turnip host tests did not pass ({status or "missing result"}); '
                             f'see {self.logs / "turnip-host-check.log"}')

    def verify_kernel(self):
        image = self.work / 'kernel/kernel.macho'
        with image.open('rb') as stream:
            if struct.unpack('<4I', stream.read(16)) != (0xfeedfacf, 0x100000c, 0, 2):
                raise ValueError('Expected the linked ARM64 kernel')
        report = json.loads(image.with_suffix('.json').read_text())
        if report['original_sha256'] != KERNEL_DONOR or base.sha256(image) != report['candidate_sha256']:
            raise ValueError('Kernel does not match its prelink record and the audited donor')
        verify_kernel_volume_ids(image)
        verify_kernel_mach_ports(image)

    def step_payload(self):
        self.verify_kernel()
        if self.gpu_stage == 'metal':
            self.verify_turnip_host_check()
            self.verify_metal_deployment()
        if not (self.tree / 'build/iphone-x-identity.im4p').is_file():
            self.small_iphone_members()
        seed = json.loads((self.tree / 'build/owner-data-fixture/seed.json').read_text())
        uuid_value = str(uuid.UUID(seed['volume_uuid'])).upper()
        if f'CONFIG_XNU_OWNER_DATA_VOLUME_UUID="{uuid_value}"' not in (self.work / '.config').read_text().splitlines():
            raise ValueError('The Data seed and linked kernel UUID must match')
        identity = hashlib.blake2b(uuid.UUID(uuid_value).bytes, digest_size=8, person=b'droidvm-ios16').hexdigest()
        trust = self.gpu_trust if self.gpu_stage == 'metal' else self.trust
        seed_path = self.data / (METAL_SEED if self.gpu_stage == 'metal' else SEED)
        if not trust.is_file() or not seed_path.is_file():
            raise ValueError(f'Missing required {self.gpu_stage} trust cache or Data seed; run kernel stage first')
        trust_flag = '--owner-trust-cache' if self.gpu_stage == 'metal' else '--trust-cache'
        self.unixnu_cmd('build', 'payload', trust_flag, trust, '--data-ramdisk', seed_path,
                        '--unique-chip-id', identity, logfile=self.logs / 'payload.log')
        self.release.mkdir(parents=True, exist_ok=True)
        disk = self.release / 'ios16-darwin23.img'
        base.run(['bash', DEVICE / 'scripts/make-droidvm-disk.sh', self.work / 'payload/payload.img',
                  self.efi_dir(), disk], env=self.env, logfile=self.logs / 'disk.log')
        members = self.verify_disk(disk)
        report = {
            'target': 'iPhone10,3 / iOS 16.7.16 (20H392)',
            'kernel_backend': 'Darwin 23 / 21H461',
            'kernel_donor_sha256': KERNEL_DONOR,
            'data_volume_uuid': uuid_value,
            'gpu_stage': self.gpu_stage,
            'trust_cache_sha256': base.sha256(trust),
            'gpu_plugin': json.loads((self.tree / 'build/adreno-metal-plugin/trust.json').read_text())
                          if self.gpu_stage == 'metal' else None,
            'boot_disk': {'bytes': disk.stat().st_size, 'sha256': base.sha256(disk)},
            'volumes': PINS, 'fat': members, 'device_verified': False,
        }
        (self.release / 'release.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))

    def verify_disk(self, disk):
        paths = {'BOOTAA64.EFI': 'EFI/BOOT/BOOTAA64.EFI', 'unixnu.toml': 'EFI/UniXNU/unixnu.toml'}
        paths.update({name: f'EFI/UniXNU/Payloads/{name}' for name in
                      ('DarwinBoot.efi', 'kernel.macho', 'kernel.macho.adt', 'kernel.macho.trustcache', 'scratch-data.img')})
        with TemporaryDirectory(dir=self.work) as folder:
            stage = Path(folder)
            base.run(['mcopy', '-i', disk, *(f'::/{location}' for location in paths.values()), stage],
                     env={**self.env, 'MTOOLS_SKIP_CHECK': '1'})
            for name, expected in {
                'BOOTAA64.EFI': EFI_PINS['UniXNU.efi'], 'DarwinBoot.efi': EFI_PINS['DarwinBoot.efi'],
                'kernel.macho': base.sha256(self.work / 'kernel/kernel.macho'),
                'kernel.macho.trustcache': base.sha256(self.gpu_trust) if self.gpu_stage == 'metal' else TRUST_SHA256,
                'scratch-data.img': base.sha256(self.data / (METAL_SEED if self.gpu_stage == 'metal' else SEED)),
            }.items():
                if base.sha256(stage / name) != expected:
                    raise ValueError(f'FAT member {name} differs from its verified source')
            record = json.loads((self.work / 'payload/payload.json').read_text())
            adt = stage / 'kernel.macho.adt'
            if base.sha256(adt) != record['adt_sha256']:
                raise ValueError('The final FAT device tree differs from the prepared tree')
            decode = runpy.run_path(str(DEVICE / 'scripts/make-droidvm-adt.py'))['read_adt']
            payload = adt.read_bytes()
            (tree, children), consumed = decode(payload)
            chosen, _ = next((props, kids) for props, kids in children if props['name'] == b'chosen\0')
            if (consumed != len(payload) or tree['model'] != b'iPhone10,3\0'
                    or PINS['os']['uuid'].encode() not in chosen.get('root-matching', b'')):
                raise ValueError('The finished device tree must select the D22AP root and the verified iPhone OS volume')
            config = tomllib.loads((stage / 'unixnu.toml').read_text())
            boot = next(entry for entry in config['entry'] if entry['id'] == config['boot']['default'])
            if config['driver'] != [{'path': 'Payloads/DarwinBoot.efi'}] or boot['path'] != 'Payloads/kernel.macho':
                raise ValueError('The finished FAT does not boot through the checked EFI and kernel')
            return {name: {'bytes': (stage / name).stat().st_size, 'sha256': base.sha256(stage / name)} for name in paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=('configure', 'kernel', 'payload'))
    parser.add_argument('--gpu-stage', choices=GPU_STAGES, default='off',
                        help='Separate GPU experiment: off (original #6), 3d (self-test), metal (plugin)')
    parser.add_argument('--unixnu', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--efi-dir', type=Path, required=True)
    args = parser.parse_args()
    build = HybridBuild(args)
    build.run_step('setup', build.configure)
    if args.stage == 'kernel':
        build.run_kernel()
    elif args.stage == 'payload':
        build.step_payload()


if __name__ == '__main__':
    main()
