# Verification reports

原始产物，由 `tools/` 下的脚本生成。**每个数字都能在这里点开核对。**

宿主绝对路径已清洗为 `<repo>` / `<home>`；其余内容为原始输出（未手工编辑）。

| 报告 | 测量什么 | 生成命令 | README 里对应的结论 |
|---|---|---|---|
| [`fault_verify_report.json`](fault_verify_report.json) | 21 场景注入/信号/回滚 | `python tools/fault_injector.py verify` | 故障复现 21/21；注入期信号成立 |
| [`alert_coverage.json`](alert_coverage.json) | 告警覆盖：注入→等够久→声明的告警是否 firing | `python tools/alert_coverage_check.py` | 声明的告警实际触发 20/21 = 95% |
| [`rca_diagnosis_report.json`](rca_diagnosis_report.json) | 端到端：注入→压测→告警→诊断→评分 | `python tools/cluster_rca_verify.py` | 严格 top-1；恢复核对 |
| [`promotion_ab.json`](promotion_ab.json) | 组件级→根因级升级的成对 A/B | `python tools/rootcause_promotion_verify.py` | 严格 17→18/21，top5 20→21/21 |
| [`heldout_report.json`](heldout_report.json) | 未参与调参的输入：措辞 + 同根因不同注入手法 | `python tools/heldout_verify.py --with-faults` | held-out 严格 4/10 = 40% |
| [`crosscluster_report.json`](crosscluster_report.json) | 跨集群归属：根因落在哪个集群（该找谁） | `python tools/crosscluster_verify.py` | 16/18 = 89% |
| [`ontology_v6_report.json`](ontology_v6_report.json) | 第 6 轮本体迭代：成对评估 + 闸门 | `python tools/evolve_ontology_cluster.py --round memory` | ontology_v6 已发布 |
| [`remediation_exec.json`](remediation_exec.json) | 处置动作执行闭环（含失败自动回滚） | `python tools/remediation_exec.py self-test` | 回滚路径验证 |

## 口径说明（重要）

- 这些报告来自**不同实验**，数字不可互相推导：`e2e` 是端到端一轮的 18 个场景，
  `promotion_ab` 是回放真实告警文本的成对 A/B，`fault_verify` 是注入层的能力验证。
- `heldout_report.json` 是本项目**主动公开的负面结果**（措辞换一种说法后严格命中 4/10）。
  修它时**不得**在同一份 held-out 集上调参。
- 阈值/窗口等判据的变更都必须走 `tools/evolve_ontology_cluster.py` 的版本化轮次 + 闸门，
  不接受「为了让检查变绿」而直接改数字。
