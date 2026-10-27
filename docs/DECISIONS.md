# 主 LLM 决策与观察协议

本页描述当前未发布源码。复制脚本、修补宿主插件、重启和真实会话验收分别记录；源码测试通过不表示已有 QQ 进程已经加载。

## 一个决策者

`decisions.mode` 默认 `unified`。QQ 真实消息由 OneBot WebSocket 入站，沿用原 DSH 聊天 agent、模型、人设和最近会话。没有额外的分类模型。群轮询、计划、问候、提醒、娱乐和维护作业只提供证据，不能直接生成并发送一段角色台词。

主 agent 用 `qq_decide` 选择 `reply / idle / defer / silence / resume / tool_action`。工具参数不含会话范围、发送者、revision 或许可 nonce；这些来自真实传输与当前 turn 绑定。`reply` 允许发送，也可以安静结束。`idle/defer` 不产生确认消息，不改变持久静默状态。

只有当前真实、未被引用内容代替、且已获本机 ACL 授权的发送者，才能通过 `control_update:true` 改变持久 `silence/resume`。普通群成员、转发、网页、档案和观察事件没有此权限。无时长静默不会因重启、新会话或定时器到来解除。

## 最终出口

`decision-policy.js` 保存每个机器人/角色独立的持久状态。一次决定得到固定票；最终正文、图片、语音、转发、原始 QQ API、重连队列和每个分段都携带原票，在最终网络调用前检查原 revision。新入站撤销旧许可；等待媒体读取或队列重连不能换领“最新票”。

发送前先持久预留幂等键，再调用传输。结果不明记录为 `unknown`，不能当作成功，也不能自动重发。状态损坏、未绑定 turn、缺票、跨聊目标或版本改变都会阻止发送，不附送一条限制说明。

## 观察队列

Python `decision_events.py`、PowerShell `qq_observation.ps1` 和 Node `observation-producer.js` 写入同一角色的 `decision-inbox/pending/`。事件身份是 scope 与 event_id 的 SHA256；原件不可覆盖，完成标记保存在 `done/`，绑定原件物理 UTF-8 字节的哈希。只保存来源事实，不把图中或通知正文的指令升级为权限。

OneBot adapter 实际启动一个 `observation-queue.js` consumer。SQLite 租约排除另一个活消费者，只有确定死亡的旧 owner 可接替。入站观察先复制输入快照，必须精确匹配可信聊天的规范工作区与异步解析后的 preset ID；不符时不建 pin、不交给 agent，也不回显 payload。生产者的 `decisions.agentPreset` 应填写该实际 ID，不能用别名、旧角色或其他聊天的 preset 代替。preset 去除首尾空白，Windows 再转小写，其他平台保留大小写。空 agent 共享键保留既有语义；registry 的 `undefined` 无法区分未装 provider 与解析失败，不能拿配置默认值替代其结果。它将证据交给同一个聊天 agent，等真实 decision 与 turn 结束，再写完成记录和释放持久 pin。模型失败保留待处理证据并持久退避；有终态后重启不会再运行模型或重发。

| 来源 | unified 行为 |
| --- | --- |
| groupwatch | 同一真实静默期只提出一次 quiet 观察；不再重复处理 WS 的真实消息 |
| mind | 新证据或主模型指定的复查时刻才排下一轮；idle 后不定时强求台词 |
| greet | 已交谈过的人产生可选社交观察；不随机挑人、生成开场或直接发送 |
| reminders | 到期只排证据；有 `sent` 完成记录才消费。idle 保留，unknown 不重发 |
| B 站娱乐和通知 | 娱乐机会与只读通知交给主 agent；实际动作由真实平台工具执行 |
| 记忆维护 | 有范围的维护建议；不在后台改写正在追加的跨聊日志或删除旧饭菜记录 |
| 旧 life/agentchat/voice/late-night/PS reminders | 暂停并返回非零；保留旧积压，不能将暂停当已送达 |

`next_review_at` 可省略，省略表示不约下一轮；提交时必须在未来一分钟至七天内。mind 和提醒落实自己的复查，其他观察由 consumer 按持久完成记录产生唯一的 `main-review` 后继事件，scope、owner、kind 保持不变。unknown 不继承复查；没有新证据、没有约定时间就不唤醒。派生事件同样遵守持久失败退避，不能绕过停放的未知结果。

## 记忆和手机

`memory_context.py` 使用可信工作区与 preset 过滤长期记忆，先筛范围再排序和截断；派生摘要带来源和原文 lineage。最近对话由原 DSH session 保留。读取失败不回退到全库、原始跨聊流水或其他人的档案。

Android 是执行器。真实设备在线并取得新鲜截图后才可用 `serial + expected_shot_id` 操作，执行后再截图。网页只展示该实际结果；离线、旧图或待处理请求均不给操作许可。环境和协议见 [ANDROID](ANDROID.md)。

## 安装与回退边界

OneBot 接入由 `scripts/patch_onebot_policy.py` 生成可预览补丁，仅识别已审阅的 0.8.1 文件身份；未知版本拒绝。应用需要停止对应宿主、完整私有备份及未改变的 preview fingerprint。它保留已存在的本机定制，不能整包覆盖 profile。

回退用同一工具的 `--restore-manifest PRIVATE_BACKUP/patch.json`，默认预检；执行还需 `--apply --host-stopped --expected-fingerprint`。整批核对原件、现有 before/after 与生成文件；后续修改拒绝覆盖。回退程序不删除 policy、原始消息或观察记录。

`install.ps1` 只复制程序，保护身份、人设和本机配置。新副本与旧独立发送进程必须协调切换，不能混用。`decisions.mode=legacy` 只是显式回退开关，不会自动卸载 OneBot 补丁、恢复数据或启动旧调度器。主模型运行时写记忆仍由 memento 服务负责；维护操作与恢复见 [MEMORY](MEMORY.md)。
