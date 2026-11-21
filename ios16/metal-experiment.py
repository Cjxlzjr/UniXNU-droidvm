#!/usr/bin/env python3
"""Strict iOS 16 deployment-target adaptation for the pinned Darwin 23 core.

Only edits generated build-tree copies, never the upstream/core checkout.
The Mach-O audit confirms deployment metadata, not runtime Metal ABI support.
"""
import argparse
import json
from pathlib import Path
import struct

PATCHES = {
    "scripts/build-turnip-darwin.py": [
        ("'ios': 'arm64-apple-ios17.0'", "'ios': 'arm64-apple-ios16.0'"),
    ],
    "scripts/build-adreno-metal-plugin.py": [
        ("'ios': 'arm64-apple-ios17.0'", "'ios': 'arm64-apple-ios16.0'"),
        ("'MinimumOSVersion': '27.0' if IOS27 else '17.0'",
         "'MinimumOSVersion': '27.0' if IOS27 else '16.0'"),
    ],
    "scripts/build-adreno-metal-bridge.py": [
        ("IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '17.0'",
         "IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '16.0'"),
    ],
}


def patched(text, substitutions, filename):
    for old, new in substitutions:
        if old in text:
            if text.count(old) != 1:
                raise ValueError(f"{filename}: ambiguous deployment target anchor: {old}")
            text = text.replace(old, new)
        elif text.count(new) != 1:
            raise ValueError(f"{filename}: pinned upstream script changed; missing {old!r}")
    return text


def apply(tree):
    manifest = []
    for relative, rules in PATCHES.items():
        source = tree / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        before = source.read_text()
        after = patched(before, rules, relative)
        if before != after:
            source.write_text(after)
        manifest.append({"path": relative, "target": "iOS 16.0",
                         "changed": before != after, "anchors": len(rules)})
    target = tree / "build/ios16-metal"
    target.mkdir(parents=True, exist_ok=True)
    (target / "deployment-anchors.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


def version(value):
    return f"{value >> 16}.{(value >> 8) & 255}.{value & 255}"


def macho_min_version(path):
    data = path.read_bytes()
    if len(data) < 32:
        raise ValueError(f"{path}: file too short")
    magic, cpu, subtype, kind, count, commands_size, flags, reserved = struct.unpack_from("<IiiIIIII", data)
    if magic != 0xfeedfacf or cpu != 0x100000c or (subtype & 0xffffff) != 0:
        raise ValueError(f"{path}: not an arm64 Mach-O (or unexpected arm64e slice)")
    pos, end = 32, 32 + commands_size
    if end > len(data):
        raise ValueError(f"{path}: invalid load-command length")
    minimum = None
    for _ in range(count):
        if pos + 8 > end:
            raise ValueError(f"{path}: truncated command")
        cmd, size = struct.unpack_from("<II", data, pos)
        if size < 8 or pos + size > end:
            raise ValueError(f"{path}: malformed command")
        if cmd == 0x32:  # LC_BUILD_VERSION
            if size < 24:
                raise ValueError(f"{path}: short LC_BUILD_VERSION")
            platform, minos = struct.unpack_from("<II", data, pos + 8)
            if platform != 2:  # PLATFORM_IOS
                raise ValueError(f"{path}: LC_BUILD_VERSION is not for iOS: {platform}")
            minimum = minos
        elif cmd == 0x25:  # LC_VERSION_MIN_IPHONEOS
            if size < 16:
                raise ValueError(f"{path}: short LC_VERSION_MIN_IPHONEOS")
            minimum = struct.unpack_from("<I", data, pos + 8)[0]
        pos += size
    if minimum is None or minimum >> 16 > 16:
        raise ValueError(f"{path}: minimum iOS version {version(minimum) if minimum else 'missing'}")
    return {"path": str(path), "cpu": "arm64", "min_ios": version(minimum), "file_type": kind}


def selftest():
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as temporary:
        base = Path(temporary)
        for relative, rules in PATCHES.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(old for old, _ in rules) + "\n")
        apply(base)
        apply(base)
        for relative, rules in PATCHES.items():
            result = (base / relative).read_text()
            assert all(result.count(new) == 1 for _, new in rules)
            assert all(old not in result for old, _ in rules)
        bad = base / next(iter(PATCHES))
        bad.write_text("upstream changed\n")
        try:
            apply(base)
        except ValueError:
            pass
        else:
            raise AssertionError("modified upstream anchor was not rejected")
    print("PASS: iOS16 target patches are strict and idempotent")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("patch", "audit", "selftest"))
    parser.add_argument("tree", nargs="?", type=Path)
    args = parser.parse_args()
    if args.command == "selftest":
        selftest()
    elif args.command == "patch":
        if args.tree is None:
            parser.error("patch needs the generated tree directory")
        apply(args.tree)
    else:
        if args.tree is None:
            parser.error("audit needs the plugin build directory")
        folder = args.tree
        paths = [folder / "NativeAdrenoMetal.bundle/NativeAdrenoMetal",
                 folder / "NativeAdrenoMetal.bundle/libvulkan_freedreno.dylib",
                 folder / "adrenoprobed"]
        print(json.dumps([macho_min_version(path) for path in paths], indent=2))


if __name__ == "__main__":
    main()
