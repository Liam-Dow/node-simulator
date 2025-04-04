from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from collections import deque
from enum import Enum
import time

class NodeStatus(Enum):
    INITIALIZING = "initializing"
    REGISTERING = "registering"
    IDLE = "idle"
    CHECKING = "checking"
    PROCESSING = "processing"
    ERROR = "error"
    SHUTDOWN = "shutdown"

@dataclass
class NodeConfig:
    node_id: str
    secret: str
    account_id: str
    pub_key: str
    private_key: str
    proxy_url: str
    models: List[str]
    ram: int
    cpu: int
    vram: int
    gpu: str
    os: str

@dataclass
class NodeMetrics:
    total_jobs: int = 0
    successful_jobs: int = 0
    failed_jobs: int = 0
    stored_responses_used: int = 0
    ollama_api_calls: int = 0
    last_activity: float = field(default_factory=time.time)
    response_times: deque = field(default_factory=lambda: deque(maxlen=100))
    current_status: NodeStatus = NodeStatus.INITIALIZING
    error_timestamp: Optional[float] = None

    @property
    def success_rate(self) -> float:
        if self.total_jobs == 0:
            return 0.0
        return (self.successful_jobs / self.total_jobs) * 100

    @property
    def avg_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        return sum(self.response_times) / len(self.response_times)

@dataclass
class ProxyStats:
    failures: int = 0
    last_failure: Optional[float] = None
    success_count: int = 0
    average_response_time: float = 0
    last_used: Optional[float] = None
    consecutive_failures: int = 0
