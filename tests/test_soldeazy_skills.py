"""打包的流程知识（skills）必须可发现、可读、内容与清单一致。

为什么需要：WorkBuddy 里的 AI 反复追问「同一 SKU 在多个店铺都有，改价范围怎么定」，
根因是工具的 docstring 说不完整条链路，它只能从一次报错里摸索。
eBay 服务用 ``skill://`` 资源解决了这个问题，Soldeazy 侧此前完全没有。

这里的断言不是"文件存在"，而是**真的能读出内容**，
并且内容里确实写了那几个会反复被问到的问题。
"""

from __future__ import annotations

from soldeazy_mcp_server.skill_registry import SkillRegistry

#: 必须覆盖的流程知识（对应实际被反复问到的判断题）。
REQUIRED = {
    "soldeazy-price-change",
    "soldeazy-stock-change",
    "soldeazy-scope-and-limits",
    "soldeazy-troubleshooting",
}


def test_all_required_skills_are_discovered() -> None:
    registry = SkillRegistry()
    names = {entry.name for entry in registry.list()}

    missing = REQUIRED - names
    assert not missing, f"缺少 skill：{sorted(missing)}"


def test_every_skill_declares_name_and_description() -> None:
    registry = SkillRegistry()
    for entry in registry.list():
        assert entry.name, entry.uri
        assert entry.frontmatter.get("description"), f"{entry.name} 缺 description"
        # frontmatter 的 name 必须与目录名一致（registry 已校验，这里再钉一层）
        assert entry.frontmatter.get("name") == entry.name


def test_read_returns_the_actual_document() -> None:
    """不是"文件存在"，而是内容真的能被读出来且像一份文档。"""

    registry = SkillRegistry()
    entry = registry.get("soldeazy-price-change")
    text, mime_type = registry.read(entry.uri)

    assert "text/markdown" in mime_type
    assert len(text) > 500, "文档太短，可能不是真内容"
    assert "# 改价完整流程" in text


def test_price_skill_answers_the_scope_question() -> None:
    """改价 skill 必须明确回答「店铺留空怎么办」——这是 AI 反复追问的那个点。"""

    registry = SkillRegistry()
    text, _ = registry.read(registry.get("soldeazy-price-change").uri)

    assert "全部店铺" in text
    assert "不要停下来问" in text or "不要反问" in text
    # 同一 SKU 跨店铺是常态，这点必须写清
    assert "多个店铺" in text or "跨店铺" in text


def test_scope_skill_documents_both_limits() -> None:
    """3000 个数据表 与 200 行/页 是两个不同的截断点，不能混。"""

    registry = SkillRegistry()
    text, _ = registry.read(registry.get("soldeazy-scope-and-limits").uri)

    assert "3000" in text
    assert "200" in text
    assert "RANGE_TOO_LARGE" in text


def test_read_rejects_unknown_skill_with_a_helpful_error() -> None:
    import pytest

    from soldeazy_mcp_server.errors import SoldeazyError

    registry = SkillRegistry()
    with pytest.raises(SoldeazyError) as caught:
        registry.get("no-such-skill")

    assert caught.value.code == "SOLDEAZY_SKILL_NOT_FOUND"
    # 报错要把可用的列出来，别让人猜
    assert caught.value.details.get("available")


def test_read_rejects_a_tampered_uri() -> None:
    """不在清单里的资源一律拒绝（防路径穿越与内容替换）。"""

    import pytest

    from soldeazy_mcp_server.errors import SoldeazyError

    registry = SkillRegistry()
    with pytest.raises(SoldeazyError):
        registry.read("skill://soldeazy-price-change/../../etc/passwd")


def test_skill_tool_lists_and_reads() -> None:
    """工具层也要能给：不传名字列清单，传了给全文。"""

    import asyncio

    from soldeazy_mcp_server import tools

    listing = asyncio.run(tools.soldeazy_read_skill())
    assert listing["success"] is True
    names = {item["name"] for item in listing["data"]["skills"]}
    assert REQUIRED <= names

    single = asyncio.run(tools.soldeazy_read_skill("soldeazy-price-change"))
    assert single["success"] is True
    assert "改价完整流程" in single["data"]["content"]
