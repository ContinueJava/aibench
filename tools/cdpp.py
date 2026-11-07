"""Minimal Chrome-DevTools-Protocol driver (no Playwright, no Node).

Why: this machine has no `bsk` CLI and no Playwright, but Edge is installed and
`websocket-client` is available, so we can drive a headless Edge directly to
take real screenshots of the running WebUI.

Usage:
    python tools/cdpp.py --url http://127.0.0.1:8000/ --shot out.png
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from typing import Any

import websocket  # websocket-client

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str:
    for path in EDGE_CANDIDATES:
        if os.path.exists(path):
            return path
    raise SystemExit("no Edge/Chrome found")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """A headless Chromium you can evaluate JS in and screenshot."""

    def __init__(self, width: int = 1440, height: int = 960, scale: float = 2.0,
                 profile: str | None = None):
        self.width, self.height, self.scale = width, height, scale
        self.port = free_port()
        self.profile = profile or os.path.join(tempfile.gettempdir(), "gb-edge-profile")
        os.makedirs(self.profile, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                find_browser(),
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--user-data-dir={self.profile}",
                f"--window-size={width},{height}",
                "--hide-scrollbars",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--force-device-scale-factor=%s" % scale,
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.ws = websocket.create_connection(self._target_ws(), timeout=60)
        self._id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")
        self.call("Emulation.setDeviceMetricsOverride", {
            "width": width, "height": height,
            "deviceScaleFactor": scale, "mobile": False,
        })

    # -- plumbing -----------------------------------------------------------
    def _target_ws(self) -> str:
        deadline = time.time() + 30
        last = ""
        while time.time() < deadline:
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=2
                ).read()
                targets = json.loads(raw)
                for t in targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        return t["webSocketDebuggerUrl"]
            except Exception as exc:  # browser still starting
                last = str(exc)
            time.sleep(0.2)
        raise SystemExit(f"devtools never came up on {self.port}: {last}")

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    # -- actions ------------------------------------------------------------
    def goto(self, url: str, settle: float = 2.0) -> None:
        self.call("Page.navigate", {"url": url})
        time.sleep(settle)

    def js(self, expression: str, await_promise: bool = True) -> Any:
        out = self.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        })
        if "exceptionDetails" in out:
            raise RuntimeError(str(out["exceptionDetails"])[:600])
        return out.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout: float = 30.0, label: str = "") -> Any:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(expression):
                    return True
            except Exception:
                pass
            time.sleep(0.25)
        raise SystemExit(f"timeout waiting for {label or expression!r}")

    def click(self, selector: str) -> None:
        self.js(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("no element " + {json.dumps(selector)});
            el.scrollIntoView({{block:"center"}});
            el.click();
            return true;
        }})()""")

    def click_text(self, text: str, tag: str = "*") -> None:
        self.js(f"""(() => {{
            const t = {json.dumps(text)};
            const els = [...document.querySelectorAll({json.dumps(tag)})];
            const el = els.find(e => (e.textContent||"").trim().includes(t)
                                     && e.offsetParent !== null);
            if (!el) throw new Error("no clickable with text " + t);
            el.scrollIntoView({{block:"center"}});
            el.click();
            return true;
        }})()""")

    def set_input(self, selector: str, value: str) -> None:
        self.js(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("no input " + {json.dumps(selector)});
            el.focus();
            el.value = {json.dumps(value)};
            el.dispatchEvent(new Event("input", {{bubbles:true}}));
            el.dispatchEvent(new Event("change", {{bubbles:true}}));
            el.blur();
            return true;
        }})()""")

    def shot(self, path: str) -> str:
        data = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        import base64

        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data["data"]))
        return path

    def full_shot(self, path: str) -> str:
        """Screenshot the whole document, not just the viewport."""
        import base64

        metrics = self.call("Page.getLayoutMetrics")
        css = metrics.get("cssContentSize") or metrics.get("contentSize")
        data = self.call("Page.captureScreenshot", {
            "format": "png",
            "captureBeyondViewport": True,
            "clip": {"x": 0, "y": 0, "width": css["width"],
                     "height": css["height"], "scale": 1},
        })
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data["data"]))
        return path

    def scroll_to(self, selector: str, block: str = "center") -> None:
        self.js(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) throw new Error("no element " + {json.dumps(selector)});
            el.scrollIntoView({{block: {json.dumps(block)}}});
            return true;
        }})()""")
        time.sleep(0.25)

    def scroll_y(self, y: int) -> None:
        self.js(f"window.scrollTo(0, {int(y)}); true")
        time.sleep(0.2)

    def rect(self, selector: str) -> dict[str, float] | None:
        return self.js(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {{x: r.x, y: r.y, w: r.width, h: r.height}};
        }})()""")

    def annotate(self, step: str, title: str, note: str = "",
                 rings: list[dict[str, str]] | None = None,
                 pad: int = 150) -> None:
        """Draw a fixed overlay: step banner on top, note bar at the bottom and
        optional numbered callout rings around page elements.

        ``rings`` items look like ``{"sel": "#pf-base", "label": "1", "text": "..."}``.
        Rects are measured in a SECOND pass, after the overlay (and the extra
        bottom padding it adds) is already in the DOM, so the callouts always
        line up with what the screenshot actually shows.

        ``pad`` is the extra bottom padding kept free for the note bar. Raise it
        when the element you want to ring is the LAST thing on the page: without
        the extra room the page cannot scroll it up out from under the note bar.
        Callers that raise it should set the same padding BEFORE scrolling, then
        scroll, then call this -- the rings are measured at call time.
        """
        self.js(
            "(function () {\n"
            "  var old = document.getElementById('__anno');\n"
            "  if (old) old.parentNode.removeChild(old);\n"
            "  var wrap = document.createElement('div');\n"
            "  wrap.id = '__anno';\n"
            "  wrap.style.cssText = 'position:fixed;inset:0;pointer-events:none;"
            "z-index:2147483647;font-family:system-ui,\"Microsoft YaHei\",sans-serif';\n"
            "  var bar = document.createElement('div');\n"
            "  bar.style.cssText = 'position:absolute;left:0;right:0;top:0;display:flex;"
            "align-items:center;gap:14px;padding:14px 24px;"
            "background:linear-gradient(90deg,rgba(2,132,199,.97),rgba(79,70,229,.97));"
            "color:#fff;box-shadow:0 2px 14px rgba(0,0,0,.45)';\n"
            "  var badge = document.createElement('span');\n"
            "  badge.textContent = " + json.dumps(step, ensure_ascii=False) + ";\n"
            "  badge.style.cssText = 'flex:0 0 auto;min-width:34px;height:34px;"
            "border-radius:17px;background:#fff;color:#0369a1;font-weight:800;"
            "font-size:17px;display:flex;align-items:center;justify-content:center;"
            "padding:0 9px';\n"
            "  var tt = document.createElement('span');\n"
            "  tt.textContent = " + json.dumps(title, ensure_ascii=False) + ";\n"
            "  tt.style.cssText = 'font-size:19px;font-weight:700;letter-spacing:.4px';\n"
            "  bar.appendChild(badge); bar.appendChild(tt); wrap.appendChild(bar);\n"
            "  if (" + json.dumps(note, ensure_ascii=False) + ") {\n"
            "    var nb = document.createElement('div');\n"
            "    nb.innerHTML = " + json.dumps(note, ensure_ascii=False) + ";\n"
            "    nb.style.cssText = 'position:absolute;left:0;right:0;bottom:0;"
            "padding:13px 24px;background:rgba(2,6,23,.94);color:#e2e8f0;"
            "font-size:14px;line-height:1.65;border-top:2px solid #38bdf8';\n"
            "    wrap.appendChild(nb);\n"
            "  }\n"
            "  document.body.appendChild(wrap);\n"
            "  document.body.style.paddingBottom = '" + str(int(pad)) + "px';\n"
            "  return true;\n"
            "})()"
        )
        time.sleep(0.2)
        for ring in rings or []:
            self.js(
                "(function () {\n"
                "  var wrap = document.getElementById('__anno');\n"
                "  var el = document.querySelector(" + json.dumps(ring["sel"]) + ");\n"
                "  if (!wrap || !el) return false;\n"
                "  var b = el.getBoundingClientRect();\n"
                "  var pad = 5;\n"
                "  var box = document.createElement('div');\n"
                "  box.style.cssText = 'position:absolute;border:2.5px dashed #f59e0b;"
                "border-radius:10px;box-shadow:0 0 0 3px rgba(245,158,11,.18);"
                "left:' + (b.left - pad) + 'px;top:' + (b.top - pad) + 'px;"
                "width:' + (b.width + pad * 2) + 'px;height:' + (b.height + pad * 2) + 'px';\n"
                "  var chip = document.createElement('div');\n"
                "  chip.textContent = " + json.dumps(
                    (ring.get("label", "") + ("  " + ring["text"] if ring.get("text") else "")),
                    ensure_ascii=False) + ";\n"
                "  chip.style.cssText = 'position:absolute;left:-2.5px;top:-30px;"
                "background:#f59e0b;color:#1c1917;font-weight:700;font-size:13px;"
                "padding:3px 9px;border-radius:8px;white-space:nowrap;"
                "box-shadow:0 2px 8px rgba(0,0,0,.4)';\n"
                "  box.appendChild(chip); wrap.appendChild(box);\n"
                "  return true;\n"
                "})()"
            )
        time.sleep(0.15)


    def close(self) -> None:
        try:
            self.ws.close()
        finally:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/")
    ap.add_argument("--shot", default="shot.png")
    args = ap.parse_args()
    b = Browser()
    try:
        b.goto(args.url)
        b.shot(args.shot)
        print("saved", args.shot)
    finally:
        b.close()


if __name__ == "__main__":
    main()
