package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.service.diag.DiagMaterial;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * **只读诊断端点** —— 把"服务端已经算出来、但只写进了日志"的那几个数取回来。
 *
 * <h2>为什么值得单开一个口子（而不是继续 grep 日志）</h2>
 *
 * 尺子那一侧（`tools/`，Python）**刻意留在项目外**，理由是它们迭代的是**判据与数据**
 * 而不是**服务**：一天 25 个提交里只有 4 个动 `src/`，其余全是判据/探针/题面；
 * 而且判分是**纯函数** —— 判据改了可以拿历史落盘重判（2026-09-29 那次 2261 条重判，
 * **一次模型都没跑**）。所以"把 189 个脚本搬进 Java"是负收益。
 *
 * <p>但那个诉求里**有一小块是真的**：有些数**只在应用内部存在**，外面只能读它打出来的日志。
 * 材料探针就是这一块 —— 它现在"发请求 + 睡 9 秒 + grep 日志"，而三个脆弱点都在日志那一步
 * （睡短了静默漏题 / 日志轮转 / 按键找不到）。⇒ **就这一小撮做成端点，其余留在外面。**
 *
 * <p>⚠️ 本端点**只读、不改任何东西、不触发任何抓取或模型调用** —— 它读的是一张
 * 内存里最多 64 条的表（见 {@link DiagMaterial}）。走的是同一套信封加密 + 令牌，
 * 没有为它放宽任何鉴权。
 */
@Slf4j
@RestController
@RequestMapping("/api/diag")
@RequiredArgsConstructor
public class DiagController {

    private final DiagMaterial diag;

    /**
     * 取材料的诊断读数。
     *
     * <pre>
     *   GET /api/diag/material?q=&lt;问题原话&gt;   —— 只取这一题（键 = 问题前 20 字）
     *   GET /api/diag/material                  —— 取最近的全部（最新在前）
     * </pre>
     *
     * <p>⚠️ **取不到时返回 {@code code=40400} 而不是空读数** —— 这一条是刻意的：
     * 材料探针旧的失败方式是"没采到"（等于少一题），而它**长得像"这题没问题"**。
     * 让"没有"变成一个**明确的回答**，探针才有机会把它记成失败而不是 0。
     */
    @GetMapping("/material")
    public R<Map<String, Object>> material(@RequestParam(required = false) String q) {
        if (q != null && !q.isBlank()) {
            Map<String, Object> one = diag.get(q);
            if (one == null) {
                return R.fail(R.CODE_NOT_FOUND,
                        "没有这道题的读数（键 = 问题前 20 字 = 「" + DiagMaterial.key(q)
                                + "」；可能还没跑过，或已被最近 " + DiagMaterial.CAP + " 题挤掉）");
            }
            return R.ok(one);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("count", diag.size());
        body.put("items", diag.all());
        return R.ok(body);
    }
}
