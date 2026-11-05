from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 30.0

    def should_retry(self, attempts_made: int) -> bool:
        """attempts_made = number of attempts already performed."""
        return attempts_made < self.max_attempts

    def delay_for(self, attempts_made: int) -> float:
        return min(self.max_delay, self.base_delay * (2 ** max(0, attempts_made - 1)))
