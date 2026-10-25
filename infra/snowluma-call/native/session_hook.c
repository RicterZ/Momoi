#define NAPI_VERSION 8
#define NODE_GYP_MODULE_NAME momoi_session_hook

#include "cxx_string.h"

#include <node_api.h>

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/uio.h>
#include <unistd.h>

/* QQ 3.2.32-52194 wrapper.node. Checked against the loaded file before patching.
   internalStartNT is the native startup QQ actually calls. Its JS export is copied
   at load and is not the call we need. */
#define NT_START_OFF 0x8e08b4u
#define NT_START_TEXT_OFF 0x6663c50u
#define NT_START_TEXT "NTWrapperSession internalStartNT ===="
#define GET_AVSDK_SLOT (0x2d0u / 8u)
#define GET_ACCOUNT_SLOT (0xf0u / 8u)
#define ADD_LISTENER_SLOT (0x10u / 8u)
#define SET_ACTION_SLOT (0x20u / 8u)

typedef struct {
    uintptr_t lo;
    uintptr_t hi;
    int exec;
} MapSpan;

typedef struct {
    void *ptr;
    void *ctrl;
} SharedPtr;

static MapSpan g_maps[12];
static int g_map_count;
static uintptr_t g_base;
static void *g_trampoline;
static void *g_session;
static int g_hooked;
static int g_fires;
static int g_registered;
static SharedPtr g_service;
static struct {
    void *vt;
    uint64_t owners;
    uint64_t weak;
} g_ctrl;
static struct {
    void *vt;
    uint64_t reserved[4];
} g_listener;
static void *g_listener_vt[6];

static int safe_copy(const void *src, void *dst, size_t n) {
    if (!src || !dst || n == 0) return 0;
    struct iovec local = {.iov_base = dst, .iov_len = n};
    struct iovec remote = {.iov_base = (void *)src, .iov_len = n};
    return process_vm_readv(getpid(), &local, 1, &remote, 1, 0) == (ssize_t)n;
}

static int span_has(uintptr_t addr, int exec) {
    for (int i = 0; i < g_map_count; i++) {
        if (g_maps[i].exec != exec) continue;
        if (addr >= g_maps[i].lo && addr < g_maps[i].hi) return 1;
    }
    return 0;
}

static int in_wrapper(uintptr_t addr) {
    for (int i = 0; i < g_map_count; i++) {
        if (addr >= g_maps[i].lo && addr < g_maps[i].hi) return 1;
    }
    return 0;
}

static int load_maps(void) {
    FILE *file = fopen("/proc/self/maps", "r");
    if (!file) return 0;
    char line[512];
    g_map_count = 0;
    g_base = 0;
    while (fgets(line, sizeof line, file)) {
        unsigned long lo = 0, hi = 0, off = 0;
        char perms[8] = {0};
        if (sscanf(line, "%lx-%lx %7s %lx", &lo, &hi, perms, &off) != 4) continue;
        if (!strstr(line, "/wrapper.node")) continue;
        if (g_map_count < (int)(sizeof g_maps / sizeof g_maps[0])) {
            g_maps[g_map_count].lo = (uintptr_t)lo;
            g_maps[g_map_count].hi = (uintptr_t)hi;
            g_maps[g_map_count].exec = perms[2] == 'x';
            g_map_count++;
        }
        if (off == 0 && perms[2] == 'x') g_base = (uintptr_t)lo;
    }
    fclose(file);
    return g_base != 0;
}

static void *vslot(void *obj, unsigned slot) {
    void *vt = NULL;
    if (!safe_copy(obj, &vt, sizeof vt) || !in_wrapper((uintptr_t)vt)) return NULL;
    void *fn = NULL;
    if (!safe_copy((void *)((uintptr_t)vt + slot * sizeof(void *)), &fn, sizeof fn)) return NULL;
    if (!span_has((uintptr_t)fn, 1)) return NULL;
    return fn;
}

static void call_sret(void *obj, void *fn, SharedPtr *out) {
    asm volatile(
        "mov x0, %[obj]\n"
        "mov x8, %[dst]\n"
        "blr %[fn]\n"
        :
        : [obj] "r"(obj), [dst] "r"(out), [fn] "r"(fn)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12", "x13",
          "x14", "x15", "x16", "x17", "x30", "memory", "cc");
}

static int read_remote_string(const unsigned char raw[24], char *out, size_t cap, size_t *out_len) {
    const char *data = NULL;
    size_t len = 0;
    char storage[23];
    int kind = momoi_cxx_string_view(raw, &data, &len, storage);
    if (!kind || len + 1 > cap) return 0;
    if (kind == 1) {
        memcpy(out, data, len + 1);
        *out_len = len;
        return 1;
    }
    if (!safe_copy(data, out, len)) return 0;
    out[len] = 0;
    *out_len = len;
    return 1;
}

static int printable(const char *text, size_t len) {
    if (!len) return 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = (unsigned char)text[i];
        if (c < 0x20 || c > 0x7e) return 0;
    }
    return 1;
}

static int all_digits(const char *text, size_t len) {
    if (!len || text[0] == '0') return 0;
    for (size_t i = 0; i < len; i++) {
        if (text[i] < '0' || text[i] > '9') return 0;
    }
    return 1;
}

static int account_path(void *session, uint32_t uin, char *out, size_t cap, size_t *out_len) {
    void *fn = vslot(session, GET_ACCOUNT_SLOT);
    if (!fn) return 0;
    unsigned char raw[32];
    memset(raw, 0, sizeof raw);
    asm volatile(
        "mov x0, %[obj]\n"
        "mov w1, %w[uin]\n"
        "mov x8, %[dst]\n"
        "blr %[fn]\n"
        :
        : [obj] "r"(session), [uin] "r"(uin), [dst] "r"(raw), [fn] "r"(fn)
        : "x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10", "x11", "x12", "x13",
          "x14", "x15", "x16", "x17", "x30", "memory", "cc");
    return read_remote_string(raw, out, cap, out_len);
}

static int resolve_identity(void *session, char *uid, size_t uid_cap, char *uin, size_t uin_cap,
                            char *path, size_t path_cap) {
    typedef struct {
        uintptr_t off;
        char text[96];
        size_t len;
        int digits;
    } Candidate;
    Candidate found[24];
    int count = 0;
    for (uintptr_t off = 0x80; off <= 0x400 && count < 24; off += 8) {
        unsigned char raw[24];
        if (!safe_copy((void *)((uintptr_t)session + off), raw, sizeof raw)) continue;
        char text[96];
        size_t len = 0;
        if (!read_remote_string(raw, text, sizeof text, &len)) continue;
        if (!printable(text, len) || len > 80) continue;
        found[count].off = off;
        memcpy(found[count].text, text, len + 1);
        found[count].len = len;
        found[count].digits = all_digits(text, len) && len >= 5 && len <= 10;
        count++;
    }
    int best = -1;
    uintptr_t best_dist = UINTPTR_MAX;
    char best_path[512];
    size_t best_path_len = 0;
    const Candidate *best_uid = NULL;
    for (int i = 0; i < count; i++) {
        if (!found[i].digits) continue;
        char scratch[512];
        size_t path_len = 0;
        unsigned long value = strtoul(found[i].text, NULL, 10);
        if (value == 0 || value > 0xfffffffful) continue;
        if (!account_path(session, (uint32_t)value, scratch, sizeof scratch, &path_len)) continue;
        if (path_len < 2 || scratch[0] != '/' || path_len >= path_cap) continue;
        const Candidate *uid_item = NULL;
        uintptr_t uid_dist = UINTPTR_MAX;
        for (int j = 0; j < count; j++) {
            if (j == i || found[j].digits || found[j].len < 4) continue;
            uintptr_t dist = found[j].off > found[i].off ? found[j].off - found[i].off
                                                         : found[i].off - found[j].off;
            if (dist <= 48 && dist < uid_dist) {
                uid_dist = dist;
                uid_item = &found[j];
            }
        }
        if (!uid_item || uid_item->len + 1 > uid_cap || found[i].len + 1 > uin_cap) continue;
        uintptr_t config_dist = found[i].off > 0x160 ? found[i].off - 0x160 : 0x160 - found[i].off;
        if (config_dist < best_dist) {
            best = i;
            best_dist = config_dist;
            best_uid = uid_item;
            best_path_len = path_len;
            memcpy(best_path, scratch, path_len + 1);
        }
    }
    if (best < 0) return 0;
    memcpy(uid, best_uid->text, best_uid->len + 1);
    memcpy(uin, found[best].text, found[best].len + 1);
    memcpy(path, best_path, best_path_len + 1);
    return 1;
}

static void listener_release(void *self) { (void)self; }

static void listener_event(void *self, void *a, void *b, void *c, void *d) {
    (void)self;
    (void)a;
    (void)b;
    (void)c;
    (void)d;
}

static void capture(void *session) {
    __atomic_store_n(&g_session, session, __ATOMIC_RELEASE);
    __atomic_add_fetch(&g_fires, 1, __ATOMIC_RELAXED);
}

static void nt_detour(void *session, void *config) {
    typedef void (*start_fn)(void *, void *);
    ((start_fn)g_trampoline)(session, config);
    capture(session);
}

static int install_hook(void) {
    if (g_hooked) return 1;
    if (!load_maps()) {
        fprintf(stderr, "[MomoiQQCall] native hook missed wrapper.node\n");
        return 0;
    }
    const char *text = (const char *)(g_base + NT_START_TEXT_OFF);
    char actual[sizeof NT_START_TEXT];
    if (!safe_copy(text, actual, sizeof actual) || memcmp(actual, NT_START_TEXT, sizeof actual) != 0) {
        fprintf(stderr, "[MomoiQQCall] native hook anchor mismatch\n");
        return 0;
    }
    uint8_t *target = (uint8_t *)(g_base + NT_START_OFF);
    uint8_t prologue[16];
    if (!safe_copy(target, prologue, sizeof prologue)) return 0;
    const uint8_t expect[] = {0xfd, 0x7b, 0xbb, 0xa9, 0xfc, 0x67, 0x01, 0xa9,
                              0xf8, 0x5f, 0x02, 0xa9, 0xf6, 0x57, 0x03, 0xa9};
    if (memcmp(prologue, expect, sizeof expect) != 0) {
        fprintf(stderr, "[MomoiQQCall] native hook prologue mismatch\n");
        return 0;
    }

    void *page = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) return 0;
    uint8_t *code = page;
    memcpy(code, prologue, sizeof prologue);
    uint32_t *jump = (uint32_t *)(code + 16);
    jump[0] = 0x58000050u; /* ldr x16, #8 */
    jump[1] = 0xd61f0200u; /* br x16 */
    uint64_t resume = (uint64_t)(target + 16);
    memcpy(code + 24, &resume, sizeof resume);
    if (mprotect(page, 4096, PROT_READ | PROT_EXEC) != 0) {
        munmap(page, 4096);
        return 0;
    }
    __builtin___clear_cache((char *)page, (char *)page + 64);

    uintptr_t start = (uintptr_t)target & ~(uintptr_t)0xfff;
    if (mprotect((void *)start, 4096, PROT_READ | PROT_WRITE | PROT_EXEC) != 0) {
        munmap(page, 4096);
        return 0;
    }
    uint32_t hook[2] = {0x58000050u, 0xd61f0200u};
    uint64_t detour = (uint64_t)(uintptr_t)nt_detour;
    memcpy(target, hook, sizeof hook);
    memcpy(target + 8, &detour, sizeof detour);
    __builtin___clear_cache((char *)target, (char *)target + 16);
    if (mprotect((void *)start, 4096, PROT_READ | PROT_EXEC) != 0) {
        /* The branch is already published. Leaving the page executable is required. */
    }
    g_trampoline = page;
    g_listener_vt[0] = (void *)listener_release;
    g_listener_vt[1] = (void *)listener_release;
    for (int i = 2; i < 6; i++) g_listener_vt[i] = (void *)listener_event;
    g_listener.vt = g_listener_vt;
    g_ctrl.vt = NULL;
    g_ctrl.owners = 0x100000;
    g_ctrl.weak = 0x100000;
    g_hooked = 1;
    fprintf(stderr, "[MomoiQQCall] native NT session hook installed\n");
    return 1;
}

static int ensure_service(void *session) {
    if (g_service.ptr) return 1;
    void *fn = vslot(session, GET_AVSDK_SLOT);
    if (!fn) return 0;
    SharedPtr out = {0, 0};
    call_sret(session, fn, &out);
    if (!out.ptr) return 0;
    g_service = out;
    return 1;
}

static napi_value js_bool(napi_env env, int value) {
    napi_value out;
    napi_get_boolean(env, value ? 1 : 0, &out);
    return out;
}

static napi_value Install(napi_env env, napi_callback_info info) {
    (void)info;
    return js_bool(env, install_hook());
}

static napi_value Ready(napi_env env, napi_callback_info info) {
    (void)info;
    void *session = __atomic_load_n(&g_session, __ATOMIC_ACQUIRE);
    return js_bool(env, session != NULL);
}

static napi_value Identity(napi_env env, napi_callback_info info) {
    (void)info;
    void *session = __atomic_load_n(&g_session, __ATOMIC_ACQUIRE);
    napi_value null_value;
    napi_get_null(env, &null_value);
    if (!session) return null_value;
    char uid[96], uin[32], path[512];
    if (!resolve_identity(session, uid, sizeof uid, uin, sizeof uin, path, sizeof path)) {
        return null_value;
    }
    if (!ensure_service(session)) return null_value;
    napi_value obj, uid_v, uin_v, path_v;
    napi_create_object(env, &obj);
    napi_create_string_utf8(env, uid, NAPI_AUTO_LENGTH, &uid_v);
    napi_create_string_utf8(env, uin, NAPI_AUTO_LENGTH, &uin_v);
    napi_create_string_utf8(env, path, NAPI_AUTO_LENGTH, &path_v);
    napi_set_named_property(env, obj, "uid", uid_v);
    napi_set_named_property(env, obj, "uin", uin_v);
    napi_set_named_property(env, obj, "path", path_v);
    return obj;
}

static napi_value Metrics(napi_env env, napi_callback_info info) {
    (void)info;
    void *session = __atomic_load_n(&g_session, __ATOMIC_ACQUIRE);
    char uid[96], uin[32], path[512];
    int identified = session &&
        resolve_identity(session, uid, sizeof uid, uin, sizeof uin, path, sizeof path);
    napi_value obj, hooked, fires, ready, service, registered, uid_len, uin_len, path_len;
    napi_create_object(env, &obj);
    napi_create_int32(env, g_hooked, &hooked);
    napi_create_int32(env, g_fires, &fires);
    napi_create_int32(env, session != NULL, &ready);
    napi_create_int32(env, g_service.ptr != NULL, &service);
    napi_create_int32(env, g_registered, &registered);
    napi_create_int32(env, identified ? (int)strlen(uid) : 0, &uid_len);
    napi_create_int32(env, identified ? (int)strlen(uin) : 0, &uin_len);
    napi_create_int32(env, identified ? (int)strlen(path) : 0, &path_len);
    napi_set_named_property(env, obj, "hooked", hooked);
    napi_set_named_property(env, obj, "fires", fires);
    napi_set_named_property(env, obj, "ready", ready);
    napi_set_named_property(env, obj, "service", service);
    napi_set_named_property(env, obj, "registered", registered);
    napi_set_named_property(env, obj, "uidLen", uid_len);
    napi_set_named_property(env, obj, "uinLen", uin_len);
    napi_set_named_property(env, obj, "pathLen", path_len);
    return obj;
}

static napi_value RegisterListener(napi_env env, napi_callback_info info) {
    (void)info;
    napi_value result;
    if (g_registered) {
        napi_create_int32(env, 1, &result);
        return result;
    }
    void *session = __atomic_load_n(&g_session, __ATOMIC_ACQUIRE);
    if (!session || !ensure_service(session)) {
        napi_throw_error(env, NULL, "QQ AVSDK service is unavailable");
        return NULL;
    }
    void *fn = vslot(g_service.ptr, ADD_LISTENER_SLOT);
    if (!fn) {
        napi_throw_error(env, NULL, "QQ AVSDK listener slot is unavailable");
        return NULL;
    }
    SharedPtr incoming = {.ptr = &g_listener, .ctrl = &g_ctrl};
    typedef void (*add_fn)(void *, SharedPtr *);
    ((add_fn)fn)(g_service.ptr, &incoming);
    g_registered = 1;
    fprintf(stderr, "[MomoiQQCall] native AVSDK listener registered\n");
    napi_create_int32(env, 1, &result);
    return result;
}

static napi_value SetAction(napi_env env, napi_callback_info info) {
    size_t argc = 2;
    napi_value args[2];
    napi_get_cb_info(env, info, &argc, args, NULL, NULL);
    if (argc < 2 || !g_service.ptr) {
        napi_throw_error(env, NULL, "QQ AVSDK action is unavailable");
        return NULL;
    }
    int32_t command = 0;
    napi_get_value_int32(env, args[0], &command);
    size_t len = 0;
    napi_get_value_string_utf8(env, args[1], NULL, 0, &len);
    if (len > 1024 * 1024) {
        napi_throw_error(env, NULL, "QQ AVSDK action is too large");
        return NULL;
    }
    char *payload = malloc(len + 1);
    if (!payload) {
        napi_throw_error(env, NULL, "QQ AVSDK action could not be stored");
        return NULL;
    }
    napi_get_value_string_utf8(env, args[1], payload, len + 1, &len);
    void *fn = vslot(g_service.ptr, SET_ACTION_SLOT);
    if (!fn) {
        free(payload);
        napi_throw_error(env, NULL, "QQ AVSDK action slot is unavailable");
        return NULL;
    }
    unsigned char raw[24];
    momoi_cxx_string_init(raw, payload, len);
    typedef void (*action_fn)(void *, int32_t, void *);
    ((action_fn)fn)(g_service.ptr, command, raw);
    free(payload);
    napi_value undef;
    napi_get_undefined(env, &undef);
    return undef;
}

static void export_fn(napi_env env, napi_value exports, const char *name, napi_callback cb) {
    napi_value fn;
    napi_create_function(env, name, NAPI_AUTO_LENGTH, cb, NULL, &fn);
    napi_set_named_property(env, exports, name, fn);
}

NAPI_MODULE_INIT() {
    export_fn(env, exports, "install", Install);
    export_fn(env, exports, "ready", Ready);
    export_fn(env, exports, "identity", Identity);
    export_fn(env, exports, "metrics", Metrics);
    export_fn(env, exports, "registerListener", RegisterListener);
    export_fn(env, exports, "setAction", SetAction);
    return exports;
}
