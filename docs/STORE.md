# 商店收录与项目发现

dsh-lanya 是面向 DeepSeek Harness 的 Windows QQ 角色集成包：连接 **NapCatQQ / SnowLuma、B站内容与互动、GPT-SoVITS 语音、可塑记忆与日记、自主活动和本地控制台**。当前为开发预览，完整使用需配置自己的外部组件、账号与配套服务。

[体验公开演示](https://bamboosir1109.github.io/dsh-lanya/) · [开始部署](INSTALL.md) · [组件接入与扩展](INTEGRATIONS.md) · [源代码](https://github.com/bamboosir1109/dsh-lanya)

## 已确认收录

截至 **2026-11-10**，以下两个独立目录均已在实时索引中返回本项目。目录收录、源码可获取和本机完整运行分别核对，收录不代表正式发布或安全认证。

| 渠道 | 公开入口与证据 | 当前结果 |
| --- | --- | --- |
| awesome-dsh-plugin / dsh-market | [项目详情](https://awesome-dsh-plugin.com/p/bamboosir1109/dsh-lanya/) · [实时索引](https://awesome-dsh-plugin.com/plugins.json) · [收录 PR](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13180) | 已收录，索引含四张示例截图；[双语功能介绍更新](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13992)另待审核。 |
| DSH 1024Store | [商店](https://deepseek1024.com/) · [精确检索 API](https://api.deepseek1024.com/v1/plugins/search?q=dsh-lanya) · [收录 PR](https://github.com/imsai-sh/awesome-deepseek-harness-plugins/pull/900) | 本批通过目录检查并合并；生产检索返回精确仓库及中英文介绍。 |

dsh-market 和使用 awesome-dsh-plugin 同一索引的其他展示站，按同一条目维护，不重复计算为独立审核。1024Store 当前 API 为本项目返回 GitHub 源码安装规格；其贡献说明仍有 npm 安装限定文案，未将接口返回当作商店内安装验收。完整安装条件以本项目[安装手册](INSTALL.md)为准。

## 本批投稿进度

2026-11-10 共向 **九个新渠道**提交申请，另更新一个已有目录条目：七个 PR、两个 GitHub 收录申请和一次网页投稿。PR、申请或排队回执表示渠道收到请求，只有明确合并和索引结果才记为收录。

| 渠道 | 本批入口 | 核查结果 |
| --- | --- | --- |
| DSH 1024Store | [PR #900](https://github.com/imsai-sh/awesome-deepseek-harness-plugins/pull/900) | 已合并，生产检索可见。 |
| SihanTeng 插件目录 | [PR #71](https://github.com/SihanTeng/awesome-deepseek-harness-plugins/pull/71) · [展示站](https://awesome-deepseek-harness-plugins.pages.dev/) | 已提交集成分类条目，待维护者审核。 |
| dsh.pub | [投稿 PR #192](https://github.com/dsh-pub/dsh-pub/pull/192) · [CI 修复 PR #193](https://github.com/dsh-pub/dsh-pub/pull/193) | 完整日志确认 Ubuntu 26.04 与锁定的 Playwright 1.58 不兼容。已提交三个浏览器任务固定 Ubuntu 24.04 的修复，待维护者审核、批准 CI 并在合并后重跑投稿。 |
| 0xsline 双语资源目录 | [PR #828](https://github.com/0xsline/awesome-deepseek-harness/pull/828) | 中英文消息通道分类条目已提交，待审核。 |
| Alex-Yanggg 插件目录 | [PR #187](https://github.com/Alex-Yanggg/awesome-DSH-plugin/pull/187) | 中英文数据及生成文档已提交，生成一致性检查通过，待审核。 |
| deepseekharnessplugins / cccakeee 目录 | [PR #63](https://github.com/cccakeee/awesome-dsh-plugins/pull/63) | 来源证据、通道分类及双语生成文档已提交，严格生成校验通过，待审核。 |
| Harness Registry / DSH Desk | [收录申请 #113](https://github.com/majiayu000/dsh-plugin-registry/issues/113) | 按该站公开 GitHub 通道提交，待审核。 |
| DSH Marketplace | [收录申请 #31](https://github.com/DshMarketPlace/awesome-dsh-plugin/issues/31) · [投稿页](https://dshmarketplace.dev/submit) | 网页仓库预检持续报告 GitHub 限流；已提交公开审核申请，尚未进入实时目录。 |
| DSHX Hub | [商店](https://dshx.io/en/plugins) | 网页确认 “Repository queued for the next operations run.”；已入队，尚未确认发布。 |
| awesome-dsh-plugin 已有条目更新 | [PR #13992](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13992) | 仅更新自己的中英文描述，保留分类、图标和既有地址；原收录继续有效，新描述待审核。 |

目录字段中的 `verified`、风险分类或来源证据属于各站自己的定义。本批没有宣称通过完整业务、安全或新版 DSH 兼容性验收。

### dsh.pub CI 故障跟进

2026-11-10 取得[失败任务](https://github.com/dsh-pub/dsh-pub/actions/runs/42820004800/job/129842815598)完整日志：来源检查、目录生成、静态检查、单测及站点构建通过，浏览器安装阶段明确报告 Playwright 1.58.0 不支持 `ubuntu26.04-x64`。该版本的[上游依赖表](https://github.com/microsoft/playwright/blob/v1.58.0/packages/playwright-core/src/server/registry/nativeDeps.ts)包含 Ubuntu 24.04；`ubuntu-latest` 的镜像升级触发了此次不兼容。

[修复 PR #193](https://github.com/dsh-pub/dsh-pub/pull/193)仅将投稿校验、合并后整合和 Quality 三个浏览器任务固定为 `ubuntu-24.04`，保留依赖版本、权限与全部校验步骤。YAML 语义和格式核对通过；上游 Quality 工作流等待维护者批准，尚未完成 Linux E2E。投稿校验使用可信 `main`，需维护者合并修复后重跑投稿，当前账号没有该仓库的写入或重跑权限。原失败记录保留，投稿仍待审核。

## 其他发现渠道与限制

| 渠道 | 已核对情况 | 处理 |
| --- | --- | --- |
| [DSH Index](https://dshindex.com/plugins/dsh-lanya) | 已有独立项目详情，归入通信分类，链接到本仓库。 | 保留自动发现，不重复投稿；能力与安装以本仓库为准。 |
| [dshmk 记忆标签](https://dshmk.com/topics/memory) | 搜索快照包含本项目，直接页面此次未检出同一条目。 | 记录发现，暂不计入已确认实时收录。 |
| [DeepSeek Harness Hub](https://dpharness.com/plugin/bamboosir1109/dsh-lanya) | 找到项目索引线索，详情请求超时。 | 保留入口，不把缓存或超时记为本次收录成功。 |
| [dshplugin.io](https://www.dshplugin.io/zh) | 其公开投稿指向 tjsdyy/dshplugin 的 GitHub issue；提交返回 HTTP 403。 | 未创建申请，记录渠道受限，未重复发送。 |
| [bruc3van 插件精选区](https://github.com/bruc3van/awesome-dsh-plugin) | 自荐精选要求超过 10 Star，项目当前未达到。 | 不向不符合条件的精选区投稿。 |
| [DSHOpen](https://dshopen.org/zh/) | 公开投稿说明要求已发布 npm 包，本项目尚未发布。 | 暂不符合该投稿路线；本批未创建 npm 包或正式 Release。 |

## 可直接使用的项目介绍

中文：Windows DSH QQ 角色集成包，连接 NapCatQQ / SnowLuma、B站互动、本地 GPT-SoVITS 语音、可塑记忆与日记、自主活动和控制台。开发预览，外部组件与配套服务需分别配置。

English: A modular Windows QQ character integration kit for DeepSeek Harness, connecting NapCatQQ or SnowLuma, Bilibili interactions, local GPT-SoVITS voice, revisable memory and diaries, autonomous activities and a dashboard. Development preview; external components and companion services require separate setup.

项目截图在根 `screenshots.json` 声明，全部来自公开示例演示。可以使用本仓库提供的公开图标、截图和演示链接；不使用真实账号、私人对话、记忆、账本、本机配置或凭据作为宣传素材。

## 后续维护

沿用同一条目或申请修订，维护者反馈在原线程处理，不重复投稿或催审。每批同步版本、README、CHANGELOG 和开发日志，检查打包与隐私；审核状态以对应公开链接为准，本表保留核查日期。

默认分支 `main` 提供当前预览源码与素材；GitHub Pages 从 `docs/` 发布静态示例，不托管 QQ API 或本地控制台。演示与目录状态不能用来判断本机服务是否在线。
