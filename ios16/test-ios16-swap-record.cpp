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
    copyField(0x50, 0x98, 0x54);
    copyField(0xc4, 0x10c, 0x4c);
    copyField(0x114, 0x15c, 16);
    copyField(0x134, 0x17c, 4);
    copyField(0x2ca, 0x336, 1);
    copyField(0x2cc, 0x338, 1);
    copyField(0x2e3, 0x34f, 1);
    copyField(0x2e5, 0x351, 1);
    copyField(0x2e6, 0x353, 1);
    copyField(0x467, 0x503, 4);

    std::memset(native, 0xff, sizeof(native));
    assert(translate(legacy, native) == 0);
    assert(std::memcmp(native, expected, sizeof(native)) == 0);

    // An unexpected value in the leading area, between fields, or at the
    // end must be reported. The caller must never dispatch a partial record.
    for (const auto index : {0x000, 0x0a4, 0x110, 0x466, 0x46b}) {
        legacy[index] = 1;
        assert(translate(legacy, native) == static_cast<size_t>(index + 1));
        assert(std::memcmp(native, expected, sizeof(native)) == 0);
        legacy[index] = 0;
    }
}
