"""
Trace 收集器：记录 ReAct 每一步的可审计信息
评审重点：展示执行过程而非完整 CoT
"""
import time
from typing import Optional, Any
from .schemas import TraceStep


class TraceCollector:
    def __init__(self) -> None:
        self._steps: list[TraceStep] = []
        self._step_id = 0

    def add(
        self,
        step_type: str,
        *,
        skill_name: Optional[str] = None,
        prompt_version: Optional[str] = None,
        context_summary: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_input: Optional[dict] = None,
        tool_output: Optional[Any] = None,
        output_summary: Optional[str] = None,
        latency_ms: Optional[int] = None,
        error: Optional[str] = None,
    ) -> TraceStep:
        self._step_id += 1
        step = TraceStep(
            step_id=self._step_id,
            step_type=step_type,
            skill_name=skill_name,
            prompt_version=prompt_version,
            context_summary=context_summary,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            output_summary=output_summary,
            latency_ms=latency_ms,
            error=error,
        )
        self._steps.append(step)
        return step

    def steps(self) -> list[TraceStep]:
        return list(self._steps)


class Timer:
    """with Timer() as t: ... ; t.ms"""
    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ms = int((time.perf_counter() - self._t0) * 1000)
