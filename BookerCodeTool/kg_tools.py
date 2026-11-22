# -*- coding: utf-8 -*-
"""kg 子命令工具集：graphify 技能按工作流拆分的步骤工具 + 基础工具。

采用开放工具调用循环：模型每轮决定调用哪个工具，编排器用 call_llm_with_toolcall_retry 驱动循环直到模型调用 tool_finish。
"""

import json
import logging
import os
import re
import subprocess
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


# ══════════════════════════════════════════════════════════════════
# kg 技能步骤规格：按 graphify 工作流拆分
# ══════════════════════════════════════════════════════════════════

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


class KgTools(ToolsMixin):
    """kg 子命令工具集。

    每个工作流步骤注册为独立工具（tool_kg_<步骤>），步骤指南带文件缓存。
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
        self.project_id = getattr(args, 'project', None) or 'kg_project'
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
    # kg 专用便捷工具
    # ══════════════════════════════════════════════════════════════════

    def tool_kg_clone_github(self, url: str, branch: str = '') -> Dict[str, Any]:
        """克隆 GitHub 仓库到项目目录。"""
        repo_dir = path.join(self.pj_dir, 'repo')
        if path.exists(repo_dir):
            # 已存在，尝试拉取更新
            cmd = f'cd "{repo_dir}" && git fetch --all && git reset --hard origin/{branch or "main"}'
        else:
            cmd = f'git clone {"-b " + branch if branch else ""} "{url}" "{repo_dir}"'
        return self.tool_bash(cmd, timeout_sec=300)

    def tool_kg_check_graphify_installed(self) -> Dict[str, Any]:
        """检查 graphifyy 是否已安装，返回可用的 python 解释器路径。"""
        # 尝试多种安装方式
        python_paths = []
        # 1. uv tool
        try:
            result = subprocess.run(['uv', 'tool', 'dir'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                uv_dir = result.stdout.strip()
                py = path.join(uv_dir, 'graphifyy', 'Scripts', 'python.exe')
                if path.exists(py):
                    python_paths.append(py)
        except Exception:
            pass
        # 2. pipx
        try:
            result = subprocess.run(['pipx', 'environment', '--value', 'PIPX_LOCAL_VENVS'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                venvs = result.stdout.strip()
                py = path.join(venvs, 'graphifyy', 'Scripts', 'python.exe')
                if path.exists(py):
                    python_paths.append(py)
        except Exception:
            pass
        # 3. 当前环境
        py = subprocess.run(['python', '-c', 'import graphify; import sys; print(sys.executable)'], capture_output=True, text=True, timeout=10)
        if py.returncode == 0:
            python_paths.append(py.stdout.strip())

        if python_paths:
            # 保存解释器路径
            py_path = path.join(self.pj_dir, 'graphify-out', '.graphify_python')
            os.makedirs(path.dirname(py_path), exist_ok=True)
            write_text(py_path, python_paths[0])
            return {'installed': True, 'python': python_paths[0]}
        return {'installed': False}

    def tool_kg_install_graphify(self) -> Dict[str, Any]:
        """安装 graphifyy。"""
        try:
            # 优先用 uv
            result = subprocess.run(['uv', 'tool', 'install', '--upgrade', 'graphifyy', '-q'], capture_output=True, text=True, timeout=180)
            if result.returncode == 0:
                return self.tool_kg_check_graphify_installed()
            # 回退 pip
            result = subprocess.run(['pip', 'install', 'graphifyy', '-q'], capture_output=True, text=True, timeout=180)
            if result.returncode == 0:
                return self.tool_kg_check_graphify_installed()
            return {'installed': False, 'error': result.stderr}
        except Exception as ex:
            return {'installed': False, 'error': str(ex)}

    def tool_kg_run_graphify_cmd(self, args: List[str], cwd: str = '') -> Dict[str, Any]:
        """运行 graphify 命令（通过保存的 python 解释器）。"""
        py_file = path.join(self.pj_dir, 'graphify-out', '.graphify_python')
        if not path.isfile(py_file):
            check = self.tool_kg_check_graphify_installed()
            if not check.get('installed'):
                inst = self.tool_kg_install_graphify()
                if not inst.get('installed'):
                    return {'returncode': -1, 'stdout': '', 'stderr': 'graphifyy 未安装且自动安装失败'}
        python = read_text(py_file).strip() if path.isfile(py_file) else 'python'
        cmd = f'"{python}" -m graphify {" ".join(args)}'
        return self.tool_bash(cmd, timeout_sec=600, cwd=cwd or self.pj_dir)

    def tool_kg_read_detect(self) -> Optional[KgDetectResult]:
        """读取检测结果。"""
        f = path.join(self.pj_dir, 'graphify-out', '.graphify_detect.json')
        if path.isfile(f):
            try:
                return KgDetectResult(**json.loads(read_text(f)))
            except Exception:
                pass
        return None

    def tool_kg_read_extract(self) -> Optional[KgExtractResult]:
        """读取抽取结果。"""
        f = path.join(self.pj_dir, 'graphify-out', '.graphify_extract.json')
        if path.isfile(f):
            try:
                return KgExtractResult(**json.loads(read_text(f)))
            except Exception:
                pass
        return None

    def tool_kg_read_graph_stats(self) -> Dict[str, Any]:
        """读取图统计信息。"""
        graph_file = path.join(self.pj_dir, 'graphify-out', 'graph.json')
        if path.isfile(graph_file):
            try:
                data = json.loads(read_text(graph_file))
                return {
                    'nodes': data.get('node_count', 0),
                    'edges': data.get('edge_count', 0),
                    'communities': data.get('community_count', 0),
                }
            except Exception:
                pass
        return {'nodes': 0, 'edges': 0, 'communities': 0}

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
        # kg 专用便捷工具
        "tool_kg_clone_github": params_schema(
            required=['url'],
            url=base_schema('string', 'GitHub 仓库 URL'),
            branch=base_schema('string', '分支名，缺省 main'),
        ),
        "tool_kg_check_graphify_installed": params_schema(),
        "tool_kg_install_graphify": params_schema(),
        "tool_kg_run_graphify_cmd": params_schema(
            required=['args'],
            args=base_schema('array', 'graphify 子命令参数列表', items={'type': 'string'}),
            cwd=base_schema('string', '工作目录'),
        ),
        "tool_kg_read_detect": params_schema(),
        "tool_kg_read_extract": params_schema(),
        "tool_kg_read_graph_stats": params_schema(),
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