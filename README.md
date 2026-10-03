# 图片文档扫描工具

一个使用 PySide6 的 Windows 桌面图片扫描工具。支持手动或自动选择页面四角、透视矫正、纸张背景增强，以及导出多页 PDF。图片处理在本地完成，不需要 LLM API。

## 功能

- 导入 JPG、PNG、WebP、TIFF 和 BMP 图片，可一次处理多页
- 自动修正照片的 EXIF 方向
- 手动选择页面四角，或尝试自动检测页面边缘
- 校正透视并提供纯白、保真彩色、灰度和黑白输出模式
- 导出 150、200 或 300 DPI 的多页 PDF，每个导入的图片文件对应一页；保存时会自动处理尚未处理的页面

## 快速启动（Windows）

双击 `start.bat`。脚本会在首次运行时创建 `.venv` 并安装依赖，然后打开桌面窗口。

也可以在 PowerShell 中手动启动：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

需要 Python 3.10 或更新版本。

## 项目结构

- `app.py`：PySide6 桌面界面和处理流程
- `scanner.py`：图片读取、自动找边、透视矫正、增强和 PDF 编码
- `start.bat`：Windows 环境初始化和启动脚本
