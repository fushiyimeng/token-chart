r"""大肥鱼（DeepSeek）用量图 —— 生成一份自带刷新的网页。

两种用法：
    python build.py                 # 生成静态单文件 token-usage.html（发群里用）
    python build.py --serve         # 起个小服务 + 自动开浏览器：**页面上的"刷新"按钮
                                    # 会现读 CSV 重画**，不用再跑脚本也不用重开文件
    python build.py <amount.csv> <cost.csv> [-o 输出.html]

CSV 从哪找（按顺序，**先找相对目录**）：
    1. 显式传的两个路径
    2. 本目录（token-chart\）下的 usage_data_* 文件夹
    3. 本目录\data\ 下的 usage_data_*
    4. 本目录 / data 下直接躺着的 amount-*.csv + cost-*.csv
    5. 最后才去下载目录找最新的 usage_data_*
⇒ 你只要把平台导出的那个文件夹（或两个 csv）丢进 token-chart\ 或 token-chart\data\，
   页面上点一下刷新就是最新的。

图里六条（默认全开，**每条一个复选框，复选框本身就是图例**）：
    · 柱：每天 token 量，按模型堆叠（紫 = flash 便宜、橙 = v4-pro 贵）
    · 花费（元）
    · 当天每元 token
    · 累计每元 token（从第一天累加到那天，总 token ÷ 总花费）
    · 增长 = **累计每元的日增量**（比昨天涨了多少；**零线**上方是正、下方是负）
纵轴 0~125%：每条按**自己的最大值**归一化（满格 100%，上面留 25% 给标注），
⚠️ 不画双纵轴 —— 量级差上百倍，硬画一根真实轴上等于凭空造"相关性"。

配色走 dataviz 技能的参考调色板（紫/橙/黄/品红/绿/青），浅色深色两套都过校验；
折线那四色 CVD 落在 6~8 区间，规矩是"必须配合直接标注"—— 我们每个点都标数字，符合。
"""

from __future__ import annotations

import csv
import datetime as dt
import http.server
import io
import json
import socketserver
import sys

try:                       # Windows 控制台默认 GBK，打「¥」会炸
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass
import webbrowser
import zipfile
from pathlib import Path

# 打包成 exe 后，__file__ 指向临时解包目录，得用 exe 自己所在的位置
HERE = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent)
TOKEN_TYPES = {"output_tokens", "input_cache_miss_tokens", "input_cache_hit_tokens"}
CHEAP = "deepseek-flash"
PRICEY = "deepseek-v4-pro"

# 浅色：紫 #6250d6、橙 #eb6834、黄 #eda100、品红 #e87ba4、绿 #008300、青 #1baf7a
# 深色：紫 #9085e9、橙 #d95926、黄 #c98500、品红 #d55181、绿 #008300、青 #199e70
# ⚠️ 紫+蓝过不了（ΔE 4.5），所以贵模型用橙不用蓝；六色全 all-pairs 过不了，
#    折线那四条（黄/品红/绿/青）实测 light/dark 都是"通过 + CVD 6.1（需配合标注）"。
#    改颜色请重新跑 dataviz 的 validate_palette.js，别凭眼睛挑。


def newest_dir(dirs) -> Path | None:
    ds = [d for d in dirs if d.is_dir()]
    return max(ds, key=lambda d: d.stat().st_mtime) if ds else None


def _zip_pairs(z: Path) -> list[tuple[tuple, tuple]]:
    """一个 zip 里可能有 (amount, cost) 一对（平台导出就是这样）。

    ⚠️ 2026-10-19 用户："只发一个下载好的压缩文件，不解压，能不能识别" —— 现在能。
    这里不落盘解压，直接按成员名配对（amount-xxx.csv ↔ cost-xxx.csv 同后缀）。
    """
    out: list[tuple[tuple, tuple]] = []
    try:
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
    except Exception:
        return out
    amts = [n for n in names
            if Path(n).name.startswith("amount-") and n.lower().endswith(".csv")]
    costs = [n for n in names
             if Path(n).name.startswith("cost-") and n.lower().endswith(".csv")]
    for a in amts:
        suffix = Path(a).name[len("amount-"):]
        c = next((c for c in costs if Path(c).name[len("cost-"):] == suffix), None)
        if c:
            out.append((("zip", z, a), ("zip", z, c)))
    return out


def _rows(ref) -> list[dict]:
    """读一份 CSV：既支持磁盘文件，也支持 zip 里的成员（ref = ("zip", 路径, 成员)）。"""
    if isinstance(ref, tuple) and len(ref) == 3 and ref[0] == "zip":
        with zipfile.ZipFile(ref[1]) as zf:
            text = zf.read(ref[2]).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    with open(ref, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _ref_name(ref) -> str:
    """给界面看的一行名字（zip 成员只取文件名）。"""
    if isinstance(ref, tuple) and len(ref) == 3 and ref[0] == "zip":
        return Path(ref[2]).name
    return Path(ref).name


def find_sources(argv: list[str]) -> list[tuple[object, object, Path, float]]:
    """找出**所有**数据源，返回 [(amount.csv, cost.csv, 目录, 修改时间)]（旧→新）。

    ⚠️ 用户 2026-10-13："如果添加了多个文件，你也得扫描进入，根据时间轴自己添加。
    比如我第一个文件是 9 月的，第二个文件是 10 月的。" ⇒ **要把所有导出都收进来**，
    同一段日期以**较新的那个文件**为准（重导一份覆盖旧的，不会重复累加）。

    找的地方（相对目录优先）：
        token-chart/ 与 token-chart/data/ 下的 usage_data_* 文件夹、
        这两处直接躺着的 amount-*.csv + cost-*.csv，最后兜底去下载目录。
    """
    args = [a for a in argv if not a.startswith("-")]
    if len(args) >= 2:
        a, c = Path(args[0]), Path(args[1])
        return [(a, c, a.parent, max(a.stat().st_mtime, c.stat().st_mtime))]

    found: list[tuple[Path, Path, Path, float]] = []
    bases = [HERE, HERE / "data"]        # 只用相对本脚本的目录，搬走也能用
    for base in bases:
        if not base.is_dir():
            continue
        for d in sorted(base.glob("usage_data_*")):
            if not d.is_dir():
                continue
            amt = next(iter(sorted(d.glob("amount-*.csv"))), None)
            cost = next(iter(sorted(d.glob("cost-*.csv"))), None)
            if amt and cost:
                found.append((amt, cost, d, max(amt.stat().st_mtime, cost.stat().st_mtime)))
        amt = next(iter(sorted(base.glob("amount-*.csv"))), None)
        cost = next(iter(sorted(base.glob("cost-*.csv"))), None)
        if amt and cost:
            found.append((amt, cost, base, max(amt.stat().st_mtime, cost.stat().st_mtime)))
        # 🆕 直接吃平台下载的压缩包（不用解压）
        for z in sorted(base.glob("*.zip")):
            for amt_ref, cost_ref in _zip_pairs(z):
                found.append((amt_ref, cost_ref, z, z.stat().st_mtime))
    if not found:
        raise SystemExit("没找到 CSV：把平台导出的 usage_data_* 文件夹丢进 "
                         f"{HERE}（或它的 data/ 子目录），或者手动传两个 csv 路径。")
    # 去重（同一份文件可能被两个地方扫到）+ 按时间从旧到新
    uniq: dict[str, tuple[object, object, Path, float]] = {}
    for a, c, d, mt in found:
        key = f"{a[1]}::{a[2]}" if isinstance(a, tuple) else str(a.resolve())
        uniq[key] = (a, c, d, mt)
    return sorted(uniq.values(), key=lambda x: x[3])


DATA_DIR = HERE / "data"
LEDGER_FILE = DATA_DIR / "ledger.txt"


def _load_ledger() -> dict:
    """只读脚本旁边的 data/ledger.txt，全是相对路径。"""
    try:
        obj = json.loads(LEDGER_FILE.read_text(encoding="utf-8-sig"))
        obj.setdefault("topups", [])
        obj.setdefault("grants", [])
        return obj
    except Exception:
        return {"topups": [], "grants": []}


def _grain_from_sources(sources):
    """把导出文件拆成最细粒度的账：{(day, api, model): [tokens, 元]} + 请求 + 当天花费。

    ⚠️ 同一段日期可能被导出过好几遍（文件夹 + zip + 重导），所以**按天整块覆盖**、
    绝不跨文件累加 —— 否则总量会翻几倍。
    """
    tok, req, daycost = {}, {}, {}
    for amt_csv, cost_csv, _d, _mt in sources:          # 旧 → 新
        s_tok, s_req, s_cost = {}, {}, {}
        for r in _rows(amt_csv):
            day = r["start_time_iso"][:10]
            api = (r.get("api_key_name") or "").strip() or "（未命名）"
            if r["type"] in TOKEN_TYPES:
                n = int(r["amount"] or 0)
                try:
                    c = float(r["price"] or 0) * n
                except (TypeError, ValueError):
                    c = 0.0
                cur = s_tok.setdefault((day, api, r["model"]), [0, 0.0])
                cur[0] += n
                cur[1] += c
            elif r["type"] == "request_count":
                k = (day, api)
                s_req[k] = s_req.get(k, 0) + int(r["amount"] or 0)
        for r in _rows(cost_csv):
            day = r["start_time_iso"][:10]
            s_cost[day] = s_cost.get(day, 0.0) + float(r["cost"])
        days = {k[0] for k in s_tok} | {k[0] for k in s_req} | set(s_cost)
        for k in [k for k in tok if k[0] in days]:
            del tok[k]
        for k in [k for k in req if k[0] in days]:
            del req[k]
        for d in days:
            daycost.pop(d, None)
        tok.update(s_tok)
        req.update(s_req)
        daycost.update(s_cost)
    return tok, req, daycost


def _load_grain():
    """读 data/ 下我们自己攒的月度 CSV。"""
    tok, req, daycost = {}, {}, {}
    if not DATA_DIR.is_dir():
        return tok, req, daycost
    for f in sorted(DATA_DIR.glob("*.csv")):
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                d, api, model = row["date"], row["api"], row["model"]
                if model == "__day__":
                    daycost[d] = daycost.get(d, 0.0) + float(row["cost"] or 0)
                elif model == "__req__":
                    req[(d, api)] = req.get((d, api), 0) + int(float(row["requests"] or 0))
                else:
                    cur = tok.setdefault((d, api, model), [0, 0.0])
                    cur[0] += int(float(row["tokens"] or 0))
                    cur[1] += float(row["cost"] or 0)
    return tok, req, daycost


def _save_grain(tok, req, daycost):
    """按月份写到 data/YYYY-MM.csv（覆盖写，内容就是全部已知数据）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    months = {}
    for (d, api, model), (n, c) in tok.items():
        months.setdefault(d[:7], []).append((d, api, model, n, c, 0))
    for (d, api), n in req.items():
        months.setdefault(d[:7], []).append((d, api, "__req__", 0, 0.0, n))
    for d, c in daycost.items():
        months.setdefault(d[:7], []).append((d, "__day__", "__day__", 0, c, 0))
    for m, rows in months.items():
        rows.sort(key=lambda r: (r[0], r[1], r[2]))
        with open(DATA_DIR / (m + ".csv"), "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "api", "model", "tokens", "cost", "requests"])
            for r in rows:
                w.writerow([r[0], r[1], r[2], r[3], "%.6f" % r[4], r[5]])
    for old in DATA_DIR.glob("*.csv"):
        if old.stem not in months:
            old.unlink(missing_ok=True)
    return sorted(months)


def daily(grain=None):
    """把最细粒度的账按天汇总。"""
    tok, req, daycost = grain if grain else _load_grain()
    per, api_day, api_req_day, api_cost_day, api_model_day = {}, {}, {}, {}, {}
    api_sum = {}
    for (day, api, model), (n, c) in tok.items():
        per.setdefault(day, {})[model] = per.setdefault(day, {}).get(model, 0) + n
        api_day.setdefault(day, {})[api] = api_day.setdefault(day, {}).get(api, 0) + n
        api_model_day.setdefault(day, {}).setdefault(api, {})
        api_model_day[day][api][model] = api_model_day[day][api].get(model, 0) + n
        api_cost_day.setdefault(day, {})[api] = api_cost_day.setdefault(day, {}).get(api, 0.0) + c
        api_sum[day] = api_sum.get(day, 0.0) + c
    for (day, api), n in req.items():
        api_req_day.setdefault(day, {})[api] = api_req_day.setdefault(day, {}).get(api, 0) + n
    cost = dict(daycost)
    for day, c in api_sum.items():
        cost.setdefault(day, c)
    req_day = {d: sum(m.values()) for d, m in api_req_day.items()}

    days = sorted(set(per) | set(cost))
    d0 = dt.date.fromisoformat(days[0])
    d1 = dt.date.fromisoformat(days[-1])
    out = []
    tok_cum = 0.0
    cost_cum = 0.0
    cum_prev = None
    for i in range((d1 - d0).days + 1):
        key = (d0 + dt.timedelta(days=i)).isoformat()
        cheap = per.get(key, {}).get(CHEAP, 0)
        pricey = per.get(key, {}).get(PRICEY, 0)
        c = round(cost.get(key, 0.0), 6)
        tok_cum += cheap + pricey
        cost_cum += c
        cum = round(tok_cum / cost_cum) if cost_cum > 0 else None
        delta = None if (cum is None or cum_prev is None) else cum - cum_prev
        cum_prev = cum if cum is not None else cum_prev
        out.append({
            "d": key, "cheap": cheap, "pricey": pricey, "total": cheap + pricey,
            "req": req_day.get(key, 0), "cost": c,
            "perYuan": round((cheap + pricey) / c) if c > 0 and (cheap + pricey) else None,
            "cum": cum, "delta": delta,
            "byApi": dict(api_day.get(key, {})),
            "byApiReq": dict(api_req_day.get(key, {})),
            "byApiCost": dict(api_cost_day.get(key, {})),
            "byApiModel": dict(api_model_day.get(key, {})),
        })
    return out


def kpis(rows: list[dict]) -> dict:
    tok = sum(r["total"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    req = sum(r["req"] for r in rows)
    return {
        "tok": tok,
        "cost": round(cost, 2),
        "req": req,
        "perYuan": round(tok / cost) if cost else 0,
        "days": len(rows),
        "empty": sum(1 for r in rows if not r["total"] and not r["cost"]),
        "from": rows[0]["d"],
        "to": rows[-1]["d"],
        "cheap": sum(r["cheap"] for r in rows),
        "pricey": sum(r["pricey"] for r in rows),
        "active": sum(1 for r in rows if r["total"]),
    }


def _gift_balance() -> float | None:
    """问一次平台的 /user/balance，拿到「赠送余额」。拿不到返回 None（不影响出图）。"""
    import os as _os
    import urllib.request as _u
    key = _os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        for cand in [HERE.parent / "page-bridge" / "key.txt",
                     Path.home() / "go-bot" / "page-bridge" / "key.txt"]:
            if cand.exists():
                key = cand.read_text(encoding="utf-8").strip()
                break
    if not key:
        return None
    try:
        req = _u.Request("https://api.deepseek.com/user/balance",
                         headers={"Authorization": "Bearer " + key})
        with _u.urlopen(req, timeout=10) as r:
            data = __import__("json").loads(r.read().decode("utf-8", "replace"))
        info = (data.get("balance_infos") or [{}])[0]
        return float(info.get("granted_balance") or 0)
    except Exception:
        return None


def _data_srcs() -> list[dict]:
    """网页的数据来源**只认 data/ 目录**（那些月度 CSV）。"""
    out = []
    for f in sorted(DATA_DIR.glob("*.csv")):
        st = f.stat()
        out.append({"where": "data/" + f.name, "name": "data/" + f.name, "key": f.stem,
                    "at": dt.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")})
    return out


def data_source_label() -> str:
    names = [f.stem for f in sorted(DATA_DIR.glob("*.csv"))]
    if not names:
        return ""
    return "data/（" + "、".join(names) + "）"


def _dedup_srcs(sources) -> list[dict]:
    """同一段日期导了多次（文件夹 + zip + 重导）时，显示只留最新的一份。"""
    import re as _re
    best: dict[str, dict] = {}
    for amt, _c, d, mt in sources:
        name = Path(str(d)).name
        m = _re.search(r"(\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2})", name)
        key = m.group(1) if m else name
        cur = {"where": str(d), "name": name, "key": key, "mt": mt,
               "at": dt.datetime.fromtimestamp(mt).strftime("%m-%d %H:%M")}
        if key not in best or mt >= best[key]["mt"]:
            best[key] = cur
    return [{k: v for k, v in x.items() if k != "mt"}
            for x in sorted(best.values(), key=lambda y: y["key"])]


def payload() -> dict:
    """现读**所有** CSV → 页面要用的那份 JSON（服务模式下每次刷新都重新算）。"""
    try:
        sources = find_sources(sys.argv[1:])
    except SystemExit:
        if not any(DATA_DIR.glob("*.csv")):
            raise                    # 既没导出文件、也没攒下来的 data/，那才是真没数据
        sources = []                 # 有 data/ 就够了
    old = _load_grain()
    fresh = _grain_from_sources(sources)
    tok = dict(old[0]); tok.update(fresh[0])
    req = dict(old[1]); req.update(fresh[1])
    dc = dict(old[2]); dc.update(fresh[2])
    _save_grain(tok, req, dc)
    rows = daily((tok, req, dc))
    apis: dict[str, int] = {}
    api_cost: dict[str, float] = {}
    for r in rows:
        for k, v in (r.get("byApi") or {}).items():
            apis[k] = apis.get(k, 0) + v
        for k, v in (r.get("byApiCost") or {}).items():
            api_cost[k] = api_cost.get(k, 0.0) + v
    return {
        "rows": rows,
        "kpis": kpis(rows),
        "gift": _gift_balance(),
        "ledger": _load_ledger(),
        "apis": [{"name": k, "tok": v, "cost": round(api_cost.get(k, 0.0), 2)}
                 for k, v in sorted(apis.items(), key=lambda kv: -kv[1])],
        "srcs": _data_srcs(),
        "source": data_source_label(),
        "where": str(sources[-1][2]) if sources else str(DATA_DIR),
        "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def render(p: dict, embedded: bool) -> str:
    """把数据塞进模板（embedded=True 时数据写死在页里，file:// 打开也能看）。"""
    data_js = json.dumps(p, ensure_ascii=False) if embedded else "null"
    return TEMPLATE.replace("__PAYLOAD__", data_js)


def main() -> int:
    argv = sys.argv[1:]
    if "--serve" in argv:
        port = 8765
        if "--port" in argv:
            port = int(argv[argv.index("--port") + 1])
        page = render(payload(), embedded=False).encode("utf-8")

        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")     # 刷新一定要拿到新的
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:                              # noqa: N802
                path = self.path.split("?")[0]
                try:
                    if path in ("/", "/index.html"):
                        self._send(page, "text/html; charset=utf-8")
                    elif path == "/data.json":
                        self._send(json.dumps(payload(), ensure_ascii=False).encode("utf-8"),
                                   "application/json; charset=utf-8")
                    else:
                        self._send(b"not found", "text/plain; charset=utf-8", 404)
                except Exception as exc:                           # 出错也让人看得见
                    self._send(f"出错：{exc!r}".encode("utf-8"),
                               "text/plain; charset=utf-8", 500)

            def do_POST(self) -> None:                             # noqa: N802
                path = self.path.split("?")[0]
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    raw = self.rfile.read(n) if n else b"{}"
                    if path == "/ledger":
                        obj = json.loads(raw.decode("utf-8", "replace"))
                        DATA_DIR.mkdir(parents=True, exist_ok=True)
                        LEDGER_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                                               encoding="utf-8")
                        self._send(b'{"ok":true}', "application/json; charset=utf-8")
                    else:
                        self._send(b"not found", "text/plain; charset=utf-8", 404)
                except Exception as exc:
                    self._send(f"出错：{exc!r}".encode("utf-8"),
                               "text/plain; charset=utf-8", 500)

            def log_message(self, fmt, *args):                     # 别刷屏
                pass

        url = f"http://127.0.0.1:{port}/"
        print(f"服务起来了：{url}")
        print("  · 页面上点「刷新数据」= 现读 CSV 重画（把新导出的文件夹丢进 "
              f"{HERE} 或它的 data\\ 子目录就行）")
        print("  · 关掉这个窗口 = 服务停掉")
        if "--no-open" not in argv:          # 测试/远程用得上：别抢人家的浏览器
            try:
                webbrowser.open(url)
            except Exception:
                pass
        with socketserver.ThreadingTCPServer(("127.0.0.1", port), H) as httpd:
            httpd.serve_forever()
        return 0

    out = HERE / "token-usage.html"
    if "-o" in argv:
        i = argv.index("-o")
        out = Path(argv[i + 1])
        del argv[i:i + 2]
    p = payload()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(p, embedded=True), encoding="utf-8")
    k = p["kpis"]
    print(f"数据源：{p['source']}")
    print(f"共 {k['days']} 天（{k['from']} → {k['to']}），有用量 {k['active']} 天，空白 {k['empty']} 天")
    print(f"总 token {k['tok']:,}（便宜 {k['cheap']:,} / 贵 {k['pricey']:,}）")
    print(f"总花费 ¥{k['cost']}，请求 {k['req']:,} 次，平均每元 {k['perYuan']:,} token")
    print(f"写出：{out}  ({out.stat().st_size / 1024:.0f} KB)")
    print("（想看「点一下刷新就是最新」的那种：python build.py --serve）")
    return 0


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>大肥鱼（DeepSeek）用量与花费</title>
<style>
  :root {
    --page:#f9f9f7; --surface-1:#fcfcfb; --text-primary:#0b0b0b; --text-secondary:#52514e;
    --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
    --cheap:#6250d6; --pricey:#eb6834; --money:#eda100; --daily:#e87ba4;
    --cum:#008300; --delta:#1baf7a; --hover:rgba(11,11,11,.05);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --page:#0d0d0d; --surface-1:#1a1a19; --text-primary:#f0efec; --text-secondary:#c3c2b7;
      --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
      --cheap:#9085e9; --pricey:#d95926; --money:#c98500; --daily:#d55181;
      --cum:#008300; --delta:#199e70; --hover:rgba(255,255,255,.06);
    }
  }
  :root[data-theme="dark"] {
    --page:#0d0d0d; --surface-1:#1a1a19; --text-primary:#f0efec; --text-secondary:#c3c2b7;
    --muted:#898781; --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
    --cheap:#9085e9; --pricey:#d95926; --money:#c98500; --daily:#d55181;
    --cum:#008300; --delta:#199e70; --hover:rgba(255,255,255,.06);
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:28px 20px 48px; background:var(--page); color:var(--text-primary);
         font-family: system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
  .wrap { max-width:1460px; margin:0 auto; }
  header { display:flex; align-items:flex-start; gap:16px; margin-bottom:18px; }
  h1 { font-size:20px; font-weight:650; margin:0 0 4px; }
  .sub { font-size:13px; color:var(--text-secondary); }
  .grow { flex:1; }
  button { font:inherit; font-size:12px; color:var(--text-secondary); background:var(--surface-1);
           border:1px solid var(--border); border-radius:8px; padding:5px 10px; cursor:pointer; }
  button:hover { background:var(--hover); }
  button.primary { color:var(--text-primary); font-weight:600; }
  .kpis { display:flex; flex-wrap:wrap; gap:26px 40px; align-items:flex-end;
          background:var(--surface-1); border:1px solid var(--border); border-radius:14px;
          padding:16px 20px; margin-bottom:14px; }
  .hero .label, .tile .label { font-size:12px; color:var(--text-secondary); }
  .hero .value { font-size:48px; font-weight:650; line-height:1.05; letter-spacing:-1px; }
  .tile .value { font-size:22px; font-weight:600; }
  .tile .value small { font-size:13px; font-weight:500; color:var(--text-secondary); }
  .card { background:var(--surface-1); border:1px solid var(--border); border-radius:14px;
          padding:14px 16px 10px; }
  /* 按 API 看：表格 + 占比条 */
  table.mini { width:100%; border-collapse:collapse; font-size:12px; margin-top:2px; }
  table.mini th, table.mini td { text-align:right; padding:5px 8px;
        border-bottom:1px solid var(--border); font-variant-numeric:tabular-nums; }
  table.mini th:first-child, table.mini td:first-child { text-align:left; font-weight:600; }
  table.mini tbody tr:hover { background:var(--hover); }
  table.mini tr.sel td { background:color-mix(in srgb, var(--accent) 12%, transparent); }
  .apibar { display:flex; height:10px; border-radius:5px; overflow:hidden; margin:10px 0 2px;
            background:var(--border); }
  .apibar i { display:block; height:100%; }
  .giftkpi { display:flex; flex-wrap:wrap; gap:18px 34px; margin-top:10px; }
  .giftkpi .cell .l { font-size:11.5px; color:var(--text-secondary); }
  .giftkpi .cell .v { font-size:20px; font-weight:600; }
  .giftkpi .cell .v small { font-size:12px; font-weight:500; color:var(--text-secondary); }
  .card h2 { font-size:13.5px; font-weight:600; margin:0 0 2px; }
  .card .note { font-size:11.5px; color:var(--text-secondary); margin-bottom:8px; }
  /* 开关那一行同时也是图例：每条一个勾选框 */
  .toggles { display:flex; flex-wrap:wrap; gap:6px 18px; align-items:center;
             font-size:12px; color:var(--text-secondary); margin:0 0 10px; }
  .toggles label { display:flex; align-items:center; gap:6px; cursor:pointer; user-select:none; }
  .toggles i { display:inline-block; width:10px; height:10px; border-radius:3px; }
  .toggles i.line { width:16px; height:3px; border-radius:2px; }
  svg { display:block; width:100%; height:auto; overflow:visible; font-family:inherit; }
  svg text { fill:var(--muted); font-size:11px; }
  svg text.ink { fill:var(--text-secondary); }
  svg text.strong { fill:var(--text-primary); font-weight:600; }
  svg text.small { font-size:9.5px; }
  svg text.halo { paint-order:stroke; stroke:var(--surface-1); stroke-width:3px;
                  stroke-linejoin:round; }
  svg .gridline { stroke:var(--grid); stroke-width:1; }
  svg .axis { stroke:var(--axis); stroke-width:1; }
  svg .zeroline { stroke:var(--text-secondary); stroke-width:1; stroke-opacity:.45; }
  svg .crosshair { stroke:var(--text-secondary); stroke-width:1; opacity:0; }
  svg .hit { fill:transparent; cursor:crosshair; }
  #tip { position:fixed; z-index:9; pointer-events:none; opacity:0; background:var(--surface-1);
         color:var(--text-primary); border:1px solid var(--border); border-radius:10px;
         box-shadow:0 6px 22px rgba(0,0,0,.16); padding:9px 11px; font-size:12px;
         min-width:200px; transition:opacity .09s linear; }
  #tip .day { font-size:12px; font-weight:650; margin-bottom:6px; }
  #tip .row { display:flex; align-items:center; gap:8px; margin-top:3px; }
  #tip .row .k { flex:1; color:var(--text-secondary); }
  #tip .row .v { font-weight:600; font-variant-numeric:tabular-nums; }
  #tip .row .sw { width:14px; height:3px; border-radius:2px; flex:none; }
  #tip hr { border:0; border-top:1px solid var(--border); margin:7px 0 6px; }
  .filters { display:flex; flex-wrap:wrap; gap:6px 10px; align-items:center;
             margin:0 0 10px; font-size:12px; color:var(--text-secondary); }
  .filters input[type=date] { font:inherit; font-size:12px; color:var(--text-primary);
             background:var(--surface-1); border:1px solid var(--border);
             border-radius:8px; padding:3px 6px; }
  .filters .chip { border:1px solid var(--border); background:var(--surface-1);
             border-radius:999px; padding:3px 10px; cursor:pointer; font-size:12px;
             color:var(--text-secondary); }
  .filters .chip:hover { background:var(--hover); }
  .filters .chip.on { color:var(--text-primary); font-weight:600;
             border-color:var(--text-secondary); }
  #apiRow { margin-top:6px; }
  #apiRow .api-chip { border:1px solid var(--border); background:var(--surface-1);
             border-radius:999px; padding:3px 10px; cursor:pointer; font-size:12px;
             color:var(--text-primary); user-select:none; }
  #apiRow .api-chip:hover { background:var(--hover); }
  #apiRow .api-chip.off { opacity:.45; text-decoration:line-through; }
  .plotwrap { overflow-x:auto; overflow-y:hidden; }
  details { background:var(--surface-1); border:1px solid var(--border); border-radius:14px;
            padding:12px 16px; margin-top:14px; }
  summary { cursor:pointer; font-size:13px; font-weight:600; }
  table { border-collapse:collapse; width:100%; margin-top:12px; font-size:12.5px;
          font-variant-numeric:tabular-nums; }
  th, td { text-align:right; padding:5px 8px; border-bottom:1px solid var(--grid); }
  th:first-child, td:first-child { text-align:left; }
  th { color:var(--text-secondary); font-weight:600; }
  .foot { margin-top:16px; font-size:12px; color:var(--text-secondary); line-height:1.75; }
  .foot b { color:var(--text-primary); font-weight:600; }
  .kpis.sel { opacity:.92; margin-top:-4px; }
  .kpis.sel .hero .value { font-size:26px; }
  .kpis .sub { font-size:11px; color:var(--text-secondary); margin-top:2px; }
  .kpis .gift { font-size:11px; color:var(--text-secondary); margin-left:4px; }
  .modal { position:fixed; inset:0; background:rgba(0,0,0,.35); display:flex;
           align-items:center; justify-content:center; z-index:99; }
  .modal-box { background:var(--surface-1); border:1px solid var(--border); border-radius:14px;
               padding:16px 18px; width:420px; max-height:80vh; overflow:auto;
               box-shadow:0 12px 40px rgba(0,0,0,.18); }
  .ledger-sec { margin-top:10px; }
  .ledger-head { font-size:13px; font-weight:600; margin-bottom:4px; }
  .ledger-list { display:flex; flex-direction:column; gap:4px; margin-bottom:6px; }
  .ledger-row { display:flex; gap:6px; align-items:center; }
  .ledger-row input { width:120px; font-size:12px; padding:3px 6px; border-radius:6px;
                      border:1px solid var(--border); background:var(--surface-1); }
  .ledger-row button { border:0; border-radius:6px; padding:3px 8px; font-size:12px;
                       background:#f0e9e9; color:#a33; cursor:pointer; }
  .modal-foot { display:flex; gap:8px; align-items:center; margin-top:14px; }
  .modal-foot button { border:0; border-radius:8px; padding:5px 10px; font-size:12px;
                       background:#4a3fd6; color:#fff; cursor:pointer; }
  .modal-foot button.ghost { background:#e8e8f5; color:#333; }
  .banner { margin:0 0 12px; font-size:12px; color:var(--text-secondary); }
  .banner.warn { color:#b00; }
</style></head><body>
<div class="wrap">
  <header>
    <div>
      <h1>大肥鱼（DeepSeek）用量与花费</h1>
      <div class="sub" id="range"></div>
    </div>
    <div class="grow"></div>
    <button id="settingsBtn">设置</button>
    <button id="theme">切换深色</button>
    <button id="fileBtn">载入文件</button>
    <input type="file" id="fileIn" multiple accept=".csv,.zip" style="display:none">
    <button id="reload" class="primary" style="display:none">刷新数据</button>
  </header>
  <div class="banner" id="banner"></div>

  <div class="kpis">
    <div class="hero"><div class="label">总 token（全部历史）</div>
      <div class="value" id="k-tok"></div>
      <div class="sub" id="k-tok-note"></div></div>
    <div class="tile"><div class="label">账单（充值）<span id="k-bill-gift" class="gift"></span></div>
      <div class="value" id="k-bill"></div></div>
    <div class="tile"><div class="label">余额<span id="k-left-gift" class="gift"></span></div>
      <div class="value" id="k-left"></div></div>
    <div class="tile"><div class="label">已花费<span id="k-gift" class="gift"></span></div>
      <div class="value" id="k-cost"></div></div>
    <div class="tile"><div class="label">请求数</div><div class="value" id="k-req"></div></div>
    <div class="tile"><div class="label">平均每 1 元</div><div class="value" id="k-py"></div></div>
  </div>
  <div class="kpis sel">
    <div class="hero"><div class="label">当前所选（时间范围 + 勾选）</div><div class="value" id="s-tok"></div></div>
    <div class="tile"><div class="label">花费</div><div class="value" id="s-cost"></div></div>
    <div class="tile"><div class="label">请求数</div><div class="value" id="s-req"></div></div>
    <div class="tile"><div class="label">平均每 1 元</div><div class="value" id="s-py"></div></div>
  </div>

  <div class="card" id="giftCard">
    <h2>排除赠送余额（全部历史，仅供参考）</h2>
    <div class="note">
      平台的「总花费」里可能有一部分是**赠送额度**抵扣的。填上赠送了多少，
      就能看出**你自己掏的那部分钱**买到多少 token —— 纯参考，不影响上面任何曲线。
    </div>
    <div class="filters">
      <label class="chip" style="cursor:pointer">
        <input type="checkbox" id="giftOn" style="margin-right:6px">启用
      </label>
      <span>赠送余额 ¥</span>
      <input type="number" id="giftVal" min="0" step="0.01" value="0"
             style="width:110px" placeholder="0.00">
      <span style="opacity:.75">（默认 0 = 和上面完全一样）</span>
    </div>
    <div class="giftkpi" id="giftKpi"></div>
  </div>

  <div class="card">
    <h2>用量全景 · 柱 + 五条曲线（每条可单独开关）</h2>
    <div class="note">
      柱 = 当天 token（下紫 = flash 便宜、上橙 = v4-pro 贵）；曲线 = 花费 / 当天每元 token /
      累计每元 token / 增长（累计的日增量，零线上方为正）。
      <b>纵轴 0~125%</b>：每条按自己的满格缩放到 100%，上面留 25% 给标注；真实数字都写在点上。
      增长那条：最高那天 100%、最低那天 0%，**零线自动落在 0 实际的位置上**（上方为正）。
    </div>
    <div class="filters" id="filters">
      <span>时间轴</span>
      <input type="date" id="from"><span>→</span><input type="date" id="to">
      <span class="chip" data-p="m30">最近 30 天</span>
      <span class="chip" data-p="thisMonth">本月</span>
      <span class="chip" data-p="lastMonth">上月</span>
      <span class="chip" data-p="m3">最近 3 个月</span>
      <span class="chip" data-p="m6">最近半年</span>
      <span class="chip" data-p="m12">最近一年</span>
      <span class="chip" data-p="thisYear">今年</span>
      <span style="opacity:.8">（只改**显示**哪一段；累计每元和增长一直是按全部历史算的）</span>
    </div>
    <div class="toggles" id="toggles"></div>
    <div class="toggles" id="apiRow"></div>
    <div class="plotwrap" id="plotwrap">
      <svg id="c" viewBox="0 0 1400 470" role="img"
           aria-label="每天 token 堆叠柱，以及花费、当天每元、累计每元、增长四条曲线"></svg>
    </div>
  </div>

  <details>
    <summary>数据表（和图上一样）</summary>
    <table id="tbl"><thead><tr>
      <th>日期</th><th>flash token</th><th>v4-pro token</th><th>合计 token</th>
      <th>请求数</th><th>花费 (元)</th><th>当天每元 token</th><th>累计每元 token</th>
      <th>增长（对昨天）</th><th>元 / 百万 token</th>
    </tr></thead><tbody></tbody></table>
  </details>

  <div class="foot">
    <b>数据来源</b>：DeepSeek 平台导出的两份 CSV（amount + cost）逐日汇总，现读现算。<br>
    <b>为什么纵轴是百分比</b>：token、元、token/元 量级差上百倍，硬画一根真实轴等于凭空造"相关性"；
    每条按自己的最大值归一化，量级看数字标注和表格。<br>
    <b>增长</b>＝今天"累计每元 token"减昨天那个数：<b>正数</b>说明这天比之前更划算，负数反之。<br>
    <b>默认显示最近 30 天</b>（时间轴那一行可以改：本月 / 上月 / 3 个月 / 半年 / 一年 / 今年，
    或者直接填两个日期）—— 数据再涨也不至于一屏几千天。<br>
    <b>用法</b>：把平台导出的 <code>usage_data_*</code> 文件夹丢进本目录（或它的 <code>data\</code>
    子目录），点「刷新数据」就是最新的；静态版（发群里那个）跑 <code>python build.py</code>。
  </div>
</div>
<div id="tip" role="status" aria-live="polite"></div>

<script>
// 服务模式：数据从 /data.json 现取（点刷新就是最新）；file:// 打开：用页里写死的那份
const EMBEDDED = __PAYLOAD__;
let DATA = [], K = {}, SOURCE = "", BUILT = "";

const fmtInt = n => Number(n || 0).toLocaleString("zh-CN");
function cn(n) {                       // 28.89 亿 / 1,000 万 / 26.4 万
  if (n >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (n >= 1e6) return Math.round(n / 1e4).toLocaleString("zh-CN") + " 万";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + " 万";
  return fmtInt(n);
}
function cnc(n) {                      // 稠密标注：不留空格、最多 5 字
  if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
  if (n >= 1e6) return Math.round(n / 1e4) + "万";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(Math.round(n));
}
function signed(n) { return (n > 0 ? "+" : n < 0 ? "−" : "") + cnc(Math.abs(n)); }
const money = n => "¥" + Number(n).toFixed(2);
const perMillion = py => py ? 1e6 / py : null;

// ── 六条序列（开关那行同时也是图例）────────────────────────────
const SERIES = [
  { key:"cheap",  kind:"bar",  color:"var(--cheap)",  label:"flash token（便宜）" },
  { key:"pricey", kind:"bar",  color:"var(--pricey)", label:"v4-pro token（贵）" },
  { key:"cost",   kind:"line", color:"var(--money)",  label:"花费（元）",
    get:d => d.cost > 0 ? d.cost : null, max:() => Math.max(...DATA.map(d => d.cost)),
    labelFmt:v => "¥" + v.toFixed(2), all:true, prefer:"above" },
  { key:"daily",  kind:"line", color:"var(--daily)",  label:"当天每元 token",
    get:d => d.perYuan, max:() => Math.max(...DATA.map(d => d.perYuan || 0)),
    labelFmt:cnc, all:true, prefer:"below" },
  { key:"cum",    kind:"line", color:"var(--cum)",    label:"累计每元 token",
    get:d => d.cum, max:() => Math.max(...DATA.map(d => d.cum || 0)),
    labelFmt:cnc, all:true, prefer:"above" },
  { key:"delta",  kind:"line", color:"var(--delta)",  label:"增长（累计的日增量）",
    get:d => d.delta, labelFmt:signed, all:true, zeroLine:true, prefer:"above" },
];
const ON = {};
// 默认开「花费」和「当天每元」；累计每元、增长默认关着（用户 2026-10-20）
SERIES.forEach(s => ON[s.key] = (s.key === "cost" || s.key === "daily"));
SERIES[5].max = () => Math.max(...DATA.map(d => Math.abs(d.delta || 0)));

// ── 布局 ────────────────────────────────────────────────────
const W = 1400, ML = 70, MR = 24, TOP = 30, PLOT_H = 380, AXIS_PCT = 125;
const BOTTOM = TOP + PLOT_H, VB_H = 470;
let PLOT_W = W - ML - MR, N = 0, BAND = 0, X0 = ML;   // X0 = 绘图区左边界
const CX = i => X0 + BAND * (i + 0.5);
const NS = "http://www.w3.org/2000/svg";
const SVG = document.getElementById("c");
const yPct = pct => BOTTOM - (pct / AXIS_PCT) * PLOT_H;     // AXIS_PCT = 顶

function el(tag, attrs, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  (parent || SVG).appendChild(e);
  return e;
}
function barPath(x, y, w, h, r) {      // 顶端圆角、基线方角
  r = Math.max(0, Math.min(r, h, w / 2));
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y}`
       + ` Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}
// delta 有正有负 ⇒ 用它自己的极值铺满，再画一条零线；其它序列按各自最大值
function deltaMap(D) {
  // 增长这条：**最高那天 = 100%、最低那天 = 0%**（和别的曲线一个规矩），
  // 零线就画在 0 实际落到的位置上 —— 正负在视觉上分得开，比率也跟别的条一致。
  const vs = (D || VIEW).map(d => d.delta).filter(v => v !== null && v !== undefined);
  const lo = Math.min(0, ...vs), hi = Math.max(0, ...vs);
  return v => (v - lo) / ((hi - lo) || 1) * 100;
}
function seriesPct(s, v) {
  if (s.key === "delta") return deltaMap()(v);
  const m = s.max() || 1;
  return (v / m) * 100;
}

let VIEW = [], FROM = "", TO = "", PRESET = "all", SVG_W = 1400, GIFT = null, BAL = null;
let DEFAULT_LEDGER = null;

function view() {
  return DATA.filter(d => (!FROM || d.d >= FROM) && (!TO || d.d <= TO));
}
const inView = s => Math.max(...VIEW.map(d => {
  const v = s.get ? s.get(d) : 0;
  return v === null || v === undefined ? 0 : Math.abs(v);
}), 0);

function render() {
  VIEW = view();
  N = VIEW.length;
  SVG.textContent = "";
  document.querySelectorAll("#c .hl").forEach(e => e.remove());
  sel = -1;
  const wrap = document.getElementById("plotwrap");
  const avail = Math.max(320, (wrap.clientWidth || 1400) - 4);
  // 点少 → 宽松点（但有上限）；点多 → 保持这个间距，超了就横向滚
  const MIN_BAND = 38, MAX_BAND = 96;
  BAND = Math.max(MIN_BAND, Math.min(MAX_BAND, avail / Math.max(1, N)));
  PLOT_W = BAND * Math.max(1, N);
  SVG_W = ML + MR + PLOT_W;
  SVG.setAttribute("viewBox", `0 0 ${SVG_W} ${VB_H}`);
  SVG.style.width = SVG_W + "px";
  SVG.style.height = VB_H + "px";
  const D = curRows();
  const W = SVG_W;
  // 只有一两天时，绘图区比容器窄 —— **居中**，别让右边空一大块（用户 2026-10-13 提的观感）
  X0 = ML + Math.max(0, (avail - PLOT_W) / 2);

  // 纵轴 0~125%
  for (let pct = 0; pct <= AXIS_PCT + 1; pct += 25) {   // 0/25/…/125（跟着 AXIS_PCT 走）
    const y = yPct(pct);
    el("line", { class: "gridline", x1: X0, x2: X0 + PLOT_W, y1: y, y2: y });
    const t2 = el("text", { x: X0 - 10, y: y + 4, "text-anchor": "end" });
    t2.textContent = pct + "%";
  }
  el("line", { class: "axis", x1: X0, x2: X0 + PLOT_W, y1: BOTTOM, y2: BOTTOM });

  if (!N) {
    const t3 = el("text", { class: "ink", x: W / 2, y: TOP + 60, "text-anchor": "middle" });
    t3.textContent = "这一段没有数据";
    return;
  }

  // ── 柱 ─────────────────────────────────────────────────
  if (true) {
    const barMax = Math.max(...D.map(d => d.total)) || 1;
    const bw = Math.min(24, BAND * 0.62);
    const yOf = v => yPct((v / barMax) * 100);
    D.forEach((d, i) => {
      if (!d.total) return;
      const x = CX(i) - bw / 2;
      const y0 = yOf(d.total);
      if (d.cheap > 0) {
        const y1 = yOf(d.cheap);
        const h1 = (d.pricey > 0) ? Math.max(0, BOTTOM - y1 - 2) : BOTTOM - y1;
        if (h1 > 0.5) {
          el("path", { d: barPath(x, y1, bw, h1, (d.pricey > 0) ? 0 : 4),
                       fill: "var(--cheap)" });
        }
      }
      if (d.pricey > 0) {
        el("path", { d: barPath(x, y0, bw, BOTTOM - y0, 4), fill: "var(--pricey)" });
      }
    });
    const peak = D.reduce((a, b, i) => b.total > D[a].total ? i : a, 0);
    const t4 = el("text", { class: "strong small halo", x: CX(peak), y: yOf(D[peak].total) - 7,
                            "text-anchor": "middle" });
    t4.textContent = cn(D[peak].total);
  }

  // ── 曲线（四条，每个点都写数值；挡着了就用上面的开关关掉） ──
  SERIES.filter(s => s.kind === "line" && ON[s.key]).forEach(s => {
    const m = Math.max(...D.map(d => Math.abs(s.get(d) || 0)), 1);
    const pctOf = v => s.key === "delta" ? deltaMap(D)(v) : (v / m) * 100;
    let seg = [], segs = [];
    D.forEach((d, i) => {
      const v = s.get(d);
      if (v === null || v === undefined) { if (seg.length) segs.push(seg); seg = []; return; }
      seg.push([CX(i), yPct(pctOf(v))]);
    });
    if (seg.length) segs.push(seg);
    segs.forEach(seg2 => {
      if (seg2.length < 2) return;
      el("path", { d: "M" + seg2.map(q => q.join(",")).join(" L"), fill: "none", stroke: s.color,
                   "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" });
    });
    D.forEach((d, i) => {
      const v = s.get(d);
      if (v === null || v === undefined) return;
      el("circle", { cx: CX(i), cy: yPct(pctOf(v)), r: 3.5, fill: s.color,
                     stroke: "var(--surface-1)", "stroke-width": 2 });
    });
    if (s.zeroLine) {
      const y = yPct(deltaMap(D)(0));
      el("line", { class: "zeroline", x1: X0, x2: X0 + PLOT_W, y1: y, y2: y });
      const z = el("text", { class: "ink small", x: X0 - 10, y: y + 3, "text-anchor": "end" });
      z.textContent = "0";
    }
    D.forEach((d, i) => {
      const v = s.get(d);
      if (v === null || v === undefined) return;
      const y = yPct(pctOf(v));
      let above = (s.prefer || "above") === "above";
      if (above && y - TOP < 20) above = false;
      if (!above && BOTTOM - y < 22) above = true;
      const t5 = el("text", { class: "ink small halo", x: CX(i), y: y + (above ? -8 : 15),
                              "text-anchor": "middle" });
      t5.textContent = (s.labelFmt || cnc)(v);
    });
  });

  // x 轴（点多就每 5 个标一个；点少就每个都标）
  const step = BAND < 48 ? 5 : 1;
  D.forEach((d, i) => {
    if (i % step !== 0 && i !== N - 1) return;
    const t6 = el("text", { class: "ink", x: CX(i), y: BOTTOM + 18, "text-anchor": "middle" });
    t6.textContent = d.d.slice(5).replace("-", "/");
  });

  // ⚠️⚠️ 命中层和十字线**必须每次重绘都重建**（原来是模块级建一次，
  //    第一次 render() 把 SVG 清空时就一起清掉了 ⇒ 鼠标划上去毫无反应，
  //    用户 2026-10-13 报"点击节点显示的那个功能被删了"）。
  cross = el("line", { class: "crosshair", y1: TOP, y2: BOTTOM, x1: 0, x2: 0 });
  hit = el("rect", { class: "hit", x: X0, y: TOP, width: PLOT_W, height: PLOT_H });
  hit.addEventListener("pointermove", ev => {
    const box = SVG.getBoundingClientRect();
    const x = (ev.clientX - box.left) / box.width * SVG_W;
    const i = Math.max(0, Math.min(N - 1, Math.floor((x - X0) / BAND)));
    show(i, ev.clientX, ev.clientY);
  });
  hit.addEventListener("pointerdown", ev => {       // 点一下也显示（触屏/不划着看）
    const box = SVG.getBoundingClientRect();
    const x = (ev.clientX - box.left) / box.width * SVG_W;
    const i = Math.max(0, Math.min(N - 1, Math.floor((x - X0) / BAND)));
    show(i, ev.clientX, ev.clientY);
  });
  hit.addEventListener("pointerleave", hide);
}

// ── 开关那行（= 图例）──────────────────────────────────────
function buildLegend() {
  const box = document.getElementById("toggles");
  if (!box) return;
  box.querySelectorAll("label.series").forEach(e => e.remove());
  const rows = curRows();
  SERIES.filter(s => s.kind === "line").forEach(s => {
    const lb = document.createElement("label");
    lb.className = "series";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.checked = !!ON[s.key];
    cb.addEventListener("change", () => { ON[s.key] = cb.checked; render(); });
    const i = document.createElement("i");
    i.className = s.kind === "line" ? "line" : "";
    i.style.background = s.color;
    const sp = document.createElement("span");
    sp.textContent = s.label;
    lb.appendChild(cb); lb.appendChild(i); lb.appendChild(sp);
    box.appendChild(lb);
  });
  if (!box.querySelector("span.hint")) {
    const hint = document.createElement("span");
    hint.className = "hint";
    hint.textContent = "（默认开「花费」「当天每元」；想看得更细就把其他几条勾上）";
    hint.style.opacity = ".75";
    box.appendChild(hint);
  }
}

// ── 悬停：一条十字线 + 明细浮层 ─────────────────────────────
const tip = document.getElementById("tip");
let cross = null, hit = null;      // ⚠️ 每次 render 重建（见 render 末尾的注释）
SVG.setAttribute("tabindex", "0");
SVG.addEventListener("focus", () => show(sel < 0 ? N - 1 : sel, null, null));
SVG.addEventListener("blur", hide);
SVG.addEventListener("keydown", ev => {
  const s = sel < 0 ? N - 1 : sel;
  if (ev.key === "ArrowLeft") sel = Math.max(0, s - 1);
  else if (ev.key === "ArrowRight") sel = Math.min(N - 1, s + 1);
  else if (ev.key === "Home") sel = 0;
  else if (ev.key === "End") sel = N - 1;
  else return;
  ev.preventDefault();
  show(sel, null, null);
});
let sel = -1;

function row(parent, k, v, color) {
  const r = document.createElement("div"); r.className = "row";
  if (color) { const sw = document.createElement("span"); sw.className = "sw";
               sw.style.background = color; r.appendChild(sw); }
  const kk = document.createElement("span"); kk.className = "k"; kk.textContent = k;
  const vv = document.createElement("span"); vv.className = "v"; vv.textContent = v;
  r.appendChild(kk); r.appendChild(vv); parent.appendChild(r);
}
function show(i, cx, cy) {
  if (!VIEW.length) return;
  sel = i;
  const d = VIEW[i];
  cross.setAttribute("x1", CX(i)); cross.setAttribute("x2", CX(i));
  cross.style.opacity = 1;
  document.querySelectorAll("#c .hl").forEach(e => e.remove());
  if (d.total) {
    const bw = Math.min(24, BAND * 0.62);
    const barMax = Math.max(...VIEW.map(x => x.total)) || 1;
    const y0 = yPct((d.total / barMax) * 100);
    el("rect", { class: "hl", x: CX(i) - bw / 2 - 1, y: y0 - 2, width: bw + 2,
                 height: BOTTOM - y0 + 2, fill: "none", stroke: "var(--text-primary)",
                 "stroke-width": 1, "stroke-opacity": .45, rx: 3 });
  }
  tip.textContent = "";
  const head = document.createElement("div"); head.className = "day"; head.textContent = d.d;
  tip.appendChild(head);
  row(tip, "flash", fmtInt(d.cheap), "var(--cheap)");
  row(tip, "v4-pro", fmtInt(d.pricey), "var(--pricey)");
  row(tip, "合计 token", fmtInt(d.total), null);
  row(tip, "请求数", fmtInt(d.req), null);
  row(tip, "花费", d.cost > 0 ? money(d.cost) : "—", "var(--money)");
  const hr = document.createElement("hr"); tip.appendChild(hr);
  row(tip, "当天每元 token", d.perYuan ? fmtInt(d.perYuan) : "—", "var(--daily)");
  row(tip, "累计每元 token", d.cum ? fmtInt(d.cum) : "—", "var(--cum)");
  row(tip, "增长（对昨天）", d.delta === null || d.delta === undefined ? "—"
      : signed(d.delta) + "  token/元", "var(--delta)");
  row(tip, "合每百万 token", d.perYuan ? "¥" + perMillion(d.perYuan).toFixed(3) : "—", null);
  tip.style.opacity = 1;
  const bw2 = tip.offsetWidth, bh2 = tip.offsetHeight;
  let tx, ty;
  if (cx == null) {
    const box = SVG.getBoundingClientRect();
    tx = box.left + (CX(i) / SVG_W) * box.width + 14; ty = box.top + box.height + 8;
  } else { tx = cx + 16; ty = cy + 16; }
  tx = Math.min(tx, innerWidth - bw2 - 10); ty = Math.min(ty, innerHeight - bh2 - 10);
  tip.style.left = Math.max(8, tx) + "px";
  tip.style.top = Math.max(8, ty) + "px";
}
function hide() {
  tip.style.opacity = 0; cross.style.opacity = 0;
  document.querySelectorAll("#c .hl").forEach(e => e.remove());
  sel = -1;
}

// ── 数据表 / KPI / 头部 ─────────────────────────────────────
function table() {
  const tb = document.querySelector("#tbl tbody");
  tb.textContent = "";
  curRows().forEach(d => {
    const cells = [d.d, fmtInt(d.cheap), fmtInt(d.pricey), fmtInt(d.total), fmtInt(d.req),
                   d.cost > 0 ? d.cost.toFixed(3) : "—",
                   d.perYuan ? fmtInt(d.perYuan) : "—",
                   d.cum ? fmtInt(d.cum) : "—",
                   (d.delta === null || d.delta === undefined) ? "—"
                        : (d.delta > 0 ? "+" : d.delta < 0 ? "−" : "") + fmtInt(Math.abs(d.delta)),
                   d.perYuan ? perMillion(d.perYuan).toFixed(3) : "—"];
    const tr = document.createElement("tr");
    cells.forEach((c, i) => {
      const td = document.createElement(i === 0 ? "th" : "td");
      if (i === 0) td.setAttribute("scope", "row");
      td.textContent = c;
      tr.appendChild(td);
    });
    tb.appendChild(tr);
  });
}
// ── 按 API 看（可多选）──────────────────────────────────────
const API_OFF = new Set();   // 被点掉的 API
const MODEL_OFF = new Set(); // 被点掉的模型
const API_COLORS = ["#6250d6", "#eb6834", "#eda100", "#e87ba4", "#1b8989", "#7a7a7a"];
let APIS = [];                       // [{name, tok}] 来自后端
function selectionRows() {            // 全部数据，按"排除了哪些 API / 模型"过滤
  if (!API_OFF.size && !MODEL_OFF.size) return DATA;
  return DATA.map(r => {
    const keep = {}, byApiCost = {}, byApiReq = {};
    let c = 0, q = 0;
    for (const [api, perModel] of Object.entries(r.byApiModel || {})) {
      if (API_OFF.has(api)) continue;
      const part = {};
      for (const [m, v] of Object.entries(perModel)) {
        if (MODEL_OFF.has(m)) continue;
        part[m] = v; keep[m] = (keep[m] || 0) + v;
      }
      if (!Object.keys(part).length) continue;
      c += (r.byApiCost || {})[api] || 0;
      q += (r.byApiReq || {})[api] || 0;
      byApiCost[api] = (r.byApiCost || {})[api] || 0;
      byApiReq[api] = (r.byApiReq || {})[api] || 0;
    }
    const cheap = keep["deepseek-flash"] || 0;
    const pricey = keep["deepseek-v4-pro"] || 0;
    const total = cheap + pricey;
    return Object.assign({}, r, { byModel: keep, total, cheap, pricey,
                                 cost: total ? c : 0, req: total ? q : 0,
                                 byApiCost, byApiReq });
  });
}
function curRows() {
  // 累计每元 / 增长 一直按**全部历史**算（2026-10-20 用户：还是沿用历史口径）
  return selectionRows().filter(r => (!FROM || r.d >= FROM) && (!TO || r.d <= TO));
}
// 2026-10-20 用户：把"按 API 看"并进图下的图例里 —— 默认全选 = 总量，
// 点掉一个就把这个 API 的用量从所有数字里减掉；当前这段没调用过的 API 不显示。
function apiNamesInRange() {
  const seen = {};
  (VIEW.length ? VIEW : DATA).forEach(r => {
    for (const [k, v] of Object.entries(r.byApi || {})) if (v > 0) seen[k] = (seen[k] || 0) + v;
  });
  return Object.keys(seen).sort((a, b) => seen[b] - seen[a]);
}
function buildModelChips() {
  const box = document.getElementById("apiRow");
  if (!box) return;
  box.querySelectorAll(".model-chip").forEach(e => e.remove());
  const inRange = (DATA || []).filter(r => (!FROM || r.d >= FROM) && (!TO || r.d <= TO));
  const models = [
    { id: "deepseek-flash", name: "flash", color: "var(--cheap)" },
    { id: "deepseek-v4-pro", name: "v4-pro", color: "var(--pricey)" },
  ].filter(m => inRange.some(r => ((r.byModel || {})[m.id] || 0) > 0 ||
                                  (r.byApiModel && Object.values(r.byApiModel)
                                     .some(pm => (pm[m.id] || 0) > 0))));
  const label = document.createElement("span");
  label.className = "model-chip";
  label.style.cssText = "opacity:.6;font-size:12px;margin-right:2px";
  label.textContent = "模型（默认全选，点一下排除）";
  box.appendChild(label);
  models.forEach(m => {
    const off = MODEL_OFF.has(m.id);
    const chip = document.createElement("span");
    chip.className = "api-chip model-chip" + (off ? " off" : "");
    chip.style.borderColor = off ? "" : m.color;
    chip.textContent = m.name;
    chip.onclick = () => {
      if (MODEL_OFF.has(m.id)) MODEL_OFF.delete(m.id);
      else if (MODEL_OFF.size + 1 >= models.length) MODEL_OFF.clear();
      else MODEL_OFF.add(m.id);
      head(); render(); table();
    };
    box.appendChild(chip);
  });
}
function buildApiChips() {
  const box = document.getElementById("apiRow");
  if (!box) return;
  box.querySelectorAll(".api-chip").forEach(e => e.remove());
  const oldSep = document.getElementById("apiSep"); if (oldSep) oldSep.remove();
  buildModelChips();
  const names = apiNamesInRange();
  if (!names.length) return;
  const sep = document.createElement("span");
  sep.id = "apiSep";
  sep.style.cssText = "opacity:.6;font-size:12px;margin-right:2px";
  sep.textContent = "API（默认全选，点一下排除）";
  box.appendChild(sep);
  names.forEach((n, i) => {
    const on = !API_OFF.has(n);
    const chip = document.createElement("span");
    chip.className = "api-chip" + (on ? "" : " off");
    chip.style.borderColor = on ? API_COLORS[i % API_COLORS.length] : "";
    chip.textContent = n;
    chip.onclick = () => {
      if (API_OFF.has(n)) { API_OFF.delete(n); }
      else {
        if (API_OFF.size + 1 >= names.length) { API_OFF.clear(); }  // 关到只剩一个 → 恢复全选
        else { API_OFF.add(n); }
      }
      head(); render(); table();
    };
    box.appendChild(chip);
  });
}
// ── 排除赠送余额（可选，纯参考）──────────────────────────────
function drawGift() {
  const box = document.getElementById("giftKpi");
  if (!box) return;
  const on = document.getElementById("giftOn").checked;
  const g = Math.max(0, Number(document.getElementById("giftVal").value || 0));
  const rows = DATA;            // 2026-10-20 用户：这个面板也是「总」的，不跟选择
  if (!on || !rows.length) { box.innerHTML = ""; return; }
  const tok = rows.reduce((a, d) => a + d.total, 0);
  const cost = rows.reduce((a, d) => a + d.cost, 0);
  const paid = Math.max(0, cost - g);
  const py0 = cost > 0 ? tok / cost : 0;
  const py1 = paid > 0 ? tok / paid : 0;
  const lift = py0 > 0 && py1 > 0 ? (py1 / py0 - 1) * 100 : 0;
  const cell = (l, v, small) => `<div class="cell"><div class="l">${l}</div>` +
      `<div class="v">${v}${small ? ` <small>${small}</small>` : ""}</div></div>`;
  box.innerHTML =
      cell("总花费", money(cost))
    + cell("其中赠送", money(g))
    + cell("自己掏的", money(paid), g > 0 && paid <= 0 ? "（全是赠送）" : "")
    + cell("每元 token（原）", cn(Math.round(py0)))
    + cell("每元 token（排除赠送）", cn(Math.round(py1)),
           (lift >= 0 ? "+" : "") + lift.toFixed(1) + "%");
}

const LEDGER_LS = "ds_ledger";
let LEDGER = { topups: [], grants: [] };
function loadLedger() {
  try {
    const raw = localStorage.getItem(LEDGER_LS);
    if (raw) {
      const j2 = JSON.parse(raw);
      LEDGER = { topups: j2.topups || [], grants: j2.grants || [] };
      return;
    }
  } catch (e) {}
  if (DEFAULT_LEDGER && (DEFAULT_LEDGER.topups || DEFAULT_LEDGER.grants)) {
    LEDGER = { topups: DEFAULT_LEDGER.topups || [], grants: DEFAULT_LEDGER.grants || [] };
  }
}
function saveLedger() {
  localStorage.setItem(LEDGER_LS, JSON.stringify(LEDGER));
  renderLedger();
  // 用 --serve 打开时，顺手写回 data/ledger.txt（file:// 下会失败，忽略）
  fetch("/ledger", { method: "POST", headers: { "Content-Type": "application/json" },
                     body: JSON.stringify(LEDGER) })
    .then(r => { if (!r.ok) throw 0; })
    .catch(() => {});   // 失败就算了，localStorage 里还有
}
const sumArr = a => a.reduce((x, y) => x + Number(y || 0), 0);
function renderLedger() {
  const secs = [
    { kind: "topups", list: "topupList", sum: "topupSum", add: "topupAdd", name: "充值" },
    { kind: "grants", list: "grantList", sum: "grantSum", add: "grantAdd", name: "赠送" },
  ];
  secs.forEach(sec => {
    const box = document.getElementById(sec.list);
    const sumEl = document.getElementById(sec.sum);
    if (sumEl) sumEl.textContent = "  合计 " + money(sumArr(LEDGER[sec.kind]));
    if (!box) return;
    box.innerHTML = "";
    LEDGER[sec.kind].forEach((v, i) => {
      const row = document.createElement("div");
      row.className = "ledger-row";
      const inp = document.createElement("input");
      inp.type = "number";
      inp.value = v;
      inp.onchange = () => {
        LEDGER[sec.kind][i] = Number(inp.value || 0);
        saveLedger(); head();
      };
      const del = document.createElement("button");
      del.textContent = "删";
      del.onclick = () => {
        LEDGER[sec.kind].splice(i, 1);
        saveLedger(); head();
      };
      row.appendChild(inp); row.appendChild(del);
      box.appendChild(row);
    });
    if (!LEDGER[sec.kind].length) {
      const empty = document.createElement("div");
      empty.style.cssText = "font-size:12px;opacity:.55";
      empty.textContent = "（还没记）";
      box.appendChild(empty);
    }
  });
  const hint = document.getElementById("ledgerHint");
  if (hint) {
    const cost = DATA.reduce((a, d) => a + d.cost, 0);
    const gt = sumArr(LEDGER.grants), tt = sumArr(LEDGER.topups);
    const used = Math.min(gt, cost);
    hint.textContent = (gt || tt)
      ? "赠送已用 " + money(used) + "，剩 " + money(Math.max(0, gt - used))
        + "；余额 " + money(Math.max(0, tt + gt - cost))
      : "";
  }
}
function head() {
  const all = DATA;                               // 全部：不受时间范围、也不受勾选影响
  const rows = curRows();                         // 当前所选：跟着勾选变
  const aTok = all.reduce((a, d) => a + d.total, 0);
  const aCost = all.reduce((a, d) => a + d.cost, 0);
  const aReq = all.reduce((a, d) => a + d.req, 0);
  const tok = rows.reduce((a, d) => a + d.total, 0);
  const cost = rows.reduce((a, d) => a + d.cost, 0);
  const req = rows.reduce((a, d) => a + d.req, 0);
  const py = cost > 0 ? Math.round(tok / cost) : 0;
  const aPy = aCost > 0 ? Math.round(aTok / aCost) : 0;
  const narrowed = !!(FROM || TO);
  document.getElementById("range").textContent = narrowed
    ? `显示 ${rows[0].d} → ${rows[rows.length - 1].d} · ${rows.length} 天`
      + (BUILT ? ` · 数据读到 ${BUILT}` : "")
    : `${K.from} → ${K.to} · 共 ${K.days} 天（有用量 ${K.active} 天，空白 ${K.empty} 天）`
      + (BUILT ? ` · 数据读到 ${BUILT}` : "");
  document.getElementById("k-tok").textContent = (aTok / 1e8).toFixed(2) + " 亿";
  const grantTotal = sumArr(LEDGER.grants);
  const topupTotal = sumArr(LEDGER.topups);
  const usedGrant = Math.min(grantTotal, aCost);
  const grantLeft = Math.max(0, grantTotal - usedGrant);
  const topupLeft = Math.max(0, topupTotal - Math.max(0, aCost - usedGrant));
  const left = (topupTotal + grantTotal) > 0 ? (topupLeft + grantLeft) : null;
  const billEl = document.getElementById("k-bill");
  if (billEl) billEl.textContent = topupTotal ? money(topupTotal) : "—";
  const billGiftEl = document.getElementById("k-bill-gift");
  if (billGiftEl) billGiftEl.textContent = grantTotal ? "（赠送 " + money(grantTotal) + "）" : "";
  const leftEl = document.getElementById("k-left");
  if (leftEl) leftEl.textContent = (left === null) ? "—" : money(left);
  const leftGiftEl = document.getElementById("k-left-gift");
  if (leftGiftEl) leftGiftEl.textContent = grantTotal ? "（赠送剩 " + money(grantLeft) + "）" : "";
  const costEl = document.getElementById("k-cost");
  if (costEl) costEl.textContent = money(aCost);
  const costGiftEl = document.getElementById("k-gift");
  if (costGiftEl) costGiftEl.textContent = grantTotal ? "（赠送已用 " + money(usedGrant) + "）" : "";
  document.getElementById("k-req").textContent = fmtInt(aReq);
  document.getElementById("k-py").innerHTML = cn(aPy) + " <small>token</small>";
  document.getElementById("k-req").textContent = fmtInt(aReq);
  const selTok = document.getElementById("s-tok");
  if (selTok) {
    selTok.textContent = (tok / 1e8).toFixed(2) + " 亿";
    document.getElementById("s-cost").textContent = money(cost);
    document.getElementById("s-req").textContent = fmtInt(req);
    document.getElementById("s-py").innerHTML = cn(py) + " <small>token</small>";
    document.getElementById("k-tok-note").textContent =
      (aTok ? ((tok / aTok) * 100).toFixed(1) + "% 被选中" : "");
  }
  buildLegend(); buildApiChips(); drawGift();
  const b = document.getElementById("banner");
  b.className = "banner";
  b.textContent = SOURCE
    ? `数据来源：${SOURCE}` + (narrowed ? "（第一行是**全部历史**，第二行才是当前这段时间 + 勾选）" : "")
    : "";
}
function apply(p) {
  DATA = p.rows || []; K = p.kpis || {}; SOURCE = p.source || ""; BUILT = p.built || "";
  APIS = p.apis || []; GIFT = (p.gift === undefined ? null : p.gift);
  DEFAULT_LEDGER = p.ledger || null;
  const f = document.getElementById("from"), to = document.getElementById("to");
  if (DATA.length) {
    f.min = to.min = DATA[0].d;
    f.max = to.max = DATA[DATA.length - 1].d;
  }
  FROM = ""; TO = ""; PRESET = "m30";
  f.value = DATA.length ? DATA[0].d : "";
  to.value = DATA.length ? DATA[DATA.length - 1].d : "";
  render(); head(); table();
  // 默认落"最近 30 天"（用户 2026-10-13："全部感觉没必要留着，以后太多了会膨胀"）——
  //    放在首屏渲染之后按一下，省得改一堆初始化顺序。
  loadLedger();
  const setBtn = document.getElementById("settingsBtn");
  const modal = document.getElementById("ledgerModal");
  if (setBtn && modal) setBtn.onclick = () => { modal.style.display = "flex"; renderLedger(); };
  const closeBtn = document.getElementById("ledgerClose");
  if (closeBtn && modal) closeBtn.onclick = () => { modal.style.display = "none"; };
  if (modal) modal.onclick = (ev) => { if (ev.target === modal) modal.style.display = "none"; };
  const bindAdd = (inputId, kind) => {
    const inp = document.getElementById(inputId);
    if (!inp) return;
    inp.onkeydown = (ev) => {
      if (ev.key !== "Enter") return;
      const v = Number(inp.value);
      if (!(v > 0)) return;
      LEDGER[kind].push(v);
      inp.value = "";
      saveLedger(); head();
    };
  };
  bindAdd("topupAdd", "topups");
  bindAdd("grantAdd", "grants");
  renderLedger();
  if (DATA.length) pick("m30");

}

// ── 时间轴：手动两格 + 快捷区间（都以**导出数据的最后一天**当"今天"） ──
const isoD = x => `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, "0")}`
              + `-${String(x.getDate()).padStart(2, "0")}`;
function presetRange(kind) {
  if (!DATA.length) return ["", ""];
  const first = DATA[0].d;
  const last = DATA[DATA.length - 1].d;
  const d = new Date(last + "T00:00:00");
  const y = d.getFullYear(), m = d.getMonth();
  let a, b;
  switch (kind) {
    case "m30": a = isoD(new Date(d.getTime() - 29 * 86400000)); b = last; break;  // ← 默认看它
    case "thisMonth": a = isoD(new Date(y, m, 1)); b = last; break;
    case "lastMonth": a = isoD(new Date(y, m - 1, 1)); b = isoD(new Date(y, m, 0)); break;
    case "m3":  a = isoD(new Date(y, m - 2, 1)); b = last; break;
    case "m6":  a = isoD(new Date(y, m - 5, 1)); b = last; break;
    case "m12": a = isoD(new Date(y, m - 11, 1)); b = last; break;
    case "thisYear": a = `${y}-01-01`; b = last; break;
    default: return ["", ""];
  }
  // 夹在数据范围内：导出的数据没那么久时，起始日就显示**实际第一天**（别显示早于数据的日子）
  return [a < first ? first : a, b > last ? last : b];
}
function pick(kind) {
  const [a, b] = presetRange(kind);
  FROM = a; TO = b; PRESET = kind;
  document.getElementById("from").value = a || (DATA[0] || {}).d || "";
  document.getElementById("to").value = b || (DATA[DATA.length - 1] || {}).d || "";
  document.querySelectorAll("#filters .chip").forEach(x => x.classList.toggle("on", x.dataset.p === kind));
  render(); head(); table();
}
document.getElementById("from").addEventListener("change", e => {
  FROM = e.target.value || ""; PRESET = "custom";
  document.querySelectorAll("#filters .chip").forEach(x => x.classList.remove("on"));
  render(); head(); table();
});
document.getElementById("to").addEventListener("change", e => {
  TO = e.target.value || ""; PRESET = "custom";
  document.querySelectorAll("#filters .chip").forEach(x => x.classList.remove("on"));
  render(); head(); table();
});
document.querySelectorAll("#filters .chip").forEach(c =>
  c.addEventListener("click", () => pick(c.dataset.p)));

// 赠送余额那两个控件：勾/改都立刻重算（纯前端，不动上面任何曲线）
["giftOn", "giftVal"].forEach(id => {
  const el = document.getElementById(id);
  if (el) { el.addEventListener("input", drawGift); el.addEventListener("change", drawGift); }
});
let rzTimer = null;
window.addEventListener("resize", () => {           // 窗口变了 → 间距/是否要滚 重算
  clearTimeout(rzTimer);
  rzTimer = setTimeout(() => render(), 150);
});

// ── 取数：优先 /data.json（服务模式，点刷新就是最新）─────────
async function load(fresh) {
  try {
    const r = await fetch("/data.json", { cache: "no-store" });
    if (!r.ok) throw new Error(r.status);
    apply(await r.json());
    document.getElementById("reload").style.display = "";
  } catch (e) {
    if (!DATA.length && EMBEDDED) apply(EMBEDDED);     // file:// 打开：用页里那份
    if (fresh) {
      const b = document.getElementById("banner");
      b.className = "banner warn";
      b.textContent = "刷新失败 —— 这个页面是静态文件吧？想要「点一下就是最新」，"
        + "用 python build.py --serve 打开。";
    }
  }
}
document.getElementById("reload").addEventListener("click", () => load(true));
const btn = document.getElementById("theme");
btn.addEventListener("click", () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark"
    || (!document.documentElement.getAttribute("data-theme")
        && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.setAttribute("data-theme", dark ? "light" : "dark");
  btn.textContent = dark ? "切换深色" : "切换浅色";
});
// ── 从本地载入（CSV 或平台导出的 zip，不用解压）────────────
function parseCSV(text) {
  const rows = [];
  let field = "", row = [], inQ = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQ) {
      if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
      else field += c;
    } else if (c === '"') inQ = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c !== "\r") field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  const head = (rows.shift() || []).map(h => h.replace(/^\uFEFF/, "").trim());
  return rows.filter(r => r.length === head.length)
             .map(r => { const o = {}; head.forEach((h, i2) => o[h] = r[i2]); return o; });
}

async function unzip(buf) {
  const dv = new DataView(buf), u8 = new Uint8Array(buf);
  let eocd = -1;
  for (let i = u8.length - 22; i >= Math.max(0, u8.length - 65558); i--) {
    if (dv.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) throw new Error("不是有效的 zip");
  const count = dv.getUint16(eocd + 10, true);
  let off = dv.getUint32(eocd + 16, true);
  const td = new TextDecoder("utf-8"), out = [];
  for (let k = 0; k < count; k++) {
    if (dv.getUint32(off, true) !== 0x02014b50) break;
    const method = dv.getUint16(off + 10, true);
    const csize = dv.getUint32(off + 20, true);
    const nlen = dv.getUint16(off + 28, true);
    const elen = dv.getUint16(off + 30, true);
    const clen = dv.getUint16(off + 32, true);
    const lho = dv.getUint32(off + 42, true);
    const name = td.decode(u8.subarray(off + 46, off + 46 + nlen));
    off += 46 + nlen + elen + clen;
    if (!/\.csv$/i.test(name)) continue;
    const lnlen = dv.getUint16(lho + 26, true), lelen = dv.getUint16(lho + 28, true);
    const start = lho + 30 + lnlen + lelen;
    const raw = u8.subarray(start, start + csize);
    let bytes = raw;
    if (method === 8) {
      const ds = new DecompressionStream("deflate-raw");
      const ab = await new Response(new Blob([raw]).stream().pipeThrough(ds)).arrayBuffer();
      bytes = new Uint8Array(ab);
    } else if (method !== 0) {
      continue;
    }
    out.push({ name: name.split("/").pop(), text: td.decode(bytes) });
  }
  return out;
}

function buildRows(amts, costs) {
  const per = {}, req = {}, cost = {}, apiDay = {}, apiReqDay = {};
  const TOK = new Set(["output_tokens", "input_cache_miss_tokens", "input_cache_hit_tokens"]);
  for (const a of amts) {
    const sPer = {}, sReq = {}, sApi = {}, sApiReq = {};
    for (const r of parseCSV(a.text)) {
      const day = (r.start_time_iso || "").slice(0, 10);
      if (!day) continue;
      const api = (r.api_key_name || "").trim() || "（未命名）";
      const n = parseInt(r.amount || "0", 10) || 0;
      if (TOK.has(r.type)) {
        sPer[day] = sPer[day] || {};
        sPer[day][r.model] = (sPer[day][r.model] || 0) + n;
        sApi[day] = sApi[day] || {};
        sApi[day][api] = (sApi[day][api] || 0) + n;
      } else if (r.type === "request_count") {
        sReq[day] = (sReq[day] || 0) + n;
        sApiReq[day] = sApiReq[day] || {};
        sApiReq[day][api] = (sApiReq[day][api] || 0) + n;
      }
    }
    Object.assign(per, sPer); Object.assign(req, sReq);
    Object.assign(apiDay, sApi); Object.assign(apiReqDay, sApiReq);
  }
  for (const c of costs) {
    for (const r of parseCSV(c.text)) {
      const day = (r.start_time_iso || "").slice(0, 10);
      if (!day) continue;
      cost[day] = (cost[day] || 0) + (parseFloat(r.cost) || 0);
    }
  }
  const days = [...new Set([...Object.keys(per), ...Object.keys(cost)])].sort();
  if (!days.length) return [];
  const out = [];
  let tokCum = 0, costCum = 0, prev = null;
  const d0 = new Date(days[0] + "T00:00:00"), d1 = new Date(days[days.length - 1] + "T00:00:00");
  for (let d = new Date(d0); d <= d1; d.setDate(d.getDate() + 1)) {
    const key = isoD(d);
    const cheap = (per[key] || {})["deepseek-flash"] || 0;
    const pricey = (per[key] || {})["deepseek-v4-pro"] || 0;
    const c = Math.round((cost[key] || 0) * 1e6) / 1e6;
    tokCum += cheap + pricey; costCum += c;
    const cum = costCum > 0 ? Math.round(tokCum / costCum) : null;
    const delta = (cum === null || prev === null) ? null : cum - prev;
    if (cum !== null) prev = cum;
    out.push({ d: key, cheap, pricey, total: cheap + pricey, req: req[key] || 0, cost: c,
               perYuan: (c > 0 && cheap + pricey) ? Math.round((cheap + pricey) / c) : null,
               cum, delta, byApi: apiDay[key] || {}, byApiReq: apiReqDay[key] || {},
               byApiCost: apiCostDay[key] || {} });
  }
  return out;
}

function kpisOf(rows) {
  const tok = rows.reduce((a, r) => a + r.total, 0);
  const cost = rows.reduce((a, r) => a + r.cost, 0);
  return { tok, cost: Math.round(cost * 100) / 100, req: rows.reduce((a, r) => a + r.req, 0),
           perYuan: cost ? Math.round(tok / cost) : 0, days: rows.length,
           empty: rows.filter(r => !r.total && !r.cost).length,
           from: rows[0].d, to: rows[rows.length - 1].d,
           cheap: rows.reduce((a, r) => a + r.cheap, 0),
           pricey: rows.reduce((a, r) => a + r.pricey, 0),
           active: rows.filter(r => r.total).length };
}

async function ingest(list) {
  const files = [...list];
  if (!files.length) return;
  const parts = [];
  try {
    for (const f of files) {
      if (/\.zip$/i.test(f.name)) {
        parts.push(...await unzip(await f.arrayBuffer()));
      } else if (/\.csv$/i.test(f.name)) {
        parts.push({ name: f.name, text: await f.text() });
      }
    }
  } catch (e) {
    alert("读文件失败：" + e.message);
    return;
  }
  const amts = parts.filter(p => /^amount-.*\.csv$/i.test(p.name)).sort((a, b) => a.name < b.name ? -1 : 1);
  const costs = parts.filter(p => /^cost-.*\.csv$/i.test(p.name)).sort((a, b) => a.name < b.name ? -1 : 1);
  if (!amts.length || !costs.length) {
    alert("没找到 amount-*.csv / cost-*.csv。\n平台导出的 zip 里应该各有一份；也可以分别选中两个 csv。");
    return;
  }
  const rows = buildRows(amts, costs);
  if (!rows.length) { alert("这些文件里没有可用数据。"); return; }
  apply({ rows, kpis: kpisOf(rows), apis: apisOf(rows),
          source: "本地载入：" + files.map(f => f.name).join(" + "),
          built: new Date().toLocaleString("zh-CN", { hour12: false }) });
}

function apisOf(rows) {
  const m = {};
  rows.forEach(r => { for (const [k, v] of Object.entries(r.byApi || {})) m[k] = (m[k] || 0) + v; });
  return Object.entries(m).sort((a, b) => b[1] - a[1]).map(([name, tok]) => ({ name, tok }));
}

// 「载入文件」按钮 → 打开文件选择框（可多选，zip / csv 都行）
document.getElementById("fileBtn").addEventListener("click", () =>
  document.getElementById("fileIn").click());
document.getElementById("fileIn").addEventListener("change", e => {
  ingest(e.target.files);
  e.target.value = "";            // 允许再次选同一个文件
});

load(false);
</script>

<div id="ledgerModal" class="modal" style="display:none">
  <div class="modal-box">
    <h3 style="margin:0 0 4px">充值与赠送（累计记账）</h3>
    <div class="note" style="margin-bottom:10px">每笔单独一行，改数字直接生效，「删」就删掉那一笔。</div>
    <div class="ledger-sec">
      <div class="ledger-head">充值<span id="topupSum" class="gift"></span></div>
      <div id="topupList" class="ledger-list"></div>
      <input id="topupAdd" type="number" placeholder="输入金额，回车添加">
    </div>
    <div class="ledger-sec">
      <div class="ledger-head">赠送<span id="grantSum" class="gift"></span></div>
      <div id="grantList" class="ledger-list"></div>
      <input id="grantAdd" type="number" placeholder="输入金额，回车添加">
    </div>
    <div class="modal-foot">
      <span id="ledgerHint" style="opacity:.75;font-size:12px"></span>
      <button id="ledgerSave" class="ghost">导出 ledger.txt</button>
      <button id="ledgerClose">关闭</button>
    </div>
  </div>
</div>

</body></html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
