from __future__ import annotations
import asyncio
import random
import logging
import time
from typing import Optional, Set
from data_models import ProxyStats

class ProxyManager:
    def __init__(self, proxy_file_path: str = 'proxies.txt'):
        self.proxy_file_path = proxy_file_path
        self.proxies = {}
        self.proxy_stats = {}
        self.used_proxies = set()
        self.lock = asyncio.Lock()
        self.logger = logging.getLogger('node_simulator')
        self.load_proxies()

    def load_proxies(self):
        try:
            with open(self.proxy_file_path, 'r') as f:
                proxy_lines = {line.strip() for line in f if line.strip()}
                for proxy in proxy_lines:
                    self.proxies[proxy] = True
                    self.proxy_stats[proxy] = ProxyStats()
            self.logger.info(f"Loaded {len(self.proxies)} proxies from {self.proxy_file_path}")
        except FileNotFoundError:
            self.logger.error(f"Proxy file {self.proxy_file_path} not found")
            raise

    def get_random_proxy(self) -> str:
        available_proxies = set(self.proxies.keys()) - self.used_proxies
        if not available_proxies:
            self.used_proxies.clear()
            available_proxies = set(self.proxies.keys())

        if not available_proxies:
            raise RuntimeError("No proxies available")

        proxy = random.choice(list(available_proxies))
        self.used_proxies.add(proxy)
        return proxy

    async def handle_proxy_failure(self, proxy: str, error_type: str, status_code: Optional[int] = None):
        async with self.lock:
            stats = self.proxy_stats[proxy]
            stats.failures += 1
            stats.consecutive_failures += 1
            stats.last_failure = time.time()
            
            time_since_last = time.time() - (stats.last_failure or time.time())
            
            if error_type == 'http' and status_code:
                if status_code in (401, 403, 407):
                    self.logger.warning(f"Proxy {proxy} authentication error")
                    return True
                elif status_code == 429:
                    self.logger.warning(f"Proxy {proxy} rate limited")
                    return True
                elif status_code >= 500:
                    if stats.consecutive_failures >= 2:
                        self.logger.warning(f"Proxy {proxy} multiple server errors")
                        return True
            
            if error_type == 'timeout':
                if stats.consecutive_failures >= 5:
                    self.logger.warning(f"Proxy {proxy} multiple timeouts")
                    return True
                elif stats.failures >= 10 and time_since_last < 3600:
                    self.logger.warning(f"Proxy {proxy} high timeout frequency")
                    return True
                    
            elif error_type == 'connection':
                if stats.consecutive_failures >= 3:
                    self.logger.warning(f"Proxy {proxy} consecutive connection failures")
                    return True
                elif stats.failures >= 5 and time_since_last < 1800:
                    self.logger.warning(f"Proxy {proxy} frequent connection issues")
                    return True
                    
            elif error_type == 'protocol':
                if stats.consecutive_failures >= 2:
                    self.logger.warning(f"Proxy {proxy} protocol errors")
                    return True
            
            if stats.failures >= 20 and time_since_last < 7200:
                self.logger.warning(f"Proxy {proxy} exceeded general failure threshold")
                return True
                
            self.logger.info(
                f"Proxy {proxy} failure recorded but continuing use: "
                f"Type: {error_type}, "
                f"Consecutive: {stats.consecutive_failures}, "
                f"Total: {stats.failures}"
            )
            return False

    async def record_proxy_success(self, proxy: str, response_time: float):
        async with self.lock:
            stats = self.proxy_stats[proxy]
            stats.success_count += 1
            stats.consecutive_failures = 0
            stats.last_used = time.time()
            stats.average_response_time = (
                (stats.average_response_time * (stats.success_count - 1) + response_time)
                / stats.success_count
            )
