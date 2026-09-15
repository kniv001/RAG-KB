package com.kniv.ragkb.domain.entity;

import com.baomidou.mybatisplus.annotation.IdType;
import com.baomidou.mybatisplus.annotation.TableId;
import com.baomidou.mybatisplus.annotation.TableName;
import lombok.Data;

import java.time.OffsetDateTime;

/**
 * 用户。
 *
 * <p>表结构与 Python 版完全一致，两边共用同一个库 —— Java 版上线后原有账号直接可用，
 * 不需要迁移数据，也不需要重设密码。
 *
 * <p>密码只以 bcrypt 哈希存在 passwordHash。Python 的 bcrypt 产出 {@code $2b$} 前缀，
 * Spring Security 的 BCryptPasswordEncoder 识别 {@code $2a$ / $2b$ / $2y$}，互通。
 */
@Data
@TableName("users")
public class User {

    @TableId(type = IdType.INPUT)
    private String id;

    private String username;

    /** bcrypt 哈希，永不存明文 */
    private String passwordHash;

    /**
     * 字段名必须叫 isActive，不能叫 active 再配 {@code @TableField("is_active")}。
     *
     * <p>原因：{@code @TableField} 只影响 MyBatis-Plus 生成的 SQL，而自定义
     * {@code @Select} 的结果映射走的是 MyBatis 的 map-underscore-to-camel-case，
     * 它按 is_active → isActive 找属性。名字对不上就静默映射为 null，
     * 表现为「密码正确却登录失败」—— 排查成本极高。
     */
    private Boolean isActive;

    private OffsetDateTime createdAt;

    private OffsetDateTime updatedAt;

    private OffsetDateTime lastLoginAt;
}
