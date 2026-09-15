package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.service.cache.CacheService;
import lombok.RequiredArgsConstructor;
import org.springframework.web.bind.annotation.*;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 三层缓存的运维接口。
 *
 * <p>因为缓存的键里含所有影响结果的参数（模型名、上下文哈希、历史哈希），
 * 「改了输入就自动不命中」—— 所以清空操作只是释放空间，<b>不影响正确性</b>，
 * 是个可以放心点的按钮。
 */
@RestController
@RequestMapping("/api/cache")
@RequiredArgsConstructor
public class CacheController {

    private final CacheService cache;

    @GetMapping("/stats")
    public R<Map<String, Object>> stats() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("stats", cache.stats());
        out.put("note", "键含模型名/上下文哈希/历史哈希，改了输入即自动不命中；清空只释放空间，不影响正确性");
        return R.ok(out);
    }

    @DeleteMapping
    public R<Map<String, Object>> clear(@RequestParam(required = false) String which) {
        if (which != null && !which.isBlank()
                && !which.equals("embeddings") && !which.equals("answers") && !which.equals("parses")) {
            return R.fail(R.CODE_BAD_REQUEST, "which 只能是 embeddings / answers / parses，或留空全清");
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("removed", cache.clear(which));
        return R.ok(out);
    }
}
