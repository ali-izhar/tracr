# src/utils/experiment_utils.py

import time
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd
import torch
from PIL import Image
from tqdm import tqdm

from .compression import CompressData
from .ml_utils import ClassificationUtils, DetectionUtils
from .network_utils import NetworkManager
from .power_meter import PowerMeter

import logging

logger = logging.getLogger(__name__)


class SplitExperimentRunner:
    """Manages split inference experiment execution and data collection."""

    def __init__(
        self,
        config: dict,
        model: torch.nn.Module,
        data_loader: torch.utils.data.DataLoader,
        network_manager: NetworkManager,
        device: torch.device,
    ) -> None:
        """Initialize experiment runner."""
        self.config = config
        self.model = model
        self.data_loader = data_loader
        self.network_manager = network_manager
        self.device = device
        self._setup_directories()
        self._setup_ml_utils()
        self.power_meter = PowerMeter(device)

    def _setup_directories(self) -> None:
        """Create necessary directories for results and images."""
        self.results_dir = Path("results")
        self.results_dir.mkdir(exist_ok=True)
        self.images_dir = self.results_dir / "images"
        self.images_dir.mkdir(exist_ok=True)
        logger.info(f"Created results directories at {self.results_dir}")

    def _setup_ml_utils(self) -> None:
        """Initialize ML utilities based on configuration."""
        input_size = tuple(self.config["model"].get("input_size", [3, 224, 224])[1:])
        common_args = {
            "font_path": self.config["default"].get("font_path", ""),
            "class_names": self.config["dataset"]["args"].get("class_names", []),
        }

        model_name = self.config["model"].get("model_name", "").lower()
        if "yolo" in model_name:
            self.ml_utils = DetectionUtils(input_size=input_size, **common_args)
            logger.info("Initialized Detection Utils for YOLO model")
        else:
            self.ml_utils = ClassificationUtils(**common_args)
            logger.info("Initialized Classification Utils")

    def process_single_image(
        self,
        inputs: torch.Tensor,
        original_image: Image.Image,
        image_file: str,
        split_layer: int,
        output_dir: Path,
    ) -> Optional[Tuple[float, float, float]]:
        """Process a single image and return timing information."""
        try:
            # Host processing
            host_start = time.time()
            input_tensor = inputs.to(self.device)
            output = self.model(input_tensor, end=split_layer)
            data_to_send = (output, original_image.size)
            compressed_output, _ = CompressData.compress_data(data=data_to_send)
            host_time = time.time() - host_start

            # Network transfer and server processing
            travel_start = time.time()
            detections, server_time = self.network_manager.communicate_with_server(
                split_layer, compressed_output
            )
            travel_time = time.time() - travel_start - server_time

            # Save results if there are any detections
            if detections:
                self._save_processed_image(
                    original_image, detections, image_file, output_dir
                )

            return host_time, travel_time, server_time

        except Exception as e:
            logger.error(f"Error processing image: {e}")
            return None

    def _save_processed_image(
        self,
        image: Image.Image,
        predictions: List[Tuple[List[int], float, int]],
        image_file: str,
        output_dir: Path,
    ) -> None:
        """Save the processed image with visualizations."""
        try:
            img = image.copy()
            img_with_predictions = self.ml_utils.draw_detections(img, predictions)
            output_path = output_dir / f"{Path(image_file).stem}_predictions.jpg"
            img_with_predictions.save(output_path)
            logger.debug(f"Saved prediction image to {output_path}")
        except Exception as e:
            logger.error(f"Error saving processed image: {e}")

    def test_split_performance(
        self, split_layer: int
    ) -> Tuple[float, float, float, float]:
        """Evaluate performance metrics for a specific split layer."""
        host_times, travel_times, server_times = [], [], []
        split_dir = self.images_dir / f"split_{split_layer}"
        split_dir.mkdir(exist_ok=True)
        logger.info(f"Testing split at layer {split_layer}")

        with torch.no_grad():
            for inputs, original_images, image_files in tqdm(
                self.data_loader, desc=f"Testing split at layer {split_layer}"
            ):
                times = self.process_single_image(
                    inputs, original_images[0], image_files[0], split_layer, split_dir
                )
                if times:
                    host_time, travel_time, server_time = times
                    host_times.append(host_time)
                    travel_times.append(travel_time)
                    server_times.append(server_time)

        return self._calculate_total_times(host_times, travel_times, server_times)

    def _calculate_total_times(
        self,
        host_times: List[float],
        travel_times: List[float],
        server_times: List[float],
    ) -> Tuple[float, float, float, float]:
        """Calculate and log total timing information."""
        total_host = sum(host_times)
        total_travel = sum(travel_times)
        total_server = sum(server_times)
        total = total_host + total_travel + total_server

        logger.info(
            f"Performance metrics:"
            f"\n\tHost time: {total_host:.2f}s"
            f"\n\tTravel time: {total_travel:.2f}s"
            f"\n\tServer time: {total_server:.2f}s"
            f"\n\tTotal time: {total:.2f}s"
        )

        return total_host, total_travel, total_server, total

    def run_experiment(self) -> None:
        """Execute the split inference experiment across all layers."""
        logger.info("Starting experiment...")
        total_layers = self.config["model"].get("total_layers", 10)
        performance_records = []

        for split_layer in range(1, total_layers):
            times = self.test_split_performance(split_layer)
            performance_records.append((split_layer, *times))

        # Determine the best split point based on total time
        best_split, host, travel, server, total = min(
            performance_records, key=lambda x: x[4]
        )
        logger.info(f"Best split at layer {best_split} with total time {total:.2f}s")

        # Save experiment results
        self.save_results(performance_records)

    def save_results(
        self, results: List[Tuple[int, float, float, float, float]]
    ) -> None:
        """Save the performance metrics to an Excel file."""
        df = pd.DataFrame(
            results,
            columns=[
                "Split Layer Index",
                "Host Time",
                "Travel Time",
                "Server Time",
                "Total Processing Time",
            ],
        )
        output_file = self.results_dir / "split_layer_times.xlsx"
        df.to_excel(output_file, index=False)
        logger.info(f"Results saved to {output_file}")
