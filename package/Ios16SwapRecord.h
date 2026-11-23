// SPDX-License-Identifier: BSD-2-Clause-Patent
#pragma once

#include <stddef.h>

namespace droidvm_ios16_swap {

constexpr unsigned kSelector = 5;
constexpr size_t kIos16Size = 0x46c;
constexpr size_t kDarwin23Size = 0x508;

struct Field {
    size_t source;
    size_t destination;
    size_t size;
};

// Offsets are relative to the inline record sent by kern_SwapEnd. Match the
// stores in the 20H392 and 21H461 SwapSetTimestamps, SwapBegin and
// SwapSetLayer routines.
constexpr Field kFields[] = {
    {0x000, 0x000, 7 * 8},  // Timestamp kinds 1–7, same order and clock conversion
    {0x050, 0x098, 4},      // Swap generation
    {0x054, 0x09c, 4 * 4},  // Four IOSurface IDs
    {0x064, 0x0ac, 4 * 16}, // Four source rectangles
    {0x0a4, 0x0ec, 4 * 4},  // Four layer edge modes
    {0x0b4, 0x0fc, 4 * 4},  // Four layer edge alpha values
    {0x0c4, 0x10c, 4 * 16}, // Four destination rectangles
    {0x104, 0x14c, 8},      // Layer update and removal masks
    {0x10c, 0x154, 4},      // Companion layer mask
    {0x114, 0x15c, 4 * 4},  // Four layer transforms, same enum
    {0x134, 0x17c, 4},      // Per-swap state cleared in SwapBegin
    {0x2ca, 0x336, 1},      // Layer option bit 5
    {0x2cc, 0x338, 1},      // Layer option bit 31
    {0x2e3, 0x34f, 1},      // Layer option bit 6
    {0x2e5, 0x351, 1},      // Layer option bit 7
    {0x2e6, 0x353, 1},      // Layer option bit 8
    {0x467, 0x503, 4},      // Layer option bit 9, one byte per layer
};

constexpr bool validFields() {
    size_t sourceEnd = 0;
    size_t destinationEnd = 0;
    for (const auto &field : kFields) {
        if (field.source < sourceEnd || field.destination < destinationEnd || !field.size)
            return false;
        sourceEnd = field.source + field.size;
        destinationEnd = field.destination + field.size;
    }
    return sourceEnd <= kIos16Size && destinationEnd <= kDarwin23Size;
}
static_assert(validFields(), "Swap fields must be ordered, non-overlapping and in bounds");

constexpr size_t kUnmappedMaskSize = (kIos16Size + 7) / 8;

// Diagnostics keep byte positions only. The wire payload stays with its caller.
inline void captureUnmappedMask(const unsigned char (&source)[kIos16Size],
                                unsigned char (&mask)[kUnmappedMaskSize]) {
    for (auto &byte : mask)
        byte = 0;
    size_t index = 0;
    for (size_t offset = 0; offset < kIos16Size; ++offset) {
        if (index < sizeof(kFields) / sizeof(kFields[0])
            && offset == kFields[index].source) {
            offset += kFields[index++].size - 1;
            continue;
        }
        if (source[offset])
            mask[offset / 8] |= 1u << (offset % 8);
    }
}

// Return the first unqualified nonzero byte as offset+1. SwapBegin clears
// both wire layouts. Zero is the default for new or unused Darwin 23 fields.
inline size_t translate(const unsigned char (&source)[kIos16Size],
                        unsigned char (&destination)[kDarwin23Size]) {
    size_t offset = 0;
    for (const auto &field : kFields) {
        for (; offset < field.source; ++offset) {
            if (source[offset])
                return offset + 1;
        }
        offset += field.size;
    }
    for (; offset < kIos16Size; ++offset) {
        if (source[offset])
            return offset + 1;
    }

    for (auto &byte : destination)
        byte = 0;
    for (const auto &field : kFields) {
        for (size_t byte = 0; byte < field.size; ++byte)
            destination[field.destination + byte] = source[field.source + byte];
    }
    return 0;
}

} // namespace droidvm_ios16_swap
