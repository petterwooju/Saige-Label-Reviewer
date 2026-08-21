# Saige标记复查工作台 v0.0.1

本地优先的浏览器标注质量复查工具。图像、项目文件、特征、模型缓存和人工复查记录默认留在本机。“疑似错标分数”只用于安排复查顺序，不是真实错误概率。

## 启动与首次联网

需要 Windows 和 Python 3.12 或 3.13（推荐 3.12）。双击 `Start Saige Reviewer.bat` 会在项目内建立 `.venv`；首次安装会联网安装固定版本的依赖，并默认预取约 350 MB 的固定 DINOv2 权重。

也可显式执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
.\.venv\Scripts\python.exe run.py --open-browser
```

无 NVIDIA GPU 或要强制安装 CPU 版 PyTorch：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -CpuOnly
```

要在安装时禁止模型下载：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -SkipModelDownload
```

`-SkipModelDownload` 只跳过权重，依赖安装仍可能联网。应用运行时默认使用 `local_only`，不会隐式下载模型；只有在分析对话框中明确选择“缺失时允许下载”，才会尝试联网。默认模型是 `facebook/dinov2-base`，revision 固定为 `f9e44c814b77203eaa57a6bdbbd535f21ede1415`。

默认 setup 会把 Hugging Face 缓存中的 `config.json` 和 `model.safetensors` 的大小与 SHA-256 写入安装标记。标记落盘后会立即执行完整自检：核对应用/清单、所有精确依赖版本、关键模块可导入性、CPU/CUDA PyTorch 变体，以及未跳过模型下载时的两个模型文件。自检失败会删除标记，不会将半安装环境宣告为就绪。

## 支持格式

- 按第一级子文件夹划分类别的图像母文件夹；
- Classification / Detection / Segmentation `.srproj`；
- Saige IAD / OCR / Det / Rod / Seg JSON；
- 包含项目 JSON 与 images 的 `.visionproj`，按需读取预览，不整体解压。

## v0.0.1 行为要点

- 软件启动时保持空工作台，不再预填 dummy/演示数据。真实数据刚载入时是“尚未分析”：没有模型建议，也没有疑似错标分数，只有 DINOv2 分析成功后才显示两者。
- 分析包括原始/弱化/中性灰背景、224/336/518/自动尺寸、ROI 外扩、mask pooling、t-SNE/UMAP 和分类器/近邻混合排序。
- “自动”设备策略会优先使用 CUDA GPU；若显存、CUDA 或驱动运行时失败，会清理显存并从头改用 CPU。显式选择 CUDA 时不会自动改变设备，完成提示会显示实际使用的 GPU/CPU。
- 复查队列、特征分布和标注详情之间的分隔条可拖拽，也可聚焦后用方向键微调、按 `Home` 或双击恢复默认；宽度保存在当前浏览器中。
- 标注详情把当前图像与最多 3 张同类参考样本并排显示；人工确认和低疑似样本优先，并可切换到任意类别或模型建议类别进行对照。参考样本来自项目本身，不宣称为标准答案。
- 分析启动前会做规模预检：特征矩阵和评分工作矩阵各自使用 512 MiB 保护预算；`t-SNE` 最多 50,000 个复查项，超限需改用 UMAP 或拆分项目。该预算是拒绝明显过大输入的上限，不等于总 RAM/显存保证。
- 分析 worker 仅返回 `item_updates`，服务端验证完整批次后在锁内一次性写回；进度轮询只返回轻量任务状态，完成后再拉取整个会话。
- 会话文件只持久化人工复查的类别、状态与撤销/重做历史。重启后需再点“运行分析”，但版本身份全部匹配时会直接命中本地分析缓存。
- 自动会话文件名同时绑定规范化源路径和源内容指纹，文件内再校验精确源路径、内容指纹和布局指纹，避免不同路径下的同内容项目串用复查记录。
- 分析缓存身份包含源与引用图像的内容指纹、原始标签、应用/算法/缓存 schema 版本、模型 revision，以及实际 Hugging Face 缓存或本地目录中模型配置/权重文件的大小和 SHA-256；还包含关键依赖、实际设备/精度与有效输入尺寸。
- srproj/Saige JSON 的每个分析项还保留私有预览内容摘要；分析完成后若外部图像被替换，下一次预览会清空整批旧分数、建议和投影来源，并持久提示重新分析，不会把旧结果继续标成已分析。
- Saige JSON 与 visionproj manifest 使用严格 JSON：拒绝 `NaN`/`Infinity`、溢出为非有限值的数字和任意层级重复键；visionproj 中出现多个有效项目 manifest 时也会因无法唯一选择而被拒绝。载入还会拒绝无法唯一映射的同名类别、没有类别目录的根图像，以及文件夹数据集中的符号链接/目录联接。
- 预览默认只能读取数据源目录内的图像。项目引用外部图像根时，必须在打开数据源时显式授权（或在 CLI 中重复使用 `--image-root`）；授权会记入 `Dataset.metadata.allowed_preview_roots`，而项目文件里的绝对路径本身不等于授权。
- 固定端口被占用时，启动器会回退到系统分配的本地端口，并打印实际 URL。

## 安全导出

默认导出新的修正版，源数据不变。覆盖源数据前需输入 `OVERWRITE_SOURCE`，系统会建立唯一名备份、在 shadow 副本上修改并严格复读验证，再做最终源位移与替换。srproj 只在原始 XML 字节中替换目标类别整数，保留 DOCTYPE、根外注释/处理指令和其他词法内容；visionproj 修正版会对 manifest 之外的每个 ZIP 条目做大小与内容 SHA-256 逐字节验证，并比对条目顺序/类型、关键元数据和归档注释。覆盖成功后会保留被位移的 `recovery_copy`；任一外部变化或验证失败都会中止提交。

详细操作见 [客户使用指南](docs/USER_GUIDE.md)，安全与架构见 [ARCHITECTURE.md](docs/ARCHITECTURE.md)，验收协议见 [VALIDATION.md](docs/VALIDATION.md)。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests run.py
node --check src/saige_reviewer/static/app.js
```

CI 还使用 PowerShell AST parser 解析 `setup.ps1`。它只安装固定版本的 NumPy、Pillow、scikit-learn 和 SciPy 等轻量测试依赖，不安装 Torch、不下载模型，因此不代替真实 DINOv2 硬件验收。测试总数以当次自动发现的用例为准，文档不写死数量。

Cloudflare Named Tunnel 的隔离远程上传模式与部署顺序见 [docs/REMOTE_DEPLOYMENT.md](docs/REMOTE_DEPLOYMENT.md)。远程模式只接收自包含 `.visionproj`，不会暴露本机路径和覆盖写回接口。
