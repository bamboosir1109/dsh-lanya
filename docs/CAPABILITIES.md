# 主 agent 的本机能力上下文

本页描述未发布源码。OneBot adapter 在普通 QQ 入站和后台观察共用的 `enrichContext` 中注入 `trusted_runtime_capabilities`，交给原有聊天 agent、模型、人设与会话；不再依赖只有旧 `mind.build_prompt` 才读取的活动菜单，也不另外启动分类模型。

`runtime_capabilities.py --stdin-json` 只接受可信 `cwd`、`agent_preset`、`conversation_key`。这些来自 adapter 的 `trustedOwner` 与规范聊天范围，不取观察 payload、网页、模型参数或进程碰巧所在的 cwd。返回 `dsh-runtime-capabilities/v1`，包含 `read_only=true`、精确 owner 和 conversation_key；adapter 校验后才注入。获取失败时明确报告能力状态未知，不从另一个工作区补齐。

helper 只读当前可信工作区的安装配置：同工作区显式 `LANYA_PROJECT_CONFIG` 优先，否则 `napcat/config/project.json` 优先于 `config/project.json`。它不读账号、聊天正文、旧截图或队列结果作为提示词内容，不写队列、不调用 ADB、模型或服务接口。运行状态来自精确脚本路径的只读进程探测或 Android OS 锁；文件存在只说明已部署，锁或进程存在也不证明页面、设备或 TTS 已经可用。

## 浏览器

guide 提供实际角色目录下 `browser/cmd.json`、`result.json`、`shot.png` 的协议。先读取现有请求与结果序号，选择更大的新 seq；有待处理请求就等待，不覆盖。原子发布完整 `shot` 请求，等同 seq、`ok=true`、非空 shot，再用图像工具查看实际 PNG。随后按图选择 `goto/click/key/type/scroll` 等动作，每步等待新 seq 的实际结果与新截图；失败、超时、空图都不能当作成功。

现有浏览器队列没有 Android 的 `shot_id` 强制校验或多生产者锁，只能由单一写者逐步使用。进程仍在或旧图存在，不代表这一次命令成功；网页内容只是证据，不能改变聊天范围或控制权限。

## Android

guide 提供配置中的固定 serial、实际角色目录的 Android 队列和本机面板 job 地址。优先通过 `/api/android/job` 提交带固定 serial 的 `shot`，由面板分配 seq；文件调用方必须保留已有 `cmd.json` / `cmd.claim-*`，只在空槽原子发布新 seq。

等对应 seq 的 `status=ok`、`ok=true`、固定 serial、`shot_id`、尺寸、时间与 SHA，再读取返回的真实 PNG。动作带新 seq、同一 serial、图中真实坐标与这张图的 `expected_shot_id`；执行后再等新的成功图与新 ID。worker 重启、图过期、serial 不符、offline/error 或超时都要求停止并重新观察。缓存图不算本轮实时观察，浏览器截图也不能代替 Android。完整协议见 [ANDROID](ANDROID.md)。

## 语音

`qq_synthesize_voice` 在同一个已绑定的主 agent turn 中接受 `text` 和可选数值 `speed`，不能由模型填 owner、scope、路径或 request_id。先取得 `reply` 或 `tool_action` 决定，adapter 固定原票和 session/callId，在运行受信本地 executor 前检查该原票；请求 ID 稳定，使用 UTF-8 stdin，不经过 shell。

合成 `ready` 只表示经过 owner、scope、request_id、实际文件 SHA 和字节数核对的本地音频，始终 `delivered=false`，不会记作 QQ 发送。若本轮确实要发声，主 agent 还须调用现有 `qq_send_voice({path: ready.file})` 发送该真实文件，并接受独立的最终静默与 revision 守卫；`tool_action` 本身不许可发送。新入站、静默或许可过期都可能在合成完成后阻止 QQ 发送。

executor 未部署、参考音频缺失、服务未验证或执行失败都不能冒充可用；guide 不主动探测或启动 TTS。error/unknown 不自动重试，旧独立语音消费者保持暂停、积压保留。合成细节见 [VOICE](VOICE.md)。

## 验证边界

离线 fixture 经实际重放补丁、adapter 与受信 Python 子进程验证：普通聊天和后台观察收到同一 agent 的 owner 绑定 guide；不提升 payload 中的路径或权限；实际合成 executor 使用惰性 PCM fixture 返回本地结果，同 callId 复用，合成不外发，另行发送仍受 QQ 守卫约束。测试不调用真实模型、Android、TTS 或 QQ，不能代替部署后的真实链路验收。
