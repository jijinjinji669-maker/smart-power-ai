# 多阶段构建：编译依赖在 builder 阶段完成，最终镜像只带运行时，体积 1.2G -> ~250M
FROM python:3.12-slim AS builder

WORKDIR /build

# 国内服务器用清华源，避免 pip 超时（在国外机器上可删掉 -i 参数）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


FROM python:3.12-slim

# tzdata 让容器内时间与宿主机一致，否则日志时间会差 8 小时
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

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
