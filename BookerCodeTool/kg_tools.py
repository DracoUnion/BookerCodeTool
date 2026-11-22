# -*- coding: utf-8 -*-
"""kg 子命令工具集：graphify 功能内联实现，不调用 CLI。

采用开放工具调用循环：模型每轮决定调用哪个工具，编排器用 call_llm_with_toolcall_retry 驱动循环直到模型调用 tool_finish。
"""

import json
import logging
import os
import re
import tempfile
from os import path
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel

from .util import (
    gen_objs_md5, read_yaml_model, write_yaml_model, read_text, write_text,
    json_dump_model, ext_code_block,
)
from .openai import *
from .kg_models import *
from .kg_pmt import *
from .kg_pmt import _JSON_OUT as KG_JSON_OUT

logger = logging.getLogger(__name__)

# 跳过目录
_SKIP_DIRS = {'__pycache__', '.git', '.idea', '.vscode', 'graphify-out', 'build', 'dist'}


def _norm_safe(name: str) -> str:
    """把名称转为合法下划线标识符片段。"""
    s = re.sub(r'[^\w]', '_', name)
    s = re.sub(r'_+', '_', s).strip('_')
    return s or 'skill'


def _pathmatch(pattern: str, rel: str) -> bool:
    """简易 glob 匹配。"""
    if not pattern:
        return True
    p = pattern.replace('\\', '/').lower()
    r = rel.replace('\\', '/').lower()
    parts = []
    for ch in p:
        if ch == '*':
            parts.append('.*')
        elif ch == '?':
            parts.append('.')
        else:
            parts.append(re.escape(ch))
    return bool(re.match('^' + ''.join(parts) + '$', r))


# ═══════════════════════════════════════════════════════════════════
# kg 技能步骤规格：按 graphify 工作流拆分
# ═══════════════════════════════════════════════════════════════════

_KG_SKILL_STEPS: Dict[str, Dict[str, Any]] = {
    'kg': {
        'description': '知识图谱构建与查询：文件检测、实体抽取、图构建、社区检测、自然语言查询、多格式导出',
        'steps': [
            {'key': 'init', 'title': '初始化与目标确认', 'doc': KG_INIT_PMT},
            {'key': 'detect', 'title': '文件检测与分类', 'doc': KG_DETECT_PMT},
            {'key': 'extract_ast', 'title': 'AST 结构化抽取', 'doc': KG_EXTRACT_AST_PMT},
            {'key': 'extract_semantic', 'title': '语义抽取（文档/论文/图片）', 'doc': KG_EXTRACT_SEMANTIC_PMT},
            {'key': 'merge_extract', 'title': '合并 AST 与语义结果', 'doc': KG_MERGE_EXTRACT_PMT},
            {'key': 'build_graph', 'title': '图构建、聚类与分析', 'doc': KG_BUILD_GRAPH_PMT},
            {'key': 'label_communities', 'title': '社区标注与报告更新', 'doc': KG_LABEL_COMMUNITIES_PMT},
            {'key': 'export', 'title': '多格式导出', 'doc': KG_EXPORT_PMT},
            {'key': 'query', 'title': '自然语言查询', 'doc': KG_QUERY_PMT},
            {'key': 'path', 'title': '最短路径查询', 'doc': KG_PATH_PMT},
            {'key': 'explain', 'title': '节点解释', 'doc': KG_EXPLAIN_PMT},
            {'key': 'add_url', 'title': '添加 URL 到语料库', 'doc': KG_ADD_URL_PMT},
            {'key': 'transcribe', 'title': '视频/音频转录', 'doc': KG_TRANSCRIBE_PMT},
            {'key': 'watch', 'title': '文件监控自动重建', 'doc': KG_WATCH_PMT},
        ],
    },
}


def _step_tool_name(skill_name: str, step_key: str) -> str:
    """步骤工具名：tool_<技能下划线名>_<步骤名>。"""
    return f'tool_{_norm_safe(skill_name)}_{step_key}'


def builtin_skills() -> List[SkillInfo]:
    """返回内置技能元信息。"""
    result = []
    for name, spec in _KG_SKILL_STEPS.items():
        slug = _norm_safe(name)
        result.append(SkillInfo(
            name=name,
            slug=slug,
            description=spec['description'],
            dir='',
            tool_name='',
            steps=[s['key'] for s in spec['steps']],
        ))
    return result


def step_tool_map() -> Dict[str, Dict[str, str]]:
    """返回 {技能名：{步骤名：工具名}}。"""
    return {
        name: {s['key']: _step_tool_name(name, s['key']) for s in spec['steps']}
        for name, spec in _KG_SKILL_STEPS.items()
    }


# 每个步骤工具的统一参数 Schema
_STEP_TOOL_PARAMS: Dict[str, Dict[str, Any]] = {
    _step_tool_name(name, step['key']): params_schema(
        target=base_schema('string', '目标路径或 GitHub URL'),
        task=base_schema('string', '本步骤要完成的具体任务'),
        context=base_schema('string', '已有上下文或前序步骤结果摘要'),
    )
    for name, spec in _KG_SKILL_STEPS.items()
    for step in spec['steps']
}


class KgAgent(ToolsMixin):
    """kg 子命令工具集。

    每个工作流步骤注册为独立工具（tool_kg_<步骤>），步骤指南带文件缓存。
    直接调用 graphify Python 模块，不通过 CLI。
    """

    pj_dir = '.'

    def __init__(self, args):
        super(ToolsMixin, self).__init__()
        self.llm = OpenAIClient.from_args(args)
        self.args = args
        self.model = getattr(args, 'model', '')
        self.retry = getattr(args, 'retry', 3)
        self.temperature = getattr(args, 'temp', 0.0)
        self.max_tokens = getattr(args, 'max_tokens', 4000)
        self.stream = not getattr(args, 'no_stream', False)
        # 项目目录：target 目录后加 _kg
        target = getattr(args, 'target', '') or '.'
        if target.startswith('https://github.com/') or target.startswith('http://github.com/'):
            # GitHub URL，使用仓库名
            repo_name = target.rstrip('/').split('/')[-1].replace('.git', '')
            self.project_id = f'{repo_name}_kg'
        else:
            # 本地路径，使用目录名加 _kg
            target_path = path.abspath(target)
            dir_name = path.basename(target_path)
            self.project_id = f'{dir_name}_kg'
        self.pj_dir = path.abspath(self.project_id)
        os.makedirs(self.pj_dir, exist_ok=True)

        self._step_tool_names: List[str] = []
        self._register_step_tools()

        self.skills: List[SkillInfo] = builtin_skills()
        logger.info('kg 内置技能：%d 个，步骤工具：%d 个', len(self.skills), len(self._step_tool_names))

    # ══════════════════════════════════════════════════════════════════
    # 技能步骤工具（动态注册）
    # ══════════════════════════════════════════════════════════════════

    def _register_step_tools(self) -> None:
        for name, spec in _KG_SKILL_STEPS.items():
            for step in spec['steps']:
                meth = _step_tool_name(name, step['key'])
                if hasattr(self, meth):
                    continue
                setattr(self, meth, self._make_step_wrapper(name, step, meth))
                self._step_tool_names.append(meth)

    def _make_step_wrapper(self, skill: str, step: Dict[str, str], meth_name: str):
        def _step_tool(target: str = '', task: str = '', context: str = ''):
            return self._run_step(skill, step, target, task, context)
        _step_tool.__name__ = meth_name
        _step_tool.__doc__ = f'{skill} · {step["title"]}：生成针对当前目标的本步骤行动指南。'
        return _step_tool

    def _run_step(self, skill: str, step: Dict[str, str], target: str = '',
                  task: str = '', context: str = '') -> KgStepGuide:
        """按该步骤的内置技能知识生成结构化行动指南（带文件缓存）。"""
        spec = _KG_SKILL_STEPS[skill]
        # 渲染提示词
        user = self._render(
            step['doc'],
            target=target or '（未指定）',
            task=task or '（按该步骤默认流程）',
            context=context or '（无）',
            scan_root=self._get_scan_root(target),
            pj_dir=self.pj_dir,
            mode=getattr(self.args, 'mode', ''),
            deep_mode='true' if getattr(self.args, 'mode', '') == 'deep' else 'false',
            has_gemini=bool(os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')),
            code_files=self._get_code_files(),
            semantic_files=self._get_semantic_files(),
            directed='true' if getattr(self.args, 'mode', '') == 'directed' else 'false',
            force='true' if getattr(self.args, 'mode', '') == 'force' else 'false',
            export_flags=json_dump_model(getattr(self.args, 'export', ExportFormat())),
            graphs_dir=path.join(self.pj_dir, 'graphify-out'),
            question=getattr(self.args, 'query', ''),
            dfs_mode='DFS' if getattr(self.args, 'dfs', False) else 'BFS',
            budget=getattr(self.args, 'budget', 1500),
            path_from=getattr(self.args, 'path_from', ''),
            path_to=getattr(self.args, 'path_to', ''),
            node_name=getattr(self.args, 'explain', ''),
            url=getattr(self.args, 'add_url', ''),
            author=getattr(self.args, 'author', ''),
            contributor=getattr(self.args, 'contributor', ''),
            video_files=self._get_video_files(),
            whisper_model=getattr(self.args, 'whisper_model', 'base'),
            watch_dir=self._get_scan_root(target),
            goal=getattr(self.args, 'goal', ''),
            authorized='true' if getattr(self.args, 'authorized', False) else 'false',
        )
        # 缓存文件名
        cache_fname = path.join(
            self.pj_dir,
            f't_kg_{step["key"]}_{gen_objs_md5(user)}.yaml')
        res = read_yaml_model(cache_fname, KgStepGuide)
        if not res:
            res = self.llm.call_llm_retry(
                user, self.model,
                retry=self.retry, temp=self.temperature,
                max_tokens=self.max_tokens,
                parse_output=self._parse_to(KgStepGuide))
            write_yaml_model(cache_fname, res)
        res.skill = skill
        res.step = step['key']
        return res

    def _get_scan_root(self, target: str) -> str:
        """获取扫描根目录。"""
        if not target:
            return self.pj_dir
        if target.startswith('https://github.com/') or target.startswith('http://github.com/'):
            # GitHub URL，返回克隆后的目录
            return path.join(self.pj_dir, 'repo')
        # 规范化路径，处理 Windows 路径
        target = target.replace('/', '\\')
        return path.abspath(target)

    def _get_code_files(self) -> List[str]:
        """从检测结果获取代码文件列表。"""
        detect_file = path.join(self.pj_dir, 'graphify-out', '.graphify_detect.json')
        if path.isfile(detect_file):
            try:
                data = json.loads(read_text(detect_file))
                return data.get('files', {}).get('code', [])
            except Exception:
                pass
        return []

    def _get_semantic_files(self) -> List[str]:
        """从检测结果获取语义类文件列表。"""
        detect_file = path.join(self.pj_dir, 'graphify-out', '.graphify_detect.json')
        if path.isfile(detect_file):
            try:
                data = json.loads(read_text(detect_file))
                files = []
                for cat in ('document', 'paper', 'image'):
                    files.extend(data.get('files', {}).get(cat, []))
                return files
            except Exception:
                pass
        return []

    def _get_video_files(self) -> List[str]:
        """从检测结果获取视频文件列表。"""
        detect_file = path.join(self.pj_dir, 'graphify-out', '.graphify_detect.json')
        if path.isfile(detect_file):
            try:
                data = json.loads(read_text(detect_file))
                return data.get('files', {}).get('video', [])
            except Exception:
                pass
        return []

    # ══════════════════════════════════════════════════════════════════
    # graphify 内联实现工具
    # ══════════════════════════════════════════════════════════════════

    def tool_kg_detect_files(self, scan_root: str = '') -> Dict[str, Any]:
        """检测目标目录下的文件，分类统计。内联 graphify.detect.detect。"""
        from graphify.detect import detect
        from graphify.paths import out_path
        from graphify.watch import _read_build_excludes, _read_build_gitignore
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            result = detect(
                Path(scan_root),
                extra_excludes=_read_build_excludes(out_path()),
                gitignore=_read_build_gitignore(out_path())
            )
            # 保存检测结果
            detect_file = out_dir / '.graphify_detect.json'
            detect_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f'检测完成：{result["total_files"]} 文件，{result["total_words"]} 字')
            return result
        except Exception as ex:
            logger.exception('文件检测失败')
            return {'error': str(ex)}

    def tool_kg_extract_ast(self, scan_root: str = '') -> Dict[str, Any]:
        """AST 结构化抽取代码文件。内联 graphify.extract.extract。"""
        from graphify.extract import collect_files, extract
        from graphify.detect import detect as detect_func
        from graphify.paths import out_path
        from graphify.watch import _read_build_excludes, _read_build_gitignore
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'
        out_dir.mkdir(parents=True, exist_ok=True)

        # 先检测获取代码文件
        det_result = detect_func(
            Path(scan_root),
            extra_excludes=_read_build_excludes(out_path()),
            gitignore=_read_build_gitignore(out_path())
        )
        code_files = []
        for f in det_result.get('files', {}).get('code', []):
            p = Path(f)
            if p.is_dir():
                code_files.extend(collect_files(p))
            else:
                code_files.append(p)

        if not code_files:
            result = {'nodes': [], 'edges': [], 'input_tokens': 0, 'output_tokens': 0}
            (out_dir / '.graphify_ast.json').write_text(
                json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return result

        try:
            result = extract(code_files, cache_root=Path(scan_root), root=Path(scan_root))
            (out_dir / '.graphify_ast.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.info(f'AST 抽取：{len(result["nodes"])} 节点，{len(result["edges"])} 边')
            return result
        except Exception as ex:
            logger.exception('AST 抽取失败')
            return {'error': str(ex)}

    def tool_kg_extract_semantic(self, scan_root: str = '') -> Dict[str, Any]:
        """语义抽取（文档/论文/图片）。内联 graphify.llm.extract_corpus_parallel 或子智能体流程。"""
        from graphify.detect import detect as detect_func
        from graphify.cache import check_semantic_cache, save_semantic_cache, drop_unreported_chunk_sources
        from graphify.paths import out_path
        from graphify.watch import _read_build_excludes, _read_build_gitignore
        from pathlib import Path
        import glob

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'
        out_dir.mkdir(parents=True, exist_ok=True)

        # 检测获取语义类文件
        det_result = detect_func(
            Path(scan_root),
            extra_excludes=_read_build_excludes(out_path()),
            gitignore=_read_build_gitignore(out_path())
        )
        all_files = []
        for cat in ('document', 'paper', 'image'):
            all_files.extend(det_result.get('files', {}).get(cat, []))

        if not all_files:
            # 无语义文件，写空结果
            result = {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0}
            (out_dir / '.graphify_semantic.json').write_text(
                json.dumps(result, ensure_ascii=False), encoding='utf-8')
            return result

        # 检查缓存
        spec_path = Path(__file__).parent.parent / 'references' / 'extraction-spec.md'
        if not spec_path.exists():
            spec_path = Path('C:/Users/Administrator/.claude/skills/graphify/references/extraction-spec.md')

        cached_nodes, cached_edges, cached_hyperedges, uncached = check_semantic_cache(
            all_files, root=Path(scan_root), prompt_file=str(spec_path))

        if cached_nodes or cached_edges or cached_hyperedges:
            (out_dir / '.graphify_cached.json').write_text(
                json.dumps({'nodes': cached_nodes, 'edges': cached_edges, 'hyperedges': cached_hyperedges}, ensure_ascii=False), encoding='utf-8')
        else:
            if (out_dir / '.graphify_cached.json').exists():
                (out_dir / '.graphify_cached.json').unlink()

        (out_dir / '.graphify_uncached.txt').write_text('\n'.join(uncached), encoding='utf-8')

        # 清理旧 chunk 文件
        for stale in out_dir.glob('.graphify_chunk_*.json'):
            stale.unlink()

        logger.info(f'语义缓存：{len(all_files)-len(uncached)} 命中，{len(uncached)} 需抽取')

        if not uncached:
            # 全部缓存命中
            cached = json.loads((out_dir / '.graphify_cached.json').read_text(encoding='utf-8'))
            (out_dir / '.graphify_semantic.json').write_text(
                json.dumps(cached, ensure_ascii=False, indent=2), encoding='utf-8')
            return cached

        # 有未缓存文件，需要语义抽取
        # 这里简化：如果有 Gemini API Key 则用 Gemini，否则返回空结果让模型知道需要 LLM
        if os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY'):
            # TODO: 实现 Gemini 并行抽取或子智能体分发
            # 暂时返回提示需要实现
            return {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0, 'note': '需要实现 Gemini 语义抽取'}
        else:
            # 无 LLM 后端，返回空结果
            result = {'nodes': [], 'edges': [], 'hyperedges': [], 'input_tokens': 0, 'output_tokens': 0}
            (out_dir / '.graphify_semantic.json').write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            logger.warning('无 LLM 后端（GEMINI_API_KEY/GOOGLE_API_KEY 未设置），跳过语义抽取')
            return result

    def tool_kg_merge_extract(self, scan_root: str = '') -> Dict[str, Any]:
        """合并 AST + 语义结果。内联 graphify 合并逻辑。"""
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'

        try:
            ast = json.loads((out_dir / '.graphify_ast.json').read_text(encoding='utf-8'))
            sem = json.loads((out_dir / '.graphify_semantic.json').read_text(encoding='utf-8'))

            # 标记语义侧来源
            for n in sem['nodes']:
                n.setdefault('_origin', 'semantic')
            for e in sem['edges']:
                e.setdefault('_origin', 'semantic')

            # 合并：AST 优先，语义去重
            seen = {n['id'] for n in ast['nodes']}
            merged_nodes = list(ast['nodes'])
            for n in sem['nodes']:
                if n['id'] not in seen:
                    merged_nodes.append(n)
                    seen.add(n['id'])

            merged_edges = ast['edges'] + sem['edges']
            merged_hyperedges = sem.get('hyperedges', [])

            merged = {
                'nodes': merged_nodes,
                'edges': merged_edges,
                'hyperedges': merged_hyperedges,
                'input_tokens': sem.get('input_tokens', 0),
                'output_tokens': sem.get('output_tokens', 0),
            }

            (out_dir / '.graphify_extract.json').write_text(
                json.dumps(merged, ensure_ascii=False, indent=2), encoding='utf-8')

            logger.info(f'合并完成：{len(merged_nodes)} 节点，{len(merged_edges)} 边 '
                       f'({len(ast["nodes"])} AST + {len(sem["nodes"])} semantic)')
            return merged
        except Exception as ex:
            logger.exception('合并失败')
            return {'error': str(ex)}

    def tool_kg_build_graph(self, scan_root: str = '', directed: bool = False, force: bool = False) -> Dict[str, Any]:
        """构建图、聚类、分析、生成报告。内联 graphify.build/cluster/analyze/report。"""
        from graphify.build import build_from_json
        from graphify.cluster import cluster, score_all
        from graphify.analyze import god_nodes, surprising_connections, suggest_questions
        from graphify.report import generate
        from graphify.export import to_json
        from graphify.diagnostics import diagnose_extraction, format_diagnostic_report
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            extraction = json.loads((out_dir / '.graphify_extract.json').read_text(encoding='utf-8'))
            detection = json.loads((out_dir / '.graphify_detect.json').read_text(encoding='utf-8'))

            # 构建图
            G = build_from_json(extraction, root=Path(scan_root), directed=directed)
            if G.number_of_nodes() == 0:
                return {'error': 'Graph is empty - extraction produced no nodes'}

            # 聚类
            communities = cluster(G)
            cohesion = score_all(G, communities)
            tokens = {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}
            gods = god_nodes(G)
            surprises = surprising_connections(G, communities)
            labels = {cid: f'Community {cid}' for cid in communities}
            questions = suggest_questions(G, communities, labels)

            # 导出 graph.json（force 控制收缩保护）
            graph_file = out_dir / 'graph.json'
            wrote = to_json(G, communities, str(graph_file), force=force)
            if not wrote:
                return {'error': 'refused to shrink graphify-out/graph.json (existing graph has more nodes; use force=True)'}

            # 生成报告
            report = generate(G, communities, cohesion, labels, gods, surprises, detection, tokens, Path(scan_root), suggested_questions=questions)
            (out_dir / 'GRAPH_REPORT.md').write_text(report, encoding='utf-8')

            # 保存分析结果
            analysis = {
                'communities': {str(k): v for k, v in communities.items()},
                'cohesion': {str(k): v for k, v in cohesion.items()},
                'gods': gods,
                'surprises': surprises,
                'questions': questions,
            }
            (out_dir / '.graphify_analysis.json').write_text(json.dumps(analysis, indent=2, ensure_ascii=False), encoding='utf-8')

            # 健康检查
            summary = diagnose_extraction(extraction, directed=directed, root=Path(scan_root))
            health_report = format_diagnostic_report(summary)
            logger.info(f'图健康检查：{health_report}')

            stats = {
                'nodes': G.number_of_nodes(),
                'edges': G.number_of_edges(),
                'communities': len(communities),
                'gods': len(gods),
                'surprises': len(surprises),
                'questions': len(questions),
            }
            logger.info(f'图构建完成：{stats}')
            return stats
        except Exception as ex:
            logger.exception('图构建失败')
            return {'error': str(ex)}

    def tool_kg_label_communities(self, scan_root: str = '', directed: bool = False) -> Dict[str, Any]:
        """社区标注与报告更新。内联 graphify.label 逻辑（需 LLM 后端）。"""
        from graphify.build import build_from_json
        from graphify.cluster import score_all
        from graphify.analyze import god_nodes, surprising_connections, suggest_questions
        from graphify.report import generate
        from graphify.export import to_json
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'

        try:
            extraction = json.loads((out_dir / '.graphify_extract.json').read_text(encoding='utf-8'))
            detection = json.loads((out_dir / '.graphify_detect.json').read_text(encoding='utf-8'))
            analysis = json.loads((out_dir / '.graphify_analysis.json').read_text(encoding='utf-8'))

            G = build_from_json(extraction, root=Path(scan_root), directed=directed)
            communities = {int(k): v for k, v in analysis['communities'].items()}
            cohesion = {int(k): v for k, v in analysis['cohesion'].items()}
            tokens = {'input': extraction.get('input_tokens', 0), 'output': extraction.get('output_tokens', 0)}

            # 这里需要 LLM 生成社区标签，暂时使用占位符
            # 实际应调用 LLM 为每个社区生成 2-5 词中文名
            labels = {cid: analysis.get('community_labels', {}).get(str(cid), f'Community {cid}') for cid in communities}

            # 重新生成问题
            questions = suggest_questions(G, communities, labels)

            # 更新报告
            report = generate(G, communities, cohesion, labels, analysis['gods'], analysis['surprises'], detection, tokens, Path(scan_root), suggested_questions=questions)
            (out_dir / 'GRAPH_REPORT.md').write_text(report, encoding='utf-8')

            # 保存标签
            (out_dir / '.graphify_labels.json').write_text(json.dumps({str(k): v for k, v in labels.items()}, ensure_ascii=False), encoding='utf-8')

            # 重新导出 graph.json 带 community_name
            graph_file = out_dir / 'graph.json'
            wrote = to_json(G, communities, str(graph_file), community_labels=labels)

            logger.info(f'社区标注完成：{len(labels)} 个社区')
            return {'labels': labels}
        except Exception as ex:
            logger.exception('社区标注失败')
            return {'error': str(ex)}

    def tool_kg_export(self, formats: List[str] = None, scan_root: str = '') -> Dict[str, Any]:
        """多格式导出。内联 graphify.export 逻辑。"""
        from graphify.export import to_html, to_svg, to_graphml, to_cypher, to_obsidian
        import networkx as nx
        import json
        from pathlib import Path

        scan_root = scan_root or self._get_scan_root(getattr(self.args, 'target', ''))
        out_dir = Path(self.pj_dir) / 'graphify-out'

        formats = formats or []
        export_args = getattr(self.args, 'export', ExportFormat())

        results = {}

        try:
            graph_file = out_dir / 'graph.json'
            if not graph_file.exists():
                return {'error': 'graph.json not found, need to build graph first'}

            # 加载图和社区
            graph_data = json.loads(graph_file.read_text(encoding='utf-8'))
            G = nx.node_link_graph(graph_data)
            communities = {int(k): v for k, v in graph_data.get('communities', {}).items()}
            community_labels = {int(k): v for k, v in graph_data.get('community_labels', {}).items()}
            cohesion = {int(k): float(v) for k, v in graph_data.get('cohesion', {}).items()}

            def try_export(fmt_name: str, export_func, *args, **kwargs):
                try:
                    export_func(*args, **kwargs)
                    return True
                except ValueError as ve:
                    logger.warning(f'{fmt_name} 导出失败: {ve}')
                    return False
                except Exception as ex:
                    logger.warning(f'{fmt_name} 导出异常: {ex}')
                    return False

            # HTML
            if export_args.html or 'html' in formats:
                html_path = str(out_dir / 'graph.html')
                if try_export('HTML', to_html, G, communities, html_path, community_labels=community_labels):
                    results['html'] = html_path

            # SVG
            if export_args.svg or 'svg' in formats:
                svg_path = str(out_dir / 'graph.svg')
                if try_export('SVG', to_svg, G, communities, svg_path, community_labels=community_labels):
                    results['svg'] = svg_path

            # GraphML
            if export_args.graphml or 'graphml' in formats:
                graphml_path = str(out_dir / 'graph.graphml')
                if try_export('GraphML', to_graphml, G, communities, graphml_path):
                    results['graphml'] = graphml_path

            # Neo4j
            if export_args.neo4j or 'neo4j' in formats:
                push_uri = export_args.neo4j_push or None
                cypher_path = str(out_dir / 'cypher.txt')
                if try_export('Neo4j', to_cypher, G, cypher_path):
                    results['neo4j'] = cypher_path if not push_uri else 'pushed'

            # FalkorDB
            if export_args.falkordb or 'falkordb' in formats:
                push_uri = export_args.falkordb_push or None
                cypher_path = str(out_dir / 'cypher_falkordb.txt')
                if try_export('FalkorDB', to_cypher, G, cypher_path):
                    results['falkordb'] = cypher_path if not push_uri else 'pushed'

            # Obsidian
            if export_args.obsidian or 'obsidian' in formats:
                obsidian_dir = export_args.obsidian_dir or str(out_dir / 'obsidian')
                if try_export('Obsidian', to_obsidian, G, communities, obsidian_dir, community_labels=community_labels, cohesion=cohesion):
                    results['obsidian'] = obsidian_dir

            logger.info(f'导出完成：{results}')
            return results
        except Exception as ex:
            logger.exception('导出失败')
            return {'error': str(ex)}

    def tool_kg_query(self, question: str = '', dfs: bool = False, budget: int = 1500, graphs_dir: str = '') -> Dict[str, Any]:
        """自然语言查询。内联 graphify.query 逻辑。"""
        from graphify.query import query_graph
        from pathlib import Path

        question = question or getattr(self.args, 'query', '')
        graphs_dir = graphs_dir or str(Path(self.pj_dir) / 'graphify-out')

        if not question:
            return {'error': 'query question is required'}

        graph_file = Path(graphs_dir) / 'graph.json'
        if not graph_file.exists():
            return {'error': 'graph.json not found, need to build graph first'}

        try:
            result = query_graph(str(graph_file), question, dfs=dfs, budget=budget)
            return result
        except Exception as ex:
            logger.exception('查询失败')
            return {'error': str(ex)}

    def tool_kg_path(self, path_from: str = '', path_to: str = '', graphs_dir: str = '') -> Dict[str, Any]:
        """最短路径查询。内联 graphify.path 逻辑。"""
        from graphify.query import path_query
        from pathlib import Path

        path_from = path_from or getattr(self.args, 'path_from', '')
        path_to = path_to or getattr(self.args, 'path_to', '')
        graphs_dir = graphs_dir or str(Path(self.pj_dir) / 'graphify-out')

        if not path_from or not path_to:
            return {'error': 'path_from and path_to are required'}

        graph_file = Path(graphs_dir) / 'graph.json'
        if not graph_file.exists():
            return {'error': 'graph.json not found, need to build graph first'}

        try:
            result = path_query(str(graph_file), path_from, path_to)
            return result
        except Exception as ex:
            logger.exception('路径查询失败')
            return {'error': str(ex)}

    def tool_kg_explain(self, node_name: str = '', graphs_dir: str = '') -> Dict[str, Any]:
        """节点解释。内联 graphify.explain 逻辑。"""
        from graphify.query import explain_node
        from pathlib import Path

        node_name = node_name or getattr(self.args, 'explain', '')
        graphs_dir = graphs_dir or str(Path(self.pj_dir) / 'graphify-out')

        if not node_name:
            return {'error': 'node_name is required'}

        graph_file = Path(graphs_dir) / 'graph.json'
        if not graph_file.exists():
            return {'error': 'graph.json not found, need to build graph first'}

        try:
            result = explain_node(str(graph_file), node_name)
            return result
        except Exception as ex:
            logger.exception('节点解释失败')
            return {'error': str(ex)}

    def tool_kg_add_url(self, url: str = '', author: str = '', contributor: str = '') -> Dict[str, Any]:
        """添加 URL 到语料库。内联 graphify.add 逻辑。"""
        from graphify.cli import add_url
        from pathlib import Path

        url = url or getattr(self.args, 'add_url', '')
        author = author or getattr(self.args, 'author', '')
        contributor = contributor or getattr(self.args, 'contributor', '')

        if not url:
            return {'error': 'url is required'}

        try:
            # graphify add 会下载到 raw/ 并触发增量更新
            saved_path = add_url(url, author=author, contributor=contributor, out_dir=Path(self.pj_dir))
            return {'url': url, 'saved_path': str(saved_path), 'author': author, 'contributor': contributor}
        except Exception as ex:
            logger.exception('添加 URL 失败')
            return {'error': str(ex)}

    def tool_kg_transcribe(self, video_files: List[str] = None, whisper_model: str = 'base') -> Dict[str, Any]:
        """视频/音频转录。内联 graphify 转录逻辑。"""
        from graphify.transcribe import transcribe_files
        from pathlib import Path

        video_files = video_files or self._get_video_files()
        if not video_files:
            return {'error': 'no video files to transcribe'}

        out_dir = Path(self.pj_dir) / 'graphify-out'
        transcript_dir = out_dir / 'transcripts'
        transcript_dir.mkdir(parents=True, exist_ok=True)

        try:
            results = transcribe_files(video_files, model=whisper_model, output_dir=transcript_dir)
            return {'transcripts': results}
        except Exception as ex:
            logger.exception('转录失败')
            return {'error': str(ex)}

    def tool_kg_watch(self, watch_dir: str = '') -> Dict[str, Any]:
        """文件监控自动重建。内联 graphify.watch 逻辑。"""
        from graphify.watch import watch_folder
        from pathlib import Path

        watch_dir = watch_dir or self._get_scan_root(getattr(self.args, 'target', ''))

        try:
            # watch 是长期运行的，这里只启动后台或返回启动命令
            # 实际使用时建议在单独进程运行
            return {'note': 'watch 模式需长期运行，建议在终端直接运行: python -m graphify watch <path>', 'command': f'python -m graphify watch "{watch_dir}"'}
        except Exception as ex:
            logger.exception('监控启动失败')
            return {'error': str(ex)}

    # ══════════════════════════════════════════════════════════════════
    # 技能元信息工具
    # ══════════════════════════════════════════════════════════════════

    def tool_list_skills(self) -> List[SkillInfo]:
        """列出全部内置 kg 技能及其步骤。"""
        return self.skills

    # ══════════════════════════════════════════════════════════════════
    # 基础工具
    # ══════════════════════════════════════════════════════════════════

    def _render(self, pmt: str, **kw) -> str:
        kw.setdefault('_JSON_OUT', KG_JSON_OUT)
        return pmt.format(**kw)

    @staticmethod
    def _parse_to(model: Type[BaseModel]) -> Callable:
        def f(s: str) -> BaseModel:
            t = s.strip()
            if t.startswith('```'):
                t = ext_code_block(t)
            return model.model_validate_json(t)
        return f

    def _state_fname(self) -> str:
        return path.join(self.pj_dir, 'kg_state.yaml')

    def tool_load_state(self) -> KgState:
        """读取当前 kg 任务状态机。"""
        st = read_yaml_model(self._state_fname(), KgState)
        if st:
            return st
        return KgState(
            project_id=self.project_id,
            target=getattr(self.args, 'target', '') or '',
            goal=getattr(self.args, 'goal', '') or '',
            authorized=bool(getattr(self.args, 'authorized', False)),
        )

    def tool_update_state(self, stage: str = '', target: str = '', goal: str = '',
                          authorized: Optional[bool] = None,
                          graph_exists: Optional[bool] = None,
                          last_query: str = '',
                          next_action: str = '',
                          assets: Optional[List[str]] = None,
                          findings: Optional[List[str]] = None,
                          notes: Optional[List[str]] = None) -> KgState:
        """更新任务状态机。"""
        st = self.tool_load_state()
        if stage:
            st.stage = stage
        if target:
            st.target = target
        if goal:
            st.goal = goal
        if authorized is not None:
            st.authorized = authorized
        if graph_exists is not None:
            st.graph_exists = graph_exists
        if last_query:
            st.last_query = last_query
        if next_action:
            st.next_action = next_action
        if assets:
            st.assets = list(dict.fromkeys(st.assets + assets))
        if findings:
            st.findings = list(dict.fromkeys(st.findings + findings))
        if notes:
            st.notes = list(dict.fromkeys(st.notes + notes))
        write_yaml_model(self._state_fname(), st)
        return st

    def tool_note(self, text: str) -> bool:
        """记录分析笔记到 notes.md（追加）。"""
        write_text(path.join(self.pj_dir, 'notes.md'), text + '\n', append=True)
        return True

    def tool_bash(self, command: str, timeout_sec: int = 120,
                  cwd: str = '') -> Dict[str, Any]:
        """在项目工作目录执行 shell 命令。"""
        cwd = path.abspath(cwd or self.pj_dir)
        os.makedirs(cwd, exist_ok=True)
        try:
            r = subprocess.run(
                command, shell=True, cwd=cwd, env=os.environ.copy(),
                capture_output=True, text=True,
                timeout=max(1, int(timeout_sec or 120)),
            )
            return {
                'returncode': r.returncode,
                'stdout': (r.stdout or '')[:20000],
                'stderr': (r.stderr or '')[:8000],
            }
        except subprocess.TimeoutExpired as ex:
            return {
                'returncode': -1,
                'stdout': (getattr(ex, 'stdout', b'') or b'').decode('utf8', 'replace')[:20000],
                'stderr': f'[超时 {timeout_sec}s]',
            }
        except Exception as ex:
            return {'returncode': -1, 'stdout': '', 'stderr': f'执行异常：{ex}'}

    def tool_readfile(self, fname: str, max_chars: int = 20000) -> str:
        """读取文件文本内容。"""
        fp = fname if path.isabs(fname) else path.join(self.pj_dir, fname)
        text = read_text(fp)
        if text is None:
            return f'文件不存在：{fp}'
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + f'\n...[截断，共 {len(text)} 字符]'
        return text

    def tool_writefile(self, fname: str, text: str, append: bool = False) -> bool:
        """写入文本文件。"""
        fp = fname if path.isabs(fname) else path.join(self.pj_dir, fname)
        write_text(fp, text, append)
        return True

    def tool_list_files(self, pattern: str = '') -> List[str]:
        """列出项目工作区文件。"""
        files = []
        for root, dirnames, fnames in os.walk(self.pj_dir):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for f in fnames:
                rel = path.relpath(path.join(root, f), self.pj_dir)
                if pattern and not _pathmatch(pattern, rel):
                    continue
                files.append(rel)
        return sorted(files)

    def tool_ask_user(self, question: str, options: Optional[List[str]] = None,
                      default: str = '') -> AskUserResult:
        """向用户提问并等待回答。"""
        options = list(options or [])
        print('=' * 60)
        print(f'【需要用户确认】{question}')
        for i, opt in enumerate(options, 1):
            print(f'  {i}. {opt}')
        if default:
            print(f'  默认：{default}')
        print('=' * 60)
        try:
            ans = input('你的回答> ').strip()
            non_interactive = False
            note = ''
        except EOFError:
            ans = default
            non_interactive = True
            note = '当前无法读取用户输入，已使用默认值；请按默认处理或自行决策。'
        if not ans:
            ans = default
        res = AskUserResult(
            question=question, answer=ans, options=options,
            non_interactive=non_interactive, note=note)
        write_text(
            path.join(self.pj_dir, 'ask_user.md'),
            f'## 问题\n{question}\n\n选项：{options}\n\n回答：{ans}\n'
            f'非交互：{non_interactive}\n\n',
            append=True)
        return res

    def tool_rebuild_report(self) -> KgFinalReport:
        """根据当前状态与笔记重建最终报告。"""
        st = self.tool_load_state()
        notes = read_text(path.join(self.pj_dir, 'notes.md')) or ''
        return self._call_llm(KG_REBUILD_REPORT_PMT, KgFinalReport,
                              state_json=json_dump_model(st), notes=notes)

    # ══════════════════════════════════════════════════════════════════
    # 工具参数 Schema
    # ══════════════════════════════════════════════════════════════════

    _TOOL_PARAMS: Dict[str, Dict[str, Any]] = {
        **ToolsMixin._TOOL_PARAMS,
        **_STEP_TOOL_PARAMS,
        # 技能元信息
        "tool_list_skills": params_schema(),
        # 基础工具
        "tool_bash": params_schema(
            required=['command'],
            command=base_schema('string', '要执行的 shell 命令'),
            timeout_sec=base_schema('integer', '超时秒数，默认 120'),
            cwd=base_schema('string', '工作目录，缺省项目工作区'),
        ),
        "tool_readfile": params_schema(
            required=['fname'],
            fname=base_schema('string', '文件路径（绝对或项目相对）'),
            max_chars=base_schema('integer', '最多返回字符数，默认 20000'),
        ),
        "tool_writefile": params_schema(
            required=['fname', 'text'],
            fname=base_schema('string', '文件路径（绝对或项目相对）'),
            text=base_schema('string', '要写入的文本内容'),
            append=base_schema('boolean', '是否追加'),
        ),
        "tool_list_files": params_schema(
            pattern=base_schema('string', 'glob 过滤，如 *.txt'),
        ),
        "tool_ask_user": params_schema(
            required=['question'],
            question=base_schema('string', '要询问用户的问题'),
            options=str_list_schema('可选项列表'),
            default=base_schema('string', '无法读取输入时使用的默认答案'),
        ),
        "tool_note": params_schema(
            required=['text'],
            text=base_schema('string', '分析笔记内容'),
        ),
        "tool_load_state": params_schema(),
        "tool_update_state": params_schema(
            stage=base_schema('string', '当前阶段 INIT/DETECT/EXTRACT/BUILD/QUERY/ADD/WATCH/DONE'),
            target=base_schema('string', '目标路径或 URL'),
            goal=base_schema('string', '任务目标描述'),
            authorized=base_schema('boolean', '是否已确认授权'),
            graph_exists=base_schema('boolean', 'graph.json 是否已存在'),
            last_query=base_schema('string', '最后一次查询'),
            next_action=base_schema('string', '下一步动作'),
            assets=str_list_schema('新增资产'),
            findings=str_list_schema('新增发现摘要'),
            notes=str_list_schema('新增笔记'),
        ),
        "tool_rebuild_report": params_schema(),
        # kg 内联实现工具
        "tool_kg_detect_files": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
        ),
        "tool_kg_extract_ast": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
        ),
        "tool_kg_extract_semantic": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
        ),
        "tool_kg_merge_extract": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
        ),
        "tool_kg_build_graph": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
            directed=base_schema('boolean', '是否有向图'),
            force=base_schema('boolean', '是否强制重建（允许收缩）'),
        ),
        "tool_kg_label_communities": params_schema(
            scan_root=base_schema('string', '扫描根目录'),
            directed=base_schema('boolean', '是否有向图'),
        ),
        "tool_kg_export": params_schema(
            formats=str_list_schema('导出格式列表'),
            scan_root=base_schema('string', '扫描根目录'),
        ),
        "tool_kg_query": params_schema(
            question=base_schema('string', '查询问题'),
            dfs=base_schema('boolean', '是否使用 DFS'),
            budget=base_schema('integer', 'Token 预算'),
            graphs_dir=base_schema('string', 'graphify-out 目录'),
        ),
        "tool_kg_path": params_schema(
            path_from=base_schema('string', '起点节点名'),
            path_to=base_schema('string', '终点节点名'),
            graphs_dir=base_schema('string', 'graphify-out 目录'),
        ),
        "tool_kg_explain": params_schema(
            node_name=base_schema('string', '目标节点名'),
            graphs_dir=base_schema('string', 'graphify-out 目录'),
        ),
        "tool_kg_add_url": params_schema(
            url=base_schema('string', '要添加的 URL'),
            author=base_schema('string', '作者'),
            contributor=base_schema('string', '贡献者'),
        ),
        "tool_kg_transcribe": params_schema(
            video_files=str_list_schema('视频文件列表'),
            whisper_model=base_schema('string', 'Whisper 模型'),
        ),
        "tool_kg_watch": params_schema(
            watch_dir=base_schema('string', '监控目录'),
        ),
    }

    def _call_llm(self, pmt: str, out_model: Type[BaseModel], *,
                  max_tokens: Optional[int] = None, **kw) -> BaseModel:
        """调用 LLM 生成结构化结果，带文件缓存。"""
        user = self._render(pmt, **kw)
        cache_fname = path.join(
            self.pj_dir, f't_{out_model.__name__.lower()}_{gen_objs_md5(user)}.yaml')
        r = read_yaml_model(cache_fname, out_model)
        if r:
            return r
        mt = max_tokens if max_tokens is not None else self.max_tokens
        res = self.llm.call_llm_retry(
            user, self.model,
            retry=self.retry, temp=self.temperature,
            max_tokens=mt,
            parse_output=self._parse_to(out_model))
        write_yaml_model(cache_fname, res)
        return res