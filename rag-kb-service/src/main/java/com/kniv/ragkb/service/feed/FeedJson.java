package com.kniv.ragkb.service.feed;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import lombok.extern.slf4j.Slf4j;

import java.util.ArrayList;
import java.util.List;

/**
 * **从站点的 JSON 接口里取条目**。
 *
 * <p><b>为什么需要它</b>：中文新闻站的列表页绝大多数是 **JS 渲染**的 —— 条目在 HTML 里
 * 根本不存在（实测：gov.cn / 人民网 / 央视 的列表页抓回来，正文里一个文章链接都没有）。
 * 而 RSS 又大面积关停。剩下还能走的路只有**它们自己的接口**：
 * 中国政府网的 {@code pushinfo.json}（国务院/国办政策推送，60 条）就是这样一条。
 *
 * <p><b>字段名各站不同，所以映射放在数据库里**（{@code feed_channels} 的
 * {@code array_path / f_title / f_link / f_date}），不写死在代码里。**
 * 换一个站点只改一行数据，不用改代码、不用重编译 —— 而这一支注定要接很多站点。
 *
 * <p>用 Jackson 而不是手写正则：JSON 的嵌套与转义（标题里带引号、换行）手写解析一定会错，
 * 而 Spring Boot 本来就带着 Jackson。
 */
@Slf4j
public final class FeedJson {

    private static final ObjectMapper M = new ObjectMapper();

    private FeedJson() {
    }

    /**
     * @param arrayPath 数组所在的路径（点号分隔，空串 = 根就是数组），如 {@code "data.list"}
     * @param titleKey  标题字段名
     * @param linkKey   链接字段名
     * @param dateKey   时间字段名（可为空）
     */
    public static List<FeedRss.Entry> parse(String json, String arrayPath,
                                            String titleKey, String linkKey, String dateKey) {
        List<FeedRss.Entry> out = new ArrayList<>();
        if (json == null || json.isBlank()) {
            return out;
        }
        JsonNode root;
        try {
            // 有些接口返回 JSONP：`({"status":0,...})` —— 剥掉外层再解
            String t = json.strip();
            if (t.startsWith("(") && t.endsWith(")")) {
                t = t.substring(1, t.length() - 1);
            }
            root = M.readTree(t);
        } catch (Exception e) {
            log.warn("JSON 接口解析失败：{}", e.getMessage());
            return out;
        }
        JsonNode arr = walk(root, arrayPath);
        if (arr == null || !arr.isArray()) {
            log.warn("JSON 接口里找不到数组（array_path={}）", arrayPath);
            return out;
        }
        for (JsonNode n : arr) {
            String title = text(n, titleKey);
            String link = text(n, linkKey);
            if (link == null || link.isBlank()) {
                continue;
            }
            out.add(new FeedRss.Entry(title == null ? "" : title.strip(),
                    link.strip(), FeedRss.parseTime(text(n, dateKey))));
        }
        return out;
    }

    private static JsonNode walk(JsonNode root, String path) {
        if (path == null || path.isBlank() || "$".equals(path)) {
            return root;
        }
        JsonNode cur = root;
        for (String seg : path.split("\\.")) {
            if (cur == null) {
                return null;
            }
            cur = cur.get(seg);
        }
        return cur;
    }

    private static String text(JsonNode n, String key) {
        if (key == null || key.isBlank()) {
            return null;
        }
        JsonNode v = n.get(key);
        return v == null || v.isNull() ? null : v.asText();
    }
}
