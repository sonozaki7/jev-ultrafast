"""Observed actions through Browser Harness; one CDP session, no per-step subprocess.

One persistent background tab per site is kept in the owner's Chrome and reused
by later runs, so repeated visits and checks never pile up duplicate tabs.
"""

import fcntl
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"

STATE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "jev-ultrafast"
TABS_FILE = STATE_DIR / "tabs.json"
LOCKS_DIR = STATE_DIR / "locks"
# The owner may park this tool's tab inside one of their own Chrome tab groups
# (Chrome exposes no group API, so the tool can never group a tab itself), and a
# tab in daily use must never be closed just to save space. Only tabs untouched
# for a month are cleaned up.
STALE_AFTER = 30 * 24 * 60 * 60


def site_key(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url


def acquire_site_lock(url):
    """Hold an exclusive per-site lock for the whole run.

    Two runs on the same site then queue instead of opening two tabs; runs on
    different sites still proceed in parallel.
    """
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCKS_DIR / f"{hashlib.sha256(site_key(url).encode()).hexdigest()[:16]}.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("jev-browser: another run is using this site; waiting for its tab...", file=sys.stderr)
        fcntl.flock(handle, fcntl.LOCK_EX)
    return handle


def load_tabs():
    try:
        data = json.loads(TABS_FILE.read_text())
        return [t for t in data if isinstance(t, dict) and t.get("id")]
    except (OSError, ValueError):
        return []


def save_tabs(tabs):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TABS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(tabs))
        os.replace(tmp, TABS_FILE)
    except OSError:
        pass


def close_quietly(target):
    try:
        cdp("Target.closeTarget", targetId=target)
    except Exception:
        pass


def site_busy(site):
    """True while another run holds this site's lock, so its tab must not be pruned."""
    try:
        LOCKS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOCKS_DIR / f"{hashlib.sha256(site.encode()).hexdigest()[:16]}.lock", "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        return True


def claim_tab(url):
    """Reuse this tool's live tab for the site when there is one, else open one.

    The registry is written before the run starts, so a crashed or interrupted
    run still leaves its tab discoverable and the next run reuses it. Tabs are
    kept until unused for a month; one the owner has parked in their own tab
    group therefore survives as long as the site keeps being used.
    """
    site = site_key(url)
    tabs = load_tabs()
    try:
        live = {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}
    except RuntimeError:
        live = set()
    tabs = [t for t in tabs if t["id"] in live]
    mine = [t for t in tabs if t.get("site") == site]
    if mine:
        current = max(mine, key=lambda t: t.get("used", 0))
        for extra in mine:
            if extra is not current:
                tabs.remove(extra)
                close_quietly(extra["id"])
        current["used"] = time.time()
        current["url"] = url
        save_tabs(tabs)
        return current["id"], True
    target = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    tabs.append({"id": target, "site": site, "url": url, "used": time.time()})
    cutoff = time.time() - STALE_AFTER
    stale = [
        t for t in tabs
        if t["id"] != target and t.get("used", 0) < cutoff and not site_busy(t.get("site", ""))
    ]
    for old in stale:
        tabs.remove(old)
        close_quietly(old["id"])
    save_tabs(tabs)
    return target, False


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.lock = acquire_site_lock(url)
        self.target, self.reused = claim_tab(url)
        self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
        self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
        # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
        self.call("Emulation.setFocusEmulationEnabled", enabled=True)
        self.call("Page.navigate", url=url)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.02)

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.call(
                    "Runtime.evaluate",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if type(node) is not int:
                return False
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def close(self):
        # The tab stays open on purpose: the next run on this site reuses it.
        self.target = None
        if getattr(self, "lock", None):
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
            self.lock = None


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return null;
              if (!e.contains(document.elementFromPoint(x,y))) return null;
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return null;
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y};
            })(""" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent",
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    call("Input.insertText", text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
