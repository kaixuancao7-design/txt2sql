# Schema、业务口径与正确 SQL 示例

`demo.json` 是版本管理的合成数据基准，状态为 `builtin`，不是用户已经完成审核的声明。
测试在独立创建的演示数据库上校验其中四条 SQL 的预期结果。
业务知识必须由操作员通过审核命令明确确认；不会自动将模型生成 SQL 加入正确示例。

## 数据与审核边界

- 结构采集使用单独的只读元数据连接，只采集授权表的字段类型、非空标志、主键及授权表间外键，不采样业务数据。
- 生产查询的表权限和 SQL 安全规则不因知识目录的内容而扩大。目前固定授权 `orders`。
- 本地默认目录为 `data/knowledge`，被 Git 忽略；包含 Schema 快照和 `entry-<id>.json` 条目。
- `draft` 不参与检索；`approved` 需要操作员身份字符串、时间、内容摘要及 Schema 摘要。
- 示例审核须通过 AST、口径过滤规则、只读执行及人工预期结果比对，且不允许截断。比对列名、行顺序及值，建议示例显式 ORDER BY。
- 内容或 Schema 改变后审核失效。指标集合改变后示例也失效，需重新审核。
- 同名问题已有不同 SQL 时拒绝覆盖，需明确区分问题口径。
- 内置基准不可通过 CLI 改写。需要新的审核版本时，将问题/SQL 作为新草稿导入，再审核。
- 审核人是本地操作员输入，尚未对接登录或数字签名；内容哈希用于检测意外修改，不能替代身份鉴权。
- 人工提供的预期结果必须独立确认，不能直接把待审核 SQL 的结果当成正确答案。

## 操作

先按应用文档安装依赖并启动一次服务生成演示库。以下 `python` 应为安装了应用依赖的解释器。

```powershell
# 查看实时 Schema，保存本地快照
python apps/txt2sql/manage_knowledge.py schema

# 查看知识状态与当前是否可用
python apps/txt2sql/manage_knowledge.py list

# 导入示例草稿，返回 id
python apps/txt2sql/manage_knowledge.py add-example --file apps/txt2sql/knowledge/templates/example.json

# 将下面编号替换为返回的 id；审核人填写实际操作员
python apps/txt2sql/manage_knowledge.py approve <id> --reviewer "实际审核人" --expected apps/txt2sql/knowledge/templates/expected.json
```

模板的 `120` 仅适用于未修改的合成演示库。审核通过后，无需重启，可询问“订单总数”。

业务口径按同样方式先添加草稿，再人工确认：

```powershell
python apps/txt2sql/manage_knowledge.py add-metric --file apps/txt2sql/knowledge/templates/metric.json
python apps/txt2sql/manage_knowledge.py approve <id> --reviewer "业务口径负责人"
```

新增口径会使此前示例需要重新审核。建议先确定口径，再审批示例。
指标当前仅支持单表 SELECT 的 WHERE 合取等值过滤规则，不验证任意公式、关联基数或完整业务语义。
`definition` 用于模型上下文与结果说明；`required_filters` 用于实际规则检查。

需要使用另一个本地数据库/知识目录时，CLI 全局参数放在子命令之前：

```powershell
python apps/txt2sql/manage_knowledge.py --database <sqlite路径> --catalog <知识目录> schema
```

此参数不改变 Web 服务的数据库配置，也不解除授权表限制。多操作员并发审核尚不支持，按顺序操作。

## 检索与溯源

实时 Schema 和全部有效指标进入上下文；相关示例按英文词、中文单字/双字重叠排序，取前两条。
草稿、失效条目和零重叠示例不进入检索。相关指标失效时阻止继续生成该指标的查询。
这仍是关键词检索，不是 embedding 向量检索。

查询 API 返回 `knowledge_version` 和 `sources`；检索节点在审计中记录相同的版本及条目编号。
检查点保留当次检索的口径快照，使后续修复使用相同上下文。
