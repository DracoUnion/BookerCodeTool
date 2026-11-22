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
- 若处于 INIT 阶段：调用 tool_kg_init 获取行动指南，再根据指南的 commands 执行实际操作（用 tool_bash 或 tool_kg_run_graphify_cmd），最后更新状态进入 DETECT
- 若处于 DETECT 阶段：调用 tool_kg_detect 获取行动指南，按指南执行 graphifyy detect，更新状态进入 EXTRACT
- 若处于 EXTRACT 阶段：调用 tool_kg_extract_ast 与 tool_kg_extract_semantic 并行抽取，按指南执行，更新状态进入 BUILD
- 若处于 BUILD 阶段：调用 tool_kg_build_graph 构建图谱，按指南执行，更新状态进入 LABEL/EXPORT
- 若处于 QUERY 阶段：调用 tool_kg_query 回答问题，按指南执行
- 若处于 PATH 阶段：调用 tool_kg_path 查询最短路径
- 若处于 EXPLAIN 阶段：调用 tool_kg_explain 解释节点
- 若处于 ADD 阶段：调用 tool_kg_add_url 添加语料
- 若处于 WATCH 阶段：调用 tool_kg_watch 启动监控
- 若处于 EXPORT 阶段：按标志调用对应导出工具
- 所有阶段完成后：调用 tool_finish 返回最终报告

【工具调用流程关键点】
- 步骤工具（tool_kg_init/detect/extract_ast 等）会返回结构化的 KgStepGuide 行动指南
- KgStepGuide 包含 commands（要执行的命令）、checks（验证要点）、save_paths（产出文件）等
- 拿到 KgStepGuide 后，应根据其中的 commands 调用 tool_bash 或 tool_kg_run_graphify_cmd 执行实际操作
- 执行完命令后，再调用下一个步骤工具推进工作流
- 只有在所有阶段完成、得到最终结果时，才调用 tool_finish 返回 KgFinalReport

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

请生成初始化行动指南（KgStepGuide），包含：
1. 目标路径有效性检查命令（如：`test -d "{target}" && echo EXISTS || echo NOT_FOUND`）
2. 是否需要克隆 GitHub 仓库（若 target 为 GitHub URL）
3. 检查 graphify-out 目录是否已存在（`test -d "{pj_dir}/graphify-out" && echo EXISTS || echo NOT_EXIST`）
4. 必要的授权确认说明
5. 下一步建议阶段（DETECT/QUERY/ADD 等）

commands 字段请给出可直接执行的 shell 命令。
输出 KgStepGuide 结构。"""

# 文件检测
KG_DETECT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 DETECT 阶段。

扫描根目录：{scan_root}
模式：{mode}
项目目录：{pj_dir}

请生成文件检测行动指南（KgStepGuide）。
graphify 通过 `graphify extract --code-only <path>` 可同时完成检测与 AST 抽取，产出 graphify-out/graph.json 与 graphify-out/.graphify_detect.json。
若仅需检测不抽取，可用 `graphify extract <path> --no-cluster --code-only` 但需 LLM 后端。

commands 字段请给出可直接执行的 graphify CLI 命令，例如：
- `cd "{scan_root}" && python -m graphify extract . --code-only --out "{pj_dir}"`
- 检测完成后读取 `graphify-out/.graphify_detect.json` 判断文件数量
- 若 total_files>500 或 total_words>2,000,000，计算前 5 大子目录并建议用户聚焦

输出 KgStepGuide 结构。"""

# AST 结构化抽取（代码文件）
KG_EXTRACT_AST_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXTRACT-AST 阶段（结构化抽取）。

代码文件列表：{code_files}
扫描根目录：{scan_root}
项目目录：{pj_dir}
模式：{mode}

请生成 AST 抽取行动指南（KgStepGuide）。
graphify 的 `extract` 子命令已在 DETECT 阶段完成（`graphify extract --code-only` 会同时做检测和 AST 抽取）。
若检测已完成但需单独做 AST 抽取，可用 `graphify extract <path> --code-only`。

commands 字段请给出可直接执行的命令，例如：
- 若未做过抽取：`cd "{scan_root}" && python -m graphify extract . --code-only --out "{pj_dir}"`
- 检查产出：`test -f "{pj_dir}/graphify-out/graph.json" && echo EXTRACTED || echo NOT_EXTRACTED`

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

请生成语义抽取行动指南（KgStepGuide）。
graphify 通过 `graphify extract <path>` 可完成语义抽取（需 LLM 后端，如 GEMINI_API_KEY）。
--mode deep 可开启深度模式产生更丰富的 INFERRED 边。

commands 字段请给出可直接执行的命令，例如：
- 全量语义抽取：`cd "{scan_root}" && python -m graphify extract . --mode deep --out "{pj_dir}"`
- 仅增量更新：`cd "{scan_root}" && python -m graphify extract . --update --out "{pj_dir}"`
- 检查产出：`test -f "{pj_dir}/graphify-out/graph.json" && echo EXTRACTED || echo NOT_EXTRACTED`

注意：
- 无 LLM API Key 时会跳过语义抽取，仅做 AST
- 设置 GEMINI_API_KEY 或 GOOGLE_API_KEY 可使用 Gemini 后端
- 大语料建议分块并行，graphify 内部已自动处理

输出 KgStepGuide 结构。"""

# 合并 AST + 语义
KG_MERGE_EXTRACT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 MERGE-EXTRACT 阶段。

AST 结果：graphify-out/.graphify_ast.json
语义结果：graphify-out/.graphify_semantic.json
扫描根目录：{scan_root}

请生成合并行动指南（KgStepGuide）。
graphify 的 `extract` 子命令已自动完成合并，产出 graphify-out/graph.json。
若需手动合并（罕见），可用 Python 调用内部模块。

commands 字段请给出可直接执行的命令，例如：
- 检查合并产出：`test -f "{pj_dir}/graphify-out/graph.json" && echo MERGED || echo NOT_MERGED`
- 统计节点边数：`python -c "import json; d=json.load(open('{pj_dir}/graphify-out/graph.json')); print(f'nodes: {d.get(\"node_count\",0)}, edges: {d.get(\"edge_count\",0)}')"`

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

请生成图构建行动指南（KgStepGuide）。
graphify 通过 `graphify cluster-only <path>` 可完成聚类、分析、报告生成。
--force 允许强制重建（节点数减少时）。

commands 字段请给出可直接执行的命令，例如：
- 聚类与分析：`cd "{scan_root}" && python -m graphify cluster-only . --out "{pj_dir}"`
- 强制重建：`cd "{scan_root}" && python -m graphify cluster-only . --force --out "{pj_dir}"`
- 健康检查：`cd "{scan_root}" && python -m graphify diagnose multigraph --graph "{pj_dir}/graphify-out/graph.json"`
- 检查产出：`test -f "{pj_dir}/graphify-out/GRAPH_REPORT.md" && echo BUILT || echo NOT_BUILT`

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

请生成社区标注行动指南（KgStepGuide）。
graphify 通过 `graphify label <path>` 可自动用 LLM 命名社区（需 LLM 后端）。
--missing-only 仅命名缺失标签。

commands 字段请给出可直接执行的命令，例如：
- 自动命名社区：`cd "{scan_root}" && python -m graphify label . --out "{pj_dir}"`
- 仅命名缺失：`cd "{scan_root}" && python -m graphify label . --missing-only --out "{pj_dir}"`
- 检查产出：`test -f "{pj_dir}/graphify-out/.graphify_labels.json" && echo LABELED || echo NOT_LABELED`

输出 KgStepGuide 结构，包含生成的 labels 字典（若手动指定）。"""

# 导出 HTML/Obsidian/Wiki 等
KG_EXPORT_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXPORT 阶段。

导出配置：{export_flags}
项目目录：{pj_dir}
graphify-out 目录：{graphs_dir}

请生成导出行动指南（KgStepGuide），按标志执行对应的 graphify export 命令。

commands 字段请给出可直接执行的命令，例如：
- HTML：`cd "{graphs_dir}/.." && python -m graphify export html . --out "{pj_dir}"`
- Obsidian：`cd "{graphs_dir}/.." && python -m graphify export obsidian . --dir "{getattr(args, 'obsidian_dir', '') or graphs_dir + '/obsidian'}" --out "{pj_dir}"`
- Wiki：`cd "{graphs_dir}/.." && python -m graphify export wiki . --out "{pj_dir}"`
- Neo4j：`cd "{graphs_dir}/.." && python -m graphify export neo4j . --push "bolt://..." --out "{pj_dir}"`
- FalkorDB：`cd "{graphs_dir}/.." && python -m graphify export falkordb . --push "falkordb://..." --out "{pj_dir}"`
- SVG：`cd "{graphs_dir}/.." && python -m graphify export svg . --out "{pj_dir}"`
- GraphML：`cd "{graphs_dir}/.." && python -m graphify export graphml . --out "{pj_dir}"`
- MCP：`cd "{graphs_dir}/.." && python -m graphify export mcp . --out "{pj_dir}"`

输出 KgStepGuide 结构。"""

# 查询
KG_QUERY_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 QUERY 阶段。

查询问题：{question}
遍历模式：{dfs_mode}（BFS/DFS）
Token 预算：{budget}
graphify-out 目录：{graphs_dir}

请生成查询行动指南（KgStepGuide）。
graphify 通过 `graphify query "<question>"` 可执行自然语言查询。
--dfs 使用深度优先遍历，--budget 限制输出 token。

commands 字段请给出可直接执行的命令，例如：
- BFS 查询：`cd "{graphs_dir}/.." && python -m graphify query "{question}" --out "{pj_dir}"`
- DFS 查询：`cd "{graphs_dir}/.." && python -m graphify query "{question}" --dfs --out "{pj_dir}"`
- 带预算：`cd "{graphs_dir}/.." && python -m graphify query "{question}" --budget {budget} --out "{pj_dir}"`

输出 KgStepGuide 结构。"""

# 路径查询
KG_PATH_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 PATH 查询阶段。

起点：{path_from}
终点：{path_to}
graphify-out 目录：{graphs_dir}

请生成最短路径查询行动指南（KgStepGuide）。
graphify 通过 `graphify path "<from>" "<to>"` 可查询最短路径。

commands 字段请给出可直接执行的命令，例如：
- 路径查询：`cd "{graphs_dir}/.." && python -m graphify path "{path_from}" "{path_to}" --out "{pj_dir}"`

输出 KgStepGuide 结构。"""

# 节点解释
KG_EXPLAIN_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 EXPLAIN 阶段。

目标节点：{node_name}
graphify-out 目录：{graphs_dir}

请生成节点解释行动指南（KgStepGuide）。
graphify 通过 `graphify explain "<node>"` 可解释节点。
--all 列出所有连接。

commands 字段请给出可直接执行的命令，例如：
- 解释节点：`cd "{graphs_dir}/.." && python -m graphify explain "{node_name}" --out "{pj_dir}"`
- 详细解释：`cd "{graphs_dir}/.." && python -m graphify explain "{node_name}" --all --out "{pj_dir}"`

输出 KgStepGuide 结构。"""

# 添加 URL
KG_ADD_URL_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 ADD 阶段。

URL：{url}
作者：{author}
贡献者：{contributor}
项目目录：{pj_dir}

请生成添加 URL 行动指南（KgStepGuide）。
graphify 通过 `graphify add <url>` 可添加 URL 到语料库。
--author/--contributor 指定元数据。

commands 字段请给出可直接执行的命令，例如：
- 添加 URL：`cd "{pj_dir}" && python -m graphify add "{url}" --author "{author}" --contributor "{contributor}" --out "{pj_dir}"`
- 增量更新：`cd "{pj_dir}" && python -m graphify update . --out "{pj_dir}"`

输出 KgStepGuide 结构。"""

# 视频转录
KG_TRANSCRIBE_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 TRANSCRIBE 阶段。

视频文件：{video_files}
Whisper 模型：{whisper_model}
项目目录：{pj_dir}

请生成视频转录行动指南（KgStepGuide）。
graphify 通过 `graphify extract <path>` 会自动调用 whisper 转录视频/音频文件。
也可单独用 `whisper` 命令行工具。

commands 字段请给出可直接执行的命令，例如：
- 单独转录：`whisper "{video_files}" --model {whisper_model} --output_dir "{pj_dir}/graphify-out/transcripts/"`
- 或通过 graphify：`cd "{pj_dir}" && python -m graphify extract . --out "{pj_dir}"`（会自动处理视频）

输出 KgStepGuide 结构。"""

# 监控模式
KG_WATCH_PMT = """{_JSON_OUT}

你正在执行 kg 子命令的 WATCH 阶段。

监控目录：{watch_dir}
项目目录：{pj_dir}

请生成监控行动指南（KgStepGuide）。
graphify 通过 `graphify watch <path>` 可监控文件变化并自动增量重建。

commands 字段请给出可直接执行的命令，例如：
- 启动监控：`cd "{watch_dir}" && python -m graphify watch . --out "{pj_dir}"`

注意：监控模式会长期运行，通常在后台执行。

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