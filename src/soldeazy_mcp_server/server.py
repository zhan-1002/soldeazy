"""Entry point for the standalone Soldeazy MCP server."""

from __future__ import annotations

import argparse
from typing import Any

from soldeazy_mcp_server import tools  # noqa: F401  导入即注册 16 个工具
from soldeazy_mcp_server.runtime import mcp
from soldeazy_mcp_server.skill_registry import SkillRegistry

#: 打包在包内的流程知识（skills/ 目录）。
#:
#: 为什么要有这个：工具 docstring 说不完整条链路，而"站点/店铺留空时范围怎么定"
#: 这类判断，客户端 AI 只能从一次报错里摸索——摸不通就反复反问用户。
#: 与 eBay 服务用同一套 ``skill://`` 机制，客户端可以主动读。
skill_registry = SkillRegistry()


def _register_skill_resources() -> None:
    """把打包的 skill 文件注册成 MCP 资源（``skill://...``）。"""

    def make_reader(uri: str):
        async def read_skill_resource() -> str:
            text, _mime_type = skill_registry.read(uri)
            return text

        safe_name = uri.removeprefix("skill://").replace("/", "_").replace("-", "_")
        read_skill_resource.__name__ = f"read_{safe_name}"
        return read_skill_resource

    for entry in skill_registry.list():
        for resource in entry.resources:
            mcp.resource(
                resource.uri,
                name=f"{entry.name}:{resource.uri.rsplit('/', 1)[-1]}",
                title=str(entry.frontmatter.get("name") or entry.name),
                description=str(entry.frontmatter.get("description") or ""),
                mime_type=resource.mime_type,
            )(make_reader(resource.uri))


_register_skill_resources()


def disable_output_schema() -> None:
    """关掉自动生成的 ``outputSchema``。

    FastMCP 会给每个返回 dict 的工具生成 ``{"additionalProperties": true}``——
    零信息，但每个请求都随工具清单发一遍。关掉后调用方拿到的仍是同一份
    JSON 文本，只是不再有 ``structuredContent``（与拆出去的 eBay 服务口径一致）。
    """

    for tool in mcp._tool_manager._tools.values():
        tool.fn_metadata.output_schema = None
        # output_schema 是 cached_property，读过一次就缓存进实例字典，必须清掉
        tool.__dict__.pop("output_schema", None)


def exposed_tools() -> list[str]:
    """当前暴露的工具名（全部 16 个——这个服务只有 Soldeazy 一族工具）。"""

    return sorted(mcp._tool_manager._tools)


# 所有工具注册完之后才能动工具面
disable_output_schema()
EXPOSED_TOOLS = exposed_tools()


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Soldeazy datasheet MCP server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport; stdio for per-session use, sse for one shared process.",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
