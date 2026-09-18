# TimescaleDB 数据库镜像：基于 postgres:16 + apt 安装 TimescaleDB 扩展
#
# 为什么不用官方 timescale/timescaledb:latest-pg16 镜像？
#   它约 500MB，国内加速器大多没有缓存，会回落直连 Docker Hub 导致下载龟速
#   甚至中断（本项目在阿里云 ECS 上实测卡死在这一步）。
#   而 postgres:16 只有约 140MB，被所有国内加速器缓存，秒下。
#
# 功能等价，只是扩展改由 apt 安装。
FROM postgres:16

ENV TZ=Asia/Shanghai

RUN set -eux; \
    # 1) Debian 源换成清华镜像，加速 apt
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    # 2) 加 TimescaleDB 官方 apt 源并安装扩展
    apt-get install -y --no-install-recommends gnupg lsb-release ca-certificates wget; \
    echo "deb https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -c -s) main" \
        > /etc/apt/sources.list.d/timescaledb.list; \
    wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg; \
    apt-get update; \
    apt-get install -y --no-install-recommends timescaledb-2-postgresql-16; \
    # 3) 清理构建依赖与 apt 缓存，缩小镜像
    apt-get purge -y --auto-remove gnupg wget; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# 关键：PostgreSQL 官方镜像会自动 include_dir 这个目录下的所有 .conf 文件。
# 把 shared_preload_libraries 放在这里，而不是追加到 postgresql.conf.sample
# （entrypoint 用 initdb 生成 postgresql.conf，不会读 .sample，所以那样写无效）。
#
# 这一步必须在 entrypoint 启动临时实例做 initdb 之前生效，
# 这样 init.sql 里的 CREATE EXTENSION timescaledb 才能成功。
# ---------------------------------------------------------------------------
RUN set -eux; \
    mkdir -p /etc/postgresql/conf.d; \
    echo "shared_preload_libraries = 'timescaledb'" \
        > /etc/postgresql/conf.d/10-timescaledb.conf; \
    cat /etc/postgresql/conf.d/10-timescaledb.conf
