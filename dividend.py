#!/usr/bin/env python3
"""
A股分红日历 — 抓取分红数据，生成ICS文件，可同步到CalDAV。

用法:
  python dividend.py                       银行股模式，输出 bank_dividend.ics
  python dividend.py -c config             配置文件模式
  python dividend.py -c config --caldav    同时同步到 CalDAV

环境变量 (.env 或系统环境):
  CALDAV_USERNAME / CALDAV_PASSWORD        CalDAV 账号密码
"""

import argparse
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd
from ics import Calendar, Event as IcsEvent
from ics.alarm import DisplayAlarm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dividend")

PRODID = "-//A-Stock Dividend Calendar//CN"

# ═══════════════════════════════════════════════════════════
# 数据模型
# ═══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class Stock:
    code: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name}({self.code})"


@dataclass(frozen=True)
class Event:
    code: str
    name: str
    ex_date: date
    cash_dividend: Optional[float] = None
    stock_dividend: Optional[int] = None
    stock_transfer: Optional[int] = None
    progress: str = ""
    reg_date: Optional[date] = None
    announce_date: Optional[date] = None

    @property
    def uid(self) -> str:
        return f"{self.code}-{self.ex_date.isoformat()}@astock-dividend"

    @property
    def summary(self) -> str:
        return f"{self.name}({self.code}) 分红除权"

    @property
    def description(self) -> str:
        parts = [f"股票: {self.name}({self.code})"]
        if self.cash_dividend:
            parts.append(f"每10股派{self.cash_dividend}元")
        if self.stock_dividend:
            parts.append(f"送{self.stock_dividend}股")
        if self.stock_transfer:
            parts.append(f"转增{self.stock_transfer}股")
        parts.append(f"进度: {self.progress}")
        if self.reg_date:
            parts.append(f"股权登记日: {self.reg_date}")
        parts.append(f"除权除息日: {self.ex_date}")
        return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
# 内置银行股列表（无配置文件时的默认值）
# ═══════════════════════════════════════════════════════════

BANK_STOCKS = [
    ("601398", "工商银行"), ("601939", "建设银行"), ("601288", "农业银行"),
    ("601988", "中国银行"), ("600036", "招商银行"), ("601328", "交通银行"),
    ("600016", "民生银行"), ("600000", "浦发银行"), ("601166", "兴业银行"),
    ("002142", "宁波银行"), ("600015", "华夏银行"), ("601818", "光大银行"),
    ("601009", "南京银行"), ("000001", "平安银行"), ("600919", "江苏银行"),
    ("601229", "上海银行"), ("601169", "北京银行"), ("600926", "杭州银行"),
    ("601838", "成都银行"), ("601997", "贵阳银行"), ("601128", "常熟银行"),
    ("600908", "无锡银行"), ("601528", "瑞丰银行"), ("601579", "张家港行"),
]


# ═══════════════════════════════════════════════════════════
# 日期/数值解析
# ═══════════════════════════════════════════════════════════

def _parse_date(value) -> Optional[date]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    try:
        ts = pd.Timestamp(value)
        return None if pd.isna(ts) else ts.date()
    except Exception:
        return None


def _parse_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        v = float(value)
        return v if not pd.isna(v) else None
    except (ValueError, TypeError):
        return None


def _parse_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        v = float(value)
        return int(v) if not pd.isna(v) else None
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════
# 数据获取 (akshare)
# ═══════════════════════════════════════════════════════════

def fetch_industry_stocks(industry: str) -> list[Stock]:
    """从申万行业板块获取股票列表"""
    df = ak.stock_board_industry_cons_em(symbol=industry)
    return [Stock(code=str(r["代码"]), name=str(r["名称"])) for _, r in df.iterrows()]


def fetch_events(
    stocks: list[Stock],
    lookahead_days: int = 365,
    min_progress: str = "实施",
    include_proposed: bool = False,
) -> list[Event]:
    """抓取股票的未来分红事件"""
    today = date.today()
    cutoff = today + timedelta(days=lookahead_days)
    events: list[Event] = []

    for stock in stocks:
        try:
            df = ak.stock_history_dividend_detail(symbol=stock.code, indicator="分红")
            if df is None or df.empty:
                continue

            for _, row in df.iterrows():
                ex_date = _parse_date(row.get("除权除息日"))
                if ex_date is None:
                    continue
                if ex_date < today or ex_date > cutoff:
                    continue
                ev = Event(
                    code=stock.code,
                    name=stock.name,
                    ex_date=ex_date,
                    cash_dividend=_parse_float(row.get("派息")),
                    stock_dividend=_parse_int(row.get("送股")),
                    stock_transfer=_parse_int(row.get("转增")),
                    progress=str(row.get("进度", "")),
                    reg_date=_parse_date(row.get("股权登记日")),
                    announce_date=_parse_date(row.get("公告日期")),
                )
                if not include_proposed and ev.progress != min_progress:
                    continue
                events.append(ev)
        except Exception as exc:
            logger.warning("获取 %s 分红失败: %s", stock.label, exc)

    events.sort(key=lambda e: e.ex_date)
    return events


# ═══════════════════════════════════════════════════════════
# ICS 生成
# ═══════════════════════════════════════════════════════════

def generate_ics(events: list[Event], output_path: str | Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cal = Calendar(creator=PRODID)
    for ev in events:
        vevent = IcsEvent()
        vevent.name = ev.summary
        vevent.description = ev.description
        vevent.begin = ev.ex_date
        vevent.make_all_day()
        vevent.uid = ev.uid
        alarm = DisplayAlarm(trigger=timedelta(days=-3))
        alarm.display_text = ev.summary
        vevent.alarms.append(alarm)
        cal.events.add(vevent)

    output_path.write_text(cal.serialize(), encoding="utf-8")
    return output_path


# ═══════════════════════════════════════════════════════════
# CalDAV 同步（可选，需要 pip install caldav icalendar）
# ═══════════════════════════════════════════════════════════

def sync_caldav(
    events: list[Event],
    server_url: str,
    calendar_name: str,
    ssl_verify: bool = True,
) -> dict:
    """同步事件到 CalDAV，返回 {created, updated, deleted, skipped, errors}"""
    result = {"created": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": []}

    try:
        from caldav import DAVClient
        from caldav.error import NotFoundError, AuthorizationError
        from icalendar import Calendar as ICal, Event as ICalEvent
    except ImportError:
        result["errors"].append("缺少 caldav/icalendar，请: pip install caldav icalendar")
        return result

    user = os.environ.get("CALDAV_USERNAME")
    pwd = os.environ.get("CALDAV_PASSWORD")
    if not user or not pwd:
        result["errors"].append("缺少 CALDAV_USERNAME / CALDAV_PASSWORD 环境变量")
        return result

    try:
        client = DAVClient(url=server_url, username=user, password=pwd, ssl_verify_cert=ssl_verify)
        principal = client.get_principal()
    except AuthorizationError as exc:
        result["errors"].append(f"CalDAV 认证失败: {exc}")
        return result
    except Exception as exc:
        result["errors"].append(f"CalDAV 连接失败: {exc}")
        return result

    cal_obj = _caldav_find_or_create(principal, calendar_name)
    if cal_obj is None:
        result["errors"].append(f"找不到也无法创建日历 '{calendar_name}'")
        return result

    existing = _caldav_index(cal_obj)
    new_uids = set()
    today = date.today()

    for ev in events:
        new_uids.add(ev.uid)
        try:
            if ev.uid in existing:
                if _caldav_changed(ev, existing[ev.uid]):
                    _caldav_upsert(cal_obj, ev)
                    result["updated"] += 1
                else:
                    result["skipped"] += 1
            else:
                _caldav_upsert(cal_obj, ev)
                result["created"] += 1
        except Exception as exc:
            result["errors"].append(str(exc))
            logger.error("CalDAV 同步 %s 失败: %s", ev.uid, exc)

    for uid in list(existing):
        if uid not in new_uids:
            try:
                cal_event = cal_obj.get_event_by_uid(uid)
                dt = _caldav_get_date(cal_event)
                if dt and dt < today:
                    cal_event.delete()
                    result["deleted"] += 1
            except Exception:
                pass

    return result


def _caldav_find_or_create(principal, name: str):
    try:
        for c in principal.get_calendars():
            try:
                if c.get_properties(["displayname"]).get("displayname") == name:
                    return c
            except Exception:
                continue
    except Exception:
        pass
    try:
        return principal.make_calendar(name=name)
    except Exception as exc:
        logger.error("创建日历 '%s' 失败: %s", name, exc)
        return None


def _caldav_index(cal_obj) -> dict[str, str]:
    from icalendar import Calendar as ICal
    index = {}
    try:
        for ev in cal_obj.events():
            try:
                ical = ICal.from_ical(ev.data)
                for comp in ical.walk("VEVENT"):
                    uid = str(comp.get("uid"))
                    if uid:
                        index[uid] = ev.data
            except Exception:
                continue
    except Exception:
        pass
    return index


def _caldav_upsert(cal_obj, ev: Event):
    from icalendar import Calendar as ICal, Event as ICalEvent, Alarm
    try:
        cal_obj.get_event_by_uid(ev.uid).delete()
    except Exception:
        pass
    ical = ICal()
    ical.add("prodid", PRODID)
    ical.add("version", "2.0")
    vevent = ICalEvent()
    vevent.add("uid", ev.uid)
    vevent.add("dtstart", ev.ex_date)
    vevent.add("dtend", ev.ex_date + timedelta(days=1))
    vevent.add("summary", ev.summary)
    vevent.add("description", ev.description)
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", ev.summary)
    alarm.add("trigger", timedelta(days=-3))
    vevent.add_component(alarm)
    ical.add_component(vevent)
    cal_obj.add_event(ical.to_ical().decode("utf-8"))


def _caldav_changed(ev: Event, raw_ics: str) -> bool:
    from icalendar import Calendar as ICal
    try:
        ical = ICal.from_ical(raw_ics)
        for comp in ical.walk("VEVENT"):
            summary_changed = str(comp.get("summary", "")) != ev.summary
            desc_changed = str(comp.get("description", "")) != ev.description
            has_alarm = any(True for _ in ical.walk("VALARM"))
            return summary_changed or desc_changed or not has_alarm
    except Exception:
        return True


def _caldav_get_date(cal_event) -> Optional[date]:
    from icalendar import Calendar as ICal
    try:
        ical = ICal.from_ical(cal_event.data)
        for comp in ical.walk("VEVENT"):
            dt = comp.get("dtstart")
            if dt is not None and isinstance(dt.dt, date):
                return dt.dt
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════════

@dataclass
class Config:
    stocks: list[Stock]
    industries: list[str]
    output_path: Path
    lookahead_days: int
    min_progress: str
    include_proposed: bool
    caldav_enabled: bool
    caldav_url: str
    caldav_calendar: str
    caldav_ssl_verify: bool


def _default_config() -> Config:
    return Config(
        stocks=[Stock(code=c, name=n) for c, n in BANK_STOCKS],
        industries=[],
        output_path=Path("bank_dividend.ics"),
        lookahead_days=365,
        min_progress="实施",
        include_proposed=False,
        caldav_enabled=False,
        caldav_url="",
        caldav_calendar="A股分红",
        caldav_ssl_verify=True,
    )


def load_config(config_dir: str | None) -> Config:
    if config_dir is None:
        return _default_config()

    config_path = Path(config_dir)
    stocks_file = config_path / "stocks.json"
    calendar_file = config_path / "calendar.json"
    if not stocks_file.exists():
        logger.error("配置文件不存在: %s", stocks_file)
        sys.exit(1)

    stocks_data = json.loads(stocks_file.read_text(encoding="utf-8"))
    cal_data = json.loads(calendar_file.read_text(encoding="utf-8")) if calendar_file.exists() else {}

    stocks = [Stock(code=str(s["code"]), name=str(s["name"])) for s in stocks_data.get("stocks", [])]
    industries = [str(i) for i in stocks_data.get("industries", [])]

    ics_cfg = cal_data.get("ics", {})
    output_path = Path(ics_cfg.get("output_dir", "output")) / ics_cfg.get("filename", "dividend.ics")

    caldav_cfg = cal_data.get("caldav", {})
    filter_cfg = cal_data.get("filter", {})

    return Config(
        stocks=stocks,
        industries=industries,
        output_path=output_path,
        lookahead_days=filter_cfg.get("lookahead_days", 365),
        min_progress=filter_cfg.get("min_progress", "实施"),
        include_proposed=filter_cfg.get("include_proposed", False),
        caldav_enabled=caldav_cfg.get("enabled", False),
        caldav_url=caldav_cfg.get("server_url", ""),
        caldav_calendar=caldav_cfg.get("calendar_name", "A股分红"),
        caldav_ssl_verify=caldav_cfg.get("ssl_verify", True),
    )


def resolve_stocks(cfg: Config) -> list[Stock]:
    seen: set[str] = set()
    result: list[Stock] = []

    for s in cfg.stocks:
        if s.code not in seen:
            seen.add(s.code)
            result.append(s)

    for ind in cfg.industries:
        try:
            for s in fetch_industry_stocks(ind):
                if s.code not in seen:
                    seen.add(s.code)
                    result.append(s)
        except Exception as exc:
            logger.warning("获取行业 '%s' 失败: %s", ind, exc)

    return result


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="A股分红日历")
    parser.add_argument("-c", "--config-dir", default=None, help="配置目录路径")
    parser.add_argument("--caldav", action="store_true", help="强制开启 CalDAV 同步")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    cfg = load_config(args.config_dir)
    if args.caldav:
        cfg.caldav_enabled = True

    stocks = resolve_stocks(cfg)
    if not stocks:
        logger.warning("没有可监控的股票")
        return 0
    logger.info("监控 %d 只股票", len(stocks))

    logger.info("正在获取分红数据 ...")
    events = fetch_events(stocks, cfg.lookahead_days, cfg.min_progress, cfg.include_proposed)
    logger.info("找到 %d 个未来分红事件", len(events))
    for ev in events:
        logger.info("  %s  %s (%s)", ev.ex_date, ev.summary, ev.progress)

    if not events:
        logger.info("无即将到来的分红事件")
        return 0

    logger.info("生成 ICS ...")
    output = generate_ics(events, cfg.output_path)
    logger.info("ICS 已写入: %s (%d 个事件)", output, len(events))

    cr = None
    if cfg.caldav_enabled:
        if not cfg.caldav_url:
            logger.warning("CalDAV server_url 未配置，跳过同步")
        else:
            logger.info("同步到 CalDAV ...")
            cr = sync_caldav(events, cfg.caldav_url, cfg.caldav_calendar, cfg.caldav_ssl_verify)

    print(f"\n{'='*50}")
    print("A股分红日历 同步摘要")
    print(f"{'='*50}")
    print(f"  ICS : {output} ({len(events)} 个事件)")
    if cr:
        print(f"  CalDAV : 新建={cr['created']} 更新={cr['updated']} 删除={cr['deleted']} 跳过={cr['skipped']}")
        for err in cr["errors"]:
            print(f"  错误: {err}")
    print(f"{'='*50}")

    return 2 if (cr and cr["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
