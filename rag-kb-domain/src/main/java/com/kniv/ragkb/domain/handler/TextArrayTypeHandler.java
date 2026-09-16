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
import java.util.Arrays;

/**
 * PostgreSQL 的 {@code text[]} ←→ {@code String[]}。
 *
 * <p>同 {@link LongArrayTypeHandler}：驱动不会自动映射，必须显式给。
 * 这里更是如此 —— 文本数组如果靠拼字面量，元素里的引号、反斜杠、逗号都要转义，
 * 漏一个就是注入面。用 JDBC 的 {@link Array} 让驱动去处理。
 */
@MappedTypes(String[].class)
@MappedJdbcTypes(value = JdbcType.ARRAY, includeNullJdbcType = true)
public class TextArrayTypeHandler extends BaseTypeHandler<String[]> {

    @Override
    public void setNonNullParameter(PreparedStatement ps, int i, String[] parameter,
                                    JdbcType jdbcType) throws SQLException {
        ps.setArray(i, ps.getConnection().createArrayOf("text", parameter));
    }

    @Override
    public String[] getNullableResult(ResultSet rs, String columnName) throws SQLException {
        return read(rs.getArray(columnName));
    }

    @Override
    public String[] getNullableResult(ResultSet rs, int columnIndex) throws SQLException {
        return read(rs.getArray(columnIndex));
    }

    @Override
    public String[] getNullableResult(CallableStatement cs, int columnIndex) throws SQLException {
        return read(cs.getArray(columnIndex));
    }

    private static String[] read(Array arr) throws SQLException {
        if (arr == null) {
            return null;
        }
        try {
            Object raw = arr.getArray();
            if (raw instanceof String[] ss) {
                return ss;
            }
            if (raw instanceof Object[] objs) {
                return Arrays.stream(objs).map((o) -> o == null ? null : o.toString())
                        .toArray(String[]::new);
            }
            return null;
        } finally {
            arr.free();
        }
    }
}
