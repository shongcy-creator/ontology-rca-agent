/**
 * 客户端半边：把 credit-card-sys-ops 的**故障注入控制台**嵌入 DSH GUI。
 *
 * 为什么是"薄插件"而不是把控制台重写一遍：
 * 注入/回滚/清理/中止的全部逻辑都在后端的 `/api/chaos/*` 里（背后是 tools/chaos），
 * 这里只做三件事 —— 取状态、发指令、显示任务日志。界面换了地方，行为完全一致。
 *
 * 挂载点（由 Slots Inspect 确认的契约）：
 *   · `sidebar.panellist`（list）：**id 与 main 面板的 key 同名**，侧栏会画一个按钮，
 *     组件收到 `{size, active}` 两个 owner props，应当只画图标；
 *   · `main`（keyed）：`key = "chaos-console"` 时渲染整个中央面板。
 *     该槽"已占用的 key"里只有 conversation/plugins，新 key 不会遮蔽任何已有 UI。
 *
 * 手写 `window.__ModuleLoader__.load` 包装：与官方插件产物同构，
 * 因此不需要 tsdown 之类的构建步骤（本插件没有 JSX，React 用 createElement 直写）。
 */
window.__ModuleLoader__.load({
  id: "cc-ops-dsh-plugin-chaos-console",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");
    const h = React.createElement;

    /** 需要客户端提供的服务：slots（注册界面）。 */
    const inject = ["slots"];

    // ── 配置（存在 DSH 这一侧的 localStorage；与控制台页面不同源，所以各存一份）──
    const LS_BASE = "cc-ops:chaos-base";
    const LS_TOKEN = "cc-ops:chaos-token";
    const PANEL_ID = "chaos-console";

    function lsGet(k, d) {
      try { return localStorage.getItem(k) || d; } catch (e) { return d; }
    }
    function lsSet(k, v) {
      try { v ? localStorage.setItem(k, v) : localStorage.removeItem(k); } catch (e) { /* 隐私模式 */ }
    }

    let BASE = lsGet(LS_BASE, "http://127.0.0.1:8088");
    let TOKEN = lsGet(LS_TOKEN, "");

    async function api(path, init) {
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        TOKEN ? { "X-Chaos-Token": TOKEN } : {}
      );
      const res = await fetch(BASE + "/api/chaos" + path, Object.assign({ headers }, init || {}));
      const text = await res.text();
      let body = null;
      try { body = text ? JSON.parse(text) : null; } catch (e) { body = { detail: text }; }
      if (!res.ok) {
        const msg = (body && (body.error || body.detail)) || ("HTTP " + res.status);
        throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
      }
      return body;
    }

    const RISK = { low: "低", medium: "中", high: "高", critical: "极高" };
    const LAYER = { application: "应用层", database: "数据库层", resource: "跨层资源", env: "运行环境" };

    const css = {
      root: { padding: "16px 20px", overflowY: "auto", height: "100%", boxSizing: "border-box",
              color: "var(--dsw-alias-label-primary)", fontSize: "13px" },
      head: { display: "flex", alignItems: "center", gap: "10px", marginBottom: "12px", flexWrap: "wrap" },
      title: { fontSize: "15px", fontWeight: 600, margin: 0 },
      sub: { fontSize: "11px", color: "var(--dsw-alias-label-tertiary)" },
      input: { fontSize: "11px", padding: "2px 6px", borderRadius: "5px",
               border: "1px solid var(--dsw-alias-border-l2)",
               background: "var(--dsw-alias-fill-l2)", color: "var(--dsw-alias-label-primary)" },
      btn: { fontSize: "12px", padding: "4px 10px", borderRadius: "6px", cursor: "pointer",
             border: "1px solid var(--dsw-alias-border-l2)",
             background: "var(--dsw-alias-fill-l2)", color: "var(--dsw-alias-label-primary)" },
      btnDanger: { borderColor: "var(--dsw-alias-label-error, #f85149)",
                   color: "var(--dsw-alias-label-error, #f85149)" },
      card: { border: "1px solid var(--dsw-alias-border-l2)", borderRadius: "8px",
              padding: "10px 12px", marginBottom: "12px", background: "var(--dsw-specific-menu, transparent)" },
      row: { display: "flex", alignItems: "center", gap: "8px", padding: "5px 0",
             borderBottom: "1px solid var(--dsw-alias-border-l2)" },
      rowLast: { borderBottom: "none" },
      name: { flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" },
      chip: { fontSize: "10px", padding: "1px 6px", borderRadius: "8px",
              border: "1px solid var(--dsw-alias-border-l2)",
              color: "var(--dsw-alias-label-tertiary)" },
      ok: { color: "var(--dsw-alias-label-success, #3fb950)" },
      bad: { color: "var(--dsw-alias-label-error, #f85149)" },
      log: { fontFamily: "var(--dsw-font-mono, monospace)", fontSize: "11px",
             whiteSpace: "pre-wrap", maxHeight: "220px", overflowY: "auto",
             color: "var(--dsw-alias-label-secondary)" },
    };

    /** 侧栏图标：只需画图标，尺寸与选中态由 owner 给。 */
    function ChaosIcon(props) {
      const s = (props && props.size) || 16;
      const active = props && props.active;
      return h(
        "svg",
        { width: s, height: s, viewBox: "0 0 24 24", fill: "none",
          stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round",
          strokeLinejoin: "round", style: { opacity: active ? 1 : 0.72 } },
        h("path", { d: "M13 2 3 14h7l-1 8 10-12h-7l1-8z" })
      );
    }

    /** 中央面板：状态 + 场景 + 最近任务。 */
    function ChaosConsole() {
      const [state, setState] = React.useState({ loading: true, error: null });
      const [busy, setBusy] = React.useState(null);
      const [showLog, setShowLog] = React.useState(true);
      const [cfgOpen, setCfgOpen] = React.useState(false);
      const [base, setBase] = React.useState(BASE);
      const [token, setToken] = React.useState(TOKEN);
      const [msg, setMsg] = React.useState(null);

      const refresh = React.useCallback(async () => {
        try {
          const [status, health, scenarios, jobs] = await Promise.all([
            api("/status"), api("/health"), api("/scenarios"),
            api("/jobs?limit=1").catch(() => ({ jobs: [] })),
          ]);
          const last = (jobs.jobs || [])[0] || null;
          let job = null;
          if (last) job = await api("/jobs/" + last.id).catch(() => last);
          setState({ loading: false, error: null, status, health, scenarios, job });
        } catch (e) {
          setState((s) => Object.assign({}, s, { loading: false, error: String(e.message || e) }));
        }
      }, []);

      React.useEffect(() => {
        let alive = true;
        const tick = () => { if (alive) refresh(); };
        tick();
        const t = window.setInterval(tick, 3000);
        return () => { alive = false; window.clearInterval(t); };
      }, [refresh]);

      const act = async (label, fn) => {
        setBusy(label); setMsg(null);
        try { const r = await fn(); setMsg({ ok: true, text: label + " 已提交" + (r && r.job ? "（任务 " + r.job.id + "）" : "") }); }
        catch (e) { setMsg({ ok: false, text: label + " 失败：" + String(e.message || e) }); }
        finally { setBusy(null); refresh(); }
      };

      const status = state.status || {};
      const health = state.health || {};
      const faults = status.active_faults || [];
      const running = !!(state.job && ["running", "pending"].indexOf(state.job.state) >= 0);
      const byLayer = {};
      (state.scenarios || []).forEach((s) => {
        const k = s.layer || "other";
        (byLayer[k] = byLayer[k] || []).push(s);
      });

      return h("div", { style: css.root },
        h("div", { style: css.head },
          h("h2", { style: css.title }, "⚡ 故障注入控制台"),
          h("span", { style: css.sub }, "credit-card-sys-ops · 复用 /api/chaos/*"),
          h("span", { style: Object.assign({}, css.chip, health.ok ? css.ok : css.bad) },
            "引擎 " + (health.ok ? "可用" : "不可用")),
          h("span", { style: Object.assign({}, css.chip, faults.length ? css.bad : css.ok) },
            "激活故障 " + faults.length),
          h("span", { style: css.sub }, "副本 " + ((status.app && status.app.running) || "-") + "/" +
            ((status.app && status.app.total) || "-")),
          h("button", { style: Object.assign({}, css.btn, { marginLeft: "auto" }),
                        onClick: () => setCfgOpen((v) => !v) }, "⚙ 连接设置"),
          h("button", { style: css.btn, onClick: refresh }, "🔄 刷新")
        ),

        cfgOpen && h("div", { style: css.card },
          h("div", { style: Object.assign({}, css.row, css.rowLast) },
            h("span", { style: css.name }, "后端地址"),
            h("input", { style: Object.assign({}, css.input, { width: "240px" }), value: base,
                         onChange: (e) => setBase(e.target.value) }),
            h("span", { style: css.name }, "访问令牌（CHAOS_API_TOKEN）"),
            h("input", { style: Object.assign({}, css.input, { width: "180px" }), value: token,
                         type: "password", placeholder: "未设置可留空",
                         onChange: (e) => setToken(e.target.value) }),
            h("button", { style: css.btn, onClick: () => {
              BASE = base.replace(/\/+$/, ""); TOKEN = token.trim();
              lsSet(LS_BASE, BASE); lsSet(LS_TOKEN, TOKEN);
              setCfgOpen(false); setMsg({ ok: true, text: "连接设置已保存" }); refresh();
            } }, "保存")
          ),
          h("div", { style: css.sub },
            "注入接口等价于「能操作这些容器」——对外暴露时后端必须设 CHAOS_API_TOKEN 并限制网段；" +
            "令牌只存在本机浏览器。")
        ),

        state.error && h("div", { style: Object.assign({}, css.card, css.bad) },
          "读取失败：" + state.error + "（检查后端地址与令牌；后端未启动时属正常）"),
        msg && h("div", { style: Object.assign({}, css.card, msg.ok ? css.ok : css.bad) }, msg.text),

        h("div", { style: css.card },
          h("div", { style: { marginBottom: "6px" } },
            h("b", null, "运行中"),
            running
              ? h("span", { style: Object.assign({}, css.chip, { marginLeft: "8px" }, css.bad) },
                  (state.job.kind || "job") + " · " + state.job.state)
              : h("span", { style: Object.assign({}, css.chip, { marginLeft: "8px" }) }, "空闲"),
            h("button", { style: Object.assign({}, css.btn, css.btnDanger, { marginLeft: "10px" }),
                          disabled: !!busy,
                          onClick: () => act("停止并回滚", () => api("/stop_and_rollback",
                            { method: "POST", body: JSON.stringify({ confirm: true }) })) },
              "⏹ 停止并回滚"),
            h("button", { style: Object.assign({}, css.btn, { marginLeft: "6px" }), disabled: !!busy,
                          onClick: () => act("清理", () => api("/cleanup", { method: "POST", body: "{}" })) },
              "🧹 清理")
          ),
          faults.length
            ? faults.map((f, i) => h("div",
                { key: i, style: Object.assign({}, css.row, i === faults.length - 1 ? css.rowLast : {}) },
                h("span", { style: css.name }, (f.title || f.fault_id) + "  → " + (f.target || "")),
                h("span", { style: css.chip }, "信号 " + ((f.signal_state && f.signal_state.state) || "-")),
                h("button", { style: css.btn, disabled: !!busy,
                              onClick: () => act("回滚 " + f.fault_id, () => api("/recover",
                                { method: "POST", body: JSON.stringify({ fault_id: f.fault_id }) })) },
                  "回滚")
              ))
            : h("div", { style: css.sub }, "当前没有激活故障")
        ),

        Object.keys(byLayer).map((layer) => h("div", { key: layer, style: css.card },
          h("div", { style: { marginBottom: "6px" } }, h("b", null, LAYER[layer] || layer),
            h("span", { style: Object.assign({}, css.chip, { marginLeft: "8px" }) },
              byLayer[layer].length + " 个场景")),
          byLayer[layer].map((s, i) => h("div",
            { key: s.id, style: Object.assign({}, css.row,
                i === byLayer[layer].length - 1 ? css.rowLast : {}) },
            h("span", { style: css.name, title: s.description }, s.title),
            h("span", { style: css.chip }, "风险 " + (RISK[s.risk] || s.risk || "-")),
            h("button", { style: css.btn, disabled: !!busy,
                          onClick: () => act("注入 " + s.id, () => api("/inject",
                            { method: "POST", body: JSON.stringify({ fault_id: s.id, confirm: true }) })) },
              "注入"),
            h("button", { style: css.btn, disabled: !!busy,
                          onClick: () => act("回滚 " + s.id, () => api("/recover",
                            { method: "POST", body: JSON.stringify({ fault_id: s.id }) })) },
              "回滚")
          ))
        )),

        state.job && h("div", { style: css.card },
          h("div", { style: { marginBottom: "6px" } },
            h("b", null, "最近任务"),
            h("span", { style: Object.assign({}, css.chip, { marginLeft: "8px" }) },
              (state.job.kind || "") + " · " + (state.job.state || "")),
            h("button", { style: Object.assign({}, css.btn, { marginLeft: "8px" }),
                          onClick: () => setShowLog((v) => !v) },
              showLog ? "收起日志" : "展开日志")
          ),
          showLog && h("div", { style: css.log },
            ((state.job.logs || []).slice(-40).map((l) => l.line || l.message || "").join("\n")
              || state.job.log_tail || "（暂无日志）"))
        ),

        h("div", { style: css.sub },
          "提示：批量演练请在原控制台（:3001）发起 —— 它有二次确认与子集/全量选择；" +
          "本面板适合「看一眼状态、单点注入/回滚」。")
      );
    }

    /** 插件体：注册侧栏图标 + 中央面板（两者 id/key 必须一致）。 */
    function apply(ctx) {
      ctx.slots.inject("sidebar.panellist", () =>
        ctx.slots.register({ name: "sidebar.panellist", id: PANEL_ID, order: 40,
                             label: "故障注入" }, ChaosIcon));
      ctx.slots.inject("main", () =>
        ctx.slots.register({ name: "main", key: PANEL_ID }, ChaosConsole));
    }

    exports.apply = apply;
    exports.inject = inject;
    return module.exports;
  },
});
