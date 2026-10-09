# 常用操作的唯一入口 —— 别再手敲长命令，也别靠"记得第二步"。
# `make up` 等价于 README 的「快速开始」：起栈 → 引导(schema+复制) → seed → 体检。
.PHONY: up up-build doctor seed bootstrap down logs verify-fast verify heldout clean

up:            ## 一条命令把环境带到可用状态
	python tools/dev_up.py

up-build:      ## 同上，但顺带重建镜像（首次或改了代码后用）
	python tools/dev_up.py --build

doctor:        ## 环境体检（18 项）
	python tools/fault_injector.py doctor

bootstrap:     ## 只做引导：建 schema + 配主从复制（这步不在 compose 里）
	python tools/cluster_bootstrap.py

seed:          ## 只建合成数据集（约 480 万行）
	python tools/fault_injector.py seed

down:          ## 停栈并删除卷（数据会没，谨慎）
	docker compose -f rca-agent/docker-compose.yml down -v

logs:          ## 看日志
	docker compose -f rca-agent/docker-compose.yml logs --tail 40 -f

verify-fast:   ## 代表性子集（7 个场景）—— 快速反馈
	python tools/fault_injector.py verify --fast

verify:        ## 全量 21 场景（约 45 分钟）
	python tools/fault_injector.py verify

heldout:       ## held-out 复验（过拟合检查）
	python tools/heldout_verify.py --with-faults

clean:         ## 回滚所有残留故障 + 清理
	python tools/fault_injector.py cleanup
