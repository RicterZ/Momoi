#ifndef MOMOI_CXX_STRING_H
#define MOMOI_CXX_STRING_H

#include <stddef.h>
#include <stdint.h>
#include <string.h>

/* libc++ std::string as used by QQ 3.2.32 wrapper.node, little-endian.
   Short strings store (size << 1) in the first byte and bytes at offset 1.
   Long strings set the low bit, with size at offset 8 and the pointer at 16. */
static int momoi_cxx_string_view(const unsigned char raw[24], const char **data, size_t *len,
                                 char short_storage[23]) {
    if ((raw[0] & 1u) == 0) {
        size_t n = (size_t)raw[0] >> 1;
        if (n > 22) return 0;
        memcpy(short_storage, raw + 1, n);
        short_storage[n] = 0;
        *data = short_storage;
        *len = n;
        return 1;
    }
    uint64_t n = 0, ptr = 0;
    memcpy(&n, raw + 8, sizeof n);
    memcpy(&ptr, raw + 16, sizeof ptr);
    if (n == 0 || n > 4096 || ptr < 0x1000) return 0;
    *data = (const char *)(uintptr_t)ptr;
    *len = (size_t)n;
    return 2;
}

static void momoi_cxx_string_init(unsigned char raw[24], const char *bytes, size_t n) {
    memset(raw, 0, 24);
    if (n <= 22) {
        raw[0] = (unsigned char)(n << 1);
        if (n) memcpy(raw + 1, bytes, n);
        return;
    }
    uint64_t cap = (uint64_t)n << 1 | 1u;
    uint64_t size = (uint64_t)n;
    uint64_t ptr = (uint64_t)(uintptr_t)bytes;
    memcpy(raw, &cap, sizeof cap);
    memcpy(raw + 8, &size, sizeof size);
    memcpy(raw + 16, &ptr, sizeof ptr);
}

#endif
