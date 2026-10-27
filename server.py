#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音续火花 Web 工具 —— 本地服务端

工作方式：
    1. 启动一个本地 Web 服务（默认 http://127.0.0.1:8765）
    2. 由 Playwright 拉起一个「用户可见」的独立 Chromium 窗口
    3. 你在那个窗口里扫码登录抖音，登录态存到 ./profile ，下次不用再扫
    4. 工具读取抖音网页版消息页的会话列表，展示在网页上
    5. 你勾选联系人与话术，点发送，工具在浏览器里逐条替你发

不做什么：
    - 不逆向抖音登录接口、不伪造签名、不调私有 API
    - 不碰你的账号密码（全程你自己扫码）
    - 不做无上限群发（内置条数与间隔限制）

风险提示：
    任何 UI 自动化都可能触发平台风控（限流 / 强制下线 / 封号）。
    请优先用小号试，发送间隔不要低于 8 秒。
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import random
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

APP_DIR = Path(__file__).resolve().parent
PROFILE_DIR = APP_DIR / "profile"
RUNTIME_DIR = APP_DIR / "runtime"
STATIC_DIR = APP_DIR / "static"
CONFIG_PATH = APP_DIR / "config.json"
SCHED_PATH = RUNTIME_DIR / "schedule.json"

STOP_EVENT = threading.Event()

HOST = "127.0.0.1"
PORT = 8765

DOUYIN_HOME = "https://www.douyin.com/"
MESSAGES_URLS = [
    "https://www.douyin.com/messages?from_tab_name=main",
    "https://www.douyin.com/messages",
    "https://www.douyin.com/chat",
]

DEFAULT_CONFIG = {
    "port": 8765,
    "max_batch": 20,
    "min_interval": 8,
    "default_interval": 10,
    "messages": [
        "早啊，今天也要好好吃饭",
        "在忙啥呢，别熬夜",
        "晚安，明天见",
    ],
}

# ---------------------------------------------------------------- 运行时状态

LOCK = threading.Lock()
LOG_BUFFER = []
JOB = {
    "id": None,
    "state": "idle",
    "total": 0,
    "done": 0,
    "ok": 0,
    "fail": 0,
    "logs": [],
    "results": [],
    "started_at": None,
    "finished_at": None,
    "source": "manual",
}


def log(msg, level="info"):
    entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "msg": str(msg),
    }
    with LOCK:
        LOG_BUFFER.append(entry)
        if len(LOG_BUFFER) > 500:
            del LOG_BUFFER[:200]
    print("[{}] {}".format(entry["ts"], entry["msg"]), flush=True)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
        except Exception as e:
            log("读取 config.json 失败，使用默认配置：{}".format(e), "warn")
    return cfg


def write_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("写入 config.json 失败：{}".format(e), "warn")


# ---------------------------------------------------------------- 浏览器工作线程


class BrowserWorker(threading.Thread):
    """Playwright 的 sync API 不能跨线程调用，所有操作都塞进这一个线程里串行跑。"""

    daemon = True

    def __init__(self):
        super().__init__(name="browser-worker")
        self.tasks = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        self.pw = None
        self.context = None
        self.page = None

    def run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:
            self.error = e
            self.ready.set()
            return
        try:
            with sync_playwright() as pw:
                self.pw = pw
                self.ready.set()
                while True:
                    item = self.tasks.get()
                    if item is None:
                        break
                    fn, args, holder, done = item
                    try:
                        holder["value"] = fn(self, *args)
                    except BaseException as e:
                        holder["error"] = e
                        holder["trace"] = traceback.format_exc()
                    finally:
                        done.set()
        except BaseException as e:
            self.error = e
            self.ready.set()
        finally:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass

    def _submit(self, fn, args):
        if not self.ready.wait(30):
            raise RuntimeError("浏览器工作线程启动超时")
        if self.error:
            raise RuntimeError("Playwright 启动失败：{!r}".format(self.error))
        holder, done = {}, threading.Event()
        self.tasks.put((fn, args, holder, done))
        return holder, done

    def call(self, fn, *args, timeout=180):
        holder, done = self._submit(fn, args)
        if not done.wait(timeout):
            raise RuntimeError("操作超时（{} 秒）".format(timeout))
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    def submit(self, fn, *args):
        self._submit(fn, args)

    def started(self):
        return self.context is not None

    def stop(self):
        self.tasks.put(None)


WORKER = BrowserWorker()


# ---------------------------------------------------------------- 页面操作


def _ensure_browser(w):
    if w.context is not None:
        try:
            if w.page is None or w.page.is_closed():
                alive = [p for p in w.context.pages if not p.is_closed()]
                w.page = alive[0] if alive else w.context.new_page()
            return w.page
        except Exception as e:
            log("浏览器上下文已失效，正在重建：{}".format(e), "warn")
            try:
                w.context.close()
            except Exception:
                pass
            w.context = None
            w.page = None
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    log("正在启动独立浏览器窗口（登录态目录：{}）".format(PROFILE_DIR))
    w.context = w.pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        no_viewport=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    try:
        w.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
    except Exception:
        pass
    pages = w.context.pages
    w.page = pages[0] if pages else w.context.new_page()
    return w.page


def _is_logged_in(w):
    if w.context is None:
        return False
    try:
        cookies = w.context.cookies()
    except Exception:
        return False
    # 只认真正的登录态 cookie；游客也有 passport_csrf_token，不能当判据
    keys = {"sessionid", "sessionid_ss", "sid_tt"}
    for c in cookies:
        if c.get("name") in keys and c.get("value"):
            return True
    return False


def _goto(w, url, wait_ms=2500):
    page = w.page
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(wait_ms)
    return page


def _body_text(page, limit=3000):
    try:
        return page.inner_text("body")[:limit]
    except Exception:
        return ""


# 会话列表提取：先按常见标记，再按「同一 class 的高重复元素」启发式兜底
EXTRACT_JS = r"""
() => {
  const out = [];
  const seen = new Set();
  const push = (name, preview, el) => {
    name = (name || '').trim();
    if (!name || name.length > 40) return;
    if (seen.has(name)) return;
    seen.add(name);
    const r = el.getBoundingClientRect();
    if (r.width < 100 || r.height < 20) return;
    out.push({
      name: name,
      preview: (preview || '').trim().slice(0, 80),
      x: r.left + r.width / 2,
      y: r.top + r.height / 2
    });
  };

  let items = [...document.querySelectorAll('[data-e2e*="conversation" i], [data-e2e*="session" i], [data-e2e*="chat-item" i]')];
  if (items.length) {
    for (const el of items) {
      const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      if (lines.length) push(lines[0], lines.slice(1).join(' '), el);
    }
    if (out.length) return { items: out, strategy: 'data-e2e', count: out.length };
  }

  const groups = new Map();
  for (const el of document.querySelectorAll('div, li, a')) {
    const r = el.getBoundingClientRect();
    if (r.width < 150 || r.height < 44 || r.height > 140) continue;
    if (r.left > window.innerWidth * 0.55) continue;
    if (r.top < 40) continue;
    const txt = (el.innerText || '').trim();
    if (!txt || txt.length > 100) continue;
    if (!el.querySelector('img, canvas, [class*="avatar" i]')) continue;
    const key = typeof el.className === 'string' ? el.className : '';
    if (!key) continue;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(el);
  }
  let best = null;
  for (const [k, arr] of groups) {
    if (arr.length < 2) continue;
    if (!best || arr.length > best.arr.length) best = { k: k, arr: arr };
  }
  if (best) {
    for (const el of best.arr) {
      const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      if (lines.length) push(lines[0], lines.slice(1).join(' '), el);
    }
    if (out.length) return { items: out, strategy: 'class-group', key: best.k, count: out.length };
  }
  return { items: out, strategy: 'none', count: 0 };
}
"""


def _open_messages_page(w):
    page = _ensure_browser(w)
    if not _is_logged_in(w):
        return page, "未登录"
    last_url = None
    for url in MESSAGES_URLS:
        try:
            page = _goto(w, url, 3000)
        except Exception as e:
            log("打开 {} 失败：{}".format(url, e), "warn")
            continue
        last_url = page.url
        body = _body_text(page)
        data = page.evaluate(EXTRACT_JS)
        if data.get("count", 0) > 0:
            return page, "ok"
        if "暂不支持" in body or "下载抖音" in body or "请在App" in body:
            return page, "网页版消息功能受限，页面提示需在 App 内使用"
    return page, "未能读取会话列表（当前地址：{}）".format(last_url)


# -- 对外动作 --------------------------------------------------------


def act_open_login(w):
    page = _ensure_browser(w)
    if "douyin.com" not in (page.url or ""):
        page = _goto(w, DOUYIN_HOME, 3000)
    if _is_logged_in(w):
        return {"ok": True, "logged_in": True, "url": page.url}
    try:
        el = page.query_selector('button:has-text("登录")')
        if el and el.is_visible():
            el.click()
            page.wait_for_timeout(1200)
    except Exception:
        pass
    return {"ok": True, "logged_in": False, "url": page.url}


def act_status(w):
    if w.context is None:
        return {"browser": False, "logged_in": False, "url": None}
    return {
        "browser": True,
        "logged_in": _is_logged_in(w),
        "url": w.page.url if w.page else None,
    }


def act_contacts(w):
    page = _ensure_browser(w)
    if not _is_logged_in(w):
        return {"ok": False, "reason": "尚未登录，请先扫码登录抖音"}
    page, note = _open_messages_page(w)
    if note != "ok":
        return {"ok": False, "reason": note, "url": page.url}
    data = page.evaluate(EXTRACT_JS)
    items = data.get("items", [])
    log("读取到 {} 个会话（策略：{}）".format(len(items), data.get("strategy")))
    return {
        "ok": True,
        "count": len(items),
        "strategy": data.get("strategy"),
        "contacts": items,
        "url": page.url,
    }


def act_debug_dump(w):
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    page = _ensure_browser(w)
    if _is_logged_in(w):
        page, _ = _open_messages_page(w)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shot = RUNTIME_DIR / "debug_{}.png".format(stamp)
    html = RUNTIME_DIR / "debug_{}.html".format(stamp)
    try:
        page.screenshot(path=str(shot), full_page=False)
    except Exception as e:
        log("截图失败：{}".format(e), "warn")
    try:
        html.write_text(page.content(), encoding="utf-8")
    except Exception as e:
        log("保存 HTML 失败：{}".format(e), "warn")
    data = page.evaluate(EXTRACT_JS)
    return {
        "ok": True,
        "screenshot": str(shot),
        "html": str(html),
        "strategy": data.get("strategy"),
        "count": data.get("count"),
        "sample": data.get("items", [])[:10],
        "url": page.url,
    }


def _locate_input(page):
    selectors = [
        'div[contenteditable="true"]',
        '[contenteditable="true"]',
        'textarea',
        '[data-e2e*="chat-input" i]',
        '[class*="chat-input" i] input',
        '[class*="editor" i] [contenteditable="true"]',
    ]
    for sel in selectors:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    return None


def _do_send_one(w, name, text):
    page = w.page
    data = page.evaluate(EXTRACT_JS)
    items = data.get("items", [])
    target = None
    for it in items:
        if it["name"] == name:
            target = it
            break
    if target is None:
        for it in items:
            if name and name in it["name"]:
                target = it
                break
    if target is None:
        return {"name": name, "ok": False, "reason": "会话列表中未找到该联系人"}

    page.mouse.click(target["x"], target["y"])
    page.wait_for_timeout(1500)

    box = _locate_input(page)
    if box is None:
        return {"name": name, "ok": False, "reason": "未定位到输入框"}
    try:
        box.click()
        page.wait_for_timeout(250)
        page.keyboard.insert_text(text)
        page.wait_for_timeout(350)
    except Exception as e:
        return {"name": name, "ok": False, "reason": "输入失败：{}".format(e)}

    clicked = False
    try:
        for sel in ['button:has-text("发送")', '[data-e2e*="send" i]']:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                clicked = True
                break
    except Exception:
        pass
    if not clicked:
        page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    return {"name": name, "ok": True, "text": text}


def _set_job(**kw):
    with LOCK:
        JOB.update(kw)


def _job_log(entry):
    with LOCK:
        JOB["logs"].append(entry)
        JOB["done"] = entry.get("index", JOB["done"])


def act_run_job(w, targets, messages, mode, interval, source="manual"):
    with LOCK:
        JOB.update({
            "id": "job_{}".format(int(time.time())),
            "state": "running",
            "total": len(targets),
            "done": 0,
            "ok": 0,
            "fail": 0,
            "logs": [],
            "results": [],
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None,
            "source": source,
        })

    page = _ensure_browser(w)
    if not _is_logged_in(w):
        _set_job(state="error", finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _job_log({"level": "error", "msg": "未登录，任务中止"})
        return {"ok": False, "reason": "未登录"}

    page, note = _open_messages_page(w)
    if note != "ok":
        _set_job(state="error", finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _job_log({"level": "error", "msg": "无法进入消息页：{}".format(note)})
        return {"ok": False, "reason": note}

    ok = fail = 0
    for idx, name in enumerate(targets):
        text = messages[0] if mode == "fixed" else random.choice(messages)
        try:
            res = _do_send_one(w, name, text)
        except Exception as e:
            res = {"name": name, "ok": False, "reason": str(e)}
        if res.get("ok"):
            ok += 1
            _job_log({"index": idx + 1, "level": "ok",
                      "msg": "已发送给 {}：{}".format(name, text)})
        else:
            fail += 1
            _job_log({"index": idx + 1, "level": "fail",
                      "msg": "{} 发送失败：{}".format(name, res.get("reason"))})
        with LOCK:
            JOB["results"].append(res)
            JOB["done"] = idx + 1
            JOB["ok"] = ok
            JOB["fail"] = fail
        if idx < len(targets) - 1:
            time.sleep(interval)

    _set_job(state="done", ok=ok, fail=fail,
             finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    log("任务完成：成功 {}，失败 {}".format(ok, fail))
    return {"ok": True, "ok_count": ok, "fail_count": fail}


# ---------------------------------------------------------------- 定时任务


def load_sched():
    if SCHED_PATH.exists():
        try:
            data = json.loads(SCHED_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tasks"), list):
                return data
        except Exception as e:
            log("读取 schedule.json 失败：{}".format(e), "warn")
    return {"tasks": []}


def save_sched(data):
    try:
        SCHED_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log("写入 schedule.json 失败：{}".format(e), "warn")


def scheduler_loop():
    log("定时调度器已启动（每 20 秒检查一次）")
    while not STOP_EVENT.is_set():
        try:
            scheduler_tick()
        except Exception as e:
            log("调度器异常：{}".format(e), "warn")
        STOP_EVENT.wait(20)


def scheduler_tick():
    now = datetime.now()
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    data = load_sched()
    dirty = False
    for task in data.get("tasks", []):
        if not task.get("enabled"):
            continue
        if (task.get("time") or "").strip() != hhmm:
            continue
        if task.get("last_run") == today:
            continue
        with LOCK:
            if JOB["state"] == "running":
                log("定时任务到点，但当前有任务在跑，本日跳过", "warn")
                continue
        task["last_run"] = today
        dirty = True
        fire_task(task)
    if dirty:
        save_sched(data)


def fire_task(task):
    targets = [str(t).strip() for t in (task.get("targets") or []) if str(t).strip()]
    messages = [str(m).strip() for m in (task.get("messages") or []) if str(m).strip()]
    if not targets or not messages:
        log("定时任务 {} 缺少联系人或话术，跳过".format(task.get("time")), "warn")
        return
    cfg = load_config()
    cap = int(cfg.get("max_batch") or 20)
    if len(targets) > cap:
        targets = targets[:cap]
        log("定时任务联系人超过 {} 人，已截断".format(cap), "warn")
    try:
        interval = int(task.get("interval") or cfg["default_interval"])
    except Exception:
        interval = int(cfg["default_interval"])
    interval = max(interval, int(cfg["min_interval"]))

    log("定时任务触发：{} 位联系人，间隔 {}s".format(len(targets), interval))
    try:
        WORKER.submit(act_run_job, targets, messages,
                      task.get("mode") or "random", interval, "schedule")
    except Exception as e:
        log("定时任务提交失败：{}".format(e), "fail")


def _new_task_id():
    return "t{}".format(int(time.time() * 1000) % 100000000)


# ---------------------------------------------------------------- HTTP 服务


def json_dumps(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinSparkWeb/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json_dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._serve_static("index.html")
        if path.startswith("/static/"):
            return self._serve_static(path[len("/static/"):])
        if path == "/api/status":
            return self._api_status()
        if path == "/api/progress":
            return self._send(200, {"ok": True, "job": self._job_snapshot(),
                                    "logs": LOG_BUFFER[-200:]})
        if path == "/api/config":
            return self._send(200, load_config())
        if path == "/api/schedule":
            return self._api_sched_list()
        return self._send(404, {"ok": False, "reason": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            return self._api_login()
        if path == "/api/contacts":
            return self._api_contacts()
        if path == "/api/send":
            return self._api_send()
        if path == "/api/debug":
            return self._api_debug()
        if path == "/api/reveal":
            return self._api_reveal()
        if path == "/api/schedule/save":
            return self._api_sched_save()
        if path == "/api/schedule/delete":
            return self._api_sched_delete()
        return self._send(404, {"ok": False, "reason": "not found"})

    def _serve_static(self, rel):
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.exists():
            return self._send(404, {"ok": False, "reason": "missing file"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        try:
            data = target.read_bytes()
        except Exception as e:
            return self._send(500, {"ok": False, "reason": str(e)})
        return self._send(200, data, ctype)

    def _api_status(self):
        try:
            if not WORKER.started():
                return self._send(200, {"browser": False, "logged_in": False,
                                        "job": self._job_snapshot()})
            st = WORKER.call(act_status, timeout=20)
            st["job"] = self._job_snapshot()
            st["ok"] = True
            return self._send(200, st)
        except Exception as e:
            return self._send(200, {"browser": WORKER.started(), "logged_in": False,
                                    "ok": False, "reason": str(e),
                                    "job": self._job_snapshot()})

    def _api_login(self):
        try:
            return self._send(200, WORKER.call(act_open_login, timeout=90))
        except Exception as e:
            return self._send(200, {"ok": False, "reason": str(e)})

    def _api_contacts(self):
        try:
            return self._send(200, WORKER.call(act_contacts, timeout=120))
        except Exception as e:
            return self._send(200, {"ok": False, "reason": str(e)})

    def _api_debug(self):
        try:
            return self._send(200, WORKER.call(act_debug_dump, timeout=120))
        except Exception as e:
            return self._send(200, {"ok": False, "reason": str(e)})

    def _api_reveal(self):
        try:
            os.startfile(str(APP_DIR))
            return self._send(200, {"ok": True, "path": str(APP_DIR)})
        except Exception as e:
            return self._send(200, {"ok": False, "reason": str(e)})

    def _api_send(self):
        body = self._read_json()
        cfg = load_config()
        targets = [str(t).strip() for t in (body.get("targets") or []) if str(t).strip()]
        messages = [str(m).strip() for m in (body.get("messages") or []) if str(m).strip()]
        mode = body.get("mode") or "random"
        try:
            interval = int(body.get("interval") or cfg["default_interval"])
        except Exception:
            interval = cfg["default_interval"]

        if not targets:
            return self._send(200, {"ok": False, "reason": "没有选择联系人"})
        if not messages:
            return self._send(200, {"ok": False, "reason": "话术不能为空"})
        if len(targets) > cfg["max_batch"]:
            return self._send(200, {"ok": False,
                                    "reason": "单次最多 {} 个联系人，当前 {}".format(cfg["max_batch"], len(targets))})
        if interval < cfg["min_interval"]:
            return self._send(200, {"ok": False,
                                    "reason": "间隔不能低于 {} 秒（太低易触发风控）".format(cfg["min_interval"])})

        with LOCK:
            if JOB["state"] == "running":
                return self._send(200, {"ok": False, "reason": "已有任务在运行"})

        WORKER.submit(act_run_job, targets, messages, mode, interval)
        log("已提交发送任务：{} 个联系人，模式 {}，间隔 {}s".format(len(targets), mode, interval))
        return self._send(200, {"ok": True, "accepted": True, "total": len(targets)})

    def _api_sched_list(self):
        data = load_sched()
        now = datetime.now()
        for t in data.get("tasks", []):
            t["scheduled_today"] = (t.get("last_run") == now.strftime("%Y-%m-%d"))
        return self._send(200, {"ok": True, "tasks": data.get("tasks", []),
                                "now": now.strftime("%Y-%m-%d %H:%M:%S")})

    def _api_sched_save(self):
        body = self._read_json()
        hhmm = str(body.get("time") or "").strip()
        if not (len(hhmm) == 5 and hhmm[2] == ":"):
            return self._send(200, {"ok": False, "reason": "时间格式应为 HH:MM"})
        try:
            hh, mm = int(hhmm[:2]), int(hhmm[3:])
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
        except Exception:
            return self._send(200, {"ok": False, "reason": "时间不合法"})

        targets = [str(t).strip() for t in (body.get("targets") or []) if str(t).strip()]
        messages = [str(m).strip() for m in (body.get("messages") or []) if str(m).strip()]
        if not targets:
            return self._send(200, {"ok": False, "reason": "请先勾选联系人"})
        if not messages:
            return self._send(200, {"ok": False, "reason": "话术不能为空"})

        cfg = load_config()
        cap = int(cfg.get("max_batch") or 20)
        if len(targets) > cap:
            return self._send(200, {"ok": False,
                                    "reason": "单次最多 {} 人，当前 {}".format(cap, len(targets))})
        try:
            interval = max(int(body.get("interval") or cfg["default_interval"]),
                           int(cfg["min_interval"]))
        except Exception:
            interval = int(cfg["default_interval"])

        data = load_sched()
        tid = body.get("id")
        task = {
            "id": tid or _new_task_id(),
            "time": hhmm,
            "targets": targets,
            "messages": messages,
            "mode": body.get("mode") or "random",
            "interval": interval,
            "enabled": bool(body.get("enabled", True)),
            "last_run": "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        replaced = False
        for i, t in enumerate(data["tasks"]):
            if t.get("id") == task["id"]:
                task["last_run"] = t.get("last_run") or ""
                task["created_at"] = t.get("created_at") or task["created_at"]
                data["tasks"][i] = task
                replaced = True
                break
        if not replaced:
            data["tasks"].append(task)
        save_sched(data)
        log("已保存定时任务：每天 {}，{} 位联系人".format(hhmm, len(targets)))
        return self._send(200, {"ok": True, "task": task})

    def _api_sched_delete(self):
        body = self._read_json()
        tid = body.get("id")
        data = load_sched()
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"] if t.get("id") != tid]
        save_sched(data)
        log("已删除定时任务 {}".format(tid))
        return self._send(200, {"ok": True, "removed": before - len(data["tasks"])})

    def _job_snapshot(self):
        with LOCK:
            return {
                "id": JOB["id"],
                "state": JOB["state"],
                "total": JOB["total"],
                "done": JOB["done"],
                "ok": JOB["ok"],
                "fail": JOB["fail"],
                "logs": JOB["logs"][-200:],
                "started_at": JOB["started_at"],
                "finished_at": JOB["finished_at"],
                "source": JOB.get("source", "manual"),
            }


# ---------------------------------------------------------------- 入口


def preflight():
    """启动前检查依赖，避免用户点了按钮才发现解释器不对。"""
    try:
        import playwright  # noqa: F401
    except Exception:
        print(
            "\n[启动失败] 当前 Python 没有安装 playwright。\n"
            "  当前解释器: {}\n\n"
            "  解决办法二选一：\n"
            "    1) 直接双击 run.bat，它会自动挑一个装了 playwright 的 Python\n"
            "    2) 在这个解释器下执行：\n"
            "         python -m pip install playwright\n"
            "         python -m playwright install chromium\n".format(sys.executable),
            file=sys.stderr,
            flush=True,
        )
        return False
    return True


def main():
    global PORT
    if not preflight():
        sys.exit(2)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    if not CONFIG_PATH.exists():
        write_config(cfg)
    PORT = int(cfg.get("port") or PORT)

    WORKER.start()
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()

    url = "http://{}:{}/".format(HOST, PORT)
    log("=" * 56)
    log("抖音续火花 Web 工具已启动：{}".format(url))
    log("解释器：{}".format(sys.executable))
    log("登录态目录：{}".format(PROFILE_DIR))
    log("关闭本窗口即可停止服务")
    log("=" * 56)

    if os.environ.get("DOUYIN_NO_BROWSER") != "1":
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:
            pass

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在退出")
    finally:
        STOP_EVENT.set()
        try:
            WORKER.stop()
        except Exception:
            pass
        httpd.server_close()


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
