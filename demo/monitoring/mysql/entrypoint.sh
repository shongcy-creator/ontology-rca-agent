#!/bin/sh
# 入口脚本：从环境变量读取 DSN 并传递给 mysqld_exporter
exec /bin/mysqld_exporter --data-source-name="${DATA_SOURCE_NAME}"
