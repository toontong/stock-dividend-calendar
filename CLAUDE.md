# CLAUDE.md — Stock Dividend Calendar 项目协作规范

## 编码规范

### 不要自动提交
修改代码后，**不要自动执行 git commit/push**。展示变更给用户确认，由用户决定是否提交。

### 最小变更原则
代码改动以最小范围为原则。每处改动必须直接服务于当前任务，不重构无关代码，不引入"顺手改"的抽象。

### 同步更新检查清单
修改代码后，必须同步检查并更新：
1. **README.md** — 用户可见行为变化、CLI 用法、配置示例
2. **config/** — 配置 schema 变化（默认是 `.yml` 格式）
3. **`.github/workflows/`** — CI 需要新环境变量或命令
4. **`.env.example`** — 新增环境变量时补充示例
5. **测试** — 如存在测试，确认不因变更受损

### 配置文件
- 所有配置使用 **YAML** 格式（`.yml`），支持注释和分组
- 敏感信息（API Key、密码）走环境变量，不落配置文件
- 配置示例文件（`.example.yml`）不含真实凭据

### 技术栈
- 单文件架构 `dividend.py`，第三方库尽量少
- LLM 分析：OpenAI-compatible API（环境变量优先于配置文件）
- ICS 生成：`ics` 库
- CalDAV 同步：`caldav` + `icalendar`（可选）
- 数据获取：`akshare`

### GitHub Actions
- 定时：UTC 00:00 每天（北京时间 08:00）
- 触发：push 到 master（config/ dividend.py requirements.txt 变更时）
- Secrets 仅放敏感值（CALDAV_USERNAME/PASSWORD、LLM_API_KEY），其他从 `config/*.yml` 读取
- output/ 通过 `peaceiris/actions-gh-pages` 部署到 gh-pages 分支
