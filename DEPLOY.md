# Couple Relay Web — 飞牛 NAS 部署指南

## 前置条件

- 飞牛 NAS 已安装 Docker（应用中心 → Docker）
- NAS 已开启 SSH（飞牛控制台 → 终端与 SNMP → 启用 SSH）
- Mac 与 NAS 在同一网络

---

## 步骤 1: 上传项目到 NAS

有三种方式，选一种最顺手的：

### 方式 A: Samba 共享（推荐，最简单）

1. Mac 打开 **Finder**
2. 按 `Cmd + K`（前往 → 连接服务器）
3. 输入 `smb://你的NAS_IP`，点连接
4. 输入 NAS 用户名密码
5. 把 `couple-relay-web.tar.gz` 拖进共享文件夹（如 `homes/你的用户名/`）

### 方式 B: SCP 命令行

打开 Mac 终端，执行（替换用户名和 IP）：

```bash
scp /Users/young/WorkBuddy/NAS/couple-relay-web.tar.gz 你的用户名@NAS_IP:~/
```

输入 NAS 密码（输入时不显示，正常现象，输完回车）

### 方式 C: 飞牛文件管理器

1. 浏览器打开飞牛管理界面
2. 文件管理 → 上传 `couple-relay-web.tar.gz` 到你的用户目录

---

## 步骤 2: SSH 登录 NAS

Mac 终端执行：

```bash
ssh 你的用户名@NAS_IP
```

输入密码登录。

---

## 步骤 3: 解压并部署

```bash
# 进入用户目录（文件上传的位置）
cd ~

# 解压
tar xzf couple-relay-web.tar.gz

# 进入项目目录
cd couple-relay-web

# 赋予执行权限
chmod +x deploy.sh

# 执行部署脚本
bash deploy.sh
```

脚本会自动完成：
1. ✅ 检查 Docker 和 Compose
2. ✅ 检查端口 8080 是否可用
3. ✅ 检查项目文件完整性
4. ✅ 构建镜像（首次 3-5 分钟）
5. ✅ 启动容器
6. ✅ 显示访问地址

**如果 8080 端口被占用**，用其他端口：

```bash
bash deploy.sh 9080
```

然后访问 `http://NAS_IP:9080`

---

## 步骤 4: 访问管理后台

浏览器打开：

```
http://你的NAS_IP:8080
```

默认密码：`admin`

---

## 首次使用流程

1. 登录管理后台
2. 创建配对（如「我和对象」）
3. **账号绑定**标签页：
   - 点「绑定 A 账号」→ 用微信扫码登录你的微信
   - 点「绑定 B 账号」→ 用微信扫码登录对象的微信
4. **AI 配置**标签页：选择模型 + 填 API Key + 测试连接
5. **人格设定**标签页：配置角色卡（描述/性格/第一条消息等）
6. **世界书**标签页：添加背景知识条目（可选）
7. 点「启动」→ 消息中继开始运行

---

## 常用运维命令

SSH 到 NAS，进入项目目录后执行：

| 操作 | 命令 |
|------|------|
| 查看实时日志 | `docker compose logs -f` |
| 查看容器状态 | `docker compose ps` |
| 停止服务 | `docker compose down` |
| 重启服务 | `docker compose restart` |
| 更新代码 | 重新上传 + `bash deploy.sh` |
| 进入容器 | `docker exec -it couple-relay-web bash` |
| 备份数据 | `docker cp couple-relay-web:/data ./backup-$(date +%Y%m%d)` |
| 恢复数据 | `docker cp ./backup-xxx/. couple-relay-web:/data/` |

---

## 常见问题

### Q: 端口 8080 被占用？

```bash
bash deploy.sh 9080  # 改用 9080 端口
```

然后访问 `http://NAS_IP:9080`

### Q: Docker Compose 不存在？

新版 Docker 已内置 Compose 插件，命令是 `docker compose`（空格）不是 `docker-compose`（连字符）。部署脚本会自动检测。

如果确实没有：

```bash
sudo apt-get install docker-compose-plugin
```

### Q: 容器启动后无法访问？

1. 检查容器是否在运行：
   ```bash
   docker compose ps
   ```
2. 查看日志：
   ```bash
   docker compose logs --tail 50
   ```
3. 检查防火墙是否放行了 8080 端口

### Q: 如何修改管理后台密码？

编辑 `docker-compose.yml`，修改 `ADMIN_PASSWORD`：

```yaml
environment:
  - ADMIN_PASSWORD=你的新密码
```

然后：

```bash
docker compose up -d
```

### Q: 如何更新到新版本？

1. 在 Mac 上重新获取最新的 `couple-relay-web.tar.gz`
2. 上传到 NAS
3. 解压覆盖旧文件
4. 执行 `bash deploy.sh`

数据不会丢失（存储在 Docker 数据卷中）。

### Q: 如何完全卸载？

```bash
docker compose down -v  # -v 同时删除数据卷
rm -rf ~/couple-relay-web
```

⚠️ 这会删除所有配对数据和聊天记录！

### Q: 多套同时运行？

创建多个配对即可，不需要部署多份。每个配对独立绑定微信账号、独立 AI 配置、独立人格设定，互不干扰。

---

## 安全建议

1. **修改默认密码** — 首次登录后立即改掉 `admin`
2. **不要暴露到公网** — 如果必须外网访问，用 Tailscale/ZeroTier 等内网穿透，不要直接端口转发
3. **定期备份** — `docker cp couple-relay-web:/data ./backup`
4. **API Key 安全** — AI 模型的 API Key 存储在 NAS 本地，不上传任何外部服务器

---

## 技术支持

遇到问题，把以下信息发给我：

1. `docker compose logs --tail 50` 的输出
2. `docker compose ps` 的输出
3. 你执行的命令和报错截图

我帮你诊断。
