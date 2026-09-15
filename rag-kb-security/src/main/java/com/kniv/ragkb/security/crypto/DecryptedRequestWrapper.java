package com.kniv.ragkb.security.crypto;

import jakarta.servlet.ReadListener;
import jakarta.servlet.ServletInputStream;
import jakarta.servlet.http.HttpServletRequest;
import jakarta.servlet.http.HttpServletRequestWrapper;

import java.io.BufferedReader;
import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;

/**
 * 把解密后的明文伪装成原始请求体，并把密文里携带的令牌注入 Authorization 头。
 *
 * <p>为什么要注入令牌：如果只加密请求体，Authorization 头仍是明文，中间设备照样能拿到
 * 令牌直接调用 API —— 加密就白做了。所以令牌随密文一起传，由过滤器解出后在这里替换。
 */
public class DecryptedRequestWrapper extends HttpServletRequestWrapper {

    private final byte[] body;
    private final String authorization;

    public DecryptedRequestWrapper(HttpServletRequest request, byte[] body, String authorization) {
        super(request);
        this.body = body;
        this.authorization = authorization;
    }

    /**
     * 只注入令牌，<b>不动请求体</b> —— 供原始字节体（加密上传）使用。
     *
     * <p>那条路径的体是文件密文本身，可能几十 MB。整份读进内存再包一层
     * 既浪费又会把堆撑爆，所以体原样透传给控制器，由它边解密边落盘。
     * 需要替换的只有 Authorization 头 —— 令牌仍然是随加密元信息传的。
     */
    public static DecryptedRequestWrapper tokenOnly(HttpServletRequest request, String authorization) {
        return new DecryptedRequestWrapper(request, null, authorization);
    }

    @Override
    public String getHeader(String name) {
        if ("Authorization".equalsIgnoreCase(name) && authorization != null) {
            return authorization;
        }
        return super.getHeader(name);
    }

    @Override
    public java.util.Enumeration<String> getHeaders(String name) {
        if ("Authorization".equalsIgnoreCase(name) && authorization != null) {
            return java.util.Collections.enumeration(java.util.List.of(authorization));
        }
        return super.getHeaders(name);
    }

    @Override
    public int getContentLength() {
        return body == null ? super.getContentLength() : body.length;
    }

    @Override
    public long getContentLengthLong() {
        return body == null ? super.getContentLengthLong() : body.length;
    }

    @Override
    public ServletInputStream getInputStream() throws IOException {
        if (body == null) {
            return super.getInputStream();   // tokenOnly：体原样透传
        }
        ByteArrayInputStream in = new ByteArrayInputStream(body);
        return new ServletInputStream() {
            @Override
            public int read() {
                return in.read();
            }

            @Override
            public int read(byte[] b, int off, int len) {
                return in.read(b, off, len);
            }

            @Override
            public boolean isFinished() {
                return in.available() == 0;
            }

            @Override
            public boolean isReady() {
                return true;
            }

            @Override
            public void setReadListener(ReadListener listener) {
                // 同步读取场景无需异步回调
            }
        };
    }

    @Override
    public BufferedReader getReader() throws IOException {
        return new BufferedReader(new InputStreamReader(getInputStream(), StandardCharsets.UTF_8));
    }

    /** body 为 null 表示只注入令牌、不替换体 */
    public boolean hasReplacedBody() {
        return body != null;
    }
}
