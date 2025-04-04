from __future__ import annotations
import asyncio
import csv
import random
import logging
from typing import Dict, List

from data_models import NodeConfig
from monitoring import NodeMonitor, AuthHealthMonitor
from proxy_manager import ProxyManager
from node_simulator import WorkerNodeSimulator
from config import (
    MIN_START_DELAY, MAX_START_DELAY, NODE_SPACING_DELAY
)

class NodeManager:
    def __init__(self, csv_path: str, proxy_file_path: str = 'proxies.txt'):
        self.csv_path = csv_path
        self.proxy_manager = ProxyManager(proxy_file_path)
        self.nodes: List[WorkerNodeSimulator] = []
        self.monitor = NodeMonitor()
        self.auth_monitor = AuthHealthMonitor()
        self.logger = logging.getLogger('node_simulator')
        self.auth_backoff_time = 30
        self.max_auth_backoff = 300
        
    def load_nodes(self) -> List[NodeConfig]:
        configs = []
        with open(self.csv_path, newline='') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                config = NodeConfig(
                    node_id=row['node_id'],
                    secret=row['secret'],
                    account_id=row['account_id'],
                    pub_key=row['pub_key'],
                    private_key=row['private_key'],
                    proxy_url=row['proxy'],
                    models=row['models'].split('|'),
                    ram=int(row['ram']),
                    cpu=int(row['cpu']),
                    vram=int(row['vram']),
                    gpu=row['gpu'],
                    os=row['os']
                )
                configs.append(config)
        return configs
        
    async def handle_auth_failure(self):
        await self.auth_monitor.mark_unhealthy()
        self.logger.warning(f"Auth service unhealthy, backing off for {self.auth_backoff_time} seconds")
        await asyncio.sleep(self.auth_backoff_time)
        self.auth_backoff_time = min(self.auth_backoff_time * 2, self.max_auth_backoff)

    async def reset_auth_backoff(self):
        self.auth_backoff_time = 30
        await self.auth_monitor.mark_healthy()
        
    async def delayed_node_start(self, node: WorkerNodeSimulator, delay: float):
        initial_delay = delay
        backoff_time = 5.0
        
        self.logger.info(f"Node {node.config.node_id[:8]} scheduled to start in {delay:.1f} seconds")
        
        while initial_delay > 0:
            while not self.auth_monitor.is_healthy:
                self.logger.info(f"Node {node.config.node_id[:8]} start paused - auth unhealthy")
                await asyncio.sleep(backoff_time)
                backoff_time = min(backoff_time * 2, 60)
                
            backoff_time = 5.0
            
            sleep_time = min(initial_delay, 5.0)
            await asyncio.sleep(sleep_time)
            initial_delay -= sleep_time
            
        self.logger.info(f"Starting node {node.config.node_id[:8]}")
        await node.run()

    async def run(self):
        try:
            asyncio.create_task(self.monitor.print_status_report())
    
            self.logger.info("Checking proxy pool status...")
            try:
                proxy_count = len(self.proxy_manager.proxies)
                self.logger.info(f"Using proxy pool with {proxy_count} proxies")
            except Exception as e:
                self.logger.error(f"Failed to access proxy manager: {str(e)}")
                return
    
            configs = self.load_nodes()
            account_groups: Dict[str, List[NodeConfig]] = {}
    
            for config in configs:
                if config.account_id not in account_groups:
                    account_groups[config.account_id] = []
                account_groups[config.account_id].append(config)
    
            total_accounts = len(account_groups)
            total_nodes = len(configs)
    
            self.monitor.expected_nodes = total_nodes
            self.logger.info(f"Loaded {total_nodes} nodes across {total_accounts} accounts")
    
            tasks = []
            global_node_index = 0
    
            sorted_accounts = sorted(account_groups.items())
    
            min_delay = MIN_START_DELAY
            max_delay = MAX_START_DELAY
            delay_range = max_delay - min_delay
    
            for account_index, (account_id, account_configs) in enumerate(sorted_accounts):
                base_delay = min_delay + (delay_range * account_index / (total_accounts - 1 if total_accounts > 1 else 1))
                
                account_delay = base_delay + random.uniform(-30, 30)
                
                account_delay = max(min_delay, min(max_delay, account_delay))
    
                account_nodes = len(account_configs)
                self.logger.info(
                    f"Account {account_id} with {account_nodes} nodes will start in "
                    f"{account_delay:.1f} seconds ({account_delay/60:.1f} minutes)"
                )
    
                for node_index, config in enumerate(account_configs):
                    node_delay = account_delay + (node_index * NODE_SPACING_DELAY)
    
                    node = WorkerNodeSimulator(
                        config=config,
                        monitor=self.monitor,
                        node_index=global_node_index,
                        total_nodes=total_nodes,
                        proxy_manager=self.proxy_manager,
                        auth_monitor=self.auth_monitor
                    )
    
                    self.nodes.append(node)
                    tasks.append(asyncio.create_task(
                        self.delayed_node_start(node, node_delay)
                    ))
                    global_node_index += 1
    
            if not tasks:
                self.logger.error("No nodes were successfully initialized")
                return
    
            self.logger.info(f"Successfully initialized {len(tasks)} nodes with proxies")
            
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                self.logger.info("Shutting down all nodes...")
                shutdown_tasks = [node.shutdown() for node in self.nodes]
                await asyncio.gather(*shutdown_tasks)
                raise
    
        except Exception as e:
            self.logger.error(f"Fatal error in node manager: {str(e)}")
            self.logger.error("Full error details:", exc_info=True)
            if self.nodes:
                self.logger.info("Attempting to shutdown nodes...")
                shutdown_tasks = [node.shutdown() for node in self.nodes]
                try:
                    await asyncio.gather(*shutdown_tasks)
                except Exception as shutdown_error:
                    self.logger.error(f"Error during emergency shutdown: {str(shutdown_error)}")
