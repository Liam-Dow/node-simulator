# LLM Worker Node Simulator

This codebase is designed to create a fully locally hosted decentralised network of nodes that are processing a queue of open source LLM inference requests with a JWT-secured API endpoint.

Each simulated node is defined in a configuration CSV with each row representing an independent worker nodes operating in a pull-based architecture. Nodes poll a central API backed by RabbitMQ for AI inference jobs and offload jobs to a shared, self-hosted Ollama server AND/OR optionally can pull from a list of predefined prompt responses if a match is found.

## Components

The simulator consists of several key components:

*   **`NodeManager` (`node_manager.py`):** Orchestrates the simulation. Loads node configurations from `example_nodes.csv`, manages the lifecycle of worker nodes, and staggers their startup times based on account IDs.
*   **`WorkerNodeSimulator` (`node_simulator.py`):** Represents a single worker node. Handles registration with the central API, proxy verification and rotation, polling for jobs, processing inference requests (using stored responses or Ollama), and reporting results.
*   **`ProxyManager` (`proxy_manager.py`):** Manages a pool of proxies loaded from `proxies.txt`, providing rotation capabilities for nodes.
*   **`NodeMonitor` (`monitoring.py`):** Tracks the status, metrics (jobs processed, success rate, response times), and points for all active nodes. Provides periodic status reports to the console.
*   **`AuthHealthMonitor` (`monitoring.py`):** Monitors the health of the authentication service (`API_AUTH_URL`). Nodes pause operations (like startup or registration) if the auth service is marked unhealthy.
*   **Central API (`API_GATEWAY_URL`, `API_AUTH_URL`):** External endpoints that nodes interact with for registration, authentication, job retrieval, result submission, and points checking.
*   **Ollama Server (`OLLAMA_API_URL`):** A self-hosted LLM inference server that nodes use as a fallback when pre-stored responses are not available for a given job prompt.
*   **Configuration Files:**
    *   `example_nodes.csv`: Contains configuration details for each simulated node (ID, credentials, hardware specs, proxy, etc.).
    *   `proxies.txt`: A list of proxy servers for nodes to use.
    *   `config.py`: Contains constants like API endpoints, timing intervals, and file paths.
*   **Logging (`logging_setup.py`):** Configures multiple log files for different aspects of the simulation (general, node-specific, points, errors, Ollama requests).

## Registration Flow

When the simulation starts, the `NodeManager` initiates the following sequence for each configured node:

1.  **Delayed Start:** Nodes don't start simultaneously. The `NodeManager` calculates a staggered start time for each node, grouped by `account_id` and spaced out further within each account (`MIN_START_DELAY`, `MAX_START_DELAY`, `NODE_SPACING_DELAY`).
2.  **Auth Health Check:** Before starting, the node waits if the `AuthHealthMonitor` indicates the authentication service is unhealthy.
3.  **Proxy Verification:** If a proxy is configured for the node, `WorkerNodeSimulator` attempts to verify it by comparing IP addresses fetched directly and via the proxy (`https://api.ipify.org`). If verification fails, it attempts to rotate to a new proxy using the `ProxyManager`. This step must succeed before proceeding.
4.  **Node Secret Check:** The node sends its `node_id` and `secret` to the `/check_node_secret` endpoint on the API Gateway. This must return a 200 status.
5.  **Authentication Check:** The node sends its `node_id`, `pub_key`, and `account_id` to the `/node/check` endpoint on the Auth API. This must return a 200 status with `{"valid_credentials": true}`. The node also waits if the `AuthHealthMonitor` is unhealthy before attempting this. Failures or timeouts here can mark the Auth service as unhealthy.
6.  **Node Registration:** The node sends a registration request to `/worker/register_node` on the API Gateway, including its ID (`node_url`), version, and hardware stats (`ram`, `cpu`, `vram`, `gpu`, `os`).
7.  **Receive Access Token:** Upon successful registration (status 200), the API returns an access token. The node stores this token in its headers (`Authorization: Bearer <token>`) for subsequent authenticated requests.
8.  **Update Models:** Immediately after registration, the node informs the API about the AI models it supports by sending a request to `/worker/update_models`.
9.  **Ready State:** The node is now registered (`node_registered = True`) and enters the `IDLE` state, ready to start polling for jobs in its `job_processing_loop`.

*Note: Steps 3-6 involve retry logic with exponential backoff in case of failures.*

## Stale Node Management

The system doesn't explicitly label nodes as "stale" based purely on inactivity time. Instead, it handles unresponsive or error-prone nodes through an error recovery mechanism:

1.  **Error Detection:** Nodes continuously monitor for errors during API requests (timeouts, connection errors, unexpected status codes), proxy issues, or job processing failures.
2.  **Error Handling (`handle_error`):** When a significant error occurs (e.g., max retries reached for an API call, critical failure during job processing), the `handle_error` method is invoked.
3.  **Shutdown:** The node attempts to deregister itself cleanly (`/worker/deregister_node`) and then shuts down its internal processes. Its status is set to `ERROR` in the `NodeMonitor`.
4.  **Delayed Restart:** A random delay is calculated (between `ERROR_RECOVERY_MIN` and `ERROR_RECOVERY_MAX` seconds, configured in `config.py`).
5.  **Restart Attempt:** After the delay, the node attempts to restart by re-initiating the registration flow (starting from Proxy Verification). If successful, it returns to normal operation; otherwise, it remains in the `ERROR` state.

This mechanism ensures that nodes experiencing transient or persistent issues are temporarily taken offline and attempt to recover after a significant backoff period, rather than being permanently marked as stale based only on a lack of recent activity. The `NodeMonitor` tracks the `last_activity` timestamp for reporting but doesn't use it for active pruning.

## API Interaction Summary

Nodes interact with several API endpoints:

*   **Auth API (`API_AUTH_URL`)**
    *   `POST /node/check`: Verifies node identity against account details.
        *   Request Body: `{ "node_id": "...", "pub_key": "...", "account_id": "..." }`
        *   Success Response: `200 OK`, Body: `{ "valid_credentials": true }`
*   **API Gateway (`API_GATEWAY_URL`)**
    *   `POST /check_node_secret`: Verifies the node's secret key.
        *   Request Body: `{ "node_id": "...", "secret": "..." }`
        *   Success Response: `200 OK`
    *   `POST /worker/register_node`: Registers a new node session.
        *   Request Body: `{ "node_url": "...", "node_version": "...", "stats": { ... } }`
        *   Success Response: `200 OK`, Body: `{ "access_token": "..." }`
    *   `POST /worker/update_models`: Informs the API about the models the node supports.
        *   Requires Auth Header.
        *   Request Body: `{ "node_url": "...", "models": ["model1", "model2"] }`
        *   Success Response: `200 OK`
    *   `GET /worker/get_job?node_version=...`: Polls for an available inference job.
        *   Requires Auth Header.
        *   Success Response (Job Available): `200 OK`, Body: `{ "job_data": { "job_id": "...", "api_key": "...", "model": "...", "messages": [...], ... } }`
        *   Success Response (No Job): `204 No Content` (or potentially other 2xx/4xx codes if handled differently by the API)
    *   `POST /worker/update_result`: Submits the result of a completed job.
        *   Requires Auth Header.
        *   Request Body: `{ "job_id": "...", "api_key": "...", "model_name": "...", "result": { "message": "..." }, "status": "success" | "error" }`
        *   Success Response: `200 OK`
    *   `GET /get_points?node_name=...`: Retrieves the points accrued by the node.
        *   Requires Auth Header.
        *   Success Response: `200 OK`, Body: `{ "points": 123.45, "pointsraw": 678.90 }`
    *   `POST /worker/deregister_node`: Informs the API that the node is shutting down.
        *   Requires Auth Header.
        *   Request Body: `{ "node_url": "..." }`
        *   Success Response: `200 OK`

*Authentication is managed via a JWT included in the `Authorization` header which is obtained during registration.*

## AI Inference Job Handling

Once registered, each `WorkerNodeSimulator` runs a `job_processing_loop`:

1.  **Poll for Job:** The node calls `GET /worker/get_job` every few seconds (approx. 5s loop time).
2.  **Receive Job:** If the API returns job data (status 200), the node extracts the details, including the prompt (last message content) and the requested model.
3.  **Check Stored Responses:**
    *   The simulator pre-loads prompt-response pairs from `control_prompts.csv` and `test_prompts.csv`.
    *   It attempts to match the incoming job prompt (case-insensitive, ignoring minor whitespace differences) against the stored prompts.
    *   If a match is found *and* stored responses exist for the specific model requested in the job, one of the stored responses is randomly selected.
    *   A simulated processing delay is introduced and a random amount of whitespace is added to the stored response content.
4.  **Fallback to Ollama:**
    *   If the incoming prompt doesn't match any stored prompt, *or* if there are no stored responses for the *specific model* requested, the node forwards the request to the configured Ollama server (`OLLAMA_API_URL`).
    *   The request to Ollama uses the messages from the job data and respects temperature/max output limits (capping temperature at 1.0). It currently defaults to using `llama3.1:latest` for Ollama requests, regardless of the original model requested in the job.
    *   The node streams the response from Ollama.
5.  **Submit Result:** The node packages the final response content (either from stored data or Ollama) along with the `job_id`, `api_key`, `model_name`, and `status` ("success" or "error") and sends it to `POST /worker/update_result`.
6.  **Loop:** The node waits briefly (if needed to complete the ~5s cycle) and polls for the next job.

## Configuration

*   **`config.py`:** Main configuration constants (API endpoints, delays, intervals).
*   **`example_nodes.csv`:** Node-specific details (credentials, hardware, proxy assignment).
*   **`proxies.txt`:** List of available proxies, one per line.

## Setup and Installation

1. **Clone the repository**

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the environment**
   - Copy `config_example.py` to `config.py` and update the API endpoints and other settings
   - Ensure you have example data files:
     - `control_prompts_example.csv` → `control_prompts.csv`
     - `test_prompts_example.csv` → `test_prompts.csv`

4. **Set up proxies (optional)**
   - Add proxy servers to `proxies.txt`, one per line
   - Format: `ip:port` or `username:password@ip:port`

5. **Configure nodes**
   - Update `example_nodes.csv` with your node configurations

## Running the Simulator

```bash
python main.py
```

Press `Ctrl+C` to initiate a graceful shutdown.

## GitHub Repository Structure

```
├── .gitignore              # Specifies files to exclude from version control
├── LICENSE                 # MIT License
├── README.md               # This documentation file
├── config_example.py       # Example configuration (copy to config.py)
├── config.py               # Main configuration (not in version control)
├── control_prompts_example.csv  # Example prompt responses (copy to control_prompts.csv)
├── data_models.py          # Data structures for the simulator
├── example_nodes.csv       # Example node configurations
├── logging_setup.py        # Logging configuration
├── main.py                 # Entry point for the simulator
├── monitoring.py           # Node monitoring and health checking
├── node_manager.py         # Manages the lifecycle of worker nodes
├── node_simulator.py       # Simulates individual worker nodes
├── proxies.txt             # List of proxy servers
├── proxy_manager.py        # Manages proxy rotation and verification
├── requirements.txt        # Python dependencies
└── test_prompts_example.csv  # Additional example prompt responses (copy to test_prompts.csv)
```

**Note:** Log files (*.log) are excluded from version control via .gitignore.
