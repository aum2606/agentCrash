"""Agent-side deterministic randomness, independent of fault sampling (plan section 4).

Derived from the run seed and agent id, so retry jitter is reproducible and never consumes a
global PRNG whose order could change fault eligibility.
"""

import hashlib

from agentcrash.domain.canonical import canonical_bytes


class SeededRng:
    def __init__(self, seed: str, agent_id: str) -> None:
        self._seed = seed
        self._agent = agent_id
        self._n = 0

    def jitter_ms(self, upper_ms: int) -> int:
        """Next value in ``[0, upper_ms]``; the sequence depends only on (seed, agent id)."""
        self._n += 1
        digest = hashlib.sha256(canonical_bytes([self._seed, self._agent, self._n])).digest()
        return int.from_bytes(digest[:8], "big") % (upper_ms + 1)
