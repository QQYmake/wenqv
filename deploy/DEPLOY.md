# 部署清单 (Ubuntu + nginx + systemd, 无 Docker)

适用：Blue Lake Agent (FastAPI + React/Vite SPA)。
所有命令在服务器上以部署用户执行（sudo 处特权步骤）。

假设部署目录 = `/opt/myapp`，域名 = `YOUR_DOMAIN`，按需替换。

---

## 0. 拉代码到服务器
```bash
sudo mkdir -p /opt/myapp
sudo chown $USER:$USER /opt/myapp
cd /opt/myapp
git clone <repo-url> .          # 或 rsync 上传
```

## 1. 装系统依赖
```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nodejs npm nginx
# 若 node/npm 版本过旧，用 NodeSource 装 LTS：
# curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs
```

## 2. 装后端 Python 依赖
```bash
cd /opt/myapp
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# 快速校验依赖可导入：
.venv/bin/python -c "import fastapi,uvicorn,openai,cryptography,yaml,aiosqlite; print('ok')"
```

## 3. 生成【固定】AGENT_SECRET_KEY（只做一次！）
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
复制输出的 44 字符串（含 `==`），填进 `deploy/myapp.service` 的
`Environment=AGENT_SECRET_KEY=...` 一行。
> 永远不要重新生成 —— 用户已保存的 API Key 都用它加密，换了就全废。

## 4. 构建前端
```bash
cd /opt/myapp/web
npm ci
npm run build          # 产物: /opt/myapp/web/dist
```
> 不要设置 `VITE_API_BASE_URL`。前端默认走同域相对路径 `/api/...`，由 nginx 代理。

## 5. 准备数据目录（给 systemd 运行用户写权限）
```bash
cd /opt/myapp
mkdir -p data
sudo chown -R www-data:www-data data
```

## 6. 安装 systemd 服务
```bash
# 先把第 3 步生成的密钥填进 deploy/myapp.service
sudo cp deploy/myapp.service /etc/systemd/system/myapp.service
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
sudo systemctl status myapp        # 应 active(running)
curl -s http://127.0.0.1:8000/api/health   # {"status":"ok"}
```

## 7. 安装 nginx 站点
```bash
# 编辑 deploy/myapp.nginx.conf: 替换 YOUR_DOMAIN 与证书路径
sudo cp deploy/myapp.nginx.conf /etc/nginx/sites-available/myapp.conf
sudo ln -sf /etc/nginx/sites-available/myapp.conf /etc/nginx/sites-enabled/myapp.conf
sudo rm -f /etc/nginx/sites-enabled/default    # 可选，避免冲突
sudo nginx -t
sudo systemctl reload nginx
```

## 8. 验证
```bash
curl -I https://YOUR_DOMAIN/                    # 200, 指向 index.html
curl -I https://YOUR_DOMAIN/api/health          # 200 (经 nginx 代理到后端)
# 浏览器打开 https://YOUR_DOMAIN/ ，应能新建会话、发消息、在设置里存 API Key
# 重启后端验证密钥固定：用户存的 Key 仍可用
sudo systemctl restart myapp
```

---

## 常见坑

- **`proxy_pass http://127.0.0.1:8000;` 末尾不能有 `/`**：后端路由自带 `/api` 前缀，
  带斜杠会被 nginx 剥成 `/`，后端 404。
- **`AGENT_COOKIE_SECURE` 保持 `true`**：生产走 HTTPS，Secure cookie 才会回传。
  `start.bat` 里的 `false` 仅用于本地 http 开发。
- **密钥必须是 Fernet 格式**（44 字符带 `==`），不能用 `secrets.token_urlsafe(32)`。
- **不要重启时换密钥**：换了 → data/agent.db 里已加密的 API Key 全部解不开。
- 端口 8000 只听 `127.0.0.1`，外网访问只能走 nginx 443。