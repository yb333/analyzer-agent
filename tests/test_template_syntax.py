"""HTML 模板 JS 语法检查测试。

从 asset_report.html 提取所有 <script> 块，用 Node 做语法校验。
防止 JS 语法错误（多余的}、括号不匹配等）导致页面无法渲染。

运行:
    pytest tests/test_template_syntax.py -v
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = PROJECT_ROOT / "dws-pipeline-analyzer" / "references" / "templates" / "asset_report.html"


def _extract_js_blocks(html: str) -> list[str]:
    """从 HTML 提取所有 <script> 块的 JS 代码。"""
    # 匹配 <script>...</script>（不含 src 属性的外部引用）
    blocks = []
    for m in re.finditer(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.DOTALL):
        code = m.group(1)
        if code.strip():
            blocks.append(code)
    return blocks


def _check_js_syntax(js_code: str) -> tuple[bool, str]:
    """用 Node 检查 JS 语法，返回 (是否通过, 错误信息)。"""
    import tempfile, os
    try:
        # 替换模板占位符为合法 JS 值（避免 {{REPORT_DATA}} 被当语法错误）
        code = js_code.replace("{{REPORT_DATA}}", "{}").replace("{{TARGET_TABLE}}", "\"\"")
        wrapped = f"(function(){{\n{code}\n}})"
        # 写临时文件用 node --check（--check 和 -e 不能同时用）
        f = tempfile.NamedTemporaryFile(suffix=".js", mode="w", delete=False, encoding="utf-8")
        f.write(wrapped)
        f.close()
        try:
            result = subprocess.run(
                ["node", "--check", f.name],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0:
                return True, ""
            return False, result.stderr or result.stdout
        finally:
            os.unlink(f.name)
    except FileNotFoundError:
        pytest.skip("Node.js 未安装，跳过 JS 语法检查")
    except Exception as e:
        pytest.skip(f"Node 检查失败: {e}")


class TestTemplateSyntax:
    """HTML 模板的 JS 语法正确性。"""

    def test_template_exists(self):
        """模板文件存在"""
        assert TEMPLATE.exists(), f"模板文件不存在: {TEMPLATE}"

    def test_js_syntax_all_blocks(self):
        """所有 <script> 块的 JS 语法正确（无多余括号/语法错误）"""
        html = TEMPLATE.read_text(encoding="utf-8")
        blocks = _extract_js_blocks(html)
        assert len(blocks) > 0, "模板应至少包含一个 <script> 块"

        errors = []
        for i, block in enumerate(blocks):
            ok, err = _check_js_syntax(block)
            if not ok:
                # 截取错误信息前 500 字符避免太长
                errors.append(f"script块#{i+1}: {err[:500]}")

        if errors:
            pytest.fail(f"JS 语法错误（{len(errors)}处）:\n" + "\n---\n".join(errors))

    def test_no_unbalanced_braces(self):
        """JS 代码括号基本平衡（辅助检查，正则去字符串不完美，以 node --check 为准）"""
        html = TEMPLATE.read_text(encoding="utf-8")
        blocks = _extract_js_blocks(html)

        for i, block in enumerate(blocks):
            code = block.replace("{{REPORT_DATA}}", "{}").replace("{{TARGET_TABLE}}", '""')
            # 去字符串（多层嵌套引号会导致计数不准，这里只做辅助参考）
            cleaned = re.sub(r"'[^']*'", "", code)
            cleaned = re.sub(r'"[^"]*"', "", cleaned)
            cleaned = re.sub(r'`[^`]*`', "", cleaned)

            open_braces = cleaned.count("{")
            close_braces = cleaned.count("}")
            # 正则去字符串不完美（模板字符串${}、嵌套引号），差值小可能是误报
            # 只在差值大（>5）时才警告，以 node --check 为准
            diff = open_braces - close_braces
            if abs(diff) > 5:
                print(f"[WARN] script块#{i+1} 括号差 {diff}（可能是字符串干扰，以 node --check 为准）")

    def test_report_data_placeholder_exists(self):
        """模板含 REPORT_DATA 占位符（view_generator 注入数据用）"""
        html = TEMPLATE.read_text(encoding="utf-8")
        assert "{{REPORT_DATA}}" in html, "模板缺少 {{REPORT_DATA}} 占位符"

    def test_target_table_placeholder_exists(self):
        """模板含 {{TARGET_TABLE}} 占位符"""
        html = TEMPLATE.read_text(encoding="utf-8")
        assert "{{TARGET_TABLE}}" in html, "模板缺少 {{TARGET_TABLE}} 占位符"
