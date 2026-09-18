# 多阶段构建：编译依赖在 builder 阶段完成，最终镜像只带运行时，体积 1.2G -> ~250M
FROM python:3.12-slim AS builder

WORKDIR /build

# 国内 pip 源，避免下载超时
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# tzdata 让容器内时间与宿主机一致，否则日志时间会差 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ---------------------------------------------------------------------------
# apt 源换成国内镜像。
#
# 默认的 deb.debian.org 在国内被严重限速：本项目在阿里云 ECS 上实测，
# 就为装 tzdata + curl 这两个小包，apt 一步耗时 897 秒（近 15 分钟），
# 换镜像后降到几秒。
#
# 注意 pypi.tuna 和 mirrors.tuna 都是清华域名，所以这里的换源只针对
# deb.debian.org，不会误伤上面的 pip 源。
# ---------------------------------------------------------------------------
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata curl; \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime; \
    echo $TZ > /etc/timezone; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local

# 以非 root 用户运行：容器被攻破时拿不到宿主机 root
RUN useradd -m -u 1000 appuser

COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser simulator/ ./simulator/
COPY --chown=appuser:appuser requirements.txt ./

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
