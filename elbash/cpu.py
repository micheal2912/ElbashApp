"""How many cores this process may actually use.

os.cpu_count() reports the machine's cores, not the share this process was
given. Under `docker run --cpus=2`, a Kubernetes limit or CPU pinning the two
differ, and sizing the worker pools from the machine count oversubscribes the
quota - more processes fighting over the same slice, which is slower than
matching it.
"""

import os
from pathlib import Path


def _cgroup_quota() -> float | None:
    """The cgroup CPU limit in cores, or None when there is no limit."""
    try:  # cgroup v2: "<quota> <period>", or "max <period>" when unlimited
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass

    try:  # cgroup v1
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass

    return None


def available_cores(default: int = 4) -> int:
    # sched_getaffinity honours CPU pinning; cpu_count does not.
    try:
        cores = len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        cores = os.cpu_count() or default

    quota = _cgroup_quota()
    if quota:
        cores = min(cores, round(quota))
    return max(1, cores)
