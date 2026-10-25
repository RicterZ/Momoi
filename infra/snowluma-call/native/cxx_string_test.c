#include "cxx_string.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(void) {
    unsigned char raw[24];
    const char *data = NULL;
    size_t len = 0;
    char storage[23];

    momoi_cxx_string_init(raw, "123456", 6);
    assert(momoi_cxx_string_view(raw, &data, &len, storage) == 1);
    assert(len == 6);
    assert(strcmp(data, "123456") == 0);

    momoi_cxx_string_init(raw, "", 0);
    assert(momoi_cxx_string_view(raw, &data, &len, storage) == 1);
    assert(len == 0);

    char long_text[40];
    memset(long_text, 'a', sizeof long_text);
    momoi_cxx_string_init(raw, long_text, sizeof long_text);
    assert(momoi_cxx_string_view(raw, &data, &len, storage) == 2);
    assert(len == sizeof long_text);
    assert(data == long_text);
    assert((raw[0] & 1u) == 1);

    raw[0] = (22u << 1) + 1u; /* long flag with an impossible short size mixed in */
    memset(raw + 8, 0, 16);
    assert(momoi_cxx_string_view(raw, &data, &len, storage) == 0);

    char span_buf[] = {'a', 'b', 'c'};
    momoi_span_string_init(raw, span_buf, 3);
    char *span_data = NULL, *span_end = NULL, *span_cap = NULL;
    memcpy(&span_data, raw, sizeof span_data);
    memcpy(&span_end, raw + 8, sizeof span_end);
    memcpy(&span_cap, raw + 16, sizeof span_cap);
    assert(span_data == span_buf);
    assert(span_end == span_buf + 3);
    assert(span_cap == span_buf + 3);
    momoi_span_string_init(raw, span_buf, 0);
    assert(raw[0] == 0 && memcmp(raw, raw + 1, 23) == 0);

    puts("cxx string ok");
    return 0;
}
