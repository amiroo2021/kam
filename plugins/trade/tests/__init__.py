"""Trade plugin tests package.

Forces FIBO_TIMER_LIFECYCLE_DRY_RUN so unittest discovery can never
enable/disable the host fibo-converge.timer.
"""
import os

os.environ.setdefault("FIBO_TIMER_LIFECYCLE_DRY_RUN", "1")
