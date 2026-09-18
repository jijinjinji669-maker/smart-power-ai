# TimescaleDB 数据库镜像：基于 postgres:16 + apt 安装 TimescaleDB 扩展
#
# 为什么不用官方 timescale/timescaledb:latest-pg16 镜像？
#   它约 500MB，国内加速器大多没有缓存，会回落直连 Docker Hub 导致下载龟速
#   甚至中断（本项目在阿里云 ECS 上实测卡死在这一步）。
#   而 postgres:16 只有约 140MB，被所有国内加速器缓存，秒下。
FROM postgres:16

ENV TZ=Asia/Shanghai

RUN set -eux; \
    # Debian 源换成国内镜像，加速 apt
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    # 加 TimescaleDB 官方 apt 源并安装扩展
    apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates wget; \
    echo "deb https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -c -s) main" \
        > /etc/apt/sources.list.d/timescaledb.list; \
    wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg; \
    apt-get update; \
    apt-get install -y --no-install-recommends timescaledb-2-postgresql-16; \
    # 清理构建依赖与 apt 缓存，缩小镜像
    apt-get purge -y --auto-remove gnupg wget; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 关键一步：让 TimescaleDB 被 preload。
# 少了这一步，init.sql 里的 CREATE EXTENSION timescaledb 会直接失败：
#     FATAL: extension "timescaledb" must be preloaded
# 而且是致命错误 —— 它会让整个 init.sql 中断，后续所有建表都不执行。
#
# 原理：官方 postgres 镜像的 entrypoint 用 initdb 初始化数据目录，
# initdb 会把 /usr/share/postgresql/16/postgresql.conf.sample **复制**成
# $PGDATA/postgresql.conf。所以要改的是这份「模板」，不是数据目录里的那份。
#
# 为什么这一行就够了：entrypoint 在「跑 init.sql 的临时实例」和
# 「最终正式实例」上都用同一份 initdb 产物，所以改模板对两者同时生效。
#
# ---------------------------------------------------------------------------
# 踩过的两个坑（都实测无效，记录在此避免重犯）：
#
#   ✗ 把配置放 /etc/postgresql/conf.d/ 或 Dockerfile 里 mkdir 一个 conf.d
#     完全没用。官方镜像的 initdb 生成的 postgresql.conf 里**没有**
#     include_dir 任何 conf.d 目录。实测日志证据：
#         "PostgreSQL Database directory appears to contain a database;
#          Skipping initialization"
#     然后 CREATE EXTENSION 依旧报 must be preloaded。
#     只有你自己提供 postgresql.conf 并显式 include 该目录时，conf.d 才有意义。
#
#   ✗ 只在 docker-compose 的 command 里加
#         -c shared_preload_libraries=timescaledb
#     也不行。因为 CREATE EXTENSION 是在 entrypoint 创建的那个**临时实例**上
#     执行的，而该实例不受 compose 的 -c 参数影响。
# ---------------------------------------------------------------------------
RUN set -eux; \
    CONF=/usr/share/postgresql/16/postgresql.conf.sample; \
    if grep -q "^shared_preload_libraries" "$CONF"; then \
        sed -i "s|^shared_preload_libraries.*|shared_preload_libraries = 'timescaledb'|" "$CONF"; \
    else \
        echo "shared_preload_libraries = 'timescaledb'" >> "$CONF"; \
    fi; \
    echo "--- 确认写入结果 ---"; \
    grep -n "shared_preload_libraries" "$CONF"
