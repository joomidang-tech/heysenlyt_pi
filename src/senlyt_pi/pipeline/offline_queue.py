"""호환 shim — 정본은 `senlyt_pi.persistence.offline_queue` (2026-09-04 이관).

adapters 가 pipeline 을 역참조하던 방향 위반(헥사고날 감사 P1)을 풀기 위해 이관했다.
기존 소비자(app·tests)의 import 경로를 위해 이름만 재수출한다 — 새 코드는 persistence 에서.
"""

from ..persistence.offline_queue import *  # noqa: F401,F403
from ..persistence.offline_queue import OfflineQueue  # noqa: F401 — 명시 재수출.
