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
# 关键一步：让 TimescaleDB 被 preload，否则
# CREATE EXTENSION timescaledb 会报 FATAL: extension "timescaledb" must be preloaded
#
# 原理：官方 postgres 镜像的 entrypoint 用 initdb 初始化数据目录，
# initdb 会把 /usr/share/postgresql/16/postgresql.conf.sample 复制成
# $PGDATA/postgresql.conf。所以必须改这份「模板文件」——改完之后，
# 无论首次 initdb 的临时实例，还是之后启动的正式实例，都会带上这个配置。
#
# 踩过的坑（记录在此，避免重犯）：
#   ✗ 放 /etc/postgresql/conf.d/ 无效 —— 生成的 postgresql.conf 里没有 include 该目录，
#     因为我们并没有覆盖 config_file。这个目录只有在你自己提供 postgresql.conf
#     并 include 它时才生效，官方镜像默认不走这条路。
#   ✗ 只在 docker-compose 的 command 里加 -c shared_preload_libraries=... 也无效 ——
#     该参数在 initdb 阶段创建的临时实例上不生效，而 CREATE EXTENSION 正是在那时执行。
# ---------------------------------------------------------------------------
RUN set -eux; \
    CONF=/usr/share/postgresql/16/postgresql.conf.sample; \
    if grep -q "^shared_preload_libraries" "$CONF"; then \
        sed -i "s|^shared_preload_libraries.*|shared_preload_libraries = 'timescaledb'|" "$CONF"; \
    else \
        echo "shared_preload_libraries = 'timescaledb'" >> "$CONF"; \
    fi; \
    grep -n "shared_preload_libraries" "$CONF"
