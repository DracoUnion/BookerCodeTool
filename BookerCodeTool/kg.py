# -*- coding: utf-8 -*-
"""kg 子命令：知识图谱构建与查询智能体（graphify 技能工具 + 开放工具调用循环）。

采用开放工具调用循环：模型每轮决定调用哪个工具，编排器用 call_llm_with_toolcall_retry 驱动循环直到模型调用 tool_finish。
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
from .kg_pmt import KG_SYSTEM_PROMPT, KG_ROUND_PROMPT, _JSON_OUT
from .kg_tools import KgTools, builtin_skills, step_tool_map

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
    """kg 智能体编排器（开放工具调用循环）。"""

    def __init__(self, args):
        self.args = args
        self.tools = KgTools(args)
        self.pj_dir = self.tools.pj_dir
        self.llm = self.tools.llm
        self.max_steps = getattr(args, 'max_steps', 80)
        logger.info('kg 可用工具：%s', list(self.tools.get_tool_dict().keys()))
        logger.info('kg 已加载技能：%s', [s.name for s in self.tools.skills])

    def _skill_catalog(self) -> str:
        """渲染技能目录。"""
        if not self.tools.skills:
            return '\n\n【技能目录】\n（未内置任何技能，请用 tool_bash 与基础工具完成目标。）'
        tmap = step_tool_map()
        lines = [
            '\n\n【技能目录】（内置 kg 技能，按工作流拆分为步骤工具，按下列顺序逐步推进）'
        ]
        for sk in self.tools.skills:
            desc = (sk.description or '').strip().replace('\n', ' ')
            if len(desc) > 160:
                desc = desc[:160] + '...'
            smap = tmap.get(sk.name, {})
            steps = ' → '.join(
                f'{key}({smap.get(key, "")})' for key in sk.steps)
            lines.append(f'- {sk.name}｜{desc}')
            lines.append(f'  步骤：{steps}')
        return '\n'.join(lines)

    def _build_initial_user_input(self) -> str:
        """构建初始用户输入。"""
        state = self.tools.tool_load_state()
        target = getattr(self.args, 'target', '') or state.target
        goal = getattr(self.args, 'goal', '') or state.goal

        # 检查是否为查询模式
        query = getattr(self.args, 'query', '')
        path_from = getattr(self.args, 'path_from', '')
        path_to = getattr(self.args, 'path_to', '')
        explain = getattr(self.args, 'explain', '')
        add_url = getattr(self.args, 'add_url', '')

        parts = [
            f'目标：{target or "（未提供）"}',
            f'任务描述：{goal or "（未提供）"}',
        ]

        if query:
            parts.append(f'查询问题：{query}')
            parts.append(f'遍历模式：{"DFS" if getattr(self.args, "dfs", False) else "BFS"}')
            if getattr(self.args, 'budget', 0):
                parts.append(f'Token 预算：{self.args.budget}')
        elif path_from and path_to:
            parts.append(f'路径查询：{path_from} → {path_to}')
        elif explain:
            parts.append(f'节点解释：{explain}')
        elif add_url:
            parts.append(f'添加 URL：{add_url}')
            if getattr(self.args, 'author', ''):
                parts.append(f'作者：{self.args.author}')
            if getattr(self.args, 'contributor', ''):
                parts.append(f'贡献者：{self.args.contributor}')
        else:
            parts.append(f'授权状态：{"已确认" if state.authorized else "未确认"}')

        parts.append(f'项目目录：{self.pj_dir}')
        parts.append(f'最大步数：{self.max_steps}')
        return '\n'.join(parts)

    def run(self) -> KgFinalReport:
        """执行 tool-call 循环直到模型调用 tool_finish 结束。"""
        state = self.tools.tool_load_state()

        # 初始化状态
        target = getattr(self.args, 'target', '') or state.target
        goal = getattr(self.args, 'goal', '') or state.goal
        if target:
            state.target = target
        if goal:
            state.goal = goal
        if getattr(self.args, 'authorized', False):
            state.authorized = True

        # 检查 graph.json 是否存在（快速查询路径）
        graph_file = path.join(self.pj_dir, 'graphify-out', 'graph.json')
        state.graph_exists = path.isfile(graph_file)

        # 确定初始阶段
        query = getattr(self.args, 'query', '')
        path_from = getattr(self.args, 'path_from', '')
        path_to = getattr(self.args, 'path_to', '')
        explain = getattr(self.args, 'explain', '')
        add_url = getattr(self.args, 'add_url', '')

        if query:
            state.stage = 'QUERY'
            state.last_query = query
        elif path_from and path_to:
            state.stage = 'PATH'
        elif explain:
            state.stage = 'EXPLAIN'
        elif add_url:
            state.stage = 'ADD'
        elif getattr(self.args, 'export', None) and any(vars(getattr(self.args, 'export')).values()):
            state.stage = 'EXPORT'
        elif getattr(self.args, 'mode', '') == 'update':
            state.stage = 'EXTRACT'  # 增量更新
        elif state.graph_exists and not (target and not state.graph_exists):
            # 图已存在且无明确重建指令，默认进入查询模式
            state.stage = 'QUERY'
        else:
            state.stage = 'INIT'

        write_yaml_model(self.tools._state_fname(), state)

        user_input = self._build_initial_user_input()
        round_prompt = render_prompt(
            KG_ROUND_PROMPT,
            state_json=json_dump_model(state),
            user_input=user_input,
            tool_results_json='无',
        )
        msgs = [
            {'role': 'system', 'content': KG_SYSTEM_PROMPT + self._skill_catalog()},
            {'role': 'user', 'content': round_prompt},
        ]
        msgs[1]['content'] += (
            f'\n\n（本次运行步数上限：{self.max_steps} 轮。'
            '接近上限时应优先收敛并调用 tool_finish。）'
        )

        final: KgFinalReport = self.llm.call_llm_with_toolcall_retry(
            msgs, self.args.model,
            self.tools.get_tool_defs(),
            self.tools.get_tool_dict(),
            tool_finish_name='tool_finish',
            history_fname=path.join(self.pj_dir, 'history.yaml'),
            retry=self.args.retry,
            temp=self.args.temp,
            top_p=self.args.top_p,
            frequency_penalty=self.args.frequency_penalty,
            presence_penalty=self.args.presence_penalty,
            max_tokens=self.args.max_tokens,
            extra_body=self.args.extra_body,
            parse_output=parse_final,
        )

        # 兜底：推理模型可能把最终 JSON 放 reasoning_content
        if (getattr(final, 'status', '') == 'needs_approval'
                and getattr(final, 'stage', '') == 'UNKNOWN'):
            try:
                final = self.tools.tool_rebuild_report()
            except Exception as ex:
                logger.warning('重建报告失败：%s', ex)

        report_path = path.join(self.pj_dir, 'final_report.md')
        write_text(report_path, json_dump_model(final))
        logger.info('kg 流程结束：%s | 阶段：%s', final.status, final.stage)
        logger.info('最终报告已写入：%s', report_path)
        print(json_dump_model(final))
        return final


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
    """入口函数：创建编排器并运行。"""
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
        help='知识图谱构建与查询（graphify 技能工具 + 开放工具调用循环）',
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
    # 目标与基础参数
    p.add_argument(
        '-T', '--target', default='',
        help='目标路径（本地目录或 GitHub URL），缺省当前目录',
    )
    p.add_argument(
        '-g', '--goal', default='',
        help='任务目标描述（如：构建完整知识图谱并导出 HTML）',
    )
    p.add_argument(
        '-p', '--project', default='kg_project',
        help='项目工作目录（也作恢复断点用）',
    )
    p.add_argument(
        '-ms', '--max-steps', type=int, default=80,
        help='本轮循环步数上限（默认 80）',
    )

    # 运行模式
    p.add_argument(
        '--mode', choices=['deep', 'update', 'cluster-only', 'force', 'directed'],
        default='', help='运行模式：deep(深度)/update(增量)/cluster-only(仅聚类)/force(强制重建)/directed(有向图)',
    )
    p.add_argument(
        '--whisper-model', default='base',
        help='Whisper 模型（视频转录用）：tiny/base/small/medium/large',
    )

    # 查询相关
    p.add_argument(
        '-q', '--query', default='',
        help='自然语言查询问题（图谱存在时直接查询）',
    )
    p.add_argument(
        '--dfs', action='store_true',
        help='查询时使用 DFS（默认 BFS）',
    )
    p.add_argument(
        '--budget', type=int, default=1500,
        help='查询回答 Token 预算上限',
    )
    p.add_argument(
        '--path-from', default='',
        help='最短路径查询起点节点名',
    )
    p.add_argument(
        '--path-to', default='',
        help='最短路径查询终点节点名',
    )
    p.add_argument(
        '--explain', default='',
        help='解释指定节点',
    )

    # 添加 URL
    p.add_argument(
        '--add-url', default='',
        help='添加 URL 到语料库',
    )
    p.add_argument(
        '--author', default='',
        help='URL 作者',
    )
    p.add_argument(
        '--contributor', default='',
        help='URL 贡献者',
    )

    # 导出格式
    p.add_argument(
        '--html', action='store_true', help='生成 HTML 可视化（默认开启）',
    )
    p.add_argument(
        '--no-viz', action='store_true', help='跳过可视化生成',
    )
    p.add_argument(
        '--svg', action='store_true', help='生成 SVG',
    )
    p.add_argument(
        '--graphml', action='store_true', help='生成 GraphML',
    )
    p.add_argument(
        '--neo4j', action='store_true', help='生成 Neo4j Cypher',
    )
    p.add_argument(
        '--neo4j-push', default='', help='直接推送到 Neo4j (bolt://...)',
    )
    p.add_argument(
        '--falkordb', action='store_true', help='生成 FalkorDB Cypher',
    )
    p.add_argument(
        '--falkordb-push', default='', help='直接推送到 FalkorDB (falkordb://...)',
    )
    p.add_argument(
        '--wiki', action='store_true', help='生成 Wiki',
    )
    p.add_argument(
        '--obsidian', action='store_true', help='生成 Obsidian Vault',
    )
    p.add_argument(
        '--obsidian-dir', default='', help='Obsidian 保存目录',
    )
    p.add_argument(
        '--mcp', action='store_true', help='启动 MCP stdio 服务器',
    )
    p.add_argument(
        '--watch', action='store_true', help='监控文件变化自动重建',
    )
    p.add_argument(
        '--no-cluster', action='store_true', help='跳过聚类步骤',
    )

    # 其他
    p.add_argument(
        '-l', '--list-skills', action='store_true',
        help='列出全部内置 kg 技能并退出',
    )
    p.add_argument(
        '--authorized', action='store_true',
        help='确认已获得授权（涉及外部写入时需要）',
    )
    p.add_argument(
        '-D', '--debug', action='store_true',
        help='调试模式',
    )
    p.set_defaults(func=kg_handle)


# 导入 argparse（放在最后避免循环导入）
import argparse