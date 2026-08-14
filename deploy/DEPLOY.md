# Blue Lake Agent Ubuntu VPS 部署

本文从一台全新的 Ubuntu VPS 开始，部署当前仓库的实际结构：React/Vite SPA、单进程 FastAPI/Uvicorn、SQLite、Nginx、systemd、Python virtualenv。全程不使用 Docker 或 Docker Compose。

本文以 Ubuntu 24.04 LTS 为基线。Vite 8 需要 Node.js 20.19+ 或 22.12+；本流程安装 Node.js 22 LTS。requirements.txt 固定 WeasyPrint 69.0。PDF 的 Python 包和 Ubuntu 原生文字渲染库是两层依赖，必须分别安装。

## 1. 部署前提与架构

先准备：

- 一个可 SSH 登录且有 sudo 权限的 Ubuntu 用户；
- 一个指向 VPS 的 DNS A 记录，若存在 AAAA 记录也必须指向同一台 VPS；
- Git 仓库地址；私有仓库的 SSH key / deploy token 由你按自己的 Git 服务配置；
- 一个用于 Let's Encrypt 的邮箱；
- 建议至少 2 GB RAM，Node build 和 PDF 渲染都可能产生瞬时内存峰值。

固定目录如下。代码仓库可更新，运行数据不放进 Git：

~~~text
/opt/bluelake-agent/                       Git checkout、venv、web/dist、skills、字体
/var/lib/bluelake-agent/data/agent.db      SQLite 主库及 WAL 文件
/var/lib/bluelake-agent/workspaces/<id>/   workspace 文件和 .agent-exports 导出文件
/var/cache/bluelake-agent/                 Fontconfig / 运行时缓存
/etc/bluelake-agent.env                    secret 和生产环境变量，非 Git 文件
127.0.0.1:8000                             Uvicorn，仅本机监听
80/443                                      Nginx 公共入口；443 终止 TLS
~~~

请求链路是：浏览器 → Nginx → 127.0.0.1:8000 → FastAPI /api → Agent Core。POST /api/chat 使用 SSE，Nginx 对 /api/ 关闭 buffering；GET /api/files/{file_id} 只按 workspace 和 opaque file_id 查找，不把服务器路径返回给浏览器。

当前代码把可信 Skill 目录和可写 workspace 根目录分开：生产使用 AGENT_SKILLS_ROOT=/opt/bluelake-agent/skills，使用 AGENT_WORKSPACE_ROOT=/var/lib/bluelake-agent/workspaces。这样 systemd 不需要给服务写入整个 Git checkout。

## 2. 设置部署变量

以下命令由登录 VPS 的普通部署用户执行；不要切换成 root shell。只替换域名、仓库地址和邮箱：

~~~bash
export DOMAIN="YOUR_DOMAIN"
export REPO_URL="YOUR_REPO_URL"
export LE_EMAIL="YOUR_EMAIL"

export DEPLOY_USER="$(id -un)"
export DEPLOY_GROUP="$(id -gn)"
export APP_DIR="/opt/bluelake-agent"
export SERVICE_USER="bluelake-agent"
export STATE_DIR="/var/lib/bluelake-agent"
export ENV_FILE="/etc/bluelake-agent.env"
export NGINX_SITE="/etc/nginx/sites-available/bluelake-agent.conf"
~~~

如果当前 shell 断开，重新连接后重新执行这一段。DOMAIN 必须是单个域名，不能包含 /。

## 3. Ubuntu 基础软件和 WeasyPrint 系统依赖

~~~bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
  ca-certificates curl gnupg git build-essential \
  python3 python3-venv python3-pip \
  nginx certbot \
  libpango-1.0-0 libpangoft2-1.0-0 \
  libharfbuzz0b libharfbuzz-subset0 \
  fontconfig shared-mime-info pango1.0-tools \
  poppler-utils
~~~

这里的 WeasyPrint runtime 包对应当前 69.0 wheel 路径：libpango-1.0-0、libpangoft2-1.0-0、libharfbuzz0b、libharfbuzz-subset0。fontconfig 用于字体发现/缓存；poppler-utils 只用于本部署文档后面的 pdftotext、pdffonts、pdfinfo smoke test，不是应用运行时必须包。

确认 Python 版本满足项目声明：

~~~bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version)'
~~~

## 4. 安装 Node.js 22

Ubuntu 自带的 nodejs 版本可能低于 Vite 8 要求，因此使用 NodeSource 的 22.x 源：

~~~bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
npm --version
~~~

node --version 必须是 v22.12.0 或更高的 22.x 版本；如果不是，先解决 Node 版本，不要继续 npm ci。

## 5. 创建服务用户、目录并 Clone 仓库

~~~bash
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  sudo useradd --system --user-group \
    --home-dir "$STATE_DIR" --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0755 "$APP_DIR"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$STATE_DIR" "$STATE_DIR/data" "$STATE_DIR/workspaces"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  /var/cache/bluelake-agent

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
else
  echo "$APP_DIR 已存在 Git checkout；跳过 clone。"
fi

cd "$APP_DIR"
git status --short --branch
test -f server/main.py
test -f web/package.json
test -f requirements.txt
~~~

服务用户只需要读取 checkout；SQLite、workspace、导出和缓存写入 /var/lib 或 /var/cache。如果你的登录用户使用了非常规 umask，确保服务用户仍能读取代码、skills/ 和字体：

~~~bash
sudo chmod -R a+rX "$APP_DIR"
~~~

## 6. 创建 Python venv 和安装 Python requirements

~~~bash
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

.venv/bin/python -c '
import aiosqlite, cryptography, docx, fastapi, markdown_it, uvicorn, weasyprint
print("Python dependencies OK")
print("WeasyPrint", weasyprint.__version__)
'
~~~

如果最后的 weasyprint import 报 native library 错误，检查第 3 步的 apt 包，不要改成只安装 Python 包。可进一步检查：

~~~bash
pango-view --version || true
fc-match "sans-serif"
.venv/bin/python -m weasyprint --info
~~~

## 7. 安装前端依赖并构建 Vite production bundle

web/vite.config.ts 没有自定义 build.outDir，因此实际产物目录是 web/dist；web/src/api/client.ts 在没有 VITE_API_BASE_URL 时使用同源相对路径 /api，生产不要设置跨域 API 地址。

~~~bash
cd "$APP_DIR/web"
npm ci
npm run build
test -f dist/index.html
cd "$APP_DIR"
~~~

## 8. 数据、workspace 和权限配置

SQLite 路径和 workspace 根目录由环境文件覆盖，避免把可写数据放进 checkout：

~~~bash
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$STATE_DIR/data" "$STATE_DIR/workspaces"
sudo -u "$SERVICE_USER" test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/lxgwwenkailite-regular.css"
sudo find "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/files" -type f -name '*.woff2' | head
~~~

应用会按 workspace 创建 <workspace_id>/.agent-exports/，导出文件和 manifest 均由 bluelake-agent 写入；下载接口只接受 32 位十六进制 opaque ID，并重新按当前 workspace 解析。

## 9. 生成固定 secret 并安装环境文件

AGENT_SECRET_KEY 用于 Fernet 加密 SQLite 中的用户 API key。它必须固定保存；丢失或更换会使旧的加密配置不可解密。下面命令生成一次 key，把 key 只写入临时文件，再以 root:bluelake-agent、0640 安装到 /etc，不会进入 systemd unit 或 Git：

~~~bash
umask 077
tmp_env="$(mktemp)"
trap 'rm -f "$tmp_env"' EXIT

cat >"$tmp_env" <<EOF
AGENT_SECRET_KEY=$("$APP_DIR/.venv/bin/python" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
AGENT_CONFIG=$APP_DIR/config.yaml
AGENT_REQUIRE_USER_CONFIG=true
AGENT_COOKIE_SECURE=true
AGENT_HOST=127.0.0.1
AGENT_PORT=8000
AGENT_SQLITE_PATH=$STATE_DIR/data/agent.db
AGENT_WORKSPACE_ROOT=$STATE_DIR/workspaces
AGENT_SKILLS_ROOT=$APP_DIR/skills
AGENT_STATIC_DIR=$APP_DIR/web/dist
AGENT_CORS_ORIGINS=https://$DOMAIN
EOF

sudo install -o root -g "$SERVICE_USER" -m 0640 "$tmp_env" "$ENV_FILE"
rm -f "$tmp_env"
trap - EXIT

sudo stat -c '%A %U %G %n' "$ENV_FILE"
sudo grep -q '^AGENT_SECRET_KEY=' "$ENV_FILE"
sudo grep -q '^AGENT_WORKSPACE_ROOT=' "$ENV_FILE"
~~~

预期权限至少应显示 -rw-r----- root bluelake-agent。日后修改非 secret 环境变量也必须保留这个权限；不要把 /etc/bluelake-agent.env 复制进仓库。

## 10. 安装并启动 systemd

模板使用固定的 /opt/bluelake-agent 和 bluelake-agent，与前面的目录步骤一致；模板没有 secret，只通过 EnvironmentFile 加载：

~~~bash
sudo install -o root -g root -m 0644 \
  "$APP_DIR/deploy/myapp.service" /etc/systemd/system/myapp.service
sudo systemd-analyze verify /etc/systemd/system/myapp.service
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
sudo systemctl status myapp --no-pager
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
echo
~~~

ExecStartPre 会同时检查 web/dist/index.html 和 WeasyPrint 是否能 import；服务只监听 127.0.0.1:8000，不要在 UFW 或云安全组开放 8000。

## 11. DNS 前置要求

在签发证书前，把：

~~~text
YOUR_DOMAIN A     VPS_PUBLIC_IPV4
YOUR_DOMAIN AAAA  VPS_PUBLIC_IPV6   # 只有 VPS 确实提供 IPv6 时才配置
~~~

配置完成后，从 VPS 检查解析：

~~~bash
getent ahosts "$DOMAIN"
~~~

如果 DNS 仍指向旧机器、AAAA 指向错误机器，HTTP-01 challenge 会失败。

## 12. Nginx HTTP bootstrap

先使用只监听 80 的 bootstrap 配置，让 certbot 能通过 webroot 验证；证书签发后再替换为 HTTPS 配置。不要在证书不存在时直接安装 myapp.nginx.conf。

~~~bash
sudo install -d -o root -g root -m 0755 /var/www/letsencrypt
sudo sed "s|YOUR_DOMAIN|$DOMAIN|g" \
  "$APP_DIR/deploy/myapp.nginx.http.conf" \
  | sudo tee "$NGINX_SITE" >/dev/null

sudo ln -sfn "$NGINX_SITE" /etc/nginx/sites-enabled/bluelake-agent.conf
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

curl --fail --silent --show-error "http://$DOMAIN/api/health"
echo
~~~

bootstrap 已包含 /api/ 的 SSE 友好代理参数和 SPA history fallback，但暂时没有 HTTPS；这一步只用于 ACME 和 HTTP 基础验证。

## 13. Certbot / Let's Encrypt HTTPS

~~~bash
sudo certbot certonly --webroot \
  --webroot-path /var/www/letsencrypt \
  --domain "$DOMAIN" \
  --email "$LE_EMAIL" \
  --agree-tos --no-eff-email --non-interactive
~~~

证书成功后安装最终配置。sed 只替换域名；证书路径使用 Certbot 的标准 /etc/letsencrypt/live/$DOMAIN/ 路径：

~~~bash
sudo sed "s|YOUR_DOMAIN|$DOMAIN|g" \
  "$APP_DIR/deploy/myapp.nginx.conf" \
  | sudo tee "$NGINX_SITE" >/dev/null
sudo nginx -t
sudo systemctl reload nginx

# Certbot renewal updates the files under /etc/letsencrypt/live, but Nginx
# must reload before it starts serving the renewed certificate.
sudo install -d -o root -g root -m 0755 /etc/letsencrypt/renewal-hooks/deploy
sudo tee /etc/letsencrypt/renewal-hooks/deploy/reload-nginx >/dev/null <<'EOF'
#!/bin/sh
systemctl reload nginx
EOF
sudo chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx

sudo certbot certificates
sudo certbot renew --dry-run
systemctl list-timers certbot.timer --no-pager
~~~

myapp.nginx.conf 的 /api/ 使用 proxy_pass http://127.0.0.1:8000;（没有尾部 /），所以 /api/chat、/api/files/{file_id} 等路径不会被错误剥掉 /api 前缀。SSE 关闭了 proxy_buffering，下载仍由 FastAPI 返回，不暴露内部路径。

## 14. UFW / 防火墙

先确认当前 SSH 端口已经允许，再启用 UFW：

~~~bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
~~~

云厂商安全组也只放行 SSH、80、443；8000 保持关闭。

## 15. 首次上线验证

~~~bash
curl --fail --silent --show-error --head "https://$DOMAIN/"
curl --fail --silent --show-error "https://$DOMAIN/api/health"
curl --fail --silent --show-error --head "https://$DOMAIN/assets/does-not-exist.js" || true
~~~

浏览器打开 https://$DOMAIN/，确认：

1. SPA 能加载，刷新一个前端路由不会 404；
2. /api/bootstrap 返回并设置 HttpOnly; Secure; SameSite=Lax 的 workspace_id Cookie；
3. Settings 可以保存 main provider 配置；
4. 新建会话后发送消息，Network 中 POST /api/chat 的响应类型为 text/event-stream，能持续收到 text_delta 并以 done 结束；
5. Agent 通过 export_file 生成 md、txt、docx、pdf，四种文件都能从浏览器下载。

## 16. 中文 PDF 真实 smoke test

不要只在 Windows 开发机检查。脚本会在生产 venv 中生成包含中文标题/正文、粗体、斜体、有序/无序列表、代码块、表格和链接的 PDF，然后通过当前 HTTPS 域名调用真实 GET /api/files/{file_id} 下载，再用 Poppler 检查 PDF 文本、页数和嵌入字体：

~~~bash
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" \
  "$APP_DIR/deploy/pdf_smoke_test.py" \
  --base-url "https://$DOMAIN" \
  --workspace-root "$STATE_DIR/workspaces"
~~~

成功条件：命令输出 PDF smoke test passed，且同时满足：

- PDF 生成成功，下载 HTTP status 为 200，文件以 %PDF 开头且大小大于 0；
- pdftotext 能读到中文标题、正文、列表、代码、表格标记和链接文字；
- pdffonts 至少发现一个嵌入字体；
- pdfinfo 显示至少 1 页；
- 浏览器实际打开下载文件后，中文、粗斜体、表格和代码块可读。

脚本会删除本次 smoke test 产生的临时导出文件；真实用户导出文件不会被删除。

## 17. 日常代码更新

在部署用户下执行。不要删除 /var/lib/bluelake-agent，也不要重新生成 secret：

~~~bash
cd "$APP_DIR"
git pull --ff-only

# requirements.txt 有变化时必须执行；无变化时重复执行也安全。
.venv/bin/python -m pip install -r requirements.txt

cd "$APP_DIR/web"
npm ci
npm run build
test -f dist/index.html
cd "$APP_DIR"

# 只有 deploy/myapp.service 改动时才需要重新安装并 daemon-reload。
sudo install -o root -g root -m 0644 \
  "$APP_DIR/deploy/myapp.service" /etc/systemd/system/myapp.service
sudo systemd-analyze verify /etc/systemd/system/myapp.service
sudo systemctl daemon-reload

# 如果 deploy/myapp.nginx.conf 改动，重新渲染域名并检查后 reload。
sudo sed "s|YOUR_DOMAIN|$DOMAIN|g" \
  "$APP_DIR/deploy/myapp.nginx.conf" \
  | sudo tee "$NGINX_SITE" >/dev/null
sudo nginx -t
sudo systemctl reload nginx

sudo systemctl restart myapp
sudo systemctl status myapp --no-pager
curl --fail --silent --show-error "https://$DOMAIN/api/health"
echo
~~~

若本次更新只涉及后端代码，仍建议先 build 再 restart；若 Git 更新包含 requirements.txt、package-lock.json、systemd 或 Nginx 文件，按上面完整流程执行。SQLite 数据和 /var/lib workspace 不受 git pull 影响。

## 18. 服务重启、日志和磁盘检查

~~~bash
sudo systemctl status myapp --no-pager
sudo systemctl restart myapp
sudo systemctl stop myapp
sudo systemctl start myapp

sudo journalctl -u myapp -n 200 --no-pager
sudo journalctl -u myapp -f

sudo nginx -t
sudo systemctl reload nginx
sudo journalctl -u nginx -n 100 --no-pager
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log

df -h
sudo du -sh "$STATE_DIR" "$APP_DIR" /var/log/nginx
sudo find "$STATE_DIR/workspaces" -maxdepth 3 -type f -printf '%s %p\n' | sort -nr | head -30
~~~

## 19. SQLite 和重要数据备份

用 SQLite backup API 复制在线数据库，覆盖 WAL 状态；不要只复制一个可能仍在写入的 .db 文件：

~~~bash
sudo install -d -o root -g root -m 0700 /var/backups/bluelake-agent
export DB_BACKUP="/var/backups/bluelake-agent/agent-$(date +%F-%H%M%S).db"
sudo test -f "$STATE_DIR/data/agent.db"

sudo "$APP_DIR/.venv/bin/python" - "$STATE_DIR/data/agent.db" "$DB_BACKUP" <<'PY'
import sqlite3
import sys

source_path, backup_path = sys.argv[1:]
source = sqlite3.connect(source_path)
target = sqlite3.connect(backup_path)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
PY

sudo chmod 0600 "$DB_BACKUP"
sudo tar -czf \
  "/var/backups/bluelake-agent/workspaces-$(date +%F-%H%M%S).tar.gz" \
  -C "$STATE_DIR" workspaces
sudo ls -lh /var/backups/bluelake-agent
~~~

至少备份两类数据：

- agent-*.db：会话、消息、workspace 记录和加密用户配置；
- workspaces-*.tar.gz：用户文件、Skill 训练状态和导出文件。

AGENT_SECRET_KEY 必须单独以受控方式保存；数据库备份没有它，里面的加密 API key 无法恢复。不要把 /etc/bluelake-agent.env 放进公开备份或 Git。

## 20. 常见故障排查

| 症状 | 先检查 | 处理 |
| --- | --- | --- |
| myapp 启动失败 | systemctl status myapp、journalctl -u myapp -n 200 | 先看 EnvironmentFile、venv、web/dist/index.html 和 weasyprint import；修复后 systemctl restart myapp。 |
| WeasyPrint 报 libgobject / Pango / HarfBuzz | .venv/bin/python -c 'import weasyprint'、pango-view --version、ldconfig -p \| grep -E 'pango|harfbuzz' | 重新安装第 3 步的 apt runtime 包；不要只重装 pip 包。 |
| 首页 502 | curl http://127.0.0.1:8000/api/health、systemctl status myapp | 后端未运行、端口不一致或 service 用户不能读取代码/venv。 |
| 首页 404 或刷新路由 404 | test -f "$APP_DIR/web/dist/index.html"、nginx -t | 检查 Nginx root 是否为 /opt/bluelake-agent/web/dist，并确认使用了 try_files ... /index.html。 |
| /api 返回 SPA HTML | nginx -T | /api/ 必须是 ^~ proxy location；proxy_pass 不能带尾部 /。 |
| SSE 一次性返回或超时 | curl -N、nginx -T、journalctl -u myapp -f | 确认 /api/ 的 proxy_buffering off、长 proxy_read_timeout，以及 FastAPI 返回 X-Accel-Buffering: no。 |
| PDF/DOCX 生成 storage_failed | namei -l "$STATE_DIR/workspaces"、journalctl -u myapp | workspace 根必须由 bluelake-agent 可写；不要把它误设为只读 checkout。 |
| PDF 中文是方框或没有字体 | ls "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/files"、pdffonts | 确认字体文件随 Git checkout 存在且服务用户可读，再重新执行 PDF smoke test。 |
| 下载接口 404 | 检查浏览器的 workspace_id Cookie、.agent-exports manifest 和 AGENT_WORKSPACE_ROOT | file ID 只在生成它的 workspace 中有效；不要直接把文件路径拼到 URL。 |
| 已保存 API key 解密失败 | stat "$ENV_FILE"、确认 AGENT_SECRET_KEY 未改变 | 恢复首次部署保存的固定 key；不要重新生成 key 作为“修复”。 |
| Certbot challenge 失败 | getent ahosts "$DOMAIN"、UFW、安全组、curl http://$DOMAIN/.well-known/... | 确认 A/AAAA、80 端口和 bootstrap 配置都指向本机；证书成功后再安装 HTTPS 模板。 |
| 磁盘空间不足 / SQLite locked | df -h、sudo du -sh "$STATE_DIR"、日志 | 先保留备份，再清理旧导出/日志；不要删除当前数据库或整个 workspace 根。 |

## 21. 安全边界和上线前复核

- secret 只在 /etc/bluelake-agent.env，权限为 0640、owner 为 root、group 为服务用户；systemd unit 不含 key。
- 服务用户不是 root；systemd 让 checkout 只读，只向 /var/lib/bluelake-agent 和缓存目录写入。
- Uvicorn 只监听 loopback，公网只暴露 Nginx 的 80/443。
- PDF renderer 禁用 Markdown 原始 HTML 和图片，并使用受限 URL fetcher：只允许读取 checkout 内置的 .woff2 字体，不允许 file:// 任意路径、外部 HTTP(S)、FTP 或 data 资源。
- workspace 隔离是应用层边界；workspace_id Cookie 不是完整的公网身份认证。若面向不可信用户开放，还需要真正的认证、授权、速率限制和更强的进程隔离。
- 每次更新后至少执行：systemd-analyze verify、nginx -t、loopback health check、HTTPS health check、浏览器 SSE 测试和 PDF smoke test。
