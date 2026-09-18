# RAG Knowledge Base · Java Edition

**English** | [简体中文](README.md)

A Java rewrite of a personal RAG knowledge base. It runs **side by side with**
[the Python edition](https://github.com/kniv001/RAG-KB/tree/python) on the same
PostgreSQL / Redis; switching between them is a one-line change to the
cloudflared forwarding port.

**Status**: feature-complete and in daily use. Browser UI, Agentic RAG,
web search, and a topic tree are all running behind a Cloudflare tunnel.

| Capability | Notes |
|---|---|
| Frontend | Single-page chat UI, no framework and no build step; file attachments, streaming, math rendering |
| Retrieval | Vector + keyword with RRF fusion; an agent plans, assesses and retries |
| Conversation | Persistent multi-turn; vector recall of older turns; line-per-fact summary; per-turn notes |
| Knowledge base | Upload files or fetch from the web; a topic tree reports what the corpus covers |
| Encryption | End-to-end (a fresh AES key per request, RSA-wrapped) — uploads included |
| Auth | Dual tokens (JWT + opaque refresh held in Redis) |

## Stack

| Layer | Choice |
|---|---|
| Runtime | Java 21 (LTS) |
| Framework | Spring Boot 3.5.16 |
| Build | Maven 3.9.11 (multi-module) |
| Persistence | PostgreSQL 18.6 + pgvector, MyBatis-Plus 3.5.17 |
| Cache / sessions | Redis |
| Auth | Spring Security + JWT + bcrypt |
| Transport encryption | RSA-OAEP(SHA-256) + AES-256-GCM hybrid |

> Spring Boot 4.x is out, but MyBatis-Plus's starter is still anchored to Boot 3
> (`mybatis-plus-spring-boot3-starter`), so this project stays on 3.5.16.

## Module layout

Dependencies are **strictly one-directional**:

```
common ← domain ← dao ← service ← web
                    ↖ security ↙
                    ↖ provider ↙
```

| Module | Responsibility |
|---|---|
| `rag-kb-common` | Response envelope `R<T>`, business exceptions, utilities, config properties |
| `rag-kb-domain` | Entities / DTOs / VOs (depends only on `mybatis-plus-annotation`, not the autoconfiguration) |
| `rag-kb-dao` | Mappers, pgvector type mapping, schema script |
| `rag-kb-security` | Spring Security + JWT dual tokens + Redis sessions + crypto filter |
| `rag-kb-provider` | Model provider abstraction (local Ollama / any OpenAI-compatible endpoint) |
| `rag-kb-service` | RAG pipeline, conversations, three-tier cache, background tasks |
| `rag-kb-web` | Entry point + controllers (the only runnable module) |

## Build and run

```powershell
$env:JAVA_HOME = 'C:\Program Files\Java\jdk-21'
$mvn = 'C:\Users\kniv\tools\apache-maven-3.9.6\bin\mvn.cmd'

& $mvn -f pom.xml -B -DskipTests clean package     # produces rag-kb-web/target/rag-kb.jar

# The DB password never enters the repo; it is read from the Python edition's credential file
$env:KB_DB_PASSWORD = (Get-Content 'D:\vs\rag-kb\data\pgapp.txt' -Raw).Trim()
java -jar rag-kb-web\target\rag-kb.jar
```

> **Stop the running app before packaging.** Otherwise `clean` cannot delete the
> in-use `rag-kb.jar` and reports "the file is being used by another process".
> The message looks like a compile error but has nothing to do with one.
>
> **An open port does not mean a working service.** Without `KB_DB_PASSWORD` the app
> still starts and the home page still returns 200, but schema init fails with
> `SCRAM-based authentication, but no password was provided` and every later database
> call fails too. After starting a new process, check `/actuator/health` — both the
> `db` and `redis` components must be `UP`; don't trust the port alone. (Hit this while
> restarting via `Start-Process`, whose parent shell lacked the variable.)
>
> Maven lives at `C:\Users\kniv\tools\apache-maven-3.9.6`. Note that it is **only
> on Git Bash's PATH** (added by the shell profile) — typing `mvn` in PowerShell
> fails with "not recognized as a cmdlet". `~/.m2/settings.xml` points at an
> Aliyun mirror (with the mirror id set to `central`, so existing local cache is reused).

Web search is off by default; enable it with an environment variable:

```bash
KB_WEB_ENABLED=true java -jar rag-kb-web/target/rag-kb.jar
```

Other environment variables: `KB_PROMPT_WINDOW` (prompt window, default 16384),
`KB_STORAGE_ROOT`, `KB_KEY_DIR`, `KB_DEEPSEEK_KEY`.

The service listens on `127.0.0.1:8080` only. Everything external goes through a
Cloudflare tunnel; no inbound port is ever opened.

## Auth: dual tokens

| Token | Form | TTL | Stored in | Revocable |
|---|---|---|---|---|
| Access | JWT (HS256) | 30 min | Frontend memory | ❌ expires on its own |
| Refresh | 32-byte random string | 30 days | Redis + httpOnly cookie | ✅ deleted on logout / password change |

| Endpoint | Purpose |
|---|---|
| `POST /api/auth/login` | Verify password → return access token, set refresh cookie |
| `POST /api/auth/refresh` | Issue a new access token and **rotate** the refresh token |
| `POST /api/auth/logout` | Delete the Redis record and clear the cookie |
| `POST /api/auth/password` | Change password and revoke all of that user's sessions |
| `GET /api/auth/me` · `GET /api/auth/config` | Current user / public login-page parameters |

Failed logins are rate-limited to 10 per 15 minutes. A nonexistent username still
runs one hash, removing the timing side channel. A 401 body carries an `expired`
flag so the frontend knows whether to refresh or redirect to login.

### Sharing the `users` table with the Python edition (important)

Both systems connect to the same database, so password hashes must match
byte-for-byte. To sidestep bcrypt's 72-byte limit, the Python edition **hashes
with SHA-256 first and hands the raw digest to bcrypt**. Java's
`BCryptPasswordEncoder` hashes the password string directly — different input,
so the same password produces different hashes on the two sides.

Hence [`Sha256BcryptEncoder`](rag-kb-security/src/main/java/com/kniv/ragkb/security/Sha256BcryptEncoder.java)
reproduces that scheme, and must use the `BCrypt.hashpw(byte[], salt)` overload —
the raw digest contains non-UTF-8 bytes that the String API would corrupt by
re-encoding. Stored values look like `$2b$12$...`, with no `{bcrypt}` prefix.

## Model providers

Local and cloud share one interface and can be switched at runtime. References
are written `providerId/model` — a slash rather than a colon, because model names
themselves often contain colons (`qwen3:8b`), which would be ambiguous.

| Endpoint | Purpose |
|---|---|
| `GET /api/provider/list` | Configured providers + default models (API keys report only whether they are set) |
| `GET /api/provider/{id}/probe` | Probe availability and list the models actually present |
| `POST /api/provider/chat` | Synchronous chat |
| `POST /api/provider/chat/stream` | **SSE streaming**, encrypted per event |
| `POST /api/provider/embed` | Embeddings |

Adding a provider (DeepSeek / Qwen / Moonshot / Zhipu / SiliconFlow / OpenAI /
vLLM / LM Studio) is a config stanza in `application.yml`, no code change.

It uses the JDK's built-in `java.net.http.HttpClient`: `BodyHandlers.ofLines()`
reads line-by-line natively, so NDJSON (Ollama) and SSE (OpenAI-compatible) both
need zero dependencies — no reactive stack just for streaming.

### Streaming versus encryption (design note)

The filter's response encryption is "buffer the whole response, then encrypt",
which directly conflicts with what makes SSE valuable: **pushing tokens as they
are generated**. So streaming paths (default `/**/stream`) **decrypt the request
only, and the controller encrypts each event itself**. The filter hands the AES
key to the controller through a request attribute, and the controller zeroes it
when the stream ends.

> The pattern must be `/**/stream`, not `**/stream`: `AntPathMatcher` splits on
> `/`, and a request URI beginning with `/` produces a leading empty segment.
> Omitting the leading slash **fails to match silently** — the symptom is
> "the streaming endpoint returns `application/json` with a single body".

### ASYNC dispatch and SecurityContext

SSE is an async request, and when the stream ends the container performs one more
**ASYNC dispatch** to the same path. `OncePerRequestFilter` skips async dispatches
by default, so the JWT filter does not run the second time — and since Spring
Security 6 no longer saves the SecurityContext automatically, that second pass
sees an unauthenticated request: "the stream runs fine, then suddenly 500 /
Access Denied at the end".

The fix is `RequestAttributeSecurityContextRepository`, which stores the context
in a **request attribute** that survives the async dispatch.

## End-to-end encryption

### Why

**Cloudflare terminates TLS, so Cloudflare can read every plaintext byte it
forwards.** Adding a layer of RSA + AES hybrid encryption at the application
level means the wire (Cloudflare included) only ever sees ciphertext.

### Boundaries you must understand

> **The frontend JS itself is also served by Cloudflare.** So this layer stops
> an intermediary from *reading*, but not from *tampering* (swapping the JS).
> True end-to-end requires a client you control — a desktop app or a mobile app.
> That is the same path the planned "rendering + streaming" work would take.

### Protocol

| Header / body | Contents |
|---|---|
| `X-Enc-Key` | `base64( RSA-OAEP(aesKey) )` — the AES key is **generated fresh per request** |
| `X-Enc-Meta` | `base64( {"iv","d"} )`, plaintext `{"ts":ms,"nonce":"..","token":".."}` |
| Request body | `{"iv","d"}`, plaintext is the original request JSON (only when the method has a body) |
| Response body | `{"iv","d"}`, same AES key, **new IV** |

**Why the token travels inside the encrypted meta header**: encrypting only the
body would leave the `Authorization` header in plaintext, and an intermediary
could simply use that token to call the API — the encryption would buy nothing.
Putting it in the meta header also makes GET/DELETE uniform with the rest.

**Why a fresh key per request**: the server never has to store client keys. It is
stateless, scales horizontally, and a Redis compromise reveals no session keys.

**Replay protection**: both `ts` and `nonce` live **inside the ciphertext** (in a
header they could be tampered with). The time window is ±5 minutes and the nonce
is recorded in Redis with a 10-minute TTL.

### Interop trap (solved)

Java's `RSA/ECB/OAEPWithSHA-256AndMGF1Padding` **uses SHA-1 for MGF1 by
default**, whereas the browser's `RSA-OAEP(hash=SHA-256)` uses SHA-256 for both
the digest and MGF1. Misaligned, they can never decrypt each other. The code
builds the `OAEPParameterSpec("SHA-256","MGF1",SHA256,...)` explicitly.

AES-GCM ciphertext layout is identical on both sides (`ciphertext‖tag`), so no
conversion is needed.

### Encrypted file upload (solved)

A multipart body exposes its boundaries, headers and filename in plaintext.
After terminating TLS, Cloudflare can reconstruct both the file and the token —
precisely what this layer exists to prevent. So rather than rework multipart, the
project **added a separate path**:

```
POST /api/docs/upload-encrypted
Content-Type: application/octet-stream
X-Enc-Key    RSA-OAEP-wrapped AES-256 key
X-Enc-Meta   encrypted {ts, nonce, token, iv, name, size}
body         the file ciphertext itself (ciphertext‖tag)
```

**The filename and file IV go in the encrypted metadata**, not in headers —
in a header they would not be encrypted at all.

Two implementation notes:

- **The body never enters memory.** Reading it all in to decrypt would cost
  100 MB of heap for a 50 MB file. The filter only hands the key and metadata to
  the controller (`ATTR_AES_KEY` / `ATTR_META`) and passes the body through
  untouched; the controller reads, decrypts and writes in a loop, peaking at a
  single 64 KB buffer.
- **Write to a temporary `.part` file, and only rename it into place once the
  authentication tag verifies.** Otherwise a request with a bad tag leaves half
  a file on disk that looks exactly like a good one. `CipherInputStream` is
  deliberately avoided here — it has a long-standing AEAD pitfall (JDK-8012631)
  where a failed tag can be swallowed and the output silently truncated. An
  explicit `update`/`doFinal` loop makes a bad tag throw at `doFinal`.

The old multipart endpoint is left untouched so any existing client keeps working.

## Agentic RAG

Plan → retrieve → assess → (re-plan if insufficient) → generate. The agent is
only control flow: **the facts always come from retrieved material**, enforced by
three hard constraints — the only available action is "retrieve" (no network, no
file writes); when material is insufficient it says so rather than improvising;
and every answer cites sources by index.

**Planning and assessment use structured output** (thinking disabled + a
grammar-constrained JSON schema). This is the single biggest win in the project:

| | plan | assess |
|---|---|---|
| Off | 51.5 s | 31.4 s |
| On | **0.94 s** | **1.6 s** |

The cause was that the model generated 4561 tokens of reasoning to emit a
three-line JSON. **Both switches are required**: disabling thinking alone makes
the reasoning leak from the `thinking` channel into `content` and shatter the
JSON; the grammar constraint alone still lets the model finish thinking first.

**Three-tier answers** (`ANSWER_SYSTEM`): with material, answer strictly from it;
without material, do not just say "not found" — state that the knowledge base
lacks it, then answer from general knowledge and mark the boundary; small talk
and identity questions are exempt from knowledge-base rules. The marking is
strict because it is the system's **only trust boundary** — the user must be able
to tell at a glance which sentences come from their own material and which are
the model's general knowledge.

## Ingesting documents

Two entry points, and **text extraction takes a different route in each**:

| Entry point | Accepts | Extraction |
|---|---|---|
| Upload | `.txt` `.md` `.markdown` `.pdf` `.docx` `.csv` `.json` `.html` `.htm` | Dispatched by extension: PDF→PDFBox, DOCX→POI, CSV→rows joined as "header \| value", JSON kept as-is, **HTML→tag stripping**, unknown extensions treated as plain text (no error — a `.log` or `.conf` should not be rejected). PDFBox and POI are heavyweight, so **they are only loaded when a file of that format actually shows up** |
| Web fetch | any page | **Walks the DOM and emits Markdown** (`WebSearchService.toMarkdown`): h1–h6 → `#`…`######`, lists → `- `, tables → `\| `, fenced code blocks preserved |

**Why fetching now emits Markdown** (landed 2026-09-17): it used to flatten the DOM with
`main.text()`, which threw the heading hierarchy away on the spot — across 41 ingested
documents only **64 heading lines** could be counted, and most of those were the title the
ingest itself adds. The cost was not cosmetic: the three later attempts to **recover
structure from the corpus** (model chapters / model merging / k-means merging) all failed,
because **the structure was already gone at extraction time** — the right move is to take it
for free while extracting, not to reconstruct it afterwards.

Result: the same blog post re-fetched came in at **7161 characters with 59 headings**. This
only applies to newly fetched documents; older ones stay flattened.

> **Known asymmetry**: uploaded `.html` still goes through tag stripping (`<[^>]+>` → space),
> losing structure the same way — inconsistent with the fetch path. To close it, swap
> `DocumentParser.fromHtml` for a DOM walk as well.

**Indexing pipeline**: parse → chunk (next section) → **context line** → embed → store, with
caching at every step — parsing is cached by **file content hash**, context lines by
"document name + chunk text", and vectors by **text + model hash** (so a full reindex is nearly
instant). All three cache keys include every parameter that affects the result, so stale data
cannot come back.

**Context lines** (`ChunkContextService`): at ingest each chunk gets one sentence answering
**"what question does this chunk answer?"**, and that sentence is **prepended to the embedded
text only — it never enters the prompt**. What the answer displays and cites is still the
original text: verbatim traceability is this system's one trust boundary, and the indexing
layer must not rewrite it.

Why it exists: on 2026-09-18 the hard-question benchmark found a real failure — questions use
**symptom words** ("how do I keep a service pinned to one machine") while documents use
**mechanism words** ("node affinity", "scheduler"), and the two never meet in embedding space:
that target sat at **rank 50**, and widening recall does not help (you would have to go to 50).
With context lines the target returns to **rank 28** and the hard set goes **13/14 → 14/14**.

Two wordings are experiment-determined and **must not be changed**: ask "what does this
**answer**" rather than "what does this say" — the latter only echoes the document's
terminology, which lifts precision but leaves the hard failures unfixed; and **meta information
must be forbidden** ("written by …", "fetched at …" — measured at 9% of lines, worthless for
retrieval yet occupying index space). The free option was tried: prepending the document title
**does nothing at all** (titles carry mechanism words too). Degenerate output (nothing but an
ellipsis or filler) is treated as "not generated"; the index is still built.

Cost: one 4B call per chunk at ingest (≈20 minutes for 661 chunks, in the background);
**zero cost at query time**.

**Ingesting does not rebuild the topic tree** — after the corpus changes, trigger it manually
(`POST /api/tree/build`, or run `tools/tree-rebuild.mjs`), otherwise the overview still reports
the cluster and chunk counts of the old corpus.

## Chunking

600-character target / 80-character overlap, but **boundaries are aligned to
sentences**, not to character positions.

Oversized paragraphs are split into sentences and packed; adjacent chunks overlap
by **whole sentences**. Only a single sentence longer than the target (code
blocks, unpunctuated runs) falls back to character splitting.

Why we bother: the share of chunks that **start mid-sentence dropped from 75% to
13%** (six documents compared; four went to 0%). A chunk that starts mid-sentence
has dangling references and incomplete meaning —— it is noise for retrieval and a
direct cause of extraction failures. The same passage, aligned to sentence
boundaries, extracted cleanly three times out of three with identical counts
(22/22/22); the character-cut version came up empty once in three.

**A newline is not automatically a boundary**: this corpus was fetched from the
web, and 75% of its lines are hard-wrapped (one sentence folded across several
lines). The rule is "a line break is a boundary when either side has no CJK" ——
code and output lines contain no CJK (75% of lines sampled), so they are natural
units; Chinese prose keeps its wrapping and is not split.

> This boundary rule **differs from the Python edition** (still character-cut).
> The two share one database, so mismatched chunking makes the same document
> retrieve differently on each side.

## How the context budget is spent

This is the most carefully tuned part of the project; every number below was
measured (see [docs/ollama-tuning.md](docs/ollama-tuning.md), in Chinese).

**The window is 16384 tokens.** Exceeding it is not graceful degradation — it is
a **cliff**: Ollama resets the entire context and **drops the beginning**, which
is the system prompt. The result is not "a worse answer" but "an answer bound by
nothing", while the model returns something that looks perfectly normal and
reports no error. So the application layer runs a `PromptBudget` that trims by
priority: oldest history → recall excerpt → retrieved material (material goes
last; it is the factual basis).

> **The summary and the recall excerpt are dropped together.** The summary used to
> go first; that changed on 2026-09-18. Recalled excerpts are verbatim text or
> notes — they have **no notion of "which one is newer"** and may carry a value
> that was later changed, while the summary is exactly the tier that holds current
> values. Measured (`tools/stale-recall-probe.py`): with a stale excerpt and **no**
> summary the model served the stale value **3/3 times**; with the summary present
> it recovered **3/3 times**. Cost: about 400 tokens.

**A measured Q&A turn costs 2.6K–6.1K tokens** (16%–37% of the window). The fixed
part is about 1100: system prompt 530 + topic overview 440 + question. The rest is
material (up to 24 chunks) and recent history (up to 16 messages).

**Three tiers of history, in decreasing reliability**:

| Tier | Contents | How it is obtained |
|---|---|---|
| Recent window | Verbatim text | Taken directly, no retrieval involved |
| Older turns | Vector-recalled excerpts | See "Query ordering" below |
| Summary | **One line per entry, each written as "was → now"** | Merged once per 6 messages |

**How the summary is written — all three rules are measured** (`tools/summary-*.py`):

- **One line per entry, each shaped "was → now".** The point is not the arrow; the
  **shape itself is a completeness constraint**: forcing both sides to be filled in
  turns "facts that did not change" into an explicit `— → now`. With the earlier
  free-form wording the model silently dropped unchanged facts — in three repeated
  runs it lost the same user preference twice.
- **A merge must not end up with fewer entries than it started with.** Merging is
  **bimodal**: given identical input (8 existing facts + the same new dialogue) five
  times in a row, the survivor counts were `[7, 7, 2, 8, 2]` — when it collapses it
  loses **6 facts at once**, always the `— → now` ones. So on shrinkage it retries,
  up to three times, keeping the entry-richest attempt (a healthy merge needs one call).
- **Numbers get a mechanical check.** The local 4B model replaces the last digit of
  certain numbers with a colon (`600 → 60:`, `16384 → 1:16384`) — **value-specific,
  deterministic, and unfixable by prompting**. After merging, every number in the
  summary must be findable in the input; if not, it logs a warning (log only, the
  text is left alone). Known gap: **isolated single-fact loss** (8 entries, one goes
  missing) is invisible to that check.

**Query ordering is the crux**: recall must happen *after* planning. The planner
rewrites "what about that?" into a self-contained query, and only that key can
retrieve the turn that defines "that" — using the user's literal words means the
key is the four characters of "what about that?", and that turn contains no such
phrase. Planning itself does not need the excerpt: it has the last 16 messages
and the rolling summary.

**Turn notes**: turns outside the window are rewritten into self-contained notes,
and **the note is what gets indexed, not the original text**. A single message is
a poor retrieval unit — the user's line often carries pronouns, the assistant's
line often does not stand alone, and both are padded with filler like "according
to reference [1]". The rewriting is done by the **chat model**, not the embedding
model (the latter only turns a finished note into a vector).

## GPU scheduling

There is one GPU and Ollama has a single inference slot
(`OLLAMA_NUM_PARALLEL=1`), with FIFO queueing. Background work therefore slows
the user down **by its full duration**:

| Background task | User request |
|---|---|
| 1314 ms | 1164 ms |
| 3091 ms | 3095 ms |

A user request costs 192 ms on its own; the rest is queueing. (Corroborating
evidence: the user's prefill stays at a steady 36–39 ms — were it computing, that
number would rise too.)

`GpuGate` makes background work yield: it may only start when no user request is
in flight and the system has been quiet for 20 seconds, and it gives up after 300
seconds of waiting (background work is incremental; it will come around again).

**Whenever a new background task is added, measure it first with
`tools/gpu-contention-probe.mjs`.**

## Web search

Search → fetch → **ingest as a document**, rather than letting the agent browse
for answers directly.

The latter would break the hard constraint above: web content that never entered
the knowledge base, was never reviewed, and has no recorded provenance — and the
user cannot tell. Ingesting turns it into a document, so it travels exactly the
same path as a file the user uploaded: it is chunked, embedded and carries its
source. That keeps "every sentence has a citation" true even after browsing.

**The fetch keeps the page's structure** (changed 2026-09-17). It used to call
jsoup's `main.text()`, which **flattens the whole DOM into one run of text** ——
headings, paragraphs, lists and code blocks all lose their shape right there. The
cost was measurable: across the 41 documents already ingested, only 64 heading
lines survived, and most of those were the title line the ingest adds itself. So
"what sections does this document have" was unrecoverable from the store.

It now walks the DOM and emits Markdown: headings → `#`~`######`, paragraphs →
blank-line separated, lists → `- `, tables → `| `, `<pre>` → fenced verbatim.
Measured on two freshly fetched pages from the same site: one came in at 7161
characters with **59 headings** and 147 paragraphs, hierarchy intact
(`## 一、持久化的作用` / `### 1. 什么是持久化`).

> Known limitation: code blocks rendered by a JS highlighter (hljs / prism) are
> `<div>` rather than `<pre>` and are not recognised — their **text survives, the
> structure does not**. Documents already in the store cannot be fixed either:
> structure can only be captured at fetch time.

**Backends were chosen by measurement** (campus network, query "three-tier cache
architecture"):

| Backend | Result |
|---|---|
| Bing | ❌ splits "three-tier" into the single character "三" and returns a dictionary page (same with `mkt`, same on `cn.bing.com`) |
| Sogou | ✅ most accurate results, but its links are JS redirects that do not resolve, so ingestion cannot record a source |
| **360** | ✅ accurate, and the real URL is right there in the `data-mdurl` attribute |
| Baidu / DuckDuckGo / Brave / Jina / Wikipedia | ❌ anti-bot pages, or unreachable |

The default order is `so360 → bing`, tried in sequence. **Fetching is protected
against SSRF** — Redis on this machine listens on `0.0.0.0:6379`, so every fetch
first checks **all** addresses a hostname resolves to (checking only the first
would let a domain resolving to both public and private IPs slip through).

**Off by default** (`KB_WEB_ENABLED=true` to enable): this feature makes the
server initiate outbound requests, which differs from the "localhost only"
default posture and should be an explicit choice.

## Topic tree

All chunks are clustered into topic groups, each with a one-line summary. It
addresses "once the corpus grows, flat top-k loses the big picture" — without it,
a failed retrieval can only answer "the knowledge base has nothing", and cannot
say "not X, but there are two related directions, Y and Z".

Clustering is k-means++ with a fixed random seed (otherwise the same corpus
produces different clusters on every rebuild); the model only names and summarises
each cluster at the end. Two levels, not a deep tree: a deep tree pays off through
progressive narrowing, but every descent costs KV, which is not worthwhile inside
a 16384-token budget.

## Regression tests

```powershell
node tools\crypto-test.mjs              # 12 checks: crypto channel, auth, replay, integrity
node tools\encrypted-upload-test.mjs    # 17 checks: encrypted upload round-trip, byte-for-byte, plus tamper rejection
node tools\frontend-contract-test.mjs   # 30 checks: every backend contract the frontend depends on
node tools\crypto-browser-path-test.mjs # 11 checks: the browser's crypto path
node tools\frontend-e2e-cdp.mjs         # 57 checks: real browser end-to-end (needs Edge)
node tools\web-search-test.mjs          # 23 checks: search, source restriction, five SSRF addresses, fetch-and-ingest
node tools\tree-test.mjs                # 11 checks: cluster quality, overview reaching the prompt, no recall for unrelated questions
node tools\answer-style-test.mjs        #  8 checks: the three-tier answer style
node tools\history-index-test.mjs       #      vector recall of older turns
```

**Retrieval-quality baselines** (not self-tests — they measure rank against known answers;
run them before and after any retrieval change):

```powershell
python tools\recall-baseline-probe.py   # 15 single-hop questions: rank of the target chunk (baseline 15/15)
python tools\hard-query-probe.py        # 15 hard questions: same targets, phrased the way people ask (baseline 14/15)
```

**The hard set is the one that matters here**: the keyword-phrased questions ("how does the G1
collector work?") contain the document's own vocabulary, so 15/15 is inevitable — **it cannot
detect any retrieval improvement**. Rephrased the way a person actually asks ("how do I keep a
service pinned to one machine?"), it does discriminate: on 2026-09-18 it exposed the "symptom
words vs mechanism words" failure (target at rank 50) and then verified the context-line fix
(13/14 → 14/14).

One question is deliberately excluded from scoring: **"why does the cache blow up when I add
machines?"** — the two consistent-hashing documents contain **zero** chunks mentioning scale-up
and 4/6 about node death, i.e. **the corpus covers the other direction entirely**. It stays in
the list because it measures a **different** ability: when the corpus genuinely lacks the answer,
the reply should say so instead of forcing a retrieval. **Do not score it as a retrieval failure.**

The first two use Node's `crypto` module, whose RSA-OAEP(SHA-256) semantics are
**identical** to the browser's WebCrypto — verifying browser interop up front.

`crypto-browser-path-test.mjs` goes further: it slices the crypto layer's
**actual source text** out of `app.js` and runs it in Node (Node 20+'s
`crypto.subtle` *is* the browser's WebCrypto). It tests the code that will run in
the browser, not a copy of it — which is what rules out the "works in Node, fails
in the browser" class of bug (OAEP hash, GCM tag placement, key export format;
any one of them mismatched shows up as a blank page).

`frontend-e2e-cdp.mjs` drives headless Edge over CDP through a real encrypted
login, a real question, and a real attachment upload, capturing console errors and
exception stacks. **Assertions always read computed style, never
`element.hidden`** — an element with `hidden` set is still visible if any CSS
declares `display`, and the first version checked the attribute, which let
"login succeeds but the overlay never disappears" through.

### On tests that assert model behaviour

A few assertions in `tree-test` and `answer-style` test **model output** (did the
answer mention a topic name, did it use the phrase "not in the knowledge base").
Such assertions are inherently nondeterministic — observed failure rate around 30%.

**The remedy is a retry, not a looser criterion**: the subject under test is not
misbehaving, the test method is. A retry must clear the answer cache first, or
the second attempt just gets the first answer back.

There is also a class of **tests that expire on their own**: the knowledge base
grows, so any case assuming "the knowledge base has no X" is a time bomb (hit
three times). They now use fictional topics and say why in a comment.

## Relationship to the Python edition

**The public endpoint now points at the Java edition**
(via cloudflared → `127.0.0.1:8080`), switched over on 2026-09-16.
The address itself is deliberately kept out of the repo —— this repo is public,
and publishing the entry point just hands it to scanners (while it was public,
the logs rolled with `/.git/config`, `/wp-includes/...` style probes).

The two share one PostgreSQL / Redis, so switching back is just a change of the
cloudflared forwarding port. The Python edition still runs on `127.0.0.1:8000` as
a reference and fallback.

> Note the local network's constraints: **IPv4 does not work** (the campus network
> blocks by SNI), so IPv6 is required. `curl` needs `-6`; Node needs
> `NODE_OPTIONS=--dns-result-order=ipv6first`, or the TLS handshake fails with
> ECONNRESET. This is the environment, not the project.

## Known limitations

- **Framework-level error responses are plaintext**: 404/405/500 are produced by
  Spring's error dispatch, which bypasses the response wrapper. They contain only
  a status code and a path, no business data.
- **Upload size is bounded by the request-body limit**: encrypted upload is a
  single whole-body transfer with no chunking, so it is limited by Cloudflare's
  100 MB and the origin's record size. Currently configured at 50 MB.
- **Local context: configured at 16384, but measurably higher**. Re-measured on
  2026-09-17 (criterion: generation at full speed **and** embedding calls at
  30~100 ms rather than thousands):

  | num_ctx | generation tok/s (three runs) |
  |---|---|
  | 24576 | 91.8 / 104.8 / 103.8 |
  | 28672 | 91.5 / 102.5 / 102.0 |
  | 32768 | 95.7 → **10.4 / 10.2** |

  With both models resident, 24576~28672 is usable; only 32768 collapses. The
  collapse is not layers falling back to CPU (`ollama ps` still reports 100%
  resident) but a VRAM capacity wall. **That wall moves with the desktop's VRAM
  footprint** — an earlier scan concluded "32768 drops to 9 tok/s" on a run where
  the desktop happened to hold 0.35 GB more, so re-measure after changing machine
  or desktop load (`tools/joint-ceiling-v2-probe.py`).

  The configuration sits at **16384** (raised from 10240 on 2026-09-17, kept in
  lockstep with `provider.local.num-ctx`). Going up to 24576 leaves only 552 MiB
  of headroom on the card, and long conversations are handled by the history index
  and the summary — not by stretching the window.
- **Within-conversation recall depends on pronoun resolution**: the planner
  resolves pronouns from the last 16 messages plus the summary, so a
  referent that is both far back and uncovered by the summary can still be missed.

---

<sub>This translation tracks [README.md](README.md) as of 2026-09-17. When the
Chinese original changes, update this file too — a stale translation is worse
than none, because it is wrong without looking wrong.</sub>
