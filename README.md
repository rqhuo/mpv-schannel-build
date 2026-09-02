# mpv-schannel-build

用 GitHub Actions 构建 **32 位（i686）libmpv-2.dll / mpv.exe**，
TLS 使用 **Windows Schannel**（系统自带证书库），**完全不链接 OpenSSL**。

产物供易语言 `LoadLibrary("libmpv-2.dll")` 等场景播放 DVD / 蓝光 / 本地 / 网络视频使用。

## 为什么这样构建

本仓库基于 [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake) superbuild。
该 superbuild 的官方运行方式是 **在 Linux 上先交叉编译出自己的 i686-w64-mingw32 GCC 工具链，
再从源码编译全部依赖 + ffmpeg + mpv**（上游自己的 CI 也是这么跑的）。

> 历史教训：v1 版工作流试图在 `windows-latest` + MSYS2 上直接跑 superbuild，
> 与其 bash 启动器 / ExternalProject 生成物不断打架（exec PE 包装器、impl.cmake 补丁、
> build.ninja 手术……），堆了 20 多轮补丁始终没跑通。v2 完全放弃该路线。

## Schannel 改造（`patches/patch_schannel.py`）

对 superbuild 做最小侵入的文本替换，每个替换都带出现次数校验（上游结构变了会立刻报错）：

| 包 | 修改 |
| --- | --- |
| ffmpeg | `--enable-openssl` → `--enable-schannel`；去掉 libssh / libsrt / libaribcaption（三者都依赖 openssl） |
| curl | `CURL_USE_OPENSSL=OFF` + `CURL_USE_SCHANNEL=ON`；去掉 HTTP/3（ngtcp2/nghttp3，仅支持 openssl）和 libssh |
| libarchive | `ENABLE_OPENSSL=OFF` |

改完后 mpv 构建依赖图里没有任何包再引用 openssl，TLS 全部走 Schannel / SSPI
（构建后工作流会用 `objdump` 验证 DLL 导入了 `SECUR32.dll` 且无 OpenSSL 字符串）。

## 使用

1. Actions 页 → `build-libmpv-i686-schannel` → Run workflow（可选 `mpv-release` 稳定版 / `mpv` 开发版）。
2. 全新构建约 1.5–3 小时（含交叉工具链 ~20-40 分钟，之后有 Actions 缓存，重跑会快很多）。
3. 下载 artifact `libmpv-2-i686-schannel`，内含：

```
libmpv-2.dll      32 位 libmpv，LoadLibrary 直接用
libmpv.dll.a      MinGW 导入库
include/mpv/      client.h / render.h 等头文件
mpv.exe           播放器本体（命令行）
mpv.com           控制台转发器
schannel-check.txt  SECUR32 导入 + 无 OpenSSL 的验证输出
```

依赖（ffmpeg、libass、libbluray、libdvdnav 等）全部静态链进 `libmpv-2.dll`，
单个文件即可分发。蓝光 AACS 解密与 DVD CSS 区碟仍需按 mpv 官方说明处理。

## 本地复现

```bash
git clone https://github.com/shinchiro/mpv-winbuild-cmake.git
python3 patches/patch_schannel.py mpv-winbuild
cd mpv-winbuild
cmake --fresh -G Ninja -B build -S . -DTARGET_ARCH=i686-w64-mingw32 \
      -DSINGLE_SOURCE_LOCATION="$PWD/src_packages" -DENABLE_CCACHE=ON
ninja -C build download
ninja -C build gcc          # 交叉工具链，只需一次
ninja -C build mpv-release  # 全部依赖 + ffmpeg + mpv + libmpv
```
