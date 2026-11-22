#!/usr/bin/env python3
"""Host-only watchdog stall adaptation for the pinned Adreno740 IOKit shim.

The host simulator supports direct and accelerator tunnel transports. The
upstream 17-second BoAlloc test injection covers only the tunnel transport;
patch the direct transport as well. Production binaries are untouched.
"""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

HOSTSIM_SOURCE_REV = "6a3c9a8865e0feae9b3be72a3c8bb4f0595d93ba"
HOSTSIM_SOURCE_BLOB_SHA = "dc5be6dbfce7cf4e3b642f1d7460f565809f5488"
HOSTSIM_SOURCE_URL = (
    f"https://raw.githubusercontent.com/UniXNU/Adreno740/"
    f"{HOSTSIM_SOURCE_REV}/tests/hostsim/host_iokit_shim.cpp"
)


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"Host shim {label} changed: expected one occurrence; found {count}")
    return text.replace(old, new, 1)


def patch_shim(source: bytes) -> str:
    digest = hashlib.sha1(f"blob {len(source)}\0".encode("ascii") + source).hexdigest()
    if digest != HOSTSIM_SOURCE_BLOB_SHA:
        raise ValueError(f"Host shim must match pinned upstream source: {digest}")
    text = source.decode("utf-8")
    anchor = 'static bool trace(){static int t=-1;if(t<0)t=getenv("HOSTSIM_TRACE")?1:0;return t;}'
    control = r'''
// Host-only stall, never included in the device kernel or Metal plugin.
// Use a single guard across the direct and tunneled user-client paths.
static void hostsimMaybeStall(uint32_t method, const char *transport) {
    static bool stalled = false;
    const char *requested = getenv("HOSTSIM_STALL");
    if (method != 2 || !requested || stalled) return;
    char *end = nullptr;
    const unsigned long seconds = strtoul(requested, &end, 10);
    if (!end || *end || seconds < 16 || seconds > 30) {
        fprintf(stderr, "HOSTSIM_STALL_REJECTED transport=%s value=%s\n", transport, requested);
        return;
    }
    stalled = true;
    fprintf(stderr, "HOSTSIM_STALL_TRIGGERED transport=%s method=%u seconds=%lu\n",
            transport, method, seconds);
    fflush(stderr);
    sleep((unsigned)seconds);
}
'''
    text = replace_one(text, anchor, anchor + "\n" + control, "stall helper")
    old_tunnel = (
        '        static bool stalled=false;\n'
        '        if(header[1]==2 && !stalled && getenv("HOSTSIM_STALL")) {stalled=true;sleep((unsigned)atoi(getenv("HOSTSIM_STALL")));}\n'
    )
    text = replace_one(text, old_tunnel, '        hostsimMaybeStall(header[1], "tunnel");\n', "tunnel")
    direct = '    return translate(host_gpu_call(selector,in,in_size,out,out_size?*out_size:0));'
    text = replace_one(
        text, direct,
        '    hostsimMaybeStall(selector, "direct");\n' + direct,
        "direct"
    )
    if text.count("hostsimMaybeStall(") != 3:
        raise ValueError("Expected exactly one helper and both transport hooks")
    return text


def selftest() -> None:
    with urlopen(HOSTSIM_SOURCE_URL, timeout=40) as response:
        content = response.read()
    patched = patch_shim(content)
    if 'HOSTSIM_STALL_TRIGGERED' not in patched:
        raise AssertionError("Host-only stall injection has no diagnostic marker")
    print("PASS: pinned IOKit shim has both direct and tunnel watchdog stall hooks")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.selftest:
        selftest()
    else:
        if not args.input or not args.output:
            parser.error("--input and --output required")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(patch_shim(args.input.read_bytes()))
