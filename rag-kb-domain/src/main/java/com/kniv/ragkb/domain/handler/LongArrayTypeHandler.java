package com.kniv.ragkb.domain.handler;

import org.apache.ibatis.type.BaseTypeHandler;
import org.apache.ibatis.type.JdbcType;
import org.apache.ibatis.type.MappedJdbcTypes;
import org.apache.ibatis.type.MappedTypes;

import java.sql.Array;
import java.sql.CallableStatement;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Types;
import java.util.Arrays;

/**
 * PostgreSQL 的 {@code bigint[]} ←→ {@code Long[]}。
 *
 * <p>与 vector / jsonb 是同一类问题：驱动不会自动映射这些类型，不显式给
 * TypeHandler 就会以 varchar 发出去，而 varchar → bigint[] 没有隐式转换，
 * 直接报类型错。
 *
 * <p>这里用 JDBC 的 {@link Array} 而不是拼字面量字符串：数组元素是数字，
 * 没有注入风险，但用原生数组能让驱动正确处理 NULL 元素与长度。
 */
@MappedTypes(Long[].class)
@MappedJdbcTypes(value = JdbcType.ARRAY, includeNullJdbcType = true)
public class LongArrayTypeHandler extends BaseTypeHandler<Long[]> {

    @Override
    public void setNonNullParameter(PreparedStatement ps, int i, Long[] parameter,
                                    JdbcType jdbcType) throws SQLException {
        ps.setArray(i, ps.getConnection().createArrayOf("bigint", parameter));
    }

    @Override
    public Long[] getNullableResult(ResultSet rs, String columnName) throws SQLException {
        return read(rs.getArray(columnName));
    }

    @Override
    public Long[] getNullableResult(ResultSet rs, int columnIndex) throws SQLException {
        return read(rs.getArray(columnIndex));
    }

    @Override
    public Long[] getNullableResult(CallableStatement cs, int columnIndex) throws SQLException {
        return read(cs.getArray(columnIndex));
    }

    private static Long[] read(Array arr) throws SQLException {
        if (arr == null) {
            return null;
        }
        try {
            Object raw = arr.getArray();
            if (raw instanceof Long[] longs) {
                return longs;
            }
            if (raw instanceof Object[] objs) {
                return Arrays.stream(objs)
                        .map((o) -> o == null ? null : ((Number) o).longValue())
                        .toArray(Long[]::new);
            }
            return null;
        } finally {
            arr.free();
        }
    }
}
