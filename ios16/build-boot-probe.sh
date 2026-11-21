#!/usr/bin/env bash
# Package the iPhone kernel handoff probe with the verified DroidVM EFI build recipe.
set -euo pipefail
if [[ $# != 3 || ( "$1" != loader && "$1" != probe ) ]]; then
  echo "Usage: bash ios16/build-boot-probe.sh {loader|probe} UNIXNU_DIR OUTPUT_DIR" >&2
  exit 2
fi
stage=$1
core=$(cd "$2" && pwd)
mkdir -p "$3"
release=$(cd "$3" && pwd)
device=$(cd "$(dirname "$0")/.." && pwd)
out="$core/out-ios16"
work="$out/droidvm-ios16/ios-16.7.16-20H392/droidvm_ios16"
if [[ "$stage" == loader ]]; then
  # EDK II and DarwinBoot are OS-independent. Isolate the existing package recipe's output.
  UNIXNU_OUT="$out/loader-build" python3 "$device/scripts/build-droidvm-all.py" --unixnu "$core" --only package
  efi="$out/loader-build/droidvm/package/Build/Out/AARCH64-DEBUG"
  python3 "$device/ios16/verify-efi.py" "$efi" | tee "$release/efi-format.txt"
  for binary in UniXNU.efi DarwinBoot.efi; do
    install -m 0644 "$efi/$binary" "$release/$binary"
  done
  (cd "$release" && shasum -a 256 UniXNU.efi DarwinBoot.efi >efi-sha256.txt)
  exit 0
fi

python3 "$device/ios16/verify-kernel.py" "$work/kernel"
python3 "$device/ios16/verify-efi.py" "$release"
(cd "$release" && shasum -a 256 -c efi-sha256.txt)
cd "$core"
./unixnu ipsw fetch DeviceTree StaticTrustCache -D "$device/ios16" --out "$out"
# A restored kernel can land on a fresh runner, so assemble the payload tree explicitly.
./unixnu tree -D "$device/ios16" --out "$out"
install -m 0644 "$device/scripts/make-droidvm-adt.py" "$work/tree/scripts/make-droidvm-adt.py"
mkdir -p "$work/tree/build"
install -m 0644 "$device/package/crosvm-gunyah.dtb" "$work/tree/build/crosvm-gunyah.dtb"
install -m 0644 "$out/ipsw/20H392/DeviceTree.d22ap.im4p" "$work/tree/build/iphone-x-identity.im4p"
trust="$work/tree/build/ios16-static.trustcache"
python3 "$device/ios16/pack-static-trust.py" "$core" "$out/ipsw/20H392/087-86698-044.dmg.trustcache" "$trust"
./unixnu build payload -D "$device/ios16" --out "$out" --trust-cache "$trust"
bash "$device/scripts/make-droidvm-disk.sh" "$work/payload/payload.img" "$release" "$release/ios16-boot-probe.img"

staged=$(mktemp -d "${TMPDIR:-/tmp}/ios16-probe-verified.XXXXXX")
trap 'rm -rf "$staged"' EXIT
MTOOLS_SKIP_CHECK=1 mcopy -i "$release/ios16-boot-probe.img" \
  ::/EFI/BOOT/BOOTAA64.EFI \
  ::/EFI/UniXNU/Payloads/DarwinBoot.efi \
  ::/EFI/UniXNU/Payloads/kernel.macho \
  ::/EFI/UniXNU/Payloads/kernel.macho.adt \
  ::/EFI/UniXNU/Payloads/kernel.macho.trustcache \
  ::/EFI/UniXNU/unixnu.toml "$staged"
cmp "$release/UniXNU.efi" "$staged/BOOTAA64.EFI"
cmp "$release/DarwinBoot.efi" "$staged/DarwinBoot.efi"
cmp "$work/kernel/kernel.macho" "$staged/kernel.macho"
python3 "$device/ios16/verify-probe.py" "$staged" "$release/ios16-boot-probe.img" "$work/payload/payload.json" \
  >"$release/boot-probe.json"
cat "$release/boot-probe.json"
