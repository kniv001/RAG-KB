package com.kniv.ragkb.domain.dto;

import lombok.Data;

/** 缓存里命中的向量。vec 是字面量字符串，避免依赖 pgvector 的类型映射。 */
@Data
public class CachedVector {

    private String key;

    private String vec;

    private Integer dim;
}
