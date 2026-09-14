# RAG 知识库

个人 RAG 检索知识库 —— 浏览器上传文档 + 交互问答，文档信息存入 PostgreSQL + pgvector。

## 为什么用隧道而不是公网 IP

本机位于校园网内，无公网 IPv4，且校园网对 **IPv6 入站做拒绝策略**（实测：校园网内 v4 可达、
v6 被 RST；换移动数据同样被拒；本机三层嫌疑——Windows 防火墙 / 火绒 / 隧道网卡——已逐一排除，
证据是 v4 路径在同一条件下可通）。

因此不走"监听公网端口"路线，改用 **Cloudflare Tunnel**：`cloudflared` 从本机**主动连出**，
隧道反向承载请求。**全程不开放任何入站端口**，无需公网 IP、无需学校配合，攻击面接近零。

```
[手机浏览器 · 外网]
      │ HTTPS
      ▼
[Cloudflare 边缘]  ◄──── 出站长连接 ────  [cloudflared · 本机]
                                                │
                                           [Basic 认证层]
                                                │
                                           [FastAPI 应用]
                                                │
                                    [PostgreSQL 18.6 + pgvector]
```

## 目录

```
app/main.py            FastAPI 应用（上传入库 / 列表 / 删除）
app/auth.py            HTTP Basic 全站中间件
app/db.py              PostgreSQL + pgvector 存储层（连接 / 建表 / 健康检查）
web/index.html         前端页面
scripts/start.ps1      一键启动：PG + 应用 + 隧道
scripts/pg.ps1         PostgreSQL 启停（start|stop|status|restart）
scripts/run.ps1        仅启动应用（前台）
scripts/tunnel.ps1     仅启动隧道（前台）
scripts/tunnel_test_direct.ps1   隧道连通性诊断（需管理员）
bin/                   cloudflared.exe            (gitignore)
pgsql/                 PostgreSQL 18.6 绿色版     (gitignore)
pgdata/                数据库数据目录             (gitignore)
data/                  上传文件 / 凭据 / 日志     (gitignore)
```

## 数据库

PostgreSQL 18.6（免安装绿色版，解压即用）+ pgvector 0.8.6，**只监听 `127.0.0.1:5432`**。

| 表 | 内容 |
|---|---|
| `documents` | 文档元数据：id / 名称 / 大小 / 存储路径 / 状态 / 分块数 |
| `chunks` | 分块与向量：doc_id / seq / content / `vector(1024)`，带 HNSW 余弦索引 |

凭据文件（均已 gitignore）：`data/pgapp.txt`（应用角色 `ragkb`）、`data/pgpass.txt`（超级用户 `postgres`）。

向量维度由 `KB_EMBED_DIM` 控制（默认 **1024**，对应 bge-m3）；**改动需重建 `chunks` 表**。

```powershell
.\scripts\pg.ps1 start | stop | status | restart
```

## 使用

```powershell
.\scripts\start.ps1     # 拉起 PG + 应用 + 隧道，打印公网地址与登录凭据
```

## 认证

全站 HTTP Basic（`app/auth.py` 中间件）。凭据优先级：
环境变量 `KB_USER` / `KB_PASS` > `data/auth.json`（首次启动自动生成随机密码）。

> 升级路径：域名解析生效后换 **named tunnel + Cloudflare Access**，
> 把"共享密码"升级为身份验证 + 边缘拦截。

## 进度

- [x] **Step 1** 链路验证：Cloudflare 隧道，外网浏览器可达（实测 401/200 符合预期）
- [x] **Step 2** 认证层：HTTP Basic 全站保护，经隧道端到端验证
- [x] **Step 3** 存储：PostgreSQL 18.6 + pgvector 0.8.6，上传 → 入库端到端验证通过
- [ ] **Step 4** RAG 管线：切分 → 向量化（Ollama bge-m3）→ 检索 → 生成
- [ ] **Step 5** 固定域名：named tunnel + Cloudflare Access（等域名实名认证通过）

## 已知事项（踩过的坑）

- **不要把 PostgreSQL 挂在可能被杀掉的 shell 下面。** `pg_ctl start` 启动的 postgres 会继承
  stdout 句柄，导致 shell 管道永不关闭而挂死；更严重的是，若随后杀掉该 shell 的进程树，会连带
  干掉 postmaster 的子进程，触发 `0xC0000142`(STATUS_DLL_INIT_FAILED) 崩溃恢复，进而卡在
  Windows 共享内存预留失败（`error code 487`）的死循环里，所有连接超时。
  `scripts/pg.ps1` 用 `Start-Process` 完全脱离进程树来规避。
- **pgvector 不是 trusted 扩展**（`vector.control` 无 `trusted = true`），必须由超级用户执行
  `CREATE EXTENSION vector;`。已在 `ragkb` 库装好。
- **快速隧道 URL 每次重启都会变**，长期使用必须上 named tunnel。
- 本机曾有一个 `Meta Tunnel`(wintun) 网卡劫持全部 DNS，并把流量塞进一个承载不了 7844 端口的
  代理，导致 Cloudflare 隧道无法建连。该网卡现已不存在。若重启代理客户端后隧道再次连不上，
  在代理规则里给 `cloudflared.exe` 加一条 `DIRECT`。
