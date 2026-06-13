# A股分红日历

抓取 A 股分红除权日，同步到 WPS 日历（CalDAV），同时生成 ICS 文件作为备份。

## 特性

- 监控任意 A 股的分红除权日
- 支持按行业自动拉取成分股（如"银行"板块）
- **LLM 智能分析** — 支持 OpenAI/DeepSeek/Moonshot/Ollama 等厂商，从分红稳定性、股息率、基本面等维度自动评估，分析报告托管到 GitHub Pages
- 同步到 **CalDAV**（WPS 日历原生支持），日程备注自动附带分析报告 URL
- 生成 **ICS 文件**，可托管到 GitHub Pages 订阅
- 单个脚本 `dividend.py`，无繁重包结构

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置股票

编辑 `config/stocks.json`，无需改代码：

```json
{
  "stocks": [
    {"code": "601398", "name": "工商银行"},
    {"code": "600036", "name": "招商银行"},
    {"code": "601088", "name": "中国神华"},
    {"code": "600900", "name": "长江电力"}
  ],
  "industries": []
}
```

- `stocks` — 手动指定股票
- `industries` — 按申万行业自动拉取成分股，可为空 `[]`

### 3. 配置 CalDAV 凭据（可选）

复制 `.env.example` 为 `.env` 并填入 WPS 账号：

```
CALDAV_USERNAME=你的手机号或邮箱
CALDAV_PASSWORD=你的密码
```

### 4. 运行

```bash
# 蓝筹股模式（无需 config 目录，使用内置 Top103 高分红蓝筹股）
python dividend.py

# 配置文件模式
python dividend.py -c config

# 配置文件 + CalDAV 同步
python dividend.py -c config --caldav

# 配置文件 + CalDAV 同步 + LLM 股票分析
python dividend.py -c config --caldav --analyze

# 详细日志
python dividend.py -c config --caldav -v
```

## 部署方式

### 方式一：Linux Crontab（推荐自建服务器）

```bash
# 1. 创建目录和虚拟环境
mkdir -p ~/dividend-calendar/{config,output,logs}
cd ~/dividend-calendar
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. 上传文件
scp dividend.py user@host:~/dividend-calendar/
scp config/*.json user@host:~/dividend-calendar/config/

# 3. 配置 crontab
crontab -e
```

crontab 示例（cron 环境变量需在 crontab 文件顶部声明）：

```cron
CALDAV_USERNAME=你的账号
CALDAV_PASSWORD=你的密码
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-xxxx

# 每天北京时间 08:00（UTC 00:00）
0 0 * * * cd ~/dividend-calendar && ./venv/bin/python dividend.py -c config --caldav --analyze >> logs/cron.log 2>&1
```

> 也可写一个 `run.sh` 脚本调用 `.env`，cron 改为执行脚本。LLM 相关环境变量可省略（不启用分析功能时）。

验证：

```bash
crontab -l                    # 查看定时任务
tail -f logs/cron.log         # 查看脚本输出
```

### 方式二：GitHub Actions（免费托管）

#### 步骤 1：推送代码

将代码推送到 GitHub 仓库的 `master` 分支：

```bash
git remote add origin git@github.com:<用户名>/<仓库名>.git
git push -u origin master
```

#### 步骤 2：添加 CalDAV 凭据 Secrets

1. 浏览器打开你的 GitHub 仓库
2. 点击顶部 **Settings** 标签
3. 左侧菜单 **Secrets and variables** → **Actions**
4. 确保选中顶部的 **Secrets** 标签（不是 Variables）
5. 点击 **New repository secret**，Name 填 `CALDAV_USERNAME`，Value 填你的 WPS 账号，点 **Add secret**
6. 同样方式再添加 `CALDAV_PASSWORD`

> 注意：不是 Environment secrets，也不是 Variables。直接在 Actions secrets 页面添加即可。

#### LLM 分析 Secrets（可选）

如果想在 CI 中启用 LLM 分析，还需额外添加以下 Secrets：

| Secret 名称 | 说明 |
|-------------|------|
| `LLM_API_KEY` | API Key |

> 只需 `LLM_API_KEY` 这一个 Secret 即可。`api_base`、`model` 等从 `config/llm.json` 读取（可安全提交仓库）。CI 检测到 `LLM_API_KEY` 存在则自动启用 `--analyze`。分析不会影响日历同步——日程先生效，分析成功后再回填 URL。

#### 步骤 3：配置 GitHub Pages

GitHub Pages 用于托管 `dividend.ics` 文件，方便 WPS 日历通过 URL 订阅。

**方法 A：网页配置（推荐）**

1. 仓库页面点击 **Settings**
2. 左侧菜单 **Pages**
3. **Source** 选 **Deploy from a branch**
4. **Branch** 选 `gh-pages`，目录选 `/ (root)`，点击 **Save**
5. 等待 1-2 分钟，页面顶部会显示 `Your site is live at https://<用户名>.github.io/<仓库名>/`
6. ICS 订阅地址就是：`https://<用户名>.github.io/<仓库名>/dividend.ics`

**方法 B：命令行（已安装 gh CLI 时）**

```bash
# 先手动跑一次生成 ICS
python dividend.py -c config

# 创建 gh-pages 分支并推送
git checkout --orphan gh-pages
cp output/dividend.ics .
git add dividend.ics
git commit -m "Init GitHub Pages"
git push origin gh-pages

# API 开启 Pages
gh api repos/<用户名>/<仓库名>/pages -X POST -f "source[branch]=gh-pages" -f "source[path]=/"
```

> Pages 只需配置一次，之后每次 Actions 运行都会自动更新 ICS 文件。

#### 步骤 4：首次手动触发

1. 仓库页面点击 **Actions** 标签
2. 左侧选中 **Update A-Stock Dividend Calendar**
3. 点击 **Run workflow** → **Run workflow**
4. 刷新页面，点进运行中的任务查看日志，确认没有报错

#### 触发方式

| 触发条件 | 说明 |
|----------|------|
| 定时自动 | 每天北京时间 08:00（UTC 00:00） |
| 代码推送 | 修改 `config/`、`dividend.py` 或 `requirements.txt` 后 push |
| 手动触发 | Actions 页面 → Run workflow |

#### 修改定时时间

编辑 `.github/workflows/update_calendar.yml`，找到 `schedule` 下的 `cron` 表达式：

```yaml
on:
  schedule:
    - cron: '0 0 * * *'   # UTC 时间
```

Cron 表达式是 **UTC 时间**，格式为 `分 时 日 月 星期`。常用示例（北京时间）：

| cron 表达式 | UTC 时间 | 北京时间 |
|-------------|----------|----------|
| `0 0 * * *` | 00:00 | 08:00 |
| `0 12 * * *` | 12:00 | 20:00 |
| `0 */6 * * *` | 每 6 小时 | 每 6 小时 |
| `30 0 * * 1-5` | 工作日 00:30 | 工作日 08:30 |

> 修改后提交推送即生效。GitHub Actions 免费额度：公开仓库无限，私有仓库每月 2000 分钟。

## 在 WPS 日历中订阅

### CalDAV（推荐）

脚本会将事件推送到 WPS CalDAV。首次运行成功后，在 WPS 日历中登录 CalDAV 账号即可看到 **A股分红** 日历。

WPS 日历添加 CalDAV 账号：服务器 `caldav.wps.cn`，填入账号密码。

### LLM 分析（可选）

#### 配置方式

LLM 配置有两种方式，优先级从高到低：

| 方式 | 适用场景 |
|------|----------|
| **环境变量** | 本地 `.env` 文件 或 GitHub Actions Secrets |
| `config/llm.json` | 配置文件模式，环境变量未设置时的回退值 |

#### 环境变量（推荐）

```bash
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o-mini
export LLM_API_KEY=sk-xxxx
```

> 三个变量都设置后运行 `--analyze` 即可，无需 `llm.json`。支持任何 OpenAI-compatible 厂商：
>
> | 厂商 | LLM_BASE_URL | 说明 |
> |------|-------------|------|
> | OpenAI | `https://api.openai.com/v1` | 默认 |
> | DeepSeek | `https://api.deepseek.com/v1` | 需设置 LLM_API_KEY |
> | Moonshot | `https://api.moonshot.cn/v1` | 需设置 LLM_API_KEY |
> | Ollama | `http://localhost:11434/v1` | 本地模型，无需 API Key |

#### 配置文件 `config/llm.json`

```json
{
  "enabled": true,
  "api_base": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "pages_base_url": "https://<用户名>.github.io/<仓库名>"
}
```

- `enabled` — 设为 `true` 则每次运行自动分析，等同默认带 `--analyze`
- `pages_base_url` — GitHub Pages 地址，分析完成后日程备注会写入报告 URL
- `api_base` / `model` 会被同名环境变量覆盖
- `analysis_prompt` — 分析框架模板，可按需修改

#### CLI 参数

```bash
python dividend.py -c config --analyze       # 启用LLM分析
python dividend.py -c config --force-analyze # 强制重新分析，忽略已有报告
```

- 同一天同模型已有报告时自动跳过，加 `--force-analyze` 强制重分析
- 分析报告写入 `output/analysis_{股票代码}.md`，随 GitHub Pages 公开

### ICS 订阅（备选）

```
https://<你的用户名>.github.io/<仓库名>/dividend.ics
```

每次 Actions 运行后自动更新。

## 配置参考

### `config/stocks.json`

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | int | 配置版本（当前 1） |
| `stocks` | array | `{"code": "代码", "name": "名称"}` |
| `industries` | array | 行业名称，自动拉取成分股 |

### `config/calendar.json`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `ics.output_dir` | string | `output` | ICS 输出目录 |
| `ics.filename` | string | `dividend.ics` | ICS 文件名 |
| `caldav.enabled` | bool | `true` | 启用 CalDAV 同步 |
| `caldav.server_url` | string | `https://caldav.wps.cn` | CalDAV 服务器 |
| `caldav.calendar_name` | string | `A股分红` | 日历名称 |
| `caldav.ssl_verify` | bool | `true` | SSL 证书校验 |
| `filter.lookahead_days` | int | `365` | 向前抓取天数 |
| `filter.min_progress` | string | `实施` | 分红进度下限 |
| `filter.include_proposed` | bool | `false` | 是否包含预案 |

### `config/llm.json`

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | bool | `false` | 启用 LLM 分析（等同默认带 `--analyze`） |
| `api_base` | string | `https://api.openai.com/v1` | API 端点（环境变量 `LLM_BASE_URL` 优先） |
| `model` | string | `gpt-4o-mini` | 模型名（环境变量 `LLM_MODEL` 优先） |
| `max_tokens` | int | `2000` | 最大输出 token |
| `temperature` | float | `0.3` | 生成温度 |
| `request_delay` | float | `1.0` | 请求间隔（秒），避免限流 |
| `pages_base_url` | string | `""` | GitHub Pages 地址，填入后日程备注附带报告 URL |
| `analysis_prompt` | string | `...` | 分析框架模板，支持 `{stock_code}` `{stock_name}` `{events_summary}` 占位符 |

## 项目结构

```
├── dividend.py          # 唯一脚本（获取 + ICS + CalDAV）
├── config/
│   ├── stocks.json      # 股票列表
│   ├── calendar.json    # 输出和同步设置
│   └── llm.json         # LLM分析配置（可选）
├── .github/workflows/   # GitHub Actions
├── requirements.txt
└── README.md
```

## 常见问题

| 问题 | 解决 |
|------|------|
| 日历无事件 | 确认 CalDAV 凭据正确，手动跑一次看日志 |
| akshare API 报错 | 接口可能变更，查看 [akshare 文档](https://akshare.akfamily.xyz/) |
| CalDAV 连接失败 | 检查服务器地址、账号密码、网络是否允许 HTTPS 出站 |
| ICS 数据不是最新 | GitHub Pages 有缓存，等几分钟 |
| 行业拉取失败 | akshare 网络问题，手动在 `stocks[]` 中指定不受影响 |
| LLM 分析不生效 | 检查 `LLM_API_KEY` 等三个环境变量是否都已设置，或 `--analyze` 标志是否加上 |
| 分析结果不理想 | 修改 `config/llm.json` 中 `analysis_prompt` 模板，调整分析维度 |
| 分析后日程无报告链接 | 确认 `config/llm.json` 的 `pages_base_url` 已正确填写 |
| 非 OpenAI 厂商报错 | 确认 `LLM_BASE_URL` 指向厂商的 chat completions 端点（兼容 OpenAI 格式） |
| GitHub Actions 无分析 | 检查 Actions Secrets 中 `LLM_API_KEY` 已添加、`config/llm.json` 中 `api_base`/`model` 已正确配置 |

## License

MIT
