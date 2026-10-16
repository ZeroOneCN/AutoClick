# AutoClick - 安卓自动化工具（GUI 版）

基于 ADB 的安卓自动化工具：在 PC 侧维持一条持久化的 `adb shell` 通道，把
`input tap X Y` 等注入命令直接送进手机端 shell 执行，从而把单次点击开销从
「每次新建 adb 连接（约 135ms）」降到「复用通道（约 24ms）」。

- 开发平台：Windows
- 运行依赖：源码运行需 Python 3.10+（tkinter 为标准库）；**交付形态为免环境独立 EXE**
- 手机要求：开启 USB 调试并授权；**无需 root，无需在手机上安装任何 App**

---

## 一、项目规划

### 1.1 目标

提供一个**图形界面**的安卓自动化工具，支持：固定坐标高频连点、多点流程编排与回放、
操作录制回放、找控件/找图点击；可随时停止，并最终以**免环境独立 EXE**交付
（目标机器无需安装 Python 与任何依赖）。

### 1.2 范围

**一期（已交付，入口已于 M6 移除）**

| 项目 | 内容 |
|---|---|
| 形态 | 命令行（CLI） |
| 点击方式 | 纯坐标点击（`input tap`） |
| 场景 | 固定位置高频连点 |
| 坐标获取 | 内置 `--pick` 拾取模式：读取手机 `getevent` 触摸流并换算为屏幕像素坐标 |

**二期（实施中）**：GUI 控制界面（tkinter）、录制回放、多点流程、找图点击；
原 CLI 入口 `autoclick.py` **已移除**（逻辑并入 `core/`），最终产出**免环境独立 EXE**。
方案见 [docs/二期扩展/DESIGN_二期扩展.md](docs/二期扩展/DESIGN_二期扩展.md)。

仍然不做：随机抖动、多设备并发、多指手势（仅单指点击/滑动）。

### 1.3 验收标准（二期）

1. GUI 双击可启动，能自动发现并连接设备，显示分辨率。
2. GUI 可完成一期全部能力：单点连点、间隔/次数/时长设置、坐标拾取、随时停止。
3. 能创建 ≥5 个步骤的流程（含 `tap/swipe/sleep/text`），保存为 JSON 并可重新加载执行。
4. 录制模式：在手机上完成「点击 → 滑动 → 再点击」，能生成对应步骤并正确回放。
5. 找控件点击：按文本/ID 定位并点击成功。
6. 执行期间界面不卡死，日志实时刷新，停止后手机端无残留注入。
7. 坐标越界、设备掉线、模板文件缺失等异常均有中文提示。
8. 打包产物 `AutoClick.exe` 在**未安装 Python 的 Windows 机器**上双击可运行。

### 1.4 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 环境探测（adb、设备、分辨率、注入权限） | 已完成 |
| M2 | 核心机制验证（持久 shell + 同步标记） | 已完成 |
| M3 | CLI 工具实现并通过实机验证 | 已完成 |
| M4 | 文档整理与交付说明 | 已完成 |
| M5 | 坐标拾取能力（`--pick`） | 已完成 |
| M6 | 二期：核心层抽取并移除 CLI（`core/device.py`、`core/actions.py`） | 已完成 |
| M7 | 二期：GUI 骨架（连接 / 连点 / 日志 / 停止） | 已完成 |
| M8 | 二期：多点流程（步骤编辑 + JSON 存取 + 执行引擎） | 已完成 |
| M9 | 二期：录制回放 | 已完成 |
| M10 | 二期：找控件点击（零依赖） | 已完成 |
| M11 | 二期：找图点击（依赖 OpenCV，EXE 版内置） | 已完成 |
| M12 | 二期：打包为免环境独立 EXE（内置 Python 运行时 / OpenCV / adb） | 已完成 |

> Q1~Q6 决策点已于 2026-10-14 确认（Q4 移除 CLI、Q5 打包免环境独立 EXE），后续按 M7 → M12 顺序实施。

---

## 二、实施策略

### 2.1 技术方案（含选型依据）

| 方案 | 实测单次开销 | 结论 |
|---|---|---|
| 每次点击单独调用一次 adb | 约 135 ms（约 7 次/秒） | 太慢，不采用 |
| 手机端 shell 内 `while` 循环 | 约 31 ms/次 | 快，但停止后易残留孤儿循环，不采用 |
| **持久 shell + 回显标记同步（采用）** | **约 24 ms（约 41 次/秒）** | 快且可精确控制、可随时停止 |

同步机制：PC 向通道写入 `input tap X Y; echo <MARK>`，只有读回 `<MARK>`
才判定该次点击已执行完毕。这样既不会让命令在管道里堆积，也保证停止后
不会有排队中的点击继续执行；关闭通道 stdin 时手机端 shell 读到 EOF 自然退出，
不会残留循环进程（已实机验证退出码为 0）。

界面层选型 **tkinter/ttk**（Python 标准库）：零安装、体积小、可被 PyInstaller
直接打包，延续项目「低依赖」基调。

### 2.2 目录结构

```
e:\Code\AutoClick\
├── README.md                    # 本项目文档（规划 / 策略 / 进度）
├── main.py                      # GUI 入口（唯一入口，M7 交付）
├── .gitignore                   # Git 忽略规则（build/、dist/ 与运行时用户数据不入库）
├── build.spec                   # PyInstaller 打包配置（M12 完成）
├── build.ps1                    # 打包脚本（完整版 / 轻量版，M12 完成）
├── dist\AutoClick.exe           # 打包产物（免环境独立 EXE，M12 完成，不入库）
├── resources\adb\               # 打包内置的 adb 三件套（M12 完成，入库）
│   ├── adb.exe
│   ├── AdbWinApi.dll
│   └── AdbWinUsbApi.dll
├── core/                        # 核心层（无 UI 逻辑）
│   ├── __init__.py
│   ├── device.py                # adb / ShellChannel / TouchStream / 截屏 / 资源定位（M6 完成）
│   ├── actions.py               # 原子动作（M6 完成）
│   ├── flow.py                  # 步骤模型 + 执行引擎（M8 完成）
│   ├── recorder.py              # 录制器（M9 完成）
│   ├── vision.py                # 找控件（M10 完成）/ 找图（M11 完成）
│   └── storage.py               # 流程 JSON 读写（M8 完成）
├── gui/
│   └── app.py                   # tkinter 界面（M7 起逐步完善）
├── flows/                       # 用户流程文件（*.json，运行时自动创建）
├── templates/                   # 找图模板图片（*.png，运行时自动创建）
└── docs\二期扩展\
    └── DESIGN_二期扩展.md        # 二期扩展设计方案
```

### 2.3 使用说明（GUI）

> GUI 入口 `main.py` 在 M7 交付，之后通过双击 `AutoClick.exe`（或
> `python main.py` 源码运行）启动。界面规划分区：

```
┌───────────────────────────────────────────────────────┐
│ [设备] adb路径/浏览/自动探测                            │
│        设备下拉 [刷新设备] [连接]  分辨率 1080x2400     │
├──────────────┬────────────────────────────────────────┤
│ 左：步骤列表  │ 右：步骤参数编辑区                       │
│ · tap 540,2000│  类型: tap                              │
│ · swipe ...   │  X: 540   Y: 2000  间隔: 0.5  次数: 3   │
│ · find_click  │  [拾取坐标(手机点一下)] [上移] [下移] [删除]│
├──────────────┴────────────────────────────────────────┤
│ [开始执行] [停止]          （流程编排页底部按钮栏）       │
│ [开始] [停止] [录制开始/停止] [保存流程] [加载流程]        │
├───────────────────────────────────────────────────────┤
│ 日志区（自动换行多行显示，带时间戳）        [清]          │
│                                            [空]          │
│                                            [日]          │
│                                            [志]          │
└───────────────────────────────────────────────────────┘
```

界面细节：字体统一为**微软雅黑**（`gui/app.py` 的 `UI_FONT`，覆盖 ttk 控件与步骤列表；
日志区用 11 号并加了行距，长时间查看更清晰）。**窗口固定 960x720、不可拉伸**；
「连接」按钮紧跟「刷新设备」放在同一行；日志区**超长行自动折行为多行显示**
（`wrap="word"`），无需横向滚动；「清空日志」按钮为**竖排文字**（自上而下读）。
分页区与日志区之间是**可拖动的分隔条**，日志区默认 10 行。

**设备实时测活**：连接成功后自动启动后台监测线程，每 3 秒枚举一次 adb 设备表
（未连接时空转等待，零 adb 开销），一旦设备掉线立即在日志区红字提示
「检测到设备连接断开」并把状态栏改为「设备已断开」，恢复在线后再提示
「设备已恢复连接」并还原状态栏——避免 USB 松动 / 授权失效导致任务中断却毫无察觉。

**流程编排使用步骤**

1. 先「连接」设备，切到「流程编排」页；
2. 点左上「添加步骤 ▾」选择步骤类型（点击 / 长按 / 滑动 / 等待 / 文本 / 按键 /
   启动应用 / 找控件点击 / 找图点击 / 循环开始 / 循环结束）；
3. 在左侧列表选中该步骤，右侧编辑参数：坐标类步骤点「拾取坐标」后直接在手机上
   点一下自动回填；找图步骤用「抓屏存为模板…」把当前手机屏幕截存为模板；
4. 改完点「应用修改」（参数摘要同步到左侧列表）；
5. 需要重复时：把要循环的步骤用「循环开始 / 循环结束」成对包住（次数填 0 表示
   无限循环）；整条流程的总重复次数填顶部「重复(0=∞)」；
6. 点页面底部「开始执行」运行、「停止」随时中断；「保存 / 加载」把流程存为
   `flows/*.json` 复用。

**运行时不弹控制台窗口**：打包为 windowed EXE 后 GUI 自身没有控制台，但每次调用 `adb.exe`
（枚举设备、截屏、shell、getevent）若不加限制会新建一个控制台窗口，表现为黑色 cmd 一闪而过。
`core/device.py` 定义了模块级常量 `NO_WINDOW_FLAGS`（值为 `CREATE_NO_WINDOW`，非 Windows
平台退化为 0），所有 subprocess 调用（共 5 处，含 `core/vision.py` 的控件树 dump）都带上它，
子进程因此在无窗口模式下运行。

核心层当前已具备的能力（可被后续 GUI 直接调用）：

| 模块 | 能力 |
|---|---|
| `core/device.py` | 定位 adb、枚举设备、查询分辨率、持久 shell 通道、触摸流采集、截屏、内置资源/用户数据目录定位 |
| `core/actions.py` | `tap / long_press / swipe / text / keyevent / start_app / sleep` |
| `core/flow.py` | 步骤模型（10 种类型）、参数校验、执行引擎（循环 / 重试 / 超时 / 可停止） |
| `core/storage.py` | 流程 JSON 读写、`flows/` 与 `templates/` 目录定位 |
| `core/recorder.py` | 手势识别（位移阈值判点击/滑动）、Douglas-Peucker 轨迹抽稀、相对延时转 sleep 步骤 |
| `core/vision.py` | 找控件（`uiautomator dump` 解析控件树，按文本/ID/描述定位，零依赖）、找图（截屏 + OpenCV 模板匹配，可选 ROI 与阈值，未装 OpenCV 时降级提示）、抓屏存为模板 |

**adb 定位顺序（`core/device.py` 的 `find_adb`）**：① 界面上显式指定的路径
② EXE 内置 `resources/adb/adb.exe`（源码运行时即项目内 `resources\adb\`）
③ `ADB_PATH` 环境变量 ④ 常见安装路径（`D:\CodeDevelop\Env\adb\adb.exe`、
`D:\CodeDevelop\Env\AndroidSDK\platform-tools\adb.exe` 等）⑤ 系统 PATH。
项目的 adb 三件套已**单独安装**到 `D:\CodeDevelop\Env\adb\`（`adb.exe` / `AdbWinApi.dll` /
`AdbWinUsbApi.dll`），因此**不依赖 Android SDK** 也能运行与打包。

**流程 JSON（`flows/*.json`）**：`{"name":…, "screen":{宽,高}, "steps":[…], "control":{"repeat":1,"stop_on_error":true}}`，
步骤类型为 `tap / long_press / swipe / sleep / text / keyevent / start_app / find_click / loop / endloop`。
循环以 **`loop` 与 `endloop` 成对**的扁平标记表示（GUI 中即「循环开始 / 循环结束」两个步骤），
支持嵌套；`loop` 的 `mode` 取 `count`（`times` 次，0 表示无限）或 `duration`（`duration` 秒）。
整条流程的重复由 `control.repeat` 控制（0 表示无限）。

**打包为免环境独立 EXE（M12 完成）**

在项目根目录执行打包脚本（需先 `pip install pyinstaller`；完整版另需 `opencv-python`、`numpy`）：

```powershell
.\build.ps1            # 完整版：内置 OpenCV，支持「找图」→ dist\AutoClick.exe（约 61.4 MB）
.\build.ps1 -Light     # 轻量版：不含 OpenCV，仅「找控件」→ dist\AutoClick_Lite.exe
```

- **内置内容**：Python 运行时 + tkinter/tcl/tk、OpenCV + numpy（完整版）、adb 三件套（`resources\adb\`）；
  adb 以 `data` 形式打入，运行时释放到 `sys._MEIPASS/resources/adb/`，由 `core/device.py` 的
  `bundled_adb()` 优先探测，因此目标机器**无需 Android SDK**；
- **用户数据不打包**：`flows\`（流程 JSON）与 `templates\`（找图模板）在 EXE 同级目录首次运行时自动创建，
  可写、可备份、升级 EXE 不丢数据；
- **实测**：产物在 PATH 中移除 Python、清空 `PYTHONHOME/PYTHONPATH/ADB_PATH` 的环境下仍能正常启动 GUI，
  即**目标机器无需安装 Python 与任何依赖**，双击即可运行。

### 2.4 已解决的兼容性问题

- **Windows 下 `select` 不支持管道**：改用「读取线程 + 超时看门狗」，超时即终止通道，避免读线程悬挂。
- **adb 输出编码**：显式以 UTF-8 解码并容错替换，避免中文路径/输出乱码。
- **权限报错可见性**：把 shell 的 stderr 合并到 stdout，一旦手机端因权限不足拒绝注入，会立即上报而非静默失败。
- **截屏走 `exec-out`**：不经过 shell 文本模式，避免 Windows 换行符损坏 PNG。
- **vivo 机型注意**：如遇点击无效，需在开发者选项中额外开启「USB 调试（安全设置）」。本次实测 vivo X100s（Android 16）仅开 USB 调试即可正常注入。

### 2.5 实测数据（vivo X100s / Android 16 / 1080x2400）

| 指标 | 实测值 |
|---|---|
| 单次 adb shell 往返 | 135 ms |
| 持久通道单次点击 | 24.4 ms |
| 全速模式实测频率（100 次） | 41.8 次/秒 |
| 间隔 30ms 模式实测频率（30 次） | 29.5 次/秒 |
| M6 核心层迁移后连点频率（100 次） | 38.8 次/秒（动作层回归） |
| 点击生效率 | 已验证 |
| 触摸设备节点 | `/dev/input/event6`（vivo_ts），原始坐标范围 12599 x 27999 |
| 坐标换算校验 | 原始 (6300, 14000) → 屏幕 (540, 1200)，换算准确 |
| 打包产物（完整版） | `dist\AutoClick.exe` 61.4 MB（含 Python 运行时 / tkinter+tcl / OpenCV / numpy / adb 三件套） |
| 打包产物（轻量版） | `dist\AutoClick_Lite.exe` 14.4 MB（不含 OpenCV，仅「找控件」） |
| 免环境启动验证 | 在 PATH 移除 Python、清空 `PYTHONHOME/PYTHONPATH/ADB_PATH` 后，两个版本 GUI 均正常启动（完整版最新实测启动 6.8 秒） |
| 子进程窗口隐藏验证 | 以无控制台的 `pythonw` 作父进程对比：不带标志时 conhost 由 14 增至 15（新建了 cmd 窗口），带 `CREATE_NO_WINDOW` 时保持 14（无窗口）；打包 EXE 启动期间 conhost 13→13 |
| 小窗口布局验证 | 720x540 下各区域正常：分页区 242px、日志区 155px；流程页步骤列表 414px / 参数区 264px；录制页列表 683px |

### 2.6 如何获取点击坐标

两种方式任选其一，**坐标系都与 `input tap` 完全一致，均无需手工换算**。

- **方式一：GUI「拾取坐标」按钮（M7 起）** —— 点按钮后在手机上点击目标位置，
  程序实时把触摸原始坐标换算为屏幕像素坐标并回填到当前步骤。
- **方式二：开发者选项「指针位置」（零依赖，最直观）** —— 手机：设置 → 系统管理 →
  开发者选项 → 打开「指针位置」；在屏幕上点击目标位置，屏幕左上角会直接显示
  `X / Y` 像素值，填入即可。

> 原理：触摸屏上报的是**原始触摸坐标**（本机为 0~12599 × 0~27999），程序读取
> `getevent` 触摸事件后按 `屏幕尺寸 ÷ 原始最大值` 线性换算成屏幕像素坐标
> （本机比例约 11.6667）。注意 `getevent` 只能捕获**真实手指触摸**，
> 程序自己注入的点击不会出现在该数据流里（因此录制不会自激）。

---

## 三、进度跟踪

| 日期 | 任务 | 状态 | 结果摘要 |
|---|---|---|---|
| 2026-10-14 | 环境探测 | 完成 | 定位 adb（Android SDK platform-tools 35.0.2）；识别设备 vivo X100s（序列号 10AE5M0S53000UT）；确认分辨率 1080x2400 |
| 2026-10-14 | 连通性排查 | 完成 | 初次连接时手机未暴露 ADB 接口（仅 MTP/USB存储），指导开启 USB 调试后 `adb devices` 正常识别 |
| 2026-10-14 | 核心机制验证 | 完成 | 验证持久 shell + 回显标记方案：24.4ms/次（约 41 次/秒），关闭 stdin 后手机端 shell 干净退出（退出码 0） |
| 2026-10-14 | 实现一期 CLI | 完成 | 单文件 CLI，支持坐标/间隔/次数/时长/序列号/adb 路径/设备检查；修复握手未发送命令的缺陷 |
| 2026-10-14 | 实机验证 | 完成 | 全速 41.8 次/秒、间隔 30ms 时 29.5 次/秒；点击真实生效 |
| 2026-10-14 | 文档整理 | 完成 | 输出 README（规划/策略/进度三部分） |
| 2026-10-14 | 新增坐标拾取模式 | 完成 | `--pick` 解析 `getevent` 触摸流并按 `12599x27999 → 1080x2400` 换算输出屏幕坐标 |
| 2026-10-14 | 二期方案设计 | 完成 | 输出 [DESIGN_二期扩展.md](docs/二期扩展/DESIGN_二期扩展.md)：分层架构、模块与接口、流程 JSON 数据模型、录制/找图流程、M6~M12 里程碑、风险对策、验收标准 |
| 2026-10-14 | 二期方案确认 | 完成 | Q1 tkinter、Q2 找控件为主+找图可选、Q3 仅单指、**Q4 移除 CLI**、**Q5 打包免环境独立 EXE**、Q6 JSON |
| 2026-10-14 | **M6 核心层抽取** | 完成 | 新增 `core/device.py`（adb 定位含内置 adb 优先、设备/分辨率、`ShellChannel`、`TouchStream` 触摸流、`screencap_png`、`resource_dir/app_dir`）与 `core/actions.py`（tap/long_press/swipe/text/keyevent/start_app/sleep）；临时自测脚本回归通过（连点 38.8 次/秒、原始(6300,14000)→屏幕(540,1200)、触摸流真机启停正常）；按 Q4 **删除一期 CLI `autoclick.py`** |
| 2026-10-14 | **M7 GUI 骨架** | 完成 | 新增 `main.py`（唯一入口，含打包路径引导）与 `gui/app.py`：设备栏（adb 路径浏览/自动探测、设备下拉、分辨率、连接）、固定坐标连点页（X/Y、间隔/次数/时长、拾取坐标、开始/停止）、实时日志区（带时间戳与着色）、状态栏；后台线程 + `queue` + `root.after` 轮询，禁止跨线程操作控件；窗口关闭时释放通道与触摸流。冒烟测试（控件构建 + 事件循环）通过 |
| 2026-10-14 | **M8 多点流程** | 完成 | 新增 `core/flow.py`（10 种步骤模型、`loop/endloop` 成对扁平循环、重试/超时/可停止的执行引擎、流程校验）与 `core/storage.py`（`flows/`+`templates/` 定位、JSON 读写 UTF-8）；GUI 新增「流程编排」页：添加/删除/复制/上移下移、左侧步骤列表 + 右侧按类型动态参数编辑器、坐标拾取回填、新建/保存/加载、开始执行/停止。冒烟测试全部通过（循环体执行次数、无限点击可停止、loop 未闭合与坐标越界校验、JSON 往返含中文、GUI 增删改与 from/to 映射） |
| 2026-10-14 | **M9 录制回放** | 完成 | 新增 `core/recorder.py`（基于 `TouchStream` 采集真实手指触摸；位移阈值判 tap/swipe、Douglas-Peucker 轨迹抽稀、相邻手势时间差转 `sleep` 步骤、`to_flow` 生成流程）；GUI 新增「录制回放」页：开始/停止录制、步骤列表、清空、一键转流程（覆盖流程页）、立即回放。冒烟测试全部通过（直线抽稀保留首尾、折线保留拐点、`tap→sleep→swipe` 识别、tap 取抬指坐标、相对延时≈0.5s、录制结果写入流程页并同步列表、立即回放生成流程） |
| 2026-10-14 | **M10 找控件点击** | 完成 | 新增 `core/vision.py`：`Locator.locate(step, timeout, stop_event)` 统一入口；方式 A 用 `uiautomator dump` 导出控件树并解析 `bounds`，支持 `text:` / `id:` / `desc:` / `cls:` 前缀与无前缀模糊匹配，自动屏蔽禁用节点、可点击节点优先；方式 B（找图）截屏 + OpenCV 模板匹配，可选 ROI，未装 OpenCV 时中文降级提示；`capture_template` 供 GUI 抓屏存模板。GUI：`_flow_worker` 接入识别器、`find_click` 参数区新增「选择模板…」「抓屏存为模板…」。自测 17/18 通过（单元用例 13 项全过：bounds 解析、文本/ID/描述匹配、禁用节点排除、可点击优先）；真机端到端通过：打开设置页 → 按文本定位「家人共享」(586,606) → 点击 → 界面切换成功 |
| 2026-10-14 | **M11 找图点击** | 完成 | 截屏（`exec-out screencap`）→ OpenCV `matchTemplate`(TM_CCOEFF_NORMED) → 按阈值取最大相似度、返回模板中心坐标；支持可选 `roi` 限定搜索范围（越界自动回退整屏）；模板用 `np.fromfile + imdecode` 读取以兼容中文路径；未装 OpenCV 或模板缺失均给出中文提示并降级为未命中。真机验证 10/10 通过：命中且坐标与原位一致（相似度 1.000）、ROI 越界不误命中、跨页面模板不误命中（相似度 -0.033）、随机噪点模板不命中、模板缺失中文提示、模拟缺 OpenCV 降级提示、引擎 `find_click(image)` 步骤端到端完成「定位→点击」；顺带修复识别器在无 `stop_event` 时忙循环刷日志的缺陷（引入 0.3s 重试间隔） |
| 2026-10-14 | **M12 打包免环境 EXE** | 完成 | 新增 `resources\adb\`（内置 adb 三件套）、`build.spec`（PyInstaller 配置，`AUTOCLICK_LIGHT` 环境变量切换完整版/轻量版）、`build.ps1`（前置校验 → 打包 → 失败自动重试一次）；产物 `dist\AutoClick.exe`（完整版 **61.5 MB**，内置 Python 运行时 + tkinter/tcl/tk + OpenCV/numpy + adb）与 `dist\AutoClick_Lite.exe`（轻量版 **14.4 MB**，不含 OpenCV）。验证 **9/9 通过**：归档内含 adb 三件套与 `cv2.pyd`/numpy；在 PATH 移除 Python、清空 `PYTHONHOME/PYTHONPATH/ADB_PATH` 的环境下，完整版与轻量版 GUI 均能启动（窗口标题 `AutoClick - 安卓自动化工具`）；运行时解包目录存在 `resources\adb\adb.exe`（即 `bundled_adb()` 的探测路径）。首次打包曾因杀毒软件实时扫描短暂锁定输出文件导致 PE 校验和写入失败，脚本已加入重试，重跑即成功 |
| 2026-10-14 | **重新打包交付 EXE** | 完成 | 按需重跑 `build.ps1`，重新生成 [dist/AutoClick.exe](file:///e:/Code/AutoClick/dist/AutoClick.exe)（完整版 **61.5 MB**）；在 PATH 移除 Python、清空 `PYTHONHOME/PYTHONPATH/ADB_PATH` 的环境下验证：GUI 6 秒内启动（窗口标题 `AutoClick - 安卓自动化工具`），运行时解包目录含 `resources\adb\adb.exe`；打包缓存 `build\`（68.2 MB）已清理 |
| 2026-10-14 | **二期收尾：Git / adb 独立安装 / 界面优化** | 完成 | ① 项目纳入 Git 并推送 `https://github.com/ZeroOneCN/AutoClick.git`（新增 `.gitignore`：`build/`、`dist/` 与运行时 `flows/`、`templates/` 不入库，内置 adb 三件套入库）；② adb 三件套单独安装到 `D:\CodeDevelop\Env\adb\`（`adb version` 1.0.41 / 35.0.2，`adb devices` 正常识别 10AE5M0S53000UT），并把该路径加入 `find_adb` 常见路径探测；③ GUI 全局字体统一为**宋体**（含 ttk 控件、Treeview 行与表头，行高 24），窗口默认 1180x880、最小 1040x720；④ 分页区与日志区改为**可拖动分割面板**，日志默认 16 行并可拉伸，各页留白压缩、清空日志按钮不再独占一行；GUI 冒烟 5/5 通过（字体、日志行数、窗口尺寸、分割面板、adb 预填） |
| 2026-10-14 | **界面体验优化 + 重新打包** | 完成 | ① **隐藏 cmd 窗口**：`core/device.py` 新增模块常量 `NO_WINDOW_FLAGS`（`CREATE_NO_WINDOW`，非 Windows 退化为 0），`run_adb` / `screencap_png` / `ShellChannel` / `TouchStream` 与 `core/vision.py` 的控件树 dump 共 5 处 subprocess 调用全部带上，消除调用 adb 时闪出的黑色控制台窗口；实测（无控制台的 `pythonw` 作父进程）：不带标志 conhost 14→15、带标志 14→14，打包 EXE 运行期间 conhost 13→13；② **字体清晰化**：界面字体由点阵式「宋体」改为带 ClearType 微调的**微软雅黑**，日志用 11 号并设 1/2/1 行距；③ **布局优化 + 小窗口支持**：分页区与日志区改按 2:1 分配，窗外边距与各页 `pady` 全面压缩，固定坐标页参数框填满整页以消除中间大片空白，窗口默认 **1040x740**、**最小 720x540**，adb 路径框 / 设备下拉 / 流程名输入框 / 步骤列表与参数区均随窗口自动收窄（720x540 实测：分页区 242px、日志区 155px、流程页列表 414px / 参数区 264px）；④ 重新打包 [dist/AutoClick.exe](file:///e:/Code/AutoClick/dist/AutoClick.exe)（61.5 MB），免 Python 环境 6.8 秒启动、运行期间无任何控制台窗口 |
| 2026-10-16 | **修复录制连点误判为滑动** | 完成 | 原因：① 快速连点时手指未完全抬离屏幕，触摸屏不上报抬指事件（`ABS_MT_TRACKING_ID=ffffffff`），第二次点击坐标以 move 形式并入当前轨迹，从第一击位置"滑"到第二击位置，首尾位移超阈值被判为滑动；② 快速点击时指肚落屏瞬间滚动漂移 20~50px，超过 20px 的滑动判定阈值。修复（`core/recorder.py`）：① 新增跳变切分——相邻采样间隔 <25ms 且位移 >60px（真实人手滑动物理上不可能）视为"半抬再按"，先把当前轨迹收尾为一次手势再从跳变点开始新手势；② 新增短促容差——手势时长 <300ms 且位移 <60px 时即使有小幅漂移仍判为点击。回归测试 7/7 通过（连点切分、短促漂移、正常滑动、快速甩动、同位置双击合并、sleep 间隔、切分后收尾），正常滑动/快速甩动不受影响 |
| 2026-10-16 | **窗口固定 + 界面微调 + 实时测活 + 重新打包** | 完成 | ① **窗口固定 960x720**（`resizable(False, False)`，不可拉伸）；② 设备栏重排：**「连接」按钮移到「刷新设备」旁**（同一行相邻），分辨率信息右移；③ **「清空日志」按钮改竖排文字**（清/空/日/志 自上而下）；④ **日志区超长行自动折行多行显示**（`wrap="word"`），移除横向滚动条；⑤ **新增设备实时测活**：连接成功后后台线程每 3 秒枚举 adb 设备表比对在线状态（未连接时空转、adb 瞬时异常不误报），掉线即时日志红字「检测到设备连接断开」+ 状态栏「设备已断开」，恢复后提示「设备已恢复连接」；⑥ **修复流程编排页「开始执行」按钮被挤出可视区**：底部按钮栏改为先 `side="bottom"` 占位再铺步骤编辑区；⑦ `build.ps1` 补 UTF-8 BOM（修复 PowerShell 5.1 按 ANSI 解析无 BOM 中文脚本报"缺少 closing '}'"的打包阻塞问题）；GUI 冒烟 **12/12 通过**；重新打包 [dist/AutoClick.exe](file:///e:/Code/AutoClick/dist/AutoClick.exe)（**61.4 MB**），免 Python 环境启动验证通过（窗口标题正常）；打包缓存 `build\` 已清理 |

### 待办 / 待确认

- [x] 二期方案决策点（Q1~Q6）已于 2026-10-14 确认：Q4 移除 CLI、Q5 打包为免环境独立 EXE。
- [x] M6 核心层抽取完成并移除 CLI。
- [x] M7 GUI 骨架完成（`main.py` + `gui/app.py`），可用 `python main.py` 启动。
- [x] M12 打包完成，二期 M6~M12 **全部完成**；交付物 [dist/AutoClick.exe](file:///e:/Code/AutoClick/dist/AutoClick.exe)（免环境独立 EXE，双击即可运行）。
- [x] 已纳入 Git 仓库并推送远程：<https://github.com/ZeroOneCN/AutoClick.git>（`main` 分支）。
