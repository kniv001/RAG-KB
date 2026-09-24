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

-- ─────────────────────────────────────────────────────────────────────────────
-- 信息流方向（`news` 分支）：周期性抓取的新闻/热搜等外部信息
--
-- 与 documents/chunks 的关系是**并存**，不是替代：
--   feed_items  是"抓到的东西"原始落点（先过闸、先去重、先记权重）
--   documents   是"值得进知识库的东西"——由 feed_items 里**过闸且不重复**的那些转过去
-- 这样切的原因：抓取量远大于值得入库的量，而**丢弃是不可逆的**（权重估计一定会错，
-- 校准来源权重又需要保留本该丢的样本）⇒ 所以这里的原则是
-- **「入库但不召回」而不是「不入库」**：status 决定可见性，行本身一律留下。
-- ─────────────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS feed_sources (
    id          bigserial PRIMARY KEY,
    domain      text NOT NULL UNIQUE,
    label       text,
    -- 来源权重：只用于**排序与准入**，不用于判定真假
    -- （否则会变成"大媒体错了一起错"；真假由证据聚合给出）。
    -- refuted_n / total_n 是它**被后续推翻**的统计，权重由这两者校准 —— 这正是
    -- "append-only + 补充/推翻"能给而覆盖式存储给不了的东西。
    authority   double precision NOT NULL DEFAULT 0.5,
    total_n     bigint NOT NULL DEFAULT 0,
    refuted_n   bigint NOT NULL DEFAULT 0,
    last_fetch  timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS feed_items (
    id           bigserial PRIMARY KEY,
    source_id    bigint NOT NULL REFERENCES feed_sources(id),
    url          text NOT NULL UNIQUE,          -- 同址重抓只更新 fetched_at，不新增行
    title        text,
    body         text NOT NULL,
    published_at timestamptz,                    -- 可能抓不到（很多页不给发布时间）
    fetched_at   timestamptz NOT NULL DEFAULT now(),
    -- 64 位 simhash（3-gram shingle 加权）。**近重复判定的载体**：
    -- 新闻的转载率极高，不去重的话 top-24 里可能十几条是同一件事的转述，
    -- 预算被吃光、多样性塌缩，而"被挤掉的别的事件"事后救不回来。
    simhash      bigint NOT NULL,
    -- 近重复指向**首见**条目（NULL = 这是首见）。指向根而不是上一跳：
    -- 一条新闻被转三次时，三条都指回最早那条，而不是串成链。
    dup_of       bigint REFERENCES feed_items(id),
    -- 0=冷存不召回 · 1=可召回 · 2=已入库为文档
    status       smallint NOT NULL,
    -- 判定理由**必须留痕**：这是后面校准阈值与权重的唯一依据，
    -- 不记的话"为什么这条没召回"永远只能靠猜。
    gate_reason  text,
    -- 与**最近候选**的汉明距离（NULL = 一条候选都没有，即没有任何分段撞上）。
    -- 这一列是**校准阈值的仪器**，不是判定结果：DUP_DISTANCE=3 是沿用经典取值，
    -- 而"转载"的真实距离分布得从数据里看 —— 不记的话阈值永远只能靠猜。
    -- ⚠️ 它只统计**LSH 候选里**的最近距离（没候选时无从知道真正的最近是多少），
    -- 读的时候要带上这个边界。
    nearest_dist smallint,
    char_n       integer NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- LSH 分带：64 位切成 4 段 16 位，**任意一段相同即可作候选**。
-- 鸽巢：汉明距离 ≤3 时，3 个不同位最多落在 3 段里 ⇒ 必有一段完全相同 ⇒ 不会漏。
-- 存成独立表而不是 bigint[]：省掉数组类型处理器，且 (band_no, band_key) 上就是普通 B 树。
CREATE TABLE IF NOT EXISTS feed_bands (
    item_id  bigint NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
    band_no  smallint NOT NULL,
    band_key bigint NOT NULL,
    PRIMARY KEY (item_id, band_no)
);

CREATE INDEX IF NOT EXISTS feed_items_pub_idx    ON feed_items (published_at DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS feed_items_status_idx ON feed_items (status, fetched_at DESC);
CREATE INDEX IF NOT EXISTS feed_items_dup_idx    ON feed_items (dup_of) WHERE dup_of IS NOT NULL;
CREATE INDEX IF NOT EXISTS feed_items_src_idx    ON feed_items (source_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS feed_bands_lookup_idx ON feed_bands (band_no, band_key);

COMMENT ON TABLE feed_items IS '信息流条目：抓到即落库，可见性由 status 决定（入库但不召回，不是不入库）';
COMMENT ON COLUMN feed_items.dup_of IS '近重复指向首见条目（simhash 汉明距离 ≤3）';
COMMENT ON TABLE feed_bands IS 'simhash 的 LSH 分带索引：4×16 位，任意一段相同即候选';

-- ── 信息流第 2、3 层：条目嵌入 · 段落级重复 · 议题归并 ────────────────────────
--
-- 为什么是三层而不是一层（2026-09-24 实测定的）：
--   整篇 simhash 抓"逐字转载"（零成本）；但三家媒体报同一件事时两两距离 **28/64**
--   （各家自己写）⇒ 文字层量不到。而它们的**段落**仍可能高度重叠（通稿段被抄）——
--   那一层用"切段 + 向量回归"抓；再上一层"同一件事的不同表述"只能靠议题聚类。
-- 三层**共用同一次 embedding**（条目的 lead 向量与段落向量在同一次批处理里算完），
-- 所以层数增加不等于 GPU 成本成倍增加。

ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS embedding    vector(1024);
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS embed_model  text NOT NULL DEFAULT '';
-- 与**最近的议题质心**的余弦相似度（NULL = 还没算 / 没有可比议题）。
-- 与 nearest_dist 同样的角色：**校准阈值用的仪器**，不是判定结果。
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS nearest_topic_sim real;
-- 段落级重复的两个读数：总段数 / 判定为"已有"的段数。
-- `dup_ratio = dup_seg_n / seg_n` 就是**"这篇有多少内容是库里已有的"**。
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS seg_n       integer;
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS dup_seg_n   integer;
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS enriched_at timestamptz;
-- 晋升之后变成哪篇文档（NULL = 还没晋升）。**只加这一列，条目本身一个字不改** ——
-- append-only 管的是事实，晋升是"多写一份可检索的副本"。
ALTER TABLE feed_items ADD COLUMN IF NOT EXISTS doc_id text;

-- 段落（**粗糙切分**的产物，只为"是不是已有的片段"这件事服务，不承担检索单元的角色）。
CREATE TABLE IF NOT EXISTS feed_segments (
    id          bigserial PRIMARY KEY,
    item_id     bigint NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
    seq         integer NOT NULL,
    text        text NOT NULL,
    char_start  integer NOT NULL,
    char_end    integer NOT NULL,
    embedding   vector(1024),
    embed_model text NOT NULL DEFAULT '',
    -- 与**库里已有段落**的最近相似度（NULL = 库里还没有可比的段落）。
    -- 与 nearest_dist / nearest_topic_sim 同样的角色：**校准阈值的仪器**。
    -- 判"是不是重复"用的是一个阈值，而阈值该取多少要看这一列的分布 —— 不记就只能拍。
    best_sim    real,
    UNIQUE (item_id, seq)
);

-- **模板登记表**：站点家具（侧栏、页脚、"最新新闻"列表…）的文本哈希。
--
-- 为什么不把标记直接写在 `feed_segments` 上：**段落会重建**（换切分器、改粒度、
-- 重跑富化），写在段上就随之丢掉了，于是每次重建都要重新"学"一遍哪些是模板 ——
-- 而重学期间算出来的读数又是脏的。按**文本哈希**登记与段落生命周期无关。
--
-- 判据是"同一段文字出现在 ≥N 个**不同条目**里"：一句话在一篇里重复三次是文风，
-- 出现在 8 篇里只有一种解释 —— 页面模板。
-- ⚠️ 第一版只认**逐字相同**；日期随页面浮动的侧栏（"最新新闻"常是这种）会漏掉一部分，
-- 那部分要按相似度再筛（还没做）。所以这一版是"先拦住大头"。
CREATE TABLE IF NOT EXISTS feed_boilerplate (
    hash      text PRIMARY KEY,      -- md5(段文本)
    n_items   integer NOT NULL,      -- 出现在多少个不同条目里
    sample    text,                  -- 留一段原文，便于人眼核对
    marked_at timestamptz NOT NULL DEFAULT now()
);

-- 议题（事件簇）。**按"事件"归并、不按实体**（用户 2026-09-24 定）：
-- 一个议题 = "谁在什么时候做了什么"；实体只做标签与查询入口，因为
-- **补充/推翻发生在"某件事的说法"上**，挂在实体上就退化成"这家公司的所有新闻"、时间线也就没了。
--
-- ⚠️ `embedding` 是**质心**（成员向量均值），属于**派生统计量**，可以更新 ——
-- 与 append-only 不冲突（append-only 管的是事实，不是统计）。
-- 维护方式见 FeedTopicService：pgvector 0.8 **没有标量乘**（`vector * float8` 不存在），
-- 所以均值在 Java 侧混合后写回，不在 SQL 里算。
CREATE TABLE IF NOT EXISTS feed_topics (
    id          bigserial PRIMARY KEY,
    label       text,
    embedding   vector(1024),
    embed_model text NOT NULL DEFAULT '',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    item_n      integer NOT NULL DEFAULT 0
);

-- 条目 × 议题 的**边**。今天只写一条，但结构上允许多条 ——
-- 因为"这条新闻对议题 A 是补充、对议题 B 是推翻"正是这一支要表达的东西（relation 待闸 2 填）。
CREATE TABLE IF NOT EXISTS feed_item_topics (
    item_id   bigint NOT NULL REFERENCES feed_items(id) ON DELETE CASCADE,
    topic_id  bigint NOT NULL REFERENCES feed_topics(id) ON DELETE CASCADE,
    sim       real,
    joined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (item_id, topic_id)
);

CREATE INDEX IF NOT EXISTS feed_segments_item_idx ON feed_segments (item_id, seq);
-- 段落召回要的是"最近的已有段落"，所以这一处**建 HNSW**（与 sentences 表不同：
-- 那张的查询被 `chunk_id IN (...)` 框死了候选，顺扫就够；这张要全库找最近邻）。
CREATE INDEX IF NOT EXISTS feed_segments_vec_idx
    ON feed_segments USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS feed_item_topics_topic_idx ON feed_item_topics (topic_id);
-- 议题不建向量索引：查询带"近 N 天"的时间过滤，而**带过滤的 HNSW 容易走不到索引**；
-- 议题量级是"每天几十个、窗口内几百个"，顺扫精确且够快。
CREATE INDEX IF NOT EXISTS feed_topics_recent_idx ON feed_topics (last_seen DESC);

-- ── 信息流的抓取层：发布方 → 频道（RSS/接口）─────────────────────────────────
--
-- 为什么分两层：`feed_sources` 是**发布方**（权重 authority / 被推翻率挂在这里），
-- 一个发布方可以有多个**频道**（要闻/国际/财经/社会 各一个 RSS）。
-- 层级（tier）挂在**发布方**上 —— "国家级 → 门户 → 垂媒"说的是谁在说，不是哪个栏目。
ALTER TABLE feed_sources ADD COLUMN IF NOT EXISTS tier smallint NOT NULL DEFAULT 9;

CREATE TABLE IF NOT EXISTS feed_channels (
    id           bigserial PRIMARY KEY,
    source_id    bigint NOT NULL REFERENCES feed_sources(id),
    url          text NOT NULL UNIQUE,      -- RSS / 接口地址
    label        text,
    enabled      boolean NOT NULL DEFAULT true,
    last_fetch   timestamptz,
    -- 已见过的**最新发布时间**。增量就靠它：只取比它新的条目。
    -- 用 pubDate 而不是抓取时间：feed 里常常一次给出最近 30 条，按抓取时间会重复抓。
    last_item_at timestamptz,
    item_n       bigint NOT NULL DEFAULT 0,
    err_n        integer NOT NULL DEFAULT 0,
    last_error   text
);
-- **取条目的通道类型**：`rss` = XML feed；`json` = 站点的接口（很多站点已经没有 RSS，
-- 但列表页是 JS 渲染的 —— 条目在 HTML 里根本不存在，只能找它的接口）。
-- JSON 的字段名各站不同，所以下面四个列是**每频道一份的映射**，不写死在代码里。
ALTER TABLE feed_channels ADD COLUMN IF NOT EXISTS kind        text NOT NULL DEFAULT 'rss';
ALTER TABLE feed_channels ADD COLUMN IF NOT EXISTS array_path  text;   -- 数组在哪：'' = 根，'data.list' = 点号路径
ALTER TABLE feed_channels ADD COLUMN IF NOT EXISTS f_title     text;
ALTER TABLE feed_channels ADD COLUMN IF NOT EXISTS f_link      text;
ALTER TABLE feed_channels ADD COLUMN IF NOT EXISTS f_date      text;

CREATE INDEX IF NOT EXISTS feed_channels_poll_idx ON feed_channels (enabled, last_fetch NULLS FIRST);

COMMENT ON COLUMN feed_channels.last_item_at IS '已见过的最新 pubDate —— 增量抓取的锚点';

-- ── 种子源清单（**实测筛出来的**，不是列出来的）─────────────────────────────
--
-- 实测（2026-09-24，本机）：
--   ✅ 中新网各栏目 RSS：HTTP 200 且 pubDate 是当天 —— **唯一一批活的**
--   ⚠️ 人民网 RSS：HTTP 200 但 pubDate 停在 2025-06-05（**僵尸 feed**）⇒ 收进来但
--      enabled=false。留着的价值是"试过、不可用"这条记录本身，免得下次再试一遍
--   ❌ 404：中国政府网 / 新华网 / 央视网 / 光明网 / 中国日报
--      ⇒ 中文新闻 RSS 已大面积关停；要扩源得走别的通道（列表页解析 / 官方 API）
-- ⚠️ **策展字段用 DO UPDATE，不能 DO NOTHING**：这些域名大多在"手动喂网址"那一轮
-- 就已经建过行（tier 默认 9），DO NOTHING 会**静默什么都不做** ⇒
-- 5 个中新网频道全是 tier 9，而 `tier-max=1` 的轮询**一个源都不会抓**
-- （而且不报错，看起来就是"没有新闻"）。实测踩到过一次。
-- tier/label 是**策展信息**（人定的），种子是它的权威来源，所以这里覆盖。
INSERT INTO feed_sources (domain, label, tier) VALUES
    ('chinanews.com.cn', '中国新闻网', 1),
    ('people.com.cn',    '人民网',     1)
ON CONFLICT (domain) DO UPDATE SET tier = EXCLUDED.tier, label = EXCLUDED.label;

INSERT INTO feed_channels (source_id, url, label, enabled)
SELECT id, 'https://www.chinanews.com.cn/rss/scroll-news.xml', '要闻', true
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label, enabled)
SELECT id, 'https://www.chinanews.com.cn/rss/china.xml', '国内', true
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label, enabled)
SELECT id, 'https://www.chinanews.com.cn/rss/world.xml', '国际', true
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label, enabled)
SELECT id, 'https://www.chinanews.com.cn/rss/finance.xml', '财经', true
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label, enabled)
SELECT id, 'https://www.chinanews.com.cn/rss/society.xml', '社会', true
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

-- **中国政府网**：RSS 早就没了（404），但它的推送接口是活的 —— 60 条国务院/国办政策，
-- 带 title/link/pubDate。这是最该收的 tier 1（政策原文，来源权重最高）。
INSERT INTO feed_sources (domain, label, tier) VALUES ('gov.cn', '中国政府网', 1)
ON CONFLICT (domain) DO UPDATE SET tier = EXCLUDED.tier, label = EXCLUDED.label;

-- ⚠️ **默认关**：接口是活的，但 **Java 侧 TLS 校验不过** ——
-- `PKIX path building failed`：gov.cn 用 **CFCA** 签的（链：*.www.gov.cn ← CFCA OV OCA ← CFCA EV ROOT），
-- 而 **JDK 21 的 cacerts 里 0 条 CFCA**（实测）。curl/浏览器能过是因为用了 Windows 证书库。
-- 这是**一类**问题（新华网那次 `news.cn` 抓取失败同源），不是这一个站。
-- 要开它得先定信任策略：① 把 CFCA 根导入 JDK cacerts（对**所有** JVM TLS 生效）
-- ② 给抓取路径单独一个信任库文件（范围窄、可回退，推荐）③ 不接 CFCA 签的站。
-- 在定之前保持关闭 —— 让它每轮去撞一次墙没有意义，而错误信息已经记在这。
-- **这个源值得开**：国务院/国办政策原文（60 条滚动），是 tier 1 里最有分量的一个。
INSERT INTO feed_channels (source_id, url, label, enabled, kind, array_path, f_title, f_link, f_date, last_error)
SELECT id, 'https://www.gov.cn/pushinfo/v150203/pushinfo.json', '政策推送', false,
       'json', '', 'title', 'link', 'pubDate',
       'TLS：JDK cacerts 无 CFCA 根（实测 0 条），Java 侧 PKIX path building failed；开之前先定信任策略'
  FROM feed_sources WHERE domain = 'gov.cn'
ON CONFLICT (url) DO NOTHING;

-- **界面新闻**（tier 2 门户/财经垂媒）：RSS 是活的（30 条、pubDate 当天）。
-- 门户层的实测结果同样惨：新浪 RSS 停在 **2018 年**、网易/搜狐/澎湃/环球的 RSS 全是空响应
-- ⇒ **门户层目前只有这一家可用**（2026-09-24 实测）。
INSERT INTO feed_sources (domain, label, tier) VALUES ('jiemian.com', '界面新闻', 2)
ON CONFLICT (domain) DO UPDATE SET tier = EXCLUDED.tier, label = EXCLUDED.label;

-- ⚠️ **列数与值数必须对齐**：这里原先多塞了一个 `true`（列只有三个），
-- 而 `SchemaInitializer` 是 `continueOnError=true` ⇒ **整条 INSERT 被静默跳过**，
-- 于是"已加界面新闻"只存在于 schema 文件里、库里根本没有它（2026-09-24 实测发现）。
-- 症状与"这个源没新闻"完全一样。**改完 seed 必须核一次库**，别只看文件。
INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://a.jiemian.com/index.php?m=article&a=rss', '要闻'
  FROM feed_sources WHERE domain = 'jiemian.com'
ON CONFLICT (url) DO NOTHING;

-- **华尔街见闻**（tier 2 财经/市场）：RSS 58 条、pubDate 当天。对"趋势"类问题最有价值的一类源
-- （市场行情、宏观数据、机构观点都在这里）。2026-09-24 实测：36氪/虎嗅/机器之心/第一财经/
-- 财联社/证券时报/中证网 的 RSS 全空或 404，财经类目前**只有这一家**供货。
INSERT INTO feed_sources (domain, label, tier) VALUES ('wallstreetcn.com', '华尔街见闻', 2)
ON CONFLICT (domain) DO UPDATE SET tier = EXCLUDED.tier, label = EXCLUDED.label;

INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://dedicated.wallstreetcn.com/rss.xml', '财经·市场'
  FROM feed_sources WHERE domain = 'wallstreetcn.com'
ON CONFLICT (url) DO NOTHING;

-- 中新网还有几个栏目是活的（要闻/国内/国际/财经/社会之外）
-- （2026-09-24 逐个名字试出来的：sports/edu/health/culture 有 30 条；
--   gn/tw/mil/house/ent2/yl 这些名字存在但**返回 0 条** —— 是空壳，别当可用源）
INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://www.chinanews.com.cn/rss/importnews.xml', '要闻·补充'
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://www.chinanews.com.cn/rss/sports.xml', '体育'
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://www.chinanews.com.cn/rss/edu.xml', '教育'
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://www.chinanews.com.cn/rss/health.xml', '健康'
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

INSERT INTO feed_channels (source_id, url, label)
SELECT id, 'https://www.chinanews.com.cn/rss/culture.xml', '文化'
  FROM feed_sources WHERE domain = 'chinanews.com.cn'
ON CONFLICT (url) DO NOTHING;

-- 僵尸 feed：收进来但关掉，并把原因写进 last_error
INSERT INTO feed_channels (source_id, url, label, enabled, last_error)
SELECT id, 'http://www.people.com.cn/rss/politics.xml', '人民网·政治', false,
       '僵尸 feed：HTTP 200 但 pubDate 停在 2025-06-05（2026-09-24 实测）'
  FROM feed_sources WHERE domain = 'people.com.cn'
ON CONFLICT (url) DO NOTHING;
