# cc-ops-dsh-plugin-chaos-console

把 **credit-card-sys-ops 的故障注入控制台**嵌进 DSH GUI 的薄客户端插件。

## 它做什么

在 DSH 侧栏加一个「故障注入」图标（`sidebar.panellist`，id `chaos-console`），
点开是一个**中央面板**（`main`，key `chaos-console`），显示：

- 引擎可用性 / 激活故障数 / 应用副本数
- 当前激活的故障（可逐个回滚）+ **⏹ 停止并回滚** + 🧹 清理
- 21 个故障场景（按层次分组、带风险等级），每个可 **注入** / **回滚**
- 最近一次任务的日志尾部
- ⚙ 连接设置：后端地址与访问令牌（存在浏览器本地）

界面只是 `/api/chaos/*` 的薄外壳 —— 注入/回滚/清理/中止的**全部逻辑仍在后端**
（背后是 `tools/chaos`），因此行为与原控制台完全一致，不存在两套实现漂移的问题。

## 装 / 卸

```bash
# 装（DSH 的 plugin_manager 工具；本地路径用 file: 前缀，写仓库根的**相对**形式即可）
install_bundle  target=file:<repo>\dsh-plugin-chaos-console
# 卸
remove_bundle   target=cc-ops-dsh-plugin-chaos-console
```

装完后**刷新一下 GUI 页面**即可看到侧栏图标（客户端插件不在 HMR 覆盖范围内）。

临时关掉（不改包）：在 profile 的 `cordis.patch.yml` 里按行 id 覆盖：

```yaml
- id: cc-ops-chaos-console
  name: 'cc-ops-dsh-plugin-chaos-console'
  config:
    enabled: false
```

## 边界（刻意不做的事）

- **不做批量演练**：一键 20–40 分钟的破坏性操作只在原控制台（`:3001`）提供 ——
  它有二次确认 + 子集/全量选择；插件面板只适合"看一眼状态、单点注入/回滚"。
- **不内嵌原页面 iframe**：原控制台要连的后端与 GUI 不同源，iframe 会引入
  额外的鉴权/尺寸问题；直接复用同一套 HTTP 接口更薄也更稳。
- **不缓存状态**：每 3 秒轮询后端，避免"插件显示的状态"与"真实集群状态"分叉。

## 已验证（2026-10-08）

| 项 | 结果 |
|---|---|
| bundle 安装 | `application: applied` ✔ |
| 客户端半边真的执行 | `Slots.listSubTree` 从**运行中的客户端**查到 `sidebar.panellist` 有 `chaos-console`（order 40, active）与本插件注册的 `main` key ✔ |
| 跨端口取数（CORS） | 以 `Origin: http://127.0.0.1:19387` 请求 `/api/chaos/status` → 200 且 `Access-Control-Allow-Origin` 回显该源；带 `x-chaos-token` 的预检也放行 ✔ |
| 面板视觉呈现 | **未自证** —— DSH GUI 需要 `dsh web` 打印的带 token 的 URL（无凭据访问一律 401），这一步需在已鉴权的窗口里点一下侧栏图标确认 |

> 安装时踩过的坑：`install_bundle` 会把包**复制**进 profile 的 `node_modules`，
> 若第一次装的是旧版本（缺 `dsh.bundle.patch`），后续 pnpm 会报 "Already up to date"
> 而不刷新副本 —— 表现是**一直报 `not-bundle`**。改包后要先删掉那份副本再重装。
