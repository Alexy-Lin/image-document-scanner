"""Streamlit entry point for the local image document scanner."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from scanner import ScanOptions, detect_document_corners, make_pdf, process_image, read_image, rgb_to_pil


st.set_page_config(page_title="图片文档扫描工具", page_icon="📄", layout="wide")


@st.cache_resource
def point_selector_component():
    component_dir = Path(__file__).parent / "components" / "point_selector"
    return components.declare_component("point_selector", path=str(component_dir))


def selector(image_rgb, initial_points, key: str):
    component = point_selector_component()
    image_buffer = BytesIO()
    rgb_to_pil(image_rgb).save(image_buffer, format="PNG")
    return component(
        image=base64.b64encode(image_buffer.getvalue()).decode("ascii"),
        image_width=image_rgb.shape[1],
        image_height=image_rgb.shape[0],
        initial_points=initial_points or [],
        key=key,
        default=None,
    )


st.title("📄 图片文档扫描工具")
st.caption("本地运行：导入照片、校正透视、去除纸张阴影，并导出多页 PDF。")

with st.sidebar:
    st.header("处理设置")
    corner_mode = st.radio("四角来源", ["手动选择", "自动检测"], help="自动检测失败时可切换到手动选择。")
    output_mode = st.selectbox("输出模式", ["纯白文档", "保真彩色", "灰度", "黑白"])
    dpi = st.selectbox("PDF DPI", [150, 200, 300], index=1)
    background_kernel = st.slider("背景估计尺度", 21, 151, 51, step=2)
    ink_threshold = st.slider("墨迹保留阈值", 5, 60, 18)
    if output_mode == "黑白":
        bw_threshold = st.slider("黑白阈值（0=自适应）", 0, 255, 0)
    else:
        bw_threshold = 0

uploads = st.file_uploader(
    "上传图片（可多选）",
    type=["jpg", "jpeg", "png", "webp", "tif", "tiff", "bmp"],
    accept_multiple_files=True,
)

if not uploads:
    st.info("请先上传一张或多张文档照片。支持 JPG、PNG、WebP、TIFF 和 BMP。")
    st.stop()

images = []
for upload in uploads:
    try:
        images.append((upload.name, read_image(upload.getvalue())))
    except Exception as exc:  # a bad page should not hide the other uploads
        st.error(f"无法读取 {upload.name}：{exc}")

if not images:
    st.stop()

st.subheader("1. 确认每张图片的四角")
st.caption("手动模式请依次点击左上、右上、右下、左下。黑色十字线会贯穿整张图片。")
all_points = []
for index, (name, image_rgb) in enumerate(images):
    with st.expander(f"第 {index + 1} 页：{name}", expanded=index == 0):
        auto_points = detect_document_corners(image_rgb)
        point_key = f"points-{index}-{name}"
        saved_points = st.session_state.get(point_key)
        if corner_mode == "自动检测":
            if auto_points is None:
                st.warning("自动检测未找到可靠的页面边界，请切换为手动选择。")
                initial = saved_points or []
            else:
                initial = saved_points if saved_points is not None else auto_points.tolist()
                st.success("已找到候选四边形，可在画布上重新点击修正。")
        else:
            initial = saved_points or []

        chosen = selector(image_rgb, initial, key=point_key)
        if chosen is not None:
            st.session_state[point_key] = chosen
        points = st.session_state.get(point_key)
        if points and len(points) == 4:
            st.caption("已选择 4 个角点。")
        elif auto_points is not None and corner_mode == "自动检测":
            points = auto_points.tolist()
        else:
            points = None
            st.caption("等待选择 4 个角点。")
        all_points.append(points)

st.subheader("2. 生成扫描结果")
if st.button("开始处理并生成 PDF", type="primary", disabled=any(points is None for points in all_points)):
    options = ScanOptions(
        mode=output_mode,
        background_kernel=background_kernel,
        ink_threshold=ink_threshold,
        bw_threshold=bw_threshold,
    )
    results = []
    progress = st.progress(0)
    for index, ((name, image_rgb), points) in enumerate(zip(images, all_points)):
        try:
            results.append((name, process_image(image_rgb, points, options)))
        except ValueError as exc:
            st.error(f"{name} 的角点无效：{exc}")
            st.stop()
        progress.progress((index + 1) / len(images))

    st.success(f"已处理 {len(results)} 页。")
    for name, result in results:
        left, right = st.columns(2)
        with left:
            st.image(next(image for image_name, image in images if image_name == name), caption=f"原图：{name}", width="stretch")
        with right:
            st.image(result, caption=f"扫描结果：{name}", width="stretch")

    pdf_bytes = make_pdf((result for _, result in results), dpi=dpi)
    st.download_button(
        "下载多页 PDF",
        data=pdf_bytes,
        file_name="scanned_document.pdf",
        mime="application/pdf",
        type="primary",
    )
