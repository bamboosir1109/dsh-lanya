# Android 视觉队列

`scripts/androidd.py` 是维护版源码；复制程序、注册并启动 worker、真实设备验收须分别核实。模拟器启动、BIOS/SVM 和 Windows 虚拟化功能也需分别核实，离线测试通过不代表真实设备验收完成。

推荐使用硬件加速；AMD 的 SVM 未开启时，加速路径仍需在用户安排的维护窗口完成 BIOS 前置并复查 Windows hypervisor。官方也保留 `-accel off` 软件模拟入口，需显式设置 `ANDROID_I_WANT_MY_TCG=yes`，但标为不受支持且很慢；不能把硬件加速检查失败写成软件模拟不存在。一次特定版本或镜像的软件模式崩溃，也不能证明所有软件模式都不可能。无论使用哪条路径，都以实际开机、ADB 在线和真实截图验收，见[命令行说明](https://developer.android.com/studio/run/emulator-commandline)与[加速说明](https://developer.android.com/studio/run/emulator-acceleration)。

项目配置示例：

```json
{
  "android": {
    "serial": "emulator-5556",
    "adbPath": "",
    "maxShotAgeSeconds": 30
  }
}
```

默认 serial 固定为 `emulator-5556`，空值或不合法值拒绝启动；`adbPath` 留空时使用工作区 `android/platform-tools/adb.exe`（非 Windows 使用 `adb`）。配置路径、工作区和角色数据目录沿用 `project_conf`，支持其环境覆盖。ADB 只在子进程使用工作区 `android/temp`，不会修改主机全局 TEMP。每条读、写、截图命令都带 `-s <固定serial>`；没有扫描端口或选择列表第一台设备的回退。

运行 `python androidd.py --setup` 只抓一张验证图并退出；无参数消费长期队列，`--once` 最多消费一个现有 job。队列文件仍是角色目录 `android/cmd.json`、`result.json`、`shot.png`；新增 `shot-meta.json` 和 OS worker 锁。生产者应先写临时文件，再原子替换 `cmd.json`。不要同时运行旧、新两种 worker。

面板状态的 `claim_wait` 仅诊断已领取请求的数量与文件等待时间，不读取任务内容。时间来自文件 mtime，重命名不会重置它；时钟异常、链接或元数据不可读时不提供时长。新旧 claim 都可能仍在处理，也可能结果未知，不能凭年龄判断 worker 已死；需人工核对持续未完成的请求，面板不会删除 claim 或自动重试动作，原有 busy 和截图许可保持生效。

首次获取观察图：

```json
{"seq":1,"action":"shot"}
```

成功结果包含 `status=ok`、`serial`、`shot_id`、`width`、`height`、`captured_at`、`sha256` 和真实图片路径。后续动作必须携带刚观察的 ID，例如：

```json
{"seq":2,"action":"tap","x":100,"y":100,"expected_shot_id":"<上次成功结果的shot_id>"}
```

`shot_id` 也可作为输入别名。`tap`、`longtap`、`swipe`、`text`、`key`、`start`、`shell` 均需当前 serial 的最近图，默认不超过 30 秒；坐标须位于该图尺寸内。图文件或元数据变化、ID 过期/错误、目标 serial 不一致均拒绝执行。动作后重新截图并生成新 ID，旧 ID 不能再授权下一步。worker 重启也必须重新观察，不沿用旧截图作为动作许可。

截图核对 PNG 签名、chunk CRC、完整 IEND、压缩流和扫描行长度，并与同一 serial 的 `wm size` 尺寸核对，支持旋转后的宽高互换。暂不接受交错 PNG。失败结果是 `offline` 或 `error`，`shot`/`shot_id` 为空；磁盘可能保留上一张好图供排查，不能把它当成这次成功截图。

原生动作名称和字段沿用旧接口。`shell` 仅支持只读 `getprop`、单服务 `dumpsys`、`wm size/density`、`settings get/list`、`pm list`；配置更改和任意 shell 写命令不兼容。文本参数做远端 shell 引号处理，不能用输入文字拼接 Android shell 命令。协议本身不做 QQ 发送，也不代表自主行为许可；接入主 LLM 统一决策仍需上层调用方负责。
