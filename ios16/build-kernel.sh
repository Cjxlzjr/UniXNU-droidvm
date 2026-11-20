#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 2 || ( "$1" != native && "$1" != kernel ) ]]; then
  echo "Usage: bash ios16/build-kernel.sh {native|kernel} /path/to/UniXNU" >&2
  exit 2
fi

stage=$1
device=$(cd "$(dirname "$0")" && pwd)
core=$(cd "$2" && pwd)
profile="$core/profiles/os/ios-16.7.16-20H392/profile.toml"
revision=dba62c1ca53cd04c25b0e501afff31d5ffe2e20e

if [[ $(git -C "$core" rev-parse HEAD) != "$revision" ]]; then
  echo "The iOS 16 target requires UniXNU $revision" >&2
  exit 1
fi
if [[ -e "$profile" ]]; then
  cmp "$device/profile.toml" "$profile"
else
  install -d "$(dirname "$profile")"
  install -m 0644 "$device/profile.toml" "$profile"
fi

cd "$core"
out="$core/out-ios16"
./unixnu defconfig droidvm_ios16_defconfig -D "$device" --out "$out"
if [[ "$stage" == native ]]; then
  ./unixnu build kernel -D "$device" --out "$out" --skip-prelink
else
  ./unixnu ipsw fetch KernelCache -D "$device" --out "$out"
  ./unixnu build kernel -D "$device" --out "$out"
fi
