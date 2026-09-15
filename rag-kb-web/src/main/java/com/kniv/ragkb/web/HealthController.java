package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import javax.sql.DataSource;
import java.sql.Connection;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 健康检查与自检。
 *
 * <p>骨架阶段只验证「进程起来了 + 数据库/Redis 连得上」，
 * 后续把向量模型自检、缓存统计等项补进来（对应 Python 版 /api/health）。
 */
@Slf4j
@RestController
@RequestMapping("/api")
@RequiredArgsConstructor
public class HealthController {

    private final DataSource dataSource;

    @GetMapping("/ping")
    public R<Map<String, Object>> ping() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("service", "rag-kb");
        body.put("version", "0.1.0");
        body.put("time", Instant.now().toString());
        return R.ok(body);
    }

    @GetMapping("/health")
    public R<Map<String, Object>> health() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("service", "rag-kb");
        body.put("version", "0.1.0");
        body.put("time", Instant.now().toString());
        body.put("database", probeDatabase());
        body.put("modules", "common,domain,dao,security,provider,service,web");
        return R.ok(body);
    }

    private Map<String, Object> probeDatabase() {
        Map<String, Object> db = new LinkedHashMap<>();
        try (Connection conn = dataSource.getConnection()) {
            db.put("ok", true);
            db.put("product", conn.getMetaData().getDatabaseProductName()
                    + " " + conn.getMetaData().getDatabaseProductVersion());
            db.put("catalog", conn.getCatalog());
        } catch (Exception e) {
            log.warn("数据库探测失败: {}", e.getMessage());
            db.put("ok", false);
            db.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
        }
        return db;
    }
}
