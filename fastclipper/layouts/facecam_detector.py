"""
Motor de Detecção, Reconhecimento Facial e Priorização de Destaque (YuNet ONNX + Embeddings + Falante Ativo).

Permite:
1. Detectar múltiplos rostos em frames de lives ou VODs via OpenCV FaceDetectorYN (YuNet ONNX).
2. Gerar assinatura facial (embedding vetorial 128-D normalizado) e miniatura JPEG base64 de cada rosto.
3. Priorizar automaticamente uma pessoa específica cadastrada/clicada pelo operador (target_face)
   ou focar dinamicamente em quem está falando na cena (auto_speaker / atividade de boca).
4. Calcular o deslocamento horizontal exato (`focus_center_x_ratio`) para o corte vertical 9:16
   e o retângulo ideal de webcam (`facecam_rect`) para o modo Empilhado.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastclipper.layouts.config import Rectangle

logger = logging.getLogger(__name__)
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_YUNET_MODEL = ROOT / "fastclipper" / "assets" / "models" / "face_detection_yunet_2023mar.onnx"


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


def cosine_similarity(vec_a: Optional[List[float]], vec_b: Optional[List[float]]) -> float:
    """Calcula similaridade cosseno entre dois vetores de assinatura facial."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 0.0
    return float(max(-1.0, min(1.0, dot / (norm_a * norm_b))))


def compute_face_embedding(img_bgr: Any, bbox: Tuple[int, int, int, int], landmarks: Optional[List[float]] = None) -> List[float]:
    """
    Extrai assinatura facial 128-D L2-normalizada invariante a escala, combinando:
    - Histograma espacial 4x4 de textura/gradiente (Sobel magnitude & orientação)
    - Histograma espacial de cromaticidade YCrCb (pele/contraste facial)
    - Razões geométricas dos 5 pontos faciais (olhos, nariz e cantos da boca)
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    h_img, w_img = img_bgr.shape[:2]
    x, y, w, h = bbox
    x0 = max(0, min(w_img - 1, int(x)))
    y0 = max(0, min(h_img - 1, int(y)))
    x1 = max(x0 + 1, min(w_img, int(x + w)))
    y1 = max(y0 + 1, min(h_img, int(y + h)))

    crop = img_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return [0.0] * 128

    face_96 = cv2.resize(crop, (96, 96), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(face_96, cv2.COLOR_BGR2GRAY)
    gray_eq = cv2.equalizeHist(gray)
    ycrcb = cv2.cvtColor(face_96, cv2.COLOR_BGR2YCrCb)

    # 1. Gradientes espaciais em grade 4x4 (16 células x 4 bins de direção = 64 dims)
    gx = cv2.Sobel(gray_eq, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray_eq, cv2.CV_32F, 0, 1, ksize=3)
    mag, ang = cv2.cartToPolar(gx, gy, angleInDegrees=True)
    ang_bins = ((ang % 180.0) / 45.0).astype(np.int32) % 4

    feats: List[float] = []
    cell_sz = 24
    for cy in range(4):
        for cx in range(4):
            sub_mag = mag[cy * cell_sz : (cy + 1) * cell_sz, cx * cell_sz : (cx + 1) * cell_sz]
            sub_bin = ang_bins[cy * cell_sz : (cy + 1) * cell_sz, cx * cell_sz : (cx + 1) * cell_sz]
            for b in range(4):
                feats.append(float(np.mean(sub_mag[sub_bin == b])) if np.any(sub_bin == b) else 0.0)

    # 2. Cromaticidade e luminância normalizada em grade 4x4 (16 células x 3 canais = 48 dims)
    for cy in range(4):
        for cx in range(4):
            patch_ycc = ycrcb[cy * cell_sz : (cy + 1) * cell_sz, cx * cell_sz : (cx + 1) * cell_sz]
            patch_gray = gray_eq[cy * cell_sz : (cy + 1) * cell_sz, cx * cell_sz : (cx + 1) * cell_sz]
            feats.append(float(np.mean(patch_gray)) / 255.0)
            feats.append(float(np.mean(patch_ycc[:, :, 1])) / 255.0)
            feats.append(float(np.mean(patch_ycc[:, :, 2])) / 255.0)

    # 3. Proporções geométricas dos 5 landmarks + perfil radial central (16 dims -> total 128 dims)
    fw = max(1.0, float(w))
    fh = max(1.0, float(h))
    if landmarks and len(landmarks) >= 10:
        for i in range(0, 10, 2):
            lx = (float(landmarks[i]) - float(x)) / fw
            ly = (float(landmarks[i + 1]) - float(y)) / fh
            feats.append(max(0.0, min(1.0, lx)))
            feats.append(max(0.0, min(1.0, ly)))
    else:
        feats.extend([0.35, 0.38, 0.65, 0.38, 0.50, 0.55, 0.38, 0.75, 0.62, 0.75])

    # Completa as 6 dimensões finais com momentos de intensidade das zonas olhos/nariz/boca
    eyes_band = gray_eq[18:44, 16:80]
    nose_band = gray_eq[38:66, 28:68]
    mouth_band = gray_eq[62:88, 22:74]
    for band in (eyes_band, nose_band, mouth_band):
        feats.append(float(np.mean(band)) / 255.0)
        feats.append(float(np.std(band)) / 128.0)

    def _zscore(vals: List[float]) -> Any:
        arr = np.array(vals, dtype=np.float32)
        arr = arr - float(np.mean(arr))
        std = float(np.std(arr))
        return arr / std if std > 1e-6 else arr

    vec = np.concatenate([
        _zscore(feats[:64]),
        _zscore(feats[64:112]),
        _zscore(feats[112:128]),
    ])
    norm = float(np.linalg.norm(vec))
    if norm > 1e-8:
        vec = vec / norm
    return [round(float(v), 5) for v in vec.tolist()]


def _encode_face_thumbnail(img_bgr: Any, bbox: Tuple[int, int, int, int], size: int = 88) -> Optional[str]:
    """Recorta o rosto com margem suave e codifica como Data URL JPEG base64."""
    try:
        import cv2  # type: ignore

        h_img, w_img = img_bgr.shape[:2]
        x, y, w, h = bbox
        pad_x = int(w * 0.32)
        pad_y = int(h * 0.32)
        x0 = max(0, x - pad_x)
        y0 = max(0, y - pad_y)
        x1 = min(w_img, x + w + pad_x)
        y1 = min(h_img, y + h + pad_y)
        crop = img_bgr[y0:y1, x0:x1]
        if crop.size == 0:
            return None
        thumb = cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), 84])
        if not ok:
            return None
        b64 = base64.b64encode(buf.tobytes()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:
        return None


class FacePriorityStore:
    """Armazena a configuração de prioridade facial e a assinatura do rosto em destaque."""

    VALID_MODES = {"off", "auto_speaker", "target_face", "largest_face"}

    def __init__(self, storage_path: Optional[str | Path] = None):
        self.storage_path = Path(storage_path) if storage_path else None
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "mode": "auto_speaker",
            "target_name": "Destaque Principal",
            "target_embedding": None,
            "target_thumbnail": None,
            "similarity_threshold": 0.54,
            "auto_boost_moments": True,
        }
        self._load()

    def _load(self) -> None:
        if not self.storage_path or not self.storage_path.is_file():
            return
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                with self._lock:
                    for k in ("mode", "target_name", "target_embedding", "target_thumbnail", "similarity_threshold", "auto_boost_moments"):
                        if k in data:
                            self._state[k] = data[k]
        except Exception as exc:
            logger.warning("Não foi possível carregar face_priority.json: %s", exc)

    def _save(self) -> None:
        if not self.storage_path:
            return
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            self.storage_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning("Não foi possível salvar face_priority.json: %s", exc)

    def get_state(self) -> Dict[str, Any]:
        with self._lock:
            st = dict(self._state)
        st["has_target"] = bool(st.get("target_embedding"))
        return st

    def update(
        self,
        mode: Optional[str] = None,
        target_name: Optional[str] = None,
        target_embedding: Optional[List[float]] = None,
        target_thumbnail: Optional[str] = None,
        clear_target: bool = False,
        auto_boost_moments: Optional[bool] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            if mode is not None:
                m = mode.strip().lower()
                if m in self.VALID_MODES:
                    self._state["mode"] = m
            if target_name is not None:
                self._state["target_name"] = target_name.strip()[:60] or "Destaque Principal"
            if clear_target:
                self._state["target_embedding"] = None
                self._state["target_thumbnail"] = None
                if self._state["mode"] == "target_face":
                    self._state["mode"] = "auto_speaker"
            else:
                if target_embedding is not None:
                    self._state["target_embedding"] = list(target_embedding)
                    self._state["mode"] = "target_face"
                if target_thumbnail is not None:
                    self._state["target_thumbnail"] = target_thumbnail
            if auto_boost_moments is not None:
                self._state["auto_boost_moments"] = bool(auto_boost_moments)
            self._save()
        return self.get_state()


class FacecamDetector:
    """Detector e reconhecedor facial para priorização de destaque e enquadramento 9:16."""

    def __init__(self, model_path: Optional[str | Path] = None):
        if model_path:
            self.model_path = Path(model_path)
        elif DEFAULT_YUNET_MODEL.is_file():
            self.model_path = DEFAULT_YUNET_MODEL
        else:
            self.model_path = ROOT / "face_detector.tflite"

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

    def _expand_face_to_webcam_rect(self, fx: int, fy: int, fw: int, fh: int, source_w: int, source_h: int) -> Rectangle:
        margin_x = int(fw * 1.15)
        margin_top = int(fh * 0.65)
        margin_bottom = int(fh * 1.20)
        return Rectangle(
            x=int(fx - margin_x),
            y=int(fy - margin_top),
            w=int(fw + 2 * margin_x),
            h=int(fh + margin_top + margin_bottom),
        ).clamp(source_w, source_h)

    def detect_faces_in_frame(
        self,
        img_bgr: Any,
        source_w: Optional[int] = None,
        source_h: Optional[int] = None,
        target_embedding: Optional[List[float]] = None,
        include_thumbnails: bool = True,
        similarity_threshold: float = 0.54,
    ) -> List[Dict[str, Any]]:
        """Detecta todos os rostos no frame via YuNet ONNX, calcula embeddings e ranqueia por prioridade."""
        try:
            import cv2  # type: ignore
        except ImportError:
            return []

        if img_bgr is None or img_bgr.size == 0:
            return []

        h_orig, w_orig = img_bgr.shape[:2]
        sw = int(source_w or w_orig)
        sh = int(source_h or h_orig)

        yunet_path = self.model_path if self.model_path.suffix.lower() == ".onnx" else DEFAULT_YUNET_MODEL
        if not yunet_path.is_file() or not hasattr(cv2, "FaceDetectorYN"):
            return []

        # Redimensiona para até 960px de largura para inferência rápida (~5ms)
        max_infer_w = 960
        if w_orig > max_infer_w:
            scale = max_infer_w / float(w_orig)
            infer_w = max_infer_w
            infer_h = max(64, int(round(h_orig * scale)))
            infer_img = cv2.resize(img_bgr, (infer_w, infer_h), interpolation=cv2.INTER_AREA)
        else:
            infer_w, infer_h = w_orig, h_orig
            infer_img = img_bgr

        detector = cv2.FaceDetectorYN.create(
            str(yunet_path),
            "",
            (infer_w, infer_h),
            0.42,
            0.3,
            5000,
        )
        _, raw_faces = detector.detect(infer_img)
        if raw_faces is None or len(raw_faces) == 0:
            return []

        sx_img = w_orig / float(infer_w)
        sy_img = h_orig / float(infer_h)
        sx_src = sw / float(w_orig)
        sy_src = sh / float(h_orig)

        detected: List[Dict[str, Any]] = []
        for idx, row in enumerate(raw_faces):
            # Coordenadas na imagem lida
            ix = int(round(float(row[0]) * sx_img))
            iy = int(round(float(row[1]) * sy_img))
            iw = max(16, int(round(float(row[2]) * sx_img)))
            ih = max(16, int(round(float(row[3]) * sy_img)))
            score = float(row[14])

            landmarks_img: List[float] = []
            for li in range(4, 14, 2):
                landmarks_img.append(float(row[li]) * sx_img)
                landmarks_img.append(float(row[li + 1]) * sy_img)

            emb = compute_face_embedding(img_bgr, (ix, iy, iw, ih), landmarks_img)
            sim = cosine_similarity(emb, target_embedding) if target_embedding else None
            is_match = bool(sim is not None and sim >= similarity_threshold)

            # Coordenadas na resolução original do vídeo (source_w x source_h)
            fx = max(0, int(round(ix * sx_src)))
            fy = max(0, int(round(iy * sy_src)))
            fw = max(16, int(round(iw * sx_src)))
            fh = max(16, int(round(ih * sy_src)))
            fc_rect = self._expand_face_to_webcam_rect(fx, fy, fw, fh, sw, sh)

            cx_ratio = max(0.0, min(1.0, (fx + fw / 2.0) / max(1.0, float(sw))))
            cy_ratio = max(0.0, min(1.0, (fy + fh / 2.0) / max(1.0, float(sh))))
            area_ratio = (fw * fh) / max(1.0, float(sw * sh))

            if cx_ratio < 0.38:
                pos_label = "Esquerda"
                align_hint = "left"
            elif cx_ratio > 0.62:
                pos_label = "Direita"
                align_hint = "right"
            else:
                pos_label = "Centro"
                align_hint = "center"

            thumb = _encode_face_thumbnail(img_bgr, (ix, iy, iw, ih)) if include_thumbnails else None
            detected.append({
                "face_id": f"face_{idx + 1}",
                "bbox": {"x": fx, "y": fy, "w": fw, "h": fh},
                "facecam_rect": {"x": fc_rect.x, "y": fc_rect.y, "w": fc_rect.w, "h": fc_rect.h},
                "center_x_ratio": round(cx_ratio, 4),
                "center_y_ratio": round(cy_ratio, 4),
                "area_ratio": round(area_ratio, 5),
                "confidence": round(score, 3),
                "similarity": round(sim, 3) if sim is not None else None,
                "is_priority_match": is_match,
                "position_label": pos_label,
                "solo_align": align_hint,
                "embedding": emb,
                "thumbnail_data_url": thumb,
                "_mouth_box_img": (
                    max(0, int(ix + iw * 0.20)),
                    max(0, int(iy + ih * 0.55)),
                    min(w_orig, int(ix + iw * 0.80)),
                    min(h_orig, int(iy + ih * 0.96)),
                ),
            })

        # Ordena: 1º rosto prioritário reconhecido, 2º maior similaridade/área
        detected.sort(
            key=lambda f: (
                1 if f["is_priority_match"] else 0,
                f["similarity"] if f["similarity"] is not None else -1.0,
                f["area_ratio"] * f["confidence"],
            ),
            reverse=True,
        )
        for i, item in enumerate(detected):
            item["rank"] = i + 1
        return detected

    def detect_from_image(
        self,
        image_path: str | Path,
        source_w: int,
        source_h: int,
        target_embedding: Optional[List[float]] = None,
    ) -> Dict[str, Any]:
        """Detecta rostos em uma imagem extraída e sugere o enquadramento priorizado."""
        try:
            import cv2  # type: ignore
        except ImportError:
            preset = self.suggest_corner_preset(source_w, source_h, corner="top-left")
            return {
                "detected": False,
                "method": "corner_fallback_no_vision",
                "facecam_rect": {"x": preset.x, "y": preset.y, "w": preset.w, "h": preset.h},
                "faces": [],
                "message": "OpenCV não instalado; usando preset de canto.",
            }

        img_bgr = cv2.imread(str(image_path))
        if img_bgr is None:
            raise ValueError(f"Não foi possível ler a imagem de prévia: {image_path}")

        faces = self.detect_faces_in_frame(
            img_bgr,
            source_w=source_w,
            source_h=source_h,
            target_embedding=target_embedding,
            include_thumbnails=True,
        )
        if not faces:
            preset = self.suggest_corner_preset(source_w, source_h, corner="top-left")
            return {
                "detected": False,
                "method": "corner_fallback_no_face",
                "facecam_rect": {"x": preset.x, "y": preset.y, "w": preset.w, "h": preset.h},
                "faces": [],
                "message": "Nenhum rosto detectado neste frame; ajuste manualmente sobre a prévia.",
            }

        best = faces[0]
        clean_faces = [{k: v for k, v in f.items() if not k.startswith("_")} for f in faces]
        msg = (
            f"Rosto prioritário reconhecido ({int((best['similarity'] or 1.0) * 100)}% similaridade) em {best['position_label']}."
            if best["is_priority_match"]
            else f"{len(clean_faces)} rosto(s) detectado(s). Destaque focado em {best['position_label']}."
        )
        return {
            "detected": True,
            "method": "yunet_face_priority" if best["is_priority_match"] else "yunet_face_detector",
            "facecam_rect": best["facecam_rect"],
            "focus_center_x_ratio": best["center_x_ratio"],
            "solo_align": best["solo_align"],
            "faces_count": len(clean_faces),
            "faces": clean_faces,
            "selected_face_id": best["face_id"],
            "message": msg,
        }

    def resolve_clip_focus(
        self,
        video_path: str | Path,
        inicio_s: float,
        fim_s: float,
        priority_mode: str = "auto_speaker",
        target_embedding: Optional[List[float]] = None,
        similarity_threshold: float = 0.54,
    ) -> Dict[str, Any]:
        """
        Analisa múltiplos frames do trecho [inicio_s, fim_s] para decidir quem terá destaque:
        - `target_face`: prioriza a pessoa com maior similaridade facial ao rosto alvo cadastrado.
        - `auto_speaker`: mede atividade na região da boca/expressão entre frames para focar em quem está falando.
        - `largest_face`: foca no maior rosto em cena.
        """
        mode = (priority_mode or "off").strip().lower()
        if mode == "off":
            return {"detected": False, "mode_used": "off"}

        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore
        except ImportError:
            return {"detected": False, "mode_used": "no_cv2"}

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {"detected": False, "mode_used": "open_failed"}

        try:
            sw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 1920)
            sh = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 1080)
            dur = max(0.4, float(fim_s) - float(inicio_s))
            sample_times = [
                float(inicio_s) + dur * frac
                for frac in (0.15, 0.40, 0.65, 0.85)
            ]

            tracks: List[Dict[str, Any]] = []
            for t_sec in sample_times:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_sec) * 1000.0)
                ok, frame = cap.read()
                if not ok or frame is None:
                    continue
                frame_faces = self.detect_faces_in_frame(
                    frame,
                    source_w=sw,
                    source_h=sh,
                    target_embedding=target_embedding,
                    include_thumbnails=False,
                    similarity_threshold=similarity_threshold,
                )
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

                used_track_indices: set[int] = set()
                for f in frame_faces:
                    mx0, my0, mx1, my1 = f["_mouth_box_img"]
                    mouth_crop = gray[my0:my1, mx0:mx1]
                    if mouth_crop.size > 0:
                        mouth_patch = cv2.resize(mouth_crop, (32, 20), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
                    else:
                        mouth_patch = None

                    # Associa a uma trilha facial existente que ainda não foi usada neste frame
                    matched_track = None
                    matched_idx = -1
                    for t_idx, trk in enumerate(tracks):
                        if t_idx in used_track_indices:
                            continue
                        dist_x = abs(f["center_x_ratio"] - trk["center_x_ratio"])
                        emb_sim = cosine_similarity(f["embedding"], trk["embedding"])
                        if dist_x < 0.16 or (dist_x < 0.24 and emb_sim >= 0.65):
                            matched_track = trk
                            matched_idx = t_idx
                            break

                    if matched_track is None:
                        used_track_indices.add(len(tracks))
                        tracks.append({
                            "center_x_ratios": [f["center_x_ratio"]],
                            "center_x_ratio": f["center_x_ratio"],
                            "facecam_rect": f["facecam_rect"],
                            "area_ratio": f["area_ratio"],
                            "confidence": f["confidence"],
                            "embedding": f["embedding"],
                            "similarity": f["similarity"] or 0.0,
                            "is_priority_match": f["is_priority_match"],
                            "mouth_patches": [mouth_patch] if mouth_patch is not None else [],
                            "appearances": 1,
                        })
                    else:
                        used_track_indices.add(matched_idx)
                        matched_track["center_x_ratios"].append(f["center_x_ratio"])
                        matched_track["center_x_ratio"] = sum(matched_track["center_x_ratios"]) / len(matched_track["center_x_ratios"])
                        matched_track["area_ratio"] = max(matched_track["area_ratio"], f["area_ratio"])
                        matched_track["similarity"] = max(matched_track["similarity"], f["similarity"] or 0.0)
                        matched_track["is_priority_match"] = matched_track["is_priority_match"] or f["is_priority_match"]
                        matched_track["appearances"] += 1
                        if mouth_patch is not None:
                            matched_track["mouth_patches"].append(mouth_patch)

            if not tracks:
                return {"detected": False, "mode_used": mode}

            # Calcula atividade de fala/expressão por trilha
            for trk in tracks:
                patches = trk["mouth_patches"]
                if len(patches) >= 2:
                    diffs = [float(np.mean(np.abs(patches[i + 1] - patches[i]))) for i in range(len(patches) - 1)]
                    trk["mouth_activity"] = sum(diffs) / len(diffs)
                else:
                    trk["mouth_activity"] = 0.02

            # Seleciona a pessoa vencedora segundo o modo configurado
            if mode == "target_face" and target_embedding:
                priority_tracks = [t for t in tracks if t["is_priority_match"]]
                if priority_tracks:
                    winner = max(priority_tracks, key=lambda t: (t["similarity"], t["mouth_activity"], t["area_ratio"]))
                    used_mode = "target_face_matched"
                else:
                    winner = max(tracks, key=lambda t: (t["mouth_activity"] * 2.5 + t["area_ratio"], t["appearances"]))
                    used_mode = "target_face_fallback_speaker"
            elif mode == "largest_face":
                winner = max(tracks, key=lambda t: (t["area_ratio"], t["appearances"]))
                used_mode = "largest_face"
            else:
                # auto_speaker (com bônus caso exista target_embedding correspondente)
                winner = max(
                    tracks,
                    key=lambda t: (
                        (0.35 if t["is_priority_match"] else 0.0)
                        + t["mouth_activity"] * 3.0
                        + t["area_ratio"] * 1.5
                        + 0.05 * t["appearances"]
                    ),
                )
                used_mode = "auto_speaker"

            cx = float(winner["center_x_ratio"])
            solo_align = "left" if cx < 0.38 else ("right" if cx > 0.62 else "center")
            return {
                "detected": True,
                "mode_used": used_mode,
                "focus_center_x_ratio": round(cx, 4),
                "facecam_rect": winner["facecam_rect"],
                "solo_align": solo_align,
                "priority_matched": bool(winner["is_priority_match"]),
                "similarity": round(float(winner["similarity"]), 3),
                "mouth_activity": round(float(winner["mouth_activity"]), 4),
                "tracks_count": len(tracks),
            }
        finally:
            cap.release()

    def score_clip_face_priority(
        self,
        video_path: str | Path,
        timestamp_s: float = 0.5,
        target_embedding: Optional[List[float]] = None,
        similarity_threshold: float = 0.54,
    ) -> Dict[str, Any]:
        """Avalia presença e destaque do rosto prioritário em um frame do trecho para bônus no Auto-Clip/Sugestões."""
        try:
            import cv2  # type: ignore
        except ImportError:
            return {"boost": 0.0, "priority_matched": False, "faces_count": 0}

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return {"boost": 0.0, "priority_matched": False, "faces_count": 0}
        try:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(timestamp_s)) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                return {"boost": 0.0, "priority_matched": False, "faces_count": 0}
            faces = self.detect_faces_in_frame(
                frame,
                target_embedding=target_embedding,
                include_thumbnails=False,
                similarity_threshold=similarity_threshold,
            )
            if not faces:
                return {"boost": 0.0, "priority_matched": False, "faces_count": 0}
            best = faces[0]
            if best["is_priority_match"]:
                return {
                    "boost": 0.22,
                    "priority_matched": True,
                    "faces_count": len(faces),
                    "similarity": best["similarity"],
                    "reason": "Rosto prioritário em destaque",
                }
            if best["area_ratio"] >= 0.025:
                return {
                    "boost": 0.08,
                    "priority_matched": False,
                    "faces_count": len(faces),
                    "similarity": best["similarity"],
                    "reason": "Rosto em close detectado",
                }
            return {
                "boost": 0.03,
                "priority_matched": False,
                "faces_count": len(faces),
                "similarity": best["similarity"],
                "reason": "Rosto em cena",
            }
        finally:
            cap.release()
