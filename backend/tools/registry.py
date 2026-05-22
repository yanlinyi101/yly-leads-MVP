"""
Tool 注册表：根据名字调用对应 Tool 函数
当前只有一个 query_product，但 Loader 设计成支持注册多个

TODO(候选人实现):
  - register(name, fn, schema)
  - call(name, args) -> tool_output
  - schema 给前端/LLM 用，描述参数
"""
from typing import Callable, Any
from dataclasses import dataclass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict   # JSON schema 风格
    fn: Callable[[dict], Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def call(self, name: str, args: dict) -> Any:
        if name not in self._tools:
            raise KeyError(f"未注册的 Tool: {name}")
        return self._tools[name].fn(args)

    def describe_all(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters_schema": t.parameters_schema,
            }
            for t in self._tools.values()
        ]
