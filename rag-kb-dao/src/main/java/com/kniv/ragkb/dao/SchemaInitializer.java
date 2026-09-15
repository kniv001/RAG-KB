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
        };
    }
}
