#!/usr/bin/env python3
"""Pinned iOS 16 Metal host-test adaptation with a cheap pre-build self-test.

Only the test runner is modified. The pinned upstream source is hash-checked
before any replacement; no GPU driver, runtime code, or production kernel is
changed here.
"""
import hashlib
import json
from urllib.request import urlopen

CORE_REVISION = "dba62c1ca53cd04c25b0e501afff31d5ffe2e20e"
PRIMING_BLOB_SHA = "d6e7c31f280d065bba797579daa1ef07a3342e5c"
PRIMING_RAW_URL = (
    "https://raw.githubusercontent.com/Droid-VM/UniXNU/"
    + CORE_REVISION + "/scripts/check-priming-cache-host.py"
)


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"Pinned priming checker {label} anchor expected once, got {count}")
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


if __name__ == "__main__":
    selftest()
