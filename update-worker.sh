#!/bin/sh
# 独立更新执行器：监控 relay 写入的更新请求，在业务容器外完成解压、构建和重启。
set -u

UPDATE_DIR="/data/update"
REQUEST_FILE="$UPDATE_DIR/update-request.json"
STATUS_FILE="$UPDATE_DIR/status.json"
APP_DIR="/host/app"
LOG_FILE="$UPDATE_DIR/update.log"
ARCHIVE="$UPDATE_DIR/upload-update.tar.gz"

mkdir -p "$UPDATE_DIR"

write_status() {
  status="$1"
  message="$2"
  extra="${3:-}"
  now="$(date '+%Y-%m-%dT%H:%M:%S%z')"
  printf '{"status":"%s","message":"%s","updated_at":"%s"%s}\n' "$status" "$message" "$now" "$extra" > "$STATUS_FILE"
}

safe_extract() {
  work="$UPDATE_DIR/extracted"
  rm -rf "$work"
  mkdir -p "$work"
  tar -xzf "$ARCHIVE" -C "$work"

  if [ -f "$work/main.py" ] && [ -d "$work/static" ]; then
    SOURCE_DIR="$work"
  else
    SOURCE_DIR="$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'couple-relay*' | head -n 1)"
    if [ -z "$SOURCE_DIR" ] || [ ! -f "$SOURCE_DIR/main.py" ] || [ ! -d "$SOURCE_DIR/static" ]; then
      return 1
    fi
  fi
}

run_update() {
  write_status "running" "正在解压更新包并构建镜像"
  printf '\n===== %s 更新开始 =====\n' "$(date '+%F %T')" >> "$LOG_FILE"

  if ! safe_extract >> "$LOG_FILE" 2>&1; then
    write_status "failed" "更新包结构无效，详情请查看更新日志"
    return
  fi

  # 只覆盖项目文件；Docker volume /data 不受影响。
  cp -a "$SOURCE_DIR"/. "$APP_DIR"/ >> "$LOG_FILE" 2>&1
  if [ $? -ne 0 ]; then
    write_status "failed" "复制更新文件失败，详情请查看更新日志"
    return
  fi

  write_status "building" "文件已覆盖，正在重建并启动新容器"
  # 读取当前 relay 的真实宿主机 bind source。容器内的 /host/app 不能直接作为
  # Docker daemon 的宿主机路径使用，必须用 inspect 得到的实际绝对路径。
  HOST_APP_SOURCE="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/host/app"}}{{.Source}}{{end}}{{end}}' couple-relay-web 2>> "$LOG_FILE")"
  if [ -z "$HOST_APP_SOURCE" ]; then
    write_status "failed" "无法读取当前服务的宿主机项目路径，详情请查看更新日志"
    return
  fi
  # Docker daemon 需要宿主机绝对路径做 bind mount；但 compose 命令运行在 updater 容器内，
  # 构建上下文必须使用容器中真实存在的 /host/app，不能传入宿主机的 /vol2/... 路径。
  if ! HOST_APP_DIR="$HOST_APP_SOURCE" APP_BUILD_CONTEXT="$APP_DIR" docker compose -p couple-relay-web -f "$APP_DIR/docker-compose.yml" config -q >> "$LOG_FILE" 2>&1; then
    write_status "failed" "Docker Compose 配置无效，详情请查看更新日志"
    rm -f "$REQUEST_FILE"
    return
  fi
  # updater 不在 relay 服务内，relay 自身被替换时本进程仍可持续运行。
  if ! HOST_APP_DIR="$HOST_APP_SOURCE" APP_BUILD_CONTEXT="$APP_DIR" docker compose -p couple-relay-web -f "$APP_DIR/docker-compose.yml" up -d --build relay >> "$LOG_FILE" 2>&1; then
    write_status "failed" "Docker 重建失败，详情请查看更新日志"
    rm -f "$REQUEST_FILE"
    return
  fi

  write_status "success" "更新完成，服务已重启"
  rm -f "$REQUEST_FILE"
  printf '===== %s 更新完成 =====\n' "$(date '+%F %T')" >> "$LOG_FILE"
}

write_status "idle" "独立更新器已就绪"
while true; do
  if [ -f "$REQUEST_FILE" ]; then
    run_update
  fi
  sleep 2
done
