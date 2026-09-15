package com.kniv.ragkb;

import org.mybatis.spring.annotation.MapperScan;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableAsync;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 个人 RAG 知识库 —— 启动类。
 *
 * <p>模块扫描：把 com.kniv.ragkb 下所有模块的 Bean 都纳入容器，
 * 各模块的自动配置类由各自的 spring.factories / AutoConfiguration.imports 提供。
 */
@SpringBootApplication(scanBasePackages = "com.kniv.ragkb")
@MapperScan("com.kniv.ragkb.dao.mapper")
@EnableAsync          // 异步索引任务
@EnableScheduling     // 缓存清理、孤儿任务回收
public class RagKbApplication {

    public static void main(String[] args) {
        SpringApplication.run(RagKbApplication.class, args);
    }
}
