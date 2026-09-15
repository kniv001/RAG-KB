package com.kniv.ragkb.service.config;

import jakarta.annotation.PostConstruct;
import lombok.Data;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.context.properties.ConfigurationProperties;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/** 上传文件的存放位置。相对路径以应用启动目录为基准。 */
@Slf4j
@Data
@ConfigurationProperties(prefix = "ragkb.storage")
@Component
public class StorageProperties {

    /** 仓库根（即工作目录）。stored_path 列存的是相对它的路径 */
    private Path root = Path.of(".");

    /** 上传目录（相对 root） */
    private String uploadsDir = "data/uploads";

    /** 单文件上限（MB） */
    private int maxUploadMb = 50;

    public Path uploads() {
        return root.resolve(uploadsDir).toAbsolutePath().normalize();
    }

    public Path resolve(String storedPath) {
        return root.resolve(storedPath).toAbsolutePath().normalize();
    }

    @PostConstruct
    public void init() {
        try {
            Files.createDirectories(uploads());
            log.info("上传目录：{}（单文件上限 {} MB）", uploads(), maxUploadMb);
        } catch (IOException e) {
            log.error("创建上传目录失败：{}", e.getMessage());
        }
    }
}
