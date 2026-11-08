"""
function_block.py — Adapter that allows using plain functions as Blocks.

Usage:
    @as_tool
    async def fetch_weather(city: str) -> str:
        '''Returns the current weather for a city.'''
        return f"Sunny in {city}"

    @as_tool(name="weather", description="Queries the current weather.")
    def fetch_weather_sync(city: str) -> str:
        return f"Sunny in {city}"

    agent = LLMAgentBlock(name="agent", tools=[fetch_weather, fetch_weather_sync])
"""

import asyncio
import inspect
from typing import Any, Callable, Optional, Type, get_type_hints

from pydantic import BaseModel, PrivateAttr, create_model

from agenticblocks.core.block import Block


class FunctionOutput(BaseModel):
    """Default output for functions that return primitive values or dicts."""

    model_config = {"arbitrary_types_allowed": True}
    result: Any


def _build_input_model(func: Callable) -> tuple[Type[BaseModel], Optional[str]]:
    """Dynamically builds a Pydantic model from the function's parameters."""
    sig = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    params = [p for name, p in sig.parameters.items() if name not in ("self", "cls")]
    if len(params) == 1:
        param_name = params[0].name
        annotation = hints.get(param_name, Any)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation, param_name

    fields: dict[str, Any] = {}
    for param_name, param in sig.parameters.items():
        if param_name in ("self", "cls"):
            continue
        annotation = hints.get(param_name, Any)
        if param.default is inspect.Parameter.empty:
            fields[param_name] = (annotation, ...)
        else:
            fields[param_name] = (annotation, param.default)

    model_name = f"{''.join(w.title() for w in func.__name__.split('_'))}Input"
    return create_model(model_name, **fields), None  # type: ignore[call-overload]


class FunctionBlock(Block):
    """
    Adapts a Python function (sync or async) to the Block interface.

    Supports:
    - async def: called directly with await.
    - def: executed in a thread pool via asyncio.to_thread (does not block the event loop).

    The function return value is normalised:
    - BaseModel → returned directly.
    - dict     → serialised as FunctionOutput(result=<dict>).
    - any other → wrapped in FunctionOutput(result=<value>).
    """

    model_config = {"arbitrary_types_allowed": True}

    # Private attributes — not exposed in Pydantic schema or serialised.
    _func: Callable = PrivateAttr()
    _input_model: Type[BaseModel] = PrivateAttr()
    _pass_model_param: Optional[str] = PrivateAttr(default=None)

    def __init__(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        **data: Any,
    ) -> None:
        func_name = name or func.__name__
        func_desc = description or inspect.getdoc(func) or f"Executes {func_name}"
        super().__init__(name=func_name, description=func_desc, **data)
        self._func = func
        self._input_model, self._pass_model_param = _build_input_model(func)

    # Instance override: returns the model generated specifically for this function.
    # Since input_schema is a classmethod on Block, we call it via the instance —
    # Python resolves the instance method first when available in the concrete class __dict__.
    def input_schema(self) -> Type[BaseModel]:  # type: ignore[override]
        return self._input_model

    async def run(self, input: BaseModel) -> BaseModel:  # type: ignore[override]
        if self._pass_model_param:
            kwargs = {self._pass_model_param: input}
        else:
            kwargs = input.model_dump()

        if asyncio.iscoroutinefunction(self._func):
            raw = await self._func(**kwargs)
        else:
            # Synchronous functions run in a thread pool so as not to block the event loop.
            raw = await asyncio.to_thread(self._func, **kwargs)

        # Normalise return value
        if isinstance(raw, BaseModel):
            return raw
        return FunctionOutput(result=raw)


# ---------------------------------------------------------------------------
# Decorator @as_tool
# ---------------------------------------------------------------------------

def as_tool(
    func: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    concurrency_safe: bool = False,
) -> Any:
    """
    Converts a function (sync or async) into a FunctionBlock ready to be
    used as a tool in LLMAgentBlock.tools — without changing the Block interface.

    Can be used in two ways:

        @as_tool
        async def my_tool(param: str) -> str: ...

        @as_tool(name="name", description="Description.")
        def my_tool(param: str) -> str: ...

    ``concurrency_safe=True`` lets an agent block run the tool together with
    the other concurrency-safe calls of one model response (see
    ``Block.concurrency_safe``).
    """
    def _wrap(f: Callable) -> FunctionBlock:
        return FunctionBlock(func=f, name=name, description=description, concurrency_safe=concurrency_safe)

    if func is not None:
        # Called without parentheses: @as_tool
        return _wrap(func)

    # Called with parentheses: @as_tool(...)
    return _wrap
