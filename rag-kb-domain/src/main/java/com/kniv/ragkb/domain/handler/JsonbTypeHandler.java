package com.kniv.ragkb.domain.handler;

import org.apache.ibatis.type.BaseTypeHandler;
import org.apache.ibatis.type.JdbcType;
import org.apache.ibatis.type.MappedJdbcTypes;
import org.apache.ibatis.type.MappedTypes;

import java.sql.CallableStatement;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;

/**
 * {@code String} ↔ PostgreSQL {@code jsonb} 的类型映射。
 *
 * <p>与 {@link VectorTypeHandler} 是同一类问题：驱动把 String 参数当作 varchar 发送，
 * 而 {@code varchar → jsonb} 没有隐式转换，直接报
 * 「column "result" is of type jsonb but expression is of type character varying」。
 *
 * <p>解法同样是 {@code setObject(i, value, Types.OTHER)} —— 让驱动按「未知类型」发送，
 * PostgreSQL 才能隐式转成 jsonb。读写都按原始 JSON 字符串处理，不在这一层做结构化 ——
 * 那属于业务层的职责，放这里会让类型处理器变成又一个需要维护的模型。
 */
@MappedTypes(String.class)
@MappedJdbcTypes(value = JdbcType.OTHER, includeNullJdbcType = true)
public class JsonbTypeHandler extends BaseTypeHandler<String> {

    @Override
    public void setNonNullParameter(PreparedStatement ps, int i, String parameter,
                                    JdbcType jdbcType) throws SQLException {
        ps.setObject(i, parameter, Types.OTHER);
    }

    @Override
    public String getNullableResult(ResultSet rs, String columnName) throws SQLException {
        return rs.getString(columnName);
    }

    @Override
    public String getNullableResult(ResultSet rs, int columnIndex) throws SQLException {
        return rs.getString(columnIndex);
    }

    @Override
    public String getNullableResult(CallableStatement cs, int columnIndex) throws SQLException {
        return cs.getString(columnIndex);
    }
}
