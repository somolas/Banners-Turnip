#!/bin/bash
# =============================================================================
#  build-turnip-perf.sh — Turnip / Mesa Vulkan builder
#  Target: Lenovo Legion Y700 Gen 3 (TB321FU)
#  SoC: Snapdragon 8 Gen 3 (SM8650-AB) | CPU: Cortex-X4 (ARMv9.2) | GPU: Adreno 750
#
#  Переменные окружения:
#    MESA_REF=26.0|main        ветка/тег Mesa (нет такой — авто-фолбэк на main)
#                              main — свежайшие фиксы (нужно для проверки на 8 Gen 5 / Adreno 840)
#    SDK_VER=34                platform-sdk-version
#    JOBS=8                    потоков (по умолчанию nproc)
#    ENABLE_LTO=0              ThinLTO: 0 = выкл (стабильность), 1 = вкл (после проверки на вылеты)
#    BUILD_VERSION=2           packageVersion (по умолчанию — таймстамп сборки)
#    SKIP_SOURCE_DOWNLOAD=1    пересборка без перекачки исходников
# =============================================================================
set -Eeuo pipefail

green='\033[0;32m'; cyan='\033[0;36m'; yellow='\033[1;33m'; red='\033[0;31m'; nocolor='\033[0m'
trap 'echo -e "${red}>>> СБОЙ: строка ${LINENO} — ${BASH_COMMAND}${nocolor}"' ERR

# -------------------------------- конфиг ------------------------------------
MESA_REPO="https://gitlab.freedesktop.org/mesa/mesa.git"
MESA_REF="${MESA_REF:-mesa-26.0.0}"
SDK_VER="${SDK_VER:-34}"
BUILD_VERSION="${BUILD_VERSION:-$(date +%Y%m%d%H%M)}"
JOBS="${JOBS:-$(nproc)}"
ENABLE_LTO="${ENABLE_LTO:-1}"
SKIP_SOURCE_DOWNLOAD="${SKIP_SOURCE_DOWNLOAD:-0}"

workdir="$(pwd)/turnip_workdir"
srcfolder="mesa"
builddir="$workdir/build-android-aarch64"

DEVICE_NAME="Lenovo Legion Y700 Gen 3 (TB321FU)"
TARGET_SOC="Qualcomm Snapdragon 8 Gen 3 (SM8650-AB)"
TARGET_CPU="cortex-x4"
TARGET_CPU_TOPO="1x X4 + 5x A720 + 2x A520 (ARMv9.2)"
TARGET_GPU="Adreno 750 (KGSL)"

WARN_FLAGS="-Wno-error -Wno-deprecated-declarations -Wno-incompatible-pointer-types-discards-qualifiers -Wno-incompatible-pointer-types"
CPU_FLAGS="-march=armv8.2-a+crypto+p16+dotprod+i8mm -mtune=cortex-x4"
ndk=""; sdkver="$SDK_VER"; githash=""

android_from_api(){ case "$1" in 33) echo 13;; 34) echo 14;; 35) echo 15;; 36) echo 16;; *) echo "?";; esac; }

print_system_telemetry(){
    local android; android="$(android_from_api "$sdkver")"
    echo -e "${cyan}================================================================${nocolor}"
    echo -e "${green}>>> ЦЕЛЕВОЕ УСТРОЙСТВО: ${DEVICE_NAME}${nocolor}"
    echo -e "${green}>>> SoC:                ${TARGET_SOC}${nocolor}"
    echo -e "${green}>>> CPU:                ${TARGET_CPU} ${TARGET_CPU_TOPO}${nocolor}"
    echo -e "${green}>>> GPU:                ${TARGET_GPU}${nocolor}"
    echo -e "${green}>>> Mesa ref: ${MESA_REF} | API ${sdkver} (Android ${android}) | jobs: ${JOBS} | LTO: ${ENABLE_LTO}${nocolor}"
    echo -e "${green}>>> packageVersion:     ${BUILD_VERSION}${nocolor}"
    echo -e "${cyan}----------------------------------------------------------------${nocolor}"
    if command -v lscpu >/dev/null 2>&1; then
        lscpu | grep -E "Model name|^CPU\(s\)|Thread|Core|MHz" || true
    else
        echo "CPU Cores: $(nproc)"
    fi
    free -h | grep "Mem:" || true
    echo -e "${cyan}================================================================${nocolor}"
}

ensure_python_mako(){
    if python3 -c 'import mako' 2>/dev/null && python3 -c 'import yaml' 2>/dev/null; then
        echo -e "${green} - python mako/yaml found${nocolor}"
        return 0
    fi
    python3 -m pip install --quiet --user mako pyyaml 2>/dev/null \
        || pip3 install --quiet --user mako pyyaml 2>/dev/null \
        || echo -e "${yellow}WARN: mako/pyyaml не поставились — поставь python3-mako python3-yaml из пакетов${nocolor}"
}

check_deps(){
    echo -e "${yellow}Checking system for required Dependencies ...${nocolor}"
    local missing=0 d
    for d in git meson ninja flex bison zip pkg-config python3; do
        if command -v "$d" >/dev/null 2>&1; then
            echo -e "${green} - $d found${nocolor}"
        else
            echo -e "${red} - $d NOT found, can't continue.${nocolor}"; missing=1
        fi
    done
    for d in patchelf unzip curl glslang glslangValidator; do
        command -v "$d" >/dev/null 2>&1 || echo -e "${yellow} - $d не найден (не критично)${nocolor}"
    done
    [ "$missing" -eq 0 ] || { echo -e "${red}Установи недостающие пакеты!${nocolor}"; exit 1; }

    # свежий Mesa требует meson >= 1.3
    local mver mmaj mrest mmin
    mver="$(meson --version 2>/dev/null || echo 0)"
    mmaj="${mver%%.*}"; mrest="${mver#*.}"; mmin="${mrest%%.*}"
    if [ "$mmaj" -lt 1 ] || { [ "$mmaj" -eq 1 ] && [ "$mmin" -lt 3 ]; }; then
        echo -e "${yellow}WARN: meson ${mver} — свежему Mesa нужен >= 1.3: python3 -m pip install --user -U meson${nocolor}"
    fi
    ensure_python_mako
}

find_ndk(){
    if [ -n "${ANDROID_NDK_HOME:-}" ]; then
        local b="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin"
        [ -d "$b" ] && { echo "$b"; return 0; }
    fi
    local hit root
    for root in "$HOME/Android/Sdk/ndk" /opt /usr/local /usr/lib /usr; do
        hit="$(find "$root" -maxdepth 10 -type f -name "aarch64-linux-android${SDK_VER}-clang" -print -quit 2>/dev/null || true)"
        [ -n "$hit" ] && { dirname "$hit"; return 0; }
    done
    # запасной поиск — любой доступный API в этом NDK (старый NDK больше не стопор)
    for root in "$HOME/Android/Sdk/ndk" /opt /usr/local /usr/lib /usr; do
        hit="$(find "$root" -maxdepth 10 -type f -name 'aarch64-linux-android*-clang' -print -quit 2>/dev/null || true)"
        [ -n "$hit" ] && { dirname "$hit"; return 0; }
    done
    return 1
}

pick_cpu_flags(){
    if ! echo 'int main(){return 0;}' | "$1" $CPU_FLAGS -x c -c - -o /dev/null 2>/dev/null; then
        echo -e "${yellow}>>> clang не знает ${CPU_FLAGS} — откат на armv8.2-a+crypto+fp16+dotprod+i8mm${nocolor}"
        CPU_FLAGS="-march=armv8.2-a+crypto+fp16+dotprod+i8mm"
    fi
}

prepare_workdir(){
    mkdir -p "$workdir"; cd "$workdir"

    if [ -d "$srcfolder/.git" ]; then
        local have_ref; have_ref="$(cat "$srcfolder/.mesa_ref" 2>/dev/null || echo 'unknown')"
        if [ "$have_ref" != "$MESA_REF" ]; then
            if [ "$SKIP_SOURCE_DOWNLOAD" = "1" ]; then
                echo -e "${yellow}WARN: дерево под '$have_ref', MESA_REF='$MESA_REF' — продолжаю как есть${nocolor}"
            else
                echo "Исходники '$have_ref' != '$MESA_REF' — перекачиваю..."
                rm -rf "$srcfolder" "$builddir"
            fi
        fi
    elif [ "$SKIP_SOURCE_DOWNLOAD" = "1" ]; then
        echo -e "${red}SKIP_SOURCE_DOWNLOAD=1, но $srcfolder отсутствует — убери флаг${nocolor}"; exit 1
    fi

    if [ ! -d "$srcfolder/.git" ]; then
        echo "Cloning Mesa ($MESA_REF)..."
        if ! git clone --depth=1 -b "$MESA_REF" "$MESA_REPO" "$srcfolder"; then
            echo -e "${yellow}ref '$MESA_REF' не найден — авто-фолбэк на main${nocolor}"
            rm -rf "$srcfolder" "$builddir"
            git clone --depth=1 "$MESA_REPO" "$srcfolder"
            MESA_REF="main"
        fi
    fi

    git -C "$srcfolder" checkout -- . || true
    git -C "$srcfolder" clean -fd || true
    echo "$MESA_REF" > "$srcfolder/.mesa_ref"
}

patch_mesa_for_ndk(){
    cd "$srcfolder"
    echo -e "${yellow}Applying NDK compat patches (идемпотентно)...${nocolor}"
    local f="include/android_stub/cutils/native_handle.h"
    if [ -f "$f" ] && grep -q 'typedef const native_handle_t\* buffer_handle_t;' "$f"; then
        sed -i 's/typedef const native_handle_t\* buffer_handle_t;/typedef void\* buffer_handle_t;/g' "$f"
        echo "  ↳ native_handle.h: buffer_handle_t -> void*"
    fi
    f="src/util/u_gralloc_fallback.c"
    if [ -f "$f" ] && grep -q ', hnd->handle' "$f"; then
        sed -i 's/, hnd->handle/, (void \*)hnd->handle/g' "$f"
        echo "  ↳ u_gralloc_fallback.c: const-cast handle"
    fi
    f="src/vulkan/runtime/vk_android.c"
    if [ -f "$f" ] && grep -qE '[a-z_]+->handle->' "$f"; then
        sed -i -E 's/([a-z_]+)->handle->/((const native_handle_t *)\1->handle)->/g' "$f"
        echo "  ↳ vk_android.c: native_handle casts"
    fi
        local tu_dev="src/freedreno/vulkan/tu_device.cc"
    if [ -f "$tu_dev" ] && ! grep -q "inject_custom_dxvk_conf" "$tu_dev"; then
        cat << 'EOF' >> "$tu_dev"

#include <stdlib.h>
#include <unistd.h>
__attribute__((constructor)) static void inject_custom_dxvk_conf(void) {
    if (access("/storage/emulated/0/Winstom/dxvk.conf", F_OK) == 0) {
        setenv("DXVK_CONFIG_FILE", "/storage/emulated/0/Winstom/dxvk.conf", 1);
    } else if (access("/storage/emulated/0/dxvk.conf", F_OK) == 0) {
        setenv("DXVK_CONFIG_FILE", "/storage/emulated/0/dxvk.conf", 1);
    }
}
EOF
        echo "  ↳ tu_device.cc: injected auto-lookup for dxvk.conf"
    fi
           local balancer_py="$(dirname "$workdir")/patches/common/2.y700_gen3_balancer.py"
    echo -e "${yellow}Applying Turnip Unleashed V7 Balancer ($balancer_py)...${nocolor}"
    python3 "$balancer_py" . --no-backup --diff || { rc=$?; [ $rc -eq 2 ] || exit $rc; }
        python3 - << 'EOF'
import re
p = "src/util/u_queue.c"
s = open(p).read()
d = 'extern void tu_balancer_worker_hook(const char *name) __attribute__((weak, visibility("default")));\n'
s = re.sub(r"[^\n]*tu_balancer_worker_hook\s*\(const char \*name\)[^;]*;\s*", "", s)
open(p, "w").write(d + s)

h = "src/freedreno/vulkan/tu_balancer.h"
hs = open(h).read()
hs = hs.replace("void tu_balancer_worker_hook(const char *name)", '__attribute__((visibility("default"), used)) void tu_balancer_worker_hook(const char *name)')
open(h, "w").write(hs)
EOF

    cd ..
}

gen_cross_files(){
    local clang="$ndk/aarch64-linux-android${sdkver}-clang"
    local clangxx="$ndk/aarch64-linux-android${sdkver}-clang++"
    # СТАБИЛЬНЫЙ набор: -fvectorize/-fslp-vectorize и так включены на -O3,
    # -falign-functions=32 — косметика. Выкинуты как лишние точки отказа.
    local c_args=( -D__ANDROID__ $CPU_FLAGS -O3 -fno-plt
                   -fomit-frame-pointer $WARN_FLAGS )
    if [ "$ENABLE_LTO" = "1" ]; then c_args+=(-flto=thin); fi
    local c_str
    c_str="$(printf "'%s', " "${c_args[@]}")"
    c_str="${c_str%, }"

    cat <<EOF > "$workdir/android-aarch64.txt"
[binaries]
ar = '$ndk/llvm-ar'
c = '$clang'
cpp = ['$clangxx', '-fno-exceptions', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '--start-no-unused-arguments', '-static-libstdc++', '--end-no-unused-arguments']
c_ld = '$ndk/ld.lld'
cpp_ld = '$ndk/ld.lld'
strip = '$ndk/llvm-strip'
pkg-config = '/bin/false'

[properties]
c_args = [$c_str]
cpp_args = [$c_str]
# 16KB-страничные прошивки (Android 15/16); на 4KB безвредно
c_link_args = ['-Wl,-z,max-page-size=16384']
cpp_link_args = ['-Wl,-z,max-page-size=16384']

[host_machine]
system = 'android'
cpu_family = 'aarch64'
cpu = '${TARGET_CPU}'
endian = 'little'
EOF

    cat <<EOF > "$workdir/native.txt"
[binaries]
c = '/usr/bin/gcc'
cpp = '/usr/bin/g++'
ar = '/usr/bin/ar'
strip = '/usr/bin/strip'
c_ld = 'bfd'
cpp_ld = 'bfd'
llvm-config = '/usr/bin/llvm-config-19'

[build_machine]
system = 'linux'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'

[properties]
c_args = ['-I/usr/lib/llvm-19/include']
cpp_args = ['-I/usr/lib/llvm-19/include']
c_link_args = ['-fuse-ld=bfd', '-L/usr/lib/llvm-19/lib']
cpp_link_args = ['-fuse-ld=bfd', '-L/usr/lib/llvm-19/lib']


EOF
}

collect_meson_opts(){
    local optfile="$workdir/$srcfolder/meson.options"
    [ -f "$optfile" ] || optfile="$workdir/$srcfolder/meson_options.txt"

    local want=(
        "--prefix=/tmp/turnip-${MESA_REF}"
        "-Dbuildtype=release"
        "-Db_ndebug=true"
        "-Dstrip=true"
        "-Dplatforms=android"
        "-Dvideo-codecs="
        "-Dplatform-sdk-version=${sdkver}"
        "-Dandroid-stub=true"
        "-Dgallium-drivers="
        "-Dvulkan-drivers=freedreno"
        "-Dvulkan-beta=true"
        "-Dfreedreno-kmds=kgsl"
        "-Degl=disabled"
        "-Dllvm=disabled"
        "-Dmesa-clc=system"
        "-Dshared-llvm=disabled"
        "-Dprecomp-compiler=system"
        "-Dandroid-libbacktrace=disabled"
        "-Dperfetto=false"
        "-Dvalgrind=disabled"
        "-Dlibunwind=disabled"
        "-Dlibarchive:crypto=disabled"
        "-Dzstd=disabled"
        "-Dspirv-tools=disabled"
        "-Dbuild-tests=false"
        "-Dtools=[]"
    )
  printf '%s\n' "${want[@]}"

}

configure_and_build(){
    cd "$workdir/$srcfolder"
    githash="$(git rev-parse --short HEAD)"
    unset CC CXX LD AR STRIP CFLAGS CXXFLAGS LDFLAGS CPPFLAGS CXX_LD C_LD
    export LDFLAGS_FOR_BUILD="-fuse-ld=bfd"


    # комьюнити-паттерн 16KB-фикса: дублирует c_link_args, повтор флага безвреден
    export LLVM_CONFIG="/usr/bin/llvm-config-19"
    export LIBRARY_PATH="/usr/lib/llvm-19/lib:${LIBRARY_PATH:-}"
    export CPATH="/usr/lib/llvm-19/include:${CPATH:-}"


    local opts=()
    mapfile -t opts < <(collect_meson_opts)
       if ! command -v mesa_clc >/dev/null 2>&1; then
        env -u CC -u CXX -u LD -u AR -u STRIP -u CFLAGS -u CXXFLAGS -u LDFLAGS \
            meson setup /tmp/clc-host \
            --native-file "$workdir/native.txt" \
            -Dbuildtype=release \
            -Dplatforms= \
            -Dgallium-drivers= \
            -Dvulkan-drivers= \
            -Dglx=disabled \
            -Degl=disabled \
            -Dgbm=disabled \
            -Dllvm=enabled \
            -Dmesa-clc=enabled \
            -Dinstall-mesa-clc=true \
            --prefix=/tmp/clc-install
        ninja -C /tmp/clc-host install
        export PATH="/tmp/clc-install/bin:$PATH"
    fi

    if [ -f "$builddir/build.ninja" ]; then
        meson setup "$builddir" --reconfigure \
            --cross-file "$workdir/android-aarch64.txt" \
            --native-file "$workdir/native.txt" \
            "${opts[@]}"
    else
        rm -rf "$builddir"
        meson setup "$builddir" \
            --cross-file "$workdir/android-aarch64.txt" \
            --native-file "$workdir/native.txt" \
            "${opts[@]}"
    fi

    echo -e "${yellow}>>> Compiling with ${JOBS} thread(s)...${nocolor}"
    ninja -j"${JOBS}" -C "$builddir" install
}

verify_and_pack(){
    cd "$workdir/$srcfolder"
    local prefix="/tmp/turnip-${MESA_REF}"
    local so="$prefix/lib/libvulkan_freedreno.so"
    [ -f "$so" ] || { echo -e "${red}libvulkan_freedreno.so не найден — сборка не удалась${nocolor}"; exit 1; }

    "$ndk/llvm-strip" --strip-unneeded "$so"

    echo -e "${cyan}>>> Проверка результата:${nocolor}"
    ls -lh "$so"
    file "$so" || true
    "$ndk/llvm-readelf" -h "$so" | grep -E "Machine:|Type:" || true

    # FIX(loader): 16KB-выравнивание
    local aligns
    aligns="$("$ndk/llvm-readelf" -lW "$so" 2>/dev/null | awk '$1=="LOAD"{print $NF}' | sort -u)"
    if [ "$aligns" != "0x4000" ]; then
        echo -e "${red}>>> LOAD-сегменты выровнены на ${aligns}, а нужно 0x4000 (16KB)${nocolor}"
        exit 1
    fi
    echo -e "${green}>>> ELF выровнен под 16KB страниц (совместим и с 4KB)${nocolor}"

    # FIX(loader): динамическая libc++ в NEEDED уронит dlopen в лоадере
    local needed
    needed="$("$ndk/llvm-readelf" -d "$so" 2>/dev/null | grep NEEDED || true)"
    echo "$needed"
    if echo "$needed" | grep -Eq 'libc\+\+_shared|libstdc\+\+'; then
        echo -e "${red}>>> В NEEDED есть libc++_shared/libstdc++ — лоадер не загрузит .so!${nocolor}"
        exit 1
    fi

    # FIX(loader): все точки входа ICD, включая legacy для старых лоадеров
    local syms s
    syms="$("$ndk/llvm-nm" -D --defined-only "$so" 2>/dev/null || true)"
    for s in vk_icdGetInstanceProcAddr vk_icdNegotiateLoaderICDInterfaceVersion vkGetInstanceProcAddr; do
        echo "$syms" | grep -qw "$s" || {
            echo -e "${red}>>> Нет экспорта $s — лоадер не сможет пользоваться драйвером${nocolor}";
        }
    done
    for s in vkEnumerateInstanceExtensionProperties vkEnumerateInstanceLayerProperties; do
        echo "$syms" | grep -qw "$s" || \
            echo -e "${yellow}WARN: нет $s — старые лоадеры (interface v0/v1) не заведутся${nocolor}"
    done

    local vkh
    vkh="$(grep -oE 'define VK_HEADER_VERSION [0-9]+' include/vulkan/vulkan_core.h 2>/dev/null | grep -oE '[0-9]+' | head -n1 || true)"
    local outzip="turnip_perf-${MESA_REF}-${githash}.zip"

    (
        cd "$prefix/lib"
        cat <<EOF > meta.json
{
  "schemaVersion": 1,
  "name": "Turnip ${MESA_REF} Y700G3 [SD8G3] ${githash}",
  "description": "Legion Y700 Gen3 | SD 8 Gen 3 (cortex-x4) | Adreno 750 KGSL | Mesa ${MESA_REF} | LTO:${ENABLE_LTO}",
  "author": "Custom",
  "packageVersion": "${BUILD_VERSION}",
  "vendor": "Mesa",
  "driverVersion": "Vulkan 1.4.${vkh:-0}",
  "minApi": 27,
  "libraryName": "libvulkan_freedreno.so"
}
EOF
        python3 -m json.tool meta.json > /dev/null
        rm -f "$workdir/$outzip"
        zip -q -X "$workdir/$outzip" libvulkan_freedreno.so meta.json
    )

    echo -e "${green}>>> Готово: ${workdir}/${outzip}${nocolor}"
    sha256sum "$workdir/$outzip" || true
    python3 -m zipfile -l "$workdir/$outzip" || true
}

run_all(){
    check_deps
    prepare_workdir

    echo -e "${yellow}Searching for Android NDK ...${nocolor}"
    ndk="$(find_ndk)" || {
        echo -e "${red}NDK не найден. Поставь NDK r26+ и задай ANDROID_NDK_HOME${nocolor}"
        exit 1
    }
    echo -e "${green} - NDK toolchain: $ndk${nocolor}"
    "$ndk/clang" --version | head -n1 || true

    if [ ! -x "$ndk/aarch64-linux-android${sdkver}-clang" ]; then
        sdkver="$(ls "$ndk" | grep -oE 'aarch64-linux-android[0-9]+-clang$' | sed -E 's/.*android([0-9]+)-clang$/\1/' | sort -n | tail -n1)"
        echo -e "${yellow} - API ${SDK_VER} в этом NDK нет, использую API ${sdkver}${nocolor}"
    fi

    pick_cpu_flags "$ndk/aarch64-linux-android${sdkver}-clang"
    print_system_telemetry

    echo -e "${cyan}>>> Активные CPU-фичи в компиляторе:${nocolor}"
    "$ndk/aarch64-linux-android${sdkver}-clang" $CPU_FLAGS -dM -E - </dev/null \
        | grep -E '__ARM_FEATURE_(FP16|DOTPROD|MATMUL_INT8|BF16)|__ARM_ARCH' | sort || true

    gen_cross_files
    patch_mesa_for_ndk
    configure_and_build
    verify_and_pack
}

run_all
