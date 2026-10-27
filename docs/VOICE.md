# 统一主 agent 语音

语音由当前聊天 agent 决定。`qq_synthesize_voice` 只调用本地 GPT-SoVITS 生成已验证的 WAV；它不会启动第二个 LLM、选择接收人或发送 QQ。此功能需要新版 OneBot 许可补丁和 `scripts/voice_executor.py`，源码存在不代表旧宿主已经加载。

需要语音回复时，同一turn先调用`qq_decide`选择`reply`，再用`qq_synthesize_voice({text, speed?})`；只有返回`status=ready`才能调用已有的`qq_send_voice({path: ready.file})`。发送工具内部生成QQ的record媒体段，不能把媒体段类型误当工具名。每次QQ发送仍校验原turn的固定票，后来的静默、新消息或过期会挡住旧发送。`tool_action`可用于内部合成，但不允许发出语音；ready、有文件或服务端HTTP200都不等于QQ已送达。

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

只访问 `127.0.0.1:ttsPort/tts`。参考音频必须是工作区内普通文件，缺失即不可用；不会自动安装、启动服务或切换edge/cloud服务。`promptText` 是参考音频的既有转写，保持私有，不要复制真实声纹素材到公开仓库。

## 持久化与结果

WAV位于角色目录的 `voice/`，私有请求账本在 `voice/.requests/`。同一owner、会话和request_id绑定完整请求哈希与参考音频哈希，输入改变会拒绝。账本在HTTP调用前写入reserved，并使用OS锁排除同请求并发；ready只复用哈希一致、格式完整的原文件。中断、网络不确定、损坏或已知失败不使用同request_id自动重试，也不以另一合成器兜底。

返回协议为 `dsh-voice-result/v1`，含可信owner、conversation_key、request_id、`delivered=false`。ready另含实际file、sha256、bytes、duration_seconds；error/unknown只返回安全错误码，不回显正文或服务端原始错误。只接受完整、可解码的PCM WAV，最大20MiB、时长120秒。private WAV、请求账本和参考素材都属于本机数据。

旧 `voice-queue.json` 不再是新版主 agent 的发送入口。unified 的旧daemon继续暂停，原队列不清空、不认领、不把观察排队当已发；切换前分别核对兼容目录与当前角色目录的队列。确有积压时保留原始字节、目标与历史投递证据，逐项决定处理，不能靠启用旧消费者清队列。新语音请求直接走当前turn的合成工具与受许可的record工具。

## 验收边界

隔离测试覆盖真实本地HTTP契约、WAV解码、未知与重启不重放、缓存被改、OS锁、scope绑定及合成后静默撤销发送。它们不使用真实TTS/QQ/模型。现场验收还要分别证明本地服务确实生成可解码音频、同一主agent取得结果，以及QQ正向投递和静默负向场景；不能用离线fixture替代这些结论。
