// SPDX-License-Identifier: BSD-2-Clause-Patent
#include "../package/Ios16SwapRecord.h"
#include <cassert>
#include <cstring>
#include <initializer_list>

int main() {
    using droidvm_ios16_swap::translate;
    unsigned char legacy[0x46c] = {};
    unsigned char expected[0x508] = {};
    unsigned char native[0x508];

    const auto copyField = [&](size_t source, size_t destination, size_t length) {
        for (size_t i = 0; i < length; ++i) {
            const auto value = static_cast<unsigned char>(1 + (source + i) % 251);
            legacy[source + i] = expected[destination + i] = value;
        }
    };
    for (size_t kind = 0; kind < 7; ++kind)
        copyField(kind * 8, kind * 8, 8);
    copyField(0x50, 0x98, 0x74);
    copyField(0xc4, 0x10c, 0x4c);
    copyField(0x114, 0x15c, 16);
    copyField(0x134, 0x17c, 4);
    // A 1024 x 1366 update at (0, 0) reproduces the four nonzero
    // offsets in the #69 rejection masks, including the validity byte.
    constexpr unsigned char updateRect[] = {
        0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 0, 0, 0x56, 5, 0, 0,
    };
    std::memcpy(legacy + 0x250, updateRect, sizeof(updateRect));
    std::memcpy(expected + 0x298, updateRect, sizeof(updateRect));
    legacy[0x260] = expected[0x2cc] = 1;
    copyField(0x2ca, 0x336, 1);
    copyField(0x2cc, 0x338, 1);
    copyField(0x2e3, 0x34f, 1);
    copyField(0x2e5, 0x351, 1);
    copyField(0x2e6, 0x353, 1);
    copyField(0x467, 0x503, 4);

    std::memset(native, 0xff, sizeof(native));
    assert(translate(legacy, native) == 0);
    assert(std::memcmp(native, expected, sizeof(native)) == 0);

    // An unexpected value beside the timestamps, between fields, or at the
    // end must be reported. The caller must never dispatch a partial record.
    for (const auto index : {0x038, 0x04f, 0x110, 0x24f, 0x261, 0x466, 0x46b}) {
        legacy[index] = 1;
        assert(translate(legacy, native) == static_cast<size_t>(index + 1));
        assert(std::memcmp(native, expected, sizeof(native)) == 0);
        legacy[index] = 0;
    }

    using namespace droidvm_ios16_swap;
    unsigned char mask[kUnmappedMaskSize];
    captureUnmappedMask(legacy, mask);
    for (const auto byte : mask)
        assert(byte == 0);
    legacy[0x38] = legacy[0x110] = legacy[0x46b] = 1;
    captureUnmappedMask(legacy, mask);
    for (size_t offset = 0; offset < sizeof(mask) * 8; ++offset) {
        const bool rejected = offset == 0x38 || offset == 0x110 || offset == 0x46b;
        assert(bool(mask[offset / 8] & (1u << (offset % 8))) == rejected);
    }
}
