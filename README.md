# dsh-lanya

当前开发版本：**v1.4.0-dev.42**。群聊根据最新对话和自己刚说过的话判断是否接话，普通短话题抓住一个重点，说清便停，减少附和、复述、总结连发；保留无需反复艾特的自然跟进和必要的详细回答，不设固定句数或强制冷却。表情支持已有标签直接选发、真实收图与主 LLM 审阅。逐项核查和验收边界见[功能检查](docs/FUNCTIONAL-AUDIT.md)。每批更新递增版本，详见 [更新日志](CHANGELOG.md)、[开发日志](docs/DEVLOG.md) 和 [交互可靠性](docs/INTERACTION-RELIABILITY.md)；开发版不表示正式 Release。

Windows 上的 QQ 角色系统：通过 NapCat 接入 QQ，由 DeepSeek Harness（DSH）运行角色提示词、长期记忆和工具，配一个本地控制台查看与管理各部件。

它包含两部分程序：DSH 插件，以及安装到工作区 `napcat/` 的 Python / PowerShell 服务。人设、身份和日记等本机数据独立保存。**拉取仓库、部署脚本、更新 DSH 中已安装的插件，是三个步骤。**

## 从哪里开始

| 文档 | 内容 |
|---|---|
| [安装与升级](docs/INSTALL.md) | 环境、安装映射、本机配置、首次启动、升级保护规则 |
| [系统架构](docs/ARCHITECTURE.md) | QQ 消息链路、进程、目录、端口和配置优先级 |
| [维护与发布](docs/MAINTENANCE.md) | 故障排查、验证、源码与运行副本同步、隐私与发布 |
| [主 LLM 决策](docs/DECISIONS.md) | 统一观察、持久静默、固定许可和部署边界 |
| [记忆维护](docs/MEMORY.md) | 作用域、版本、压缩来源与可恢复变更 |
| [运行能力上下文](docs/CAPABILITIES.md) | 同一主agent中的浏览器、安卓与语音协议和真实状态 |
| [统一语音](docs/VOICE.md) | 本地合成、结果校验和独立QQ投递许可 |
| [真实安卓](docs/ANDROID.md) | 模拟器前提、固定设备、截图与动作校验 |
| [已知限制](docs/LIMITATIONS.md) | 外部依赖、当前边界、尚未验证的能力 |
| [更新日志](CHANGELOG.md) | 版本变化；未发布改动单独列出 |
| [开发日志](docs/DEVLOG.md) | 按日期记录实现、验证、部署和剩余验收，保留早期排查笔记 |

[开发日志中的早期记录](docs/DEVLOG.md#早期排查记录) 和 [QQ 分段补丁记录](docs/PATCH-qq-burst.md) 保留当时的排查过程；执行当前安装或运维时，以以上当前手册和代码为准。

## 能做什么

当前未发布源码默认统一决策：真实 QQ 入站与自主观察交给同一个聊天 agent，LLM 可以安静结束；发送前持久许可拦住旧队列和跨聊旁路。记忆维护有明确范围、版本与恢复记录；手机页展示真实 Android 截图。完整接入需按决策文档协调修补与重启，不能只复制某个脚本。

群消息会合并同一可信范围内尚未开始处理的输入，并按原生实际领取记录绑定当轮权限，减少旧输入逐条积压。静默与过期许可检查继续生效；原生上下文过大仍需单独压缩，具体配置、生效校验与历史保留见[维护手册](docs/MAINTENANCE.md#qq-上下文与原生压缩)。

原生工具提醒、压缩检查点与重新装配的系统上下文不算新的聊天输入，不再使已有回复绑定失效；单独的内部上下文仍不能取得发送许可。控制台预算摘要与完整日报分别读取，活动和 B站共享有界的工具事实缓存。页面验收可运行 `python -B scripts/panel_health.py`，它检查嵌套记录状态，并区分有效空列表、部分结果和来源不可用。

QQ 表情由同一个主 LLM 选择：简单反应可调用 `qq_send_meme({tags:["happy"]})`，一次完成已有标签检索、当轮回复决定与一张真实图片发送；已有许可会复用，非回复决定不会升级。标签不证明图片具体内容，需要核对画面时仍可选真实候选并看图。新收图片先隔离，主 LLM 空闲时审阅并保存描述、标签和关键词，未审照片不进入图库。只有成功回执才算接口接受，自然使用频率与客户端实收分别验收。

同一主 LLM 的运行指南补充自然接话方式：回应具体内容与情绪，减少套话、重复复述和机械反问，按话题决定长度；保留角色语气，不强制称呼、口头禅或撒娇。群聊先辨明说话对象，活动经历须有真实依据。指南在新输入入队前读取，安装后新排队的输入采用新版；不追改已排队或进行中的上下文，长期对话体验仍需实际使用验收。

运行状态也可在单个命令窗口持续查看：双击已安装的 `napcat/monitor.cmd`，每五秒原地刷新，重复打开会退出第二个监控实例。它仅读取状态，关闭窗口不会停止服务；预算检查通过无控制台包装运行，具体安装及现有任务维护见[维护手册](docs/MAINTENANCE.md#持续监控与后台窗口)。

B 站、语音和独立浏览器需要分别安装依赖、配置账号或本机服务。B 站评论和 QQ 消息会真正发出；安装脚本本身只复制文件，不启动这些活动。

活动页按真实轮次展示做了什么和工具证据，B站页展示实际视频内容摘录与回复状态。每日事实回顾保存在本机，可在活动页按日期查看；覆盖范围和未知结果始终标明，不把读取字幕写成完整看完视频。

本项目按所有者确认的人民币口径记录预算估值，原数值直接按元管理，不作汇率换算；每天自主额度默认 5 元。默认价表估算不代表服务商实际扣款，原始成本插件账本另行保留。预算、预留和页面采用同一单位，未知费用不会清零。

当前源码将可信运行能力说明接入同一agent的普通聊天和后台观察，设备离线不编造可用状态。语音先生成本地WAV，再由原QQ许可控制发送；B站直接动作需额外部署审定的scroll 2.2.0主agent补丁，避免旧定时和手动入口另开headless。说明与执行器的离线验证不代表已安装的外部插件已切换。

原生 memento 0.5.23 的写入历史需在可见性补丁之后单独部署：add、replace、remove、consolidate 与 seed 在事实事务内保存完整前后行和恢复批次。恢复先核对当前完整行，冲突拒绝覆盖；代码补丁撤回与记忆数据恢复各用自己的清单，见[记忆维护](docs/MEMORY.md)。历史只能从补丁启用之后开始保留。

```text
QQ 群 / 私聊 → NapCat → dsh-onebot-qq → DSH agent
                                      ↑ 人设、记忆、工具
控制台 ← 面板 API ← 本机数据与后台脚本 ─┘
```

## 快速安装

需要 Windows 10/11、Windows PowerShell 5.1、Node.js 22.13+、Python 3.10+ 和可用的 DSH。NapCat 另行安装，QQ 连接与凭证由使用者在本机配置。

在 PowerShell 中：

```powershell
$workspace = Join-Path $env:USERPROFILE 'qq-character-work'
$repo = Join-Path $workspace 'dsh-lanya'
git clone https://github.com/bamboosir1109/dsh-lanya.git $repo
Set-Location -LiteralPath $repo
.\install.ps1 -Target $workspace -DataDir 'character' -WhatIf
.\install.ps1 -Target $workspace -DataDir 'character'
```

接着完成[安装说明中的本机配置和 DSH 插件安装](docs/INSTALL.md)。首次启动前填好身份、群号和 NapCat 连接，确认使用的 Python 环境。

控制台默认入口为 [http://127.0.0.1:19388/lanya-panel.html](http://127.0.0.1:19388/lanya-panel.html)，API 默认端口为 19389。两个端口由同一份 `lanya-panel-server.py` 分别监听；DSH 自身的 19387 是另一个服务。

## 仓库布局

```text
plugin/lib/index.js     DSH 插件入口
cordis.patch.yml        仅注册本插件的 bundle patch
install.ps1             把源码部署为本机运行布局
server/                 面板服务、快照、表情来源辅助模块
panel/                  控制台、样式、手机页
scripts/                后台脚本、配置读取器、检查工具
prompts/                后台任务提示词
config/project.json     公开默认设置
docs/                   使用说明、示例和历史记录
tests/                  离线回归与交互验证脚本
```

## 维护约定

`install.ps1 -Force` 更新程序文件，已有身份、人设和本地配置始终保留。它不重启常驻进程，也不替换 DSH profile 中已经安装的另一份插件。`auto-update.ps1` 只更新源码和刷新快照，后续部署与重启需要单独完成。

身份配置、QQ 号、token、账号凭证、日记、人物档案及本机日志留在运行目录。提交前运行 `python -B scripts/privacy-scan.py`，再人工检查差异；自动扫描不能证明所有私人内容都已清除。完整检查与发布流程见[维护与发布](docs/MAINTENANCE.md)。

## 许可

[MIT](LICENSE)

后续开发目标与验收见 [统一决策与运行改进路线](docs/ROADMAP.md)。
