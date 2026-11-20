# DroidVM iOS 16 kernel target

This target builds a 4 KiB ARM64 XNU for the iPhone X (iPhone10,3 / D22AP,
iOS 16.7.16, build 20H392). It selects Apple XNU 8796.141.3 and the crosvm/Gunyah
platform components. The ordered Darwin 22 build adaptations are pinned to
[UniXNU-nuwa](https://github.com/wh1stl3/UniXNU-nuwa/tree/a840aaebb96dd3624c4d06f35f23960664fded64/patches/xnu).
The iOS 16 target has its own configuration and output tree.

Run **UniXNU DroidVM iOS 16** in GitHub Actions. The `kernel` stage compiles XNU
and prelinks the selected iPhone X donor extensions. The `native` stage builds
the source executable and records that prelink was skipped. Both validate the
Mach-O header and print the SHA-256. The workflow exports only the encrypted
kernel and text diagnostics.

On a macOS machine with the CI toolchain and the pinned UniXNU checkout, run
`bash ios16/build-kernel.sh kernel /path/to/UniXNU`. Outputs are under
`out-ios16/droidvm-ios16/ios-16.7.16-20H392/droidvm_ios16/kernel/` in that
checkout. Rerunning locally reuses the per-target source and object directories.

Run **UniXNU DroidVM iOS 16 boot stages** for the next build steps:

| Stage | Verified output |
| --- | --- |
| `loader` | ARM64 `UniXNU.efi` and legacy-SMP `DarwinBoot.efi`, with hashes. |
| `probe` | An encrypted FAT boot disk with those loaders, the prelinked kernel and the original iPhone X identity. The report checks files extracted from the finished disk. |
| `volumes` | Separately encrypted OS and SystemOS disk images with Apple input pins, decoded hashes and APFS volume UUIDs. |

The probe builds or restores its checked prelinked kernel before packaging.
It exercises UEFI to XNU handoff. Mounting the root volumes and reaching the
iPhone UI require Darwin 22 storage, root policy and display integration.
