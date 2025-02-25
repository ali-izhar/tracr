# src/api/device_mgmt.py

import logging
import os
import socket
import sys
from typing import List, Union
from pathlib import Path

import ipaddress
import yaml
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

from .remote_connection import (
    SSHKeyHandler,
    SSHError,
    create_ssh_client,
    SSHConfig,
)

# Add project root to path so we can import from src module
project_root = Path(__file__).resolve().parents[2]
if str(project_root) not in sys.path:
    sys.path.append(str(project_root))

from src.utils import get_repo_root  # noqa: E402

logger = logging.getLogger("split_computing_logger")


# -------------------- Networking Utilities --------------------


class LAN:
    """Provides general networking utilities for the local area network."""

    # Define a list of IP addresses in the local network (192.168.1.0/24).
    LOCAL_CIDR_BLOCK: List[str] = [
        str(ip) for ip in ipaddress.ip_network("192.168.1.0/24").hosts()
    ]

    @classmethod
    def is_host_reachable(
        cls, host: str, port: int, timeout: Union[int, float]
    ) -> bool:
        """Determine if the given host is reachable on the given port within the given timeout.
        Attempts to open a socket connection; if successful, the host is reachable."""
        try:
            with socket.create_connection((host, port), timeout):
                logger.debug(f"Host {host} is reachable on port {port}")
                return True
        except Exception as error:
            logger.debug(f"Host {host} is not reachable on port {port}: {error}")
            return False

    @classmethod
    def get_available_hosts(
        cls,
        hosts: List[str] = None,
        port: int = 22,
        timeout: Union[int, float] = 0.5,
        max_threads: int = 10,
    ) -> List[str]:
        """Determine the availability of the given hosts on the local network.
        Uses multithreading to speed up the reachability checks."""
        # Use the provided list of hosts or default to the local CIDR block.
        hosts_to_check = hosts or cls.LOCAL_CIDR_BLOCK
        available_hosts = Queue()

        # Define a helper function to check each host.
        def check_host(host: str):
            if cls.is_host_reachable(host, port, timeout):
                available_hosts.put(host)

        logger.debug(f"Checking availability of {len(hosts_to_check)} hosts")
        # Use ThreadPoolExecutor to perform checks in parallel.
        with ThreadPoolExecutor(max_workers=max_threads) as executor:
            executor.map(check_host, hosts_to_check)

        available = list(available_hosts.queue)
        logger.debug(f"Found {len(available)} available hosts")
        return available


# -------------------- SSH Connection Parameters --------------------


class SSHConnectionParams:
    """Encapsulates SSH connection parameters for a remote host."""

    REQUIRED_FIELDS = {"host", "user", "pkey_fp"}
    SSH_PORT: int = 22  # Default SSH port
    TIMEOUT: Union[int, float] = 0.5  # Timeout for connectivity checks

    def __init__(
        self,
        host: str,
        username: str,
        rsa_key_path: Union[Path, str],
        port: int = None,
        ssh_port: int = None,
        is_default: bool = True,
    ) -> None:
        """Initialize SSH connection parameters.

        Args:
            host: Remote host address
            username: SSH username
            rsa_key_path: Path to RSA private key
            port: Port for experiment communication
            ssh_port: Port for SSH connection (defaults to 22)
            is_default: Whether this is the default connection

        Raises:
            ValueError: If required parameters are missing or invalid
        """
        self.host = host
        self._set_username(username)
        self._set_rsa_key(rsa_key_path)
        self.experiment_port = port  # Port for experiment communication
        self.ssh_port = ssh_port or self.SSH_PORT  # Port for SSH connections
        self._is_default = is_default
        logger.debug(f"Initialized SSHConnectionParams for host {host}")

    @property
    def host(self) -> str:
        """Get the host address."""
        return self._host

    @host.setter
    def host(self, value: str) -> None:
        """Set the host address."""
        self._host = value

    @classmethod
    def from_dict(cls, source: dict) -> "SSHConnectionParams":
        """Create SSHConnectionParams from a dictionary configuration.

        Args:
            source: Dictionary containing connection parameters

        Returns:
            SSHConnectionParams instance

        Raises:
            ValueError: If required fields are missing
        """
        # Validate required fields
        missing_fields = cls.REQUIRED_FIELDS - set(source.keys())
        if missing_fields:
            raise ValueError(f"Missing required fields: {missing_fields}")

        return cls(
            host=source["host"],
            username=source["user"],
            rsa_key_path=source["pkey_fp"],
            port=source.get("port"),  # Optional experiment port
            ssh_port=source.get("ssh_port"),  # Optional SSH port
            is_default=source.get("default", True),
        )

    def _set_username(self, username: str) -> None:
        """Set the username after stripping whitespace and validating its length."""
        clean_username = username.strip()
        if 0 < len(clean_username) < 32:
            self.username = clean_username
        else:
            logger.error(f"Invalid username '{username}' provided.")
            raise ValueError(f"Invalid username '{username}'.")

    def _set_rsa_key(self, rsa_key_path: Union[Path, str]) -> None:
        """Set the RSA key path and validate it.
        If the key file is not absolute, resolve it relative to the project root.
        Then attempt to load and detect the key type."""
        try:
            # Ensure rsa_key_path is a Path object.
            rsa_path = (
                Path(rsa_key_path)
                if not isinstance(rsa_key_path, Path)
                else rsa_key_path
            )

            # If the path is not absolute, resolve it relative to the project root.
            if not rsa_path.is_absolute():
                rsa_path = project_root / rsa_path

            rsa_path = rsa_path.expanduser().absolute()

            if rsa_path.exists() and rsa_path.is_file():
                # Detect the type of the SSH key.
                key_type = SSHKeyHandler.detect_key_type(rsa_path)
                logger.debug(f"Detected key type: {key_type} for {rsa_path}")

                # Load the private key.
                self.private_key = SSHKeyHandler.load_key(str(rsa_path))
                self.private_key_path = rsa_path
                logger.debug(f"SSH key loaded successfully from {rsa_path}")
            else:
                logger.error(f"Invalid SSH key path: {rsa_path}")
                raise ValueError(f"Invalid SSH key path: {rsa_path}")
        except Exception as e:
            logger.error(f"Failed to load SSH key: {e}")
            raise ValueError(f"Failed to load SSH key: {e}")

    def is_host_reachable(self) -> bool:
        """Check if the host is reachable."""
        return LAN.is_host_reachable(self.host, self.ssh_port, self.TIMEOUT)

    def get_ssh_config(self) -> SSHConfig:
        """Get SSH configuration for establishing connections.

        Returns:
            SSHConfig: Configuration for SSH connections
        """
        return SSHConfig(
            host=self.host,
            user=self.username,
            private_key_path=self.private_key_path,
            port=self.ssh_port,  # Use SSH port for connections
            timeout=self.TIMEOUT,
        )

    def to_dict(self) -> dict:
        """Serialize connection parameters to a dictionary for storage or transmission."""
        return {
            "host": self.host,
            "user": self.username,
            "pkey_fp": str(self.private_key_path),
            "port": self.experiment_port,
            "ssh_port": self.ssh_port,
        }

    def is_default(self) -> bool:
        """Return whether this connection is the default one."""
        return self._is_default


# -------------------- Device Representation --------------------


class Device:
    """Represents a network device with multiple SSH connection parameters."""

    def __init__(self, device_record: dict) -> None:
        """Initialize the Device using its configuration record.

        Args:
            device_record: Dictionary containing device configuration

        Raises:
            SSHError: If private key permissions are incorrect or key is invalid
            ValueError: If required configuration fields are missing
        """
        if "device_type" not in device_record:
            raise ValueError("Device record missing 'device_type' field")

        self.device_type = device_record["device_type"]

        if "connection_params" not in device_record:
            raise ValueError(f"Device {self.device_type} missing 'connection_params'")

        # Validate and process connection parameters
        valid_connections = []
        for conn_params in device_record["connection_params"]:
            try:
                # Verify the private key permissions before creating connection params
                private_key_path = Path(conn_params["pkey_fp"]).resolve()
                if not SSHKeyHandler.check_key_permissions(private_key_path):
                    logger.warning(
                        f"Skipping connection for {self.device_type} due to invalid key permissions: "
                        f"{private_key_path}"
                    )
                    continue

                valid_connections.append(SSHConnectionParams.from_dict(conn_params))
            except (ValueError, SSHError) as e:
                logger.warning(
                    f"Failed to initialize connection parameters for {self.device_type}: {e}"
                )
                continue
            except Exception as e:
                logger.error(
                    f"Unexpected error initializing connection for {self.device_type}: {e}"
                )
                continue

        if not valid_connections:
            logger.error(
                f"No valid connections available for device {self.device_type}. "
                "Check private key permissions (600) and directory permissions (700)."
            )
            raise SSHError(f"No valid connections for device {self.device_type}")

        # Sort connections so default is first
        self.connection_params = sorted(
            valid_connections,
            key=lambda cp: cp.is_default(),
            reverse=True,
        )

        # Select the first connection that is reachable
        self.working_cparams = next(
            (cp for cp in self.connection_params if cp.is_host_reachable()), None
        )

        if self.working_cparams:
            logger.info(
                f"Initialized device {self.device_type}, reachable at {self.working_cparams.host}"
                f" (SSH port: {self.working_cparams.ssh_port}, "
                f"experiment port: {self.working_cparams.experiment_port})"
            )
        else:
            logger.warning(
                f"Device {self.device_type} is not reachable on any configured connection"
            )

    def get_host(self) -> str:
        """Return the host address of the working connection."""
        return self.working_cparams.host

    def get_port(self) -> int:
        """Return the port of the working connection."""
        return self.working_cparams.experiment_port

    def get_username(self) -> str:
        """Return the username for the working connection."""
        return self.working_cparams.username

    def get_private_key_path(self) -> Path:
        """Return the private key path for the working connection."""
        return self.working_cparams.private_key_path

    def is_reachable(self) -> bool:
        """Return True if the device has at least one reachable connection."""
        return self.working_cparams is not None

    def serialize(self) -> tuple[str, dict[str, Union[str, bool]]]:
        """Serialize the device to a tuple containing its type and its connection parameters.
        This can be used for saving or transmitting device configuration."""
        return self.device_type, {
            "connection_params": [cp.to_dict() for cp in self.connection_params],
        }

    def get_attribute(self, attribute: str) -> Union[str, None]:
        """Retrieve a specific attribute (like host or username) of the active connection.
        Attribute matching is done in a case-insensitive way."""
        if self.working_cparams:
            attr_clean = attribute.lower().strip()
            if attr_clean in {"host", "hostname", "host name"}:
                return self.working_cparams.host
            if attr_clean in {"user", "username", "usr", "user name"}:
                return self.working_cparams.username
        return None

    def execute_remote_command(self, command: str) -> dict:
        """Execute a command on the remote device via SSH.
        Establishes an SSH client connection and runs the command."""
        if not self.is_reachable():
            raise SSHError("Device is not reachable")

        client = create_ssh_client(
            host=self.working_cparams.host,
            user=self.working_cparams.username,
            private_key_path=self.working_cparams.private_key_path,
            port=self.working_cparams.ssh_port,
        )

        with client:
            # Execute the command using the SSH client.
            return client.execute_command(command)

    def transfer_files(self, source: Path, destination: Path) -> None:
        """Transfer files to the remote device.
        If the source is a directory, transfer the entire directory; otherwise, transfer a single file.
        """
        if not self.is_reachable():
            raise SSHError("Device is not reachable")

        with self.create_ssh_client() as client:
            if source.is_dir():
                client.transfer_directory(source, destination)
            else:
                client.transfer_file(source, destination)


# -------------------- Device Manager --------------------


class DeviceManager:
    """Manages a collection of network devices using a YAML configuration file."""

    # Define default paths for device configuration and private keys
    DEFAULT_DATAFILE: Path = get_repo_root() / "config" / "devices_config.yaml"
    DEFAULT_PKEYS_DIR: Path = get_repo_root() / "config" / "pkeys"

    def __init__(self, datafile_path: Union[Path, None] = None) -> None:
        """Initialize the DeviceManager with a specified datafile path or use the default.

        Args:
            datafile_path: Optional path to the device configuration file

        Raises:
            FileNotFoundError: If config file or pkeys directory doesn't exist
            SSHError: If private key permissions are incorrect
        """
        self.datafile_path = datafile_path or self.DEFAULT_DATAFILE

        # Ensure config file exists
        if not self.datafile_path.exists():
            raise FileNotFoundError(
                f"Devices config file not found at {self.datafile_path}"
            )

        # Ensure pkeys directory exists and has correct permissions
        if not self.DEFAULT_PKEYS_DIR.exists():
            raise FileNotFoundError(
                f"PKeys directory not found at {self.DEFAULT_PKEYS_DIR}"
            )

        # Check if running on Windows
        is_windows = os.name == "nt"

        if not is_windows:
            # Only check directory permissions on Unix-like systems (Linux/WSL)
            # Check pkeys directory permissions
            dir_mode = self.DEFAULT_PKEYS_DIR.stat().st_mode & 0o777
            if dir_mode != SSHKeyHandler.REQUIRED_DIR_PERMISSIONS:
                raise SSHError(
                    f"Invalid permissions on pkeys directory: {oct(dir_mode)}. "
                    f"Required: {oct(SSHKeyHandler.REQUIRED_DIR_PERMISSIONS)}"
                )

        self._load_devices()
        logger.debug(f"DeviceManager initialized with {len(self.devices)} devices")

    def _load_devices(self) -> None:
        """Load devices from the YAML configuration file.
        Also adjusts the private key file paths to be absolute paths."""
        logger.debug(f"Loading devices from {self.datafile_path}")
        try:
            with open(self.datafile_path) as file:
                data = yaml.safe_load(file)

            # Update the private key file paths to point to the DEFAULT_PKEYS_DIR
            for device in data.get("devices", []):
                for conn_param in device.get("connection_params", []):
                    if "pkey_fp" in conn_param:
                        # Convert to absolute path in pkeys directory
                        key_name = os.path.basename(conn_param["pkey_fp"])
                        conn_param["pkey_fp"] = str(self.DEFAULT_PKEYS_DIR / key_name)

            # Create Device objects for each device configuration
            self.devices = []
            for record in data.get("devices", []):
                try:
                    device = Device(record)
                    self.devices.append(device)
                except SSHError as e:
                    logger.error(f"Failed to initialize device: {e}")
                    continue

            if not self.devices:
                logger.warning(
                    "No devices were successfully loaded. Check private key permissions."
                )
            else:
                logger.info(f"Successfully loaded {len(self.devices)} devices")

        except Exception as e:
            logger.error(f"Error loading devices configuration: {e}")
            raise

    def get_devices(
        self, available_only: bool = False, device_type: str = None
    ) -> List[Device]:
        """Retrieve devices based on their availability and (optionally) device type.
        If available_only is True, only return devices that are currently reachable."""
        filtered_devices = [
            device
            for device in self.devices
            if (not available_only or device.is_reachable())
            and (device_type is None or device.device_type == device_type)
        ]
        logger.info(
            f"Retrieved {len(filtered_devices)} devices "
            f"(available_only={available_only}, device_type={device_type})"
        )
        return filtered_devices

    def get_device_by_type(self, device_type: str) -> Device:
        """Retrieve the first device that matches the given device type (e.g., SERVER or PARTICIPANT).
        Returns None if no matching device is found."""
        return next(
            (device for device in self.devices if device.device_type == device_type),
            None,
        )

    def create_server_socket(self, host: str, port: int) -> socket.socket:
        """Create a server socket bound to the specified host and port.
        If binding to the host fails, falls back to binding to all interfaces."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Try binding to the specified host.
            sock.bind((host, port))
        except OSError:
            logger.warning(
                f"Could not bind to {host}. Falling back to all available interfaces."
            )
            # Bind to all interfaces if the specific host cannot be used.
            sock.bind(("", port))
        sock.listen(1)  # Listen for incoming connections (with a backlog of 1).
        return sock

    def _save_devices(self) -> None:
        """Save the current device configurations back to the YAML configuration file.
        Serializes each device and writes the output to the file."""
        logger.info(f"Saving devices to {self.datafile_path}")
        serialized_devices = {
            name: details
            for name, details in [device.serialize() for device in self.devices]
        }
        with open(self.datafile_path, "w") as file:
            yaml.dump(serialized_devices, file)
        logger.info(f"Saved {len(self.devices)} devices")

    def execute_command_on_devices(
        self, command: str, device_type: str = None
    ) -> dict[str, dict]:
        """Execute a command on all matching devices (filterable by device type).
        Returns a dictionary mapping each device's host to the command's output or error.
        """
        results = {}
        # Get only the devices that are available (reachable) and match the device type (if provided).
        devices = self.get_devices(available_only=True, device_type=device_type)

        for device in devices:
            try:
                # Execute the command via SSH and store the result.
                results[device.get_host()] = device.execute_remote_command(command)
            except SSHError as e:
                logger.error(f"Failed to execute command on {device.get_host()}: {e}")
                results[device.get_host()] = {"success": False, "error": str(e)}

        return results

    def transfer_to_devices(
        self, source: Path, destination: Path, device_type: str = None
    ) -> dict[str, bool]:
        """Transfer files (or directories) from a source to a destination on all matching devices.
        Returns a dictionary mapping each device's host to a boolean indicating success.
        """
        results = {}
        # Get only the available devices that match the given type.
        devices = self.get_devices(available_only=True, device_type=device_type)

        for device in devices:
            try:
                # Transfer files to the remote device.
                device.transfer_files(source, destination)
                results[device.get_host()] = True
            except SSHError as e:
                logger.error(f"Failed to transfer files to {device.get_host()}: {e}")
                results[device.get_host()] = False

        return results
