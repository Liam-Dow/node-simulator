from __future__ import annotations
import asyncio
import time
import logging
from typing import Dict
from data_models import NodeMetrics, NodeStatus
from config import STATUS_REPORT_INTERVAL

class AuthHealthMonitor:
    def __init__(self):
        self.is_healthy = True
        self.paused_since = None
        self.lock = asyncio.Lock()
        self.health_check_event = asyncio.Event()
        self.health_check_event.set()

    async def mark_unhealthy(self):
        async with self.lock:
            if self.is_healthy:
                self.is_healthy = False
                self.paused_since = time.time()
                self.health_check_event.clear()
                logging.getLogger('node_simulator').warning("Auth service marked as unhealthy, pausing all node operations")

    async def mark_healthy(self):
        async with self.lock:
            if not self.is_healthy:
                pause_duration = time.time() - self.paused_since
                self.is_healthy = True
                self.paused_since = None
                self.health_check_event.set()
                logging.getLogger('node_simulator').info(f"Auth service marked as healthy after {pause_duration:.1f}s pause")

    async def wait_for_healthy(self):
        await self.health_check_event.wait()

class NodeMonitor:
    def __init__(self):
        self.metrics: Dict[str, NodeMetrics] = {}
        self.points_data: Dict[str, Dict] = {}
        self.lock = asyncio.Lock()
        self.expected_nodes = 0
        self.nodes_processed = 0
        self.registration_complete = asyncio.Event()
        self.current_cycle_points = {}
        self.current_cycle_number = 0
        self.logger = logging.getLogger('node_simulator')

    async def mark_node_processed(self):
        async with self.lock:
            self.nodes_processed += 1
            if self.nodes_processed == self.expected_nodes:
                self.logger.info("All nodes have completed registration process. Starting points collection.")
                self.registration_complete.set()

    async def register_node(self, node_id: str):
        async with self.lock:
            self.metrics[node_id] = NodeMetrics()

    async def update_metrics(self, node_id: str, **kwargs):
        async with self.lock:
            if node_id not in self.metrics:
                await self.register_node(node_id)

            metrics = self.metrics[node_id]
            metrics.last_activity = time.time()

            for key, value in kwargs.items():
                if hasattr(metrics, key):
                    setattr(metrics, key, value)

    async def update_points(self, node_id: str, points_data: Dict):
        async with self.lock:
            if self.current_cycle_number not in self.current_cycle_points:
                self.current_cycle_points[self.current_cycle_number] = {}

            self.current_cycle_points[self.current_cycle_number][node_id] = points_data

            if len(self.current_cycle_points[self.current_cycle_number]) == self.expected_nodes:
                cycle_data = self.current_cycle_points[self.current_cycle_number]
                total_points = sum(data['points'] for data in cycle_data.values())
                total_raw_points = sum(data['pointsraw'] for data in cycle_data.values())

                points_logger = logging.getLogger('node_simulator.points')
                points_logger.info(
                    f"\nTotal Points Across All Nodes (Cycle {self.current_cycle_number}):"
                    f"\nPoints: {total_points:.2f}"
                    f"\nRaw Points: {total_raw_points:.2f}"
                    f"\n{'-'*50}"
                )
                self.points_data = cycle_data
                self.current_cycle_number += 1

    async def log_response_time(self, node_id: str, response_time: float):
        async with self.lock:
            self.metrics[node_id].response_times.append(response_time)

    async def print_status_report(self):
        while True:
            async with self.lock:
                print("\n=== Node Status Report ===")
                print(f"Total Nodes: {len(self.metrics)}")

                status_counts = {status: 0 for status in NodeStatus}
                total_jobs = 0
                total_success = 0
                total_stored_responses = 0
                total_api_calls = 0

                for node_id, metrics in self.metrics.items():
                    status_counts[metrics.current_status] += 1
                    total_jobs += metrics.total_jobs
                    total_success += metrics.successful_jobs
                    total_stored_responses += metrics.stored_responses_used
                    total_api_calls += metrics.ollama_api_calls

                    if metrics.current_status == NodeStatus.ERROR:
                        print(f"\nNode {node_id[:8]} in ERROR state")
                        print(f"Success Rate: {metrics.success_rate:.1f}%")
                        print(f"Avg Response Time: {metrics.avg_response_time:.2f}s")

                print("\nStatus Distribution:")
                for status, count in status_counts.items():
                    print(f"{status.value}: {count}")

                if total_jobs > 0:
                    print(f"\nOverall Success Rate: {(total_success/total_jobs)*100:.1f}%")

                print(f"Total Jobs Processed: {total_jobs}")
                print(f"  Stored Responses: {total_stored_responses}")
                print(f"  Ollama API Calls: {total_api_calls}")
                print("========================\n")

            await asyncio.sleep(STATUS_REPORT_INTERVAL)
