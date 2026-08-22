# Cullumi

Cullumi 是一款仅在本机运行的 Windows 照片筛选应用。它会递归扫描照片目录，生成不裁切的缩略图，检查画质，寻找完全重复照片和相似连拍，最后由用户决定保留或隔离哪些照片。

当前版本：v1.0.2

## 使用

便携包中双击 `Cullumi.exe`。首次打开后：

1. 点击“从文件夹导入”并选择照片目录。
2. 等待照片发现、解码分析、重复确认、相似分组和眨眼检测完成。扫描可以取消，再次扫描时会复用未变化照片的结果。
3. 在“照片库”中组合决定状态与分析结果筛选，也可以从“智能建议、待决定、已保留、已移除”进入对应照片。“智能建议”中的“一键移除”只会标记尚未决定且明确建议移除的照片，不会立即移动文件。相似连拍会按组展示。
4. 点击照片可以放大查看。使用 `W/↑` 保留、`S/↓` 移除、`A/←` 查看上一张、`D/→` 查看下一张。动态照片会显示 `LIVE` 标识，放大后可以播放、拖动时间轴、选择封面，并根据新封面重新分析照片质量。
5. 点击“隔离已标记移除”并检查完整清单。确认后，相关文件会移入原照片目录下的 `_照片筛选隔离`，之后仍可从隔离历史恢复。

Cullumi 只监听 `127.0.0.1`，接口使用每次启动随机生成的会话令牌。没有 WebView2 时，Cullumi 会改用默认浏览器打开界面。

## 支持格式与使用限制

### 图片格式

- 常见图片支持 JPG、JPEG、PNG、WebP、TIFF、TIF 和 BMP。
- HEIC、HEIF、HEICS、HEIFS 与 HIF 由 pillow-heif 解码。
- RAW 支持 DNG、CR2、CR3、NEF、ARW、RAF、ORF、RW2 与 PEF，由 rawpy 和 LibRaw 解码。
- RAW、HEIC、HEIF 与 TIFF 首次放大时会生成最长边不超过 2560 像素的 JPEG 预览缓存，原文件不会因此改写。
- 普通视频不会单独加入照片库，只会在组成受支持的动态照片时使用。

### 动态照片

- iPhone Live Photo 支持同一目录中同名的 HEIC、HEIF 或 JPEG 与 MOV 配对。
- Android Motion Photo 支持带标准 Motion Photo XMP 的 JPEG 内嵌视频。
- 动态部分首次播放时会生成保留声音的 WebM 缓存，因此项目缓存会占用额外空间。
- 照片与配对 MOV 会作为同一项处理，隔离和恢复时不会拆开。
- 设置中可以选择不修改原图、每次修改前提醒或始终修改原图。原图封面修改支持 JPEG、HEIC 与 HEIF 动态照片，修改前会在项目缓存的 `source-backups` 目录保留备份。配对或内嵌的视频内容不会重新编码。
- 动态视频帧的分辨率可能低于原始静态照片。将视频帧写入原图后，静态图片会采用该帧的分辨率。

### 分析范围

- 自动分析会检查清晰度、曝光、对比度等技术指标，不会判断构图、表情偏好或照片的纪念价值。所有决定仍由用户确认。
- 眨眼检测只处理非完全重复的相似连拍候选，只会调整组内推荐顺序，不会自动标记照片为移除。
- 小脸、侧脸、遮挡和低光照片可能无法可靠判断。只有非推荐照片中可靠检测到闭眼时才会显示“眨眼”，其余情况不显示状态。
- 关闭后重新启用眨眼检测不会自动开始扫描。现有结果失效时，设置页会显示“需要重新扫描”。

## 文件与缓存

- 项目数据库、缩略图、高清预览和动态视频缓存保存在项目缓存目录。需要保留决定和隔离历史时，不要直接删除该目录。
- 更换项目存储位置时，Cullumi 会先复制并校验数据库，成功后才切换到新位置。旧缓存由用户确认后清理。
- 配置或旧项目数据库需要修复和升级时，Cullumi 会先保留备份。发生恢复时，启动界面会给出提示。
- 隔离操作始终需要确认。隔离文件保存在原照片目录中，不会放入项目缓存。

## 开发与测试

项目需要 Python 3.12 及以上版本。`requirements.txt`、`requirements-dev.txt` 和 `requirements-build.txt` 分别提供运行、开发检查和便携构建依赖。

主要代码按下面的职责拆分。

- `cullumi/config.py` 负责Cullumi 配置、模式定义和参数校验。
- `cullumi/classification.py` 负责照片筛选条件、项目统计和画质分类。
- `cullumi/scanner.py` 协调照片发现、增量分析、完全重复确认、相似关系与眨眼分析。
- `cullumi/analysis_refresh.py` 根据配置或单张照片的变化决定需要刷新的分析阶段。
- `cullumi/project_store.py` 负责项目模型、SQLite 连接与迁移、缓存路径和写入一致性。
- `cullumi/media.py` 负责图片解码、缩略图、高清预览、图像指标与感知哈希。
- `cullumi/face_analysis.py` 负责人脸定位、眼部分类、多人聚合和模型缓存指纹。
- `cullumi/motion.py` 负责动态照片探测、视频缓存、帧提取和原图封面底层处理。
- `cullumi/motion_cover_service.py` 负责动态封面更新及相关数据库事务。
- `cullumi/photo_query_service.py` 负责照片列表、相似组查询和接口数据整理。
- `cullumi/settings_service.py` 负责设置保存、模式应用和分析刷新事务。
- `cullumi/workflows.py` 负责决定导入导出、批量标记、隔离与恢复。
- `cullumi/similarity.py` 负责相似候选索引、结构比较、分组和推荐排序。
- `cullumi/fs_utils.py` 提供路径边界判断与原子 JSON 写入。
- `cullumi/core.py` 保留旧版 Python 导入入口的兼容导出。
- `web/js/runtime.js` 提供共享状态、接口请求、提示与主题功能。
- `web/js/session.js` 负责最近项目、项目打开和扫描进度。
- `web/js/similar.js` 负责相似照片列表与分组浏览。
- `web/js/settings.js` 负责模式、设置、确认框和更新提示。
- `web/js/gallery.js` 负责图库分页、筛选、照片卡片和决定同步。
- `web/js/viewer.js` 负责图片预览、缩放和 Live Photo 控制。
- `web/js/app.js` 负责统一初始化和全局快捷键。

前端使用无需构建工具的经典脚本。脚本依次加载 `runtime`、`session`、`similar`、`settings`、`gallery`、`viewer` 和 `app`。样式依次加载 `base`、`workspace`、`viewer`、`settings`、`theme`、`responsive` 和 `home`。

创建运行环境并启动Cullumi 。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

运行完整开发检查前，还需要安装 Node.js 20 及以上版本和 Microsoft Edge。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
npm ci
.\verify.ps1
```

`verify.ps1` 会依次运行 Ruff、Python 测试、眨眼模型校验和 Edge Playwright。浏览器测试使用隔离的临时配置和模拟接口数据，失败产物会写入 `test-results\` 与 `playwright-report\`。

不少于 300 张、60 组授权连拍的真实眨眼评估流程见 `evaluation/README.md`。评估工具会输出逐人脸预测、精确率、召回率、组推荐成功率以及 P50 和 P95 性能报告。

安装构建依赖并生成便携版。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\build.ps1
```

构建结果位于 `dist\Cullumi-v1.0.2\`，同时生成 `dist\Cullumi-v1.0.2-Windows-Portable.zip`。
