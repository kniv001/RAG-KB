package com.kniv.ragkb.domain.dto;

import jakarta.validation.constraints.NotBlank;
import jakarta.validation.constraints.Size;
import lombok.AllArgsConstructor;
import lombok.Data;
import lombok.NoArgsConstructor;

import java.time.OffsetDateTime;

/**
 * 认证相关的请求/响应对象。
 *
 * <p>放在同一个文件里是因为它们总是一起演化 —— 分散成 6 个文件反而增加查找成本。
 */
public final class AuthDtos {

    private AuthDtos() {
    }

    @Data
    public static class LoginRequest {
        @NotBlank(message = "用户名不能为空")
        @Size(max = 64)
        private String username;

        @NotBlank(message = "密码不能为空")
        @Size(max = 256)
        private String password;
    }

    @Data
    public static class ChangePasswordRequest {
        @NotBlank(message = "当前密码不能为空")
        private String oldPassword;

        @NotBlank(message = "新密码不能为空")
        @Size(min = 8, max = 256, message = "新密码至少 8 位")
        private String newPassword;
    }

    /**
     * 登录 / 刷新的响应。
     *
     * <p>刻意<b>不含</b> refreshToken —— 它由 httpOnly Cookie 下发，JS 读不到，
     * 这样即使有 XSS 也偷不走那把长期钥匙。
     */
    @Data
    @NoArgsConstructor
    @AllArgsConstructor
    public static class TokenResponse {
        private String accessToken;
        private String tokenType;
        private long expiresIn;
        private UserInfo user;

        public static TokenResponse of(String accessToken, long expiresIn, UserInfo user) {
            return new TokenResponse(accessToken, "bearer", expiresIn, user);
        }
    }

    @Data
    @NoArgsConstructor
    @AllArgsConstructor
    public static class UserInfo {
        private String username;
        private OffsetDateTime createdAt;
        private OffsetDateTime lastLoginAt;
        private Long sessions;
    }

    /** 登录页需要的公开参数，不含任何凭据。 */
    @Data
    @AllArgsConstructor
    public static class AuthConfig {
        private long accessTtl;
        private long refreshTtl;
        private boolean hasUser;
        private boolean redis;
    }
}
