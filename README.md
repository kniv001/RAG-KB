# RAG 知识库

个人 RAG 检索知识库 —— 浏览器上传文档 + 交互问答，文档信息存入成熟数据库。

## 为什么用隧道而不是公网 IP

本机位于校园网内，无公网 IPv4，且校园网对 **IPv6 入站做拒绝策略**（实测：校园网内 v4 可达、
v6 被 RST；换移动数据同样被拒；本机三层嫌疑——Windows 防火墙 / 火绒 / 隧道网卡——已逐一排除）。

因此不走"监听公网端口"路线，改用 **Cloudflare Tunnel**：`cloudflared` 从本机**主动连出**，
隧道反向承载请求。**全程不开放任何入站端口**，无需公网 IP、无需学校配合，攻击面接近零。

```
[手机浏览器 · 外网]
      │ HTTPS
      ▼
[Cloudflare 边缘]  ◄──── 出站长连接 ────  [cloudflared · 本机]
                                                │
                                          [FastAPI 应用]
                                                │
                                        [PostgreSQL + pgvector]
```

## 目录

```
app/main.py         FastAPI 应用（当前：上传收件 + 登记）
web/index.html      前端页面（上传 / 列表 / 健康状态）
scripts/run.ps1     启动本地服务
scripts/tunnel.ps1  启动 Cloudflare 隧道
bin/                cloudflared.exe（已 gitignore）
data/               上传的文档与索引（已 gitignore）
```

## 使用

```powershell
# 终端 1
.\scripts\run.ps1

# 终端 2
.\scripts\tunnel.ps1      # 打印 https://xxx.trycloudflare.com
```

## 认证

全站 HTTP Basic 保护（`app/auth.py` 中间件）。凭据优先级：

1. 环境变量 `KB_USER` / `KB_PASS`
2. `data/auth.json`（首次启动自动生成随机密码，已 gitignore）

改密码：编辑 `data/auth.json` 后重启服务。

> 升级路径：取得域名后可换 **named tunnel + Cloudflare Access**，把"共享密码"升级为
> 身份验证 + 边缘拦截。快速隧道（`*.trycloudflare.com`）**不支持** Access。

## 进度

- [x] **Step 1** 链路验证：本机 FastAPI + Cloudflare 快速隧道，外网浏览器可达（实测 401/200 符合预期）
- [x] **Step 2** 认证层：HTTP Basic 全站保护，经隧道端到端验证
- [ ] **Step 3** 存储：PostgreSQL + pgvector
- [ ] **Step 4** RAG 管线：切分 → 向量化 → 检索 → 生成

## 已知事项

- **快速隧道 URL 每次重启都会变**，且 `cloudflared` 需随服务一起启动。长期使用建议上域名 + named tunnel。
- 本机曾有一个 `Meta Tunnel`(wintun) 网卡劫持全部 DNS 并把流量塞进一个承载不了 7844 端口的代理，
  导致 Cloudflare 隧道无法建连。该网卡现已不存在，原生网络正常。若重启代理客户端后隧道再次连不上，
  在代理规则里给 `cloudflared.exe` 加一条 `DIRECT`。
