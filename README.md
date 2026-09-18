# RAG 知识库 · Java 版

[English](README.en.md) | **简体中文**

个人 RAG 知识库的 Java 重写。与 [`D:\vs\rag-kb`（Python 版）](https://github.com/kniv001/RAG-KB/tree/python)
**并行运行、互不干扰**，两套连同一个 PostgreSQL / Redis，切换只需改 cloudflared 的转发端口。

**当前阶段**：功能完整可用。浏览器界面、Agentic RAG、联网搜索、主题树都在跑，
外网经 Cloudflare 隧道访问。

| 能力 | 说明 |
|---|---|
| 前端 | 单页聊天界面，无框架无构建；附件上传、流式渲染、公式渲染 |
| 检索 | 向量 + 关键词 + RRF 融合；agent 做规划/评估/重试 |
| 会话 | 多轮持久化；旧轮次向量召回；滚动摘要；轮次笔记 |
| 知识库 | 上传或联网抓取；主题树给出「覆盖了哪些方向」 |
| 加密 | 端到端（每请求一把 AES 密钥，RSA 封装）；上传也一样 |
| 认证 | 双令牌（JWT + Redis 托管的不透明 refresh） |

## 技术栈

| 层 | 选型 |
|---|---|
| 运行时 | Java 21 (LTS) |
| 框架 | Spring Boot 3.5.16 |
| 构建 | Maven 3.9.11（多模块） |
| 持久化 | PostgreSQL 18.6 + pgvector，MyBatis-Plus 3.5.17 |
| 缓存/会话 | Redis |
| 认证 | Spring Security + JWT + bcrypt |
| 传输加密 | RSA-OAEP(SHA-256) + AES-256-GCM 混合 |

> Spring Boot 4.x 已发布，但 MyBatis-Plus 的 starter 仍锚在 Boot 3（`mybatis-plus-spring-boot3-starter`），
> 生态未跟上，故停在 3.5.16。

## 模块结构

依赖**单向无回环**：

```
common ← domain ← dao ← service ← web
                    ↖ security ↙
                    ↖ provider ↙
```

| 模块 | 职责 |
|---|---|
| `rag-kb-common` | 统一响应体 `R<T>`、业务异常、工具、配置属性 |
| `rag-kb-domain` | 实体 / DTO / VO（只引 `mybatis-plus-annotation`，不引自动配置） |
| `rag-kb-dao` | Mapper、pgvector 类型映射、建表脚本 |
| `rag-kb-security` | Spring Security + JWT 双令牌 + Redis 会话 + 加解密过滤器 |
| `rag-kb-provider` | 模型提供方抽象（本地 Ollama / 任意 OpenAI 兼容端点） |
| `rag-kb-service` | RAG 管线、会话、三层缓存、异步任务 |
| `rag-kb-web` | 启动类 + Controller（唯一可执行模块） |

## 构建与运行

```powershell
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-21'
$mvn = 'C:\Users\kniv\tools\apache-maven-3.9.6\bin\mvn.cmd'

& $mvn -f pom.xml -B -DskipTests clean package     # 产出 rag-kb-web/target/rag-kb.jar

# 数据库密码不进仓库，从 Python 版的凭据文件读
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()
java -jar rag-kb-web\target\rag-kb.jar
```

> **打包前必须先停掉正在跑的应用** —— 否则 `clean` 删不掉被占用的 `rag-kb.jar`，
> 报「另一个程序正在使用此文件」。这个报错跟编译错误长得很像，但完全无关。
>
> **端口通不等于服务可用** —— 漏掉 `KB_DB_PASSWORD` 那行时，应用照常启动、首页照常
> 200，但建表这一步会报 `SCRAM-based authentication, but no password was provided`，
> 之后所有数据库操作全废。**换进程启动后要看 `/actuator/health`**：`db` 与 `redis`
> 两个组件都得是 `UP`，别只看端口。（这个坑踩过：重启时用 `Start-Process` 起，
> 父进程没有那个环境变量。）
>
> Maven 在 `C:\Users\kniv\tools\apache-maven-3.9.6`。注意它**只在 Git Bash 的 PATH 里**
> （profile 加的），PowerShell 里直接敲 `mvn` 会报「不是 cmdlet」。
> `~/.m2/settings.xml` 已配阿里云镜像（镜像 id 设为 `central`，以复用本地仓库缓存）。

联网功能默认关闭，要开就带上环境变量：

```bash
KB_WEB_ENABLED=true java -jar rag-kb-web/target/rag-kb.jar
```

其他可用环境变量：`KB_PROMPT_WINDOW`（提示词窗口，默认 10240）、
`KB_STORAGE_ROOT`、`KB_KEY_DIR`、`KB_DEEPSEEK_KEY`。

服务只监听 `127.0.0.1:8080`，对外仍走 Cloudflare 隧道，不开任何入站端口。

## 认证：双令牌

| 令牌 | 形态 | 有效期 | 存放 | 可否吊销 |
|---|---|---|---|---|
| Access | JWT(HS256) | 30 分钟 | 前端内存 | ❌ 到期自然失效 |
| Refresh | 32 字节随机串 | 30 天 | Redis + httpOnly Cookie | ✅ 登出/改密即删 |

| 接口 | 作用 |
|---|---|
| `POST /api/auth/login` | 校验密码 → 返回 access，下发 refresh Cookie |
| `POST /api/auth/refresh` | 换新 access，并**轮换** refresh |
| `POST /api/auth/logout` | 删 Redis 记录 + 清 Cookie |
| `POST /api/auth/password` | 改密并吊销该用户全部会话 |
| `GET /api/auth/me` · `GET /api/auth/config` | 当前用户 / 登录页公开参数 |

失败限速 10 次 / 15 分钟；用户名不存在时也跑一次哈希，消除时序侧信道。
401 响应体带 `expired` 标记，前端据此决定「去刷新」还是「跳登录」。

### 与 Python 版共用 users 表（重要）

两套系统连同一个库，因此密码哈希必须逐字节兼容。Python 版为绕开 bcrypt 的
72 字节上限，**先做一次 SHA-256 再把原始摘要交给 bcrypt**；Java 的
`BCryptPasswordEncoder` 是直接哈希口令字符串 —— 输入不同，同一个密码两边算出的哈希对不上。

所以实现了 [`Sha256BcryptEncoder`](rag-kb-security/src/main/java/com/kniv/ragkb/security/Sha256BcryptEncoder.java)
复刻同一方案，并必须走 `BCrypt.hashpw(byte[], salt)` 重载（原始摘要含非 UTF-8 字节，
用 String API 会被二次编码破坏）。库里的值形如 `$2b$12$...`，不加 `{bcrypt}` 前缀。

## 模型提供方

本地与云端同一套接口，运行时可切。引用格式 `providerId/model` —— 用斜杠是因为模型名
本身常含冒号（`qwen3:8b`），用冒号分隔会歧义。

| 接口 | 作用 |
|---|---|
| `GET /api/provider/list` | 已配置的提供方 + 默认模型（API Key 只回传是否已设置） |
| `GET /api/provider/{id}/probe` | 探测可用性并列出真实可用的模型 |
| `POST /api/provider/chat` | 同步对话 |
| `POST /api/provider/chat/stream` | **SSE 流式**，逐事件加密 |
| `POST /api/provider/embed` | 向量化 |

接一家新服务（DeepSeek / 通义 / Moonshot / 智谱 / 硅基流动 / OpenAI / vLLM / LM Studio）
只需在 `application.yml` 补一段配置，不用改代码。

用 JDK 自带的 `java.net.http.HttpClient`：`BodyHandlers.ofLines()` 原生支持逐行读，
做 NDJSON（Ollama）与 SSE（OpenAI 兼容）都是零依赖，不必为流式引入响应式栈。

### 流式与加密的冲突（设计要点）

过滤器的响应加密是「把响应整体缓存下来再加密」，而 SSE 的价值恰恰是**边生成边推** ——
两者直接冲突。所以流式路径（默认 `/**/stream`）**只解密请求，响应由控制器逐事件加密**：
过滤器把 AES 密钥放进请求属性交接给控制器，控制器在流结束时清零。

> 模式必须写成 `/**/stream` 而不是 `**/stream`：`AntPathMatcher` 按 `/` 切段，
> 请求 URI 以 `/` 开头会产生一个空段，缺前导斜杠会**静默匹配失败** ——
> 表现为「流式接口返回 application/json 且只有一个响应体」。

### ASYNC 派发与 SecurityContext

SSE 是异步请求，容器在流结束时会对同一路径再做一次 **ASYNC 派发**。
`OncePerRequestFilter` 默认跳过异步派发，所以 JWT 过滤器第二趟不执行；
而 Spring Security 6 起不再自动保存 SecurityContext —— 不显式保存的话，
异步派发会判定为未认证，表现为「流跑到最后突然 500 / Access Denied」。
解法：`RequestAttributeSecurityContextRepository` 把上下文存进**请求属性**，它会跨异步派发保留。

## 端到端加密

### 为什么做

**Cloudflare 会终止 TLS，所以 CF 能看到经它转发的全部明文。** 应用层再做一层
RSA + AES 混合加密后，链路上（含 CF）只能看到密文。

### 必须清楚的边界

> **前端 JS 本身也由 Cloudflare 分发。** 所以这层能做到「防中间设备偷看」，
> 但做不到「防中间设备使坏（替换 JS）」。要拿到完整端到端，客户端必须由自己控制
> —— 桌面端或 App。这也是后续「渲染 + 流式输出」规划的同一条路。

### 协议

| 头 / 体 | 内容 |
|---|---|
| `X-Enc-Key` | `base64( RSA-OAEP(aesKey) )`，AES 密钥由客户端**每请求新生成** |
| `X-Enc-Meta` | `base64( {"iv","d"} )`，明文 `{"ts":毫秒,"nonce":"..","token":".."}` |
| 请求体 | `{"iv","d"}`，明文即原始请求 JSON（仅方法有体时） |
| 响应体 | `{"iv","d"}`，复用同一把 AES 密钥、**换新 IV** |

**为什么令牌放在加密的 meta 头里**：若只加密请求体，`Authorization` 头仍是明文，
中间设备照样能拿到令牌直接调用 API —— 加密就白做了。头的方式对 GET/DELETE 也统一。

**为什么密钥每请求新生成**：服务端不需要存任何客户端密钥，无状态、可水平扩展，
也不怕 Redis 被读到密钥。

**防重放**：`ts` 与 `nonce` 都放在**密文内**（放头里可被篡改）；时间窗 ±5 分钟，
nonce 落 Redis（TTL 10 分钟）。

### 互操作陷阱（已解决）

Java 的 `RSA/ECB/OAEPWithSHA-256AndMGF1Padding` **默认用 SHA-1 做 MGF1**，
而浏览器 WebCrypto 的 `RSA-OAEP(hash=SHA-256)` 摘要与 MGF1 **都用 SHA-256**。
不对齐就永远解不开。代码里显式构造了 `OAEPParameterSpec("SHA-256","MGF1",SHA256,...)`。

AES-GCM 的密文布局两边一致（ciphertext‖tag），无需转换。

### 文件上传的加密（已解决）

multipart 的分片边界、头字段、文件名全是明文，Cloudflare 终止 TLS 后能完整还原出
文件与令牌 —— 而那正是这层加密要防的东西。所以**不改造 multipart，另开了一条路径**：

```
POST /api/docs/upload-encrypted
Content-Type: application/octet-stream
X-Enc-Key    RSA-OAEP 包裹的 AES-256 密钥
X-Enc-Meta   加密的 {ts, nonce, token, iv, name, size}
体           文件密文本身（ciphertext‖tag）
```

**文件名与文件 IV 放在加密的元信息里**，不放请求头 —— 放头里等于没加密。

两个实现要点：

- **体不进内存**。整个读进来解密的话，一个 50MB 文件要占 100MB 堆。
  过滤器只把密钥与元信息交接给控制器（`ATTR_AES_KEY` / `ATTR_META`），
  体原样透传，控制器边读边解密边落盘，峰值只有一个 64KB 缓冲。
- **先写 `.part` 临时文件，认证标签校验通过后才改名就位**。否则一个标签不对的
  请求会在磁盘上留下半截文件，而它看起来和正常文件一模一样。
  这里刻意不用 `CipherInputStream` —— 它在 AEAD 上有历史坑（JDK-8012631），
  标签失败时可能吞掉异常、静默截断。改成自己 update/doFinal 循环，标签不对
  会在 `doFinal` 上明确抛出。

老的 multipart 接口保留未动，避免破坏可能存在的旧客户端。

## Agentic RAG

规划 → 检索 → 评估 →（不足则换查询再来）→ 生成。agent 只是控制流，
**事实来源始终只有检索到的资料** —— 三条硬约束保证这一点：可用动作只有「检索」
（不联网、不写文件）；资料不足时如实说不知道；回答逐条标注来源编号。

**规划与评估强制结构化输出**（关思考 + 语法约束 JSON）。这是本项目收益最大的一处优化：

| | plan | assess |
|---|---|---|
| 关闭 | 51.5 秒 | 31.4 秒 |
| 开启 | **0.94 秒** | **1.6 秒** |

根因是模型为吐一个三行 JSON 先生成了 4561 个 token 的推理。**两个开关必须同时给**：
只关思考，推理会从 `thinking` 通道漏进 `content` 通道把 JSON 冲碎；
只给约束，模型照样先想完再输出。

**回答三段式**（`ANSWER_SYSTEM`）：有资料就严格按资料答；没资料也不要甩一句
「没有」了事，而是先声明知识库没有、再用通用知识作答并标注来源边界；
闲聊与身份问题不套知识库约束。标注写得硬，是因为它是这套系统**唯一的信任边界**
—— 用户必须一眼分得清哪句来自自己的资料、哪句是模型的通用知识。

## 分块

600 字目标 / 80 字重叠，**边界按句子对齐**，不按字符位置硬切。

超长段落先切成句子再打包，相邻块之间带**整句级别**的重叠；只有单句本身超过 600 字
（代码块、无标点长串）才退回字符切。

为什么较这个真：**「从句子中间开始」的块占比实测 75% → 13%**（六篇文档对照，四篇降到 0）。
半句开头的块指代无从解析、语义不完整，既是检索的噪声，也是抽取类任务失败的直接原因 ——
同一段按句子边界对齐后，模型三次抽取都正常且条数完全一致（22/22/22），硬切那版三次里空过一次。

**换行不能一律当边界**：这个语料是网页抓来的，75% 的行是硬折行（一句话折成好几行）。
判据是「换行两侧只要有一行不含中文就当边界」—— 代码/输出行不含中文（抽样占 75%），
天然是独立单位；中文散文即使折行，两侧都含中文就不切。

> 这一版边界规则与 Python 版**不同**（Python 版仍是字符硬切）。两套共用一个库，
> 切分口径不一致会让同一篇文档在两边的检索结果对不上。

## 上下文怎么花

这块是本项目做得最细的地方，每个数都是测出来的（详见 [docs/ollama-tuning.md](docs/ollama-tuning.md)）。

**窗口 10240 token，但真正的红线是 9920。** 超限不是渐进退化，是**悬崖**：
Ollama 把整个上下文重置到 5122 token 并**丢掉开头** —— 也就是系统提示词。
后果不是「回答变差」，是「回答不再受任何约束」，而模型照常返回看起来正常的回答、
不报任何错。所以应用层有 `PromptBudget` 按优先级裁：摘要 → 最旧的历史 →
召回片段 → 资料（资料最后动，它是事实依据）。

**实测一次问答 2.6K~6.1K token**（窗口的 16%~60%），固定部分约 1100：
系统提示 530 + 主题概览 440 + 问题。其余是资料（最多 12 段）与最近历史（最多 16 条）。

**三层历史，可靠性递减**：

| 层 | 内容 | 怎么来的 |
|---|---|---|
| 最近窗口 | 原文全文 | 直接取，无检索参与 |
| 更早轮次 | 向量召回的片段 | 见下面的「查询侧顺序」 |
| 滚动摘要 | 覆盖全部历史的一段 | 攒 6 条消息调一次模型 |

**查询侧顺序是关键**：召回必须发生在**规划之后**。规划器把「那它呢」改写成了
自包含的查询，用那个当检索键才召得回定义「它」的那一轮；用用户原话当键的话，
键就是「那它呢」四个字 —— 而那一轮的文本里根本没有「它」。
规划本身用不到召回片段：它有最近 16 条原文和滚动摘要。

**轮次笔记**：窗口之外的轮次会被改写成自包含的笔记，**索引的是笔记而不是原文**。
单条 message 不是好的检索单元 —— 用户那句常带指代、助手那句常脱离问题、
两边都夹着「根据参考资料[1]」这类包装。改写交给**对话模型**，不是向量模型
（后者只负责把改好的笔记变成向量）。

## GPU 调度

本机只有一块 GPU，Ollama 只有一个推理槽（`OLLAMA_NUM_PARALLEL=1`），请求 FIFO 排队。
所以后台任务会**等满**地拖慢用户：

| 后台任务耗时 | 用户请求耗时 |
|---|---|
| 1314ms | 1164ms |
| 3091ms | 3095ms |

用户请求自己只要 192ms，其余全在等槽（旁证：用户的 prefill 始终稳定在 36~39ms，
如果在算这里也会涨）。

`GpuGate` 让后台任务避让：没有用户请求在跑、且静默 20 秒，才允许开始；
等超过 300 秒就放弃本次（后台任务都是增量的，下次还会再来）。

**以后每加一个后台任务，先用 `tools/gpu-contention-probe.mjs` 量一遍。**

## 联网搜索

搜到 → 抓取 → **入库成为文档**，而不是让 agent 直接联网找答案。

后者会破坏那条硬约束：网页内容没进过库、没被审过、也不知道出处，
而用户完全看不出来。变成文档之后，它跟用户自己上传的文件走同一条路 ——
一样被切分、向量化、留下出处，所以「回答里的每一句都有出处」这条性质在联网之后依然成立。

**抓取时保留原文的结构**（2026-09-17 改）。原先用 jsoup 的 `main.text()` 取正文，
那会把整棵 DOM **拍平成一串文本** —— 标题、段落、列表、代码块的结构全在这一步丢掉。
代价是可量化的：41 篇已入库文档合计只剩 64 个标题行，而其中绝大多数是抓取时自己加的那行标题，
于是「这篇文档讲了哪几块」在库里无从恢复。

现在改成遍历 DOM 输出 Markdown：标题 → `#`~`######`、段落 → 空行分隔、列表 → `- `、
表格 → `| `、`<pre>` → 围栏原样保留。实测同一站点新抓的两篇：一篇 7161 字里 **59 个标题**、
147 个段落，层级完整（`## 一、持久化的作用` / `### 1. 什么是持久化`）。

> 已知限制：用 JS 高亮器（hljs / prism）渲染的代码块是 `<div>` 而非 `<pre>`，
> 提取时不认，那部分**文字不丢但结构拿不到**。已入库的文档也补不回来 —— 结构只能在抓取时拿到。

**后端选择是实测筛的**（校园网，查「三层缓存架构」）：

| 后端 | 结果 |
|---|---|
| Bing | ❌ 把「三层」拆成单字「三」返回汉字百科页（加 mkt、换 cn.bing.com 都一样） |
| 搜狗 | ✅ 结果最准，但链接是解不出目标地址的 JS 跳转，入库拿不到出处 |
| **360** | ✅ 结果准，真实网址直接写在 `data-mdurl` 属性里 |
| 百度 / DuckDuckGo / Brave / Jina / 维基 | ❌ 反爬页或连不上 |

默认 `so360 → bing` 按顺序试。**抓取有 SSRF 防护** —— 本机 Redis 监听在
`0.0.0.0:6379`，所以每次抓取前逐个检查 DNS 解析出的**全部**地址
（只查第一个的话，一个同时解析到公网与内网的域名就能绕过）。

**默认关闭**（`KB_WEB_ENABLED=true` 才开）：这个功能会让服务端主动向外发起请求，
与「只监听本机」的默认姿态不同，应当由用户明确打开。

## 主题树

把全部块聚成若干主题簇，每簇一句概括。解决的是「语料一大，扁平的 top-k 就看不出全局」
—— 检索不到时只能回「知识库中没有」，而说不出「没有 X，但有 Y 和 Z 两个方向」。

聚类用 k-means++（固定随机种子，否则同样语料每次重建会得到不同分簇），
模型只在最后一步给每簇起名概括。只做两层不做深树：深树的收益来自逐层收窄，
而每下降一层都要付 KV，在 10240 的预算下不划算。

## 回归测试

```powershell
node tools\crypto-test.mjs              # 12 项：加密通道、认证、防重放、完整性校验
node tools\encrypted-upload-test.mjs    # 17 项：加密上传往返（逐字节比对）+ 篡改拒绝
node tools\frontend-contract-test.mjs   # 30 项：前端依赖的每一处后端契约
node tools\crypto-browser-path-test.mjs # 11 项：浏览器那条加密路径
node tools\frontend-e2e-cdp.mjs         # 57 项：真浏览器端到端（需 Edge）
node tools\web-search-test.mjs          # 23 项：搜索 / 来源限定 / SSRF 五种地址 / 抓取入库
node tools\tree-test.mjs                # 11 项：聚类质量 / 概览进提示词 / 无关问题不召回
node tools\answer-style-test.mjs        #  8 项：回答三段式
node tools\history-index-test.mjs       #      旧轮次向量召回
```

### 关于「断言模型行为」的测试

`tree-test` 和 `answer-style` 里有几条断言测的是**模型输出**（回答里有没有提到主题名、
有没有「知识库中没有」的措辞）。这类断言天然不确定 —— 实测失败率约三成。

**对策是重试，不是放宽判据**：不是被测对象出错，是测试方法不对。
重试前必须清回答缓存，否则第二次拿到的还是上一次的答案。

还有一类是**测试自己过期**：知识库会长大，凡假设「知识库没有 X」的用例都是定时炸弹
（踩过三次）。现在一律用虚构话题，并在注释里写明原因。

前两个用 Node 的 `crypto` 模块实现，其 RSA-OAEP(SHA-256) 语义与浏览器 WebCrypto
**完全一致**，等于提前验证了浏览器端能不能对上。

`crypto-browser-path-test.mjs` 更进一步：它把 `app.js` 里加密层的**源码原文**按标记
切出来、在 Node 里执行（Node 20+ 的 `crypto.subtle` 就是浏览器那套 WebCrypto）。
验的是将要在浏览器里跑的那段代码本身，而不是它的副本 —— 这样才能排掉
「Node 能通、浏览器不通」这类隐患（OAEP 的 hash、GCM 的标签位置、密钥导出格式，
任一处不匹配的表现都是整站白屏）。

`frontend-e2e-cdp.mjs` 用 CDP 驱动无头 Edge，走真实加密登录、发问、附件上传，
并捕获 console 报错与异常栈。**断言一律看计算样式而不是 `element.hidden`** ——
属性为 true 但 CSS 里写了 `display` 的元素照样显示，第一版就是查了属性，
把「登录成功但遮罩不消失」放了过去。

## 与 Python 版的关系

**外网已经指向 Java 版**（经 cloudflared → `127.0.0.1:8080`），2026-09-16 完成切换。
访问地址刻意不写进仓库 —— 本仓库是公开的，写出来等于把入口交给扫描器
（实测公开期间日志里滚过大量 `/.git/config`、`/wp-includes/...` 之类的探测）。

两套系统共用一个 PostgreSQL / Redis，切换只需改 cloudflared 的转发端口，可随时回退。
Python 版仍在 `127.0.0.1:8000` 上运行，作为对照与回退方案保留。

> 注意本机网络的限制：**IPv4 走不通**（校园网按 SNI 阻断），必须走 IPv6。
> `curl` 要加 `-6`，Node 要加 `NODE_OPTIONS=--dns-result-order=ipv6first`，
> 否则会在 TLS 握手阶段收到 ECONNRESET。这与本项目无关，是网络环境。

## 已知边界

- **框架级错误响应为明文**：404/405/500 由 Spring 的错误分发产生，绕过了响应包装器。
  只含状态码与路径，无业务数据。
- **上传大小受请求体上限约束**：加密上传是单请求整体传输，没有分片，
  上限受 Cloudflare 100MB 与原站记录数限制。当前配置 50MB。
- **本地上下文上限：配置写的是 10240，但实测还能更高**。2026-09-17 重测（判据：生成全速
  **且** 向量调用延迟 30~100ms 而非数千毫秒）：

  | num_ctx | 生成 tok/s（三轮） |
  |---|---|
  | 24576 | 91.8 / 104.8 / 103.8 |
  | 28672 | 91.5 / 102.5 / 102.0 |
  | 32768 | 95.7 → **10.4 / 10.2** |

  两个模型同时驻留时可用到 **24576~28672**，32768 才崩。崩的机制不是「层被甩到 CPU」
  （`ollama ps` 仍报 100% 驻留），而是显存容量墙。**墙的位置随桌面显存占用浮动** ——
  第一轮扫描曾据此得出过「32768 掉到 9 tok/s」的错结论（那次桌面多占了 0.35G），
  所以换机器或改桌面占用务必重测（`tools/joint-ceiling-v2-probe.py`）。

  配置仍保守停在 10240：把窗口提上去的收益是「每轮能多塞几段资料」，而资料段数的
  边际收益还没量过（`max-contexts: 12`）。长对话的正解仍是历史索引与摘要，不是硬撑窗口。
- **单会话内检索依赖指代解析**：规划器用最近 16 条原文与滚动摘要解析指代，
  所以指代对象若在很久以前且摘要没覆盖到，仍会召不回。
