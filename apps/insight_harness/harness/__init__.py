"""Insight harness package.

Environment hardening for sandboxed/containerized runs: pin pyarrow to
single-threaded mode and use python-backed string storage. Both are no-ops
functionally and avoid pyarrow thread-pool instability seen in some
restricted environments (harmless on a normal workstation).
"""
import pandas as _pd

try:
    import threading as _threading
    _threading.stack_size(16 * 1024 * 1024)  # roomy stack for script-runner threads
except Exception:
    pass
try:
    import pyarrow as _pa
    _pa.set_cpu_count(1)
    _pa.set_io_thread_count(1)
except Exception:
    pass
try:
    _pd.set_option("mode.string_storage", "python")
except Exception:
    pass
