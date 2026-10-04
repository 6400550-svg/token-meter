#!/usr/bin/env python3
# <xbar.title>Claude Code Token Meter</xbar.title>
# <xbar.desc>统计 ~/.claude/projects 里的 Claude Code token 消耗，菜单栏显示 5h 窗口与今日用量</xbar.desc>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""
数据来源：Claude Code 写在 ~/.claude/projects/**/*.jsonl 的会话记录（Cowork 的在 App 数据目录里），
每条 assistant 消息带 message.usage。按 message.id 去重（同一条回复会分多行写入）。
后台调用（标题生成、权限判断等）不写进会话记录，只出现在每次运行结束时的 cost-state 汇总里，
用 cost-state 减去记录里的用量补上。
增量解析：缓存每个文件读到的字节位置，只读新增部分。
"""
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
PROJECTS = HOME / ".claude" / "projects"
APP_DIR = HOME / "Library/Application Support/Claude"
COWORK = APP_DIR / "local-agent-mode-sessions"
APP_SESSIONS = APP_DIR / "claude-code-sessions"
STATE_DIR = HOME / ".token-meter"
CACHE = STATE_DIR / "cache.json"
REPORT = STATE_DIR / "report.html"
# v2：加了思考强度、思考 tokens；v3：加了会话 id、后台标记，另存 cost-state 和会话标题
CACHE_VERSION = 3
REC_LEN = 13

# 个人设置在 ~/.token-meter/config.json（安装脚本会生成）：
#   plan: pro / max5x / max20x —— 用来算"API 价值 / 套餐费"
#   weekly_reset_utc: 某一次每周额度重置的时刻（UTC），之后每 7 天一次
PLANS = {"pro": ("Pro", 20), "max5x": ("Max 5x", 100), "max20x": ("Max 20x", 200)}


def load_config():
    try:
        return json.loads((STATE_DIR / "config.json").read_text())
    except (OSError, ValueError):
        return {}


CONFIG = load_config()
PLAN_NAME, PLAN_USD_MONTH = PLANS.get(CONFIG.get("plan"), (None, None))

# 思考强度显示顺序；9 月 3 日前的记录没有这个字段
EFFORTS = ["max", "xhigh", "high", "medium", "low"]
NO_EFFORT = "未记录"
BG = "后台"  # 会话记录里没有、从 cost-state 补上的调用
EFFORT_ORDER = EFFORTS + [NO_EFFORT, BG]

ENTRY_NAMES = {"claude-desktop": "桌面 App", "cli": "终端", "local-agent": "Cowork",
               "claude-vscode": "VS Code", "sdk-ts": "SDK", "sdk-py": "SDK"}

WINDOW = 5 * 3600

# 每百万 token 美元价：input, output, cache写5m, cache写1h, cache读
PRICES = {
    "claude-opus-5-5":   (4, 20, 5, 8, 0.20),
    "claude-opus-5":     (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-8":   (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-7":   (5, 25, 6.25, 10, 0.50),
    "claude-opus-4-6":   (5, 25, 6.25, 10, 0.50),
    "claude-fable-5-1":  (10, 50, 12.5, 20, 0.25),
    "claude-fable-5":    (10, 50, 12.5, 20, 1.00),
    "claude-sonnet-5-5": (2, 10, 2.5, 4, 0.20),
    "claude-sonnet-5":   (2, 10, 2.5, 4, 0.20),
    "claude-sonnet-4-6": (3, 15, 3.75, 6, 0.30),
    "claude-haiku-4-5":  (1, 5, 1.25, 2, 0.10),
}
FALLBACK_PRICE = PRICES["claude-opus-5"]

SPARK = "▁▂▃▄▅▆▇█"


# ---------- 解析 ----------

def price_for(model):
    if model in PRICES:
        return PRICES[model]
    for key in sorted(PRICES, key=len, reverse=True):  # 带日期后缀的 id
        if model.startswith(key):
            return PRICES[key]
    return FALLBACK_PRICE


def short(model):
    """显示用：claude-haiku-4-5-20251001 → haiku-4-5"""
    return re.sub(r"-\d{8}$", "", model.replace("claude-", ""))


def is_claude(model):
    return model.startswith("claude-")


def parse_line(line):
    if '"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    msg = d.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return None
    u = msg.get("usage")
    mid = msg.get("id")
    model = msg.get("model") or ""
    if not u or not mid or model == "<synthetic>":
        return None
    try:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return None
    cw_total = u.get("cache_creation_input_tokens") or 0
    cc = u.get("cache_creation") or {}
    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
    cw5m = cc.get("ephemeral_5m_input_tokens") or 0
    if cw1h + cw5m != cw_total:  # 没有细分时全部当 5m
        cw5m, cw1h = cw_total, 0
    project = os.path.basename((d.get("cwd") or "").rstrip("/")) or "?"
    think = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
    return mid, [
        ts, model,
        u.get("input_tokens") or 0,
        u.get("output_tokens") or 0,
        cw5m, cw1h,
        u.get("cache_read_input_tokens") or 0,
        project,
        1 if u.get("speed") == "fast" else 0,
        d.get("perTurnEffort") or d.get("effort"),
        think,
        d.get("sessionId"),
        0,
    ], d


def backup_cache(reason):
    """缓存里有已被 Claude Code 清理掉的历史，重建前先留一份，不直接丢掉。"""
    if CACHE.exists():
        dst = STATE_DIR / f"cache.{reason}-{datetime.now():%Y%m%d-%H%M%S}.json"
        try:
            CACHE.replace(dst)
        except OSError:
            pass


def load_cache():
    c = None
    try:
        c = json.loads(CACHE.read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        backup_cache("broken")  # 文件损坏
    if c and c.get("v", 0) > CACHE_VERSION:
        backup_cache("newer")  # 更新版本的插件写的，旧插件读不懂
        c = None
    if not c:
        c = {"v": CACHE_VERSION, "files": {}, "msgs": {}}
    if c["v"] < CACHE_VERSION:
        # 升级：保留旧记录（有些会话文件已被清理），从头重读现存文件补上新字段
        defaults = [None, 0, None, 0]  # [9] 思考强度 [10] 思考 tokens [11] 会话 [12] 后台
        for r in c["msgs"].values():
            r += defaults[len(r) - 9:]
        c["files"] = {}
        c["v"] = CACHE_VERSION
    for k in ("lastts", "cs", "sess", "win"):
        c.setdefault(k, {})
    return c


TS_RE = re.compile(rb'"timestamp":\s*"([^"]+)"')


def transcript_files():
    yield from PROJECTS.rglob("*.jsonl")
    if COWORK.is_dir():  # Cowork 的会话记录（同一目录下的 audit.jsonl 不算）
        for path in COWORK.rglob("*.jsonl"):
            if "/.claude/projects/" in str(path):
                yield path


def iso_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def update(cache):
    files, msgs, lastts = cache["files"], cache["msgs"], cache["lastts"]
    cs, sess = cache["cs"], cache["sess"]
    snap = lambda: json.dumps([files, cache["win"], cache["v"], (cache.get("cx") or {}).get("files")], sort_keys=True)
    before = snap()
    seen = set()
    for path in transcript_files():
        key = str(path)
        seen.add(key)
        cowork = key.startswith(str(COWORK))
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = files.get(key, 0)
        if size < offset:  # 文件被重写，从头读
            offset = 0
        if size == offset:
            continue
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            continue  # 还没有完整的一行
        last = lastts.get(key)
        for raw in chunk[: end + 1].splitlines():
            m = TS_RE.search(raw)
            if m:
                last = iso_ts(m.group(1).decode()) or last
            if b'"cost-state"' in raw:
                read_cost_state(raw, last, cs)
                continue
            if b'-title"' in raw[:40]:
                read_title(raw, sess)
                continue
            text = raw.decode("utf-8", "replace")
            rec = parse_line(text)
            if not rec:
                continue
            mid, r, d = rec
            if cowork:
                r[7] = "Cowork"
            old = msgs.get(mid)
            if old:  # 同一条回复多次写入：token 取最大值
                for i in (2, 3, 4, 5, 6, 10):
                    r[i] = max(r[i], old[i])
                r[0] = min(r[0], old[0])
                r[9] = r[9] or old[9]
                r[11] = old[11] or r[11]
            msgs[mid] = r
            if r[11]:
                s = sess.setdefault(r[11], {})
                s["e"] = "local-agent" if cowork else (d.get("entrypoint") or s.get("e"))
        files[key] = offset + end + 1
        lastts[key] = last
    for key in [k for k in files if k not in seen]:  # 会话文件已被删除：只清掉读取位置，记录保留
        files.pop(key, None)
        lastts.pop(key, None)
    read_app_windows(cache["win"])
    update_codex(cache)
    if CACHE.exists() and snap() == before:
        return  # 没有新内容，不用每分钟重写一遍
    STATE_DIR.mkdir(exist_ok=True)
    tmp = CACHE.with_name(f"cache.{os.getpid()}.tmp")  # 每个进程各用各的，同时运行也不会写坏
    tmp.write_text(json.dumps(cache, separators=(",", ":")))
    tmp.replace(CACHE)


def read_cost_state(raw, ts, cs):
    """Claude Code 每次运行结束写一条 cost-state：这次运行（从 startTime 起）所有 API 调用的累计。"""
    try:
        d = json.loads(raw)
        key = f"{d['sessionId']}|{d['startTime'] / 1000:.0f}"
    except (ValueError, KeyError, TypeError):
        return
    if ts is None:
        return
    old = cs.get(key)
    if old and old[1] >= d.get("totalCostUSD", 0):
        return
    models = {}
    for m, v in (d.get("modelUsage") or {}).items():
        models[m] = [v.get("inputTokens") or 0, v.get("outputTokens") or 0,
                     v.get("cacheCreationInputTokens") or 0, v.get("cacheReadInputTokens") or 0]
    cs[key] = [ts, d.get("totalCostUSD", 0), models]


def read_title(raw, sess):
    try:
        d = json.loads(raw)
    except ValueError:
        return
    sid = d.get("sessionId")
    title = d.get("customTitle") or d.get("aiTitle")
    if not sid or not title:
        return
    prio = 2 if d.get("type") == "custom-title" else 1
    s = sess.setdefault(sid, {})
    if prio >= s.get("tp", 0):
        s["t"], s["tp"] = title, prio


def read_app_windows(win):
    """桌面 App 的每个窗口（Code 标签页 / Cowork）对应一个或多个 CLI 会话，记下窗口标题。"""
    paths = []
    if APP_SESSIONS.is_dir():
        paths += [(p, "桌面 App") for p in APP_SESSIONS.rglob("local_*.json")]
    if COWORK.is_dir():
        paths += [(p, "Cowork") for p in COWORK.glob("*/*/local_*.json")]
    for p, kind in paths:
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        wid = d.get("sessionId") or p.stem
        info = [wid, d.get("title") or "", kind, d.get("cwd") or ""]
        for sid in [d.get("cliSessionId")] + list(d.get("priorCliSessionIds") or []):
            if sid:
                win[sid] = info


def background_recs(cache):
    """cost-state 减去同一次运行里会话记录的用量 = 没写进记录的后台调用。
    按这次运行里每小时的用量比例摊回去，这样 5 小时窗口、每日统计也对得上。"""
    by_sid = defaultdict(list)
    for r in cache["msgs"].values():
        if r[11]:
            by_sid[r[11]].append(r)
    out = []
    for key, (ts, _usd, models) in cache["cs"].items():
        sid, start = key.rsplit("|", 1)
        start = float(start)
        run = sorted((r for r in by_sid.get(sid, ()) if start - 2 <= r[0] <= ts + 2), key=lambda r: r[0])
        proj = run[-1][7] if run else "?"
        for model, v in models.items():
            mine = [r for r in run if r[1] == model]
            diff = [max(0, v[0] - sum(r[2] for r in mine)),
                    max(0, v[1] - sum(r[3] for r in mine)),
                    max(0, v[2] - sum(r[4] + r[5] for r in mine)),
                    max(0, v[3] - sum(r[6] for r in mine))]
            if not any(diff):
                continue
            base = mine or run
            one_hour = sum(r[5] for r in base) >= sum(r[4] for r in base)  # 缓存写按这次运行的主要类型计价
            weight, when = defaultdict(float), {}
            for r in base:
                h = int(r[0] // 3600)
                weight[h] += tokens(r) or 1
                when[h] = max(when.get(h, 0), r[0])
            if not weight:
                h = int(ts // 3600)
                weight[h], when[h] = 1, ts
            total = sum(weight.values())
            for h, w in weight.items():
                f = w / total
                cw = round(diff[2] * f)
                out.append([when[h], model, round(diff[0] * f), round(diff[1] * f),
                            0 if one_hour else cw, cw if one_hour else 0, round(diff[3] * f),
                            proj, 0, BG, 0, sid, 1])
    return out


def clean(text):
    """标题是用户/AI 起的，里面的 | 会被 SwiftBar 当成参数分隔符，开头的 - 会被当成子菜单"""
    return text.replace("|", "｜").replace("\n", " ").lstrip("-") or "无标题"


def window_of(sid, cache):
    """一条记录属于哪个窗口：(窗口 id, 标题, 来源)。"""
    info = cache["win"].get(sid) if sid else None
    s = cache["sess"].get(sid) or {}
    if info:
        return info[0], clean(info[1] or s.get("t") or "无标题"), info[2]
    if not sid:
        return "?", "（早期记录，会话已清理）", "?"
    e = s.get("e")
    return sid, clean(s.get("t") or f"会话 {sid[:8]}"), ENTRY_NAMES.get(e, e or "?")


# ---------- Codex（OpenAI，~/.codex） ----------
# 每次模型回复写一条 token_usage_record（带唯一的 response_id），
# 每条 token_count 带 Codex 官方额度（5 小时 / 每周的已用百分比和重置时刻）。
# 只读这几类事件行，不读对话内容。
CODEX_DIR = HOME / ".codex"
CODEX_INDEX = CODEX_DIR / "session_index.jsonl"
CX_REC, CX_TC, CX_CTX, CX_META = (b'"type":"token_usage_record"', b'"type":"token_count"',
                                  b'"type":"turn_context"', b'"type":"session_meta"')
# 记录：[0 时间, 1 模型, 2 输入(含缓存), 3 其中缓存, 4 输出(含推理), 5 其中推理, 6 思考强度, 7 线程, 8 是否占 Codex 额度]
CX_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def codex_files():
    for sub in ("sessions", "archived_sessions"):
        d = CODEX_DIR / sub
        if d.is_dir():
            yield from d.rglob("rollout-*.jsonl")


def update_codex(cache):
    cx = cache.setdefault("cx", {})
    for k in ("files", "ctx", "msgs", "par"):
        cx.setdefault(k, {})
    files, ctxs, msgs = cx["files"], cx["ctx"], cx["msgs"]
    if "rlh" not in cx:  # 第一次：把最近 10 天读过的文件里的周额度记录补回来
        cx["rlh"] = codex_backfill_rlh(files)
    seen = set()
    for path in codex_files():
        key = str(path)
        seen.add(key)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = files.get(key, 0)
        if size < offset:
            offset = 0
        if size == offset:
            continue
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        ctx = ctxs.setdefault(key, {})
        for raw in chunk[: end + 1].splitlines():
            head = raw[:200]  # 事件类型就在行首，图片、对话内容这些大行不用解析
            kind = next((k for k in (CX_REC, CX_TC, CX_CTX, CX_META) if k in head), None)
            if not kind:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            p = d.get("payload") or {}
            ts = iso_ts(d.get("timestamp") or "")
            if kind is CX_META:
                src = p.get("source")
                spawn = (src.get("subagent") or {}).get("thread_spawn") if isinstance(src, dict) else None
                ctx["t"] = p.get("id") or ctx.get("t")
                ctx["o"] = 1 if p.get("model_provider") == "openai" else 0  # custom = 走代理的第三方模型
                if spawn and spawn.get("parent_thread_id") and ctx["t"]:
                    cx["par"][ctx["t"]] = spawn["parent_thread_id"]  # 子代理的用量算到父线程上
            elif kind is CX_CTX:
                cm = p.get("collaboration_mode")
                cm = (cm.get("settings") or {}) if isinstance(cm, dict) else {}
                ctx["m"] = p.get("model") or cm.get("model") or ctx.get("m")
                ctx["e"] = p.get("effort") or cm.get("reasoning_effort") or ctx.get("e")
            elif kind is CX_REC:
                u, rid = p.get("usage") or {}, p.get("response_id")
                if not rid or not ts:
                    continue
                thread = p.get("thread_id") or ctx.get("t")
                if not ctx.get("rec"):  # 这个线程有逐条记录了：丢掉之前按累计值推出来的
                    ctx["rec"] = 1
                    for k in [k for k in msgs if k.startswith(f"tc|{thread}|")]:
                        del msgs[k]
                if (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) > 0:
                    msgs[rid] = [ts, ctx.get("m") or "?", *(u.get(f) or 0 for f in CX_FIELDS), ctx.get("e"), thread, ctx.get("o", 1)]
            else:  # token_count：官方额度；没有逐条记录的旧格式还要靠它的累计值算用量
                rl = p.get("rate_limits") or {}
                if ts and rl.get("limit_id") in (None, "codex") and (rl.get("secondary") or {}).get("used_percent") is not None:
                    se = rl["secondary"]
                    cx["rlh"].append([ts, se["used_percent"], se.get("resets_at")])  # 周额度历史，算「今天用了多少」
                if ts and rl.get("limit_id") in (None, "codex") and ts >= (cx.get("rl") or [0])[0]:
                    pr, se = rl.get("primary") or {}, rl.get("secondary") or {}
                    cx["rl"] = [ts, rl.get("plan_type"), pr.get("used_percent"), pr.get("window_minutes"), pr.get("resets_at"),
                                se.get("used_percent"), se.get("window_minutes"), se.get("resets_at")]
                info = p.get("info")
                if ctx.get("rec") or not info or not ts:
                    continue
                tot = [(info.get("total_token_usage") or {}).get(f) or 0 for f in CX_FIELDS]
                prev = ctx.get("tot")
                if prev and tot[0] + tot[2] <= prev[0] + prev[2]:
                    continue  # 重复的一条
                delta = [a - b for a, b in zip(tot, prev)] if prev else tot
                if min(delta) < 0:  # 累计值被重置过：用这一条自己的用量
                    delta = [(info.get("last_token_usage") or {}).get(f) or 0 for f in CX_FIELDS]
                ctx["tot"] = tot
                if delta[0] + delta[2] <= 0:
                    continue  # 空线程（建了没用）的 0 用量
                thread = ctx.get("t")
                msgs[f"tc|{thread}|{d.get('timestamp')}"] = [ts, ctx.get("m") or "?", *delta, ctx.get("e"), thread, ctx.get("o", 1)]
        files[key] = offset + end + 1
    for key in [k for k in files if k not in seen]:  # 文件被删：只清读取位置，记录保留
        files.pop(key, None)
        ctxs.pop(key, None)
    # 周额度历史：按时间排好，只留数值变化的点和最近 10 天
    cut, out = time.time() - 10 * 86400, []
    for h in sorted({tuple(h) for h in cx["rlh"] if h[0] >= cut}, key=lambda h: (h[0], h[1], h[2] or 0)):
        if not out or h[1] != out[-1][1] or abs((h[2] or 0) - (out[-1][2] or 0)) > 3600:  # 重置时刻每条会差几秒
            out.append(list(h))
    cx["rlh"] = out


def codex_backfill_rlh(files):
    out, cut = [], time.time() - 10 * 86400
    for key in files:
        path = Path(key)
        try:
            if path.stat().st_mtime < cut:
                continue
            with open(path, "rb") as f:
                for raw in f:
                    if CX_TC not in raw[:200]:
                        continue
                    try:
                        d = json.loads(raw)
                    except ValueError:
                        continue
                    rl = (d.get("payload") or {}).get("rate_limits") or {}
                    se = rl.get("secondary") or {}
                    ts = iso_ts(d.get("timestamp") or "")
                    if ts and ts >= cut and rl.get("limit_id") in (None, "codex") and se.get("used_percent") is not None:
                        out.append([ts, se["used_percent"], se.get("resets_at")])
        except OSError:
            continue
    return out


def codex_titles():
    out = {}
    try:
        with open(CODEX_INDEX, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("id") and d.get("thread_name"):
                    out[d["id"]] = clean(d["thread_name"])
    except OSError:
        pass
    return out


def codex_quota(cache, now):
    """Codex 官方额度：[(名称, 已用%, 重置时刻, 是否已过重置, 窗口开始时刻)]，以及套餐和记录时间。"""
    rl = (cache.get("cx") or {}).get("rl")
    if not rl:
        return None
    ts, plan, pu, pw, pr, su, sw, sr = rl
    rows = []
    for name, used, win, reset in (("5 小时", pu, pw, pr), ("每周", su, sw, sr)):
        if used is None or not reset:
            continue
        passed = reset <= now
        if passed:  # 记录之后已经重置过，按窗口长度推算下一次
            step = (win or 0) * 60 or WEEK
            while reset <= now:
                reset += step
        rows.append((name, 0 if passed else round(used), reset, passed, reset - ((win or 0) * 60 or WEEK)))
    return {"ts": ts, "plan": plan, "rows": rows}


def load_all():
    cache = load_cache()
    update(cache)
    recs = list(cache["msgs"].values()) + background_recs(cache)
    recs.sort(key=lambda r: r[0])
    return cache, recs


# ---------- 官方额度 ----------
# 桌面 App 运行时每 15 分钟把官方额度百分比记到这个文件：fh=5 小时，sd=每周
PLAN_HISTORY = HOME / "Library/Application Support/Claude/plan-usage-history.json"
WEEK = 7 * 86400
try:
    WEEKLY_ANCHOR = datetime.fromisoformat(CONFIG["weekly_reset_utc"].replace("Z", "+00:00")).timestamp()
except (KeyError, ValueError, AttributeError):
    WEEKLY_ANCHOR = None  # 没设置：本周累计改按最近 7 天算，不显示重置时间


# ---------- Claude 官方额度：直接向 Anthropic 查 ----------
# 和 Code 标签页里「Plan usage limits」面板同一个数据源。每 2 分钟最多查一次；被限流就按服务器给的时间暂停。
# 登录凭据：桌面 App 启动 Claude Code 时通过环境变量交给它；终端版的在钥匙串里。只在内存里用，不写盘、不显示。
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_FILE = STATE_DIR / "claude-usage.json"
USAGE_EVERY = 120


def claude_token(st, now):
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
        pids = [ln.split()[0] for ln in out.splitlines() if re.search(r"/claude\.app/Contents/MacOS/claude(\s|$)", ln)]
        for pid in reversed(pids):  # 后启动的进程拿到的凭据更新
            env = subprocess.run(["ps", "eww", "-o", "command=", "-p", pid], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"(?:^|\s)CLAUDE_CODE_OAUTH_TOKEN=(\S+)", env)
            if m:
                return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    if now < st.get("kc_skip", 0):
        return None
    try:  # 终端版 Claude Code：钥匙串（第一次会弹授权窗口）
        raw = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                             capture_output=True, text=True, timeout=30).stdout
        o = json.loads(raw)["claudeAiOauth"]
        if (o.get("expiresAt") or 0) / 1000 > now + 60:
            return o["accessToken"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        pass
    st["kc_skip"] = now + 6 * 3600  # 钥匙串里没有可用的：6 小时内别再问
    return None


def iso_or_none(s):
    t = iso_ts(s) if isinstance(s, str) else None
    return round(t / 60) * 60 if t else None  # 接口给的是 21:59:59.8 这种，取整到分钟


def claude_usage(now, fetch=True):
    """读缓存；到时间了就向 Anthropic 查一次。返回 {t: 查到的时刻, d: {fh, fh_r, sd, sd_r, scoped}, err, h: 历史}"""
    try:
        st = json.loads(USAGE_FILE.read_text())
    except (OSError, ValueError):
        st = {}
    if not fetch or now < st.get("next", 0):
        return st
    st["next"] = now + USAGE_EVERY
    tok = claude_token(st, now)
    if not tok:
        st["err"] = "没找到登录凭据（Claude 桌面 App 或 Claude Code 没在运行）"
    else:
        req = urllib.request.Request(USAGE_URL, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                                                         "Accept": "application/json", "User-Agent": "token-meter"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = json.load(r)
            fh, sd = raw.get("five_hour") or {}, raw.get("seven_day") or {}
            scoped = []
            for lim in raw.get("limits") or []:
                name = (((lim.get("scope") or {}).get("model") or {}).get("display_name"))
                if lim.get("kind") == "weekly_scoped" and name and lim.get("percent") is not None:
                    scoped.append([name, lim["percent"], iso_or_none(lim.get("resets_at"))])
            d = {"fh": fh.get("utilization"), "fh_r": iso_or_none(fh.get("resets_at")),
                 "sd": sd.get("utilization"), "sd_r": iso_or_none(sd.get("resets_at")), "scoped": scoped}
            st.update(t=now, d=d, err=None)
            h = [x for x in st.get("h", []) if x[0] >= now - 10 * 86400]
            if not h or h[-1][1:] != [d["fh"], d["sd"]] or now - h[-1][0] > 1800:
                h.append([now, d["fh"], d["sd"]])
            st["h"] = h
        except urllib.error.HTTPError as e:
            ra = e.headers.get("retry-after") or ""
            if e.code == 429:
                st["next"] = now + max(USAGE_EVERY, int(ra) if ra.isdigit() else 1200)
                st["err"] = f"被限流，{fmt_dur(st['next'] - now)}后再查"
            else:
                st["err"] = "登录凭据失效，等 Claude Code 换新凭据" if e.code in (401, 403) else f"接口返回 {e.code}"
        except (urllib.error.URLError, OSError, ValueError):
            st["err"] = "网络不通"
    try:
        STATE_DIR.mkdir(exist_ok=True)
        tmp = USAGE_FILE.with_name(f"claude-usage.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(USAGE_FILE)
    except OSError:
        pass
    return st


APP_LOG = HOME / "Library/Logs/Claude/main.log"


def app_poll_paused():
    """新版桌面 App（2026-09-30 起）只有在 24 小时内右键打开过它菜单栏图标的额度菜单，才在后台记额度
    （左键是快速输入，不算）。
    看日志里最后一条 [plan-usage] 是不是「已暂停」。"""
    step, limit = 1 << 20, 32 << 20  # 从文件尾往前每次读 1 MB，最多 32 MB
    try:
        with open(APP_LOG, "rb") as f:
            end = f.seek(0, 2)
            pos = end
            while pos > 0 and end - pos < limit:
                pos = max(0, pos - step)
                f.seek(pos)
                chunk = f.read(min(step + 4096, end - pos))  # 多读一点，免得切断跨块的那一行
                hits = [ln for ln in chunk.split(b"\n") if b"[plan-usage]" in ln]
                if hits:
                    return b"background poll paused" in hits[-1]
    except OSError:
        pass
    return False


def read_plan_history():
    try:
        d = json.loads(PLAN_HISTORY.read_text())
    except (OSError, ValueError):
        return []
    return [(x["t"] / 1000, x["u"]) for x in d.get("samples", []) if x.get("u")]


def next_weekly_reset(now):
    if WEEKLY_ANCHOR is None:
        return None
    k = -(-(now - WEEKLY_ANCHOR) // WEEK)  # 向上取整
    return WEEKLY_ANCHOR + k * WEEK


def bar(pct, width=20):
    n = max(0, min(width, round(pct / 100 * width)))
    return "█" * n + "░" * (width - n)


def level_color(pct):
    return "#e5533d" if pct >= 85 else ("#e6a23c" if pct >= 60 else None)


def gauge_icon(pct):
    return ("gauge.with.dots.needle.0percent" if pct < 17 else
            "gauge.with.dots.needle.33percent" if pct < 50 else
            "gauge.with.dots.needle.50percent" if pct < 67 else
            "gauge.with.dots.needle.67percent" if pct < 90 else
            "gauge.with.dots.needle.100percent")


# ---------- 统计 ----------

def tokens(r):
    return r[2] + r[3] + r[4] + r[5] + r[6]


def cost_parts(r):
    """API 价拆成 输入 / 输出 / 缓存写 / 缓存读 四份（美元）。"""
    if not is_claude(r[1]):
        return (0.0, 0.0, 0.0, 0.0)
    pi, po, pw5, pw1, pr = price_for(r[1])
    k = 2e-6 if r[8] else 1e-6  # fast 模式双倍价
    return (r[2] * pi * k, r[3] * po * k, (r[4] * pw5 + r[5] * pw1) * k, r[6] * pr * k)


def cost(r):
    return sum(cost_parts(r))


class Agg:
    __slots__ = ("inp", "out", "cw", "cr", "think", "usd", "parts", "n")

    def __init__(self):
        self.inp = self.out = self.cw = self.cr = self.think = self.n = 0
        self.usd = 0.0
        self.parts = [0.0, 0.0, 0.0, 0.0]

    def add(self, r):
        self.inp += r[2]
        self.out += r[3]
        self.cw += r[4] + r[5]
        self.cr += r[6]
        self.think += r[10]
        cp = cost_parts(r)
        for i in range(4):
            self.parts[i] += cp[i]
        self.usd += sum(cp)
        self.n += 1

    @property
    def tok(self):
        return self.inp + self.out + self.cw + self.cr


def windows(recs):
    """模拟 5 小时额度窗口：窗口从第一条消息所在的 10 分钟整点开始，持续 5 小时。"""
    out = []
    for r in recs:
        if not out or r[0] >= out[-1][0] + WINDOW:
            start = r[0] - (r[0] % 600)  # 官方窗口起点对齐到 10 分钟
            out.append([start, Agg()])
        out[-1][1].add(r)
    return out


# ---------- 输出 ----------

def fmt_tok(n):
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def fmt_usd(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def fmt_dur(sec):
    sec = max(0, int(sec))
    h, m = divmod(sec // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def spark(values):
    top = max(values) or 1
    return "".join(SPARK[min(7, int(v / top * 7.999))] if v else " " for v in values)


def pad(text, width):
    """按显示宽度截断/补空格（中文占两格）。"""
    out, w = "", 0
    for ch in text:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width:
            break
        out += ch
        w += cw
    return out + " " * (width - w)


INK = "#1D1D1F,#ECECEC"  # 浅色模式,深色模式
ACTIONS = ("bash", "href", "refresh", "shell")
_in_menu = False  # 第一条 --- 之前是菜单栏标题，颜色交给系统


def line(text, **params):
    global _in_menu
    if text == "---":
        _in_menu = True
    # 没有点击动作的行会被 SwiftBar 画成灰色（不可点），这里显式上色；分隔线不能带参数
    if _in_menu and text.strip("-") and not any(k in params for k in ACTIONS):
        params.setdefault("color", INK)
        if "sfimage" in params:
            params.setdefault("sfcolor", params["color"])
    p = " ".join(f"{k}={v}" for k, v in params.items())
    print(f"{text} | {p}" if p else text)


def breakdown(prefix, a):
    line(f"{prefix}输入 {fmt_tok(a.inp)} · 输出 {fmt_tok(a.out)}", size=12, font="Menlo")
    line(f"{prefix}缓存写 {fmt_tok(a.cw)} · 缓存读 {fmt_tok(a.cr)}", size=12, font="Menlo")


def unit_price(model):
    pi, po, pw5, pw1, pr = price_for(model)
    g = lambda x: f"{x:g}"
    return f"每百万：输入 ${g(pi)} · 输出 ${g(po)} · 缓存写 ${g(pw5)} · 缓存读 ${g(pr)}"


def week_section(recs, sd, now, cache):
    """本周累计：按官方每周额度的周期（上次重置到现在）统计。"""
    wr = next_weekly_reset(now)
    w0 = wr - WEEK if wr else now - WEEK
    rs = [r for r in recs if r[0] >= w0]
    cl = [r for r in rs if is_claude(r[1])]
    wk = Agg()
    by_model = defaultdict(Agg)
    by_effort = defaultdict(Agg)
    by_model_effort = defaultdict(lambda: defaultdict(Agg))
    for r in cl:
        e = r[9] or NO_EFFORT
        wk.add(r); by_model[r[1]].add(r); by_effort[e].add(r); by_model_effort[r[1]][e].add(r)
    days = (now - w0) / 86400

    ws = datetime.fromtimestamp(w0)
    if wr:
        line(f"Claude 本周累计（周{'一二三四五六日'[ws.weekday()]} {ws:%H:%M} 起 · 第 {int(days) + 1} 天）", sfimage="chart.bar.xaxis", size=13)
    else:
        line("Claude 本周累计（最近 7 天）", sfimage="chart.bar.xaxis", size=13)
    line(f"共 {fmt_tok(wk.tok)} tokens · API 价 {fmt_usd(wk.usd)}", **_f())
    if BG in by_effort:
        line(f"其中后台调用 {fmt_usd(by_effort[BG].usd)}（会话记录里没有，按 Claude Code 的费用汇总补上）", size=11, color="#888888")

    # 套餐
    plan = f"套餐 {PLAN_NAME or '未设置'}"
    if sd is not None:
        plan += f" · 周额度已用 {sd}%"
    line(plan, **_f())
    if wk.usd and PLAN_USD_MONTH:
        week_fee = PLAN_USD_MONTH * 12 / 52
        line(f"--套餐费约 ${week_fee:.0f}/周（${PLAN_USD_MONTH}/月）", **_f())
        line(f"--本周 API 价是周费的 {wk.usd / week_fee:.1f} 倍", **_f())
        if sd:
            line(f"--每 1% 周额度 ≈ {fmt_usd(wk.usd / sd)} API 价", **_f())
            line(f"--按这个比例，用满一周 ≈ {fmt_usd(wk.usd / sd * 100)}", **_f())
        line(f"--日均 {fmt_usd(wk.usd / max(days, 1))} · {fmt_tok(wk.tok / max(days, 1))}", **_f())

    # 按模型
    line(f"按模型  {len(by_model)} 个", sfimage="cpu", **_f())
    for m, a in sorted(by_model.items(), key=lambda kv: -kv[1].usd):
        line(f"--{short(m):<12} {fmt_tok(a.tok):>7}  {fmt_usd(a.usd):>9}  {a.usd / wk.usd * 100 if wk.usd else 0:>3.0f}%", **_f())
        line(f"----{unit_price(m)}", size=11, color="#888888")
        line(f"----输入 {fmt_tok(a.inp)} · 输出 {fmt_tok(a.out)}（思考 {fmt_tok(a.think)}）", **_f())
        line(f"----缓存写 {fmt_tok(a.cw)} · 缓存读 {fmt_tok(a.cr)}", **_f())
        line("-------")
        for e in EFFORT_ORDER:
            if e in by_model_effort[m]:
                b = by_model_effort[m][e]
                line(f"----{pad(e, 6)} {fmt_tok(b.tok):>7}  {fmt_usd(b.usd):>9}  {b.n} 次回复", **_f())

    # 按思考强度
    line("按思考强度", sfimage="brain", **_f())
    for e in EFFORT_ORDER:
        if e in by_effort:
            a = by_effort[e]
            line(f"--{pad(e, 6)} {fmt_tok(a.tok):>7}  {fmt_usd(a.usd):>9}  思考 {fmt_tok(a.think):>5}  {a.n} 次", **_f())
    line("-----")
    line(f"--思考 tokens 共 {fmt_tok(wk.think)}，占输出 {wk.think / wk.out * 100 if wk.out else 0:.0f}%", **_f())
    line("--思考 tokens 按输出价计费，已含在输出里", size=11, color="#888888")

    # API 价构成
    line("API 价构成", sfimage="dollarsign.circle", **_f())
    for name, n, usd in (("输入", wk.inp, wk.parts[0]), ("输出", wk.out, wk.parts[1]),
                         ("缓存写", wk.cw, wk.parts[2]), ("缓存读", wk.cr, wk.parts[3])):
        line(f"--{pad(name, 6)} {fmt_tok(n):>7}  {fmt_usd(usd):>9}  {usd / wk.usd * 100 if wk.usd else 0:>3.0f}%", **_f())

    # 各窗口
    wins = {}
    for r in cl:
        wid, title, kind = window_of(r[11], cache)
        w = wins.get(wid)
        if not w:
            w = wins[wid] = {"title": title, "kind": kind, "a": Agg(), "bg": 0.0, "models": defaultdict(float),
                             "efforts": defaultdict(float), "t0": r[0], "t1": r[0]}
        w["a"].add(r)
        w["models"][r[1]] += cost(r)
        w["efforts"][r[9] or NO_EFFORT] += cost(r)
        if r[12]:
            w["bg"] += cost(r)
        w["t1"] = r[0]
    line(f"本周各窗口  {len(wins)} 个", sfimage="macwindow.on.rectangle", **_f())
    for w in sorted(wins.values(), key=lambda w: -w["a"].usd)[:30]:
        a = w["a"]
        line(f"--{pad(w['title'], 26)} {fmt_tok(a.tok):>7} {fmt_usd(a.usd):>8}", **_f())
        line(f"----{w['kind']} · {datetime.fromtimestamp(w['t0']):%m-%d %H:%M} – {datetime.fromtimestamp(w['t1']):%m-%d %H:%M}", size=11, color="#888888")
        line(f"----输入 {fmt_tok(a.inp)} · 输出 {fmt_tok(a.out)}（思考 {fmt_tok(a.think)}）", **_f())
        line(f"----缓存写 {fmt_tok(a.cw)} · 缓存读 {fmt_tok(a.cr)}", **_f())
        line("-------")
        for m, usd in sorted(w["models"].items(), key=lambda kv: -kv[1]):
            line(f"----{short(m):<14} {fmt_usd(usd):>8}", **_f())
        line("-------")
        for e in EFFORT_ORDER:
            if e in w["efforts"]:
                line(f"----{pad(e, 6)} {fmt_usd(w['efforts'][e]):>8}", **_f())
    if len(wins) > 30:
        line(f"--……还有 {len(wins) - 30} 个，见详细报告", size=11, color="#888888")

    other = [r for r in rs if not is_claude(r[1])]
    if other:
        line(f"第三方模型 {fmt_tok(sum(tokens(r) for r in other))}（不占额度，不计价）", size=11, color="#888888")


def fmt_reset(t, now):
    d = datetime.fromtimestamp(t)
    left = fmt_dur(t - now) if t - now < 86400 else f"{(t - now) / 86400:.1f} 天"
    gap = (d.date() - datetime.fromtimestamp(now).date()).days
    day = "" if gap == 0 else "明天 " if gap == 1 else f"周{'一二三四五六日'[d.weekday()]} "
    return f"{day}{d:%H:%M} 重置（{left}）"


def fmt_at(t, now):
    d, n = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    day = "今天" if d.date() == n.date() else "明天" if (d.date() - n.date()).days == 1 else f"周{'一二三四五六日'[d.weekday()]}"
    return f"{day} {d:%H:%M}"


def daily_lines(used, start, reset, now, used_at, per_pct, unit):
    """把周额度平分到 7 天（每天从重置时刻起算 24 小时），哪天没用完的累积到下一天。
    used_at(t)：t 时刻周额度已用了多少 %。per_pct：这周每 1% 额度实际对应多少（Claude 是 API 价，Codex 是 tokens）。"""
    ind = " " * 9
    days = max(1, round((reset - start) / 86400))
    share = 100 / days
    remain = max(0, 100 - used)
    if remain <= 0:
        line(f"{ind}周额度已经用满，{fmt_at(reset, now)} 重置", **_f(), color="#e5533d")
        return
    k = min(days - 1, max(0, int((now - start) // 86400)))
    d0 = start + k * 86400
    base = min(used, max(0, used_at(d0)))  # 今天这一段开始时已经用到多少
    allow = 100 - base if k == days - 1 else (k + 1) * share - base  # 最后一天：剩下的全可以用
    today = max(0, used - base)
    carry = allow - share
    if allow <= 0.05:
        line(f"今天     {bar(100, 14)}  ——  前几天超用了 {-carry:.1f}%，今天的额度已经没了", **_f(), color="#e5533d")
    else:
        pct = today / allow * 100
        col = "#e5533d" if pct > 100 else ("#e6a23c" if pct >= 85 else None)
        line(f"今天     {bar(min(pct, 100), 14)} {pct:>3.0f}%  已用 {today:.1f}% / 可用 {allow:.1f}%", **_f(), **({"color": col} if col else {}))
    last_day = k == days - 1
    note = [f"第 {k + 1}/{days} 天，" + (f"{fmt_at(reset, now)} 重置" if last_day else f"{fmt_at(d0 + 86400, now)} 换下一天")]
    if allow <= 0.05:
        pass  # 上面一行已经说了
    elif last_day:
        note.append("最后一天，剩下的都可以用")
    elif carry > 0.05:
        note.append(f"含前几天没用完的 {carry:.1f}%")
    elif carry < -0.05:
        note.append(f"前几天超用 {-carry:.1f}%，今天少了")
    else:
        note.append(f"每天 {share:.1f}%")
    if per_pct and used >= 3 and allow > today:  # 用得太少时换算不准，不显示
        note.append(f"还能用 ≈ {unit((allow - today) * per_pct)}")
    line(ind + " · ".join(note), size=11, color="#888888")
    elapsed = (now - start) / 86400
    if elapsed < 0.25 or used <= 0:
        return  # 刚重置，还看不出速度
    pace = used / elapsed
    left = (reset - now) / 86400
    if used + pace * left >= 100:
        line(f"{ind}现在平均每天 {pace:.1f}%，照这样{fmt_at(now + remain / pace * 86400, now)} 左右用完", size=11, color="#e6a23c")
    else:
        line(f"{ind}现在平均每天 {pace:.1f}%，照这样到重置时用到 {used + pace * left:.0f}%", size=11, color="#888888")


def claude_used_at(samples, recs, w0, used_now):
    """Claude 周额度在某个时刻用到了多少：优先查桌面 App 的记录；App 没开那段，按这周花费的比例估。"""
    pts = [(t, u["sd"]) for t, u in samples if t >= w0 and u.get("sd") is not None]
    spend = [(r[0], cost(r)) for r in recs if r[0] >= w0]
    total = sum(c for _, c in spend)

    def at(t):
        before = [u for ts, u in pts if ts <= t]
        if before and t - max(ts for ts, _ in pts if ts <= t) < 2 * 3600:
            return before[-1]
        est = used_now * sum(c for ts, c in spend if ts < t) / total if total else 0
        return max(before[-1] if before else 0, est)
    return at


def codex_used_at(cache, reset):
    """Codex 周额度在某个时刻用到了多少：查记录里的周额度历史（只有用 Codex 时才有记录，没记录 = 没用）。"""
    hist = [(t, u) for t, u, r in (cache.get("cx") or {}).get("rlh", []) if r and abs(r - reset) < 6 * 3600]

    def at(t):
        before = [u for ts, u in hist if ts <= t]
        return before[-1] if before else 0
    return at


def codex_quota_lines(q, now, cache):
    plan = cx_plan(q["plan"])
    line("-----")
    line(f"Codex 官方额度" + (f"（{plan}）" if plan else ""), sfimage="chevron.left.forwardslash.chevron.right", size=13)
    for name, used, reset, _, start in q["rows"]:
        lc = level_color(used)
        line(f"{pad(name, 9)}{bar(used, 14)} {used:>3}%  {fmt_reset(reset, now)}", **_f(), **({"color": lc} if lc else {}))
        if name == "每周":
            msgs = (cache.get("cx") or {}).get("msgs") or {}
            spent = sum(cx_tok(r) for r in msgs.values() if r[8] and r[0] >= start)
            daily_lines(used, start, reset, now, codex_used_at(cache, reset), spent / used if used else None, lambda x: f"{fmt_tok(x)} tokens")
    age = now - q["ts"]
    note = "刚刚" if age < 60 else f"{int(age // 60)} 分钟前" if age < 3600 else (f"{age / 3600:.0f} 小时前" if age < 86400 else f"{age / 86400:.0f} 天前")
    line(f"来自 Codex 记录 · {note}（用 Codex 时才会更新）", size=11, color="#888888")
    if any(r[3] for r in q["rows"]):
        line("记录之后已过重置时间，按 0% 显示", size=11, color="#888888")


def cx_plan(p):
    return {"plus": "Plus", "pro": "Pro", "go": "Go", "free": "免费版", "team": "Team", "business": "Business", "enterprise": "Enterprise"}.get(p, p or "")


CX_EFFORTS = ["ultra", "max", "xhigh", "high", "medium", "low", "minimal", NO_EFFORT]


def cx_title(t, rs, titles):
    if titles.get(t):
        return titles[t]
    if rs and all(r[1] == "codex-auto-review" for r in rs):
        return "自动审查"
    return f"线程 {t[:8]}"


def cx_tok(r):
    return r[2] + r[4]  # 输入（含缓存）+ 输出（含推理）


def codex_section(cache, now, q):
    """Codex 本周用量：按 Codex 每周额度的周期统计。OpenAI 没公布这些模型的单价，只算 tokens。"""
    cx = cache["cx"]
    recs = sorted(cx["msgs"].values(), key=lambda r: r[0])
    weekly = next((r for r in (q or {}).get("rows", []) if r[0] == "每周"), None)
    w0 = weekly[4] if weekly else now - WEEK
    wk = [r for r in recs if r[0] >= w0]
    mine = [r for r in wk if r[8]]
    other = [r for r in wk if not r[8]]
    titles, par = codex_titles(), cx.get("par", {})

    def tot(rs):
        return sum(cx_tok(r) for r in rs)

    ws = datetime.fromtimestamp(w0)
    line(f"Codex 本周（周{'一二三四五六日'[ws.weekday()]} {ws:%H:%M} 起 · 第 {int((now - w0) / 86400) + 1} 天）", sfimage="chart.bar.xaxis", size=13)
    inp, cached, out, rsn = (sum(r[i] for r in mine) for i in (2, 3, 4, 5))
    line(f"共 {fmt_tok(inp + out)} tokens · {len(mine):,} 次回复", **_f())
    if inp:
        line(f"其中缓存命中 {cached / inp * 100:.0f}% · 输出 {fmt_tok(out)}（推理 {fmt_tok(rsn)}）", size=11, color="#888888")

    by_m = defaultdict(list)
    for r in mine:
        by_m[r[1]].append(r)
    line(f"按模型  {len(by_m)} 个", sfimage="cpu", **_f())
    for m, rs in sorted(by_m.items(), key=lambda kv: -tot(kv[1])):
        line(f"--{pad(m, 18)} {fmt_tok(tot(rs)):>7}  {len(rs):>5} 次  {tot(rs) / max(1, tot(mine)) * 100:>3.0f}%", **_f())
        i, c, o, rr = (sum(r[k] for r in rs) for k in (2, 3, 4, 5))
        line(f"----输入 {fmt_tok(i)}（缓存 {fmt_tok(c)}）· 输出 {fmt_tok(o)}（推理 {fmt_tok(rr)}）", **_f())
    by_e = defaultdict(list)
    for r in mine:
        by_e[r[6] or NO_EFFORT].append(r)
    line("按思考强度", sfimage="brain", **_f())
    for e in sorted(by_e, key=lambda e: CX_EFFORTS.index(e) if e in CX_EFFORTS else 99):
        rs = by_e[e]
        line(f"--{pad(e, 8)} {fmt_tok(tot(rs)):>7}  推理 {fmt_tok(sum(r[5] for r in rs)):>5}  {len(rs):>5} 次", **_f())

    by_t = defaultdict(list)
    for r in wk:
        t = r[7] or "?"
        by_t[par.get(t, t)].append(r)  # 子代理算到父线程
    line(f"本周各线程  {len(by_t)} 个", sfimage="macwindow.on.rectangle", **_f())
    for t, rs in sorted(by_t.items(), key=lambda kv: -tot(kv[1]))[:30]:
        line(f"--{pad(cx_title(t, rs, titles), 26)} {fmt_tok(tot(rs)):>7} {len(rs):>5} 次", **_f())
        models = sorted({r[1] for r in rs}, key=lambda m: -tot([r for r in rs if r[1] == m]))
        line(f"----{datetime.fromtimestamp(rs[0][0]):%m-%d %H:%M} – {datetime.fromtimestamp(rs[-1][0]):%m-%d %H:%M} · {'、'.join(models)}", size=11, color="#888888")
    if len(by_t) > 30:
        line(f"--……还有 {len(by_t) - 30} 个", size=11, color="#888888")

    t0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    mo = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    own = [r for r in recs if r[8]]
    line(f"今日 {fmt_tok(tot([r for r in own if r[0] >= t0]))} · 近7天 {fmt_tok(tot([r for r in own if r[0] >= t0 - 6 * 86400]))} · 本月 {fmt_tok(tot([r for r in own if r[0] >= mo]))}", sfimage="sun.max", **_f())
    if other:
        by_o = defaultdict(list)
        for r in other:
            by_o[r[1]].append(r)
        line(f"第三方模型（走代理，不占 Codex 额度）{fmt_tok(tot(other))}", sfimage="puzzlepiece", **_f())
        for m, rs in sorted(by_o.items(), key=lambda kv: -tot(kv[1])):
            line(f"--{pad(m, 18)} {fmt_tok(tot(rs)):>7}  {len(rs):>5} 次", **_f())
    line(f"OpenAI 没公布这些模型的 API 单价，Codex 只统计 tokens · 累计 {fmt_tok(tot(own))}，自 {datetime.fromtimestamp(recs[0][0]):%Y-%m-%d}", size=11, color="#888888")


def main():
    try:
        cache, recs = load_all()
    except Exception as e:  # 统计失败也要让菜单栏有东西
        line("⚠︎ token", color="#d9534f")
        line("---")
        line(f"解析出错：{e}")
        return

    claude = [r for r in recs if is_claude(r[1])]
    other = [r for r in recs if not is_claude(r[1])]

    now = time.time()
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = today.timestamp()
    month0 = today.replace(day=1).timestamp()
    week0 = t0 - 6 * 86400

    day = Agg(); week = Agg(); month = Agg(); total = Agg()
    by_model_today = defaultdict(Agg)
    by_model_month = defaultdict(Agg)
    by_proj_month = defaultdict(Agg)
    daily = defaultdict(float)
    for r in claude:
        total.add(r)
        ts = r[0]
        daily[datetime.fromtimestamp(ts).date()] += cost(r)
        if ts >= month0:
            month.add(r); by_model_month[r[1]].add(r); by_proj_month[r[7]].add(r)
        if ts >= week0:
            week.add(r)
        if ts >= t0:
            day.add(r); by_model_today[r[1]].add(r)

    wins = windows(claude)
    cur = wins[-1] if wins and now < wins[-1][0] + WINDOW else None

    global WEEKLY_ANCHOR
    api = claude_usage(now)
    live = api.get("d") if api.get("d") and now - api.get("t", 0) < 20 * 60 else None
    if live and live.get("sd_r"):
        WEEKLY_ANCHOR = live["sd_r"]  # 官方的每周重置时刻，比 config 里填的准
    # App 的记录 + 自己查到的历史，合在一起给走势图和「今天」用
    samples = sorted(read_plan_history() + [(t, {"fh": a, "sd": b}) for t, a, b in api.get("h", [])], key=lambda x: x[0])
    last = samples[-1] if samples else None
    age = now - last[0] if last else None
    stale = age is not None and age > 20 * 60
    fh = last[1].get("fh") if last else None
    sd = last[1].get("sd") if last else None
    if live:
        fh, sd, stale, age = live["fh"], live["sd"], False, now - api["t"]
        last = (api["t"], {"fh": fh, "sd": sd})
        fh = None if fh is None else round(fh)
        sd = None if sd is None else round(sd)
    # App 每 15 分钟才记一次。最近一次记录如果早于最近一次重置，它记的是上一个窗口的数，
    # 当前窗口按 0% 显示，等 App 下次记录再更新。
    # 最近一次 5 小时重置 = 当前窗口的起点；空闲时 = 上一个窗口的结束
    fh_reset_at = cur[0] if cur else (wins[-1][0] + WINDOW if wins else None)
    if live and live.get("fh_r"):
        fh_reset_at = live["fh_r"] - WINDOW
    fh_reset = fh is not None and ((fh_reset_at is not None and last[0] < fh_reset_at) or (not cur and not live and age > WINDOW))
    if fh_reset:
        fh = 0
    wr_now = next_weekly_reset(now)
    sd_reset = sd is not None and wr_now is not None and last[0] < wr_now - WEEK
    if sd_reset:
        sd = 0

    cxq = codex_quota(cache, now)
    cx_worst = max((r[1] for r in cxq["rows"]), default=None) if cxq else None

    # --- 菜单栏标题 ---
    if fh is not None or sd is not None:
        parts = []
        if fh is not None:
            parts.append(f"5h {fh}%")
        if sd is not None:
            parts.append(f"周 {sd}%")
        if cx_worst is not None and cx_worst >= 60:  # Codex 快满了才挤进菜单栏
            parts.append(f"Codex {cx_worst}%")
        worst = max(v for v in (fh, sd, cx_worst if cx_worst is not None and cx_worst >= 60 else None) if v is not None)
        title = " · ".join(parts) + (" ?" if stale else "")
        params = {"sfimage": gauge_icon(worst), "sfsize": 13}
        if level_color(worst):
            params["color"] = level_color(worst)
    else:
        title = f"今 {fmt_tok(day.tok)}"
        params = {"sfimage": "gauge.with.dots.needle.bottom.0percent", "sfsize": 13}
    line(title, **params)
    line("---")

    # --- 官方额度 ---
    line("Claude 官方额度", sfimage="checkmark.seal", size=13)
    if fh is not None:
        fh_end = live["fh_r"] if live and live.get("fh_r") else (cur[0] + WINDOW if cur else None)
        when = fmt_reset(fh_end, now) if fh_end and fh_end > now else "空闲中"
        line(f"5 小时   {bar(fh, 14)} {fh:>3}%  {when}", **_f(), **({"color": level_color(fh)} if level_color(fh) else {}))
        recent = [u.get("fh") or 0 for t, u in samples if fh_reset_at and t >= fh_reset_at]
        if len(recent) >= 2:
            line(f"         {spark(recent)}  本窗口走势", **_f(), color="#888888")
    if sd is not None:
        wr = next_weekly_reset(now)
        when = fmt_reset(wr, now) if wr else ""
        line(f"每周     {bar(sd, 14)} {sd:>3}%  {when}".rstrip(), **_f(), **({"color": level_color(sd)} if level_color(sd) else {}))
        if wr:
            spent = sum(cost(r) for r in claude if r[0] >= wr - WEEK)
            daily_lines(sd, wr - WEEK, wr, now, claude_used_at(samples, claude, wr - WEEK, sd), spent / sd if sd else None, lambda x: f"${x:,.0f} API 价")
    for name, pct, reset in (live or {}).get("scoped", []):
        lc = level_color(pct)
        line(f"{pad(name + ' 周', 9)}{bar(pct, 14)} {round(pct):>3}%  {fmt_reset(reset, now) if reset else ''}".rstrip(), **_f(), **({"color": lc} if lc else {}))
    if live:
        line(f"来自 Anthropic 官方接口 · {'刚刚' if age < 60 else f'{int(age // 60)} 分钟前'}（每 2 分钟更新）", size=11, color="#888888")
    elif last:
        if api.get("err"):
            line(f"官方接口暂时查不到：{api['err']}，先显示 App 的记录", size=11, color="#e6a23c")
        note = f"{int(age // 60)} 分钟前" if age >= 60 else "刚刚"
        paused = stale and app_poll_paused()
        line(f"来自 Claude App 记录 · {note}" + ("" if not stale else "（App 暂停了记录）" if paused else "（App 没开时不会更新）"), size=11, color="#e6a23c" if stale else "#888888")
        for done, what, at in ((fh_reset and cur, "5 小时额度", fh_reset_at), (sd_reset, "每周额度", wr_now - WEEK if wr_now else None)):
            if done and at:
                line(f"{what} {datetime.fromtimestamp(at):%H:%M} 已重置，App 下次记录后显示新数字", size=11, color="#888888")
    else:
        if api.get("err"):
            line(f"官方接口暂时查不到：{api['err']}", size=11, color="#e6a23c")
        line("也没找到 Claude 桌面 App 的额度记录", size=11, color="#e6a23c")
    if cxq:
        codex_quota_lines(cxq, now, cache)
    line("---")

    week_section(recs, sd, now, cache)
    line("查看详细报告…", sfimage="doc.text.magnifyingglass", bash=os.path.abspath(__file__), param1="--report", terminal="false")
    line("---")
    if (cache.get("cx") or {}).get("msgs"):
        codex_section(cache, now, cxq)
        line("---")

    # --- 本地统计的当前窗口 ---
    if cur:
        start, a = cur
        wend = start + WINDOW
        line("本窗口本地统计", sfimage="timer", size=13)
        line(f"{datetime.fromtimestamp(start):%H:%M} 起 · {fmt_tok(a.tok)} tokens · 折合 {fmt_usd(a.usd)}", **_f())
        breakdown("", a)
    else:
        line("5 小时窗口：空闲中", sfimage="moon.zzz", size=13)
    line("---")

    # --- 今日 / 周 / 月 ---
    line(f"今日  {fmt_tok(day.tok):>7}  {fmt_usd(day.usd):>9}", sfimage="sun.max", **_f())
    for m, a in sorted(by_model_today.items(), key=lambda kv: -kv[1].usd):
        line(f"--{short(m):<12} {fmt_tok(a.tok):>7}  {fmt_usd(a.usd):>9}", **_f())
    line("-----")  # 子菜单分隔
    breakdown("--", day)
    line(f"近7天 {fmt_tok(week.tok):>7}  {fmt_usd(week.usd):>9}", sfimage="calendar", **_f())
    line(f"本月  {fmt_tok(month.tok):>7}  {fmt_usd(month.usd):>9}", sfimage="calendar.circle", **_f())
    for m, a in sorted(by_model_month.items(), key=lambda kv: -kv[1].usd):
        line(f"--{short(m):<12} {fmt_tok(a.tok):>7}  {fmt_usd(a.usd):>9}", **_f())

    days = [(today - timedelta(days=i)).date() for i in range(13, -1, -1)]
    line(f"近14天 {spark([daily[d] for d in days])}", **_f())
    for d in reversed(days):
        if daily[d]:
            line(f"--{d:%m-%d %a}  {fmt_usd(daily[d]):>9}", **_f())
    line("---")

    # --- 项目 ---
    line("本月项目排行", sfimage="folder", size=13)
    for name, a in sorted(by_proj_month.items(), key=lambda kv: -kv[1].usd)[:6]:
        line(f"{pad(name, 18)} {fmt_tok(a.tok):>7} {fmt_usd(a.usd):>8}", **_f())

    if other:
        o = Agg()
        by = defaultdict(int)
        for r in other:
            o.add(r); by[r[1]] += tokens(r)
        line("---")
        line(f"第三方模型（不占套餐额度）{fmt_tok(o.tok)}", sfimage="puzzlepiece", size=13)
        for m, n in sorted(by.items(), key=lambda kv: -kv[1]):
            line(f"--{m:<16} {fmt_tok(n):>7}", **_f())

    line("---")
    line(f"累计 {fmt_tok(total.tok)} · 折合 {fmt_usd(total.usd)} · 自 {datetime.fromtimestamp(claude[0][0]):%Y-%m-%d}" if claude else "暂无数据", size=11, color="#888888")
    line(f"更新于 {datetime.now():%H:%M:%S}", size=11, color="#888888")
    line("立即刷新", sfimage="arrow.clockwise", refresh="true")
    line("重建缓存", sfimage="trash", bash=os.path.abspath(__file__), param1="--reset", terminal="false", refresh="true")


def _f():
    return {"font": "Menlo", "size": 12}


# ---------- 详细报告（HTML） ----------

def write_report():
    global WEEKLY_ANCHOR
    api = claude_usage(time.time(), fetch=False)
    if (api.get("d") or {}).get("sd_r"):
        WEEKLY_ANCHOR = api["d"]["sd_r"]
    cache, recs = load_all()
    models, projs, efforts, wins = [], [], [], []
    mi, pi, ei, wi = {}, {}, {}, {}

    def ix(d, table, key, val=None):
        if key not in d:
            d[key] = len(table)
            table.append(val if val is not None else key)
        return d[key]

    rows = []
    for r in recs:
        wid, title, kind = window_of(r[11], cache)
        cp = cost_parts(r)
        rows.append([int(r[0]), ix(mi, models, r[1]), r[2], r[3], r[4] + r[5], r[6], r[10],
                     ix(wi, wins, wid, [title, kind]), ix(ei, efforts, r[9] or NO_EFFORT),
                     r[12], ix(pi, projs, r[7])] + [round(x, 6) for x in cp])
    prices = {m: list(price_for(m)) for m in models if is_claude(m)}
    now = time.time()
    wr = next_weekly_reset(now)
    data = {
        "rows": rows, "models": models, "projs": projs, "efforts": efforts, "wins": wins,
        "effortOrder": EFFORT_ORDER, "prices": prices,
        "plan": [x for x in read_plan_history_raw()],
        "weekStart": (wr - WEEK) if wr else None,
        "planName": PLAN_NAME, "planUsd": PLAN_USD_MONTH,
        "generated": now,
        "cx": codex_report_data(cache, now),
    }
    STATE_DIR.mkdir(exist_ok=True)
    html = REPORT_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    tmp = REPORT.with_suffix(".tmp")
    tmp.write_text(html)
    tmp.replace(REPORT)


def codex_report_data(cache, now):
    cx = cache.get("cx") or {}
    if not cx.get("msgs"):
        return None
    titles, par = codex_titles(), cx.get("par", {})
    recs = sorted(cx["msgs"].values(), key=lambda r: r[0])
    by_t = defaultdict(list)
    for r in recs:
        by_t[par.get(r[7] or "?", r[7] or "?")].append(r)
    models, efforts, threads = [], [], []
    mi, ei, ti = {}, {}, {}

    def ix(d, table, key, val=None):
        if key not in d:
            d[key] = len(table)
            table.append(val if val is not None else key)
        return d[key]

    rows = []
    for t, rs in by_t.items():
        k = ix(ti, threads, t, cx_title(t, rs, titles))
        for r in rs:
            rows.append([int(r[0]), ix(mi, models, r[1]), r[2], r[3], r[4], r[5], ix(ei, efforts, r[6] or NO_EFFORT), k, r[8]])
    q = codex_quota(cache, now)
    return {"rows": rows, "models": models, "efforts": efforts, "threads": threads, "effortOrder": CX_EFFORTS,
            "quota": [[n, u, int(t)] for n, u, t, _, _ in q["rows"]] if q else [], "plan": cx_plan(q["plan"]) if q else None}


def read_plan_history_raw():
    own = [(t, {"fh": a, "sd": b}) for t, a, b in claude_usage(time.time(), fetch=False).get("h", [])]
    return [[int(t), u.get("fh"), u.get("sd")] for t, u in sorted(read_plan_history() + own, key=lambda x: x[0])]


REPORT_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 用量明细</title>
<style>
:root {
  --bg: #f6f6f4; --card: #ffffff; --ink: #1d1d1b; --muted: #74746f; --line: #e6e5e0;
  --accent: #c96442; --accent-soft: #f3e3dc; --bg-call: #9a9a93; --hover: #f1f0ec;
  --c1: #c96442; --c2: #3b7dd8; --c3: #3f9d6d; --c4: #b0801f; --c5: #8b5cc4; --c6: #d0527b; --c7: #4aa3b5; --c8: #7a7a72;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #191918; --card: #232322; --ink: #ecebe6; --muted: #9c9b95; --line: #34342f;
    --accent: #e0825f; --accent-soft: #3a2a23; --bg-call: #6e6e68; --hover: #2c2c2a;
    --c1: #e0825f; --c2: #6aa2ee; --c3: #62bf8d; --c4: #d6a743; --c5: #a987dc; --c6: #e27a9c; --c7: #6cc1d1; --c8: #9a9a93;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", "Helvetica Neue", sans-serif; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; }
header { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 20px; }
h1 { font-size: 22px; margin: 0; letter-spacing: .2px; }
.sub { color: var(--muted); font-size: 12px; margin-top: 4px; }
.controls { display: flex; flex-wrap: wrap; gap: 8px; }
.seg { display: inline-flex; background: var(--card); border: 1px solid var(--line); border-radius: 9px; padding: 3px; }
.seg button { border: 0; background: none; color: var(--muted); font: inherit; font-size: 13px; padding: 5px 11px; border-radius: 6px; cursor: pointer; }
.seg button.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
select { font: inherit; font-size: 13px; padding: 6px 10px; border-radius: 9px; border: 1px solid var(--line); background: var(--card); color: var(--ink); }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 16px; }
.kpi { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.kpi .v { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.kpi .l { color: var(--muted); font-size: 12px; }
.kpi .n { color: var(--muted); font-size: 11px; margin-top: 2px; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }
section h2 { font-size: 15px; margin: 0 0 4px; }
section .hint { color: var(--muted); font-size: 12px; margin: 0 0 12px; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.grid2 section { margin-bottom: 0; }
@media (max-width: 820px) { .grid2 { grid-template-columns: 1fr; } }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { padding: 7px 8px; text-align: right; white-space: nowrap; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; font-size: 12px; cursor: pointer; user-select: none; position: sticky; top: 0; background: var(--card); }
th.sorted::after { content: " ↓"; }
th.sorted.asc::after { content: " ↑"; }
th:first-child, td:first-child { text-align: left; }
td.title { max-width: 320px; overflow: hidden; text-overflow: ellipsis; }
tr.row { cursor: pointer; }
tr.row:hover td { background: var(--hover); }
tr.open td { background: var(--hover); }
tr.detail td { background: var(--bg); padding: 12px 14px; white-space: normal; text-align: left; }
.tag { display: inline-block; font-size: 11px; padding: 1px 7px; border-radius: 99px; background: var(--hover); color: var(--muted); margin-left: 6px; }
.muted { color: var(--muted); }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 3px; margin-right: 6px; vertical-align: 0; }
.bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden; background: var(--hover); margin: 6px 0 10px; }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--muted); }
.chart { width: 100%; height: 240px; }
.chart text { fill: var(--muted); font-size: 11px; }
.chart .grid { stroke: var(--line); }
.tip { position: fixed; pointer-events: none; background: var(--ink); color: var(--bg); font-size: 12px; padding: 7px 9px; border-radius: 7px; white-space: pre; opacity: 0; transition: opacity .1s; z-index: 9; }
.minis { position: sticky; left: 14px; max-width: calc(min(1180px, 100vw) - 110px); display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px; }
.minis h3 { font-size: 12px; color: var(--muted); font-weight: 500; margin: 0 0 4px; }
.minis table td, .minis table th { padding: 4px 6px; font-size: 12px; }
.pct { display: inline-block; min-width: 38px; }
.meter { display: inline-block; width: 60px; height: 6px; border-radius: 3px; background: var(--hover); vertical-align: middle; margin-left: 6px; overflow: hidden; }
.meter i { display: block; height: 100%; background: var(--accent); }
footer { color: var(--muted); font-size: 12px; line-height: 1.7; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Claude 用量明细</h1>
      <div class="sub" id="sub"></div>
    </div>
    <div class="controls">
      <div class="seg" id="range"></div>
      <select id="source"></select>
    </div>
  </header>

  <div class="kpis" id="kpis"></div>

  <section>
    <h2>每日用量</h2>
    <p class="hint" id="chartHint">按 API 价格折算，颜色区分模型；灰色是后台调用。把鼠标放到柱子上看明细。</p>
    <svg class="chart" id="chart"></svg>
    <div class="legend" id="chartLegend"></div>
  </section>

  <section>
    <h2>各窗口</h2>
    <p class="hint">一个窗口 = 桌面 App 里的一个对话（Code 标签页或 Cowork），终端里的会话按会话算。点一行展开明细，点表头排序。</p>
    <div class="scroll"><table id="winTable"></table></div>
  </section>

  <div class="grid2">
    <section>
      <h2>按模型</h2>
      <p class="hint">单价为每百万 token 的美元价（输入 / 输出 / 缓存写 / 缓存读）</p>
      <div class="scroll"><table id="modelTable"></table></div>
    </section>
    <section>
      <h2>按思考强度</h2>
      <p class="hint">思考 tokens 按输出价计费，已包含在输出里</p>
      <div class="scroll"><table id="effortTable"></table></div>
    </section>
  </div>
  <div style="height:16px"></div>

  <section>
    <h2>API 价构成</h2>
    <div class="bar" id="compBar"></div>
    <div class="scroll"><table id="compTable"></table></div>
  </section>

  <section>
    <h2>5 小时窗口</h2>
    <p class="hint">按本地记录推算的 5 小时额度窗口；「官方峰值」是这段时间里 Claude App 记到的最高百分比（App 没开时没有）。</p>
    <div class="scroll"><table id="fhTable"></table></div>
  </section>

  <section id="cxSec" style="display:none">
    <h2>Codex</h2>
    <p class="hint">OpenAI 没公布这些模型的 API 单价，这里只统计 tokens（输入含缓存命中，输出含推理）。子代理的用量算在发起它的线程里；走代理的第三方模型不占 Codex 额度，单独标出。</p>
    <div class="kpis" id="cxKpis"></div>
    <svg class="chart" id="cxChart"></svg>
    <div class="legend" id="cxLegend"></div>
    <div class="grid2" style="margin-top:14px">
      <div><h3 style="font-size:13px;margin:0 0 6px">按模型</h3><div class="scroll"><table id="cxModels"></table></div></div>
      <div><h3 style="font-size:13px;margin:0 0 6px">按思考强度</h3><div class="scroll"><table id="cxEfforts"></table></div></div>
    </div>
    <h3 style="font-size:13px;margin:16px 0 6px">各线程</h3>
    <div class="scroll"><table id="cxThreads"></table></div>
  </section>

  <footer id="foot"></footer>
</div>
<div class="tip" id="tip"></div>

<script>
const D = __DATA__;
const R = { ts:0, m:1, inp:2, out:3, cw:4, cr:5, think:6, w:7, e:8, bg:9, p:10, uIn:11, uOut:12, uCw:13, uCr:14 };
const COLORS = ["--c1","--c2","--c3","--c4","--c5","--c6","--c7","--c8"].map(v => getComputedStyle(document.documentElement).getPropertyValue(v).trim());
const BGCOLOR = getComputedStyle(document.documentElement).getPropertyValue("--bg-call").trim();
const DAY = 86400, WEEKDAYS = "日一二三四五六";

const usd = r => r[R.uIn] + r[R.uOut] + r[R.uCw] + r[R.uCr];
const isClaude = m => D.models[m].startsWith("claude-");
const short = m => D.models[m].replace("claude-", "").replace(/-\d{8}$/, "");
function fTok(n) { return n >= 1e9 ? (n/1e9).toFixed(2)+"B" : n >= 1e6 ? (n/1e6).toFixed(1)+"M" : n >= 1e3 ? Math.round(n/1e3)+"K" : String(Math.round(n)); }
function fUsd(x) { return x >= 100 ? "$" + Math.round(x).toLocaleString() : "$" + x.toFixed(2); }
function pad2(n) { return String(n).padStart(2, "0"); }
function fTime(t, withDay = true) { const d = new Date(t*1000); return (withDay ? `${pad2(d.getMonth()+1)}-${pad2(d.getDate())} ` : "") + `${pad2(d.getHours())}:${pad2(d.getMinutes())}`; }
function fDay(t) { const d = new Date(t*1000); return `${pad2(d.getMonth()+1)}-${pad2(d.getDate())} 周${WEEKDAYS[d.getDay()]}`; }
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) { if (k === "class") e.className = v; else if (k === "style") e.style.cssText = v; else e.setAttribute(k, v); }
  for (const k of kids) e.append(k instanceof Node ? k : document.createTextNode(k));
  return e;
}
function startOfDay(t) { const d = new Date(t*1000); d.setHours(0,0,0,0); return d.getTime()/1000; }

// 模型颜色：按总花费排序固定
const modelTotals = {};
D.rows.forEach(r => modelTotals[r[R.m]] = (modelTotals[r[R.m]] || 0) + usd(r));
const modelColor = {};
Object.keys(modelTotals).map(Number).filter(isClaude).sort((a, b) => modelTotals[b] - modelTotals[a]).forEach((m, i) => modelColor[m] = COLORS[i % COLORS.length]);

// ---------- 筛选 ----------
const now = D.generated;
const RANGES = [];
if (D.weekStart) RANGES.push(["week", "本周", D.weekStart]);
RANGES.push(["today", "今天", startOfDay(now)]);
RANGES.push(["7d", "近 7 天", startOfDay(now) - 6*DAY]);
RANGES.push(["30d", "近 30 天", startOfDay(now) - 29*DAY]);
{ const d = new Date(now*1000); d.setDate(1); d.setHours(0,0,0,0); RANGES.push(["month", "本月", d.getTime()/1000]); }
RANGES.push(["all", "全部", 0]);
let range = RANGES[0][0];
let source = "all";

const rangeBox = document.getElementById("range");
RANGES.forEach(([k, label]) => {
  const b = el("button", {}, label);
  b.onclick = () => { range = k; render(); };
  b.dataset.k = k;
  rangeBox.append(b);
});
const sources = [...new Set(D.wins.map(w => w[1]))];
const srcSel = document.getElementById("source");
srcSel.append(el("option", { value: "all" }, "全部来源"));
sources.forEach(s => srcSel.append(el("option", { value: s }, s)));
srcSel.onchange = () => { source = srcSel.value; render(); };

function agg() { return { inp:0, out:0, cw:0, cr:0, think:0, usd:0, bg:0, n:0, parts:[0,0,0,0], t0:Infinity, t1:0 }; }
function add(a, r) {
  a.inp += r[R.inp]; a.out += r[R.out]; a.cw += r[R.cw]; a.cr += r[R.cr]; a.think += r[R.think];
  const u = usd(r); a.usd += u; if (r[R.bg]) a.bg += u; else a.n++;
  a.parts[0] += r[R.uIn]; a.parts[1] += r[R.uOut]; a.parts[2] += r[R.uCw]; a.parts[3] += r[R.uCr];
  a.t0 = Math.min(a.t0, r[R.ts]); a.t1 = Math.max(a.t1, r[R.ts]);
}
const tok = a => a.inp + a.out + a.cw + a.cr;

// ---------- 渲染 ----------
const tip = document.getElementById("tip");
function showTip(ev, text) { tip.textContent = text; tip.style.opacity = 1; tip.style.left = Math.min(ev.clientX + 14, innerWidth - tip.offsetWidth - 8) + "px"; tip.style.top = (ev.clientY + 14) + "px"; }
function hideTip() { tip.style.opacity = 0; }

function render() {
  [...rangeBox.children].forEach(b => b.classList.toggle("on", b.dataset.k === range));
  const t0 = RANGES.find(x => x[0] === range)[2];
  const rows = D.rows.filter(r => r[R.ts] >= t0 && (source === "all" || D.wins[r[R.w]][1] === source));
  const claude = rows.filter(r => isClaude(r[R.m]));
  const other = rows.filter(r => !isClaude(r[R.m]));

  const all = agg(); claude.forEach(r => add(all, r));
  const wins = new Set(claude.map(r => r[R.w]));
  const days = Math.max(1, (now - (t0 || (claude[0] ? claude[0][R.ts] : now))) / DAY);

  // KPI
  const kpis = document.getElementById("kpis"); kpis.textContent = "";
  const lastPlan = D.plan.length ? D.plan[D.plan.length - 1] : null;
  const k = (v, l, n) => kpis.append(el("div", { class: "kpi" }, el("div", { class: "l" }, l), el("div", { class: "v" }, v), el("div", { class: "n" }, n || "")));
  k(fTok(tok(all)), "Tokens", `输入 ${fTok(all.inp)} · 输出 ${fTok(all.out)}`);
  k(fUsd(all.usd), "API 价折合", `日均 ${fUsd(all.usd / days)}`);
  k(fUsd(all.bg), "其中后台调用", all.usd ? `占 ${(all.bg / all.usd * 100).toFixed(0)}%，会话记录里没有` : "");
  k(String(all.n), "回复次数", `${wins.size} 个窗口`);
  k(fTok(all.think), "思考 tokens", all.out ? `占输出 ${(all.think / all.out * 100).toFixed(0)}%` : "");
  if (lastPlan) {
    const planFee = D.planUsd ? D.planUsd * 12 / 52 : null;
    k(`${lastPlan[2] ?? "–"}%`, `周额度${D.planName ? "（" + D.planName + "）" : ""}`,
      planFee && range === "week" ? `本周 API 价是周费的 ${(all.usd / planFee).toFixed(1)} 倍` : `5 小时 ${lastPlan[1] ?? "–"}%`);
  }

  drawChart(claude, t0);
  drawWindows(claude);
  drawModels(claude, all);
  drawEfforts(claude, all);
  drawComp(all);
  drawFiveHour(claude);
  drawCodex(t0);

  const oTok = other.reduce((s, r) => s + r[R.inp] + r[R.out] + r[R.cw] + r[R.cr], 0);
  document.getElementById("foot").textContent =
    (other.length ? `另有第三方模型（${[...new Set(other.map(r => D.models[r[R.m]]))].join("、")}）${fTok(oTok)} tokens，不占套餐额度、不计价。\n` : "") +
    "API 价按 Anthropic 官方单价折算，订阅套餐不按这个收费。后台调用 = Claude Code 每次运行结束时记录的费用汇总减去会话记录里的用量（标题生成、权限判断等），按运行期间的用量比例摊到各时段。";
  document.getElementById("sub").textContent = `生成于 ${fTime(now)} · 数据自 ${D.rows.length ? fDay(D.rows[0][R.ts]) : "–"}`;
}

function drawChart(rows, t0) {
  const svg = document.getElementById("chart"); svg.textContent = "";
  const hourly = (now - t0) <= 1.5 * DAY;
  const step = hourly ? 3600 : DAY;
  const start = hourly ? Math.floor(t0 / 3600) * 3600 : startOfDay(t0 || (rows[0] ? rows[0][R.ts] : now));
  const bucketOf = t => hourly ? Math.floor((t - start) / 3600) : Math.round((startOfDay(t) - start) / DAY);
  const n = Math.max(1, bucketOf(now) + 1);
  const buckets = Array.from({ length: n }, () => ({ total: 0, bg: 0, by: {}, tok: 0 }));
  rows.forEach(r => {
    const b = buckets[bucketOf(r[R.ts])]; if (!b) return;
    const u = usd(r); b.total += u; b.tok += r[R.inp] + r[R.out] + r[R.cw] + r[R.cr];
    if (r[R.bg]) b.bg += u; else b.by[r[R.m]] = (b.by[r[R.m]] || 0) + u;
  });
  document.getElementById("chartHint").firstChild && (document.getElementById("chartHint").textContent =
    `${hourly ? "每小时" : "每天"}按 API 价格折算，颜色区分模型；灰色是后台调用。把鼠标放到柱子上看明细。`);
  const W = svg.clientWidth || 1000, H = 240, L = 46, B = 24, T = 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const max = Math.max(...buckets.map(b => b.total), 0.01);
  const niceMax = (() => { const p = Math.pow(10, Math.floor(Math.log10(max))); return Math.ceil(max / p) * p; })();
  const ns = "http://www.w3.org/2000/svg";
  const mk = (tag, a) => { const e = document.createElementNS(ns, tag); for (const k in a) e.setAttribute(k, a[k]); svg.append(e); return e; };
  for (let i = 0; i <= 4; i++) {
    const y = T + (H - B - T) * (1 - i / 4);
    mk("line", { x1: L, x2: W, y1: y, y2: y, class: "grid" });
    const t = mk("text", { x: L - 6, y: y + 4, "text-anchor": "end" }); t.textContent = fUsd(niceMax * i / 4);
  }
  const bw = (W - L) / n, gap = Math.min(4, bw * 0.2);
  const labelEvery = Math.ceil(n / Math.floor((W - L) / 56));
  buckets.forEach((b, i) => {
    const x = L + i * bw + gap / 2, w = Math.max(1, bw - gap);
    let y = H - B;
    const segs = Object.entries(b.by).sort((a, c) => modelTotals[c[0]] - modelTotals[a[0]]).map(([m, u]) => [modelColor[m], u]);
    if (b.bg) segs.push([BGCOLOR, b.bg]);
    segs.forEach(([c, u]) => { const h = (u / niceMax) * (H - B - T); y -= h; mk("rect", { x, y, width: w, height: Math.max(0, h), fill: c, rx: 1.5 }); });
    const t = start + i * step;
    const hit = mk("rect", { x: L + i * bw, y: T, width: bw, height: H - B - T, fill: "transparent" });
    const lines = [hourly ? fTime(t) + " 起" : fDay(t), `合计 ${fUsd(b.total)} · ${fTok(b.tok)} tokens`];
    Object.entries(b.by).sort((a, c) => c[1] - a[1]).forEach(([m, u]) => lines.push(`${short(+m)}  ${fUsd(u)}`));
    if (b.bg) lines.push(`后台调用  ${fUsd(b.bg)}`);
    hit.addEventListener("mousemove", ev => showTip(ev, lines.join("\n")));
    hit.addEventListener("mouseleave", hideTip);
    if (i % labelEvery === 0) {
      const lbl = mk("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" });
      const d = new Date(t * 1000);
      lbl.textContent = hourly ? `${pad2(d.getHours())}:00` : `${d.getMonth() + 1}/${d.getDate()}`;
    }
  });
  const lg = document.getElementById("chartLegend"); lg.textContent = "";
  Object.keys(modelColor).map(Number).filter(m => rows.some(r => r[R.m] === m && !r[R.bg])).forEach(m =>
    lg.append(el("span", {}, el("i", { class: "dot", style: `background:${modelColor[m]}` }), short(m))));
  if (rows.some(r => r[R.bg])) lg.append(el("span", {}, el("i", { class: "dot", style: `background:${BGCOLOR}` }), "后台调用"));
}

// 通用可排序表格
function table(id, cols, data, opts = {}) {
  const t = document.getElementById(id); t.textContent = "";
  const state = t._sort || (t._sort = { col: opts.sort ?? cols.length - 1, asc: false });
  const head = el("tr");
  cols.forEach((c, i) => {
    const th = el("th", { class: i === state.col ? "sorted" + (state.asc ? " asc" : "") : "" }, c.label);
    th.onclick = () => { if (state.col === i) state.asc = !state.asc; else { state.col = i; state.asc = false; } table(id, cols, data, opts); };
    head.append(th);
  });
  t.append(el("thead", {}, head));
  const body = el("tbody");
  const key = cols[state.col].sort || cols[state.col].get;
  const sorted = [...data].sort((a, b) => { const x = key(a), y = key(b); return (x < y ? -1 : x > y ? 1 : 0) * (state.asc ? 1 : -1); });
  sorted.forEach(d => {
    const tr = el("tr", { class: opts.detail ? "row" : "" });
    cols.forEach(c => { const v = c.render ? c.render(d) : c.fmt ? c.fmt(c.get(d)) : c.get(d); const td = el("td", { class: c.cls || "" }); td.append(v instanceof Node ? v : String(v)); tr.append(td); });
    body.append(tr);
    if (opts.detail) {
      const dr = el("tr", { class: "detail", style: "display:none" }); const td = el("td", { colspan: cols.length }); dr.append(td);
      tr.onclick = () => { const open = dr.style.display === "none"; dr.style.display = open ? "" : "none"; tr.classList.toggle("open", open); if (open && !td.firstChild) td.append(opts.detail(d)); };
      body.append(dr);
    }
  });
  t.append(body);
}

function share(x, total) { const p = total ? x / total * 100 : 0; return el("span", {}, el("span", { class: "pct" }, p.toFixed(0) + "%"), el("span", { class: "meter" }, el("i", { style: `width:${Math.min(100, p)}%` }))); }

function drawWindows(rows) {
  const by = new Map();
  rows.forEach(r => { let w = by.get(r[R.w]); if (!w) by.set(r[R.w], w = { id: r[R.w], a: agg(), rows: [] }); add(w.a, r); w.rows.push(r); });
  const data = [...by.values()];
  const total = data.reduce((s, w) => s + w.a.usd, 0);
  table("winTable", [
    { label: "窗口", get: w => D.wins[w.id][0], render: w => el("span", {}, D.wins[w.id][0], el("span", { class: "tag" }, D.wins[w.id][1])), cls: "title" },
    { label: "时间", get: w => w.a.t1, render: w => `${fTime(w.a.t0)} – ${fTime(w.a.t1, new Date(w.a.t0*1000).toDateString() !== new Date(w.a.t1*1000).toDateString())}` },
    { label: "回复", get: w => w.a.n },
    { label: "输入", get: w => w.a.inp, fmt: fTok },
    { label: "输出", get: w => w.a.out, fmt: fTok },
    { label: "思考", get: w => w.a.think, fmt: fTok },
    { label: "缓存写", get: w => w.a.cw, fmt: fTok },
    { label: "缓存读", get: w => w.a.cr, fmt: fTok },
    { label: "后台", get: w => w.a.bg, fmt: fUsd },
    { label: "API 价", get: w => w.a.usd, fmt: fUsd },
    { label: "占比", get: w => w.a.usd, render: w => share(w.a.usd, total) },
  ], data, { sort: 9, detail: winDetail });
}

function miniTable(title, head, rows) {
  const t = el("table", {}, el("thead", {}, el("tr", {}, ...head.map(h => el("th", {}, h)))));
  const b = el("tbody"); rows.forEach(r => b.append(el("tr", {}, ...r.map(c => el("td", {}, c))))); t.append(b);
  return el("div", {}, el("h3", {}, title), t);
}
function groupBy(rows, key) { const m = new Map(); rows.forEach(r => { const k = key(r); if (!m.has(k)) m.set(k, agg()); add(m.get(k), r); }); return m; }

function winDetail(w) {
  const box = el("div", { class: "minis" });
  const byM = [...groupBy(w.rows, r => r[R.m])].sort((a, b) => b[1].usd - a[1].usd);
  box.append(miniTable("按模型", ["模型", "Tokens", "思考", "API 价"], byM.map(([m, a]) => [short(m), fTok(tok(a)), fTok(a.think), fUsd(a.usd)])));
  const byE = groupBy(w.rows, r => r[R.bg] ? "后台" : D.efforts[r[R.e]]);
  box.append(miniTable("按思考强度", ["强度", "回复", "Tokens", "API 价"],
    D.effortOrder.filter(e => byE.has(e)).map(e => { const a = byE.get(e); return [e, e === "后台" ? "–" : a.n, fTok(tok(a)), fUsd(a.usd)]; })));
  const byD = [...groupBy(w.rows, r => startOfDay(r[R.ts]))].sort((a, b) => b[0] - a[0]);
  box.append(miniTable("按天", ["日期", "回复", "Tokens", "API 价"], byD.map(([d, a]) => [fDay(d), a.n, fTok(tok(a)), fUsd(a.usd)])));
  const byP = [...groupBy(w.rows, r => D.projs[r[R.p]])].sort((a, b) => b[1].usd - a[1].usd);
  box.append(miniTable("工作目录", ["目录", "Tokens", "API 价"], byP.map(([p, a]) => [p, fTok(tok(a)), fUsd(a.usd)])));
  return box;
}

function drawModels(rows, all) {
  const data = [...groupBy(rows, r => r[R.m])].map(([m, a]) => ({ m, a }));
  table("modelTable", [
    { label: "模型", get: d => short(d.m), render: d => el("span", {}, el("i", { class: "dot", style: `background:${modelColor[d.m]}` }), short(d.m)) },
    { label: "单价", get: d => (D.prices[D.models[d.m]] || [0])[0], render: d => { const p = D.prices[D.models[d.m]]; return p ? `${p[0]} / ${p[1]} / ${p[2]} / ${p[4]}` : "–"; } },
    { label: "Tokens", get: d => tok(d.a), fmt: fTok },
    { label: "输出", get: d => d.a.out, fmt: fTok },
    { label: "API 价", get: d => d.a.usd, fmt: fUsd },
    { label: "占比", get: d => d.a.usd, render: d => share(d.a.usd, all.usd) },
  ], data, { sort: 4 });
}

function drawEfforts(rows, all) {
  const m = groupBy(rows, r => r[R.bg] ? "后台" : D.efforts[r[R.e]]);
  const data = D.effortOrder.filter(e => m.has(e)).map(e => ({ e, a: m.get(e) }));
  table("effortTable", [
    { label: "强度", get: d => D.effortOrder.indexOf(d.e), render: d => d.e },
    { label: "回复", get: d => d.e === "后台" ? -1 : d.a.n, render: d => d.e === "后台" ? "–" : d.a.n },
    { label: "Tokens", get: d => tok(d.a), fmt: fTok },
    { label: "思考", get: d => d.a.think, fmt: fTok },
    { label: "API 价", get: d => d.a.usd, fmt: fUsd },
    { label: "占比", get: d => d.a.usd, render: d => share(d.a.usd, all.usd) },
  ], data, { sort: 4 });
}

function drawComp(all) {
  const names = ["输入", "输出", "缓存写", "缓存读"], toks = [all.inp, all.out, all.cw, all.cr];
  const bar = document.getElementById("compBar"); bar.textContent = "";
  all.parts.forEach((u, i) => bar.append(el("span", { style: `width:${all.usd ? u / all.usd * 100 : 0}%;background:${COLORS[i]}`, title: `${names[i]} ${fUsd(u)}` })));
  table("compTable", [
    { label: "类型", get: d => d.i, render: d => el("span", {}, el("i", { class: "dot", style: `background:${COLORS[d.i]}` }), names[d.i]) },
    { label: "Tokens", get: d => toks[d.i], fmt: fTok },
    { label: "API 价", get: d => all.parts[d.i], fmt: fUsd },
    { label: "占比", get: d => all.parts[d.i], render: d => share(all.parts[d.i], all.usd) },
  ], [0, 1, 2, 3].map(i => ({ i })), { sort: 2 });
}

function drawFiveHour(rows) {
  const W5 = 5 * 3600, wins = [];
  [...rows].sort((a, b) => a[R.ts] - b[R.ts]).forEach(r => {
    const last = wins[wins.length - 1];
    if (!last || r[R.ts] >= last.start + W5) wins.push({ start: r[R.ts] - r[R.ts] % 600, a: agg() });
    add(wins[wins.length - 1].a, r);
  });
  wins.forEach(w => { const s = D.plan.filter(p => p[0] >= w.start && p[0] <= w.start + W5 && p[1] != null); w.peak = s.length ? Math.max(...s.map(p => p[1])) : null; });
  table("fhTable", [
    { label: "开始", get: w => w.start, render: w => `${fDay(w.start)} ${fTime(w.start, false)}` },
    { label: "结束", get: w => w.start + W5, render: w => fTime(w.start + W5, false) + (w.start + W5 > now ? "（进行中）" : "") },
    { label: "回复", get: w => w.a.n },
    { label: "Tokens", get: w => tok(w.a), fmt: fTok },
    { label: "后台", get: w => w.a.bg, fmt: fUsd },
    { label: "API 价", get: w => w.a.usd, fmt: fUsd },
    { label: "官方峰值", get: w => w.peak ?? -1, render: w => w.peak == null ? "–" : w.peak + "%" },
  ], wins, { sort: 0 });
}

// ---------- Codex ----------
function drawCodex(t0) {
  const X = D.cx, sec = document.getElementById("cxSec");
  if (!X) { sec.style.display = "none"; return; }
  sec.style.display = "";
  const C = { ts: 0, m: 1, inp: 2, cached: 3, out: 4, rsn: 5, e: 6, t: 7, own: 8 };
  const rows = X.rows.filter(r => r[C.ts] >= t0);
  const own = rows.filter(r => r[C.own]), other = rows.filter(r => !r[C.own]);
  const tk = r => r[C.inp] + r[C.out];
  const sum = (rs, f) => rs.reduce((s, r) => s + f(r), 0);
  const inp = sum(own, r => r[C.inp]), cached = sum(own, r => r[C.cached]), out = sum(own, r => r[C.out]), rsn = sum(own, r => r[C.rsn]);
  const kp = document.getElementById("cxKpis"); kp.textContent = "";
  const k = (v, l, n) => kp.append(el("div", { class: "kpi" }, el("div", { class: "l" }, l), el("div", { class: "v" }, v), el("div", { class: "n" }, n || "")));
  k(fTok(inp + out), "Tokens", `输入 ${fTok(inp)} · 输出 ${fTok(out)}`);
  k(own.length.toLocaleString(), "回复次数", `${new Set(own.map(r => r[C.t])).size} 个线程`);
  k(inp ? (cached / inp * 100).toFixed(0) + "%" : "–", "缓存命中", `命中 ${fTok(cached)}`);
  k(fTok(rsn), "推理 tokens", out ? `占输出 ${(rsn / out * 100).toFixed(0)}%` : "");
  X.quota.forEach(([n, u, t]) => k(`${u}%`, `Codex ${n}额度${X.plan ? "（" + X.plan + "）" : ""}`, `${fDay(t)} ${fTime(t, false)} 重置`));
  if (other.length) k(fTok(sum(other, tk)), "第三方模型", "走代理，不占 Codex 额度");

  // 每日 / 每小时 tokens，按模型堆叠
  const totals = {}; X.rows.forEach(r => totals[r[C.m]] = (totals[r[C.m]] || 0) + tk(r));
  const color = {}; Object.keys(totals).map(Number).sort((a, b) => totals[b] - totals[a]).forEach((m, i) => color[m] = COLORS[i % COLORS.length]);
  const svg = document.getElementById("cxChart"); svg.textContent = "";
  const hourly = (now - t0) <= 1.5 * DAY, step = hourly ? 3600 : DAY;
  const start = hourly ? Math.floor(t0 / 3600) * 3600 : startOfDay(t0 || (rows[0] ? rows[0][C.ts] : now));
  const bucketOf = t => hourly ? Math.floor((t - start) / 3600) : Math.round((startOfDay(t) - start) / DAY);
  const n = Math.max(1, bucketOf(now) + 1);
  const buckets = Array.from({ length: n }, () => ({ total: 0, by: {} }));
  rows.forEach(r => { const b = buckets[bucketOf(r[C.ts])]; if (!b) return; b.total += tk(r); b.by[r[C.m]] = (b.by[r[C.m]] || 0) + tk(r); });
  const W = svg.clientWidth || 1000, H = 240, L = 52, B = 24, T = 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const max = Math.max(...buckets.map(b => b.total), 1);
  const niceMax = (() => { const p = Math.pow(10, Math.floor(Math.log10(max))); return Math.ceil(max / p) * p; })();
  const ns = "http://www.w3.org/2000/svg";
  const mk = (tag, a) => { const e = document.createElementNS(ns, tag); for (const kk in a) e.setAttribute(kk, a[kk]); svg.append(e); return e; };
  for (let i = 0; i <= 4; i++) {
    const y = T + (H - B - T) * (1 - i / 4);
    mk("line", { x1: L, x2: W, y1: y, y2: y, class: "grid" });
    mk("text", { x: L - 6, y: y + 4, "text-anchor": "end" }).textContent = fTok(niceMax * i / 4);
  }
  const bw = (W - L) / n, gap = Math.min(4, bw * 0.2), labelEvery = Math.ceil(n / Math.floor((W - L) / 56));
  buckets.forEach((b, i) => {
    const x = L + i * bw + gap / 2, w = Math.max(1, bw - gap); let y = H - B;
    Object.entries(b.by).sort((a, c) => totals[c[0]] - totals[a[0]]).forEach(([m, v]) => { const h = v / niceMax * (H - B - T); y -= h; mk("rect", { x, y, width: w, height: Math.max(0, h), fill: color[m], rx: 1.5 }); });
    const t = start + i * step;
    const hit = mk("rect", { x: L + i * bw, y: T, width: bw, height: H - B - T, fill: "transparent" });
    const lines = [hourly ? fTime(t) + " 起" : fDay(t), `合计 ${fTok(b.total)} tokens`];
    Object.entries(b.by).sort((a, c) => c[1] - a[1]).forEach(([m, v]) => lines.push(`${X.models[m]}  ${fTok(v)}`));
    hit.addEventListener("mousemove", ev => showTip(ev, lines.join("\n"))); hit.addEventListener("mouseleave", hideTip);
    if (i % labelEvery === 0) { const d = new Date(t * 1000); mk("text", { x: L + i * bw + bw / 2, y: H - 6, "text-anchor": "middle" }).textContent = hourly ? `${pad2(d.getHours())}:00` : `${d.getMonth() + 1}/${d.getDate()}`; }
  });
  const lg = document.getElementById("cxLegend"); lg.textContent = "";
  Object.keys(color).map(Number).filter(m => rows.some(r => r[C.m] === m)).forEach(m => lg.append(el("span", {}, el("i", { class: "dot", style: `background:${color[m]}` }), X.models[m] + (rows.some(r => r[C.m] === m && !r[C.own]) ? "（第三方）" : ""))));

  const group = key => { const g = new Map(); rows.forEach(r => { const kk = key(r); if (!g.has(kk)) g.set(kk, []); g.get(kk).push(r); }); return [...g]; };
  const all = sum(rows, tk);
  table("cxModels", [
    { label: "模型", get: d => X.models[d[0]], render: d => el("span", {}, el("i", { class: "dot", style: `background:${color[d[0]]}` }), X.models[d[0]], d[1].some(r => !r[C.own]) ? el("span", { class: "tag" }, "第三方") : "") },
    { label: "回复", get: d => d[1].length },
    { label: "缓存命中", get: d => sum(d[1], r => r[C.cached]) / Math.max(1, sum(d[1], r => r[C.inp])), render: d => (sum(d[1], r => r[C.cached]) / Math.max(1, sum(d[1], r => r[C.inp])) * 100).toFixed(0) + "%" },
    { label: "输出", get: d => sum(d[1], r => r[C.out]), fmt: fTok },
    { label: "Tokens", get: d => sum(d[1], tk), fmt: fTok },
    { label: "占比", get: d => sum(d[1], tk), render: d => share(sum(d[1], tk), all) },
  ], group(r => r[C.m]), { sort: 4 });
  table("cxEfforts", [
    { label: "强度", get: d => X.effortOrder.indexOf(X.efforts[d[0]]), render: d => X.efforts[d[0]] },
    { label: "回复", get: d => d[1].length },
    { label: "推理", get: d => sum(d[1], r => r[C.rsn]), fmt: fTok },
    { label: "Tokens", get: d => sum(d[1], tk), fmt: fTok },
    { label: "占比", get: d => sum(d[1], tk), render: d => share(sum(d[1], tk), all) },
  ], group(r => r[C.e]), { sort: 3 });
  table("cxThreads", [
    { label: "线程", get: d => X.threads[d[0]], cls: "title" },
    { label: "时间", get: d => d[1][d[1].length - 1][C.ts], render: d => { const a = d[1][0][C.ts], b = d[1][d[1].length - 1][C.ts]; return `${fTime(a)} – ${fTime(b, new Date(a * 1000).toDateString() !== new Date(b * 1000).toDateString())}`; } },
    { label: "模型", get: d => [...new Set(d[1].map(r => X.models[r[C.m]]))].join("、") },
    { label: "回复", get: d => d[1].length },
    { label: "推理", get: d => sum(d[1], r => r[C.rsn]), fmt: fTok },
    { label: "Tokens", get: d => sum(d[1], tk), fmt: fTok },
    { label: "占比", get: d => sum(d[1], tk), render: d => share(sum(d[1], tk), all) },
  ], group(r => r[C.t]).map(([t, rs]) => [t, rs.sort((a, b) => a[C.ts] - b[C.ts])]), { sort: 5 });
}

render();
addEventListener("resize", () => { clearTimeout(window._rt); window._rt = setTimeout(render, 150); });
</script>
</body>
</html>
'''


if __name__ == "__main__":
    if "--reset" in sys.argv:
        backup_cache("reset")  # 不直接删：已被清理的旧会话只存在这份缓存里
    elif "--report" in sys.argv:
        write_report()
        if "--no-open" not in sys.argv:
            subprocess.run(["open", str(REPORT)])
    else:
        main()
