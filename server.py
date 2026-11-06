#!/usr/bin/env python3
"""token-chart 网页服务（N100）：出图页面 + 「更新数据」按钮 + 上次更新日期。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(os.environ.get("TOKEN_CHART_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(ROOT))
import build as _build  # noqa: E402  （同一份出图逻辑，供 data.json 用）
HTML = ROOT / "token-usage.html"
STATE = ROOT / "state.json"
UPDATE = ROOT / "update.py"
PORT = int(os.environ.get("TOKEN_CHART_PORT", "6186"))
HOST = os.environ.get("TOKEN_CHART_HOST", "0.0.0.0")

_lock = threading.Lock()
_proc = None


def state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


INJECT = """
<style>
#tcBox{position:fixed;right:18px;bottom:18px;z-index:9999;display:flex;gap:10px;align-items:center;
  font:13px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;background:#fcfcfb;
  border:1px solid rgba(11,11,11,.10);border-radius:12px;padding:8px 12px;box-shadow:0 4px 16px rgba(0,0,0,.10)}
#tcBox button{font:inherit;font-weight:600;padding:6px 14px;border-radius:9px;border:1px solid rgba(11,11,11,.16);
  background:#6250d6;color:#fff;cursor:pointer}
#tcBox button:disabled{opacity:.6;cursor:default}
#tcBox .st{color:#52514e}
</style>
<div id="tcBox"><button id="tcBtn">更新数据</button><span class="st" id="tcSt">…</span></div>
<script>
(function(){
  var btn=document.getElementById('tcBtn'), st=document.getElementById('tcSt');
  function paint(s){
    if(!s){ st.textContent='状态未知'; return; }
    if(s.busy){ st.textContent='正在更新…'; return; }
    var e=s.last_export_at||'', b=s.last_build_at||'';
    st.textContent = e ? ('上次更新：'+e) : ('未导出过（仅合并本地）'+(b?('，页面 '+b):''));
    if (s.last_export_ok === false && s.last_run_at) {
      st.textContent += ' ⚠️ 最近一次导出失败（token 可能过期），点右边按钮重试';
      st.style.color = '#b42318';
    } else { st.style.color = ''; }
    var bn=document.getElementById('banner');
    if(bn && !bn.dataset.lu){ bn.dataset.lu='1'; if(e) bn.textContent += ' ｜ 上次自动导出：'+e; }
  }
  // 页面可能挂在 /token/ 这样的子路径下，接口必须跟着前缀走，否则请求会打到站点根
  var BASE=(function(){var p=location.pathname||'/';if(p.slice(-1)!=='/'){var i=p.lastIndexOf('/');p=p.slice(0,i+1)||'/';}return p;})();
  function status(){ return fetch(BASE+'api/status',{cache:'no-store'}).then(function(r){return r.json();}); }
  function show(){ status().then(paint).catch(function(){}); }
  btn.addEventListener('click', function(){
    btn.disabled=true; btn.textContent='更新中…';
    fetch(BASE+'refresh',{method:'POST'}).catch(function(){});
    var n=0;
    var t=setInterval(function(){
      n++;
      status().then(function(s){
        paint(s);
        if(s && !s.busy && n>1){ clearInterval(t); location.reload(); }
        if(n>180){ clearInterval(t); btn.disabled=false; btn.textContent='更新数据'; }
      }).catch(function(){});
    }, 2000);
  });
  show(); setInterval(show, 20000);
})();
</script>
"""


def page() -> bytes:
    try:
        html = HTML.read_text(encoding="utf-8")
    except Exception:
        return "<h1>还没有 token-usage.html</h1>".encode("utf-8")
    # 页面里的 /data.json、/ledger 是绝对路径；挂到 /token/ 下会被打到站点根。
    # 改成相对路径后，根路径和子路径都成立。
    html = html.replace('fetch("/data.json"', 'fetch("data.json"')
    html = html.replace('fetch("/ledger"', 'fetch("ledger"')
    if "</body>" in html:
        html = html.replace("</body>", INJECT + "</body>", 1)
    else:
        html += INJECT
    return html.encode("utf-8")


def start_update() -> bool:
    global _proc
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return False
        _proc = subprocess.Popen([sys.executable, str(UPDATE)], cwd=str(ROOT),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True


class H(BaseHTTPRequestHandler):
    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", code)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/token/", "/token"):
            self._send(page())
        elif path == "/api/status":
            st = state()
            st.setdefault("busy", False)
            self._json(st)
        elif path == "/api/state":
            self._json(state())
        elif path == "/data.json":
            try:
                self._json(_build.payload())
            except Exception as exc:  # noqa: BLE001
                self._send(f"出错：{exc!r}".encode("utf-8"), "text/plain; charset=utf-8", 500)
        elif path == "/favicon.ico":
            self._send(b"", "image/x-icon", 204)
        else:
            self._send(b"not found", "text/plain; charset=utf-8", 404)

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/refresh":
            ok = start_update()
            self._json({"ok": ok, "started": ok, "busy": not ok}, 200 if ok else 409)
        elif path == "/ledger":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                obj = json.loads(raw.decode("utf-8-sig", "replace"))
                _build.LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
                _build.LEDGER_FILE.write_text(
                    json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
                self._json({"ok": True})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": repr(exc)}, 500)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"[token-chart] http://{HOST}:{PORT}/", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
