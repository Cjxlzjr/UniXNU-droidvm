#!/usr/bin/env python3
"""Build an additive iOS 16 owner trust module while preserving all Apple modules.

Input is the verified #6 three-module static/SystemOS/AppOS trust bundle.
Do not reuse the iPadOS owner trust helper: its pinned Apple hash is different.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import uuid


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def verify_module(module):
    if len(module) < 24 or struct.unpack_from("<I", module)[0] != 2:
        raise ValueError("Not a complete version-2 trust module")
    count = struct.unpack_from("<I", module, 20)[0]
    if len(module) != 24 + 24 * count:
        raise ValueError("Incorrect trust entry count or byte length")
    previous = None
    for index in range(count):
        offset = 24 + 24 * index
        entry = module[offset:offset + 20]
        if previous is not None and previous >= entry:
            raise ValueError("Trust module entries not strictly sorted")
        if not 1 <= module[offset + 20] <= 4:
            raise ValueError("Invalid trust hash type")
        previous = entry


def unpack_bundle(data):
    if len(data) < 8:
        raise ValueError("Truncated trust bundle")
    count = struct.unpack_from("<I", data)[0]
    if not 1 <= count <= 128 or len(data) < 4 + 4 * count:
        raise ValueError("Invalid trust module count")
    offsets = list(struct.unpack_from(f"<{count}I", data, 4))
    if offsets[0] != 4 + 4 * count or offsets != sorted(offsets) or len(set(offsets)) != count:
        raise ValueError("Invalid trust module offsets")
    modules = [data[begin:end] for begin, end in zip(offsets, offsets[1:] + [len(data)])]
    if any(begin % 4 for begin in offsets):
        raise ValueError("Unaligned trust offsets")
    for module in modules:
        verify_module(module)
    return modules


def pack_bundle(modules):
    cursor = 4 + 4 * len(modules)
    offsets = []
    for module in modules:
        if len(module) % 4:
            raise ValueError("Unaligned trust module")
        verify_module(module)
        offsets.append(cursor)
        cursor += len(module)
    packed = struct.pack(f"<{len(modules)+1}I", len(modules), *offsets) + b"".join(modules)
    if unpack_bundle(packed) != modules:
        raise ValueError("Trust bundle did not round-trip")
    return packed


def make_owner(original, expected_hash, plugin):
    if sha256(original) != expected_hash:
        raise ValueError("Original Apple trust bundle SHA-256 differs from pinned #6")
    modules = unpack_bundle(original)
    if len(modules) != 3:
        raise ValueError(f"Expected three unchanged iOS 16 Apple modules, got {len(modules)}")
    hashes = []
    for key in ("cdhash", "daemon_cdhash", "turnip_cdhash"):
        value = plugin.get(key)
        if not isinstance(value, str) or len(value) != 40:
            raise ValueError(f"Missing or invalid plugin CDHash {key}")
        hashes.append(bytes.fromhex(value))
    if len(set(hashes)) != len(hashes):
        raise ValueError("Duplicate plugin CDHash")
    hashes.sort()
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "org.droidvm.ios16.metal:" + "".join(v.hex() for v in hashes))
    own = struct.pack("<I16sI", 2, namespace.bytes, len(hashes))
    own += b"".join(value + bytes([2, 0, 0, 0]) for value in hashes)
    result = pack_bundle([own, *modules])
    if unpack_bundle(result)[1:] != modules:
        raise ValueError("Original Apple trust modules changed")
    return result, [h.hex() for h in hashes], [sha256(x) for x in modules]


def selftest():
    def module(values):
        sorted_values = sorted(values)
        return (struct.pack("<I16sI", 2, bytes(16), len(sorted_values)) +
                b"".join(value + bytes([2, 0, 0, 0]) for value in sorted_values))
    original_modules = [module([bytes([n]) * 20]) for n in (5, 6, 7)]
    original = pack_bundle(original_modules)
    plugin = {k: bytes([n])*20 for k, n in (("cdhash", 1), ("daemon_cdhash", 3), ("turnip_cdhash", 2))}
    plugin = {k: v.hex() for k, v in plugin.items()}
    new, hashes, checksums = make_owner(original, sha256(original), plugin)
    assert unpack_bundle(new)[1:] == original_modules
    assert len(hashes) == 3 and len(checksums) == 3
    try:
        make_owner(original, "0"*64, plugin)
    except ValueError:
        pass
    else:
        raise AssertionError("Tampered original hash not rejected")
    print("PASS: additive iOS16 trust module; exact Apple module preservation and tamper refusal")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--expected-original")
    parser.add_argument("--plugin", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        return
    if any(value is None for value in (args.original, args.expected_original, args.plugin, args.output)):
        parser.error("Need --original, --expected-original, --plugin and --output")
    blob, hashes, apple = make_owner(args.original.read_bytes(), args.expected_original,
                                    json.loads(args.plugin.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(blob)
    report = {"bundle_sha256": sha256(blob), "original_apple_sha256": args.expected_original,
              "apple_module_sha256": apple, "plugin_cdhashes": hashes,
              "apple_modules_unchanged": True,
              "device_verified": False}
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
