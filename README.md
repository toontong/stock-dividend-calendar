# A股分红日历

抓取 A 股分红除权日，同步到 WPS 日历（CalDAV），同时生成 ICS 文件作为备份。

## 特性

- 监控任意 A 股的分红除权日
- 支持按行业自动拉取成分股（如"银行"板块）
- 同步到 **CalDAV**（WPS 日历原生支持）
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
    {"code": "600036", "name": "招商银行"}
  ],
  "industries": ["银行"]
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
# 银行股默认模式（无需 config 目录）
python dividend.py

# 配置文件模式
python dividend.py -c config

# 配置文件 + CalDAV 同步
python dividend.py -c config --caldav

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

# 每天北京时间 08:00（UTC 00:00）
0 0 * * * cd ~/dividend-calendar && ./venv/bin/python dividend.py -c config --caldav >> logs/cron.log 2>&1
```

> 也可写一个 `run.sh` 脚本调用 `.env`，cron 改为执行脚本。

验证：

```bash
crontab -l                    # 查看定时任务
tail -f logs/cron.log         # 查看脚本输出
```

### 方式二：GitHub Actions（免费托管）

1. 推送代码到 GitHub
2. 在仓库 **Settings → Secrets → Actions** 添加 `CALDAV_USERNAME` 和 `CALDAV_PASSWORD`
3. 开启 GitHub Pages（`gh-pages` 分支）

触发方式：每天 08:00 自动运行、推送到 `config/` 或 `dividend.py` 时触发、手动在 Actions 页面触发。

## 在 WPS 日历中订阅

### CalDAV（推荐）

脚本会将事件推送到 WPS CalDAV。首次运行成功后，在 WPS 日历中登录 CalDAV 账号即可看到 **A股分红** 日历。

WPS 日历添加 CalDAV 账号：服务器 `caldav.wps.cn`，填入账号密码。

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

## 项目结构

```
├── dividend.py          # 唯一脚本（获取 + ICS + CalDAV）
├── config/
│   ├── stocks.json      # 股票列表
│   └── calendar.json    # 输出和同步设置
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

## License

MIT
