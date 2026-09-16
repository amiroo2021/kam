from __future__ import annotations
from typing import Any, Dict, List
from fibolearn.research.patterns import Pattern

def create_candidate_from_live_state(state: Dict[str,Any]) -> Pattern:
    symbol=state.get('symbol','UNKNOWN')
    p=Pattern.create('live-study-setup', {'frozen_state': state}, {'live_timestamp_ms': state.get('timestamp_ms')}, [symbol])
    p.mark_candidate(); return p
