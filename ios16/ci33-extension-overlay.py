#!/usr/bin/env python3
"""CI33: strictly patch the generated XNU GPU-addon overlay for iOS16 diagnostics.

Runs *after* ios16/metal-experiment.py patch. Changes only the generated core
patch, not the pinned upstream repo. Recalculates unified-diff hunk positions.
"""
from __future__ import annotations
import argparse
import re
from pathlib import Path
from tempfile import TemporaryDirectory

TARGET = Path("patches/xnu/zzy-native-gpu-addon-mount.patch")
INSERT_BEFORE = "+static void\n+native_gpu_addon_prepare(proc_t p, user_addr_t scratch)\n"
OLD_CALL = (
    "+\tnative_gpu_addon_mount(p, scratch, 0x400, extensions_source, sizeof(extensions_source), extensions_point,\n"
    "+\t    sizeof(extensions_point), \"/System/Library/Extensions/AGXMetalA11.bundle\",\n"
    "+\t    \"/System/Library/Extensions/NativeAdrenoMetal.bundle/Info.plist\", &native_gpu_addon_stage,\n"
    "+\t    &native_gpu_addon_status);\n"
)
PATH_A11 = "/System/Library/Extensions/AGXMetalA11.bundle"
PATH_COUNTER = "/System/Library/Extensions/AGXGPURawCounterBundle.bundle"
PATH_GL = "/System/Library/Extensions/AppleMetalGLRenderer.bundle"
PATH_OWNER = "/System/Library/Extensions/NativeAdrenoMetal.bundle/Info.plist"
PATH_OWNER_SOURCE = "/private/var/ngpu/addons/NativeAdrenoMetal.bundle/Info.plist"

GLOBS = r"""
/* CI33 iOS16: distinguish source availability from union-mount name lookup.
 * These scalars can be inspected from the verified running Darwin kernel.
 * selected_lower: 1=A11, 2=AGX counter, 3=AppleMetalGLRenderer, 0=none.
 * Do NOT report 5/0 unless a preexisting lower entry and owner file resolve
 * through the actual post-mount namespace. Never mask ENOENT. */
volatile int native_gpu_addon_ext_pre_dir = -1;
volatile int native_gpu_addon_ext_pre_a11 = -1;
volatile int native_gpu_addon_ext_pre_counter = -1;
volatile int native_gpu_addon_ext_pre_gl = -1;
volatile int native_gpu_addon_ext_pre_owner_source = -1;
volatile int native_gpu_addon_ext_post_dir = -1;
volatile int native_gpu_addon_ext_post_a11 = -1;
volatile int native_gpu_addon_ext_post_counter = -1;
volatile int native_gpu_addon_ext_post_gl = -1;
volatile int native_gpu_addon_ext_post_owner = -1;
volatile int native_gpu_addon_ext_selected_lower = 0;
static int
native_gpu_addon_probe_path(const char *path)
{
    vnode_t vnode = NULLVP;
    int error = vnode_lookup(path, 0, &vnode, vfs_context_current());
    if (!error) {
        vnode_put(vnode);
    }
    return error;
}
"""

NEW_CALL = f"""
/* CI33: probe in the *running* guest before mounting. A verified OS image
 * might not reflect the live namespace if the root volume overlay changed. */
native_gpu_addon_ext_pre_dir = native_gpu_addon_probe_path("/System/Library/Extensions");
native_gpu_addon_ext_pre_a11 = native_gpu_addon_probe_path("{PATH_A11}");
native_gpu_addon_ext_pre_counter = native_gpu_addon_probe_path("{PATH_COUNTER}");
native_gpu_addon_ext_pre_gl = native_gpu_addon_probe_path("{PATH_GL}");
native_gpu_addon_ext_pre_owner_source = native_gpu_addon_probe_path("{PATH_OWNER_SOURCE}");
const char *lower = "{PATH_A11}";
if (!native_gpu_addon_ext_pre_a11) {{
    native_gpu_addon_ext_selected_lower = 1;
}} else if (!native_gpu_addon_ext_pre_counter) {{
    native_gpu_addon_ext_selected_lower = 2;
    lower = "{PATH_COUNTER}";
}} else if (!native_gpu_addon_ext_pre_gl) {{
    native_gpu_addon_ext_selected_lower = 3;
    lower = "{PATH_GL}";
}}
native_gpu_addon_mount(p, scratch, 0x400, extensions_source, sizeof(extensions_source), extensions_point,
    sizeof(extensions_point), lower,
    "{PATH_OWNER}", &native_gpu_addon_stage,
    &native_gpu_addon_status);
native_gpu_addon_ext_post_dir = native_gpu_addon_probe_path("/System/Library/Extensions");
native_gpu_addon_ext_post_a11 = native_gpu_addon_probe_path("{PATH_A11}");
native_gpu_addon_ext_post_counter = native_gpu_addon_probe_path("{PATH_COUNTER}");
native_gpu_addon_ext_post_gl = native_gpu_addon_probe_path("{PATH_GL}");
native_gpu_addon_ext_post_owner = native_gpu_addon_probe_path("{PATH_OWNER}");
"""

HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,(?P<old_count>\d+))? \+(?P<new>\d+)(?:,(?P<new_count>\d+))? @@(?P<tail>.*)$")


def add_diff_lines(text: str) -> str:
    return ''.join('+' + line + '\n' for line in text.strip('\n').splitlines())


def recount_hunks(text: str) -> str:
    """Correct old/new counts and shifted new starts for each unified patch file."""
    lines = text.splitlines(keepends=True)
    shifts: dict[str, int] = {}
    current = ''
    output = []
    i = 0
    count = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('diff --git '):
            current = line.strip()
            shifts[current] = 0
            output.append(line)
            i += 1
            continue
        match = HUNK.match(line.rstrip('\n'))
        if not match:
            output.append(line)
            i += 1
            continue
        j = i + 1
        old_count = new_count = 0
        while j < len(lines) and not lines[j].startswith(('@@ ', 'diff --git ')):
            h = lines[j]
            # A raw blank line separates files/hunks; empty diff content is prefixed by space or +.
            if not h.strip():
                if h.startswith((' ', '+', '-')):
                    pass
                else:
                    break
            if h.startswith((' ', '-')) and not h.startswith(('--- ',)):
                old_count += 1
            if h.startswith((' ', '+')) and not h.startswith(('+++ ',)):
                new_count += 1
            if not h.startswith((' ', '+', '-', '\\')):
                raise ValueError(f"Unexpected patch hunk line: {h[:80]!r}")
            j += 1
        if not current:
            raise ValueError('Hunk outside a diff')
        old_start = int(match['old'])
        new_start = int(match['new']) + shifts[current]
        orig_old = int(match['old_count'] or '1')
        orig_new = int(match['new_count'] or '1')
        if old_count != orig_old:
            raise ValueError(f"Unexpected old hunk length at {old_start}: {old_count} != {orig_old}")
        shifts[current] += new_count - orig_new
        output.append(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{match['tail']}\n")
        output.extend(lines[i + 1:j])
        i = j
        count += 1
    if not count:
        raise ValueError('Missing unified patch hunks')
    return ''.join(output)


def apply(text: str) -> str:
    sentinel = '+volatile int native_gpu_addon_ext_pre_a11 = -1;'
    if sentinel in text:
        if text.count(sentinel) != 1:
            raise ValueError('Duplicate CI33 patch marker')
        return text
    if text.count(INSERT_BEFORE) != 1 or text.count(OLD_CALL) != 1:
        raise ValueError('Pinned XNU patch layout changed: refusing CI33 adaptation')
    updated = text.replace(INSERT_BEFORE, add_diff_lines(GLOBS) + INSERT_BEFORE, 1)
    updated = updated.replace(OLD_CALL, add_diff_lines(NEW_CALL), 1)
    updated = recount_hunks(updated)
    for value in ('native_gpu_addon_ext_post_a11', 'native_gpu_addon_ext_selected_lower', PATH_OWNER):
        if value not in updated:
            raise ValueError(f'CI33 patch missing {value}')
    return updated


def selftest() -> None:
    # A real hunks fixture is assembled with no unknown lines.
    orig = (
        'diff --git a/demo b/demo\n'
        '--- a/demo\n'
        '+++ b/demo\n'
        '@@ -10,2 +10,3 @@\n'
        ' context\n'
        '+one\n'
        ' context\n'
        '@@ -20,1 +21,1 @@\n'
        ' old\n'
    )
    assert recount_hunks(orig) == orig
    changed = orig.replace('+one\n', '+one\n+two\n')
    fixed = recount_hunks(changed)
    assert '@@ -10,2 +10,4 @@' in fixed
    assert '@@ -20,1 +22,1 @@' in fixed
    assert PATH_A11 in NEW_CALL and PATH_COUNTER in NEW_CALL
    assert 'native_gpu_addon_ext_post_owner' in GLOBS
    assert 'vnode_put(vnode)' in GLOBS
    with TemporaryDirectory() as td:
        p = Path(td) / 'fixture.patch'
        p.write_text(fixed)
        assert p.read_text() == fixed
    print('PASS: unified-diff hunk recount, runtime probes and safe fallback anchors')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('patch', 'selftest'))
    parser.add_argument('tree', nargs='?', type=Path)
    args = parser.parse_args()
    if args.command == 'selftest':
        selftest()
        return
    if not args.tree:
        parser.error('patch requires generated core tree')
    path = args.tree / TARGET
    original = path.read_text()
    changed = apply(original)
    if changed != original:
        path.write_text(changed)
    print(f'CI33 Extensions diagnostics: {path}: {"updated" if changed != original else "already patched"}')


if __name__ == '__main__':
    main()
