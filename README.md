<a id="readme-top"></a>

<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/readme-hero-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/readme-hero-light.svg">
  <img src="docs/assets/readme-hero-light.svg" width="1120" alt="dsh-lanya · 岚娅 — 有记忆，也有自己的日常。DSH QQ 角色框架：自然会话、可塑记忆与自主活动。">
</picture>

# dsh-lanya

**在 QQ 里认真接话，也会安静待着。**

一个可自己部署的 QQ 角色框架。<br>
由同一个主 LLM，结合人设、对话和记忆，决定回应或下一件想做的事。

[![Development preview](docs/assets/badge-preview.svg)](CHANGELOG.md)
[![DeepSeek Harness plugin](docs/assets/badge-dsh.svg)](https://github.com/deepseek-ai/deepseek-harness)
[![Windows 10 and 11](docs/assets/badge-windows.svg)](docs/INSTALL.md)
[![MIT License](docs/assets/badge-license.svg)](LICENSE)

**[体验交互演示 ↗](https://bamboosir1109.github.io/dsh-lanya/)** &nbsp; · &nbsp; **[开始部署](#quick-start)** &nbsp; · &nbsp; **[阅读文档](#documentation)** &nbsp; · &nbsp; **[反馈问题](https://github.com/bamboosir1109/dsh-lanya/issues)**

<sub>v1.4.0-dev.54 · 人设与记忆 · 群聊与私聊 · 真实表情和语音 · 自主活动 · 本地控制台</sub>

</div>

<br>

<a href="https://bamboosir1109.github.io/dsh-lanya/">
  <img src="docs/assets/showcase-overview.png" width="1120" alt="控制台交互演示：自主活动、近期片段与日记。全部为示例数据，不连接 QQ。">
</a>

<p align="center"><sub>↑ 无需登录，点击体验。画面全部使用示例数据，不代表真实活动或发送结果。</sub></p>

> [!NOTE]
> 当前为 **v1.4.0-dev.54 开发预览**。最新源码在[开发分支](https://github.com/bamboosir1109/dsh-lanya/tree/codex/unified-decision-memory-android)，默认分支与已发布版本可能不同。社区商店[收录申请 #13180](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13180) 等待审核。

<details>
<summary><strong>目录 · 找到你关心的部分</strong></summary>

- [能做什么](#features)
- [界面一览](#gallery)
- [如何组成](#how-it-works)
- [开始部署](#quick-start)
- [常见问题](#faq)
- [文档导航](#documentation)
- [参与项目](#contributing)

</details>

<br>

<a id="features"></a>
## 不只接话，也有自己的日常

可以使用岚娅的风格，也可以换成你自己的角色。把人设、记忆和真实工具接起来，让表达与行动有上下文。

<table>
  <tr>
    <td width="50%" valign="top"><h3>💬 自然地聊</h3>群聊、私聊、引用消息与图片。合并近期输入，由主 LLM 判断接话或安静，支持持续静默与恢复。<br><br><a href="docs/INTERACTION-RELIABILITY.md">了解会话链路 →</a></td>
    <td width="50%" valign="top"><h3>🎭 不只有文字</h3>按已有标签选择真实表情，一轮最多一张，文字与图片独立发送。语音在本机生成，再按当前会话权限发送。<br><br><a href="docs/VOICE.md">了解语音接入 →</a></td>
  </tr>
  <tr>
    <td valign="top"><h3>📓 带着记忆</h3>按工作区与对象检索，保留时间和来源。可修订记忆、压缩历史与日记一起维护，记得住，也能慢慢修正。<br><br><a href="docs/MEMORY.md">了解记忆方案 →</a></td>
    <td valign="top"><h3>🌤 决定下一件事</h3>结合兴趣与近况安排活动，浏览 B 站，选择是否评论或分享。休息、预算、静默与联系间隔继续生效。<br><br><a href="docs/DECISIONS.md">了解自主决策 →</a></td>
  </tr>
  <tr>
    <td valign="top"><h3>🪟 看得见过程</h3>活动证据、视频内容摘录、回复结果、记忆日记与每日费用估值集中查看。缺少资料与未知结果会保留。<br><br><a href="https://bamboosir1109.github.io/dsh-lanya/">体验界面演示 →</a></td>
    <td valign="top"><h3>🔧 自己维护系统</h3>选择 NapCatQQ 或 SnowLuma，检查连接、查看部件、持续监控。身份、人设、记忆与日记保存在本机。<br><br><a href="docs/QQ-BACKENDS.md">了解 QQ 后端 →</a></td>
  </tr>
</table>

<a id="gallery"></a>
## 一扇看见日常的窗口

<table>
  <tr>
    <td width="50%" valign="top"><strong>自主活动</strong><br><sub>看过的内容、做出的决定与结果分开记录。</sub><br><br><a href="docs/assets/showcase-activity.png"><img src="docs/assets/showcase-activity.png" alt="自主活动示例：视频内容摘录、评论决定和分享决定。"></a></td>
    <td width="50%" valign="top"><strong>记忆日记</strong><br><sub>从时间、对象与来源，找回相关的人和事。</sub><br><br><a href="docs/assets/showcase-memory.png"><img src="docs/assets/showcase-memory.png" alt="记忆检索示例：兴趣、交流偏好与待办，均保留示例时间和来源。"></a></td>
  </tr>
</table>

<details>
<summary><strong>再看看每日花销</strong> · 按天查看人民币估值</summary>

<br>

<img src="docs/assets/showcase-money.png" width="1120" alt="费用演示：人民币每日花销柱状图。金额全部为示例估值，不是服务商账单。">

费用依据本机价表与记录估算，**不等于服务商实际扣款**。真实控制台保留待核算轮次与覆盖范围，不将缺少记录显示成零元。

</details>

<p align="center"><sub>以上均为示例数据演示，无真实账号、私人对话或本机 API。实际功能与外观以所装版本为准。</sub></p>

<a id="how-it-works"></a>
## 一套角色，两部分程序

**DSH 插件**提供角色提示词、调度和控制台入口；**Windows 配套服务**连接 QQ、记忆、表情、B 站等能力。外部插件与服务按需配置。

```text
                人设 · 最近对话 · 记忆
                          │
QQ 群 / 私聊 ── QQ 后端 ── 主 LLM ── 工具与自主活动
                          │
                     本地控制台
                活动 · 记忆日记 · 费用
```

在商店安装插件后，仍需完成本机服务和账号配置。自主行为可以选择不执行，设备离线或发送结果未知时不会编造完成状态。

<a id="quick-start"></a>
## 从自己的工作区开始

| 必需环境 | 接入方式 |
|---|---|
| Windows 10 / 11 · PowerShell 5.1 | 本机运行配套服务 |
| Node.js 22.13+ · Python 3.10+ · 可用的 DSH | 插件与服务分别部署 |
| NapCatQQ **或** SnowLuma | 二选一，使用自己的 QQ 账号与凭据 |

语音、B 站与 Android 按需接入；Android 模拟器还需要系统虚拟化条件。[完整环境与安装说明 →](docs/INSTALL.md)

### 01 · 获取预览，部署服务

在 PowerShell 中执行，先预览将复制的文件：

```powershell
$workspace = Join-Path $env:USERPROFILE 'qq-character-work'
$repo = Join-Path $workspace 'dsh-lanya'
git clone --branch 'codex/unified-decision-memory-android' https://github.com/bamboosir1109/dsh-lanya.git $repo
Set-Location -LiteralPath $repo
.\install.ps1 -Target $workspace -DataDir 'character' -WhatIf
.\install.ps1 -Target $workspace -DataDir 'character'
```

### 02 · 接入自己的 DSH

```powershell
dsh plugin --profile desktop add ("link:" + $repo)
```

`desktop` 换成实际使用的 profile。按[插件配置说明](docs/INSTALL.md#单独安装-dsh-插件)设置 `workDir`，选择一种调度方式，再完成依赖、身份与 QQ 连接配置。模型和凭据使用自己的 DSH 配置。

### 03 · 启动，核对真实收件

按安装手册启动，在 `http://127.0.0.1:19388/lanya-panel.html` 打开控制台，查看「QQ 接入」和「部件与插件」，再用自己的 QQ 客户端核对消息。

安装器只复制文件。**拉取源码 → 部署本机服务 → 加载 DSH 插件**需要分别完成，刷新页面不会升级常驻进程。升级前阅读[维护手册](docs/MAINTENANCE.md)，保留自己的身份、人设与运行配置。

<a id="faq"></a>
## 你可能还想知道

<details>
<summary><strong>现在可以直接装好使用吗？</strong></summary>

这是开发预览，需要配置自己的 DSH、QQ 后端、外部插件与本机服务。公开演示可以直接访问，但不会连接 QQ，也不读取本机数据。商店收录、正式发布和完整部署是不同步骤，见[商店说明](docs/STORE.md)。

</details>

<details>
<summary><strong>哪些场景已经实际验证？</strong></summary>

SnowLuma 的私聊、群聊、引用图片、真实表情图和可播放语音已有实际客户端验收。这不代表全部平台动作、机器或版本都通过验收。说说、签名与 B 站评论等真实写入按次记录，源码、实际加载与客户端可见分别核对，见[功能核查](docs/FUNCTIONAL-AUDIT.md)与[已知限制](docs/LIMITATIONS.md)。

</details>

<details>
<summary><strong>她会不停发消息吗？记忆放在哪里？</strong></summary>

主 LLM 可以回复、保持安静或选择自主活动，休息、静默、预算与联系间隔仍会约束执行。自主额度和休息时间由使用者配置，公开默认值不会替代你现有的配置。

人设、身份、记忆和日记保存在本机。首次部署不附带作者的私人数据；对话使用自己配置的模型服务，具体数据处理以所选服务和插件为准。

</details>

<a id="documentation"></a>
## 按需阅读

| 我想…… | 入口 |
|---|---|
| 安装并接入 QQ | [安装与升级](docs/INSTALL.md) · [QQ 后端](docs/QQ-BACKENDS.md) |
| 理解接话、自主与并发 | [主 LLM 决策](docs/DECISIONS.md) · [交互可靠性](docs/INTERACTION-RELIABILITY.md) |
| 调整记忆或接入媒体 | [记忆](docs/MEMORY.md) · [语音](docs/VOICE.md) · [Android](docs/ANDROID.md) |
| 看结构、排故与恢复 | [架构](docs/ARCHITECTURE.md) · [维护](docs/MAINTENANCE.md) · [已知限制](docs/LIMITATIONS.md) |
| 了解进度与下一步 | [更新日志](CHANGELOG.md) · [开发日志](docs/DEVLOG.md) · [路线图](docs/ROADMAP.md) |
| 了解商店申请 | [收录说明](docs/STORE.md) · [目录 PR #13180](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13180) |

<a id="contributing"></a>
## 一起把日常打磨好

欢迎在 [Issues](https://github.com/bamboosir1109/dsh-lanya/issues) 留下复现步骤或改进建议。附上版本、QQ 后端、场景和时间即可，日志请先去掉账号、token、私人对话和本机路径。

项目使用 [MIT](LICENSE) 许可。感谢 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 与 QQ 接入及配套插件的上游贡献者；各程序独立维护，需另行安装并遵循各自许可。

<details>
<summary><strong>English overview</strong></summary>

A self-hosted Windows QQ character framework for DeepSeek Harness. One main LLM combines persona, recent conversation and scoped memory to decide replies or autonomous activities, with a local console for activity, diary and estimated costs. NapCatQQ or SnowLuma and companion services must be configured separately. This branch is a development preview; all public demo screenshots use synthetic data.

</details>

<br>

<p align="center"><strong>有记忆，也有自己的日常。</strong><br><sub>dsh-lanya · MIT · Built with DeepSeek Harness</sub><br><br><a href="#readme-top">回到顶部 ↑</a></p>
