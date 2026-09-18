#!/usr/bin/env bash
# 一键部署：在服务器上用 ./deploy.sh 执行
set -euo pipefail

cd "$(dirname "$0")"

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; NC=$'\033[0m'
info() { echo "${GREEN}[deploy]${NC} $*"; }
warn() { echo "${YELLOW}[deploy]${NC} $*"; }
die()  { echo "${RED}[deploy]${NC} $*" >&2; exit 1; }

# ---------- 前置检查 ----------
command -v docker >/dev/null || die "未安装 docker"
docker compose version >/dev/null 2>&1 || die "未安装 docker compose (v2)"

[[ -f .env ]] || die ".env 不存在。先执行： cp .env.example .env  然后填写密码"

# 默认密码必须改掉，否则等于把数据库敞开
if grep -qE '^DB_PASSWORD=(change_me|power|postgres)$' .env; then
    die ".env 里的 DB_PASSWORD 还是默认值，请改成强密码"
fi

# ---------- 磁盘检查 ----------
AVAIL_GB=$(df -BG --output=avail / | tail -1 | tr -dc '0-9')
if (( AVAIL_GB < 5 )); then
    warn "根分区剩余 ${AVAIL_GB}G，建议先清理。容器日志可能写满磁盘"
fi

# ---------- 构建与启动 ----------
info "构建镜像（首次约 3-8 分钟，取决于网络）"
docker compose build

info "启动全部服务"
docker compose up -d

info "等待数据库就绪"
for i in $(seq 1 60); do
    if docker compose exec -T db pg_isready -U "$(grep -E '^DB_USER=' .env | cut -d= -f2)" >/dev/null 2>&1; then
        info "数据库已就绪（等待 ${i}s）"
        break
    fi
    (( i == 60 )) && die "数据库 60s 未就绪，执行 docker compose logs db 查看"
    sleep 1
done

info "等待 API 健康检查"
for i in $(seq 1 40); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        info "API 健康（等待 ${i}s）"
        break
    fi
    (( i == 40 )) && warn "API 未在 40s 内健康，稍后手动确认： curl http://127.0.0.1:8000/health"
    sleep 1
done

# ---------- 结果输出 ----------
echo
docker compose ps
echo
info "部署完成。常用命令："
cat <<'EOF'
  docker compose logs -f consumer      # 看采集与检测日志（最常用）
  docker compose logs -f api           # 看接口日志
  docker compose ps                    # 看各服务状态
  docker compose restart api           # 重启单个服务
  docker compose down                  # 停止（保留数据卷）
  docker compose down -v               # 停止并删除数据（慎用）
  curl http://127.0.0.1:8000/health    # 本机自测
  curl http://127.0.0.1:8000/api/stats/summary
EOF
echo
info "EMQX 控制台（需先在安全组放行 18083）： http://<服务器IP>:18083"
warn "生产环境请把 18083 用 Nginx + 基础认证保护起来，或直接不对公网开放"
