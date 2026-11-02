# -*- coding: utf-8 -*-
"""保卫鱼娘 · 自动挂机助手（桌面 GUI）

用法：python yu_gui.py  或双击生成的 exe
"""
import os
import re
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import bot as botmod  # noqa: E402

LOG_Q = queue.Queue()
FONT = ("Microsoft YaHei UI", 10)
FONT_B = ("Microsoft YaHei UI", 10, "bold")
FONT_S = ("Microsoft YaHei UI", 9)


class App:
    def __init__(self, root):
        self.root = root
        self.running = False
        self.thread = None
        self.bot = None
        root.title("保卫鱼娘 · 自动挂机助手")
        root.geometry("1020x660")
        root.minsize(940, 560)
        self._build()
        self._log("[系统] 就绪。首次使用请先点「登录斗鱼」登录账号（登录态保存在本机）。")
        self._log("[系统] 数据目录：%s" % botmod.DATA_DIR)
        self._log("[系统] 说明：只执行免费操作；绝技/加速/技能刷新等付费功能不会自动点击。")
        cands = botmod.chrome_candidates()
        if not cands:
            self._log("[警告] 未检测到 Chrome/Edge，请先安装 Google Chrome", "warn")
        else:
            self._log("[系统] 使用浏览器：%s" % cands[0][0], "dim")
        self.root.after(150, self._pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text="房间号", font=FONT).grid(row=0, column=0, sticky="w")
        self.rid = tk.StringVar(value=botmod.DEFAULT_RID)
        ttk.Entry(top, textvariable=self.rid, width=10, font=FONT).grid(row=0, column=1, padx=(4, 14))

        ttk.Label(top, text="场数(0=不限)", font=FONT).grid(row=0, column=2, sticky="w")
        self.rounds = tk.StringVar(value="0")
        ttk.Entry(top, textvariable=self.rounds, width=6, font=FONT).grid(row=0, column=3, padx=(4, 14))

        ttk.Label(top, text="场间等待(秒)", font=FONT).grid(row=0, column=4, sticky="w")
        self.gap_min = tk.StringVar(value="5")
        self.gap_max = tk.StringVar(value="15")
        ttk.Entry(top, textvariable=self.gap_min, width=5, font=FONT).grid(row=0, column=5, padx=(4, 2))
        ttk.Label(top, text="~", font=FONT).grid(row=0, column=6)
        ttk.Entry(top, textvariable=self.gap_max, width=5, font=FONT).grid(row=0, column=7, padx=(2, 14))

        # 回血守护：血量低于阈值自动回满
        hp_frame = ttk.Frame(top)
        hp_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.hp_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(hp_frame, text="血量低于", variable=self.hp_var).pack(side="left")
        self.hp_threshold = tk.StringVar(value="5")
        ttk.Entry(hp_frame, textvariable=self.hp_threshold, width=4, font=FONT).pack(side="left", padx=3)
        ttk.Label(hp_frame, text="滴自动回满", font=FONT_S, foreground="#666").pack(side="left")

        # 银币续打：免费用尽后花银币继续（银币免费获取，不用鱼翅）
        self.coin_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="免费用尽后用银币续打（银币免费获得）",
                        variable=self.coin_var).grid(row=1, column=4, columnspan=5, sticky="w", pady=(8, 0))

        # 秒杀模式：我方 9 张卡造成的任何伤害直接击杀怪物
        self.one_hit_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="秒杀模式（卡牌伤害直接击杀）",
                        variable=self.one_hit_var).grid(row=1, column=9, columnspan=4,
                                                        sticky="w", pady=(8, 0))

        self.headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="后台运行(隐藏浏览器)", variable=self.headless_var).grid(row=0, column=8, padx=(0, 14))

        self.start_btn = tk.Button(top, text="开始挂机", width=11, font=FONT_B,
                                   bg="#43a047", fg="white", command=self.start)
        self.start_btn.grid(row=0, column=9, padx=(0, 8))
        self.stop_btn = tk.Button(top, text="停止", width=7, font=FONT_B,
                                  state="disabled", command=self.stop)
        self.stop_btn.grid(row=0, column=10, padx=(0, 14))

        ttk.Button(top, text="登录斗鱼", width=10, command=self.login).grid(row=0, column=11, padx=(0, 6))
        ttk.Button(top, text="打开数据目录", width=12, command=self.open_dir).grid(row=0, column=12)

        # 防封：日活跃上限（到点长休，避免 7x24 特征）
        lim = ttk.Frame(top)
        lim.grid(row=2, column=0, columnspan=13, sticky="w", pady=(8, 0))
        ttk.Label(lim, text="每日活跃上限", font=FONT).pack(side="left")
        self.daily_limit = tk.StringVar(value="240")
        ttk.Entry(lim, textvariable=self.daily_limit, width=5, font=FONT).pack(side="left", padx=(4, 2))
        ttk.Label(lim, text="分钟，超限休息", font=FONT).pack(side="left")
        self.rest_min = tk.StringVar(value="90")
        ttk.Entry(lim, textvariable=self.rest_min, width=4, font=FONT).pack(side="left", padx=(4, 2))
        ttk.Label(lim, text="分钟（0=不限）", font=FONT).pack(side="left")
        ttk.Label(lim, text="· 驱动：%s" % botmod.DRIVER_NAME,
                  font=FONT_S, foreground="#2e7d32").pack(side="left", padx=(14, 0))

        tips = ttk.Label(top, text="每日免费挑战 10 次；免费用尽后若银币 ≥10 会自动用银币续打（银币为免费获取；"
                                   "绝技/加速等需鱼翅付费的功能一律不会点击）。"
                                   "中途停止或关闭会导致当前一场作废，建议打完一场再停。",
                         font=FONT_S, foreground="#666")
        tips.grid(row=3, column=0, columnspan=13, sticky="w", pady=(6, 0))

        self.status = tk.StringVar(value="状态：未运行")
        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(10, 5),
                  background="#f0f0f0", font=FONT_B).pack(fill="x", pady=(6, 0))

        frame = ttk.Frame(self.root, padding=(10, 8, 10, 10))
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(frame, wrap="none", height=22, font=("Consolas", 10))
        sb = ttk.Scrollbar(frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, color in (("warn", "#ef6c00"), ("error", "#c62828"),
                           ("win", "#2e7d32"), ("dim", "#888")):
            self.text.tag_config(tag, foreground=color)

    # ---------- 日志 ----------
    def _log(self, msg, tag=None):
        self.text.insert("end", msg + "\n", tag)
        self.text.see("end")
        try:
            if int(self.text.index("end-1c").split(".")[0]) > 3000:
                self.text.delete("1.0", "800.0")
        except Exception:
            pass

    def _pump(self):
        try:
            while True:
                item = LOG_Q.get_nowait()
                if isinstance(item, tuple):
                    line, level = item
                else:
                    line, level = item, None
                tag = None
                if level == "warn" or "!!" in line or "警告" in line:
                    tag = "warn"
                elif level == "error" or "异常" in line or "失败" in line:
                    tag = "error"
                elif "完成" in line or "结束" in line and "运行" in line:
                    tag = "win"
                self._log(line, tag)
                m = re.search(r"第 (\d+)/(\d+) 场", line)
                if m:
                    self._round = "%s/%s" % (m.group(1), m.group(2))
                m2 = re.search(r"进行中 t=(\d+)s 第(\S+)关 HP=(\S+) 免费剩余=(\S+)", line)
                if m2:
                    self._hud = ("进行中 %ss｜第%s关｜HP %s｜免费剩余 %s"
                                 % (m2.group(1), m2.group(2), m2.group(3), m2.group(4)))
                m3 = re.search(r"战报：起始第(\S+)关 → 到达第(\S+)关，用时 (\d+) 秒", line)
                if m3:
                    self._hud = "上场：第%s→%s关，用时%ss" % (m3.group(1), m3.group(2), m3.group(3))
                if self.running:
                    b = self.bot
                    st = b.status if b else {}
                    self.status.set("运行中 · 第 %s 场｜%s｜状态 %s｜回血 %s 次｜银币 %s｜天梯排名 %s"
                                    % (st.get("round", "-"), getattr(self, "_hud", "启动中"),
                                       st.get("state", "-"), st.get("refills", 0),
                                       st.get("coin", "-"), st.get("weekRank", "-")))
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(150, self._pump)

    # ---------- 控制 ----------
    def start(self):
        if self.running:
            return
        if not botmod.chrome_candidates():
            messagebox.showerror("缺少浏览器",
                                 "未检测到 Chrome / Edge。\n请先安装 Google Chrome，"
                                 "或设置环境变量 CHROME_PATH 指向 chrome.exe")
            return
        try:
            rid = (self.rid.get() or botmod.DEFAULT_RID).strip()
            rounds = int((self.rounds.get() or "0").strip() or 0)
            gap_min = float((self.gap_min.get() or "0").strip() or 0)
            gap_max = float((self.gap_max.get() or "0").strip() or 0)
            hp_th = int((self.hp_threshold.get() or "5").strip() or 5)
            daily_lim = int((self.daily_limit.get() or "240").strip() or 0)
            rest_min = int((self.rest_min.get() or "90").strip() or 90)
        except ValueError:
            messagebox.showwarning("输入错误", "房间号/场数/等待时间/回血阈值/日上限格式不对")
            return
        if rounds < 0:
            messagebox.showwarning("输入错误", "场数不能为负数（0=不限）")
            return
        if hp_th < 1:
            hp_th = 1
        if daily_lim < 0:
            daily_lim = 0
        if rest_min < 1:
            rest_min = 1
        if gap_max < gap_min:
            gap_min, gap_max = gap_max, gap_min
            self.gap_min.set("%g" % gap_min)
            self.gap_max.set("%g" % gap_max)

        self.running = True
        self._hud = ""
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log("[系统] 开始挂机：房间 %s，场数 %s，场间等待 %.0f~%.0f 秒%s"
                  % (rid, rounds if rounds > 0 else "不限", gap_min, gap_max,
                     "（后台模式）" if self.headless_var.get() else ""))
        if self.hp_var.get():
            self._log("[系统] 回血守护：开（血量 < %d 自动回满）" % hp_th)
        self._log("[系统] 银币续打：%s" % ("开（免费用尽后银币≥10 自动继续）" if self.coin_var.get() else "关"))
        if self.one_hit_var.get():
            self._log("[系统] 秒杀模式：开（我方卡牌造成的任何伤害直接击杀怪物）")
        self._log("[系统] 日活跃上限：%s" % (("%d 分钟，超限休息 %d 分钟" % (daily_lim, rest_min))
                                            if daily_lim > 0 else "不限"))
        self.thread = threading.Thread(target=self._run,
                                       args=(rid, rounds, gap_min, gap_max, self.headless_var.get(),
                                             self.hp_var.get(), hp_th, self.coin_var.get(),
                                             self.one_hit_var.get(), daily_lim, rest_min),
                                       daemon=True)
        self.thread.start()

    def _run(self, rid, rounds, gap_min, gap_max, headless, hp_guard, hp_th, coin_play,
             one_hit=False, daily_lim=240, rest_min=90):
        try:
            self.bot = botmod.GuardFishBot(rid=rid, rounds=rounds, gap_min=gap_min,
                                           gap_max=gap_max, headless=headless,
                                           hp_guard=hp_guard, hp_threshold=hp_th,
                                           coin_play=coin_play, one_hit=one_hit,
                                           daily_limit_min=daily_lim, rest_min=rest_min)
            self.bot.add_log_sink(lambda line, level=None: LOG_Q.put((line, level)))
            self.bot.start()
            self.bot._thread.join()
        except Exception as e:
            LOG_Q.put(("[异常] %s" % e, "error"))
        finally:
            self.running = False
            self.root.after(0, self._on_finished)

    def _on_finished(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status.set("状态：已停止")
        self._log("[系统] 已停止")

    def login(self):
        if self.running:
            messagebox.showinfo("提示", "挂机运行中，请先停止再登录")
            return
        self._log("[系统] 正在打开登录窗口（扫码后等提示「登录成功」再关窗口）…")

        def run():
            ok = False
            try:
                ok = bool(botmod.open_login(self.rid.get() or botmod.DEFAULT_RID))
            except Exception as e:
                self.root.after(0, lambda: self._log("[异常] 登录失败：%s" % e, "error"))
                return
            if ok:
                self.root.after(0, lambda: self._log("[系统] 登录成功，可以开始挂机了。"))
            else:
                self.root.after(0, lambda: messagebox.showwarning(
                    "登录未完成",
                    "没有检测到登录成功。\n\n"
                    "常见原因：\n"
                    "1. 扫码后没等页面跳转就关了窗口\n"
                    "2. 同一账号在别处重新登录，把这里的会话挤掉了\n\n"
                    "请重新点「登录斗鱼」，扫码后等到提示「登录成功」再关窗口。"))
                self.root.after(0, lambda: self._log(
                    "[系统] 登录未完成，请重试（详见提示框）。", "warn"))
        threading.Thread(target=run, daemon=True).start()

    def open_dir(self):
        try:
            os.makedirs(botmod.DATA_DIR, exist_ok=True)
            os.startfile(botmod.DATA_DIR)
        except Exception as e:
            messagebox.showwarning("打不开", str(e))

    def stop(self):
        if self.bot:
            self.bot.request_stop()
            self._log("[系统] 停止中…（等待当前动作收尾；战斗中停止会导致本场作废）")
        self.stop_btn.config(state="disabled")

    def on_close(self):
        if self.running and self.bot:
            if not messagebox.askokcancel("退出", "挂机还在运行，确定退出？\n（战斗中退出会导致当前一场作废）"):
                return
            self.bot.request_stop()
        self.root.destroy()


def main():
    argv = sys.argv[1:]
    # 有命令行参数时走 bot.py 的 CLI（--run / --login / --selftest）
    if any(a.startswith("-") for a in argv):
        botmod.main()
        return
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.15)
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
