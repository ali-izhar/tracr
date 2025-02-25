#!/usr/bin/env python
"""
Server-side implementation of the split computing architecture.

This module implements the server side of a split computing architecture.
It can be run in either networked mode (handling connections from clients) or local mode
(running experiments locally without network communication).
"""

import logging
import pickle
import socket
import sys
import time
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple, Any, Dict, Final, Generator

import torch

# Add project root to path so we can import from src module
project_root = Path(__file__).resolve().parent
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from src.api import (
    DataCompression,
    DeviceManager,
    ExperimentManager,
    DeviceType,
    start_logging_server,
    shutdown_logging_server,
)
from src.utils import read_yaml_file

# Default configuration
DEFAULT_CONFIG: Dict[str, Any] = {
    "logging": {"log_file": "logs/server.log", "log_level": "INFO"}
}

# Start logging server
logging_server = start_logging_server(device=DeviceType.SERVER, config=DEFAULT_CONFIG)
logger = logging.getLogger("split_computing_logger")

# Highest pickle protocol for efficient serialization
HIGHEST_PROTOCOL = pickle.HIGHEST_PROTOCOL
# Number of bytes used to encode message lengths
LENGTH_PREFIX_SIZE: Final[int] = 4


def get_device(requested_device: str = "cuda") -> str:
    """
    Determine the appropriate device based on availability and request.

    Args:
        requested_device: The requested device ('cuda', 'gpu', 'mps', or 'cpu')

    Returns:
        The selected device name ('cuda' or 'cpu')
    """
    requested_device = requested_device.lower()

    if requested_device == "cpu":
        logger.info("CPU device explicitly requested")
        return "cpu"

    if requested_device in ("cuda", "gpu", "mps") and torch.cuda.is_available():
        logger.info("CUDA is available and will be used")
        return "cuda"

    logger.warning("CUDA requested but not available, falling back to CPU")
    return "cpu"


@dataclass
class ServerMetrics:
    """Container for metrics collected during server operation."""

    total_requests: int = 0
    total_processing_time: float = 0.0
    avg_processing_time: float = 0.0

    def update(self, processing_time: float) -> None:
        """Update metrics with a new processing time measurement."""
        self.total_requests += 1
        self.total_processing_time += processing_time
        self.avg_processing_time = self.total_processing_time / self.total_requests


class Server:
    """
    Handles server operations for managing connections and processing data.

    This class implements both networked and local modes:
    - Networked mode: listens for client connections and processes data sent by clients
    - Local mode: runs experiments locally using the provided configuration
    """

    def __init__(
        self, local_mode: bool = False, config_path: Optional[str] = None
    ) -> None:
        """
        Initialize the Server.

        Args:
            local_mode: Whether to run in local mode (without network)
            config_path: Path to the configuration file
        """
        logger.debug("Initializing server...")
        self.device_manager = DeviceManager()
        self.experiment_manager: Optional[ExperimentManager] = None
        self.server_socket: Optional[socket.socket] = None
        self.local_mode = local_mode
        self.config_path = config_path
        self.metrics = ServerMetrics()
        self.compress_data: Optional[DataCompression] = None

        # Configure device based on config if provided
        self._load_config_and_setup_device()

        # Setup compression if in networked mode
        if not local_mode:
            self._setup_compression()
            logger.debug("Server initialized in network mode")
        else:
            logger.debug("Server initialized in local mode")

    def _load_config_and_setup_device(self) -> None:
        """Load configuration and set up device."""
        if not self.config_path:
            return

        self.config = read_yaml_file(self.config_path)
        requested_device = self.config.get("default", {}).get("device", "cuda")
        self.config["default"]["device"] = get_device(requested_device)

    def _setup_compression(self) -> None:
        """Initialize compression with minimal settings for optimal performance."""
        self.compress_data = DataCompression(
            {
                "clevel": 1,  # Minimum compression level for speed
                "filter": "NOFILTER",  # No filtering applied
                "codec": "BLOSCLZ",  # Fast codec
            }
        )
        logger.debug("Initialized compression with minimal settings")

    def start(self) -> None:
        """Start the server in either networked or local mode."""
        if self.local_mode:
            self._run_local_experiment()
        else:
            self._run_networked_server()

    def _run_local_experiment(self) -> None:
        """Run experiment locally on the server."""
        if not self.config_path:
            logger.error("Config path required for local mode")
            return

        try:
            logger.info("Starting local experiment...")
            self._setup_and_run_local_experiment()
            logger.info("Local experiment completed successfully")
        except Exception as e:
            logger.error(f"Error running local experiment: {e}", exc_info=True)

    def _setup_and_run_local_experiment(self) -> None:
        """Set up and run a local experiment based on configuration."""
        from src.experiment_design.datasets import DataManager
        import torch.utils.data

        # Load experiment configuration
        config = read_yaml_file(self.config_path)

        # Set up experiment manager and experiment
        self.experiment_manager = ExperimentManager(config, force_local=True)
        experiment = self.experiment_manager.setup_experiment()

        # Set up data loader
        dataset_config = config.get("dataset", {})
        dataloader_config = config.get("dataloader", {})

        # Get the appropriate collate function if specified
        collate_fn = self._get_collate_function(dataloader_config)

        # Create dataset and data loader
        dataset = DataManager.get_dataset(
            {"dataset": dataset_config, "dataloader": dataloader_config}
        )

        data_loader = torch.utils.data.DataLoader(
            dataset,
            batch_size=dataloader_config.get("batch_size"),
            shuffle=dataloader_config.get("shuffle"),
            num_workers=dataloader_config.get("num_workers"),
            collate_fn=collate_fn,
        )

        # Attach data loader to experiment and run
        experiment.data_loader = data_loader
        experiment.run()

    def _get_collate_function(self, dataloader_config: Dict[str, Any]) -> Optional[Any]:
        """Get the collate function specified in the configuration."""
        if not dataloader_config.get("collate_fn"):
            return None

        try:
            from src.experiment_design.datasets.collate_fns import COLLATE_FUNCTIONS

            collate_fn_name = dataloader_config["collate_fn"]
            collate_fn = COLLATE_FUNCTIONS[collate_fn_name]
            logger.debug(f"Using custom collate function: {collate_fn_name}")
            return collate_fn
        except KeyError:
            logger.warning(
                f"Collate function '{dataloader_config['collate_fn']}' not found. "
                "Using default collation."
            )
            return None

    def _run_networked_server(self) -> None:
        """Run server in networked mode, accepting client connections."""
        # Get server device configuration
        server_device = self.device_manager.get_device_by_type("SERVER")
        if not server_device:
            logger.error("No SERVER device configured. Cannot start server.")
            return

        if not server_device.is_reachable():
            logger.error("SERVER device is not reachable. Check network connection.")
            return

        # Use experiment port for network communication
        port = server_device.get_port()
        if not port:
            logger.error("No experiment port configured for SERVER device.")
            return

        logger.info(f"Starting networked server on port {port}...")

        try:
            self._setup_socket(port)
            self._accept_connections()
        except KeyboardInterrupt:
            logger.info("Server shutdown requested...")
        except Exception as e:
            logger.error(f"Server error: {e}", exc_info=True)
        finally:
            self.cleanup()

    def _accept_connections(self) -> None:
        """Accept and handle client connections."""
        while True:
            try:
                conn, addr = self.server_socket.accept()
                logger.info(f"Connected by {addr}")
                self.handle_connection(conn)
            except socket.timeout:
                # Handle timeout, allow checking for keyboard interrupt
                continue
            except ConnectionError as e:
                logger.error(f"Connection error: {e}")
                continue

    def _setup_socket(self, port: int) -> None:
        """Set up server socket with proper error handling."""
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Set a timeout to allow graceful shutdown on keyboard interrupt
            self.server_socket.settimeout(1.0)
            self.server_socket.bind(("", port))
            self.server_socket.listen()
            logger.info(f"Server is listening on port {port} (all interfaces)")
        except Exception as e:
            logger.error(f"Failed to create server socket: {e}")
            raise

    def _receive_config(self, conn: socket.socket) -> dict:
        """
        Receive and parse configuration from client.

        Args:
            conn: The client connection socket

        Returns:
            The deserialized configuration dictionary
        """
        config_length = int.from_bytes(conn.recv(LENGTH_PREFIX_SIZE), "big")
        if not self.compress_data:
            logger.error("Compression not initialized")
            return {}

        config_data = self.compress_data.receive_full_message(
            conn=conn, expected_length=config_length
        )
        return pickle.loads(config_data)

    def _process_data(
        self,
        experiment: Any,
        output: torch.Tensor,
        original_size: Tuple[int, int],
        split_layer_index: int,
    ) -> Tuple[Any, float]:
        """
        Process received data through model and return results along with processing time.

        Args:
            experiment: The experiment object that will process the data
            output: The tensor output from the client
            original_size: Original size information
            split_layer_index: The index of the split layer

        Returns:
            Tuple of (processed_result, processing_time)
        """
        server_start_time = time.time()
        processed_result = experiment.process_data(
            {"input": (output, original_size), "split_layer": split_layer_index}
        )
        return processed_result, time.time() - server_start_time

    @contextmanager
    def _safe_connection(self, conn: socket.socket) -> Generator[None, None, None]:
        """Context manager for safely handling client connections."""
        try:
            yield
        except Exception as e:
            logger.error(f"Error handling connection: {e}", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception as e:
                logger.debug(f"Error closing connection: {e}")

    def handle_connection(self, conn: socket.socket) -> None:
        """
        Handle an individual client connection.

        Args:
            conn: The client connection socket
        """
        with self._safe_connection(conn):
            # Receive configuration from the client
            config = self._receive_config(conn)
            self._update_compression(config)

            # Initialize experiment based on received configuration
            self.experiment_manager = ExperimentManager(config)
            experiment = self.experiment_manager.setup_experiment()
            experiment.model.eval()

            # Cache torch.no_grad() context for inference
            no_grad_context = torch.no_grad()

            # Send acknowledgment to the client
            conn.sendall(b"OK")

            # Process incoming data in a loop
            while True:
                # Receive header
                header = conn.recv(LENGTH_PREFIX_SIZE * 2)
                if not header or len(header) != LENGTH_PREFIX_SIZE * 2:
                    break

                split_layer_index = int.from_bytes(header[:LENGTH_PREFIX_SIZE], "big")
                expected_length = int.from_bytes(header[LENGTH_PREFIX_SIZE:], "big")

                # Receive compressed data from client
                if not self.compress_data:
                    logger.error("Compression not initialized")
                    break

                compressed_data = self.compress_data.receive_full_message(
                    conn=conn, expected_length=expected_length
                )

                # Process the data
                with no_grad_context:
                    # Decompress received data
                    output, original_size = self.compress_data.decompress_data(
                        compressed_data=compressed_data
                    )

                    # Process data using the experiment's model
                    processed_result, processing_time = self._process_data(
                        experiment=experiment,
                        output=output,
                        original_size=original_size,
                        split_layer_index=split_layer_index,
                    )

                    # Update metrics
                    self.metrics.update(processing_time)

                # Compress the processed result to send back
                compressed_result, result_size = self.compress_data.compress_data(
                    processed_result
                )

                # Send result back to client
                self._send_result(conn, result_size, processing_time, compressed_result)

    def _send_result(
        self,
        conn: socket.socket,
        result_size: int,
        processing_time: float,
        compressed_result: bytes,
    ) -> None:
        """Send the processed result back to the client."""
        # Send result size as header
        conn.sendall(result_size.to_bytes(LENGTH_PREFIX_SIZE, "big"))

        # Send processing time as fixed-length bytes
        time_bytes = (
            str(processing_time).ljust(LENGTH_PREFIX_SIZE).encode()[:LENGTH_PREFIX_SIZE]
        )
        conn.sendall(time_bytes)

        # Send compressed result data
        conn.sendall(compressed_result)

    def _update_compression(self, config: dict) -> None:
        """
        Update compression settings from received configuration.

        Args:
            config: Configuration dictionary that may contain compression settings
        """
        if "compression" in config:
            logger.debug("Updating compression settings from received config")
            self.compress_data = DataCompression(config["compression"])
        else:
            logger.warning(
                "No compression settings in config, keeping minimal settings"
            )

    def cleanup(self) -> None:
        """Clean up server resources and close the socket."""
        logger.info("Starting server cleanup...")
        if self.server_socket:
            try:
                self.server_socket.shutdown(socket.SHUT_RDWR)
                self.server_socket.close()
                self.server_socket = None
                logger.info("Server socket cleaned up")
            except Exception as e:
                logger.error(f"Error during socket cleanup: {e}")

        if logging_server:
            shutdown_logging_server(logging_server)

        # Log final metrics if any requests were processed
        if self.metrics.total_requests > 0:
            logger.info(
                f"Final metrics: {self.metrics.total_requests} requests processed, "
                f"average processing time: {self.metrics.avg_processing_time:.4f}s"
            )


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run server for split computing")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run experiment locally instead of as a network server",
    )
    parser.add_argument(
        "--config",
        type=str,
        help="Path to configuration file (required for local mode)",
        required=False,
    )
    args = parser.parse_args()

    if args.local and not args.config:
        parser.error("--config is required when running in local mode")

    return args


if __name__ == "__main__":
    args = parse_arguments()

    server = Server(local_mode=args.local, config_path=args.config)
    try:
        server.start()
    except KeyboardInterrupt:
        logger.info("Shutting down server due to keyboard interrupt...")
    except Exception as e:
        logger.error(f"Server crashed with error: {e}", exc_info=True)
    finally:
        server.cleanup()
