from __future__ import annotations
import os

# File paths
NODE_CONFIG_FILE = 'example_nodes.csv'
PROXY_CONFIG_FILE = 'proxies.txt'
LOG_FILE = 'node_simulator.log'
POINTS_LOG_FILE = 'node_points.log'
ERROR_LOG_FILE = 'node_connection_errors.log'
CONTROL_PROMPTS_FILE = 'control_prompts.csv'
TEST_PROMPTS_FILE = 'test_prompts.csv'
OLLAMA_REQUESTS_LOG = 'ollama_requests.log'

# Feature flags
USE_STORED_RESPONSES = True  # Whether to load and use stored responses
OLLAMA_OVERRIDE_MODEL = True  # Whether to override the model in Ollama requests
OLLAMA_FALLBACK_MODEL = 'llama3.1:latest'  # Model to use when OLLAMA_OVERRIDE_MODEL is True
OLLAMA_CAP_TEMPERATURE = True  # Whether to cap temperature at 1.0 for Ollama requests

# API endpoints - Replace these with your actual endpoints
API_GATEWAY_URL = os.environ.get("API_GATEWAY_URL", "https://your-api-gateway-endpoint.com")
API_AUTH_URL = os.environ.get("API_AUTH_URL", "https://your-auth-endpoint.com")
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://localhost:11434/api/chat")

# Node Starting Delays (in seconds)
MIN_START_DELAY = int(os.environ.get("MIN_START_DELAY", "1"))
MAX_START_DELAY = int(os.environ.get("MAX_START_DELAY", "500"))
NODE_SPACING_DELAY = int(os.environ.get("NODE_SPACING_DELAY", "5"))

# Time intervals (in seconds)
MODELS_UPDATE_MIN = int(os.environ.get("MODELS_UPDATE_MIN", "7920"))
MODELS_UPDATE_MAX = int(os.environ.get("MODELS_UPDATE_MAX", "15840"))
POINTS_CHECK_INTERVAL = int(os.environ.get("POINTS_CHECK_INTERVAL", "15840"))
STATUS_REPORT_INTERVAL = int(os.environ.get("STATUS_REPORT_INTERVAL", "60"))

# Error Recovery
ERROR_RECOVERY_MIN = int(os.environ.get("ERROR_RECOVERY_MIN", "3600"))
ERROR_RECOVERY_MAX = int(os.environ.get("ERROR_RECOVERY_MAX", "14400"))
