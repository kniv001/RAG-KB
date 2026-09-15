package com.kniv.ragkb.domain.dto;

import lombok.Data;

/**
 * 一次检索命中的分块。
 *
 * <p>{@code distance} 是余弦距离（越小越近）；关键词通道命中时它为 null，
 * 对应的 {@code hits} 才是有效分数。两种通道的分数不可直接比较 ——
 * 这正是混合检索要用 RRF（只看排名不看分数）融合的原因。
 */
@Data
public class ChunkHit {

    private Long id;

    private String docId;

    private String docName;

    private Integer seq;

    private String content;

    /** 余弦距离，仅向量通道有值 */
    private Double distance;

    /** 关键词命中数，仅关键词通道有值 */
    private Integer hits;

    public Double getScore() {
        return distance == null ? null : 1.0 - distance;
    }
}
