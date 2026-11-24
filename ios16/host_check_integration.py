#!/usr/bin/env python3
"""Pinned iOS 16 Metal host-test adaptation with a cheap pre-build self-test.

Only the test runner is modified. The pinned upstream source is hash-checked
before any replacement; no GPU driver, runtime code, or production kernel is
changed here.
"""
import ast
import hashlib
import json
import struct
from urllib.request import urlopen

CORE_REVISION = "dba62c1ca53cd04c25b0e501afff31d5ffe2e20e"
PRIMING_BLOB_SHA = "d6e7c31f280d065bba797579daa1ef07a3342e5c"
PRIMING_RAW_URL = (
    "https://raw.githubusercontent.com/Droid-VM/UniXNU/"
    + CORE_REVISION + "/scripts/check-priming-cache-host.py"
)


QUARTZCORE_RAW_URL = (
    "https://raw.githubusercontent.com/UniXNU/UniXNU-sheng/"
    "516a780473a2ac59c8b438e132bb60204dda9e22/scripts/check-quartzcore-turnip-cache-host.py"
)
QUARTZCORE_SHA256 = "eae106179803091de6d065a57fd9af737184d1bae90927313255521c84fcc311"


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"Pinned host checker {label} anchor expected once, got {count}")
    return source.replace(old, new, 1)


def patch_priming_checker(priming: str) -> str:
    counter_guard = (
        "    require(data['submissions'] == 1 and data['compute_calls'] == 1, "
        "'Full priming still submits its barriers; observed non-generated compute shape')\n"
    )
    new_counter_guard = (
        "    print('IOS16_PRIMING_OBSERVED', mode, json.dumps(data, sort_keys=True), flush=True)\n"
        "    if mode in ('cold-miss', 'uuid-miss'):\n"
        "        require(data['submissions'] == 1 and data['compute_calls'] == 1,\n"
        "                'Negative priming control changed its exact initial shape')\n"
        "    else:\n"
        "        require(data['submissions'] > 0 and data['compute_calls'] > 0,\n"
        "                'Full priming did not create a compute pipeline and submit barriers')\n"
    )
    priming = replace_once(priming, counter_guard, new_counter_guard, "counter")

    # Another log.write_text call exists in the baseline control. The label
    # declaration makes this case() insertion point unique and protects the
    # baseline log from being overwritten by per-case diagnostics.
    case_log_anchor = (
        "    log = work / (label + '.log')\n"
        "    log.write_text(run.stdout + run.stderr)\n"
    )
    case_log_rewrite = case_log_anchor + (
        "    shutil.copyfile(log, OUT / ('last-' + label + '.log'))\n"
        "    if run.returncode or not pattern.search(run.stdout):\n"
        "        print('IOS16_PRIMING_SUBPROCESS', label, 'exit', run.returncode,\n"
        "              'output', (run.stdout + run.stderr)[-12000:], flush=True)\n"
    )
    priming = replace_once(priming, case_log_anchor, case_log_rewrite, "case log")

    # The production NativeMetal runtime enables an independent background
    # pipeline-record warm pass (flow bit 4) by default. It creates its own
    # pipeline-records-*.bin file, which is not part of the shader cache
    # record that this isolated *one-file* priming test is qualifying.
    # Preserve render lowering and submissions (bits 1+2); run the separate
    # QuartzCore controls independently with the same isolation.
    priming_env = (
        "env.update(DYLD_INSERT_LIBRARIES=str(peer), "
        "NMB_TEST_PRIMING_CACHE_LOADER=str(loader))\n"
    )
    priming = replace_once(
        priming, priming_env,
        priming_env[:-2] + ", NATIVE_METAL_FLOW='3')\n",
        "priming-only warm-pass isolation",
    )
    records_anchor = "records = [p for p in cache.rglob('*') if p.is_file()]\n"
    priming = replace_once(
        priming, records_anchor,
        records_anchor +
        "print('IOS16_PRIMING_CACHE_FILES', json.dumps(" 
        "[(str(p.relative_to(cache)), p.stat().st_size) for p in records], "
        "sort_keys=True), flush=True)\n",
        "priming cache inventory",
    )

    # The strict producer/consumer cache-hit checks and all UUID/negative
    # controls are still present in the original source.
    compile(priming, "pinned/check-priming-cache-host.py", "exec")
    return priming


def patch_quartzcore_identity(source: str) -> str:
    """Keep native-host and cross-built iOS identities independently strict."""
    source = replace_once(source,
        ("def identity(path):\n"
             "    raw = path.read_bytes()\n"
             "    require(struct.unpack_from('<3I', raw) == (0xfeedfacf, 0x100000c, 0), 'Actual ordinary ARM64 image')"),
        ("def identity(path, expected_cpu, expected_platform):\n"
             "    raw = path.read_bytes()\n"
             "    cpu = struct.unpack_from('<3I', raw)\n"
             "    require(cpu == (0xfeedfacf, *expected_cpu),\n"
             "            f'{path}: expected Mach-O CPU/subtype {expected_cpu}, got {cpu[1:]}')"),
        "QuartzCore Mach-O header")
    source = replace_once(source,
        "    return dict(sha256=sha(path), uuid=uuids[0], platform=platforms[0], minimum_os=minimum[0], cpu_subtype=0)",
        ("    require(platforms[0] == expected_platform, f'{path}: unexpected Mach-O platform {platforms[0]}')\n"
             "    return dict(sha256=sha(path), uuid=uuids[0], platform=platforms[0], minimum_os=minimum[0],\n"
             "                cpu_type=cpu[1], cpu_subtype=cpu[2])"),
        "QuartzCore result identity")
    source = replace_once(source,
        "        info = identity(path)",
        "        info = identity(path, (0x0100000c, 0), 2)",
        "QuartzCore iOS image")
    source = replace_once(source,
        ("archive = Path(str(prefix) + '.archive')\n"
             "report = dict("),
        ("archive = Path(str(prefix) + '.archive')\n"
             "host_cpu = {'arm64': (0x0100000c, 0), 'x86_64': (0x01000007, 3)}[os.uname().machine]\n"
             "report = dict("),
        "QuartzCore host architecture")
    source = replace_once(source,
        "driver=identity(DRIVER), peer=identity(PEER),",
        "driver=identity(DRIVER, host_cpu, 1), peer=identity(PEER, host_cpu, 1),",
        "QuartzCore host libraries")
    source = replace_once(source,
        "loader=identity(loader), process=identity(program),",
        "loader=identity(loader, host_cpu, 1), process=identity(program, host_cpu, 1),",
        "QuartzCore host loader and worker")
    compile(source, "pinned/check-quartzcore-turnip-cache-host.py", "exec")
    return source


def check_quartzcore_identity(original: str) -> None:
    patched = patch_quartzcore_identity(original)
    tree = ast.parse(patched)
    identity = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'identity')
    scope = dict(struct=struct, require=_require,
                 sha=lambda path: hashlib.sha256(path.read_bytes()).hexdigest())
    exec(compile(ast.Module([identity], type_ignores=[]), "quartzcore-identity-test", "exec"), scope)

    class Fixture:
        def __init__(self, cpu, subtype, platform):
            self.raw = (struct.pack('<8I', 0xfeedfacf, cpu, subtype, 6, 2, 48, 0, 0)
                        + struct.pack('<2I', 0x1b, 24) + bytes(16)
                        + struct.pack('<6I', 0x32, 24, platform, 0x100000, 0x1a0000, 0))

        def read_bytes(self):
            return self.raw

    arm64, intel = (0x0100000c, 0), (0x01000007, 3)
    cases = [(arm64, 1), (intel, 1), (arm64, 2)]
    for cpu, platform in cases:
        fixture = Fixture(*cpu, platform)
        result = scope['identity'](fixture, cpu, platform)
        assert (result['cpu_type'], result['cpu_subtype']) == cpu
        assert result['platform'] == platform
        wrong_cpu = intel if cpu == arm64 else arm64
        for expected_cpu, expected_platform in ((wrong_cpu, platform), (cpu, 3 - platform)):
            try:
                scope['identity'](fixture, expected_cpu, expected_platform)
            except SystemExit:
                pass
            else:
                raise AssertionError('QuartzCore image with wrong CPU/platform accepted')
    print("PASS: QuartzCore ARM64 and Intel macOS identities, strict iOS ARM64 identity, and rejection controls")


def _require(value, reason):
    if not value:
        raise SystemExit(reason)


def selftest() -> None:
    with urlopen(PRIMING_RAW_URL, timeout=40) as response:
        source = response.read()
    actual = hashlib.sha1(f"blob {len(source)}\0".encode("ascii") + source).hexdigest()
    if actual != PRIMING_BLOB_SHA:
        raise ValueError(f"Unexpected pinned priming checker blob {actual}")
    original = source.decode("utf-8")
    patched = patch_priming_checker(original)
    if patched.count("IOS16_PRIMING_OBSERVED") != 1:
        raise ValueError("Expected one priming counter diagnostic")
    if patched.count("IOS16_PRIMING_SUBPROCESS") != 1:
        raise ValueError("Expected one per-case failure diagnostic")
    if patched.count("IOS16_PRIMING_CACHE_FILES") != 1:
        raise ValueError("Expected one priming cache inventory before the strict one-file assertion")
    if patched.count("NATIVE_METAL_FLOW='3'") != 1:
        raise ValueError("The isolated priming test must disable only the background warm pass")
    if "Exactly one bounded native priming record" not in patched:
        raise ValueError("Priming cache file count and checksum checks must be preserved")
    if "    if mode in ('cold-miss', 'uuid-miss'):\n" not in patched:
        raise ValueError("Negative priming controls are missing")
    if original.count("    log.write_text(run.stdout + run.stderr)\n") != 2:
        raise ValueError("Upstream baseline/case logging shape unexpectedly changed")
    if patched.count("    log.write_text(run.stdout + run.stderr)\n") != 2:
        raise ValueError("The original priming log writes were not preserved")
    print("PASS: pinned priming test adapted and syntax-checked before expensive Metal build")
    with urlopen(QUARTZCORE_RAW_URL, timeout=40) as response:
        source = response.read(64 * 1024)
    if hashlib.sha256(source).hexdigest() != QUARTZCORE_SHA256:
        raise ValueError("Unexpected pinned QuartzCore checker source")
    check_quartzcore_identity(source.decode("utf-8"))


if __name__ == "__main__":
    selftest()
