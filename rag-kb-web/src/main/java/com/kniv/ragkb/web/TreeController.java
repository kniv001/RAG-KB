package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.service.tree.TreeBuildService;
import com.kniv.ragkb.service.tree.TreeService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.*;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 主题树：查看覆盖范围、重建。
 *
 * <p>重建是同步的。个人规模的语料下几十秒内能完成（聚类是纯几何运算，
 * 只有给每个簇起名那一步调模型，一个簇约一秒），没有超出 Cloudflare
 * 的 100 秒源站超时，不值得为它引入任务队列。
 */
@Slf4j
@RestController
@RequestMapping("/api/tree")
@RequiredArgsConstructor
public class TreeController {

    private final TreeService tree;
    private final TreeBuildService builder;

    @GetMapping("/status")
    public R<Map<String, Object>> status() {
        return R.ok(tree.status(builder.indexedChunks()));
    }

    /**
     * 重建。耗时集中在给各簇起名上，所以返回里带上用了多久 ——
     * 语料变大之后用户能据此判断要不要继续等。
     */
    @PostMapping("/build")
    public R<Map<String, Object>> build() {
        try {
            TreeBuildService.Result r = builder.build();
            Map<String, Object> out = new LinkedHashMap<>();
            out.put("chunks", r.chunks());
            out.put("clusters", r.clusters());
            out.put("elapsedMs", r.ms());
            return R.ok(out);
        } catch (IllegalStateException e) {
            return R.fail(R.CODE_BAD_REQUEST, e.getMessage());
        } catch (Exception e) {
            log.warn("建树失败", e);
            return R.fail(R.CODE_ERROR, "建树失败：" + e.getMessage());
        }
    }
}
