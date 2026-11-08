# 商店收录与展示

本项目申请进入社区维护的 DSH 插件目录。审核通过后，dsh-market 和使用同一索引的商店会同步收录；申请不等于审核通过，也不代表 DSH 官方认证。

2026-11-08 已提交[目录 PR #13180](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/pull/13180)，当前等待审核，尚未合并。仅新增本项目一个 YAML 条目。当前公开开发预览、README 与截图统一由默认分支 `main` 提供；仓库 About 已提供演示入口并补齐 `dsh-plugin`、`deepseek-harness` 等发现标签。

## 从哪里开始

- [交互演示](https://bamboosir1109.github.io/dsh-lanya/)使用示例数据，无账号、无本机接口。
- 当前公开预览在 [`main`](https://github.com/bamboosir1109/dsh-lanya/tree/main)，按 [README](../README.md) 与[安装手册](INSTALL.md)部署。
- 默认仓库入口指向同一份预览源码；安装配套服务、加载插件与核对实际版本仍需分别完成。
- 完整系统需要 DSH 插件、Windows 配套服务，以及自己的 QQ 后端、外部插件和配置。

## 收录方式

按 [dsh-market 投稿说明](https://github.com/dsh-market/dsh-market#submit-your-plugin)，向 [awesome-dsh-plugin](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin) 投稿，不向市场程序仓库增加条目。

[贡献指南](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md)要求一个独立 YAML 文件、可安装的 `dsh.bundle`、真实代码、仓库满一天和 `dsh-plugin` topic。描述只说功能事实，维护者决定是否收录。

条目为 `data/plugins/bamboosir1109__dsh-lanya.yml`，分类 `identity`，描述角色提示词与本地控制台。其他配套能力的配置条件在项目说明中列明，不把其他作者的插件作为本项目独立实现宣传。

根 `screenshots.json` 声明公开截图顺序；截图全部来自示例演示，不包含真实账号、私人对话、运行配置或凭据。默认分支提供图标、截图及其声明；素材可访问不代表目录已审核或商店已抓取。

## 维护

目录 PR、开发 PR 与审核结果分别核对；沿用同一条目修订，不重复投稿或重复催审。每批同步版本、README、CHANGELOG 与开发日志，检查打包和隐私。

公开演示由 GitHub Pages 从 `main` 的 `docs/` 提供静态内容，不托管 QQ API 或本地控制台。演示状态不代表本机服务健康。
