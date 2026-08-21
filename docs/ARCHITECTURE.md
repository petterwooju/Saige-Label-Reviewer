# v0.0.1 架构与安全边界

```text
浏览器 UI（原生 HTML/CSS/JS，Canvas 散点）
        │ loopback HTTP + 每次启动随机 token
AppState / ThreadingHTTPServer
        ├── adapters.py   只读解析、内容指纹、受限预览
        ├── analysis.py   ROI → DINOv2 → 混合评分 → UMAP|t-SNE → item_updates
        ├── session.py    人工复查状态 → 原子持久化 → undo/redo
        └── exporter.py   hash guard → backup → shadow → 严格复读 → commit/recovery
```

## 模块边界

- `domain.py`：统一 `Dataset` 和 `ReviewItem`，分开原始类别、人工会话类别与分析状态。
- `adapters.py`：只读解析 Folder / srproj / Saige JSON / visionproj，用内容而非仅元数据生成指纹；`.visionproj` 按需读取 ZIP 条目。
- `analysis.py`：完成 ROI 校验与裁剪、DINOv2 特征、分类器/近邻混合分数、UMAP/t-SNE、模型预检和版本化缓存。
- `session.py`：以 ID 索引执行更新；验证整批分析结果；持久化人工类别/状态与 undo/redo。
- `exporter.py`：按数据格式写出，建立唯一备份，默认生成新的修正版；覆盖使用 shadow 与可恢复提交。
- `server.py`：仅绑定 loopback，验证 Host/token/CSP/请求大小，协调会话与后台分析。指定端口被占用时回退到系统分配端口。
- `static/`：数据源、分析、队列、散点、详情与导出 UI；没有直接文件系统写权。

## 分析状态与并发契约

真实来源载入后，每个项的 `analysis_state` 是 `not_analyzed`，`suspicion_score` 与 `suggested_label` 为空。适配器只提供用于待分析布局的坐标，不伪造模型结果。演示数据使用独立的 `demo` 状态。

分析 worker 在数据集快照上计算，不直接修改共享 `Dataset`。它返回包含 ID、坐标、分数、建议类别、置信度、邻域支持与 `analysis_state="analyzed"` 的 `item_updates`。服务端确认：

1. 当前会话仍是启动分析时的同一代；
2. 更新集合和 ID 集合完整一致；
3. 数值有限且在合法范围，类别存在。

只有全部验证通过才在 `AppState` 锁内一次性写回；任一项失败则整批不生效。分析期间禁用开源、复查和导出写操作，避免与 worker 竞争。

`GET /api/job` 只返回任务状态、进度、消息、结果摘要或错误，不包含整个 items 数组。前端仅在任务完成后再请求 `/api/session`。

对 srproj/Saige JSON，分析更新还带每个唯一预览图像的私有 SHA-256（不会进入浏览器 payload 或会话文件）。预览请求发现内容与分析快照不同时，会在锁内清空当前会话全部分析分数、建议、投影坐标、支持度和旧 job provenance，再以一次性 409 诊断通知前端刷新会话并持久提示重新分析；人工类别、状态和撤销历史不受影响。

### 规模与内存预检

分析在加载模型前使用样本数 `N` 和实际出现的类别数 `C` 做两个独立的保护性估算：

- DINOv2-base 预期 float32 特征矩阵是 `N × 768 × 4` 字节，上限 512 MiB；拿到模型的实际输出维度后会在分配整个矩阵前再检查一次。
- 交叉验证概率与近邻工作矩阵按 `N × C × 16` 字节保守估算，上限也是 512 MiB。类别 ID 会先紧凑编码，不会因稀疏的大 ID 虚构巨大矩阵。

`t-SNE` 最多接受 50,000 个复查项；超限会在特征提取前拒绝，要求改用 UMAP 或拆分项目。少于 4 项时投影实际退化为 SVD。这些 512 MiB 限额是单个主要矩阵的拒绝门槛，不是进程总 RAM、scikit-learn 临时内存或 GPU 显存的可用量检测/承诺。

## 疑似错标分数

特征先 L2 归一化。数据条件允许时使用分层交叉验证逻辑回归，否则退化为类别中心相似度；再与余弦近邻的标签支持度按 70%/30% 混合。当前标签的混合支持越低，分数越高。该数值是排序信号，未经真实错误事件概率校准。

## 模型与缓存身份

默认 DINOv2 是 `facebook/dinov2-base`，必须提供完整的 40 位 revision；v0.0.1 默认固定为 `f9e44c814b77203eaa57a6bdbbd535f21ede1415`。对 Hugging Face 模型，分析身份不只是 repo/revision：它会在该 revision 的实际缓存中定位 `config.json` 和 `model.safetensors`，对两者记录名称、大小和 SHA-256；两个文件不齐则拒绝分析。对显式本地模型目录，身份覆盖 `config.json`、所有 `*.safetensors` 和权重索引文件的大小/SHA-256。权重加载后会再算一次模型身份，阻断加载窗口内的变更。

缓存键包含：

- 应用版本、分析算法版本和缓存 schema；
- 源数据内容指纹，以及 srproj/Saige JSON 引用的每个唯一图像内容指纹；
- item ID、规范化路径与原始标签；
- 模型身份、关键依赖版本、实际 CPU/CUDA 与 float32/float16；
- 影响语义的分析配置与有效输入尺寸。

`batch_size`、用户选择的 `device=auto` 文本与 `model_access` 不直接作为语义键；键中记录的是最终设备、精度和有效尺寸。缓存复用前、特征提取后与缓存写入后都重验源和引用图像内容。

## 会话持久化

`workspace/sessions` 只保存人工复查状态：已复查 item 的会话类别与状态、undo/redo 栈、源路径、源内容指纹和布局指纹。坐标、模型建议、分数、置信度和邻域支持不进入会话文件，它们由版本化分析缓存管理。因此重启后界面先显示“尚未分析”，需再点“运行分析”；身份匹配时不会重做模型推理。

当前自动会话文件名为 `v2-<identity>.json`，其中 `identity = SHA-256(normcase(resolved_source_path) + NUL + source_hash)`，因此两个路径不同但内容相同的项目不共用同一状态文件。文件内的 `saige-review-state/v2` 还必须通过精确 `source`、`source_hash` 和 `layout_hash` 校验；布局指纹覆盖 source type、类别、item ID/路径/原始类别与非分析元数据。

为了安全迁移旧的 `<source_hash>.json` 文件，服务端只在该文件本身已是 `saige-review-state/v2` 且上述路径/内容/布局全部匹配时导入，然后原子保存到新的路径+内容隔离文件。旧文件不会被删除，因为另一个同内容源仍可能需要独立检查并拒绝它。

会话保存使用同目录临时文件和 `os.replace`。恢复时还会校验 item ID、类别、状态、历史长度与历史链；损坏的历史不会带入运行时。

## 严格输入约束

- Saige JSON 和 visionproj 中候选 manifest 通过统一严格解析器读取。它拒绝非标准 `NaN`/`Infinity` 常量、解析后为非有限 float 的溢出数字、任意层级的重复对象键和过深嵌套。标注坐标/尺寸即使是字符串，也必须转换为有限数，边界加法不得溢出。
- srproj 在去除规范的数字前缀后若出现重复类别显示名，则无法安全映射类别索引并拒绝载入。Saige 项目中的同名类别如果 `classId` 或 `classColor` 不一致，也拒绝载入。
- 文件夹数据集必须用第一级子目录表示类别。根目录中的图像没有唯一标签语义，因此拒绝整个数据集。数据集根内任何层级条目如果是符号链接、Windows 目录联接或非普通文件/目录类型也会拒绝，防止指纹和导出穿越数据集边界。
- visionproj 载入会拒绝重复 ZIP 条目名和携带数据的目录条目，并只接受大小受限、可以严格解析且包含 `project.projectFiles` 的 manifest。若归档中存在多个这样的有效候选，则拒绝整个归档，不按路径顺序猜测主 manifest。
- 普通 JSON/srproj 在同一有界文件快照上解析并计算哈希，解析后再次核对磁盘内容；visionproj 在同一打开句柄上做前后哈希，文件夹在枚举前后做完整内容指纹。普通项目文件上限 128 MiB，单 manifest 16 MiB、候选 manifest 累计 64 MiB，另有类别、item、归档条目和候选数量上限，防止在分析规模预检前耗尽内存/CPU。

## 图像读取边界

普通文件源默认只授权数据源目录；路径解析后必须位于已授权根下，并且是受支持的图像扩展名。信任的适配器或集成可将绝对根目录显式加入 `Dataset.metadata.allowed_preview_roots`。未授权的绝对路径和 `..` 越界路径会被拒绝；项目文件中的字符串本身不是授权。

`.visionproj` 预览只按解析时确认的 ZIP 条目读取，并限制类型与解压后大小。

## 安装标记与自检

setup 使用项目级独占锁，并在开始时删除旧安装标记。未使用 `-SkipModelDownload` 时，预取固定 revision 后会在 Hugging Face cache 中定位 `config.json` 与 `model.safetensors`，把两者的大小/SHA-256 和模型 revision 一起写入 `.venv/.saige-reviewer-setup.json`。标记还包含应用版本、`pyproject.toml` SHA-256、是否跳过模型下载与 CPU/CUDA 13.0 PyTorch 变体。

标记写入后，setup 立即运行 `run.py --check-setup`。该检查核对标记/应用/清单，从 `pyproject.toml` 确认每个分析直接依赖都是精确 pin 且已安装同版本，实际导入 NumPy/Pillow/scikit-learn/Torch/transformers/UMAP，检查 PyTorch 变体与 CUDA GPU 可用性，并在未跳过下载时重验两个 HF cache 文件的大小/SHA-256。任一步失败都删除标记并使 setup 失败；启动器后续也使用同一自检决定是否需要修复环境。`-SkipModelDownload` 仅跳过模型文件完整性部分，不跳过依赖、导入或 PyTorch 变体检查。

## 安全写出协议

1. 载入时保存内容 SHA-256；文件夹指纹覆盖类别目录和每个图像的路径/内容。
2. 写出前重算源指纹；若与载入时不同，立即停止。
3. 创建完整、唯一命名的备份，并验证其完整指纹。
4. 在与最终目标同卷的唯一 shadow 上应用更改；目录源从不就地移动图像。
5. 对 shadow 做严格复读：项数、ID/路径、最终类别、图像内容与非预期文件必须符合期望。
6. srproj 通过 Expat 定位稳定 image/label 序号对应的原始数字字节，只拼接一次修改后的切片，不重序列化 XML；DOCTYPE、根外/根内注释、处理指令、未知节点和混合内容保持原样。visionproj 保持 ZIP 条目清单/顺序/目录标记、归档注释和关键 `ZipInfo` 元数据；对 manifest 以外的每个条目比对解压后大小与内容 SHA-256，证明逻辑负载逐字节一致。
7. 再次检查源指纹，然后先将原源位移到唯一 `recovery_copy`，再将已验证 shadow 放到源路径。
8. 位移后对被位移对象再做指纹检查，用于捕获位移窗口中外部句柄的迟到写入。
9. 若提交失败且源路径仍空缺，从 `recovery_copy` 恢复；若成功，则保留该副本并在结果中返回其路径。

默认目标是 `workspace/exports` 中的新修正版。覆盖源项目需使用独立 API 并提交精确确认短语 `OVERWRITE_SOURCE`。常规备份在 `workspace/backups`；`recovery_copy` 是覆盖提交时被位移的原对象，两者用途不同，均不应在确认产物前删除。

本协议防护意外并发写入、路径冲突、链接/目录联接和故障注入，不把“同一 Windows 账户下另一个恶意进程可持续监视随机目标名，并在独占创建后的指令级时间窗替换目录为联接”纳入安全保证。拥有该账户文件系统写权限的恶意进程也能直接改写应用、源项目或备份；运行导出时应避免同时运行不受信任的同账户程序。
