#!/usr/bin/env python
# host.py

import argparse
import logging
from pathlib import Path
import sys
import torch
from functools import lru_cache
from typing import Optional, Dict, Any, Final

# Add project root to path so we can import from src module
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from src.api import (  # noqa: E402
    DeviceType,
    ExperimentManager,
    start_logging_server,
)
from src.experiment_design.datasets import DataManager  # noqa: E402
from src.utils import read_yaml_file  # noqa: E402

# Constants
DEFAULT_SOURCE_DIR: Final[str] = "results"
DEFAULT_DEST_DIR: Final[str] = "/mnt/d/github/RACR_AI/results"

class ExperimentHost:
    """Manages the experiment setup and execution."""

    def __init__(self, config_path: str) -> None:
        """Initialize with configuration and set up components."""
        self.config = self._load_config(config_path)
        self._setup_logger(config_path)

        # Cache experiment manager and experiment
        self.experiment_manager = ExperimentManager(self.config)
        self.experiment = self.experiment_manager.setup_experiment()
        self._setup_network_connection()
        self.setup_dataloader()
        self.experiment.data_loader = self.data_loader

        logger.debug("Experiment host initialization complete")

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_config(config_path: str) -> Dict[str, Any]:
        """Cache config loading since it's used multiple times."""
        return read_yaml_file(config_path)

    def _setup_logger(self, config_path: str) -> None:
        """Initialize logger with configuration."""
        global logger
        default_log_file = self.config["logging"].get("log_file", "logs/app.log")
        default_log_level = self.config["logging"].get("log_level", "INFO")
        model_log_file = self.config["model"].get("log_file", None)
        logger_config = {
            "logging": {"log_file": default_log_file, "log_level": default_log_level},
            "model": {"log_file": model_log_file} if model_log_file else {},
        }
        self.logging_host = start_logging_server(
            device=DeviceType.PARTICIPANT, config=logger_config
        )
        logger = logging.getLogger("split_computing_logger")
        logger.info(f"Initializing experiment host with config from {config_path}")

    def setup_dataloader(self) -> None:
        """Set up the data loader with the specified configuration."""
        logger.debug("Setting up data loader...")
        dataset_config = self.config.get("dataset", {})
        dataloader_config = self.config.get("dataloader", {})

        # Cache collate function lookup
        collate_fn = self._get_collate_fn(dataloader_config)

        # Pre-fetch dataloader configs
        batch_size = dataloader_config.get("batch_size")
        shuffle = dataloader_config.get("shuffle")
        num_workers = dataloader_config.get("num_workers")

        dataset = DataManager.get_dataset(
            {"dataset": dataset_config, "dataloader": dataloader_config}
        )
        
        # Create dataloader with pre-fetched configs
        self.data_loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            num_workers=num_workers,
            collate_fn=collate_fn,
        )
        logger.debug("Data loader setup complete")

    @staticmethod
    def _get_collate_fn(dataloader_config: Dict[str, Any]) -> Optional[Any]:
        """Get collate function with error handling."""
        if collate_fn_name := dataloader_config.get("collate_fn"):
            try:
                from src.experiment_design.datasets.collate_fns import COLLATE_FUNCTIONS
                return COLLATE_FUNCTIONS[collate_fn_name]
            except KeyError:
                logger.warning(
                    f"Collate function '{collate_fn_name}' not found. Using default collation."
                )
        return None

    def run_experiment(self) -> None:
        """Execute the experiment."""
        logger.info("Starting experiment execution...")
        self.experiment.run()

    def _copy_results_to_server(self) -> None:
        """Copy results to the server using SSH utilities."""
        try:
            from src.api import SSHSession

            server_device = self.experiment_manager.device_manager.get_device_by_type("SERVER")
            if not server_device:
                logger.error("No server device found for copying results")
                return

            # Pre-compute paths and config
            ssh_config = {
                "host": server_device.get_host(),
                "user": server_device.get_username(),
                "private_key_path": str(server_device.get_private_key_path()),
                "port": server_device.get_port(),
            }

            results_config = self.config.get("results", {})
            source_dir = Path(results_config.get("source_dir", DEFAULT_SOURCE_DIR))
            destination_dir = Path(results_config.get("destination_dir", DEFAULT_DEST_DIR))

            logger.info(f"Establishing SSH connection to server {ssh_config['host']}...")
            with SSHSession(**ssh_config) as ssh:
                if ssh.copy_results_to_server(source_dir=source_dir, destination_dir=destination_dir):
                    logger.info(f"Results successfully copied to {destination_dir} on server")
                else:
                    logger.error("Failed to copy results to server")

        except Exception as e:
            logger.error(f"Error copying results to server: {e}")

    def cleanup(self) -> None:
        """Clean up resources and copy results."""
        logger.info("Starting cleanup process...")
        try:
            if hasattr(self.experiment, "network_client"):
                self.experiment.network_client.cleanup()
            self._copy_results_to_server()
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")
        finally:
            logger.info("Cleanup complete")

    def _setup_network_connection(self) -> None:
        """Establish connection to the server with detailed logging."""
        logger.debug("Setting up network connection...")
        try:
            server_device = self.experiment_manager.device_manager.get_device_by_type("SERVER")
            
            if not server_device or not server_device.is_reachable():
                logger.info("No server device configured or unreachable - running locally")
                return

            logger.debug(
                f"Server device info - Host: {server_device.get_host()}, Port: {server_device.get_port()}"
            )

            if hasattr(self.experiment, "network_client"):
                logger.debug("Attempting to connect to server...")
                self.experiment.network_client.connect()
                logger.info(
                    f"Successfully connected to server at {server_device.get_host()}:{server_device.get_port()}"
                )
            else:
                logger.debug("No network client found - running locally")

        except Exception as e:
            logger.warning(f"Network connection failed, falling back to local execution: {str(e)}")
            logger.debug("Connection error details:", exc_info=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run split inference experiment")
    parser.add_argument(
        "--config", type=str, required=True, help="Path to configuration file"
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    host = None
    try:
        host = ExperimentHost(str(config_path))
        logger = logging.getLogger("split_computing_logger")
        host.run_experiment()
    except KeyboardInterrupt:
        if logger:
            logger.info("Experiment interrupted by user")
    except Exception as e:
        if logger:
            logger.error(f"Experiment failed: {e}", exc_info=True)
        else:
            print(f"Failed to initialize: {e}")
    finally:
        if host:
            host.cleanup()
