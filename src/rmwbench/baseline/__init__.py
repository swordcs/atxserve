from .occ_backoff import occ_backoff_delay
from .pessimistic_fifo import PessimisticFIFOReasoner
BASELINE_POLICIES = {'original': {'mode': 'eager_fifo', 'refine_access_sets': False, 'reuse_probe_snapshot': False}, 'occ_backoff': {'mode': 'occ_backoff', 'refine_access_sets': False, 'reuse_probe_snapshot': False}, 'pessimistic_fifo': {'mode': 'pessimistic_fifo', 'refine_access_sets': True, 'reuse_probe_snapshot': True}}
BASELINE_SETTINGS_BY_MODE = {str(settings['mode']): settings for settings in BASELINE_POLICIES.values()}
__all__ = ['BASELINE_POLICIES', 'BASELINE_SETTINGS_BY_MODE', 'PessimisticFIFOReasoner', 'occ_backoff_delay']
