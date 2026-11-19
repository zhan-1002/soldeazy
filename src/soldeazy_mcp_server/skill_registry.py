"""Packaged Agent Skill discovery and safe resource reads.

照 eBay 包的同一套做法（``ebay_mcp_server/skill_registry.py``）实现，
因为 WorkBuddy 那类客户端已经在用 ``skill://`` 资源读取流程知识：

* 每个 skill 是一个目录，里面必须有 ``SKILL.md``（带 YAML frontmatter）；
* frontmatter 的 ``name`` 必须与目录名一致，``description`` 不能为空；
* 发现时给每个文件算 sha256，读取时校验——包内容被改动就报错，
  而不是悄悄返回一份和清单不符的文档。
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any
from urllib.parse import urlparse

from soldeazy_mcp_server.errors import SoldeazyError

_NAME_RE = re.compile(r"^[a-z0-9-]{1,64}$")


@dataclass(frozen=True)
class SkillFile:
    uri: str
    digest: str
    size: int
    mime_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "digest": self.digest,
            "size": self.size,
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class SkillEntry:
    name: str
    uri: str
    frontmatter: dict[str, Any]
    resources: tuple[SkillFile, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "frontmatter": dict(self.frontmatter),
            "resources": [item.as_dict() for item in self.resources],
        }


class SkillRegistry:
    def __init__(self) -> None:
        self._root = files("soldeazy_mcp_server").joinpath("skills")
        self._entries = self._discover()

    def list(self) -> list[SkillEntry]:
        return [self._entries[name] for name in sorted(self._entries)]

    def get(self, name_or_uri: str) -> SkillEntry:
        name = _skill_name(name_or_uri)
        try:
            return self._entries[name]
        except KeyError as exc:
            raise SoldeazyError(
                "SOLDEAZY_SKILL_NOT_FOUND",
                f"没有这个 skill：{name}",
                details={"available": sorted(self._entries)},
            ) from exc

    def read(self, uri: str) -> tuple[str, str]:
        parsed = urlparse(uri)
        if parsed.scheme != "skill" or not _NAME_RE.fullmatch(parsed.netloc):
            raise SoldeazyError("SOLDEAZY_INVALID_SKILL_URI", f"skill URI 不合法：{uri}")
        name = parsed.netloc
        relative = parsed.path.lstrip("/")
        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise SoldeazyError("SOLDEAZY_INVALID_SKILL_URI", f"skill 路径不合法：{uri}")
        entry = self.get(name)
        allowed = {item.uri: item for item in entry.resources}
        if uri not in allowed:
            raise SoldeazyError(
                "SOLDEAZY_SKILL_RESOURCE_NOT_FOUND",
                f"这个资源不在 skill 清单里：{uri}",
            )
        resource = self._root.joinpath(name)
        for part in relative.split("/"):
            resource = resource.joinpath(part)
        try:
            raw = resource.read_bytes()
        except OSError as exc:
            raise SoldeazyError(
                "SOLDEAZY_SKILL_RESOURCE_NOT_FOUND",
                f"读不到 skill 资源：{uri}",
            ) from exc
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if digest != allowed[uri].digest or len(raw) != allowed[uri].size:
            raise SoldeazyError(
                "SOLDEAZY_SKILL_INTEGRITY_ERROR",
                f"skill 资源与清单不一致（包内容被改过）：{uri}",
            )
        return raw.decode("utf-8"), allowed[uri].mime_type

    def _discover(self) -> dict[str, SkillEntry]:
        entries: dict[str, SkillEntry] = {}
        for directory in sorted(self._root.iterdir(), key=lambda value: value.name):
            if not directory.is_dir() or not _NAME_RE.fullmatch(directory.name):
                continue
            skill_file = directory.joinpath("SKILL.md")
            if not skill_file.is_file():
                continue
            raw = skill_file.read_bytes()
            frontmatter = _parse_frontmatter(raw.decode("utf-8"))
            name = str(frontmatter.get("name") or "")
            description = str(frontmatter.get("description") or "")
            if name != directory.name or not description:
                raise RuntimeError(
                    f"skill 元数据不合法（目录名与 name 不一致，或 description 为空）："
                    f"{directory.name}"
                )
            manifest: list[SkillFile] = []
            for relative, child in _walk_files(directory):
                child_raw = child.read_bytes()
                mime = mimetypes.guess_type(relative)[0] or "application/octet-stream"
                manifest.append(
                    SkillFile(
                        uri=f"skill://{name}/{relative}",
                        digest="sha256:" + hashlib.sha256(child_raw).hexdigest(),
                        size=len(child_raw),
                        mime_type=mime,
                    )
                )
            entries[name] = SkillEntry(
                name=name,
                uri=f"skill://{name}/SKILL.md",
                frontmatter=frontmatter,
                resources=tuple(manifest),
            )
        return entries


def _skill_name(name_or_uri: str) -> str:
    value = (name_or_uri or "").strip()
    if value.startswith("skill://"):
        value = urlparse(value).netloc
    if not _NAME_RE.fullmatch(value):
        raise SoldeazyError("SOLDEAZY_INVALID_SKILL_NAME", f"skill 名不合法：{value}")
    return value


def _walk_files(directory, prefix: str = ""):
    for child in sorted(directory.iterdir(), key=lambda value: value.name):
        relative = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            yield from _walk_files(child, relative)
        elif child.is_file():
            yield relative.replace("\\", "/"), child


def _parse_frontmatter(text: str) -> dict[str, Any]:
    # Git for Windows 默认 autocrlf、解压工具也可能写 BOM。必须在判断
    # frontmatter 之前归一化，否则这里一抛错整个 server 就 import 不了。
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise RuntimeError("SKILL.md 必须以 YAML frontmatter 开头")
    marker = normalized.find("\n---\n", 4)
    if marker < 0:
        raise RuntimeError("SKILL.md 的 frontmatter 没有结束标记")
    values: dict[str, Any] = {}
    for line in normalized[4:marker].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition(":")
        if not separator:
            raise RuntimeError(f"frontmatter 行不合法：{line}")
        value: Any = raw.strip().strip('"').strip("'")
        if value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        values[key.strip()] = value
    if not values.get("name") or not values.get("description"):
        raise RuntimeError("SKILL.md 必须有 name 和 description")
    return values
