#!/usr/bin/env python3
"""
A股分红日历 — 抓取分红数据，生成ICS文件，可同步到CalDAV。

用法:
  python dividend.py                       蓝筹股模式，输出 blue_chip_dividend.ics
  python dividend.py -c config             配置文件模式
  python dividend.py -c config --caldav    同时同步到 CalDAV
  python dividend.py -c config --analyze   启用LLM股票分析

环境变量 (.env 或系统环境):
  CALDAV_USERNAME / CALDAV_PASSWORD        CalDAV 账号密码
  LLM_BASE_URL / LLM_MODEL / LLM_API_KEY   大模型连接信息（优先于 llm.yml）
"""

import argparse
import json
import logging
import os
import sys
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import time
from pathlib import Path
from typing import Optional

import akshare as ak
import pandas as pd
import requests
import yaml
warnings.filterwarnings("ignore", category=FutureWarning, module="ics")
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


@dataclass
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
    analysis_url: Optional[str] = None

    @property
    def event_date(self) -> date:
        """日程日期：优先股权登记日，无则回退除权除息日"""
        return self.reg_date or self.ex_date

    @property
    def uid(self) -> str:
        return f"{self.code}-{self.event_date.isoformat()}@astock-dividend"

    @property
    def summary(self) -> str:
        return f"{self.name}({self.code}) 分红股权登记"

    @property
    def description(self) -> str:
        parts = [f"股票: {self.name}({self.code})"]
        if self.cash_dividend:
            per_share = self.cash_dividend / 10
            parts.append(f"每10股派{self.cash_dividend}元（每股派{per_share:.3f}元）")
        if self.stock_dividend:
            parts.append(f"送{self.stock_dividend}股")
        if self.stock_transfer:
            parts.append(f"转增{self.stock_transfer}股")
        impact = self._ex_impact
        if impact:
            parts.append(impact)
        parts.append(f"进度: {self.progress}")
        if self.reg_date:
            parts.append(f"股权登记日: {self.reg_date}")
            parts.append(f"除权除息日: {self.ex_date}")
        else:
            parts.append(f"除权除息日: {self.ex_date}")
        if self.analysis_url:
            parts.append(f"\nLLM分析报告: {self.analysis_url}")
        return "\n".join(parts)

    @property
    def _ex_impact(self) -> str:
        """计算除权除息对股价的简约影响"""
        cash = self.cash_dividend or 0
        bonus = self.stock_dividend or 0
        transfer = self.stock_transfer or 0
        if cash == 0 and bonus == 0 and transfer == 0:
            return ""
        lines = ["除权除息影响:"]
        if cash:
            per_share = cash / 10
            lines.append(f"  每股派现 {per_share:.3f}元 → 除息后股价约下调{per_share:.2f}元")
        if bonus or transfer:
            expand = 1 + bonus / 10 + transfer / 10
            pct = (expand - 1) * 100
            lines.append(f"  每10股→{expand*10:.0f}股 (扩股{pct:.0f}%) → 股价等比摊薄")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
# 内置高分红蓝筹股 Top 122（无配置文件时的默认值）
# 筛选标准：A股主板+创业板+科创板 + 市值>500亿 + 近年稳定分红
# ═══════════════════════════════════════════════════════════

BLUE_CHIP_STOCKS = [
    # ═══ 银行 (15) ═══
    ("601398", "工商银行"), ("601939", "建设银行"), ("601288", "农业银行"),
    ("601988", "中国银行"), ("600036", "招商银行"), ("601328", "交通银行"),
    ("601166", "兴业银行"), ("600000", "浦发银行"), ("601998", "中信银行"),
    ("600016", "民生银行"), ("601818", "光大银行"), ("000001", "平安银行"),
    ("601229", "上海银行"), ("600919", "江苏银行"), ("002142", "宁波银行"),
    # ═══ 保险 (5) ═══
    ("601318", "中国平安"), ("601628", "中国人寿"), ("601601", "中国太保"),
    ("601336", "新华保险"), ("601319", "中国人保"),
    # ═══ 券商 (6) ═══
    ("600030", "中信证券"), ("601688", "华泰证券"), ("601211", "国泰君安"),
    ("300059", "东方财富"), ("600837", "海通证券"), ("000776", "广发证券"),
    # ═══ 能源/煤炭 (6) ═══
    ("601088", "中国神华"), ("601225", "陕西煤业"), ("601898", "中煤能源"),
    ("600188", "兖矿能源"), ("601699", "潞安环能"), ("600985", "淮北矿业"),
    # ═══ 石油石化 (4) ═══
    ("601857", "中国石油"), ("600028", "中国石化"), ("600938", "中国海油"),
    ("600346", "恒力石化"),
    # ═══ 电力/公用事业 (10) ═══
    ("600900", "长江电力"), ("600025", "华能水电"), ("601985", "中国核电"),
    ("600905", "三峡能源"), ("600886", "国投电力"), ("600674", "川投能源"),
    ("600795", "国电电力"), ("600011", "华能国际"), ("600023", "浙能电力"),
    ("001289", "龙源电力"),
    # ═══ 白酒 (6) ═══
    ("600519", "贵州茅台"), ("000858", "五粮液"), ("000568", "泸州老窖"),
    ("600809", "山西汾酒"), ("002304", "洋河股份"), ("000596", "古井贡酒"),
    # ═══ 消费/食品 (5) ═══
    ("600887", "伊利股份"), ("603288", "海天味业"), ("000895", "双汇发展"),
    ("600298", "安琪酵母"), ("600872", "中炬高新"),
    # ═══ 家电 (4) ═══
    ("000333", "美的集团"), ("000651", "格力电器"), ("600690", "海尔智家"),
    ("002032", "苏泊尔"),
    # ═══ 医药 (6) ═══
    ("600276", "恒瑞医药"), ("300760", "迈瑞医疗"), ("600436", "片仔癀"),
    ("000538", "云南白药"), ("000661", "长春高新"), ("603259", "药明康德"),
    # ═══ 创业板蓝筹 (5) ═══
    ("300750", "宁德时代"), ("300124", "汇川技术"), ("300498", "温氏股份"),
    ("300015", "爱尔眼科"), ("300274", "阳光电源"),
    # ═══ 汽车/零部件 (5) ═══
    ("002594", "比亚迪"), ("600104", "上汽集团"), ("601633", "长城汽车"),
    ("000625", "长安汽车"), ("600660", "福耀玻璃"),
    # ═══ 建材 (3) ═══
    ("600585", "海螺水泥"), ("002271", "东方雨虹"),
    ("000786", "北新建材"),
    # ═══ 钢铁/有色 (6) ═══
    ("600019", "宝钢股份"), ("601899", "紫金矿业"), ("600362", "江西铜业"),
    ("603993", "洛阳钼业"), ("601600", "中国铝业"),
    ("600547", "山东黄金"),
    # ═══ 化工 (3) ═══
    ("600309", "万华化学"), ("600426", "华鲁恒升"),
    ("002493", "荣盛石化"),
    # ═══ 交通运输 (5) ═══
    ("601006", "大秦铁路"), ("601816", "京沪高铁"), ("601919", "中远海控"),
    ("600018", "上港集团"), ("600377", "宁沪高速"),
    # ═══ 建筑 (4) ═══
    ("601668", "中国建筑"), ("601390", "中国中铁"), ("601186", "中国铁建"),
    ("601800", "中国交建"),
    # ═══ 通信 (3) ═══
    ("600941", "中国移动"), ("601728", "中国电信"),
    ("600050", "中国联通"),
    # ═══ 机械/装备 (4) ═══
    ("600031", "三一重工"), ("000157", "中联重科"),
    ("601100", "恒立液压"), ("600150", "中国船舶"),
    # ═══ 电子/半导体 (3) ═══
    ("601138", "工业富联"), ("002475", "立讯精密"),
    ("603501", "韦尔股份"),
    # ═══ 科创板 (14) ═══
    ("688981", "中芯国际"), ("688111", "金山办公"), ("688036", "传音控股"),
    ("688012", "中微公司"), ("688008", "澜起科技"), ("688256", "寒武纪"),
    ("688169", "石头科技"), ("688599", "天合光能"), ("688009", "中国通号"),
    ("688187", "时代电气"), ("688065", "凯赛生物"), ("688396", "华润微"),
    ("688303", "大全能源"), ("688561", "奇安信"),
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

    events.sort(key=lambda e: e.event_date)
    return events


def _fetch_dividend_history(stock: Stock, lookback_years: int = 5) -> list[dict]:
    """获取股票历史分红记录（过去年份）"""
    cutoff_start = date.today() - timedelta(days=lookback_years * 365)
    rows: list[dict] = []
    try:
        df = ak.stock_history_dividend_detail(symbol=stock.code, indicator="分红")
        if df is None or df.empty:
            return rows
        for _, row in df.iterrows():
            ex_date = _parse_date(row.get("除权除息日"))
            if ex_date is None or ex_date >= date.today():
                continue
            if ex_date < cutoff_start:
                continue
            rows.append({
                "ex_date": ex_date,
                "cash": _parse_float(row.get("派息")) or 0,
                "bonus": _parse_int(row.get("送股")) or 0,
                "transfer": _parse_int(row.get("转增")) or 0,
                "progress": str(row.get("进度", "")),
            })
        rows.sort(key=lambda r: r["ex_date"], reverse=True)
    except Exception as exc:
        logger.warning("获取 %s 历史分红失败: %s", stock.label, exc)
    return rows


def _history_to_json(history: list[dict]) -> list[dict]:
    return [{**h, "ex_date": h["ex_date"].isoformat()} for h in history]


def _history_from_json(data: list[dict]) -> list[dict]:
    from datetime import date as _date
    result = []
    for h in data:
        try:
            result.append({**h, "ex_date": _date.fromisoformat(h["ex_date"])})
        except Exception:
            result.append(h)
    return result


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
        vevent.begin = ev.event_date
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
        import caldav as _caldav
        AuthorizationError = _caldav.error.AuthorizationError
        from icalendar import Calendar as ICal, Event as ICalEvent
    except ImportError:
        result["errors"].append("缺少 caldav/icalendar，请: pip install caldav icalendar")
        return result

    # 抑制 caldav 兼容层警告（WPS iCal 非标格式触发，不影响功能）
    logging.getLogger("caldav").setLevel(logging.ERROR)
    logging.getLogger("caldav.lib.icalendar_compat").setLevel(logging.ERROR)

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
        cal_obj = _caldav_fallback(principal, calendar_name)
    if cal_obj is None:
        result["errors"].append("找不到也无法创建日历，且无可用日历")
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
    """找到指定日历，不存在则创建"""
    existing_names = []
    try:
        for c in principal.get_calendars():
            try:
                dn = c.get_display_name()
            except Exception:
                dn = ""
            existing_names.append(dn)
            if dn == name:
                return c
    except Exception:
        pass

    logger.info("已有日历: %s", existing_names)
    logger.info("未找到 '%s'，尝试创建 ...", name)

    try:
        new_cal = principal.make_calendar(
            name=name,
            supported_calendar_component_set=["VEVENT"],
        )
        logger.info("已创建日历 '%s'", name)
        return new_cal
    except Exception as exc:
        logger.error("创建日历 '%s' 失败: %s", name, exc)
        logger.error("WPS 可能不支持 API 创建日历，请在 WPS 日历 App 中手动创建 '%s' 后重试", name)
        return None


def _caldav_fallback(principal, wanted: str):
    """找不到目标日历且无法创建时，从后往前找第一个可写的日历"""
    cals = []
    try:
        cals = list(principal.get_calendars())
    except Exception:
        pass
    if not cals:
        return None
    # 从前往后遍历，WPS 第一个日历通常是可写的主日历
    for c in cals:
        try:
            dn = c.get_display_name()
            try:
                c.events()
                logger.info("回退到现有日历 '%s'（请求的 '%s' 找不到且无法创建）", dn, wanted)
                return c
            except Exception:
                logger.debug("日历 '%s' 无法访问，跳过", dn)
                continue
        except Exception:
            continue
    logger.warning("无可用日历（请求的 '%s' 找不到且无法创建）", wanted)
    return None


def _caldav_index(cal_obj) -> dict[str, str]:
    from icalendar import Calendar as ICal
    index = {}
    try:
        for ev in cal_obj.events():
            try:
                raw = ev.data
                # 预过滤：只解析属于本脚本生成的事件
                if "@astock-dividend" not in raw:
                    continue
                ical = ICal.from_ical(raw)
                for comp in ical.walk("VEVENT"):
                    uid = str(comp.get("uid"))
                    if uid:
                        index[uid] = raw
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
    vevent.add("dtstart", ev.event_date)
    vevent.add("dtend", ev.event_date + timedelta(days=1))
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
# LLM 股票分析（OpenAI-compatible API）
# ═══════════════════════════════════════════════════════════

class StockAnalyzer:
    """使用可配置的LLM对股票进行分红分析

    优先级: 环境变量 > llm.json 配置
      LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
    """

    def __init__(self, cfg: dict):
        self.api_base = os.environ.get("LLM_BASE_URL", "").rstrip("/") or cfg.get("api_base", "").rstrip("/")
        self.api_key = os.environ.get("LLM_API_KEY", "") or os.environ.get(cfg.get("api_key_env", ""), "")
        self.model = os.environ.get("LLM_MODEL", "") or cfg.get("model", "gpt-4o-mini")
        self.max_tokens = cfg.get("max_tokens", 4000)
        self.temperature = cfg.get("temperature", 0.3)
        self.prompt_template = cfg.get("analysis_prompt", "")
        self.request_delay = cfg.get("request_delay", 1.0)
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            })

    def analyze(self, stock: Stock, events: list[Event], history: list[dict] | None = None) -> Optional[dict]:
        """对一只股票进行分析，返回结构化 dict 或 None"""
        # 近期未来分红
        future_text = "\n".join(
            f"- {e.ex_date} 每10股派{e.cash_dividend}元 (送{e.stock_dividend}股/转增{e.stock_transfer}股) 进度:{e.progress}"
            for e in events
        )
        # 历史分红
        history_text = ""
        if history:
            rows = []
            for h in history:
                row = f"- {h['ex_date']} 每10股派{h['cash']:.1f}元"
                if h.get('bonus'): row += f" 送{h['bonus']}股"
                if h.get('transfer'): row += f" 转增{h['transfer']}股"
                rows.append(row)
            history_text = "\n历史分红记录（近N年）:\n" + "\n".join(rows)

        events_summary = history_text + "\n\n近期分红事件:\n" + future_text if history_text else future_text

        user_prompt = (self.prompt_template
            .replace("{stock_code}", stock.code)
            .replace("{stock_name}", stock.name)
            .replace("{events_summary}", events_summary))
        messages = [
            {"role": "system", "content": "你是一位专业的A股投资分析师，擅长分红策略评估。请严格按要求的JSON格式输出。"},
            {"role": "user", "content": user_prompt},
        ]
        raw = self._call_llm(messages)
        return _parse_analysis(raw, stock)

    def _call_llm(self, messages: list[dict]) -> str:
        base = self.api_base
        url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        logger.info("LLM 请求: %s (model=%s)", url, self.model)
        try:
            resp = self._session.post(url, json=payload, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            content = body["choices"][0]["message"]["content"].strip()
            usage = body.get("usage", {})
            logger.info("LLM 响应: tokens in=%s out=%s", usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"))
            return content
        except requests.exceptions.RequestException as exc:
            logger.error("LLM 请求失败: %s", exc)
            if hasattr(exc, "response") and exc.response is not None:
                text = exc.response.text[:500]
                status = exc.response.status_code
                logger.error("HTTP %s 响应: %s", status, text)
                if status == 404:
                    logger.error("请检查 LLM_BASE_URL 是否正确（当前: %s → %s）", self.api_base, url)
            return f"[分析失败: {exc}]"
        except (KeyError, IndexError, ValueError) as exc:
            logger.error("LLM 响应解析失败: %s", exc)
            return f"[解析失败: {exc}]"


def _parse_analysis(raw: str, stock: Stock) -> Optional[dict]:
    """解析LLM返回的JSON，失败返回降级dict"""
    try:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            start = 1 if lines[0].strip() in ("```", "```json") else 0
            end = -1 if lines[-1].strip() == "```" else None
            text = "\n".join(lines[start:end])
        data = json.loads(text)
        assert "analysis" in data
        return data
    except Exception:
        logger.warning("JSON解析失败，降级为纯文本: %s", raw[:100])
        return {
            "overall_score": 0,
            "dividend_stability": "未知",
            "estimated_yield_pct": 0,
            "earnings_quality": "未知",
            "valuation_level": "未知",
            "growth_outlook": "未知",
            "risk_level": "未知",
            "analysis": raw,
            "highlights": [],
            "risks": [],
        }


# ═══════════════════════════════════════════════════════════
# HTML 分析报告（单文件，含 SVG 图表）
# ═══════════════════════════════════════════════════════════

def generate_analysis_html(results: list[dict], output_path: Path, generated_date: str, model: str) -> Path:
    """将多只股票的结构化分析结果渲染为单个HTML报告"""
    if not results:
        return output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _build_html(results, generated_date, model)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _build_html(results: list[dict], date_str: str, model: str) -> str:
    stocks_json = json.dumps([
        {
            "code": r["stock"].code,
            "name": r["stock"].name,
            "score": r["analysis"].get("overall_score", 0),
            "yield_pct": r["analysis"].get("estimated_yield_pct", 0),
            "stability": r["analysis"].get("dividend_stability", "-"),
            "earnings": r["analysis"].get("earnings_quality", "-"),
            "valuation": r["analysis"].get("valuation_level", "-"),
            "growth": r["analysis"].get("growth_outlook", "-"),
            "risk": r["analysis"].get("risk_level", "-"),
            "analysis": _md_to_html(r["analysis"].get("analysis", "")),
            "highlights": r["analysis"].get("highlights", []),
            "risks": r["analysis"].get("risks", []),
            "events": [
                {"ex_date": str(e.ex_date), "cash": e.cash_dividend or 0, "bonus": e.stock_dividend or 0, "transfer": e.stock_transfer or 0, "progress": e.progress}
                for e in r["events"]
            ],
            "history": [
                {"ex_date": str(h["ex_date"]), "cash": h["cash"], "bonus": h.get("bonus", 0), "transfer": h.get("transfer", 0), "progress": h.get("progress", "")}
                for h in r.get("history", [])
            ],
        }
        for r in results
    ], ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>A股分红分析报告 — {date_str}</title>
<style>
  :root {{ --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a; --text: #e4e6eb; --muted: #9ca3af; --green: #22c55e; --yellow: #eab308; --red: #ef4444; --blue: #3b82f6; --purple: #a855f7; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", sans-serif; background: var(--bg); color: var(--text); line-height: 1.6; padding: 24px; max-width: 1200px; margin: 0 auto; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 4px; }}
  h2 {{ font-size: 1.3rem; margin: 32px 0 16px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  h3 {{ font-size: 1.1rem; color: var(--blue); }}
  .meta {{ color: var(--muted); font-size: .85rem; margin-bottom: 32px; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 20px 0; }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; text-align: center; }}
  .kpi-value {{ font-size: 2rem; font-weight: 700; }}
  .kpi-label {{ font-size: .8rem; color: var(--muted); margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 16px 0; font-size: .9rem; }}
  th, td {{ padding: 10px 14px; text-align: left; border-bottom: 1px solid var(--border); }}
  th {{ background: var(--card); color: var(--muted); font-weight: 600; position: sticky; top: 0; }}
  tr:hover {{ background: rgba(255,255,255,.03); }}
  .score-bar {{ display: inline-block; height: 8px; border-radius: 4px; background: var(--border); min-width: 80px; vertical-align: middle; margin-left: 6px; }}
  .score-fill {{ height: 100%; border-radius: 4px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: .78rem; font-weight: 600; }}
  .tag-low {{ background: rgba(34,197,94,.15); color: var(--green); }}
  .tag-mid {{ background: rgba(234,179,8,.15); color: var(--yellow); }}
  .tag-high {{ background: rgba(239,68,68,.15); color: var(--red); }}
  .stock-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin: 20px 0; }}
  .stock-card h3 {{ margin-bottom: 12px; }}
  .stock-card h3 a {{ color: var(--blue); text-decoration: none; }}
  .stock-card h4 {{ color: var(--purple); margin: 16px 0 8px; font-size: .95rem; }}
  .fields {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin: 12px 0; }}
  .field {{ background: rgba(255,255,255,.03); border-radius: 8px; padding: 10px 14px; }}
  .field-label {{ font-size: .75rem; color: var(--muted); }}
  .field-value {{ font-size: .95rem; font-weight: 600; }}
  .highlight {{ color: var(--green); }}
  .risk-item {{ color: var(--red); }}
  ul {{ padding-left: 20px; margin: 8px 0; }}
  li {{ margin: 4px 0; }}
  .chart-container {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin: 20px 0; }}
  .chart-container svg {{ width: 100%; }}
  .stock-link {{ color: var(--blue); text-decoration: none; font-weight: 600; }}
  .stock-link:hover {{ text-decoration: underline; }}
  .event-row {{ font-size: .82rem; color: var(--muted); margin: 2px 0; }}
  .analysis-text {{ margin-top: 16px; }}
  .analysis-text p {{ margin: 8px 0; }}
  .analysis-text strong {{ color: #fff; }}
  .divider {{ border: none; border-top: 1px solid var(--border); margin: 40px 0; }}
</style>
</head>
<body>

<h1> A股分红分析报告</h1>
<p class="meta">生成日期: {date_str} &nbsp;|&nbsp; 模型: {model} &nbsp;|&nbsp; 覆盖 {len(results)} 只股票</p>

<div class="kpis" id="kpi-row"></div>

<h2> 横向对比总览</h2>
<div class="chart-container"><svg id="chart-score" viewBox="0 0 800 0"></svg></div>
<div class="chart-container"><svg id="chart-yield" viewBox="0 0 800 0"></svg></div>

<h2> 对比明细表</h2>
<div style="overflow-x:auto;"><table id="compare-table"></table></div>

<h2> 逐只深度分析</h2>
<div id="stock-cards"></div>

<hr class="divider">
<p style="text-align:center;color:var(--muted);font-size:.8rem;">此报告由AI生成，仅供参考，不构成投资建议。</p>

<script>
const DATA = {stocks_json};
const COLORS = ['#3b82f6','#22c55e','#eab308','#ef4444','#a855f7','#06b6d4','#f97316','#ec4899','#84cc16','#14b8a6'];
const RISK_COLOR = {{ '低': '#22c55e', '中': '#eab308', '高': '#ef4444' }};
const SCORE_COLOR = s => s>=8?'#22c55e':s>=6?'#eab308':s>=4?'#f97316':'#ef4444';

// KPI row
const scores = DATA.map(d=>d.score).filter(s=>s>0);
const avgScore = scores.length ? (scores.reduce((a,b)=>a+b,0)/scores.length).toFixed(1) : '-';
const yields = DATA.map(d=>d.yield_pct).filter(y=>y>0);
const avgYield = yields.length ? (yields.reduce((a,b)=>a+b,0)/yields.length).toFixed(1) : '-';
document.getElementById('kpi-row').innerHTML = [
  {{v:avgScore, l:'平均综合评分 /10'}},
  {{v:avgYield+'%', l:'平均预估股息率'}},
  {{v:DATA.filter(d=>d.risk==='低').length, l:'低风险股票'}},
  {{v:DATA.filter(d=>d.score>=7).length, l:'评分≥7股票'}},
].map(k=>`<div class="kpi"><div class="kpi-value">${{k.v}}</div><div class="kpi-label">${{k.l}}</div></div>`).join('');

// Bar chart helper
function barChart(id, getVal, unit, title) {{
  const svg = document.getElementById(id);
  const valid = DATA.filter(d => getVal(d) > 0);
  if (!valid.length) {{ svg.parentElement.style.display='none'; return; }}
  const max = Math.max(...valid.map(getVal)) * 1.15;
  const H = Math.max(valid.length * 44 + 30, 100);
  const W = 780, L = 140, R = 80, BW = W - L - R;
  svg.setAttribute('viewBox', `0 0 ${{W}} ${{H}}`);
  let html = '';
  // title
  html += `<text x="${{L}}" y="16" fill="#9ca3af" font-size="13" font-weight="600">${{title}}</text>`;
  // bars
  valid.forEach((d,i) => {{
    const w = getVal(d) / max * BW;
    const y = 36 + i * 44;
    const c = COLORS[i % COLORS.length];
    html += `<text x="${{L-8}}" y="${{y+18}}" fill="#e4e6eb" font-size="13" text-anchor="end">${{d.name}}(${{d.code}})</text>`;
    html += `<rect x="${{L}}" y="${{y}}" width="${{w}}" height="24" rx="4" fill="${{c}}" opacity="0.85"/>`;
    html += `<text x="${{L+w+6}}" y="${{y+17}}" fill="#e4e6eb" font-size="12">${{getVal(d)}}${{unit}}</text>`;
  }});
  svg.innerHTML = html;
}}

barChart('chart-score', d=>d.score, '', '综合评分');
barChart('chart-yield', d=>d.yield_pct, '%', '预估股息率');

// Comparison table
const FIELDS = [
  {{k:'score', l:'综合评分', f:d=>`<span style="color:${{SCORE_COLOR(d.score)}};font-weight:700">${{d.score||'-'}}</span>`}},
  {{k:'stability', l:'分红稳定性', f:d=>d.stability}},
  {{k:'yield_pct', l:'预估股息率', f:d=>d.yield_pct?`<span style="font-weight:700">${{d.yield_pct}}%</span>`:'-'}},
  {{k:'earnings', l:'盈利能力', f:d=>d.earnings}},
  {{k:'valuation', l:'估值水平', f:d=>d.valuation}},
  {{k:'growth', l:'成长性', f:d=>d.growth}},
  {{k:'risk', l:'风险等级', f:d=>`<span class="tag tag-${{d.risk==='低'?'low':d.risk==='中'?'mid':'high'}}">${{d.risk}}</span>`}},
];
let thead = '<tr><th>股票</th>'+FIELDS.map(f=>`<th>${{f.l}}</th>`).join('')+'</tr>';
let tbody = DATA.map((d,i)=>`<tr>
  <td><a class="stock-link" href="#stock-${{d.code}}">${{d.name}}<br><span style="font-weight:400;color:var(--muted);font-size:.8rem;">${{d.code}}</span></a></td>
  ${{FIELDS.map(f=>`<td>${{f.f(d)}}</td>`).join('')}}
</tr>`).join('');
document.getElementById('compare-table').innerHTML = thead + tbody;

// Stock cards
const CARDS = DATA.map((d,i) => {{
  const eventsHtml = d.events.map(e=>`<div class="event-row"> ${{e.ex_date}} 每10股派${{e.cash}}元${{e.bonus?' 送'+e.bonus+'股':''}}${{e.transfer?' 转增'+e.transfer+'股':''}} · ${{e.progress}}</div>`).join('');
  const hlHtml = d.highlights.map(h=>`<li class="highlight">${{h}}</li>`).join('');
  const riskHtml = d.risks.map(r=>`<li class="risk-item">${{r}}</li>`).join('');
  const color = COLORS[i % COLORS.length];

  // 历史分红小表格
  let historyHtml = '';
  if (d.history && d.history.length) {{
    const rows = d.history.map(h=>`<tr><td>${{h.ex_date}}</td><td>${{h.cash}}</td><td>${{h.bonus||'-'}}</td><td>${{h.transfer||'-'}}</td><td class="event-row">${{h.progress}}</td></tr>`).join('');
    historyHtml = `<h4> 历史分红记录 <span style="font-weight:400;color:var(--muted);font-size:.8rem;">（近${{d.history.length}}次）</span></h4>
      <table style="margin:8px 0;font-size:.82rem;">
        <thead><tr><th>除权除息日</th><th>每10股派(元)</th><th>送股</th><th>转增</th><th>进度</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table>`;
  }}

  return `<div class="stock-card" id="stock-${{d.code}}">
    <h3><a href="#stock-${{d.code}}" style="color:${{color}}">${{d.name}}(${{d.code}})</a></h3>
    <div class="event-row">即将分红: ${{eventsHtml||'无近期分红事件'}}</div>
    <div class="fields">
      <div class="field"><div class="field-label">综合评分</div><div class="field-value" style="color:${{SCORE_COLOR(d.score)}}">${{d.score||'-'}}/10</div></div>
      <div class="field"><div class="field-label">分红稳定性</div><div class="field-value">${{d.stability}}</div></div>
      <div class="field"><div class="field-label">预估股息率</div><div class="field-value">${{d.yield_pct?d.yield_pct+'%':'-'}}</div></div>
      <div class="field"><div class="field-label">盈利能力</div><div class="field-value">${{d.earnings}}</div></div>
      <div class="field"><div class="field-label">估值水平</div><div class="field-value">${{d.valuation}}</div></div>
      <div class="field"><div class="field-label">成长性</div><div class="field-value">${{d.growth}}</div></div>
      <div class="field"><div class="field-label">风险等级</div><div class="field-value" style="color:${{RISK_COLOR[d.risk]||'#9ca3af'}}">${{d.risk}}</div></div>
    </div>
    ${{historyHtml}}
    ${{hlHtml ? `<h4> 亮点</h4><ul>${{hlHtml}}</ul>` : ''}}
    ${{riskHtml ? `<h4> 风险提示</h4><ul>${{riskHtml}}</ul>` : ''}}
    <h4> 详细分析</h4>
    <div class="analysis-text">${{d.analysis||'无'}}</div>
  </div>`;
}});
document.getElementById('stock-cards').innerHTML = CARDS.join('');
</script>
</body>
</html>"""


def _md_to_html(md: str) -> str:
    """极简 Markdown → HTML，无外部依赖"""
    import re
    lines = md.split("\n")
    out = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("")
            continue
        if stripped.startswith("### "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h4>{stripped[4:]}</h4>")
        elif stripped.startswith("## "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h3>{stripped[3:]}</h3>")
        elif stripped.startswith("# "):
            if in_list: out.append("</ul>"); in_list = False
            out.append(f"<h3>{stripped[2:]}</h3>")
        elif re.match(r"^[\*\-\+]\s", stripped):
            content = re.sub(r"^[\*\-\+]\s+", "", stripped)
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{content}</li>")
        else:
            if in_list: out.append("</ul>"); in_list = False
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


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
    llm_enabled: bool = False
    llm_config: Optional[dict] = None


def _parse_yaml_stocks(data: dict) -> list[Stock]:
    """解析YAML中的分组股票列表，扁平化为 [Stock]"""
    result: list[Stock] = []
    seen: set[str] = set()
    # 兼容旧格式扁平列表
    flat = data.get("stocks", [])
    if flat:
        for item in flat:
            if isinstance(item, dict):
                code = str(item.get("code", ""))
                name = str(item.get("name", ""))
            else:
                code, name = str(item[0]), str(item[1]) if isinstance(item, (list,)) else ("", "")
            if code and code not in seen:
                seen.add(code)
                result.append(Stock(code=code, name=name))
    else:
        # 分组格式: { 行业: [{"code": "name"}, ...], ... }
        for _category, items in data.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                for code, name in item.items():
                    code = str(code)
                    if code not in seen:
                        seen.add(code)
                        result.append(Stock(code=code, name=str(name)))
    return result


def _default_config() -> Config:
    return Config(
        stocks=[Stock(code=c, name=n) for c, n in BLUE_CHIP_STOCKS],
        industries=[],
        output_path=Path("blue_chip_dividend.ics"),
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
    stocks_file = config_path / "stocks.yml"
    calendar_file = config_path / "calendar.yml"
    if not stocks_file.exists():
        logger.error("配置文件不存在: %s", stocks_file)
        sys.exit(1)

    stocks_data = yaml.safe_load(stocks_file.read_text(encoding="utf-8")) or {}
    cal_data = yaml.safe_load(calendar_file.read_text(encoding="utf-8")) if calendar_file.exists() else {}

    stocks = _parse_yaml_stocks(stocks_data)
    industries = [str(i) for i in stocks_data.get("industries", [])]

    ics_cfg = cal_data.get("ics", {})
    output_path = Path(ics_cfg.get("output_dir", "output")) / ics_cfg.get("filename", "dividend.ics")

    caldav_cfg = cal_data.get("caldav", {})
    filter_cfg = cal_data.get("filter", {})

    # LLM 配置（可选）
    llm_enabled = False
    llm_config: Optional[dict] = None
    llm_file = config_path / "llm.yml"
    if llm_file.exists():
        llm_config = yaml.safe_load(llm_file.read_text(encoding="utf-8")) or {}
        llm_enabled = llm_config.get("enabled", False)

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
        llm_enabled=llm_enabled,
        llm_config=llm_config,
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


def _analysis_meta_path(out_dir: Path) -> Path:
    return out_dir / "analysis_meta.json"


def _load_analysis_meta(out_dir: Path) -> dict:
    mp = _analysis_meta_path(out_dir)
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_analysis_meta(out_dir: Path, meta: dict):
    _analysis_meta_path(out_dir).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(description="A股分红日历")
    parser.add_argument("-c", "--config-dir", default=None, help="配置目录路径")
    parser.add_argument("--caldav", action="store_true", help="强制开启 CalDAV 同步")
    parser.add_argument("--analyze", action="store_true", help="启用LLM股票分析")
    parser.add_argument("--force-analyze", action="store_true", help="强制重新分析，忽略已有报告")
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
        logger.info("  %s  %s (%s)", ev.event_date, ev.summary, ev.progress)

    if not events:
        logger.info("无即将到来的分红事件")
        return 0

    # ── Phase 1: 先创建日历（不含分析URL），确保日程先生效 ──
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

    # ── Phase 2: LLM 分析（分析成功后再回填URL到日程）──
    analysis_count = 0
    skipped_count = 0
    any_updated = False
    pages_base = (cfg.llm_config or {}).get("pages_base_url", "").rstrip("/")
    if args.analyze or (cfg.llm_enabled and cfg.llm_config is not None):
        if cfg.llm_config is None:
            logger.warning("LLM 分析已启用但 llm.json 未加载，跳过")
        elif not os.environ.get("LLM_API_KEY") and not cfg.llm_config.get("api_key_env"):
            logger.warning("LLM 分析已启用但未设置 LLM_API_KEY 环境变量，跳过")
        else:
            analyzer = StockAnalyzer(cfg.llm_config)
            seen: set[str] = set()
            today_str = date.today().isoformat()
            out_dir = cfg.output_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)
            meta = _load_analysis_meta(out_dir)
            results: list[dict] = []  # 收集所有股票的结构化结果

            for ev in events:
                if ev.code in seen:
                    continue
                seen.add(ev.code)
                stock = Stock(code=ev.code, name=ev.name)
                stock_events = [e for e in events if e.code == ev.code]
                history = _fetch_dividend_history(stock)

                # 检查是否已有今日同模型的分析
                prev = meta.get(ev.code, {})
                if not args.force_analyze and prev.get("date") == today_str and prev.get("model") == analyzer.model:
                    logger.info("LLM 分析 %s(%s) 已有今日(%s)报告，跳过", ev.name, ev.code, today_str)
                    skipped_count += 1
                    cached = prev.get("cached_analysis")
                    if cached:
                        cached_history = _history_from_json(prev.get("history", []))
                        results.append({"stock": stock, "events": stock_events, "history": cached_history, "analysis": cached})
                    continue

                logger.info("LLM 分析 %s(%s) [%d条历史记录] ...", ev.name, ev.code, len(history))
                result = analyzer.analyze(stock, stock_events, history)
                if result and result.get("analysis"):
                    results.append({"stock": stock, "events": stock_events, "history": history, "analysis": result})
                    meta[ev.code] = {"date": today_str, "model": analyzer.model, "cached_analysis": result, "history": _history_to_json(history)}
                    analysis_count += 1
                    preview = result.get("analysis", "")[:200].replace("\n", " ")
                    print(f"\n--- {ev.name}({ev.code}) 评分:{result.get('overall_score','?')}/10 ---")
                    print(f"{preview}...")
                else:
                    logger.warning("  %s(%s) 分析失败", ev.name, ev.code)
                time.sleep(analyzer.request_delay)

            _save_analysis_meta(out_dir, meta)

            if skipped_count:
                print(f"\nLLM 分析跳过: {skipped_count} 只（已有今日报告，加 --force-analyze 强制重分析）")

            # ── 生成统一 HTML 报告 ──
            if results:
                html_path = out_dir / "analysis.html"
                generate_analysis_html(results, html_path, today_str, analyzer.model)
                logger.info("分析报告已写入: %s", html_path)
                print(f"\nLLM 分析报告: {html_path}")
                if not pages_base:
                    logger.warning("未配置 pages_base_url，日程备注不会包含分析报告链接")
                else:
                    analysis_base_url = f"{pages_base}/analysis.html"
                    for r in results:
                        anchor = f"{analysis_base_url}#stock-{r['stock'].code}"
                        matching = [e for e in events if e.code == r['stock'].code]
                        for se in matching:
                            se.analysis_url = anchor
                    any_updated = True
                    logger.info("已为 %d 只股票注入分析链接到日程备注", len(results))
            else:
                logger.warning("无分析结果，跳过 HTML 报告")

            # ── Phase 3: 分析成功后更新日程（回填分析URL）──
            if any_updated:
                logger.info("更新日历（含分析报告URL）...")
                output = generate_ics(events, cfg.output_path)
                logger.info("ICS 已更新: %s", output)
                if cfg.caldav_enabled and cfg.caldav_url:
                    logger.info("回填 CalDAV 日程备注 ...")
                    cr2 = sync_caldav(events, cfg.caldav_url, cfg.caldav_calendar, cfg.caldav_ssl_verify)
                    if cr:
                        cr["updated"] += cr2["updated"]
                        cr["errors"].extend(cr2["errors"])
                    logger.info("CalDAV 回填完成: 更新=%d", cr2.get("updated", 0))
                else:
                    logger.info("ICS 已更新（无 CalDAV 同步）")
            elif results:
                logger.info("pages_base_url 未配置，日程无需更新")

    print(f"\n{'='*50}")
    print("A股分红日历 同步摘要")
    print(f"{'='*50}")
    print(f"  ICS : {output} ({len(events)} 个事件)")
    if cr:
        print(f"  CalDAV : 新建={cr['created']} 更新={cr['updated']} 删除={cr['deleted']} 跳过={cr['skipped']}")
        for err in cr["errors"]:
            print(f"  错误: {err}")
    print(f"{'='*50}")

    return 0  # CalDAV 错误不阻塞CI（ICS+HTML已生成）


if __name__ == "__main__":
    sys.exit(main())
