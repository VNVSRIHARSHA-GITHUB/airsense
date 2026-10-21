"""Shared test fixtures."""

import pytest

from airsense.processing.process_history import build_spark


@pytest.fixture(scope="session")
def spark():
    """One small local Spark session for all Spark tests (start-up takes ~10 s)."""
    session = build_spark(
        {"spark_master": "local[2]", "driver_memory": "1g", "shuffle_partitions": 2}
    )
    yield session
    session.stop()
