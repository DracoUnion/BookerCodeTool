# -*- coding: utf-8 -*-
"""kg 子命令 Pydantic 模型。"""

from typing import *
from pydantic import BaseModel, Field


class SkillInfo(BaseModel):
    """一个内置 kg 技能的元信息"""
    name: str = Field(..., description="技能名称")
    slug: str = Field(..., description="下划线形式的技能标识")
    description: str = Field('', description="技能用途描述")
    dir: str = Field('', description="技能来源目录（内置技能为空）")
    tool_name: str = Field('', description="对应的工具名")
    files: List[str] = Field(default_factory=list, description="附带文件（内置技能为空）")
    steps: List[str] = Field(default_factory=list, description="按工作流拆分后的步骤名（key）列表")


class AskUserResult(BaseModel):
    """询问用户的工具返回结果"""
    question: str = Field(..., description="提出的问题")
    answer: str = Field('', description="用户的回答")
    options: List[str] = Field(default_factory=list, description="提供的可选项")
    non_interactive: bool = Field(False, description="是否因无法读取输入而使用默认值")
    note: str = Field('', description="补充说明")


class KgState(BaseModel):
    """kg 任务状态机。"""
    project_id: str = Field(default='kg_project', description='项目 ID')
    target: str = Field(default='', description='目标路径或 GitHub URL')
    goal: str = Field(default='', description='任务目标描述')
    stage: str = Field(default='INIT', description='当前阶段：INIT/DETECT/EXTRACT/BUILD/QUERY/ADD/WATCH/DONE')
    authorized: bool = Field(default=False, description='是否已确认授权（针对敏感操作）')
    graph_exists: bool = Field(default=False, description='graph.json 是否已存在')
    graphs_dir: str = Field(default='', description='graphify-out 目录路径')
    last_query: str = Field(default='', description='最后一次查询')
    next_action: str = Field(default='', description='下一步建议动作')
    assets: List[str] = Field(default_factory=list, description='已发现资产')
    findings: List[str] = Field(default_factory=list, description='发现摘要')
    notes: List[str] = Field(default_factory=list, description='分析笔记')


class KgDetectResult(BaseModel):
    """文件检测结果。"""
    total_files: int = Field(description='总文件数')
    total_words: int = Field(description='总字数估算')
    files: Dict[str, List[str]] = Field(default_factory=dict, description='分类文件列表')
    skipped_sensitive: List[str] = Field(default_factory=list, description='跳过的敏感文件')
    scan_root: str = Field(description='扫描根目录')


class KgExtractResult(BaseModel):
    """实体关系抽取结果。"""
    nodes: List[Dict[str, Any]] = Field(default_factory=list, description='节点列表')
    edges: List[Dict[str, Any]] = Field(default_factory=list, description='边列表')
    hyperedges: List[Dict[str, Any]] = Field(default_factory=list, description='超边列表')
    input_tokens: int = Field(default=0, description='输入 token 数')
    output_tokens: int = Field(default=0, description='输出 token 数')


class KgBuildResult(BaseModel):
    """图构建结果。"""
    nodes: int = Field(description='节点数')
    edges: int = Field(description='边数')
    communities: int = Field(description='社区数')
    gods: List[Dict[str, Any]] = Field(default_factory=list, description='核心节点')
    surprises: List[Dict[str, Any]] = Field(default_factory=list, description='惊人连接')
    questions: List[str] = Field(default_factory=list, description='建议问题')


class KgQueryResult(BaseModel):
    """查询结果。"""
    answer: str = Field(description='回答')
    nodes: List[Dict[str, Any]] = Field(default_factory=list, description='相关节点')
    edges: List[Dict[str, Any]] = Field(default_factory=list, description='相关边')
    path: List[str] = Field(default_factory=list, description='路径节点')


class KgAddResult(BaseModel):
    """添加 URL 结果。"""
    url: str = Field(description='添加的 URL')
    saved_path: str = Field(description='保存路径')
    author: str = Field(default='', description='作者')
    contributor: str = Field(default='', description='贡献者')


class KgFinalReport(BaseModel):
    """最终报告。"""
    status: str = Field(default='done', description='状态：done/needs_approval/error')
    stage: str = Field(default='DONE', description='最终阶段')
    target: str = Field(default='', description='目标')
    summary: str = Field(default='', description='总结')
    graph_stats: Dict[str, Any] = Field(default_factory=dict, description='图统计')
    outputs: List[str] = Field(default_factory=list, description='输出文件列表')
    blockers: List[str] = Field(default_factory=list, description='阻塞项')
    next_action: str = Field(default='', description='下一步动作')


# ══════════════════════════════════════════════════════════════════
# 工具步骤产出模型（带文件缓存）
# ══════════════════════════════════════════════════════════════════

class SkillStepGuide(BaseModel):
    """技能步骤行动指南（统一结构，用于文件缓存）。"""
    skill: str = Field(default='', description='技能名')
    step: str = Field(default='', description='步骤名')
    title: str = Field(description='步骤标题')
    commands: List[str] = Field(default_factory=list, description='建议执行的命令')
    payloads: List[str] = Field(default_factory=list, description='载荷/参数')
    checks: List[str] = Field(default_factory=list, description='验证要点')
    save_paths: List[str] = Field(default_factory=list, description='产出文件路径')
    notes: str = Field(default='', description='备注')


class KgStepGuide(SkillStepGuide):
    """kg 专用步骤指南（继承通用结构，可扩展特定字段）。"""
    mode: str = Field(default='', description='运行模式')
    flags: List[str] = Field(default_factory=list, description='额外标志')


# ══════════════════════════════════════════════════════════════════
# 导出格式模型
# ══════════════════════════════════════════════════════════════════

class ExportFormat(BaseModel):
    """导出格式配置。"""
    html: bool = Field(default=True, description='生成 HTML 可视化')
    svg: bool = Field(default=False, description='生成 SVG')
    graphml: bool = Field(default=False, description='生成 GraphML')
    neo4j: bool = Field(default=False, description='生成 Neo4j Cypher')
    neo4j_push: str = Field(default='', description='推送到 Neo4j')
    falkordb: bool = Field(default=False, description='生成 FalkorDB Cypher')
    falkordb_push: str = Field(default='', description='推送到 FalkorDB')
    wiki: bool = Field(default=False, description='生成 Wiki')
    obsidian: bool = Field(default=False, description='生成 Obsidian Vault')
    obsidian_dir: str = Field(default='', description='Obsidian 保存目录')
    mcp: bool = Field(default=False, description='启动 MCP 服务器')
    watch: bool = Field(default=False, description='监控文件变化')
    no_viz: bool = Field(default=False, description='跳过可视化')
    no_cluster: bool = Field(default=False, description='跳过聚类')


# ══════════════════════════════════════════════════════════════════
# 参数模型（用于子命令解析器）
# ══════════════════════════════════════════════════════════════════

class KgArgs(BaseModel):
    """kg 子命令参数。"""
    target: str = Field(default='', description='目标路径或 GitHub URL')
    mode: str = Field(default='', description='模式：deep/update/cluster-only/force/directed')
    whisper_model: str = Field(default='', description='Whisper 模型')
    query: str = Field(default='', description='查询问题')
    dfs: bool = Field(default=False, description='使用 DFS 遍历')
    budget: int = Field(default=0, description='Token 预算上限')
    path_from: str = Field(default='', description='路径查询起点')
    path_to: str = Field(default='', description='路径查询终点')
    explain: str = Field(default='', description='解释节点')
    add_url: str = Field(default='', description='添加的 URL')
    author: str = Field(default='', description='作者')
    contributor: str = Field(default='', description='贡献者')
    export: ExportFormat = Field(default_factory=ExportFormat, description='导出格式')
    project: str = Field(default='kg_project', description='项目工作目录')
    max_steps: int = Field(default=80, description='最大步数')
    list_exports: bool = Field(default=False, description='列出可用导出格式')
    authorized: bool = Field(default=False, description='确认授权')