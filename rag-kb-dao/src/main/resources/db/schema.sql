-- RAG 知识库表结构。全部幂等，与 Python 版保持一致 —— 两套系统共用一个库。
-- 向量维度 1024 对应 bge-m3；换向量模型需改这里并重建 chunks 表。

CREATE TABLE IF NOT EXISTS users (
    id            text PRIMARY KEY,
    username      text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE IF NOT EXISTS documents (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    suffix      text NOT NULL,
    bytes       bigint NOT NULL,
    stored_path text NOT NULL,
    uploaded_at timestamptz NOT NULL DEFAULT now(),
    status      text NOT NULL DEFAULT 'stored',
    chunk_count integer NOT NULL DEFAULT 0,
    embed_model text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chunks (
    id          bigserial PRIMARY KEY,
    doc_id      text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    content     text NOT NULL,
    embedding   vector(1024),
    embed_model text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_id, seq)
);

-- 来源信息：区分「用户自己传的」与「联网抓回来的」。
--
-- 为什么网页内容必须记抓取时间：它会过期。同一条结论，三年前抓的和昨天抓的
-- 可信度完全不同，而一旦入库、两者在检索结果里长得一模一样。
-- 回答引用网页来源时要带上时间，否则会把陈旧结论当成现行事实。
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'upload';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS source_url  text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS fetched_at  timestamptz;

CREATE INDEX IF NOT EXISTS chunks_doc_idx   ON chunks (doc_id);
CREATE INDEX IF NOT EXISTS chunks_model_idx ON chunks (embed_model);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS conversations (
    id         text PRIMARY KEY,
    title      text NOT NULL DEFAULT '新对话',
    provider   text,
    model      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- 会话滚动摘要：历史索引漏召时的兜底。
--
-- 为什么需要它：向量检索只给 top-k，召不回就丢了，而且**没有第二次机会**。
-- 指代与省略尤其致命 —— 用户问「那它呢」，检索很可能匹配不上定义「它」的那一轮，
-- 因为那轮的文本里根本没有「它」这个词。摘要覆盖全部历史，粗糙但不会完全丢。
--
-- summary_upto 记录摘要覆盖到哪条消息（messages.id）。它是增量的关键：
-- 每次只把「新掉出最近窗口」的那几条并入已有摘要，而不是重读整个会话 ——
-- 否则长对话每轮都要重算一遍，成本随轮数线性增长。
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary      text;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS summary_upto bigint NOT NULL DEFAULT 0;

-- ctx：语境行（一句「这段能回答什么问题」），**只进检索索引、不进提示词**。
-- 嵌入时是「语境行 + 换行 + 正文」；展示与引用仍用 content 原文。
-- 为什么加：难题集实测，问句用症状词、文档用机制词，向量空间里够不着
-- （靶子排在 rank 50）；加上用提问者说法写的语境行后回到 rank 28，难题 13/14 → 14/14。
-- 见 tools/contextual-retrieval-probe.py 与 ChunkContextService。
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS ctx text;

CREATE TABLE IF NOT EXISTS messages (
    id         bigserial PRIMARY KEY,
    conv_id    text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role       text NOT NULL,
    content    text NOT NULL,
    sources    jsonb,
    provider   text,
    model      text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conv_idx ON messages (conv_id, id);

-- 历史索引：旧轮次不再整段丢弃，而是向量化后按需召回。
--
-- 为什么值得：KV 缓存每 token 约 147KB，而向量每 600 token 只要 4KB —— 差约 2.2 万倍。
-- 100 轮对话全量进上下文要 6.2GB 显存（8GB 卡物理上不可能），而索引只要 0.3MB。
-- 真正省下的是「没被召回的那绝大部分」：召回回来的那几轮照样付全额的 KV。
--
-- 刻意不给 embedding 建 HNSW 索引：检索永远带 conv_id 过滤，单个会话至多几百条，
-- messages_conv_idx 上的顺序扫描是微秒级；HNSW 在这种规模反而更慢且要维护。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS embedding   vector(1024);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS embed_model text NOT NULL DEFAULT '';

-- 轮次笔记：把一轮对话改写成自包含的一段话，索引**它**而不是原文。
--
-- 为什么需要：单条 message 不是好的检索单元。
--   · 单独索引用户那句 —— 常常很短、带指代（「那它呢」），当检索键很差
--   · 单独索引助手那句 —— 脱离问题可能没头没尾（「它的失效策略有三点…」）
--   · 助手回答里还夹着「根据参考资料[1]」「以下为通用知识」这类包装，
--     检索时稀释向量、阅读时是噪音
--
-- 改写成「问：<去指代> 答：<结论>」之后才是自包含、可独立检索的单元。
-- 原文保留在 content 列不动 —— 它是会话记录，改了就没有可追溯性。
ALTER TABLE messages ADD COLUMN IF NOT EXISTS index_text  text;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS index_topic text;

-- 主题树：把全部块聚成若干主题簇，每簇一段摘要。
--
-- 解决什么：扁平的 top-k 只给固定的几块，语料一大就**看不出全局** ——
-- 检索不到时只能回一句「知识库中没有」，而说不出「没有 X，但有 Y 和 Z 两个
-- 相关方向」。有了主题层，回答可以先给出知识库的覆盖范围，再落到细节。
--
-- 为什么只做两层（根 + 主题簇）不做深树：深树的收益来自「逐层收窄」，
-- 而那需要每下降一层付一次 KV —— 在 10240 的上下文预算下不划算。
-- 两层的收益（全局概览 + 粗筛）已经拿到了绝大部分。
--
-- chunk_ids 是该簇覆盖的块，检索时可以据此把搜索范围收窄到相关主题 ——
-- 这是「先粗后细」里「粗」那一步的实际作用。
CREATE TABLE IF NOT EXISTS tree_nodes (
    id         text PRIMARY KEY,
    label      text NOT NULL,
    summary    text NOT NULL,
    chunk_ids  bigint[] NOT NULL DEFAULT '{}',
    -- 簇的质心向量（k-means 算出来的那个均值）。检索时拿问题向量与它比距离，
    -- 就能判断「这个问题最可能落在哪个主题」，而不必把全簇的块都捞出来算。
    centroid   vector(1024),
    doc_ids    text[]   NOT NULL DEFAULT '{}',
    size       integer  NOT NULL DEFAULT 0,
    built_at   timestamptz NOT NULL DEFAULT now()
);

-- 建树时全库有多少块。与当前块数一比就知道树是否过期 ——
-- 语料一直在长，树不重建就会越来越不准，而它「不准」的表现是静默的
-- （回答仍然能出，只是覆盖范围描述落后于实际）。
CREATE TABLE IF NOT EXISTS tree_meta (
    id            integer PRIMARY KEY DEFAULT 1,
    chunk_count   integer NOT NULL DEFAULT 0,
    cluster_count integer NOT NULL DEFAULT 0,
    built_at      timestamptz,
    CONSTRAINT tree_meta_single CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS index_tasks (
    id         text PRIMARY KEY,
    doc_id     text NOT NULL,
    kind       text NOT NULL DEFAULT 'index',
    status     text NOT NULL DEFAULT 'running',
    progress   integer NOT NULL DEFAULT 0,
    total      integer NOT NULL DEFAULT 0,
    message    text,
    result     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS index_tasks_doc_idx ON index_tasks (doc_id, created_at DESC);

-- 缓存三表。向量缓存刻意用 text 存而非 vector 类型：
-- 缓存只按 key 精确查、从不做相似度检索，用 text 就不被 embed_dim 的 DDL 绑死。
CREATE TABLE IF NOT EXISTS cache_embeddings (
    key         text PRIMARY KEY,
    model       text NOT NULL,
    vec         text NOT NULL,
    dim         integer NOT NULL,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

CREATE TABLE IF NOT EXISTS cache_answers (
    key         text PRIMARY KEY,
    question    text NOT NULL,
    answer      text NOT NULL,
    provider    text,
    model       text,
    sources     jsonb,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

CREATE TABLE IF NOT EXISTS cache_parses (
    key         text PRIMARY KEY,
    name        text,
    text        text NOT NULL,
    chars       integer NOT NULL,
    hits        integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_hit_at timestamptz
);

-- 长期记忆：跨会话存活的事实（偏好、约定、已定下来的决定）。
--
-- 为什么需要它：在此之前"记忆"只有 conversations.summary —— 按会话隔离，
-- 这次对话里说定的约定，换个会话就没了。
-- 形状**沿用摘要那一套**（`主题：曾经 → 现在`），于是摘要服务的机械件
-- （解析 / 着落判据 / 覆盖边 / 只增不删）原样可用，不是另起一套。
--
-- **topic 是主键** —— 一条主题只留一行，覆盖时改这一行而不是追加：
-- 追加会让同一个主题堆出十几行，而"当前值"要靠时间戳去猜。
CREATE TABLE IF NOT EXISTS memory_items (
    topic      text PRIMARY KEY,
    item       text NOT NULL,
    src_conv   text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE memory_items IS '跨会话的长期记忆，一行一条（主题：曾经 → 现在）';
COMMENT ON COLUMN memory_items.src_conv IS '最早写下这条的会话，便于追溯';

-- 句子级索引：把每块切成**带字符区间的句子**，给它们一个可在提示词里引用的地址。
--
-- 为什么需要它（2026-09-23）：把思考按句切开量过 —— **中位 38% 的字是在复述
-- 已经在提示词里的资料**（`tools/think-structure-read.py`）。而"禁止复述"这条
-- 提示词试过了、不通（思考反而 +29%，符号检验 p=0.007）：**光禁止，模型会换个
-- 方式做同一件事**。改成"换任务"才有希望 —— 让模型输出**地址**（用哪几句），
-- 正文由**代码按地址原样拼**。于是句子必须可寻址。
--
-- 为什么区间要存 char_start/char_end：展开时必须**逐字原样**取出，
-- 而不是靠模型或靠"再切一遍" —— 再切一遍就可能与当初切的不一致。
--
-- 为什么存 sent_hash：本项目吃过"一切按位置对齐的缓存会随重建静默错位"的亏
--（向量缓存那次，661 条里 438 条错位且不报错）。**按内容锚定**是这里的纪律。
--
-- ⚠️ 地址（供模型引用）用 **块内序号 `[3.2]`**（第 3 段的第 2 句），
-- 因为那个地址**只需在这一次提示词里唯一**；全局 id 只在库内用。
CREATE TABLE IF NOT EXISTS sentences (
    id          bigserial PRIMARY KEY,
    chunk_id    bigint  NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    doc_id      text    NOT NULL,
    seq         integer NOT NULL,          -- 块内第几句，1 起
    char_start  integer NOT NULL,          -- 在 chunks.content 里的半开区间
    char_end    integer NOT NULL,
    kind        text    NOT NULL DEFAULT 'sent',   -- sent/table/code/head
    text        text    NOT NULL,
    sent_hash   text    NOT NULL,          -- sha1(去空白后的正文)[:12]
    stamp       text    NOT NULL,          -- 建这份索引时的**语料戳**
    built_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (chunk_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_sentences_doc   ON sentences (doc_id);
CREATE INDEX IF NOT EXISTS idx_sentences_hash  ON sentences (sent_hash);
CREATE INDEX IF NOT EXISTS idx_sentences_chunk ON sentences (chunk_id);

COMMENT ON TABLE sentences IS '句子级索引：带字符区间的可寻址切片，供"地址计划→代码展开"使用';
COMMENT ON COLUMN sentences.stamp IS '建索引时的语料戳；与当前语料戳不一致即为陈旧，需重建';

-- 句子向量：**分层注入**用（块召回 → 块内按问题对句子排序 → 只注入那几句）。
--
-- 为什么要有它（2026-09-23 实测）：
--   · `tools/sent-recall.py` 的分层臂：块 top-12 → 句 top-20，**靶子内容保留 30.6%**、
--     精度 36.8%（整块注入是 21.9%）、每题 1416 字（整块是 6339 字）
--   · 而**答案只覆盖被引用块内容的 14%（中位）/ 31%（90 百分位）**（438 个样本）
--     ⇒ 保留三成，覆盖九成题目的需要
--   · 收益两条线：prefill −0.7s；**decode 速率 75.7 → 84.5 tok/s**
--     （实拟合 `速率 ≈ 94.9 − 0.00338 × 装入token`，r = −0.97）⇒ 合计约 −2.9s/题
--
-- 不建 HNSW 索引：查询是 `WHERE chunk_id = ANY(...) ORDER BY embedding <=> q LIMIT M`，
-- 候选只有一两百句（一次问答注入的块里的句子），顺扫就够；而带过滤的 HNSW 反而容易走不到索引。
ALTER TABLE sentences ADD COLUMN IF NOT EXISTS embedding   vector(1024);
ALTER TABLE sentences ADD COLUMN IF NOT EXISTS embed_model text NOT NULL DEFAULT '';
