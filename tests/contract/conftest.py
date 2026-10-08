"""Offline suite: always hermetic, even when FINPLAN_TARGET_ENV happens to be set."""

from tests.offline_env import apply_offline_environment

apply_offline_environment()
