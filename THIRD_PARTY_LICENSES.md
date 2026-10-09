# 第三方组件与许可证

本仓库包含（或以依赖形式使用）以下第三方组件。**再分发时必须保留其版权声明。**

## 随仓库分发（vendored）

| 组件 | 位置 | 许可证 | 版权 |
|---|---|---|---|
| [EvoOntology](https://github.com/ruc-datalab/EvoOntology) | `vendor/EvoOntology/` | **MIT** | Copyright (c) 2026 Meiduo Chong（版权人以 `vendor/EvoOntology/LICENSE` 原文为准） |
| cytoscape.js（前者的可视化依赖） | `vendor/EvoOntology/**/visualization/vendor/` | MIT（见同目录 `LICENSE.cytoscape.txt`） | 见该文件 |

MIT 允许再分发与修改，唯一义务是**保留版权与许可声明** —— `vendor/EvoOntology/LICENSE`
与 `LICENSE.cytoscape.txt` 已随目录保留，请勿删除。

## 以依赖形式使用（未 vendored，由各自包管理器安装）

| 组件 | 用途 | 许可证 |
|---|---|---|
| FastAPI / Uvicorn / Pydantic | 诊断后端 | MIT / BSD-3-Clause |
| prometheus-client / PromQL 相关库 | 指标与告警 | Apache-2.0 |
| rdflib | TTL 本体解析（`tools/ontology_ttl_sync.py`） | BSD-3-Clause |
| React / Vite | 控制台前端 | MIT |
| MySQL 8.0、Prometheus、Grafana、Alertmanager、blackbox-exporter、Nginx | 运行组件（**以容器镜像形式使用，不随仓库分发**） | GPL-2.0 / Apache-2.0 / AGPL-3.0 / MIT 等，按其各自条款 |
| `@deepseek-ai/dsh-*`（可选） | `dsh-plugin-chaos-console/` 依赖的 DSH 平台 | MIT |

> 注意：**Grafana 为 AGPL-3.0**，Prometheus 为 Apache-2.0，MySQL 社区版为 GPL-2.0。
> 本项目只通过镜像/接口使用它们，不修改也不分发其源码；若你要**再分发镜像**，请自行核对其条款。
