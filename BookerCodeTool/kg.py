# -*- coding: utf-8 -*-
"""kg 子命令：知识图谱构建与查询（graphify 内联实现 + 固定工作流）。

采用固定工作流：编排器按预定义步骤顺序执行，每步调用对应的内联实现工具。
支持模式：build（构建）、query（查询）、path（路径）、explain（解释）、add（添加URL）、export（导出）。
"""

import json
import logging
import os
import re
import traceback
from os import path
from typing import Dict, List, Any, Optional

import json_repair

from .openai import logger as oai_logger
from .util import render_prompt, write_text, json_dump_model, write_yaml_model
from .kg_models import *
from .kg_pmt import KG_SYSTEM_PROMPT, _JSON_OUT
from .kg_tools import KgAgent, builtin_skills, step_tool_map

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s][%(name)s][%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


def _sanitize_findings(findings: Any) -> List[Dict[str, Any]]:
    """规范化 findings 结构。"""
    result = []
    if not isinstance(findings, list):
        return result
    for it in findings:
        if isinstance(it, str):
            result.append({'title': it})
        elif isinstance(it, dict):
            if 'title' not in it:
                it = {**it, 'title': str(it.get('name', '未命名发现'))}
            result.append(it)
    return result


def parse_final(ans: str) -> KgFinalReport:
    """解析最终输出为 KgFinalReport。"""
    text = ans.strip()
    if text.startswith('```'):
        m = re.search(r'```(?:json)?\s*([\s\S]+?)```', text)
        if m:
            text = m.group(1)
    data = None
    try:
        data = json_repair.loads(text)
    except Exception:
        data = None
    if not isinstance(data, dict):
        try:
            start = text.find('{')
            end = text.rfind('}')
            if start != -1 and end > start:
                data = json_repair.loads(text[start:end + 1])
        except Exception:
            data = None
    if not isinstance(data, dict):
        return KgFinalReport(
            status='needs_approval',
            stage='UNKNOWN',
            summary=f'无法解析模型最终输出，原文如下：\n{text[:2000]}',
            blockers=['最终输出非合法 JSON'],
            next_action='人工介入',
        )
    if 'done' in data and 'status' not in data:
        data['status'] = 'done'
    if 'findings' in data:
        data['findings'] = _sanitize_findings(data['findings'])
    try:
        return KgFinalReport(**data)
    except Exception:
        return KgFinalReport(
            status='needs_approval',
            stage=str(data.get('stage', 'UNKNOWN')),
            target=str(data.get('target', '')),
            summary=str(data.get('summary', json_dump_model(data))),
            blockers=['最终输出字段不全，请人工检查'],
            next_action='人工介入',
        )


class KgOrchestrator:
    """kg 编排器（固定工作流）。"""

    def __init__(self, args):
        self.args = args
        self.tools = KgAgent(args)
        self.pj_dir = self.tools.pj_dir
        self.llm = self.tools.llm
        logger.info('kg 已加载技能：%s', [s.name for s in self.tools.skills])

    def _init_state(self) -> KgState:
        """初始化状态机。"""
        state = self.tools.tool_load_state()
        target = getattr(self.args, 'target', '') or state.target
        goal = getattr(self.args, 'goal', '') or state.goal
        if target:
            state.target = target
        if goal:
            state.goal = goal
        if not getattr(self.args, 'no_authorized', False):
            state.authorized = True

        # 检查 graph.json 是否存在
        graph_file = path.join(self.pj_dir, 'graphify-out', 'graph.json')
        state.graph_exists = path.isfile(graph_file)

        # 确定运行模式
        query = getattr(self.args, 'query', '')
        path_from = getattr(self.args, 'path_from', '')
        path_to = getattr(self.args, 'path_to', '')
        explain = getattr(self.args, 'explain', '')
        add_url = getattr(self.args, 'add_url', '')
        export_flags = getattr(self.args, 'export', ExportFormat())
        has_export = any(vars(export_flags).values())
        mode = getattr(self.args, 'mode', '')

        if query:
            state.stage = 'QUERY'
            state.last_query = query
        elif path_from and path_to:
            state.stage = 'PATH'
        elif explain:
            state.stage = 'EXPLAIN'
        elif add_url:
            state.stage = 'ADD'
        elif has_export and state.graph_exists:
            state.stage = 'EXPORT'
        elif mode == 'update':
            state.stage = 'EXTRACT'
        elif state.graph_exists and not (target and not state.graph_exists):
            state.stage = 'QUERY'
        else:
            state.stage = 'INIT'

        write_yaml_model(self.tools._state_fname(), state)
        return state

    def run(self) -> KgFinalReport:
        """执行固定工作流。"""
        state = self._init_state()
        target = state.target
        scan_root = self.tools._get_scan_root(target)

        # 根据阶段执行对应工作流
        # 构建工作流阶段：INIT -> DETECT -> EXTRACT -> BUILD -> LABEL -> EXPORT
        build_stages = ['INIT', 'DETECT', 'EXTRACT', 'BUILD', 'LABEL', 'EXPORT']

        if state.stage in build_stages:
            return self._run_build_workflow(scan_root, state)
        elif state.stage == 'QUERY':
            return self._run_query(state)
        elif state.stage == 'PATH':
            return self._run_path(state)
        elif state.stage == 'EXPLAIN':
            return self._run_explain(state)
        elif state.stage == 'ADD':
            return self._run_add_url(state)
        elif state.stage == 'EXPORT':
            return self._run_export(state)
        else:
            return KgFinalReport(
                status='needs_approval',
                stage=state.stage,
                summary=f'未知阶段：{state.stage}',
                blockers=[f'不支持的阶段：{state.stage}'],
                next_action='检查参数',
            )

    def _run_build_workflow(self, scan_root: str, state: KgState) -> KgFinalReport:
        """完整构建工作流：DETECT -> EXTRACT -> BUILD -> LABEL -> EXPORT。"""
        findings = []
        outputs = []

        try:
            # Step 1: DETECT - 文件检测
            logger.info('[1/5] DETECT: 文件检测与分类')
            state.stage = 'DETECT'
            self.tools.tool_update_state(stage='DETECT', next_action='调用 tool_kg_detect_files')
            detect_result = self.tools.tool_kg_detect_files(scan_root)
            if 'error' in detect_result:
                raise RuntimeError(f'检测失败: {detect_result["error"]}')
            findings.append(f'检测完成：{detect_result["total_files"]} 文件，{detect_result["total_words"]} 字')

            # 大语料提示（由内联工具已处理用户交互）
            if detect_result.get('total_files', 0) > 500 or detect_result.get('total_words', 0) > 2_000_000:
                findings.append('大语料检测：已提示用户选择子目录或全量构建')

            # Step 2: EXTRACT - 并行 AST + 语义抽取
            logger.info('[2/5] EXTRACT: 实体关系抽取')
            state.stage = 'EXTRACT'
            self.tools.tool_update_state(stage='EXTRACT', next_action='并行调用 tool_kg_extract_ast 与 tool_kg_extract_semantic')

            # AST 抽取
            ast_result = self.tools.tool_kg_extract_ast(scan_root)
            if 'error' in ast_result:
                raise RuntimeError(f'AST 抽取失败: {ast_result["error"]}')
            findings.append(f'AST 抽取：{len(ast_result.get("nodes", []))} 节点，{len(ast_result.get("edges", []))} 边')

            # 语义抽取
            sem_result = self.tools.tool_kg_extract_semantic(scan_root)
            if 'error' in sem_result:
                raise RuntimeError(f'语义抽取失败: {sem_result["error"]}')
            sem_nodes = len(sem_result.get('nodes', []))
            sem_edges = len(sem_result.get('edges', []))
            findings.append(f'语义抽取：{sem_nodes} 节点，{sem_edges} 边' + (' (无 LLM 后端，跳过)' if sem_nodes == 0 else ''))

            # 合并
            merge_result = self.tools.tool_kg_merge_extract(scan_root)
            if 'error' in merge_result:
                raise RuntimeError(f'合并失败: {merge_result["error"]}')
            findings.append(f'合并完成：{len(merge_result.get("nodes", []))} 节点，{len(merge_result.get("edges", []))} 边')

            # Step 3: BUILD - 图构建、聚类、分析
            logger.info('[3/5] BUILD: 图构建、聚类与分析')
            state.stage = 'BUILD'
            self.tools.tool_update_state(stage='BUILD', next_action='调用 tool_kg_build_graph')
            build_args = {
                'scan_root': scan_root,
                'directed': getattr(self.args, 'mode', '') == 'directed',
                'force': getattr(self.args, 'mode', '') == 'force',
            }
            build_result = self.tools.tool_kg_build_graph(**build_args)
            if 'error' in build_result:
                raise RuntimeError(f'图构建失败: {build_result["error"]}')
            findings.append(f'图构建：{build_result["nodes"]} 节点，{build_result["edges"]} 边，{build_result["communities"]} 社区')

            # Step 4: LABEL - 社区标注
            logger.info('[4/5] LABEL: 社区标注')
            state.stage = 'LABEL'
            self.tools.tool_update_state(stage='LABEL', next_action='调用 tool_kg_label_communities')
            label_args = {
                'scan_root': scan_root,
                'directed': getattr(self.args, 'mode', '') == 'directed',
            }
            label_result = self.tools.tool_kg_label_communities(**label_args)
            if 'error' in label_result:
                logger.warning(f'社区标注失败（使用默认标签）: {label_result["error"]}')
                findings.append('社区标注：使用默认标签')
            else:
                findings.append(f'社区标注：{len(label_result.get("labels", {}))} 个社区已命名')

            # Step 5: EXPORT - 多格式导出
            logger.info('[5/5] EXPORT: 多格式导出')
            state.stage = 'EXPORT'
            export_flags = getattr(self.args, 'export', ExportFormat())
            export_args = {
                'formats': self._get_export_formats(export_flags),
                'scan_root': scan_root,
            }
            export_result = self.tools.tool_kg_export(**export_args)
            if 'error' in export_result:
                logger.warning(f'导出部分失败: {export_result["error"]}')
            else:
                for fmt, out_path in export_result.items():
                    outputs.append(f'{fmt}: {out_path}')
                findings.append(f'导出完成：{list(export_result.keys())}')

            # 完成
            state.stage = 'DONE'
            self.tools.tool_update_state(stage='DONE', next_action='')

            return KgFinalReport(
                status='done',
                stage='DONE',
                target=state.target,
                summary='知识图谱构建完成',
                graph_stats=build_result,
                outputs=outputs,
                findings=findings,
                blockers=[],
                next_action='',
            )

        except Exception as ex:
            logger.exception('构建工作流失败')
            return KgFinalReport(
                status='needs_approval',
                stage=state.stage or 'ERROR',
                target=state.target,
                summary=f'构建失败: {ex}',
                graph_stats={},
                outputs=[],
                findings=findings,
                blockers=[str(ex)],
                next_action='检查日志并重试',
            )

    def _get_export_formats(self, export_flags: ExportFormat) -> List[str]:
        """获取启用的导出格式列表。"""
        formats = []
        flags = vars(export_flags)
        for fmt, enabled in flags.items():
            if enabled and fmt not in ('obsidian_dir', 'neo4j_push', 'falkordb_push'):
                formats.append(fmt)
        return formats

    def _run_query(self, state: KgState) -> KgFinalReport:
        """查询工作流。"""
        query = state.last_query or getattr(self.args, 'query', '')
        if not query:
            return KgFinalReport(status='needs_approval', stage='QUERY', summary='缺少查询问题', blockers=['query 参数为空'], next_action='提供 -q 参数')

        state.stage = 'QUERY'
        self.tools.tool_update_state(stage='QUERY', next_action='调用 tool_kg_query')

        query_args = {
            'question': query,
            'dfs': getattr(self.args, 'dfs', False),
            'budget': getattr(self.args, 'budget', 1500),
            'graphs_dir': str(path.join(self.pj_dir, 'graphify-out')),
        }
        result = self.tools.tool_kg_query(**query_args)

        if 'error' in result:
            return KgFinalReport(status='needs_approval', stage='QUERY', summary=f'查询失败: {result["error"]}', blockers=[result['error']], next_action='确保 graph.json 存在')

        return KgFinalReport(
            status='done', stage='QUERY', target=state.target,
            summary=f'查询完成: {query}',
            graph_stats={'answer_length': len(str(result.get("answer", "")))},
            outputs=[f'answer: {result.get("answer", "")[:200]}...'],
            findings=[str(result.get("answer", ""))],
            blockers=[],
            next_action='',
        )

    def _run_path(self, state: KgState) -> KgFinalReport:
        """路径查询工作流。"""
        path_from = getattr(self.args, 'path_from', '')
        path_to = getattr(self.args, 'path_to', '')
        if not path_from or not path_to:
            return KgFinalReport(status='needs_approval', stage='PATH', summary='缺少路径参数', blockers=['path_from/path_to 为空'], next_action='提供 --path-from 和 --path-to')

        state.stage = 'PATH'
        self.tools.tool_update_state(stage='PATH', next_action='调用 tool_kg_path')

        path_args = {
            'path_from': path_from,
            'path_to': path_to,
            'graphs_dir': str(path.join(self.pj_dir, 'graphify-out')),
        }
        result = self.tools.tool_kg_path(**path_args)

        if 'error' in result:
            return KgFinalReport(status='needs_approval', stage='PATH', summary=f'路径查询失败: {result["error"]}', blockers=[result['error']], next_action='确保 graph.json 存在')

        return KgFinalReport(
            status='done', stage='PATH', target=state.target,
            summary=f'路径查询完成: {path_from} -> {path_to}',
            graph_stats={'path_length': len(result.get("path", []))},
            outputs=[f'path: {result.get("path", [])}'],
            findings=[str(result)],
            blockers=[],
            next_action='',
        )

    def _run_explain(self, state: KgState) -> KgFinalReport:
        """节点解释工作流。"""
        explain = getattr(self.args, 'explain', '')
        if not explain:
            return KgFinalReport(status='needs_approval', stage='EXPLAIN', summary='缺少节点名', blockers=['explain 参数为空'], next_action='提供 --explain 参数')

        state.stage = 'EXPLAIN'
        self.tools.tool_update_state(stage='EXPLAIN', next_action='调用 tool_kg_explain')

        explain_args = {
            'node_name': explain,
            'graphs_dir': str(path.join(self.pj_dir, 'graphify-out')),
        }
        result = self.tools.tool_kg_explain(**explain_args)

        if 'error' in result:
            return KgFinalReport(status='needs_approval', stage='EXPLAIN', summary=f'节点解释失败: {result["error"]}', blockers=[result['error']], next_action='确保 graph.json 存在')

        return KgFinalReport(
            status='done', stage='EXPLAIN', target=state.target,
            summary=f'节点解释完成: {explain}',
            graph_stats={},
            outputs=[str(result)],
            findings=[str(result.get("explanation", result))],
            blockers=[],
            next_action='',
        )

    def _run_add_url(self, state: KgState) -> KgFinalReport:
        """添加 URL 工作流。"""
        add_url = getattr(self.args, 'add_url', '')
        if not add_url:
            return KgFinalReport(status='needs_approval', stage='ADD', summary='缺少 URL', blockers=['add_url 参数为空'], next_action='提供 --add-url 参数')

        state.stage = 'ADD'
        self.tools.tool_update_state(stage='ADD', next_action='调用 tool_kg_add_url')

        add_args = {
            'url': add_url,
            'author': getattr(self.args, 'author', ''),
            'contributor': getattr(self.args, 'contributor', ''),
        }
        result = self.tools.tool_kg_add_url(**add_args)

        if 'error' in result:
            return KgFinalReport(status='needs_approval', stage='ADD', summary=f'添加 URL 失败: {result["error"]}', blockers=[result['error']], next_action='检查 URL 有效性')

        # 触发增量更新
        logger.info('触发增量更新...')
        scan_root = self.tools._get_scan_root(state.target)
        build_args = {
            'scan_root': scan_root,
            'directed': getattr(self.args, 'mode', '') == 'directed',
            'force': False,
        }
        build_result = self.tools.tool_kg_build_graph(**build_args)

        return KgFinalReport(
            status='done', stage='ADD', target=state.target,
            summary=f'URL 添加并增量更新完成: {add_url}',
            graph_stats=build_result if 'error' not in build_result else {},
            outputs=[f'saved: {result.get("saved_path", "")}'],
            findings=[f'URL 已添加到语料库，增量更新完成'],
            blockers=[],
            next_action='',
        )

    def _run_export(self, state: KgState) -> KgFinalReport:
        """导出工作流。"""
        state.stage = 'EXPORT'
        export_flags = getattr(self.args, 'export', ExportFormat())
        export_args = {
            'formats': self._get_export_formats(export_flags),
            'scan_root': self.tools._get_scan_root(state.target),
        }
        self.tools.tool_update_state(stage='EXPORT', next_action='调用 tool_kg_export')

        result = self.tools.tool_kg_export(**export_args)

        if 'error' in result:
            return KgFinalReport(status='needs_approval', stage='EXPORT', summary=f'导出失败: {result["error"]}', blockers=[result['error']], next_action='确保 graph.json 存在')

        outputs = [f'{fmt}: {p}' for fmt, p in result.items()]
        return KgFinalReport(
            status='done', stage='EXPORT', target=state.target,
            summary=f'导出完成: {list(result.keys())}',
            graph_stats={},
            outputs=outputs,
            findings=[f'导出格式: {list(result.keys())}'],
            blockers=[],
            next_action='',
        )


# ══════════════════════════════════════════════════════════════════
# 入口函数
# ══════════════════════════════════════════════════════════════════

def list_skills(args):
    """列出内置 kg 技能及步骤。"""
    skills = builtin_skills()
    tmap = step_tool_map()
    print('=' * 70)
    print('内置 kg 技能（graphify 工作流拆分为步骤工具）')
    print('=' * 70)
    if not skills:
        print('未内置任何技能。')
        return
    step_total = 0
    for sk in skills:
        print(f'- {sk.name}')
        print(f'  描述  ：{sk.description}')
        smap = tmap.get(sk.name, {})
        for key in sk.steps:
            step_total += 1
            print(f'    · {key} -> {smap.get(key, "")}')
    print(f'\n共 {len(skills)} 个技能，{step_total} 个步骤工具。')


def kg_handle(args):
    """入口函数：创建编排器并运行固定工作流。"""
    if getattr(args, 'list_skills', False):
        list_skills(args)
        raise SystemExit(0)
    if getattr(args, 'debug', False):
        logger.setLevel(logging.DEBUG)
        oai_logger.setLevel(logging.DEBUG)
    return KgOrchestrator(args).run()


def reg_subparser(subparsers):
    """注册 kg 子命令参数。"""
    p = subparsers.add_parser(
        'kg',
        help='知识图谱构建与查询（graphify 内联实现 + 固定工作流）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例：
  BookerCodeTool kg -T ./myproject                    # 对目录构建知识图谱
  BookerCodeTool kg -T https://github.com/user/repo   # 克隆并构建
  BookerCodeTool kg -T ./myproject --mode deep        # 深度模式（更丰富推断边）
  BookerCodeTool kg -T ./myproject --mode update      # 增量更新
  BookerCodeTool kg -T ./myproject -q "How does auth work?"  # 查询已有图谱
  BookerCodeTool kg -T ./myproject --path-from A --path-to B  # 最短路径
  BookerCodeTool kg -T ./myproject --explain "UserService"    # 解释节点
  BookerCodeTool kg -T ./myproject --add-url "https://..."   # 添加 URL
  BookerCodeTool kg -T ./myproject --wiki --obsidian         # 导出 Wiki + Obsidian
  BookerCodeTool kg -T ./myproject --neo4j-push bolt://localhost:7687  # 推送到 Neo4j
'''
    )
    p.add_argument('-T', '--target', default='', help='目标路径（本地目录或 GitHub URL），缺省当前目录')
    p.add_argument('-g', '--goal', default='', help='任务目标描述（如：构建完整知识图谱并导出 HTML）')
    p.add_argument('-p', '--project', default='kg_project', help='项目工作目录（也作恢复断点用）')
    p.add_argument('-ms', '--max-steps', type=int, default=80, help='本轮循环步数上限（默认 80）')

    p.add_argument('--mode', choices=['deep', 'update', 'cluster-only', 'force', 'directed'], default='', help='运行模式：deep(深度)/update(增量)/cluster-only(仅聚类)/force(强制重建)/directed(有向图)')
    p.add_argument('--whisper-model', default='base', help='Whisper 模型（视频转录用）：tiny/base/small/medium/large')

    p.add_argument('-q', '--query', default='', help='自然语言查询问题（图谱存在时直接查询）')
    p.add_argument('--dfs', action='store_true', help='查询时使用 DFS（默认 BFS）')
    p.add_argument('--budget', type=int, default=1500, help='查询回答 Token 预算上限')
    p.add_argument('--path-from', default='', help='最短路径查询起点节点名')
    p.add_argument('--path-to', default='', help='最短路径查询终点节点名')
    p.add_argument('--explain', default='', help='解释指定节点')

    p.add_argument('--add-url', default='', help='添加 URL 到语料库')
    p.add_argument('--author', default='', help='URL 作者')
    p.add_argument('--contributor', default='', help='URL 贡献者')

    p.add_argument('--html', action='store_true', help='生成 HTML 可视化（默认开启）')
    p.add_argument('--no-viz', action='store_true', help='跳过可视化生成')
    p.add_argument('--svg', action='store_true', help='生成 SVG')
    p.add_argument('--graphml', action='store_true', help='生成 GraphML')
    p.add_argument('--neo4j', action='store_true', help='生成 Neo4j Cypher')
    p.add_argument('--neo4j-push', default='', help='直接推送到 Neo4j (bolt://...)')
    p.add_argument('--falkordb', action='store_true', help='生成 FalkorDB Cypher')
    p.add_argument('--falkordb-push', default='', help='直接推送到 FalkorDB (falkordb://...)')
    p.add_argument('--wiki', action='store_true', help='生成 Wiki')
    p.add_argument('--obsidian', action='store_true', help='生成 Obsidian Vault')
    p.add_argument('--obsidian-dir', default='', help='Obsidian 保存目录')
    p.add_argument('--mcp', action='store_true', help='启动 MCP stdio 服务器')
    p.add_argument('--watch', action='store_true', help='监控文件变化自动重建')
    p.add_argument('--no-cluster', action='store_true', help='跳过聚类步骤')

    p.add_argument('-l', '--list-skills', action='store_true', help='列出全部内置 kg 技能并退出')
    p.add_argument('--no-authorized', '--AU', action='store_true', help='确认未获得授权（涉及外部写入时需要，默认假设已授权）')
    p.add_argument('-D', '--debug', action='store_true', help='调试模式')
    p.set_defaults(func=kg_handle)


import argparse