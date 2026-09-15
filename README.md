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
| `rag-kb-security` | Spring Security + JWT + Redis 会话 + 加解密过滤器 |
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
