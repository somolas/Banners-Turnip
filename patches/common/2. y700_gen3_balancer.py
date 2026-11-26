#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
y700_gen3_balancer.py — патчер Mesa/Turnip (freedreno) для Lenovo Y700 Gen 3
(SM8650) и совместимых Snapdragon SoC.

Шаги:
  1. Генерирует src/freedreno/vulkan/tu_balancer.h (Unleashed V7):
       - автораскладка кластеров CPU по cpufreq (не требует правок под SoC);
       - UNLEASHED / GUARD (перегрев) / CALM (низкая нагрузка);
       - uclamp min+max, nice, EMA термалки, антифлап, бонус на зарядке;
       - весь конфиг через env, есть kill-switch.
  2. tu_device.cc   — tu_balancer_init() в tu_CreateDevice + экспорт hook'а.
  3. tu_knl_kgsl.cc — хук сабмита (fallback: tu_queue.cc).
  4. tu_shader.cc   — изоляция потоков-трансляторов (dxvk/vkd3d/shader).
  5. src/util/u_queue.c — weak-хук воркеров компиляции.

Коды выхода: 0 — успех; 1 — критическая ошибка; 2 — есть предупреждения;
3 — self-test провален.
"""

import argparse
import difflib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

BACKUP_SUFFIX = ".tubak"
TMP_SUFFIX    = ".tutmp"
INCLUDE_MARK  = '#include "tu_balancer.h"'
ABI           = 7

REL_HEADER = "src/freedreno/vulkan/tu_balancer.h"
REL_DEVICE = "src/freedreno/vulkan/tu_device.cc"
REL_KGSL   = "src/freedreno/vulkan/tu_knl_kgsl.cc"
REL_QUEUE  = "src/freedreno/vulkan/tu_queue.cc"
REL_SHADER = "src/freedreno/vulkan/tu_shader.cc"
REL_UQUEUE = "src/util/u_queue.c"

TARGET_FILES = (REL_HEADER, REL_DEVICE, REL_KGSL, REL_QUEUE, REL_SHADER, REL_UQUEUE)

# ------------------------------------------------------------- фрагменты ----

DEVICE_INIT = "\n   tu_balancer_init();\n"

GLOBAL_DEF = "tu_balancer_state_t g_tu_balancer = {};"

HOOK_DEF = (
    'extern "C" void tu_balancer_worker_hook(const char *queue_name)\n'
    "{\n"
    "   tu_balancer_sync_worker(queue_name);\n"
    "}\n"
)

DEVICE_BLOCK = INCLUDE_MARK + "\n" + GLOBAL_DEF + "\n\n" + HOOK_DEF + "\n"

SUBMIT_HOOK = (
    "\n   {\n"
    "      tu_balancer_init();\n"
    "      if (queue->vk.queue_family_index == 0)\n"
    "         tu_balancer_tick_render();          /* render queue */\n"
    "      else\n"
    "         tu_apply_cpu_mask(&g_tu_balancer.shader_mask);\n"
    "   }\n"
)

SHADER_HOOK = "\n   tu_isolate_translator_threads();\n"

UQUEUE_DECL = (
    "/* [y700_balancer] weak-хук балансировщика (реализация в turnip) */\n"
    "#ifdef __ANDROID__\n"
    "__attribute__((weak)) void tu_balancer_worker_hook(const char *queue_name);\n"
    "#endif\n"
    "\n"
)

UQUEUE_CALL = (
    "{i}#ifdef __ANDROID__\n"
    "{i}if (tu_balancer_worker_hook)\n"
    "{i}   tu_balancer_worker_hook(queue->name);\n"
    "{i}#endif\n"
)

# ---------------------------------------------------------------- header ----

HEADER_CODE = r"""#ifndef TU_BALANCER_H
#define TU_BALANCER_H

/*
 * Turnip Unleashed V7 — CPU/термал-балансер для Turnip (Android/KGSL).
 * Сделан под Lenovo Y700 Gen 3 (SM8650), но раскладка ядер определяется
 * автоматически по cpufreq, так что работает и на других Snapdragon.
 *
 * Состояния:
 *   UNLEASHED — полный разгон рендера (uclamp.min = uclamp.max = 1024)
 *   GUARD     — перегрев: урезаем uclamp.max и приоритет, антифлап-удержание
 *   CALM      — низкая активность (меню/лаунчер): экономим батарею
 *
 * Переменные окружения:
 *   TU_BALANCER_DISABLE=1   — полностью выключить балансер (kill-switch)
 *   TU_BALANCER_LOG=1       — отладочный лог (logcat: TurnipUnleashed)
 *   TU_THERMAL_CEILING=81   — порог GUARD, °C
 *   TU_THERMAL_HYST=4       — гистерезис выхода из GUARD, °C
 *   TU_THERMAL_HOLD_MS=5000 — минимальное время в GUARD (антифлап)
 *   TU_THERMAL_ZONE=N       — форс thermal_zone N (иначе автопоиск gpuss/hwmon)
 *   TU_CHARGE_BOOST=1       — на зарядке поднять порог на +2°C
 *   TU_CALM_RATE=60         — порог CALM, сабмитов/с (0 = выключить)
 *   TU_CPU_FLOOR=2          — нижняя граница CPU для env/статических масок
 *   TU_CPU_CEIL=7           — верхняя граница CPU для env/статических масок
 *   TU_RENDER_CORES=4-5     — маска рендера ("4-5", "2,3", "6")
 *   TU_SHADER_CORES=2-3     — маска компиляции шейдеров
 *   TU_WORKER_CORES=2-3     — маска прочих util_queue-воркеров
 *
 * Маски из env и статические фоллбэки клампятся в [TU_CPU_FLOOR..TU_CPU_CEIL]
 * и пересекаются с affinity процесса. Автораскладка по топологии их не клампит.
 *
 * ВАЖНО: заголовок подключается только из C++ TU (.cc): используются
 * thread_local и static-локалы inline-функций.
 */

#define TU_BALANCER_ABI 7

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include <ctype.h>
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

#ifdef __ANDROID__
#include <android/log.h>
#define TU_LOGI(...) __android_log_print(ANDROID_LOG_INFO, "TurnipUnleashed", __VA_ARGS__)
#else
#define TU_LOGI(...) fprintf(stderr, "[TurnipUnleashed] " __VA_ARGS__)
#endif

/* --- uapi: sched_setattr (фоллбэки для старых sysroot) --- */
#ifndef SCHED_FLAG_RESET_ON_FORK
#define SCHED_FLAG_RESET_ON_FORK  0x01
#endif
#ifndef SCHED_FLAG_RECLAIM
#define SCHED_FLAG_RECLAIM        0x02
#endif
#ifndef SCHED_FLAG_DL_OVERRUN
#define SCHED_FLAG_DL_OVERRUN     0x04
#endif
#ifndef SCHED_FLAG_KEEP_POLICY
#define SCHED_FLAG_KEEP_POLICY    0x08
#endif
#ifndef SCHED_FLAG_KEEP_PARAMS
#define SCHED_FLAG_KEEP_PARAMS    0x10
#endif
#ifndef SCHED_FLAG_UTIL_CLAMP_MIN
#define SCHED_FLAG_UTIL_CLAMP_MIN 0x20
#endif
#ifndef SCHED_FLAG_UTIL_CLAMP_MAX
#define SCHED_FLAG_UTIL_CLAMP_MAX 0x40
#endif

typedef enum {
    TU_MAX_UNLEASHED = 0,
    TU_MAX_GUARD     = 1,
    TU_MAX_CALM      = 2
} tu_max_state_t;

/* ABI ядра: struct sched_attr (uapi/linux/sched/types.h) */
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

/*
 * Горячие поля (state/epoch/rate/temp) — в первой кэш-линии (align 64).
 * state/epoch/submit_rate/soc_temp_c — только через __atomic_*.
 */
typedef struct __attribute__((aligned(64))) {
    int       state;
    uint32_t  epoch;
    uint32_t  submit_rate;   /* сабмитов/с, EMA */
    int       soc_temp_c;    /* EMA, °C; <0 — нет данных */
    int       thermal_zone;  /* -1 = не определена */

    bool      ready;
    bool      debug_log;
    bool      disabled;
    bool      charge_boost;
    int       ceiling_c;
    int       hyst_c;
    int       calm_rate;
    int64_t   hold_ns;
    char      temp_path[96];
    cpu_set_t render_mask;
    cpu_set_t shader_mask;
    cpu_set_t worker_mask;

    int64_t   guard_enter_ns;
} tu_balancer_state_t;

extern tu_balancer_state_t g_tu_balancer;

static inline uint64_t tu_now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static inline int tu_env_int(const char *name, int def)
{
    const char *v = getenv(name);
    return (v && *v) ? atoi(v) : def;
}

static inline int64_t tu_ms_to_ns(int ms)
{
    return (int64_t)ms * 1000000LL;
}

static inline int tu_cpu_clamp(int cpu)
{
    int lo = tu_env_int("TU_CPU_FLOOR", 2);
    int hi = tu_env_int("TU_CPU_CEIL", 7);
    if (lo > hi) { int t = lo; lo = hi; hi = t; }
    return cpu < lo ? lo : (cpu > hi ? hi : cpu);
}

/* Список CPU ("4-5", "2,3", "6") -> cpu_set_t, кламп в [FLOOR..CEIL] */
static inline bool tu_parse_cpu_set(const char *spec, cpu_set_t *out)
{
    CPU_ZERO(out);
    if (!spec || !*spec)
        return false;

    char buf[128];
    size_t n = strlen(spec);
    if (n >= sizeof(buf))
        n = sizeof(buf) - 1;
    memcpy(buf, spec, n);
    buf[n] = '\0';

    char *save = NULL;
    for (char *tok = strtok_r(buf, ",", &save); tok;
         tok = strtok_r(NULL, ",", &save)) {
        int lo, hi;
        char *dash = strchr(tok, '-');
        if (dash) {
            *dash = '\0';
            lo = atoi(tok);
            hi = atoi(dash + 1);
        } else {
            lo = hi = atoi(tok);
        }
        if (lo > hi) { int t = lo; lo = hi; hi = t; }
        for (int c = lo; c <= hi && c < CPU_SETSIZE; c++)
            CPU_SET(tu_cpu_clamp(c), out);
    }
    return CPU_COUNT(out) > 0;
}

static inline void tu_mask_copy(cpu_set_t *dst, const cpu_set_t *src)
{
    CPU_ZERO(dst);
    for (int i = 0; i < CPU_SETSIZE; i++)
        if (CPU_ISSET(i, src))
            CPU_SET(i, dst);
}

static inline void tu_mask_and_allowed(cpu_set_t *m)
{
    cpu_set_t allowed;
    if (sched_getaffinity(0, sizeof(allowed), &allowed) != 0)
        return;
    for (int i = 0; i < CPU_SETSIZE; i++)
        if (CPU_ISSET(i, m) && !CPU_ISSET(i, &allowed))
            CPU_CLR(i, m);
}

static inline void tu_apply_cpu_mask(const cpu_set_t *mask)
{
    if (CPU_COUNT(mask) == 0)
        return;                       /* пустая маска — не пиним (иначе EINVAL) */
    cpu_set_t cur;
    if (sched_getaffinity(0, sizeof(cur), &cur) == 0 && CPU_EQUAL(mask, &cur))
        return;
    sched_setaffinity(0, sizeof(cpu_set_t), mask);
}

/* nice + uclamp (uclamp требует CAP_SYS_NICE; иначе фоллбэк на setpriority) */
static inline void tu_boost_thread(int nice_val, uint32_t uclamp_min,
                                   uint32_t uclamp_max)
{
#ifdef SYS_sched_setattr
    static int setattr_ok = 1;   /* 0 = ядро отказало — syscall не дёргаем */
    if (__atomic_load_n(&setattr_ok, __ATOMIC_RELAXED)) {
        struct tu_sched_attr attr;
        memset(&attr, 0, sizeof(attr));
        attr.size           = (uint32_t)sizeof(attr);
        attr.sched_policy   = SCHED_OTHER;
        attr.sched_flags    = SCHED_FLAG_UTIL_CLAMP_MIN |
                              SCHED_FLAG_UTIL_CLAMP_MAX;
        attr.sched_nice     = nice_val;
        attr.sched_util_min = uclamp_min;
        attr.sched_util_max = uclamp_max;
        if (syscall(SYS_sched_setattr, 0, &attr, 0) == 0)
            return;
        __atomic_store_n(&setattr_ok, 0, __ATOMIC_RELAXED);
    }
#endif
    setpriority(PRIO_PROCESS, 0, nice_val);
}

/* --- автораскладка кластеров по cpufreq --- */

struct tu_cpu_info { int cpu; int khz; };

static inline int tu_cpu_max_freq_khz(int cpu)
{
    char path[96];
    snprintf(path, sizeof(path),
             "/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq", cpu);
    FILE *f = fopen(path, "r");
    int khz = -1;
    if (f) {
        if (fscanf(f, "%d", &khz) != 1)
            khz = -1;
        fclose(f);
    }
    return khz;
}

static inline void tu_sort_freq_desc(struct tu_cpu_info *a, int n)
{
    for (int i = 1; i < n; i++) {
        const struct tu_cpu_info t = a[i];
        int j = i - 1;
        while (j >= 0 && a[j].khz < t.khz) {
            a[j + 1] = a[j];
            j--;
        }
        a[j + 1] = t;
    }
}

/* Конец кластера, начиная с start (частоты внутри кластера различаются <5%) */
static inline int tu_cluster_end(const struct tu_cpu_info *a, int n, int start)
{
    if (start >= n || a[start].khz <= 0)
        return n;
    const int64_t ref = a[start].khz;
    int i = start + 1;
    while (i < n && a[i].khz > 0 &&
           (int64_t)a[i].khz * 100 >= ref * 95 &&
           (int64_t)a[i].khz * 100 <= ref * 105)
        i++;
    return i;
}

static inline bool tu_mask_from_range(cpu_set_t *m, const struct tu_cpu_info *a,
                                      int from, int to, int cap)
{
    CPU_ZERO(m);
    int added = 0;
    for (int i = from; i < to && added < cap; i++) {
        CPU_SET(a[i].cpu, m);
        added++;
    }
    return CPU_COUNT(m) > 0;
}

/*
 * Топология -> маски. Самый частотный кластер оставляем игре, рендер —
 * следующий, шейдеры — ещё ниже, worker — самый тихий. false = нет cpufreq
 * или меньше 4 ядер: вызывающий возьмёт статические фоллбэки.
 */
static inline bool tu_auto_masks(cpu_set_t *render, cpu_set_t *shader,
                                 cpu_set_t *worker)
{
    long ncpu = sysconf(_SC_NPROCESSORS_ONLN);
    if (ncpu < 4)
        return false;
    if (ncpu > CPU_SETSIZE)
        ncpu = CPU_SETSIZE;

    struct tu_cpu_info cs[CPU_SETSIZE];
    int cnt = 0;
    for (int c = 0; c < (int)ncpu && cnt < CPU_SETSIZE; c++) {
        const int khz = tu_cpu_max_freq_khz(c);
        if (khz > 0) {
            cs[cnt].cpu = c;
            cs[cnt].khz = khz;
            cnt++;
        }
    }
    if (cnt < 4)
        return false;

    tu_sort_freq_desc(cs, cnt);

    const int g_end = tu_cluster_end(cs, cnt, 0);   /* игра — не трогаем */
    const int r_end = tu_cluster_end(cs, cnt, g_end);
    const int s_end = tu_cluster_end(cs, cnt, r_end);

    if (!tu_mask_from_range(render, cs, g_end, r_end, 4))
        return false;
    if (!tu_mask_from_range(shader, cs, r_end, s_end, 4))
        tu_mask_from_range(shader, cs, r_end, cnt, 4);
    if (!tu_mask_from_range(worker, cs, s_end, cnt, 3))
        tu_mask_copy(worker, shader);

    tu_mask_and_allowed(render);
    tu_mask_and_allowed(shader);
    tu_mask_and_allowed(worker);

    return CPU_COUNT(render) > 0 && CPU_COUNT(shader) > 0 &&
           CPU_COUNT(worker) > 0;
}

/* --- термальная телеметрия --- */

static inline bool tu_stristr(const char *hay, const char *needle)
{
    for (; *hay; hay++) {
        const char *h = hay, *n = needle;
        while (*n && tolower((unsigned char)*h) == tolower((unsigned char)*n)) {
            h++;
            n++;
        }
        if (!*n)
            return true;
    }
    return false;
}

/* Источник температуры: env -> thermal_zone (gpuss-*) -> hwmon (gpu) */
static inline bool tu_pick_temp_path(char *out, size_t cap, int *zone_out)
{
    int forced = tu_env_int("TU_THERMAL_ZONE", -1);
    if (forced >= 0) {
        snprintf(out, cap, "/sys/class/thermal/thermal_zone%d/temp", forced);
        if (zone_out)
            *zone_out = forced;
        return true;
    }

    static const char *const prefs[] = {
        "gpuss-0", "gpuss-1", "gpu-0", "gpu", "cpu-1-0"
    };

    char path[96], type[64];
    for (size_t p = 0; p < sizeof(prefs) / sizeof(prefs[0]); p++) {
        const size_t plen = strlen(prefs[p]);
        for (int i = 0; i < 64; i++) {
            snprintf(path, sizeof(path),
                     "/sys/class/thermal/thermal_zone%d/type", i);
            FILE *f = fopen(path, "r");
            if (!f)
                continue;
            bool hit = false;
            if (fgets(type, sizeof(type), f)) {
                type[strcspn(type, "\n")] = '\0';
                hit = (strncmp(type, prefs[p], plen) == 0);
            }
            fclose(f);
            if (hit) {
                snprintf(out, cap, "/sys/class/thermal/thermal_zone%d/temp", i);
                if (zone_out)
                    *zone_out = i;
                return true;
            }
        }
    }

    for (int i = 0; i < 16; i++) {
        snprintf(path, sizeof(path), "/sys/class/hwmon/hwmon%d/name", i);
        FILE *f = fopen(path, "r");
        if (!f)
            continue;
        bool hit = false;
        if (fgets(type, sizeof(type), f)) {
            type[strcspn(type, "\n")] = '\0';
            hit = tu_stristr(type, "gpu");
        }
        fclose(f);
        if (hit) {
            snprintf(out, cap, "/sys/class/hwmon/hwmon%d/temp1_input", i);
            if (zone_out)
                *zone_out = -2;
            return true;
        }
    }
    return false;
}

static inline bool tu_charging(void)
{
    FILE *f = fopen("/sys/class/power_supply/battery/status", "r");
    if (!f)
        return false;
    char s[32] = {0};
    bool ch = false;
    if (fgets(s, sizeof(s), f))
        ch = (strncmp(s, "Charging", 8) == 0 || strncmp(s, "Full", 4) == 0);
    fclose(f);
    return ch;
}

/* °C или -1, если телеметрия недоступна */
static inline int tu_read_temp_c(void)
{
    const char *path = g_tu_balancer.temp_path;
    if (!path[0])
        return -1;
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    int milli = 0;
    const bool ok = (fscanf(f, "%d", &milli) == 1);
    fclose(f);
    if (!ok || milli < -40000 || milli > 150000)
        return -1;
    return milli / 1000;
}

/* --- состояние --- */

static inline int tu_state(void)
{
    return __atomic_load_n(&g_tu_balancer.state, __ATOMIC_RELAXED);
}

/* Переключение состояния: epoch++ (RELEASE), лог. true — если сменилось */
static inline bool tu_set_state(int next, int64_t now_ns)
{
    tu_balancer_state_t *b = &g_tu_balancer;
    const int cur = tu_state();
    if (next == cur)
        return false;
    __atomic_store_n(&b->state, next, __ATOMIC_RELAXED);
    b->guard_enter_ns = (next == TU_MAX_GUARD) ? now_ns : 0;
    __atomic_fetch_add(&b->epoch, 1u, __ATOMIC_RELEASE);
    if (b->debug_log)
        TU_LOGI("state -> %s (temp %d C, rate %u/s)",
                next == TU_MAX_UNLEASHED ? "UNLEASHED" :
                next == TU_MAX_GUARD     ? "GUARD"     : "CALM",
                __atomic_load_n(&b->soc_temp_c, __ATOMIC_RELAXED),
                __atomic_load_n(&b->submit_rate, __ATOMIC_RELAXED));
    return true;
}

/* Профиль приоритета/маски рендера по состоянию */
static inline void tu_apply_render_state(void)
{
    tu_balancer_state_t *b = &g_tu_balancer;
    tu_apply_cpu_mask(&b->render_mask);
    switch (tu_state()) {
    case TU_MAX_UNLEASHED: tu_boost_thread(-8, 1024, 1024); break;
    case TU_MAX_GUARD:     tu_boost_thread(-4, 880, 700);   break;
    default:               tu_boost_thread(0, 0, 512);      break; /* CALM */
    }
}

/* CALM/UNLEASHED по частоте сабмитов (GUARD не трогаем) */
static inline void tu_decide_calm(int cur, int64_t now)
{
    tu_balancer_state_t *b = &g_tu_balancer;
    if (cur == TU_MAX_GUARD)
        return;
    if (b->calm_rate <= 0) {
        if (cur != TU_MAX_UNLEASHED)
            tu_set_state(TU_MAX_UNLEASHED, now);
        return;
    }
    const uint32_t rate = __atomic_load_n(&b->submit_rate, __ATOMIC_RELAXED);
    if (cur != TU_MAX_CALM && rate < (uint32_t)b->calm_rate)
        tu_set_state(TU_MAX_CALM, now);
    else if (cur == TU_MAX_CALM && rate > (uint32_t)b->calm_rate * 2u)
        tu_set_state(TU_MAX_UNLEASHED, now);
}

/* Раз в ~2с: термалка + решение о состоянии */
static inline void tu_decide_state(int64_t now)
{
    tu_balancer_state_t *b = &g_tu_balancer;
    int cur = tu_state();

    if (!b->temp_path[0]) {
        if (cur == TU_MAX_GUARD)
            tu_set_state(TU_MAX_UNLEASHED, now);
        tu_decide_calm(tu_state(), now);
        return;
    }

    const int t = tu_read_temp_c();
    if (t >= 0) {
        const int prev = __atomic_load_n(&b->soc_temp_c, __ATOMIC_RELAXED);
        const int ema = (prev < 0 || prev > 150) ? t : (prev * 2 + t) / 3;
        __atomic_store_n(&b->soc_temp_c, ema, __ATOMIC_RELAXED);

        const int ceil_c = b->ceiling_c +
                           ((b->charge_boost && tu_charging()) ? 2 : 0);
        if (ema >= ceil_c) {
            tu_set_state(TU_MAX_GUARD, now);
            return;
        }
        if (cur == TU_MAX_GUARD) {
            if (ema <= ceil_c - b->hyst_c && now - b->guard_enter_ns > b->hold_ns)
                ; /* выходим из GUARD: дальше решает нагрузка */
            else
                return;
        }
    } else if (cur == TU_MAX_GUARD) {
        /* телеметрия отвалилась — не застревать в GUARD */
        tu_set_state(TU_MAX_UNLEASHED, now);
        return;
    }

    tu_decide_calm(tu_state(), now);
}

/* --- инициализация (ровно один раз, pthread_once) --- */

static inline void tu_balancer_init_impl(void)
{
    tu_balancer_state_t *b = &g_tu_balancer;
    memset(b, 0, sizeof(*b));

    b->debug_log    = tu_env_int("TU_BALANCER_LOG", 0) > 0;
    b->disabled     = tu_env_int("TU_BALANCER_DISABLE", 0) > 0;
    b->ceiling_c    = tu_env_int("TU_THERMAL_CEILING", 81);
    b->hyst_c       = tu_env_int("TU_THERMAL_HYST", 4);
    b->calm_rate    = tu_env_int("TU_CALM_RATE", 60);
    b->charge_boost = tu_env_int("TU_CHARGE_BOOST", 0) > 0;
    b->hold_ns      = tu_ms_to_ns(tu_env_int("TU_THERMAL_HOLD_MS", 5000));
    b->thermal_zone = -1;

    tu_pick_temp_path(b->temp_path, sizeof(b->temp_path), &b->thermal_zone);

    /* маски: авто по топологии -> env-оверрайды -> пересечение с affinity */
    if (!tu_auto_masks(&b->render_mask, &b->shader_mask, &b->worker_mask)) {
        tu_parse_cpu_set("4-5", &b->render_mask);
        tu_parse_cpu_set("2-3", &b->shader_mask);
        tu_parse_cpu_set("2-3", &b->worker_mask);
    }
    const char *spec;
    if ((spec = getenv("TU_RENDER_CORES")) && *spec)
        tu_parse_cpu_set(spec, &b->render_mask);
    if ((spec = getenv("TU_SHADER_CORES")) && *spec)
        tu_parse_cpu_set(spec, &b->shader_mask);
    if ((spec = getenv("TU_WORKER_CORES")) && *spec)
        tu_parse_cpu_set(spec, &b->worker_mask);
    tu_mask_and_allowed(&b->render_mask);
    tu_mask_and_allowed(&b->shader_mask);
    tu_mask_and_allowed(&b->worker_mask);
    if (CPU_COUNT(&b->render_mask) == 0) tu_parse_cpu_set("4-5", &b->render_mask);
    if (CPU_COUNT(&b->shader_mask) == 0) tu_parse_cpu_set("2-3", &b->shader_mask);
    if (CPU_COUNT(&b->worker_mask) == 0) tu_parse_cpu_set("2-3", &b->worker_mask);

    __atomic_store_n(&b->state, (int)TU_MAX_UNLEASHED, __ATOMIC_RELAXED);
    __atomic_store_n(&b->epoch, 1u, __ATOMIC_RELEASE);
    __atomic_store_n(&b->soc_temp_c, 50, __ATOMIC_RELAXED);
    __atomic_store_n(&b->submit_rate, 0u, __ATOMIC_RELAXED);
    b->ready = true;

    if (b->debug_log)
        TU_LOGI("Unleashed V7: render=%d cpu, shader=%d cpu, worker=%d cpu; "
                "temp=%s; ceiling=%dC%s",
                CPU_COUNT(&b->render_mask), CPU_COUNT(&b->shader_mask),
                CPU_COUNT(&b->worker_mask),
                b->temp_path[0] ? b->temp_path : "off",
                b->ceiling_c,
                b->disabled ? " [DISABLED]" : "");
}

static inline void tu_balancer_init(void)
{
    static pthread_once_t once = PTHREAD_ONCE_INIT;
    pthread_once(&once, tu_balancer_init_impl);
}

/* --- сабмит рендера (kgsl_queue_submit, family==0) --- */

static inline void tu_balancer_tick_render(void)
{
    tu_balancer_init();
    tu_balancer_state_t *b = &g_tu_balancer;
    if (!b->ready || __atomic_load_n(&b->disabled, __ATOMIC_RELAXED))
        return;

    static thread_local uint32_t tick         = 0;
    static thread_local int      local_state  = -1;
    static thread_local uint32_t last_epoch   = 0;
    static thread_local int64_t  last_thermal = 0;
    static thread_local int64_t  win_start    = 0;
    static thread_local uint32_t win_submits  = 0;

    const int64_t now = tu_now_ns();
    tick++;

    /* частота сабмитов: окно 1с + EMA (3:1) */
    win_submits++;
    if (win_start == 0)
        win_start = now;
    const int64_t win = now - win_start;
    if (win >= 1000000000LL) {
        const uint32_t inst =
            (uint32_t)(((uint64_t)win_submits * 1000000000ULL) / (uint64_t)win);
        const uint32_t prev =
            __atomic_load_n(&b->submit_rate, __ATOMIC_RELAXED);
        __atomic_store_n(&b->submit_rate, (prev * 3u + inst) / 4u,
                         __ATOMIC_RELAXED);
        win_submits = 0;
        win_start = now;
    }

    /* телеметрия/состояние: каждые 128 сабмитов, не чаще 2с */
    if ((tick & 127u) == 0 && now - last_thermal > 2000000000LL) {
        last_thermal = now;
        tu_decide_state(now);
    }

    /* аффинность/приоритет: при смене состояния или раз в 64 сабмита */
    const uint32_t cur_epoch =
        __atomic_load_n(&b->epoch, __ATOMIC_ACQUIRE);
    const int cur_state = tu_state();
    if (cur_state != local_state || cur_epoch != last_epoch ||
        (tick & 63u) == 0) {
        tu_apply_render_state();
        local_state = cur_state;
        last_epoch  = cur_epoch;
    }
}

/* --- воркеры u_queue --- */

static inline void tu_balancer_sync_worker(const char *queue_name)
{
    tu_balancer_init();
    tu_balancer_state_t *b = &g_tu_balancer;
    if (!b->ready || __atomic_load_n(&b->disabled, __ATOMIC_RELAXED))
        return;

    static thread_local bool     first      = true;
    static thread_local uint32_t last_epoch = 0;

    const uint32_t cur_epoch =
        __atomic_load_n(&b->epoch, __ATOMIC_ACQUIRE);
    if (!first && cur_epoch == last_epoch)
        return;
    first      = false;
    last_epoch = cur_epoch;

    const bool is_shader =
        queue_name && strstr(queue_name, "shader") != NULL;

    if (is_shader) {
        tu_apply_cpu_mask(&b->shader_mask);
        switch (tu_state()) {
        case TU_MAX_UNLEASHED: tu_boost_thread(-4, 900, 1024); break;
        case TU_MAX_GUARD:     tu_boost_thread(2, 0, 600);     break;
        default:               tu_boost_thread(2, 0, 500);     break; /* CALM */
        }
    } else {
        tu_apply_cpu_mask(&b->worker_mask);
        tu_boost_thread(0, 0, 1024);
    }
}

/* --- изоляция трансляторов DXVK/VKD3D (tu_CreateShaderModule) --- */

static inline void tu_isolate_translator_threads(void)
{
    static thread_local bool checked = false;
    if (checked)
        return;
    checked = true;

    char name[64] = {0};
    if (pthread_getname_np(pthread_self(), name, sizeof(name)) != 0)
        return;
    if (!strstr(name, "dxvk") && !strstr(name, "vkd3d") &&
        !strstr(name, "shader"))
        return;

    tu_balancer_init();
    tu_balancer_state_t *b = &g_tu_balancer;
    if (!b->ready || __atomic_load_n(&b->disabled, __ATOMIC_RELAXED))
        return;

    tu_apply_cpu_mask(&b->shader_mask);
    tu_boost_thread(-2, 850, 1024);

    if (b->debug_log)
        TU_LOGI("translator '%s' -> shader cores", name);
}

#endif /* TU_BALANCER_H */
"""

# ------------------------------------------------------------- утилиты ------

_VERBOSE = False
_QUIET = False


def info(msg: str) -> None:
    if not _QUIET:
        print(f"[y700_balancer] {msg}")


def dbg(msg: str) -> None:
    if _VERBOSE:
        print(f"[y700_balancer] · {msg}")


def warn(msg: str) -> None:
    print(f"[y700_balancer] ВНИМАНИЕ: {msg}", file=sys.stderr)


def err(msg: str) -> None:
    print(f"[y700_balancer] ОШИБКА: {msg}", file=sys.stderr)


def die(msg: str, code: int = 1) -> None:
    err(msg)
    sys.exit(code)


def find_fn_def(src: str, name: str):
    """Первое ОПРЕДЕЛЕНИЕ `name` (прототипы пропускаются: у них до '{'
    встречается ';'). Возвращает (match, индекс '{') или (None, -1)."""
    for m in re.finditer(r"\b" + re.escape(name) + r"\s*\(", src):
        depth = 0
        for i in range(m.end(), len(src)):
            ch = src[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth <= 0 and ch == "{":
                return m, i
            elif depth <= 0 and ch == ";":
                break
    return None, -1


def insert_before_first_include(src: str, block: str) -> str:
    m = re.search(r"^#include\b", src, re.M)
    pos = m.start() if m else 0
    return src[:pos] + block + src[pos:]


def strip_c_code(text: str) -> str:
    """Убирает комментарии и содержимое строковых/символьных литералов."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "/" and nxt == "*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif c in "\"'":
            q = c
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == q:
                    i += 1
                    break
                i += 1
            out.append(q + q)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def brace_delta(text: str):
    s = strip_c_code(text)
    return tuple(s.count(a) - s.count(b) for a, b in (("{", "}"), ("(", ")"), ("[", "]")))


def verify_c_text(new_text: str, expect=(), orig_text=None):
    """Проверки ДО записи: баланс скобок (сравнивается с оригиналом) и
    наличие ожидаемых фрагментов. Вставки балансные, поэтому расхождение
    с оригиналом означает, что патч сломал синтаксис."""
    problems = []
    d_new = brace_delta(new_text)
    if orig_text is not None:
        if d_new != brace_delta(orig_text):
            problems.append(f"баланс скобок изменился: {brace_delta(orig_text)} -> {d_new}")
    elif d_new != (0, 0, 0):
        problems.append(f"несбалансированные скобки: {d_new}")
    for s in expect:
        if s not in new_text:
            problems.append(f"нет фрагмента {s!r}")
    return problems


def find_mesa_root(explicit: str | None):
    if explicit:
        return pathlib.Path(explicit).resolve()
    cur = pathlib.Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if (d / "src" / "freedreno" / "vulkan").is_dir():
            return d
    return None


def read_mesa_version(root: pathlib.Path):
    v = root / "VERSION"
    if v.is_file():
        try:
            m = re.search(r"(\d+\.\d+\.\d+)", v.read_text(encoding="utf-8", errors="replace"))
            return m.group(1) if m else None
        except OSError:
            return None
    return None

# ------------------------------------------------------------- патчер -------

@dataclass
class HookSpec:
    label: str
    targets: list          # [(файл, (кандидаты функций...)), ...]
    hook_code: str
    expect: str            # фрагмент, который обязан появиться после патча
    required: bool


SUBMIT_TARGETS = [
    (REL_KGSL, ("kgsl_queue_submit",)),
    (REL_QUEUE, ("tu_queue_submit",)),   # fallback, если в ревизии нет kgsl-файла
]

SUBMIT_SPEC = HookSpec("kgsl-submit", SUBMIT_TARGETS, SUBMIT_HOOK,
                       "tu_balancer_tick_render();", True)
SHADER_SPEC = HookSpec("shader-isolate",
                       [(REL_SHADER, ("tu_CreateShaderModule", "tu_CreateShaderModules"))],
                       SHADER_HOOK, "tu_isolate_translator_threads();", False)


class MesaPatcher:
    def __init__(self, root: pathlib.Path, dry_run=False, make_backup=True, diff=False):
        self.root = root
        self.dry_run = dry_run
        self.make_backup = make_backup
        self.diff = diff
        self.origs = {}
        self.report = {
            "root": str(root), "mesa_version": None, "abi": ABI,
            "steps": [], "warnings": 0, "failures": 0,
        }

    # ---- io ----

    def _read(self, rel: str):
        p = self.root / rel
        if not p.is_file():
            return None, p
        try:
            return p.read_text(encoding="utf-8", errors="surrogateescape"), p
        except OSError as e:
            die(f"не могу прочитать {p}: {e}")

    def _skip(self, rel: str, label: str) -> None:
        self.report["steps"].append({"step": label, "file": rel, "status": "skipped"})
        info(f"{rel}: уже пропатчен — пропуск")

    def _commit(self, rel: str, p: pathlib.Path, new_src: str, meta: dict) -> bool:
        orig = self.origs.get(rel)
        problems = verify_c_text(new_src, meta.get("expect", ()), orig)
        entry = {"step": meta.get("step", "?"), "file": rel,
                 "function": meta.get("function"), "line": meta.get("line")}

        if problems:
            entry["status"] = "fail"
            entry["problems"] = problems
            self.report["steps"].append(entry)
            self.report["failures"] += 1
            warn(f"{rel}: верификация НЕ пройдена, файл не записан: {'; '.join(problems)}")
            return False

        if self.diff and orig is not None:
            sys.stdout.writelines(difflib.unified_diff(
                orig.splitlines(keepends=True), new_src.splitlines(keepends=True),
                fromfile=rel + " (orig)", tofile=rel + " (patched)"))

        if self.dry_run:
            entry["status"] = "applied (dry-run)"
            self.report["steps"].append(entry)
            info(f"[dry-run] будет записан: {p}")
            return True

        try:
            if self.make_backup and p.exists():
                bak = p.with_name(p.name + BACKUP_SUFFIX)
                if not bak.exists():
                    shutil.copy2(p, bak)
            tmp = p.with_name(p.name + TMP_SUFFIX)
            with tmp.open("w", encoding="utf-8", errors="surrogateescape", newline="\n") as f:
                f.write(new_src)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except OSError as e:
            entry["status"] = "fail"
            entry["problems"] = [f"io: {e}"]
            self.report["steps"].append(entry)
            self.report["failures"] += 1
            err(f"не могу записать {p}: {e}")
            return False

        entry["status"] = "applied"
        self.report["steps"].append(entry)
        info(f"{rel}: {meta.get('note') or 'записан'}")
        return True

    def _soft_warn(self, label: str, msg: str) -> None:
        self.report["steps"].append({"step": label, "status": "warn"})
        self.report["warnings"] += 1
        warn(msg)

    # ---- шаги ----

    def step_header(self) -> None:
        src, p = self._read(REL_HEADER)
        note = "создание"
        if src is not None:
            m = re.search(r"TU_BALANCER_ABI\s+(\d+)", src)
            if m and int(m.group(1)) >= ABI:
                self._skip(REL_HEADER, "header")
                return
            note = f"обновление до версии {ABI}"
            self.origs[REL_HEADER] = src
        self._commit(REL_HEADER, p, HEADER_CODE,
                     {"step": "header", "note": note, "expect": ("TU_BALANCER_ABI",)})

    def step_device(self) -> None:
        src, p = self._read(REL_DEVICE)
        if src is None:
            die(f"{REL_DEVICE} не найден — путь не похож на дерево Mesa с freedreno")
        if INCLUDE_MARK in src:
            self._skip(REL_DEVICE, "device-init")
            return

        m, b = find_fn_def(src, "tu_CreateDevice")
        if m is None:
            die("tu_CreateDevice не найден в tu_device.cc — нестандартная версия Mesa?")
        line = src[: m.start()].count("\n") + 1

        new_src = src[: b + 1] + DEVICE_INIT + src[b + 1:]
        new_src = insert_before_first_include(new_src, DEVICE_BLOCK)
        self.origs[REL_DEVICE] = src
        ok = self._commit(REL_DEVICE, p, new_src, {
            "step": "device-init", "function": "tu_CreateDevice", "line": line,
            "note": "tu_balancer_init() в tu_CreateDevice + экспорт hook'а",
            "expect": (INCLUDE_MARK, "tu_balancer_init();", "tu_balancer_worker_hook")})
        if not ok:
            die(f"{REL_DEVICE}: верификация не прошла — патч прерван")

    def _patch_hook_step(self, spec: HookSpec) -> None:
        for rel, names in spec.targets:
            src, p = self._read(rel)
            if src is None:
                dbg(f"{rel}: файла нет — пробую следующий вариант")
                continue
            if INCLUDE_MARK in src:
                self._skip(rel, spec.label)
                return
            found = None
            for name in names:
                m, b = find_fn_def(src, name)
                if m is not None:
                    found = (name, m, b)
                    break
            if found is None:
                dbg(f"{rel}: не найдено ни одного из {names}")
                continue

            name, m, b = found
            line = src[: m.start()].count("\n") + 1
            new_src = src[: b + 1] + spec.hook_code + src[b + 1:]
            new_src = insert_before_first_include(new_src, INCLUDE_MARK + "\n")
            self.origs[rel] = src
            ok = self._commit(rel, p, new_src, {
                "step": spec.label, "function": name, "line": line,
                "note": f"хук в {name}()",
                "expect": (INCLUDE_MARK, spec.expect)})
            if not ok and spec.required:
                die(f"{rel}: верификация не прошла — патч прерван")
            return

        msg = (f"{spec.label}: не найдено ни одного target'а: "
               f"{[t[0] for t in spec.targets]}")
        if spec.required:
            die(msg)
        self._soft_warn(spec.label, msg)

    def step_uqueue(self) -> None:
        label = "u_queue-workers"
        src, p = self._read(REL_UQUEUE)
        if src is None:
            self._soft_warn(label, f"{REL_UQUEUE} не найден — хук воркеров не применяется")
            return
        if "tu_balancer_worker_hook" in src:
            self._skip(REL_UQUEUE, label)
            return

        m, _b = find_fn_def(src, "util_queue_thread_func")
        if m is None:
            self._soft_warn(label, "util_queue_thread_func не найден в u_queue.c — хук пропущен")
            return
        je = re.search(r"^([ \t]*)job\.execute\s*\(", src, re.M)
        if je is None:
            self._soft_warn(label, "job.execute(...) не найден в u_queue.c — хук пропущен")
            return

        # 1) вызов хука перед исполнением задачи (позиция ниже — вставляем первой)
        call = UQUEUE_CALL.format(i=je.group(1))
        new_src = src[: je.start()] + call + src[je.start():]

        # 2) weak-декларация СТРОГО перед строкой определения функции:
        #    вставка «перед именем» попадала внутрь сигнатуры и ломала C.
        line_start = src.rfind("\n", 0, m.start()) + 1
        new_src = new_src[:line_start] + UQUEUE_DECL + new_src[line_start:]
        self.origs[REL_UQUEUE] = src

        self._commit(REL_UQUEUE, p, new_src, {
            "step": label, "function": "util_queue_thread_func",
            "line": src[: m.start()].count("\n") + 1,
            "note": "weak-хук воркеров компиляции шейдеров",
            "expect": ("__attribute__((weak)) void tu_balancer_worker_hook",
                       "if (tu_balancer_worker_hook)")})

    # ---- откат ----

    def restore(self) -> None:
        n = 0
        for rel in TARGET_FILES:
            p = self.root / rel
            bak = p.with_name(p.name + BACKUP_SUFFIX)
            if bak.is_file():
                if self.dry_run:
                    info(f"[dry-run] будет восстановлен {p}")
                else:
                    shutil.copy2(bak, p)
                    bak.unlink()
                    info(f"восстановлен: {p}")
                n += 1
        if n == 0:
            info("бэкапы *.tubak не найдены — откатывать нечего")

# ------------------------------------------------------------ self-test -----

FAKE_DEVICE = """#include <stdbool.h>
#include "tu_device.h"

VkResult tu_CreateDevice(VkPhysicalDevice phys, const VkDeviceCreateInfo *ci, VkDevice *pdev)
{
   struct tu_device *dev = (struct tu_device *)*pdev;
   (void)dev;
   return VK_SUCCESS;
}
"""

FAKE_KGSL = """#include "tu_knl.h"

static VkResult kgsl_queue_submit(struct tu_queue *queue, struct vk_queue_submit *vk_submit)
{
   (void)vk_submit;
   return VK_SUCCESS;
}
"""

FAKE_SHADER = """#include "tu_shader.h"

VkResult tu_CreateShaderModule(VkDevice dev, const VkShaderModuleCreateInfo *ci, VkShaderModule *mod)
{
   (void)ci;
   (void)mod;
   return VK_SUCCESS;
}
"""

FAKE_UQUEUE = """#include "u_queue.h"

static THREAD_RET
util_queue_thread_func(void *input)
{
   struct util_queue *queue = input;
   for (;;) {
      struct util_queue_job job = get_job(queue);
      if (job.job) {
         job.execute(job.job, job.gdata, 0);
      }
   }
   return 0;
}
"""


def run_self_test(verbose: bool) -> bool:
    """Прогоняет весь патчер на синтетическом мини-дереве во временном каталоге.
    Проверяет оба исторических бага (decl внутри сигнатуры; NameError на
    повторном запуске) и идемпотентность."""
    global _VERBOSE
    _VERBOSE = verbose
    ok = True
    with tempfile.TemporaryDirectory(prefix="tubal-selftest-") as td:
        root = pathlib.Path(td)
        (root / "src/freedreno/vulkan").mkdir(parents=True)
        (root / "src/util").mkdir(parents=True)
        (root / REL_DEVICE).write_text(FAKE_DEVICE, encoding="utf-8")
        (root / REL_KGSL).write_text(FAKE_KGSL, encoding="utf-8")
        (root / REL_SHADER).write_text(FAKE_SHADER, encoding="utf-8")
        (root / REL_UQUEUE).write_text(FAKE_UQUEUE, encoding="utf-8")

        p = MesaPatcher(root, dry_run=False, make_backup=False, diff=False)
        try:
            p.step_header()
            p.step_device()
            p._patch_hook_step(SUBMIT_SPEC)
            p._patch_hook_step(SHADER_SPEC)
            p.step_uqueue()
        except SystemExit as e:
            err(f"self-test: неожиданный die() (код {e.code})")
            return False

        read = lambda rel: (root / rel).read_text(encoding="utf-8")
        checks = [
            ("header создан (ABI)", (root / REL_HEADER).is_file()
             and "TU_BALANCER_ABI 7" in read(REL_HEADER)),
            ("device: include+init+hook", INCLUDE_MARK in read(REL_DEVICE)
             and "tu_balancer_init();" in read(REL_DEVICE)
             and "tu_balancer_worker_hook" in read(REL_DEVICE)),
            ("kgsl: tick_render", "tu_balancer_tick_render();" in read(REL_KGSL)),
            ("shader: isolate", "tu_isolate_translator_threads();" in read(REL_SHADER)),
            ("u_queue: weak-decl", "__attribute__((weak)) void tu_balancer_worker_hook"
             in read(REL_UQUEUE)),
            ("u_queue: вызов hook'а", "if (tu_balancer_worker_hook)" in read(REL_UQUEUE)),
            ("u_queue: decl НЕ внутри сигнатуры",
             re.search(r"static THREAD_RET\s*\nutil_queue_thread_func", read(REL_UQUEUE)) is not None),
            ("верификация без сбоев", p.report["failures"] == 0),
        ]
        for name, res in checks:
            info(f"  [{'OK' if res else 'FAIL'}] {name}")
            ok = ok and res

        # идемпотентность: второй прогон — только skipped, без исключений
        p2 = MesaPatcher(root, dry_run=False, make_backup=False, diff=False)
        try:
            p2.step_header()
            p2.step_device()
            p2._patch_hook_step(SUBMIT_SPEC)
            p2._patch_hook_step(SHADER_SPEC)
            p2.step_uqueue()
            idem = all(s.get("status") == "skipped" for s in p2.report["steps"])
            info(f"  [{'OK' if idem else 'FAIL'}] повторный запуск — ничего не задублировано")
            ok = ok and idem
        except SystemExit as e:
            err(f"self-test: второй прогон упал (код {e.code}) — регрессия!")
            ok = False

    info("self-test: " + ("PASS" if ok else "FAIL"))
    return ok

# ---------------------------------------------------------------- main ------

GLYPH = {"applied": "+", "applied (dry-run)": "*", "skipped": "=",
         "warn": "!", "fail": "X"}


def print_summary(report: dict) -> None:
    info("-" * 64)
    for st in report["steps"]:
        status = st.get("status", "?")
        g = GLYPH.get(status, "?")
        loc = f"  [{st['function']}:{st.get('line')}]" if st.get("function") else ""
        info(f" {g} {status:<17} {st.get('file') or st.get('step', '?')}{loc}")
    info("-" * 64)
    applied = sum(1 for s in report["steps"] if s.get("status", "").startswith("applied"))
    skipped = sum(1 for s in report["steps"] if s.get("status") == "skipped")
    info(f"итог: applied={applied}, skipped={skipped}, "
         f"warnings={report['warnings']}, failures={report['failures']}, "
         f"elapsed={report.get('elapsed_s')}s")


def main() -> None:
    global _VERBOSE, _QUIET
    ap = argparse.ArgumentParser(
        description="Патчер Mesa/Turnip: CPU/термал-балансер Unleashed V7 (Y700 Gen 3)")
    ap.add_argument("root", nargs="?", default=None,
                    help="корень исходников Mesa (иначе авто-поиск вверх от cwd)")
    ap.add_argument("--dry-run", action="store_true", help="не писать файлы")
    ap.add_argument("--diff", action="store_true", help="показать unified diff")
    ap.add_argument("--no-backup", action="store_true", help="без бэкапов *.tubak")
    ap.add_argument("--restore", action="store_true", help="откат из бэкапов *.tubak")
    ap.add_argument("--json", metavar="PATH", help="сохранить JSON-отчёт")
    ap.add_argument("--build", metavar="BUILD_DIR", help="запустить ninja -C BUILD_DIR после патча")
    ap.add_argument("--self-test", action="store_true",
                    help="проверить патчер на синтетическом дереве (Mesa не нужна)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()
    _VERBOSE, _QUIET = args.verbose, args.quiet

    if args.self_test:
        sys.exit(0 if run_self_test(args.verbose) else 3)

    root = find_mesa_root(args.root)
    if root is None or not (root / REL_DEVICE).is_file():
        die("корень Mesa не найден (нет src/freedreno/vulkan/tu_device.cc); "
            "укажите путь явно: y700_gen3_balancer.py /путь/к/mesa")

    patcher = MesaPatcher(root, dry_run=args.dry_run,
                          make_backup=not args.no_backup, diff=args.diff)
    patcher.report["mesa_version"] = read_mesa_version(root)

    ver = patcher.report["mesa_version"]
    info(f"корень Mesa: {root}" + (f" (mesa {ver})" if ver else ""))

    t0 = time.monotonic()
    patcher.step_header()
    patcher.step_device()
    patcher._patch_hook_step(SUBMIT_SPEC)
    patcher._patch_hook_step(SHADER_SPEC)
    patcher.step_uqueue()
    patcher.report["elapsed_s"] = round(time.monotonic() - t0, 3)

    if args.build and not args.dry_run and patcher.report["failures"] == 0:
        info(f"сборка: ninja -C {args.build}")
        patcher.report["build_rc"] = subprocess.call(
            ["ninja", "-C", args.build], cwd=str(root))

    if args.json:
        try:
            pathlib.Path(args.json).write_text(
                json.dumps(patcher.report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            info(f"json-отчёт: {args.json}")
        except OSError as e:
            warn(f"не могу записать json-отчёт: {e}")

    print_summary(patcher.report)
    if patcher.report["failures"]:
        sys.exit(1)
    if patcher.report["warnings"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
