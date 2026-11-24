"""Keep strict Clang warnings across the supported Xcode versions."""
import subprocess


def optional_virtual_specifier_warning(compiler: str) -> list[str]:
    # Older Clang may accept an unknown negative flag without diagnosing it.
    probe = subprocess.run(
        [compiler, '-Werror=unknown-warning-option', '-Wunnecessary-virtual-specifier',
         '-x', 'c++', '-fsyntax-only', '-'],
        input=b'', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return ['-Wno-unnecessary-virtual-specifier'] if probe.returncode == 0 else []
