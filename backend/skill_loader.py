"""
Skill Loader（参考 https://agentskills.io/specification）

约定：
  - 一个 Skill 就是一个目录，必须包含 SKILL.md
  - SKILL.md 顶部 YAML frontmatter:
        ---
        name: lead-scoring-followup       # 必填，唯一标识，建议 kebab-case
        description: <一句话描述>          # 必填，给路由决策用
        version: 0.1.0                    # 可选，缺省时为 "0.0.0"
        allowed_tools:                    # 可选，白名单；为空表示不限
          - query_product
        ---
  - frontmatter 之后是 Markdown 正文 = instructions（注入到 system prompt）

设计要点：
  - frontmatter 缺 name 或 description -> 直接报错（Skill 没法被路由）
  - allowed_tools 缺失 -> 默认空列表（即 Runner 不允许调用任何 Tool，必须显式开放）
  - 支持加载多个 Skill：load_all_skills 扫描根目录下每个子目录的 SKILL.md
  - 不做 instructions 的格式校验，让候选人自由组织 Skill 内容
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

SKILL_FILE = "SKILL.md"
FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    version: str
    allowed_tools: tuple[str, ...]
    instructions: str  # 注入到 system prompt
    source_path: Path  # 来源 SKILL.md 路径（Trace 里方便审计）

    @property
    def prompt_version(self) -> str:
        """给 Trace 用：name@version，便于审计本次执行用了哪个版本"""
        return f"{self.name}@{self.version}"

    def is_tool_allowed(self, tool_name: str) -> bool:
        # 空 allowed_tools = 不允许任何 Tool；显式声明才放开
        return tool_name in self.allowed_tools


class SkillLoadError(ValueError):
    """SKILL.md 解析失败时抛出。Runner 应该捕获后写入 Trace。"""


def _parse_skill_md(text: str, source: Path) -> Skill:
    m = FRONTMATTER_RE.match(text)
    if not m:
        raise SkillLoadError(
            f"{source}: 未找到合法的 YAML frontmatter（应以 '---' 包裹 name/description 等字段）"
        )

    try:
        meta = yaml.safe_load(m.group("frontmatter")) or {}
    except yaml.YAMLError as e:
        raise SkillLoadError(f"{source}: frontmatter YAML 解析失败 - {e}") from e

    if not isinstance(meta, dict):
        raise SkillLoadError(f"{source}: frontmatter 必须是键值对（dict），实际是 {type(meta).__name__}")

    name = meta.get("name")
    description = meta.get("description")
    if not name or not isinstance(name, str):
        raise SkillLoadError(f"{source}: frontmatter 缺少必填字段 'name'")
    if not description or not isinstance(description, str):
        raise SkillLoadError(f"{source}: frontmatter 缺少必填字段 'description'")

    version = str(meta.get("version", "0.0.0"))

    allowed_tools_raw = meta.get("allowed_tools") or []
    if not isinstance(allowed_tools_raw, list):
        raise SkillLoadError(
            f"{source}: allowed_tools 必须是列表，实际是 {type(allowed_tools_raw).__name__}"
        )
    allowed_tools = tuple(str(t).strip() for t in allowed_tools_raw if str(t).strip())

    body = m.group("body").strip()
    if not body:
        raise SkillLoadError(f"{source}: SKILL.md 正文为空，instructions 不能缺失")

    return Skill(
        name=name.strip(),
        description=description.strip(),
        version=version,
        allowed_tools=allowed_tools,
        instructions=body,
        source_path=source,
    )


def load_skill(skill_dir: Path) -> Skill:
    """从单个 Skill 目录加载 SKILL.md"""
    if not skill_dir.is_dir():
        raise SkillLoadError(f"Skill 目录不存在: {skill_dir}")
    md_path = skill_dir / SKILL_FILE
    if not md_path.is_file():
        raise SkillLoadError(f"未找到 {SKILL_FILE}: {md_path}")
    text = md_path.read_text(encoding="utf-8")
    return _parse_skill_md(text, md_path)


def _iter_skill_dirs(skills_root: Path) -> Iterator[Path]:
    if not skills_root.is_dir():
        return
    for child in sorted(skills_root.iterdir()):
        if child.is_dir() and (child / SKILL_FILE).is_file():
            yield child


def load_all_skills(skills_root: Path) -> dict[str, Skill]:
    """扫描 skills/ 下所有含 SKILL.md 的目录，返回 {skill_name: Skill}"""
    result: dict[str, Skill] = {}
    for skill_dir in _iter_skill_dirs(skills_root):
        skill = load_skill(skill_dir)
        if skill.name in result:
            raise SkillLoadError(
                f"Skill 名字重复: '{skill.name}' 同时出现在 "
                f"{result[skill.name].source_path} 和 {skill.source_path}"
            )
        result[skill.name] = skill
    return result
