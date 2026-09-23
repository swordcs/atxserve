from __future__ import annotations
from hashlib import blake2s
BASE_DELAY_SECONDS = 0.1
MAX_DELAY_SECONDS = 1.6

def occ_backoff_delay(op_id: str, retry_index: int, *, base_seconds: float=BASE_DELAY_SECONDS, cap_seconds: float=MAX_DELAY_SECONDS) -> float:
    if retry_index < 1:
        raise ValueError('retry_index must be positive')
    if not 0 < base_seconds <= cap_seconds:
        raise ValueError('backoff requires 0 < base_seconds <= cap_seconds')
    exponent = min(retry_index - 1, 30)
    window = min(cap_seconds, base_seconds * 2 ** exponent)
    digest = blake2s(f'{op_id}:{retry_index}'.encode('utf-8'), digest_size=8).digest()
    fraction = int.from_bytes(digest, 'big') / ((1 << 64) - 1)
    return window * fraction
