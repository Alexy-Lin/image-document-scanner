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

    if len(np.unique(pts, axis=0)) != 4:
        raise ValueError("四个角点不能重复")

    # Sort cyclically around the center. Unlike the common sum/difference
    # shortcut, this does not assign the same corner twice for a 45° page.
    center = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    ordered = pts[np.argsort(angles)]

    # Start at the visually top-left corner for ordinary pages. When a rotated
    # page makes that ambiguous (for example, an exact diamond), start at the
    # topmost of the tied candidates so the order is still deterministic.
    start = min(
        range(4),
        key=lambda index: (
            float(pts[index, 0] + pts[index, 1]),
            float(pts[index, 1]),
            float(pts[index, 0]),
        ),
    )
    start_point = pts[start]
    start_index = int(np.flatnonzero(np.all(ordered == start_point, axis=1))[0])
    ordered = np.roll(ordered, -start_index, axis=0)

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
    # A higher user-facing ink-retention value should preserve lighter marks.
    # Keep the default threshold unchanged (18 -> 187).
    dark_limit = int(np.clip(169 + options.ink_threshold + threshold_adjustment, 120, 240))
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


def make_pdf(
    images_rgb: Iterable[np.ndarray],
    dpi: int = 200,
    page_size: str = "原始比例",
    page_rotations: Sequence[int] | None = None,
) -> bytes:
    """Encode RGB pages as a high-quality multi-page PDF.

    ``page_size`` can preserve each image's physical dimensions or fit every
    image, without cropping, onto an A4 canvas. ``page_rotations`` contains
    clockwise quarter-turn rotations for the corresponding pages.
    """

    pages = [rgb_to_pil(image).convert("RGB") for image in images_rgb]
    if not pages:
        raise ValueError("至少需要一页图片")
    rotations = list(page_rotations) if page_rotations is not None else [0] * len(pages)
    if len(rotations) != len(pages):
        raise ValueError("页面旋转角度数量必须与 PDF 页数一致")
    for index, degrees in enumerate(rotations):
        if degrees % 90 != 0:
            raise ValueError("页面旋转角度必须是 90 度的整数倍")
        rotation = degrees % 360
        if rotation:
            transpose = {
                90: Image.Transpose.ROTATE_270,
                180: Image.Transpose.ROTATE_180,
                270: Image.Transpose.ROTATE_90,
            }[rotation]
            pages[index] = pages[index].transpose(transpose)

    if page_size == "A4（按页方向）":
        fitted_pages: list[Image.Image] = []
        for page in pages:
            # Honor each page's orientation while preserving standard A4 proportions.
            if page.width > page.height:
                a4_size = (round(297 / 25.4 * dpi), round(210 / 25.4 * dpi))
            else:
                a4_size = (round(210 / 25.4 * dpi), round(297 / 25.4 * dpi))
            fitted = ImageOps.contain(page, a4_size, method=Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", a4_size, "white")
            offset = ((a4_size[0] - fitted.width) // 2, (a4_size[1] - fitted.height) // 2)
            canvas.paste(fitted, offset)
            fitted_pages.append(canvas)
        pages = fitted_pages
    elif page_size != "原始比例":
        raise ValueError(f"不支持的 PDF 页面尺寸：{page_size}")

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
