# -*- coding: utf-8 -*-
"""
保卫鱼娘 · 自动挂机助手（核心）
=================================
斗鱼直播间互动小游戏「保卫鱼娘」的自动化工具：
  自动开局 → 自动选技能（含跳过BOSS关的前置三选一）→ 自动领周宝箱 → 自动领结算宝箱 → 多场循环

原则（用户红线）：
  - 只做免费操作；任何涉及鱼翅/礼物（绝技/加速/技能刷新/银币兑换/充值）一律不自动点击
  - battle 进行中绝不刷新页面

数据抓包：
  - logs/api_<date>.jsonl     所有 /actgame/** 接口原始响应
  - logs/battles_<date>.jsonl 每场战报（起始关/结束关/用时/宝箱/技能选择）
"""

import os
import re
import sys
import json
import time
import base64
import random
import subprocess
import tempfile
import threading
import traceback
import datetime

APP_NAME = "保卫鱼娘挂机助手"
DEFAULT_RID = "12306"
GAME_URL = "https://www.douyu.com/pages/vibe-lab-act202606-game/mine?rid={rid}&isAnchorSide=0"
HOME_URL = "https://www.douyu.com/"

# 1x1 透明 GIF：用于「假装上报成功」地拦掉埋点请求
GIF_1PX = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")

# ---------------------------------------------------------------- 防封基础工具
# 驱动：优先 patchright（Playwright 反检测分支，API 完全兼容），不可用时降级 playwright。
# 为什么：原版 Playwright 启动时会开 Runtime.enable 并让 navigator.webdriver=true，
#         页面（含游戏风控 SDK）可直接读到；patchright 在驱动层抹掉这些特征。
def pick_driver():
    """返回 (驱动名, sync_playwright 可调用对象)"""
    try:
        from patchright.sync_api import sync_playwright as _sp
        return "patchright", _sp
    except Exception:
        from playwright.sync_api import sync_playwright as _sp
        return "playwright", _sp


DRIVER_NAME, sync_playwright = pick_driver()


def usleep(sec, ratio=0.25, lo=0.05):
    """带抖动的等待。阈值 lo 很重要：低于 50ms 的等待不动，避免破坏库内部时序。"""
    try:
        v = float(sec)
    except Exception:
        return
    if v <= 0:
        return
    time.sleep(max(lo, v + random.uniform(-ratio, ratio) * v))


def jitter(sec, lo=0.0, hi=0.0):
    """在 [lo, hi] 基础上抖动；用于轮询间隔"""
    return max(0.05, sec + random.uniform(lo, hi))


MAX_BATTLE_SECONDS = 13 * 60      # 单场最长等待（服务端上限 600s + 余量）
TICK = 0.5                         # 状态轮询间隔（实际用 jitter 抖动）
IDLE_STUCK_LIMIT = 90              # 空闲态卡住多久后尝试恢复（刷新页面）
PRE_BATTLE_LIMIT = 90              # 点挑战后"无进展"超时（每有进展会重新计时）
PRE_BATTLE_HARD_LIMIT = 300        # 点挑战后绝对上限（防止面板反复刷而无限等）


def log_msg(*a):
    s = " ".join(str(x) for x in a)
    try:
        if sys.stdout is not None:
            print(s, flush=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 路径
IS_FROZEN = getattr(sys, "frozen", False)
if IS_FROZEN:
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _writable(d):
    try:
        t = os.path.join(d, ".wtest")
        with open(t, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(t)
        return True
    except Exception:
        return False


if IS_FROZEN and not _writable(APP_DIR):
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", APP_DIR), "GuardFishBot")
else:
    DATA_DIR = APP_DIR

PROFILE = os.path.join(DATA_DIR, "chrome_profile")
LOGDIR = os.path.join(DATA_DIR, "logs")


def ensure_dirs():
    try:
        os.makedirs(LOGDIR, exist_ok=True)
    except Exception:
        pass
    try:
        os.makedirs(PROFILE, exist_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------- Chrome 发现
def _reg_app_path(name):
    try:
        import winreg
    except Exception:
        return None
    subs = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % name,)
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in subs:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    v = winreg.QueryValue(k, None)
                    if v and os.path.isfile(v):
                        return v
            except Exception:
                pass
    return None


def portable_marks(exe):
    """识别 Chrome++ / 绿色版特征（官方 Chrome / Edge 都不带）"""
    d = os.path.dirname(exe)
    m = []
    try:
        if os.path.isfile(os.path.join(d, "version.dll")):
            m.append("version.dll(Chrome++)")
        if os.path.isfile(os.path.join(d, "chrome++.ini")):
            m.append("chrome++.ini")
        parent = os.path.dirname(d)
        if os.path.isdir(os.path.join(parent, "Data")):
            m.append("../Data")
    except Exception:
        pass
    return m


def chrome_candidates():
    """返回 [(exe, rank, name)]，rank 越小越优先：0=官方Chrome 1=Edge 2=便携改装版"""
    found = {}

    def add(p):
        if p and os.path.isfile(p):
            a = os.path.abspath(p).lower()
            if a not in found:
                found[a] = p

    add(os.environ.get("CHROME_PATH"))
    add(_reg_app_path("chrome.exe"))
    add(_reg_app_path("msedge.exe"))
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    for base in (pf, pf86, lad):
        if not base:
            continue
        add(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        add(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    try:
        import shutil
        for n in ("chrome.exe", "msedge.exe"):
            w = shutil.which(n)
            add(w)
    except Exception:
        pass

    prio = os.environ.get("CHROME_PATH")
    lst = []
    for a, p in found.items():
        base = os.path.basename(p).lower()
        if "msedge" in base:
            rank, name = 1, "Microsoft Edge"
        elif "chrome" in base:
            marks = portable_marks(p)
            if marks:
                rank, name = 2, "便携版Chrome(%s)" % ",".join(marks)
            else:
                rank, name = 0, "Chrome"
        else:
            rank, name = 3, base
        if prio and a == os.path.abspath(prio).lower():
            rank = -1
        lst.append((p, rank, name))
    lst.sort(key=lambda x: (x[1], x[0]))
    return lst


def kill_stale_chrome(profile):
    """按 CommandLine 精确匹配 profile 路径杀残留 chrome（不误伤用户浏览器）"""
    try:
        ps1 = os.path.join(tempfile.gettempdir(), "kill_guardfish_chrome.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write("$p = Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                    "Where-Object { $_.CommandLine -like '*" + profile + "*' };\n"
                    "if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } }\n")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
                       capture_output=True, timeout=30)
    except Exception:
        pass


def clean_profile_locks(profile):
    for lock in ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile", "DevToolsActivePort"):
        fp = os.path.join(profile, lock)
        try:
            if os.path.exists(fp):
                os.remove(fp)
        except Exception:
            pass


# Chrome 启动参数。
# ⚠️ 不要加 --disable-gpu：本游戏是 PixiJS/WebGL 画面，禁用它会让 Chrome 退化为
# SwiftShader 软件渲染，1500x940@60fps 的战斗画面全部由 CPU 光栅化。
# 实测（2026-10-15，战斗中，浏览器进程树）：
#   带 --disable-gpu → 839% 单核（整机 52.4%），内存 1238→1802MB
#   硬件加速        → 82% 单核（整机 5.1%），内存 917→1165MB
# 即该项单独导致 CPU 高 10 倍。无显卡/驱动异常的环境由 launch_persistent 自动回退。
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
]
VIEWPORT = {"width": 1500, "height": 940}


def fix_headless_ua(ctx, headless):
    """headless 模式下把 UA 里的 HeadlessChrome 换成 Chrome。

    为什么：游戏侧风控 SDK 的 UA 正则显式匹配 `headlesschrome`，这是 headless 最致命的暴露点。
    为什么用 CDP：patchright 出于反检测目的封死了 add_init_script 与
      Page.addScriptToEvaluateOnNewDocument，只有 Emulation.setUserAgentOverride 可用，
      且它同时作用于 JS 层（navigator.userAgent）与 HTTP 请求头层。
    返回 (原始UA, 修正后UA 或 None, 错误信息或 None)
    """
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        ua = page.evaluate("() => navigator.userAgent") or ""
    except Exception as e:
        return None, None, "读取 UA 失败：%s" % str(e)[:100]
    if "HeadlessChrome" not in ua:
        return ua, ua, None
    fixed = ua.replace("HeadlessChrome", "Chrome")
    m = re.search(r"Chrome/(\d+)", fixed)
    major = m.group(1) if m else "140"
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Emulation.setUserAgentOverride", {
            "userAgent": fixed,
            "userAgentMetadata": {
                "brands": [
                    {"brand": "Not A;Brand", "version": "24"},
                    {"brand": "Chromium", "version": major},
                    {"brand": "Google Chrome", "version": major},
                ],
                "fullVersion": "%s.0.0.0" % major,
                "mobile": False,
                "platform": "Windows",
                "platformVersion": "15.0.0",
                "architecture": "x86",
                "bitness": "64",
                "model": "",
                "wow64": False,
            },
        })
        return ua, fixed, None
    except Exception as e:
        return ua, None, "setUserAgentOverride 失败：%s" % str(e)[:120]


def launch_persistent(pw, user_data_dir, headless=False):
    """逐候选启动持久化 Chrome；每个候选重试 2 次。返回 (ctx, used_path)

    GPU 回退：第一轮用硬件加速参数（CHROME_ARGS，不含 --disable-gpu）；
    若所有候选都起不来，再加 --disable-gpu 重试一轮，兼容无显卡/驱动异常的环境。
    headless 下额外用 CDP 修正 UA（见 fix_headless_ua）。
    """
    cands = chrome_candidates()
    if not cands:
        raise RuntimeError("未找到可用的 Chrome / Edge，请先安装 Google Chrome 或 Microsoft Edge")

    def _try(args):
        errs = []
        for exe, rank, name in cands:
            for attempt in (1, 2):
                try:
                    if attempt == 2:
                        kill_stale_chrome(user_data_dir)
                        clean_profile_locks(user_data_dir)
                        time.sleep(2)
                    ctx = pw.chromium.launch_persistent_context(
                        user_data_dir=user_data_dir, executable_path=exe, headless=headless,
                        viewport=VIEWPORT, args=args,
                        ignore_default_args=["--enable-automation"])
                    if ctx.pages:
                        page = ctx.pages[0]
                    else:
                        page = ctx.new_page()
                    try:
                        page.evaluate("1")
                    except Exception:
                        try:
                            ctx.close()
                        except Exception:
                            pass
                        raise RuntimeError("context 启动后不可用（可能被已有浏览器实例接管）")
                    # headless：UA 去 HeadlessChrome（覆盖 selftest / open_login / 挂机全部路径）
                    if headless:
                        ua_raw, ua_new, ua_err = fix_headless_ua(ctx, headless)
                        if ua_err:
                            log_msg("[防封] UA 修正失败：%s" % ua_err)
                        elif ua_new and ua_raw != ua_new:
                            log_msg("[防封] UA 已修正：HeadlessChrome → Chrome")
                    return ctx, exe
                except Exception as e:
                    errs.append("%s(%s) 第%d次: %s" % (name, os.path.basename(exe), attempt, str(e)[:120]))
                    time.sleep(1)
        return None, errs

    # 第一轮：硬件加速（默认，CPU 友好）
    ctx, res = _try(list(CHROME_ARGS))
    if ctx is not None:
        return ctx, res
    errors = list(res)

    # 第二轮：软件渲染兜底（无显卡/驱动异常的环境）
    ctx, res = _try(list(CHROME_ARGS) + ["--disable-gpu"])
    if ctx is not None:
        return ctx, res
    errors += list(res)
    raise RuntimeError("浏览器启动失败：\n" + "\n".join(errors[-6:]))


# ---------------------------------------------------------------- 注入脚本
# 伪装 React DevTools hook（必须在页面脚本前注入）——用于拿到 fiber root、定位战斗引擎实例
FAKE_DEVTOOLS_HOOK = r"""
(() => {
  try {
    let hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
    if (!hook) hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__ = {};
    if (!hook.renderers) hook.renderers = new Map();
    if (typeof hook.inject !== 'function') {
      let nextId = 1;
      const roots = {};
      hook.inject = function(internals) {
        const id = nextId++;
        try { hook.renderers.set(id, internals); } catch (e) {}
        return id;
      };
      hook.onCommitFiberRoot = function(id, root) { try { roots[id] = root; hook.__roots = roots; } catch (e) {} };
      hook.onCommitFiberUnmount = function() {};
      hook.onPostCommitFiberRoot = function() {};
      hook.checkDCE = false;
      hook.isDisabled = false;
      hook.supportsFiber = true;
    }
  } catch (e) {}
})()
"""

# 血条守护：定位 BattleManager（coreHp），血量低于阈值自动回满（客户端引擎层，免费）
JS_HP_WATCH = r"""
(() => {
  if (window.__GF) return 'exists';
  const GF = window.__GF = {enabled: true, hpGuard: true, threshold: 5, refills: 0, hpEvents: [], hp: null,
                            mgrRef: null, loopRef: null, wrapped: false, foundOnce: false,
                            fullHp: 0,
                            battleT0: 0, maxRefillMs: 0, capped: false,
                            oneHit: false, oneHitKills: 0, oneHitWrapped: false,
                            safe: false, killProb: 0.12,
                            scanCount: 0, scanInfo: null, diag: null, err: null, comp: ''};
  // 解析：ref.current 可能是 BattleLoop（直接含 coreHp）或 BattleManager（显示层，coreHp 在 .loop 里）
  function resolveTarget(c) {
    if (!c || typeof c !== 'object') return null;
    try {
      if (typeof c.coreHp === 'number' && c.config && c.config.battle && c.callbacks)
        return {loop: c, mgr: null, kind: 'loop'};
    } catch (e) {}
    try {
      const l = c.loop;
      if (l && typeof l === 'object' && typeof l.coreHp === 'number' && l.config && l.config.battle)
        return {loop: l, mgr: c, kind: 'manager'};
    } catch (e) {}
    try {
      const l = c.loop;
      if (l && typeof l === 'object' && (l.config || l.callbacks) && Array.isArray(c.towers))
        return {loop: l, mgr: c, kind: 'manager-pending'};
    } catch (e) {}
    return null;
  }
  function scanTree() {
    GF.scanCount++;
    const res = {fibers: 0, refs: 0, best: 0, comp: '', err: null};
    try {
      const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || {};
      const roots = hook.__roots || {};
      const starts = [];
      for (const id of Object.keys(roots)) { const fr = roots[id]; if (fr && fr.current) starts.push(fr.current); }
      if (!starts.length) {
        // DOM 兜底：DevTools hook 在 patchright 下装不进 document-start，拿不到 roots，
        // 只能从元素上的 __reactFiber$ 反向回溯到根。
        const FK = ['__reactFiber$', '__reactInternalInstance$'];
        const keyOf = el => { try { return Object.keys(el).find(k => FK.some(p => k.startsWith(p))) || null; } catch (e) { return null; } };
        let el = document.querySelector('[class*="GameHUD-module_container"], [class*="CardLanes"], canvas');
        let k = el ? keyOf(el) : null;
        if (!k) {
          // 全页扫第一个带 fiber 的元素（主世界才看得到这个 expando）
          try {
            for (const e of document.querySelectorAll('*')) {
              const kk = keyOf(e);
              if (kk) { el = e; k = kk; break; }
            }
          } catch (e) {}
        }
        if (el && k) { let f = el[k]; while (f && f.return) f = f.return; if (f) starts.push(f); }
      }
      let best = null, bestScore = 0, bestName = '', bestKind = '';
      const seen = new Set();
      const queue = [...starts];
      // 索引游标而非 queue.shift()：大数组 shift() 是 O(n)，整棵树最坏 O(n^2)
      let qi = 0;
      let visited = 0;
      while (qi < queue.length && visited < 60000) {
        const f = queue[qi++]; visited++;
        if (!f || seen.has(f)) continue;
        seen.add(f);
        res.fibers++;
        try {
          let h = f.memoizedState, hc = 0;
          while (h && hc < 4000) {
            try {
              const v = h.memoizedState;
              if (v && typeof v === 'object' && 'current' in v) {
                res.refs++;
                const t = resolveTarget(v.current);
                if (t) {
                  const sc = t.kind === 'loop' ? 10 : (t.kind === 'manager' ? 9 : 5);
                  if (sc > bestScore) {
                    bestScore = sc; best = v; bestKind = t.kind;
                    bestName = (f.type && (f.type.name || f.type.displayName)) || 'fn';
                  }
                }
              }
            } catch (e) {}
            h = h.next; hc++;
          }
        } catch (e) {}
        if (f.child) queue.push(f.child);
        if (f.sibling) queue.push(f.sibling);
      }
      res.best = bestScore; res.comp = bestName;
      if (best) {
        GF.mgrRef = best; GF.foundOnce = true; GF.comp = bestName;
        const t = resolveTarget(best.current);
        GF.loopRef = t ? t.loop : null;
        try { GF.diag = {kind: bestKind, comp: bestName, coreHp: t ? t.loop.coreHp : null}; } catch (e) {}
      }
    } catch (e) { res.err = String(e); }
    GF.scanInfo = res;
    return res;
  }
  window.__gfScan = scanTree;
  function refill(loop, tag) {
    try {
      if (!GF.hpGuard) return;
      if (!loop || typeof loop.coreHp !== 'number') return;
      if (loop.running === false) return;
      // 回血时间上限：默认 0 = 不限（跟随"单场不限时"）。设 >0 时到点停手让战斗自然结束。
      if (GF.maxRefillMs > 0 && GF.battleT0 && (Date.now() - GF.battleT0) > GF.maxRefillMs) {
        if (!GF.capped) {
          GF.capped = true;
          GF.hpEvents.push({t: Date.now(), type: 'cap', mins: Math.round(GF.maxRefillMs / 60000)});
        }
        return;
      }
      if (loop.coreHp < GF.threshold) {
        // 「满血」目标 = 本次战斗见过的基础血量最大值（至少 20），
        // 不用当前关继承来的低血量（否则跨关后会只回到低值甚至原地打转）
        const stageMax = (loop.config && loop.config.battle && loop.config.battle.baseCoreHp) || 20;
        if (stageMax > (GF.fullHp || 0)) GF.fullHp = stageMax;
        let target = Math.max(GF.fullHp || 0, 20);
        // 低风险模式：不死也保持不住——偶尔不回血、且不总回满，
        // 让战斗存在真实死亡的可能（真实玩家的基地是会爆的）
        if (GF.safe) {
          if (Math.random() < 0.22) {
            GF.hpEvents.push({t: Date.now(), type: 'skip', hp: loop.coreHp});
            return;
          }
          const lo = Math.max(6, Math.round(target * 0.55));
          target = lo + Math.floor(Math.random() * (target - lo + 1));
        }
        if (target <= loop.coreHp) return;   // 防呆：无法提升就不再尝试
        const before = loop.coreHp;
        loop.coreHp = target;
        if (loop.callbacks && typeof loop.callbacks.onCoreHpChanged === 'function') loop.callbacks.onCoreHpChanged(target, target);
        GF.refills++;
        GF.hpEvents.push({t: Date.now(), type: 'refill', from: before, to: target, tag: tag});
      }
    } catch (e) { GF.err = String(e); }
  }
  window.__gfRefill = refill;
  // 伤害包装：抽成独立函数，既供 tick 调用，也方便单测（window.__gfWrapDamage）
  function wrapDamage(loop) {
    if (!loop || loop.__gfOneHitWrapped) return false;
    const proto2 = Object.getPrototypeOf(loop);
    const orig2 = loop._dealDamage || (proto2 && proto2._dealDamage);
    if (typeof orig2 !== 'function') return false;
    loop._dealDamage = function (enemy, damage, cardId, isCrit) {
      try {
        if (enemy && typeof enemy.currentHp === 'number' && enemy.currentHp > 0) {
          if (GF.safe) {
            const p = (GF.killProb === undefined) ? 0.12 : GF.killProb;
            if (Math.random() < p) {
              GF.oneHitKills++;
              return orig2.call(this, enemy, enemy.currentHp, cardId, isCrit);
            }
            const r = 0.55 + Math.random() * 0.35;
            const dmg = Math.max(1, Math.floor(enemy.currentHp * r));
            return orig2.call(this, enemy, dmg, cardId, isCrit);
          }
          if (GF.oneHit) {
            GF.oneHitKills++;
            return orig2.call(this, enemy, enemy.currentHp, cardId, isCrit);
          }
        }
      } catch (e) { GF.err = String(e); }
      return orig2.call(this, enemy, damage, cardId, isCrit);
    };
    loop.__gfOneHitWrapped = true;
    return true;
  }
  window.__gfWrapDamage = wrapDamage;
  let lastHp = null;
  let lastScan = 0;
  setInterval(() => {
    try {
      // 配置从 localStorage 读（跨 JS 世界共享）：patchright 的 evaluate 在隔离世界，
      // 直接改 window.__GF 对主世界无效，因此必须走这条通道。
      try {
        const raw = localStorage.getItem('gf_cfg');
        if (raw) {
          const c = JSON.parse(raw);
          GF.hpGuard = !!c.hpGuard;
          GF.oneHit = !!c.oneHit;
          GF.threshold = Number(c.threshold) || 5;
          // 低风险模式的两个开关必须在这里落地——漏读会让 GF.safe 恒为 undefined，
          // 概率分支被跳过、每次都走秒杀（v1.0.14/15 的实际故障）。
          GF.safe = !!c.safe;
          GF.killProb = Number(c.killProb);
          if (!isFinite(GF.killProb) || GF.killProb < 0 || GF.killProb > 1) GF.killProb = 0.12;
          if (c.refillCapMs !== undefined) GF.maxRefillMs = Number(c.refillCapMs) || 0;
          GF.enabled = !!(GF.hpGuard || GF.oneHit || GF.safe);
        }
      } catch (e) {}
      // 一次性命令通道（开局打点 / 清零计数）
      try {
        const cmd = localStorage.getItem('gf_cmd');
        if (cmd) {
          localStorage.removeItem('gf_cmd');
          const c = JSON.parse(cmd);
          if (c.battleT0) { GF.battleT0 = c.battleT0; GF.capped = false; }
          if (c.resetKills) { GF.oneHitKills = 0; }
        }
      } catch (e) {}
      // 状态回传给 Python（同样走 localStorage）
      try {
        if (!GF._pubT || Date.now() - GF._pubT > 400) {
          GF._pubT = Date.now();
          if (GF.hpEvents.length > 400) GF.hpEvents.splice(0, GF.hpEvents.length - 400);
          const base = Math.max(0, GF.hpEvents.length - 200);
          localStorage.setItem('gf_status', JSON.stringify({
            refills: GF.refills || 0, wrapped: !!GF.wrapped, found: !!GF.mgrRef,
            comp: GF.comp || '', hp: GF.hp,
            oneHit: !!GF.oneHit, oneHitWrapped: !!GF.oneHitWrapped,
            oneHitKills: GF.oneHitKills || 0,
            safe: !!GF.safe, killProb: GF.killProb,
            scanCount: GF.scanCount || 0, scanInfo: GF.scanInfo, err: GF.err,
            evTotal: GF.hpEvents.length, evBase: base,
            events: GF.hpEvents.slice(base)
          }));
        }
      } catch (e) {}
      if (!GF.enabled) return;
      let cur = GF.mgrRef && GF.mgrRef.current;
      let t = resolveTarget(cur);
      if (!t || typeof t.loop.coreHp !== 'number') {
        const now = Date.now();
        if (now - lastScan > 2000) { lastScan = now; scanTree(); }
        cur = GF.mgrRef && GF.mgrRef.current;
        t = resolveTarget(cur);
      }
      if (!t || typeof t.loop.coreHp !== 'number') return;
      const loop = t.loop;
      GF.loopRef = loop;
      if (!GF.battleT0) { GF.battleT0 = Date.now(); GF.capped = false; }
      if (!loop.__gfWrapped) {
        try {
          const proto = Object.getPrototypeOf(loop);
          const fn = loop._checkEnemiesReachedCore || (proto && proto._checkEnemiesReachedCore);
          if (typeof fn === 'function') {
            const orig = fn.bind(loop);
            loop._checkEnemiesReachedCore = function () {
              try { if (this.coreHp <= GF.threshold) refill(this, 'pre'); } catch (e) {}
              const r = orig();
              try { refill(this, 'post'); } catch (e) {}
              return r;
            };
            loop.__gfWrapped = true; GF.wrapped = true;
          }
        } catch (e) { GF.err = String(e); }
      }
      // 秒杀/伤害弱化：包装 _dealDamage。
      // 原实现 actualDmg = min(max(1,floor(damage)), enemy.currentHp)，
      // 因此传 currentHp 必然打成 0 → 走原生的 _killEnemy（伤害统计/击杀回调/掉落/清关全保留）。
      // 低风险模式下只有 killProb 概率这样打，其余只打 55%~90%，需要 2~3 次命中。
      if (!loop.__gfOneHitWrapped) {
        try {
          if (wrapDamage(loop)) { GF.oneHitWrapped = true; }
        } catch (e) { GF.err = String(e); }
      }
      const hp = loop.coreHp;
      const stageMax2 = (loop.config && loop.config.battle && loop.config.battle.baseCoreHp) || 20;
      if (stageMax2 > (GF.fullHp || 0)) GF.fullHp = stageMax2;
      const maxShown = Math.max(GF.fullHp || 0, 20);
      GF.hp = {cur: hp, max: maxShown, t: Date.now(), running: !!loop.running};
      if (lastHp !== null && hp < lastHp) GF.hpEvents.push({t: Date.now(), type: 'damage', from: lastHp, to: hp});
      lastHp = hp;
      refill(loop, 'watch');
    } catch (e) { GF.err = String(e); }
  }, 250);
  return 'installed';
})()
"""

JS_HP_CFG = r"""
(cfg) => {
  if (!window.__GF) return false;
  if (cfg && cfg.enabled !== undefined) window.__GF.hpGuard = !!cfg.enabled;   // 兼容旧参数名
  if (cfg && cfg.hpGuard !== undefined) window.__GF.hpGuard = !!cfg.hpGuard;
  if (cfg && cfg.threshold !== undefined) window.__GF.threshold = Number(cfg.threshold) || 5;
  if (cfg && cfg.oneHit !== undefined) window.__GF.oneHit = !!cfg.oneHit;
  // 守护循环（250ms）只要「回血」或「秒杀」有一个开着就要跑
  window.__GF.enabled = !!(window.__GF.hpGuard || window.__GF.oneHit);
  return {hpGuard: window.__GF.hpGuard, enabled: window.__GF.enabled,
          threshold: window.__GF.threshold, oneHit: window.__GF.oneHit};
}
"""

JS_HP_DRAIN = r"""
() => {
  const gf = window.__GF;
  if (!gf) return null;
  const ev = gf.hpEvents ? gf.hpEvents.splice(0, 200) : [];
  return {refills: gf.refills || 0, wrapped: !!gf.wrapped, found: !!gf.mgrRef,
          comp: gf.comp || '', hp: gf.hp,
          oneHit: !!gf.oneHit, oneHitWrapped: !!gf.oneHitWrapped,
          oneHitKills: gf.oneHitKills || 0,
          scanInfo: gf.scanInfo, err: gf.err, events: ev};
}
"""

# 主世界代理注入器：patchright 的 evaluate 跑在隔离世界，无法直接装主世界的 hook，
# 而内联脚本（运行时插入）可以执行。所以用「插一个 <script> 标签」把 JS_HP_WATCH
# 装进主世界，之后配置/状态通过 localStorage 双向传递（localStorage 跨世界共享）。
JS_GF_BOOT = r"""
(agentSrc) => {
  if (window.__GF) return 'exists';
  try {
    const s = document.createElement('script');
    s.textContent = agentSrc;
    (document.head || document.documentElement).appendChild(s);
    s.remove();
    return window.__GF ? 'injected' : 'tried';
  } catch (e) {
    return 'err:' + String(e).slice(0, 80);
  }
}
"""

# 通用主世界执行通道：把任意脚本源码以运行时 <script> 插进页面主世界。
# （patchright 的 evaluate 在隔离世界，装指纹补丁必须走这条路）
JS_MAIN_EVAL = r"""
(src) => {
  try {
    const s = document.createElement('script');
    s.textContent = src;
    (document.head || document.documentElement).appendChild(s);
    s.remove();
    return 'ok';
  } catch (e) {
    return 'err:' + String(e).slice(0, 80);
  }
}
"""

# 指纹补丁（主世界）。
# 设计原则：**只在异常时才改**——真 Chrome 窗口模式下所有检测项都正常，
# 这段脚本全程 no-op，不会给正常环境引入任何"被改过"的痕迹。
# 只处理 headless / 软件渲染下必然异常、且真实浏览器绝不会出现的项：
#   1. window.chrome 缺失
#   2. navigator.plugins 为空（headless 经典特征）
#   3. hardwareConcurrency / deviceMemory 为 0
#   4. WebGL UNMASKED_RENDERER 是 SwiftShader/llvmpipe（软件渲染）
#   5. Notification.permission 恒为 denied
JS_FP_HARDEN = r"""
(() => {
  try {
    if (window.__GF_FP) return;
    const done = [];
    const mask = (fn, name) => {
      try {
        const orig = Function.prototype.toString;
        if (!Function.prototype.__gfMasked) {
          const table = new WeakMap();
          Function.prototype.toString = function () {
            const n = table.get(this);
            if (n) return 'function ' + n + '() { [native code] }';
            return orig.call(this);
          };
          Object.defineProperty(Function.prototype, '__gfMasked', {value: table});
        }
        Function.prototype.__gfMasked.set(fn, name);
      } catch (e) {}
    };

    // 1) window.chrome
    try {
      if (!window.chrome) {
        window.chrome = {
          app: {isInstalled: false, InstallState: {DISABLED: 'disabled'},
                RunningState: {RUNNING: 'running'}},
          runtime: {}, csi: function () {}, loadTimes: function () {}
        };
        done.push('chrome');
      }
    } catch (e) {}

    // 2) navigator.plugins 为空
    try {
      if (!navigator.plugins || navigator.plugins.length === 0) {
        const mkMime = () => {
          const m = Object.create(MimeType.prototype);
          Object.defineProperty(m, 'type', {value: 'application/pdf'});
          Object.defineProperty(m, 'suffixes', {value: 'pdf'});
          Object.defineProperty(m, 'description', {value: 'Portable Document Format'});
          return m;
        };
        const mk = (name) => {
          const p = Object.create(Plugin.prototype);
          Object.defineProperty(p, 'name', {value: name});
          Object.defineProperty(p, 'description', {value: 'Portable Document Format'});
          Object.defineProperty(p, 'filename', {value: 'internal-pdf-viewer'});
          Object.defineProperty(p, 'length', {value: 1});
          const m = mkMime();
          Object.defineProperty(m, 'enabledPlugin', {value: p});
          Object.defineProperty(p, '0', {value: m});
          return p;
        };
        const list = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer',
                      'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'].map(mk);
        const arr = Object.create(PluginArray.prototype);
        list.forEach((p, i) => Object.defineProperty(arr, i, {value: p}));
        Object.defineProperty(arr, 'length', {value: list.length});
        Object.defineProperty(Navigator.prototype, 'plugins', {
          configurable: true, get: function () { return arr; }
        });
        mask(Object.getOwnPropertyDescriptor(Navigator.prototype, 'plugins').get, 'get plugins');
        done.push('plugins');
      }
    } catch (e) {}

    // 3) CPU / 内存为 0
    try {
      if (!navigator.hardwareConcurrency) {
        Object.defineProperty(Navigator.prototype, 'hardwareConcurrency', {
          configurable: true, get: function () { return 8; }
        });
        mask(Object.getOwnPropertyDescriptor(Navigator.prototype, 'hardwareConcurrency').get,
             'get hardwareConcurrency');
        done.push('cores');
      }
      if (!navigator.deviceMemory) {
        Object.defineProperty(Navigator.prototype, 'deviceMemory', {
          configurable: true, get: function () { return 8; }
        });
        mask(Object.getOwnPropertyDescriptor(Navigator.prototype, 'deviceMemory').get,
             'get deviceMemory');
        done.push('mem');
      }
    } catch (e) {}

    // 4) WebGL 软件渲染
    //    先探测当前 renderer；不是软件渲染就完全不包装 getParameter，
    //    保证真机（有独显）路径 100% 零改动。
    try {
      const probe = () => {
        const c = document.createElement('canvas');
        const g = c.getContext('webgl') || c.getContext('experimental-webgl');
        if (!g) return null;
        const d = g.getExtension('WEBGL_debug_renderer_info');
        if (!d) return null;
        return {vendor: g.getParameter(d.UNMASKED_VENDOR_WEBGL),
                renderer: g.getParameter(d.UNMASKED_RENDERER_WEBGL)};
      };
      const cur = probe();
      const soft = cur && typeof cur.renderer === 'string' &&
                   /swiftshader|llvmpipe|software|basic render/i.test(cur.renderer);
      if (soft) {
        const patch = (proto) => {
          if (!proto || proto.__gfGL) return;
          const orig = proto.getParameter;
          const wrapped = function (p) {
            if (p === 37446) {
              const v = orig.apply(this, [p]);
              if (typeof v === 'string' && /swiftshader|llvmpipe|software|basic render/i.test(v)) {
                return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)';
              }
              return v;
            }
            if (p === 37445) {
              const v = orig.apply(this, [p]);
              if (typeof v === 'string' && /google/i.test(v)) return 'Google Inc. (NVIDIA)';
              return v;
            }
            return orig.apply(this, [p]);
          };
          proto.getParameter = wrapped;
          mask(wrapped, 'getParameter');
          Object.defineProperty(proto, '__gfGL', {value: 1});
        };
        patch(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
        patch(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
        done.push('webgl:' + String(cur.renderer).slice(0, 30));
      }
    } catch (e) {}

    // 5) 通知权限恒为 denied（headless）
    try {
      if (window.Notification && Notification.permission === 'denied') {
        Object.defineProperty(Notification, 'permission', {
          configurable: true, get: function () { return 'default'; }
        });
        done.push('notify');
      }
    } catch (e) {}

    window.__GF_FP = {patched: done};
    // 主世界的 window 变量在隔离世界里读不到，结果用 localStorage 回传给 Python
    try { localStorage.setItem('gf_fp', JSON.stringify(done)); } catch (e) {}
  } catch (e) {
    try { localStorage.setItem('gf_fp', JSON.stringify(['error:' + String(e).slice(0, 60)])); } catch (e2) {}
  }
})();
"""

# 从主世界代理读状态（localStorage 共享）
JS_GF_STATUS = r"""
() => {
  try {
    const raw = localStorage.getItem('gf_status');
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) { return null; }
}
"""

# 写配置到主世界代理
JS_GF_CFG = r"""
(cfg) => {
  try {
    localStorage.setItem('gf_cfg', JSON.stringify(cfg));
    return cfg;
  } catch (e) { return null; }
}
"""

# 一次性命令（开局时间打点 / 清零秒杀计数）
JS_GF_CMD = r"""
(cmd) => {
  try {
    localStorage.setItem('gf_cmd', JSON.stringify(cmd));
    return true;
  } catch (e) { return false; }
}
"""

JS_HOOK = r"""
(() => {
  if (window.__WM) return;
  const W = window.__WM = {panelInfo:null, battleInfo:null, stage:null, maxStage:0,
                          freeNum:null, battleSkill:null, battleOver:false, last:null, respCount:0};
  const pick = (url, j) => {
    try {
      const d = j && j.data || {};
      W.last = {url: url, t: Date.now(), body: j};
      W.respCount++;
      if (url.indexOf('basics/panelInfo') >= 0) W.panelInfo = j;
      if (url.indexOf('game/battleInfo') >= 0) W.battleInfo = j;
      if (d.userInfo && d.userInfo.freeNum != null) W.freeNum = d.userInfo.freeNum;
      if (d.battleInfo && d.battleInfo.curStage != null) {
        W.stage = {curStage: d.battleInfo.curStage, curHp: d.battleInfo.baseHp, t: Date.now()};
        W.maxStage = Math.max(W.maxStage || 0, d.battleInfo.curStage);
      }
      if (d.stageInfo) {
        W.stage = {curStage: d.stageInfo.curStage, curHp: d.stageInfo.curHp, t: Date.now()};
        W.maxStage = Math.max(W.maxStage || 0, d.stageInfo.curStage);
      }
      if (d.battleSkill) W.battleSkill = d.battleSkill;
      if (d.status === 20) W.battleOver = true;
      if (d.status === 10) W.battleOver = false;
    } catch (e) {}
  };
  const of = window.fetch;
  window.fetch = function() {
    const args = arguments;
    return of.apply(this, args).then(resp => {
      try {
        const u = (typeof args[0] === 'string') ? args[0] : ((args[0] && args[0].url) || '');
        if (u.indexOf('/actgame/') >= 0) { resp.clone().json().then(j => pick(u, j)).catch(()=>{}); }
      } catch (e) {}
      return resp;
    });
  };
  const oo = XMLHttpRequest.prototype.open;
  const osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m, u) { this.__u = u; return oo.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function() {
    this.addEventListener('load', () => {
      try {
        if ((this.__u||'').indexOf('/actgame/') >= 0) pick(this.__u, JSON.parse(this.responseText));
      } catch (e) {}
    });
    return osend.apply(this, arguments);
  };
})()
"""

JS_STATE = r"""
(() => {
  const q = s => document.querySelector(s);
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden'; };
  const out = { t: Date.now() };

  out.inBattle = vis(q('[class*="GameHUD-module_container"]'));

  // 技能三选一面板（战斗中 SkillPanel / 开场后 Starter）
  const sp = q('[class*="SkillPanel-module_panel"]');
  const stp = q('[class*="Starter-module_skillsPanel"]');
  if (vis(sp) || vis(stp)) {
    const root = vis(sp) ? sp : stp;
    const cards = [...root.querySelectorAll('[class*="SkillPanel-module_skill-"], [class*="Starter-module_skill-"]')].filter(vis);
    out.skillPanel = cards.map((c, i) => ({ i: i, txt: (c.innerText || '').trim().replace(/\n/g, ' | ').slice(0, 160) }));
    out.skillPanelKind = vis(sp) ? 'stage' : 'starter';
  }

  // 已选技能确认列表（带开始按钮）
  const fl = q('[class*="Starter-module_skillList"]');
  if (vis(fl)) out.finalSkillList = (fl.innerText || '').trim().replace(/\n/g, ' | ').slice(0, 200);
  const la = q('[class*="Starter-module_limitAlter"]');
  if (vis(la)) out.limitAlter = true;

  out.confirmDlg = vis(q('[class*="finalSkillPayAlter"]'));   // 绝技付费确认（不会点，做哨兵）
  out.result = vis(q('[class*="Result-module_modal"]'));
  out.treasure = vis(q('[class*="TreasureResult-module_modal"]'));
  out.speedDlg = vis(q('[class*="GameHUD-module_payAlter"]')); // 加速付费弹窗（哨兵）

  const cb = q('[class*="challengeButton"]');
  out.canChallenge = vis(cb);
  if (vis(cb)) {
    out.challengeTxt = (cb.innerText || '').replace(/\n/g, ' ').trim().slice(0, 60);
    // 按钮可能在长页面底部，视口外时 JS click() 不生效（真实 bug，见需求文档第五节）
    try {
      const r = cb.getBoundingClientRect();
      const cy = r.top + r.height / 2;
      out.challengeOffscreen = (cy > window.innerHeight || cy < 0);
      out.challengeRect = {x: r.left + r.width / 2 + window.scrollX,
                           y: r.top + r.height / 2 + window.scrollY};
    } catch (e) { out.challengeOffscreen = false; }
  }

  // 每周宝箱「领取」（战斗宝箱领取按钮：固定 id js-target-treasure）
  if (out.inBattle) {
    const b = document.querySelector('#js-target-treasure button');
    let claimable = false;
    if (b) {
      const r = b.getBoundingClientRect();
      claimable = r.width > 1 && r.height > 1;
    }
    out.hudClaimable = claimable;
  }

  const w = window.__WM || {};
  out.wm = {respCount: w.respCount || 0, freeNum: w.freeNum, maxStage: w.maxStage || 0,
            panelReady: !!w.panelInfo,
            stage: w.stage, battleOver: !!w.battleOver,
            nick: (w.panelInfo && w.panelInfo.data && w.panelInfo.data.userInfo && w.panelInfo.data.userInfo.nn) || '',
            weekRank: (w.panelInfo && w.panelInfo.data && w.panelInfo.data.siteUserInfo && w.panelInfo.data.siteUserInfo.siteWeekRank) || 0,
            coin: (() => {
              try {
                const s = w.panelInfo && w.panelInfo.data && w.panelInfo.data.siteUserInfo;
                if (s && typeof s.coin === 'number') return s.coin;
              } catch (e) {}
              return null;
            })(),
            panelCode: (w.panelInfo && w.panelInfo.code) || 0};
  try {
    const ct = document.querySelector('[class*="TopBar-module_coinText"]');
    out.coinDom = ct ? (ct.innerText || '').trim() : null;
  } catch (e) { out.coinDom = null; }
  return out;
})()
"""

JS_CLICK_CHALLENGE = r"""
() => { const b = document.querySelector('[class*="challengeButton"]');
  if (!b) return null; b.click(); return (b.innerText || '').replace(/\n/g, ' ').trim(); }
"""

JS_CLICK_SKILL = r"""
(i) => {
  const cards = [...document.querySelectorAll('[class*="SkillPanel-module_skill-"], [class*="Starter-module_skill-"]')]
    .filter(c => { const r = c.getBoundingClientRect(); return r.width > 1 && r.height > 1; });
  const c = cards[i]; if (!c) return null;
  c.click(); return (c.innerText || '').trim().slice(0, 60);
}
"""

# 技能三选一：直接点「一键随机选择」，不再逐张挑卡（点一次会把本轮所有选择随机选完）。
#
# 识别方式（2026-11-04 实测，按钮文字是背景图，innerText 为空，不能按文本找）：
#   一键随机选择 <button class="Starter-module_btn-xxx Starter-module_auto-yyy">
#                背景图 .../refresh-auto-<hash>.png
#   刷新         <button class="Starter-module_btn-xxx">
#                背景图 .../refresh-<hash>.png
# 因此用「类名含 Starter-module_auto」或「背景图含 refresh-auto」**正向**识别；
# 两者都不满足就放弃，绝不退化成"点第一个按钮"——右边那个是刷新，要鱼翅。
JS_CLICK_RANDOM_SKILL = r"""
() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const bgOf = el => { try { return getComputedStyle(el).backgroundImage || ''; } catch (e) { return ''; } };

  const isAuto = el => {
    const cls = String(el.className || '');
    const bg = bgOf(el);
    return /Starter-module_auto/.test(cls) || /refresh-auto/i.test(bg);
  };

  // 只在技能面板范围内找，缩小误判面
  const scope = document.querySelector('[class*="Starter-module_skillsPanel"]')
             || document.querySelector('[class*="SkillPanel-module_panel"]')
             || document.body;

  let btn = null;
  for (const el of scope.querySelectorAll('button, [role="button"]')) {
    if (vis(el) && isAuto(el)) { btn = el; break; }
  }
  if (!btn) {
    for (const el of document.querySelectorAll('button')) {
      if (vis(el) && isAuto(el)) { btn = el; break; }
    }
  }
  if (!btn) return {ok: false, why: 'no-auto-btn'};

  // 正向再确认一次，避免任何情况下点到「刷新」
  if (!isAuto(btn)) return {ok: false, why: 'guard'};
  const bg = bgOf(btn);
  if (/refresh-/i.test(bg) && !/refresh-auto/i.test(bg)) return {ok: false, why: 'guard-refresh'};

  try { btn.click(); } catch (e) { return {ok: false, why: 'click-threw'}; }
  return {ok: true, cls: String(btn.className || ''), img: bg.slice(-40)};
}
"""

JS_CLICK_FINAL_START = r"""
() => {
  const vis = el => { if (!el) return false; const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.display !== 'none' && s.visibility !== 'hidden'; };
  const T = el => (el.innerText || '').replace(/\s+/g, '').trim();
  // 自身或内部真实按钮被禁用，都算不可点（外层 div 常是按钮的包装）
  const off = el => {
    if (el.disabled || el.getAttribute('aria-disabled') === 'true'
        || /disabled/i.test(String(el.className || ''))) return true;
    if (getComputedStyle(el).pointerEvents === 'none') return true;
    const inner = el.querySelector && el.querySelector('button, [role="button"]');
    return !!(inner && (inner.disabled || inner.getAttribute('aria-disabled') === 'true'));
  };
  const isBtn = el => el.matches('button, [role="button"]');
  const clickable = el => isBtn(el)
      || /btn|Btn|footer|Footer|confirm|Confirm|start|Start/.test(String(el.className || ''));

  const list = document.querySelector('[class*="Starter-module_skillList"]');
  if (!list) return {ok: false, why: 'no-skillList'};

  // 技能卡本身要排除，其余可点元素都算候选（含 footer 容器，点击可冒泡）
  const cand = root => [...root.querySelectorAll('button, [role="button"], [class*="footer"], [class*="Footer"], [class*="btn"], [class*="Btn"]')]
      .filter(b => vis(b) && !b.closest('[class*="_skill-"]') && clickable(b));

  // 面板根：footer 常是 skillList 的兄弟节点，逐层上溯
  let scope = list, found = [], hops = 0;
  while (scope && hops < 6) {
    found = cand(scope);
    if (found.length) break;
    scope = scope.parentElement; hops++;
  }

  // 全页兜底（弹窗可能挂在 body 下）
  const all = [...document.querySelectorAll('button, [role="button"]')]
      .filter(b => vis(b) && !b.closest('[class*="_skill-"]'));

  // 前缀匹配优先；再退化到包含匹配（应对「点击开始」这类文案）
  const KEY = [/^开始/, /^确认/, /^确定/, /出战/, /进入战斗/, /^战斗/, /^立即/,
               /开始/, /确认/, /出战/, /进入/];

  // 优先真实 <button>；容器元素只在找不到按钮时兜底，避免选中包装层而漏判 disabled
  const choose = arr => {
    const real = arr.filter(isBtn);
    for (const re of KEY) { const b = real.find(x => re.test(T(x))); if (b) return {b: b, how: 'text'}; }
    const f = real.find(x => /footer/i.test(String(x.className || '')));
    if (f) return {b: f, how: 'footer'};
    for (const re of KEY) { const b = arr.find(x => re.test(T(x))); if (b) return {b: b, how: 'text-wrapper'}; }
    if (real.length === 1) return {b: real[0], how: 'single'};
    if (arr.length === 1) return {b: arr[0], how: 'single-wrapper'};
    return {b: null, how: null};
  };

  let {b: btn, how} = choose(found);
  if (!btn) { const p = choose(all); btn = p.b; how = p.how ? 'page-' + p.how : null; }

  const dump = arr => [...new Set(arr)].slice(0, 6).map(b =>
      b.tagName + '|' + T(b).slice(0, 16) + '|' + String(b.className || '').slice(0, 34));

  if (!btn) return {ok: false, why: 'no-button', hops: hops,
                    panelCls: String((scope && scope.className) || '').slice(0, 90),
                    seen: dump(found.concat(all))};
  if (off(btn)) return {ok: false, why: 'disabled', how: how, text: T(btn).slice(0, 24),
                        seen: dump(found.concat(all))};
  try { btn.click(); } catch (e) { return {ok: false, why: 'click-threw:' + String(e).slice(0, 60)}; }
  return {ok: true, how: how, text: T(btn).slice(0, 24) || 'ok'};
}
"""

JS_CLOSE_LIMIT_ALTER = r"""
() => {
  const m = document.querySelector('[class*="Starter-module_limitAlter"]');
  if (!m) return false;
  const c = m.querySelector('[class*="Starter-module_close"]');
  if (c) { c.click(); return true; }
  return false;
}
"""

JS_RESULT_BTN = r"""
() => {
  const b = document.querySelector('[class*="Result-module_btn"]');
  if (b) { b.click(); return 'result-btn'; }
  const c = document.querySelector('[class*="TreasureResult-module_close"]');
  if (c) { c.click(); return 'treasure-close'; }
  return null;
}
"""

JS_HUD_CLAIM = r"""
() => {
  const b = document.querySelector('#js-target-treasure button');
  if (!b) return null;
  const r = b.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return null;
  b.click(); return 'claimed';
}
"""

JS_RESULT_TEXT = r"""
() => {
  const m = document.querySelector('[class*="Result-module_modal"]');
  return m ? (m.innerText || '').trim().replace(/\n/g, ' | ').slice(0, 300) : '';
}
"""

JS_CLEAR_NO_TIP = r"""
() => {
  const cleared = [];
  ['act202606Game_clsSkillNoTip', 'act202606Game_speedNoTip2x', 'act202606Game_speedNoTip4x',
   'act202606Game_stageRefreshSkillTip', 'act202606Game_battleRefreshSkillTip'].forEach(k => {
    try { if (localStorage.getItem(k) != null) { localStorage.removeItem(k); cleared.push(k); } } catch (e) {}
  });
  return cleared;
}
"""

JS_HUD_HTML = r"""
() => { const h = document.querySelector('[class*="GameHUD-module_container"]');
  return h ? h.outerHTML.slice(0, 6000) : null; }
"""


# ---------------------------------------------------------------- 技能评分
def score_skill_text(txt):
    t = (txt or "").replace(" ", "").replace("\n", "")
    # 付费相关（绝技次数）无价值 —— 我们不用绝技
    if "绝技" in t:
        return 60
    if "全部出战卡" in t and "攻击力" in t:
        return 950
    if "基地血量" in t:
        return 900
    if "全部出战卡" in t and "攻速" in t:
        return 880
    if "土系出战卡" in t and "攻击力" in t:
        return 820
    if "土系出战卡" in t and "攻速" in t:
        return 760
    if "土系出战卡" in t:
        return 620          # 眩晕等
    if "雷系出战卡" in t and "攻击力" in t:
        return 700
    if "火系出战卡" in t and "攻击力" in t:
        return 680
    if "冰系出战卡" in t and "攻击力" in t:
        return 660
    if "风系出战卡" in t and "攻击力" in t:
        return 660
    if "雷系出战卡" in t:
        return 520
    if "火系出战卡" in t:
        return 500
    if "冰系出战卡" in t:
        return 480
    if "风系出战卡" in t:
        return 470
    if "攻击力" in t:
        return 430
    if "攻速" in t:
        return 420
    if any(k in t for k in ("传导", "暴击", "冰冻", "横扫", "眩晕")):
        return 400
    return 300


# ---------------------------------------------------------------- Bot
class GuardFishBot:
    def __init__(self, rid=DEFAULT_RID, rounds=10, gap_min=5, gap_max=15,
                 headless=False, claim_weekpass=True, on_log=None,
                 hp_guard=True, hp_threshold=5, coin_play=True, one_hit=False,
                 daily_limit_min=240, rest_min=90,
                 block_stats=True, fp_harden=True,
                 safe_mode=None, safe_kill_prob=0.12,
                 max_battle_min=0, refill_cap_min=0):
        self.rid = str(rid)
        self.rounds = int(rounds)
        self.gap_min = float(gap_min or 0)
        self.gap_max = float(gap_max or 0)
        self.headless = bool(headless)
        self.claim_weekpass = bool(claim_weekpass)
        self.hp_guard = bool(hp_guard)
        self.hp_threshold = int(hp_threshold or 5)
        self.coin_play = bool(coin_play)          # 免费用尽后用银币续打（银币为免费获取）
        self.one_hit = bool(one_hit)              # 秒杀模式：我方卡牌造成的伤害直接击杀
        self._one_hit_kills = 0
        self.daily_limit_min = int(daily_limit_min or 0)   # 日活跃上限（分钟，0=不限）
        self.rest_min = int(rest_min or 90)                # 达到上限后长休（分钟）
        # 防封加固开关
        self.block_stats = bool(block_stats)   # 拦掉斗鱼通用埋点（与游戏无关）
        self.fp_harden = bool(fp_harden)       # 主世界指纹补丁（仅在异常时才改）
        # 低风险模式：弱化秒杀与回血，让对局数据看起来像真人
        # （秒杀=每次伤害恰好等于剩余血量，是服务端伤害日志里可检测的数学指纹）
        # 低风险已并入「秒杀模式」：不显式指定时，开秒杀即启用低风险口径
        self.safe_mode = bool(one_hit) if safe_mode is None else bool(safe_mode)
        self.safe_kill_prob = float(safe_kill_prob or 0.12)
        # 单场时长：0 = 不限（默认）。非 0 时到点主动结束，仅作为卡死兜底。
        self.max_battle_sec = int(max_battle_min or 0) * 60
        # 回血时长上限（分钟，0=不限）；非 0 时到点停手让战斗自然结束
        self.refill_cap_min = int(refill_cap_min or 0)
        self._stats_blocked = 0
        self._battle_paid = "free"                # free / coin

        self._stop = threading.Event()
        self._thread = None
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = threading.Lock()
        self._sinks = []
        if on_log:
            self._sinks.append(on_log)

        self._api_fp = None
        self._battle_fp = None
        self._battle_boxes = 0
        self._server_reject_ts = 0
        self._report_errs = 0
        self._date = datetime.date.today().strftime("%Y%m%d")

        # 网络层数据缓存（patchright 封了 init script，__WM 不可用时靠它）
        self._net = {"nick": None, "freeNum": None, "coin": None, "weekRank": None,
                     "maxStage": 0, "stage": None, "notLogin": False,
                     "panelTs": 0, "battleTs": 0, "panelEver": False}
        self._login_code = None
        self._gf_ev_idx = 0          # 已处理的主世界事件序号（localStorage 通道去重）
        # 日活跃计时（防 7x24 特征）：累计活跃秒数，跨天重置
        self._active_sec = 0.0
        self._active_mark = 0.0
        self._active_date = self._date

        # 对外可见的状态（GUI 读取）
        self.status = {
            "state": "空闲", "round": 0, "rounds": self.rounds, "stage": 0, "hp": 0,
            "freeNum": None, "maxStage": 0, "weekRank": 0, "nick": "", "coin": None,
        }

    # -------- 日志 --------
    def add_log_sink(self, fn):
        self._sinks.append(fn)

    def _log(self, msg, level="info"):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        log_msg(line)
        for fn in list(self._sinks):
            try:
                fn(line, level)
            except Exception:
                pass

    # -------- 停止 --------
    def request_stop(self):
        self._stop.set()
        self._log("收到停止指令…", "warn")

    def stop(self):
        self.request_stop()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=8)

    # -------- 浏览器 --------
    def _ensure_browser(self):
        if self._ctx is not None and self._page is not None:
            try:
                self._eval("1")
                return
            except Exception:
                self._close_browser()
        if self._pw is None:
            self._pw = sync_playwright().start()
            self._log("驱动：%s" % DRIVER_NAME)
        kill_stale_chrome(PROFILE)
        clean_profile_locks(PROFILE)
        ctx, exe = launch_persistent(self._pw, PROFILE, headless=self.headless)
        self._ctx = ctx
        self._page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self._log("浏览器就绪：%s（驱动：%s）" % (exe, DRIVER_NAME))
        # 埋点拦截：斗鱼通用统计 GIF（与游戏无关，拦掉不影响任何玩法）
        if self.block_stats:
            try:
                ctx.route("**/fish3/1.gif*", self._on_stats_beacon)
                self._log("埋点上报拦截：开（dotserver fish3/1.gif）")
            except Exception as e:
                self._log("埋点拦截注册失败：%s" % str(e)[:100], "warn")
        # patchright 封死了 add_init_script / addScriptToEvaluateOnNewDocument，
        # 因此注入改为「每次导航后 evaluate 注入」，见 _inject_page_scripts。
        # playwright 降级路径下 init script 仍然可用，两者都保留更稳。
        try:
            ctx.add_init_script(FAKE_DEVTOOLS_HOOK)
            ctx.add_init_script(JS_HP_WATCH)
            self._page.add_init_script(JS_HOOK)
        except Exception:
            pass
        try:
            self._page.on("response", self._on_response)
        except Exception:
            pass
        # 关闭付费「不再提示」标记（安全兜底）
        try:
            cleared = self._eval(JS_CLEAR_NO_TIP)
            if cleared:
                self._log("已清理付费免提示标记：%s" % cleared, "warn")
        except Exception:
            pass

    def _inject_page_scripts(self):
        """导航后把「主世界代理」装进页面。

        为什么这么绕：patchright 封死了 add_init_script / addScriptToEvaluateOnNewDocument，
        而它的 page.evaluate 默认跑在**隔离世界**——在隔离世界里看不到页面的
        window.__reactFiber$、看不到页面 fetch、也拿不到游戏引擎实例。
        实测：运行时插入 <script> 标签可以执行（走主世界），所以用 JS_GF_BOOT
        把 JS_HP_WATCH 装进主世界；配置与状态用 localStorage 双向传（跨世界共享）。
        """
        ok = []
        try:
            r = self._eval(JS_GF_BOOT, JS_HP_WATCH)
            ok.append("agent:%s" % r)
        except Exception as e:
            self._log("注入主世界代理失败：%s" % str(e)[:120], "warn")
        # 指纹补丁（主世界）：正常环境下全程 no-op
        if self.fp_harden:
            try:
                self._eval(JS_MAIN_EVAL, JS_FP_HARDEN)
                fp = self._eval("() => { try { const v = localStorage.getItem('gf_fp');"
                                " return v ? JSON.parse(v) : 'MISSING'; } catch (e) { return 'MISSING'; } }")
                if fp == "MISSING":
                    ok.append("fp:missing")
                    self._log("指纹补丁未生效（主世界脚本没执行）", "warn")
                elif fp:
                    ok.append("fp:%s" % ",".join(fp))
                    self._log("指纹补丁已生效：%s" % ", ".join(fp))
                else:
                    ok.append("fp:clean")
                    self._log("指纹检测：无需修补（真实浏览器环境正常）")
            except Exception as e:
                self._log("指纹补丁失败：%s" % str(e)[:100], "warn")
        try:
            self._eval(JS_HOOK)
            ok.append("wm-hook")
        except Exception:
            pass
        self._apply_hp_config()
        return ok

    def _on_stats_beacon(self, route):
        """拦掉斗鱼通用埋点：返回一张 1x1 透明 GIF，让埋点 SDK 以为上报成功。

        为什么用 fulfill 而不是 abort：abort 会让 SDK 触发重试/错误回调，
        反而制造额外异常；返回合法图片最安静。
        注意：**不拦任何 /japi/revenuenc/web/actgame/** 接口**——那是游戏业务接口，
        拦掉会导致推关/结算全部失败，而且「有开局没过程」本身就是更强的异常信号。
        """
        try:
            route.fulfill(status=200, content_type="image/gif", body=GIF_1PX)
            self._stats_blocked += 1
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    def _apply_hp_config(self):
        """把回血/秒杀配置写入 localStorage（主世界代理每 250ms 读一次）"""
        cfg = {"hpGuard": bool(self.hp_guard),
               "threshold": int(self.hp_threshold),
               "oneHit": bool(self.one_hit),
               "safe": bool(self.safe_mode),
               "killProb": float(self.safe_kill_prob),
               "refillCapMs": int(self.refill_cap_min * 60000)}
        try:
            self._eval(JS_GF_CFG, cfg)
            if self.safe_mode:
                self._log("秒杀模式：开（低风险口径：仅 %.0f%% 概率一击必杀，其余打 55~90%% 血量）"
                          "｜回血：%s（阈值 < %s，偶尔不回/不全回）"
                          % (self.safe_kill_prob * 100,
                             "开" if cfg["hpGuard"] else "关", cfg["threshold"]))
            else:
                self._log("秒杀模式：关｜回血守护：%s（阈值 < %s 回满）"
                          % ("开" if cfg["hpGuard"] else "关", cfg["threshold"]))
            return cfg
        except Exception as e:
            self._log("下发回血/秒杀配置失败：%s" % str(e)[:100], "warn")
            return None

    def _gf_status(self):
        """读主世界代理发布的状态（localStorage 通道）"""
        try:
            return self._eval(JS_GF_STATUS)
        except Exception:
            return None

    def _close_browser(self):
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        self._ctx = None
        self._page = None
        try:
            kill_stale_chrome(PROFILE)
            clean_profile_locks(PROFILE)
        except Exception:
            pass

    def _shutdown_pw(self):
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    # -------- 抓包 --------
    def _on_response(self, resp):
        try:
            url = resp.url
            if "/japi/revenuenc/" not in url or "/actgame/" not in url:
                return
            api = url.split("/actgame/")[-1].split("?")[0]
            try:
                body = resp.text()
            except Exception:
                body = ""
            if body:
                try:
                    j = json.loads(body)
                    # ==== 网络层数据缓存（patchright 下 __WM 不可用，这里是主通道）====
                    self._net_update(api, j)
                    # 战斗统计：宝箱领取累计
                    if api == "game/receiveBox" and isinstance(j, dict):
                        d = j.get("data") or {}
                        n = ((d.get("boxStat") or {}).get("total")) or len(d.get("list") or [])
                        with self._lock:
                            self._battle_boxes = (self._battle_boxes or 0) + int(n or 0)
                    # 服务端拒绝（如战斗超时 1042100「游戏参与异常」）
                    if api in ("game/stageEnd", "game/stageStart", "game/nextStage", "game/battleStart"):
                        err = j.get("error") if isinstance(j, dict) else None
                        try:
                            err = int(err or 0)
                        except Exception:
                            err = 0
                        if err:
                            if not self._server_reject_ts:
                                self._log("服务端对局限制：%s 返回 %s（%s）"
                                          % (api, err, str(j.get("msg") or "")[:60]), "warn")
                            self._server_reject_ts = time.time()
                    # 对局事件上报连续失败（≥3 次）也视为战斗被服务端中断
                    if api == "game/stageEventReport":
                        err2 = j.get("error") if isinstance(j, dict) else None
                        try:
                            err2 = int(err2 or 0)
                        except Exception:
                            err2 = 0
                        if err2:
                            self._report_errs += 1
                            if self._report_errs >= 3 and not self._server_reject_ts:
                                self._log("对局上报连续失败（%s），判定战斗已被服务端中断" % err2, "warn")
                                self._server_reject_ts = time.time()
                        else:
                            self._report_errs = 0
                    body = json.dumps(j, ensure_ascii=False)
                except Exception:
                    body = body[:4000]
            self._api_write({"t": time.strftime("%H:%M:%S"), "api": api, "status": resp.status, "body": body[:6000]})
        except Exception:
            pass

    # -------- 网络层数据缓存（patchright 下 __WM 注入不可用，这里是主通道）--------
    def _net_update(self, api, j):
        """从接口响应里抽取面板/对局数据。失败静默，不能影响抓包主流程。"""
        try:
            if not isinstance(j, dict):
                return
            d = j.get("data") or {}
            now = time.time()
            # 通用兜底：几乎所有 actgame 响应都带 userInfo / siteUserInfo，
            # 这样 battleStart / stageEnd 等接口也能刷新 freeNum（否则会用到过期值）
            try:
                u0 = d.get("userInfo") or {}
                s0 = d.get("siteUserInfo") or {}
                if u0.get("freeNum") is not None:
                    self._net["freeNum"] = u0.get("freeNum")
                if u0.get("maxStage") is not None:
                    self._net["maxStage"] = u0.get("maxStage")
                if s0.get("coin") is not None:
                    self._net["coin"] = s0.get("coin")
                if s0.get("siteWeekRank") is not None:
                    self._net["weekRank"] = s0.get("siteWeekRank")
            except Exception:
                pass
            if api == "basics/panelInfo":
                self._login_code = j.get("code") if j.get("code") is not None else self._login_code
                self._net["notLogin"] = (j.get("code") == 1002)
                u = d.get("userInfo") or {}
                s = d.get("siteUserInfo") or {}
                if u.get("nn"):
                    self._net["nick"] = u.get("nn")
                if s.get("siteWeekRank") is not None:
                    self._net["weekRank"] = s.get("siteWeekRank")
                self._net["panelTs"] = now
                if j.get("code") in (0, None):
                    self._net["panelEver"] = True
            elif api == "game/battleInfo":
                b = d.get("battleInfo") or {}
                if b.get("curStage") is not None:
                    self._net["stage"] = {"curStage": b.get("curStage"),
                                          "curHp": b.get("baseHp")}
                self._net["battleTs"] = now
            elif api in ("game/stageStart", "game/stageEnd", "game/nextStage", "game/battleStart"):
                si = d.get("stageInfo") or {}
                b = d.get("battleInfo") or {}
                if si.get("curStage") is not None:
                    self._net["stage"] = {"curStage": si.get("curStage"),
                                          "curHp": si.get("baseHp")}
                elif b.get("curStage") is not None:
                    self._net["stage"] = {"curStage": b.get("curStage"),
                                          "curHp": b.get("baseHp")}
        except Exception:
            pass

    def _net_fresh(self, kind="panel", ttl=120):
        """网络缓存是否新鲜（默认 2 分钟内）"""
        ts = self._net.get("panelTs" if kind == "panel" else "battleTs") or 0
        return (time.time() - ts) <= ttl

    def _api_write(self, obj):
        try:
            if self._api_fp is None:
                path = os.path.join(LOGDIR, "api_%s.jsonl" % self._date)
                self._api_fp = open(path, "a", encoding="utf-8")
                self._log("接口抓包写入：%s" % path)
            self._api_fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._api_fp.flush()
        except Exception:
            pass

    def _battle_write(self, obj):
        try:
            if self._battle_fp is None:
                path = os.path.join(LOGDIR, "battles_%s.jsonl" % self._date)
                self._battle_fp = open(path, "a", encoding="utf-8")
                self._log("战报写入：%s" % path)
            self._battle_fp.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._battle_fp.flush()
        except Exception:
            pass

    # -------- 页面动作 --------
    def _merge_net(self, st):
        """把网络回调缓存合并进状态（patchright 下 __WM 为空，这是主数据源）。

        原则：页面/__WM 有值优先（实时性更好），缺失时用网络缓存补齐；
              panelReady 用「页面 or 网络最近 2 分钟收到过 panelInfo」。
        """
        if not st:
            return st
        try:
            wm = st.get("wm")
            if not isinstance(wm, dict):
                wm = {}
            net = self._net
            if net.get("nick") and not wm.get("nick"):
                wm["nick"] = net["nick"]
            if wm.get("freeNum") is None and net.get("freeNum") is not None:
                wm["freeNum"] = net["freeNum"]
            if wm.get("coin") is None and net.get("coin") is not None:
                wm["coin"] = net["coin"]
            if not wm.get("weekRank") and net.get("weekRank"):
                wm["weekRank"] = net["weekRank"]
            if not wm.get("maxStage") and net.get("maxStage"):
                wm["maxStage"] = net["maxStage"]
            if not wm.get("stage") and net.get("stage"):
                wm["stage"] = net["stage"]
            if not wm.get("panelReady") and self._net_fresh("panel", 180):
                wm["panelReady"] = True
            st["wm"] = wm
        except Exception:
            pass
        return st

    def _state(self):
        try:
            st = self._eval(JS_STATE)
            return self._merge_net(st)
        except Exception as e:
            self._log("状态读取失败：%s" % str(e)[:120], "warn")
            return None

    def _sync_status(self, st):
        try:
            wm = st.get("wm") or {}
            self.status["stage"] = (wm.get("stage") or {}).get("curStage") or self.status["stage"]
            self.status["hp"] = (wm.get("stage") or {}).get("curHp", 0)
            self.status["freeNum"] = wm.get("freeNum")
            self.status["maxStage"] = wm.get("maxStage") or self.status["maxStage"]
            self.status["weekRank"] = wm.get("weekRank") or self.status["weekRank"]
            self.status["nick"] = wm.get("nick") or self.status["nick"]
            coin = self._coin_balance(st)
            if coin is not None:
                self.status["coin"] = coin
        except Exception:
            pass

    @staticmethod
    def _coin_balance(st):
        """读取银币余额：优先接口数据，其次页面 TopBar 文本（formatUnit 可能带 万/亿）
        注意：页面数据未就绪（panelReady=false）时不可信任任何读数（DOM 可能是占位 0）"""
        try:
            wm = st.get("wm") or {}
            if not wm.get("panelReady"):
                return None
            v = wm.get("coin")
            if isinstance(v, (int, float)):
                return int(v)
        except Exception:
            pass
        try:
            dom = (st.get("coinDom") or "").replace(",", "").strip()
            if dom:
                mul = 1
                if "万" in dom:
                    mul = 10000
                elif "亿" in dom:
                    mul = 100000000
                m = re.match(r"^(\d+(?:\.\d+)?)", dom)
                if m:
                    return int(float(m.group(1)) * mul)
        except Exception:
            pass
        return None

    # -------- 可信点击（防封：走浏览器输入管线，isTrusted=true）--------
    def _scroll_to_challenge(self, st=None):
        """若「开启挑战」按钮在视口外，用真实滚轮滚过去。

        为什么：按钮在长页面底部（实测 y≈1758，视口 940），JS 的 element.click()
        对视口外元素不生效——rect 有尺寸、canChallenge=True、点击无报错，但游戏收不到。
        滚动容器是 [class*="rc-scrollbars-view"]，用 page.mouse.wheel 更接近真人。
        """
        try:
            st = st or self._state() or {}
            if not st.get("challengeOffscreen"):
                return True
            for _ in range(6):
                # 鼠标先移到页面中部（滚动容器上）
                self._page.mouse.move(random.randint(700, 800), random.randint(430, 520))
                time.sleep(random.uniform(0.06, 0.18))
                self._page.mouse.wheel(0, random.randint(420, 680))
                time.sleep(random.uniform(0.25, 0.5))
                st2 = self._state() or {}
                if st2.get("canChallenge") and not st2.get("challengeOffscreen"):
                    return True
            return not (self._state() or {}).get("challengeOffscreen")
        except Exception as e:
            self._log("滚动到挑战按钮失败：%s" % str(e)[:100], "warn")
            return False

    def _human_move(self, x, y, steps=None):
        """沿二次贝塞尔曲线移动鼠标（带抖动），代替瞬移。

        为什么：`mouse.move()` 一次性跳点，在 mousemove 事件序列上是
        「一个点 → 另一个点」，与真人连续轨迹差异极大，且缺少时间分布。
        这里按 8~18 段折线 + 每步 ±1.2px 抖动，并把总时长随机化到 180~420ms。
        """
        try:
            cur = getattr(self, "_mouse_xy", None) or (random.randint(600, 900),
                                                       random.randint(300, 500))
            sx, sy = cur
            if steps is None:
                steps = random.randint(8, 18)
            # 控制点：垂直于两点连线偏移，制造弧度
            mx, my = (sx + x) / 2.0, (sy + y) / 2.0
            dx, dy = x - sx, y - sy
            dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
            off = random.uniform(-0.18, 0.18) * dist
            cx, cy = mx - dy / dist * off, my + dx / dist * off
            total = random.uniform(0.18, 0.42)
            for i in range(1, steps + 1):
                t = i / float(steps)
                bx = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t * t * x
                by = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t * t * y
                bx += random.uniform(-1.2, 1.2)
                by += random.uniform(-1.2, 1.2)
                self._page.mouse.move(bx, by)
                time.sleep(total / steps * random.uniform(0.6, 1.5))
            self._page.mouse.move(x, y)
            self._mouse_xy = (x, y)
        except Exception:
            try:
                self._page.mouse.move(x, y)
                self._mouse_xy = (x, y)
            except Exception:
                pass

    def _trusted_click(self, selector, label=""):
        """真实鼠标点击元素中心（isTrusted=true）。找不到/不可见时返回 False。"""
        try:
            box = self._eval(
                """(sel) => { const b = document.querySelector(sel); if (!b) return null;
                     const r = b.getBoundingClientRect();
                     if (r.width < 1 || r.height < 1) return null;
                     const cy = r.top + r.height / 2;
                     return {x: r.left + r.width / 2, y: cy,
                             off: (cy > window.innerHeight || cy < 0)}; }""", selector)
            if not box or box.get("off"):
                return False
            x = box["x"] + random.uniform(-2.5, 2.5)
            y = box["y"] + random.uniform(-2.0, 2.0)
            self._human_move(x, y)
            time.sleep(random.uniform(0.08, 0.25))
            self._page.mouse.down()
            time.sleep(random.uniform(0.03, 0.09))
            self._page.mouse.up()
            if label:
                self._log("鼠标点击：%s" % label)
            return True
        except Exception as e:
            if label:
                self._log("鼠标点击 %s 失败：%s" % (label, str(e)[:100]), "warn")
            return False

    def _click_random_skill(self, retries=2):
        """点「一键随机选择」；面板重绘瞬间可能短暂找不到，重试一次再放弃。"""
        r = None
        for i in range(max(1, retries)):
            r = self._click(JS_CLICK_RANDOM_SKILL)
            if isinstance(r, dict) and r.get("ok"):
                return r
            if i + 1 < retries:
                usleep(0.8)
        return r

    def _click_challenge(self, st=None):
        """点「开启挑战」：优先可信鼠标点击（必要时先滚动），失败再退回 JS click。"""
        st = st or self._state() or {}
        if st.get("challengeOffscreen"):
            self._scroll_to_challenge(st)
        if self._trusted_click('[class*="challengeButton"]', "开启挑战"):
            try:
                txt = self._eval(
                    "() => { const b = document.querySelector('[class*=\"challengeButton\"]');"
                    " return b ? (b.innerText||'').replace(/\\n/g,' ').trim() : null; }")
            except Exception:
                txt = None
            return txt or "clicked"
        return self._click(JS_CLICK_CHALLENGE)

    def _gate_challenge(self, st, wait_s=25):
        """开局闸门：免费 → 直接开；免费用尽 → 银币够则开（银币免费获取），否则停止。
        内部等待页面数据就绪（防止刷新后误判资源）。
        返回 (可开局: bool, 原因/说明: str)"""
        t0 = time.time()
        while True:
            wm = st.get("wm") or {}
            txt = st.get("challengeTxt") or ""
            hydrated = bool(wm.get("panelReady")) and wm.get("freeNum") is not None
            btn_ready = ("关" in txt) or bool(re.search(r"\d", txt))
            if hydrated and btn_ready:
                break
            if self._stop.is_set() or (time.time() - t0) > wait_s:
                return False, "等待游戏数据加载超时（页面未就绪），停止"
            usleep(1.0)
            st2 = self._state()
            if st2:
                st = st2
        txt = st.get("challengeTxt") or ""
        if "关" in txt:                      # 免费局（副标题 第N关）
            self._battle_paid = "free"
            return True, txt
        # 免费用尽：解析按钮上的银币价（默认 10）
        nums = re.findall(r"\d+", txt)
        cost = int(nums[-1]) if nums else 10
        if not self.coin_play:
            return False, "免费次数已用尽（按钮显示：%s），未开启银币续打，停止" % (txt or "?")
        coin = self._coin_balance(st)
        if coin is None:
            return False, "免费次数已用尽，且无法读取银币余额，为安全起见停止"
        if coin < cost:
            return False, "免费次数与银币均不足（银币余额 %d < %d），停止" % (coin, cost)
        self._battle_paid = "coin"
        return True, "免费次数已用尽 → 消耗 %d 银币开启本场（银币余额 %d）" % (cost, coin)

    def _try_start_battle(self):
        """尝试开启一场并进入战场（数据就绪等待 + 点击重试 + 面板处理）。
        返回 (ok, info)：info = {"skills": [...]}"""
        info = {"skills": []}
        # a) 已在对局/面板中 → 直接进入
        st = self._state() or {}
        if st.get("inBattle") or st.get("skillPanel") or st.get("finalSkillList"):
            self._log("检测到战斗进行中，继续监控")
            return True, info

        # b) 门闸（内部会等待页面数据就绪）
        if not st.get("canChallenge"):
            self._log("未找到「开启挑战」按钮，尝试恢复页面…", "warn")
            if self._try_recover():
                st = self._state() or {}
        ok, why = self._gate_challenge(st)
        if not ok:
            self._log(why + "。", "warn")
            return False, info
        r = self._click_challenge(st)
        self._log("已点击「开启挑战」（%s%s）"
                  % (why, "，本场消耗银币" if self._battle_paid == "coin" else ""))
        if not r:
            return False, info
        # 标记本场开始时间（回血守护据此在 9 分钟时收手，避免撞服务端 600 秒上限）
        try:
            self._eval(JS_GF_CMD, {"battleT0": int(time.time() * 1000), "resetKills": True})
        except Exception:
            pass
        last_click = time.time()
        clicks = 1

        # c) 开局等待：处理面板/弹窗；仍停在主页面则重试点击（点击可能落在按钮就绪前）
        #    超时用「无进展窗口」而非固定总时长：技能三选一/确认列表变化都算进展，
        #    避免技能多、面板刷新慢时被固定 90 秒误判失败。
        t0 = time.time()
        last_progress = t0
        last_final_txt = None
        last_final_click = 0.0
        final_fail_why = ""
        while True:
            if self._stop.is_set():
                return False, info
            if time.time() - t0 > PRE_BATTLE_HARD_LIMIT:
                self._log("开局超时（已达绝对上限 %ds）" % PRE_BATTLE_HARD_LIMIT, "warn")
                break
            if time.time() - last_progress > PRE_BATTLE_LIMIT:
                if last_final_txt is not None:
                    self._log("开局超时：技能确认列表停在本页 %ds 未能开始（%s）"
                              % (PRE_BATTLE_LIMIT, final_fail_why or "原因未知"), "warn")
                else:
                    self._log("开局超时：%ds 内无任何进展" % PRE_BATTLE_LIMIT, "warn")
                break
            time.sleep(jitter(0.8, -0.15, 0.35))
            st = self._state()
            if not st:
                continue
            self._sync_status(st)

            # 服务端已明确拒绝本场（如 1042008「游戏状态异常」）：
            # 继续点「开启挑战」只会重复扣银币且不会成功，直接放弃本次尝试。
            if self._server_reject_ts and (time.time() - self._server_reject_ts) < 120:
                self._log("服务端拒绝开局（状态异常），本场放弃，避免重复扣银币", "warn")
                return False, info

            if st.get("limitAlter"):
                self._click(JS_CLOSE_LIMIT_ALTER)
                self._log("卡组不足提示：需要 9 张出战卡（请到编队里配满）", "warn")
                last_progress = time.time()
                usleep(1.0)
                continue

            cards = st.get("skillPanel")
            if cards:
                # 优先「一键随机选择」（点一次把所有轮次随机选完）
                time.sleep(random.uniform(0.4, 1.2))
                rr = self._click_random_skill()
                if isinstance(rr, dict) and rr.get("ok"):
                    self._log("前置技能三选一 → 一键随机选择")
                    info["skills"].append({"text": "一键随机选择", "score": 0, "random": True})
                    last_progress = time.time()
                    usleep(1.8)
                    continue
                texts = [c.get("txt", "") for c in cards]
                scores = [score_skill_text(t) for t in texts]
                best = scores.index(max(scores))
                self._log("前置技能三选一 → 选 #%d（%d分）：%s" % (best + 1, max(scores), texts[best][:70]))
                # 人类行为：先停顿一下再点（模拟阅读技能描述）
                time.sleep(random.uniform(0.4, 1.4))
                got = self._click(JS_CLICK_SKILL, best)
                if got:
                    info["skills"].append({"text": texts[best][:80], "score": max(scores)})
                last_progress = time.time()
                usleep(2.0)
                continue

            if st.get("finalSkillList"):
                txt = st.get("finalSkillList") or ""
                if txt != last_final_txt:
                    last_final_txt = txt
                    last_progress = time.time()
                    self._log("技能确认列表：%s" % txt[:120])
                # 点击「开始战斗」：带冷却，避免每 0.8s 狂点；并校验返回结果
                if time.time() - last_final_click > 3.0:
                    last_final_click = time.time()
                    # 人类行为：犹豫一下再点
                    time.sleep(random.uniform(0.6, 1.8))
                    r = self._click(JS_CLICK_FINAL_START)
                    if isinstance(r, dict):
                        if r.get("ok"):
                            self._log("已点击开始战斗（%s · %s）" % (r.get("text") or "", r.get("how") or ""))
                            last_progress = time.time()
                        else:
                            final_fail_why = str(r.get("why") or "?")
                            seen = r.get("seen") or []
                            self._log("技能确认列表未能开始：%s；面板=%s；候选=%s"
                                      % (final_fail_why, (r.get("panelCls") or "")[:60], seen),
                                      "warn")
                    else:
                        final_fail_why = "no-skillList"
                        self._log("技能确认列表未能开始：页面未找到 Starter-module_skillList", "warn")
                usleep(1.0)
                continue

            if st.get("inBattle"):
                return True, info
            if st.get("result") or st.get("treasure"):
                self._log("战斗未开始即出现结算？", "warn")
                self._click(JS_RESULT_BTN)
                usleep(1.5)
                continue

            # 仍停在主页面：点击可能落空（按钮未就绪/被拦截）→ 重试
            if st.get("canChallenge") and (time.time() - last_click) > 15 and clicks < 6:
                r2 = self._click_challenge(st)
                if r2:
                    clicks += 1
                    last_click = time.time()
                    last_progress = time.time()
                    self._log("再次点击「开启挑战」（重试第 %d 次）" % clicks)

        return False, info

    def _goto_game(self):
        self._log("打开游戏页：房间 %s" % self.rid)
        try:
            self._page.goto(GAME_URL.format(rid=self.rid), wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            self._log("页面加载异常：%s" % str(e)[:150], "warn")
        # patchright 下 init script 无效，加载后补注入
        self._inject_page_scripts()
        # 登录判定：网络缓存优先，__WM hook 兜底
        self._wait_ready(60)
        ok = self._wait_login(45)
        if ok:
            return True
        if self._login_code == 1002:
            self._log("未登录：服务端返回「用户未登录」（code 1002）。请点「登录斗鱼」重新登录，"
                      "扫码后等提示「登录成功」再关窗口。注意：同一账号在别处登录会把这里的会话挤掉。",
                      "error")
        else:
            self._log("未登录（panelInfo code=%s / 网络缓存 %s）。请点「登录斗鱼」完成登录后再挂机。"
                      % (self._login_code, "无" if not self._net_fresh("panel") else "未登录"), "error")
        return False

    def _wait_login(self, timeout=45):
        """登录判定（双通道）：网络回调缓存的 panelInfo 优先，页面 __WM 兜底。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self._stop.is_set():
                return False
            # 通道 1：网络回调
            if self._net.get("nick"):
                self.status["nick"] = self._net["nick"]
                self._log("登录账号：%s（网络回调）" % self._net["nick"])
                return True
            if self._net.get("notLogin"):
                self._login_code = 1002
                return False
            # 通道 2：页面 hook
            try:
                info = self._eval(
                    "() => window.__WM && window.__WM.panelInfo ? {code: window.__WM.panelInfo.code, "
                    "nick: (window.__WM.panelInfo.data && window.__WM.panelInfo.data.userInfo && "
                    "window.__WM.panelInfo.data.userInfo.nn) || ''} : null")
            except Exception:
                info = None
            if info:
                self._login_code = info.get("code")
                if info.get("nick"):
                    self.status["nick"] = info["nick"]
                    self._log("登录账号：%s" % info["nick"])
                    return True
                if self._login_code == 1002:
                    return False
            time.sleep(1)
        # 最后再看一次网络缓存（慢网）
        if self._net.get("nick"):
            self.status["nick"] = self._net["nick"]
            self._log("登录账号：%s（网络回调）" % self._net["nick"])
            return True
        return False

    def _wait_ready(self, timeout):
        """等待页面出现主界面按钮或战场（数据就绪才算数，防止刷新后过早操作）

        防封改造后 __WM 可能不可用，就绪判定放宽为：
          按钮文案含「关」或数字，且（页面 panelReady 或 网络已收到 panelInfo 或 freeNum 已知）。
        每 15 秒打一次诊断，避免「浏览器开着但不动」时无从排查。
        """
        t0 = time.time()
        last_diag = 0.0
        while time.time() - t0 < timeout:
            if self._stop.is_set():
                return False
            st = self._state()
            if st:
                wm = st.get("wm") or {}
                txt = st.get("challengeTxt") or ""
                net_ok = self._net_fresh("panel", 180) or wm.get("freeNum") is not None
                if st.get("canChallenge"):
                    if (wm.get("panelReady") or net_ok) and (("关" in txt) or re.search(r"\d", txt)):
                        return True
                elif st.get("inBattle") or st.get("result") or st.get("treasure") \
                        or st.get("skillPanel") or st.get("finalSkillList"):
                    return True
                now = time.time()
                if now - last_diag >= 15:
                    last_diag = now
                    self._log("[诊断] 等待页面就绪：btn=%r canChallenge=%s panelReady=%s "
                              "网络panel=%s freeNum=%s 驱动=%s"
                              % (txt[:24], bool(st.get("canChallenge")),
                                 bool(wm.get("panelReady")),
                                 "有" if self._net_fresh("panel", 180) else "无",
                                 wm.get("freeNum"), DRIVER_NAME), "warn")
            time.sleep(jitter(0.8, -0.15, 0.35))
        return False

    def _eval(self, js, arg=None):
        """在**页面主世界**执行 JS。

        为什么必须有这个包装：patchright 的 page.evaluate 默认
        isolatedContext=True（隔离世界），与页面主世界不通——
        在里面看不到 window.__reactFiber$、看不到页面 fetch、也拿不到游戏引擎实例。
        这会让回血/秒杀/抓包 hook 全部失效（表现为「浏览器开着但什么都不做」）。
        因此统一强制主世界；同时兼容没有该参数的 playwright。
        """
        try:
            if arg is None:
                return self._page.evaluate(js, isolatedContext=False)
            return self._page.evaluate(js, arg, isolatedContext=False)
        except TypeError:
            # 旧版 driver 不接受 isolatedContext（playwright 降级路径）
            if arg is None:
                return self._page.evaluate(js)
            return self._page.evaluate(js, arg)

    def _click(self, js, *args):
        try:
            return self._eval(js, args[0]) if args else self._eval(js)
        except Exception as e:
            self._log("点击执行失败：%s" % str(e)[:120], "warn")
            return None

    # -------- 主流程 --------
    def start(self):
        if self._thread and self._thread.is_alive():
            self._log("已在运行中", "warn")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="guardfish-bot", daemon=True)
        self._thread.start()

    def _run(self):
        ensure_dirs()
        self._load_active()
        try:
            self._ensure_browser()
            if not self._goto_game():
                self.status["state"] = "未登录"
                return

            self._log("驱动：%s｜日活跃上限：%s｜单场：%s"
                      % (DRIVER_NAME,
                         ("%d 分钟（超限休息 %d 分钟）" % (self.daily_limit_min, self.rest_min))
                         if self.daily_limit_min > 0 else "不限",
                         ("不限（打到自然结束）" if self.max_battle_sec <= 0
                          else "%d 分钟" % (self.max_battle_sec // 60))))
            if self.safe_mode:
                self._log("低风险模式：开（伤害弱化、偶尔不回血）｜回血停手：%s"
                          % ("不限" if self.refill_cap_min <= 0
                             else "%d 分钟后停手" % self.refill_cap_min))
            rounds_done = 0
            i = 0
            while True:
                i += 1
                if self.rounds > 0 and i > self.rounds:
                    break
                if self._stop.is_set():
                    break
                self.status["round"] = i
                t_round = time.time()
                r = self._one_battle(i)
                self._add_active(time.time() - t_round)
                if r is None:
                    self._log("第%d场未能完成，停止。" % i, "error")
                    break
                rounds_done += 1
                self._battle_write({
                    "date": self._date, "round": i,
                    "paid": r.get("paid"),
                    "start": r.get("t0"), "end": r.get("t1"), "dur_s": r.get("dur"),
                    "stage0": r.get("stage0"), "stageN": r.get("stageN"),
                    "freeNum": r.get("freeNum"), "boxes": r.get("boxes"),
                    "result": r.get("resultText"), "skills": r.get("skills"),
                    "refills": r.get("refills"), "hpMin": r.get("hpMin"),
                    "oneHitKills": r.get("oneHitKills"),
                    "ended": r.get("ended"),
                })
                self._log("战报：起始第%s关 → 到达第%s关，用时 %.0f 秒，回血 %s 次，%s%s"
                          % (r.get("stage0"), r.get("stageN"), r.get("dur") or 0,
                             r.get("refills") if r.get("refills") is not None else "-",
                             "（银币局）" if r.get("paid") == "coin" else "",
                             ("结算：" + (r.get("resultText") or "")) if r.get("resultText") else ""))
                if self._stop.is_set():
                    break
                # 日活跃上限：累计活跃超限则长休（防止 7x24 特征）
                if self._daily_limit_reached():
                    if not self._long_rest():
                        break
                    continue
                if i < self.rounds:
                    self._sleep_gap()

            self.status["state"] = "已结束" if not self._stop.is_set() else "已停止"
            self._log("运行结束（完成 %d 场）" % rounds_done)
            if self.block_stats:
                self._log("本次共拦截埋点上报 %d 次" % self._stats_blocked)
        except Exception as e:
            self._log("异常终止：%s" % str(e)[:300], "error")
            self._log(traceback.format_exc()[-800:], "error")
            self.status["state"] = "异常"
        finally:
            try:
                self._close_browser()
            except Exception:
                pass
            try:
                self._shutdown_pw()
            except Exception:
                pass

    # -------- 日活跃计时（防 7x24 特征）--------
    def _active_path(self):
        return os.path.join(DATA_DIR, "active_state.json")

    def _load_active(self):
        try:
            with open(self._active_path(), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("date") == self._date:
                self._active_sec = float(d.get("sec") or 0)
            else:
                self._active_sec = 0.0     # 跨天重置
        except Exception:
            self._active_sec = 0.0

    def _save_active(self):
        try:
            with open(self._active_path(), "w", encoding="utf-8") as f:
                json.dump({"date": self._date, "sec": round(self._active_sec, 1)}, f)
        except Exception:
            pass

    def _add_active(self, sec):
        """累加活跃秒数；跨天自动重置"""
        today = datetime.date.today().strftime("%Y%m%d")
        if today != self._date:
            self._date = today
            self._active_sec = 0.0
        self._active_sec += max(0.0, float(sec or 0))
        self._save_active()

    def _daily_limit_reached(self):
        if self.daily_limit_min <= 0:
            return False
        return self._active_sec >= self.daily_limit_min * 60

    def _long_rest(self):
        """达到日上限后的长休。返回 False 表示被用户停止。"""
        rest = max(1, self.rest_min) * 60
        self._log("已达到每日活跃上限 %d 分钟，休息 %d 分钟（防 7x24 特征）…"
                  % (self.daily_limit_min, self.rest_min), "warn")
        self.status["state"] = "日上限休息"
        t0 = time.time()
        while time.time() - t0 < rest:
            if self._stop.is_set():
                return False
            time.sleep(min(5.0, rest - (time.time() - t0)))
        # 休息结束视为跨入新的一天配额
        self._active_sec = 0.0
        self._save_active()
        self._log("休息结束，继续挂机")
        self.status["state"] = "运行中"
        return True

    def _sleep_gap(self):
        lo, hi = min(self.gap_min, self.gap_max), max(self.gap_min, self.gap_max)
        if hi <= 0:
            return
        sec = random.uniform(max(lo, 1), max(hi, 2))
        # 长尾休息：12% 概率插入一段较长等待，避免间隔分布过于整齐
        extra = ""
        if random.random() < 0.12:
            sec = random.uniform(40, 150)
            extra = "（长尾休息）"
        self._log("场间等待 %.0f 秒…%s" % (sec, extra))
        t0 = time.time()
        while time.time() - t0 < sec:
            if self._stop.is_set():
                return
            time.sleep(0.5)

    def _one_battle(self, idx):
        """打一场，返回统计 dict 或 None"""
        self.status["state"] = "准备开局"
        with self._lock:
            self._battle_boxes = 0
        self._server_reject_ts = 0
        self._report_errs = 0
        self._log("========== 第 %d%s 场 =========="
                  % (idx, ("/%d" % self.rounds) if self.rounds > 0 else ""))

        # 0) 清理可能残留的弹窗
        for _ in range(3):
            st = self._state()
            if not st:
                return None
            if st.get("result") or st.get("treasure"):
                r = self._click(JS_RESULT_BTN)
                self._log("清理残留弹窗：%s" % r)
                usleep(1.2)
                continue
            if st.get("finalSkillList"):
                r = self._click(JS_CLICK_FINAL_START)
                if isinstance(r, dict) and r.get("ok"):
                    self._log("残留的技能确认列表 → 已点开始战斗（%s）" % (r.get("text") or ""))
                else:
                    self._log("残留的技能确认列表 → 未能开始（%s）"
                              % ((r or {}).get("why") if isinstance(r, dict) else "no-skillList"), "warn")
                usleep(1.5)
                continue
            break

        st = self._state()
        if not st:
            return None

        # 1+2) 开局（含数据就绪等待、点击重试；失败自动恢复后重试一次）
        stage0 = None
        started, info = self._try_start_battle()
        skills_chosen = list(info.get("skills") or [])
        if not started:
            self._log("开局未成功，刷新页面后重试一次…", "warn")
            self._snap("pre_battle_retry")
            try:
                self._page.reload(wait_until="domcontentloaded", timeout=60000)
                self._inject_page_scripts()
                self._wait_ready(60)
                self._apply_hp_config()
            except Exception as e:
                self._log("刷新失败：%s" % str(e)[:120], "error")
            started, info2 = self._try_start_battle()
            if not started:
                self._log("开局再次失败（未能进入战场），停止。", "error")
                self._snap("pre_battle_timeout")
                return None
            skills_chosen = list(info2.get("skills") or [])

        # 3) 战斗循环
        self.status["state"] = "战斗中"
        t_battle0 = time.time()
        ended = None
        result_text = ""
        boxes = None
        last_hud = 0
        last_claim = 0
        next_drift_ts = time.time() + random.uniform(20, 45)
        snap_done = False
        hp_timeline = []
        hp_min = None
        hp_refill_count = 0
        hp_found_logged = False
        last_progress_ts = time.time()
        last_sig = None
        # 清掉上一场的残留事件：把序号对齐到当前总数
        try:
            st0 = self._gf_status() or {}
            self._gf_ev_idx = int(st0.get("evTotal") or 0)
        except Exception:
            self._gf_ev_idx = 0
        while self.max_battle_sec <= 0 or (time.time() - t_battle0) < self.max_battle_sec:
            if self._stop.is_set():
                return None
            time.sleep(jitter(TICK, -0.08, 0.12))
            st = self._state()
            if not st:
                continue
            self._sync_status(st)
            wm = st.get("wm") or {}
            stage = (wm.get("stage") or {})
            if stage0 is None and stage.get("curStage"):
                stage0 = stage.get("curStage")

            # 血条守护：回收事件（回血/掉血）——走 localStorage 通道
            gf = self._gf_status()
            if gf:
                if gf.get("found") and not hp_found_logged:
                    hp_found_logged = True
                    self._log("回血守护已接管（%s）" % (gf.get("comp") or "engine"))
                if gf.get("err"):
                    self._log("回血守护异常：%s" % str(gf.get("err"))[:120], "warn")
                if gf.get("oneHitKills") is not None:
                    self._one_hit_kills = int(gf.get("oneHitKills") or 0)
                    self.status["one_hit_kills"] = self._one_hit_kills
                # 只处理新事件（代理每次发布最近 200 条 + 总序号）
                total = int(gf.get("evTotal") or 0)
                base = int(gf.get("evBase") or 0)
                evs = gf.get("events") or []
                if total > self._gf_ev_idx:
                    start = max(0, self._gf_ev_idx - base)
                    new_evs = evs[start:]
                    self._gf_ev_idx = total
                else:
                    new_evs = []
                for ev in new_evs:
                    if ev.get("type") == "refill":
                        hp_refill_count += 1
                        self._log("回血触发：血量 %s → %s（第 %d 次）"
                                  % (ev.get("from"), ev.get("to"), hp_refill_count))
                        if len(hp_timeline) < 500:
                            hp_timeline.append({"t": int(time.time() - t_battle0), "ev": "refill",
                                                "from": ev.get("from"), "to": ev.get("to")})
                    elif ev.get("type") == "damage":
                        v = ev.get("to")
                        if isinstance(v, (int, float)):
                            hp_min = v if hp_min is None else min(hp_min, v)
                        if len(hp_timeline) < 500:
                            hp_timeline.append({"t": int(time.time() - t_battle0), "ev": "hp", "to": v})
                    elif ev.get("type") == "cap":
                        self._log("回血守护：本场已到 %s 分钟，停止回血让战斗自然结束（避免服务端超时判异常）"
                                  % ev.get("mins", 9))

            # 战斗停滞检测（服务端限制 / 卡死）：任意状态变化都算进展
            sig = (stage.get("curStage"), stage.get("curHp"), hp_refill_count,
                   bool(st.get("skillPanel")), bool(st.get("result")), bool(st.get("treasure")),
                   bool(st.get("hudClaimable")), bool(st.get("finalSkillList")))
            if sig != last_sig:
                last_sig = sig
                last_progress_ts = time.time()
            stuck_by_server = self._server_reject_ts and (time.time() - self._server_reject_ts > 15)
            stuck_by_time = (time.time() - last_progress_ts) > 240
            if st.get("inBattle") and (stuck_by_server or stuck_by_time) \
                    and not st.get("skillPanel") and not st.get("result") and not st.get("treasure") \
                    and not st.get("finalSkillList"):
                self._log("战斗停滞（%s），判定本场结束，稍后刷新恢复"
                          % ("服务端限制" if stuck_by_server else "长时间无进展"), "warn")
                self._snap("battle_stuck")
                ended = "停滞恢复"
                break

            # 记录 HUD 结构一次（供诊断）
            if st.get("inBattle") and not snap_done:
                snap_done = True
                try:
                    html = self._eval(JS_HUD_HTML)
                    if html:
                        with open(os.path.join(LOGDIR, "hud_sample.html"), "w", encoding="utf-8") as f:
                            f.write(html)
                except Exception:
                    pass

            el = int(time.time() - t_battle0)
            self.status["refills"] = hp_refill_count
            if el - last_hud >= 60:
                last_hud = el
                self._log("进行中 t=%ss 第%s关 HP=%s 免费剩余=%s 回血=%d%s%s"
                          % (el, stage.get("curStage", "?"), stage.get("curHp", "?"),
                             wm.get("freeNum", "?"), hp_refill_count,
                             (" 秒杀=%d" % self._one_hit_kills) if self.one_hit else "",
                             (" 拦截埋点=%d" % self._stats_blocked) if self.block_stats else ""))

            # 空闲微动：真人挂机时鼠标不会十几分钟一动不动（只移动，不点击）
            if time.time() >= next_drift_ts:
                next_drift_ts = time.time() + random.uniform(35, 75)
                try:
                    self._human_move(random.randint(220, 1280), random.randint(650, 900))
                except Exception:
                    pass

            # 3.1 结算（只有真正的结算弹窗才算本场结束；宝箱弹窗不算）
            if st.get("result") or st.get("treasure"):
                had_result = bool(st.get("result"))
                if had_result:
                    result_text = self._click(JS_RESULT_TEXT) or ""
                # 人类行为：看一眼结算数据再点
                time.sleep(random.uniform(0.8, 2.2))
                r = self._click(JS_RESULT_BTN)
                self._log("%s弹窗 → %s %s" % ("结算" if had_result else "奖励", r,
                                              ("｜" + result_text[:120]) if result_text else ""))
                usleep(2.0)
                st2 = self._state() or {}
                if st2.get("treasure"):
                    r2 = self._click(JS_RESULT_BTN)
                    self._log("奖励弹窗 → %s" % r2)
                    usleep(1.5)
                st3 = self._state() or {}
                # 判定本场结束：只看「战场消失 + 主界面挑战按钮出现」。
                # 不能只看弹窗——新版把「宝箱奖励」也做成了 Result-module_modal，
                # 只看弹窗会在第一个宝箱掉落时误判本场结束（实测 6 秒就结束）。
                if not st3.get("inBattle") and st3.get("canChallenge") \
                        and not st3.get("skillPanel") and not st3.get("finalSkillList"):
                    ended = "结算完成"
                    break
                usleep(1.0)
                continue

            # 3.2 关卡技能三选一
            cards = st.get("skillPanel")
            if cards:
                # 优先「一键随机选择」（点一次把所有轮次随机选完）
                time.sleep(random.uniform(0.4, 1.2))
                rr = self._click_random_skill()
                if isinstance(rr, dict) and rr.get("ok"):
                    self._log("第%s关技能三选一 → 一键随机选择" % stage.get("curStage", "?"))
                    skills_chosen.append({"text": "一键随机选择", "score": 0, "random": True})
                    usleep(1.8)
                    continue
                texts = [c.get("txt", "") for c in cards]
                scores = [score_skill_text(t) for t in texts]
                best = scores.index(max(scores))
                self._log("第%s关技能三选一 → 选 #%d（%d分）：%s"
                          % (stage.get("curStage", "?"), best + 1, max(scores), texts[best][:70]))
                time.sleep(random.uniform(0.4, 1.4))
                got = self._click(JS_CLICK_SKILL, best)
                if got:
                    skills_chosen.append({"text": texts[best][:80], "score": max(scores)})
                usleep(2.2)
                continue

            # 3.3 每周宝箱领取（免费）
            if self.claim_weekpass and st.get("hudClaimable") and time.time() - last_claim > 25:
                last_claim = time.time()
                r = self._click(JS_HUD_CLAIM)
                if r:
                    self._log("领取战斗宝箱")
                    usleep(1.5)
                    st2 = self._state() or {}
                    if st2.get("treasure"):
                        self._click(JS_RESULT_BTN)
                        self._log("宝箱奖励弹窗已关闭")
                        usleep(1.0)

            # 3.4 异常兜底：若既不在战场也无面板
            if not st.get("inBattle") and not st.get("skillPanel") and not st.get("result") \
                    and not st.get("treasure") and not st.get("finalSkillList"):
                # 可能已经回到主页面（提前结束）
                if st.get("canChallenge"):
                    self._log("战斗已结束（回到主界面）", "warn")
                    ended = "提前结束"
                    break

        dur = time.time() - t_battle0
        if ended is None:
            ended = "超时保护"
            if self.max_battle_sec > 0:
                self._log("达到单场最大等待时间（%d 分钟），继续下一场"
                          % (self.max_battle_sec // 60), "warn")

        # 3.5 结束清理（可能还有弹窗）
        for _ in range(4):
            st = self._state() or {}
            if st.get("result") or st.get("treasure"):
                self._click(JS_RESULT_BTN)
                time.sleep(1.5)
            else:
                break

        # 3.6 停滞/异常恢复：战场卡死 → 刷新页面（该场已被服务端结束，刷新安全）
        if ended == "停滞恢复":
            try:
                self._log("刷新页面恢复…", "warn")
                self._page.reload(wait_until="domcontentloaded", timeout=60000)
                self._inject_page_scripts()
                self._wait_ready(60)
                self._apply_hp_config()
                self._log("页面已恢复")
            except Exception as e:
                self._log("刷新恢复失败：%s" % str(e)[:120], "error")

        final = self._state() or {}
        fwm = (final.get("wm") or {})
        fstage = (fwm.get("stage") or {})
        with self._lock:
            boxes = self._battle_boxes
        return {
            "t0": time.strftime("%H:%M:%S", time.localtime(t_battle0)),
            "t1": time.strftime("%H:%M:%S"),
            "dur": dur,
            "stage0": stage0,
            "stageN": fstage.get("curStage") or self.status.get("stage"),
            "freeNum": fwm.get("freeNum"),
            "boxes": boxes,
            "paid": self._battle_paid,
            "resultText": result_text,
            "skills": skills_chosen,
            "oneHitKills": self._one_hit_kills,
            "refills": hp_refill_count,
            "hpMin": hp_min,
            "hpTimeline": hp_timeline,
            "ended": ended,
        }

    def _try_recover(self):
        """主页面卡住时的恢复：刷新页面（仅在非战斗中调用）"""
        try:
            self._log("刷新页面恢复…", "warn")
            self._page.reload(wait_until="domcontentloaded", timeout=60000)
            self._inject_page_scripts()
            ok = self._wait_ready(60)
            try:
                self._apply_hp_config()
            except Exception:
                pass
            return ok
        except Exception as e:
            self._log("刷新失败：%s" % str(e)[:120], "error")
            return False

    def _snap(self, tag):
        try:
            fp = os.path.join(LOGDIR, "err_%s_%s.png" % (tag, time.strftime("%H%M%S")))
            self._page.screenshot(path=fp)
            self._log("截图：%s" % fp)
        except Exception:
            pass


# ---------------------------------------------------------------- 登录
def page_login_state(page):
    """从页面内容判断登录态。

    为什么要这么绕：未登录时游戏页也会渲染一个占位按钮「开启挑战 0」
    （canChallenge=True），所以「按钮存在」或「按钮含数字」都不能当登录成功的依据。
    真正的信号是页面有没有加载出账号数据（周天梯/关卡），或者干脆显示「加载失败」。

    返回 (state, detail)：'ok' 已登录 / 'no' 未登录 / None 还在加载
    """
    try:
        d = page.evaluate("""() => {
            const b = document.querySelector('[class*="challengeButton"]');
            const btn = b ? (b.innerText || '').split(String.fromCharCode(10)).join(' ').trim() : '';
            const body = (document.body.innerText || '').slice(0, 400);
            return {btn: btn, body: body};
        }""")
    except Exception:
        return None, ""
    btn = (d.get("btn") or "").strip()
    body = d.get("body") or ""
    if "加载失败" in body or "用户未登录" in body:
        return "no", body[:60]
    has_data = ("周天梯" in body) or ("图鉴" in body) or ("关" in btn)
    if has_data and ("关" in btn or "周天梯" in body):
        return "ok", (btn or "已登录")
    return None, btn


def await_login_state(page, timeout=45, stable=5):
    """轮询页面登录态；「已登录」必须**连续稳定** stable 秒才认可。

    为什么要求稳定：游戏页会先用浏览器缓存渲染一次旧画面（可能短暂显示「周天梯」和
    占位按钮「开启挑战 0」），随后接口失败才切成「加载失败」。单点采样会把这种
    瞬时脏渲染误判成「已登录」——实测踩过。
    返回 (ok, detail)
    """
    t0 = time.time()
    ok_cnt = 0
    no_cnt = 0
    detail = ""
    while time.time() - t0 < timeout:
        st, d = page_login_state(page)
        if d:
            detail = d
        if st == "ok":
            ok_cnt += 1
            no_cnt = 0
            if ok_cnt >= stable:
                return True, detail
        elif st == "no":
            no_cnt += 1
            ok_cnt = 0
            if no_cnt >= 2:          # 明确「加载失败」连续 2 次即判定未登录
                return False, detail
        time.sleep(1)
    return False, detail


def verify_login(timeout=45, headless=True, rid=DEFAULT_RID):
    """独立复核登录态（打开游戏页 + 稳定判定）。

    返回 (ok: bool, detail: str, code, msg)
    """
    state = {"code": None, "msg": ""}
    ok, detail = False, ""
    try:
        with sync_playwright() as pw:
            ctx, exe = launch_persistent(pw, PROFILE, headless=headless)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            def _on_resp(resp):
                try:
                    if "basics/panelInfo" not in resp.url:
                        return
                    j = json.loads(resp.text())
                    state["code"] = j.get("code")
                    state["msg"] = str(j.get("msg") or "")
                except Exception:
                    pass

            page.on("response", _on_resp)
            try:
                page.goto(GAME_URL.format(rid=rid), wait_until="domcontentloaded", timeout=90000)
            except Exception:
                pass
            ok, detail = await_login_state(page, timeout=timeout, stable=5)
            try:
                ctx.close()
            except Exception:
                pass
    except Exception as e:
        state["msg"] = str(e)[:150]
    return ok, detail, state["code"], state["msg"]


def open_login(rid=DEFAULT_RID):
    """拉起浏览器登录，并在登录过程中实时检测是否真的登录成功。

    改进点（旧版只说「窗口已关闭」，登录失败也看不出来）：
      1. 先开斗鱼首页（有明确的「登录」入口；直接开游戏页只会显示“加载失败”）。
      2. 盯 acf_auth 这个登录凭据的变化——一变化就自动跳到游戏页确认，
         确认成功立刻打「✅ 登录成功」，提示用户可以关窗口。
      3. 判定不是看按钮存在与否（未登录时也有占位按钮「开启挑战 0」），
         而是要求页面**连续稳定**显示真实关卡（见 await_login_state）。
      4. 关窗口前再复核一次，明确告知登录态到底有没有保存。
    返回 True/False（是否确认登录成功）。
    """
    ensure_dirs()
    try:
        from patchright.sync_api import TimeoutError as PWTimeoutError
    except Exception:
        from playwright.sync_api import TimeoutError as PWTimeoutError
    kill_stale_chrome(PROFILE)
    clean_profile_locks(PROFILE)
    log_msg("打开浏览器，请完成登录…（驱动：%s）" % DRIVER_NAME)

    state = {"code": None, "nick": "", "msg": ""}
    logged = False
    login_detail = ""
    with sync_playwright() as pw:
        ctx, exe = launch_persistent(pw, PROFILE, headless=False)
        log_msg("浏览器：%s" % exe)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def _on_resp(resp):
            try:
                if "basics/panelInfo" not in resp.url:
                    return
                j = json.loads(resp.text())
                state["code"] = j.get("code")
                state["msg"] = str(j.get("msg") or "")
                u = ((j.get("data") or {}).get("userInfo") or {})
                if u.get("nn"):
                    state["nick"] = u["nn"]
            except Exception:
                pass

        page.on("response", _on_resp)
        # 先开首页（首页有明确的「登录」入口，未登录时不会只显示“加载失败”）
        try:
            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        log_msg("请扫码登录。登录成功后本程序会提示「登录成功」，届时再关闭浏览器窗口。")

        def _acf():
            """当前 acf_auth（登录凭据）。用变化来判断「刚刚登录成功」——"""
            try:
                for c in ctx.cookies():
                    if c.get("name") == "acf_auth" and "douyu" in (c.get("domain") or ""):
                        return c.get("value") or ""
            except Exception:
                pass
            return ""

        base_acf = _acf()
        checked_acf = base_acf
        t0 = time.time()
        last_tip = 0.0
        while time.time() - t0 < 600:
            # 首页没有 panelInfo，靠登录凭据变化触发一次「跳到游戏页用 DOM 确认」
            if not logged:
                cur = _acf()
                if cur and cur != checked_acf:
                    checked_acf = cur
                    log_msg("检测到新的登录凭据，正在确认…")
                    try:
                        page.goto(GAME_URL.format(rid=rid),
                                  wait_until="domcontentloaded", timeout=60000)
                        ok2, det2 = await_login_state(page, timeout=30, stable=3)
                    except Exception as e:
                        ok2, det2 = False, ""
                        log_msg("确认失败：%s" % str(e)[:100], "warn")
                    if ok2:
                        logged = True
                        login_detail = det2
                        log_msg("✅ 登录成功：%s（现在可以关闭浏览器窗口了）" % det2)
                    else:
                        log_msg("登录凭据已更新，但游戏页仍不可用（%s），正在退回登录页，请重试"
                                % (state["code"] or "页面加载失败"), "warn")
                        try:
                            page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                        except Exception:
                            pass
            if not logged and (time.time() - last_tip) > 20:
                last_tip = time.time()
                log_msg("仍在等待登录…（%s）"
                        % ("服务端返回未登录" if state["code"] == 1002 else "等待扫码"), "warn")
            try:
                ctx.wait_for_event("close", timeout=2000)
                break
            except PWTimeoutError:
                pass
            except Exception:
                break

        # 关窗之前做最后一次确认：切到游戏页，稳定判定
        if not logged:
            try:
                log_msg("正在复核登录态…")
                page.goto(GAME_URL.format(rid=rid), wait_until="domcontentloaded", timeout=60000)
                ok2, det2 = await_login_state(page, timeout=30, stable=3)
                if ok2:
                    logged = True
                    login_detail = det2
                    log_msg("✅ 登录成功：%s" % det2)
            except Exception as e:
                log_msg("复核失败：%s" % str(e)[:100], "warn")

        try:
            ctx.close()
        except Exception:
            pass
        time.sleep(0.8)      # 给 Chrome 一点时间把 cookie 落盘

    if logged:
        log_msg("✅ 登录成功并已保存（%s）。现在可以点「开始挂机」了。" % (login_detail or "已登录"))
    else:
        if state["code"] == 1002:
            log_msg("登录未完成：服务端仍返回「用户未登录」（code 1002）。"
                    "常见原因：① 扫码后没等跳转就关了窗口；② 同一账号在别处登录，"
                    "把这里的会话挤掉了。请重新点「登录斗鱼」，等到提示「登录成功」再关窗口。", "error")
        else:
            log_msg("未检测到登录成功（%s）。请重新点「登录斗鱼」再试一次。"
                    % (state["code"] or "游戏页显示加载失败"), "error")
    return logged


# ---------------------------------------------------------------- selftest
def selftest():
    ensure_dirs()
    selftest_path = os.path.join(DATA_DIR, "selftest.txt")
    lines = []

    def emit(s):
        lines.append(str(s))
        try:
            with open(selftest_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
        log_msg(s)

    emit("selftest @ %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    emit("APP_DIR=%s" % APP_DIR)
    emit("DATA_DIR=%s" % DATA_DIR)
    emit("PROFILE=%s" % PROFILE)
    try:
        cands = chrome_candidates()
        emit("浏览器候选(%d):" % len(cands))
        for p, r, n in cands:
            emit("  rank=%d %s | %s" % (r, n, p))
    except Exception as e:
        emit("chrome_candidates FAIL: %s" % str(e)[:300])
    emit("driver: %s" % DRIVER_NAME)
    emit("import %s ..." % DRIVER_NAME)
    try:
        emit("%s import: OK" % DRIVER_NAME)
        emit("sync_playwright().start() ...")
        with sync_playwright() as pw:
            emit("driver started")
            ctx, exe = launch_persistent(pw, PROFILE, headless=True)
            emit("launch_persistent: OK (%s)" % exe)
            p = ctx.pages[0] if ctx.pages else ctx.new_page()
            p.goto("about:blank", timeout=15000)
            emit("about:blank: OK")
            # 防封自检：webdriver / UA
            try:
                wd = p.evaluate("() => navigator.webdriver")
                ua = p.evaluate("() => navigator.userAgent")
                emit("navigator.webdriver: %r" % wd)
                emit("userAgent: %s" % ua)
                emit("UA 含 HeadlessChrome: %s" % ("HeadlessChrome" in (ua or "")))
                emit("webdriver 检查: %s" % ("PASS" if not wd else "FAIL"))
                emit("UA 检查: %s" % ("PASS" if "HeadlessChrome" not in (ua or "") else "FAIL"))
            except Exception as e:
                emit("反检测自检失败: %s" % str(e)[:200])
            ctx.close()
            emit("selftest: ALL OK")
    except Exception as e:
        emit("%s/launch FAIL: %s" % (DRIVER_NAME, str(e)[:400]))
        emit(traceback.format_exc()[-600:])
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main():
    import argparse
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--run", action="store_true", help="命令行挂机")
    ap.add_argument("--login", action="store_true", help="打开浏览器登录斗鱼")
    ap.add_argument("--selftest", action="store_true", help="自检（driver/浏览器）")
    ap.add_argument("--rid", default=DEFAULT_RID, help="房间号（默认 %s）" % DEFAULT_RID)
    ap.add_argument("--rounds", type=int, default=10, help="场数（0=不限，打完免费次数后银币够就继续）")
    ap.add_argument("--gap-min", type=float, default=5, help="场间最小间隔秒")
    ap.add_argument("--gap-max", type=float, default=15, help="场间最大间隔秒")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--no-claim", action="store_true", help="不自动领取战斗宝箱")
    ap.add_argument("--no-hp", action="store_true", help="关闭「血量低于阈值自动回满」")
    ap.add_argument("--hp-threshold", type=int, default=5, help="回血阈值（血量低于该值回满，默认 5）")
    ap.add_argument("--no-coin", action="store_true", help="免费用尽后不用银币续打（默认会用，银币为免费获取）")
    ap.add_argument("--one-hit", action="store_true", help="秒杀模式：我方卡牌伤害直接击杀怪物")
    ap.add_argument("--daily-limit", type=int, default=240,
                    help="日活跃上限分钟（0=不限，默认 240）；超限后长休 --rest-min 分钟")
    ap.add_argument("--rest-min", type=int, default=90, help="日上限后的长休分钟（默认 90）")
    ap.add_argument("--no-block-stats", action="store_true",
                    help="不拦截斗鱼通用埋点（默认拦截）")
    ap.add_argument("--no-fp-harden", action="store_true",
                    help="不注入指纹补丁（默认注入，正常环境下本身是 no-op）")
    ap.add_argument("--no-safe", action="store_true",
                    help="关闭低风险模式（默认开：伤害弱化/部分回血/单场<=5分钟）")
    ap.add_argument("--safe-kill-prob", type=float, default=0.12,
                    help="低风险模式下仍秒杀的概率（默认 0.12）")
    ap.add_argument("--max-battle-min", type=int, default=0,
                    help="单场最长等待分钟数（默认 0=不限，只在卡死时兜底）")
    ap.add_argument("--refill-cap-min", type=int, default=0,
                    help="回血多久后停手（分钟，默认 0=不限）")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return
    if args.login:
        ok = open_login(args.rid)
        print("登录成功" if ok else "登录未完成（详见上方日志）")
        return
    if args.run:
        ensure_dirs()
        logf = open(os.path.join(DATA_DIR, "run.log"), "a", encoding="utf-8")

        def sink(line, level="info"):
            try:
                logf.write(line + "\n")
                logf.flush()
            except Exception:
                pass

        bot = GuardFishBot(rid=args.rid, rounds=args.rounds,
                           gap_min=args.gap_min, gap_max=args.gap_max,
                           headless=args.headless, claim_weekpass=not args.no_claim,
                           hp_guard=not args.no_hp, hp_threshold=args.hp_threshold,
                           coin_play=not args.no_coin, one_hit=args.one_hit,
                           daily_limit_min=args.daily_limit, rest_min=args.rest_min,
                           block_stats=not args.no_block_stats,
                           fp_harden=not args.no_fp_harden,
                           safe_mode=False if args.no_safe else None,
                           safe_kill_prob=args.safe_kill_prob,
                           max_battle_min=args.max_battle_min,
                           refill_cap_min=args.refill_cap_min)
        bot.add_log_sink(sink)
        bot._log("命令行挂机启动：房间=%s 场数=%d%s%s" % (args.rid, args.rounds,
                 " [headless]" if args.headless else "",
                 " [秒杀模式]" if args.one_hit else ""))
        bot.start()
        try:
            while bot._thread and bot._thread.is_alive():
                bot._thread.join(timeout=1)
        except KeyboardInterrupt:
            bot.request_stop()
            bot.stop()
        logf.close()
        return

    print("保卫鱼娘 · 自动挂机助手")
    print("用法：")
    print("  --login          打开浏览器登录斗鱼（首次/掉登录后）")
    print("  --run            命令行挂机（--rounds N --rid 12306 --headless）")
    print("  --one-hit        秒杀模式：我方卡牌伤害直接击杀怪物")
    print("  --daily-limit N  日活跃上限分钟（0=不限，默认 240），超限长休")
    print("  --rest-min N     日上限后的长休分钟（默认 90）")
    print("  --no-hp          关闭血量低于阈值自动回满")
    print("  --no-coin        免费用尽后不用银币续打")
    print("  --selftest       环境自检")
    print("图形界面请运行「保卫鱼娘助手.exe」或 gui.py")


if __name__ == "__main__":
    main()
