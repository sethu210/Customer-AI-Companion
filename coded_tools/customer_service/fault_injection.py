"""
Fault injection for the data layer, so failure handling can be tested without breaking a real backend.

    CUSTOMER_DATA_FAULTS = "<function|*>:<mode>[:<count>], ..."

    mode   timeout      raise TimeoutError                 (retryable)
           error        raise ConnectionError              (retryable transient backend error)
           broken       raise RuntimeError                 (permanent backend error, not retried)
           permission   raise PermissionError              (credentials refused, not retried)
           slow         wait FAULT_SLOW_S seconds (default 2), then answer normally
    count  how many calls fail before the function recovers (default: every call)

Examples:
    get_stuck_cases:timeout:1        the first call times out, the retry succeeds
    *:error                          the whole backend is down
    resolve_channel_identity:permission

get_adapter() wraps the configured backend with FaultInjectingAdapter when the variable is set.
"""

import asyncio
import os
from typing import Any

from coded_tools.customer_service.data_adapter import CustomerDataAdapter

MODES = {"timeout", "error", "broken", "permission", "slow"}


def parse_faults(spec: str) -> dict[str, list[Any]]:
    faults: dict[str, list[Any]] = {}
    for part in (p.strip() for p in (spec or "").split(",")):
        if not part:
            continue
        bits = part.split(":")
        if len(bits) < 2 or bits[1] not in MODES:
            raise ValueError(f"bad fault {part!r}; expected <function|*>:<{'|'.join(sorted(MODES))}>[:count]")
        faults[bits[0]] = [bits[1], int(bits[2]) if len(bits) > 2 else -1]
    return faults


class FaultInjectingAdapter(CustomerDataAdapter):
    def __init__(self, inner: CustomerDataAdapter, faults: dict[str, list[Any]]) -> None:
        self.inner = inner
        self.faults = faults
        self.name = f"{inner.name}+faults"
        self.calls: dict[str, int] = {}

    async def _maybe_fail(self, function: str) -> None:
        self.calls[function] = self.calls.get(function, 0) + 1
        fault = self.faults.get(function) or self.faults.get("*")
        if not fault or fault[1] == 0:
            return
        if fault[1] > 0:
            fault[1] -= 1
        mode = fault[0]
        if mode == "slow":
            await asyncio.sleep(float(os.environ.get("FAULT_SLOW_S", "2")))
            return
        raise {"timeout": TimeoutError, "error": ConnectionError, "broken": RuntimeError,
               "permission": PermissionError}[mode](f"injected {mode} fault in {function}")


def _wrap(function: str):
    async def method(self: FaultInjectingAdapter, *args: Any, **kwargs: Any) -> Any:
        await self._maybe_fail(function)
        return await getattr(self.inner, function)(*args, **kwargs)

    method.__name__ = function
    return method


for _name in sorted(CustomerDataAdapter.__abstractmethods__):
    setattr(FaultInjectingAdapter, _name, _wrap(_name))
FaultInjectingAdapter.__abstractmethods__ = frozenset()
