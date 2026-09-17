import com.kniv.ragkb.service.chunk.TextChunker;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * 单独验分块器：给定一个 .md，打印块数、长度分布、每块的开头。
 * 目的是看**块首是不是句子开头** —— 改动的全部意义就在这里。
 */
public class ChunkCheck {
    public static void main(String[] args) throws Exception {
        String text = new String(Files.readAllBytes(Path.of(args[0])), StandardCharsets.UTF_8);
        TextChunker tc = new TextChunker();
        List<String> parts = tc.split(text);
        int n = parts.size();
        int min = Integer.MAX_VALUE, max = 0, sum = 0, badHead = 0;
        for (String p : parts) {
            min = Math.min(min, p.length());
            max = Math.max(max, p.length());
            sum += p.length();
            // 「半句开头」的判据：首字符不是句子起首常见形态，且上一块不是以句末标点结尾
            char c = p.charAt(0);
            if (!(Character.isLetterOrDigit(c) || c == '#' || c == '>' || c == '-' || c == '*'
                    || c == '"' || c == '「' || c == '《' || c == '(')) {
                badHead++;
            }
        }
        System.out.printf("块数 %d，长度 min %d / 均 %d / max %d%n", n, min, sum / Math.max(n, 1), max);
        System.out.println("前 8 块的开头：");
        for (int i = 0; i < Math.min(8, n); i++) {
            String p = parts.get(i).replace('\n', ' ');
            System.out.printf("  [%2d] %3d字  %s%n", i, p.length(),
                    p.substring(0, Math.min(46, p.length())));
        }
        System.out.println("最后 3 块的开头：");
        for (int i = Math.max(0, n - 3); i < n; i++) {
            String p = parts.get(i).replace('\n', ' ');
            System.out.printf("  [%2d] %3d字  %s%n", i, p.length(),
                    p.substring(0, Math.min(46, p.length())));
        }
        // 第二个参数给了路径就落盘全部块（JSON 数组，保留换行），供离线比对
        if (args.length > 1) {
            StringBuilder sb = new StringBuilder("[");
            for (int i = 0; i < n; i++) {
                if (i > 0) {
                    sb.append(',');
                }
                sb.append('"').append(parts.get(i)
                        .replace("\\", "\\\\").replace("\"", "\\\"")
                        .replace("\r", "\\r").replace("\n", "\\n")
                        .replace("\t", "\\t")).append('"');
            }
            sb.append(']');
            Files.write(Path.of(args[1]), sb.toString().getBytes(StandardCharsets.UTF_8));
            System.out.println("已写 " + args[1]);
        }
    }
}
