from pydantic import BaseModel
from typing import TypeVar, Generic, Any, get_type_hints

Input = TypeVar("Input", bound=BaseModel)
Output = TypeVar("Output", bound=BaseModel)

class Block(BaseModel, Generic[Input, Output]):
    name: str
    description: str = ""
    # A tool that may run at the same time as the other concurrency-safe calls
    # of the batch a model issued: it shares no mutable state with them and
    # needs no answer from the user while it runs. Agent blocks that honour it
    # (MemGPTAgentBlock) run consecutive such calls with asyncio.gather and keep
    # every result in the order the calls were made. False runs it alone.
    concurrency_safe: bool = False

    async def run(self, input: Input) -> Output:
        raise NotImplementedError

    @classmethod
    def input_schema(cls) -> type[BaseModel]:
        try:
            hints = get_type_hints(cls.run)
            if 'input' in hints:
                return hints['input']
        except Exception:
            pass
        return BaseModel

    @classmethod
    def output_schema(cls) -> type[BaseModel]:
        try:
            hints = get_type_hints(cls.run)
            if 'return' in hints:
                return hints['return']
        except Exception:
            pass
        return BaseModel
