"""
Detector automático de Facecam com fallback determinístico.

Usa o modelo `face_detector.tflite` (MediaPipe Tasks) quando `[vision]`
(opencv-python + mediapipe) está instalado; caso contrário, informa
que a sugestão automática requer o extra `[vision]` e fornece presets de canto.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastclipper.layouts.config import Rectangle

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[2]


def compute_iou(a: Rectangle, b: Rectangle) -> float:
    """Calcula Intersection over Union entre dois retângulos."""
    x1 = max(a.x, b.x)
    y1 = max(a.y, b.y)
    x2 = min(a.x + a.w, b.x + b.w)
    y2 = min(a.y + a.h, b.y + b.h)

    inter_w = max(0, x2 - x1)
    inter_h = max(0, y2 - y1)
    inter_area = inter_w * inter_h
    if inter_area == 0:
        return 0.0

    area_a = max(0, a.w) * max(0, a.h)
    area_b = max(0, b.w) * max(0, b.h)
    union = area_a + area_b - inter_area
    return round(inter_area / union, 4) if union > 0 else 0.0


class FacecamDetector:
    """Sugere o retângulo inicial de Facecam a partir de um frame extraído."""

    def __init__(self, model_path: Optional[str | Path] = None):
        self.model_path = Path(model_path or (ROOT / "face_detector.tflite"))

    def suggest_corner_preset(self, source_w: int, source_h: int, corner: str = "top-left") -> Rectangle:
        """Retorna retângulo padrão de webcam (aprox. 24% largura x 28% altura) em um dos cantos."""
        w = max(160, int(round(source_w * 0.24)))
        h = max(120, int(round(source_h * 0.28)))
        pad_x = int(round(source_w * 0.02))
        pad_y = int(round(source_h * 0.03))

        if corner == "top-right":
            x, y = source_w - w - pad_x, pad_y
        elif corner == "bottom-left":
            x, y = pad_x, source_h - h - pad_y
        elif corner == "bottom-right":
            x, y = source_w - w - pad_x, source_h - h - pad_y
        else:
            x, y = pad_x, pad_y

        return Rectangle(x=x, y=y, w=w, h=h).clamp(source_w, source_h)

    def detect_from_image(self, image_path: str | Path, source_w: int, source_h: int) -> Dict[str, Any]:
        """Tenta detectar o rosto do streamer no frame; retorna sugestão + origem."""
        try:
            import cv2  # type: ignore
            import mediapipe as mp  # type: ignore
            from mediapipe.tasks import python as mp_python  # type: ignore
            from mediapipe.tasks.python import vision  # type: ignore
        except ImportError:
            preset = self.suggest_corner_preset(source_w, source_h, corner="top-left")
            return {
                "detected": False,
                "method": "corner_fallback_no_vision",
                "facecam_rect": {"x": preset.x, "y": preset.y, "w": preset.w, "h": preset.h},
                "message": "Pacotes [vision] (opencv-python/mediapipe) não instalados; usando preset de canto.",
            }

        if not self.model_path.is_file():
            preset = self.suggest_corner_preset(source_w, source_h, corner="top-left")
            return {
                "detected": False,
                "method": "corner_fallback_no_model",
                "facecam_rect": {"x": preset.x, "y": preset.y, "w": preset.w, "h": preset.h},
                "message": f"Modelo {self.model_path.name} não encontrado; usando preset de canto.",
            }

        img_bgr = cv2.imread(str(image_path))
        if img_bgr is None:
            raise ValueError(f"Não foi possível ler a imagem de prévia: {image_path}")

        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=img_rgb)

        base_options = mp_python.BaseOptions(model_asset_path=str(self.model_path))
        options = vision.FaceDetectorOptions(base_options=base_options, min_detection_confidence=0.45)
        with vision.FaceDetector.create_from_options(options) as detector:
            result = detector.detect(mp_image)

        if not result.detections:
            preset = self.suggest_corner_preset(source_w, source_h, corner="top-left")
            return {
                "detected": False,
                "method": "corner_fallback_no_face",
                "facecam_rect": {"x": preset.x, "y": preset.y, "w": preset.w, "h": preset.h},
                "message": "Nenhum rosto detectado neste frame; ajuste manualmente sobre a prévia.",
            }

        # Pega o maior rosto detectado e expande para enquadrar a webcam inteira (ombros + fundo)
        best = max(result.detections, key=lambda d: d.bounding_box.width * d.bounding_box.height)
        bb = best.bounding_box
        margin_x = int(bb.width * 1.1)
        margin_top = int(bb.height * 0.65)
        margin_bottom = int(bb.height * 1.15)

        rect = Rectangle(
            x=int(bb.origin_x - margin_x),
            y=int(bb.origin_y - margin_top),
            w=int(bb.width + 2 * margin_x),
            h=int(bb.height + margin_top + margin_bottom),
        ).clamp(source_w, source_h)

        return {
            "detected": True,
            "method": "mediapipe_face_detector",
            "facecam_rect": {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h},
            "message": "Facecam sugerida automaticamente a partir do rosto detectado.",
        }
