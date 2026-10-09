#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证可调宽面板（splitter）已正确构建并服务."""
import glob, json, re, sys
from pathlib import Path
import urllib.request

FRONT = "http://localhost:3001"
DIST = Path(r"D:\05_code\credit-card-sys-ops\rca-agent\frontend\dist")
PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + name)
    else:
        FAIL += 1
        print("  [FAIL] " + name + " -- " + str(detail)[:200])


print("=" * 72)
print("RESIZABLE PANEL VERIFICATION")
print("=" * 72)

# ── 1. 源码文件存在 ────────────────────────────────────────────────────
print("\n[1] Source files")
hook = Path(r"D:\05_code\credit-card-sys-ops\rca-agent\frontend\src\hooks\useResizablePanel.ts")
page = Path(r"D:\05_code\credit-card-sys-ops\rca-agent\frontend\src\pages\AgentPage.tsx")
check("hook file exists", hook.exists(), hook)
check("page file exists", page.exists(), page)

hook_src = hook.read_text(encoding="utf-8") if hook.exists() else ""
page_src = page.read_text(encoding="utf-8") if page.exists() else ""

for token in ["onHandleMouseDown", "onHandleTouchStart", "onHandleKeyDown",
              "onHandleDoubleClick", "toggleCollapsed", "localStorage",
              "ResizeObserver", "ArrowLeft", "ArrowRight"]:
    check("hook implements " + token, token in hook_src)

for token in ["useResizablePanel", "splitter", "role=\"separator\"",
              "panel.collapsed", "onMouseDown={panel.onHandleMouseDown}"]:
    check("page wires " + token, token in page_src, token)

# ── 2. 构建产物 ────────────────────────────────────────────────────────
print("\n[2] Built bundle")
js_files = [p for p in glob.glob(str(DIST / "assets" / "*.js")) if not p.endswith(".map")]
css_files = glob.glob(str(DIST / "assets" / "*.css"))
check("js bundle exists", len(js_files) == 1, js_files)
check("css bundle exists", len(css_files) == 1, css_files)

js = Path(js_files[0]).read_text(encoding="utf-8", errors="replace") if js_files else ""
css = Path(css_files[0]).read_text(encoding="utf-8", errors="replace") if css_files else ""

for token in ["separator", "col-resize", "rca-agent:detail-width", "localStorage",
              "ResizeObserver", "ArrowLeft", "ArrowRight"]:
    check("bundle contains " + token, token in js, token)

for token in ["splitter", "splitter-grip", "col-resize", "is-dragging",
              "panel-detail.collapsed", "pointer-events:none"]:
    check("css contains " + token, token.replace(" ", "") in css.replace(" ", ""), token)

# ── 3. 服务可达 ────────────────────────────────────────────────────────
print("\n[3] Served by nginx")


def get(url, timeout=20):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


try:
    st, html = get(FRONT + "/")
    check("index served", st == 200 and "<!doctype html" in html.lower())
    m = re.search(r'src="(/assets/[^"]+\.js)"', html)
    check("index references bundle", m is not None, html[:200])
    m2 = re.search(r'href="(/assets/[^"]+\.css)"', html)
    check("index references css", m2 is not None)
    if m:
        st2, served_js = get(FRONT + m.group(1))
        check("bundle fetchable via nginx", st2 == 200 and len(served_js) > 10000, len(served_js))
        check("served bundle has splitter logic", "separator" in served_js and "col-resize" in served_js)
    if m2:
        st3, served_css = get(FRONT + m2.group(1))
        check("css fetchable via nginx", st3 == 200)
        check("served css has .splitter", ".splitter" in served_css)
except Exception as e:
    check("nginx serve", False, e)

# ── 4. API 未受影响 ────────────────────────────────────────────────────
print("\n[4] API regression")
try:
    st, body = get(FRONT + "/api/health")
    check("api health ok", '"ok"' in body, body[:80])
    st, body = get(FRONT + "/api/rca/incidents")
    d = json.loads(body)
    check("incidents still served", d.get("count", 0) > 0, d.get("count"))
except Exception as e:
    check("api regression", False, e)

print("\n" + "=" * 72)
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
print("=" * 72)
sys.exit(0 if FAIL == 0 else 1)
