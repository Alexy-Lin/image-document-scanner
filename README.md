# 图片文档扫描工具

一个使用 PySide6 的 Windows 桌面图片扫描工具。支持手动或自动选择页面四角、透视矫正、纸张背景增强，以及导出多页 PDF。图片处理在本地完成，不需要 LLM API。

## 功能

- 导入 JPG、PNG、WebP、TIFF 和 BMP 图片，可一次处理多页
- 自动修正照片的 EXIF 方向
- 手动选择页面四角，或尝试自动检测页面边缘
- 校正透视并提供纯白、保真彩色、灰度和黑白输出模式
- 纯白模式可选择轻度、标准或强力去阴影
- 可调背景估计范围；纯白模式还可调节墨迹保留，帮助保留较浅的灰色图形
- 每页可单独旋转 90°、180° 或 270°，处理结果预览和 PDF 会应用旋转
- 导出 150、200 或 300 DPI 的多页 PDF，每个导入的图片文件对应一页；保存时会自动处理尚未处理的页面
- PDF 页面尺寸可选原始比例或 A4；A4 模式会按旋转后的页面方向选择纵向/横向 A4，等比缩放并居中，不裁切内容

## 快速启动（Windows）

双击 `start.bat`。脚本会在首次运行时创建 `.venv` 并安装依赖，然后打开桌面窗口；界面启动后，命令行窗口会自动关闭。若程序发生未处理错误，会提示查看 `%LOCALAPPDATA%\ImageDocumentScanner\app.log`。

也可以在 PowerShell 中手动启动：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

需要 Python 3.10 或更新版本。

## 打包绿色版（Windows）

当前保留的绿色版是 `dist\compat\ImageDocumentScanner-compat.exe`，直接复制并运行即可，不需要安装 Python。该版本使用 Python 3.12 和 PySide6 6.7.3 构建，兼容性更保守；开发者可运行 `build_exe_compat.bat` 重建。

首次打包需要联网安装 PyInstaller；打包后的运行环境不需要联网。

## 项目结构

- `app.py`：PySide6 桌面界面和处理流程
- `scanner.py`：图片读取、自动找边、透视矫正、增强和 PDF 编码
- `start.bat`：Windows 环境初始化和启动脚本
