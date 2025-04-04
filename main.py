#!/usr/bin/env python3
from __future__ import annotations
import asyncio
import logging
import sys

from logging_setup import setup_logging
from node_manager import NodeManager
from config import NODE_CONFIG_FILE, PROXY_CONFIG_FILE

async def main():
    try:
        manager = NodeManager(NODE_CONFIG_FILE, PROXY_CONFIG_FILE)
        await manager.run()
    except FileNotFoundError:
        logger.error("Could not find required files. Please ensure NODE_CONFIG_FILE, PROXIES_CONFIG_FILE exist in the same directory.")
    except KeyboardInterrupt:
        logger.info("Shutting down nodes due to keyboard interrupt...")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")

if __name__ == "__main__":
    logger, node_logger, points_logger, error_logger, ollama_logger = setup_logging()
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down gracefully...")
    except Exception as e:
        print(f"Fatal error: {e}")
    finally:
        # Close all log handlers
        loggers = [logger, node_logger, points_logger, error_logger, ollama_logger]
        for log in loggers:
            if log:
                for handler in log.handlers[:]:
                    handler.close()
                    log.removeHandler(handler)
