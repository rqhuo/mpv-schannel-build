# mpv-schannel-build

构建 **32 位（i686）libmpv-2.dll / mpv.exe**，
TLS 使用 **Windows Schannel**（系统自带证书库），不引用 openssl，TLS 全部走 Schannel / SSPI，
本仓库基于 [shinchiro/mpv-winbuild-cmake](https://github.com/shinchiro/mpv-winbuild-cmake) superbuild。


```
