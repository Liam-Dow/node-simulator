from __future__ import annotations
import anyio
import asyncio
import httpx
import logging
import json
import random
import pandas as pd
import re
import time
from typing import Dict, List, Optional, Any, Tuple
from collections import deque

from data_models import NodeConfig, NodeStatus, NodeMetrics
from monitoring import NodeMonitor, AuthHealthMonitor
from proxy_manager import ProxyManager
from config import (
    API_GATEWAY_URL, API_AUTH_URL, OLLAMA_API_URL,
    ERROR_RECOVERY_MIN, ERROR_RECOVERY_MAX,
    CONTROL_PROMPTS_FILE, TEST_PROMPTS_FILE,
    USE_STORED_RESPONSES, OLLAMA_OVERRIDE_MODEL,
    OLLAMA_FALLBACK_MODEL, OLLAMA_CAP_TEMPERATURE
)

class WorkerNodeSimulator:
    def __init__(self, config: NodeConfig, monitor: NodeMonitor, node_index: int, 
                 total_nodes: int, proxy_manager: ProxyManager, auth_monitor: AuthHealthMonitor):
        self.config = config
        self.monitor = monitor
        self.api_gateway_url = API_GATEWAY_URL
        self.api_auth_url = API_AUTH_URL
        self.node_registered = False
        self.headers = {}
        self.stored_responses = {}
        self.prompt_patterns = {}
        self.points_check_offset = (node_index * 600) // total_nodes
        self.ERROR_RECOVERY_THRESHOLD = 600
        self.proxy_manager = proxy_manager
        self.last_successful_job_poll = time.time()
        self.proxy_verified = False
        self.auth_monitor = auth_monitor
        self.start_time = None
        self.initialization_complete = asyncio.Event()
        self.shutdown_flag = asyncio.Event()
        self.TIMEOUTS = {
            'default': httpx.Timeout(30.0),
            'auth': httpx.Timeout(90.0),
            'long': httpx.Timeout(120.0),
            'short': httpx.Timeout(10.0),
            'jobs': httpx.Timeout(4.0)
        }

        self.logger = logging.LoggerAdapter(
            logging.getLogger('node_simulator.node'),
            {'node_id': self.config.node_id[:8]}
        )
        self.points_logger = logging.getLogger('node_simulator.points')
        self.error_logger = logging.LoggerAdapter(
            logging.getLogger('node_simulator.errors'),
            {'node_id': self.config.node_id}
        )
        self.ollama_logger = logging.getLogger('node_simulator.ollama_requests')

        if USE_STORED_RESPONSES:
            self.logger.info("Stored responses enabled, loading from CSV files")
            asyncio.create_task(self.load_stored_responses())
        else:
            self.logger.info("Stored responses disabled")
            self.stored_responses = {}
            self.prompt_patterns = {}
        
        transport_config = {
            "retries": 1,
            "verify": False,
            "limits": httpx.Limits(
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30.0
            )
        }
        
        if self.config.proxy_url:
            proxy_url = f"http://{self.config.proxy_url}" if not self.config.proxy_url.startswith('http') else self.config.proxy_url
            transport_config["proxy"] = httpx.Proxy(url=proxy_url)
        
        self.transport = httpx.AsyncHTTPTransport(**transport_config)
    
    async def job_processing_loop(self):
        self.logger.info("Starting job processing loop")
        
        while not self.shutdown_flag.is_set():
            try:
                if self.node_registered and self.proxy_verified:
                    start_time = time.time()
                    await self.process_job()
                    
                    elapsed = time.time() - start_time
                    sleep_time = max(0, 5 - elapsed)
                    
                    await asyncio.sleep(sleep_time)
                else:
                    await asyncio.sleep(1)
                    
            except Exception as e:
                self.logger.error(f"Error in job processing loop: {e}")
                await asyncio.sleep(5)
                
    async def make_get_job_request(self) -> Optional[httpx.Response]:
        retries = 0
        max_retries = 5
        
        while retries < max_retries:
            try:
                start_time = time.time()
                
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.TIMEOUTS['jobs']
                ) as client:
                    response = await client.get(
                        f"{self.api_gateway_url}/worker/get_job?node_version=0.0.4",
                        headers=self.headers
                    )
                    
                    self.last_successful_job_poll = time.time()
                    response_time = time.time() - start_time
                    await self.monitor.log_response_time(self.config.node_id, response_time)
                    
                    if response.status_code == 400:
                        try:
                            error_response = response.json()
                            status_message = error_response.get("status", "")
                            await self.handle_error(f"Received 400 status code: {status_message}")
                        except Exception as e:
                            await self.handle_error(f"Received 400 status code. Failed to parse error: {e}")
                        return None
                    
                    response.raise_for_status()
                    return response
                    
            except (httpx.TimeoutException, httpx.RequestError, 
                    httpx.HTTPStatusError, anyio.ClosedResourceError,
                    anyio.EndOfStream, ConnectionError) as e:
                retries += 1
                if retries == max_retries:
                    self.logger.warning(f"Max retries reached for get_job request: {str(e)}")
                    return None
                
                self.logger.debug(
                    f"Get job request attempt {retries}/{max_retries} failed: {str(e)}. "
                    f"Retrying in {2 ** retries} seconds..."
                )
                await asyncio.sleep(2 ** retries)
                
            except Exception as e:
                retries += 1
                if retries == max_retries:
                    self.logger.error(f"Unexpected error in get_job request after all retries: {str(e)}")
                    return None
                
                self.logger.debug(f"Unexpected error in get_job request (attempt {retries}): {str(e)}")
                await asyncio.sleep(2 ** retries)
        
        return None
    
    async def update_models(self):
        await self.make_request_with_retries(
            "POST",
            f"{self.api_gateway_url}/worker/update_models",
            timeout_type='default',
            headers=self.headers,
            json={
                "node_url": self.config.node_id,
                "models": self.config.models
            }
        )

    def create_prompt_pattern(self, prompt: str) -> str:
        escaped = re.escape(prompt.strip()).replace('\\ ', ' ')
        return fr'^\s*{escaped}[.?]?\s*$'

    def find_matching_prompt(self, input_prompt: str) -> Optional[str]:
        input_prompt = input_prompt.strip()
        self.logger.debug(f"\nAttempting to match prompt: {input_prompt}")

        for stored_prompt, pattern in self.prompt_patterns.items():
            self.logger.debug(f"Trying pattern: {pattern.pattern}")
            try:
                if pattern.match(input_prompt):
                    self.logger.info(f"Found matching prompt: {stored_prompt}")
                    return stored_prompt
            except Exception as e:
                self.logger.error(f"Error matching pattern: {str(e)}")
                continue

        self.logger.debug("No match found. Input prompt details:")
        self.logger.debug(f"Length: {len(input_prompt)}")
        self.logger.debug(f"Characters: {repr(input_prompt)}")

        return None

    async def load_stored_responses(self):
        try:
            self.stored_responses = {}
            self.prompt_patterns = {}

            df1 = pd.read_csv(CONTROL_PROMPTS_FILE)
            df2 = pd.read_csv(TEST_PROMPTS_FILE)

            for _, row in df1.iterrows():
                original_prompt = row['prompt'].strip('"').strip()
                pattern = self.create_prompt_pattern(original_prompt)
                self.prompt_patterns[original_prompt] = re.compile(pattern, re.IGNORECASE)

                model_responses = {}
                for model in ["llama3:latest", "llama3.2:latest", "llama3.1:latest",
                            "mistral:latest", "mixtral:latest", "gemma:latest",
                            "llava:latest", "qwen2:latest", "qwen:latest",
                            "yi:latest", "phi3.5:latest", "gemma2:latest"]:
                    try:
                        responses = [
                            json.loads(row[f'{model}_response_{i}'])
                            if isinstance(row[f'{model}_response_{i}'], str)
                            else row[f'{model}_response_{i}']
                            for i in range(1, 6)
                        ]
                        model_responses[model] = responses
                    except Exception as e:
                        self.logger.warning(f"Error loading responses for model {model} from first CSV: {str(e)}")
                        continue

                self.stored_responses[original_prompt] = model_responses

            for _, row in df2.iterrows():
                prompt = row['prompt'].strip('"').strip()
                if prompt in self.stored_responses:
                    for model in ["llama3:latest", "llama3.2:latest", "llama3.1:latest",
                                "mistral:latest", "mixtral:latest", "gemma:latest",
                                "llava:latest", "qwen2:latest", "qwen:latest",
                                "yi:latest", "phi3.5:latest", "gemma2:latest"]:
                        try:
                            additional_responses = [
                                json.loads(row[f'{model}_response_{i}'])
                                if isinstance(row[f'{model}_response_{i}'], str)
                                else row[f'{model}_response_{i}']
                                for i in range(6, 11)
                            ]
                            if model in self.stored_responses[prompt]:
                                self.stored_responses[prompt][model].extend(additional_responses)
                        except Exception as e:
                            self.logger.warning(f"Error loading additional responses for model {model} from second CSV: {str(e)}")
                            continue

            self.logger.info(f"Successfully loaded {len(self.stored_responses)} stored prompt-response pairs")

        except Exception as e:
            self.logger.error(f"Error loading stored responses: {str(e)}")
            self.stored_responses = {}
            self.prompt_patterns = {}

    def get_next_response(self, prompt: str, model: str) -> str:
        try:
            available_responses = self.stored_responses[prompt][model]
            
            if not available_responses:
                raise ValueError(f"No responses available for prompt: {prompt[:30]} and model: {model}")
            
            selected_response = random.choice(available_responses)
            
            self.logger.info(
                f"\nResponse selection complete:\n"
                f"- Prompt: {prompt[:30]}\n"
                f"- Model: {model}\n"
                f"- Available responses: {len(available_responses)}"
            )
            
            return selected_response
        
        except Exception as e:
            self.logger.error(f"Error in get_next_response: {str(e)}")
            self.logger.error(f"Prompt: {prompt[:30]}, Model: {model}")
            raise

    def get_client_config(self):
        if self.config.proxy_url:
            proxy_url = f"http://{self.config.proxy_url}" if not self.config.proxy_url.startswith('http') else self.config.proxy_url
            return {
                "transport": httpx.AsyncHTTPTransport(proxy=httpx.Proxy(url=proxy_url)),
                "verify": False
            }
        return {}

    async def check_points(self):
        try:
            response = await self.make_request_with_retries(
                "GET",
                f"{self.api_gateway_url}/get_points?node_name={self.config.node_id}",
                timeout_type='short',
                headers=self.headers
            )

            if response.status_code == 200:
                points_data = response.json()
                log_message = (
                    f"Node: {self.config.node_id} | "
                    f"Points: {points_data['points']:.2f} | "
                    f"Raw Points: {points_data['pointsraw']:.2f}"
                )
                self.points_logger.info(log_message)
                await self.monitor.update_points(self.config.node_id, points_data)
            else:
                self.logger.error(f"Failed to get points - Status: {response.status_code}")

        except Exception as e:
            self.logger.error(f"Error checking points: {str(e)}")

    async def periodic_tasks(self):
        from config import MODELS_UPDATE_MIN, MODELS_UPDATE_MAX, POINTS_CHECK_INTERVAL
        
        models_update_interval = random.randint(MODELS_UPDATE_MIN, MODELS_UPDATE_MAX)
        points_check_interval = POINTS_CHECK_INTERVAL
    
        last_models_update = time.time()
        last_points_check = time.time()
    
        while self.node_registered:
            current_time = time.time()
    
            if current_time - last_models_update >= models_update_interval:
                try:
                    await self.update_models()
                    self.logger.info(f"Successfully updated installed models after {models_update_interval/3600:.1f} hours")
                except Exception as e:
                    self.logger.error(f"Failed to update installed models: {str(e)}")
                last_models_update = current_time
                models_update_interval = random.randint(MODELS_UPDATE_MIN, MODELS_UPDATE_MAX)
                self.logger.info(f"Next models update scheduled in {models_update_interval/3600:.1f} hours")
    
            if current_time - last_points_check >= points_check_interval:
                await self.check_points()
                last_points_check = current_time
    
            await asyncio.sleep(60)

    async def test_proxy(self):
        try:
            async with httpx.AsyncClient(
                timeout=self.TIMEOUTS['short'],
                verify=False
            ) as client:
                direct_response = await client.get("https://api.ipify.org?format=json")
                direct_ip = direct_response.json()['ip']
    
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.TIMEOUTS['short']
            ) as client:
                proxy_response = await client.get("https://api.ipify.org?format=json")
                proxy_ip = proxy_response.json()['ip']
    
            if direct_ip != proxy_ip:
                self.logger.info(f"Proxy {self.config.proxy_url} working correctly - IP changed from {direct_ip} to {proxy_ip}")
                return True, proxy_ip
            else:
                self.logger.error(f"Proxy {self.config.proxy_url} may not be working - IP unchanged: {direct_ip}")
                return False, direct_ip
        except Exception as e:
            self.logger.error(f"Proxy test failed for {self.config.proxy_url}: {str(e)}")
            return False, None
    
    async def verify_proxy_connection(self) -> bool:
        if not self.config.proxy_url:
            return True
    
        try:
            proxy_working, proxy_ip = await self.test_proxy()
            if not proxy_working:
                self.error_logger.error(
                    "Proxy verification failed - IP check failed",
                    extra={
                        'url': 'https://api.ipify.org',
                        'proxy': self.config.proxy_url,
                        'headers': 'N/A'
                    }
                )
                if await self.rotate_proxy():
                    self.logger.info("Rotated to new proxy after verification failure")
                    return await self.verify_proxy_connection()
                return False
    
            self.logger.info(f"Verified proxy {self.config.proxy_url} (IP: {proxy_ip})")
            return True
    
        except Exception as e:
            self.error_logger.error(
                f"Proxy verification failed: {str(e)}",
                extra={
                    'url': 'https://api.ipify.org',
                    'proxy': self.config.proxy_url,
                    'headers': 'N/A'
                }
            )
            if await self.rotate_proxy():
                self.logger.info("Rotated to new proxy after verification error")
                return await self.verify_proxy_connection()
            return False
    
    async def rotate_proxy(self) -> bool:
        try:
            await asyncio.sleep(1)
            new_proxy = self.proxy_manager.get_random_proxy()
            self.logger.info(f"Rotating proxy from {self.config.proxy_url} to {new_proxy}")
            self.config.proxy_url = new_proxy

            proxy_url = f"http://{new_proxy}" if not new_proxy.startswith('http') else new_proxy
            self.transport = httpx.AsyncHTTPTransport(
                proxy=httpx.Proxy(url=proxy_url),
                verify=False,
                limits=httpx.Limits(
                    max_keepalive_connections=10,
                    max_connections=20,
                    keepalive_expiry=30.0
                )
            )

            proxy_working, proxy_ip = await self.test_proxy()
            if proxy_working:
                self.logger.info(f"Successfully rotated to new proxy {new_proxy} (IP: {proxy_ip})")
                return True
            else:
                self.logger.warning(f"New proxy {new_proxy} failed verification")
                return False

        except Exception as e:
            self.logger.error(f"Error rotating proxy: {str(e)}")
            return False

    async def make_request_with_retries(
        self,
        method: str,
        url: str,
        max_retries: int = 5,
        timeout_type: str = 'default',
        **kwargs
    ) -> httpx.Response:
        start_time = time.time()
        retries = 0
        consecutive_failures = 0
        is_auth_request = '/node/check' in url
        
        while retries < max_retries:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.TIMEOUTS[timeout_type]
                ) as client:
                    response = await client.request(
                        method=method,
                        url=url,
                        **kwargs
                    )
                    
                    if response.status_code == 400:
                        return response
                    
                    response.raise_for_status()
                    
                    response_time = time.time() - start_time
                    await self.monitor.log_response_time(
                        self.config.node_id,
                        response_time
                    )
                    
                    if is_auth_request:
                        await self.auth_monitor.mark_healthy()
                    
                    return response
                    
            except (httpx.TimeoutException, httpx.ConnectError, 
                    httpx.RemoteProtocolError, anyio.ClosedResourceError,
                    httpx.ReadError) as e:
                retries += 1
                consecutive_failures += 1
                
                if isinstance(e, httpx.TimeoutException) and is_auth_request:
                    await self.auth_monitor.mark_unhealthy()
                
                if (not is_auth_request and 
                    self.config.proxy_url and 
                    consecutive_failures >= 3 and 
                    retries < (max_retries - 1)):
                    self.logger.warning(f"Multiple consecutive failures detected, rotating proxy")
                    await self.rotate_proxy()
                    consecutive_failures = 0
                
                if retries == max_retries:
                    self.logger.error(f"Max retries reached for request to {url}")
                    await self.handle_error(f"Max retries reached for request to {url}")
                    raise
                else:
                    self.logger.warning(f"Request attempt {retries}/{max_retries} failed")
                
                wait_time = 2 ** retries + random.uniform(0, 1)
                await asyncio.sleep(wait_time)
    
        raise RuntimeError("Unexpected end of retry loop")

    def add_random_whitespace(self, text: str) -> str:
        whitespace_positions = [i for i, char in enumerate(text) if char.isspace()]

        if not whitespace_positions:
            return text + ' '

        insert_position = random.choice(whitespace_positions)
        modified_text = text[:insert_position] + ' ' + text[insert_position:]

        if modified_text == text:
            modified_text = text + ' '

        self.logger.debug(f"Original length: {len(text)}, Modified length: {len(modified_text)}")
        return modified_text

    async def process_job(self):
        if not self.node_registered:
            return
    
        try:
            await self.monitor.update_metrics(
                self.config.node_id,
                current_status=NodeStatus.CHECKING
            )
    
            response = await self.make_get_job_request()
            
            if not response:
                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.IDLE
                )
                return
    
            if response.status_code == 200:
                raw_job_data = response.json().get("job_data")
                if raw_job_data is None:
                    await self.monitor.update_metrics(
                        self.config.node_id,
                        current_status=NodeStatus.IDLE
                    )
                    return

                self.logger.info("\n" + "="*50)
                self.logger.info("Received Raw Job Data:")
                self.logger.info("-"*50)
                self.logger.info(f"Full job data structure:\n{json.dumps(raw_job_data, indent=2)}")
                self.logger.info("-"*50)
                self.logger.info(f"Job ID: {raw_job_data.get('job_id')}")
                self.logger.info(f"API Key: {raw_job_data.get('api_key')}")
                self.logger.info(f"Model: {raw_job_data.get('model')}")
                self.logger.info(f"Max Output: {raw_job_data.get('max_output')}")
                self.logger.info(f"Temperature: {raw_job_data.get('temperature')}")

                self.logger.info("\nMessage Structure:")
                for idx, message in enumerate(raw_job_data.get("messages", [])):
                    self.logger.info(f"\nMessage {idx + 1}:")
                    self.logger.info(f"Role: {message.get('role')}")
                    self.logger.info(f"Content: {message.get('content')}")

                self.logger.info("="*50 + "\n")

                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.PROCESSING
                )

                prompt = raw_job_data["messages"][-1]["content"]
                self.logger.info("\nPrompt Matching:")
                self.logger.info(f"Received: {prompt}")

                matching_prompt = None
                if USE_STORED_RESPONSES:
                    matching_prompt = self.find_matching_prompt(prompt)

                self.logger.info(f"\n{'='*50}")
                self.logger.info(f"Processing new job:")
                self.logger.info(f"Job ID: {raw_job_data.get('job_id')}")
                self.logger.info(f"Original Model Requested: {raw_job_data.get('model')}")

                original_temp = raw_job_data.get('temperature', 0.7)
                capped_temp = min(original_temp, 1.0) if original_temp is not None else 0.7
                if original_temp > 1.0:
                    self.logger.info(f"Temperature capped from {original_temp} to 1.0")
                else:
                    self.logger.info(f"Temperature: {capped_temp}")

                self.logger.info(f"Max Output: {raw_job_data.get('max_output')}")

                if matching_prompt:
                    self.logger.info(f"Found matching stored prompt: {matching_prompt}")

                    model_name = raw_job_data.get('model', 'llama3').lower()
                    model_responses = self.stored_responses[matching_prompt].get(model_name)

                    if model_responses:
                        self.logger.info(f"\nGetting stored response for prompt: {matching_prompt[:30]}...")
                        self.logger.info(f"Available responses: {len(model_responses)}")
                        
                        complete_response = self.get_next_response(matching_prompt, model_name)
                        
                        self.logger.info(f"\nResponse selection complete:")
                        self.logger.info(f"Prompt: {matching_prompt[:30]}")
                        self.logger.info(f"Model: {model_name}")

                        delay = random.uniform(10, 20)
                        self.logger.info(f"Simulating processing delay of {delay:.2f} seconds")
                        await asyncio.sleep(delay)

                        status = "success"
                        await self.monitor.update_metrics(
                            self.config.node_id,
                            stored_responses_used=self.monitor.metrics[self.config.node_id].stored_responses_used + 1,
                            total_jobs=self.monitor.metrics[self.config.node_id].total_jobs + 1,
                            successful_jobs=self.monitor.metrics[self.config.node_id].successful_jobs + 1
                        )
                    else:
                        self.logger.info(f"No stored responses for model {model_name}, using Ollama API")
                        
                        # Determine which model to use
                        ollama_model = OLLAMA_FALLBACK_MODEL if OLLAMA_OVERRIDE_MODEL else raw_job_data.get('model', 'llama3')
                        self.logger.info(f"Using Ollama model: {ollama_model}")
                        
                        # Determine temperature to use
                        ollama_temp = capped_temp if OLLAMA_CAP_TEMPERATURE else original_temp
                        if ollama_temp is None:
                            ollama_temp = 0.7
                        self.logger.info(f"Using temperature: {ollama_temp}")
                        
                        complete_response, status = await self.process_ollama_request({
                            "model": ollama_model,
                            "messages": raw_job_data["messages"],
                            "options": {
                                "temperature": ollama_temp,
                                "num_predict": raw_job_data["max_output"]
                            }
                        })
                        await self.monitor.update_metrics(
                            self.config.node_id,
                            ollama_api_calls=self.monitor.metrics[self.config.node_id].ollama_api_calls + 1,
                            total_jobs=self.monitor.metrics[self.config.node_id].total_jobs + 1,
                            successful_jobs=self.monitor.metrics[self.config.node_id].successful_jobs + 1
                        )
                else:
                    self.logger.info("No matching stored prompt, proceeding with Ollama API call")
                    
                    # Determine which model to use
                    ollama_model = OLLAMA_FALLBACK_MODEL if OLLAMA_OVERRIDE_MODEL else raw_job_data.get('model', 'llama3')
                    self.logger.info(f"Using Ollama model: {ollama_model}")
                    
                    # Determine temperature to use
                    ollama_temp = capped_temp if OLLAMA_CAP_TEMPERATURE else original_temp
                    if ollama_temp is None:
                        ollama_temp = 0.7
                    self.logger.info(f"Using temperature: {ollama_temp}")
                    
                    complete_response, status = await self.process_ollama_request({
                        "model": ollama_model,
                        "messages": raw_job_data["messages"],
                        "options": {
                            "temperature": ollama_temp,
                            "num_predict": raw_job_data["max_output"]
                        }
                    })
                    await self.monitor.update_metrics(
                        self.config.node_id,
                        ollama_api_calls=self.monitor.metrics[self.config.node_id].ollama_api_calls + 1,
                        total_jobs=self.monitor.metrics[self.config.node_id].total_jobs + 1,
                        successful_jobs=self.monitor.metrics[self.config.node_id].successful_jobs + 1
                    )

                self.logger.info("\nFinal response content:")
                self.logger.info(complete_response)

                if matching_prompt and model_responses:
                    if isinstance(complete_response, dict) and 'content' in complete_response:
                        complete_response['content'] = self.add_random_whitespace(complete_response['content'])
                    elif isinstance(complete_response, str):
                        complete_response = self.add_random_whitespace(complete_response)
                    self.logger.info("Added random whitespace to response for unique hash")

                result = {
                    "job_id": raw_job_data["job_id"],
                    "api_key": raw_job_data["api_key"],
                    "model_name": raw_job_data["model"],
                    "result": {"message": complete_response},
                    "status": status
                }

                self.logger.info("\nSending result to endpoint:")
                self.logger.info(json.dumps(result, indent=2))

                await self.make_request_with_retries(
                    "POST",
                    f"{self.api_gateway_url}/worker/update_result",
                    timeout_type='default',
                    headers=self.headers,
                    json=result
                )

        except Exception as e:
            self.logger.error("Full error details:", exc_info=True)
            await self.handle_error(f"Error in job processing loop: {str(e)}")

    async def delayed_restart(self, delay: float):
        try:
            self.logger.info(f"Node will attempt restart in {delay/3600:.2f} hours")
            await asyncio.sleep(delay)
            
            self.logger.info("Attempting node restart")
            self.node_registered = False
            self.headers = {}
            
            if not await self.verify_proxy_connection():
                self.logger.error("Failed to verify proxy before restart")
                return
                
            registration_successful = await self.register_node()
            if registration_successful:
                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.IDLE,
                    error_timestamp=None
                )
                self.logger.info("Node successfully restarted")
                
                asyncio.create_task(self.periodic_tasks())
            else:
                self.logger.error("Failed to restart node")
                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.ERROR,
                    error_timestamp=time.time()
                )
                
        except Exception as e:
            self.logger.error(f"Error during delayed restart: {e}")
            await self.monitor.update_metrics(
                self.config.node_id,
                current_status=NodeStatus.ERROR,
                error_timestamp=time.time()
            )
                    
    async def verify_node_secret(self):
        try:
            self.logger.info("Verifying node credentials...")
            response = await self.make_request_with_retries(
                "POST",
                f"{self.api_gateway_url}/check_node_secret",
                timeout_type='short',
                json={
                    "node_id": self.config.node_id,
                    "secret": self.config.secret
                }
            )
            if response.status_code != 200:
                self.logger.error("Node ID and secret verification failed")
                return False
            return True
        except Exception as e:
            self.logger.error(f"Node secret verification failed: {str(e)}")
            return False
    
    async def verify_auth(self):
        try:
            await self.auth_monitor.wait_for_healthy()
            
            response = await self.make_request_with_retries(
                "POST",
                f"{self.api_auth_url}/node/check",
                timeout_type='auth',
                json={
                    "node_id": self.config.node_id,
                    "pub_key": self.config.pub_key,
                    "account_id": self.config.account_id
                }
            )

            if response.status_code != 200:
                if response.status_code >= 500 or isinstance(response, httpx.TimeoutException):
                    await self.auth_monitor.mark_unhealthy()
                self.logger.error("Node authentication verification failed")
                return False

            verification_response = response.json()
            valid_credentials = verification_response.get("valid_credentials", False)

            if valid_credentials:
                await self.auth_monitor.mark_healthy()

            return valid_credentials

        except httpx.TimeoutException:
            await self.auth_monitor.mark_unhealthy()
            self.logger.error("Auth verification timed out")
            return False
        except Exception as e:
            self.logger.error(f"Auth verification failed: {str(e)}")
            return False

    async def register_node(self):
        try:
            if not await self.verify_node_secret():
                self.logger.error("Node secret verification failed")
                return False
    
            if not await self.verify_auth():
                self.logger.error("Auth verification failed")
                return False
    
            response = await self.make_request_with_retries(
                "POST",
                f"{self.api_gateway_url}/worker/register_node",
                timeout_type='long',
                json={
                    "node_url": self.config.node_id,
                    "node_version": "0.0.4",
                    "stats": {
                        "ram": self.config.ram,
                        "cpu": self.config.cpu,
                        "vram": self.config.vram,
                        "gpu": self.config.gpu,
                        "os": self.config.os
                    }
                }
            )
    
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("access_token"):
                    self.headers["Authorization"] = f"Bearer {response_json['access_token']}"
                    self.node_registered = True
                    self.logger.info("Node registered successfully")
                    
                    await self.update_models()
                    return True
                else:
                    self.logger.error(f"Registration failed - Missing access token. Response: {json.dumps(response_json, indent=2)}")
            else:
                self.logger.error(f"Registration failed - Unexpected status code: {response.status_code}")
                try:
                    error_body = response.json()
                    self.logger.error(f"Error response: {json.dumps(error_body, indent=2)}")
                except json.JSONDecodeError:
                    self.logger.error(f"Error response (raw): {response.text}")
            return False
        except Exception as e:
            self.logger.error(f"Registration request failed: {str(e)}")
            raise

    async def run(self):
        try:
            if not await self.verify_proxy_connection():
                self.logger.error(f"Failed to verify proxy for node {self.config.node_id}")
                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.ERROR,
                    error_timestamp=time.time()
                )
                return
    
            self.proxy_verified = True
            
            await self.monitor.register_node(self.config.node_id)
            await self.monitor.update_metrics(
                self.config.node_id,
                current_status=NodeStatus.REGISTERING
            )
    
            max_retries = 5
            retry_count = 0
            registration_successful = False
    
            while retry_count < max_retries and not registration_successful:
                try:
                    await self.auth_monitor.wait_for_healthy()
                    
                    registration_successful = await self.register_node()
                    if registration_successful:
                        await self.monitor.update_metrics(
                            self.config.node_id,
                            current_status=NodeStatus.IDLE
                        )
                        break
                    else:
                        retry_count += 1
                        if retry_count < max_retries:
                            wait_time = 2 ** retry_count
                            self.logger.warning(f"Registration failed, retrying in {wait_time} seconds...")
                            await asyncio.sleep(wait_time)
                            
                except httpx.TimeoutException:
                    retry_count += 1
                    await self.auth_monitor.mark_unhealthy()
                    if retry_count < max_retries:
                        wait_time = 2 ** retry_count
                        self.logger.warning(f"Registration timeout, retrying in {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise
                        
                except (httpx.RequestError, httpx.HTTPStatusError) as e:
                    retry_count += 1
                    if retry_count < max_retries:
                        wait_time = 2 ** retry_count
                        self.logger.warning(f"Registration error: {str(e)}. Retrying in {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise
                        
                except Exception as e:
                    retry_count += 1
                    if retry_count < max_retries:
                        wait_time = 2 ** retry_count
                        self.logger.error(f"Unexpected error during registration: {str(e)}")
                        self.logger.error("Full error details:", exc_info=True)
                        self.logger.warning(f"Retrying in {wait_time} seconds...")
                        await asyncio.sleep(wait_time)
                    else:
                        raise
    
            await self.monitor.mark_node_processed()
    
            if registration_successful:
                self.start_time = time.time()
                self.initialization_complete.set()
                
                tasks = [
                    asyncio.create_task(self.periodic_tasks(), name=f"periodic_{self.config.node_id}"),
                    asyncio.create_task(self.job_processing_loop(), name=f"jobs_{self.config.node_id}")
                ]
                
                try:
                    await asyncio.gather(*tasks)
                except Exception as e:
                    self.logger.error(f"Task error: {e}")
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
            else:
                self.logger.error(f"Failed to register node after {max_retries} attempts")
                await self.monitor.update_metrics(
                    self.config.node_id,
                    current_status=NodeStatus.ERROR,
                    error_timestamp=time.time()
                )
    
        except Exception as e:
            self.logger.error(f"Fatal error in node: {e}")
            self.logger.error("Full error details:", exc_info=True)
            await self.monitor.update_metrics(
                self.config.node_id,
                current_status=NodeStatus.ERROR,
                error_timestamp=time.time()
            )
            await self.shutdown()

    async def shutdown(self):
        self.shutdown_flag.set()
        
        if self.node_registered:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.TIMEOUTS['short']
                ) as client:
                    await client.post(
                        f"{self.api_gateway_url}/worker/deregister_node",
                        headers=self.headers,
                        json={"node_url": self.config.node_id}
                    )
            except Exception as e:
                self.logger.error(f"Error during deregistration: {e}")
    
        await self.monitor.update_metrics(
            self.config.node_id,
            current_status=NodeStatus.SHUTDOWN
        )
        self.node_registered = False
        self.headers = {}

    async def process_ollama_request(self, ollama_request, max_retries=8) -> Tuple[str, str]:
        retry_count = 0
        while retry_count < max_retries:
            try:
                transport_config = {
                    "retries": 1,
                    "verify": False,
                    "limits": httpx.Limits(
                        max_keepalive_connections=1,
                        max_connections=1,
                        keepalive_expiry=5.0
                    )
                }
    
                transport = httpx.AsyncHTTPTransport(**transport_config)
                request_body = {
                    "model": ollama_request["model"],
                    "messages": ollama_request["messages"],
                    "options": {
                        "temperature": ollama_request["options"]["temperature"],
                        "num_predict": ollama_request["options"]["num_predict"]
                    }
                }
    
                self.ollama_logger.info(
                    f"Node ID: {self.config.node_id}\n"
                    f"Request URL: {OLLAMA_API_URL}\n"
                    f"Request Body:\n{json.dumps(request_body, indent=2)}"
                )
    
                complete_response = ""
                async with httpx.AsyncClient(transport=transport,
                    timeout=self.TIMEOUTS['long']) as client:
                    async with client.stream(
                        "POST",
                        OLLAMA_API_URL, 
                        json=request_body,
                        timeout=360.0
                    ) as stream:
                        self.logger.info("Started streaming response from Ollama")
                        async for line in stream.aiter_lines():
                            try:
                                if line.strip():
                                    chunk = json.loads(line)
                                    if chunk.get("done") == True:
                                        self.logger.info("Completed streaming response from Ollama")
                                        return complete_response.strip(), "success"
    
                                    if "message" in chunk:
                                        content = chunk["message"].get("content", "")
                                        complete_response += content
    
                            except json.JSONDecodeError as e:
                                self.logger.error(f"Error parsing chunk: {line}")
                                self.logger.error(f"Parse error: {str(e)}")
                                continue
    
                return complete_response.strip(), "success"
    
            except (httpx.ReadTimeout, httpx.ReadError) as e:
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = 2 ** retry_count
                    self.logger.warning(f"Ollama request timed out, retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(f"Final timeout/read error after {max_retries} retries: {str(e)}")
                    return " ", "error"
            
            except Exception as e:
                retry_count += 1
                if retry_count < max_retries:
                    wait_time = 2 ** retry_count
                    self.logger.error(f"Error in Ollama request: {str(e)}")
                    await asyncio.sleep(wait_time)
                else:
                    self.logger.error(f"Final error after {max_retries} retries: {str(e)}")
                    return " ", "error"
        
        return " ", "error"
        
    async def handle_error(self, error_msg: str):
        self.logger.error(error_msg)
        
        restart_delay = random.uniform(ERROR_RECOVERY_MIN, ERROR_RECOVERY_MAX)
        self.logger.info(f"Scheduling node restart in {restart_delay/3600:.2f} hours")
        
        await self.shutdown()
        
        await self.monitor.update_metrics(
            self.config.node_id,
            current_status=NodeStatus.ERROR,
            error_timestamp=time.time()
        )
        
        asyncio.create_task(self.delayed_restart(restart_delay))
