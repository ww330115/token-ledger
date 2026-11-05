#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Token 账本 —— 本机 AI 客户端每次请求的真实 token、额度与费用（实时悬浮窗）。

三条通道分开记，绝不混算：
  · opencode 网关通道：requestModelId 以 "custom-local:" 开头，网关直接回传
    list_cost（美元标价）和 allowance（实扣额度），是真实账单，不用估算。
  · WorkBuddy 内置通道：日志里只有 token，金额按官方实扣积分记；
    倍率表仅作参考展示，不折成钱（积分单价未知，硬折就是编）。
  · opencode 桌面版：从它自己的会话库读每条消息的 tokens 与 cost。

数据源（全部只读，路径可在 config.json 或命令行改）：
  WorkBuddy 日志   ~/.workbuddy/projects/**/*.jsonl
  WorkBuddy 台账   ~/.workbuddy/workbuddy.db
  opencode 会话库  ~/.local/share/opencode/opencode.db
  opencode 服务    ~/.local/state/opencode/service.json
网络：只请求本机 127.0.0.1 上的 opencode 服务（取真实额度），不上传任何数据。
写文件：配置/日志/导出都在用户目录，不写代码目录。

用法：
    pythonw token_ledger.py            # 启动悬浮窗（无控制台）
    python  token_ledger.py --check    # 只打印统计，不开窗口
    python  token_ledger.py --check --json   # 机器可读输出（给 agent 用）
    python  token_ledger.py --demo     # 假数据预览界面，不读任何本机文件
"""
import os
import sys
import json
import glob
import threading
import time
import csv
import base64
import sqlite3
import argparse
import datetime as dt
import urllib.request

VERSION = "1.0.0"
APP_NAME = "TokenLedger"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _portable():
    """便携包模式：脚本旁边放一个 portable.txt，配置和日志就写在 data/ 里。"""
    return os.path.exists(os.path.join(BASE_DIR, "portable.txt"))


def _data_dir():
    """配置和日志放哪：环境变量 > 便携模式 > 系统用户目录（不写代码目录）。"""
    env = os.environ.get("TOKEN_LEDGER_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if _portable():
        return os.path.join(BASE_DIR, "data")
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
            os.path.expanduser("~"), ".config")
    return os.path.join(base, APP_NAME)


DATA_DIR = _data_dir()
CFG_PATH = os.path.join(DATA_DIR, "config.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
QUOTA_LOG = os.path.join(LOG_DIR, "额度观测.csv")
ERROR_LOG = os.path.join(LOG_DIR, "error.log")


def ensure_dirs():
    for d in (DATA_DIR, LOG_DIR, EXPORT_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def set_data_dir(path):
    """--data-dir 用：换一处放配置和日志。"""
    global DATA_DIR, CFG_PATH, LOG_DIR, EXPORT_DIR, QUOTA_LOG, ERROR_LOG
    DATA_DIR = os.path.abspath(os.path.expanduser(path))
    CFG_PATH = os.path.join(DATA_DIR, "config.json")
    LOG_DIR = os.path.join(DATA_DIR, "logs")
    EXPORT_DIR = os.path.join(DATA_DIR, "exports")
    QUOTA_LOG = os.path.join(LOG_DIR, "额度观测.csv")
    ERROR_LOG = os.path.join(LOG_DIR, "error.log")
    ensure_dirs()


ensure_dirs()

# ---------------------------------------------------------------- 可调参数

POLL_MS = 2000          # 界面刷新 / 增量扫描间隔（毫秒）
LIVE_ROWS = 8           # 悬浮窗一屏几行
HISTORY = 30            # 悬浮窗最多往回翻多少条
ROW_H = 21              # 每行像素高
SBAR_W = 12             # 滚动条宽
DETAIL_CAP = 4000       # 明细表最多渲染多少行（合计仍按全量算）
OC_RECHECK_SECS = 600   # 每隔多久重读一次数据库里最近的消息（它们是边流式边更新的）
FILE_RESCAN_SECS = 30   # jsonl 文件清单多久重建一次（目录遍历 50 ms，别每 2 秒都做）
GO_PRICE_TTL = 600      # 价目表缓存多久（解析一次约 150 ms）
DOCK_EDGE = 26          # 松手时离屏幕边多近就吸附上去
DOCK_PEEK = 7           # 收起后留在屏幕上的一条（鼠标靠这一条就弹回来）
DOCK_POLL_MS = 120      # 鼠标位置采样间隔
DOCK_GRACE_MS = 420     # 鼠标离开后等多久才收起（防手滑扫过就缩走）

BG      = "#14161b"
BG_ALT  = "#1b1e25"
FG      = "#e7e9ee"
FG_DIM  = "#8b93a5"
ACCENT  = "#4ea1ff"
GREEN   = "#7ee787"
GOLD    = "#e3b341"
BORDER  = "#2a2f3a"

FONT_UI   = ("Microsoft YaHei UI", 9)
FONT_UI_B = ("Microsoft YaHei UI", 9, "bold")
FONT_NUM  = ("Consolas", 9)
FONT_NUM_B= ("Consolas", 9, "bold")
FONT_BIG  = ("Consolas", 15, "bold")

# WorkBuddy 内置模型的积分倍率。默认值来自WorkBuddy 模型选择器截图；
# 启动时会优先读WorkBuddy 自己的产品配置（cache/acc-product-config-v3-*.json），
# 那里每个模型都带 "credits": "x0.11"，比截图权威。
MULT = {
    "hy3": 0.00, "hy3-x": 0.05, "hy4-preview": 0.29, "hy4free": 0.00, "hy4std": 0.29,
    "space-bunny": 0.08, "minimax-m3.1-flash": 0.08, "glm-5.3-flash": 0.08,
    "deepseek-v4.1-flash": 0.11, "mimo-v2.6-pro": 0.13,
    "step-5-preview": 0.33, "deepseek-v4-pro": 0.51, "glm-5.3": 0.69,
    "kimi-k2.8-preview": 0.77, "glm-5.2": 0.79, "kimi-k3.1-pro": 0.97,
    "kimi-k3-1": 1.62,
}
# 模式通道（快通均衡/极致）的倍率
MODE_MULT = {"fast-model": 0.15, "balanced-model": 0.50, "deep-model": 1.00, "auto": 0.50}
MULT_BASE = dict(MULT)          # 静态表，刷新时不丢原始值

LANE_SHORT = {"opencode": "网关", "内置": "内置", "桌面": "桌面", "本地": "本地"}


# ---------------------------------------------------------------- 数据源路径
def default_paths():
    """各客户端默认把数据放哪（各家都用自己的目录，这里只是默认值）。"""
    home = os.path.expanduser("~")
    return {
        "wb_projects": os.path.join(home, ".workbuddy", "projects"),
        "wb_db": os.path.join(home, ".workbuddy", "workbuddy.db"),
        "oc_db": os.path.join(home, ".local", "share", "opencode", "opencode.db"),
        "oc_service": os.path.join(home, ".local", "state", "opencode", "service.json"),
    }


PATHS = default_paths()
DEFAULT_ROOT = PATHS["wb_projects"]
DB_PATH = PATHS["wb_db"]
OC_DB = PATHS["oc_db"]
OC_SERVICE = PATHS["oc_service"]


def apply_paths(cfg=None, args=None):
    """config.json 和命令行里的路径覆盖默认值；两边都没写就用默认。"""
    global PATHS, DEFAULT_ROOT, DB_PATH, OC_DB, OC_SERVICE
    p = default_paths()
    for key in list(p):
        v = (cfg or {}).get(key)
        if isinstance(v, str) and v.strip():
            p[key] = os.path.abspath(os.path.expanduser(v.strip()))
    if args is not None:
        for key, attr in (("wb_projects", "projects"), ("wb_db", "workbuddy_db"),
                          ("oc_db", "opencode_db"), ("oc_service", "service_json")):
            v = getattr(args, attr, None)
            if v:
                p[key] = os.path.abspath(os.path.expanduser(v))
    PATHS = p
    DEFAULT_ROOT = p["wb_projects"]
    DB_PATH = p["wb_db"]
    OC_DB = p["oc_db"]
    OC_SERVICE = p["oc_service"]
    return p


def path_report():
    """给人看的一行：每个数据源在不在。"""
    rows = []
    for key, label in (("wb_projects", "WorkBuddy 日志"),
                       ("wb_db", "WorkBuddy 台账"),
                       ("oc_db", "opencode 会话库"),
                       ("oc_service", "opencode 服务")):
        rows.append("%s %s %s" % ("[有]" if os.path.exists(PATHS[key]) else "[无]",
                                  label, PATHS[key]))
    return "\n".join(rows)


def load_multipliers():
    """优先用 WorkBuddy 产品配置里的官方倍率。"""
    out = dict(MULT_BASE)
    try:
        files = glob.glob(os.path.join(os.path.dirname(DB_PATH),
                                       "cache", "acc-product-config-v3-*.json"))
        if files:
            with open(max(files, key=os.path.getmtime), encoding="utf-8") as fh:
                cfg = json.load(fh)
            for m in cfg.get("models", []):
                c = m.get("credits")
                if isinstance(c, str) and c.lower().startswith("x"):
                    try:
                        out[m.get("id")] = float(c[1:])
                    except ValueError:
                        pass
    except Exception:
        pass
    return out


def load_credits(path=None):
    """从 workbuddy.db 读每轮真实扣的积分，键是 conversationRequestId（去掉横线）。"""
    p = path or DB_PATH
    out = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % p.replace("\\", "/"), uri=True, timeout=3)
        try:
            for (cj,) in con.execute(
                    "select credit_json from session_usage where credit_json is not null"):
                try:
                    d = json.loads(cj)
                except Exception:
                    continue
                for k, v in d.items():
                    kk = k.replace("-", "")
                    try:
                        val = float(v)
                    except (TypeError, ValueError):
                        continue
                    if val > out.get(kk, 0.0):
                        out[kk] = val
        finally:
            con.close()
    except Exception:
        return {}
    return out


# 启动时（apply_paths 之后）用官方配置覆盖静态表
def refresh_multipliers():
    global MULT
    MULT = load_multipliers()
    return MULT


# ---------------------------------------------------------------- 解析

def parse_record(o):
    """一条 jsonl 记录 = 一行账，或 None。"""
    pd = o.get("providerData")
    if not isinstance(pd, dict):
        return None
    ru = pd.get("rawUsage")
    if not isinstance(ru, dict) or not ru.get("total_tokens"):
        return None
    ts = o.get("timestamp")
    if not isinstance(ts, (int, float)):
        return None
    ts = ts / 1000.0

    rid = pd.get("requestModelId") or ""
    model = pd.get("model") or "?"
    cid = (pd.get("conversationRequestId") or "").replace("-", "")
    oc = ru.get("opencode") if isinstance(ru.get("opencode"), dict) else None

    if oc:
        t = oc.get("tokens") or {}
        inp   = int(t.get("input") or 0)
        out   = int(t.get("output") or 0)
        think = int(t.get("reasoning") or 0)
        cache = int(t.get("cache_read") or 0)
        cw    = int(t.get("cache_write_5m") or 0) + int(t.get("cache_write_1h") or 0)
        lane  = "opencode"
        usd   = float(oc.get("list_cost") or 0)
        allow = float(oc.get("allowance") or 0)
    else:
        ptd = ru.get("prompt_tokens_details") or {}
        cache = int(ptd.get("cached_tokens") or ru.get("prompt_cache_hit_tokens") or 0)
        inp = int(ru.get("prompt_tokens") or 0) - cache
        out = int(ru.get("completion_tokens") or 0)
        ctd = ru.get("completion_tokens_details") or {}
        think = int(ctd.get("reasoning_tokens") or 0)
        cw = 0
        usd = allow = 0.0
        if rid.startswith("custom-local:"):
            lane = "本地"            # 自定义通道里的本地/离线模型，不计费
        elif rid:
            lane = "内置"
        else:
            lane = "未知"

    key = o.get("id") or "%s|%d|%d|%d" % (o.get("sessionId") or "", ts, inp, out)
    # WorkBuddy 会在 rawUsage 里回传这一步实扣的积分（0 表示免费模型）
    raw_credit = ru.get("credit")
    try:
        raw_credit = float(raw_credit) if raw_credit is not None else None
    except (TypeError, ValueError):
        raw_credit = None
    return {
        "key": key, "ts": ts, "lane": lane, "model": model, "rid": rid, "cid": cid,
        "session": (o.get("sessionId") or "")[:8],
        "sess": "wb:" + (o.get("sessionId") or ""),
        "cwd": os.path.basename((o.get("cwd") or "").replace("\\", "/"))[:24],
        "in": inp, "out": out, "think": think, "cache": cache, "cw": cw,
        "usd": usd, "allow": allow, "credit": None, "credit_raw": raw_credit,
    }


def parse_oc_message(mid, sid, ts_ms, data):
    """opencode 桌面版一条 assistant 消息 = 一行账。"""
    try:
        d = json.loads(data)
    except Exception:
        return None
    tk = d.get("tokens")
    if not isinstance(tk, dict):
        return None
    cache = tk.get("cache") or {}
    m = d.get("model") or {}
    inp = int(tk.get("input") or 0)
    out = int(tk.get("output") or 0)
    if not (inp or out):
        return None
    return {
        "key": "ocd:" + (mid or ""),
        "ts": ts_ms / 1000.0,
        "lane": "桌面",
        "model": m.get("id") or "?",
        "rid": m.get("providerID") or "",
        "cid": "",
        "session": (sid or "")[:8],
        "sess": "oc:" + (sid or ""),
        "cwd": (d.get("agent") or "")[:24],
        "in": inp, "out": out,
        "think": int(tk.get("reasoning") or 0),
        "cache": int(cache.get("read") or 0),
        "cw": int(cache.get("write") or 0),
        "usd": float(d.get("cost") or 0.0), "allow": 0.0, "credit": None,
    }


# ---------------------------------------------------------------- 演示数据
# --demo 专用：字段和真实记录一模一样，数字全是编的，不读本机任何文件。
DEMO_TITLES = {
    "wb:d1a2b3c4": "整理机舱备件清单",
    "wb:e5f6a7b8": "翻译交接班报告",
    "wb:c9d0e1f2": "PSC 自查表核对",
    "wb:a3b4c5d6": "写主机巡检脚本",
    "oc:oc-demo-01": "备件查询页面",
    "oc:oc-demo-02": "重构日志解析",
}

DEMO_PROMPTS = {
    "oc:oc-demo-01": "把这份备件表做成能搜索的网页",
    "oc:oc-demo-02": "帮我理顺这段日志解析的顺序",
}

# (几天前, 时, 分, 通道, 模型, providerID, 会话, 目录, 输入, 输出, 思考, 缓存读, 缓存写, 美元, 实扣额度, 积分)
DEMO_SPECS = [
    (0, 8, 42, "内置", "deepseek-v4.1-flash", "deepseek-v4.1-flash", "wb:d1a2b3c4", "ship-spares", 1830, 640, 210, 12200, 0, 0.0, 0.0, 1.06),
    (0, 8, 47, "内置", "deepseek-v4.1-flash", "deepseek-v4.1-flash", "wb:d1a2b3c4", "ship-spares", 2460, 880, 305, 9800, 0, 0.0, 0.0, 1.42),
    (0, 9, 15, "opencode", "deepseek-v4.1-flash", "custom-local:wb", "wb:e5f6a7b8", "handover", 3150, 1120, 0, 6400, 0, 0.0043, 0.0041, None),
    (0, 9, 22, "内置", "glm-5.3", "glm-5.3", "wb:c9d0e1f2", "psc", 5120, 1730, 640, 21400, 0, 0.0, 0.0, 3.86),
    (0, 9, 40, "本地", "qwen3-8b", "custom-local:local", "wb:a3b4c5d6", "scripts", 2400, 700, 0, 0, 0, 0.0, 0.0, None),
    (0, 10, 5, "内置", "hy4free", "hy4free", "wb:d1a2b3c4", "ship-spares", 900, 220, 0, 1500, 0, 0.0, 0.0, 0.0),
    (0, 10, 31, "opencode", "kimi-k3.1-pro", "custom-local:wb", "wb:e5f6a7b8", "handover", 4200, 1500, 0, 8200, 0, 0.0126, 0.0118, None),
    (0, 11, 2, "桌面", "deepseek-v4.1-flash", "opencode-go", "oc:oc-demo-01", "agent", 6800, 2300, 410, 15200, 0, 0.0, 0.0, None),
    (0, 11, 50, "桌面", "glm-5.3-flash", "opencode", "oc:oc-demo-02", "agent", 3900, 1200, 180, 9600, 0, 0.0, 0.0, None),
    (0, 13, 18, "内置", "deepseek-v4-pro", "deepseek-v4-pro", "wb:c9d0e1f2", "psc", 7600, 2600, 980, 30100, 0, 0.0, 0.0, 5.24),
    (0, 14, 6, "opencode", "deepseek-v4.1-flash", "custom-local:wb", "wb:c9d0e1f2", "psc", 5200, 1800, 0, 11800, 0, 0.0071, 0.0068, None),
    (0, 14, 44, "内置", "step-5-preview", "step-5-preview", "wb:a3b4c5d6", "scripts", 3300, 1100, 520, 7600, 0, 0.0, 0.0, 1.78),
    (0, 15, 20, "桌面", "kimi-k3.1-pro", "opencode-go", "oc:oc-demo-01", "agent", 8100, 2900, 760, 18300, 0, 0.0, 0.0, None),
    (0, 16, 2, "内置", "deepseek-v4.1-flash", "deepseek-v4.1-flash", "wb:d1a2b3c4", "ship-spares", 2740, 910, 240, 10400, 0, 0.0, 0.0, 1.58),
    (0, 16, 35, "opencode", "glm-5.3-flash", "custom-local:wb", "wb:e5f6a7b8", "handover", 2900, 980, 0, 5100, 0, 0.0034, 0.0032, None),
    (1, 9, 10, "内置", "glm-5.3", "glm-5.3", "wb:c9d0e1f2", "psc", 4400, 1500, 430, 16000, 0, 0.0, 0.0, 3.12),
    (1, 10, 26, "opencode", "deepseek-v4.1-flash", "custom-local:wb", "wb:e5f6a7b8", "handover", 3600, 1300, 0, 7200, 0, 0.0049, 0.0047, None),
    (1, 11, 15, "桌面", "deepseek-v4.1-flash", "opencode-go", "oc:oc-demo-02", "agent", 5400, 1900, 300, 11200, 0, 0.0, 0.0, None),
    (1, 13, 40, "内置", "deepseek-v4.1-flash", "deepseek-v4.1-flash", "wb:d1a2b3c4", "ship-spares", 2100, 780, 260, 8800, 0, 0.0, 0.0, 1.31),
    (1, 14, 55, "内置", "kimi-k3.1-pro", "kimi-k3.1-pro", "wb:c9d0e1f2", "psc", 6100, 2100, 880, 24000, 0, 0.0, 0.0, 4.48),
    (1, 15, 33, "本地", "llama-4-8b", "custom-local:local", "wb:a3b4c5d6", "scripts", 1800, 500, 0, 0, 0, 0.0, 0.0, None),
    (1, 16, 12, "opencode", "deepseek-v4.1-flash", "custom-local:wb", "wb:e5f6a7b8", "handover", 3300, 1150, 0, 6600, 0, 0.0044, 0.0042, None),
    (1, 17, 1, "桌面", "glm-5.3-flash", "opencode", "oc:oc-demo-01", "agent", 4100, 1400, 220, 9800, 0, 0.0, 0.0, None),
    (2, 9, 55, "内置", "deepseek-v4.1-flash", "deepseek-v4.1-flash", "wb:d1a2b3c4", "ship-spares", 1980, 720, 190, 8100, 0, 0.0, 0.0, 1.22),
    (2, 11, 20, "opencode", "glm-5.3-flash", "custom-local:wb", "wb:e5f6a7b8", "handover", 2700, 900, 0, 4800, 0, 0.0031, 0.0029, None),
    (2, 14, 8, "桌面", "deepseek-v4.1-flash", "opencode-go", "oc:oc-demo-02", "agent", 7600, 2700, 650, 16900, 0, 0.0, 0.0, None),
]


def demo_records():
    """造一份假账目，返回 (records, titles, prompts)。"""
    now = time.time()
    d0 = day_start()
    today_n = sum(1 for s in DEMO_SPECS if s[0] == 0)
    k = 0
    recs = []
    prompts = {}
    for idx, spec in enumerate(DEMO_SPECS):
        (day, hh, mm, lane, model, rid, sess, cwd,
         i_, o_, th, ca, cw, usd, allow, credit) = spec
        if day == 0:
            # 今天的记录按“最近 7 小时”铺开，别出现未来时间
            ts = max(d0 + 300, now - (today_n - 1 - k) * 28 * 60)
            k += 1
        else:
            ts = d0 - day * 86400 + hh * 3600 + mm * 60
        cid = ("demo-%02d" % idx) if lane in ("内置", "opencode") else ""
        recs.append({
            "key": "demo:%02d" % idx, "ts": ts, "lane": lane, "model": model,
            "rid": rid, "cid": cid, "session": sess.split(":")[-1][:8],
            "sess": sess, "cwd": cwd,
            "in": i_, "out": o_, "think": th, "cache": ca, "cw": cw,
            "usd": usd, "allow": allow, "credit": credit,
        })
        if sess in DEMO_PROMPTS and sess not in prompts:
            prompts[sess] = [(ts - 90, DEMO_PROMPTS[sess])]
    recs.sort(key=lambda r: r["ts"])
    return recs, dict(DEMO_TITLES), prompts


class Store:
    """增量扫描日志目录，只解析新追加的行。"""

    def __init__(self, root, demo=False):
        self.root = root
        self.demo = demo
        self.offsets = {}       # path -> (已读字节, 残留半行)
        self.records = []
        self.seen = set()
        self.turn_last = {}     # conversationRequestId -> 该轮最后一条记录（dict 引用）
        self.turn_credit = {}   # conversationRequestId -> 该轮日志里自报的积分合计
        self.credits = {}
        self.db_path = DB_PATH
        self.oc_db = OC_DB
        self.oc_rowid = 0
        self.by_key = {}        # key -> 记录 dict（用于原地更新）
        self.last_updated = 0   # 上一轮scan() 里被"刷新数据"的条数
        self._files = []        # 缓存的jsonl 清单
        self._files_at = 0.0
        self.titles = {}        # "wb:<id>" / "oc:<id>" -> 任务标题
        self.prompts = {}       # "oc:<id>" -> [(时间, 你的提问)]
        self.last_scan = 0.0
        if demo:
            self.load_demo()

    def load_demo(self):
        """--demo：装一份假账目，界面能完整跑，但一条本机文件都不读。"""
        recs, titles, prompts = demo_records()
        self.records = recs
        self.seen = {r["key"] for r in recs}
        self.titles = titles
        self.prompts = prompts
        self.turn_last = {}
        self.turn_credit = {}
        for r in recs:
            if r.get("credit") is not None and r["cid"]:
                self.turn_last[r["cid"]] = r
                self.turn_credit[r["cid"]] = r["credit"]
        self.db_path = ""
        self.oc_db = ""
        self._files = []
        # 演示用的单价表，免得 --demo 去读本机 opencode 目录
        global GO_PRICE, _GO_PRICE_AT
        GO_PRICE = {
            "deepseek-v4.1-flash": (0.27, 1.10, 0.027, []),
            "glm-5.3": (0.60, 2.40, 0.060, []),
            "glm-5.3-flash": (0.09, 0.36, 0.009, []),
            "kimi-k3.1-pro": (0.60, 2.40, 0.060, []),
        }
        _GO_PRICE_AT = time.time()
        return len(recs)

    def reload_credits(self):
        """演示模式没有数据库，直接返回空（别去读本机台账）。"""
        return {} if self.demo else load_credits(self.db_path)

    def load_titles(self):
        """从两个数据库读会话标题，用来把多开的任务分开显示。"""
        if self.demo:
            return self.titles
        out = {}
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.db_path.replace("\\", "/"),
                                  uri=True, timeout=3)
            try:
                for sid, t, ct, cwd in con.execute(
                        "select id, title, custom_title, cwd from sessions"):
                    name = (ct or t or "").strip()
                    if name:
                        out["wb:" + sid] = name
            finally:
                con.close()
        except Exception:
            pass
        if self.oc_db and os.path.exists(self.oc_db):
            try:
                con = sqlite3.connect("file:%s?mode=ro" % self.oc_db.replace("\\", "/"),
                                      uri=True, timeout=3)
                try:
                    for sid, t in con.execute(
                            "select id, title from session_v2 "
                            "where title is not null and title<>''"):
                        out["oc:" + sid] = t.strip()
                    for sid, ts, data in con.execute(
                            "select session_id, time_created, data from session_message "
                            "where type='user'"):
                        try:
                            d = json.loads(data)
                        except Exception:
                            continue
                        t = (d.get("text") or "").strip()
                        if not t:
                            continue
                        self.prompts.setdefault("oc:" + sid, []).append(
                            (ts / 1000.0, t.replace("\r", "")))
                    for v in self.prompts.values():
                        v.sort()
                finally:
                    con.close()
            except Exception:
                pass
        self.titles = out
        return out

    def prompt_of(self, rec):
        """这条请求是你在问什么：取同会话里时间最接近的上一条用户提问。"""
        lst = self.prompts.get(rec.get("sess") or "")
        if not lst:
            return ""
        ts = rec["ts"]
        best = ""
        for pts, txt in lst:
            if pts <= ts + 3:
                best = txt
            else:
                break
        return best

    def task_name(self, rec, units=14):
        """一条记录属于哪个任务：标题 > 工作目录 > 会话号。"""
        t = self.titles.get(rec.get("sess") or "")
        if not t:
            t = rec.get("cwd") or ""
        if not t:
            t = (rec.get("sess") or "")[-6:]
        return fit(t, units)

    def apply_credits(self, credits):
        """把每轮积分挂到该轮最后一条记录上，避免按行重复计数。

        优先用数据库 credit_json（官方实扣台账）；
        该轮数据库没记时，再用日志rawUsage.credit 的合计补上。
        """
        changed = 0
        for cid, rec in self.turn_last.items():
            val = credits.get(cid)
            if val is None and cid in self.turn_credit:
                val = round(self.turn_credit[cid], 2)
            if val is not None and rec.get("credit") != val:
                rec["credit"] = val
                changed += 1
        self.credits = credits
        return changed

    def scan(self):
        if self.demo:
            return 0
        added = 0
        self.last_updated = 0
        # 目录遍历很贵（实测 50 ms/次，单tick 的95%），清单缓存 FILE_RESCAN_SECS 秒重建。
        # 新会话晚这么多秒才被发现，对账本没影响。
        if not self._files or time.time() - self._files_at > FILE_RESCAN_SECS:
            self._files = glob.glob(os.path.join(self.root, "**", "*.jsonl"),
                                    recursive=True)
            self._files_at = time.time()
        for path in self._files:
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            off, carry = self.offsets.get(path, (0, b""))
            if size < off:                     # 文件被重建
                off, carry = 0, b""
            if size == off and not carry:
                continue
            try:
                with open(path, "rb") as fh:
                    fh.seek(off)
                    chunk = fh.read(size - off)
            except OSError:
                continue
            data = carry + chunk
            if b"\n" in data:
                lines = data.split(b"\n")
                carry = lines.pop()
            else:
                lines, carry = [], data
            for ln in lines:
                if b'"usage"' not in ln and b'"rawUsage"' not in ln:
                    continue
                try:
                    o = json.loads(ln.decode("utf-8", "ignore"))
                except Exception:
                    continue
                rec = parse_record(o)
                if not rec or rec["key"] in self.seen:
                    continue
                self.seen.add(rec["key"])
                self.records.append(rec)
                if rec["cid"]:
                    old = self.turn_last.get(rec["cid"])
                    if old is not None and old.get("credit") is not None:
                        # 这一轮的"最后一条"换人了：旧的那条不能再代表整轮，
                        # 否则同一轮积分会被重复计入总额。
                        old["credit"] = None
                        self.last_updated += 1
                    self.turn_last[rec["cid"]] = rec
                    if rec.get("credit_raw") is not None:
                        self.turn_credit[rec["cid"]] = (
                            self.turn_credit.get(rec["cid"], 0.0) + rec["credit_raw"])
                added += 1
            self.offsets[path] = (size, carry)
        added += self.scan_opencode()
        if added:
            self.records.sort(key=lambda r: r["ts"])
        self.last_scan = time.time()
        return added

    def scan_opencode(self):
        """读 opencode 桌面版的库：session_message 里 type='assistant' 的 tokens/cost。

        坑：assistant 消息是*先插入、再边流式边更新**的（tokens 从 0 慢慢长到最终值）。
        只按 rowid 水位线增量扫，第一次抓到的那一版就永远不重读：
          · 抓到半截 → 金额和 token 偏小，越跑越少；
          · 一条都还没 token → parse_oc_message 返回 None，这条直接永久丢失。
        所以除了新行，还要把*最近 OC_RECHECK_SECS 内*的行重读一遍，按 key 原地更新。
        """
        if not self.oc_db or not os.path.exists(self.oc_db):
            return 0
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self.oc_db.replace("\\", "/"),
                                  uri=True, timeout=3)
        except Exception:
            return 0
        cutoff_ms = int((time.time() - OC_RECHECK_SECS) * 1000)
        rows, dirs = [], {}
        try:
            # 1) 新行：走 rowid 主键，最便宜
            rows = list(con.execute(
                "select rowid, id, session_id, time_created, data from session_message "
                "where type='assistant' and rowid > ? order by rowid", (self.oc_rowid,)))
            # 2) 最近窗口：走time_created 索引，用于抓"边流式边更新"的消息。
            #    注意不能和1) 写成 OR ——那样 SQLite 会退化成全表扫（实测每次读6 MB）。
            if cutoff_ms:
                rows += list(con.execute(
                    "select rowid, id, session_id, time_created, data from session_message "
                    "where type='assistant' and time_created >= ?", (cutoff_ms,)))
            dirs = dict(con.execute("select id, directory from session_v2"))
        except Exception:
            rows, dirs = [], {}
        finally:
            con.close()
        added = 0
        seen_ids = set()
        for rowid, mid, sid, tcr, data, *_ in rows:
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            if rowid > self.oc_rowid:
                self.oc_rowid = rowid
            rec = parse_oc_message(mid, sid, tcr, data)
            if not rec:
                continue
            directory = dirs.get(sid)
            if directory:
                rec["cwd"] = os.path.basename(directory.replace("\\", "/").rstrip("/"))[:24]
            old = self.by_key.get(rec["key"])
            if old is not None:
                new_state = (rec["in"], rec["out"], rec["think"], rec["cache"], rec["usd"])
                if (old["in"], old["out"], old["think"], old["cache"], old["usd"]) != new_state:
                    old.update(rec)          # 原地更新，records 里的引用一起变
                    self.last_updated += 1
                continue
            self.seen.add(rec["key"])
            self.by_key[rec["key"]] = rec
            self.records.append(rec)
            added += 1
        return added


# ---------------------------------------------------------------- 格式区

def dwidth(s):
    """按显示宽度算：中文算 2，英文算 1。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def fit(s, units):
    """把字符串裁到指定显示宽度，超了用 —结尾。"""
    out = ""
    w = 0
    for ch in s:
        cw = 2 if ord(ch) > 127 else 1
        if w + cw > units - 1:
            return out + "…"
        out += ch
        w += cw
    return out


def fmt_tok(n):
    n = int(n or 0)
    if n >= 100_000_000:
        return "%.2f亿" % (n / 1e8)
    if n >= 10_000:
        return "%.1f万" % (n / 1e4)
    return "{:,}".format(n)


def fmt_k(n):
    n = int(n or 0)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1e6)
    if n >= 1_000:
        return "%.1fk" % (n / 1e3)
    return str(n)


def fmt_usd(x):
    x = float(x or 0)
    if x == 0:
        return "$0"
    if x < 0.001:
        return "$%.6f" % x
    if x < 1:
        return "$%.4f" % x
    return "$%.2f" % x


# ---------------------------------------------------------------- Go 金额
# Go 套餐按订阅收费，本地日志里每条的 cost 恒为 0，但控制台每笔都显示真实 Cost。
# 那个 Cost 可以原样复算出来（已和官网逐条对过账，误差 0）：
#   (输入×输入价 + (输出+思考)×输出价 + 缓存读×缓存价) ÷ 100万 × 高峰倍率
#   单价和高峰倍率都读 opencode 自己的模型目录（kv: models-dev:v2:catalog）。
GO_PRICE = {}
_GO_PRICE_AT = 0.0


def _hm(s):
    """'01:30' → 1.5 小时。"""
    h, m = str(s).split(":")
    return int(h) + int(m) / 60.0


def _when_hit(when, u):
    """opencode 价目表里的加价时段：utc_days + any/all(utc_hours)。"""
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    days = (when.get("utc_days") or {}).get("in") or []
    if days and names[u.weekday()] not in days:
        return False
    alts = when.get("any")
    if not alts:
        return True
    h = u.hour + u.minute / 60.0
    for alt in alts:
        conds = (alt or {}).get("all") or []
        ok = True
        for c in conds:
            hh = ((c or {}).get("utc_hours") or {})
            a, b = hh.get("gte"), hh.get("lt")
            if a is not None and h < _hm(a):
                ok = False
            if b is not None and h >= _hm(b):
                ok = False
        if ok and conds:
            return True
    return False


def load_go_prices(force=False):
    """读 Go 套餐单价（每百万 token 的美元）。读不到就空着，界面只显示"额度"。

    解析一次约 150 ms，缓存GO_PRICE_TTL 秒；手动刷新时不再卡这一下。
    """
    global GO_PRICE, _GO_PRICE_AT
    if not force and GO_PRICE and time.time() - _GO_PRICE_AT < GO_PRICE_TTL:
        return
    path = OC_DB
    try:
        c = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
        row = c.execute(
            "select value from kv where key='models-dev:v2:catalog'").fetchone()
        c.close()
        cat = json.loads(json.loads(row[0])["body"])
        models = (cat.get("opencode-go") or {}).get("models") or {}
    except Exception:
        return

    out = {}
    for mid, md in models.items():
        cost = md.get("cost") or {}
        text = cost.get("text") or {}
        pin = (text.get("input") or {}).get("price")
        pout = (text.get("output") or {}).get("price")
        pc = ((text.get("input") or {}).get("cache") or {}).get("read")
        if pin is None and pout is None:
            continue
        muls = []
        for mul in cost.get("multipliers") or []:
            f = float(mul.get("factor") or 1)
            if f > 1 and mul.get("when"):
                muls.append((f, mul["when"]))
        out[mid] = (float(pin or 0), float(pout or 0), float(pc or 0), muls)
    GO_PRICE = out
    _GO_PRICE_AT = time.time()


def go_est_usd(rec):
    """按官方单价折算这条Go 请求值多少钱；没有单价返回None。"""
    p = GO_PRICE.get(rec["model"])
    if not p:
        return None
    pin, pout, pc, muls = p
    u = dt.datetime.fromtimestamp(rec["ts"], dt.timezone.utc)
    mult = 1.0
    for f, when in muls:
        if _when_hit(when, u):
            mult = max(mult, f)
    return (rec["in"] * pin + (rec["out"] + rec["think"]) * pout
            + rec["cache"] * pc) / 1e6 * mult


# 实测（2026-11-04 实验）：花 $0.85 让面板月度从 9% 涨到 13%。
# 反推账号级月度总额度≈$21，跟模型表上的$100/$60/$15 无关。
GO_POOL_HINT = 20

# opencode Go（10/月）各模型的月度额度（美元），来自官方文档
# https://opencode.ai/v2/docs/console/go/  ——改价就改这张行
GO_CAP = {
    "deepseek-v4.1-flash": 100,
    "deepseek-v4-pro": 15,
    "glm-5.3-flash": 60,
    "glm-5.3": 15,
    "glm-5.2": 60,
    "kimi-k3.1-pro": 15,
    "kimi-k3": 15,
    "kimi-k2.7-code": 60,
    "longcat-2.0": 60,
    "step-5-preview": 15,
    "mimo-v2.6-flash": 60,
    "mimo-v2.6-pro": 15,
    "mimo-v2.5": 60,
    "mimo-v2.5-pro": 15,
    "minimax-m3.1-flash": 20,
    "minimax-m3": 60,
    "hy4-preview": 30,
    "hy3": 60,
    "grok-4.7": 15,
    "gpt-6-luna": 15,
    "gpt-5.6-luna": 15,
    "claude-haiku-5.5": 15,
    "qwen3.8-max": 15,
    "qwen3.8-flash": 30,
    "qwen3.7-plus": 60,
    "gemini-4-argon": 30,
}


def fmt_clock(ts):
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def fmt_clock2(ts):
    """悬浮窗用：时:分秒，方便和控制台逐条对账。"""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def fmt_left(ts_ms):
    """距离重置还有多久（8 分钟 / 4 天 17 时 / 24 天 9 时）。"""
    if not ts_ms:
        return "—"
    left = ts_ms / 1000.0 - time.time()
    if left <= 0:
        return "已重置"
    if left < 3600:
        return "%d 分钟" % max(1, int(left / 60))
    if left < 86400:
        return "%d 小时" % int(left / 3600)
    d, h = int(left / 86400), int(left % 86400 / 3600)
    return "%d 天 %d 时" % (d, h)


# opencode 桌面版把本地服务的地址和口令写在这个文件里，
# 拿到它就能问服务要真实额度，不用登录网页、不用截图。
def fetch_go_limits(timeout=6):
    """向 opencode 本地服务要 Go 额度。

    返回 (go, 错误说明)。go = {"rolling": {...}, "weekly": {...}, "monthly": {...}}
    每个窗口：percent = 已用百分比，resetsAt = 重置时间（毫秒）。
    """
    try:
        with open(OC_SERVICE, "r", encoding="utf-8") as f:
            svc = json.load(f)
        url, pwd = svc["url"], svc.get("password") or ""
    except Exception:
        return None, "opencode 没开"

    tok = base64.b64encode(("opencode:" + pwd).encode()).decode()
    req = urllib.request.Request(
        url.rstrip("/") + "/api/experimental/usage/limits",
        headers={"Authorization": "Basic " + tok})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
    except Exception:
        return None, "接口不通"

    go = ((body or {}).get("data") or {}).get("go")
    if not go:
        return None, "没登录账号"
    return go, ""


def load_cfg():
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(cfg):
    try:
        ensure_dirs()
        tmp = CFG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)      # 断电也不会留下半截配置
    except Exception:
        pass


def go_value_since(records, since):
    """某时间点之后，Go 套餐内消耗折算成多少钱。"""
    total = 0.0
    for r in records:
        if r["ts"] >= since and bill_of(r) == "额度":
            total += go_est_usd(r) or 0
    return total


def append_quota_row(row):
    """把一次观测写入 CSV，方便事后反推百分比的真实分母。"""
    try:
        os.makedirs(os.path.dirname(QUOTA_LOG), exist_ok=True)
        new = not os.path.exists(QUOTA_LOG)
        with open(QUOTA_LOG, "a", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["时间", "本地5小时$", "本地本周$", "本地本月$",
                            "面板5小时%", "面板本周%", "面板本月%",
                            "反推月上限", "当前模型"])
            w.writerow(row)
    except Exception:
        pass


def short_model(m):
    """悬浮窗窄，模型名去掉厂商前缀。"""
    m = m or "?"
    low = m.lower()
    if low.startswith("deepseek-"):
        return m[len("deepseek-"):]
    return m


def day_start(ts=None):
    d = dt.datetime.fromtimestamp(ts or time.time())
    return time.mktime(d.replace(hour=0, minute=0, second=0, microsecond=0).timetuple())


def week_start():
    d = dt.datetime.now()
    d = d - dt.timedelta(days=d.weekday())
    return time.mktime(d.replace(hour=0, minute=0, second=0, microsecond=0).timetuple())


def month_start():
    d = dt.datetime.now()
    return time.mktime(d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timetuple())


def mult_of(rec):
    rid = rec["rid"]
    if rec["lane"] in ("opencode", "桌面"):
        return "-"
    if rid in MODE_MULT:
        return "%.2fx" % MODE_MULT[rid]
    m = MULT.get(rec["model"].lower())
    if m is None:
        return "-"
    return "免费" if m == 0 else "%.2fx" % m


def bill_of(rec):
    """这条记录到底按什么收钱：美元 / 积分 / 额度 / 免费 / 本地。"

    注意"额度"和"免费"是两回事：
      · opencode Go 的收费模型（providerID=opencode-go）不按token 收美元，
        日志里 cost 恒为 0，但它扣的是 Go 套餐的Rolling/Weekly/Monthly 额度。
      · -free 结尾的模型才是真免费。
    """
    if rec["lane"] == "本地":
        return "本地"
    if rec["usd"] > 0:
        return "美元"
    if rec["lane"] == "桌面":
        return "额度" if (rec.get("rid") or "").startswith("opencode-go") else "免费"
    if rec["lane"] == "opencode":
        return "免费"          # 走网关但这轮 list_cost 是0
    if rec.get("credit") is not None:
        return "免费" if rec["credit"] == 0 else "积分"
    if rec.get("rid") in MODE_MULT:
        return "免费" if MODE_MULT[rec["rid"]] == 0 else "积分"
    m = MULT.get(rec["model"].lower())
    if m is None:
        return "积分" if rec["lane"] == "内置" else "?"
    return "免费" if m == 0 else "积分"


def money_text(rec):
    """悬浮窗最后一列显示什么。"""
    b = bill_of(rec)
    if b == "美元":
        return fmt_usd(rec["usd"])
    if b == "额度":
        est = go_est_usd(rec)
        return fmt_usd(est) if est else "额度"
    if b == "积分":
        cr = rec.get("credit")
        return ("%.2f分" % cr) if cr is not None else "积分"
    return b


# ---------------------------------------------------------------- 托盘图标
# 纯 ctypes 实现，不装第三方库；失败就不托盘、窗口留在原地
import ctypes

try:                       # 托盘 / 多屏是 Windows 专属，其它系统上只是没有这几个功能
    from ctypes import wintypes
except Exception:          # pragma: no cover - 非 Windows
    wintypes = None


if wintypes is not None:

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    class _NID(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeoutOrVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", wintypes.HICON),
        ]

    _WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
else:                      # pragma: no cover - 非 Windows
    _GUID = _NID = None
    _WNDPROC = None


class TrayIcon:
    """任务栏托盘图标：左键单击回调 on_click。"""

    WM_TRAY = 0x0400 + 1          # WM_APP + 1
    NIM_ADD, NIM_DELETE = 0, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4

    def __init__(self, tip, on_click):
        self.ok = False
        if _WNDPROC is None:      # 非 Windows：托盘不可用，调用方会退回普通窗口
            return
        self.on_click = on_click
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._shell32 = ctypes.windll.shell32
        self._proc = _WNDPROC(self._wndproc)

        self._user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                                wintypes.WPARAM, wintypes.LPARAM]
        self._user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self._shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                                    ctypes.POINTER(_NID)]
        self._shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        hinst = self._kernel32.GetModuleHandleW(None)
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "TokenLedgerTray"
        if not self._user32.RegisterClassW(ctypes.byref(wc)):
            return
        self.hwnd = self._user32.CreateWindowExW(
            0, "TokenLedgerTray", "TokenLedgerTray", 0, 0, 0, 0, 0,
            wintypes.HWND(-3), None, hinst, None)   # HWND_MESSAGE
        if not self.hwnd:
            return

        self.nid = _NID()
        self.nid.cbSize = ctypes.sizeof(_NID)
        self.nid.hWnd = self.hwnd
        self.nid.uID = 1
        self.nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        self.nid.uCallbackMessage = self.WM_TRAY
        self.nid.hIcon = self._user32.LoadIconW(None, 32512)   # IDI_APPLICATION
        self.nid.szTip = tip[:120]
        self.ok = bool(self._shell32.Shell_NotifyIconW(self.NIM_ADD,
                                                       ctypes.byref(self.nid)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == self.WM_TRAY:
            event = lparam & 0xFFFF
            if event in (0x0202, 0x0205):          # 左键 / 右键抬起
                try:
                    self.on_click()
                except Exception:
                    pass
        return self._user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def remove(self):
        if self.ok:
            try:
                self._shell32.Shell_NotifyIconW(self.NIM_DELETE,
                                                ctypes.byref(self.nid))
            except Exception:
                pass
            self.ok = False


# ---------------------------------------------------------------- 主窗口

try:                       # --check 只要文本输出，没装 tk 的机器也能跑
    import tkinter as tk
    from tkinter import ttk, messagebox
except Exception:          # pragma: no cover - 无图形环境
    tk = None
    ttk = messagebox = None


# ---------------------------------------------------------------- 多显示器定位
def work_areas():
    """所有显示器的可用工作区（已扣掉任务栏）。失败就返回空，由调用方兜底。"""
    areas = []
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        PROC = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.POINTER(RECT), ctypes.c_void_p)

        def _cb(hmon, hdc, lprc, data):
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                r = mi.rcWork
                areas.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        ctypes.windll.user32.EnumDisplayMonitors(None, None, PROC(_cb), 0)
    except Exception:
        pass
    return areas


_AREA_CACHE = {"at": 0.0, "list": []}


def work_areas_cached():
    """work_areas() 要枚举显示器（每次几十毫秒），贴边轮询别每 120 ms 都做。"""
    now = time.time()
    if not _AREA_CACHE["list"] or now - _AREA_CACHE["at"] > 10:
        _AREA_CACHE["list"] = work_areas()
        _AREA_CACHE["at"] = now
    return _AREA_CACHE["list"]


def clamp_pos(x, y, w, h, areas):
    """把窗口塞回某块屏幕里。拔掉显示器 / 换分辨率后不会跑到看不见的地方。"""
    if not areas:
        return x, y
    cx, cy = x + w // 2, y + h // 2
    chosen = None
    for a in areas:                       # 1) 窗口中心落在哪块屏，就放哪块
        if a[0] <= cx < a[0] + a[2] and a[1] <= cy < a[1] + a[3]:
            chosen = a
            break
    if chosen is None:                    # 2) 否则取重叠面积最大的那块
        best = -1
        for a in areas:
            ox = min(x + w, a[0] + a[2]) - max(x, a[0])
            oy = min(y + h, a[1] + a[3]) - max(y, a[1])
            area = max(0, ox) * max(0, oy)
            if area > best:
                best, chosen = area, a
    if chosen is None:
        chosen = areas[0]
    ax, ay, aw, ah = chosen
    return (min(max(x, ax), ax + max(0, aw - w)),
            min(max(y, ay), ay + max(0, ah - h)))


class Ledger:
    def __init__(self, store):
        self.store = store
        self.detail = None
        self._drag = None
        self.credit_age = 0.0
        self.title_age = 0.0
        self.quota_age = 0.0
        self.cfg = load_cfg()
        if store.demo:
            # 演示用的余额基准，不写回配置文件
            self.cfg.setdefault("credit_left", 2680.00)
            self.cfg.setdefault("credit_left_at", time.time() - 26 * 3600)
        self.go_limits = None
        self.pinned = bool(self.cfg.get("pin", True))
        self.tray = None
        self._quota_busy = False
        self._quota_pending = None

        # 贴边自动收起的状态
        self.dock = self.cfg.get("dock")
        if self.dock not in ("left", "right", "top", "bottom"):
            self.dock = None
        self._dock_hidden = False
        self._dock_anim = None
        self._dock_hold = 0.0
        self._dock_armed = False      # 鼠标先碰过窗口，才开始自动收（避免刚启动就缩走）
        self._dock_lock = False       # 收起后要等鼠标离开那一条，才允许再弹出
        self._dock_msg_job = None

        self.root = tk.Tk()
        self.root.report_callback_exception = self._on_tk_error
        self.root.title("Token 账本")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", self.pinned)
        self.root.overrideredirect(True)

        w, h = 560, 452
        pos = self.cfg.get("win_pos")
        if isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
        else:
            sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
            x, y = sw - w - 28, sh - h - 90
        x, y = clamp_pos(x, y, w, h, work_areas_cached())
        if self.dock:
            x, y = self._dock_xy(self.dock, False, x, y, w, h,
                                 self._area_at(x, y, w, h))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

        # 顶部标题条
        bar = tk.Frame(self.root, bg=BG_ALT, height=26)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        title = tk.Label(bar, text="Token 账本", bg=BG_ALT, fg=FG_DIM, font=FONT_UI)
        title.pack(side="left", padx=10)
        self.title_lbl = title
        self.state_lbl = tk.Label(bar, text="●", bg=BG_ALT, fg=FG_DIM, font=("Consolas", 10))
        self.state_lbl.pack(side="left")
        help_lbl = tk.Label(bar, text="?", bg=BG_ALT, fg="#6b7385",
                            font=("Microsoft YaHei UI", 8, "bold"), padx=6,
                            cursor="hand2")
        help_lbl.pack(side="left")
        help_lbl.bind("<Button-1>", lambda e: self.show_help())
        close = tk.Label(bar, text="✕", bg=BG_ALT, fg=FG_DIM, font=FONT_UI, cursor="hand2")
        close.pack(side="right", padx=10)
        close.bind("<Button-1>", lambda e: self.quit())

        self.btn_refresh = tk.Label(bar, text="↻ 刷新", bg="#2b3140", fg=ACCENT,
                                    font=("Microsoft YaHei UI", 8, "bold"), padx=8,
                                    cursor="hand2")
        self.btn_refresh.pack(side="right", padx=(0, 6))
        self.btn_refresh.bind("<Button-1>", lambda e: self.manual_refresh())

        btn_d = tk.Label(bar, text="明细", bg=BG_ALT, fg=ACCENT, font=FONT_UI, cursor="hand2")
        btn_d.pack(side="right", padx=6)
        btn_d.bind("<Button-1>", lambda e: self.open_detail())

        self.only_oc = False
        self.filter_lbl = tk.Label(bar, text="全部", bg="#262b35", fg=FG_DIM,
                                   font=("Microsoft YaHei UI", 8), padx=8, cursor="hand2")
        self.filter_lbl.pack(side="right", padx=8)
        self.filter_lbl.bind("<Button-1>", lambda e: self.toggle_filter())

        # 默认手动：不点刷新就不动；自动模式可点"自动"打开。
        self.auto = bool(self.cfg.get("auto", False))
        self.auto_lbl = tk.Label(bar, text="", bg=BG_ALT, fg="#5b6474",
                                 font=("Microsoft YaHei UI", 8), padx=6, cursor="hand2")
        self.auto_lbl.pack(side="right")
        self.auto_lbl.bind("<Button-1>", lambda e: self.toggle_auto())
        self.sync_auto_lbl()

        self.pin_lbl = tk.Label(bar, text="悬浮 开" if self.pinned else "悬浮 关",
                                bg=BG_ALT, fg=ACCENT if self.pinned else "#5b6474",
                                font=("Microsoft YaHei UI", 8), padx=6, cursor="hand2")
        self.pin_lbl.pack(side="right")
        self.pin_lbl.bind("<Button-1>", lambda e: self.toggle_pin())

        self.tray_lbl = tk.Label(bar, text="托盘", bg=BG_ALT, fg=FG_DIM,
                                 font=("Microsoft YaHei UI", 8), padx=6, cursor="hand2")
        self.tray_lbl.pack(side="right")
        self.tray_lbl.bind("<Button-1>", lambda e: self.hide_to_tray())

        for wdg in (bar, title, self.state_lbl):
            wdg.bind("<Button-1>", self._drag_start)
            wdg.bind("<B1-Motion>", self._drag_move)
            wdg.bind("<ButtonRelease-1>", self._drag_end)

        # 账户区①：opencode Go 额度（真实值，来自本地服务接口）
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill="x", padx=14, pady=(6, 0))

        head = tk.Frame(top, bg=BG)
        head.pack(side="left")

        tk.Label(head, text="opencode Go", bg=BG, fg=FG_DIM, font=FONT_UI,
                 anchor="w").grid(row=0, column=0, sticky="w")
        for c, txt in ((1, "已用"), (2, "剩余"), (3, "重置")):
            tk.Label(head, text=txt, bg=BG, fg="#5b6474", font=("Microsoft YaHei UI", 8),
                     anchor="e", width=5 if c < 3 else 7).grid(row=0, column=c, sticky="e")

        self.meter_lbl = {}
        self.meter_left = {}
        names = (("rolling", "这5 小时"), ("weekly", "本周"), ("monthly", "本月"))
        for i, (key, label) in enumerate(names, start=1):
            tk.Label(head, text=label, bg=BG, fg=FG_DIM, font=FONT_UI, anchor="w",
                     width=10).grid(row=i, column=0, sticky="w")
            used = tk.Label(head, text="—", bg=BG, fg=GOLD, font=FONT_NUM,
                            anchor="e", width=5)
            used.grid(row=i, column=1, sticky="e")
            left = tk.Label(head, text="—", bg=BG, fg=GREEN, font=FONT_NUM_B,
                            anchor="e", width=5)
            left.grid(row=i, column=2, sticky="e")
            rst = tk.Label(head, text="—", bg=BG, fg=FG_DIM,
                           font=("Microsoft YaHei UI", 8), anchor="e", width=7)
            rst.grid(row=i, column=3, sticky="e")
            self.meter_lbl[key] = used
            self.meter_left[key] = (left, rst)

        # 账户区②：WorkBuddy 积分（实扣，余额需手填）

        # 右侧空白：今日按模型 Top（多了可以滚）
        right = tk.Frame(top, bg=BG)
        right.pack(side="right", anchor="n", padx=(12, 0))
        self.mframe = right
        self.mhead = tk.Label(right, text="今日模型", bg=BG, fg="#5b6474",
                              font=("Microsoft YaHei UI", 8), anchor="w")
        self.mhead.grid(row=0, column=0, sticky="we")
        self.mhead.bind("<MouseWheel>", lambda e: "break")
        for c, (txt, px, anc) in enumerate(
                (("", 124, "w"), ("Token", 44, "e"), ("总花费", 56, "e"))):
            if txt:
                tk.Label(right, text=txt, bg=BG, fg="#5b6474",
                         font=("Microsoft YaHei UI", 8), anchor=anc).grid(
                    row=0, column=c, sticky="we")
            right.grid_columnconfigure(c, minsize=px)

        self.mrow = []
        for i in range(1, 4):
            cells = []
            for c, (anc, fnt) in enumerate(
                    (("w", FONT_UI), ("e", FONT_NUM), ("e", FONT_NUM_B))):
                lb = tk.Label(right, text="", bg=BG, fg=FG, font=fnt, anchor=anc)
                lb.grid(row=i, column=c, sticky="we")
                lb.bind("<MouseWheel>", self._model_wheel)
                cells.append(lb)
            self.mrow.append(cells)
        self.moffset = 0
        self.model_rows = []
        right.bind("<MouseWheel>", self._model_wheel)

        # 账户区②：WorkBuddy 积分（实扣，余额需手填）
        wb = tk.Frame(self.root, bg=BG)
        wb.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(wb, text="WorkBuddy 积分", bg=BG, fg=FG_DIM, font=FONT_UI,
                 anchor="w").grid(row=0, column=0, sticky="w")
        self.cr_today = tk.Label(wb, text="今日 —", bg=BG, fg=GOLD,
                                 font=("Consolas", 9, "bold"), anchor="w")
        self.cr_today.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.cr_total = tk.Label(wb, text="累计 —", bg=BG, fg=FG_DIM,
                                 font=("Consolas", 9), anchor="w")
        self.cr_total.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.cr_left = tk.Label(wb, text="剩 —", bg=BG, fg=GREEN,
                                font=("Consolas", 9, "bold"), anchor="e", cursor="hand2")
        self.cr_left.grid(row=0, column=3, sticky="e")
        wb.columnconfigure(3, weight=1)
        self.cr_left.bind("<Button-1>", lambda e: self.edit_credit_left())

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", padx=12, pady=6)

        # 表头（与列表同宽，居中）
        self.list_head = tk.Frame(self.root, bg=BG)
        self.list_head.pack(fill="x", padx=(12, 12 + SBAR_W))

        # 列表区：Canvas + 真滚动条
        wrap = tk.Frame(self.root, bg=BG)
        wrap.pack(fill="both", expand=True, padx=(12, 12 + SBAR_W - SBAR_W))
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0, bd=0,
                                height=LIVE_ROWS * ROW_H)
        self.vbar = tk.Scrollbar(wrap, orient="vertical", width=SBAR_W,
                                 command=self.canvas.yview, bg="#5c6678",
                                 troughcolor="#0f1116", activebackground=ACCENT,
                                 bd=0, highlightthickness=0, relief="flat",
                                 elementborderwidth=0)
        self.canvas.configure(yscrollcommand=self.vbar.set,
                              yscrollincrement=ROW_H)
        self.vbar.pack(side="right", fill="y", padx=(2, 0))
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(
            self._win, width=e.width))

        self.root.bind_all("<MouseWheel>", self._wheel)

        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", padx=12, pady=(4, 8))
        self.beat_lbl = tk.Label(foot, text="", bg=BG, fg="#5b6474", font=("Microsoft YaHei UI", 8))
        self.beat_lbl.pack(side="right")
        self.foot_lbl = tk.Label(foot, text="读取中…", bg=BG, fg=FG_DIM, font=FONT_UI)
        self.foot_lbl.pack(side="left")

    def _on_tk_error(self, exc, val, tb):
        """任何回调里的异常都写进 logs/error.log，pythonw 下也能看见。"""
        import traceback
        try:
            ensure_dirs()
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write("".join(traceback.format_exception(exc, val, tb)))
        except Exception:
            pass

    def boot(self):
        """先出窗口再扫描，免得双击后一两秒没反应。首次扫描约 0.8 秒。"""
        try:
            self.root.deiconify()      # overrideredirect 的窗口偶尔不自动 map
            self.root.lift()
        except tk.TclError:
            pass
        self.beat_lbl.config(text="扫描中…", fg=FG_DIM)
        self.root.update_idletasks()
        t0 = time.time()
        n = len(self.store.records) if self.store.demo else self.store.scan()
        credits = self.store.reload_credits()
        if credits:
            self.store.apply_credits(credits)
        self.store.load_titles()
        if not self.store.demo:
            load_go_prices()
        self.refresh()
        self.show_credit_left()
        self.fetch_quota()
        self.beat_lbl.config(text="%s 已读 %s 条（%.1f 秒）" % (
            time.strftime("%H:%M:%S"), "{:,}".format(n), time.time() - t0), fg="#5b6474")
        self.tick()
        self.root.after(DOCK_POLL_MS, self._dock_tick)

    # ---- 拖动
    def _drag_start(self, e):
        if self._dock_hidden:          # 藏在边上的那一条：先弹出来，别直接拖
            self._dock_reveal(instant=True)
            return
        self._dock_hold = time.time()
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry("+%d+%d" % (e.x_root - self._drag[0], e.y_root - self._drag[1]))

    def _drag_end(self, e):
        """松手：贴到屏幕边就吸附并记住，否则当普通位置存。"""
        if self._drag is None:
            return
        self._drag = None
        self._dock_snap()

    def _save_pos(self):
        try:
            if self.dock:
                x, y = self._dock_xy(self.dock, False, self.root.winfo_x(),
                                     self.root.winfo_y(), self.root.winfo_width(),
                                     self.root.winfo_height(),
                                     self._area_at(self.root.winfo_x(),
                                                   self.root.winfo_y(),
                                                   self.root.winfo_width(),
                                                   self.root.winfo_height()))
            else:
                x, y = self.root.winfo_x(), self.root.winfo_y()
            self.cfg["win_pos"] = [x, y]
            self.cfg["dock"] = self.dock
            save_cfg(self.cfg)
        except tk.TclError:
            pass

    # ---- 贴边自动收起
    def _area_at(self, x, y, w, h):
        """窗口中心落在哪块显示器上，就返回那块的工作区。"""
        areas = work_areas_cached()
        if not areas:
            return (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        cx, cy = x + w // 2, y + h // 2
        for a in areas:
            if a[0] <= cx < a[0] + a[2] and a[1] <= cy < a[1] + a[3]:
                return a
        return min(areas, key=lambda a: abs(a[0] + a[2] // 2 - cx) +
                   abs(a[1] + a[3] // 2 - cy))

    def _dock_xy(self, edge, hidden, x, y, w, h, area):
        """算出贴边状态下的窗口左上角。hidden=True 时只留 DOCK_PEEK 像素在屏内。"""
        ax, ay, aw, ah = area
        if edge == "left":
            x = ax - (w - DOCK_PEEK) if hidden else ax
            y = min(max(y, ay), ay + max(0, ah - h))
        elif edge == "right":
            x = ax + aw - (DOCK_PEEK if hidden else w)
            y = min(max(y, ay), ay + max(0, ah - h))
        elif edge == "top":
            y = ay - (h - DOCK_PEEK) if hidden else ay
            x = min(max(x, ax), ax + max(0, aw - w))
        else:                                   # bottom
            y = ay + ah - (DOCK_PEEK if hidden else h)
            x = min(max(x, ax), ax + max(0, aw - w))
        return x, y

    def _dock_snap(self):
        """松手时判断要不要贴边；贴上了就把另一条轴夹回屏内并开启自动收起。"""
        x, y = self.root.winfo_x(), self.root.winfo_y()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        area = self._area_at(x, y, w, h)
        ax, ay, aw, ah = area
        cand = []
        if x - ax <= DOCK_EDGE:
            cand.append(("left", abs(x - ax)))
        if (ax + aw) - (x + w) <= DOCK_EDGE:
            cand.append(("right", abs((ax + aw) - (x + w))))
        if y - ay <= DOCK_EDGE:
            cand.append(("top", abs(y - ay)))
        if (ay + ah) - (y + h) <= DOCK_EDGE:
            cand.append(("bottom", abs((ay + ah) - (y + h))))
        edge = min(cand, key=lambda t: t[1])[0] if cand else None

        self.dock = edge
        self._dock_hidden = False
        if edge:
            # 贴边收起必须置顶：不然弹出来会被别的窗口盖住，看着像没反应。
            turned_on = False
            if not self.pinned:
                self.pinned = True
                self.cfg["pin"] = True
                try:
                    self.root.attributes("-topmost", True)
                except tk.TclError:
                    pass
                self.sync_pin_lbl()
                turned_on = True
            nx, ny = self._dock_xy(edge, False, x, y, w, h, area)
            self.root.geometry("+%d+%d" % (nx, ny))
            self._dock_hold = time.time()
            self._dock_armed = True     # 手刚离开窗口，先别急着收
            self._dock_lock = False
            side = {"left": "左", "right": "右", "top": "上", "bottom": "下"}[edge]
            self._dock_msg("贴住%s边 · %s移开鼠标自动收起" % (
                side, "已开悬浮，" if turned_on else ""))
        else:
            self._dock_armed = False
            self._dock_lock = False
        self._save_pos()

    def _dock_msg(self, text):
        """标题栏临时换成提示语，4 秒后还原；不跟正文抢地方。"""
        try:
            self.title_lbl.config(text=text, fg=ACCENT)
            if self._dock_msg_job:
                try:
                    self.root.after_cancel(self._dock_msg_job)
                except Exception:
                    pass

            def restore():
                self._dock_msg_job = None
                try:
                    self.title_lbl.config(text="Token 账本", fg=FG_DIM)
                except tk.TclError:
                    pass

            self._dock_msg_job = self.root.after(4000, restore)
        except tk.TclError:
            pass

    def _slide_to(self, tx, ty):
        """滑过去（120 ms 左右），不平移跳变，看着不闪。"""
        if self._dock_anim:
            try:
                self.root.after_cancel(self._dock_anim)
            except Exception:
                pass
            self._dock_anim = None
        x0, y0 = self.root.winfo_x(), self.root.winfo_y()
        steps = 10

        def step(i):
            self._dock_anim = None
            try:
                if i >= steps:
                    self.root.geometry("+%d+%d" % (tx, ty))
                    return
                f = 1.0 - (1.0 - i / float(steps)) ** 2
                self.root.geometry("+%d+%d" % (int(round(x0 + (tx - x0) * f)),
                                               int(round(y0 + (ty - y0) * f))))
                self._dock_anim = self.root.after(12, lambda: step(i + 1))
            except tk.TclError:
                self._dock_anim = None

        step(0)

    def _dock_reveal(self, instant=False):
        if not self.dock or (not self._dock_hidden and not instant):
            return
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        tx, ty = self._dock_xy(self.dock, False, x, y, w, h, self._area_at(x, y, w, h))
        self._dock_hidden = False
        self._dock_lock = True      # 弹出来以后，鼠标得先离开那一条
        self._dock_hold = time.time()
        if instant:
            try:
                self.root.geometry("+%d+%d" % (tx, ty))
            except tk.TclError:
                pass
        else:
            self._slide_to(tx, ty)

    def _dock_hide(self):
        if not self.dock or self._dock_hidden:
            return
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()
        tx, ty = self._dock_xy(self.dock, True, x, y, w, h, self._area_at(x, y, w, h))
        self._dock_hidden = True
        self._dock_lock = True      # 鼠标还在原来的位置，先别又弹回来
        self._slide_to(tx, ty)

    def _child_open(self):
        """明细 / 统计窗开着时别收主窗，不然点开就没影了。"""
        for attr in ("detail", "stats"):
            obj = getattr(self, attr, None)
            if obj is None:
                continue
            try:
                if obj.win.winfo_exists() and obj.win.winfo_viewable():
                    return True
            except Exception:
                continue
        return False

    def _dock_tick(self):
        try:
            self._dock_poll()
        except tk.TclError:
            return
        except Exception:
            self._on_tk_error(*sys.exc_info())
        try:
            self.root.after(DOCK_POLL_MS, self._dock_tick)
        except tk.TclError:
            pass

    def _dock_poll(self):
        if not self.dock or self._drag is not None or self._dock_anim is not None:
            return
        if not self.root.winfo_ismapped() or self.root.state() != "normal":
            return
        if not self.pinned:
            # 不置顶时收起没意义：弹出来也会被别的窗口盖住。干脆保持展开。
            if self._dock_hidden:
                self._dock_reveal()
            return
        if self._child_open():
            self._dock_hold = time.time()
            if self._dock_hidden:
                self._dock_reveal()
            return

        px, py = self.root.winfo_pointerxy()
        w, h = self.root.winfo_width(), self.root.winfo_height()
        x, y = self.root.winfo_x(), self.root.winfo_y()

        if self._dock_hidden:
            ax, ay, aw, ah = self._area_at(x, y, w, h)
            l = max(x, ax) - 4
            r = min(x + w, ax + aw) + 4
            t = max(y, ay) - 4
            b = min(y + h, ay + ah) + 4
            if l <= px <= r and t <= py <= b:
                if not self._dock_lock:
                    self._dock_reveal()
            else:
                self._dock_lock = False
            return

        if x - 2 <= px <= x + w + 2 and y - 2 <= py <= y + h + 2:
            self._dock_armed = True
            self._dock_hold = time.time()
            return
        if self._dock_armed and (time.time() - self._dock_hold) * 1000.0 >= DOCK_GRACE_MS:
            self._dock_hide()

    # ---- 数据汇总
    def totals(self):
        now = time.time()
        t0, w0, m0 = day_start(now), week_start(), month_start()
        t = {"usd_today": 0.0, "usd_month": 0.0, "usd_all": 0.0,
             "allow_today": 0.0, "allow_month": 0.0,
             "bi_today": 0, "bi_month": 0, "bi_all": 0,
             "oc_today": 0, "oc_month": 0,
             "ocd_today": 0, "ocd_all": 0, "ocd_usd_all": 0.0,
             "cr_today": 0.0, "cr_all": 0.0, "cr_turns": 0, "cr_turns_today": 0}
        for r in self.store.records:
            ts = r["ts"]
            if r["lane"] == "opencode":
                t["usd_all"] += r["usd"]
                if ts >= t0:
                    t["usd_today"] += r["usd"]; t["allow_today"] += r["allow"]; t["oc_today"] += 1
                if ts >= m0:
                    t["usd_month"] += r["usd"]; t["allow_month"] += r["allow"]; t["oc_month"] += 1
            elif r["lane"] == "桌面":
                t["ocd_all"] += 1
                t["ocd_usd_all"] += r["usd"]
                if ts >= t0:
                    t["ocd_today"] += 1
                    t["usd_today"] += r["usd"]
                t["usd_all"] += r["usd"]
            elif r["lane"] == "内置":
                tok = r["in"] + r["out"] + r["cache"]
                if ts >= t0:
                    t["bi_today"] += tok
                if ts >= m0:
                    t["bi_month"] += tok
                t["bi_all"] += tok
        # 积分按轮计：只认该轮最后一条记录上挂的那一个数
        for rec in self.store.turn_last.values():
            cr = rec.get("credit")
            if cr is None:
                continue
            t["cr_all"] += cr
            t["cr_turns"] += 1
            if rec["ts"] >= t0:
                t["cr_today"] += cr
                t["cr_turns_today"] += 1
        return t

    def toggle_filter(self):
        self.only_oc = not self.only_oc
        self.filter_lbl.config(text="只看 opencode" if self.only_oc else "全部",
                               fg=ACCENT if self.only_oc else FG_DIM)
        self.refresh()

    def _wheel(self, e):
        """滚轮翻历史；在明细窗口上滚不拦。"""
        try:
            if e.widget.winfo_toplevel() is not self.root:
                return
        except Exception:
            return
        # 鼠标在右上角模型榜上滚动，不动流水
        p = self.root.winfo_containing(e.x_root, e.y_root)
        while p is not None and p is not self.root:
            if p is self.mframe:
                return self._model_wheel(e)
            p = getattr(p, "master", None)
        step = -1 if e.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")
        return "break"

    # ---- 池子
    def _pool(self):
        pool = self.store.records
        if self.only_oc:
            pool = [r for r in pool if r["lane"] in ("opencode", "桌面")]
        return pool

    def sessions_today(self):
        """今日按任务聚合：一个任务一行。"""
        t0 = day_start()
        agg = {}
        for r in self._pool():
            if r["ts"] < t0:
                continue
            k = r.get("sess") or r["lane"]
            a = agg.get(k)
            if a is None:
                a = agg[k] = {"sess": k, "lane": r["lane"],
                              "name": self.store.task_name(r, 25),
                              "usd": 0.0, "credit": 0.0, "n": 0, "last": 0.0,
                              "has_credit": False}
            a["usd"] += r["usd"]
            a["n"] += 1
            if r["ts"] > a["last"]:
                a["last"] = r["ts"]
                a["lane"] = r["lane"]
            if r.get("credit") is not None:
                a["credit"] += r["credit"]
                a["has_credit"] = True
        return sorted(agg.values(), key=lambda a: -a["last"])

    # ---- 渲染
    def _mkrow(self, parent, specs, bg, center=False):
        """按像素定死每列宽，保证表头和数据行严格对齐。"""
        row = tk.Frame(parent, bg=bg)
        row.pack(anchor="center" if center else "w")
        out = []
        for c, (text, px, anchor, fg, font, pad) in enumerate(specs):
            lb = tk.Label(row, text=text, bg=bg, fg=fg, font=font,
                          anchor=anchor, padx=pad)
            lb.grid(row=0, column=c, sticky="we")
            row.grid_columnconfigure(c, minsize=px)
            out.append(lb)
        return row, out

    # (名称, 像素宽, 对齐, 左内边距)
    SPEC = [
        ("时间", 68, "w", 1), ("模型", 138, "w", 10),
        ("入", 48, "e", 1), ("出", 48, "e", 1),
        ("思", 36, "e", 1), ("缓存", 64, "e", 1), ("计费", 64, "e", 1),
    ]

    def render_list(self):
        try:
            at_top = self.canvas.yview()[0] <= 0.005
        except Exception:
            at_top = True

        for ch in self.list_head.winfo_children():
            ch.destroy()
        for ch in self.inner.winfo_children():
            ch.destroy()

        self._mkrow(self.list_head,
                    [(n, px, a, FG_DIM, FONT_UI, p) for n, px, a, p in self.SPEC],
                    BG, center=True)

        pool = self._pool()[-HISTORY:]          # 旧的 → 新的
        for i, r in enumerate(reversed(pool)):  # 最新的排最上面
            bg = BG if i % 2 == 0 else BG_ALT
            row, cells = self._mkrow(
                self.inner,
                [("", px, a, FG, FONT_UI, p) for n, px, a, p in self.SPEC],
                bg, center=True)
            b = bill_of(r)
            cols = [
                (fmt_clock2(r["ts"]), FG_DIM, FONT_NUM),
                (fit(short_model(r["model"]), 15),
                 ACCENT if r["lane"] in ("opencode", "桌面")
                 else FG_DIM if r["lane"] == "本地" else FG, FONT_UI),
                (fmt_k(r["in"]), FG, FONT_NUM),
                (fmt_k(r["out"]), FG, FONT_NUM),
                (fmt_k(r["think"]), GOLD if r["think"] else FG_DIM, FONT_NUM),
                (fmt_k(r["cache"]), FG_DIM, FONT_NUM),
                (money_text(r), {"美元": GREEN, "积分": GOLD, "额度": "#9ecbff"}.get(b, FG_DIM), FONT_NUM),
            ]
            for c, (v, fg, fnt) in enumerate(cols):
                cells[c].config(text=v, fg=fg, font=fnt)

        self.inner.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        if at_top:
            self.canvas.yview_moveto(0.0)

    def top_models(self, n=3):
        """今日按模型汇总：token 合计、真扣美元、Go 金额、积分。"""
        today = day_start()
        agg = {}
        for r in self.store.records:
            if r["ts"] < today:
                continue
            a = agg.get(r["model"])
            if a is None:
                a = agg[r["model"]] = {"tok": 0, "usd": 0.0, "go": 0.0,
                                       "credit": 0.0, "has_cr": False}
            a["tok"] += r["in"] + r["out"] + r["think"] + r["cache"]
            a["usd"] += r["usd"]
            if bill_of(r) == "额度":
                a["go"] += go_est_usd(r) or 0
            if (r.get("credit") or 0) > 0:
                a["credit"] += r["credit"]
                a["has_cr"] = True
        return sorted(agg.items(), key=lambda kv: -kv[1]["tok"])[:n]

    def _model_wheel(self, e):
        """在右上角模型榜上滚轮翻页。"""
        rows = self.model_rows
        if len(rows) <= len(self.mrow):
            return "break"
        step = 1 if e.delta < 0 else -1
        new = max(0, min(len(rows) - len(self.mrow), self.moffset + step))
        if new != self.moffset:
            self.moffset = new
            self.render_top_models()
        return "break"

    def render_top_models(self):
        self.model_rows = self.top_models(30)
        rows = self.model_rows
        if len(rows) > len(self.mrow):
            self.mhead.config(text="今日模型 %d/%d ↕" % (self.moffset + 1, len(rows)))
        else:
            self.mhead.config(text="今日模型")
            self.moffset = 0
        for i, cells in enumerate(self.mrow):
            idx = self.moffset + i
            if idx >= len(rows):
                for c in cells:
                    c.config(text="")
                continue
            model, a = rows[idx]
            cells[0].config(text=fit(short_model(model), 16), fg=FG)
            cells[1].config(text=fmt_k(a["tok"]), fg=FG_DIM)
            money = a["usd"] + a["go"]
            if money:
                txt, fg = fmt_usd(money), (GREEN if a["usd"] else "#9ecbff")
            elif a["has_cr"]:
                txt, fg = "%.0f分" % a["credit"], GOLD
            else:
                txt, fg = "免费", FG_DIM
            cells[2].config(text=txt, fg=fg)

    def go_cap_line(self):
        """本月 Go 花费 + 面板进度（额度总量按实测约 $20，不是模型表上的 $100）。"""
        used = go_value_since(self.store.records, month_start())
        lim = self.go_limits or {}
        pct = float((lim.get("monthly") or {}).get("percent") or 0)
        if not used:
            return "", FG_DIM
        fg = GREEN if pct < 60 else GOLD if pct < 85 else "#ff7b72"
        if pct >= 1:
            return ("本月 Go %s · 面板 %.0f%%（实测总额度≈$%d，不是 $100）"
                    % (fmt_usd(used), pct, GO_POOL_HINT), fg)
        return ("本月 Go %s · 面板进度不足 1%%（实测额度≈$%d）"
                % (fmt_usd(used), GO_POOL_HINT), FG_DIM)

    def refresh(self):
        t = self.totals()
        self.cr_today.config(text="今日 %s" % (("%.1f" % t["cr_today"]) if t["cr_today"] else "0"))
        self.cr_total.config(text="累计 %s" % (("%.0f" % t["cr_all"]) if t["cr_all"] else "0"))
        self.render_top_models()
        self.render_list()

        n_task = len(self.sessions_today())
        hint = "  滚轮翻页"
        self.foot_lbl.config(
            text="共 %s 条 · 内置 %s · 积分 %s · 桌面 %s 条%s"
                 % ("{:,}".format(len(self.store.records)), fmt_tok(t["bi_all"]),
                    ("%.0f" % t["cr_all"]) if t["cr_all"] else "0",
                    "{:,}".format(t["ocd_all"]), hint))
        self.show_credit_left()          # 剩余积分跟着实扣走，每次刷新都要重算
        self.state_lbl.config(fg=GREEN)

    # ---- 额度（只在点刷新时取，不自动联网）
    def log_quota(self, go):
        """每次取到额度就记一行：面板百分比 vs 本地算出的钱。"""
        if self.store.demo:
            return                       # 演示模式不往磁盘写观测记录
        now = time.time()
        try:
            p = {k: int(round(float((go.get(k) or {}).get("percent") or 0)))
                 for k in ("rolling", "weekly", "monthly")}
        except Exception:
            return
        h5 = go_value_since(self.store.records, now - 5 * 3600)
        wk = go_value_since(self.store.records, week_start())
        mo = go_value_since(self.store.records, month_start())
        cur = ""
        for r in reversed(self.store.records):
            if bill_of(r) == "额度":
                cur = r["model"]
                break
        key = (p["rolling"], p["weekly"], p["monthly"], round(mo, 3), cur)
        if key == getattr(self, "_quota_last", None):
            return                       # 没变化就不重复记
        self._quota_last = key
        cap = ("%.2f" % (mo / p["monthly"] * 100)) if p["monthly"] else ""
        append_quota_row([
            time.strftime("%Y-%m-%d %H:%M:%S"), "%.4f" % h5, "%.4f" % wk,
            "%.4f" % mo, p["rolling"], p["weekly"], p["monthly"], cap, cur])

    def fetch_quota(self):
        """额度是网络请求（约 380 ms），放后台线程，别把 2 秒循环卡住。"""
        if self.store.demo:
            now_ms = int(time.time() * 1000)
            self._apply_quota({
                "rolling": {"percent": 38, "resetsAt": now_ms + 108 * 60000},
                "weekly": {"percent": 52, "resetsAt": now_ms + 46 * 3600000},
                "monthly": {"percent": 61, "resetsAt": now_ms + 21 * 86400000},
            }, "")
            return
        if self._quota_busy:
            return
        self._quota_busy = True

        def work():
            try:
                go, err = fetch_go_limits()
            except Exception as e:
                go, err = None, type(e).__name__
            self._quota_pending = (go, err)

        threading.Thread(target=work, daemon=True).start()

    def _apply_quota(self, go, err):
        self.go_limits = go
        if not go:
            for key in ("rolling", "weekly", "monthly"):
                self.meter_lbl[key].config(text="—", fg=FG_DIM)
                left, rst = self.meter_left[key]
                left.config(text="—", fg=FG_DIM)
                rst.config(text=err)
            return
        for key in ("rolling", "weekly", "monthly"):
            m = go.get(key) or {}
            pct = int(round(float(m.get("percent") or 0)))
            left_pct = max(0, 100 - pct)
            left, rst = self.meter_left[key]
            self.meter_lbl[key].config(text="%d%%" % pct,
                                       fg=GREEN if pct < 60 else GOLD if pct < 85 else "#ff7b72")
            left.config(text="%d%%" % left_pct,
                        fg=GREEN if left_pct > 40 else GOLD if left_pct > 15 else "#ff7b72")
            rst.config(text=fmt_left(m.get("resetsAt")))
        self.log_quota(go)

    def manual_refresh(self):
        self.store.scan()
        credits = self.store.reload_credits()
        if credits:
            self.store.apply_credits(credits)
        self.store.load_titles()
        if not self.store.demo:
            load_go_prices()
        self.fetch_quota()
        self.quota_age = time.time()
        self.refresh()
        self._refresh_detail()
        self.btn_refresh.config(fg=GREEN)
        self.root.after(900, lambda: self.btn_refresh.config(fg=ACCENT))
        self.beat_lbl.config(text="%s 已刷新" % time.strftime("%H:%M:%S"), fg=FG_DIM)

    def toggle_auto(self):
        self.auto = not self.auto
        self.cfg["auto"] = self.auto
        save_cfg(self.cfg)
        self.sync_auto_lbl()
        if not self.auto:
            self.beat_lbl.config(text="%s 已读" % time.strftime("%H:%M:%S"), fg="#5b6474")

    def sync_auto_lbl(self):
        self.auto_lbl.config(text="自动 开" if self.auto else "自动 关",
                             fg=ACCENT if self.auto else "#5b6474")

    # ---- 悬浮 / 托盘
    def toggle_pin(self):
        self.pinned = not self.pinned
        try:
            self.root.attributes("-topmost", self.pinned)
        except tk.TclError:
            pass
        self.cfg["pin"] = self.pinned
        save_cfg(self.cfg)
        self.sync_pin_lbl()

    def sync_pin_lbl(self):
        self.pin_lbl.config(text="悬浮 开" if self.pinned else "悬浮 关",
                            fg=ACCENT if self.pinned else "#5b6474")

    def hide_to_tray(self):
        if self.tray is None:
            self.tray = TrayIcon("Token 账本 · 单击恢复", self.show_from_tray)
        if not self.tray.ok:
            self.beat_lbl.config(text="托盘不可用", fg="#ff7b72")
            return
        self.root.withdraw()

    def show_from_tray(self):
        try:
            self.root.deiconify()
            self.root.attributes("-topmost", self.pinned)
            self.root.lift()
            if self.dock:
                self._dock_reveal(instant=True)
            self._dock_armed = False     # 从托盘恢复后，先让人看一眼，别马上缩
        except tk.TclError:
            pass

    def quit(self):
        self._save_pos()
        if self.tray is not None:
            self.tray.remove()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def show_help(self):
        dlg = tk.Toplevel(self.root)
        dlg.overrideredirect(True)
        dlg.attributes("-topmost", True)
        dlg.configure(bg=BG_ALT)

        tk.Label(dlg, text="这些数字分别是什么", bg=BG_ALT, fg=FG,
                 font=FONT_UI_B).pack(anchor="w", padx=14, pady=(12, 6))

        body = tk.Frame(dlg, bg=BG_ALT)
        body.pack(fill="both", expand=True, padx=14)

        rows = [
            ("opencode Go（百分比）",
             "账号级额度，所有模型累加，切换模型不清零。\n"
             "近 5 小时 / 本周 / 本月 = 三个滚动窗口已用多少；\n"
             "剩余 = 100% − 已用；重置= 多久后回滚。", GOLD),
            ("现金（绿色 $）",
             "真金白银扣掉的钱：\n"
             "· WorkBuddy 自定义模型走网关，网关回传的 list_cost\n"
             "· 桌面版超出套餐时 opencode 自己算的 cost", GREEN),
            ("Go（浅蓝色 $）",
             "套餐内的消耗，按 opencode 官网单价折算。\n"
             "和控制台 Activity 里每条的 Cost 完全一致，\n"
             "但它代表“用掉了多少额度价值”，不额外扣现金。", "#9ecbff"),
            ("积分（金色）",
             "WorkBuddy 内置模型实扣的积分，官方台账口径。", GOLD),
            ("模型榜的“总花费”",
             "该模型今天的 现金 + Go 合计，所以可能比上面\n"
             "任何单个数字都大。", FG),
            ("现金 / Go / 累计现金",
             "现在都显示在【明细】窗口顶部第一行：\n"
             "现金 = 真扣的钱；Go = 套餐内折算；累计现金 = 历史现金。", GREEN),
            ("“本月 $1.12 / $100”",
             "明细顶部第二行：该模型本月用掉的钱 / 它自己的上限。\n"
             "上限是各模型的天花板，但三个百分比是账号共享的，\n"
             "所以这个 $100 不等于你能独享 100 美元。", "#9ecbff"),
            ("累计现金",
             "从开始记账到现在的现金合计，不含 Go（在明细顶部）。", FG),
            ("贴边收起",
             "把窗口拖到屏幕上/下/左/右任意一条边，松手就贴上去；\n"
             "鼠标移开约 0.4 秒自动缩到边上只剩一条，鼠标靠过去立刻弹回。\n"
             "想取消：等它弹出来后，拖离屏幕边再松手。", "#9ecbff"),
        ]
        for title, text, color in rows:
            tk.Label(body, text=title, bg=BG_ALT, fg=color, font=FONT_UI_B,
                     anchor="w").pack(anchor="w", pady=(6, 0))
            tk.Label(body, text=text, bg=BG_ALT, fg=FG_DIM, font=FONT_UI,
                     anchor="w", justify="left").pack(anchor="w")

        tk.Label(body, text="明细表的“金额”列 = 这一条请求的钱；\n"
                            "点一行可在表格上方看到你那句提问、任务名和目录。",
                 bg=BG_ALT, fg="#6b7385", font=("Microsoft YaHei UI", 8),
                 anchor="w", justify="left").pack(anchor="w", pady=(10, 0))

        btn = tk.Label(dlg, text="知道了", bg="#2b3140", fg=ACCENT,
                       font=FONT_UI_B, padx=16, pady=4, cursor="hand2")
        btn.pack(pady=12)
        btn.bind("<Button-1>", lambda e: dlg.destroy())
        dlg.bind("<Escape>", lambda e: dlg.destroy())

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        dlg.geometry("%dx%d+%d+%d" % (max(w, 430), h, rx - 40, max(80, ry - h - 20)))
        dlg.grab_set()

    # ---- WorkBuddy 余额（本地没有，只能手填）
    def ask_number(self, title, prompt, initial=""):
        """自绘输入框：overrideredirect 主窗配系统对话框会抢不到焦点。"""
        dlg = tk.Toplevel(self.root)
        dlg.overrideredirect(True)
        dlg.attributes("-topmost", True)
        dlg.configure(bg=BG_ALT)

        tk.Label(dlg, text=title, bg=BG_ALT, fg=FG, font=FONT_UI_B).pack(
            anchor="w", padx=12, pady=(10, 2))
        tk.Label(dlg, text=prompt, bg=BG_ALT, fg=FG_DIM, font=FONT_UI).pack(
            anchor="w", padx=12)
        ent = tk.Entry(dlg, bg="#0f1116", fg=FG, insertbackground=FG,
                       relief="flat", font=FONT_NUM, width=18)
        ent.pack(padx=12, pady=(8, 8))
        ent.insert(0, initial)
        ent.select_range(0, "end")
        ent.focus_set()

        out = {"v": None}

        def ok(_=None):
            out["v"] = ent.get().strip()
            dlg.destroy()

        def cancel(_=None):
            dlg.destroy()

        row = tk.Frame(dlg, bg=BG_ALT)
        row.pack(fill="x", padx=12, pady=(0, 10))
        yes = tk.Label(row, text="保存", bg="#2b3140", fg=ACCENT, font=FONT_UI_B,
                       padx=12, pady=3, cursor="hand2")
        yes.pack(side="right")
        yes.bind("<Button-1>", ok)
        no = tk.Label(row, text="取消", bg=BG_ALT, fg=FG_DIM, font=FONT_UI,
                      padx=10, pady=3, cursor="hand2")
        no.pack(side="right")
        no.bind("<Button-1>", cancel)
        ent.bind("<Return>", ok)
        dlg.bind("<Escape>", cancel)

        dlg.update_idletasks()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        dlg.geometry("+%d+%d" % (rx + 90, ry + 120))
        dlg.grab_set()
        self.root.wait_window(dlg)
        return out["v"]

    def edit_credit_left(self):
        cur = self.cfg.get("credit_left")
        if cur is None:
            shown = ""
        else:
            left = float(cur) - self.credit_spent_since(self.cfg.get("credit_left_at"))
            shown = "%.2f" % left
        val = self.ask_number("WorkBuddy 剩余积分",
                              "填应用里看到的剩余积分（留空=清除）",
                              shown)
        if val is None:
            return
        if not val:
            self.cfg.pop("credit_left", None)
            self.cfg.pop("credit_left_at", None)
        else:
            try:
                self.cfg["credit_left"] = float(val)
            except ValueError:
                return
            self.cfg["credit_left_at"] = int(time.time())
        save_cfg(self.cfg)
        self.show_credit_left()

    def credit_spent_since(self, since):
        """手填基准之后实扣了多少积分（每轮实扣挂在轮内最后一条记录上）。"""
        if not since:
            return 0.0
        return sum(r["credit"] for r in self.store.records
                   if r.get("credit") is not None and r["ts"] >= since)

    def show_credit_left(self):
        """剩余 = 手填基准 减 基准之后日志里实扣的积分（不能只显示手填值）。"""
        v = self.cfg.get("credit_left")
        if v is None:
            self.cr_left.config(text="剩 未填（点这里）", fg="#5b6474")
            return
        at = self.cfg.get("credit_left_at")
        used = self.credit_spent_since(at)
        left = float(v) - used
        at = at or time.time()
        if time.strftime("%Y-%m-%d", time.localtime(at)) == time.strftime("%Y-%m-%d"):
            when = time.strftime("%H:%M", time.localtime(at))
        else:
            when = time.strftime("%m-%d %H:%M", time.localtime(at))
        if used:
            txt = "剩 %.2f（%s 起扣 %.2f）" % (left, when, used)
        else:
            txt = "剩 %.2f（基准 %s）" % (left, when)
        self.cr_left.config(text=txt, fg=GREEN if left > 0 else "#ff7b72")

    def tick(self):
        """定时任务：任何异常都不能打断循环，否则界面就永远停住。"""
        try:
            self._tick_once()
        except Exception:
            self.log_error()
            try:
                self.state_lbl.config(fg="#ff7b72")
            except tk.TclError:
                pass
        finally:
            try:
                self.root.after(POLL_MS, self.tick)
            except tk.TclError:
                pass

    def log_error(self):
        import traceback
        try:
            ensure_dirs()
            with open(ERROR_LOG, "a", encoding="utf-8") as fh:
                fh.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
                fh.write(traceback.format_exc())
        except Exception:
            pass

    def _tick_once(self):
        # 后台线程取回的额度，在主线程里落到界面上
        if self._quota_pending is not None:
            go, err = self._quota_pending
            self._quota_pending = None
            self._quota_busy = False
            self._apply_quota(go, err)
        if not self.auto:
            return
        added = self.store.scan()
        upd = self.store.last_updated
        dirty = bool(added or upd)
        if added:
            pass
        if time.time() - self.credit_age > 15:
            self.credit_age = time.time()
            credits = self.store.reload_credits()
            if credits and self.store.apply_credits(credits):
                dirty = True
        if time.time() - self.title_age > 60:
            self.title_age = time.time()
            before = len(self.store.titles)
            self.store.load_titles()
            if len(self.store.titles) != before:
                dirty = True
        if time.time() - self.quota_age > 60:
            self.quota_age = time.time()
            self.fetch_quota()
        if added:
            self.state_lbl.config(fg="#a6ff8f")
            self.beat_lbl.config(text="+%d 新记录" % added, fg=GREEN)
            self.root.after(1600, lambda: self.beat_lbl.config(
                text="%s 刷新" % time.strftime("%H:%M:%S"), fg="#5b6474"))
        elif upd:
            self.state_lbl.config(fg="#a6ff8f")
            self.beat_lbl.config(text="%d 条更新" % upd, fg=GREEN)
            self.root.after(1600, lambda: self.beat_lbl.config(
                text="%s 刷新" % time.strftime("%H:%M:%S"), fg="#5b6474"))
        else:
            self.state_lbl.config(fg=GREEN if self.store.records else FG_DIM)
            if not self.beat_lbl.cget("text").startswith("+"):
                self.beat_lbl.config(text="%s 刷新" % time.strftime("%H:%M:%S"), fg="#5b6474")
        if dirty or self.detail is not None:
            self.refresh()
            self._refresh_detail()

    def open_detail(self):
        if self.detail is not None:
            try:
                self.detail.win.lift()
                return
            except tk.TclError:
                self.detail = None          # 窗口已经被关掉了
        self.detail = DetailWindow(self)
        self.detail.refresh()

    def run(self):
        self.root.mainloop()

    def _refresh_detail(self):
        """明细窗可能已经被关掉，别计TclError 把主循环带崩。"""
        if self.detail is None:
            return
        try:
            self.detail.refresh()
        except tk.TclError:
            self.detail = None

    def open_stats(self):
        st = getattr(self, "stats", None)
        if st is not None:
            try:
                st.win.lift()
                return
            except tk.TclError:
                self.stats = None
        self.stats = ModelStats(self)


# ---------------------------------------------------------------- 明细窗口

class DetailWindow:
    COLS = [
        ("ts",     "时间",     112, "w"),
        ("task",   "任务 / 会话", 282, "w"),
        ("lane",   "通道",      52, "w"),
        ("bill",   "计费",      54, "w"),
        ("model",  "模型",     146, "w"),
        ("in",     "输入",      76, "e"),
        ("out",    "输出",      72, "e"),
        ("think",  "思考",      68, "e"),
        ("cache",  "缓存读",    88, "e"),
        ("usd",    "金额",      78, "e"),
        ("credit", "积分",      62, "e"),
        ("mult",   "倍率",      50, "e"),
    ]

    def __init__(self, app):
        self.app = app
        self.range = "today"
        self.sort = ("ts", True)     # (字段, 是否降序)
        self.query = ""
        self.rowmap = {}

        self.win = tk.Toplevel(app.root)
        self.win.title("Token 账本 · 明细")
        self.win.configure(bg=BG)
        self.win.geometry("1200x706+%d+%d" % (110, 70))
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        bar = tk.Frame(self.win, bg=BG_ALT)
        bar.pack(fill="x")
        self.range_btns = {}
        for key, label in (("today", "今日"), ("week", "本周"),
                           ("month", "本月"), ("all", "全部")):
            b = tk.Label(bar, text=label, bg=BG_ALT, fg=FG_DIM, font=FONT_UI,
                         padx=12, pady=6, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self.set_range(k))
            self.range_btns[key] = b

        tk.Label(bar, text="搜索", bg=BG_ALT, fg=FG_DIM, font=FONT_UI).pack(side="left", padx=(16, 4))
        self.q_entry = tk.Entry(bar, bg="#22262f", fg=FG, insertbackground=FG,
                                relief="flat", font=FONT_UI, width=22)
        self.q_entry.pack(side="left", ipady=3)
        self.q_entry.bind("<KeyRelease>", self._on_query)

        exp = tk.Label(bar, text="导出 CSV", bg=BG_ALT, fg=ACCENT, font=FONT_UI,
                       padx=12, pady=6, cursor="hand2")
        exp.pack(side="right")
        exp.bind("<Button-1>", lambda e: self.export_csv())

        stat = tk.Label(bar, text="统计图", bg=BG_ALT, fg=ACCENT, font=FONT_UI,
                        padx=12, pady=6, cursor="hand2")
        stat.pack(side="right")
        stat.bind("<Button-1>", lambda e: self.open_stats())

        # 钱和额度的总览（从悬浮窗挪过来）
        self.head_lbl = tk.Label(self.win, text="", bg=BG, fg=FG_DIM,
                                 font=("Consolas", 9), anchor="w", justify="left")
        self.head_lbl.pack(fill="x", padx=12, pady=(6, 0))

        wrap = tk.Frame(self.win, bg=BG)
        wrap.pack(fill="both", expand=True, padx=10, pady=(8, 4))

        style = ttk.Style(self.win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TL.Treeview", background=BG, fieldbackground=BG, foreground=FG,
                        rowheight=22, borderwidth=0, font=FONT_UI)
        style.configure("TL.Treeview.Heading", background="#23262f", foreground="#cfd4de",
                        relief="flat", font=FONT_UI_B)
        style.map("TL.Treeview", background=[("selected", "#2d4f7c")],
                  foreground=[("selected", "#ffffff")])
        style.map("TL.Treeview.Heading", background=[("active", "#2c313c")])

        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS],
                                 show="headings", style="TL.Treeview")
        for key, label, width, anchor in self.COLS:
            self.tree.heading(key, text=label, anchor=anchor,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=(key in ("model", "cwd")))
        self.tree.tag_configure("oc", foreground=GREEN)
        self.tree.tag_configure("bi", foreground=FG)
        self.tree.tag_configure("lo", foreground=FG_DIM)

        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        self.info_lbl = tk.Label(self.win, text="", bg=BG, fg="#8b93a5",
                                 font=("Microsoft YaHei UI", 8), anchor="w",
                                 justify="left")
        self.info_lbl.pack(fill="x", padx=12, pady=(4, 0))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.sum_lbl = tk.Label(self.win, text="", bg=BG, fg=FG, font=FONT_NUM,
                                anchor="w", justify="left")
        self.sum_lbl.pack(fill="x", padx=12)
        note = tk.Label(self.win,
                        text="金额 = 这一条请求的钱（绿色=真扣现金，浅蓝Go 套餐内按官网单价折算）。"
                             "积分 = WorkBuddy 实扣，按轮计，记在该轮最后一条上。"
                             "点一行可在上方看完整任务名与目录。",
                        bg=BG, fg="#6b7385", font=("Microsoft YaHei UI", 8), anchor="w")
        note.pack(fill="x", padx=12, pady=(2, 8))

        self.set_range("today")

    def _render_head(self):
        """顶部两行：今日现金、Go / 累计，以及额度百分比 + 本月已用/上限。"""
        t = self.app.totals()
        go_today = sum(go_est_usd(r) or 0 for r in self.app.store.records
                       if r["ts"] >= day_start() and bill_of(r) == "额度")
        line1 = "今日现金 %s · Go %s · 累计现金 %s" % (
            fmt_usd(t["usd_today"]), fmt_usd(go_today), fmt_usd(t["usd_all"]))
        lim = self.app.go_limits or {}
        if lim:
            line2 = "Go 额度（账号级，各模型累加）近 5 小时 %d%% · 本周 %d%% · 本月 %d%%" % (
                int(round(float((lim.get("rolling") or {}).get("percent") or 0))),
                int(round(float((lim.get("weekly") or {}).get("percent") or 0))),
                int(round(float((lim.get("monthly") or {}).get("percent") or 0))))
        else:
            line2 = "Go 额度：未取到"
        cap, _ = self.app.go_cap_line()
        if cap:
            line2 += "  · " + cap
        self.head_lbl.config(text=line1 + "\n" + line2)

    def _task_text(self, r, units=52):
        """优先显示你那句话（取第一行），比会话标题有信息量。"""
        p = self.app.store.prompt_of(r)
        if p:
            first = p.split("\n")[0].strip()
            if first:
                return fit(first, units)
        return self.app.store.task_name(r, units // 2)

    # ---- 交互
    def _on_select(self, e=None):
        sel = self.tree.selection()
        rec = self.rowmap.get(sel[0]) if sel else None
        if not rec:
            return
        title = self.app.store.titles.get(rec.get("sess") or "") or "（无标题）"
        prompt = self.app.store.prompt_of(rec)
        cwd = rec.get("cwd") or ""
        lines = []
        if prompt:
            flat = " ".join(prompt.split())
            lines.append("提问：%s" % (flat[:110] + ("…" if len(flat) > 110 else "")))
        lines.append("任务：%s" % title)
        lines.append("目录：%s ｜ 会话 %s ｜ 通道 %s"
                     % (cwd, rec.get("session") or "-", rec.get("lane") or "-"))
        self.info_lbl.config(text="\n".join(lines))
    def _on_query(self, e=None):
        self.query = self.q_entry.get().strip().lower()
        self.refresh()

    def set_range(self, key):
        self.range = key
        for k, b in self.range_btns.items():
            b.config(fg=ACCENT if k == key else FG_DIM, bg="#262b35" if k == key else BG_ALT)
        self.refresh()

    def sort_by(self, key):
        f, desc = self.sort
        self.sort = (key, not desc if f == key else True)
        self.refresh()

    def _filtered(self):
        if self.range == "today":
            lo = day_start()
        elif self.range == "week":
            lo = week_start()
        elif self.range == "month":
            lo = month_start()
        else:
            lo = 0
        out = [r for r in self.app.store.records if r["ts"] >= lo]
        if self.query:
            q = self.query
            out = [r for r in out
                   if q in r["model"].lower() or q in r["lane"].lower()
                   or q in r["cwd"].lower() or q in r["session"]
                   or q in (self.app.store.titles.get(r.get("sess") or "") or "").lower()
                   or q in self.app.store.prompt_of(r).lower()]
        f, desc = self.sort
        keyf = {
            "ts": lambda r: r["ts"], "lane": lambda r: r["lane"],
            "task": lambda r: self.app.store.titles.get(r.get("sess") or "") or r["cwd"],
            "bill": lambda r: bill_of(r),
            "model": lambda r: r["model"], "in": lambda r: r["in"],
            "out": lambda r: r["out"], "think": lambda r: r["think"],
            "cache": lambda r: r["cache"], "usd": lambda r: r["usd"],
            "credit": lambda r: (r.get("credit") or 0.0),
            "mult": lambda r: r["model"], "cwd": lambda r: r["cwd"],
        }[f]
        out.sort(key=keyf, reverse=desc)
        return out

    def refresh(self):
        rows = self._filtered()
        self.tree.delete(*self.tree.get_children())
        self.rowmap = {}
        self._render_head()

        shown = rows[:DETAIL_CAP]
        for i, r in enumerate(shown):
            cr = r.get("credit")
            b = bill_of(r)
            est = go_est_usd(r) if b == "额度" else None
            iid = str(i)
            self.rowmap[iid] = r
            self.tree.insert("", "end", iid=iid, values=(
                time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"])),
                self._task_text(r), r["lane"], b, r["model"],
                "{:,}".format(r["in"]), "{:,}".format(r["out"]),
                "{:,}".format(r["think"]), "{:,}".format(r["cache"]),
                fmt_usd(r["usd"]) if b == "美元"
                else (fmt_usd(est) if est else "—"),
                ("%.2f" % cr) if cr is not None else "",
                mult_of(r),
            ), tags=(("oc" if r["lane"] in ("opencode", "桌面")
                      else "lo" if r["lane"] == "本地" else "bi"),))

        usd = sum(r["usd"] for r in rows)
        go_est = sum(go_est_usd(r) or 0 for r in rows if bill_of(r) == "额度")
        allow = sum(r["allow"] for r in rows)
        bi = sum(r["in"] + r["out"] + r["cache"] for r in rows if r["lane"] == "内置")
        crs = [r["credit"] for r in rows if r.get("credit") is not None]
        ti = sum(r["in"] for r in rows); to = sum(r["out"] for r in rows)
        tt = sum(r["think"] for r in rows); tc = sum(r["cache"] for r in rows)
        cls = {}
        for r in rows:
            cls[bill_of(r)] = cls.get(bill_of(r), 0) + 1
        cls_txt = "  ".join("%s %d 条" % (k, cls[k])
                            for k in ("美元", "额度", "积分", "免费", "本地") if k in cls)
        cap = "" if len(rows) <= DETAIL_CAP else "  （表内仅显示前 %d 条，合计按全部 %s 条）" % (
            DETAIL_CAP, "{:,}".format(len(rows)))
        self.sum_lbl.config(
            text="合计 %s 条  入 %s  出 %s  思考 %s  缓存 %s      "
                 "美元 %s（实扣 %s）· Go %s · 积分 %s（%d 轮有记录）\n"
                 "%s  ·  内置 token %s%s"
                 % ("{:,}".format(len(rows)), "{:,}".format(ti), "{:,}".format(to),
                    "{:,}".format(tt), "{:,}".format(tc),
                    fmt_usd(usd), fmt_usd(allow), fmt_usd(go_est),
                    ("%.2f" % sum(crs)) if crs else "0", len(crs),
                    cls_txt, fmt_tok(bi), cap))

    def export_csv(self):
        rows = self._filtered()
        name = "token账本_%s.csv" % time.strftime("%Y%m%d_%H%M%S")
        ensure_dirs()
        path = os.path.join(EXPORT_DIR, name)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["时间", "任务", "通道", "计费", "模型", "会话", "工作目录",
                        "输入", "输出", "思考", "缓存读", "缓存写",
                        "美元标价", "实扣额度", "Go金额", "积分", "倍率"])
            for r in rows:
                cr = r.get("credit")
                est = go_est_usd(r) if bill_of(r) == "额度" else None
                w.writerow([
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])),
                    self.app.store.titles.get(r.get("sess") or "") or r["cwd"],
                    r["lane"], bill_of(r), r["model"], r["session"], r["cwd"],
                    r["in"], r["out"], r["think"], r["cache"], r["cw"],
                    "%.8f" % r["usd"], "%.8f" % r["allow"],
                    "%.8f" % est if est is not None else "",
                    ("%.4f" % cr) if cr is not None else "", mult_of(r),
                ])
        messagebox.showinfo("已导出", "导出了%s 条\n\n%s" % ("{:,}".format(len(rows)), path))

    def open_stats(self):
        self.app.open_stats()

    def close(self):
        """关窗口时把引用清掉，否则再点「明细」会对着死窗口报错。"""
        self.app.detail = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass


class ModelStats:
    """按模型汇总的条形图：Token / 金额 / 积分 / 条数，四个时间段可切。"""

    METRICS = (("tok", "Token"), ("money", "金额"),
               ("credit", "积分"), ("n", "条数"))

    def __init__(self, app):
        self.app = app
        self.range = "today"
        self.metric = "tok"

        self.win = tk.Toplevel(app.root)
        self.win.title("Token 账本 · 模型统计")
        self.win.configure(bg=BG)
        self.win.geometry("920x640+%d+%d" % (90, 80))
        self.win.attributes("-topmost", True)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        bar = tk.Frame(self.win, bg=BG_ALT)
        bar.pack(fill="x")
        self.rbtn = {}
        for key, label in (("today", "今日"), ("week", "本周"),
                           ("month", "本月"), ("all", "全部")):
            b = tk.Label(bar, text=label, bg=BG_ALT, fg=FG_DIM, font=FONT_UI,
                         padx=12, pady=6, cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, k=key: self.set_range(k))
            self.rbtn[key] = b

        right = tk.Frame(bar, bg=BG_ALT)
        right.pack(side="right")
        self.mbtn = {}
        for key, label in reversed(self.METRICS):
            b = tk.Label(right, text=label, bg=BG_ALT, fg=FG_DIM, font=FONT_UI,
                         padx=12, pady=6, cursor="hand2")
            b.pack(side="right")
            b.bind("<Button-1>", lambda e, k=key: self.set_metric(k))
            self.mbtn[key] = b

        self.canvas = tk.Canvas(self.win, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=18, pady=(10, 4))
        self.canvas.bind("<Configure>", lambda e: self.draw())

        self.sum_lbl = tk.Label(self.win, text="", bg=BG, fg=FG_DIM,
                                font=FONT_NUM, anchor="w")
        self.sum_lbl.pack(fill="x", padx=18, pady=(0, 10))

        self.set_range("today")

    # ---- 交互
    def close(self):
        try:
            self.app.stats = None
        except Exception:
            pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def set_range(self, key):
        self.range = key
        for k, b in self.rbtn.items():
            b.config(fg=ACCENT if k == key else FG_DIM,
                     bg="#262b35" if k == key else BG_ALT)
        self.draw()

    def set_metric(self, key):
        self.metric = key
        for k, b in self.mbtn.items():
            b.config(fg=GOLD if k == key else FG_DIM,
                     bg="#262b35" if k == key else BG_ALT)
        self.draw()

    # ---- 数据
    def _rows(self):
        lo = {"today": day_start(), "week": week_start(),
              "month": month_start()}.get(self.range, 0)
        agg = {}
        for r in self.app.store.records:
            if r["ts"] < lo:
                continue
            a = agg.get(r["model"])
            if a is None:
                a = agg[r["model"]] = {"tok": 0, "money": 0.0, "credit": 0.0,
                                       "n": 0}
            a["tok"] += r["in"] + r["out"] + r["think"] + r["cache"]
            a["money"] += r["usd"]
            if bill_of(r) == "额度":
                a["money"] += go_est_usd(r) or 0
            a["credit"] += r["credit"] or 0
            a["n"] += 1
        rows = [(k, a) for k, a in agg.items() if a[self.metric] > 0]
        return sorted(rows, key=lambda kv: -kv[1][self.metric])

    @staticmethod
    def _fmt(metric, v):
        if metric == "tok":
            return fmt_tok(v)
        if metric == "money":
            return fmt_usd(v)
        if metric == "credit":
            return "%.1f 分" % v
        return "%d 条" % v

    # ---- 绘制
    def draw(self):
        c = self.canvas
        c.delete("all")
        rows = self._rows()
        if not rows:
            c.create_text(20, 20, text="这个时间段没有记录", anchor="nw",
                          fill=FG_DIM, font=FONT_UI)
            self.sum_lbl.config(text="")
            return

        w = max(c.winfo_width(), 400)
        h = max(c.winfo_height(), 300)
        label_w = 210
        pad_r = 110
        bar_max = max(w - label_w - pad_r, 80)
        maxv = max((a[self.metric] for _, a in rows), default=0) or 1
        color = {"tok": ACCENT, "money": GREEN,
                 "credit": GOLD, "n": "#9ecbff"}[self.metric]

        row_h, gap = 26, 8
        n = min(len(rows), max(1, int((h - 10) / (row_h + gap))))
        for i, (model, a) in enumerate(rows[:n]):
            y = 8 + i * (row_h + gap)
            v = a[self.metric]
            bw = max(2, bar_max * v / maxv)
            c.create_rectangle(label_w, y, label_w + bw, y + row_h,
                               fill=color, outline="")
            c.create_text(6, y + row_h / 2, text=fit(short_model(model), 22),
                          anchor="w", fill=FG, font=FONT_UI)
            txt = self._fmt(self.metric, v)
            c.create_text(label_w + bw + 8, y + row_h / 2, text=txt,
                          anchor="w", fill=color, font=FONT_NUM)
            if bw > 70:
                c.create_text(label_w + bw - 8, y + row_h / 2,
                              text=m_fmt_inside(self.metric, v), anchor="e",
                              fill="#14161b", font=FONT_NUM_B)

        tot = sum(a[self.metric] for _, a in rows)
        self.sum_lbl.config(
            text="%s · %d 个模型· %s 合计 %s"
                 % ({"today": "今日", "week": "本周", "month": "本月",
                     "all": "全部"}[self.range], len(rows),
                    {"tok": "Token", "money": "金额",
                     "credit": "积分", "n": "条数"}[self.metric],
                    self._fmt(self.metric, tot)))


def m_fmt_inside(metric, v):
    """条形内部那个更短的数字。"""
    if metric == "tok":
        return fmt_tok(v)
    if metric == "money":
        return fmt_usd(v)
    if metric == "credit":
        return "%.0f" % v
    return str(v)


# ---------------------------------------------------------------- 入口

def main():
    ap = argparse.ArgumentParser(
        description="Token 账本 —— 本机 AI 客户端的 token、额度与费用（只读本地日志）")
    ap.add_argument("--check", action="store_true", help="只打印统计，不开窗口")
    ap.add_argument("--json", action="store_true", help="配合 --check 输出机器可读 JSON")
    ap.add_argument("--demo", action="store_true", help="假数据预览界面，不读本机任何文件")
    ap.add_argument("--data-dir", help="配置和日志放哪（默认 %%APPDATA%%\\TokenLedger）")
    ap.add_argument("--config", help="指定配置文件（默认 <data-dir>/config.json）")
    ap.add_argument("--projects", help="WorkBuddy 日志目录")
    ap.add_argument("--workbuddy-db", help="WorkBuddy 台账 workbuddy.db")
    ap.add_argument("--opencode-db", help="opencode 会话库 opencode.db")
    ap.add_argument("--service-json", help="opencode 服务地址文件 service.json")
    ap.add_argument("--detail", action="store_true", help="启动时直接打开明细表")
    ap.add_argument("--stats", action="store_true", help="启动时直接打开模型统计图")
    ap.add_argument("--only-oc", action="store_true", help="悬浮窗只显示 opencode 通道")
    ap.add_argument("--version", action="version", version="Token 账本 %s" % VERSION)
    args = ap.parse_args()

    # 输出被重定向到管道/文件时，Windows 默认用 cp1252，打印中文会直接崩；
    # 控制台里保持系统默认，避免改了编码反而乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    global CFG_PATH
    if args.data_dir:
        set_data_dir(args.data_dir)
    if args.config:
        CFG_PATH = os.path.abspath(os.path.expanduser(args.config))
    if args.demo:
        CFG_PATH = os.path.join(DATA_DIR, "config.demo.json")

    cfg = {} if args.demo else load_cfg()
    apply_paths(cfg, args)
    refresh_multipliers()

    store = Store(PATHS["wb_projects"], demo=args.demo)

    if not (args.check or args.json):
        app = Ledger(store)
        if args.only_oc:
            app.toggle_filter()
        if args.detail:
            app.root.after(400, app.open_detail)
        if args.stats:
            app.root.after(700, app.open_stats)
        app.root.after(30, app.boot)     # 先出窗口，再扫描
        app.run()
        return

    t0 = time.time()
    n = len(store.records) if store.demo else store.scan()
    credits = store.reload_credits()
    if credits:
        store.apply_credits(credits)
    store.load_titles()
    cost = time.time() - t0

    today = day_start()
    lanes = {}
    for r in store.records:
        lanes.setdefault(r["lane"], []).append(r)
    oc = [r for r in store.records if r["lane"] == "opencode" and r["ts"] >= today]
    bi = [r for r in store.records if r["lane"] == "内置" and r["ts"] >= today]
    ocd = [r for r in store.records if r["lane"] == "桌面" and r["ts"] >= today]
    cr_today = [r for r in store.turn_last.values()
                if r.get("credit") is not None and r["ts"] >= today]
    all_cr = [r["credit"] for r in store.turn_last.values()
              if r.get("credit") is not None]
    cls = {}
    for r in store.records:
        cls[bill_of(r)] = cls.get(bill_of(r), 0) + 1
    today_tasks = {}
    for r in store.records:
        if r["ts"] >= today:
            k = r.get("sess")
            today_tasks[k] = today_tasks.get(k, 0) + 1

    if args.json:
        print(json.dumps({
            "version": VERSION,
            "demo": bool(args.demo),
            "data_dir": DATA_DIR,
            "config": CFG_PATH,
            "sources": {k: {"path": PATHS[k], "exists": os.path.exists(PATHS[k])}
                        for k in ("wb_projects", "wb_db", "oc_db", "oc_service")},
            "scan_seconds": round(cost, 3),
            "records": n,
            "lanes": {lane: {
                "records": len(rs),
                "input": sum(r["in"] for r in rs),
                "output": sum(r["out"] for r in rs),
                "thinking": sum(r["think"] for r in rs),
                "cache_read": sum(r["cache"] for r in rs),
                "usd": round(sum(r["usd"] for r in rs), 6),
            } for lane, rs in sorted(lanes.items(), key=lambda kv: -len(kv[1]))},
            "today": {
                "opencode_records": len(oc),
                "usd": round(sum(r["usd"] for r in oc), 6),
                "allowance": round(sum(r["allow"] for r in oc), 6),
                "builtin_records": len(bi),
                "builtin_tokens": sum(r["in"] + r["out"] + r["cache"] for r in bi),
                "desktop_records": len(ocd),
                "desktop_usd": round(sum(r["usd"] for r in ocd), 6),
                "credits": round(sum(r["credit"] for r in cr_today), 2),
                "credit_turns": len(cr_today),
            },
            "totals": {
                "records": len(store.records),
                "usd": round(sum(r["usd"] for r in store.records), 6),
                "credits": round(sum(all_cr), 2),
                "credit_turns": len(all_cr),
                "turn_groups": len(store.turn_last),
                "titles": len(store.titles),
                "tasks_today": len(today_tasks),
            },
            "billing": cls,
        }, ensure_ascii=False, indent=2))
        return

    if args.demo:
        print("（演示数据：以下数字都是编的，不读本机文件）")
    print("扫描 %.2f 秒，读到 %s 条请求；积分条目 %s 个" % (
        cost, "{:,}".format(n), "{:,}".format(len(credits))))
    for lane, rs in sorted(lanes.items(), key=lambda kv: -len(kv[1])):
        usd = sum(r["usd"] for r in rs)
        print("  %-9s %6s 条 输入 %12s  输出 %10s  思考%10s  缓存 %14s  美元 %.4f"
              % (lane, "{:,}".format(len(rs)),
                 "{:,}".format(sum(r["in"] for r in rs)),
                 "{:,}".format(sum(r["out"] for r in rs)),
                 "{:,}".format(sum(r["think"] for r in rs)),
                 "{:,}".format(sum(r["cache"] for r in rs)), usd))
    print("  今日：opencode %d 条 $%.4f ｜ 内置 %d 条 %s token ｜ 积分 %.2f（%d 轮）"
          % (len(oc), sum(r["usd"] for r in oc), len(bi),
             fmt_tok(sum(r["in"] + r["out"] + r["cache"] for r in bi)),
             sum(r["credit"] for r in cr_today), len(cr_today)))
    print("  桌面版：%d 条（累计 %d 条）今日 $%.4f ｜ 入 %s 出 %s 思考 %s 缓存 %s"
          % (len(ocd), sum(1 for r in store.records if r["lane"] == "桌面"),
             sum(r["usd"] for r in ocd),
             "{:,}".format(sum(r["in"] for r in ocd)),
             "{:,}".format(sum(r["out"] for r in ocd)),
             "{:,}".format(sum(r["think"] for r in ocd)),
             "{:,}".format(sum(r["cache"] for r in ocd))))
    print("  积分累计 %.2f（%d 轮有记录 / 共 %d 轮）"
          % (sum(all_cr), len(all_cr), len(store.turn_last)))
    print("  计费分类：" + "  ".join("%s %s 条" % (k, cls[k])
                                 for k in ("美元", "额度", "积分", "免费", "本地") if k in cls))
    print("  会话标题：%d 个（WorkBuddy+桌面）" % len(store.titles))
    print("  今日不同任务数：%d" % len(today_tasks))

    if n == 0 and not args.demo:
        print()
        print("没读到记录。四个数据源的实际位置：")
        print(path_report())
        print("用 --projects / --workbuddy-db / --opencode-db / --service-json 指定，")
        print("或写进 %s（键名同名）。" % CFG_PATH)


if __name__ == "__main__":
    main()

