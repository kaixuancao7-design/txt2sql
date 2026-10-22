# TXT2SQL LangGraph 自助取数 Agent

独立的 LangGraph 应用，使用 sqlglot 进行 SQL AST 校验，SQLite 保存演示数据、检查点和审计记录。

## 启动

需要 Python 3.11+，建议使用虚拟环境。仓库根目录执行：

```powershell
python -m venv apps/txt2sql/.venv
apps/txt2sql/.venv/Scripts/python -m pip install -r apps/txt2sql/requirements.txt
apps/txt2sql/.venv/Scripts/python apps/txt2sql/server.py
```

访问 http://127.0.0.1:8080 。默认本地单用户、固定示例模式，不需要密钥。
开发环境也支持将依赖安装到应用 `.deps` 目录；该目录优先于系统依赖，且不入 Git。

## 对应流程图

`rewrite_query → ask_user(interrupt)/retrieve_schema → generate_sql → sql_security_validate → execute_sql → result_validate → explain_result → final_answer`

- 缺失信息通过真实 LangGraph `interrupt()` 暂停；HTTP 返回 `thread_id`。
- 用户补充时通过相同 `thread_id` 和 `Command(resume=...)` 恢复，再执行改写。
- 检查点持久化到 `data/checkpoints.sqlite`，重启仍可恢复；页面在 sessionStorage 保留待补充会话编号。
- Schema 从授权表自动采集字段、类型、主键和外键，不抽样数据；口径与示例从独立知识目录加载。Schema RAG 采用中文单字/双字及英文词检索；有效口径强制保留，相关有效示例取前两条，尚未引入 embedding/向量库。
- sqlglot AST 限制单条 SELECT、物理表白名单、跨库及高危操作；SQLite 只读连接和授权回调为第二道执行边界。
- 4 个连接的有界只读连接池，单次执行预算约 3 秒，最多返回 200 行。
- 执行失败或可修复的结果口径错误进入 `fix_sql`，最多修复 2 次；修复后必须重新安全校验。安全拒绝直接终止。
- 空结果作为有效结果附提示；非有限数值触发修复，超过 1 万亿的数值提示核对。
- 演示销售额要求单表且 WHERE 必须以合取条件限定已完成状态，复杂子查询口径转为失败待确认。不是通用语义正确性证明。
- 每节点开始、完成、失败及中断事件写入 `data/audit.sqlite`。审计保存阶段、状态与错误摘要；完整问题/SQL/结果位于检查点中，需管理本地文件访问与保留周期。
- 页面展示表格、SQL、解释、口径、流程轨迹和修复次数，可导出当前结果 CSV。
- 本地 CLI 支持知识草稿与审核；示例必须提供人工预期结果并经过实际只读执行比对。Schema、内容或指标变更会使相关审核失效，详见 [知识目录操作](knowledge/README.md)。

## 模型配置

服务进程环境变量（不自动读取 .env）：

- `TXT2SQL_API_KEY`：接口密钥。
- `TXT2SQL_MODEL_URL`：兼容 Chat Completions 的完整 HTTPS 接口地址。
- `TXT2SQL_MODEL`：模型名称。
- `TXT2SQL_PORT`：可选，默认 8080。

配置后重启。改写、生成、修复、解读分别调用模型，单次 HTTP 模型超时 40 秒。
结果解读会将最多前 20 行查询结果发送到配置的模型服务；使用前确认该数据出境/外发范围适合你的环境。
无密钥时支持四个内置合成数据示例，以及审核通过且仍有效的本地示例；按问题文本匹配。未知问题会中断等待用户选择示例，不能自动修复任意 SQL。
没有真实密钥，因此真实服务商请求与自然语言准确率尚未验证。

## API

先 GET `/api/context` 获取页面请求令牌；POST `/api/ask` 携带 `X-App-Token`：

- 新查询：JSON `question`。
- 恢复追问：JSON `question`（补充回答）和 `thread_id`。
- 响应 `status`：`interrupted`、`completed`、`failed` 或 `rejected`。
- 失败响应不携带之前失败查询的结果。已结束会话不能恢复。
- 查询返回 `knowledge_version` 和 `sources`，检索节点审计记录相同知识版本与来源。

目前串行执行图请求，以避免同一会话并发恢复；没有多用户身份隔离。
仅监听 127.0.0.1，不直接用于公网或生产敏感数据。尚未提供登录、行列级租户权限、图表、审计查询界面或 MySQL/PostgreSQL 数据源。

## 授权表与访问边界

当前固定访问本地演示库的 `orders`，只允许查询。表权限由服务端代码及数据库只读连接执行，模型不能修改。
Schema 采集器、AST 校验器和数据库授权回调分别限制表范围，三者新增授权必须同步维护。
知识审核不授予数据权限；CLI 的 `--database` 只影响该次本地命令，不改变 Web 服务连接。

AST 检测到其他表、跨库或写操作时，返回安全拒绝并结束流程。
数据库层仍独立阻断越权操作；当前数据库异常统一进入有限修复流程，权限错误尚未单独分类，
因此不能把现有实现描述为完整的用户权限审批系统。
多人身份、行列级权限、真实数据库只读角色及访问申请流程的边界见 [根 README](../../README.md#数据授权与未授权请求)。

## 测试

```powershell
apps/txt2sql/.venv/Scripts/python -m unittest discover -s apps/txt2sql -p "test_*.py" -v
```

覆盖 HTTP、模型输出解析、恶意 SQL、只读权限、超时、行数上限、口径、持久化中断恢复、有限修复和审计。
CI 安装相同顶层版本后运行以上测试。间接依赖尚未生成完整锁文件。
