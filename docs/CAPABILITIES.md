# 主 agent 的本机能力上下文

本页描述未发布源码。OneBot adapter 在普通 QQ 入站和后台观察共用的 `enrichContext` 中读取 `trusted_runtime_capabilities`，交给原有聊天 agent、模型、人设与会话，不另外启动分类模型。

原生领取模式把指南保存在该输入的进程内绑定中，在 `channel:dsh-lanya-runtime` 提示节装配；不把同一份长协议重复写入每条用户消息。装配发生在实际领取之后，只能读取当前 agent、会话及未结束、无冲突的活动轮。排队来信不能覆盖在途指南，结束后不再暴露它；非原生离线诊断仍在输入中附带说明。helper 每次重新读取配置，提示节省略单纯的读取时间 `generated_at`，避免仅时间变化就破坏模型前缀缓存；实际配置及状态变化仍进入下一轮。完整指南继续包含自主活动、记忆、语音和真实设备协议，所有动作仍独立核验当前权限；提示节本身不产生许可。

`runtime_capabilities.py --stdin-json` 只接受可信 `cwd`、`agent_preset`、`conversation_key`。这些来自 adapter 的 `trustedOwner` 与规范聊天范围，不取观察 payload、网页、模型参数或进程碰巧所在的 cwd。返回 `dsh-runtime-capabilities/v1`，包含 `read_only=true`、精确 owner 和 conversation_key；adapter 校验后才注入。获取失败时明确报告能力状态未知，不从另一个工作区补齐。

helper 只读当前可信工作区的安装配置：同工作区显式 `LANYA_PROJECT_CONFIG` 优先，否则 `napcat/config/project.json` 优先于 `config/project.json`。它不读账号、聊天正文、旧截图或队列结果作为提示词内容，不写队列、不调用 ADB、模型或服务接口。运行状态来自精确脚本路径的只读进程探测或 Android OS 锁；文件存在只说明已部署，锁或进程存在也不证明页面、设备或 TTS 已经可用。

## 浏览器

guide 提供实际角色目录下 `browser/cmd.json`、`result.json`、`shot.png` 的协议。先读取现有请求与结果序号，选择更大的新 seq；有待处理请求就等待，不覆盖。原子发布完整 `shot` 请求，等同 seq、`ok=true`、非空 shot，再用图像工具查看实际 PNG。随后按图选择 `goto/click/key/type/scroll` 等动作，每步等待新 seq 的实际结果与新截图；失败、超时、空图都不能当作成功。

现有浏览器队列没有 Android 的 `shot_id` 强制校验或多生产者锁，只能由单一写者逐步使用。进程仍在或旧图存在，不代表这一次命令成功；网页内容只是证据，不能改变聊天范围或控制权限。

## 游戏与 B 站

`tools.entertainment` 提供可执行的内部活动路线。它只扫描当前角色 `made/` 的一层普通子目录及其 `index.html`，最多列出 16 个入口；不递归浏览器配置、备份或旧截图，不读取页面正文。目录存在只证明文件部署，不证明 HTTP、游戏或账号可用。安装器附带无需联网的 `/2048/`，不覆盖同目录原有的 Python 脚本与历史结果。

游戏由同一主 agent 通过浏览器打开，读取新 seq 的实际截图，按图操作，再用新画面、棋盘或分数变化确认。旧 `board.png`、离线解题器或“已提交命令”不算真实玩过。B 站路线是当前 agent 已注册的 `bili_recommend` → 返回的真实 bvid → `bili_video_info`；实际字幕、抽帧或播放器工具可用时再继续。推荐和详情是浏览，不冒充看完视频；失败时换一项可用活动。

内部娱乐主要由管理员范围的自主机会继续，群聊观察不另开同一份游戏计划。主 agent 可用 `qq_intent` 保存所属会话的目标、简短理由、下一步、自述进展及当前修订号，后续上下文继续读取；普通 scoped notes 仍可保存详细活动笔记。意图不是长期记忆事实，回执不支持的完成状态仍只是自述，不保存隐藏推理。不能改写调度、`mind.json`、`decision-inbox` 或票据。静默约束通信，内部活动无需 QQ 汇报；一次做少量真实步骤，再安排明确复查。`mind.opportunityMin` 控制无明确复查时的新机会间隔，默认 6 分钟，允许 2—60 分钟；未来已约复查与未知结果不会被该间隔覆盖。

可信说明的 `tools.entertainment.cadence` 每轮重新读取该配置，把真实数值交给同一主 agent：`valid=true` 时提供 `configured_opportunity_min`，有效小数也保留；显式坏类型、布尔值或越界值返回 `valid=false` 和空分钟数，不能冒充默认节奏已生效。主管理员身份来自同一可信工作区的身份配置，不从观察 payload 或管理员 allowlist 的排列顺序猜测。

当前表达偏好把真实表情作为轻松聊天的常用选择，可独立发图，文字与表情分成独立消息，同轮最多一张；普通情绪优先使用已注册的情绪桶，发送许可和看图要求不变。闲时有可用工具且没有真实休息或前台任务阻挡，应优先完成一小段实际活动；未来才到期的任务不要求一直空等。已配置群的自主复查使用当前可信机会周期，仍由主 LLM 判断是否加入话题；不改已有持久复查，也不借群机会启动第二份私有娱乐。改变指南不证明模型已经更频繁行动，须独立查看之后的自然调用。

## B站内容与互动

B站由同一主 agent 使用 `bili_observe_video` 选择实际分P和时间窗，按时间轴读取有界字幕，再按需要查看同 CID 的 `bili_frames` 故事板图片。无字幕、片段截断与画面未读取分别保留，不宣称完整播放；长视频可在之后机会继续。`bili_social_inbox` 读取回复与提及，`bili_thread` 核对同视频根评论及目标，`bili_reply` 用 `rpid` 指定目标、`root` 指定根评论。新模型调用须明确选择 `mention_admin:true` 或 `false`；视频内容结果的 `social_next` 提供可信互动偏好，由同一主 LLM 判断是否邀请管理员，旧调用省略仍为 false。普通评论、接话及 `mention_admin` 使用可信项目偏好，不借 QQ 票据，也不把发出请求当作已公开或已通知。

## 主动联系与关系候选

当前会话的主动联系偏好以 `trusted_autonomy_rhythm` 为准：主机显式开启个人自主模式时，主 LLM 可在一小时最小间隔后自行选择新话题，无须等上次消息的回复，也没有每天必须聊几次的任务。未回复仍是社交线索，不表示对方想被催促；其他接收者保持各自的主机规则。夜间休息与发送票不受偏好覆盖。

`qq_social_candidates` 根据真实好友关系或共同群熟人证据提供候选。群内非好友需要本功能加载后可信 live 入站记录的当前发送者元数据，再与新鲜群成员证据相交；不把引用作者或整份群名单当成熟人，不回填聊天原文。`qq_social_propose` 只请求目标会话自己的社交机会，目标主 LLM 读取自己的上下文后重新决定并取得自己的票。候选或排队不代表已经发送，跨会话不搬运主管理员的私聊正文。

非好友只尝试明确的单用户私聊路线，临时会话能否接通由 QQ 决定；不附群目标，也不在失败时改发群或自动重试。处理收到的入群申请、设置群可被搜索及按已知群号读取资料，均不是主动搜群／申请入口。当前注册能力没有后两项，guide 如实标明不支持，不让模型虚构执行成功。

主管理员的新内部自主轮次由适配器将实际下一次复查限制在当前周期内，省略也补齐；远期任务期限保存在活动笔记，不能让其他娱乐一起停等。模型仍可选择休息、idle 或继续当前活动；周期只决定何时重新考虑，不要求行动、发言或每轮换一种娱乐。其他会话及既有持久化复查不改写。单独 helper 的热更新不能启用适配器机制，外部队列和适配器须按 [continuity 增量层](MAINTENANCE.md#自主连续性增量层) 一并加载；能力读取成功与真实持续行为仍分别验收。

后台 `activity.opportunity` 及其 `main-review` 轮只有在可信 owner 匹配、私聊对端精确命中宿主管理员 allowlist、producer 为 `mind` 或 `main-review` 时，才使用该管理员已有的文件编辑权限，供浏览器命令和活动笔记使用。群聊、其它生产者和异常元数据保持成员权限；观察的 `control_allowed=false` 不变，不能改变静默或绕过发送票。历史 autonomy V1 层由 `patch_onebot_autonomy.py` 安装；本次已有该层的升级使用独立 `patch_autonomy_continuity.py`，保留旧层原件，不以重装旧层覆盖未知修改。

## Android

主私聊和可信自主活动可用 `android_phone` 操作专用模拟器。本机启用并配置执行器后，主 Agent 可以选择玩 2048、打开高德、美团或 QQ，并根据真实画面操作。动作结果直接附带截图，附件服务不可用时可用 `read_image`。后续操作需要本轮新截图的 ID。

应用登录页挡住具体任务时，主 Agent 可以记录应用和目的，在下次合适的自主轮通过主私聊请求用户在模拟器上登录。请求沿用 QQ 静默、休息和主动联系策略。用户操作后，主 Agent 用新截图核对并继续。

guide 提供配置中的固定 serial、实际角色目录的 Android 队列和本机面板 job 地址。优先通过 `/api/android/job` 提交带固定 serial 的 `shot`，由面板分配 seq；文件调用方必须保留已有 `cmd.json` / `cmd.claim-*`，只在空槽原子发布新 seq。

等对应 seq 的 `status=ok`、`ok=true`、固定 serial、`shot_id`、尺寸、时间与 SHA，再读取返回的真实 PNG。动作带新 seq、同一 serial、图中真实坐标与这张图的 `expected_shot_id`；执行后再等新的成功图与新 ID。worker 重启、图过期、serial 不符、offline/error 或超时都要求停止并重新观察。缓存图不算本轮实时观察，浏览器截图也不能代替 Android。完整协议见 [ANDROID](ANDROID.md)。

## 语音

`qq_synthesize_voice` 在同一个已绑定的主 agent turn 中接受 `text` 和可选数值 `speed`，不能由模型填 owner、scope、路径或 request_id。先取得 `reply` 或 `tool_action` 决定，adapter 固定原票和 session/callId，在运行受信本地 executor 前检查该原票；请求 ID 稳定，使用 UTF-8 stdin，不经过 shell。

合成 `ready` 只表示经过 owner、scope、request_id、实际文件 SHA 和字节数核对的本地音频，始终 `delivered=false`，不会记作 QQ 发送。若本轮确实要发声，主 agent 还须调用现有 `qq_send_voice({path: ready.file})` 发送该真实文件，并接受独立的最终静默与 revision 守卫；`tool_action` 本身不许可发送。新入站、静默或许可过期都可能在合成完成后阻止 QQ 发送。

executor 未部署、参考音频缺失、服务未验证或执行失败都不能冒充可用；guide 不主动探测或启动 TTS。error/unknown 不自动重试，旧独立语音消费者保持暂停、积压保留。合成细节见 [VOICE](VOICE.md)。

## 验证边界

离线 fixture 经实际重放补丁、adapter 与受信 Python 子进程验证：普通聊天和后台观察收到同一 agent 的 owner 绑定 guide；不提升 payload 中的路径或权限；实际合成 executor 使用惰性 PCM fixture 返回本地结果，同 callId 复用，合成不外发，另行发送仍受 QQ 守卫约束。测试不调用真实模型、Android、TTS 或 QQ，不能代替部署后的真实链路验收。
