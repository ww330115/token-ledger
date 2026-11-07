---
name: token-ledger
description: 查询本机 WorkBuddy / opencode 桌面版的 token 用量、额度、积分与费用。用户问“今天用了多少 token / 花了多少钱 / 额度还剩多少 / 哪个模型最费”时使用。只读本地日志，不上传数据。
---

# Token 账本（给 agent）

一个 Windows 本地小工具，把本机 AI 客户端的用量记成账。**所有操作都是只读的**：
只读日志文件，只向本机 `127.0.0.1` 的 opencode 服务要额度。

## 什么时候用

用户问到下面这类问题时：

- 今天 / 本周 / 本月用了多少 token、花了多少钱
- opencode Go 套餐额度还剩多少（近 5 小时 / 本周 / 本月）
- WorkBuddy 积分今天扣了多少、还剩多少
- 哪个模型最费、哪条请求最贵
- 把用量导出成 CSV 或表格

## 怎么调用

先确认脚本位置。约定为当前项目根目录下的 `token_ledger.py`：

```powershell
python token_ledger.py --check --json
```

这是**首选**方式：一次调用拿到全部结构化数据，不开窗口、不需要 GUI。

> Windows 上如果 `python` 指向 Microsoft Store 占位符，用完整路径，例如
> `C:\Users\<你>\AppData\Local\Programs\Python\Python312\python.exe`。

### 常用命令

| 目的 | 命令 |
|---|---|
| 结构化数据（推荐） | `python token_ledger.py --check --json` |
| 人看的摘要 | `python token_ledger.py --check` |
| 打开悬浮窗 | `pythonw token_ledger.py` |
| 打开悬浮窗 + 明细表 | `pythonw token_ledger.py --detail` |
| 打开悬浮窗 + 统计图 | `pythonw token_ledger.py --stats` |
| 只看 opencode 通道 | `pythonw token_ledger.py --only-oc` |
| 没有数据时演示界面 | `python token_ledger.py --demo` |

### 数据源不在默认位置

四个路径都可以用参数覆盖（也可写进 `config.json`）：

```powershell
python token_ledger.py --check --json `
  --projects "D:\data\.workbuddy\projects" `
  --workbuddy-db "D:\data\.workbuddy\workbuddy.db" `
  --opencode-db "D:\data\.local\share\opencode\opencode.db" `
  --service-json "D:\data\.local\state\opencode\service.json"
```

配置和数据目录：

- 默认数据目录：`%APPDATA%\TokenLedger\`（配置 `config.json`、`logs\`、`exports\`）
- 环境变量 `TOKEN_LEDGER_HOME` 或参数 `--data-dir DIR` 可改

## JSON 输出怎么读

```json
{
  "version": "1.0.0",
  "demo": false,
  "data_dir": "C:\\Users\\me\\AppData\\Roaming\\TokenLedger",
  "sources": {
    "wb_projects": { "path": "...", "exists": true },
    "wb_db":       { "path": "...", "exists": true },
    "oc_db":       { "path": "...", "exists": true },
    "oc_service":  { "path": "...", "exists": true }
  },
  "records": 15366,
  "lanes": {
    "内置":    { "records": 12071, "input": 53647060, "output": 10831855,
                 "thinking": 4580755, "cache_read": 1508192188, "usd": 0.0 },
    "桌面":    { "records": 2254, "usd": 0.0 },
    "opencode":{ "records": 995, "usd": 2.7099 }
  },
  "today": {
    "opencode_records": 0, "usd": 0.0, "allowance": 0.0,
    "builtin_records": 366, "builtin_tokens": 51130000,
    "desktop_records": 205, "desktop_usd": 0.0,
    "credits": 12.5, "credit_turns": 9
  },
  "totals": {
    "records": 15366, "usd": 2.7099, "credits": 2763.79,
    "credit_turns": 856, "turn_groups": 974, "titles": 135, "tasks_today": 4
  },
  "billing": { "美元": 995, "额度": 911, "积分": 8522, "免费": 4892, "本地": 46 }
}
```

字段含义：

| 字段 | 含义 |
|---|---|
| `lanes` | 按通道聚合；`usd` 只统计真扣的美元（Go 套餐内为 0） |
| `today.usd` / `today.allowance` | 今天网关通道的美元标价 / 实扣额度 |
| `today.builtin_tokens` | 今天内置模型的 token 总量（输入+输出+缓存读） |
| `today.credits` | 今天实扣积分；`credit_turns` 是轮数 |
| `totals.usd` | 历史累计美元（不含 Go 套餐） |
| `totals.credits` | 历史累计积分 |
| `billing` | 各计费口径的请求条数 |

## 回答用户时的注意事项（重要）

1. **五种口径不能相加**：`本地` / `美元` / `额度` / `免费` / `积分` 是五种不同的东西。
   不要输出“总花费 = 美元 + 积分 + 额度”这种数字。
2. **积分不折价**：官方没公开积分单价，只说积分数量和轮数。
3. **Go 额度是百分比**，来自本机服务；`usd` 字段是折算的“额度价值”，不是额外扣款。
4. 引用数字时说明时间范围（今天 / 本月 / 累计）和口径。
5. 如果 `records` 为 0，看 `sources` 里哪个 `exists` 是 `false`，把路径告诉用户。
6. `--demo` 的数据是假的，**不要当作真实用量汇报**。
7. 界面模型榜按“通道 + 模型”分行；同名模型可能出现两行（内置积分 / Go 额度 / 网关美元），
   不要把两行的钱相加。

## 依赖与平台

- 零第三方依赖，标准库即可（tkinter / sqlite3 / ctypes / urllib）
- 完整功能仅 Windows；其他系统 `--check` 可用，托盘/贴边/多屏自动关闭
- 唯一网络请求：`127.0.0.1` 上的 opencode 服务

## 故障排查

| 现象 | 处理 |
|---|---|
| `records: 0` | 用 `sources` 逐个核对路径是否存在 |
| 额度显示 `—` | opencode 桌面版没开，或 `service.json` 路径不对 |
| 积分是 0 | 当天没有内置模型请求，或 `workbuddy.db` 路径不对 |
| 中文乱码 | 终端设 `PYTHONIOENCODING=utf-8` 和 `chcp 65001` |
