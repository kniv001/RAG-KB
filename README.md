# RAG 知识库 · Java 版

个人 RAG 知识库的 Java 重写。与 `D:\vs\rag-kb`（Python 版）**并行运行、互不干扰**，
两套连同一个 PostgreSQL / Redis，切换只需改 cloudflared 的转发端口。

**当前阶段**：骨架 + 端到端加密已可运行；RAG 业务功能待补。

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
$mvn = 'C:\Program Files\JetBrains\IntelliJ IDEA 2026.1\plugins\maven\lib\maven3\bin\mvn.cmd'

& $mvn -f pom.xml -B -DskipTests clean package     # 产出 rag-kb-web/target/rag-kb.jar

# 数据库密码不进仓库，从 Python 版的凭据文件读
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()
java -jar rag-kb-web\target\rag-kb.jar
```

> Maven 用的是 IntelliJ 自带的，免安装。`~/.m2/settings.xml` 已配阿里云镜像
> （镜像 id 设为 `central`，以复用你已有的本地仓库缓存）。

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

### 已知缺口

- **文件上传未加密**：multipart 需要单独的分片加密方案，目前只依赖 TLS + 令牌。
  内容最大的恰恰是文件，这是下一步要补的重点。
- **框架级错误响应为明文**：404/405/500 由 Spring 的错误分发产生，绕过了响应包装器。
  只含状态码与路径，无业务数据。

## 回归测试

```powershell
node tools\crypto-test.mjs        # 12 项：加密通道、认证、防重放、完整性校验
```

用 Node 的 `crypto` 模块实现，其 RSA-OAEP(SHA-256) 语义与浏览器 WebCrypto **完全一致**，
等于提前验证了浏览器端能不能对上。

## 与 Python 版的关系

Python 版仍在线上服务（`https://rag-kb-awa.xyz`，经 cloudflared → `127.0.0.1:8000`）。
Java 版达到功能对等后，把隧道的转发目标改成 `127.0.0.1:8080` 即完成切换，可随时回退。
