# SparkKeeper

**火花守夜人** · 在你自己的电脑上，替你守住那点微弱的小火苗。

扫码一次，之后每天到点自动发一条 —— 用真实的浏览器会话，不碰你的密码，不逆向任何接口。

</div>

---

> A local-first console that keeps your Douyin chat streak alive.
> It drives a real Chromium session that **you** log into by scanning a QR code, reads your own conversation list, and sends messages you wrote yourself — on a schedule you set.
> No password handling, no protocol reverse engineering, no bulk spam.

## 它是什么，不是什么

| 是 | 不是 |
|---|---|
| 一个跑在 `127.0.0.1` 的本地网页控制台 | 一个云端服务（你的数据不出本机） |
| 用 Playwright 驱动**你自己登录**的浏览器 | 破解登录、伪造签名、抓取他人数据 |
| 你勾人、你写话术、你定时间 | 无上限群发、自动加好友、自动养号 |
| 把「每天发一句」这件事交给机器 | 能单方面续火花（对方不回，火花照样断） |

## 功能

- **扫码即用**：有头 Chromium + 持久化 profile，登录态存本地，扫一次管很久
- **只读取会话列表**：登录后不会擅自发送任何内容，发送必须由你点按钮触发
- **勾谁发谁**：左侧列表支持搜索、全选，只发你勾中的人
- **话术池**：多行话术，随机取一条或固定用第一条
- **刹车内置**：单次上限 20 人、最小间隔 8 秒、提交前二次确认
- **每日定时**：设好后每天到点自动跑一轮，同一任务每天只触发一次
- **选择器自校准**：一键导出当前页面截图与 HTML，方便定位改版后的 DOM

## 快速开始

```bash
# 1. 依赖（用哪个 Python 就装到哪个里）
python -m pip install playwright
python -m playwright install chromium
```

```text
# 2. Windows 直接双击
run.bat
```

3. 浏览器自动打开 `http://127.0.0.1:8765/`（没弹出就手动输入）
4. 右上角 **扫码登录** → 在弹出的独立 Chromium 窗口里扫码
5. 回到网页点 **刷新联系人** → 勾人 → 写话术 → **开始发送**

关闭那个命令行窗口 = 停止服务。

> macOS / Linux 没有 `run.bat`，直接 `python3 server.py` 即可。

## 每日定时

1. 勾好联系人、写好话术、拖好间隔
2. 在「每日定时自动发送」里选时间，点「保存为每日任务」
3. 到点自动发一轮，列表会标记「今天已执行」

生效条件（缺一不可）：

- 服务在跑，电脑没关机/睡眠
- 浏览器登录态仍有效（掉登录要重新扫码）
- 到点时若有手动任务在发，当天跳过，不并发
- 每个任务每天只触发一次，去重记录在 `runtime/schedule.json` 的 `last_run`

## 文件结构

```text
SparkKeeper/
├── run.bat              # Windows 启动入口：探测可用的 Python 解释器
├── server.py            # 本地 HTTP 服务 + Playwright 自动化 + 定时调度
├── static/index.html    # 网页控制台
├── config.json          # 端口、批量上限、间隔、默认话术
├── profile/             # 运行时生成：浏览器登录态（.gitignore 已排除）
└── runtime/             # 运行时生成：调试产物、schedule.json（已排除）
```

`run.bat` 是纯 ASCII + CRLF 的批处理，**改动时不要写中文注释、不要存成 LF**，否则 cmd 会逐行解析错乱。

## 常见问题

<details>
<summary><b>ModuleNotFoundError: No module named 'playwright'</b></summary>

启动用的 Python 里没装 playwright。一台机器上常有好几个 Python，`run.bat` 会依次探测：

1. 环境变量 `DOUYIN_PY` 指定的解释器
2. 同目录下的 `portable_python\python.exe`
3. PATH 里的 `python`
4. `py -3.11` / `py -3.12`
5. `%LOCALAPPDATA%\Programs\Python\Python3xx`

取第一个能 `import playwright` 的，并在窗口打印 `Python : <实际路径>`。全失败会直接给出 pip 安装命令。
</details>

<details>
<summary><b>点「刷新联系人」读不到数据</b></summary>

抖音网页版的会话列表结构会变，工具用两套策略：

- 先找 `data-e2e` 里含 conversation / session / chat-item 的元素
- 再退化为「左侧栏、同类 class 数量最多、带头像」的启发式匹配

仍为 0 条时点 **导出页面结构**，会生成 `runtime/debug_<时间戳>.png` 与 `.html`，据此校准选择器。
</details>

<details>
<summary><b>提示「网页版消息功能受限」</b></summary>

抖音对部分账号/地区不开放网页版私信，会引导去 App。这种情况本工具无解，只能换官方渠道。
</details>

<details>
<summary><b>发送失败「未定位到输入框」</b></summary>

点开会话后输入框选择器没命中。同样用「导出页面结构」抓一份页面，调整 `server.py` 里的 `_locate_input()`。
</details>

## 风险与限制

自动化操作抖音 Web 端，**平台是能检测到的**。可能遇到：发送失败、限流、要求二次验证、强制下线、极端情况下封号。

内置的几道刹车可以在 `config.json` 里改，但不建议放宽：

| 项 | 默认 | 说明 |
|---|---|---|
| `max_batch` | 20 | 单次最多发多少人 |
| `min_interval` | 8 s | 低于此值直接拒绝执行 |
| `default_interval` | 10 s | 界面滑块默认值 |

建议先用小号试、一次不超过 10 人、间隔 15 秒以上、话术别完全雷同。

**关于火花本身**：抖音的火花要求双方当天都有消息往来。本工具只保证你这侧每天发出去，对方不回，当天照样断。它不能替你维系一段关系，只能替你按发送键。

## 免责声明

本项目仅供个人学习与技术研究（Playwright 浏览器自动化、本地 HTTP 服务设计）。

- 请勿用于骚扰他人、批量营销或任何违反抖音服务条款的用途
- 账号风险、封号后果由使用者自行承担
- 本项目与字节跳动 / 抖音无任何关联，未获得其授权或认可
- 仓库中不包含任何账号凭据；`profile/`、`runtime/`、`schedule.json` 均为本机私有数据，已被忽略

如果你在别的仓库看到有人把 `profile/` 传上去了 —— 那人的号大概已经没了。

## License

[MIT](LICENSE)
*（内容由AI生成，仅供参考）*
