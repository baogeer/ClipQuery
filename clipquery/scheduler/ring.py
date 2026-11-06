"""一致性 hash 环。

用于决定某个 key（article_id / job_id / url）归属哪个 worker。
每个 worker 用 N 个虚拟节点均匀分布，降低扩缩容时的 key 迁移比例。

性能：150 vnode × 10 worker = 1500 节点，二分查找 O(log n)，单次 owner() 微秒级。
"""
from __future__ import annotations

import bisect
import hashlib
from typing import Iterable


def _hash(s: str) -> int:
    """stable 64-bit hash，不依赖 Python 进程的随机化。"""
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


class ConsistentHashRing:
    """只读快照。每次成员变化就重建一个新实例（反正 1500 节点计算成本可忽略）。"""

    def __init__(self, members: Iterable[str], vnodes: int = 150) -> None:
        self._members = sorted(set(members))
        self._vnodes = vnodes

        # 构建虚拟节点 → 真实 worker 的映射
        pairs: list[tuple[int, str]] = []
        for worker_id in self._members:
            for v in range(vnodes):
                pairs.append((_hash(f"{worker_id}#{v}"), worker_id))
        pairs.sort(key=lambda p: p[0])

        self._positions: list[int] = [p[0] for p in pairs]
        self._owners: list[str] = [p[1] for p in pairs]

    @property
    def members(self) -> list[str]:
        return list(self._members)

    def __len__(self) -> int:
        return len(self._members)

    def __bool__(self) -> bool:
        return bool(self._members)

    def owner(self, key: str) -> str | None:
        """返回 key 归属的 worker_id；环为空时返回 None。"""
        if not self._positions:
            return None
        h = _hash(key)
        idx = bisect.bisect_right(self._positions, h)
        if idx == len(self._positions):
            idx = 0
        return self._owners[idx]

    def is_owner(self, key: str, worker_id: str) -> bool:
        return self.owner(key) == worker_id
