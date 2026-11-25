from app.utils.upload_resources import calculate_parallel_upload_limit


def test_parallel_limit_is_bounded_and_never_below_one():
    capacity = calculate_parallel_upload_limit(
        video_size_bytes=2 * 1024 * 1024 * 1024,
        channel_count=10,
    )
    assert 1 <= capacity.limit <= 5


def test_parallel_limit_never_exceeds_selected_channels():
    capacity = calculate_parallel_upload_limit(
        video_size_bytes=500 * 1024 * 1024,
        channel_count=2,
    )
    assert capacity.limit <= 2


def test_active_uploads_reduce_cpu_limit():
    capacity = calculate_parallel_upload_limit(
        video_size_bytes=100 * 1024 * 1024,
        channel_count=5,
        active_uploads=100,
    )
    assert capacity.limit == 1
