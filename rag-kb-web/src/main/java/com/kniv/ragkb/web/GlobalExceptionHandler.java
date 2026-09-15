package com.kniv.ragkb.web;

import com.kniv.ragkb.common.api.R;
import com.kniv.ragkb.security.AuthException;
import com.kniv.ragkb.security.crypto.CryptoException;
import lombok.extern.slf4j.Slf4j;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.validation.FieldError;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

import java.util.stream.Collectors;

/**
 * 全局异常处理：把各类异常统一收敛成 {@link R} 信封。
 *
 * <p>原则：业务异常把原因讲清楚（用户能据此行动），系统异常只回显类型与简短信息，
 * 详细堆栈只进日志 —— 对外暴露堆栈是给攻击者送情报。
 */
@Slf4j
@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(AuthException.class)
    public ResponseEntity<R<Void>> handleAuth(AuthException e) {
        HttpStatus status = switch (e.getCode()) {
            case 42900 -> HttpStatus.TOO_MANY_REQUESTS;
            case 40000 -> HttpStatus.BAD_REQUEST;
            case 40300 -> HttpStatus.FORBIDDEN;
            default -> HttpStatus.UNAUTHORIZED;
        };
        return ResponseEntity.status(status).body(R.fail(e.getCode(), e.getMessage()));
    }

    @ExceptionHandler(CryptoException.class)
    public ResponseEntity<R<Void>> handleCrypto(CryptoException e) {
        return ResponseEntity.badRequest().body(R.fail(R.CODE_BAD_REQUEST, e.getMessage()));
    }

    /** @Valid 校验失败：把所有字段错误拼成一句，避免前端逐条弹窗 */
    @ExceptionHandler(MethodArgumentNotValidException.class)
    public ResponseEntity<R<Void>> handleValidation(MethodArgumentNotValidException e) {
        String msg = e.getBindingResult().getFieldErrors().stream()
                .map(FieldError::getDefaultMessage)
                .collect(Collectors.joining("；"));
        return ResponseEntity.badRequest()
                .body(R.fail(R.CODE_BAD_REQUEST, msg.isBlank() ? "参数校验失败" : msg));
    }

    @ExceptionHandler(IllegalArgumentException.class)
    public ResponseEntity<R<Void>> handleIllegalArgument(IllegalArgumentException e) {
        return ResponseEntity.badRequest()
                .body(R.fail(R.CODE_BAD_REQUEST, e.getMessage() == null ? "参数不合法" : e.getMessage()));
    }

    @ExceptionHandler(Exception.class)
    public ResponseEntity<R<Void>> handleOther(Exception e) {
        log.error("未预期的异常", e);
        return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR)
                .body(R.fail(R.CODE_ERROR, e.getClass().getSimpleName()
                        + (e.getMessage() == null ? "" : ": " + e.getMessage())));
    }
}
