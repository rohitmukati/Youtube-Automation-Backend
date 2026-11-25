import os
import shutil
from dataclasses import dataclass

import psutil


MAX_PARALLEL_UPLOADS = 5
MIN_PARALLEL_UPLOADS = 1
SAFETY_RATIO = 0.50
RAM_BYTES_PER_UPLOAD = 64 * 1024 * 1024


@dataclass(frozen=True)
class UploadCapacity:
    limit: int
    reason: str
    available_ram_mb: int
    usable_ram_mb: int
    free_disk_mb: int
    usable_disk_mb: int
    cpu_limit: int


def calculate_parallel_upload_limit(
    video_size_bytes: int,
    channel_count: int,
    *,
    active_uploads: int = 0,
) -> UploadCapacity:
    """Calculate a conservative per-job upload limit from server resources."""
    channel_count = max(1, int(channel_count))
    video_size_bytes = max(0, int(video_size_bytes))
    active_uploads = max(0, int(active_uploads))

    try:
        memory = psutil.virtual_memory()
        available_ram = max(0, int(memory.available))
    except (AttributeError, OSError):
        available_ram = 0

    try:
        disk = shutil.disk_usage(os.getcwd())
        free_disk = max(0, int(disk.free))
    except (AttributeError, OSError):
        free_disk = 0

    try:
        logical_cpu_count = max(1, int(psutil.cpu_count(logical=True) or 1))
    except (AttributeError, OSError):
        logical_cpu_count = 1

    usable_ram = int(available_ram * SAFETY_RATIO)
    usable_disk = int(free_disk * SAFETY_RATIO)

    ram_limit = usable_ram // RAM_BYTES_PER_UPLOAD
    disk_limit = channel_count
    if video_size_bytes > 0:
        estimated_disk_per_upload = max(video_size_bytes * 2, RAM_BYTES_PER_UPLOAD)
        disk_limit = usable_disk // estimated_disk_per_upload

    cpu_limit = max(1, int(logical_cpu_count * SAFETY_RATIO))

    if active_uploads:
        cpu_limit = max(1, cpu_limit - active_uploads)

    if usable_ram == 0 or usable_disk == 0:
        limit = 1
        reason = "Resource usage could not be measured safely; using the minimum limit."
    else:
        limit = min(
            channel_count,
            ram_limit,
            disk_limit,
            cpu_limit,
            MAX_PARALLEL_UPLOADS,
        )
        limit = max(MIN_PARALLEL_UPLOADS, limit)

        limits = {
            "RAM": ram_limit,
            "disk": disk_limit,
            "CPU": cpu_limit,
            "selected channels": channel_count,
        }
        reason_limit = min(limits, key=limits.get)
        reason = f"Limited automatically by {reason_limit} capacity."

    return UploadCapacity(
        limit=limit,
        reason=reason,
        available_ram_mb=available_ram // (1024 * 1024),
        usable_ram_mb=usable_ram // (1024 * 1024),
        free_disk_mb=free_disk // (1024 * 1024),
        usable_disk_mb=usable_disk // (1024 * 1024),
        cpu_limit=cpu_limit,
    )
