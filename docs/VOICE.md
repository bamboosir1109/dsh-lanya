# 统一主 agent 语音

语音由当前聊天 agent 决定。`qq_synthesize_voice` 只调用本地 GPT-SoVITS 生成已验证的 WAV；它不会启动第二个 LLM、选择接收人或发送 QQ。此功能需要新版 OneBot 许可补丁和 `scripts/voice_executor.py`，源码存在不代表旧宿主已经加载。

需要语音回复时，同一turn先调用`qq_decide`选择`reply`，再用`qq_synthesize_voice({text, speed?})`；只有返回`status=ready`才能调用已有的`qq_send_voice({path: ready.file})`。`ready.file` 是经过内容校验并放入渠道媒体目录的实际 WAV 副本，不要自行拼回工作区 `voice/` 路径。普通群成员无需取得整个工作区的读取权限，原 QQ 媒体围栏保持。

发送工具内部生成QQ的record媒体段，不能把媒体段类型误当工具名。每次QQ发送仍校验原turn的固定票、绑定音频及其实际编码内容，后来的静默、新消息、篡改或过期会挡住旧发送。`tool_action`可用于内部合成，但不允许发出语音；ready、有文件或服务端HTTP200都不等于QQ已送达。

语音可以是简短问候、玩笑或合适回应的自然选择，不要求用户先明确索要。仍由同一主 LLM 结合当前聊天和近期记忆决定，不设置每几句话必须发语音的配额。代码、链接、精确步骤与长说明优先文字，也不为凑两种媒介重复发送同一段内容。只读指引中的 `unverified_not_probed` 表示该指引未探测引擎，不能据此报告语音离线；有实际工具与素材时可在当前许可下尝试一次，再依据真实回执判断。

模型只提供正文和可选速度，文本最多600字，速度0.5至2。scope、cwd、preset、request_id、脚本路径和服务地址由可信执行上下文绑定，不接受观察 payload 的覆盖。`request_id` 由实际 session/callId产生，整个异步过程沿用该次绑定。

## 本地配置

按可信工作区读取 `napcat/config/project.json`，没有该文件时才读 `config/project.json`。首个现存配置损坏则停止，不降级到另一个角色。进程明确设置 `LANYA_PROJECT_CONFIG` 时，该路径必须仍在可信工作区内。

```json
{
  "dataDirName": "YOUR_ROLE_DIRECTORY",
  "ttsPort": 9880,
  "tts": {
    "refAudioPath": "tts/ref/ref-main.wav",
    "promptText": ""
  }
}
```

只访问 `127.0.0.1:ttsPort/tts`，明确请求 `media_type=wav` 与 `streaming_mode=false`。参考音频必须是工作区内普通文件，缺失即不可用；不会自动安装、启动服务或切换edge/cloud服务。`promptText` 是参考音频的既有转写，保持私有，不要复制真实声纹素材到公开仓库。空转写是否支持取决于实际本地模型版本，不能以某一 v2 配置的成功代替其他模型的验证。

## 持久化与结果

WAV位于角色目录的 `voice/`，私有请求账本在 `voice/.requests/`。同一owner、会话和request_id绑定完整请求哈希与参考音频哈希，输入改变会拒绝。账本在HTTP调用前写入reserved，并使用OS锁排除同请求并发；ready只复用哈希一致、格式完整的原文件。中断、网络不确定、损坏或已知失败不使用同request_id自动重试，也不以另一合成器兜底。

返回协议为 `dsh-voice-result/v1`，含可信owner、conversation_key、request_id、`delivered=false`。ready另含实际file、sha256、bytes、duration_seconds；error/unknown只返回安全错误码，不回显正文或服务端原始错误。只接受完整、可解码的PCM WAV，最大20MiB、时长120秒。private WAV、请求账本和参考素材都属于本机数据。

HTTP 非成功使用 `local_tts_http_error` 与数字状态码，不读取或展示服务端错误正文；连接拒绝为 `local_tts_unreachable`，坏音频为 `invalid_local_audio`，超时为 `local_tts_timeout` 且结果保持 unknown。已生成或已处理不等于可安全重复，同一请求不会因重启自动再合成；当前轮相同语音已有 sent／unknown 发送记录时，不通过换调用号再发。

## 控制台状态

系统页显示同一主 Agent 按需合成、执行器安装情况与本地引擎进程线索，不要求独立语音队列常驻。`GET /api/voice-status` 和 `/api/state.voice` 使用 `dsh-voice-status/v1`；该接口只读配置、普通程序存在性与进程快照，不读取音频、参考转写、队列或请求账本，也不触发合成或 QQ 发送。超过 90 秒的进程线索标记过期。

进程存在不等于可合成；此投影的 `synthesis.state=not_probed`、`delivery.state=unverified` 明确不对实际音频与 QQ 送达作成功断言。固定泛用短句的本机合成验收保存在私有维护凭据，不能冒充角色主动发送或用户已听到。

旧 `voice-queue.json` 不再是新版主 agent 的发送入口。unified 的旧daemon继续暂停，原队列不清空、不认领、不把观察排队当已发；切换前分别核对兼容目录与当前角色目录的队列。确有积压时保留原始字节、目标与历史投递证据，逐项决定处理，不能靠启用旧消费者清队列。新语音请求直接走当前turn的合成工具与受许可的record工具。

## 验收边界

隔离测试覆盖真实本地HTTP契约、WAV解码、未知与重启不重放、缓存被改、OS锁、scope绑定及合成后静默撤销发送。它们不使用真实TTS/QQ/模型。现场验收还要分别证明本地服务确实生成可解码音频、同一主agent取得结果，以及QQ正向投递和静默负向场景；不能用离线fixture替代这些结论。
