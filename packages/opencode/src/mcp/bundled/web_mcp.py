#!/usr/bin/env python3
"""Standalone MCP server (stdio, JSON-RPC 2.0, NDJSON) named `web`.

Exposes simple functions with typed args: web search (keyless DuckDuckGo
reimplementation), web fetch via Jina Reader, browser automation via
playwright/chromium, yt-dlp downloads, aria2 downloads, plus tool
status/install helpers. Missing binaries/packages are LAZY AUTO-INSTALLED
on first use; MCP startup never blocks on installs.

Protocol conventions (initialize, notifications/*, tools/list, tools/call,
error format, text content blocks) mirror memory_mcp.py so both servers
behave identically.
"""
import atexit
import glob
import html as htmlmod
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from html.parser import HTMLParser

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_INSTALL_LOCK = threading.Lock()
_INSTALLING = set()

_BROWSER = None
_PAGE = None
_PW = None
_CTX = None

# ---- FIX.doc expansion state (all lazy; browser still starts on first use) ----
_DEFAULT_TIMEOUT = 30000
_NETLOG = []          # [{event, method, url, status}]
_WSLOG = []           # [{url, direction, payload}]
_CONSOLE = []         # [{type, text}]
_DIALOGS = []         # [{type, message, default_value}]
_PENDING_DIALOGS = []  # live playwright dialog objects awaiting accept/dismiss
_OVERLAY_ACTION = None  # None | "accept" | "dismiss" (auto-handle dialogs)
_INIT_SCRIPTS = []    # scripts applied to every new context via add_init_script
_ROUTES = {}          # pattern -> {action, status, body, content_type}
_TRACING = False
_TRACE_OPTS = {}
_VIDEO_DIR = None
_MAXLOG = 2000


def log_err(msg):
    try:
        sys.stderr.write(str(msg) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _tail(text, n=1500):
    if not isinstance(text, str):
        text = str(text)
    return text[-n:]


def _run(cmd, timeout):
    """Run cmd, return (returncode, combined_output)."""
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired as e:
        out = ""
        try:
            if e.stdout:
                out += e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else str(e.stdout)
            if e.stderr:
                out += e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else str(e.stderr)
        except Exception:
            pass
        return 124, out + ("\n[TIMEOUT after %ss]" % timeout)
    except Exception as e:
        return 127, "failed to exec %s: %s" % (" ".join(cmd), e)


# ---------------- status helpers ----------------

def _playwright_status():
    spec = importlib.util.find_spec("playwright")
    if spec is None:
        return False, "python package 'playwright' not importable"
    try:
        import playwright  # noqa: F401
        return True, "python package 'playwright' importable"
    except Exception as e:
        return False, "playwright found but import failed: %s" % e


def _cache_homes():
    homes = []
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        homes.append(os.path.join(xdg, "ms-playwright"))
    bp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if bp:
        homes.append(bp)
    homes.append(os.path.expanduser("~/.cache/ms-playwright"))
    seen = []
    for h in homes:
        if h and h not in seen:
            seen.append(h)
    return seen


def _chromium_status():
    roots = _cache_homes() + ["/root/.cache/ms-playwright", "/ms-playwright"]
    pats = ("chromium-*/chrome-linux/chrome",
            "chromium-*/chrome-linux64/chrome",
            "chromium-*/chrome-linux/headless_shell",
            "chromium_headless_shell-*/chrome-linux/headless_shell",
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
            "chrome-linux/chrome",
            "chrome-linux64/chrome")
    found = []
    for home in roots:
        for pat in pats:
            try:
                found.extend(g for g in glob.glob(os.path.join(home, pat)) if os.path.isfile(g))
            except Exception:
                pass
    if found:
        return True, "chromium executable present: %s" % found[0]
    # Ground truth: ask playwright where it expects the binary.
    if importlib.util.find_spec("playwright") is not None:
        try:
            rc, out = _run([sys.executable, "-c",
                            "from playwright.sync_api import sync_playwright as _s;"
                            " _p=_s().start(); print(_p.chromium.executable_path); _p.stop()"], 60)
            if rc == 0:
                cand = out.strip().splitlines()[-1].strip()
                if cand and os.path.isfile(cand):
                    return True, "chromium executable present: %s" % cand
                # Headless-shell sibling under the same browsers root.
                root = cand
                for _ in range(3):
                    root = os.path.dirname(root)
                for rel in ("chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                            "chromium_headless_shell-*/chrome-linux/headless_shell"):
                    hits = [g for g in glob.glob(os.path.join(root, rel)) if os.path.isfile(g)]
                    if hits:
                        return True, "chromium executable present: %s" % hits[0]
        except Exception:
            pass
        return False, "no chromium executable found (checked XDG/HOME cache roots and playwright registry)"
    return False, "no chromium executable found (playwright pkg missing)"
    # Fallback: ask playwright for a dry run (only if package installed).
    if importlib.util.find_spec("playwright") is not None:
        rc, out = _run([sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"], 60)
        if rc == 0 and ("is already installed" in out or "chromium" in out.lower()):
            if "already installed" in out.lower() or "up to date" in out.lower():
                return True, "playwright reports chromium installed (dry-run)"
        return False, "no chromium executable under ~/.cache/ms-playwright"
    return False, "no chromium executable under ~/.cache/ms-playwright (playwright pkg missing)"


def _yt_dlp_status():
    cli = shutil.which("yt-dlp")
    mod = importlib.util.find_spec("yt_dlp")
    if cli:
        return True, "CLI found: %s" % cli
    if mod is not None:
        return True, "python module 'yt_dlp' importable (no CLI on PATH)"
    return False, "neither 'yt-dlp' CLI nor python module 'yt_dlp' found"


def _aria2_status():
    cli = shutil.which("aria2c")
    if cli:
        return True, "CLI found: %s" % cli
    return False, "'aria2c' not found on PATH"


def do_tools_status(args):
    return {
        "playwright": dict(zip(("installed", "detail"), _playwright_status())),
        "chromium": dict(zip(("installed", "detail"), _chromium_status())),
        "yt_dlp": dict(zip(("installed", "detail"), _yt_dlp_status())),
        "aria2c": dict(zip(("installed", "detail"), _aria2_status())),
    }


# ---------------- install helpers ----------------

def _pip_install(pkg):
    return _run([sys.executable, "-m", "pip", "install", "--quiet", pkg], 600)


def _install_playwright():
    logs = []
    rc, out = _pip_install("playwright")
    logs.append("$ %s -m pip install playwright\n%s" % (sys.executable, out))
    if rc != 0:
        return False, _tail("\n".join(logs))
    rc, out = _run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], 600)
    logs.append("$ playwright install --with-deps chromium\n%s" % out)
    if rc != 0:
        logs.append("NOTE: --with-deps failed (often apt issues); retrying without --with-deps")
        rc2, out2 = _run([sys.executable, "-m", "playwright", "install", "chromium"], 600)
        logs.append("$ playwright install chromium\n%s" % out2)
        rc = rc2
    if rc != 0:
        return False, _tail("\n".join(logs))
    ok_pw, _ = _playwright_status()
    ok_ch, _ = _chromium_status()
    if ok_pw and ok_ch:
        return True, _tail("\n".join(logs))
    logs.append("post-install check: playwright=%s chromium=%s" % (ok_pw, ok_ch))
    return (ok_pw and ok_ch), _tail("\n".join(logs))


def _install_yt_dlp():
    rc, out = _pip_install("yt-dlp")
    log = "$ %s -m pip install yt-dlp\n%s" % (sys.executable, out)
    if rc != 0:
        return False, _tail(log)
    ok, _ = _yt_dlp_status()
    if not ok:
        log += "\npost-install check failed"
    return ok, _tail(log)


def _install_aria2():
    logs = []
    if shutil.which("apt-get") and os.geteuid() == 0:
        rc, out = _run(["apt-get", "update", "-qq"], 300)
        logs.append("$ apt-get update -qq\n%s" % out)
        rc2, out2 = _run(["apt-get", "install", "-y", "-q", "aria2"], 600)
        logs.append("$ apt-get install -y -q aria2\n%s" % out2)
        if rc2 == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
        logs.append("apt path failed (rc=%s); trying apk/brew fallbacks" % rc2)
    elif shutil.which("apt-get"):
        logs.append("apt-get exists but not running as root (uid=%s); skipping apt" % os.geteuid())
    if shutil.which("apk"):
        rc, out = _run(["apk", "add", "aria2"], 600)
        logs.append("$ apk add aria2\n%s" % out)
        if rc == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
    if shutil.which("brew"):
        rc, out = _run(["brew", "install", "aria2"], 600)
        logs.append("$ brew install aria2\n%s" % out)
        if rc == 0 and shutil.which("aria2c"):
            return True, _tail("\n".join(logs))
    logs.append("aria2 install FAILED: no working provider (need apt-get as root, apk, or brew)")
    return False, _tail("\n".join(logs))


_INSTALLERS = {
    "playwright": _install_playwright,
    "yt-dlp": _install_yt_dlp,
    "aria2": _install_aria2,
}


def _install_one(name):
    with _INSTALL_LOCK:
        if name in _INSTALLING:
            return {"installed": False, "log_tail": "install of %r already in progress; retry shortly" % name}
        _INSTALLING.add(name)
    try:
        try:
            ok, tail = _INSTALLERS[name]()
        except Exception as e:
            ok, tail = False, "installer raised: %s" % e
        return {"installed": bool(ok), "log_tail": _tail(tail)}
    finally:
        with _INSTALL_LOCK:
            _INSTALLING.discard(name)


def do_tools_install(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "name" not in args:
        raise ValueError("missing required argument: name")
    name = args["name"]
    if name not in ("all", "playwright", "yt-dlp", "aria2"):
        raise ValueError("name must be one of all, playwright, yt-dlp, aria2")
    targets = ["playwright", "yt-dlp", "aria2"] if name == "all" else [name]
    return {t: _install_one(t) for t in targets}


def _ensure(names):
    """Ensure named tools installed; return installed_now flag."""
    installed_now = False
    for n in names:
        checker = {"playwright": _playwright_status, "yt-dlp": _yt_dlp_status, "aria2": _aria2_status}[n]
        ok, _ = checker()
        if not ok:
            res = _install_one(n)
            if res.get("installed"):
                installed_now = True
    return installed_now


# ---------------- search ----------------

class _DDGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self._cap = None  # (kind, href)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._cap = ("title", d.get("href", ""))
            self._buf = []
        elif tag == "a" and "result__snippet" in cls:
            self._cap = ("snippet", "")
            self._buf = []
        elif tag == "td" and "result-snippet" in cls:
            self._cap = ("snippet", "")
            self._buf = []

    def handle_data(self, data):
        if self._cap:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if not self._cap:
            return
        kind, href = self._cap
        if (tag == "a" and kind in ("title", "snippet")) or (tag == "td" and kind == "snippet"):
            text = htmlmod.unescape(re.sub(r"\s+", " ", "".join(self._buf)).strip())
            if kind == "title":
                self.results.append({"title": text, "url": href, "snippet": ""})
            elif kind == "snippet" and self.results and not self.results[-1]["snippet"]:
                self.results[-1]["snippet"] = text[:300]
            self._cap = None
            self._buf = []


def _resolve_ddg_url(href):
    if not href:
        return ""
    try:
        if "uddg=" in href:
            m = re.search(r"uddg=([^&]+)", href)
            if m:
                return urllib.parse.unquote(m.group(1))
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return "https://duckduckgo.com" + href
        return href
    except Exception:
        return href


def _ddg_fetch(endpoint, query, post=False):
    data = None
    url = endpoint + urllib.parse.urlencode({"q": query})
    headers = {"User-Agent": UA, "Accept": "text/html"}
    if post:
        url = endpoint
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


def do_search(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "query" not in args:
        raise ValueError("missing required argument: query")
    query = args["query"]
    num = args.get("num_results", 8)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(num, int) or isinstance(num, bool):
        raise ValueError("num_results must be an int")
    num = max(1, min(num, 20))
    html_text = ""
    try:
        html_text = _ddg_fetch("https://html.duckduckgo.com/html/?", query, post=True)
    except Exception as e1:
        try:
            html_text = _ddg_fetch("https://lite.duckduckgo.com/lite/?", query, post=True)
        except Exception as e2:
            return {"error": "search failed", "detail": "html endpoint: %s; lite fallback: %s" % (e1, e2)}
    parser = _DDGParser()
    try:
        parser.feed(html_text)
    except Exception:
        pass
    out = []
    for r in parser.results:
        url = _resolve_ddg_url(r.get("url", ""))
        if not url or url.startswith("https://duckduckgo.com/y.js"):
            continue
        out.append({"title": r.get("title", ""), "url": url, "snippet": (r.get("snippet", "") or "")[:300]})
        if len(out) >= num:
            break
    return {
        "engine": "duckduckgo-html (keyless reimplementation of opencode builtin websearch — MCP cannot call the builtin)",
        "results": out,
    }


# ---------------- fetch ----------------

def _html_to_text(html_text):
    txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1\s*>", " ", html_text)
    txt = re.sub(r"(?s)<[^>]+>", " ", txt)
    txt = htmlmod.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def do_fetch(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    fmt = args.get("format", "markdown")
    max_chars = args.get("max_chars", 20000)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if fmt not in ("markdown", "text"):
        raise ValueError('format must be one of markdown, text')
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise ValueError("max_chars must be an int")
    max_chars = max(1, min(max_chars, 200000))
    jina_url = "https://r.jina.ai/" + url
    headers = {
        # jina-reader 403s browser-mimicking UAs; non-browser UA returns 200 (verified)
        "User-Agent": "python-requests/2.32.3",
        "Accept": "text/plain",
        "X-Return-Format": "markdown" if fmt == "markdown" else "text",
    }
    try:
        req = urllib.request.Request(jina_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise RuntimeError("jina-reader HTTP %s" % r.status)
            raw = r.read().decode("utf-8", "replace")
        truncated = len(raw) > max_chars
        return {"url": url, "source": "jina-reader", "content": raw[:max_chars], "truncated": truncated}
    except Exception as e1:
        reason = str(e1) or repr(e1)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode("utf-8", "replace")
            text = _html_to_text(raw)
            truncated = len(text) > max_chars
            return {
                "url": url,
                "source": "direct-fallback (%s)" % reason,
                "content": text[:max_chars],
                "truncated": truncated,
            }
        except Exception as e2:
            return {"error": "fetch failed", "detail": "jina-reader: %s; direct fallback: %s" % (reason, e2)}


# ---------------- browser ----------------

def _log_capped(lst, item):
    lst.append(item)
    del lst[:-_MAXLOG]


def _on_request(req):
    try:
        _log_capped(_NETLOG, {"event": "request", "method": req.method, "url": req.url})
    except Exception:
        pass


def _on_response(resp):
    try:
        _log_capped(_NETLOG, {"event": "response", "url": resp.url, "status": resp.status})
    except Exception:
        pass


def _on_console(msg):
    try:
        _log_capped(_CONSOLE, {"type": msg.type, "text": (msg.text or "")[:2000]})
    except Exception:
        pass


def _on_dialog(dlg):
    try:
        _DIALOGS.append({"type": dlg.type, "message": dlg.message, "default_value": dlg.default_value})
        del _DIALOGS[:-_MAXLOG]
        _PENDING_DIALOGS.append(dlg)
        del _PENDING_DIALOGS[:-50]
    except Exception:
        pass
    try:
        if _OVERLAY_ACTION == "accept":
            dlg.accept()
        elif _OVERLAY_ACTION == "dismiss":
            dlg.dismiss()
    except Exception:
        pass


def _on_websocket(ws):
    try:
        url = ws.url
    except Exception:
        url = ""
    try:
        ws.on("framesent", lambda payload: _log_capped(_WSLOG, {"url": url, "direction": "sent", "payload": str(payload)[:2000]}))
    except Exception:
        pass
    try:
        ws.on("framereceived", lambda payload: _log_capped(_WSLOG, {"url": url, "direction": "received", "payload": str(payload)[:2000]}))
    except Exception:
        pass


def _attach_page_listeners(page):
    for event, handler in (("console", _on_console), ("dialog", _on_dialog), ("websocket", _on_websocket)):
        try:
            page.on(event, handler)
        except Exception:
            pass


def _attach_context_listeners(ctx):
    for event, handler in (("request", _on_request), ("response", _on_response)):
        try:
            ctx.on(event, handler)
        except Exception:
            pass
    try:
        ctx.on("page", _attach_page_listeners)
    except Exception:
        pass


def _apply_route(ctx, pattern, spec):
    action = (spec or {}).get("action", "abort")
    if action in ("off", "unroute"):
        try:
            ctx.unroute(pattern)
        except Exception:
            pass
        return
    def _handler(route, request):
        try:
            if action == "abort":
                route.abort()
            elif action == "fulfill":
                kwargs = {}
                if spec.get("status") is not None:
                    kwargs["status"] = spec["status"]
                if spec.get("body") is not None:
                    kwargs["body"] = spec["body"]
                if spec.get("content_type") is not None:
                    kwargs["content_type"] = spec["content_type"]
                route.fulfill(**kwargs)
            else:
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass
    try:
        ctx.route(pattern, _handler)
    except Exception as e:
        raise RuntimeError("route failed for %r: %s" % (pattern, e))


def _ensure_browser():
    """Return (installed_now_bool). Launches singleton browser via sync API."""
    global _BROWSER, _PAGE, _PW, _CTX
    installed_now = _ensure(["playwright"])
    ok_ch, _ = _chromium_status()
    if not ok_ch:
        res = _install_one("playwright")
        if res.get("installed"):
            installed_now = True
        else:
            raise RuntimeError("chromium install failed: %s" % res.get("log_tail", "")[-500:])
    if _BROWSER is not None:
        try:
            if _PAGE is not None and _CTX is not None:
                _PAGE.url  # touch: raises if the page/context was closed
                return installed_now
        except Exception:
            pass
        _close_browser()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        raise RuntimeError("playwright import failed even after ensure: %s" % e)
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"], headless=True)
    except Exception as e:
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError("chromium launch failed: %s" % e)
    try:
        ctx_kwargs = {}
        if _VIDEO_DIR:
            ctx_kwargs["record_video_dir"] = _VIDEO_DIR
        ctx = browser.new_context(**ctx_kwargs)
        for script in _INIT_SCRIPTS:
            try:
                ctx.add_init_script(script=script)
            except Exception:
                pass
        for pattern, spec in _ROUTES.items():
            try:
                _apply_route(ctx, pattern, spec)
            except Exception:
                pass
        if _TRACING:
            try:
                ctx.tracing.start(screenshots=_TRACE_OPTS.get("screenshots", True),
                                 snapshots=_TRACE_OPTS.get("snapshots", True))
            except Exception:
                pass
        _attach_context_listeners(ctx)
        page = ctx.new_page()
        page.set_default_timeout(_DEFAULT_TIMEOUT)
        _attach_page_listeners(page)
    except Exception as e:
        try:
            browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError("browser context setup failed: %s" % e)
    _PW = pw
    _BROWSER = browser
    _CTX = ctx
    _PAGE = page
    return installed_now


def _close_browser():
    global _BROWSER, _PAGE, _PW, _CTX
    try:
        if _CTX is not None:
            _CTX.close()
    except Exception:
        pass
    try:
        if _BROWSER is not None:
            _BROWSER.close()
    except Exception:
        pass
    try:
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass
    _BROWSER = None
    _PAGE = None
    _PW = None
    _CTX = None
    _PENDING_DIALOGS[:] = []


atexit.register(_close_browser)


def do_browser_open(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    wait_ms = args.get("wait_ms", 1500)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool):
        raise ValueError("wait_ms must be an int")
    wait_ms = max(0, min(wait_ms, 30000))
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.goto(url, wait_until="load", timeout=30000)
        _PAGE.wait_for_timeout(wait_ms)
        title = _PAGE.title()
        try:
            text = _PAGE.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            text = ""
        if not isinstance(text, str):
            text = str(text)
        out = {"url": _PAGE.url, "title": title, "text": text[:50000]}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_open failed", "detail": str(e)}


def _loc(scope, target):
    """Resolve a click/fill target: CSS selector (leading . # >) or visible text."""
    if target and target[0] in (".", "#", ">"):
        return scope.locator(target)
    return scope.get_by_text(target).first


def _poll_new_pages(before, timeout_ms=3000):
    """Poll context pages for popups opened by a click; attach listeners to them."""
    import time
    before_ids = set(id(p) for p in before)
    deadline = time.time() + max(0, timeout_ms) / 1000.0
    found = []
    while time.time() < deadline:
        try:
            current = list(_CTX.pages)
        except Exception:
            break
        for p in current:
            if id(p) not in before_ids and all(id(q) != id(p) for q in found):
                try:
                    _attach_page_listeners(p)
                except Exception:
                    pass
                found.append(p)
        if found:
            try:
                found[-1].wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass
            break
        time.sleep(0.2)
    out = []
    for p in found:
        try:
            out.append({"url": p.url, "title": p.title()})
        except Exception as e:
            out.append({"url": "", "detail": str(e)})
    return out


def do_browser_click(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    target = args["text"]
    if not isinstance(target, str) or not target:
        raise ValueError("text must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        before = list(_CTX.pages)
    except Exception:
        before = []
    try:
        _loc(_PAGE, target).click(timeout=30000)
        clicked = target
        _PAGE.wait_for_timeout(1000)
        new_pages = _poll_new_pages(before)
        try:
            cur = _PAGE.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            cur = ""
        out = {
            "ok": True,
            "url": _PAGE.url,
            "new_pages": new_pages,
            "text": (cur[:50000] if isinstance(cur, str) else str(cur)[:50000]),
        }
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_click failed", "detail": "could not click %r: %s" % (target, e)}


def do_browser_screenshot(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    full_page = args.get("full_page", False)
    selector = args.get("selector")
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if not isinstance(full_page, bool):
        raise ValueError("full_page must be a boolean")
    if selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("selector must be a non-empty string or null")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        if selector:
            _PAGE.locator(selector).screenshot(path=path)
        else:
            _PAGE.screenshot(path=path, full_page=full_page)
        size = os.path.getsize(path)
        out = {"path": path, "bytes": size, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_screenshot failed", "detail": str(e)}


def do_browser_extract(args):
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector", "body")
    if not isinstance(selector, str) or not selector:
        raise ValueError("selector must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        text = _PAGE.inner_text(selector, timeout=30000)
        out = {"text": (text[:50000] if isinstance(text, str) else str(text)[:50000])}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_extract failed", "detail": str(e)}


# ---------------- FIX.doc expansion: tabs ----------------

def _tab_info(page, index):
    try:
        active = page is _PAGE
    except Exception:
        active = False
    try:
        url = page.url
    except Exception:
        url = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    return {"index": index, "url": url, "title": title, "active": active}


def do_browser_tabs(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        pages = list(_CTX.pages)
    except Exception as e:
        return {"error": "browser_tabs failed", "detail": str(e)}
    out = {"tabs": [_tab_info(p, i) for i, p in enumerate(pages)]}
    if installed_now:
        out["installed_now"] = True
    return out


def do_browser_switch_tab(args):
    global _PAGE
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    index = args.get("index")
    url_contains = args.get("url_contains")
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
        raise ValueError("index must be an int or null")
    if url_contains is not None and not isinstance(url_contains, str):
        raise ValueError("url_contains must be a string or null")
    if index is None and not url_contains:
        raise ValueError("one of index or url_contains is required")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        pages = list(_CTX.pages)
        target = None
        if index is not None:
            if index < 0 or index >= len(pages):
                return {"error": "browser_switch_tab failed", "detail": "index %s out of range (%d tabs)" % (index, len(pages))}
            target = pages[index]
        else:
            for p in pages:
                try:
                    if url_contains in (p.url or ""):
                        target = p
                        break
                except Exception:
                    continue
            if target is None:
                return {"error": "browser_switch_tab failed", "detail": "no tab with url containing %r" % url_contains}
        try:
            target.bring_to_front()
        except Exception:
            pass
        _attach_page_listeners(target)
        _PAGE = target
        out = {"ok": True, "url": _PAGE.url, "title": _PAGE.title()}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_switch_tab failed", "detail": str(e)}


def do_browser_close_tab(args):
    global _PAGE
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    index = args.get("index")
    if index is not None and (not isinstance(index, int) or isinstance(index, bool)):
        raise ValueError("index must be an int or null")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        pages = list(_CTX.pages)
        if len(pages) <= 1:
            return {"error": "browser_close_tab failed", "detail": "cannot close the last tab; use browser_close to quit"}
        if index is None:
            target = _PAGE
        else:
            if index < 0 or index >= len(pages):
                return {"error": "browser_close_tab failed", "detail": "index %s out of range (%d tabs)" % (index, len(pages))}
            target = pages[index]
        target.close()
        pages = list(_CTX.pages)
        if _PAGE not in pages:
            _PAGE = pages[0]
            try:
                _PAGE.bring_to_front()
            except Exception:
                pass
        out = {"ok": True, "url": _PAGE.url, "tabs": [_tab_info(p, i) for i, p in enumerate(pages)]}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_close_tab failed", "detail": str(e)}


def do_browser_close(args):
    try:
        _close_browser()
    except Exception as e:
        return {"error": "browser_close failed", "detail": str(e)}
    return {"ok": True}


def do_browser_info(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        pages = list(_CTX.pages)
        out = {
            "url": _PAGE.url,
            "title": _PAGE.title(),
            "tabs": len(pages),
            "viewport": _PAGE.viewport_size,
            "default_timeout_ms": _DEFAULT_TIMEOUT,
            "network_events": len(_NETLOG),
            "console_events": len(_CONSOLE),
        }
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_info failed", "detail": str(e)}


# ---------------- FIX.doc expansion: downloads ----------------

def do_browser_click_download(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    target = args["text"]
    output_path = args.get("output_path")
    timeout_ms = args.get("timeout_ms", 30000)
    if not isinstance(target, str) or not target:
        raise ValueError("text must be a non-empty string")
    if output_path is not None and (not isinstance(output_path, str) or not output_path):
        raise ValueError("output_path must be a non-empty string or null")
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool):
        raise ValueError("timeout_ms must be an int")
    timeout_ms = max(1000, min(timeout_ms, 300000))
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        with _PAGE.expect_download(timeout=timeout_ms) as dl_info:
            _loc(_PAGE, target).click(timeout=timeout_ms)
        download = dl_info.value
        suggested = download.suggested_filename
        dest = output_path or os.path.join(os.getcwd(), suggested)
        parent = os.path.dirname(os.path.abspath(dest))
        if parent:
            os.makedirs(parent, exist_ok=True)
        download.save_as(dest)
        out = {
            "path": dest,
            "filename": suggested,
            "bytes": os.path.getsize(dest),
            "url": download.url,
        }
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_click_download failed", "detail": "could not download via %r: %s" % (target, e)}


# ---------------- FIX.doc expansion: network ----------------

def do_browser_network_log(args):
    if not isinstance(args, dict):
        args = {}
    limit = args.get("limit", 200) if isinstance(args, dict) else 200
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an int")
    limit = max(1, min(limit, _MAXLOG))
    return {"events": _NETLOG[-limit:], "count": len(_NETLOG)}


def do_browser_network_clear(args):
    _NETLOG[:] = []
    _WSLOG[:] = []
    return {"ok": True}


def do_browser_har_save(args):
    import datetime
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    entries = []
    started = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for i, ev in enumerate(_NETLOG):
        entries.append({
            "startedDateTime": started,
            "request": {"method": ev.get("method", "GET"), "url": ev.get("url", "")},
            "response": {"status": ev.get("status", 0)},
            "comment": ev.get("event", ""),
        })
    har = {"log": {"version": "1.2", "creator": {"name": "web-mcp", "version": "1.0"},
                   "pages": [], "entries": entries}}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(har, f)
        return {"path": path, "bytes": os.path.getsize(path), "entries": len(entries)}
    except Exception as e:
        return {"error": "browser_har_save failed", "detail": str(e)}


def do_browser_websocket_log(args):
    if not isinstance(args, dict):
        args = {}
    limit = args.get("limit", 200) if isinstance(args, dict) else 200
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an int")
    limit = max(1, min(limit, _MAXLOG))
    return {"events": _WSLOG[-limit:], "count": len(_WSLOG)}


def do_browser_route(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "pattern" not in args:
        raise ValueError("missing required argument: pattern")
    pattern = args["pattern"]
    action = args.get("action", "abort")
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("pattern must be a non-empty string")
    if action not in ("abort", "continue", "fulfill", "off"):
        raise ValueError("action must be one of abort, continue, fulfill, off")
    spec = {"action": action}
    if action == "fulfill":
        if args.get("status") is not None:
            if not isinstance(args["status"], int) or isinstance(args["status"], bool):
                raise ValueError("status must be an int or null")
            spec["status"] = args["status"]
        if args.get("body") is not None:
            if not isinstance(args["body"], str):
                raise ValueError("body must be a string or null")
            spec["body"] = args["body"]
        if args.get("content_type") is not None:
            if not isinstance(args["content_type"], str):
                raise ValueError("content_type must be a string or null")
            spec["content_type"] = args["content_type"]
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if action == "off":
            _ROUTES.pop(pattern, None)
        else:
            _ROUTES[pattern] = spec
        _apply_route(_CTX, pattern, {"action": "off"} if action == "off" else spec)
        out = {"ok": True, "pattern": pattern, "action": action, "routes": sorted(_ROUTES)}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_route failed", "detail": str(e)}


# ---------------- FIX.doc expansion: attributes ----------------

def do_browser_get_attribute(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "selector" not in args:
        raise ValueError("missing required argument: selector")
    if "name" not in args:
        raise ValueError("missing required argument: name")
    selector = args["selector"]
    name = args["name"]
    if not isinstance(selector, str) or not selector:
        raise ValueError("selector must be a non-empty string")
    if not isinstance(name, str) or not name:
        raise ValueError("name must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        value = _PAGE.locator(selector).first.get_attribute(name, timeout=30000)
        out = {"selector": selector, "name": name, "value": value}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_get_attribute failed", "detail": str(e)}


# ---------------- FIX.doc expansion: eval + injection ----------------

_EXFIL_PATTERNS = (
    r"document\.cookie", r"localStorage", r"sessionStorage", r"indexedDB",
    r"\.password\b", r"Authorization",
)
_NET_PATTERNS = (
    r"fetch\s*\(", r"XMLHttpRequest", r"sendBeacon", r"WebSocket\s*\(",
    r"\.src\s*=", r"navigator\.sendBeacon",
)


def _eval_guardrail(script, allow):
    if allow:
        return None
    exfil = any(re.search(p, script) for p in _EXFIL_PATTERNS)
    net = any(re.search(p, script) for p in _NET_PATTERNS)
    if exfil and net:
        return ("browser_eval guardrail: script reads page secrets (cookies/storage/credentials) "
                "AND performs network sends — blocked as a likely exfiltration/forgery attempt. "
                "Pass allow_exfil:true to override.")
    return None


def do_browser_eval(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "script" not in args:
        raise ValueError("missing required argument: script")
    script = args["script"]
    arg = args.get("arg")
    allow_exfil = args.get("allow_exfil", False)
    if not isinstance(script, str) or not script:
        raise ValueError("script must be a non-empty string")
    if not isinstance(allow_exfil, bool):
        raise ValueError("allow_exfil must be a boolean")
    blocked = _eval_guardrail(script, allow_exfil)
    if blocked:
        return {"error": "browser_eval blocked", "detail": blocked}
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        result = _PAGE.evaluate(script, arg)
        try:
            text = json.dumps(result)[:50000]
        except Exception:
            text = str(result)[:50000]
        out = {"result": text, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_eval failed", "detail": str(e)}


def do_browser_init_script(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "script" not in args:
        raise ValueError("missing required argument: script")
    script = args["script"]
    if not isinstance(script, str) or not script:
        raise ValueError("script must be a non-empty string")
    _INIT_SCRIPTS.append(script)
    applied = False
    if _CTX is not None:
        try:
            _CTX.add_init_script(script=script)
            applied = True
        except Exception:
            pass
    return {"ok": True, "count": len(_INIT_SCRIPTS), "applied_to_live_context": applied}


def do_browser_inject_script(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    content = args.get("content")
    url = args.get("url")
    if content is not None and not isinstance(content, str):
        raise ValueError("content must be a string or null")
    if url is not None and not isinstance(url, str):
        raise ValueError("url must be a string or null")
    if not content and not url:
        raise ValueError("one of content or url is required")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if content:
            _PAGE.add_script_tag(content=content)
        else:
            _PAGE.add_script_tag(url=url)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_inject_script failed", "detail": str(e)}


def do_browser_inject_style(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    content = args.get("content")
    url = args.get("url")
    if content is not None and not isinstance(content, str):
        raise ValueError("content must be a string or null")
    if url is not None and not isinstance(url, str):
        raise ValueError("url must be a string or null")
    if not content and not url:
        raise ValueError("one of content or url is required")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if content:
            _PAGE.add_style_tag(content=content)
        else:
            _PAGE.add_style_tag(url=url)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_inject_style failed", "detail": str(e)}


# ---------------- yt-dlp ----------------

def _yt_cmd():
    cli = shutil.which("yt-dlp")
    if cli:
        return [cli], cli
    return [sys.executable, "-m", "yt_dlp"], "python -m yt_dlp"


def _tool_version(cmd, flag="--version"):
    try:
        rc, out = _run(cmd + [flag], 60)
        if rc == 0:
            return out.strip().splitlines()[0][:100] if out.strip() else "unknown"
        return "unknown"
    except Exception:
        return "unknown"


def do_yt_download(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    output_dir = args.get("output_dir", ".")
    audio_only = args.get("audio_only", False)
    filename = args.get("filename")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir must be a non-empty string")
    if not isinstance(audio_only, bool):
        raise ValueError("audio_only must be a boolean")
    if filename is not None and not isinstance(filename, str):
        raise ValueError("filename must be a string or null")
    installed_now = _ensure(["yt-dlp"])
    base, tool_label = _yt_cmd()
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        return {"error": "yt_download failed", "detail": "cannot create output_dir: %s" % e,
                "tool": "yt-dlp", "installed_now": installed_now,
                "tool_version": _tool_version(base)}
    template = os.path.join(output_dir, filename if filename else "%(title)s.%(ext)s")
    cmd = base + ["-o", template, "--no-progress", "--newline",
                  "--print", "after_move:filepath", url]
    if audio_only:
        cmd = base + ["-o", template, "--extract-audio", "--audio-format", "mp3",
                      "--no-progress", "--newline", "--print", "after_move:filepath", url]
    rc, out = _run(cmd, 300)
    version = _tool_version(base)
    if rc != 0:
        return {"error": "yt_download failed", "detail": _tail(out, 2000),
                "tool": "yt-dlp", "installed_now": installed_now, "tool_version": version}
    files = []
    for line in out.splitlines():
        line = line.strip()
        if line and os.path.isfile(line):
            try:
                files.append({"path": line, "bytes": os.path.getsize(line)})
            except Exception:
                pass
    if not files:
        # Fallback: newest files in output_dir (yt-dlp may print nothing on some versions).
        try:
            cands = [os.path.join(output_dir, f) for f in os.listdir(output_dir)]
            cands = [p for p in cands if os.path.isfile(p)]
            cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in cands[:5]:
                files.append({"path": p, "bytes": os.path.getsize(p)})
        except Exception:
            pass
    if not files:
        return {"error": "yt_download reported success but no output file found",
                "detail": _tail(out, 2000), "tool": "yt-dlp",
                "installed_now": installed_now, "tool_version": version}
    return {"files": files, "tool": "yt-dlp", "installed_now": installed_now, "tool_version": version}


# ---------------- aria2 ----------------

def do_aria2_download(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "url" not in args:
        raise ValueError("missing required argument: url")
    url = args["url"]
    output_dir = args.get("output_dir", ".")
    filename = args.get("filename")
    connections = args.get("connections", 4)
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    if not isinstance(output_dir, str) or not output_dir:
        raise ValueError("output_dir must be a non-empty string")
    if filename is not None and not isinstance(filename, str):
        raise ValueError("filename must be a string or null")
    if not isinstance(connections, int) or isinstance(connections, bool):
        raise ValueError("connections must be an int")
    connections = max(1, min(connections, 16))
    installed_now = _ensure(["aria2"])
    if not shutil.which("aria2c"):
        return {"error": "aria2c unavailable", "detail": "aria2c not installed and auto-install failed; run tools_install('aria2') for the log",
                "tool": "aria2c", "installed_now": installed_now, "tool_version": "unknown"}
    version = _tool_version(["aria2c"])
    try:
        os.makedirs(output_dir, exist_ok=True)
    except Exception as e:
        return {"error": "aria2_download failed", "detail": "cannot create output_dir: %s" % e,
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    before = set()
    try:
        before = set(os.listdir(output_dir))
    except Exception:
        pass
    cmd = ["aria2c", "-x", str(connections), "-s", str(connections),
           "-d", output_dir, "--console-log-level=warn", "--summary-interval=0"]
    if filename:
        cmd += ["-o", filename]
    cmd += [url]
    rc, out = _run(cmd, 300)
    if rc != 0:
        return {"error": "aria2_download failed", "detail": _tail(out, 2000),
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    files = []
    try:
        if filename:
            p = os.path.join(output_dir, filename)
            if os.path.isfile(p):
                files.append({"path": p, "bytes": os.path.getsize(p)})
        else:
            for f in sorted(set(os.listdir(output_dir)) - before):
                p = os.path.join(output_dir, f)
                if os.path.isfile(p):
                    files.append({"path": p, "bytes": os.path.getsize(p)})
            if not files:
                for f in sorted(os.listdir(output_dir)):
                    p = os.path.join(output_dir, f)
                    if os.path.isfile(p):
                        files.append({"path": p, "bytes": os.path.getsize(p)})
    except Exception as e:
        return {"error": "aria2_download listing failed", "detail": str(e),
                "tool": "aria2c", "installed_now": installed_now, "tool_version": version}
    if not files:
        return {"error": "aria2c reported success but no output file found",
                "detail": _tail(out, 2000), "tool": "aria2c",
                "installed_now": installed_now, "tool_version": version}
    return {"files": files, "tool": "aria2c", "installed_now": installed_now, "tool_version": version}


# ---------------- FIX.doc expansion: forms / pointer / reads ----------------

def _need_selector(args, *names):
    for n in names:
        v = args.get(n)
        if not isinstance(v, str) or not v:
            raise ValueError("%s must be a non-empty string" % n)
    return True


def do_browser_fill(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    _need_selector(args, "selector")
    text = args["text"]
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.fill(text, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_fill failed", "detail": str(e)}


def do_browser_type(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    _need_selector(args, "selector")
    text = args["text"]
    delay_ms = args.get("delay_ms", 0)
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    if not isinstance(delay_ms, int) or isinstance(delay_ms, bool):
        raise ValueError("delay_ms must be an int")
    delay_ms = max(0, min(delay_ms, 5000))
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.press_sequentially(text, delay=delay_ms, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_type failed", "detail": str(e)}


def do_browser_press(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "key" not in args:
        raise ValueError("missing required argument: key")
    _need_selector(args, "selector")
    key = args["key"]
    if not isinstance(key, str) or not key:
        raise ValueError("key must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.press(key, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_press failed", "detail": str(e)}


def do_browser_clear(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.clear(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_clear failed", "detail": str(e)}


def do_browser_focus(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.focus(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_focus failed", "detail": str(e)}


def do_browser_blur(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.evaluate("el => el.blur()")
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_blur failed", "detail": str(e)}


def do_browser_check(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.check(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_check failed", "detail": str(e)}


def do_browser_uncheck(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.uncheck(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_uncheck failed", "detail": str(e)}


def do_browser_select(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "values" not in args:
        raise ValueError("missing required argument: values")
    _need_selector(args, "selector")
    values = args["values"]
    if not isinstance(values, list) or not values or not all(isinstance(v, str) for v in values):
        raise ValueError("values must be a non-empty array of strings")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        selected = _PAGE.locator(args["selector"]).first.select_option(values, timeout=30000)
        out = {"ok": True, "selected": selected, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_select failed", "detail": str(e)}


def do_browser_upload(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "paths" not in args:
        raise ValueError("missing required argument: paths")
    _need_selector(args, "selector")
    paths = args["paths"]
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("paths must be a non-empty array of strings")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.set_input_files(paths, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_upload failed", "detail": str(e)}


def do_browser_hover(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.hover(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_hover failed", "detail": str(e)}


def do_browser_drag(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "source", "target")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["source"]).first.drag_to(_PAGE.locator(args["target"]).first, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_drag failed", "detail": str(e)}


def do_browser_scroll(args):
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector")
    x = args.get("x", 0)
    y = args.get("y", 500)
    if selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("selector must be a non-empty string or null")
    if not isinstance(x, int) or isinstance(x, bool) or not isinstance(y, int) or isinstance(y, bool):
        raise ValueError("x and y must be ints")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if selector:
            _PAGE.locator(selector).first.scroll_into_view_if_needed(timeout=30000)
        else:
            _PAGE.evaluate("([dx, dy]) => window.scrollBy(dx, dy)", [x, y])
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_scroll failed", "detail": str(e)}


def do_browser_tap(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.locator(args["selector"]).first.tap(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_tap failed", "detail": str(e)}


def do_browser_state(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    _need_selector(args, "selector")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        loc = _PAGE.locator(args["selector"]).first
        out = {"selector": args["selector"], "url": _PAGE.url}
        for name, fn in (("visible", loc.is_visible), ("hidden", loc.is_hidden),
                         ("enabled", loc.is_enabled), ("disabled", loc.is_disabled),
                         ("editable", loc.is_editable), ("checked", loc.is_checked)):
            try:
                out[name] = bool(fn())
            except Exception:
                out[name] = None
        try:
            out["value"] = loc.input_value()
        except Exception:
            out["value"] = None
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_state failed", "detail": str(e)}


def do_browser_snapshot(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        try:
            snap = _PAGE.accessibility.snapshot()
            text = json.dumps(snap)[:50000]
        except Exception:
            text = _PAGE.evaluate("() => document.body ? document.body.innerText : ''")[:50000]
        out = {"url": _PAGE.url, "snapshot": text}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_snapshot failed", "detail": str(e)}


def do_browser_html(args):
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector")
    if selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("selector must be a non-empty string or null")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if selector:
            html_text = _PAGE.locator(selector).first.inner_html(timeout=30000)
        else:
            html_text = _PAGE.content()
        out = {"url": _PAGE.url, "html": html_text[:100000], "truncated": len(html_text) > 100000}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_html failed", "detail": str(e)}


def do_browser_console_errors(args):
    if not isinstance(args, dict):
        args = {}
    limit = args.get("limit", 100) if isinstance(args, dict) else 100
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be an int")
    limit = max(1, min(limit, _MAXLOG))
    errs = [e for e in _CONSOLE if e.get("type") == "error"][-limit:]
    return {"errors": errs, "count": len(errs), "console_total": len(_CONSOLE)}


# ---------------- FIX.doc expansion: nav / waits / dialogs / frames ----------------

def do_browser_back(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.go_back(timeout=30000)
        _PAGE.wait_for_timeout(500)
        out = {"ok": True, "url": _PAGE.url, "title": _PAGE.title()}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_back failed", "detail": str(e)}


def do_browser_forward(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.go_forward(timeout=30000)
        _PAGE.wait_for_timeout(500)
        out = {"ok": True, "url": _PAGE.url, "title": _PAGE.title()}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_forward failed", "detail": str(e)}


def do_browser_reload(args):
    if not isinstance(args, dict):
        args = {}
    wait_until = args.get("wait_until", "load") if isinstance(args, dict) else "load"
    if wait_until not in ("load", "domcontentloaded", "networkidle", "commit"):
        raise ValueError("wait_until must be one of load, domcontentloaded, networkidle, commit")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _PAGE.reload(wait_until=wait_until, timeout=30000)
        out = {"ok": True, "url": _PAGE.url, "title": _PAGE.title()}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_reload failed", "detail": str(e)}


def do_browser_wait_for(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector")
    text = args.get("text")
    url_substring = args.get("url_substring")
    ms = args.get("ms", 1000)
    state = args.get("state", "visible")
    if selector is not None and (not isinstance(selector, str) or not selector):
        raise ValueError("selector must be a non-empty string or null")
    if text is not None and (not isinstance(text, str) or not text):
        raise ValueError("text must be a non-empty string or null")
    if url_substring is not None and not isinstance(url_substring, str):
        raise ValueError("url_substring must be a string or null")
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise ValueError("ms must be an int")
    if state not in ("visible", "hidden", "attached", "detached"):
        raise ValueError("state must be one of visible, hidden, attached, detached")
    ms = max(0, min(ms, 120000))
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if url_substring:
            _PAGE.wait_for_url("**%s**" % url_substring, timeout=ms)
        elif selector:
            _PAGE.locator(selector).first.wait_for(state=state, timeout=ms)
        elif text:
            _PAGE.get_by_text(text).first.wait_for(state=state, timeout=ms)
        else:
            _PAGE.wait_for_timeout(ms)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_wait_for failed", "detail": str(e)}


def do_browser_set_timeout(args):
    global _DEFAULT_TIMEOUT
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "ms" not in args:
        raise ValueError("missing required argument: ms")
    ms = args["ms"]
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise ValueError("ms must be an int")
    ms = max(1000, min(ms, 300000))
    _DEFAULT_TIMEOUT = ms
    if _PAGE is not None:
        try:
            _PAGE.set_default_timeout(ms)
        except Exception:
            pass
    return {"ok": True, "default_timeout_ms": ms}


def do_browser_add_overlay_handler(args):
    global _OVERLAY_ACTION
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "action" not in args:
        raise ValueError("missing required argument: action")
    action = args["action"]
    if action not in ("accept", "dismiss", "off"):
        raise ValueError("action must be one of accept, dismiss, off")
    _OVERLAY_ACTION = None if action == "off" else action
    return {"ok": True, "overlay_action": _OVERLAY_ACTION}


def do_browser_dialog(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "action" not in args:
        raise ValueError("missing required argument: action")
    action = args["action"]
    prompt_text = args.get("prompt_text")
    if action not in ("accept", "dismiss"):
        raise ValueError("action must be one of accept, dismiss")
    if prompt_text is not None and not isinstance(prompt_text, str):
        raise ValueError("prompt_text must be a string or null")
    while _PENDING_DIALOGS:
        dlg = _PENDING_DIALOGS.pop(0)
        try:
            if action == "accept":
                if prompt_text is not None:
                    dlg.accept(prompt_text)
                else:
                    dlg.accept()
            else:
                dlg.dismiss()
            return {"ok": True, "action": action}
        except Exception:
            continue
    return {"error": "browser_dialog failed", "detail": "no pending dialog to handle"}


def do_browser_dialog_last(args):
    if not _DIALOGS:
        return {"dialog": None, "count": 0}
    return {"dialog": _DIALOGS[-1], "count": len(_DIALOGS)}


def _find_frame(args):
    if _CTX is None:
        raise RuntimeError("browser context not started")
    frames = list(_CTX.frames)
    if "index" in args and args["index"] is not None:
        index = args["index"]
        if not isinstance(index, int) or isinstance(index, bool):
            raise ValueError("index must be an int or null")
        if index < 0 or index >= len(frames):
            raise ValueError("index %s out of range (%d frames)" % (index, len(frames)))
        return frames[index]
    name = args.get("name")
    url_contains = args.get("url_contains")
    if name:
        for f in frames:
            try:
                if f.name == name:
                    return f
            except Exception:
                continue
        raise ValueError("no frame named %r" % name)
    if url_contains:
        for f in frames:
            try:
                if url_contains in (f.url or ""):
                    return f
            except Exception:
                continue
        raise ValueError("no frame with url containing %r" % url_contains)
    raise ValueError("one of index, name or url_contains is required")


def do_browser_frames(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        out = {"frames": []}
        for i, f in enumerate(_CTX.frames):
            try:
                out["frames"].append({"index": i, "name": f.name, "url": f.url})
            except Exception as e:
                out["frames"].append({"index": i, "detail": str(e)})
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_frames failed", "detail": str(e)}


def do_browser_frame_click(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    target = args["text"]
    if not isinstance(target, str) or not target:
        raise ValueError("text must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        frame = _find_frame(args)
        _loc(frame, target).click(timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_frame_click failed", "detail": str(e)}


def do_browser_frame_extract(args):
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    selector = args.get("selector", "body")
    if not isinstance(selector, str) or not selector:
        raise ValueError("selector must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        frame = _find_frame(args)
        text = frame.locator(selector).first.inner_text(timeout=30000)
        out = {"text": (text[:50000] if isinstance(text, str) else str(text)[:50000])}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_frame_extract failed", "detail": str(e)}


def do_browser_frame_fill(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "text" not in args:
        raise ValueError("missing required argument: text")
    _need_selector(args, "selector")
    text = args["text"]
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        frame = _find_frame(args)
        frame.locator(args["selector"]).first.fill(text, timeout=30000)
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_frame_fill failed", "detail": str(e)}


# ---------------- FIX.doc expansion: auth / emulation ----------------

def do_browser_cookies_get(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        out = {"cookies": _CTX.cookies()}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_cookies_get failed", "detail": str(e)}


def do_browser_cookies_set(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "cookies" not in args:
        raise ValueError("missing required argument: cookies")
    cookies = args["cookies"]
    if not isinstance(cookies, list) or not cookies or not all(isinstance(c, dict) for c in cookies):
        raise ValueError("cookies must be a non-empty array of objects")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _CTX.add_cookies(cookies)
        out = {"ok": True, "count": len(cookies)}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_cookies_set failed", "detail": str(e)}


def do_browser_cookies_clear(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _CTX.clear_cookies()
        out = {"ok": True}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_cookies_clear failed", "detail": str(e)}


def do_browser_storage_save(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        _CTX.storage_state(path=path)
        out = {"path": path, "bytes": os.path.getsize(path)}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_storage_save failed", "detail": str(e)}


def do_browser_storage_load(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    if not os.path.isfile(path):
        return {"error": "browser_storage_load failed", "detail": "no such file: %s" % path}
    return do_browser_context_new({"storage_state": path})


def do_browser_context_new(args):
    global _PAGE, _CTX, _VIDEO_DIR
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        if args.get("record_video_dir"):
            if not isinstance(args["record_video_dir"], str):
                raise ValueError("record_video_dir must be a string or null")
            _VIDEO_DIR = args["record_video_dir"]
            os.makedirs(_VIDEO_DIR, exist_ok=True)
        try:
            _CTX.close()
        except Exception:
            pass
        ctx_kwargs = {}
        if _VIDEO_DIR:
            ctx_kwargs["record_video_dir"] = _VIDEO_DIR
        for key in ("viewport", "user_agent", "locale", "timezone_id", "storage_state",
                    "ignore_https_errors", "device_scale_factor", "has_touch"):
            if args.get(key) is not None:
                ctx_kwargs[key] = args[key]
        ctx = _BROWSER.new_context(**ctx_kwargs)
        for script in _INIT_SCRIPTS:
            try:
                ctx.add_init_script(script=script)
            except Exception:
                pass
        for pattern, spec in _ROUTES.items():
            try:
                _apply_route(ctx, pattern, spec)
            except Exception:
                pass
        if _TRACING:
            try:
                ctx.tracing.start(screenshots=_TRACE_OPTS.get("screenshots", True),
                                 snapshots=_TRACE_OPTS.get("snapshots", True))
            except Exception:
                pass
        if args.get("permissions"):
            perms = args["permissions"]
            if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
                raise ValueError("permissions must be an array of strings or null")
            try:
                ctx.grant_permissions(perms)
            except Exception as e:
                return {"error": "browser_context_new failed", "detail": "grant_permissions: %s" % e}
        if args.get("headers"):
            headers = args["headers"]
            if not isinstance(headers, dict):
                raise ValueError("headers must be an object or null")
            try:
                ctx.set_extra_http_headers(headers)
            except Exception as e:
                return {"error": "browser_context_new failed", "detail": "set_extra_http_headers: %s" % e}
        _attach_context_listeners(ctx)
        page = ctx.new_page()
        page.set_default_timeout(_DEFAULT_TIMEOUT)
        _attach_page_listeners(page)
        _CTX = ctx
        _PAGE = page
        out = {"ok": True, "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_context_new failed", "detail": str(e)}


def do_browser_grant_permissions(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "permissions" not in args:
        raise ValueError("missing required argument: permissions")
    perms = args["permissions"]
    if not isinstance(perms, list) or not all(isinstance(p, str) for p in perms):
        raise ValueError("permissions must be an array of strings")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _CTX.grant_permissions(perms)
        out = {"ok": True, "permissions": perms}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_grant_permissions failed", "detail": str(e)}


def do_browser_set_headers(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "headers" not in args:
        raise ValueError("missing required argument: headers")
    headers = args["headers"]
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _CTX.set_extra_http_headers(headers)
        out = {"ok": True, "headers": headers}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_set_headers failed", "detail": str(e)}


# ---------------- FIX.doc expansion: outputs ----------------

def do_browser_pdf(args):
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    if "path" not in args:
        raise ValueError("missing required argument: path")
    path = args["path"]
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        _PAGE.pdf(path=path)
        out = {"path": path, "bytes": os.path.getsize(path), "url": _PAGE.url}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_pdf failed", "detail": str(e)}


def do_browser_trace_start(args):
    global _TRACING, _TRACE_OPTS
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    screenshots = args.get("screenshots", True)
    snapshots = args.get("snapshots", True)
    if not isinstance(screenshots, bool) or not isinstance(snapshots, bool):
        raise ValueError("screenshots and snapshots must be booleans")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        _TRACE_OPTS = {"screenshots": screenshots, "snapshots": snapshots}
        _CTX.tracing.start(screenshots=screenshots, snapshots=snapshots)
        _TRACING = True
        out = {"ok": True}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_trace_start failed", "detail": str(e)}


def do_browser_trace_stop(args):
    global _TRACING
    if not isinstance(args, dict):
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    path = args.get("path", "trace.zip")
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty string")
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        _CTX.tracing.stop(path=path)
        _TRACING = False
        out = {"path": path, "bytes": os.path.getsize(path)}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_trace_stop failed", "detail": str(e)}


def do_browser_video_path(args):
    try:
        installed_now = _ensure_browser()
    except Exception as e:
        return {"error": "browser unavailable", "detail": str(e)}
    try:
        video = _PAGE.video
        if video is None:
            return {"path": None, "detail": "no video recording for this page; pass record_video_dir to browser_context_new first"}
        try:
            p = video.path()
        except Exception as e:
            return {"path": None, "detail": "video path not yet available: %s" % e}
        out = {"path": p}
        if installed_now:
            out["installed_now"] = True
        return out
    except Exception as e:
        return {"error": "browser_video_path failed", "detail": str(e)}


# ---------------- MCP wiring (mirrors memory_mcp.py) ----------------

TOOLS = [
    {
        "name": "tools_status",
        "description": "Report whether playwright, chromium, yt-dlp and aria2c are installed, with details.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "tools_install",
        "description": "Install a missing helper tool (playwright, yt-dlp, aria2, or all) and return per-item status with log tails.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "enum": ["all", "playwright", "yt-dlp", "aria2"]},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search",
        "description": "Search the web via keyless DuckDuckGo HTML endpoints and return titles, urls and snippets.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "num_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch",
        "description": "Read any web page via Jina Reader (r.jina.ai) as clean markdown; falls back to direct fetch and says which source was used.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "format": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
                "max_chars": {"type": "integer", "default": 20000},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_open",
        "description": "Open a URL in headless Chromium (auto-installs playwright on first use) and return the page title and text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "wait_ms": {"type": "integer", "default": 1500},
            },
            "required": ["url"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click a page element by its visible text (or a CSS selector starting with . # or >) in the open headless page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_screenshot",
        "description": "Save a screenshot of the open headless page to a file path (optional full_page or selector).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "full_page": {"type": "boolean", "default": False},
                "selector": {"type": ["string", "null"], "default": None},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_extract",
        "description": "Extract the visible text of a CSS selector (default body) from the open headless page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body"},
            },
        },
    },
    {
        "name": "browser_tabs",
        "description": "List open browser tabs (pages) with index, url, title and which is active.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_switch_tab",
        "description": "Switch the active page to the tab at index (or whose url contains url_contains).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": {"type": ["integer", "null"], "default": None},
                "url_contains": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_close_tab",
        "description": "Close a tab by index (default: the active tab). Refuses to close the last tab.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": {"type": ["integer", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_close",
        "description": "Close the headless browser entirely. The next browser tool re-launches it lazily.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_info",
        "description": "Return the current page url/title plus tab count, viewport and event counters.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click_download",
        "description": "Click an element and save the resulting download (expect_download+save_as); returns path/filename/bytes/url.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "output_path": {"type": ["string", "null"], "default": None},
                "timeout_ms": {"type": "integer", "default": 30000},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_network_log",
        "description": "Return recent captured network request/response events.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    {
        "name": "browser_network_clear",
        "description": "Clear the captured network and websocket logs.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_har_save",
        "description": "Write the captured network log to a HAR 1.2 JSON file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_websocket_log",
        "description": "Return recent captured websocket frames (sent/received).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    {
        "name": "browser_route",
        "description": "Route URL patterns: abort, continue, fulfill with status/body, or off to remove.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "action": {"type": "string", "enum": ["abort", "continue", "fulfill", "off"], "default": "abort"},
                "status": {"type": ["integer", "null"], "default": None},
                "body": {"type": ["string", "null"], "default": None},
                "content_type": {"type": ["string", "null"], "default": None},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "browser_get_attribute",
        "description": "Return the value of an attribute on the first element matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["selector", "name"],
        },
    },
    {
        "name": "browser_eval",
        "description": "Evaluate JavaScript in the page and return the JSON result. Scripts that read page secrets AND send them over the network are blocked unless allow_exfil is true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "arg": {"description": "Optional JSON value passed as the second evaluate() argument."},
                "allow_exfil": {"type": "boolean", "default": False},
            },
            "required": ["script"],
        },
    },
    {
        "name": "browser_init_script",
        "description": "Register an init script applied to every new browser context (and the live one).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
            },
            "required": ["script"],
        },
    },
    {
        "name": "browser_inject_script",
        "description": "Inject a script tag (inline content or url) into the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": ["string", "null"], "default": None},
                "url": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_inject_style",
        "description": "Inject a style tag (inline content or url) into the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": ["string", "null"], "default": None},
                "url": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_fill",
        "description": "Fill an input matching a CSS selector with text (clears first).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_type",
        "description": "Type text key-by-key into an element matching a CSS selector (optional per-key delay_ms).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "delay_ms": {"type": "integer", "default": 0},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_press",
        "description": "Press a key (e.g. Enter, Tab, Escape) on an element matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["selector", "key"],
        },
    },
    {
        "name": "browser_clear",
        "description": "Clear the value of an input matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_focus",
        "description": "Focus the element matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_blur",
        "description": "Blur (remove focus from) the element matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_check",
        "description": "Check a checkbox or radio matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_uncheck",
        "description": "Uncheck a checkbox matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_select",
        "description": "Select option values in a <select> matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "values": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["selector", "values"],
        },
    },
    {
        "name": "browser_upload",
        "description": "Set file input files for an <input type=file> matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["selector", "paths"],
        },
    },
    {
        "name": "browser_hover",
        "description": "Hover the mouse over the element matching a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_drag",
        "description": "Drag from the element matching source selector onto the target selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["source", "target"],
        },
    },
    {
        "name": "browser_scroll",
        "description": "Scroll an element into view (selector) or scroll the page by x/y pixels.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": ["string", "null"], "default": None},
                "x": {"type": "integer", "default": 0},
                "y": {"type": "integer", "default": 500},
            },
        },
    },
    {
        "name": "browser_tap",
        "description": "Tap the element matching a CSS selector (touch).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_state",
        "description": "Return visibility/enabled/editable/checked state and value of a CSS selector.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
            },
            "required": ["selector"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Return the accessibility snapshot of the current page (truncated).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_html",
        "description": "Return page HTML (or inner HTML of a selector), truncated with a flag.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_console_errors",
        "description": "Return captured console error messages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100},
            },
        },
    },
    {
        "name": "browser_back",
        "description": "Navigate back in history.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_forward",
        "description": "Navigate forward in history.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_reload",
        "description": "Reload the current page.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "wait_until": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle", "commit"], "default": "load"},
            },
        },
    },
    {
        "name": "browser_wait_for",
        "description": "Wait for a selector/text/url or a fixed ms delay.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": ["string", "null"], "default": None},
                "text": {"type": ["string", "null"], "default": None},
                "url_substring": {"type": ["string", "null"], "default": None},
                "ms": {"type": "integer", "default": 1000},
                "state": {"type": "string", "enum": ["visible", "hidden", "attached", "detached"], "default": "visible"},
            },
        },
    },
    {
        "name": "browser_set_timeout",
        "description": "Set the default timeout (ms) for page actions.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ms": {"type": "integer"},
            },
            "required": ["ms"],
        },
    },
    {
        "name": "browser_add_overlay_handler",
        "description": "Auto-handle unexpected dialogs/overlays: accept, dismiss, or off.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["accept", "dismiss", "off"]},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_dialog",
        "description": "Accept or dismiss the pending page dialog (optionally answering a prompt).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["accept", "dismiss"]},
                "prompt_text": {"type": ["string", "null"], "default": None},
            },
            "required": ["action"],
        },
    },
    {
        "name": "browser_dialog_last",
        "description": "Return the most recently seen page dialog.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_frames",
        "description": "List frames in the current page with index, name and url.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_frame_click",
        "description": "Click text/selector inside a frame (by index, name or url_contains).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "index": {"type": ["integer", "null"], "default": None},
                "name": {"type": ["string", "null"], "default": None},
                "url_contains": {"type": ["string", "null"], "default": None},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_frame_extract",
        "description": "Extract visible text of a selector inside a frame.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "default": "body"},
                "index": {"type": ["integer", "null"], "default": None},
                "name": {"type": ["string", "null"], "default": None},
                "url_contains": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_frame_fill",
        "description": "Fill an input inside a frame (by index, name or url_contains).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "index": {"type": ["integer", "null"], "default": None},
                "name": {"type": ["string", "null"], "default": None},
                "url_contains": {"type": ["string", "null"], "default": None},
            },
            "required": ["selector", "text"],
        },
    },
    {
        "name": "browser_cookies_get",
        "description": "Return cookies of the current browser context.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_cookies_set",
        "description": "Add cookies to the current browser context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cookies": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["cookies"],
        },
    },
    {
        "name": "browser_cookies_clear",
        "description": "Clear all cookies of the current browser context.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_storage_save",
        "description": "Save context storage state (cookies+localStorage) to a file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_storage_load",
        "description": "Load storage state from a file into a fresh browser context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_context_new",
        "description": "Replace the browser context (viewport, user agent, locale, permissions, headers, storage, video dir).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "viewport": {"type": ["object", "null"], "default": None},
                "user_agent": {"type": ["string", "null"], "default": None},
                "locale": {"type": ["string", "null"], "default": None},
                "timezone_id": {"type": ["string", "null"], "default": None},
                "storage_state": {"type": ["string", "null"], "default": None},
                "ignore_https_errors": {"type": ["boolean", "null"], "default": None},
                "permissions": {"type": ["array", "null"], "items": {"type": "string"}, "default": None},
                "headers": {"type": ["object", "null"], "default": None},
                "record_video_dir": {"type": ["string", "null"], "default": None},
            },
        },
    },
    {
        "name": "browser_grant_permissions",
        "description": "Grant context permissions (e.g. geolocation, notifications).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "permissions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["permissions"],
        },
    },
    {
        "name": "browser_set_headers",
        "description": "Set extra HTTP headers for all context requests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "headers": {"type": "object"},
            },
            "required": ["headers"],
        },
    },
    {
        "name": "browser_pdf",
        "description": "Save the current page as PDF.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "browser_trace_start",
        "description": "Start playwright tracing on the current context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "screenshots": {"type": "boolean", "default": True},
                "snapshots": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "browser_trace_stop",
        "description": "Stop tracing and save the trace archive.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "trace.zip"},
            },
        },
    },
    {
        "name": "browser_video_path",
        "description": "Return the video recording path of the current page (needs record_video_dir).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "yt_download",
        "description": "Download media with yt-dlp (auto-installed on first use); supports audio-only mp3 extraction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_dir": {"type": "string", "default": "."},
                "audio_only": {"type": "boolean", "default": False},
                "filename": {"type": ["string", "null"], "default": None},
            },
            "required": ["url"],
        },
    },
    {
        "name": "aria2_download",
        "description": "Download a file with aria2c using multiple connections (auto-installed on first use).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "output_dir": {"type": "string", "default": "."},
                "filename": {"type": ["string", "null"], "default": None},
                "connections": {"type": "integer", "default": 4},
            },
            "required": ["url"],
        },
    },
]

HANDLERS = {
    "tools_status": do_tools_status,
    "tools_install": do_tools_install,
    "search": do_search,
    "fetch": do_fetch,
    "browser_open": do_browser_open,
    "browser_click": do_browser_click,
    "browser_screenshot": do_browser_screenshot,
    "browser_extract": do_browser_extract,
    "browser_tabs": do_browser_tabs,
    "browser_switch_tab": do_browser_switch_tab,
    "browser_close_tab": do_browser_close_tab,
    "browser_close": do_browser_close,
    "browser_info": do_browser_info,
    "browser_click_download": do_browser_click_download,
    "browser_network_log": do_browser_network_log,
    "browser_network_clear": do_browser_network_clear,
    "browser_har_save": do_browser_har_save,
    "browser_websocket_log": do_browser_websocket_log,
    "browser_route": do_browser_route,
    "browser_get_attribute": do_browser_get_attribute,
    "browser_eval": do_browser_eval,
    "browser_init_script": do_browser_init_script,
    "browser_inject_script": do_browser_inject_script,
    "browser_inject_style": do_browser_inject_style,
    "browser_fill": do_browser_fill,
    "browser_type": do_browser_type,
    "browser_press": do_browser_press,
    "browser_clear": do_browser_clear,
    "browser_focus": do_browser_focus,
    "browser_blur": do_browser_blur,
    "browser_check": do_browser_check,
    "browser_uncheck": do_browser_uncheck,
    "browser_select": do_browser_select,
    "browser_upload": do_browser_upload,
    "browser_hover": do_browser_hover,
    "browser_drag": do_browser_drag,
    "browser_scroll": do_browser_scroll,
    "browser_tap": do_browser_tap,
    "browser_state": do_browser_state,
    "browser_snapshot": do_browser_snapshot,
    "browser_html": do_browser_html,
    "browser_console_errors": do_browser_console_errors,
    "browser_back": do_browser_back,
    "browser_forward": do_browser_forward,
    "browser_reload": do_browser_reload,
    "browser_wait_for": do_browser_wait_for,
    "browser_set_timeout": do_browser_set_timeout,
    "browser_add_overlay_handler": do_browser_add_overlay_handler,
    "browser_dialog": do_browser_dialog,
    "browser_dialog_last": do_browser_dialog_last,
    "browser_frames": do_browser_frames,
    "browser_frame_click": do_browser_frame_click,
    "browser_frame_extract": do_browser_frame_extract,
    "browser_frame_fill": do_browser_frame_fill,
    "browser_cookies_get": do_browser_cookies_get,
    "browser_cookies_set": do_browser_cookies_set,
    "browser_cookies_clear": do_browser_cookies_clear,
    "browser_storage_save": do_browser_storage_save,
    "browser_storage_load": do_browser_storage_load,
    "browser_context_new": do_browser_context_new,
    "browser_grant_permissions": do_browser_grant_permissions,
    "browser_set_headers": do_browser_set_headers,
    "browser_pdf": do_browser_pdf,
    "browser_trace_start": do_browser_trace_start,
    "browser_trace_stop": do_browser_trace_stop,
    "browser_video_path": do_browser_video_path,
    "yt_download": do_yt_download,
    "aria2_download": do_aria2_download,
}

# Legacy web_browser_* aliases for the original four tools (kept working).
for _alias, _orig in (
    ("web_browser_open", "browser_open"),
    ("web_browser_click", "browser_click"),
    ("web_browser_screenshot", "browser_screenshot"),
    ("web_browser_extract", "browser_extract"),
):
    _src = next(t for t in TOOLS if t["name"] == _orig)
    TOOLS.append({
        "name": _alias,
        "description": _src["description"] + " (Legacy alias of %s.)" % _orig,
        "inputSchema": _src["inputSchema"],
    })
    HANDLERS[_alias] = HANDLERS[_orig]


def handle_message(msg):
    method = msg.get("method") if isinstance(msg, dict) else None
    has_id = isinstance(msg, dict) and "id" in msg
    req_id = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg, dict) or not isinstance(method, str):
        if has_id:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32600, "message": "Invalid Request"}}
        return None
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        pv = params.get("protocolVersion") if isinstance(params, dict) else None
        if not isinstance(pv, str):
            pv = "2024-11-05"
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "web", "version": "1.0.0"},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if not isinstance(params, dict):
            params = {}
        name = params.get("name")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        try:
            if name not in HANDLERS:
                raise ValueError("unknown tool: %r" % (name,))
            result = HANDLERS[name](arguments)
            if isinstance(result, (dict, list)):
                text = json.dumps(result)
            elif not isinstance(result, str):
                text = str(result)
            else:
                text = result
            return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text}]}}
        except Exception as e:
            try:
                emsg = str(e) if str(e) else repr(e)
            except Exception:
                emsg = "error"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": "Error: " + emsg}], "isError": True},
            }
    if not has_id:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}


def main():
    stdin = sys.stdin
    stdout = sys.stdout
    while True:
        line = stdin.readline()
        if line == "":
            break
        if line.strip() == "":
            continue
        try:
            msg = json.loads(line)
        except Exception:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        try:
            resp = handle_message(msg)
        except Exception as e:
            try:
                rid = msg.get("id") if isinstance(msg, dict) and "id" in msg else None
            except Exception:
                rid = None
            if rid is None and not (isinstance(msg, dict) and "id" in msg):
                continue
            resp = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": "Internal error: " + str(e)}}
        if resp is None:
            continue
        stdout.write(json.dumps(resp) + "\n")
        stdout.flush()


if __name__ == "__main__":
    main()
