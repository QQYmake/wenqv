# Blue Lake Agent（Agent Lake）Ubuntu VPS 部署

本文从一台全新的 Ubuntu VPS 开始，部署当前仓库的实际结构：React/Vite SPA、单进程 FastAPI/Uvicorn、浏览器本地隐私模式（Browser-local privacy）、Nginx、systemd、Python virtualenv。全程不使用 Docker 或 Docker Compose。

本文以 Ubuntu 24.04 LTS 为基线。Vite 8 需要 Node.js 20.19+ 或 22.12+；本流程安装 Node.js 22 LTS。requirements.txt 固定 WeasyPrint 69.0。PDF 的 Python 包和 Ubuntu 原生文字渲染库是两层依赖，必须分别安装。

## 0. 当前架构（先读，避免照旧文档部署）

本项目已经迁移为**浏览器本地隐私模式**：

- 聊天历史、provider API key、会话状态全部加密保存在**浏览器本地**（IndexedDB / Web Crypto），服务端不保存任何对话与凭据；
- 服务端不启动 SQLite、不生成/使用 `AGENT_SECRET_KEY`、不签发身份 cookie、不保存用户配置；
- 浏览器每次请求在 `X-Workspace-ID` 请求头中携带本地生成的匿名 workspace ID，`POST /api/chat` 请求体携带浏览器解密后的 `provider_config`；
- 服务端只负责：提供静态 SPA、代理 OpenAI-compatible LLM 流式请求（SSE）、执行 workspace 受限的文件工具、生成并托管 md/txt/docx/pdf 导出文件（`<workspace>/.agent-exports/`）；
- **重要**：旧部署文档中的 `AGENT_SECRET_KEY`、`AGENT_SQLITE_PATH`、`AGENT_COOKIE_SECURE`、`AGENT_REQUIRE_USER_CONFIG`、`AGENT_MAIN_*`、`AGENT_SUMMARY_*`、`REDIS_URL` 等环境变量现在会被 `server/config.py` 的 `_reject_legacy_persistence` **拒绝并导致启动失败**，不要设置它们；
- 应用进程内维护运行中的 chat run（`ActiveRun`），`--workers 1` 是硬性要求，多 worker 会破坏单进程运行表。

请求链路：浏览器 → Nginx（443 终止 TLS）→ 127.0.0.1:8000 → FastAPI `/api` → LLM Provider。`POST /api/chat` 使用 SSE，Nginx 对 `/api/` 关闭 buffering。

## 1. 部署前提

准备：

- 一个可 SSH 登录且有 sudo 权限的 Ubuntu 用户；
- 一个指向 VPS 的 DNS A 记录（若存在 AAAA 记录也必须指向同一台 VPS）；
- Git 仓库地址；私有仓库的 SSH key / deploy token 由你按自己的 Git 服务配置；
- 一个用于 Let's Encrypt 的邮箱；
- 建议至少 2 GB RAM，Node build 和 PDF 渲染都可能产生瞬时内存峰值。

固定目录如下。代码仓库可更新，运行数据不放进 Git：

```text
/opt/bluelake-agent/                       Git checkout、venv、web/dist、skills、字体
/var/lib/bluelake-agent/workspaces/<id>/   workspace 文件与 .agent-exports 导出文件（唯一服务端持久数据）
/var/cache/bluelake-agent/                 Fontconfig / 运行时缓存
/etc/bluelake-agent.env                    部署路径环境变量（无 secret），非 Git 文件
127.0.0.1:8000                             Uvicorn，仅本机监听
80/443                                     Nginx 公共入口；443 终止 TLS
```

## 2. 设置部署变量

以下命令由登录 VPS 的普通部署用户执行；不要切换成 root shell。只替换域名、仓库地址和邮箱：

```bash
export DOMAIN="YOUR_DOMAIN"
export REPO_URL="YOUR_REPO_URL"

export DEPLOY_USER="$(id -un)"
export DEPLOY_GROUP="$(id -gn)"
export APP_DIR="/opt/bluelake-agent"
export SERVICE_USER="bluelake-agent"
export STATE_DIR="/var/lib/bluelake-agent"
export ENV_FILE="/etc/bluelake-agent.env"
export NGINX_SITE="/etc/nginx/sites-available/bluelake-agent.conf"
```

如果当前 shell 断开，重新连接后重新执行这一段。DOMAIN 必须是单个域名，不能包含 `/`。

## 3. 全部依赖清单

### 3.1 Python 依赖（requirements.txt）

```text
fastapi>=0.115,<1.0
uvicorn[standard]>=0.30,<1.0
httpx>=0.27,<1.0
openai>=1.54,<3.0
pydantic>=2.9,<3.0
PyYAML>=6.0,<7.0
Pillow>=10.4,<13.0
pathspec>=0.12,<1.0
markdown-it-py>=3.0,<5.0
python-docx>=1.1,<2.0
weasyprint==69.0
```

Python 版本要求：**>= 3.11**（pyproject.toml `requires-python`）。

### 3.2 前端依赖（web/package.json + package-lock.json）

- React 18、react-markdown、remark-gfm、rehype-highlight、three；
- 构建工具链：TypeScript 5.9、Vite 8、Vitest 4（devDependencies，仅构建/测试时使用）；
- 使用 `npm ci`（锁定 package-lock.json），`npm run build` = `tsc -b && vite build`，产物在 `web/dist/`；
- 字体：`web/public/fonts/lxgw-wenkai-lite/`（浏览器分片字体 + PDF 专用完整字体），随 Git checkout 存在，服务用户必须可读。

### 3.3 Ubuntu 系统依赖

```bash
sudo apt update
sudo DEBIAN_FRONTEND=noninteractive apt install -y \
  ca-certificates curl gnupg git build-essential \
  python3 python3-venv python3-pip \
  nginx certbot \
  libpango-1.0-0 libpangoft2-1.0-0 \
  libharfbuzz0b libharfbuzz-subset0 \
  libffi-dev libjpeg-dev libopenjp2-7-dev \
  fontconfig shared-mime-info pango1.0-tools \
  poppler-utils
```

说明：

- WeasyPrint 69.0 wheel 运行时至少需要 `libpango-1.0-0`、`libpangoft2-1.0-0`、`libharfbuzz0b`、`libharfbuzz-subset0`；
- `libffi-dev`、`libjpeg-dev`、`libopenjp2-7-dev` 覆盖 pip 退回源码构建时的编译依赖；
- `fontconfig` 用于字体发现/缓存；`shared-mime-info`、`pango1.0-tools` 供 WeasyPrint 与排查使用；
- `poppler-utils` 只用于本文档后面的 `pdftotext`、`pdffonts`、`pdfinfo` smoke test，不是应用运行时必须包。

确认 Python 版本满足声明：

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version; print(sys.version)'
```

## 4. 安装 Node.js 22

Ubuntu 自带的 nodejs 版本可能低于 Vite 8 要求，使用 NodeSource 的 22.x 源：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node --version
npm --version
```

`node --version` 必须是 v22.12.0 或更高的 22.x 版本；如果不是，先解决 Node 版本，不要继续 `npm ci`。

## 5. 创建服务用户、目录并 Clone 仓库

```bash
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  sudo useradd --system --user-group \
    --home-dir "$STATE_DIR" --create-home \
    --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo install -d -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" -m 0755 "$APP_DIR"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$STATE_DIR" "$STATE_DIR/workspaces"
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
```

服务用户只需要读取 checkout；workspace 和导出文件写入 `/var/lib/bluelake-agent`，缓存写入 `/var/cache/bluelake-agent`。如果你的登录用户使用了非常规 umask，确保服务用户仍能读取代码、skills/ 和字体：

```bash
sudo chmod -R a+rX "$APP_DIR"
```

## 6. 创建 Python venv 和安装 Python 依赖

```bash
cd "$APP_DIR"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check

.venv/bin/python -c '
import fastapi, httpx, openai, pydantic, yaml, PIL, pathspec, markdown_it, docx, weasyprint, uvicorn
print("Python dependencies OK")
print("WeasyPrint", weasyprint.__version__)
'
```

若 `import weasyprint` 报 native library 错误，检查第 3.3 步的 apt 包，不要只改 Python 包。

可进一步检查：

```bash
pango-view --version || true
fc-match "sans-serif"
.venv/bin/python -m weasyprint --info
```

## 7. 安装前端依赖并构建 Vite production bundle

`web/vite.config.ts` 没有自定义 build.outDir，产物目录是 `web/dist`；`web/src/api/client.ts` 在未设置 `VITE_API_BASE_URL` 时使用同源相对路径 `/api`，生产不要设置跨域 API 地址。

```bash
cd "$APP_DIR/web"
npm ci
npm run build
test -f dist/index.html
cd "$APP_DIR"
```

## 8. workspace 与权限配置

浏览器本地模式没有服务端 SQLite，唯一的服务端持久数据是 workspace 文件与导出文件：

```bash
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 \
  "$STATE_DIR/workspaces"
sudo -u "$SERVICE_USER" test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/lxgwwenkailite-regular.css"
sudo -u "$SERVICE_USER" test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/lxgwwenkailite-regular-pdf.css"
sudo -u "$SERVICE_USER" test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/files/lxgwwenkailite-regular-full.woff2"
```

浏览器使用 Unicode 分片字体；PDF 导出使用 `lxgwwenkailite-regular-pdf.css` 和完整的 `lxgwwenkailite-regular-full.woff2`。两者都必须能由服务用户读取。PDF 专用字体来自官方 LXGW WenKai Lite v1.250 TTF 转换，许可见 checkout 内的 `web/public/fonts/lxgw-wenkai-lite/OFL.txt`。

应用会按 workspace 创建 `<workspace_id>/.agent-exports/`，导出文件和 manifest 均由服务用户写入；下载接口只接受 opaque file ID 并按当前 workspace 解析。

## 9. 安装环境文件

浏览器本地模式下**没有 secret**。环境文件只包含部署路径与运行设置。**不要**写入 `AGENT_SECRET_KEY`、`AGENT_SQLITE_PATH`、`AGENT_COOKIE_SECURE` 等旧变量——`server/config.py` 会拒绝它们并导致启动失败。

```bash
umask 077
tmp_env="$(mktemp)"
trap 'rm -f "$tmp_env"' EXIT

cat >"$tmp_env" <<EOF
AGENT_CONFIG=$APP_DIR/config.yaml
AGENT_HOST=127.0.0.1
AGENT_PORT=8000
AGENT_STATIC_DIR=$APP_DIR/web/dist
AGENT_WORKSPACE_ROOT=$STATE_DIR/workspaces
AGENT_SKILLS_ROOT=$APP_DIR/skills
AGENT_CORS_ORIGINS=https://$DOMAIN
AGENT_DEFAULT_SKILLS=wenqu
EOF

sudo install -o root -g "$SERVICE_USER" -m 0640 "$tmp_env" "$ENV_FILE"
rm -f "$tmp_env"
trap - EXIT

sudo stat -c '%A %U %G %n' "$ENV_FILE"
sudo grep -q '^AGENT_WORKSPACE_ROOT=' "$ENV_FILE"
```

环境变量说明（全部可选，均有默认值，见 `server/config.py`）：

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `AGENT_CONFIG` | 配置文件路径 | `config.yaml` |
| `AGENT_HOST` / `AGENT_PORT` | Uvicorn 监听地址 | `127.0.0.1:8000` |
| `AGENT_STATIC_DIR` | SPA 静态目录 | 相对 config 的 `web/dist` |
| `AGENT_WORKSPACE_ROOT` | 文件工具与导出的 workspace 根 | config 的 `workspace.root`（开发默认 `.`） |
| `AGENT_SKILLS_ROOT` | 可信 Skill 目录 | config 的 `workspace.skills_root` → config 同级 `skills/` → 仓库 `skills/` |
| `AGENT_CORS_ORIGINS` | 允许的跨域来源（逗号分隔） | config 的 `server.cors_origins` |
| `AGENT_DEFAULT_SKILLS` | 默认注入 Skill（逗号分隔） | config 的 `agent.default_skills`（`wenqu`） |
| `AGENT_MAX_TURNS` 等 | agent/context 调优参数 | 见 `server/config.py` |

预期权限应显示 `-rw-r----- root bluelake-agent`。日后修改环境变量也必须保留这个权限；不要把 `/etc/bluelake-agent.env` 复制进仓库。

## 10. 安装并启动 systemd

模板使用固定的 `/opt/bluelake-agent` 和 `bluelake-agent`，与前面的目录步骤一致；模板没有 secret，只通过 EnvironmentFile 加载部署路径：

```bash
sudo install -o root -g root -m 0644 \
  "$APP_DIR/deploy/myapp.service" /etc/systemd/system/myapp.service
sudo systemd-analyze verify /etc/systemd/system/myapp.service
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
sudo systemctl status myapp --no-pager
curl --fail --silent --show-error http://127.0.0.1:8000/api/health
echo
```

要点：

- `ExecStartPre` 检查 `web/dist/index.html` 和 WeasyPrint 是否能 import；
- 服务只监听 `127.0.0.1:8000`，**不要**在 UFW 或云安全组开放 8000；
- `--workers 1` 是硬性要求（进程内维护 chat run 状态表），不要改成多 worker；
- 服务以 `bluelake-agent` 系统用户运行，checkout 只读，仅向 `/var/lib/bluelake-agent`、`/var/cache/bluelake-agent` 写入。

## 11. DNS 前置要求

在签发证书前，把：

```text
YOUR_DOMAIN A     VPS_PUBLIC_IPV4
YOUR_DOMAIN AAAA  VPS_PUBLIC_IPV6   # 只有 VPS 确实提供 IPv6 时才配置
```

配置完成后，从 VPS 检查解析：

```bash
getent ahosts "$DOMAIN"
```

如果 DNS 仍指向旧机器、AAAA 指向错误机器，HTTP-01 challenge 会失败。

## 12. Nginx HTTP bootstrap

先使用只监听 80 的 bootstrap 配置，让 certbot 能通过 webroot 验证；证书签发后再替换为 HTTPS 配置。不要在证书不存在时直接安装 `myapp.nginx.conf`。

```bash
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
```

bootstrap 已包含 `/api/` 的 SSE 友好代理参数和 SPA history fallback，但暂时没有 HTTPS；这一步只用于 ACME 和 HTTP 基础验证。

## 13. Certbot / Let's Encrypt HTTPS

```bash
sudo certbot certonly --webroot \
  --webroot-path /var/www/letsencrypt \
  --domain "$DOMAIN" \
  --email "$LE_EMAIL" \
  --agree-tos --no-eff-email --non-interactive
```

> `LE_EMAIL` 请在第 2 节导出；如果没有，直接替换成你的邮箱。

证书成功后安装最终配置。sed 只替换域名；证书路径使用 Certbot 的标准 `/etc/letsencrypt/live/$DOMAIN/` 路径：

```bash
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
```

`myapp.nginx.conf` 的 `/api/` 使用 `proxy_pass http://127.0.0.1:8000;`（没有尾部 `/`），所以 `/api/chat`、`/api/files/{file_id}` 等路径不会被错误剥掉 `/api` 前缀。SSE 关闭了 `proxy_buffering`；`client_max_body_size 6m` 与浏览器运行时上下文 5 MiB 上限匹配。

## 14. UFW / 防火墙

先确认当前 SSH 端口已经允许，再启用 UFW：

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
sudo ufw status verbose
```

云厂商安全组也只放行 SSH、80、443；8000 保持关闭。

## 15. 首次上线验证

```bash
curl --fail --silent --show-error --head "https://$DOMAIN/"
curl --fail --silent --show-error "https://$DOMAIN/api/health"
curl --fail --silent --show-error --head "https://$DOMAIN/assets/does-not-exist.js" || true
```

浏览器打开 `https://$DOMAIN/`，确认：

1. SPA 能加载，刷新一个前端路由不会 404；
2. Settings 可以保存 main/summary provider 配置（保存在当前浏览器本地，换浏览器/设备需要重新配置）；
3. 新建会话后发送消息，Network 中 `POST /api/chat` 的响应类型为 `text/event-stream`，请求头含 `X-Workspace-ID`，能持续收到 `text_delta` 并以 `done` 结束；
4. Agent 通过 `export_file` 生成 md、txt、docx、pdf，四种文件都能从浏览器下载；
5. 同一浏览器关闭重开（历史仍在 IndexedDB）后，对话记录可恢复。

## 16. 四种格式真实 export smoke test

不要只检查 Python import。脚本会在生产 venv 中实际生成 `md`、`txt`、`docx`、`pdf` 四种文件，通过当前 HTTPS 域名调用真实 GET `/api/files/{file_id}` 下载，并检查四种响应的状态码、Content-Type 与 Content-Disposition；PDF 另外用 Poppler 检查文本、页数和嵌入字体。脚本自带 `X-Workspace-ID` 匿名请求头：

```bash
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" \
  "$APP_DIR/deploy/pdf_smoke_test.py" \
  --base-url "https://$DOMAIN" \
  --workspace-root "$STATE_DIR/workspaces"
```

成功条件：命令输出 `export smoke test passed`，且同时满足：

- md、txt、docx、pdf 均生成成功，下载 HTTP status 都为 200，且 Content-Disposition 都是 attachment；
- md 内容保持 Markdown，txt 为纯文本，docx 以 PK 开头且 PDF 以 %PDF 开头；
- pdftotext 能读到中文标题、正文、列表、代码、表格标记和链接文字；
- pdffonts 至少发现一个嵌入字体；
- pdfinfo 显示至少 1 页；
- 浏览器实际打开下载文件后，中文、粗斜体、表格和代码块可读。

脚本会删除本次 smoke test 产生的临时导出文件；真实用户导出文件不会被删除。

## 17. 日常代码更新

在部署用户下执行。不要删除 `/var/lib/bluelake-agent`：

```bash
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
```

若本次更新只涉及后端代码，仍建议先 build 再 restart；若 Git 更新包含 requirements.txt、package-lock.json、systemd 或 Nginx 文件，按上面完整流程执行。workspace 文件不受 git pull 影响。

## 18. 服务重启、日志和磁盘检查

```bash
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
```

## 19. 数据备份

浏览器本地模式下服务端没有 SQLite，唯一需要备份的服务端数据是 **workspace 文件**（用户通过文件工具保存的内容 + `.agent-exports/` 导出文件）：

```bash
sudo install -d -o root -g root -m 0700 /var/backups/bluelake-agent
sudo tar -czf \
  "/var/backups/bluelake-agent/workspaces-$(date +%F-%H%M%S).tar.gz" \
  -C "$STATE_DIR" workspaces
sudo ls -lh /var/backups/bluelake-agent
```

注意：

- provider API key 与聊天历史只存在用户浏览器，**不在**服务端备份范围内；换浏览器前需在 Settings 中重新配置；
- `.env.example` 只描述开发模式；生产环境文件见第 9 节，里面没有 secret，但也不要放进公开仓库；
- 建议为备份加异地/对象存储同步（如 rclone），并按需设置 cron。

## 20. 常见故障排查

| 症状 | 先检查 | 处理 |
| --- | --- | --- |
| myapp 启动失败 | systemctl status myapp、journalctl -u myapp -n 200 | 先看 EnvironmentFile、venv、web/dist/index.html 和 weasyprint import；修复后 systemctl restart myapp。 |
| 启动报 "Legacy server-side chat persistence ... not supported" | grep -E 'AGENT_SECRET_KEY\|AGENT_SQLITE\|AGENT_COOKIE\|REDIS' /etc/bluelake-agent.env | 从环境文件删除所有旧架构变量（见第 0、9 节），只保留部署路径变量。 |
| WeasyPrint 报 libgobject / Pango / HarfBuzz | .venv/bin/python -c 'import weasyprint'、pango-view --version、ldconfig -p \| grep -E 'pango\|harfbuzz' | 重新安装第 3.3 步的 apt runtime 包；不要只重装 pip 包。 |
| 首页 502 | curl http://127.0.0.1:8000/api/health、systemctl status myapp | 后端未运行、端口不一致或 service 用户不能读取代码/venv。 |
| 首页 404 或刷新路由 404 | test -f "$APP_DIR/web/dist/index.html"、nginx -t | 检查 Nginx root 是否为 /opt/bluelake-agent/web/dist，并确认使用了 try_files ... /index.html。 |
| /api 返回 SPA HTML | nginx -T | /api/ 必须是 ^~ proxy location；proxy_pass 不能带尾部 /。 |
| SSE 一次性返回或超时 | curl -N、nginx -T、journalctl -u myapp -f | 确认 /api/ 的 proxy_buffering off、长 proxy_read_timeout，以及 FastAPI 返回 X-Accel-Buffering: no。 |
| API 返回 401 workspace_id_required | 浏览器 Network 中 POST /api/chat 请求头 | 确认请求带 X-Workspace-ID 且格式合法；这是浏览器自动生成的匿名 ID，无需服务端配置。 |
| PDF/DOCX 生成 storage_failed | namei -l "$STATE_DIR/workspaces"、journalctl -u myapp | workspace 根必须由 bluelake-agent 可写；不要把它误设为只读 checkout。 |
| PDF 中文乱码、方框或没有字体 | test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/lxgwwenkailite-regular-pdf.css"、test -r "$APP_DIR/web/public/fonts/lxgw-wenkai-lite/files/lxgwwenkailite-regular-full.woff2"、pdffonts | 确认 PDF 专用 CSS 和完整字体随 Git checkout 存在且服务用户可读，再重新执行 PDF smoke test。 |
| 下载接口 404 | 检查浏览器的 X-Workspace-ID、.agent-exports manifest 和 AGENT_WORKSPACE_ROOT | file ID 只在生成它的 workspace 中有效；不要直接把文件路径拼到 URL。 |
| Certbot challenge 失败 | getent ahosts "$DOMAIN"、UFW、安全组、curl http://$DOMAIN/.well-known/... | 确认 A/AAAA、80 端口和 bootstrap 配置都指向本机；证书成功后再安装 HTTPS 模板。 |
| 磁盘空间不足 | df -h、sudo du -sh "$STATE_DIR" /var/log/nginx | 先保留备份，再清理旧导出/日志；不要删除整个 workspace 根。 |

## 21. 安全边界和上线前复核

- 服务端**不保存**对话、provider API key 或用户配置；密钥只在用户浏览器内用 Web Crypto 加密存储，服务端进程内存中仅短暂出现于单次请求。
- `X-Workspace-ID` 是浏览器本地生成的**匿名**隔离边界，不是公网身份认证；workspace 之间的隔离是应用层边界。
- secret 不存在：环境文件 `/etc/bluelake-agent.env` 只含部署路径，权限 `0640`、owner root、group 为服务用户。
- 服务用户不是 root；systemd 让 checkout 只读，只向 `/var/lib/bluelake-agent` 和 `/var/cache/bluelake-agent` 写入；`ProtectSystem=strict`、`PrivateTmp`、`NoNewPrivileges` 已启用。
- Uvicorn 只监听 loopback，公网只暴露 Nginx 的 80/443；`--workers 1` 保持单进程运行表一致。
- PDF renderer 禁用 Markdown 原始 HTML 和图片，并使用受限 URL fetcher：只允许读取 checkout 内置的 .woff2 字体，不允许 file:// 任意路径、外部 HTTP(S)、FTP 或 data 资源。
- 若面向不可信用户开放部署，还需要真正的认证、授权、速率限制和更强的进程隔离。
- 每次更新后至少执行：systemd-analyze verify、nginx -t、loopback health check、HTTPS health check、浏览器 SSE 测试和 PDF smoke test。
