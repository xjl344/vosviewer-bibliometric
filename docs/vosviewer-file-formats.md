# VOSviewer 文件格式规范

来源：VOSviewer 官方手册 v1.6.21 第 4 章 + 官方示例文件实测。
`references/` 目录下的 `journal_map.txt`、`journal_network_sparse.txt`、`thesaurus_terms.txt` 等
是官方原版示例，可直接对照。

## 通用规则

- **编码**：UTF-8 with BOM（文件开头有 `\ufeff`）
- **分隔符**：制表符（也接受逗号、分号）
- **首行**：表头，声明每列对应的属性
- **字段内含分隔符时**：整个字段用双引号包裹

---

## 1. map 文件（节点表）

每个非首行对应一个节点（关键词/作者/期刊等）。

### 可用列

| 列名 | 含义 | 约束 |
|---|---|---|
| `id` | 节点编号 | 与 network 文件联用时必须提供 |
| `label` | 显示标签 | 与 id 至少提供其一 |
| `sublabel` | 副标签（小字显示在标签下方） | 有 sublabel 必须有 label |
| `description` | 悬停时在信息面板显示的说明，支持 HTML | — |
| `url` | 点击节点打开的网页 | — |
| `x` | 横坐标 | 必须与 y 同时出现 |
| `y` | 纵坐标 | 必须与 x 同时出现 |
| `cluster` | 所属聚类编号 | 整数 1–1000 |
| `weight` | 权重（决定节点显示大小） | 非负 |
| `normalized weight` | 归一化权重 | 非负，一般不用 |
| `score` | 得分（overlay 视图按此上色） | — |
| `red` / `green` / `blue` | 用户自定义颜色分量 | 整数 0–255，三者必须同时出现 |

### 关键约束

- `weight` 与 `normalized weight` 不能同时用
- `score` 与 `red/green/blue` 不能同时用
- 可含**多个** weight 列与多个 score 列，用尖括号区分：

```
id	label	x	y	cluster	weight<Occurrences>	weight<Total link strength>	score<Avg. pub. year>	score<Avg. citations>
1	smart home	0.0525	-0.057	4	107	458	2020.8	15.5
```

**这是本工具的关键技巧**：同时写入 `score<Avg. pub. year>` 与 `score<Avg. citations>`，
在 VOSviewer 里切到 Overlay 视图就能直接选「平均发表年」做时间趋势分析。

---

## 2. network 文件（连线表）

### 稀疏格式（推荐）

每行一条连线：`id1 <TAB> id2 <TAB> 强度`

```
1	2	1
1	5	41
1	7	77
```

- 强度省略时默认为 1
- 同一对节点出现多行时，VOSviewer 会**累加**强度
- 本工具写入的是**原始共现次数**，归一化（association strength）交给 VOSviewer 内部处理

### 全格式

n 个节点 → n 行、n+1 列（首列为节点 id，其余为邻接矩阵）。
网络必须无向；若矩阵不对称，VOSviewer 会取对角线两侧的平均值。

---

## 3. thesaurus 文件（同义词表）

两列：`label <TAB> replace by`

```
label	replace by
calero, c	calero-medina, c
medina, cc	calero-medina, c
copyright	
elsevier b v	
```

- 有替代词 → 合并
- **替代词留空 → 删除该词**（用于过滤无意义词）

这是人工介入成本最低的环节：只需列出要改的词，不必重排全部关键词。

---

## 4. 配色文件

### cluster_colors.txt

```
cluster	red	green	blue
1	225	131	47
2	162	152	223
```

### overlay_colors.txt / density_colors.txt

```
color value	red	green	blue
0	127	127	127
0.5	30	30	255
1	255	0	0
```

- `color value` 为 0–1 之间的数值
- VOSviewer 在相邻色标之间做线性插值
- 默认色标：0=灰、0.5=蓝、1=红

---

## 5. corpus 文件（语料）

每行一篇文档的纯文本。**必须英文**——VOSviewer 的 NLP 算法不支持其他语言。
通常用「标题 + 摘要」拼接。

配合 `-scores` 文件可给文档附加数值（如年份、被引）：

```
score<Avg. pub. year>	score<Avg. cit. impact>
2017	4
2016	4
```

---

## 6. 命令行参数（手册 5.1 节）

### 打开/创建图谱

| 参数 | 说明 |
|---|---|
| `-map <file>` | 指定 map 文件 |
| `-network <file>` | 指定 network 文件 |
| `-json <file>` | 指定 VOSviewer JSON 文件 |
| `-gml <file>` | 指定 GML 文件 |
| `-corpus <file>` | 指定语料文件（走文本挖掘路线） |
| `-thesaurus <file>` | 指定同义词表 |
| `-pajek_network` / `-pajek_partition` / `-pajek_vector` | Pajek 系列文件 |

### 计算控制

| 参数 | 说明 |
|---|---|
| `-run_layout` | 强制重算布局 |
| `-run_clustering` | 强制重算聚类 |
| `-skip_clustering` | 跳过聚类 |
| `-resolution <n>` | 聚类分辨率，越大聚类越多 |
| `-min_cluster_size <n>` | 最小聚类规模 |
| `-merge_small_clusters true/false` | 是否合并小聚类 |
| `-largest_component` | 只保留最大连通分量 |
| `-attraction <n>` / `-repulsion <n>` | 布局参数 |
| `-min_n_occurrences <n>` | 术语最低出现次数（配合 corpus） |
| `-n_terms <n>` | 保留术语数（配合 corpus） |
| `-counting_method 1/2` | 1=二进制计数，2=全计数 |

### 保存结果

| 参数 | 说明 |
|---|---|
| `-save_map <file>` | 回写 map 文件（含算好的 x/y/cluster） |
| `-save_network <file>` | 回写 network 文件 |
| `-save_json <file>` | 回写 JSON |
| `-save_gml` / `-save_pajek_network` / `-save_pajek_partition` / `-save_pajek_vector` | 其他格式 |

### 导出截图（★ 自动出图的关键）

| 参数 | 格式 |
|---|---|
| `-save_screenshot_png` | PNG 位图 |
| `-save_screenshot_pdf` | PDF 矢量 |
| `-save_screenshot_svg` | SVG 矢量 |
| `-save_screenshot_tiff` | TIFF 位图 |
| `-save_screenshot_jpg` | JPG |
| `-save_screenshot_bmp` | BMP |
| `-save_screenshot_eps` | EPS 矢量 |
| `-save_screenshot_emf` | EMF 矢量 |
| `-save_screenshot_gif` | GIF |
| `-save_screenshot_swf` | SWF |

### 视图与外观

| 参数 | 说明 |
|---|---|
| `-network_visualization` | 网络视图 |
| `-overlay_visualization` | 叠加视图（按 score 上色） |
| `-density_visualization` | 密度视图 |
| `-density 1/2` | 1=元素密度，2=聚类密度 |
| `-white_background true/false` | 白底 |
| `-black_background true/false` | 黑底 |
| `-zoom_level <n>` | 缩放级别（≥1） |
| `-scale <n>` | 缩放滑块初值 |
| `-min_line_strength <n>` | 连线最小强度（过滤弱连线） |
| `-max_n_lines <n>` | 最多显示连线数 |
| `-label_size_variation <n>` | 标签大小差异 |
| `-line_size_variation <n>` | 连线粗细差异 |
| `-curved_lines true/false` | 曲线连线 |
| `-colored_lines true/false` | 彩色连线 |
| `-circles_frames 1/2` | 1=圆圈，2=边框 |
| `-max_label_length <n>` | 标签最大长度 |
| `-cluster_colors <file>` | 聚类配色文件 |
| `-overlay_colors <file>` | 叠加配色文件 |
| `-density_colors <file>` | 密度配色文件 |
| `-min_score` / `-max_score` | 叠加视图色标范围 |
| `-scores_normalization 1-4` | 1=不归一，2=除以均值，3=减均值，4=减均值除标准差 |
| `-show_item <id>` | 启动时聚焦某节点 |

### 其他

| 参数 | 说明 |
|---|---|
| `-encoding <enc>` | 文本文件编码（如 UTF-8） |
| `-file_location <dir>` | 默认文件位置 |

### 内存（JVM 参数，写在 -jar 之前）

```bash
java -Xmx4000m -jar VOSviewer.jar     # 堆内存 4000 MB
java -Xss1000k -jar VOSviewer.jar     # 栈大小 1000 KB
```

---

## 7. 互斥与依赖关系

- `-map` 与 `-pajek_network` 不能同时用
- `-map` 与 `-corpus` 不能同时用
- `-pajek_partition` 只能与 `-pajek_network` 联用
- `-counting_method` 只能与 `-corpus` 联用

---

## 8. 实测要点

**VOSviewer 是 GUI 程序，出图后不会自动退出。**

正确流程是「启动 → 轮询等待目标文件出现 → 关闭进程」。本工具的 `voslib/render.py`
已封装该逻辑（`run_vosviewer` 函数）。

**输出分辨率**：实测截图约 **3213 × 1714 像素**，满足期刊出版要求。
需要无损缩放时用 `-save_screenshot_svg` 或 `_pdf`。

**执行耗时**：200 篇文献的规模下，布局+聚类约 50 秒，每张截图约 50 秒。

**中文字体**：VOSviewer 用系统字体渲染中文，中文环境下一般正常。
若出现方框，改用 SVG 输出后在矢量软件里替换字体。

**在线分享**：也可用 URL 参数方式让 VOSviewer Online 打开图谱：

```
https://www.vosviewer.com/vosviewer.php?map=<map文件URL>&network=<network文件URL>&density_visualization&zoom_level=2.5
```
