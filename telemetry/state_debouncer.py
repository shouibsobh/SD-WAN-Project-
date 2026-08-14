import threading

DEFAULT_CONFIRM_READS = 2


class StateDebouncer:
    """Holds back a state change until it repeats confirm_reads times."""

    def __init__(self, confirm_reads=DEFAULT_CONFIRM_READS):
        self._confirm_reads = confirm_reads
        self._pending = {}
        # telemetry_agent calls confirm() from its probe threads, and a
        # read-modify-write on the dict is not atomic.
        self._lock = threading.Lock()

    def confirm(self, key, candidate_state):
        """Return the state once it's trusted, else None."""
        with self._lock:
            pending_state, streak = self._pending.get(key, (None, 0))

            if candidate_state == pending_state:
                streak += 1
            else:
                pending_state, streak = candidate_state, 1

            self._pending[key] = (pending_state, streak)

            return pending_state if streak >= self._confirm_reads else None