# 迁移指南

## 从早期原型升级到 v0.0.1

### 运行时与安装

v0.0.1 只支持 Python 3.12 和 3.13，推荐 3.12。旧 `.venv` 的 Python、项目版本、`pyproject.toml` 指纹或模型 revision 不符合安装标记时，启动器会要求修复环境。

推荐在可联网环境重跑：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

该命令默认预取约 350 MB 的 `facebook/dinov2-base` 固定 revision `f9e44c814b77203eaa57a6bdbbd535f21ede1415`。两个安装开关可组合使用：

```powershell
# 强制 CPU PyTorch
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -CpuOnly

# 跳过模型预取（依赖安装仍可能联网）
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -SkipModelDownload
```

v0.0.1 运行时默认不下载模型。对于使用 `-SkipModelDownload` 建立的环境，可稍后重跑默认 setup 预取，或在某次分析对话框中明确选择 `download_if_missing`。

安装标记的契约也已收紧。默认 setup 会在 Hugging Face cache 中定位固定 revision 的 `config.json` 和 `model.safetensors`，将两者的大小/SHA-256 连同应用版本、`pyproject.toml` SHA-256、revision 和 PyTorch 变体写入标记。写入后会立即执行 `run.py --check-setup`，完整核对精确依赖版本、关键模块导入、CPU/CUDA 13.0 变体与 GPU 可用性，并重验模型文件。失败时标记会被删除。因此旧 setup 标记不应手工复制或补字段；直接重跑 setup。`-SkipModelDownload` 只跳过两个模型文件的标记/完整性部分，其他自检仍执行。

### 载入状态

早期原型适配器生成的伪随机分数/建议已删除。v0.0.1 中真实项目初始状态始终是：

```text
analysis_state = not_analyzed
suspicion_score = null
suggested_label = null
```

上层集成不应再假设这两个字段在载入后是数字/字符串。排序、筛选和详情 UI 必须处理空值，只对 `analysis_state="analyzed"` 的项显示真实分析结果。演示数据使用 `analysis_state="demo"`。

### 外部图像路径

v0.0.1 默认只读取数据源目录内的图像。过去依赖任意绝对路径或 `..` 越界路径的 srproj/Saige JSON 需要迁移：

- 在“打开数据源”对话框的“额外授权的图像根目录”中填入必要根目录，多个 Windows 路径用分号分隔；或
- 自定义可信适配器将已获用户授权的绝对根写入 `Dataset.metadata.allowed_preview_roots`。

不要将项目文件中的路径字符串自动当作授权，也不要授权磁盘根目录。

### 严格输入迁移

过去能被宽松解析的歧义数据在 v0.0.1 中会显式失败。迁移时应在源的可恢复副本上清理，不应修改读取器来猜测：

- 用标准有限 JSON 数字替换 `NaN`/`Infinity` 常量、溢出数字与非有限坐标字符串；消除所有层级的重复对象键；
- 确保 srproj 去除数字前缀后的类别显示名唯一，并确保 Saige 同名类别的 `classId`/`classColor` 一致；
- 将文件夹根下的无类别图像移入明确的第一级类别子目录；
- 将数据集根目录内符号链接/Windows 目录联接指向的需要内容复制为普通文件，不要保留链接语义；
- 重建含重复归档路径、携带数据的目录条目或多个有效项目 manifest 的 visionproj，确保主 manifest 唯一。

### 分析 API 与并发

早期原型代码可能假设 `analyze(dataset, ...)` 会原地修改 `dataset.items`。v0.0.1 的契约是纯结果批次：

```json
{
  "effective_input_size": 224,
  "cache_hit": true,
  "item_updates": [
    {
      "id": "0",
      "x": 0.0,
      "y": 0.0,
      "suspicion_score": 42.0,
      "suggested_label": "ok",
      "label_confidence": 0.58,
      "neighbor_support": 0.61,
      "analysis_state": "analyzed"
    }
  ]
}
```

调用方必须把 `item_updates` 作为完整批次验证，并在会话锁内一次性应用。服务端会用会话代际阻止旧 worker 污染已替换的会话。对外的 job 结果不返回 `item_updates`。

高频 `GET /api/job` 现在只返回轻量 job 数据。自定义前端应在 job 进入 `completed` 后再请求 `/api/session`，不要从每次 job 响应中读全量 session。

自定义分析调用方还必须处理新的规模拒绝：特征矩阵按 `N×768×4` 字节估算并按实际特征维度重验，评分工作矩阵按 `N×C×16` 字节估算，两者各有 512 MiB 保护预算。`t-SNE` 超过 50,000 项会被拒绝；给用户提供 UMAP 或拆分项目的选择。不要把该预算显示为分析总 RAM/显存的承诺。

### 缓存迁移

v0.0.1 使用新的 cache schema 和分析算法版本，旧 `.npz` 不会命中，可留待人工确认后清理。新缓存身份包含：

- 应用、算法与 cache schema 版本；
- 源内容、原始标签，以及 srproj/Saige JSON 的外部引用图像内容；
- 模型 repo + revision，以及该 revision 当前 Hugging Face cache 中 `config.json`/`model.safetensors` 的大小和 SHA-256；本地模型则包含配置、所有 safetensors 权重/索引的大小和 SHA-256；
- 关键依赖版本、实际 CPU/CUDA、精度、有效尺寸与分析语义配置。

同一个外部图像被多个标注引用时只读取/哈希一次；缺失图像进入确定的缺失身份。分析进行中任意源或外部图像改动都会废弃本批，并删除本次刚写入的缓存。

### 会话迁移

v0.0.1 会话文件只持久化人工复查字段和 undo/redo 历史。旧会话中的 `x`、`y`、`suspicion_score`、`suggested_label`、`analysis_state`、`label_confidence` 和 `neighbor_support` 会被忽略，不会覆盖 v0.0.1 运行时分析结果。

重启后的预期行为是：人工修改已恢复，分析暂显示“尚未分析”。用户再点“运行分析”；新缓存身份匹配时直接命中缓存。

当前会话文件名是 `v2-SHA256(normcase(resolved_path) + NUL + source_hash).json`；文件内还验证精确 `source`、`source_hash` 和 `layout_hash`。这使同内容但不同路径的项目会话彻底隔离，也使同路径内容/布局变化后的旧状态无法误恢复。

旧版可能已生成以 `<source_hash>.json` 命名、但文件内部已是 `saige-review-state/v2` 的状态。服务端仅在完整 v2 恢复验证（精确路径+内容+布局、item、类别/状态、历史链）通过后导入，然后原子保存到新隔离路径。旧文件会保留，因为其他同内容源必须能独立检查并拒绝它。不要删除、手工重命名或编辑这类旧 v2 文件来强制迁移。

### 导出与恢复迁移

早期原型的目录覆盖可能直接在源目录中移动文件。v0.0.1 不再就地修改目录源：

1. 备份使用唯一命名，避免同秒导出冲突；
2. 文件和目录都在同卷唯一 shadow 上修改；
3. 目录复读严格比对项数、类别/路径、图像内容与非预期文件；
4. visionproj 保持归档条目清单/顺序/目录标记、归档注释和关键元数据；对 manifest 之外所有条目比对解压后大小与内容 SHA-256，验证逻辑负载逐字节一致；
5. 覆盖时先将原源位移到唯一 `recovery_copy`，重验后再将 shadow 放入源路径；
6. 成功结果保留并返回 `recovery_copy`；它与 `workspace/backups` 中的常规备份是两个独立恢复层。

自定义导出调用方应接受结果中新的 `recovery_copy` 字段，并不要在产物验收前删除它。

### 端口行为

`--port 8765` 仍是默认，但当该端口因“地址已使用”无法绑定时，v0.0.1 会回退到系统分配的空闲端口。启动脚本、监控或测试不应再假设最终端口始终是 8765，应读取打印的实际 URL 或 server address。

## 从原型迁移的设计取舍

### 已选择性迁移

- `dinov2_folder_cls_tsne.py`：一级类别扫描、方形补边、patch token 池化思路；
- `dinov2_srproj_seg_tsne.py`：srproj 类别/轮廓解析、反射填充 crop、内外轮廓 mask、中性灰背景、mask-pooled token；
- `dinov2_tsne_review.py`：detection JSON 读取、ROI 预览与轮廓叠加、方向键导航；
- `srproj_label_review_app.py`：逻辑回归/近邻混合分数、分析缓存思路、SHA-256、安全写出与完整备份。

### 调整后迁移

- 将即时写回改为人工会话暂存，最终批量导出；
- 分数统一命名为“疑似错标分数”，不将其描述为概率；
- 将单文件 Plotly/HTTP 原型拆分为适配器、分析、会话、导出、服务器和独立 UI；
- 将布尔 `mask_outside` 扩展为 `original` / `dimmed` / `neutral_outside`，并将尺寸、ROI 外扩和 mask pooling 纳入缓存语义。

### 未迁移

- 单文件 HTTP handler、base64 全量缩略图、pickle object cache；
- 对真实源项目的即时编辑与仅支持单步撤销的备份恢复；
- 对任意第三方 JSON 字段强耦合的默认写回；后续扩展应通过可注册适配器。
