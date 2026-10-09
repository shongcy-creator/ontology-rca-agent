/**
 * Host 半边：本插件是**纯客户端**的（界面只是 `/api/chaos/*` 的薄外壳，
 * 真正的注入/回滚逻辑在后端与 tools/chaos 里）。
 *
 * 这里刻意什么都不做 —— 但必须存在且导出 `apply`：
 * DSH 的加载器会同时加载插件的 host/client 两个半边，
 * 缺 host 半边会让整条 bundle 记录不完整（表现为插件列表里"已装但未生效"）。
 */
export const name = "cc-ops-chaos-console";

/** 无 Host 侧副作用：不注册 Service、不监听 Event。 */
export function apply() {
  /* intentionally empty */
}
