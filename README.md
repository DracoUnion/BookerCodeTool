# BookerCodeTool

iBooker/DracoUnion 代码相关大模型 Agent 工具。

## 安装

```bash
pip install -e .
# 或手动安装依赖
pip install -r requirements.txt
```

## 子命令

| 命令 | 说明 |
| --- | --- |
| `doc` | 为代码添加注释（源自 `code2doc`） |
| `book` | 代码 → 书籍（源自 `code2book`） |
| `check-book` | 代码 → 书籍校验器（源自 `code2book-check`） |
| `re` | 逆向工程智能体（IDA / x64dbg / x32dbg + 基础工具） |

## 用法

```bash
# 查看帮助
BookerCodeTool -h
BookerCodeTool doc -h
BookerCodeTool book -h
BookerCodeTool check-book -h
BookerCodeTool re -h

# 为代码添加注释
BookerCodeTool doc 文件名或目录名

# 代码 → 书籍
BookerCodeTool book 项目目录名

# 校验生成的书籍
BookerCodeTool check-book 以 _book 结尾的项目目录名

# 逆向工程
BookerCodeTool re -g "定位某个函数" -p re_project

# 检查逆向环境（IDA / x64dbg）是否可用
BookerCodeTool re -ca
```

## 通用参数

所有子命令共享以下全局参数（来自 `OPENAI_*` 环境变量或显式传入）：

```
-m, --model            模型名称（默认 $OPENAI_CHAT_MODEL / gpt-3.5-turbo）
-k, --key              OpenAI API Key（默认 $OPENAI_API_KEY）
-r, --retry            重试次数（默认 1000000）
-tm, --temp            温度（默认 1）
-tp, --top-p           top-p
-fp, --frequency-penalty  频率惩罚
-pp, --presence-penalty   存在惩罚
-mt, --max-tokens      最大 token 数
-H, --host             API 地址（默认 $OPENAI_BASE_URL）
--emb                  Embedding 模型路径
-vm, --vmodel          视觉模型名称（默认 $OPENAI_VIS_MODEL）
-im, --tti-model       文生图模型名称（默认 $OPENAI_TTI_MODEL）
-ua, --user-agent      HTTP User-Agent 请求头
-ST, --no-stream      关闭流式输出模式
-eb, --extra-body      额外请求体
-ct, --conn-timeout    连接超时秒数（默认 60）
-rt, --read-timeout    读取超时秒数（默认 120）
-rr, --repetition-regex 重复检测正则
-TN, --no-think        关闭思考模式
-v,  --version         打印版本
```

## 开发

参照 `AGENTS.md` 添加子命令。

## License

SATA