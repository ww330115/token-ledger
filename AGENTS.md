# AGENTS.md

给维护这个仓库的编码 agent 看的说明。人类用户请看 `README.md`。

## 项目结构

```
token-ledger/
├─ token_ledger.py        # 全部实现（单文件）
├─ SKILL.md               # 给 agent 的使用说明
├─ README.md              # 给用户的使用说明
├─ config.example.json    # 配置模板
├─ run.bat                # Windows 双击启动
├─ build_portable.ps1     # 打便携包（Python embed + tkinter）
└─ docs/screenshots/      # README 截图（--demo 假数据）
```

## 硬约束

1. **零第三方依赖**。只用标准库。不要引入 requests / pystray / Pillow 等。
2. **不写代码目录**。配置、日志、导出必须走 `DATA_DIR`（`CFG_PATH` / `LOG_DIR` / `EXPORT_DIR`）。
3. **Windows 专属 API 必须可选**。`ctypes.windll`、`wintypes`、`tkinter` 都存在导入失败的机器，
   保持 try/except 与 `None` 兜底，保证 `--check` 在无 GUI / 非 Windows 上仍可运行。
4. **不合并计费口径**。本地 / 美元 / 额度 / 免费 / 积分是五种不同东西，见 `bill_of()`。
5. **演示数据必须假**。`--demo` 只允许读 `DEMO_SPECS`，禁止读任何真实文件或数据库。
6. **提交前不要带**：`config.json`、`config.demo.json`、`data/`、`logs/`、`exports/`、`*.pyc`、`dist/`。

## 关键函数

| 函数 / 类 | 作用 |
|---|---|
| `default_paths()` / `apply_paths()` | 四个数据源的默认路径与覆盖顺序（参数 > 配置 > 默认） |
| `parse_record()` | WorkBuddy 一条 jsonl → 一行账 |
| `parse_oc_message()` | opencode 桌面版一条消息 → 一行账 |
| `Store.scan()` | 增量扫描（按文件偏移 + 数据库 rowid 水位线） |
| `Store.load_demo()` | 装入 `--demo` 假数据 |
| `bill_of()` | 判定计费口径（唯一入口，别在别处重复判断） |
| `go_est_usd()` | 按官方单价折算 Go 套餐消耗 |
| `Ledger` | 悬浮窗 |
| `DetailWindow` / `ModelStats` | 明细表 / 统计图 |

## 修改后必须验证

```powershell
python token_ledger.py --check          # 真实数据能读
python token_ledger.py --check --json   # JSON 合法、字段齐全
python token_ledger.py --demo           # 界面能开、不读本机文件
```

再确认仓库目录没有新文件：

```powershell
git status --short
```

预期只有源码/文档改动，不出现 `config*.json`、`logs/`、`exports/`、`data/`、`__pycache__/`。

## 便携包

`build_portable.ps1` 会下载 Python embed 包，并从本机完整安装里拷 `tkinter`、`tcl`、`tk`、
`_tkinter.pyd`、`tcl86t.dll`、`tk86t.dll`，最后压成 `dist/token-ledger-portable-win64.zip`。

- 便携包内的 `portable.txt` 会让数据写到解压目录的 `data\`
- 产物只作为 GitHub Release 附件，不进 git

## 代码风格

- 保留现有的中文注释风格，解释“为什么”而不是“是什么”
- 新增界面文案用中文；日志/异常信息简洁
- 不要重构与当前任务无关的部分
