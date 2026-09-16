package com.kniv.ragkb.service.config;

import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * GPU 空闲门闸：后台任务只在用户静默时才允许开始。
 *
 * <p><b>为什么必须有</b>：本机只有一块 GPU，而 Ollama 只有一个推理槽
 * （实测服务端配置 {@code OLLAMA_NUM_PARALLEL:1}），请求是 FIFO 排队的。
 * 后台任务在跑的时候，用户的问题只能等 —— 而实测这个「等」是**等满**：
 *
 * <table>
 *   <tr><th>后台任务耗时</th><th>用户请求耗时</th></tr>
 *   <tr><td>1314ms</td><td>1164ms</td></tr>
 *   <tr><td>2492ms</td><td>2496ms</td></tr>
 *   <tr><td>3091ms</td><td>3095ms</td></tr>
 * </table>
 *
 * <p>用户请求自己的耗时只有 192ms（基线），其余全是在排队。旁证是用户的
 * prefill 始终稳定在 36~39ms —— 如果是在算，这里也会涨。它没涨，说明纯粹在等。
 *
 * <p><b>做法</b>是给后台任务加两道闸：没有用户请求在跑（{@code inFlight == 0}），
 * 且静默了 {@code quietSeconds}。等到超过 {@code maxWaitSeconds} 就放弃这一次 ——
 * 后台任务都是增量的，这次不做下次还会再来，而让用户等是实时损失。
 */
@Slf4j
@Component
@ConfigurationProperties(prefix = "ragkb.gpu")
public class GpuGate {

    /** 用户静默多少秒后，后台任务才允许开始 */
    private int quietSeconds = 20;

    /** 最多等多少秒。超了就放弃这次，下次再说 —— 增量的活不值得让用户等 */
    private int maxWaitSeconds = 300;

    /** 轮询间隔：用户在等，这个值决定了「用户一走开多久后台能开始」的粒度 */
    private int pollMillis = 500;

    private final AtomicInteger inFlight = new AtomicInteger();
    private final AtomicLong lastActiveAt = new AtomicLong(System.currentTimeMillis());

    // ---------------- 用户侧 ----------------

    /** 用户请求开始。用 try/finally 保证成对，否则一次泄漏就永久堵死后台任务 */
    public void beginUserRequest() {
        inFlight.incrementAndGet();
        lastActiveAt.set(System.currentTimeMillis());
    }

    public void endUserRequest() {
        if (inFlight.decrementAndGet() < 0) {
            // 多减了一次说明调用方没配对，纠正回来并告警 —— 静默负值会让闸门永远关着
            inFlight.set(0);
            log.warn("GPU 门闸计数被多减了一次，已纠正。检查 begin/end 是否成对");
        }
        lastActiveAt.set(System.currentTimeMillis());
    }

    /** 当前是否有用户请求在跑 */
    public boolean busy() {
        return inFlight.get() > 0;
    }

    // ---------------- 后台侧 ----------------

    /**
     * 后台任务调用：等到用户静默才返回 true。
     *
     * <p>阻塞式的，调用方应当在专用的后台线程上跑（本项目所有后台任务都在
     * 单线程池里）。
     *
     * @return true 表示可以开始；false 表示等太久放弃了，本次跳过
     */
    public boolean awaitIdle() {
        long deadline = System.currentTimeMillis() + maxWaitSeconds * 1000L;
        long quietMs = quietSeconds * 1000L;
        while (System.currentTimeMillis() < deadline) {
            if (!busy() && System.currentTimeMillis() - lastActiveAt.get() >= quietMs) {
                return true;
            }
            try {
                Thread.sleep(pollMillis);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                return false;
            }
        }
        log.debug("等待用户空闲超过 {} 秒，本次后台任务跳过", maxWaitSeconds);
        return false;
    }

    /** 供状态展示 */
    public java.util.Map<String, Object> status() {
        java.util.Map<String, Object> m = new java.util.LinkedHashMap<>();
        m.put("busy", busy());
        m.put("inFlight", inFlight.get());
        m.put("idleSeconds", (System.currentTimeMillis() - lastActiveAt.get()) / 1000);
        m.put("quietSeconds", quietSeconds);
        m.put("maxWaitSeconds", maxWaitSeconds);
        return m;
    }

    public int getQuietSeconds() {
        return quietSeconds;
    }

    public void setQuietSeconds(int quietSeconds) {
        this.quietSeconds = quietSeconds;
    }

    public int getMaxWaitSeconds() {
        return maxWaitSeconds;
    }

    public void setMaxWaitSeconds(int maxWaitSeconds) {
        this.maxWaitSeconds = maxWaitSeconds;
    }

    public int getPollMillis() {
        return pollMillis;
    }

    public void setPollMillis(int pollMillis) {
        this.pollMillis = pollMillis;
    }
}
