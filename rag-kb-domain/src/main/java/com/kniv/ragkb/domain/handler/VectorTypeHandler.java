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
 * {@code float[]} ↔ PostgreSQL {@code vector} 的类型映射。
 *
 * <p>放在 domain 而不是 dao：{@code Chunk} 实体要用 {@code @TableField(typeHandler=...)}
 * 引用它，而 dao 依赖 domain —— 放 dao 会形成循环依赖。
 *
 * <p>pgvector 在 JDBC 层没有原生类型，只能用<b>字面量</b> {@code [1,2,3]} 传输。
 * 写入时必须用 {@code setObject(i, literal, Types.OTHER)} —— 让驱动把参数当作
 * 「未知类型」发给服务端，PostgreSQL 才能隐式转成 vector。用 {@code setString} 的话
 * 参数类型是 varchar，而 {@code varchar → vector} 没有隐式转换，会直接报
 * 「column "embedding" is of type vector but expression is of type character varying」。
 *
 * <p>刻意<b>不</b>用 pgvector 官方 Java 库：它要求把 Connection 包一层才能工作，
 * 与连接池、事务管理的集成更麻烦；而读写其实只是字符串转换这一件事。
 */
@MappedTypes(float[].class)
@MappedJdbcTypes(value = JdbcType.OTHER, includeNullJdbcType = true)
public class VectorTypeHandler extends BaseTypeHandler<float[]> {

    @Override
    public void setNonNullParameter(PreparedStatement ps, int i, float[] parameter,
                                    JdbcType jdbcType) throws SQLException {
        ps.setObject(i, toLiteral(parameter), Types.OTHER);
    }

    @Override
    public float[] getNullableResult(ResultSet rs, String columnName) throws SQLException {
        return parse(rs.getString(columnName));
    }

    @Override
    public float[] getNullableResult(ResultSet rs, int columnIndex) throws SQLException {
        return parse(rs.getString(columnIndex));
    }

    @Override
    public float[] getNullableResult(CallableStatement cs, int columnIndex) throws SQLException {
        return parse(cs.getString(columnIndex));
    }

    /** float[] → {@code [1,2,3]}。用 %.7g 与 Python 版保持一致，避免精度差异带来的无谓 diff。 */
    public static String toLiteral(float[] v) {
        if (v == null) {
            return null;
        }
        StringBuilder sb = new StringBuilder(v.length * 8 + 2);
        sb.append('[');
        for (int i = 0; i < v.length; i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(String.format("%.7g", v[i]));
        }
        return sb.append(']').toString();
    }

    /** {@code [1,2,3]} → float[]。 */
    public static float[] parse(String s) {
        if (s == null || s.isBlank()) {
            return null;
        }
        String body = s.trim();
        if (body.startsWith("[")) {
            body = body.substring(1);
        }
        if (body.endsWith("]")) {
            body = body.substring(0, body.length() - 1);
        }
        if (body.isBlank()) {
            return new float[0];
        }
        String[] parts = body.split(",");
        float[] out = new float[parts.length];
        for (int i = 0; i < parts.length; i++) {
            out[i] = Float.parseFloat(parts[i].trim());
        }
        return out;
    }
}
