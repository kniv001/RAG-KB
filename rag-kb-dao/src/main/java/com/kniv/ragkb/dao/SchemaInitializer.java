package com.kniv.ragkb.dao;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.core.io.ClassPathResource;
import org.springframework.jdbc.datasource.init.ResourceDatabasePopulator;

import javax.sql.DataSource;

/**
 * 建表。
 *
 * <p>全部是 {@code CREATE TABLE IF NOT EXISTS}，幂等，可反复执行。
 * 与 Python 版的表结构保持一致 —— 两套系统连同一个库，谁先启动都不会破坏对方。
 *
 * <p>刻意不引 Flyway/Liquibase：个人项目、单库、表少，引入迁移框架的收益不抵它的复杂度。
 * 将来若表结构开始频繁演进，再换。
 */
@Slf4j
@Configuration
@RequiredArgsConstructor
public class SchemaInitializer {

    private final DataSource dataSource;

    @Bean
    public ApplicationRunner schemaRunner() {
        return args -> {
            try {
                ResourceDatabasePopulator populator = new ResourceDatabasePopulator(
                        new ClassPathResource("db/schema.sql"));
                populator.setContinueOnError(true);   // 单条失败不阻断其余建表
                populator.execute(dataSource);
                log.info("数据库表结构已就绪");
            } catch (Exception e) {
                log.error("建表失败：{}", e.getMessage(), e);
            }
            // **种子行数的可见性检查**（2026-09-24 加）。
            //
            // 为什么需要：`continueOnError(true)` 让**一条写错的 seed 被静默跳过** ——
            // 实测踩过两次：① 列数与值数不匹配（多写了一个值）；② 同一个坏模式被复制成六条。
            // 症状与"这个源本来就没新闻"一模一样，而我去核的是**文件**不是**库**，
            // 于是"已加某某源"只存在于 schema 里。这里把它们打出来，**每次启动都看得见**。
            try (var conn = dataSource.getConnection();
                 var st = conn.createStatement()) {
                StringBuilder b = new StringBuilder();
                for (String t : new String[]{"documents", "chunks", "sentences",
                        "feed_sources", "feed_channels", "feed_items"}) {
                    try (var rs = st.executeQuery("SELECT count(*) FROM " + t)) {
                        b.append(rs.next() ? rs.getLong(1) : -1).append(' ').append(t).append("　");
                    } catch (Exception ignore) {
                        // 表还不存在就算了（首次启动时 feed_* 是新加的）
                    }
                }
                log.info("数据现状：{}", b.toString().trim());
            } catch (Exception e) {
                log.debug("行数检查跳过：{}", e.getMessage());
            }
        };
    }
}
