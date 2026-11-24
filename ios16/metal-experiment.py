#!/usr/bin/env python3
"""Strict iOS 16 Metal adaptation for the pinned Darwin 23 core.

Only edits generated build-tree copies, never the upstream/core checkout.
The Mach-O audit confirms deployment metadata, not runtime Metal ABI support.
"""
import argparse
import json
from pathlib import Path
import plistlib
import struct

PATCHES = {
    "drivers/AdrenoMetal/NativeBridgeMetalFrontend.mm": [
        # The iPhone10,3 20H392 ARM64 dyld cache records this class_ro_t instanceSize.
        (" // The target's base class ABI (iPadOS 17.7.11, 536 bytes): the dynamic subclass adds no instance variables and reads none of the base's,\n"
         " // so only 17's size is held to; the arm64e build (iPadOS 27, whose _MTLDevice is larger) needs the class to exist.",
         " // iPhone10,3 on iOS 16.7.16 (20H392) has a 528-byte _MTLDevice. The subclass adds no instance variables.\n"
         " // Keep the runtime size check for the ARM64 iOS 16 build."),
        (' if(!base || class_getInstanceSize(base)!=536){NBLOG(@"native-metal: unexpected _MTLDevice ABI");return nil;}',
         ' if(!base || class_getInstanceSize(base)!=528){NBLOG(@"native-metal: unexpected _MTLDevice ABI for 20H392 (expected 528)");return nil;}'),
    ],
    "scripts/build-turnip-darwin.py": [
        ("'ios': 'arm64-apple-ios17.0'", "'ios': 'arm64-apple-ios16.0'"),
        ("'ios': 'build/mesa-tu-ios'", "'ios': 'build/mesa-tu-ios16'"),
    ],
    "scripts/build-adreno-metal-plugin.py": [
        ("'ios': 'arm64-apple-ios17.0'", "'ios': 'arm64-apple-ios16.0'"),
        ("('mesa-tu-' + TARGET)", "('mesa-tu-ios16' if TARGET == 'ios' else 'mesa-tu-' + TARGET)"),
        ("'MinimumOSVersion': '27.0' if IOS27 else '17.0'",
         "'MinimumOSVersion': '27.0' if IOS27 else '16.0'"),
        ("frontend = (ROOT / 'drivers/AdrenoMetal/NativeBridgeMetalFrontend.mm').read_text()",
         "frontend_source = ROOT / 'drivers/AdrenoMetal/NativeBridgeMetalFrontend.mm'\n"
         "frontend = frontend_source.read_text()\n"
         "if TARGET == 'ios':\n"
         "    default_language = '(NSUInteger)MTLLanguageVersion3_1;'\n"
         "    if frontend.count(default_language) != 1:\n"
         "        raise SystemExit(f'{frontend_source}: expected one Metal 3.1 default')\n"
         "    frontend = frontend.replace(default_language, '(NSUInteger)MTLLanguageVersion3_0;')"),
    ],
    "scripts/build-adreno-metal-bridge.py": [
        ("'host': 'aarch64-apple-darwin'",
         "'host': ('x86_64-apple-darwin' if os.uname().machine == 'x86_64' else 'aarch64-apple-darwin')"),
        ("IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '17.0'",
         "IPHONEOS_DEPLOYMENT_TARGET='27.0' if args.target == 'ios27' else '16.0'"),
        ("target_dir = ROOT / 'build/m2v-target'\n",
         "target_dir = ROOT / ('build/m2v-target-ios16' if args.target == 'ios' else 'build/m2v-target')\n"),
    ],
}


def patched(text, substitutions, filename):
    for old, new in substitutions:
        if old in text:
            if text.count(old) != 1:
                raise ValueError(f"{filename}: ambiguous iOS 16 adaptation anchor: {old}")
            text = text.replace(old, new)
        elif text.count(new) != 1:
            raise ValueError(f"{filename}: pinned upstream source changed; missing {old!r}")
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


def audit_plugin(folder):
    bundle = folder / "NativeAdrenoMetal.bundle"
    paths = (bundle / "NativeAdrenoMetal", bundle / "libvulkan_freedreno.dylib", folder / "adrenoprobed")
    report = [macho_min_version(path) for path in paths]
    info = bundle / "Info.plist"
    minimum = plistlib.loads(info.read_bytes()).get("MinimumOSVersion")
    if minimum != "16.0":
        raise ValueError(f"{info}: expected MinimumOSVersion 16.0, got {minimum!r}")
    return report


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
        bundle = base / "NativeAdrenoMetal.bundle"
        bundle.mkdir()
        header = struct.pack("<IiiIIIII", 0xfeedfacf, 0x100000c, 0, 8, 1, 24, 0, 0)
        for path in (bundle / "NativeAdrenoMetal", bundle / "libvulkan_freedreno.dylib", base / "adrenoprobed"):
            path.write_bytes(header + struct.pack("<6I", 0x32, 24, 2, 16 << 16, 17 << 16, 0))
        info = bundle / "Info.plist"
        info.write_bytes(plistlib.dumps({"MinimumOSVersion": "16.0"}))
        assert len(audit_plugin(base)) == 3
        for path, content, message in (
            (bundle / "NativeAdrenoMetal", header + struct.pack("<6I", 0x32, 24, 2, 17 << 16, 17 << 16, 0),
             "minimum iOS version 17.0.0"),
            (info, plistlib.dumps({"MinimumOSVersion": "17.0"}), "expected MinimumOSVersion 16.0"),
        ):
            original = path.read_bytes()
            path.write_bytes(content)
            try:
                audit_plugin(base)
            except ValueError as error:
                assert message in str(error)
            else:
                raise AssertionError("iOS 17 deployment metadata was not rejected")
            finally:
                path.write_bytes(original)
    print("PASS: iOS16 Metal adaptations are strict and idempotent")
    print("PASS: the plugin audit rejects iOS 17 Mach-O and Info.plist metadata")


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
        print(json.dumps(audit_plugin(args.tree), indent=2))


if __name__ == "__main__":
    main()
