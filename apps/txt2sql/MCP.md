# 使用 TXT2SQL MCP 与配套 SKILL

提供本地 stdio 服务，供支持 MCP 的客户端启动。复用 LangGraph 问数、授权表检查、只读执行、知识目录和审计。
本实现固定使用官方 Python MCP SDK `1.26.0` 的 FastMCP 接口；不是面向 SDK 2.x 的示例。

## 安装与启动

在仓库根目录执行，独立虚拟环境可以避免影响其他 Python 项目：

```powershell
python -m venv apps/txt2sql/.venv
apps/txt2sql/.venv/Scripts/python -m pip install -r apps/txt2sql/requirements-mcp.txt
```

客户端负责启动服务，一般不需要在终端单独保持服务运行。直接运行以下命令会等待 stdin 上的 MCP 协议消息，而不是启动网页：

```powershell
apps/txt2sql/.venv/Scripts/python apps/txt2sql/mcp_server.py
```

默认目录为 `apps/txt2sql/data/mcp`，首次运行创建合成演示数据库、检查点、审计及知识目录。
可以由本地操作员通过 `--data-dir` 指定另一个目录；该参数不暴露为 MCP 工具参数。
它仍然创建并使用合成演示库，不是任意生产数据库连接功能。
网页服务默认使用 `apps/txt2sql/data`，两者默认分开。同一 MCP 目录不要同时启动多个服务进程。

无密钥时使用有效示例问题。自由问数需要在服务进程环境配置 `TXT2SQL_API_KEY`、`TXT2SQL_MODEL_URL` 和 `TXT2SQL_MODEL`，详见应用 README。
密钥放入客户端本地安全配置或启动环境，不提交到 Git。模型解读会发送最多前 20 行结果到配置的模型服务。

## 客户端配置

[mcp-client.example.json](mcp-client.example.json) 为通用 `mcpServers` 配置片段。
将其中 `C:/path/to/txt2sql` 替换为本机真实仓库路径，使用绝对解释器和脚本路径；不用依赖客户端工作目录。
Linux/macOS 的解释器路径改为 `apps/txt2sql/.venv/bin/python`。

不同客户端使用的配置文件位置和顶层字段可能不同，应将 `command`、`args` 映射到客户端的 stdio MCP 配置。
本仓库不会自动修改全局客户端配置。服务不监听网络端口，stdout 只用于 MCP JSON-RPC，日志写 stderr。

## 工具约定

| 工具 | 输入 | 行为及主要输出 |
| --- | --- | --- |
| `get_data_catalog` | 无 | 返回授权 Schema、有效口径、示例问题、行数限制及本地使用范围；刷新本地 Schema 快照，不读取业务数据行 |
| `ask_data` | `question` | 同步执行一次查询流程；返回终态或追问，成功时包含表格、口径、SQL、知识来源和会话编号 |
| `resume_question` | `thread_id`、`answer` | 仅恢复等待用户回答的会话；其他状态拒绝恢复 |
| `get_query_status` | `thread_id` | 从检查点读取已有追问、结果或失败信息；不执行 SQL，不推进状态图 |

`ask_data` 与 `resume_question` 会写本地检查点及审计，可能调用外部模型。
因此它们标记为非幂等工具；虽然业务数据库只读，也不把整个工具标记为无副作用读取。
`get_query_status` 为只读、幂等工具。`get_data_catalog` 刷新本地快照，声明有本地写入副作用。

业务响应状态包括：

- `interrupted`：显示 `clarification`，保留 `thread_id`，等待用户实际回答。
- `completed`：展示 `columns`、`rows`、`definition`、`warnings`，说明 `truncated` 限制。
- `failed`、`rejected`：展示原因，停止当前自动尝试，不携带之前失败查询的结果。
- `incomplete`：状态读取发现上次流程异常中止，没有后台任务在继续运行，用户需重新发起查询。

业务失败作为结构化响应中的 `status`/`error` 返回，不等同于 MCP 协议错误。
查询调用是同步的，没有后台任务队列，无需持续轮询。客户端可适当配置工具超时，
模型节点各有 40 秒 HTTP 超时，整个流程可能包含多次模型调用，超时后避免盲目重复提交。
若已取得会话编号，先读取状态；若没有收到编号，当前未提供请求幂等键来定位该次调用。

## 分发与使用 SKILL

配套文件位于仓库 [`skills/txt2sql/SKILL.md`](../../skills/txt2sql/SKILL.md)。
它指导 Agent 发现 MCP 工具、查看目录、追问恢复、解释结果和处理拒绝。
用户可以复制整个 `skills/txt2sql` 文件夹到其客户端支持的技能目录，或按客户端方式加载该技能。
无需安装到开发者个人全局目录；本次只提供仓库内分发文件。

SKILL 和 MCP 需分别连接/加载。仅安装 SKILL 不会安装 Python 依赖、启动服务或授予数据库访问权限。
只支持 MCP、不支持 SKILL 的客户端，仍可通过工具的 schema、描述和服务 instructions 使用服务。
配套 SKILL 对 Codex 形式进行了结构验证，其他客户端的技能加载能力需要各自确认。

## 权限与本地使用边界

- 仅提供四个问数工具，不提供任意 SQL 执行、数据库写入、知识审批或修改授权的工具。
- 固定授权 `orders`，所有 SQL 仍经过 AST 校验及数据库只读授权回调；SKILL 不能绕过它们。
- 这是一个本地操作系统用户的服务实例，不提供用户登录、OAuth、租户隔离或远程共享。
- 拥有同一数据目录访问权限的人可以读取其中的会话；会话编号不是身份凭证。
- 不要把本入口包成多人远程服务。远程版本需要先增加身份认证、会话归属、数据权限和结果隔离。
- 共享给他人的是代码、配置模板和 SKILL。不要共享密钥、数据库、检查点或审计文件。
- 默认数据目录下的人工知识审批可使用管理 CLI 的 `--database` 和 `--catalog` 指向 MCP 对应目录，由操作员本地执行。

## 验证

安装 `requirements-mcp.txt` 后执行：

```powershell
apps/txt2sql/.venv/Scripts/python -m unittest discover -s apps/txt2sql -p "test_*.py" -v
```

协议测试通过真实 stdio 子进程验证初始化、工具发现、查询、状态读取、无效会话、终态恢复拒绝和重启后的追问恢复。
仅安装基础依赖时，MCP 可选测试会被标记跳过；CI 安装 MCP 依赖后执行它。
测试使用合成数据且不调用真实模型，尚未在具体桌面客户端中手动验证加载体验。

官方参考：[Python SDK v1.26.0](https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0)。
