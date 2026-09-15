# RAG 知识库

个人 RAG 检索知识库 —— 浏览器上传文档、多轮对话、混合检索；
文档与向量存 PostgreSQL + pgvector，模型本地/云端可切换。

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
                                                │
                                    [Ollama 本地 / 云端 API]
```

## 架构：分层，每层可单独替换

```
                      ┌── parse.py     文件 → 纯文本
索引：main → pipeline ┼── chunk.py     文本 → 分块
                      ├── embed.py     分块 → 向量
                      └── store.py     向量 → PostgreSQL

                      ┌── retrieve.py  问题 → 召回（vector / keyword / hybrid）
问答：main → pipeline ┼── generate.py  提示词 + 调用模型
      main → chat.py  └── providers.py 传输层（ollama / openai 兼容）
```

| 想改什么 | 改哪里 |
|---|---|
| 切分策略 | `chunk.py` 的 `split()`，保持 `str -> list[str]` |
| 换 embedding 模型 | 改 `data/settings.json`，**并重建索引**（维度或空间变了） |
| 接一家新的模型服务 | 改 `data/settings.json`，加一条 `kind: openai` 的 provider，**不用改代码** |
| 改提示词 / 引用格式 | `generate.py` 的 `SYSTEM` 与 `build_messages()` |
| 混合检索权重、加 rerank | `retrieve.py` 的 `search()`，签名不变 |
| 多轮记忆策略 | `chat.py` 的 `history()` |
| 整条链路换玩法 | `pipeline.py` 的两个函数，路由层不用动 |

## 模型：本地与云端混合，运行时可切

配置在 `data/settings.json`（含 API Key，已 gitignore；模板见 `settings.example.json`）。
任何 **OpenAI 兼容端点**都能接：DeepSeek / 通义百炼 / Moonshot / 智谱 / 硅基流动 /
OpenAI / 本地 vLLM / LM Studio。

- **默认模型**在「设置」页切换，落盘到 settings.json
- **每次请求**也可覆盖：`/api/chat`、`/api/ask` 传 `provider` + `model`
- 每条助手消息都记录实际使用的 provider/model，可回溯

> 向量模型切换是**重操作**：不同模型的向量空间不可比较。
> 每个分块都记录了 `embed_model`，检索时按当前模型过滤 —— 切换后旧块自动失效而不是返回垃圾结果，
> 「文档」页会把这类文档标成"需重建"。

## 检索：三种模式

| 模式 | 说明 |
|---|---|
| `vector` | pgvector 余弦检索（HNSW 索引） |
| `keyword` | 关键词召回（ASCII 词 + 中文二元组，ILIKE 计数打分） |
| `hybrid` | **默认**，两路召回后用 RRF 融合 |

## 三层缓存（全在 PostgreSQL，重启不丢）

| 层 | 键 | 省掉什么 |
|---|---|---|
| 向量 | `sha256(文本 + 模型)` | 最贵的向量计算；重建索引、跨文档重复内容直接复用 |
| 解析 | `sha256(文件内容)` | PDF/docx 解析 |
| 回答 | `sha256(问题 + 上下文哈希 + 历史哈希 + provider + model)` | 本地模型数十秒的推理 |

**两个关键设计：**

1. **键里含所有影响结果的参数**——上下文哈希、历史哈希、模型名、温度。资料改了、对话历史变了、
   换了模型，键就变，**不存在返回陈旧结果的窗口**。所以「缓存」页的清空按钮只是释放空间，
   不影响正确性。
2. **向量缓存用 `text` 存而非 `vector` 类型**——缓存只按 key 精确查、从不做相似度检索，
   因此不被 `embed_dim` 的 DDL 绑死；将来换 1536 维的向量模型，缓存表无需迁移。

**实测**：重建索引从数秒降到 **1.76s**（解析+向量全命中）；重复提问从 21.8s 降到 **0.44s**。

## 异步索引：为什么必须

**Cloudflare 免费版对源站响应有 100 秒硬超时（524），无法延长。** 一篇大 PDF 的
切分+向量化很容易超过 100 秒，同步接口必然被 CF 掐断。

因此 `POST /api/docs/{id}/index` 立即返回 **202 + task_id**，前端轮询 `/api/tasks/{tid}` 显示进度。
任务状态写在 `index_tasks` 表里，刷新页面、重启应用都不丢；进程重启时启动钩子会把残留的
`running` 任务标记为中断。

> 若将来要多 worker 部署，把 `app/tasks.py` 的 `submit()` 换成真正的队列（RQ / arq / Celery）即可，
> 上层接口不用动。

## 目录

```
app/main.py        路由层（只做 HTTP）
app/pipeline.py    索引链路 + 无状态问答
app/chat.py        多轮对话（会话与消息持久化）
app/parse.py       pdf/docx/md/html/csv/json → 文本
app/chunk.py       切分
app/embed.py       向量化门面
app/store.py       分块与向量持久化、失效检测
app/retrieve.py    三模式检索
app/generate.py    提示词与生成
app/providers.py   模型传输层（ollama / openai 兼容）
app/settings.py    运行时设置
app/db.py          PostgreSQL 连接与建表
web/index.html     前端：对话 / 搜索 / 文档 / 设置
scripts/           启动与运维脚本
bin/ pgsql/ pgdata/ data/      均 gitignore
```

## 使用

```powershell
.\scripts\start.ps1              # 拉起 PostgreSQL + 应用（隧道由 cloudflared 服务负责）
.\scripts\pg.ps1 status          # 数据库启停：start|stop|status|restart
```

## 开机自启

| 组件 | 方式 |
|---|---|
| 隧道 | **Windows 服务** `cloudflared`（Automatic，装一次即可） |
| PostgreSQL + 应用 | **启动文件夹**：`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\RAG-KB.vbs`，登录后隐藏窗口调用 `scripts\start.ps1` |

> 为什么不用计划任务：`Register-ScheduledTask` 与 `schtasks /create` 在本机都返回
> **Access is denied**，用户级创建同样被拒。启动文件夹方式**无需管理员**，效果等价
> （代价是必须登录一次才会触发）。
>
> 若哪天想改成真正的系统级自启（开机即起、不需登录），需要管理员权限。


## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/upload` | 上传文档 |
| POST | `/api/docs/{id}/index` | **202 + task_id**（异步，见下） |
| GET | `/api/tasks/{tid}` `/api/docs/{id}/task` | 任务进度 / 该文档最近任务 |
| POST | `/api/search` | 检索（`mode` / `doc_id` / `top_k`） |
| POST | `/api/ask` | 无状态问答 |
| POST | `/api/chat` | 多轮对话（不传 `conv_id` 则新建） |
| GET | `/api/conversations` `/api/conversations/{id}` | 会话列表 / 明细 |
| GET | `/api/cache/stats` · DELETE `/api/cache?which=` | 缓存统计 / 清空 |
| GET/PUT | `/api/settings` `/api/settings/defaults` | 设置 |
| GET | `/api/providers/{id}/probe` | 探测某提供方可用模型 |
| GET | `/api/health` | 数据库 + 向量模型自检 + 失效文档 |

## 认证

全站 HTTP Basic（`app/auth.py`）。凭据：环境变量 `KB_USER`/`KB_PASS` > `data/auth.json`。

> 升级路径：域名解析生效后换 **named tunnel + Cloudflare Access**，
> 把"共享密码"升级为身份验证 + 边缘拦截。

## 进度

- [x] **Step 1** 链路验证：Cloudflare 隧道，外网浏览器可达
- [x] **Step 2** 认证层：HTTP Basic 全站保护，经隧道端到端验证
- [x] **Step 3** 存储：PostgreSQL 18.6 + pgvector 0.8.6
- [x] **Step 4** RAG 管线：切分 → 向量化 → 三模式检索 → 生成；多轮对话落库
- [x] **Step 5** 混合模型：本地 Ollama 与 OpenAI 兼容 API 运行时可切
- [x] **Step 6** 索引异步化（规避 CF 100s 超时）+ 三层缓存
- [ ] **Step 7** 固定域名：named tunnel + Cloudflare Access（域名 `rag-kb-awa.xyz` 委派已下发，待 CF 显示 Active）
- [ ] **Step 8** 对话流式输出（SSE）：进一步规避 100s 超时，并改善本地模型的等待体验

## 已知事项（踩过的坑）

- **不要把 PostgreSQL 挂在可能被杀掉的 shell 下面。** `pg_ctl start` 启动的 postgres 会继承
  stdout 句柄导致管道挂死；若随后杀掉该进程树，会连带干掉 postmaster 的子进程，触发
  `0xC0000142` 崩溃恢复，进而卡在 Windows 共享内存预留失败（`error code 487`）死循环。
  `scripts/pg.ps1` 用 `Start-Process` 完全脱离进程树来规避。
- **pgvector 不是 trusted 扩展**，必须由超级用户执行 `CREATE EXTENSION vector;`。
- **PowerShell 5.1 的 `Invoke-RestMethod` 会把无 charset 的 JSON 按 Latin-1 解码**，
  中文显示成乱码。用 `curl.exe` 或显式指定编码读取，数据本身没问题。
- **快速隧道 URL 每次重启都会变**，长期使用需 named tunnel。
- 本机曾有一个 `Meta Tunnel`(wintun) 网卡劫持全部 DNS 并把流量塞进一个承载不了 7844 端口的
  代理，导致 Cloudflare 隧道无法建连。该网卡现已不存在。若重启代理客户端后隧道再次连不上，
  在代理规则里给 `cloudflared.exe` 加一条 `DIRECT`。
