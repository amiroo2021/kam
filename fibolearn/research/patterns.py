from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List
import time
class PatternStatus(Enum):
    DISCOVERED='DISCOVERED'; CANDIDATE='CANDIDATE'; BACKTESTED='BACKTESTED'; OOS_TESTING='OOS_TESTING'; VALIDATED='VALIDATED'; REJECTED='REJECTED'; DEGRADED='DEGRADED'
@dataclass
class Pattern:
    name: str; definition: Dict[str,Any]; discovery_range: Dict[str,Any]; symbols: List[str]; status: PatternStatus=PatternStatus.DISCOVERED; discovered_at_ms:int=field(default_factory=lambda:int(time.time()*1000)); sample_size:int=0; discovery_result:Dict[str,Any]=field(default_factory=dict); oos_result:Dict[str,Any]=field(default_factory=dict); walk_forward_result:Dict[str,Any]=field(default_factory=dict); live_forward_result:Dict[str,Any]=field(default_factory=dict); baseline_rate:float|None=None; candidate_rate:float|None=None; effect_size:float|None=None; confidence_interval:tuple[float,float]|None=None
    @classmethod
    def create(cls, name, definition, discovery_range, symbols): return cls(name, definition, discovery_range, list(symbols))
    def mark_backtested(self, sample_size:int, result:Dict[str,Any]):
        self.sample_size=sample_size; self.discovery_result=dict(result); self.status=PatternStatus.BACKTESTED
    def mark_candidate(self): self.status=PatternStatus.CANDIDATE
