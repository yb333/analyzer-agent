"""DDL 解析鲁棒性测试（parse_ddl_for_metadata）。

覆盖真实 GaussDB DDL 的各种复杂写法：
- WITH 子句（orientation/compression）
- DISTRIBUTE BY
- PARTITION BY（含分区定义的嵌套括号）
- IF NOT EXISTS
- 行内注释 / COMMENT ON COLUMN
- 多空格/tab/换行容错
- schema 前缀
- PRIMARY KEY / CONSTRAINT 等非字段行跳过

运行:
    pytest tests/test_ddl_parsing.py -v
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANALYZER_REF = PROJECT_ROOT / "dws-pipeline-analyzer" / "references"
sys.path.insert(0, str(ANALYZER_REF))

from engine import parse_ddl_for_metadata, _extract_create_table_body, _extract_table_props


def _write_ddl(tmp_path, table_name, ddl_content):
    """写一个 DDL 文件到临时目录，返回目录路径。"""
    ddl_dir = tmp_path / "ddl"
    ddl_dir.mkdir(exist_ok=True)
    (ddl_dir / f"{table_name}.sql").write_text(ddl_content, encoding="utf-8")
    return str(ddl_dir)


class TestExtractCreateTableBody:
    """_extract_create_table_body 括号配平提取。"""

    def test_simple(self):
        body = _extract_create_table_body("CREATE TABLE t (a INT, b VARCHAR(64))")
        assert "a INT" in body
        assert "b VARCHAR(64)" in body

    def test_with_clause_not_included(self):
        """WITH 子句不应进入 body。"""
        ddl = "CREATE TABLE t (a INT) WITH (orientation=column, compression=high)"
        body = _extract_create_table_body(ddl)
        assert "orientation" not in body
        assert "compression" not in body
        assert "a INT" in body

    def test_distribute_by_not_included(self):
        """DISTRIBUTE BY 不应进入 body。"""
        ddl = "CREATE TABLE t (a INT) DISTRIBUTE BY HASH (a)"
        body = _extract_create_table_body(ddl)
        assert "DISTRIBUTE" not in body.upper()
        assert "a INT" in body

    def test_partition_by_not_included(self):
        """PARTITION BY（含嵌套括号）不应进入 body。"""
        ddl = ("CREATE TABLE t (a INT, b DATE) "
               "PARTITION BY RANGE (b) (\n"
               "  PARTITION p1 VALUES LESS THAN ('20240101'),\n"
               "  PARTITION p2 VALUES LESS THAN ('20240201')\n"
               ")")
        body = _extract_create_table_body(ddl)
        assert "PARTITION" not in body.upper()
        assert "a INT" in body
        assert "b DATE" in body

    def test_field_type_with_paren_not_broken(self):
        """字段类型含括号（DECIMAL(18,2)）不应被误判为表定义闭合。"""
        ddl = "CREATE TABLE t (amount DECIMAL(18,2), name VARCHAR(64))"
        body = _extract_create_table_body(ddl)
        assert "amount DECIMAL(18,2)" in body
        assert "name VARCHAR(64)" in body

    def test_if_not_exists(self):
        ddl = "CREATE TABLE IF NOT EXISTS t (a INT)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body

    def test_schema_prefix(self):
        ddl = "CREATE TABLE dws.dwb_t (a INT)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body

    def test_multiline_and_tabs(self):
        """多行/tab/换行容错。"""
        ddl = "CREATE\tTABLE\nt (\n\ta INT,\n\tb VARCHAR(64)\n)"
        body = _extract_create_table_body(ddl)
        assert "a INT" in body


class TestParseDdlGaussDb:
    """真实 GaussDB DDL 解析。"""

    def test_full_gaussdb_ddl(self, tmp_path):
        """完整 GaussDB DDL（WITH + DISTRIBUTE + PARTITION）能正确解析字段。"""
        ddl = """CREATE TABLE dws.dwb_trade_order_d (
  order_id VARCHAR(64) NOT NULL,
  cust_id VARCHAR(64),
  total_amount DECIMAL(18,2),
  order_time TIMESTAMP,
  PRIMARY KEY (order_id)
) WITH (orientation=column, compression=high)
DISTRIBUTE BY HASH (order_id)
PARTITION BY RANGE (order_time) (
  PARTITION p202401 VALUES LESS THAN ('20240201')
);
COMMENT ON COLUMN dws.dwb_trade_order_d.order_id IS '订单ID';
COMMENT ON COLUMN dws.dwb_trade_order_d.cust_id IS '客户ID';
COMMENT ON COLUMN dws.dwb_trade_order_d.total_amount IS '订单总额';
COMMENT ON TABLE dws.dwb_trade_order_d IS '订单汇总';"""
        ddl_dir = _write_ddl(tmp_path, "dwb_trade_order_d", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "dwb_trade_order_d")

        # 字段类型正确（不被 WITH/PARTITION 干扰）
        assert "order_id" in meta
        assert "VARCHAR" in meta["order_id"]["type"]
        assert "total_amount" in meta
        assert "DECIMAL(18,2)" in meta["total_amount"]["type"], \
            f"类型应含精度，实际 {meta['total_amount']['type']}"
        assert "order_time" in meta
        assert "TIMESTAMP" in meta["order_time"]["type"]

        # COMMENT ON COLUMN 注释正确
        assert meta["order_id"]["comment"] == "订单ID"
        assert meta["total_amount"]["comment"] == "订单总额"

        # PRIMARY KEY 不应被当成字段
        assert "primary" not in meta

    def test_inline_comment(self, tmp_path):
        """行内注释 /* 中文名 */ 能解析。"""
        ddl = """CREATE TABLE t (
  order_id VARCHAR(64) /* 订单编号 */,
  amount DECIMAL(18,2) /* 金额 */
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["order_id"]["comment"] == "订单编号"
        assert meta["amount"]["comment"] == "金额"

    def test_comment_on_column_overrides_inline(self, tmp_path):
        """COMMENT ON COLUMN 覆盖行内注释。"""
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2) /* 旧行内注释 */
);
COMMENT ON COLUMN t.amount IS '新注释';"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "新注释"

    def test_single_line_multi_fields(self, tmp_path):
        """防回归：单行 DDL 多字段能全部解析（不能只取第一个）。

        历史 bug：字段解析按换行 split，单行 DDL（a INT, b VARCHAR）只有一行，
        正则只匹配第一个字段，后面的全丢。
        """
        ddl = "CREATE TABLE t (id VARCHAR(64), amount DECIMAL(18,2), name VARCHAR(128));"
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert "id" in meta, "id 应解析"
        assert "amount" in meta, "amount 应解析（不能丢）"
        assert "name" in meta, "name 应解析（不能丢）"

    def test_comment_double_quotes(self, tmp_path):
        """防回归：COMMENT ON COLUMN 支持双引号（真实 DDL 导出工具常用）。

        历史 bug：正则只支持单引号 '([^']*)'，双引号 COMMENT 解析不到注释，
        导致 HTML 报告/mapping 里字段业务含义为空。
        """
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2)
);
COMMENT ON COLUMN t.amount IS "金额";"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "金额", \
            f"双引号 COMMENT 应解析，实际 {meta['amount']['comment']!r}"

    def test_inline_dash_comment(self, tmp_path):
        """防回归：行内 -- 注释能解析。"""
        ddl = """CREATE TABLE t (
  amount DECIMAL(18,2) -- 金额
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert meta["amount"]["comment"] == "金额", \
            f"行内 -- 注释应解析，实际 {meta['amount']['comment']!r}"

    def test_default_with_dash_not_misinterpreted(self, tmp_path):
        """防回归：DEFAULT 值里的 -- 不被误匹配为注释。

        历史 bug：行内 -- 注释正则太宽泛，DEFAULT 'http://--test' 里的 --
        被当成注释，导致 default_value 丢失 + comment 污染。
        """
        ddl = """CREATE TABLE t (
  url VARCHAR(256) DEFAULT 'http://--test',
  amount DECIMAL(18,2) -- 金额
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        # url 不应有注释（DEFAULT 里的 -- 不应匹配）
        assert not meta["url"].get("comment"), \
            f"url 不应有注释，实际 {meta['url'].get('comment')!r}"
        # url 的 DEFAULT 值应保留
        assert meta["url"]["default_value"], "url 的 DEFAULT 值不应丢失"
        # amount 有注释
        assert meta["amount"]["comment"] == "金额"

    def test_constraint_skipped(self, tmp_path):
        """CONSTRAINT/UNIQUE/CHECK 行不被当成字段。"""
        ddl = """CREATE TABLE t (
  id VARCHAR(64),
  amount DECIMAL(18,2),
  CONSTRAINT pk_t PRIMARY KEY (id),
  UNIQUE (id),
  CHECK (amount >= 0)
);"""
        ddl_dir = _write_ddl(tmp_path, "t", ddl)
        meta = parse_ddl_for_metadata(ddl_dir, "t")
        assert "id" in meta
        assert "amount" in meta
        # 约束相关的不应被当成字段
        meta_keys = set(meta.keys())
        assert "constraint" not in meta_keys
        assert "check" not in meta_keys
        assert "unique" not in meta_keys

    def test_empty_ddl_dir(self, tmp_path):
        """空 DDL 目录返回空 dict。"""
        empty = tmp_path / "empty"
        empty.mkdir()
        assert parse_ddl_for_metadata(str(empty), "t") == {}

    def test_no_ddl_dir(self, tmp_path):
        """不存在的路径返回空 dict。"""
        assert parse_ddl_for_metadata(str(tmp_path / "noexist"), "t") == {}

    def test_table_not_in_ddl_file(self, tmp_path):
        """DDL 文件里没有目标表，返回空。"""
        ddl = "CREATE TABLE other_table (a INT);"
        ddl_dir = _write_ddl(tmp_path, "other", ddl)
        assert parse_ddl_for_metadata(ddl_dir, "not_exist_table") == {}

    def test_multiple_ddl_files(self, tmp_path):
        """DDL 目录有多个文件，只解析含目标表的。"""
        ddl_dir = tmp_path / "ddl"
        ddl_dir.mkdir()
        (ddl_dir / "dwb_order_f.sql").write_text(
            "CREATE TABLE dwb_order_f (order_id INT);\n"
            "COMMENT ON COLUMN dwb_order_f.order_id IS '订单ID';",
            encoding="utf-8")
        (ddl_dir / "dwb_cust_f.sql").write_text(
            "CREATE TABLE dwb_cust_f (cust_id VARCHAR(64));\n"
            "COMMENT ON COLUMN dwb_cust_f.cust_id IS '客户ID';",
            encoding="utf-8")
        meta = parse_ddl_for_metadata(str(ddl_dir), "dwb_cust_f")
        assert "cust_id" in meta
        assert "order_id" not in meta


# ═══════════════════════════════════════════════════════════════
# 类型长度保留（回归测试：字符类型不能丢长度）
# ═══════════════════════════════════════════════════════════════

class TestTypeLengthPreservation:
    """字符类型必须保留类型名+长度，不能被截断。

    背景：character varying(20) / nvarchar(10) 等类型如果丢失长度，
    会直接影响字段类型变化的影响判定和质量评估。
    """

    def test_nvarchar_keeps_length(self, tmp_path):
        """nvarchar(50) 不能变成 nvarchar 或 character。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (user_name nvarchar(50) NOT NULL);")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["user_name"]["type"] == "nvarchar(50)", \
            f"nvarchar(50) 长度丢失: {meta['user_name']['type']}"

    def test_varchar_keeps_length(self, tmp_path):
        """varchar(20) 保留长度。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (phone varchar(20));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["phone"]["type"] == "varchar(20)"

    def test_character_varying_keeps_length(self, tmp_path):
        """character varying(100) 两段式类型名+长度都要保留。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (email character varying(100));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        t = meta["email"]["type"].lower()
        assert "varying" in t, f"character varying 丢了 varying: {t}"
        assert "100" in t, f"character varying 丢了长度: {t}"

    def test_character_varying_uppercase(self, tmp_path):
        """CHARACTER VARYING(30) 大写也能解析。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (name CHARACTER VARYING(30));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        t = meta["name"]["type"].lower()
        assert "varying" in t and "30" in t

    def test_nvarchar2_keeps_length(self, tmp_path):
        """NVARCHAR2(10) 含数字的类型名也要保留。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (code NVARCHAR2(10));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert "10" in meta["code"]["type"], \
            f"NVARCHAR2(10) 长度丢失: {meta['code']['type']}"

    def test_numeric_keeps_precision(self, tmp_path):
        """numeric(18,2) 精度参数保留（原有能力，防回归）。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (amount numeric(18,2));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["amount"]["type"] == "numeric(18,2)"

    def test_type_not_eaten_by_default(self, tmp_path):
        """类型后跟 DEFAULT，类型不能把 DEFAULT 吃进去。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (status integer DEFAULT 0);")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["status"]["type"] == "integer", \
            f"类型被 DEFAULT 污染: {meta['status']['type']}"

    def test_type_not_eaten_by_not_null(self, tmp_path):
        """类型后跟 NOT NULL，类型不能把 NOT NULL 吃进去。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (user_name nvarchar(50) NOT NULL);")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["user_name"]["type"] == "nvarchar(50)", \
            f"类型被 NOT NULL 污染: {meta['user_name']['type']}"

    def test_all_char_types_mixed(self, tmp_path):
        """三种字符类型混在一个 DDL 里都正确。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (\n"
            "  a nvarchar(10),\n"
            "  b character varying(20),\n"
            "  c varchar(30)\n"
            ");")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["a"]["type"] == "nvarchar(10)"
        assert "varying" in meta["b"]["type"].lower() and "20" in meta["b"]["type"]
        assert meta["c"]["type"] == "varchar(30)"


class TestExtractTableProps:
    """_extract_table_props：表定义闭合括号之后的物理属性提取（v1.8 波 1b）。

    真实 GaussDB(DWS) 表尾子句：WITH(...) 存储参数 / DISTRIBUTE BY 分布键 /
    PARTITION BY 分区定义 / TABLESPACE。这是 catalog.__table_props__ 的唯一
    生产点（docs/13：单一权威生产点，投影/视图只消费）。
    """

    def test_full_gaussdb_ddl(self):
        """典型三段式：WITH + DISTRIBUTE BY HASH + PARTITION BY RANGE 全部提取。"""
        ddl = (
            "CREATE TABLE customer_addr (\n"
            "  ca_address_sk INTEGER NOT NULL,\n"
            "  ca_address_id CHARACTER(16)\n"
            ")\n"
            "WITH (orientation=column, compression=low)\n"
            "DISTRIBUTE BY HASH (ca_address_sk)\n"
            "PARTITION BY RANGE (ca_address_sk)\n"
            "(\n"
            "  PARTITION p1 VALUES LESS THAN (10000),\n"
            "  PARTITION p2 VALUES LESS THAN (20000)\n"
            ");"
        )
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (ca_address_sk)"
        assert props["distribute_columns"] == ["ca_address_sk"]
        assert props["with_options"] == "orientation=column, compression=low"
        assert props["partition_by"].startswith("PARTITION BY RANGE (ca_address_sk)")
        assert "VALUES LESS THAN (10000)" in props["partition_by"]

    def test_multi_column_hash(self):
        """HASH 多列分布键，全部进 distribute_columns（小写）。"""
        ddl = "CREATE TABLE t (a INT, b INT, c INT) DISTRIBUTE BY HASH (A, b);"
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (A, b)"  # 原文不重排
        assert props["distribute_columns"] == ["a", "b"]

    def test_replication_no_columns(self):
        """REPLICATION 复制表无分布列。"""
        ddl = "CREATE TABLE dim_code (code INT, name VARCHAR(64)) DISTRIBUTE BY REPLICATION;"
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "REPLICATION"
        assert "distribute_columns" not in props

    def test_roundrobin(self):
        ddl = "CREATE TABLE t (a INT) DISTRIBUTE BY ROUNDROBIN;"
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "ROUNDROBIN"

    def test_distributed_by_spelling(self):
        """部分导出工具写 DISTRIBUTED BY（过去分词），容错。"""
        ddl = "CREATE TABLE t (a INT) DISTRIBUTED BY HASH (a);"
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (a)"
        assert props["distribute_columns"] == ["a"]

    def test_clause_order_distribute_before_partition(self):
        """DISTRIBUTE BY 在 PARTITION BY 之前：两边都要正确切分。"""
        ddl = (
            "CREATE TABLE t (dt DATE, uid INT)\n"
            "DISTRIBUTE BY HASH (uid)\n"
            "PARTITION BY RANGE (dt)\n"
            "(\n"
            "  PARTITION p202401 VALUES LESS THAN ('2024-02-01')\n"
            ");"
        )
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (uid)"
        assert props["partition_by"].startswith("PARTITION BY RANGE (dt)")
        assert "DISTRIBUTE" not in props["partition_by"].upper()

    def test_clause_order_partition_before_distribute(self):
        """PARTITION BY 在 DISTRIBUTE BY 之前：partition_by 截断到下一子句。"""
        ddl = (
            "CREATE TABLE t (dt DATE, uid INT)\n"
            "PARTITION BY RANGE (dt) (PARTITION p1 VALUES LESS THAN ('2024-02-01'))\n"
            "DISTRIBUTE BY HASH (uid)\n"
            "WITH (orientation=column);"
        )
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (uid)"
        assert props["with_options"] == "orientation=column"
        part = props["partition_by"]
        assert part.startswith("PARTITION BY RANGE (dt)")
        assert "DISTRIBUTE" not in part.upper()
        assert "WITH" not in part.upper()

    def test_tablespace(self):
        ddl = "CREATE TABLE t (a INT) TABLESPACE pg_default;"
        props = _extract_table_props(ddl)
        assert props["tablespace"] == "pg_default"

    def test_no_props_returns_empty(self):
        """无任何表尾子句 → 空 dict（不写 __table_props__ key）。"""
        ddl = "CREATE TABLE t (a INT, b VARCHAR(10));"
        assert _extract_table_props(ddl) == {}

    def test_lowercase_keywords(self):
        """关键字小写也识别（DDL 导出工具大小写不统一）。"""
        ddl = "create table t (a int) distribute by hash (a) with (orientation=column);"
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "hash (a)"
        assert props["with_options"] == "orientation=column"

    def test_statement_end_at_semicolon(self):
        """尾部分号之后的内容（下一条语句）不混入。"""
        ddl = (
            "CREATE TABLE t (a INT) DISTRIBUTE BY HASH (a);\n"
            "COMMENT ON TABLE t IS '注释';"
        )
        props = _extract_table_props(ddl)
        assert props["distribute_by"] == "HASH (a)"
        assert "COMMENT" not in props["distribute_by"].upper()


class TestTablePropsInMetadata:
    """parse_ddl_for_metadata：__table_props__ 注入 + is_distribute_key 字段标记。"""

    def test_metadata_carries_table_props(self, tmp_path):
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (uid INT, dt DATE)\n"
            "DISTRIBUTE BY HASH (uid)\n"
            "PARTITION BY RANGE (dt) (PARTITION p1 VALUES LESS THAN ('2024-02-01'));")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert meta["__table_props__"]["distribute_by"] == "HASH (uid)"
        assert meta["__table_props__"]["distribute_columns"] == ["uid"]
        assert meta["__table_props__"]["partition_by"].startswith("PARTITION BY RANGE")
        # 分布键列标记
        assert meta["uid"]["is_distribute_key"] is True
        assert meta["dt"]["is_distribute_key"] is False

    def test_metadata_no_props_no_key(self, tmp_path):
        """无物理属性 → 不写 __table_props__（字段标记 False）。"""
        ddl_dir = _write_ddl(tmp_path, "test_f", "CREATE TABLE test_f (uid INT);")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert "__table_props__" not in meta
        assert meta["uid"]["is_distribute_key"] is False

    def test_comment_only_field_gets_flag(self, tmp_path):
        """COMMENT ON COLUMN 出现 body 外新字段时，同样带 is_distribute_key 键。"""
        ddl_dir = _write_ddl(tmp_path, "test_f",
            "CREATE TABLE test_f (uid INT) DISTRIBUTE BY HASH (uid);\n"
            "COMMENT ON COLUMN test_f.extra IS '补充字段';")
        meta = parse_ddl_for_metadata(ddl_dir, "test_f")
        assert "extra" in meta
        assert meta["extra"]["is_distribute_key"] is False
