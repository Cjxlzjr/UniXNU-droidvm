#!/usr/bin/env python3
"""Adapt the pinned Metal watchdog and its host-only IOKit stall fixture."""
import argparse
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import urlopen

HOSTSIM_SOURCE_REV = "6a3c9a8865e0feae9b3be72a3c8bb4f0595d93ba"
HOSTSIM_SOURCE_BLOB_SHA = "dc5be6dbfce7cf4e3b642f1d7460f565809f5488"
HOSTSIM_SOURCE_URL = (
    f"https://raw.githubusercontent.com/UniXNU/Adreno740/"
    f"{HOSTSIM_SOURCE_REV}/tests/hostsim/host_iokit_shim.cpp"
)
WATCHDOG_HEADER = "NativeTurnipSmoke.h"
WATCHDOG_SOURCE_REV = "935c4686828baaed47d5bf7e987e2b5f9722ab6a"
WATCHDOG_HEADER_SHA256 = "2f48988a76e9259505523b8e02be4ae8444696ab26b57032846a2d955aafe4b1"
WATCHDOG_SOURCE_URL = (
    f"https://raw.githubusercontent.com/Droid-VM/NativeMetal/{WATCHDOG_SOURCE_REV}/{WATCHDOG_HEADER}"
)


def replace_one(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ValueError(f"{label}: expected one source anchor, found {count}")
    return text.replace(old, new, 1)


def patch_shim(source: bytes) -> str:
    digest = hashlib.sha1(f"blob {len(source)}\0".encode("ascii") + source).hexdigest()
    if digest != HOSTSIM_SOURCE_BLOB_SHA:
        raise ValueError(f"Host shim must match pinned upstream source: {digest}")
    text = source.decode("utf-8")
    text = replace_one(text, "#include <cstdint>\n", "#include <chrono>\n#include <cstdint>\n#include <thread>\n",
                       "stall clock headers")
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
    std::this_thread::sleep_until(std::chrono::steady_clock::now() + std::chrono::seconds(seconds));
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


def patch_watchdog(source: bytes) -> str:
    if hashlib.sha256(source).hexdigest() != WATCHDOG_HEADER_SHA256:
        raise ValueError(f"{WATCHDOG_HEADER} differs from the pinned NativeMetal header")
    text = source.decode("utf-8")
    text = replace_one(text, "#include <pthread.h>\n", "#include <chrono>\n#include <pthread.h>\n",
                       "watchdog clock header")
    text = replace_one(text, "volatile uint32_t lastMark;", "uint32_t lastMark;", "progress storage")
    text = replace_one(text, "w->lastMark = value;", "__atomic_store_n(&w->lastMark, value, __ATOMIC_RELAXED);",
                       "progress store")
    text = replace_one(text, "unsigned(w.lastMark)", "__atomic_load_n(&w.lastMark, __ATOMIC_RELAXED)",
                       "progress sample")
    # The iOS build keeps the original ARM64 registers; the Intel simulator
    # samples the native x86_64 thread using the same watchdog deadlines.
    arm_state = """    arm_thread_state64_t state = {};
    count = ARM_THREAD_STATE64_COUNT;
    if (thread_get_state(thread, ARM_THREAD_STATE64, reinterpret_cast<thread_state_t>(&state), &count) == KERN_SUCCESS) {
        const uint64_t pc = arm_thread_state64_get_pc(state), lr = arm_thread_state64_get_lr(state);
        uint64_t fp = arm_thread_state64_get_fp(state);"""
    intel_state = """#if defined(__x86_64__)
    x86_thread_state64_t state = {};
    count = x86_THREAD_STATE64_COUNT;
    if (thread_get_state(thread, x86_THREAD_STATE64, reinterpret_cast<thread_state_t>(&state), &count) == KERN_SUCCESS) {
        const uint64_t pc = state.__rip, lr = 0;
        uint64_t fp = state.__rbp;
#else
"""
    text = replace_one(text, arm_state, intel_state + arm_state + "\n#endif", "native thread state")
    lr_frame = "        if (at < size) at += frameText(text + at, size - at, lr);"
    text = replace_one(text, lr_frame, "#if !defined(__x86_64__)\n" + lr_frame + "\n#endif",
                       "ARM64 link register frame")
    return replace_one(
        text,
        """    unsigned next = 0;
    for (unsigned tenths = 0; !__atomic_load_n(&w->done, __ATOMIC_ACQUIRE); ++tenths) {
        usleep(100000);
        if (next < 3 && tenths == checkpoints[next] * 10) {""",
        """    const auto start = std::chrono::steady_clock::now();
    unsigned next = 0;
    while (!__atomic_load_n(&w->done, __ATOMIC_ACQUIRE)) {
        usleep(100000);
        const auto elapsed = std::chrono::steady_clock::now() - start;
        if (next < 3 && elapsed >= std::chrono::seconds(checkpoints[next]) &&
            !__atomic_load_n(&w->done, __ATOMIC_ACQUIRE)) {""",
        "watchdog schedule",
    )


@contextmanager
def staged_watchdog_driver(tree: Path):
    """Use the same adjusted header in the host suite and signed iOS plugin."""
    driver = tree / "drivers/AdrenoMetal"
    original = driver.readlink()
    source = driver.resolve(strict=True)
    header = patch_watchdog((source / WATCHDOG_HEADER).read_bytes())
    with TemporaryDirectory(prefix="metal-watchdog-", dir=tree) as temp:
        work = Path(temp)
        staged = work / "AdrenoMetal"
        shutil.copytree(source, staged, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        (staged / WATCHDOG_HEADER).write_text(header)
        replacement = work / "driver-link"
        replacement.symlink_to(staged, target_is_directory=True)
        os.replace(replacement, driver)
        try:
            yield
        finally:
            replacement.symlink_to(original, target_is_directory=True)
            os.replace(replacement, driver)


def check_watchdog_timing(header: str) -> None:
    # Keep the real watchdog body. A small fixture replaces the Vulkan worker.
    watchdog = header[header.index("struct Watched {"):header.index("// Path of libvulkan_freedreno.dylib")]
    # With 50 ms of overhead per poll the original 150-tick timer would miss
    # an 18-second stall. The elapsed-time timer still samples at 15 seconds.
    watchdog = replace_one(watchdog, "        usleep(100000);", "        usleep(150000);", "fixture polling")
    if sys.platform != "darwin":
        before, rest = watchdog.split("static size_t frameText(", 1)
        _, after = rest.split("template <typename Marker, typename Reporter>", 1)
        watchdog = before + """
static void sample(Watched &watched, pthread_t, char *text, size_t size) {
    snprintf(text, size, "W %x", __atomic_load_n(&watched.lastMark, __ATOMIC_RELAXED));
}
template <typename Marker, typename Reporter>
""" + after
    harness = r'''
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#include <pthread.h>
#include <thread>
#include <unistd.h>
#ifdef __APPLE__
#include <mach/mach.h>
#endif

struct Result { int stage = 0; int vkResult = 0; };
template <class Marker> static Result run(Marker mark, const char *) {
    mark(0x6004);
    std::this_thread::sleep_for(std::chrono::seconds(18));
    return {};
}
''' + watchdog + r'''
int main() {
    unsigned samples = 0;
    char message[64] = {};
    auto mark = [](uint32_t) {};
    Result result = runWatched(mark, [&](const char *text) {
        ++samples;
        snprintf(message, sizeof message, "%s", text);
    }, nullptr);
    printf("Watchdog preflight: samples=%u, %s\n", samples, message);
    return result.vkResult || samples != 1 || strncmp(message, "W 6004", 6) ||
           strstr(message, "kern") || strstr(message, "nosusp");
}
'''
    with TemporaryDirectory(prefix="metal-watchdog-check-") as temp:
        source = Path(temp) / "watchdog.cpp"
        binary = source.with_suffix("")
        source.write_text(harness)
        compiler = ["xcrun", "clang++"] if sys.platform == "darwin" else ["c++"]
        subprocess.run([*compiler, "-std=c++17", "-pthread", source, "-o", binary], check=True, timeout=30)
        subprocess.run([binary], check=True, timeout=30)


def selftest() -> None:
    with urlopen(HOSTSIM_SOURCE_URL, timeout=40) as response:
        content = response.read()
    patched = patch_shim(content)
    if 'HOSTSIM_STALL_TRIGGERED' not in patched:
        raise AssertionError("Host-only stall injection has no diagnostic marker")
    print("PASS: pinned IOKit shim has both direct and tunnel watchdog stall hooks")
    with urlopen(WATCHDOG_SOURCE_URL, timeout=40) as response:
        watchdog = patch_watchdog(response.read())
    check_watchdog_timing(watchdog)
    print("PASS: watchdog samples a blocked worker at the monotonic deadline")


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
