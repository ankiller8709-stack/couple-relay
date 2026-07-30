#!/bin/bash
set -e

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Couple Relay Web 部署脚本              ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

# === 1. 检查 Docker ===
echo -e "${YELLOW}[1/5] 检查 Docker...${NC}"
if ! command -v docker &> /dev/null; then
    echo -e "${RED}❌ Docker 未安装！${NC}"
    echo "   飞牛 NAS: 应用中心 → 搜索 Docker → 安装"
    exit 1
fi
echo -e "${GREEN}✅ Docker: $(docker --version)${NC}"

# 检查 Docker 权限（如果没有权限，自动用 sudo 重新执行）
if ! docker ps &> /dev/null 2>&1; then
    echo -e "${YELLOW}⚠️  当前用户无 Docker 权限，使用 sudo 重新执行...${NC}"
    exec sudo bash "$0" "$@"
fi

# === 2. 检查 Docker Compose ===
echo ""
echo -e "${YELLOW}[2/5] 检查 Docker Compose...${NC}"
COMPOSE_CMD=""
if docker compose version &> /dev/null 2>&1; then
    COMPOSE_CMD="docker compose"
    echo -e "${GREEN}✅ Docker Compose (plugin)${NC}"
elif command -v docker-compose &> /dev/null 2>&1; then
    COMPOSE_CMD="docker-compose"
    echo -e "${GREEN}✅ docker-compose (standalone)${NC}"
else
    echo -e "${YELLOW}⚠️  未检测到 Compose，尝试安装插件...${NC}"
    if command -v apt-get &> /dev/null; then
        sudo apt-get update -qq && sudo apt-get install -y -qq docker-compose-plugin 2>/dev/null
    fi
    if docker compose version &> /dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
        echo -e "${GREEN}✅ Docker Compose 安装成功${NC}"
    else
        echo -e "${RED}❌ Docker Compose 不可用${NC}"
        echo "   手动安装: sudo apt-get install docker-compose-plugin"
        exit 1
    fi
fi

# === 3. 检查端口 ===
echo ""
echo -e "${YELLOW}[3/5] 检查端口...${NC}"
PORT=8080
if [ -n "$1" ]; then
    PORT=$1
fi

if command -v ss &> /dev/null; then
    if ss -tlnp | grep ":$PORT " &> /dev/null; then
        echo -e "${YELLOW}⚠️  端口 $PORT 已被占用！${NC}"
        echo "   占用进程:"
        ss -tlnp | grep ":$PORT " 2>/dev/null || ss -tln | grep ":$PORT "
        echo ""
        echo "   解决方案:"
        echo "   1. 停止占用 $PORT 的服务"
        echo "   2. 或用其他端口: bash deploy.sh 9080"
        read -p "   是否继续使用端口 $PORT？(y/N): " CONFIRM
        if [ "$CONFIRM" != "y" ] && [ "$CONFIRM" != "Y" ]; then
            echo "部署已取消"
            exit 0
        fi
    else
        echo -e "${GREEN}✅ 端口 $PORT 可用${NC}"
    fi
else
    echo -e "${GREEN}✅ 端口 $PORT（跳过检测）${NC}"
fi

# 如果端口不是默认的 8080，修改 docker-compose.yml
if [ "$PORT" != "8080" ]; then
    echo "   修改端口映射为 $PORT ..."
    sed -i.bak "s/\"8080:8080\"/\"$PORT:8080\"/" docker-compose.yml 2>/dev/null || \
    sed -i '' "s/\"8080:8080\"/\"$PORT:8080\"/" docker-compose.yml
    echo -e "${GREEN}✅ 端口已改为 $PORT${NC}"
fi

# === 4. 检查项目文件 ===
echo ""
echo -e "${YELLOW}[4/5] 检查项目文件...${NC}"
MISSING=0
for f in main.py engine.py database.py ilink_client.py Dockerfile docker-compose.yml requirements.txt static/index.html; do
    if [ ! -f "$f" ]; then
        echo -e "${RED}❌ 缺少文件: $f${NC}"
        MISSING=1
    fi
done
if [ "$MISSING" -eq 1 ]; then
    echo "   请确认你已正确解压 couple-relay-web.tar.gz"
    exit 1
fi
echo -e "${GREEN}✅ 项目文件完整${NC}"

# === 5. 构建并启动 ===
echo ""
echo -e "${YELLOW}[5/5] 构建镜像并启动...${NC}"
echo "   （首次构建约 3-5 分钟，请耐心等待）"
echo ""

$COMPOSE_CMD up -d --build 2>&1 | tail -20

# 等待容器启动
sleep 3

# === 显示结果 ===
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
echo -e "${GREEN}  ✅ 部署完成！${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
echo ""

# 获取 NAS IP
NAS_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [ -z "$NAS_IP" ]; then
    NAS_IP=$(ip addr show 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}' | cut -d/ -f1 | head -1)
fi
if [ -z "$NAS_IP" ]; then
    NAS_IP="<你的NAS_IP>"
fi

echo -e "🌐 Web 管理后台: ${GREEN}http://$NAS_IP:$PORT${NC}"
echo -e "🔑 默认密码: ${GREEN}admin${NC}"
echo ""
echo -e "📋 常用命令:"
echo "   查看日志:   $COMPOSE_CMD logs -f"
echo "   查看状态:   $COMPOSE_CMD ps"
echo "   停止服务:   $COMPOSE_CMD down"
echo "   重启服务:   $COMPOSE_CMD restart"
echo "   更新代码:   重新上传后执行 bash deploy.sh"
echo ""

# 检查容器是否正常运行
if $COMPOSE_CMD ps 2>/dev/null | grep -q "running\|Up"; then
    echo -e "${GREEN}✅ 容器运行正常${NC}"
else
    echo -e "${YELLOW}⚠️  容器可能未正常启动，查看日志:${NC}"
    echo "   $COMPOSE_CMD logs --tail 50"
fi
echo ""
