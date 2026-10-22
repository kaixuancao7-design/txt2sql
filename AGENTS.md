# TXT2SQL 开发约定

## 架构

独立应用位于 `apps/txt2sql`。`server.py` 提供 HTTP、模型适配和数据库执行器；
`workflow.py` 定义 LangGraph 流程、Schema 检索、安全验证、重试和审计。
`security.py` 提供共享 AST 安全规则；`knowledge.py` 负责结构采集、知识审核与检索，
`manage_knowledge.py` 提供本地操作员 CLI，`knowledge/demo.json` 为合成数据基准。

## 验证

安装 `apps/txt2sql/requirements.txt`，运行：

```text
python -m unittest discover -s apps/txt2sql -p "test_*.py" -v
```

提交前执行 `git diff --check`。使用 Conventional Commits。

## 实现约束

- SQL 修复后必须重新经过安全验证；保留数据库只读执行边界。
- 追问必须使用持久化检查点与 interrupt/resume，不能丢失原问题。
- 对失败查询不得返回之前查询结果，重试必须有上限。
- 密钥、数据文件、检查点、审计及本地依赖目录不得入库。
- 修改行为应添加实际调用对应模块的测试，不写源码字符串断言测试。
- 文档明确区分固定示例、模拟模型测试和真实模型验证。
- 草稿知识不可进入检索；示例审批须校验人工预期。Schema、内容与指标变更后需重新审核。
