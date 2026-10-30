# 主 LLM 决策与观察协议

本页描述当前未发布源码。复制脚本、修补宿主插件、重启和真实会话验收分别记录；源码测试通过不表示已有 QQ 进程已经加载。

## 一个决策者

`decisions.mode` 默认 `unified`。QQ 真实消息由 OneBot WebSocket 入站，沿用原 DSH 聊天 agent、模型、人设和最近会话。没有额外的分类模型。群轮询、计划、问候、提醒、娱乐和维护作业只提供证据，不能直接生成并发送一段角色台词。

主 agent 用 `qq_decide` 选择 `reply / idle / defer / silence / resume / tool_action`。工具参数不含会话范围、发送者、revision 或许可 nonce；这些来自真实传输与当前 turn 绑定。`reply` 允许发送，也可以安静结束。`idle/defer` 不产生确认消息，不改变持久静默状态。

只有当前真实、未被引用内容代替、且已获本机 ACL 授权的发送者，才能通过 `control_update:true` 改变持久 `silence/resume`。普通群成员、转发、网页、档案和观察事件没有此权限。无时长静默不会因重启、新会话或定时器到来解除。

## 最终出口

`decision-policy.js` 保存每个机器人/角色独立的持久状态。一次决定得到固定票；最终正文、图片、语音、转发、原始 QQ API、重连队列和每个分段都携带原票，在最终网络调用前检查原 revision。新入站撤销旧许可；等待媒体读取或队列重连不能换领“最新票”。

发送前先持久预留幂等键，再调用传输。结果不明记录为 `unknown`，不能当作成功，也不能自动重发。状态损坏、未绑定 turn、缺票、跨聊目标或版本改变都会阻止发送，不附送一条限制说明。

### QQ 内容与许可分别校验

`qq-egress.js` 提供内容规范，独立的 `patch_onebot_egress.py` 将它接到已打 policy/replay 补丁的最终发送路径。普通正文、媒体说明和原始消息的文字段使用同一规范；模型生成的表情占位、Markdown 伪图和明确动作旁白不会作为真实图片或普通聊天发送。空内容不给“已发送”结果，媒体段保持原顺序且只投递一次。

完整单行 Markdown 图片整体移除，包括目标 URL 中平衡嵌套或转义的圆括号、尖括号目标及可选标题，避免漏出 `.png)` 等尾部。代码和可信技术上下文中的完整 JSON、普通链接与其余正文保留；未闭合、跨行或标题不完整的语法保守保留，不追到后续正文寻找闭合。真实图片仍通过 `qq_send_image` 发送。既有 V1 的单模块升级与独立撤回见 [images 增量层](MAINTENANCE.md#onebot-伪图解析增量层)，内容修复不改变发送许可。

普通叙述按 Unicode 字素边界拆成最多 40 字的气泡，不把超量片段重新拼回最后一条。代码、完整链接、报错、步骤等明确保护内容保持完整，不能用吞掉正文换取短回复。当前真实直接入站的明确角色扮演请求可取得本轮格式例外；引用、模型参数、后台观察和旧队列不能给它授权，例外不持久化，也不授予发送权限。

每个新增片段仍使用原 turn 的固定票，在最终传输前重新校验。静默或许可改变停止后续片段；已有成功片段、被抑制的余量和 `unknown` 结果分别记录，不能自动续传或把部分送达当作全部完成。安装和增量撤回见 [维护](MAINTENANCE.md#onebot-内容出口补丁)。

## 观察队列

Python `decision_events.py`、PowerShell `qq_observation.ps1` 和 Node `observation-producer.js` 写入同一角色的 `decision-inbox/pending/`。事件身份是 scope 与 event_id 的 SHA256；原件不可覆盖，完成标记保存在 `done/`，绑定原件物理 UTF-8 字节的哈希。只保存来源事实，不把图中或通知正文的指令升级为权限。

OneBot adapter 实际启动一个 `observation-queue.js` consumer。SQLite 租约排除另一个活消费者，只有确定死亡的旧 owner 可接替。入站观察先复制输入快照，必须精确匹配可信聊天的规范工作区与异步解析后的 preset ID；不符时不建 pin、不交给 agent，也不回显 payload。生产者的 `decisions.agentPreset` 应填写该实际 ID，不能用别名、旧角色或其他聊天的 preset 代替。preset 去除首尾空白，Windows 再转小写，其他平台保留大小写。空 agent 共享键保留既有语义；registry 的 `undefined` 无法区分未装 provider 与解析失败，不能拿配置默认值替代其结果。它将证据交给同一个聊天 agent，等真实 decision 与 turn 结束，再写完成记录和释放持久 pin。模型失败保留待处理证据并持久退避；有终态后重启不会再运行模型或重发。

| 来源 | unified 行为 |
| --- | --- |
| groupwatch | 同一真实静默期只提出一次 quiet 观察；不再重复处理 WS 的真实消息 |
| mind | 为配置中的主管理员和群聊分别提供低频自主机会；允许娱乐、延续计划或 idle，不定时强求台词 |
| greet | 已交谈过的人产生可选社交观察；不随机挑人、生成开场或直接发送 |
| reminders | 到期只排证据；有 `sent` 完成记录才消费。idle 保留，unknown 不重发 |
| B 站娱乐和通知 | 娱乐机会与只读通知交给主 agent；实际动作由真实平台工具执行 |
| 记忆维护 | 有范围的维护建议；不在后台改写正在追加的跨聊日志或删除旧饭菜记录 |
| 旧 life/agentchat/voice/late-night/PS reminders | 暂停并返回非零；保留旧积压，不能将暂停当已送达 |

`next_review_at` 可省略，省略表示不约该次决定的明确复查；提交时必须在未来一分钟至七天内。提醒及旧 `activity_due` 保留生产者复查语义，新的 `activity.opportunity` 与其他观察由 consumer 按持久完成记录产生唯一的 `main-review` 后继事件，scope、owner、kind 保持不变。mind 只读核对并推进后继锚点，不重复提交该次复查。unknown 不继承复查；派生事件同样遵守持久失败退避。

### 自主机会与面板控制

完成一次决定却没指定复查，不应让自主功能永久睡眠。统一模式的 mind 在已知终态后按可信项目配置 `mind.opportunityMin` 提供下一次可选机会，允许范围为 2—60 分钟，公开默认 6 分钟；旧计划的 `next_check_min` 不覆盖这项配置。这只唤醒同一主 agent 观察，不预设发送、娱乐动作或成功结果。明确复查优先，待处理事件和同证据的 unknown 不因时钟或手动检查重放；真正的新证据可另作判断。持久静默仍约束最终 QQ 出口。

主管理员与配置群聊使用各自可信 scope、事件和固定票，计划延续在各自原会话中进行。旧单范围状态原样保存在 `decision_legacy`，迁移后的控制状态放在版本 2 的 `decision_scopes`；旧管理员计划只是未核实参考，不注入群聊或冒充已完成动作。

面板的“自主活动”显示主 agent 调度、消费者与观察状态，保留真实进程数。mind 是定时短进程，进程数为 0 不等于功能停止。统一模式的两个控制入口均走同一个“请求检查”，不会启动已退役的 Life 任务；请求有互斥和短冷却，只确认观察生产者启动，不表示主模型已完成决定或消息已送达。调度归属无法核实时显示未知或离线，不能回退启动独立循环。

### 内部娱乐、证据与作品服务

主管理员私聊的自主机会负责内部游戏、B 站浏览和创作，群聊机会按该群的社交上下文判断，不再各开一份相同娱乐计划。主 agent 使用可信能力说明中的实际游戏入口、工具与活动笔记位置，按真实结果续写本会话计划；不能通过改写 mind、观察队列、policy 或配置文件来安排唤醒或伪造完成。

持久 `silence` 约束 QQ 输出，内部娱乐是否执行另由同一主 agent 判断；调频、网页和观察不能解除静默，也不要求娱乐结束后发一条汇报。内部工具仍遵守原有权限、范围和结果校验，`tool_action` 不授予 QQ 发言许可。

某个范围的观察原件缺失或后继链不可验证时，记录该范围的安全错误元数据并阻止新机会，保留原状态和已有证据，不删掉 pending、不猜测完成、不重新提交旧动作；其他证据完整的范围可继续。机会签名只包含范围内证据，提示词或能力说明更新本身不能触发 unknown 的重放。

作品站与面板 API 是同一脚本的两个端口，分别核验进程、监听 owner 和 `/api/service-health` 返回的角色、PID、端口及程序哈希。健康实例复用；端口未知占用、进程活着但 HTTP 不健康均不能直接重复启动。后台看护在确认目标端口空闲且无对应进程后才单实例启动，失败退避；界面“检查恢复”复用这条路径，不另开旧 Works 循环。

新增 `games/2048/index.html`、`game.js`、`app.js` 是本地真实游戏。部署到角色的 `made/2048/` 时只新增这三个文件，原 Python 脚本、图片和其他作品保留。游戏规则及实际 UI 消费者有 14 项离线测试，另有隔离浏览器的键盘、触屏和重开验证；它们不证明安装副本已加载，也不证明主 agent 已自行玩过。现场证据要求见 [验收矩阵](ROADMAP.md#验收矩阵)。

### B 站直接执行

`bili-main-agent.js` 通过已装 `dsh-bili-scroll` 的 direct MCP client 补充 `bili_video_info`、`bili_recommend` 和 `bili_send_message`。搜索、字幕、封面、抽帧及评论仍沿用该插件原工具。统一模式的 `bili_run` 直接返回推荐事实，由当前主 agent 继续调用原语或 idle，不创建 headless 会话，也不自动向 QQ 汇报。旧窗口定时器及私信独立回复在 unified 下停止认领；只有共享 project 配置明确 `decisions.mode=legacy` 才恢复原分支。其他 B 站插件的独立调度仍需在协调切换中停用。

新私信工具按可信 host session/callId 在每账号独立的私有 `bili-actions.sqlite` 预留执行身份，仅保存参数哈希和回执。取得实际 `msg_key` 才返回 sent；失败、unknown 或重启后的同一执行均不自动重试。相同执行换参数会被拒绝，新 callId 表示新的动作，不能据此宣称按正文去重或平台提供了 exactly-once。账本达到容量会拒绝新增，不能删除旧账来冒充一次新执行。它不授予 QQ 发言权限，也不表示所有 B 站动作已纳入同一账本。

源码补丁和合成 host/MCP 调用回归不表示已修改安装、登录账号、发出私信或验证真实平台。安装与恢复步骤见 [维护](MAINTENANCE.md#b-站主-agent-补丁)。

## 记忆和手机

`memory_context.py` 使用可信工作区与 preset 过滤长期记忆，先筛范围再排序和截断；派生摘要带来源和原文 lineage。最近对话由原 DSH session 保留。读取失败不回退到全库、原始跨聊流水或其他人的档案。

原生 `memory_recall` 的会话原文是另一个入口，需搭配独立 recall-scope 补丁：仅当前可信逻辑 session、目录和 preset 的本地事件可见，候选与读取结果均核 header，未证明归属的父会话继承前缀不参与片段或命中计数。不能按目录猜测其他 session 属于同一个 QQ 聊天；长期事实的合法共享层照旧，详见 [MEMORY](MEMORY.md#会话原文与长期事实分开核验)。

同一 OneBot 上下文入口也为普通聊天和后台观察注入 owner 绑定的只读本机能力 guide，包含浏览器、Android 的真实文件协议与语音合成/发送边界。脚本存在不等于 worker 在线，缓存图不算当前观察，本地音频 ready 不算 QQ 已送达；观察 payload 不能指定工具工作区、preset 或升级权限，见 [CAPABILITIES](CAPABILITIES.md)。

Android 是执行器。真实设备在线并取得新鲜截图后才可用 `serial + expected_shot_id` 操作，执行后再截图。网页只展示该实际结果；离线、旧图或待处理请求均不给操作许可。环境和协议见 [ANDROID](ANDROID.md)。

## 安装与回退边界

OneBot 接入由 `scripts/patch_onebot_policy.py` 生成可预览补丁，仅识别已审阅的 0.8.1 文件身份；未知版本拒绝。应用需要停止对应宿主、完整私有备份及未改变的 preview fingerprint。它保留已存在的本机定制，不能整包覆盖 profile。

回退用同一工具的 `--restore-manifest PRIVATE_BACKUP/patch.json`，默认预检；执行还需 `--apply --host-stopped --expected-fingerprint`。整批核对原件、现有 before/after 与生成文件；后续修改拒绝覆盖。回退程序不删除 policy、原始消息或观察记录。

### 离线 replay 的独立隔离层

已打 policy V1 的 OneBot 包还需 `patch_onebot_replay.py`，否则原 replay 构造 bridge 时仍可能读取生产 policy 配置并启动观察 consumer。新层在构造前选择临时 policy、停用 pump、记忆和能力 helper；入站解析继续运行，媒体下载与本机跨聊日志写入被阻止。原操作员指定的录制文件输入、报告文件输出保留，其他状态只在本次新建临时目录内使用，成功或异常后清理。

这不改变 `lanyaInjected` 的非真实入站判定，也不提供主模型静默/恢复的真实会话验收。离线录制 agent 只记录 followup，不调用模型或 QQ/B 站网络；内置 file inject 的 dry-run 不能据此当作所有工具的全局隔离。补丁必须接在原 V1 manifest 后，回退先撤 replay 层再撤 V1，见 [维护步骤](MAINTENANCE.md#onebot-离线-replay-隔离补丁)。

`install.ps1` 只复制程序，保护身份、人设和本机配置。新副本与旧独立发送进程必须协调切换，不能混用。`decisions.mode=legacy` 只是显式回退开关，不会自动卸载 OneBot 补丁、恢复数据或启动旧调度器。主模型运行时写记忆仍由 memento 服务负责；维护操作与恢复见 [MEMORY](MEMORY.md)。
