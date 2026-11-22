# -*- coding: utf-8 -*-
"""kg 子命令提示词模板。"""

# 系统提示词：kg 智能体的核心指令
KG_SYSTEM_PROMPT = """你是 BookerCodeTool 的 kg（Knowledge Graph）子命令智能体。
你的任务是将任意代码库、文档、论文、图片、视频转换为可查询的知识图谱。

【核心能力】
- 文件检测与分类（代码/文档/论文/图片/视频）
- 结构化抽取（AST）与语义抽取（LLM）
- 图构建、社区检测、核心节点分析
- 自然语言查询、路径追踪、节点解释
- 增量更新、监控模式、多格式导出

【工作流阶段】
1. INIT      - 初始化，确认目标与授权
2. DETECT    - 检测目标路径下的文件
3. EXTRACT   - 抽取实体与关系（AST + 语义）
4. BUILD     - 构建图、聚类、分析、生成报告
5. QUERY     - 自然语言查询图谱
6. ADD       - 添加 URL 到语料库
7. WATCH     - 监控文件变化自动重建
8. EXPORT    - 导出多种格式（HTML/SVG/GraphML/Neo4j/Obsidian/Wiki/MCP）
9. DONE      - 完成，输出最终报告

【工具调用规则】
- 每轮必须调用至少一个工具，直到调用 tool_finish 结束
- 优先使用内置技能步骤工具（tool_kg_*），按工作流顺序推进
- 基础工具：tool_bash、tool_readfile、tool_writefile、tool_list_files、tool_print、tool_ask_user、tool_note、tool_load_state、tool_update_state
- 遇到不确定或高风险操作（如删除文件、推送数据库）必须用 tool_ask_user 确认
- 关键发现、决策记录用 tool_note 追加到 notes.md
- 状态变更用 tool_update_state 持久化

【输出格式】
- 最终报告以 JSON 形式通过 tool_finish 返回，字段见 KgFinalReport
- 中间过程工具返回结构化数据（SkillStepGuide/KgStepGuide）
- 所有输出优先用中文

【诚实原则】
- 绝不编造图谱内容，不确定时标记为 AMBIGUOUS
- 必须展示 token 成本、社区内聚分数原始数值
- 节点超 5000 时必须警告 HTML 可视化性能风险
- 图为空或抽取失败必须明确报错，不静默继续
"""

# 轮次提示词：每轮工具调用前的上下文构建
KG_ROUND_PROMPT = """{_JSON_OUT}

=== 当前任务状态 ===
{state_json}

=== 用户输入 ===
{user_input}

=== 上一轮工具结果 ===
{tool_results_json}

=== 指令 ===
请根据当前状态与用户输入，决定下一步动作。
- 若处于 INIT 阶段：确认目标、检查授权、初始化项目目录
- 若处于 DETECT 阶段：调用 tool_kg_detect 进行文件检测
- 若处于 EXTRACT 阶段：根据文件类型并行调用 tool_kg_extract_ast 与 tool_kg_extract_semantic
- 若处于 BUILD 阶段：调用 tool_kg_build_graph 构建图谱
- 若处于 QUERY 阶段：调用 tool_kg_query 回答问题
- 若处于 ADD 阶段：调用 tool_kg_add_url 添加语料
- 若处于 WATCH 阶段：调用 tool_kg_watch 启动监控
- 若处于 EXPORT 阶段：按标志调用对应导出工具
- 所有阶段完成后：调用 tool_finish 返回最终报告

注意：
- 目标为 GitHub URL 时先克隆（tool_kg_clone_github）
- 大语料（>200万字或>500文件）需询问用户是否聚焦子目录
- 视频文件需先转录（tool_kg_transcribe_video）
- 增量更新（--update）仅重抽取变更文件
- 状态机字段 next_action 用于指引下一步，完成时置空
"""

# JSON 输出格式指引
_JSON_OUT = """【输出格式要求】
- 调用工具时参数为 JSON，工具返回结构化对象
- 最终调用 tool_finish 时，参数必须为合法 JSON，字段对应 KgFinalReport
- 所有字符串字段务必转义，不要包含未闭合的引号
- 工具返回的对象可直接在后续轮次引用
"""

# ══════════════════════════════════════════════════════════════════
# 各步骤专用提示词（用于生成行动指南，带文件缓存）
# ══════════════════════════════════════════════════════════════════

# 初始化与目标确认
KG_INIT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 INIT 阶段。

目标：{target}
目标描述：{goal}
授权状态：{authorized}
项目目录：{pj_dir}

请生成初始化行动指南，包含：
1. 目标路径有效性检查（本地路径存在性、GitHub URL 格式）
2. 是否需要克隆 GitHub 仓库
3. graphify-out 目录是否已存在（决定是否走快速查询路径）
4. 必要的授权确认（如 --neo4j-push、--falkordb-push、--watch 涉及外部写入）
5. 下一步建议阶段（DETECT/QUERY/ADD 等）

输出 KgStepGuide 结构。"""

# 文件检测
KG_DETECT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 DETECT 阶段。

扫描根目录：{scan_root}
模式：{mode}
项目目录：{pj_dir}

请生成文件检测行动指南，调用 graphifyy CLI 的 detect 功能。
需处理：
1. 递归扫描目标目录，分类文件（code/document/paper/image/video）
2. 读取 .gitignore 与 graphify-out/.graphify_excludes 排除规则
3. 统计总文件数、总字数估算
4. 识别敏感文件并记录跳过列表
5. 将检测结果写入 graphify-out/.graphify_detect.json
6. 大语料预警：total_files>500 或 total_words>2,000,000 时，计算前 5 大子目录供用户选择

输出 KgStepGuide 结构，commands 包含具体的 graphifyy detect 调用命令。"""

# AST 结构化抽取（代码文件）
KG_EXTRACT_AST_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXTRACT-AST 阶段（结构化抽取）。

代码文件列表：{code_files}
扫描根目录：{scan_root}
项目目录：{pj_dir}
模式：{mode}

请生成 AST 抽取行动指南：
1. 使用 graphifyy 的 extract 模块对代码文件做 AST 抽取
2. 传递 root=scan_root 与 cache_root=scan_root 保证节点 ID 稳定
3. 产出写入 graphify-out/.graphify_ast.json
4. 无代码文件时写入空结果结构

输出 KgStepGuide 结构。"""

# 语义抽取（文档/论文/图片）
KG_EXTRACT_SEMANTIC_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXTRACT-SEMANTIC 阶段（语义抽取）。

待抽取文件：{semantic_files}
扫描根目录：{scan_root}
项目目录：{pj_dir}
模式：{mode}
Deep 模式：{deep_mode}
Gemini 可用：{has_gemini}

请生成语义抽取行动指南：
1. 检查缓存（graphify-out/.graphify_cached.json）
2. 未命中文件写入 graphify-out/.graphify_uncached.txt
3. 按 20-25 文件/~6000 行分块，同目录文件尽量同块
4. 每块派发子智能体并行抽取，产出 graphify-out/.graphify_chunk_NN.json
5. 收集合并，校验覆盖率，缓存新结果
6. 最终合并写入 graphify-out/.graphify_semantic.json

输出 KgStepGuide 结构。"""

# 合并 AST + 语义
KG_MERGE_EXTRACT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 MERGE-EXTRACT 阶段。

AST 结果：graphify-out/.graphify_ast.json
语义结果：graphify-out/.graphify_semantic.json
扫描根目录：{scan_root}

请生成合并行动指南：
1. 读取两侧结果，给语义节点/边打 _origin=semantic 标记
2. 按 ID 去重合并（AST 优先）
3. 产出写入 graphify-out/.graphify_extract.json
4. 统计合并后节点数、边数

输出 KgStepGuide 结构。"""

# 图构建、聚类、分析
KG_BUILD_GRAPH_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 BUILD 阶段。

抽取结果：graphify-out/.graphify_extract.json
检测结果：graphify-out/.graphify_detect.json
扫描根目录：{scan_root}
有向图：{directed}
强制重建：{force}
项目目录：{pj_dir}

请生成图构建行动指南：
1. 调用 build_from_json 构建 NetworkX 图（有向/无向）
2. 空图保护：节点数为 0 时报错退出
3. 社区检测（Leiden/回退 Louvain）
4. 核心节点、惊人连接、建议问题分析
5. 导出 graph.json（force 控制收缩保护）
6. 生成 GRAPH_REPORT.md、.graphify_analysis.json
7. 健康检查（诊断悬空/缺失/自环/折叠边）

输出 KgStepGuide 结构。"""

# 社区标注
KG_LABEL_COMMUNITIES_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 LABEL-COMMUNITIES 阶段。

分析结果：graphify-out/.graphify_analysis.json
抽取结果：graphify-out/.graphify_extract.json
检测结果：graphify-out/.graphify_detect.json
扫描根目录：{scan_root}
有向图：{directed}
项目目录：{pj_dir}

请生成社区标注行动指南：
1. 读取每个社区的节点标签，生成 2-5 词的中文社区名
2. 用真实标签重新生成建议问题
3. 重写 GRAPH_REPORT.md 与 graph.json（带 community_name）
4. 保存标签到 .graphify_labels.json

输出 KgStepGuide 结构，包含生成的 labels 字典。"""

# 导出 HTML/Obsidian/Wiki 等
KG_EXPORT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXPORT 阶段。

导出配置：{export_flags}
项目目录：{pj_dir}
graphify-out 目录：{graphs_dir}

请生成导出行动指南，按标志执行：
- HTML：graphify export html（>5000 节点自动聚合社区视图）
- Obsidian：graphify export obsidian [--dir PATH]
- Wiki：graphify export wiki
- Neo4j：graphify export neo4j [--push URI]
- FalkorDB：graphify export falkordb [--push URI]
- SVG/GraphML：graphify export svg|graphml
- MCP：graphify export mcp

输出 KgStepGuide 结构。"""

# 查询
KG_QUERY_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 QUERY 阶段。

查询问题：{question}
遍历模式：{dfs_mode}（BFS/DFS）
Token 预算：{budget}
graphify-out 目录：{graphs_dir}

请生成查询行动指南：
1. 对问题做词汇扩展（基于图谱词汇表）
2. 执行 BFS/DFS 遍历收集相关节点/边
3. 按预算截断，生成自然语言回答
4. 引用 source_location 作为证据

输出 KgStepGuide 结构。"""

# 路径查询
KG_PATH_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 PATH 查询阶段。

起点：{path_from}
终点：{path_to}
graphify-out 目录：{graphs_dir}

请生成最短路径查询行动指南：
1. 在图中查找两节点间最短路径
2. 返回路径节点序列与边关系
3. 解释路径跨越的社区边界

输出 KgStepGuide 结构。"""

# 节点解释
KG_EXPLAIN_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXPLAIN 阶段。

目标节点：{node_name}
graphify-out 目录：{graphs_dir}

请生成节点解释行动指南：
1. 读取节点完整信息（标签、属性、邻居、社区）
2. 生成通俗易懂的中文解释
3. 列出关键关联节点与关系

输出 KgStepGuide 结构。"""

# 添加 URL
KG_ADD_URL_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 ADD 阶段。

URL：{url}
作者：{author}
贡献者：{contributor}
项目目录：{pj_dir}

请生成添加 URL 行动指南：
1. 下载 URL 内容保存到 ./raw/
2. 记录作者/贡献者元数据
3. 更新语料库并触发增量重建（--update）

输出 KgStepGuide 结构。"""

# 视频转录
KG_TRANSCRIBE_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 TRANSCRIBE 阶段。

视频文件：{video_files}
Whisper 模型：{whisper_model}
项目目录：{pj_dir}

请生成视频转录行动指南：
1. 对每个视频/音频文件调用 whisper 转录
2. 将转录文本作为 document 类型参后续抽取
3. 产出保存到 graphify-out/transcripts/

输出 KgStepGuide 结构。"""

# 监控模式
KG_WATCH_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 WATCH 阶段。

监控目录：{watch_dir}
项目目录：{pj_dir}

请生成监控行动指南：
1. 启动 graphify watch 监控文件变化
2. 变化时自动增量重建（无需 LLM）
3. 记录监控日志

输出 KgStepGuide 结构。"""

# 最终报告重建（兜底）
KG_REBUILD_REPORT_PMT = """{_JSON_OUT}

根据当前状态与笔记重建最终报告。

状态：{state_json}
笔记：{notes}

请生成 KgFinalReport 结构的 JSON。"""

# 步骤指南输出格式
STEP_GUIDE_JSON = """
{
  "skill": "kg",
  "step": "<步骤名>",
  "title": "<步骤标题>",
  "commands": ["<命令1>", "<命令2>", ...],
  "payloads": ["<载荷1>", "<载荷2>", ...],
  "checks": ["<验证要点1>", "<验证要点2>", ...],
  "save_paths": ["<产出文件1>", "<产出文件2>", ...],
  "notes": "<备注>",
  "mode": "<运行模式>",
  "flags": ["<标志1>", "<标志2>", ...]
}
"""