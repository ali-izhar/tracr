#!/usr/bin/env python
"""
Host-side implementation of the split computing architecture.

This module implements the host side of a split computing architecture.
It manages experiment setup, data loading, and network communication with the server
for distributed computing experiments.
"""

import argparse
import logging
import os
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Generator

from src.api import (
    DeviceManager,
    DeviceType,
    create_ssh_client,
    read_yaml_file,
    start_logging_server,
    shutdown_logging_server,
)
from src.api.network.protocols import SSH_DEFAULT_CONNECT_TIMEOUT, DEFAULT_PORT

logger = logging.getLogger(__name__)


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Split Computing Host Application")

    # Required arguments
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        required=True,
        help="Path to the configuration file (YAML)",
    )

    # Optional arguments
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output"
    )

    parser.add_argument(
        "--copy-results",
        action="store_true",
        help="Copy results to the server after experiment",
    )

    return parser.parse_args()


class ExperimentHost:
    """
    Manages the experiment setup and execution on the host side.

    Responsible for:
    1. Loading and validating configuration
    2. Setting up logging infrastructure
    3. Initializing the appropriate experiment type (local or networked)
    4. Managing datasets and data loaders
    5. Running the experiment and handling results
    """

    def __init__(self, config_path: str) -> None:
        """Initialize the experiment host with specified configuration."""
        self.results_copied = False
        self.logging_server_started = False
        self.config = read_yaml_file(config_path)
        if not self.config:
            raise ValueError(f"Failed to load configuration from {config_path}")

        self._setup_logging()
        self.device_mgr = DeviceManager()
        self._verify_devices()
        self._setup_experiment()

        logger.info("Experiment host initialized successfully")

    @staticmethod
    @lru_cache(maxsize=1)
    def _load_config(config_path: str) -> Dict[str, Any]:
        """Load and cache configuration from file for efficient reuse."""
        return read_yaml_file(config_path)

    def _setup_logging(self) -> None:
        """
        Configure logging system based on configuration settings.

        Supports multiple output destinations:
        - Console output with configurable verbosity
        - File logging with rotation
        - Remote logging via logging server
        """
        log_config = self.config.get("logging", {})
        log_level_str = log_config.get("level", "INFO")
        log_level = getattr(logging, log_level_str.upper(), logging.INFO)

        # Configure file logging if specified
        log_file = log_config.get("file")
        if log_file:
            log_file = Path(log_file)
            log_file.parent.mkdir(parents=True, exist_ok=True)

        # Configure logging format
        log_format = log_config.get(
            "format", "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )

        # Start logging server if remote logging is enabled
        if log_config.get("remote", False):
            start_logging_server()
            self.logging_server_started = True
            logger.info("Remote logging server started")

        # Configure root logger
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=log_file,
            filemode="a" if log_file else None,
        )

        logger.debug(f"Logging initialized with level {log_level_str}")

    def _verify_devices(self) -> None:
        """
        Verify that all devices are properly configured and reachable.

        This method validates SERVER device connectivity which is crucial
        for networked experiments. Device connectivity issues will be logged
        as warnings or errors.
        """
        logger.info("Verifying device configuration...")

        from src.api import DeviceType

        # Check all devices - use get_devices() instead of get_all_devices()
        devices = self.device_mgr.get_devices()
        logger.info(f"Loaded {len(devices)} device(s)")

        for device in devices:
            logger.info(
                f"Device: {device.device_type}, Host: {device.get_host()}, Port: {device.get_port()}, Reachable: {device.is_reachable()}"
            )

        # Check specifically for SERVER device using both the enum and string
        server_device = self.device_mgr.get_device_by_type(DeviceType.SERVER)
        if not server_device:
            # Try with string value as fallback
            server_device = self.device_mgr.get_device_by_type("SERVER")
            if server_device:
                logger.info("SERVER device found using string name 'SERVER'")

        if server_device:
            logger.info(
                f"SERVER device found: {server_device.get_host()}:{server_device.get_port()}"
            )
            # Test if it's truly reachable
            is_reachable = server_device.is_reachable()
            logger.info(f"SERVER is reachable: {is_reachable}")
        else:
            logger.warning("No SERVER device found in configuration")

    def _setup_experiment(self) -> None:
        """
        Initialize the appropriate experiment type based on configuration.

        This method determines whether to run in local or networked mode:
        - Local mode: Runs entirely on the host machine
        - Networked mode: Distributes computation between host and server

        The mode selection can be:
        - Explicitly specified in the configuration
        - Automatically determined based on server availability
        """
        logger.info("Setting up experiment...")

        from src.api import DeviceType

        server_device = self.device_mgr.get_device_by_type(DeviceType.SERVER)
        # Try with string name if the enum doesn't work
        if not server_device:
            server_device = self.device_mgr.get_device_by_type("SERVER")

        host = None
        port = None

        if server_device:
            host = server_device.get_host()
            port = server_device.get_port()
            if port is None:
                logger.info(
                    f"No port configured in device settings, using DEFAULT_PORT={DEFAULT_PORT}"
                )
                port = DEFAULT_PORT
            logger.info(f"Using server at {host}:{port}")
        else:
            logger.warning("No SERVER device found, will run in local mode")

        # Determine if networked mode is requested or forced
        exp_config = self.config.get("experiment", {})
        exp_type = exp_config.get("type", "auto")

        # If experiment type is auto, use networked if server device is available
        if exp_type == "auto":
            exp_type = "networked" if server_device else "local"
            logger.info(f"Auto-selecting experiment type: {exp_type}")

        # Set up experiment based on type
        if exp_type == "local" or not server_device:
            from src.api.experiments.local import LocalExperiment

            self.experiment = LocalExperiment(self.config, host, port)
            logger.info("Using local experiment mode")
        elif exp_type == "networked":
            from src.api.experiments.networked import NetworkedExperiment

            if not host:
                raise ValueError(
                    "Networked experiment requires a server device with host"
                )

            self.experiment = NetworkedExperiment(self.config, host, port)
            logger.info("Using networked experiment mode")
        else:
            raise ValueError(f"Unsupported experiment type: {exp_type}")

        # Setup data loader
        self._setup_dataloader()

        # Attach data loader to experiment
        self.experiment.data_loader = self.data_loader

        logger.info(f"Experiment of type '{exp_type}' set up successfully")

    def _setup_dataloader(self) -> None:
        """
        Initialize dataset and data loader for experiment execution.

        This method:
        1. Resolves dataset specification from configuration
        2. Registers and loads the appropriate dataset
        3. Configures batch size, shuffling, and workers
        4. Sets up custom collation functions if needed
        """
        logger.info("Setting up data loader...")

        from src.experiment_design.datasets.core.loaders import DatasetRegistry
        from src.experiment_design.datasets.core.collate_fns import CollateRegistry
        import torch.utils.data

        # Get dataset and dataloader configurations
        dataset_config = self.config.get("dataset", {})
        dataloader_config = self.config.get("dataloader", {})

        # Get dataset name - required parameter
        dataset_name = dataset_config.get("name")
        if not dataset_name:
            raise ValueError(
                "Dataset name not specified in config (required 'name' field)"
            )

        # Create a copy of the dataset config for loading
        complete_config = dataset_config.copy()

        # Add transform from dataloader config if not already specified
        if "transform" not in complete_config and "transform" in dataloader_config:
            complete_config["transform"] = dataloader_config.get("transform")

        # Get the appropriate collate function if specified
        collate_fn = None
        if dataloader_config.get("collate_fn"):
            try:
                collate_fn_name = dataloader_config["collate_fn"]
                collate_fn = CollateRegistry.get(collate_fn_name)
                if not collate_fn:
                    logger.warning(
                        f"Collate function '{collate_fn_name}' not found in registry. "
                        "Using default collation."
                    )
            except KeyError:
                logger.warning(
                    f"Collate function '{dataloader_config['collate_fn']}' not found. "
                    "Using default collation."
                )

        # Load dataset using registry
        try:
            # First register the dataset if needed
            if DatasetRegistry.get_metadata(dataset_name) is None:
                logger.info(f"Registering dataset '{dataset_name}'")
                DatasetRegistry.register_dataset(dataset_name)

            # Now load the dataset
            dataset = DatasetRegistry.load(complete_config)
            logger.info(f"Loaded dataset '{dataset_name}' successfully")
        except Exception as e:
            logger.error(f"Failed to load dataset '{dataset_name}': {e}")
            raise  # Re-raise to ensure the error is properly handled

        # Create data loader
        self.data_loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=dataloader_config.get("batch_size", 1),
            shuffle=dataloader_config.get("shuffle", False),
            num_workers=dataloader_config.get("num_workers", 0),
            collate_fn=collate_fn,
        )

        logger.info(
            f"Data loader for '{dataset_name}' dataset initialized successfully"
        )

    def run_experiment(self) -> None:
        """Execute the configured experiment and handle any exceptions."""
        logger.info("Starting experiment...")

        try:
            self.experiment.run()
            logger.info(
                f"Experiment completed successfully. Results saved to {self.experiment.paths.results_dir}"
            )
        except Exception as e:
            logger.error(f"Error during experiment execution: {e}", exc_info=True)
            raise

    @contextmanager
    def _ssh_connection(self, server_device: Any) -> Generator[Any, None, None]:
        """
        Create and manage an SSH connection to the server.

        This context manager ensures proper resource cleanup even if
        an exception occurs during SSH operations.
        """
        ssh_client = None
        try:
            # Get SSH port for file transfer operations
            ssh_port = server_device.working_cparams.ssh_port
            logger.info(
                f"Establishing SSH connection to server {server_device.get_host()}..."
            )

            # Create SSH client
            ssh_client = create_ssh_client(
                host=server_device.get_host(),
                user=server_device.get_username(),
                private_key_path=server_device.get_private_key_path(),
                port=ssh_port,
                timeout=SSH_DEFAULT_CONNECT_TIMEOUT,
            )

            yield ssh_client

        except Exception as e:
            logger.error(f"SSH connection error: {e}", exc_info=True)
            raise
        finally:
            if ssh_client:
                ssh_client.close()
                logger.debug("SSH connection closed")

    def _copy_results_to_server(self) -> bool:
        """
        Transfer experiment results to the server using SCP.

        Returns:
            bool: True if results were successfully copied
        """
        logger.info("Copying results to server...")
        server_device = self.device_mgr.get_device_by_type(DeviceType.SERVER)
        if not server_device:
            logger.error("No server device found, cannot copy results")
            return False

        try:
            # Create SSH client to remote server
            ssh_client = create_ssh_client(
                server_device.hostname,
                server_device.port,
                server_device.username,
                server_device.key_path,
            )

            # Copy results directory to server
            local_results_path = self.experiment.paths.results_dir
            remote_results_path = (
                f"{server_device.results_dir}/{local_results_path.name}"
            )

            logger.info(
                f"Copying results from {local_results_path} to {remote_results_path}"
            )

            # Use scp to copy the directory
            cmd = f"scp -r -i {server_device.key_path} {local_results_path} {server_device.username}@{server_device.hostname}:{server_device.results_dir}/"
            logger.debug(f"Running command: {cmd}")

            # Execute the command
            os.system(cmd)
            logger.info("Results copied successfully")

            # Close SSH connection
            ssh_client.close()

            # Set the flag to indicate results have been copied
            self.results_copied = True
            return True
        except Exception as e:
            logger.error(f"Failed to copy results: {e}", exc_info=True)
            return False

    def cleanup(self) -> None:
        """
        Release resources and perform final operations.

        Handles:
        1. Optional copying of results to the server
        2. Shutdown of logging infrastructure
        """
        logger.info("Cleaning up...")

        # Optionally copy results to server if not already done
        if not self.results_copied and self.config.get(
            "copy_results_on_cleanup", False
        ):
            success = self._copy_results_to_server()
            if success:
                logger.info("Results copied during cleanup")

        if self.logging_server_started:
            shutdown_logging_server()

        logger.info("Cleanup completed")


def main() -> None:
    """
    Main entry point for the host application.

    Handles:
    1. Command-line argument parsing
    2. Initialization of the experiment host
    3. Experiment execution and result handling
    4. Error handling and cleanup
    """
    args = parse_arguments()
    config_path = Path(args.config)

    logging_level = logging.INFO
    logging.basicConfig(
        level=logging_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    host = None
    try:
        print(f"Initializing experiment with config from {config_path}...")

        # Load config to check if we should modify it
        config = read_yaml_file(str(config_path))

        # Add experiment type if not already set - prefer networked unless explicit
        if "experiment" not in config:
            config["experiment"] = {}
        if "type" not in config["experiment"]:
            config["experiment"]["type"] = "networked"
            print("Setting experiment type to 'networked'")

        host = ExperimentHost(str(config_path))

        print("Starting experiment...")
        host.run_experiment()

        if args.copy_results and host:
            print("Copying results to server...")
            success = host._copy_results_to_server()
            if success:
                print("Results successfully copied to server")
            else:
                print("Failed to copy results to server")

    except KeyboardInterrupt:
        print("\nExperiment interrupted by user")
        logger.info("Experiment interrupted by user")
    except Exception as e:
        print(f"Error: {e}")
        logger.error(f"Experiment failed: {e}", exc_info=True)
    finally:
        if host:
            print("Cleaning up...")
            host.cleanup()
            print("Done.")
        else:
            print("Exiting without cleanup (host was not initialized)")


if __name__ == "__main__":
    main()
