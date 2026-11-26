#!/usr/bin/env python3
import pathlib
import sys

# 1. Создаем заголовочный файл tu_balancer.h (Unleashed V5 под Snapdragon 8 Gen 3 / CPU 2-7)
header_path = pathlib.Path("src/freedreno/vulkan/tu_balancer.h")
header_code = r"""#ifndef TU_BALANCER_H
#define TU_BALANCER_H

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <sched.h>
#include <unistd.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <pthread.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <linux/sched.h>

#ifdef __ANDROID__
#include <android/log.h>
#define TU_LOGI(...) __android_log_print(ANDROID_LOG_INFO, "TurnipUnleashed", __VA_ARGS__)
#else
#define TU_LOGI(...) fprintf(stderr, "[TurnipUnleashed] " __VA_ARGS__)
#endif

typedef enum {
    TU_MAX_UNLEASHED = 0,
    TU_MAX_GUARD     = 1
} tu_max_state_t;

typedef struct __attribute__((aligned(64))) {
    _Atomic int state;
    _Atomic uint32_t epoch;
    _Atomic int soc_temp_c;
    bool initialized;
    bool debug_log;
    int thermal_ceiling_c;
} tu_balancer_state_t;

extern tu_balancer_state_t g_tu_balancer;

struct tu_sched_attr {
    uint32_t size;
    uint32_t sched_policy;
    uint64_t sched_flags;
    int32_t  sched_nice;
    uint32_t sched_priority;
    uint64_t sched_runtime;
    uint64_t sched_deadline;
    uint64_t sched_period;
    uint32_t sched_util_min;
    uint32_t sched_util_max;
};

#ifndef SCHED_FLAG_UTIL_CLAMP_MIN
#define SCHED_FLAG_UTIL_CLAMP_MIN 0x20
#endif
#ifndef SCHED_FLAG_UTIL_CLAMP_MAX
#define SCHED_FLAG_UTIL_CLAMP_MAX 0x40
#endif

static inline uint64_t tu_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

/* Жесткая фиксация ядер в рамках CPU 2..7 (0-1 система, 6-7 игра) */
static inline void tu_pin_cores(int start_cpu, int end_cpu) {
    if (start_cpu < 2) start_cpu = 2;
    if (end_cpu > 7) end_cpu = 7;

    cpu_set_t target, current;
    CPU_ZERO(&target);
    for (int i = start_cpu; i <= end_cpu; i++) {
        CPU_SET(i, &target);
    }

    if (sched_getaffinity(0, sizeof(cpu_set_t), &current) == 0) {
        if (CPU_EQUAL(&target, &current)) return;
    }
    sched_setaffinity(0, sizeof(cpu_set_t), &target);
}

static inline void tu_boost_thread(int nice_val, uint32_t min_util) {
    struct tu_sched_attr attr = {
        .size = sizeof(struct tu_sched_attr),
        .sched_policy = SCHED_OTHER,
        .sched_flags = SCHED_FLAG_UTIL_CLAMP_MIN | SCHED_FLAG_UTIL_CLAMP_MAX,
        .sched_nice = nice_val,
        .sched_util_min = min_util,
        .sched_util_max = 1024,
    };
    if (syscall(SYS_sched_setattr, 0, &attr, 0) != 0) {
        setpriority(PRIO_PROCESS, 0, nice_val);
    }
}

static inline int tu_find_sm8650_thermal_zone(void) {
    char path[128], type[64];
    for (int i = 0; i < 60; i++) {
        snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/type", i);
        FILE *f = fopen(path, "r");
        if (!f) continue;
        if (fgets(type, sizeof(type), f)) {
            if (strncmp(type, "gpuss-0", 7) == 0 || strncmp(type, "cpu-1-0", 7) == 0) {
                fclose(f);
                return i;
            }
        }
        fclose(f);
    }
    return 0;
}

static inline int tu_read_temp_fast(int zone) {
    char path[128];
    int temp = 50000;
    snprintf(path, sizeof(path), "/sys/class/thermal/thermal_zone%d/temp", zone);
    FILE *f = fopen(path, "r");
    if (f) {
        if (fscanf(f, "%d", &temp) != 1) temp = 50000;
        fclose(f);
    }
    return temp / 1000;
}

static inline void tu_balancer_init(void) {
    if (g_tu_balancer.initialized) return;

    const char *env_log = getenv("TU_BALANCER_LOG");
    const char *env_ceil = getenv("TU_THERMAL_CEILING");

    g_tu_balancer.debug_log = (env_log && atoi(env_log) > 0);
    g_tu_balancer.thermal_ceiling_c = env_ceil ? atoi(env_ceil) : 81;

    __atomic_store_n(&g_tu_balancer.state, TU_MAX_UNLEASHED, __ATOMIC_RELAXED);
    __atomic_store_n(&g_tu_balancer.epoch, 1, __ATOMIC_RELAXED);
    __atomic_store_n(&g_tu_balancer.soc_temp_c, 50, __ATOMIC_RELAXED);
    g_tu_balancer.initialized = true;

    if (g_tu_balancer.debug_log) {
        TU_LOGI("UNLEASHED V5 Active: Submit [4-5], Shaders [2-3], Game [6-7]");
    }
}

static inline void tu_balancer_tick_render(void) {
    static thread_local uint64_t last_temp_ns = 0;
    static thread_local int thermal_zone = -1;
    static thread_local int local_state = -1;
    static thread_local uint32_t tick = 0;

    tick++;
    int cur_state = __atomic_load_n(&g_tu_balancer.state, __ATOMIC_RELAXED);

    if (__builtin_expect((tick & 127) == 0, 0)) {
        uint64_t now = tu_now_ns();
        if (now - last_temp_ns > 3000000000ULL) {
            if (thermal_zone == -1) thermal_zone = tu_find_sm8650_thermal_zone();
            int temp_c = tu_read_temp_fast(thermal_zone);
            __atomic_store_n(&g_tu_balancer.soc_temp_c, temp_c, __ATOMIC_RELAXED);
            last_temp_ns = now;

            int next_state = cur_state;
            if (temp_c >= g_tu_balancer.thermal_ceiling_c) {
                next_state = TU_MAX_GUARD;
            } else if (cur_state == TU_MAX_GUARD && temp_c <= (g_tu_balancer.thermal_ceiling_c - 4)) {
                next_state = TU_MAX_UNLEASHED;
            }

            if (next_state != cur_state) {
                __atomic_store_n(&g_tu_balancer.state, next_state, __ATOMIC_RELAXED);
                __atomic_fetch_add(&g_tu_balancer.epoch, 1, __ATOMIC_RELEASE);
                cur_state = next_state;
                if (g_tu_balancer.debug_log) {
                    TU_LOGI("State -> %s (Temp: %dC)",
                            cur_state == TU_MAX_UNLEASHED ? "UNLEASHED" : "GUARD", temp_c);
                }
            }
        }
    }

    if (__builtin_expect(cur_state != local_state || (tick & 63) == 0, 0)) {
        tu_pin_cores(4, 5);
        if (cur_state == TU_MAX_UNLEASHED) {
            tu_boost_thread(-8, 1024);
        } else {
            tu_boost_thread(-4, 880);
        }
        local_state = cur_state;
    }
}

static inline void tu_balancer_sync_worker(void) {
    static thread_local uint32_t last_epoch = 0;
    uint32_t cur_epoch = __atomic_load_n(&g_tu_balancer.epoch, __ATOMIC_ACQUIRE);

    if (__builtin_expect(cur_epoch == last_epoch, 1)) return;

    int state = __atomic_load_n(&g_tu_balancer.state, __ATOMIC_RELAXED);
    if (state == TU_MAX_UNLEASHED) {
        tu_pin_cores(2, 3);
        tu_boost_thread(-4, 900);
    } else {
        tu_pin_cores(2, 2);
        tu_boost_thread(0, 600);
    }
    last_epoch = cur_epoch;
}

static inline void tu_isolate_translator_threads(void) {
    static thread_local bool checked = false;
    if (__builtin_expect(checked, 1)) return;
    checked = true;

    char name[32] = {0};
    if (pthread_getname_np(pthread_self(), name, sizeof(name)) == 0) {
        if (strstr(name, "dxvk") || strstr(name, "vkd3d") || strstr(name, "shader")) {
            tu_pin_cores(2, 3);
            tu_boost_thread(-2, 850);
        }
    }
}

#endif /* TU_BALANCER_H */
"""
header_path.write_text(header_code)
print("[y700_gen3_balancer] Created tu_balancer.h")

# 2. Патчим tu_device.cc (инициализация и экспорт хука)
dev_file = pathlib.Path("src/freedreno/vulkan/tu_device.cc")
dev_src = dev_file.read_text()
if "tu_balancer.h" not in dev_src:
    prefix = '#include "tu_balancer.h"\ntu_balancer_state_t g_tu_balancer = {0};\nextern "C" void tu_balancer_worker_hook(void) { tu_balancer_sync_worker(); }\n'
    dev_src = prefix + dev_src
    idx = dev_src.find("tu_CreateDevice(")
    if idx == -1:
        sys.exit("Error: tu_CreateDevice not found")
    brace = dev_src.find("{", idx)
    dev_src = dev_src[:brace+1] + "\n   tu_balancer_init();\n" + dev_src[brace+1:]
    dev_file.write_text(dev_src)
    print("[y700_gen3_balancer] Patched tu_device.cc")

# 3. Патчим tu_knl_kgsl.cc (балансировка очередей KGSL)
kgsl_file = pathlib.Path("src/freedreno/vulkan/tu_knl_kgsl.cc")
kgsl_src = kgsl_file.read_text()
if "tu_balancer.h" not in kgsl_src:
    kgsl_src = '#include "tu_balancer.h"\n' + kgsl_src
    idx = kgsl_src.find("kgsl_queue_submit(")
    if idx == -1:
        sys.exit("Error: kgsl_queue_submit not found")
    brace = kgsl_src.find("{", idx)
    hook = "\n   if (queue->vk.queue_family_index == 0) tu_balancer_tick_render(); else tu_pin_cores(2, 3);\n"
    kgsl_src = kgsl_src[:brace+1] + hook + kgsl_src[brace+1:]
    kgsl_file.write_text(kgsl_src)
    print("[y700_gen3_balancer] Patched tu_knl_kgsl.cc")

# 4. Патчим tu_shader.cc (изоляция потоков DXVK/VKD3D)
shader_file = pathlib.Path("src/freedreno/vulkan/tu_shader.cc")
if shader_file.exists():
    sh_src = shader_file.read_text()
    if "tu_balancer.h" not in sh_src:
        sh_src = '#include "tu_balancer.h"\n' + sh_src
        idx = sh_src.find("tu_CreateShaderModule(")
        if idx != -1:
            brace = sh_src.find("{", idx)
            sh_src = sh_src[:brace+1] + "\n   tu_isolate_translator_threads();\n" + sh_src[brace+1:]
            shader_file.write_text(sh_src)
            print("[y700_gen3_balancer] Patched tu_shader.cc")

# 5. Патчим src/util/u_queue.c (воркеры компиляции шейдеров на CPU 2-3)
uq_file = pathlib.Path("src/util/u_queue.c")
uq_src = uq_file.read_text()
if "tu_balancer_worker_hook" not in uq_src:
    decl = '\n#ifdef __ANDROID__\n__attribute__((weak)) void tu_balancer_worker_hook(void);\n#endif\n'
    idx = uq_src.find("util_queue_thread_func(")
    if idx == -1:
        sys.exit("Error: util_queue_thread_func not found")
    uq_src = uq_src[:idx] + decl + uq_src[idx:]
    target = "job.execute(job.job, job.gdata, thread_index);"
    if target not in uq_src:
        sys.exit("Error: job.execute not found in u_queue.c")
    inject = "#ifdef __ANDROID__\n      if (tu_balancer_worker_hook) tu_balancer_worker_hook();\n#endif\n      " + target
uq_src = uq_src.replace(target, inject, 1)
uq_file.write_text(uq_src)
print("[y700_gen3_balancer] Patched u_queue.c")
sh_src = '#include "tu_balancer.h"\n' + sh_src
idx = sh_src.find("tu_CreateShaderModule(")
if idx != -1:
brace = sh_src.find("{", idx)
sh_src = sh_src[:brace+1] + "\n   tu_isolate_translator_threads();\n" + sh_src[brace+1:]
shader_file.write_text(sh_src)
print("[y700_gen3_balancer] Patched tu_shader.cc")

5. Патчим src/util/u_queue.c

uq_file = pathlib.Path("src/util/u_queue.c")
uq_src = uq_file.read_text()
if "tu_balancer_worker_hook" not in uq_src:
decl = '\n#ifdef ANDROID\n__attribute__((weak)) void tu_balancer_worker_hook(void);\n#endif\n'
idx = uq_src.find("util_queue_thread_func(")
if idx == -1:
sys.exit("Error: util_queue_thread_func not found")
uq_src = uq_src[:idx] + decl + uq_src[idx:]
target = "job.execute(job.job, job.gdata, thread_index);"
if target not in uq_src:
sys.exit("Error: job.execute not found in u_queue.c")
inject = "#ifdef ANDROID\n      if (tu_balancer_worker_hook) tu_balancer_worker_hook();\n#endif\n      " + target
uq_src = uq_src.replace(target, inject, 1)
uq_file.write_text(uq_src)
print("[y700_gen3_balancer] Patched u_queue.c")
