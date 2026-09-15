package com.kniv.ragkb.domain.dto;

import lombok.Data;

/** 缓存里命中的回答。sources 是原始 JSON 字符串，由上层决定怎么用。 */
@Data
public class CachedAnswer {

    private String key;

    private String answer;

    private String provider;

    private String model;

    private String sources;
}
