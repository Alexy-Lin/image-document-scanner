"""Core image operations for the local document scanner."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

import cv2
import numpy as np
from PIL import Image, ImageOps


Point = tuple[float, float]


@dataclass(frozen=True)
class ScanOptions:
    """User-facing processing options."""

    mode: str = "纯白文档"
    shadow_strength: str = "标准"
    background_kernel: int = 51
    ink_threshold: int = 18
    bw_threshold: int = 0


def read_image(source: str | Path | bytes | BinaryIO) -> np.ndarray:
    """Read an image, apply EXIF orientation, and return an RGB uint8 array."""

    if isinstance(source, (str, Path)):
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            return np.asarray(image, dtype=np.uint8).copy()

    if isinstance(source, bytes):
        source = BytesIO(source)
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        return np.asarray(image, dtype=np.uint8).copy()


def rgb_to_pil(image_rgb: np.ndarray) -> Image.Image:
    """Convert an RGB array to a detached Pillow image."""

    return Image.fromarray(np.ascontiguousarray(image_rgb.astype(np.uint8)), "RGB")


def _polygon_area(points: np.ndarray) -> float:
    return float(abs(cv2.contourArea(points.astype(np.float32))))


def order_points(points: Sequence[Sequence[float]]) -> np.ndarray:
    """Return points in top-left, top-right, bottom-right, bottom-left order."""

    pts = np.asarray(points, dtype=np.float32)
    if pts.shape != (4, 2) or not np.isfinite(pts).all():
        raise ValueError("必须提供恰好四个有效坐标点")

    # Sum/difference ordering is stable for ordinary document photos.
    ordered = np.zeros((4, 2), dtype=np.float32)
    sums = pts.sum(axis=1)
    diffs = np.diff(pts, axis=1).ravel()
    ordered[0] = pts[np.argmin(sums)]
    ordered[2] = pts[np.argmax(sums)]
    ordered[1] = pts[np.argmin(diffs)]
    ordered[3] = pts[np.argmax(diffs)]

    if len({tuple(point) for point in ordered}) != 4:
        raise ValueError("四个角点不能重复")
    if _polygon_area(ordered) < 100:
        raise ValueError("四个角点围成的区域太小")
    if not cv2.isContourConvex(ordered.reshape(-1, 1, 2)):
        raise ValueError("四个角点必须构成凸四边形")
    return ordered


def detect_document_corners(image_rgb: np.ndarray) -> np.ndarray | None:
    """Find the largest plausible document quadrilateral."""

    height, width = image_rgb.shape[:2]
    scale = min(1.0, 1200.0 / max(height, width))
    small = cv2.resize(image_rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    # Phone photos often have a nearly-white page against a light table;
    # lower thresholds keep that page boundary detectable.
    edges = cv2.Canny(gray, 5, 25)
    edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    image_area = small.shape[0] * small.shape[1]
    candidates: list[tuple[float, np.ndarray]] = []
    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:80]:
        area = cv2.contourArea(contour)
        if area < image_area * 0.12:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.025 * perimeter, True)
        if len(approx) != 4 or not cv2.isContourConvex(approx):
            continue
        quad = approx.reshape(4, 2).astype(np.float32)
        # A document should not be an extremely thin quadrilateral.
        x, y, w, h = cv2.boundingRect(quad.astype(np.int32))
        if w < small.shape[1] * 0.25 or h < small.shape[0] * 0.25:
            continue
        candidates.append((area, quad))

    if not candidates:
        return None
    best = candidates[0][1] / scale
    return order_points(best)


def perspective_warp(image_rgb: np.ndarray, points: Sequence[Sequence[float]]) -> np.ndarray:
    """Rectify a page to a front-facing RGB image."""

    rect = order_points(points)
    tl, tr, br, bl = rect
    width_top = np.linalg.norm(tr - tl)
    width_bottom = np.linalg.norm(br - bl)
    height_right = np.linalg.norm(br - tr)
    height_left = np.linalg.norm(bl - tl)
    output_width = max(1, int(round(max(width_top, width_bottom))))
    output_height = max(1, int(round(max(height_left, height_right))))
    output_width = min(output_width, 5000)
    output_height = min(output_height, 7000)
    destination = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype=np.float32,
    )
    matrix = cv2.getPerspectiveTransform(rect, destination)
    return cv2.warpPerspective(image_rgb, matrix, (output_width, output_height), borderValue=(255, 255, 255))


def _odd(value: int) -> int:
    value = max(3, int(value))
    return value if value % 2 else value + 1


def enhance_document(image_rgb: np.ndarray, options: ScanOptions) -> np.ndarray:
    """Enhance a rectified document while keeping ink, stamps, and logos."""

    mode = options.mode
    if mode == "保真彩色":
        return image_rgb.copy()

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    min_side = min(gray.shape)
    strength_profiles = {
        "轻度": (0.04, 15),
        "标准": (0.06, 0),
        "强力": (0.10, -20),
    }
    kernel_scale, threshold_adjustment = strength_profiles.get(
        options.shadow_strength,
        strength_profiles["标准"],
    )
    # Scale the illumination field with the rectified page dimensions. A fixed
    # small kernel follows broad phone-camera shadows instead of flattening them.
    kernel_size = max(_odd(options.background_kernel), _odd(round(min_side * kernel_scale)))
    kernel_size = min(kernel_size, _odd(max(3, min_side // 3)))
    # Closing estimates the bright paper background even with uneven lighting.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
    background = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel)
    normalized = cv2.divide(gray, np.maximum(background, 1), scale=255)
    # Do not use global min/max normalization here: on an all-white page it
    # would turn the constant value 255 into black.
    normalized = np.clip(normalized, 0, 255).astype(np.uint8)

    if mode == "灰度":
        return cv2.cvtColor(normalized, cv2.COLOR_GRAY2RGB)

    if mode == "黑白":
        if options.bw_threshold > 0:
            _, binary = cv2.threshold(normalized, options.bw_threshold, 255, cv2.THRESH_BINARY)
        else:
            binary = cv2.adaptiveThreshold(
                normalized,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                31,
                12,
            )
        return cv2.cvtColor(binary, cv2.COLOR_GRAY2RGB)

    # Use illumination-normalized darkness for monochrome ink. A raw local
    # background difference also marks shadow edges, so it must not be used as
    # an ink mask. Keep saturated marks separately so stamps and logos survive.
    dark_limit = int(np.clip(205 - options.ink_threshold + threshold_adjustment, 120, 220))
    dark_ink = normalized < dark_limit
    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    colored_ink = (hsv[:, :, 1] > 35) & (hsv[:, :, 2] < 250)
    ink = dark_ink | colored_ink
    output = np.full_like(image_rgb, 255)
    output[ink > 0] = image_rgb[ink > 0]
    return output


def process_image(
    image_rgb: np.ndarray,
    points: Sequence[Sequence[float]],
    options: ScanOptions,
) -> np.ndarray:
    """Perspective-correct and enhance one image."""

    return enhance_document(perspective_warp(image_rgb, points), options)


def make_pdf(images_rgb: Iterable[np.ndarray], dpi: int = 200) -> bytes:
    """Encode RGB pages as a high-quality multi-page PDF."""

    pages = [rgb_to_pil(image).convert("RGB") for image in images_rgb]
    if not pages:
        raise ValueError("至少需要一页图片")
    output = BytesIO()
    pages[0].save(
        output,
        format="PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=int(dpi),
        quality=95,
        subsampling=0,
    )
    return output.getvalue()
