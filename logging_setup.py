from __future__ import annotations
import logging
import sys
from config import LOG_FILE, POINTS_LOG_FILE, ERROR_LOG_FILE, OLLAMA_REQUESTS_LOG

class Colors:
    INFO = '\033[94m'
    SUCCESS = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    RESET = '\033[0m'

class ColoredFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: Colors.RESET,
        logging.INFO: Colors.INFO,
        logging.WARNING: Colors.WARNING,
        logging.ERROR: Colors.ERROR,
        logging.CRITICAL: Colors.ERROR
    }

    def format(self, record):
        color = self.COLORS.get(record.levelno, Colors.RESET)
        record.levelname = f"{color}{record.levelname}{Colors.RESET}"
        
        if hasattr(record, 'node_id'):
            record.node_id = f"{Colors.INFO}[{record.node_id}]{Colors.RESET}"
            
        return super().format(record)

def setup_logging():
    logger = logging.getLogger('node_simulator')
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    console_node_formatter = ColoredFormatter(
        '%(asctime)s - %(node_id)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_general_formatter = ColoredFormatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_node_formatter = logging.Formatter(
        '%(asctime)s - [%(node_id)s] - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_general_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(console_general_formatter)
    logger.addHandler(console_handler)

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_node_formatter)
    
    node_logger = logging.getLogger('node_simulator.node')
    node_logger.setLevel(logging.INFO)
    node_logger.propagate = False

    node_console_handler = logging.StreamHandler(sys.stdout)
    node_console_handler.setFormatter(console_node_formatter)
    node_logger.addHandler(node_console_handler)
    node_logger.addHandler(file_handler)

    points_formatter = logging.Formatter(
        '%(asctime)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    points_logger = logging.getLogger('node_simulator.points')
    points_logger.setLevel(logging.INFO)
    points_logger.propagate = False

    points_handler = logging.FileHandler(POINTS_LOG_FILE)
    points_handler.setFormatter(points_formatter)
    points_logger.addHandler(points_handler)
    
    error_formatter = logging.Formatter(
        '%(asctime)s - [%(node_id)s] - %(levelname)s - %(message)s\n'
        'Request Details:\n'
        '  URL: %(url)s\n'
        '  Proxy: %(proxy)s\n'
        '  Headers: %(headers)s\n'
        '----------------------------------------',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    error_logger = logging.getLogger('node_simulator.errors')
    error_logger.setLevel(logging.ERROR)
    error_logger.propagate = False

    error_handler = logging.FileHandler(ERROR_LOG_FILE)
    error_handler.setFormatter(error_formatter)
    error_logger.addHandler(error_handler)

    ollama_formatter = logging.Formatter(
        '%(asctime)s - %(message)s\n----------------------------------------\n',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    ollama_logger = logging.getLogger('node_simulator.ollama_requests')
    ollama_logger.setLevel(logging.INFO)
    ollama_logger.propagate = False

    ollama_handler = logging.FileHandler(OLLAMA_REQUESTS_LOG)
    ollama_handler.setFormatter(ollama_formatter)
    ollama_logger.addHandler(ollama_handler)

    return logger, node_logger, points_logger, error_logger, ollama_logger
