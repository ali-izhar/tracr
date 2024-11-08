# src/utils/ml_utils.py

import logging
from typing import Any, List, Tuple, Optional

import numpy as np
import torch
from PIL import Image, ImageDraw, ImageFont
import cv2  # type: ignore

logger = logging.getLogger("split_computing_logger")


class ClassificationUtils:
    """Utilities for classification tasks."""

    def __init__(self, class_names: List[str], font_path: str):
        self.class_names = class_names
        self.font_path = font_path

    def postprocess(self, output: torch.Tensor) -> Tuple[str, float]:
        """Postprocess ImageNet classification results."""
        if output.dim() > 2:
            output = output.squeeze()
        logits = output[0] if output.dim() == 2 else output

        probabilities = torch.nn.functional.softmax(logits, dim=0)
        top5_prob, top5_catid = torch.topk(probabilities, 5)
        if max(top5_catid) >= len(self.class_names):
            logger.error(
                f"Invalid class index {max(top5_catid)} for {len(self.class_names)} classes"
            )
            return ("unknown", 0.0)

        logger.debug("\nTop 5 predictions:")
        logger.debug("-" * 50)
        for i, (prob, catid) in enumerate(zip(top5_prob, top5_catid)):
            class_name = self.class_names[catid.item()]
            prob_value = prob.item()
            logger.debug(
                f"#{i+1:<2} {class_name:<30} - {prob_value:>6.2%} (index: {catid.item()})"
            )
        logger.debug("-" * 50)

        class_name = self.class_names[top5_catid[0].item()]
        confidence = top5_prob[0].item()
        return (class_name, confidence)

    def draw_prediction_with_truth(
        self,
        image: Image.Image,
        predicted_class: str,
        confidence: float,
        true_class: str,
        font_size: int = 20,
        text_color: str = "black",
        bg_color: str = "white",
        padding: int = 10,
    ) -> Image.Image:
        """Draw both prediction and ground truth on image."""
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype(self.font_path, font_size)
        except IOError:
            font = ImageFont.load_default()
            logger.warning("Failed to load font. Using default font.")

        # Prepare prediction and truth texts
        pred_text = f"Pred: {predicted_class} ({confidence:.1%})"
        truth_text = f"True: {true_class}"

        # Calculate text dimensions
        pred_bbox = draw.textbbox((0, 0), pred_text, font=font)
        truth_bbox = draw.textbbox((0, 0), truth_text, font=font)

        text_width = max(pred_bbox[2] - pred_bbox[0], truth_bbox[2] - truth_bbox[0])
        text_height = (
            (pred_bbox[3] - pred_bbox[1]) + (truth_bbox[3] - truth_bbox[1]) + padding
        )

        # Calculate positions
        x = image.width - text_width - 2 * padding
        y_pred = padding
        y_truth = y_pred + (pred_bbox[3] - pred_bbox[1]) + padding

        # Draw background
        background = Image.new(
            "RGBA", (text_width + 2 * padding, text_height + 2 * padding), bg_color
        )
        image.paste(background, (x - padding, y_pred - padding), background)

        # Draw prediction and truth
        draw.text((x, y_pred), pred_text, font=font, fill=text_color)
        draw.text((x, y_truth), truth_text, font=font, fill=text_color)

        return image


class DetectionUtils:
    """Utilities for object detection tasks."""

    def __init__(
        self,
        class_names: List[str],
        font_path: str,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        input_size: Tuple[int, int] = (224, 224),
    ):
        """Initialize detection utilities with thresholds and input size."""
        self.class_names = class_names
        self.font_path = font_path
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.input_size = input_size

    def postprocess(
        self,
        outputs: Any,
        original_img_size: Optional[Tuple[int, int]] = None,
        *args,
        **kwargs,
    ) -> List[Tuple[List[int], float, int]]:
        """Postprocess detection output."""
        if not original_img_size:
            raise ValueError(
                "original_img_size is required for detection postprocessing"
            )

        logger.debug(f"Starting postprocessing with image size {original_img_size}")

        if isinstance(outputs, tuple):
            outputs = outputs[0]

        outputs = outputs.detach().cpu().numpy()
        outputs = outputs[np.newaxis, :] if outputs.ndim == 1 else outputs
        outputs = np.transpose(np.squeeze(outputs))
        rows = outputs.shape[0]

        boxes, scores, class_ids = [], [], []
        img_w, img_h = original_img_size
        input_h, input_w = self.input_size

        x_factor = img_w / input_w
        y_factor = img_h / input_h

        for i in range(rows):
            class_scores = outputs[i][4:]
            max_score = np.max(class_scores)

            if max_score >= self.conf_threshold:
                class_id = np.argmax(class_scores)
                x, y, w, h = outputs[i][:4]
                left = int((x - w / 2) * x_factor)
                top = int((y - h / 2) * y_factor)
                width = int(w * x_factor)
                height = int(h * y_factor)
                class_ids.append(class_id)
                scores.append(max_score)
                boxes.append([left, top, width, height])

        if boxes:
            indices = cv2.dnn.NMSBoxes(
                boxes, scores, self.conf_threshold, self.iou_threshold
            )
            detections = []

            if indices is not None and len(indices) > 0:
                for i in indices.flatten():
                    detections.append((boxes[i], scores[i], class_ids[i]))
            return detections

        return []

    def draw_detections(
        self,
        image: Image.Image,
        detections: List[Tuple[List[int], float, int]],
        font_size: int = 12,
        box_color: str = "red",
        text_color: str = "white",
        bg_color: Tuple[int, int, int, int] = (0, 0, 0, 128),
        padding: int = 5,
    ) -> Image.Image:
        """Draw detection boxes and labels on an image."""
        draw = ImageDraw.Draw(image)
        try:
            font = ImageFont.truetype(self.font_path, font_size)
        except IOError:
            font = ImageFont.load_default()
            logger.warning("Failed to load font. Using default font.")

        for box, score, class_id in detections:
            if isinstance(box, (list, tuple)) and len(box) == 4:
                x1, y1, w, h = box
                x2, y2 = x1 + w, y1 + h

                # Draw box
                draw.rectangle([x1, y1, x2, y2], outline=box_color, width=2)

                # Draw label
                label = f"{self.class_names[class_id]}: {score:.2f}"
                bbox = draw.textbbox((0, 0), label, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]

                label_x = max(x1 + padding, 0)
                label_y = max(y1 + padding, 0)

                # Draw label background
                background = Image.new(
                    "RGBA", (text_w + 2 * padding, text_h + 2 * padding), bg_color
                )
                image.paste(
                    background, (label_x - padding, label_y - padding), background
                )

                # Draw label text
                draw.text((label_x, label_y), label, fill=text_color, font=font)

        return image
