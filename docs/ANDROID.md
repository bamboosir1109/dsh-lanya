# Android 视觉队列

`scripts/androidd.py` 是维护版源码；复制程序、注册并启动 worker、真实设备验收须分别核实。模拟器启动、BIOS/SVM 和 Windows 虚拟化功能也需分别核实，离线测试通过不代表真实设备验收完成。

推荐使用硬件加速；AMD 的 SVM 未开启时，加速路径仍需在用户安排的维护窗口完成 BIOS 前置并复查 Windows hypervisor。官方也保留 `-accel off` 软件模拟入口，需显式设置 `ANDROID_I_WANT_MY_TCG=yes`，但标为不受支持且很慢；不能把硬件加速检查失败写成软件模拟不存在。一次特定版本或镜像的软件模式崩溃，也不能证明所有软件模式都不可能。无论使用哪条路径，都以实际开机、ADB 在线和真实截图验收，见[命令行说明](https://developer.android.com/studio/run/emulator-commandline)与[加速说明](https://developer.android.com/studio/run/emulator-acceleration)。

已实测的离线兼容环境是 SDK Tools 25.2.5 经典 ARM 模拟器与 Android 6.0 AOSP（API 23）。此环境用于离线 2048 开发验证，来宾采用受限网络、专用数据盘及独立回环 ADB 服务。SDK Tools 的维护状态见 [Android Developers 发布说明](https://developer.android.com/tools/releases/sdk-tools)。

启动后先确认目标设备在线、`sys.boot_completed` 为 `1`、`pm path org.dshlanya.game2048` 返回安装路径，再使用工作队列。经典来宾内重启如果在系统日志中确认已关机，重新启动同一模拟器实例并保留数据盘。独立 ADB 服务显示目标离线时，先按固定 serial 检查连接，必要时对该设备执行一次 `adb -P <端口> -s <serial> reconnect`，随后重新观察屏幕。

想在 Windows 桌面直接查看和操作模拟器，可用独立窗口启动同一个 AVD。启动参数去掉 `-no-window`，保留原数据盘、设备 serial 和 ADB 服务。对已运行的实例，先正常关机再以窗口模式启动。可把同一启动入口放到桌面和用户登录启动目录。相关选项见 [Android Emulator 命令行说明](https://developer.android.com/studio/run/emulator-commandline)。

使用 `scripts/android_window.py` 可以在 Windows 固定模拟器位置。指定 `--exe` 和完整的 `--title`，默认停在主屏幕右上方。可以用 `--position X Y` 指定桌面坐标，或加 `--watch` 监控窗口并在拖动结束后归位。截图和触摸使用设备坐标。设备窗口被其他应用遮挡时，仍可通过 ADB 观察设备。

需要独立桌面窗口和较新的安卓版本时，可使用 MuMuPlayer 的 Android 15 实例。先在 BIOS 开启硬件虚拟化，再验证独立窗口、目标应用、ADB 回环连接、实际截图和触摸。已在 Windows 上实测 MuMuPlayer 6.8.9 的 Android 15 实例，包含 720×1280 截图、鼠标操作和自动归位。[系统要求](https://www.mumuplayer.com/help/win/minimum-system-request-mumu-windows.html) · [Android 15 支持](https://www.mumuplayer.com/help/win/how-to-upgrade-mumuplayer.html) · [ADB 说明](https://www.mumuplayer.com/help/win/developers-essentials-manual.html)

桌面模拟器接入时，设置 `agentTransport: "desktop_loopback"`，用实例实际回环端口填 `serial`，例如 `127.0.0.1:16384`。`adbServerPort` 填另一个独立本机端口。用该端口执行 `adb -P <独立服务端口> connect <serial>`，确认 `get-state` 和 `sys.boot_completed` 后启动 worker。窗口自动归位使用同一实例的实际可执行文件和完整标题。

桌面设备断线时，请在模拟器恢复后重新截图。worker 会对同一回环设备尝试重连，两次重连至少间隔十秒。先看新截图确认当前界面，再决定下一步。断线前已提交的点击、滑动或应用启动结果可能未知。

确认应用已安装后，可在 `android.agentApps` 里启用 `amap`（高德）、`meituan`（美团）或 `qq`（QQ）。主 Agent 在主私聊或可信自主轮选择 `tool_action`，先看截图，再用 `android_phone({action:"open_app",app:"amap",expected_shot_id:"<本轮截图 ID>"})`。查看新图确认前台应用后继续点击或滑动。确认搜索框已聚焦后，可以用 `type_text` 输入最多 160 个可打印 ASCII 字符，再看新截图确认输入。首次使用协议和账号登录由用户在应用界面处理。

桌面模拟器的启动命令先解析应用的启动 Activity，再等待 Android 确认目标应用显示。启动结果未确认时，重新截图观察当前界面后再决定下一步。

项目配置示例：

```json
{
  "android": {
    "agentEnabled": false,
    "serial": "emulator-5556",
    "adbPath": "",
    "adbServerPort": 5037,
    "maxShotAgeSeconds": 30
  }
}
```

默认 serial 为 `emulator-5556`。`adbPath` 留空时使用工作区 `android/platform-tools/adb.exe`（非 Windows 使用 `adb`）。配置路径、工作区和角色数据目录沿用 `project_conf`。ADB 子进程使用工作区 `android/temp` 和配置的本机 ADB 服务端口。每条设备命令都带 `-s <固定serial>`。

## 主 Agent 实玩

用已配置的 ADB 确认模拟器实际开机，再启动 `androidd.py`。运行副本需带同版的 `android_executor.py`、`android_panel.py`、能力指南和 OneBot 策略适配器。确认面板可以抓图后，再为该专用模拟器设置 `agentEnabled: true`。使用 QEMU 转发时，可配置 `agentTransport: "qemu_loopback"`、固定 `serial: "127.0.0.1:15555"` 和独立 `adbServerPort: 5039`。来宾应使用隔离网络，ADB 转发仅绑定本机回环地址。远端地址与默认 ADB 服务不适用于此转发配置。

项目提供原生离线 2048。安装 JDK 21、Android SDK Platform 35 和 Build Tools 35.0.0 后，可以从仓库构建。输出目录须是新目录，调试密钥与生成文件留在工作区的私有目录。

```powershell
python -B scripts/build_android_game.py --jdk '<JDK 21 目录>' --sdk '<Android SDK 目录>' --output '<新构建目录>' --keystore '<本地调试密钥文件>'
adb -P 5037 -s emulator-5556 install '<新构建目录>/lanya-2048.apk'
```

同一主 Agent 在主私聊或可信自主活动中选择 `tool_action`，调用 `android_phone({action:"shot"})` 看随结果返回的 PNG。附件服务不可用时，可用 `read_image` 读取结果里的路径。用当前轮的图 ID 打开 `open_game`，观察游戏实际进入前台，再用 `swipe` 或 `tap` 操作。每步返回独立的 PNG 和新 ID。动作结果未知时重新截图，依据屏幕决定下一步。

登录页挡住实际任务时，`request_login` 将当前应用和简短目的保存在主私聊的本机状态中。之后的自主轮可通过 `ask_owner` 请求用户在模拟器登录。发送沿用该私聊的正常守卫，状态不明时停止重试。用户完成后，主 Agent 打开应用并核对新画面，通过 `login_resolved` 继续任务。

主私聊的精简提示会介绍已启用的原生安卓工具，截图按需获取。自主活动指南在专用 worker 持锁时把离线 2048 列入可选活动，由主 Agent 决定何时尝试并通过新截图确认设备和棋盘状态。

## 视觉队列

运行 `python androidd.py --setup` 只抓一张验证图并退出。无参数立即消费队列，`--once` 最多消费一个现有 job。worker 启动时使先前截图失效，并保留已完成的序号。队列使用角色目录的 `android/cmd.json`、`result.json`、`shot.png`、`shot-meta.json` 和 OS worker 锁。生产者须原子发布到空槽。

主 Agent 请求从入队起共用 45 秒设备操作期限。过期请求停止，已经提交的输入如果缺少新截图，按结果未知处理，再从真实截图判断下一步。

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

面板队列的 `shell` 支持只读 `getprop`、单服务 `dumpsys`、`wm size/density`、`settings get/list`、`pm list`。文本参数按远端 shell 规则加引号。主 Agent 工具提供截图、点击、滑动、返回、主页、打开固定游戏、打开已配置应用和输入查询。
