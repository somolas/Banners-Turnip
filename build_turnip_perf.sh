#!/bin/bash 
set -e

# Цвета для информативного вывода
green='\033[0;32m'
cyan='\033[0;36m'
yellow='\033[1;33m'
red='\033[0;31m'
nocolor='\033[0m'

# Обязательные зависимости (ccache убран, так как на GitHub Actions он не предустановлен)
deps="git meson ninja patchelf unzip curl pip flex bison zip glslang glslangValidator pkg-config"

workdir="$(pwd)/turnip_workdir"
ndkver="android-ndk-r27c"
ndk="$(dirname "$(find /usr -name "aarch64-linux-android34-clang" 2>/dev/null | head -n 1)")"
sdkver="34"
mesasrc="https://gitlab.freedesktop.org/mesa/mesa" 
srcfolder="mesa"

# Целевая конфигурация: Lenovo Legion Y700 Gen 3
DEVICE_NAME="Lenovo Legion Y700 Gen 3 (TB321FU)"
TARGET_SOC="Qualcomm Snapdragon 8 Gen 3 (SM8650-AB)"
TARGET_CPU="cortex-x4"
TARGET_ARCH="armv9-a+crypto+fp16+dotprod+i8mm"
TARGET_GPU="Adreno 750 (A750 / KGSL)"

print_system_telemetry(){
	echo -e "${cyan}================================================================${nocolor}"
	echo -e "${green}>>> ЦЕЛЕВОЕ УСТРОЙСТВО: ${DEVICE_NAME}${nocolor}"
	echo -e "${green}>>> ЧИПСЕТ (SoC):       ${TARGET_SOC}${nocolor}"
	echo -e "${green}>>> АРХИТЕКТУРА CPU:    ARM64 / ${TARGET_ARCH}${nocolor}"
	echo -e "${green}>>> ЯДРО ТЮНИНГА:       ${TARGET_CPU} (1x X4 + 5x A720 + 2x A520)${nocolor}"
	echo -e "${green}>>> ВИДЕОУСКОРИТЕЛЬ:    ${TARGET_GPU}${nocolor}"
	echo -e "${green}>>> ANDROID TARGET SDK: ${sdkver} (Android 14 / ZUI 16)${nocolor}"
	echo -e "${cyan}----------------------------------------------------------------${nocolor}"
	echo -e "${yellow}>>> СБОРОЧНЫЙ СЕРВЕР (HOST CPU / RAM):${nocolor}"
	if command -v lscpu >/dev/null 2>&1; then
		lscpu | grep -E "Model name|CPU\(s\):|Thread|Core|MHz" || true
	else
		echo "CPU Cores: $(nproc)"
	fi
	free -h | grep "Mem:" || true
	echo -e "${cyan}================================================================${nocolor}"
}

check_deps(){
	echo -e "${yellow}Checking system for required Dependencies ...${nocolor}"
	local deps_missing=0
	for deps_chk in $deps; do
		if command -v "$deps_chk" >/dev/null 2>&1; then
			echo -e "${green} - $deps_chk found${nocolor}"
		else
			echo -e "${red} - $deps_chk NOT found, can't continue.${nocolor}"
			deps_missing=1
		fi
	done

	if [ "$deps_missing" -eq 1 ]; then
		echo -e "${red}Please install missing dependencies!${nocolor}" && exit 1
	fi

	echo "Ensuring python Mako is available..."
	pip install --quiet mako &> /dev/null || true
}

prepare_workdir(){
	echo -e "${yellow}Preparing work directory...${nocolor}"
	mkdir -p "$workdir" && cd "$workdir"

	if [ "${SKIP_SOURCE_DOWNLOAD:-0}" = "1" ]; then
		echo "Skipping NDK + Mesa download (reusing existing source)..."
		if [ -d "$srcfolder/.git" ]; then
			echo "Resetting Mesa source tree..."
			git -C "$srcfolder" checkout .
			git -C "$srcfolder" clean -fd
		fi
		return
	fi



	if [ ! -d "$srcfolder" ]; then
		echo "Cloning Mesa source (main branch)..."
		git clone "$mesasrc" --depth=1 -b main "$srcfolder"
	fi
}

build_lib_for_android(){
	local branch_name="$1"
	cd "$workdir/$srcfolder"
	echo -e "${cyan}==== Building Mesa on $branch_name branch (${DEVICE_NAME} / ${TARGET_GPU}) ====${nocolor}"

	# Применение обязательных патчей KGSL
	if [ -f "../../patches/common/apply_common.sh" ]; then
		bash ../../patches/common/apply_common.sh . || { echo -e "${red}patches/common did not apply, aborting!${nocolor}"; exit 1; }
	else
		echo -e "${yellow}Warning: patches/common/apply_common.sh not found, skipping...${nocolor}"
	fi

	# Фиксы совместимости для NDK r29
	sed -i 's/typedef const native_handle_t\* buffer_handle_t;/typedef void\* buffer_handle_t;/g' include/android_stub/cutils/native_handle.h || true
	sed -i 's/, hnd->handle/, (void \*)hnd->handle/g' src/util/u_gralloc/u_gralloc_fallback.c || true
	sed -i -E 's/([a-z_]+)->handle->/((const native_handle_t *)\1->handle)->/g' src/vulkan/runtime/vk_android.c || true

	mkdir -p "$workdir/bin"
	ln -sf "$ndk/clang" "$workdir/bin/cc"
	ln -sf "$ndk/clang++" "$workdir/bin/c++"
	export PATH="$workdir/bin:$ndk:$PATH"
	export CC=clang
	export CXX=clang++
	export AR=llvm-ar
	export RANLIB=llvm-ranlib
	export STRIP=llvm-strip
	export OBJDUMP=llvm-objdump
	export OBJCOPY=llvm-objcopy

	# Аппаратные флаги Snapdragon 8 Gen 3 (ARMv9 + Cortex-X4 + SVE2) без рискованного -ffast-math
	ARCH_FLAGS="-march=${TARGET_ARCH} -mtune=${TARGET_CPU} -O3 -fno-plt -flto=full -fvectorize -fslp-vectorize -funroll-loops -falign-functions=32 -fno-semantic-interposition -fomit-frame-pointer -ffunction-sections -fdata-sections"
	
	# Оптимизация линковщика LLD + 16KB page size alignment
	export LDFLAGS="-fuse-ld=lld -flto=full -Wl,-O3,--lto-O3,-Bsymbolic-functions,--gc-sections,--as-needed--as-needed"
	
	COMMON_WARNINGS="-Wno-error -Wno-deprecated-declarations -Wno-incompatible-pointer-types-discards-qualifiers -Wno-incompatible-pointer-types"
	export CFLAGS="-D__ANDROID__ $ARCH_FLAGS $COMMON_WARNINGS"
	export CXXFLAGS="-D__ANDROID__ $ARCH_FLAGS $COMMON_WARNINGS"

	echo -e "${cyan}>>> Проверка активных инструкций CPU в компиляторе NDK:${nocolor}"
	"$ndk/aarch64-linux-android${sdkver}-clang" -march="${TARGET_ARCH}" -mtune="${TARGET_CPU}" -dM -E - < /dev/null | grep -E "ARM_FEATURE_SVE2|ARM_FEATURE_DOTPROD|ARM_FEATURE_MATMUL_INT8|ARM_FEATURE_FP16|ARM_ARCH" || true

	GITHASH=$(git rev-parse --short HEAD)

	echo "Generating cross-compilation files..."
	cat <<EOF >"android-aarch64.txt"
[binaries]
ar = '$ndk/llvm-ar'
c = '$ndk/aarch64-linux-android${sdkver}-clang'
cpp = ['$ndk/aarch64-linux-android${sdkver}-clang++', '-fno-exceptions', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '--start-no-unused-arguments', '-static-libstdc++', '--end-no-unused-arguments']
c_ld = '$ndk/ld.lld'
cpp_ld = '$ndk/ld.lld'
strip = '$ndk/llvm-strip'
pkg-config = ['env', 'PKG_CONFIG_LIBDIR=$ndk/pkg-config', '/usr/bin/pkg-config']

[host_machine]
system = 'android'
cpu_family = 'aarch64'
cpu = '${TARGET_CPU}'
endian = 'little'
EOF

	cat <<EOF >"native.txt"
[build_machine]
c = 'clang'
cpp = 'clang++'
ar = 'llvm-ar'
strip = 'llvm-strip'
c_ld = 'ld.lld'
cpp_ld = 'ld.lld'
system = 'linux'
cpu_family = 'x86_64'
cpu = 'x86_64'
endian = 'little'
EOF

	MESA_CMD="meson setup build-android-aarch64 \
		--cross-file android-aarch64.txt \
		--native-file native.txt \
		--prefix /tmp/turnip-$branch_name \
		-Dbuildtype=release \
		-Db_ndebug=true \
		-Dstrip=true \
		-Dplatforms=android \
		-Dvideo-codecs= \
		-Dplatform-sdk-version=$sdkver \
		-Dandroid-stub=true \
		-Dgallium-drivers= \
		-Dvulkan-drivers=freedreno \
		-Dvulkan-beta=true \
		-Dfreedreno-kmds=kgsl \
		-Degl=disabled \
		-Dandroid-libbacktrace=disabled \
		-Dperfetto=false \
		-Dgpuvis=false \
		-Dvalgrind=disabled \
		-Dlibunwind=disabled \
		-Dshader-cache=enabled"

	if [ -d "build-android-aarch64" ]; then
		$MESA_CMD --reconfigure
	else
		$MESA_CMD
	fi

	echo -e "${yellow}Compiling build files using $(nproc) threads...${nocolor}"
	ninja -j"$(nproc)" -C build-android-aarch64 install

	SO_PATH="/tmp/turnip-$branch_name/lib/libvulkan_freedreno.so"
	if [ ! -f "$SO_PATH" ]; then
		echo -e "${red}Build failed! libvulkan_freedreno.so not found.${nocolor}" && exit 1
	fi

	# Безопасный стриппинг с сохранением таблицы динамических символов Vulkan
	"$ndk/llvm-strip" --strip-unneeded "$SO_PATH"

	echo -e "${cyan}>>> Итоговая проверка скомпилированного драйвера:${nocolor}"
	ls -lh "$SO_PATH"
	file "$SO_PATH" || true
	"$ndk/llvm-readelf" -h "$SO_PATH" | grep -E "Machine:|Type:|Entry" || true

	echo "Making Adreno Tools ZIP archive..."
	cd "/tmp/turnip-$branch_name/lib"

	_zip_name="mesa-turnip-perf-$branch_name.zip"

	cat <<EOF >"meta.json"
{
  "schemaVersion": 1,
  "name": "Turnip Y700 Gen3 ULTIMATE [SD8G3/X4] ${GITHASH}",
  "description": "Device: Legion Y700 Gen 3 | CPU: SD 8 Gen 3 (ARMv9/Cortex-X4/SVE2) | GPU: Adreno 750 | Full LTO + O3",
  "author": "Custom",
  "packageVersion": "1",
  "vendor": "Mesa",
  "driverVersion": "Vulkan 1.4.335",
  "minApi": 27,
  "libraryName": "libvulkan_freedreno.so"
}
EOF
	zip -q "/tmp/${_zip_name}" libvulkan_freedreno.so meta.json
	cd - > /dev/null

	if [ ! -f "/tmp/${_zip_name}" ]; then
		echo -e "${red}Failed to pack the archive!${nocolor}" && exit 1
	else
		cp "/tmp/${_zip_name}" "$workdir/"
		echo -e "${green}Build completed successfully! → ${_zip_name}${nocolor}"
	fi
}

run_all(){
	echo -e "${green}====== Begin building Y700 Gen 3 ULTIMATE-PERF Turnip V${BUILD_VERSION:-1}! ======${nocolor}"
	print_system_telemetry
	check_deps
	prepare_workdir
	build_lib_for_android main
}

run_all
