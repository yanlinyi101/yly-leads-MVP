"""
持久化辅助层（jsonl 追加 / 读取 + playbook 文件名安全校验）

这里把"数据目录"做成可配置：
  - 缺省取 PROJECT_ROOT / "data"
  - 可通过环境变量 AI_GROWTH_DATA_DIR 覆盖
  - 也可被测试 monkeypatch 直接替换 _DATA_DIR_OVERRIDE

这样测试可以用 tmp_path 隔离，不会污染真实 data/ 目录。

为什么单独抽 persistence 模块（不直接在 main.py 写）：
  - jsonl / playbook 多处复用，集中一处写 helper
  - 路径校验逻辑（safe_playbook_path）是安全关键代码，集中评审
  - 数据目录抽象化（get_data_dir）方便测试 / 多环境部署
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

# 测试 monkeypatch 直接设这个变量即可切换数据目录
_DATA_DIR_OVERRIDE: Optional[Path] = None


def set_data_dir(path: Path | str | None) -> None:
    """显式切换数据目录。传 None 表示恢复默认。

    测试场景常用：tmp_path / "data" 隔离每个测试。
    """
    global _DATA_DIR_OVERRIDE
    _DATA_DIR_OVERRIDE = Path(path) if path is not None else None


def get_data_dir() -> Path:
    """读取当前生效的数据目录。

    优先级：set_data_dir() 显式设置 > AI_GROWTH_DATA_DIR 环境变量 > 默认 PROJECT_ROOT/data。
    """
    if _DATA_DIR_OVERRIDE is not None:
        return _DATA_DIR_OVERRIDE
    env = os.getenv("AI_GROWTH_DATA_DIR")
    if env:
        return Path(env)
    return _DEFAULT_DATA_DIR


# -----------------------------------------------------------------------------
# 时间戳
# -----------------------------------------------------------------------------

def iso_now() -> str:
    """ISO8601 时间戳（UTC，秒精度）。

    单独抽出来：测试可以 monkeypatch 这一个函数固定时间。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -----------------------------------------------------------------------------
# JSONL 读写
# -----------------------------------------------------------------------------

def append_jsonl(filename: str, record: dict) -> None:
    """往 data/<filename> 追加一行 JSON。文件不存在时自动创建（含父目录）。

    用 ensure_ascii=False 保留中文可读性（便于人肉审计）。
    """
    path = get_data_dir() / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def read_jsonl(filename: str) -> list[dict]:
    """读取 data/<filename> 的所有行（健壮性优先）。

    - 文件不存在 → 返回空列表（不抛异常，让上层 endpoint 不必判 404）
    - 损坏的行 → 跳过并 logger.warning（一行坏不能拉低整个查询）
    """
    path = get_data_dir() / filename
    if not path.is_file():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8") as fp:
        for i, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning("跳过 %s 第 %d 行损坏 JSON: %s", filename, i, e)
                continue
    return out


# -----------------------------------------------------------------------------
# 安全的 playbook 路径
# -----------------------------------------------------------------------------

# 文件名校验：只允许 a-z A-Z 0-9 _ - 共 1~40 个字符 + .md
# 这条正则故意写在模块顶端，方便审计；任何变更都应被 review。
_PLAYBOOK_NAME_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,40}\.md$")


class PlaybookNameError(ValueError):
    """非法 playbook 文件名。路由层应捕获后返回 4xx。"""


def get_playbooks_dir() -> Path:
    """custom_playbooks 目录（不存在时按需创建）。"""
    p = get_data_dir() / "custom_playbooks"
    return p


def safe_playbook_path(name: str) -> Path:
    """把 name 严格校验后返回拼好的绝对路径。

    防护重点：
      1. 正则强约束 → 任何含 '..'、'/'、'\\'、绝对路径前缀的名字都会被拒
      2. 额外二道防线：拼接后再用 Path.resolve() 比对父目录前缀
         （正则已经够严，但多一道安全网总没坏处；防"未来不小心放宽正则"）

    入参 `name` 必须包含 .md 后缀（前端 / API 调用方约定）。
    """
    if not isinstance(name, str) or not name:
        raise PlaybookNameError("playbook 名字不能为空")
    if not _PLAYBOOK_NAME_RE.match(name):
        raise PlaybookNameError(
            f"非法 playbook 名字: {name!r}；只允许 ^[a-zA-Z0-9_-]{{1,40}}\\.md$"
        )
    base = get_playbooks_dir().resolve()
    candidate = (get_playbooks_dir() / name).resolve()
    # 二道防线：拼好后必须仍在 base 下
    try:
        candidate.relative_to(base)
    except ValueError as e:
        raise PlaybookNameError(f"路径越界: {name!r}") from e
    return candidate


# -----------------------------------------------------------------------------
# Playbook frontmatter 解析（极简）
# -----------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(?P<frontmatter>.*?)\n---\s*\n(?P<body>.*)\Z",
    re.DOTALL,
)


def parse_playbook_text(text: str) -> tuple[dict, str]:
    """解析 playbook 文件文本，返回 (frontmatter_dict, body_str)。

    没有 frontmatter 时返回 ({}, text)。
    出于轻量考量，不引入 PyYAML（已经在 skill_loader 用了，但 playbook 字段简单
    可以用纯 Python 行解析；这里仍走 PyYAML 保持一致）。
    """
    import yaml  # 局部 import 避免每次 import 整个模块都拉 yaml

    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(m.group("frontmatter")) or {}
    except yaml.YAMLError as e:
        logger.warning("playbook frontmatter 解析失败：%s", e)
        return {}, text.strip()
    if not isinstance(meta, dict):
        return {}, m.group("body").strip()
    return meta, m.group("body").strip()


def dump_playbook_text(meta: dict, body: str) -> str:
    """把 frontmatter dict + body 合并成 .md 文件文本。"""
    import yaml
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


# -----------------------------------------------------------------------------
# Playbook 列表 + 内容（供 main 路由 & runner 共用）
# -----------------------------------------------------------------------------

def list_playbooks(include_example: bool = True) -> list[dict]:
    """列出所有 playbook 的元信息。

    返回字段：name / title / updated_at / size，按 updated_at desc 排序。
    """
    pdir = get_playbooks_dir()
    if not pdir.is_dir():
        return []
    out: list[dict] = []
    for f in pdir.iterdir():
        if not f.is_file() or f.suffix != ".md":
            continue
        if not include_example and f.name == "_example.md":
            continue
        # 文件名形式被允许（runner 加载时还会再校验）
        try:
            text = f.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读取 playbook 失败 %s: %s", f, e)
            continue
        meta, _ = parse_playbook_text(text)
        title = str(meta.get("title") or f.stem)
        stat = f.stat()
        out.append({
            "name": f.name,
            "title": title,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "size": stat.st_size,
        })
    out.sort(key=lambda r: r["updated_at"], reverse=True)
    return out


def iter_active_playbooks() -> Iterable[tuple[str, str, str]]:
    """供 Runner 在 build_system_prompt 时遍历"应当注入到 prompt 的"playbook。

    生成 (name, title, body)，自动跳过 _example.md。
    单个文件加载失败不抛异常，logger.warning。
    """
    pdir = get_playbooks_dir()
    if not pdir.is_dir():
        return
    for f in sorted(pdir.iterdir()):
        if not f.is_file() or f.suffix != ".md":
            continue
        if f.name == "_example.md":
            continue
        try:
            text = f.read_text(encoding="utf-8")
            meta, body = parse_playbook_text(text)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("加载自定义 playbook 失败 %s: %s", f.name, e)
            continue
        title = str(meta.get("title") or f.stem)
        if not body.strip():
            logger.warning("自定义 playbook %s 正文为空，跳过", f.name)
            continue
        yield f.name, title, body
