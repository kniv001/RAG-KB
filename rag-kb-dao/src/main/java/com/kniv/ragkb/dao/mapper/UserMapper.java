package com.kniv.ragkb.dao.mapper;

import com.baomidou.mybatisplus.core.mapper.BaseMapper;
import com.kniv.ragkb.domain.entity.User;
import org.apache.ibatis.annotations.Mapper;
import org.apache.ibatis.annotations.Param;
import org.apache.ibatis.annotations.Select;
import org.apache.ibatis.annotations.Update;

import java.time.OffsetDateTime;

/**
 * 用户表读写。
 *
 * <p>用 MyBatis-Plus 的 BaseMapper 拿到基础 CRUD，只有需要精确控制的地方才写 SQL。
 */
@Mapper
public interface UserMapper extends BaseMapper<User> {

    @Select("SELECT * FROM users WHERE username = #{username}")
    User findByUsername(@Param("username") String username);

    @Select("SELECT count(*) FROM users")
    long countAll();

    @Update("UPDATE users SET last_login_at = #{at}, updated_at = now() WHERE username = #{username}")
    int touchLogin(@Param("username") String username, @Param("at") OffsetDateTime at);

    @Update("UPDATE users SET password_hash = #{hash}, updated_at = now() WHERE username = #{username}")
    int updatePassword(@Param("username") String username, @Param("hash") String hash);
}
