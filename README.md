# Cullumi

这是一个仅在本机运行的 Windows 照片筛选应用。它递归扫描照片目录，生成不裁切的缩略图，评估画质、寻找完全重复与相似连拍，并让用户最终决定保留或隔离。

当前版本：v1.0.2

## 使用

便携包中双击 `Cullumi.exe`。首次打开后：

1. 从文件夹导入。
2. 等待“发现照片、解码分析、重复确认、相似分组、眨眼检测”完成；扫描可取消，重新开始会增量复用未变化照片。
3. 在“照片库”中组合决定状态与 AI 分析筛选，或从“AI 建议、待决定、已保留、已移除”快捷入口进入对应结果；“AI 建议”中的“一键移除”只会批量标记 AI 明确建议移除且尚未决定的照片，不会立即移动文件；相似连拍按组整理。
4. 点击照片可放大查看，并使用 `W/↑` 保留、`S/↓` 移除、`A/←` 上一张、`D/→` 下一张。
   动态照片会显示 `LIVE` 标识；放大后可播放、拖动时间轴、将当前画面设为封面，或使用技术质量评分推荐封面。
5. 点击“隔离已标记移除”，检查完整清单并确认。文件会进入原照片目录下的 `_照片筛选隔离`，可从隔离历史恢复。

应用只监听 `127.0.0.1`，接口使用每次启动随机生成的会话令牌。没有 WebView2 时会退回默认浏览器。

## 格式与限制

- 常见图片由 Pillow 解码；HEIC/HEIF 使用 pillow-heif；DNG、CR2/CR3、NEF、ARW、RAF、ORF、RW2、PEF 使用 rawpy/LibRaw。
- iPhone Live Photo 支持同目录、同文件名的 HEIC/JPEG 与 MOV 配对；带标准 Motion Photo XMP 的 Android JPEG 可读取其内嵌视频。未配对的普通视频仍不会加入图库。
- 动态部分会在首次播放时转换为浏览器兼容、保留声音的 WebM 缓存。设置中可选择“不修改原图”“每次修改前提醒”或“始终修改原图”；修改原图支持 JPEG、HEIC 与 HEIF 动态照片，会先将原图片备份到项目缓存的 `source-backups` 目录，再以经过解码和动态结构校验的临时文件原子替换原图。配对或内嵌的视频内容不会重新编码。
- 将动态帧设为原图封面后，静态图片分辨率会以动态视频帧为准，可能低于相机拍摄的原始静态照片；“每次修改前提醒”模式会在操作前明确提示这一点。
- 动态照片被隔离或恢复时，照片与配对 MOV 会作为同一个逻辑对象一起处理。
- RAW、HEIC/HEIF 与 TIFF 的放大预览会在首次打开时按需生成最长边不超过 2560 像素的高质量 JPEG 缓存，原文件不会被改写。
- 项目数据库仅记录可迁移的相对缩略图路径；旧版本留下的绝对路径会在读取或迁移时自动兼容。
- 相似照片重建会先使用分块向量化哈希筛选候选，再进行结构比较；筛选条件与原有逐对比较保持一致。
- 相似连拍会在 512 像素缩略图上使用 YuNet 与 OCEC 检测眨眼，只调整组内推荐顺序，不会自动修改照片决定；设置中可关闭该功能，自定义模式可调整检测阈值。
- 百分位阈值第一版应用于清晰度；其余指标使用明确的绝对阈值。
- 自动分析仅给建议。任何文件移动都要求用户确认。
- 如果本机配置文件损坏，应用会先保留一份 `config.damaged-*.json` 备份，再尽量恢复可用设置并在启动时提示。
- 首次升级旧项目数据库时，会在项目缓存中生成 `project.pre-v1-*.db` 完整备份，再建立新索引和记录数据库版本。
- 迁移当前项目存储位置时，数据库通过 SQLite 一致快照复制，并在文件大小和数据库完整性校验通过后才切换到新位置；旧缓存仍需用户确认后清理。

## 开发与测试

需要 Python 3.12 及以上。项目以 `requirements.txt`、`requirements-dev.txt`
和 `requirements-build.txt` 作为运行、开发检查与构建依赖的唯一来源。

主要代码按职责拆分：

- `cullumi/config.py`：应用配置、模式定义和参数校验。
- `cullumi/classification.py`：照片筛选条件、项目统计和画质分类。
- `cullumi/scanner.py`：扫描、增量分析、重复确认和相似关系重建。
- `cullumi/core.py`：旧版 Python 导入入口的兼容导出，不再承载具体实现。
- `cullumi/project_store.py`：项目模型、SQLite 连接与迁移、缓存路径和写入一致性。
- `cullumi/media.py`：图片解码、缩略图、高清预览、图像指标与感知哈希。
- `cullumi/face_analysis.py`：缩略图人脸定位、眼部分类、多人聚合和模型缓存指纹。
- `cullumi/motion.py`：动态照片探测、视频缓存、帧提取与原图封面安全回写。
- `cullumi/fs_utils.py`：跨模块共享的路径边界判断与原子 JSON 写入。
- `cullumi/settings_service.py`：设置保存、模式应用与无副作用预估事务。
- `cullumi/workflows.py`：决定导入导出、批量标记、隔离与恢复。
- `cullumi/similarity.py`：相似候选索引、结构比较和分组算法。
- `web/js/runtime.js`：共享状态、接口请求、提示与主题。
- `web/js/session.js`：最近项目、项目打开和扫描进度。
- `web/js/similar.js`：相似照片列表与分组浏览。
- `web/js/settings.js`：模式、设置、确认框和更新提示。
- `web/js/gallery.js`：图库分页、照片卡片、查看器和决定状态同步。
- `web/js/app.js`：全局事件与应用启动入口；功能事件由对应脚本自行绑定。
- `web/css/`：基础、工作区、预览器、设置、主题、响应式和首页样式。
- `web/assets/`：按图片、字体和图标分类的静态资源。

前端脚本是无需构建工具的经典脚本，并按 `runtime`、`session`、`similar`、`settings`、`gallery`、`app` 的顺序加载。样式按 `base`、`workspace`、`viewer`、`settings`、`theme`、`responsive`、`home` 的顺序加载。新增跨模块功能时，应把实现放入对应职责文件，并保持依赖顺序。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe app.py
```

开发检查额外安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\ruff.exe check .
```

真实 DOM 界面测试需要 Node.js 20 及以上，并使用本机 Microsoft Edge，在隔离的临时配置与模拟接口数据下加载正式页面、样式和全部前端脚本：

```powershell
npm ci
npm run test:dom
```

测试覆盖首页与最近项目异步渲染、项目和照片决定交互、设置窗口中的错误提示层级，以及使用中自定义模式的删除警告。失败时的截图、追踪文件和测试报告会写入 `test-results\` 与 `playwright-report\`，这些目录不会进入版本控制。

构建便携目录：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

输出位于 `dist\Cullumi-v1.0.2\`，并生成可上传到 GitHub Release 的 `dist\Cullumi-v1.0.2-Windows-Portable.zip`。
