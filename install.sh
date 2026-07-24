#!/usr/bin/env bash
set -euo pipefail

# ============================================================
#  Couple Relay - 双账号微信消息同步 + AI 伴聊
#  独立安装脚本
#  依赖: 已安装 fnchat 并创建了 2 个 clawbot 账号
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${SCRIPT_DIR}"
SERVICE_NAME="couple-relay"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔══════════════════════════════════════════════╗"
echo "║    Couple Relay - 双账号消息同步 + AI 伴聊    ║"
echo "║    独立安装程序                               ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ---- 1. 检查 Python ----
echo -e "${YELLOW}[1/6] 检查 Python3...${NC}"
if command -v python3 &>/dev/null; then
    PYVER=$(python3 --version 2>&1)
    echo -e "  ${GREEN}✓ ${PYVER}${NC}"
else
    echo -e "  ${RED}✗ 未找到 python3，请先安装${NC}"
    exit 1
fi

# ---- 2. 定位 fnchat ----
echo -e "${YELLOW}[2/6] 定位 fnchat...${NC}"

FNCHAT_DIR=""
DEFAULT_PATHS=(
    "/vol2/@appshare/fnchat"
    "/vol1/@appshare/fnchat"
    "/opt/fnchat"
    "$HOME/fnchat"
)

for p in "${DEFAULT_PATHS[@]}"; do
    if [ -d "$p/userdata" ]; then
        FNCHAT_DIR="$p"
        break
    fi
done

if [ -z "$FNCHAT_DIR" ]; then
    read -rp "  请输入 fnchat 数据目录路径 (如 /vol2/@appshare/fnchat): " FNCHAT_DIR
fi

if [ ! -d "$FNCHAT_DIR/userdata" ]; then
    echo -e "  ${RED}✗ 目录 ${FNCHAT_DIR}/userdata 不存在${NC}"
    echo -e "  ${YELLOW}请确认 fnchat 已安装且创建过 clawbot 账号${NC}"
    exit 1
fi

echo -e "  ${GREEN}✓ fnchat 数据目录: ${FNCHAT_DIR}${NC}"

# ---- 3. 定位 SDK ----
echo -e "${YELLOW}[3/6] 定位 weixin-channel-sdk...${NC}"

SDK_DIR=""
SDK_PATHS=(
    "/vol2/@appcenter/fnchat/server/weixin-channel-sdk/src"
    "/vol1/@appcenter/fnchat/server/weixin-channel-sdk/src"
    "/opt/fnchat/server/weixin-channel-sdk/src"
)

for p in "${SDK_PATHS[@]}"; do
    if [ -d "$p/weixin_channel" ]; then
        SDK_DIR="$p"
        break
    fi
done

if [ -z "$SDK_DIR" ]; then
    read -rp "  请输入 weixin-channel-sdk/src 路径 (如 /vol2/@appcenter/fnchat/server/weixin-channel-sdk/src): " SDK_DIR
fi

if [ ! -d "$SDK_DIR/weixin_channel" ]; then
    echo -e "  ${RED}✗ ${SDK_DIR}/weixin_channel 不存在${NC}"
    exit 1
fi

echo -e "  ${GREEN}✓ SDK 路径: ${SDK_DIR}${NC}"

# ---- 4. 发现账号 ----
echo -e "${YELLOW}[4/6] 发现 clawbot 账号...${NC}"

USERDATA_DIR="${FNCHAT_DIR}/userdata"
ACCOUNT_DIRS=()

# 扫描 userdata 下的子目录
for d in "$USERDATA_DIR"/*/; do
    name=$(basename "$d")
    if [ -f "${d}wechat/accounts/"*_im.bot.json ] 2>/dev/null || \
       [ -f "${d}accounts.json" ] 2>/dev/null; then
        ACCOUNT_DIRS+=("$name")
    fi
done

if [ ${#ACCOUNT_DIRS[@]} -lt 2 ]; then
    echo -e "  ${RED}✗ 只找到 ${#ACCOUNT_DIRS[@]} 个账号目录，至少需要 2 个${NC}"
    echo -e "  ${YELLOW}请在 fnchat 中创建 2 个 clawbot 账号后再运行本安装${NC}"
    exit 1
fi

echo -e "  找到 ${#ACCOUNT_DIRS[@]} 个账号目录:"
for i in "${!ACCOUNT_DIRS[@]}"; do
    echo -e "  ${CYAN}$((i+1))${NC}. ${ACCOUNT_DIRS[$i]}"
done

# 选择哪个是"我"，哪个是"对象"
ME_IDX=""
PARTNER_IDX=""

echo ""
echo -e "${YELLOW}请选择哪个是「你」(你的微信跟这个 clawbot 聊):${NC}"
select_opt() {
    local prompt="$1"
    shift
    local arr=("$@")
    local n=${#arr[@]}
    while true;
    do
        read -rp "$prompt " choice
        if [[ "$choice" =~ ^[1-$n]$ ]]; then
            echo $((choice - 1))
            return
        fi
        echo "请输入 1-$n"
    done
}

ME_IDX=$(select_opt "输入编号: " "${ACCOUNT_DIRS[@]}")

echo -e "${YELLOW}请选择哪个是「对象」(对象的微信跟这个 clawbot 聊):${NC}"
PARTNER_IDX=$(select_opt "输入编号: " "${ACCOUNT_DIRS[@]}")

ME_DIR_NAME="${ACCOUNT_DIRS[$ME_IDX]}"
PARTNER_DIR_NAME="${ACCOUNT_DIRS[$PARTNER_IDX]}"

echo -e "  ${GREEN}✓ 你 = ${ME_DIR_NAME}${NC}"
echo -e "  ${GREEN}✓ 对象 = ${PARTNER_DIR_NAME}${NC}"

# ---- 5. 生成配置 ----
echo -e "${YELLOW}[5/6] 生成配置文件...${NC}"

RELAY_NAME="couple-relay"
INSTANCE_DIR="${FNCHAT_DIR}/${RELAY_NAME}"

mkdir -p "$INSTANCE_DIR"
mkdir -p /tmp/${RELAY_NAME}

# 生成 config.json
CONFIG_FILE="${INSTANCE_DIR}/config.json"
cat > "$CONFIG_FILE" <<EOF
{
  "fnchat_dir": "${FNCHAT_DIR}",
  "sdk_src": "${SDK_DIR}",
  "me": {
    "label": "你",
    "data_dir": "${USERDATA_DIR}/${ME_DIR_NAME}",
    "wechat_user_id": ""
  },
  "partner": {
    "label": "对象",
    "data_dir": "${USERDATA_DIR}/${PARTNER_DIR_NAME}",
    "wechat_user_id": ""
  },
  "data_dir": "${INSTANCE_DIR}"
}
EOF

echo -e "  ${GREEN}✓ 配置文件: ${CONFIG_FILE}${NC}"

# 生成 persona.json
PERSONA_FILE="${INSTANCE_DIR}/persona.json"
cat > "$PERSONA_FILE" <<'PERSONA_EOF'
{
  "ai_enabled": true,
  "persona": "你是对方的男朋友，发自内心喜欢她，习惯迁就包容她的小情绪。她嘴硬傲娇爱说反话，闹脾气时她不争辩。她习惯偏爱短句，喜欢打一连串哈哈（至少8个哈）。她的口头禅：老婆、笨蛋、好不好。被怼了用哈哈哈接住再反击。允许口语碎片，啊嗯噗嘿嘿嘿都可以是完整消息。认错要快，撒娇邀功。",
  "delay_seconds": 4,
  "model": "deepseek-chat",
  "api_key": "",
  "base_url": "https://api.deepseek.com",
  "max_tokens": 300,
  "temperature": 0.8
}
PERSONA_EOF

echo -e "  ${GREEN}✓ 人格配置: ${PERSONA_FILE}${NC}"
echo -e "  ${YELLOW}  ⚠ 请编辑 persona.json 填入 DeepSeek api_key${NC}"

# ---- 6. 安装 systemd 服务 ----
echo -e "${YELLOW}[6/6] 安装 systemd 服务...${NC}"

# 确定运行用户
RUN_USER="$(whoami)"
if [ "$RUN_USER" = "root" ]; then
    # 尝试找 fnchat 的用户
    RUN_USER=$(stat -c '%U' "$FNCHAT_DIR" 2>/dev/null || echo "root")
fi

SCRIPT_PATH="${INSTALL_DIR}/couple_relay.py"

SERVICE_FILE="/etc/systemd/system/${RELAY_NAME}.service"

if [ -f "$SERVICE_FILE" ]; then
    echo -e "  ${YELLOW}服务文件已存在，覆盖中...${NC}"
fi

sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Couple Relay - WeChat Message Sync + AI
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${PYTHON_PATH:-python3} ${SCRIPT_PATH} --config ${CONFIG_FILE}
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${RELAY_NAME}.service"

echo -e "  ${GREEN}✓ 服务已安装: ${RELAY_NAME}.service${NC}"

# ---- 完成 ----
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════╗"
echo -e "║           ✅ 安装完成！                       ║"
echo -e "╚══════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}下一步:${NC}"
echo ""
echo -e "  1. 编辑人格配置，填入 DeepSeek API Key:"
echo -e "     ${YELLOW}nano ${PERSONA_FILE}${NC}"
echo -e "     将 \"api_key\": \"\" 改为你的 DeepSeek API Key"
echo ""
echo -e "  2. 在两个微信里分别给各自的 clawbot 发一条消息"
echo -e "     (建立 context_token，否则无法发送)"
echo ""
echo -e "  3. 启动服务:"
echo -e "     ${YELLOW}sudo systemctl start ${RELAY_NAME}${NC}"
echo ""
echo -e "  4. 查看日志:"
echo -e "     ${YELLOW}tail -f /tmp/${RELAY_NAME}/relay.log${NC}"
echo ""
echo -e "  5. 停止/重启:"
echo -e "     ${YELLOW}sudo systemctl stop ${RELAY_NAME}${NC}"
echo -e "     ${YELLOW}sudo systemctl restart ${RELAY_NAME}${NC}"
echo ""
echo -e "  6. 修改人格/模型后无需重启，脚本会自动热加载"
echo ""
