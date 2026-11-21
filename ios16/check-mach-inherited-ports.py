#!/usr/bin/env python3
"""Test the guest's actual Mach port adapters against the pinned XNU helpers."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

IOS = Path(__file__).resolve().parent
METHODS = ('mach_ports_register', 'mach_ports_lookup')


def extract(source, name):
    body = re.search(rf'^kern_return_t\n{re.escape(name)}\(.*?^\}}', source, re.M | re.S)
    if not body:
        raise ValueError(f'No XNU implementation of {name}')
    return body.group()


def run_checks(source):
    compiler = os.getenv('CC', 'cc')
    ipc = Path('osfmk/kern/ipc_tt.c')
    task = Path('osfmk/mach/task.defs')
    with TemporaryDirectory() as folder:
        work = Path(folder)
        for relative in (ipc, task):
            copy = work / relative
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / relative, copy)
        # Avoid git discovering the caller's checkout when run in a temporary directory.
        subprocess.run(['git', 'apply', str(IOS / 'patches/legacy-mach-inherited-ports.patch')],
                       cwd=work, check=True)
        text = (work / task).read_text()
        wire = work / 'mach-ports.defs'
        spans = [re.search(rf'(?ms)^#if LIBSYSCALL_INTERFACE[^\n]*\nroutine\s+{method}\(.*?^#endif', text)
                 for method in METHODS]
        if not all(spans):
            raise ValueError('Expected two conditional Mach RPC definitions')
        wire.write_text('\n'.join(span.group() for span in spans) + '\n')
        for flag in ('KERNEL_SERVER', 'KOBJECT_SERVER', 'LIBSYSCALL_INTERFACE'):
            parsed = subprocess.check_output([compiler, '-E', '-P', '-x', 'c', f'-D{flag}=1', wire], text=True)
            if '^array[]' not in parsed or any(f'_kernelrpc_{method}3' in parsed for method in METHODS):
                raise ValueError(f'Old Mach port wire definitions missing with {flag}')
        native = subprocess.check_output([compiler, '-E', '-P', '-x', 'c', wire], text=True)
        if any(f'_kernelrpc_{method}3' not in native for method in METHODS):
            raise ValueError('The modern internal user client prototypes were lost')
        c = (work / ipc).read_text()
        helpers = [extract(c, f'_kernelrpc_{method}3') for method in METHODS]
        adapters = [extract(c, method) for method in METHODS]
        fixture = (IOS / 'tests/mach-inherited-ports.c').read_text()
        unit = work / 'mach-ports.c'
        unit.write_text(fixture.replace('/* XNU_IMPLEMENTATIONS */', '\n\n'.join(helpers + adapters)))
        exe = work / 'mach-ports-test.exe'
        subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra', '-Werror', unit, '-o', exe], check=True)
        subprocess.run([exe], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('xnu', type=Path)
    run_checks(parser.parse_args().xnu.resolve())
