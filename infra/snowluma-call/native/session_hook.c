#define NAPI_VERSION 8
#define NODE_GYP_MODULE_NAME momoi_session_hook

#include "cxx_string.h"

#include <node_api.h>

#include <errno.h>
#include <stdbool.h>
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
#define ADD_LISTENER_SLOT (0x10u / 8u)
#define SET_ACTION_SLOT (0x20u / 8u)
/* NTWrapperSession fields after internalStartNT, QQ 3.2.32-52194.
   Slot 0xf0 is an account-path type switch (1/2/3), not getAccountPath(uin). */
#define NT_UIN_OFF 0x160u
#define NT_UID_OFF 0x180u
#define NT_DATA_OFF 0x1b8u

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
static void *g_listener_vt[16];
static napi_threadsafe_function g_tsfn;
static napi_ref g_listener_ref;
static void *g_action_tramp;
static void *g_invite_tramp;
static void *g_s2c_tramp;
static int g_callbacks;

/* Djinni entry points in NodeIKernelAVSDKListener.cpp, QQ 3.2.32-52194.
   These already unpack the kernel call into the values the JS bridge expects. */
#define AV_ACTION_OFF 0x1a02304u
#define AV_ACTION_IMM 0xd8du
#define AV_INVITE_OFF 0x1a02830u
#define AV_INVITE_IMM 0xd9du
#define AV_S2C_OFF 0x1a02e44u
#define AV_S2C_IMM 0xdd4u

enum { EV_ACTION = 1, EV_INVITE = 2, EV_S2C = 3 };

typedef struct {
    int kind;
    int32_t type;
    size_t len;
    char *payload;
} AvEvent;

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

static int field_string(void *session, uintptr_t off, char *out, size_t cap, size_t *out_len) {
    unsigned char raw[24];
    if (!safe_copy((void *)((uintptr_t)session + off), raw, sizeof raw)) return 0;
    if (!read_remote_string(raw, out, cap, out_len)) return 0;
    return printable(out, *out_len);
}

static int resolve_identity(void *session, char *uid, size_t uid_cap, char *uin, size_t uin_cap,
                            char *path, size_t path_cap) {
    uint32_t uin_value = 0;
    if (!safe_copy((void *)((uintptr_t)session + NT_UIN_OFF), &uin_value, sizeof uin_value)) return 0;
    if (uin_value < 10000u) return 0;
    char uid_buf[96];
    size_t uid_len = 0;
    if (!field_string(session, NT_UID_OFF, uid_buf, sizeof uid_buf, &uid_len)) return 0;
    if (uid_len < 4 || uid_len + 1 > uid_cap || uid_buf[0] != 'u' || uid_buf[1] != '_') return 0;
    char data_path[512];
    size_t data_len = 0;
    const char *suffix = "/nt_data";
    size_t suffix_len = strlen(suffix);
    if (!field_string(session, NT_DATA_OFF, data_path, sizeof data_path, &data_len)) return 0;
    if (data_len <= suffix_len || memcmp(data_path + data_len - suffix_len, suffix, suffix_len) != 0) {
        return 0;
    }
    data_len -= suffix_len;
    data_path[data_len] = 0;
    if (data_path[0] != '/' || data_len + 1 > path_cap) return 0;
    int written = snprintf(uin, uin_cap, "%u", uin_value);
    if (written <= 0 || (size_t)written >= uin_cap) return 0;
    memcpy(uid, uid_buf, uid_len + 1);
    memcpy(path, data_path, data_len + 1);
    return 1;
}

static void listener_release(void *self) { (void)self; }

static uintptr_t listener_event(void *self, void *a, void *b, void *c, void *d) {
    (void)self;
    (void)a;
    (void)b;
    (void)c;
    (void)d;
    return 0;
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

static char *copy_cxx_string(const unsigned char raw[24], size_t *out_len) {
    if ((raw[0] & 1u) == 0) {
        size_t n = (size_t)raw[0] >> 1;
        if (n > 22) return NULL;
        char *text = malloc(n + 1);
        if (!text) return NULL;
        if (n) memcpy(text, raw + 1, n);
        text[n] = 0;
        *out_len = n;
        return text;
    }
    uint64_t n = 0, ptr = 0;
    memcpy(&n, raw + 8, sizeof n);
    memcpy(&ptr, raw + 16, sizeof ptr);
    if (n > 1024u * 1024u || ptr < 0x1000) return NULL;
    char *text = malloc((size_t)n + 1);
    if (!text) return NULL;
    if (n && !safe_copy((void *)(uintptr_t)ptr, text, (size_t)n)) {
        free(text);
        return NULL;
    }
    text[n] = 0;
    *out_len = (size_t)n;
    return text;
}

static AvEvent *take_event(void *frame, int kind, uintptr_t type_off, uintptr_t str_off) {
    if ((uintptr_t)frame < 0x1000) return NULL;
    int32_t type = 0;
    if (!safe_copy((void *)((uintptr_t)frame + type_off), &type, sizeof type)) return NULL;
    char *payload = NULL;
    size_t len = 0;
    if (str_off) {
        unsigned char raw[24];
        if (!safe_copy((void *)((uintptr_t)frame + str_off), raw, sizeof raw)) return NULL;
        payload = copy_cxx_string(raw, &len);
        if (!payload) return NULL;
    }
    AvEvent *event = calloc(1, sizeof *event);
    if (!event) {
        free(payload);
        return NULL;
    }
    event->kind = kind;
    event->type = type;
    event->len = len;
    event->payload = payload;
    return event;
}

static void free_event(AvEvent *event) {
    if (!event) return;
    free(event->payload);
    free(event);
}

static void js_invoke(napi_env env, napi_value js_callback, void *context, void *data) {
    AvEvent *event = data;
    (void)js_callback;
    (void)context;
    if (!event) return;
    napi_value listener = NULL;
    if (g_listener_ref) napi_get_reference_value(env, g_listener_ref, &listener);
    napi_valuetype listener_type = napi_undefined;
    if (listener) napi_typeof(env, listener, &listener_type);
    if (listener_type == napi_object) {
        const char *name = event->kind == EV_INVITE ? "OnInviteActionToAVSDK"
                           : event->kind == EV_S2C  ? "onS2CActionToAVSDK"
                                                    : "onActionToAVSDK";
        napi_value method, global, result, args[3];
        int argc = 0;
        napi_get_named_property(env, listener, name, &method);
        napi_valuetype method_type = napi_undefined;
        napi_typeof(env, method, &method_type);
        if (method_type == napi_function) {
            if (event->kind == EV_S2C) {
                napi_value reason;
                napi_create_object(env, &args[0]);
                napi_create_int32(env, event->type, &reason);
                napi_set_named_property(env, args[0], "destroyReason", reason);
                argc = 1;
            } else if (event->kind == EV_INVITE) {
                napi_create_object(env, &args[0]);
                napi_create_int32(env, event->type, &args[1]);
                napi_create_string_utf8(env, event->payload ? event->payload : "", event->len, &args[2]);
                argc = 3;
            } else {
                napi_create_int32(env, event->type, &args[0]);
                napi_create_string_utf8(env, event->payload ? event->payload : "", event->len, &args[1]);
                argc = 2;
            }
            napi_get_global(env, &global);
            napi_call_function(env, listener, method, (size_t)argc, args, &result);
            bool pending = false;
            napi_is_exception_pending(env, &pending);
            if (pending) {
            napi_value exception;
            napi_get_and_clear_last_exception(env, &exception);
            (void)exception;
            }
        }
    }
    free_event(event);
}

static void post_event(AvEvent *event) {
    size_t len = event ? event->len : 0;
    int kind = event ? event->kind : 0;
    if (!event) return;
    if (!g_tsfn || napi_call_threadsafe_function(g_tsfn, event, napi_tsfn_nonblocking) != napi_ok) {
        free_event(event);
        return;
    }
    __atomic_add_fetch(&g_callbacks, 1, __ATOMIC_RELAXED);
    fprintf(stderr, "[MomoiQQCall] native AVSDK callback %d bytes=%zu\n", kind, len);
}

/* Copy the kernel arguments, then branch to the stolen prologue. A C call would
   sit on the stack without the original function's unwind tables, and a throw
   inside QQ becomes std::terminate. x30 stays the caller's return address. */
#define CAPTURE(name, kind, type_off, str_off)                                          \
    __attribute__((used, noinline)) static void capture_##name(void *frame) {          \
        post_event(take_event(frame, (kind), (type_off), (str_off)));                   \
    }                                                                                   \
    __attribute__((naked, used)) static void name##_detour(void) {                     \
        __asm__ volatile("stp x0, x1, [sp, #-80]!\n\t"                                  \
                         "stp x2, x3, [sp, #16]\n\t"                                    \
                         "stp x4, x5, [sp, #32]\n\t"                                    \
                         "stp x6, x7, [sp, #48]\n\t"                                    \
                         "str x30, [sp, #64]\n\t"                                       \
                         "bl capture_" #name "\n\t"                                     \
                         "ldr x30, [sp, #64]\n\t"                                       \
                         "ldp x6, x7, [sp, #48]\n\t"                                    \
                         "ldp x4, x5, [sp, #32]\n\t"                                    \
                         "ldp x2, x3, [sp, #16]\n\t"                                    \
                         "ldp x0, x1, [sp], #80\n\t"                                    \
                         "adrp x16, g_" #name "_tramp\n\t"                              \
                         "ldr x16, [x16, :lo12:g_" #name "_tramp]\n\t"                  \
                         "br x16\n\t");                                                 \
    }

CAPTURE(action, EV_ACTION, 0x18, 0x20)
CAPTURE(invite, EV_INVITE, 0x40, 0x48)
CAPTURE(s2c, EV_S2C, 0x18, 0)

static int function_mentions(const void *fn, uint32_t imm) {
    uint8_t buf[0x180];
    if (!safe_copy(fn, buf, sizeof buf)) return 0;
    for (size_t i = 0; i + 4 <= sizeof buf; i += 4) {
        uint32_t insn = 0;
        memcpy(&insn, buf + i, sizeof insn);
        if ((insn & 0xffc00000u) == 0x91000000u && ((insn >> 10) & 0xfffu) == imm) return 1;
    }
    return 0;
}

static int patch_head(uint8_t *target, void *detour, void **tramp_out) {
    uint8_t prologue[16];
    if (!safe_copy(target, prologue, sizeof prologue)) return 0;
    void *page = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (page == MAP_FAILED) return 0;
    memcpy(page, prologue, sizeof prologue);
    uint32_t *jump = (uint32_t *)((uint8_t *)page + 16);
    jump[0] = 0x58000050u;
    jump[1] = 0xd61f0200u;
    uint64_t resume = (uint64_t)(uintptr_t)(target + 16);
    memcpy((uint8_t *)page + 24, &resume, sizeof resume);
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
    *tramp_out = page;
    uint32_t hook[2] = {0x58000050u, 0xd61f0200u};
    uint64_t dest = (uint64_t)(uintptr_t)detour;
    memcpy(target, hook, sizeof hook);
    memcpy(target + 8, &dest, sizeof dest);
    __builtin___clear_cache((char *)target, (char *)target + 16);
    mprotect((void *)start, 4096, PROT_READ | PROT_EXEC);
    return 1;
}

static int install_callback_hook(uintptr_t off, uint32_t imm, const uint8_t prologue[16], void *detour,
                                 void **tramp_out) {
    uint8_t *target = (uint8_t *)(g_base + off);
    uint8_t actual[16];
    if (!safe_copy(target, actual, sizeof actual) || memcmp(actual, prologue, sizeof actual) != 0 ||
        !function_mentions(target, imm)) {
        return 0;
    }
    return patch_head(target, detour, tramp_out);
}

static void install_callback_hooks(void) {
    const uint8_t shared[] = {0xff, 0x03, 0x02, 0xd1, 0xfd, 0x7b, 0x05, 0xa9,
                              0xf6, 0x57, 0x06, 0xa9, 0xf4, 0x4f, 0x07, 0xa9};
    const uint8_t s2c_prologue[] = {0xff, 0xc3, 0x01, 0xd1, 0xfd, 0x7b, 0x04, 0xa9,
                                    0xf6, 0x57, 0x05, 0xa9, 0xf4, 0x4f, 0x06, 0xa9};
    int action = install_callback_hook(AV_ACTION_OFF, AV_ACTION_IMM, shared, action_detour, &g_action_tramp);
    int invite = install_callback_hook(AV_INVITE_OFF, AV_INVITE_IMM, shared, invite_detour, &g_invite_tramp);
    int s2c = install_callback_hook(AV_S2C_OFF, AV_S2C_IMM, s2c_prologue, s2c_detour, &g_s2c_tramp);
    if (action && invite && s2c) fprintf(stderr, "[MomoiQQCall] native AVSDK callbacks hooked\n");
    else fprintf(stderr, "[MomoiQQCall] native AVSDK callback anchor mismatch\n");
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
    for (int i = 2; i < (int)(sizeof g_listener_vt / sizeof g_listener_vt[0]); i++) {
        g_listener_vt[i] = (void *)listener_event;
    }
    g_listener.vt = g_listener_vt;
    g_ctrl.vt = NULL;
    g_ctrl.owners = 0x100000;
    g_ctrl.weak = 0x100000;
    g_hooked = 1;
    fprintf(stderr, "[MomoiQQCall] native NT session hook installed\n");
    install_callback_hooks();
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
    napi_value callback_count;
    napi_create_int32(env, identified ? (int)strlen(path) : 0, &path_len);
    napi_create_int32(env, g_callbacks, &callback_count);
    napi_set_named_property(env, obj, "hooked", hooked);
    napi_set_named_property(env, obj, "fires", fires);
    napi_set_named_property(env, obj, "ready", ready);
    napi_set_named_property(env, obj, "service", service);
    napi_set_named_property(env, obj, "registered", registered);
    napi_set_named_property(env, obj, "uidLen", uid_len);
    napi_set_named_property(env, obj, "uinLen", uin_len);
    napi_set_named_property(env, obj, "pathLen", path_len);
    napi_set_named_property(env, obj, "callbacks", callback_count);
    return obj;
}

static int ensure_tsfn(napi_env env) {
    if (g_tsfn) return 1;
    napi_value name;
    napi_create_string_utf8(env, "momoi-avsdk", NAPI_AUTO_LENGTH, &name);
    if (napi_create_threadsafe_function(env, NULL, NULL, name, 0, 1, NULL, NULL, NULL, js_invoke,
                                        &g_tsfn) != napi_ok) {
        fprintf(stderr, "[MomoiQQCall] native AVSDK callback queue unavailable\n");
        return 0;
    }
    return 1;
}

static napi_value RegisterListener(napi_env env, napi_callback_info info) {
    size_t argc = 1;
    napi_value args[1];
    napi_get_cb_info(env, info, &argc, args, NULL, NULL);
    if (argc >= 1) {
        napi_valuetype type = napi_undefined;
        napi_typeof(env, args[0], &type);
        if (type == napi_object && ensure_tsfn(env)) {
            if (g_listener_ref) napi_delete_reference(env, g_listener_ref);
            napi_create_reference(env, args[0], 1, &g_listener_ref);
        }
    }
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
    momoi_span_string_init(raw, payload, len);
    typedef void (*action_fn)(void *, int32_t, void *);
    fprintf(stderr, "[MomoiQQCall] native setAction command=%d bytes=%zu\n", command, len);
    ((action_fn)fn)(g_service.ptr, command, raw);
    free(payload);
    napi_value undef;
    napi_get_undefined(env, &undef);
    return undef;
}

static napi_value Close(napi_env env, napi_callback_info info) {
    (void)info;
    if (g_tsfn) {
        napi_release_threadsafe_function(g_tsfn, napi_tsfn_release);
        g_tsfn = NULL;
    }
    if (g_listener_ref) {
        napi_delete_reference(env, g_listener_ref);
        g_listener_ref = NULL;
    }
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
    export_fn(env, exports, "close", Close);
    return exports;
}
