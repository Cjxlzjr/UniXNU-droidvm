#!/usr/bin/env python3
"""Drive the Turnip darwin backend through a real Vulkan instance against the host copy of the NativeAdreno kernel stack.

The kernel half is the real GPU context bring-up, VM, ring, submit and user-client code over software SMMU/CP peers
(tests/hostsim/host_gpu_service.cpp); an IOKit shim answers the backend's IOKit calls. Nothing here runs a shader or touches
hardware: the software CP retires every submission at once. Needs the host Turnip build (build-turnip-darwin.py --target host)."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
kit = ROOT / 'drivers/Adreno740'
sim = kit / 'tests/hostsim'
out = ROOT / 'build/turnip-darwin-host'
out.mkdir(parents=True, exist_ok=True)
# This legacy DroidVM board's Adreno740 source is the NativeAdreno submodule
# (Droid-VM/UniXNU wip/hackintosh, drivers/Adreno740 at 627836d). Its hostsim
# expects ArmSmmu4KLiveArena.mapping/capacity_pages, before the on-demand table
# API change at ArmSmmu4K 48dbd6c (2026-10-31). The main kernel uses the newer
# ArmSmmu4K pin, so isolate the older kit strictly to this *host simulation*.
# Do not change the production kernel's SMMU headers or sources.
HOSTSIM_SMMU_REV = '3ba92451bd44bb742d11758fbfcd3a446ecad685'
smmu = out / 'legacy-ArmSmmu4K'
if not (smmu / '.git').is_dir():
    # Do not use a promisor/partial clone here: checkout of the pinned legacy
    # revision must have its complete source blobs locally. GitHub Actions has
    # no interactive credentials for an on-demand checkout blob fetch.
    subprocess.run(['git', 'clone', '-q', '--depth', '1', '--no-checkout',
                    'https://github.com/UniXNU/ArmSmmu4K.git', str(smmu)], check=True)
current = subprocess.run(['git', '-C', str(smmu), 'rev-parse', 'HEAD'],
                         capture_output=True, text=True, check=True).stdout.strip()
if current != HOSTSIM_SMMU_REV:
    subprocess.run(['git', '-C', str(smmu), 'fetch', '-q', '--depth', '1', 'origin', HOSTSIM_SMMU_REV], check=True)
    subprocess.run(['git', '-C', str(smmu), 'checkout', '-q', '--detach', HOSTSIM_SMMU_REV], check=True)
legacy_header = (smmu / 'ArmSmmu4KLive.h').read_text()
if 'volatile uint64_t *mapping;' not in legacy_header or 'size_t capacity_pages;' not in legacy_header:
    sys.exit('hostsim ArmSmmu4K pin lacks the legacy arena mapping/capacity_pages ABI')
print('hostsim-only ArmSmmu4K revision:', HOSTSIM_SMMU_REV, flush=True)
library = ROOT / 'build/mesa-tu/src/freedreno/vulkan/libvulkan_freedreno.dylib'
if not library.exists():
    sys.exit('Build the host Turnip first: python3 scripts/build-turnip-darwin.py --target host')

includes = out / 'host-include'
for name in ['IOKit/IOTypes.h', 'IOKit/IOReturn.h', 'IOKit/IOBufferMemoryDescriptor.h', 'IOKit/IOLib.h', 'kern/task.h']:
    path = includes / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#include "XnuMemoryFixture.h"\n')

objects = []
for stem in ['ArmSmmu4KContext', 'ArmSmmu4KProgram', 'ArmSmmu4KLive', 'ArmSmmu4KStreams', 'ArmSmmu4KMmio']:
    obj = out / (stem + '.o')
    subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-fPIC', '-c', smmu / (stem + '.c'), '-o', obj], check=True)
    objects.append(obj)
obj = out / 'AdrenoPM4.o'
subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-fPIC', '-c', ROOT / 'kits/AdrenoPM4/AdrenoPM4.c', '-o', obj], check=True)
objects.append(obj)

service = out / 'host_gpu_service.o'
subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-fPIC', '-I', includes, '-I', sim.parent, '-I', kit, '-I', smmu,
                '-I', ROOT / 'kits/AdrenoPM4', '-c', sim / 'host_gpu_service.cpp', '-o', service], check=True)
core = []
for name in ['NativeAdrenoMemory', 'NativeAdrenoSmmuMemory', 'NativeAdrenoVm', 'NativeAdrenoRing', 'NativeAdrenoSubmit',
             'NativeAdrenoUserClient', 'NativeAdrenoTimer', 'NativeAdrenoSmmuContext', 'NativeAdrenoGpuContext']:
    obj = out / (name + '.o')
    subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-fPIC', '-I', includes, '-I', sim.parent, '-I', kit, '-I', smmu,
                    '-I', ROOT / 'kits/AdrenoPM4', '-c', kit / (name + '.cpp'), '-o', obj], check=True)
    core.append(obj)
shim_obj = out / 'host_iokit_shim.o'
# Make both IOKit transport routes test the same watchdog timeout.
# This is a generated host-only shim; all production sources remain pinned.
subprocess.run([sys.executable, ROOT / 'scripts/hostsim_watchdog_patch.py',
                '--input', sim / 'host_iokit_shim.cpp',
                '--output', out / 'host_iokit_shim.watchdog.cpp'], check=True)
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-fPIC',
                '-c', out / 'host_iokit_shim.watchdog.cpp', '-o', shim_obj], check=True)
shim = out / 'libhostiokit.dylib'
subprocess.run(['xcrun', 'clang++', '-dynamiclib', '-o', shim, service, shim_obj, *core, *objects,
                '-framework', 'IOKit', '-framework', 'CoreFoundation'], check=True)
smoke = out / 'vulkan_smoke'
subprocess.run(['xcrun', 'clang', '-std=c11', '-O1', '-I', '/opt/homebrew/include', sim / 'vulkan_smoke.c', '-o', smoke,
                '-Wl,-rpath,' + str(out)], check=True)

plugin_smoke = out / 'plugin_smoke'
subprocess.run([sys.executable, ROOT / 'scripts/gen-turnip-smoke-shaders.py'], check=True)
subprocess.run(['xcrun', 'clang++', '-std=c++17', '-O1', '-I', ROOT / 'drivers/AdrenoMetal', '-I', ROOT / '.cache/mesa-work/include',
                '-I', ROOT / 'build/turnip-smoke',
                sim / 'plugin_smoke.cpp', '-o', plugin_smoke], check=True)
env = dict(os.environ, DYLD_INSERT_LIBRARIES=str(shim), MESA_LOG='')
env.pop('TU_DARWIN_SERVICE', None)   # the default is the production transport: accelerator service, open type 0x4147, tunnel through one admitted selector
result = subprocess.run([str(smoke), str(library)], env=env, capture_output=True, text=True, timeout=300)
(out / 'smoke.log').write_text(result.stdout + result.stderr)
print(result.stdout, end='')
if result.stderr.strip():
    print(result.stderr[-3000:], end='')
if result.returncode or 'PASS' not in result.stdout:
    sys.exit('Turnip darwin host smoke failed (exit %d); see %s' % (result.returncode, out / 'smoke.log'))
# Keep a software ring fence pending while freeing an unrelated mapped allocation.
# The actual KMD must defer native handle/view/IOVA retirement, bound pressure waits,
# and preserve ownership on failures. This exercises real transport code, not pixels.
deferred = out / 'deferred_bo'
ios16_inputs = os.environ.get('DROIDVM_IOS16_METAL_HOST_CHECKS') == '1'
deferred_source = (ROOT / 'build/turnip-host-inputs/deferred_bo.cpp' if ios16_inputs else sim / 'deferred_bo.cpp')
subprocess.run(['xcrun', 'clang++', '-std=c++23', '-O1', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                '-I', '/opt/homebrew/include', '-I', kit, deferred_source, '-o', deferred], check=True)
deferred_env = dict(env, TU_DARWIN_RETIRE_TRACE='1')
deferred_env.pop('HOSTSIM_FAIL_USER_UNMAP', None)
for mode in ['normal', 'busy', 'fence-error', 'free-error', 'lease-error', 'view-error', 'map-cleanup-error', 'pressure']:
    # The release paths need every free to reach the kernel: only the normal mode runs with the BO cache on.
    mode_env = dict(deferred_env, TU_DARWIN_BO_CACHE='1' if mode == 'normal' else '0')
    test = subprocess.run([str(deferred), str(library), mode], env=mode_env, capture_output=True, text=True, timeout=60)
    (out / ('deferred-' + mode + '.log')).write_text(test.stdout + test.stderr)
    verdict = [line for line in test.stdout.splitlines() if line.startswith('PASS:')]
    if test.returncode or not verdict:
        sys.exit('Deferred BO %s failed (exit %d); see %s' % (mode, test.returncode, out / ('deferred-' + mode + '.log')))
    if mode == 'pressure' and 'pressure=0' not in test.stderr:
        sys.exit('Deferred BO pressure control waited for the GPU instead of growing its queue')
    print(verdict[0])
# Driver cache controls compile Adreno code but submit/execute no shader.
subprocess.run([sys.executable, ROOT / 'scripts/check-turnip-pipeline-cache-host.py'], check=True)
# Original UI recipes additionally cover graphics-library caches, strict driver
# feedback and genuine UUID/empty-cache refusals. The software peer runs no shader.
if ios16_inputs:
    subprocess.run([sys.executable, ROOT / 'scripts/build-adreno-metal-bridge.py', '--target', 'host'], check=True)
# The regression suites are independent. Collect both outcomes and proceed to
# the plugin smoke/watchdog so a single CI run exposes more than the first
# failure. Never mark the host check PASS when any suite fails.
host_regressions = []

def run_host_regression(label, path, output, timeout):
    args = [sys.executable, ROOT / 'scripts' / path, '--output', output]
    try:
        result = subprocess.run(args, check=False, timeout=timeout)
        if result.returncode:
            error = f'{label}: exit={result.returncode}'
            host_regressions.append(error)
            print('FAIL host regression:', error, flush=True)
    except subprocess.TimeoutExpired:
        error = f'{label}: timeout after {timeout}s'
        host_regressions.append(error)
        print('FAIL host regression:', error, flush=True)

with tempfile.TemporaryDirectory(prefix='quartzcore-driver-cache-', dir=out) as temporary:
    run_host_regression('quartzcore', 'check-quartzcore-turnip-cache-host.py',
                        Path(temporary) / 'control', 660)

with tempfile.TemporaryDirectory(prefix='priming-driver-cache-', dir=out) as temporary:
    run_host_regression('priming', 'check-priming-cache-host.py',
                        Path(temporary) / 'control', 360)

env_plugin = dict(env)
plugin = subprocess.run([str(plugin_smoke), str(library)], env=env_plugin, capture_output=True, text=True, timeout=300)
(out / 'plugin_smoke.log').write_text(plugin.stdout + plugin.stderr)
print(plugin.stdout, end='')
if plugin.returncode:
    sys.exit('The plugin Turnip sequence did not behave as expected on the host (exit %d); see %s' % (plugin.returncode, out / 'plugin_smoke.log'))
# Watchdog: block the first BoAlloc for 20 s; the 15 s sample must name the last marker (6004), show the worker waiting (not running) and
# carry at least one return address inside the Turnip image ('v' frames).
stalled = subprocess.run([str(plugin_smoke), str(library)],
                         env=dict(env_plugin, HOSTSIM_STALL='20', HOSTSIM_TRACE='1'),
                         capture_output=True, text=True, timeout=300)
(out / 'plugin_watchdog.log').write_text(stalled.stdout + stalled.stderr)
stall_events = [line for line in stalled.stderr.splitlines() if line.startswith('HOSTSIM_STALL_TRIGGERED')]
if not stall_events:
    sys.exit('Watchdog stall never reached a BO allocation via either IOKit transport; see ' + str(out / 'plugin_watchdog.log'))
print('\n'.join(stall_events), flush=True)
lines = [l for l in stalled.stdout.splitlines() if l.startswith('watchdog: W ')]
print('\n'.join(lines))
if stalled.returncode or len(lines) != 1 or not lines[0].startswith('watchdog: W 6004 ') or ' v' not in lines[0] or 'kern' in lines[0]:
    sys.exit('The plugin watchdog did not sample the stalled worker as expected; see %s' % (out / 'plugin_watchdog.log'))
if host_regressions:
    sys.exit('FAIL: independent host regressions: ' + '; '.join(host_regressions))
print('PASS: the plugin watchdog sampled a worker blocked in vkCreateDevice (one line, marker 6004, frames inside Turnip)')
print('PASS: the plugin Turnip sequence (fill, compute pipeline, render pass) built, submitted and retired every command buffer over the host kernel stack (the software CP executes nothing, so no result is verified by design)')
print('PASS: Turnip darwin backend drove a Vulkan instance, device, memory mapping and a submission over the host kernel stack; no GPU')
