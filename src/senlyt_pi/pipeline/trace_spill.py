"""호환 shim — 정본은 `senlyt_pi.persistence.trace_spill` (2026-09-04 이관 — 사유는 offline_queue shim 참조)."""

from ..persistence.trace_spill import *  # noqa: F401,F403
from ..persistence.trace_spill import TraceSpill  # noqa: F401 — 명시 재수출.
