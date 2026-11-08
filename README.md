<div align="center">

<img src="docs/assets/icon.png" width="76" height="76" alt="dsh-lanya 图标">

# dsh-lanya · 岚娅

**让 QQ 里的角色，有记忆，也有自己的日常。**

基于 DeepSeek Harness 的 QQ 角色框架 · 人设与记忆 · 表情和语音 · 自主活动 · 本地控制台

[交互演示](https://bamboosir1109.github.io/dsh-lanya/) · [开始安装](docs/INSTALL.md) · [QQ 后端](docs/QQ-BACKENDS.md) · [反馈问题](https://github.com/bamboosir1109/dsh-lanya/issues)

**开发预览 v1.4.0-dev.53** · Windows 10 / 11 · Node.js 22.13+ · Python 3.10+ · [MIT](LICENSE)

</div>

![控制台功能演示，全部为示例数据](docs/assets/showcase-overview.png)

> 上图为交互演示，使用示例数据，不连接 QQ，不代表真实活动或发送结果。最新预览在 [`codex/unified-decision-memory-android`](https://github.com/bamboosir1109/dsh-lanya/tree/codex/unified-decision-memory-android)，默认分支和已发布版本可能不同。商店收录进度见[商店说明](docs/STORE.md)。

## 它是什么

dsh-lanya 是可自己部署的 QQ 角色框架。由同一个主 LLM 结合人设、最近对话和记忆，决定回复、保持安静，或做点感兴趣的事。你可以保留岚娅的风格，也可以使用自己的角色。

它由 **DSH 插件 + Windows 本机配套服务**组成。插件提供角色提示词、调度与控制台入口；QQ 连接、记忆、表情、B 站、语音等需要各自的依赖和本机配置。**在商店安装插件，不等于整套系统已配置完成。**

| 你想做什么 | 框架提供什么 |
|---|---|
| 自然参与聊天 | 群聊、私聊、引用消息与图片，合并近期输入，支持静默和恢复，由主 LLM 判断是否接话 |
| 用图片和声音表达 | 按已有标签选一张真实表情，文字与图片独立发送；本机生成语音后按当前会话权限发送 |
| 记住人和发生过的事 | 按工作区与对象检索，保留时间和来源，维护可修订记忆、压缩记录与日记 |
| 有自己的日常 | 根据兴趣安排活动、浏览 B 站，选择是否评论或分享，遵守预算、休息与发送规则 |
| 看清实际做了什么 | 控制台查看活动证据、视频内容摘录、回复结果、记忆日记与每日费用估值 |
| 自己维护系统 | NapCatQQ / SnowLuma 后端选择、连接检查、单窗口监控、部署保护与离线检查工具 |

自主行为可以选择不执行。设备离线、资料不足和发送结果未知会明确保留，不把工具调用写成“已经完成”。

## 开始使用

需要可用的 DSH、Windows PowerShell 5.1 和上面列出的 Node.js / Python。QQ 后端从 **NapCatQQ 或 SnowLuma 二选一**；语音、B 站和 Android 按需接入。Android 模拟器还需要系统虚拟化条件。

**1. 获取当前预览并部署本机服务**

```powershell
$workspace = Join-Path $env:USERPROFILE 'qq-character-work'
$repo = Join-Path $workspace 'dsh-lanya'
git clone --branch 'codex/unified-decision-memory-android' https://github.com/bamboosir1109/dsh-lanya.git $repo
Set-Location -LiteralPath $repo
.\install.ps1 -Target $workspace -DataDir 'character' -WhatIf
.\install.ps1 -Target $workspace -DataDir 'character'
```

**2. 将同一份源码接入自己的 DSH profile**

```powershell
dsh plugin --profile desktop add ("link:" + $repo)
```

`desktop` 换成你实际使用的 profile。再按[安装手册](docs/INSTALL.md#单独安装-dsh-插件)设置 `workDir`、选择一种调度方式，并完成依赖、身份和 QQ 连接配置。模型和凭据沿用自己的 DSH 配置，本仓库不附带账号。

**3. 按手册启动，检查连接与收件**

控制台默认在 `http://127.0.0.1:19388/lanya-panel.html`。先查看「QQ 接入」与「部件与插件」，再用自己的客户端核对消息。安装器只复制文件；拉取源码、部署本机服务和加载 DSH 插件需要分别完成。

升级前阅读[维护手册](docs/MAINTENANCE.md)，不要用公开模板覆盖自己的身份、人设或运行配置。刷新页面不会升级常驻进程。

## 先逛一逛

[打开交互演示](https://bamboosir1109.github.io/dsh-lanya/)：无需登录，可切换活动、记忆和费用视图，查看功能与安装步骤。演示不调用本机接口，不包含私人对话、账号、日记或凭据。

![自主活动与可读证据演示，全部为示例数据](docs/assets/showcase-activity.png)

## 当前状态与边界

当前为开发预览，完整部署需要核对外部插件与配套补丁。SnowLuma 的私聊、群聊、引用图片、真实表情图和可播放语音已有实际客户端验收；这不代表全部平台动作、全部机器或版本都已验收。说说、签名和 B 站评论等真实写入按次记录，源码、实际加载与客户端可见分别核对。

费用页面采用本机配置的人民币估值口径，**不是服务商实际账单**。自主额度和休息时间可由使用者设置，公开默认值不是现有配置的替代品。记忆与日记保存在本机，首次部署不会带入作者的私人数据。

更多边界见[已知限制](docs/LIMITATIONS.md)和[功能核查](docs/FUNCTIONAL-AUDIT.md)。

## 文档

| 入口 | 内容 |
|---|---|
| [安装](docs/INSTALL.md) / [QQ 后端](docs/QQ-BACKENDS.md) | 环境、目录映射、首次启动与接入 |
| [主 LLM 决策](docs/DECISIONS.md) / [交互可靠性](docs/INTERACTION-RELIABILITY.md) | 接话、静默、并发队列和发送边界 |
| [记忆](docs/MEMORY.md) / [语音](docs/VOICE.md) / [Android](docs/ANDROID.md) | 各功能的配置与验收 |
| [架构](docs/ARCHITECTURE.md) / [维护](docs/MAINTENANCE.md) | 部件关系、诊断、更新与恢复 |
| [商店说明](docs/STORE.md) / [更新日志](CHANGELOG.md) / [开发日志](docs/DEVLOG.md) | 收录方式、版本变化与验证记录 |
| [路线图](docs/ROADMAP.md) | 后续改进与验收目标 |

## 参与与反馈

欢迎通过 [Issues](https://github.com/bamboosir1109/dsh-lanya/issues) 提交版本、QQ 后端、场景、时间和脱敏复现步骤。请移除账号、token、对话原文与本机私有路径。

使用 [MIT](LICENSE) 许可。DSH、QQ 接入程序和其他插件由各自上游维护，需另行安装并遵循各自许可。

**English:** A Windows QQ character framework for DeepSeek Harness, with persona injection, scoped memory, autonomous activities and a local dashboard. NapCatQQ or SnowLuma and companion services must be configured separately. The current branch is a development preview; the public demo contains synthetic data only.
