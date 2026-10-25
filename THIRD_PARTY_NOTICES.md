# 上游来源与修改

`bridge/napcat-plugin/`、`bridge/av-host/host.cjs`、`bridge/av-host/host.html`、
`bridge/tests/bridge.test.mjs` 来自 ClaudiaGardner 的
[maibot-qq-voice-call](https://github.com/ClaudiaGardner/maibot-qq-voice-call)，
固定参考提交 `22f30c021cd3170f75af9dff66cc959a07ebda4b`，原许可证 GPL-3.0-only。
保留上游作者标识和 GPL 许可证；本组合项目自 0.2.0 起使用 GPL-3.0-only。

本地改动：Windows 隔离启动脚本与 DLL 路径适配、MaiBot 控制的短租约自动接听白名单、
真实 AVSDK 可用性检测、通话状态自动跟踪、本地 ASR/TTS 对接。
未包含或复制 QQ、NapCat、AVSDK 二进制与登录数据。

上游 Linux 接听参数映射不等于已验证 Windows 通话兼容性。
