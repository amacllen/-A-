"""
A股智能选股系统 v7.0
三套报告：
  1. 上午快报（11:35）— AKShare实时行情 + Tushare新闻公告
  2. 收盘深度报告（18:00）— Tushare Pro全接口 + 财报模块（财报季内）
  3. 财报专项报告（周五18:30）— 仅财报季内运行
AI引擎：DeepSeek   推送：邮件（多收件人）
"""

import os
import re
import datetime
import time
import smtplib
import sqlite3
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from xml.etree import ElementTree

import tushare as ts
import pandas as pd
import numpy as np
from openai import OpenAI

# ══════════════════════════════════════════════════════════
#  初始化
# ══════════════════════════════════════════════════════════
deepseek = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)
ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

EMAIL_SENDER    = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD  = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVER  = os.environ["EMAIL_RECEIVER"]
EMAIL_RECEIVER_2 = os.environ.get("EMAIL_RECEIVER_2", "")
EMAIL_RECEIVER_3 = os.environ.get("EMAIL_RECEIVER_3", "")
EMAIL_RECEIVERS  = [r for r in [EMAIL_RECEIVER, EMAIL_RECEIVER_2, EMAIL_RECEIVER_3] if r]

# 北京时间
_bj      = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
TODAY    = _bj.strftime("%Y%m%d")
TODAY_CN = _bj.strftime("%Y年%m月%d日")
WEEKDAY  = _bj.weekday()   # 0=周一 … 6=周日
NOW_H    = datetime.datetime.utcnow().hour
NOW_M    = datetime.datetime.utcnow().minute
IS_WEEKEND = WEEKDAY >= 5

# 运行模式判断（UTC时间）
# 11:35北京 = UTC 03:35  → 上午快报
# 18:00北京 = UTC 10:00  → 收盘深度
# 18:30北京 = UTC 10:30  → 财报专项（周五）
if IS_WEEKEND:
    # 周末：A股休市，无论触发时间一律走周报模式
    MODE = "weekly"
elif NOW_H == 3 and NOW_M >= 30:
    MODE = "morning"
elif NOW_H == 10 and NOW_M < 20:
    MODE = "closing"
elif NOW_H == 10 and NOW_M >= 20:
    MODE = "financial"
else:
    # 手动触发时根据北京时间判断
    bj_h    = _bj.hour
    bj_min  = _bj.minute
    bj_hm   = bj_h * 60 + bj_min   # 北京时间分钟数
    if bj_hm < 9 * 60 + 25:
        # 09:25之前：盘前，按上午快报处理
        MODE = "morning"
    elif bj_hm < 15 * 60 + 30:
        # 盘中(09:25 → 15:30)：A股未收盘，当日收盘数据未入库
        # 默认走上午快报模式，避免生成空数据收盘报告
        MODE = "morning"
        print(f"⚠ 盘中触发({_bj.strftime('%H:%M')})：A股未收盘，自动降级为上午快报模式")
        print(f"  如需收盘报告，请在 15:30 之后触发（建议 18:00 后，资金/龙虎榜数据完整）")
    else:
        # 15:30后：可以跑收盘报告（部分数据可能要等到18:00才入库齐全）
        MODE = "closing"
        if bj_hm < 18 * 60:
            print(f"⚠ 提前触发收盘报告({_bj.strftime('%H:%M')})：")
            print(f"  涨跌停/连板/龙虎榜/主力资金 通常在18:00后才入库完整，本次报告可能数据不全")

# 财报季判断
def is_earnings_season() -> bool:
    m = _bj.month
    d = _bj.day
    # 一季报：4月，半年报：7-8月，三季报：10月，年报：1-4月
    if m in [1, 2, 3]:   return True   # 年报季
    if m == 4:            return True   # 年报+一季报
    if m in [7, 8]:       return True   # 半年报
    if m == 10:           return True   # 三季报
    return False

IS_EARNINGS = is_earnings_season()

POLICY_KEYWORDS = [
    # 政策机构
    "政策","国务院","发改委","工信部","财政部","央行","证监会","国资委",
    # 战略方向
    "战略","支持","利好","重磅","突破","攻关","规划","意见","通知","办法",
    # 科技产业
    "算力","半导体","新能源","军工","生物","机器人","低空","无人机",
    "储能","芯片","光伏","氢能","核能","量子","卫星","商业航天",
    "人形机器人","具身智能","大模型","AI","人工智能","数字经济",
    # 资本市场
    "专项债","产业基金","补贴","减税","降息","降准","流动性",
    "并购","重组","国企改革","混改","分拆上市",
    # 行业关键词
    "自主可控","国产替代","先进制造","智能制造","绿色",
    "新质生产力","专精特新","独角兽","科创",
]

print(f"\n{'='*55}")
print(f"A股智能选股系统 v7.0 — {MODE}模式")
print(f"北京时间：{_bj.strftime('%Y-%m-%d %H:%M')}")
print(f"财报季：{'是' if IS_EARNINGS else '否'}")
print(f"{'='*55}\n")


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None and str(val) not in ["", "nan", "None"] else default
    except:
        return default


# ───────── 交易日工具 ─────────
def get_trade_dates_in_week(end_date=None) -> list:
    """
    返回截至end_date(含)所在自然周的所有A股交易日 YYYYMMDD 列表（升序）。
    end_date为空时取今天；周末调用得到周一→周五的实际交易日。
    """
    end_dt = _bj if end_date is None else datetime.datetime.strptime(end_date, "%Y%m%d")
    monday = end_dt - datetime.timedelta(days=end_dt.weekday())
    sunday = monday + datetime.timedelta(days=6)
    try:
        cal = pro.trade_cal(
            exchange="",
            start_date=monday.strftime("%Y%m%d"),
            end_date=sunday.strftime("%Y%m%d"),
            is_open="1",
        )
        if cal is not None and len(cal) > 0:
            return sorted(cal["cal_date"].tolist())
    except Exception as e:
        print(f"  trade_cal失败: {e}")
    return []


def get_last_trade_date() -> str:
    """返回最近一个已收盘的交易日YYYYMMDD（周末/今日未开盘时回退）。"""
    try:
        start = (_bj - datetime.timedelta(days=10)).strftime("%Y%m%d")
        cal = pro.trade_cal(exchange="", start_date=start, end_date=TODAY, is_open="1")
        if cal is not None and len(cal) > 0:
            return sorted(cal["cal_date"].tolist())[-1]
    except Exception as e:
        print(f"  最近交易日查询失败: {e}")
    return TODAY


def ask_deepseek(prompt: str, max_tokens: int = 2500) -> str:
    for attempt in range(2):
        try:
            resp = deepseek.chat.completions.create(
                model="deepseek-chat",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"  DeepSeek失败（{attempt+1}）: {e}")
            time.sleep(3)
    return "AI分析暂时不可用。"


# ══════════════════════════════════════════════════════════
#  模块一：政策与新闻
# ══════════════════════════════════════════════════════════

def fetch_policy_news() -> list:
    """
    政策新闻 - 全部使用Tushare官方接口，强制今日日期过滤
    彻底告别AKShare不稳定和历史旧数据问题
    """
    print("【政策新闻】Tushare官方接口抓取...")
    result = []
    today_str   = _bj.strftime("%Y-%m-%d")
    today_ts    = _bj.strftime("%Y%m%d")

    # 1. Tushare官方新闻快讯（最稳定，强制今日过滤）
    news_fetched = False
    for src in ["cls", "sina", "10jqka"]:
        try:
            start_dt = today_str + " 00:00:00"
            end_dt   = today_str + " 23:59:59"
            df = pro.news(
                src=src,
                start_date=start_dt,
                end_date=end_dt,
                fields="title,content,datetime"
            )
            if df is None or len(df) == 0:
                print(f"  Tushare新闻({src})：今日暂无")
                continue
            count = 0
            for _, row in df.head(100).iterrows():
                pub_time = str(row.get("datetime", ""))
                # 双重验证：必须是今天
                if today_str not in pub_time and today_ts not in pub_time:
                    continue
                title = str(row.get("title", ""))
                text  = title + str(row.get("content", ""))[:100]
                if any(k in text for k in POLICY_KEYWORDS):
                    result.append(f"[{src.upper()}快讯 {pub_time[11:16]}] {title[:180]}")
                    count += 1
            if count > 0:
                print(f"  Tushare新闻({src})：今日政策相关{count}条")
                news_fetched = True
                break
            else:
                print(f"  Tushare新闻({src})：今日数据{len(df)}条，无政策关键词匹配")
        except Exception as e:
            print(f"  Tushare新闻({src})失败: {e}")

    if not news_fetched:
        print("  今日政策新闻：Tushare暂无数据")

    # 注：原新华社RSS(xinhuanet.com/*.xml)已移除——该端点常年返回缓存旧内容、
    # 且多数条目无pubDate绕过日期过滤，导致历史政策新闻(如COP15、公祭日等)漏入。
    # 政策新闻仅保留上方有可靠日期过滤的Tushare CLS快讯，无匹配时如实显示"今日暂无"。

    # 去重
    seen, deduped = set(), []
    for item in result:
        key = item[8:40]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    if deduped:
        print(f"  政策新闻合计（今日）：{len(deduped)}条")
    else:
        print("  今日政策新闻：暂无（报告中将显示为空，不引用历史信息）")

    return deduped[:20]

def fetch_announcements() -> list:
    """重大公告：业绩快报、重大合同、股权变动等"""
    print("【公告】重大公告抓取...")
    result = []
    try:
        # Tushare公告接口
        df = pro.anns(ann_date=TODAY)
        if df is not None and len(df) > 0:
            # 筛选重要公告类型
            important_types = ["业绩快报", "业绩预告", "重大合同", "股权激励",
                                "收购", "重组", "增持", "回购", "分红"]
            for _, row in df.head(50).iterrows():
                title = str(row.get("title", ""))
                if any(t in title for t in important_types):
                    result.append({
                        "code":  row.get("ts_code", ""),
                        "title": title[:100],
                        "date":  row.get("ann_date", ""),
                    })
        print(f"  重大公告：{len(result)}条")
    except Exception as e:
        print(f"  公告接口失败: {e}")
    return result[:15]


# ══════════════════════════════════════════════════════════
#  模块二：上午快报数据（基于测试结论）
#  经测试确认：盘中接口（涨停/连板/游资/热榜）均为收盘后入库
#  上午快报可用数据：
#    ✓ Tushare news 新闻快讯（实时162条）
#    ✓ Tushare anns 今日公告（实时）
#    ✓ 昨日龙虎榜 top_list/top_inst（T-1数据）
#    ✓ 昨日主力资金 moneyflow（T-1数据）
#    ✓ 昨日北向资金 moneyflow_hsgt（T-1数据）
#  以下接口移至18:00收盘报告：
#    → limit_list_d 涨停板（收盘后入库）
#    → limit_step 连板天梯（收盘后入库）
#    → hm_detail 游资明细（收盘后入库）
#    → ths_hot 热榜（收盘后入库）
# ══════════════════════════════════════════════════════════

def get_morning_news() -> list:
    """
    今日新闻快讯
    不做关键词过滤，把今日所有财经新闻标题传给AI
    由AI判断哪些重要，避免关键词匹配失败导致AI用历史信息填充
    """
    print("【上午新闻】Tushare实时新闻...")
    result = []
    seen = set()  # 标题去重
    today_str = _bj.strftime("%Y-%m-%d")
    for src in ["cls", "sina"]:
        try:
            df = pro.news(
                src=src,
                start_date=today_str + " 00:00:00",
                end_date=today_str + " 23:59:59",
                fields="datetime,title"
            )
            if df is None:
                print(f"  {src}新闻：接口返回None（可能API失败/积分不足/token问题）")
                continue
            if len(df) == 0:
                print(f"  {src}新闻：接口返回0条（数据源可能未入库）")
                continue
            print(f"  {src}新闻：接口返回{len(df)}条（去重前），开始过滤今日")
            for _, row in df.iterrows():
                pub_time = str(row.get("datetime", ""))
                # 严格验证是今天
                if today_str not in pub_time:
                    continue
                title = str(row.get("title", "")).strip()
                if len(title) < 5:
                    continue
                if title in seen:        # CLS会重复推送同一条，去重
                    continue
                seen.add(title)
                time_str = pub_time[11:16] if len(pub_time) > 10 else ""
                result.append({
                    "time":  time_str,
                    "title": title[:150],
                    "src":   src.upper(),
                })
            if result:
                print(f"  {src.upper()}新闻：今日有效{len(result)}条")
                break
            else:
                print(f"  {src}新闻：返回{len(df)}条但无今日条目，可能日期过滤过严")
        except Exception as e:
            print(f"  {src}新闻失败: {type(e).__name__}: {e}")

    if not result:
        print("  今日新闻：暂无数据")
    # 最多取30条，按时间倒序（最新的在前）
    return result[:30]


def get_morning_announcements() -> list:
    """今日重大公告（实时入库）"""
    print("【上午公告】今日重大公告...")
    result = []
    important = ["业绩快报", "业绩预告", "重大合同", "股权激励",
                 "收购", "重组", "增持", "回购", "分红", "中标", "定增"]
    try:
        df = pro.anns(ann_date=TODAY, fields="ts_code,ann_date,title")
        if df is not None and len(df) > 0:
            for _, row in df.head(100).iterrows():
                title = str(row.get("title", ""))
                if any(t in title for t in important):
                    result.append({
                        "code":  row.get("ts_code", ""),
                        "title": title[:100],
                    })
            print(f"  重大公告：{len(result)}条")
        else:
            print("  重大公告：今日暂无")
    except Exception as e:
        print(f"  公告失败: {e}")
    return result[:12]


def get_yesterday_capital() -> dict:
    """
    昨日资金数据（T-1，上午可用）
    给上午快报提供资金背景参考
    """
    print("【昨日资金】T-1资金数据...")
    result = {
        "north_total": 0,
        "north_top":   [],
        "dragon_tiger": [],
        "top_moneyflow": [],
    }
    # 昨日日期（跳过周末）
    yesterday = _bj - datetime.timedelta(days=1)
    if yesterday.weekday() == 6:  # 周日则取周五
        yesterday = _bj - datetime.timedelta(days=3)
    elif yesterday.weekday() == 5:  # 周六则取周五
        yesterday = _bj - datetime.timedelta(days=2)
    ydate = yesterday.strftime("%Y%m%d")
    names_map = get_stock_names()  # 代码 -> (名称, 行业)，龙虎榜与主力共用

    try:
        # 昨日龙虎榜
        df_list = pro.top_list(trade_date=ydate)
        df_inst = pro.top_inst(trade_date=ydate)
        if df_inst is not None and len(df_inst) > 0:
            for code in df_inst["ts_code"].unique()[:6]:
                name = code
                if df_list is not None:
                    row = df_list[df_list["ts_code"] == code]
                    if len(row) > 0:
                        name = row["name"].values[0]
                result["dragon_tiger"].append({
                    "code": code,
                    "name": name,
                    "industry": names_map.get(code, ("", ""))[1],
                })
        print(f"  昨日龙虎榜机构：{len(result['dragon_tiger'])}只")
    except Exception as e:
        print(f"  昨日龙虎榜失败: {e}")

    try:
        # 昨日主力资金TOP10
        df = pro.moneyflow(trade_date=ydate,
                           fields="ts_code,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount")
        if df is not None and len(df) > 0:
            df["big_net"] = (
                pd.to_numeric(df["buy_elg_amount"],  errors="coerce").fillna(0) +
                pd.to_numeric(df["buy_lg_amount"],   errors="coerce").fillna(0) -
                pd.to_numeric(df["sell_elg_amount"], errors="coerce").fillna(0) -
                pd.to_numeric(df["sell_lg_amount"],  errors="coerce").fillna(0)
            )
            for _, row in df.nlargest(8, "big_net").iterrows():
                net = safe_float(row.get("big_net", 0))
                if net > 0:
                    code = row["ts_code"]
                    result["top_moneyflow"].append({
                        "code":    code,
                        "name":    names_map.get(code, ("", ""))[0],
                        "industry": names_map.get(code, ("", ""))[1],
                        "net_yi":  round(net / 1e4, 2),  # moneyflow金额单位万元，÷1e4得亿
                    })
        print(f"  昨日主力净流入：{len(result['top_moneyflow'])}只")
    except Exception as e:
        print(f"  昨日主力资金失败: {e}")

    return result



# ══════════════════════════════════════════════════════════
#  模块三：收盘Tushare全量数据
# ══════════════════════════════════════════════════════════

def get_daily_data(date=None) -> tuple:
    """收盘行情 + 基础指标"""
    d = date or TODAY
    print(f"【行情】收盘数据 {d}...")
    try:
        price = pro.daily(
            trade_date=d,
            fields="ts_code,open,high,low,close,pre_close,change,pct_chg,vol,amount"
        )
        basic = pro.daily_basic(
            trade_date=d,
            fields="ts_code,close,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv,pct_chg"
        )
        price_ok = price is not None and isinstance(price, pd.DataFrame) and not price.empty
        basic_ok = basic is not None and isinstance(basic, pd.DataFrame) and not basic.empty
        if price_ok and basic_ok:
            print(f"  收盘数据：{len(price)}只")
        return (price if price_ok else pd.DataFrame()), (basic if basic_ok else pd.DataFrame())
    except Exception as e:
        print(f"  收盘数据失败: {e}")
        return pd.DataFrame(), pd.DataFrame()


def get_stock_names() -> dict:
    try:
        df = pro.stock_basic(exchange="", list_status="L",
                             fields="ts_code,name,industry")
        if df is not None:
            return {row["ts_code"]: (row["name"], row["industry"])
                    for _, row in df.iterrows()}
    except Exception as e:
        print(f"  股票名称失败: {e}")
    return {}


def get_stk_factor(date=None) -> pd.DataFrame:
    """批量技术因子（6000积分）"""
    d = date or TODAY
    print(f"【技术因子】批量获取 {d}...")
    try:
        df = pro.stk_factor(
            trade_date=d,
            fields="ts_code,close,ma5,ma10,ma20,ma60,dif,dea,macd,kdj_k,kdj_d,kdj_j,rsi_6,rsi_12,boll_upper,boll_mid,boll_lower,volume_ratio"
        )
        if df is not None and len(df) > 0:
            print(f"  技术因子：{len(df)}只")
            return df
        print("  技术因子：今日未入库")
        return pd.DataFrame()
    except Exception as e:
        print(f"  技术因子失败: {e}")
        return pd.DataFrame()


def get_northbound() -> dict:
    print("【资金】北向资金...")
    result = {"total": 0, "sh": 0, "sz": 0, "top_stocks": []}
    try:
        df = pro.moneyflow_hsgt(start_date=TODAY, end_date=TODAY)
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            result["sh"]    = round(safe_float(row.get("sh_hgt")) / 1e8, 2)
            result["sz"]    = round(safe_float(row.get("sz_hgt")) / 1e8, 2)
            result["total"] = round(safe_float(row.get("north_money")) / 1e8, 2)
        df_top = pro.hsgt_top10(trade_date=TODAY, market_type="N")
        if df_top is not None and len(df_top) > 0:
            result["top_stocks"] = df_top[["name", "net_amount"]].head(10).to_dict("records")
        print(f"  北向资金：{result['total']}亿")
    except Exception as e:
        print(f"  北向资金失败: {e}")
    return result


def get_moneyflow() -> list:
    print("【资金】主力资金流向...")
    result = []
    try:
        df = pro.moneyflow(
            trade_date=TODAY,
            fields="ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount"
        )
        if df is None or len(df) == 0:
            print("  主力资金：未入库")
            return result
        df["big_net"] = (
            pd.to_numeric(df["buy_elg_amount"],  errors="coerce").fillna(0) +
            pd.to_numeric(df["buy_lg_amount"],   errors="coerce").fillna(0) -
            pd.to_numeric(df["sell_elg_amount"], errors="coerce").fillna(0) -
            pd.to_numeric(df["sell_lg_amount"],  errors="coerce").fillna(0)
        )
        for _, row in df.nlargest(15, "big_net").iterrows():
            net = safe_float(row.get("big_net"))
            if net > 0:
                result.append({
                    "code":        row["ts_code"],
                    "net_flow_yi": round(net / 1e4, 2),  # moneyflow金额单位万元，÷1e4得亿
                })
        print(f"  主力净流入：{len(result)}只")
    except Exception as e:
        print(f"  主力资金失败: {e}")
    return result[:10]


def get_dragon_tiger() -> list:
    print("【资金】龙虎榜...")
    result = []
    try:
        df_list = pro.top_list(trade_date=TODAY)
        df_inst = pro.top_inst(trade_date=TODAY)
        if df_inst is not None and len(df_inst) > 0:
            for code in df_inst["ts_code"].unique()[:10]:
                name = code
                if df_list is not None:
                    row = df_list[df_list["ts_code"] == code]
                    if len(row) > 0:
                        name = row["name"].values[0]
                result.append({"code": code, "name": name, "signal": "机构席位买入"})
        print(f"  龙虎榜机构：{len(result)}只")
    except Exception as e:
        print(f"  龙虎榜失败: {e}")
    return result[:8]


def get_block_trade() -> list:
    print("【资金】大宗交易...")
    result = []
    try:
        df = pro.block_trade(trade_date=TODAY)
        if df is not None and len(df) > 0:
            df["discount_rate"] = pd.to_numeric(df["discount_rate"], errors="coerce")
            for _, row in df[df["discount_rate"] < -2].nsmallest(8, "discount_rate").iterrows():
                result.append({
                    "name":          row.get("name", ""),
                    "code":          row.get("ts_code", ""),
                    "amount_wan":    round(safe_float(row.get("amount")) / 1e4, 0),
                    "discount_rate": round(safe_float(row.get("discount_rate")), 2),
                })
        print(f"  大宗折价：{len(result)}条")
    except Exception as e:
        print(f"  大宗交易失败: {e}")
    return result


def get_sector_flow() -> list:
    print("【资金】行业资金流向...")
    result = []
    try:
        df = pro.moneyflow_ind_ths(trade_date=TODAY)
        if df is not None and len(df) > 0:
            df["net_amount"] = pd.to_numeric(df.get("net_amount", 0), errors="coerce")
            for _, row in df.nlargest(8, "net_amount").iterrows():
                net = safe_float(row.get("net_amount"))
                if net > 0:
                    result.append({
                        "sector":      row.get("industry", ""),
                        "net_flow_yi": round(net, 2),  # moneyflow_ind_ths net_amount单位已是亿元，不除
                    })
            print(f"  行业资金：{len(result)}个板块")
        else:
            print("  行业资金：暂无数据")
    except Exception as e:
        print(f"  行业资金失败: {e}")
    return result


def get_broker_recommend() -> list:
    print("【券商】金股推荐...")
    result = []
    try:
        month = _bj.strftime("%Y%m")
        df = pro.broker_recommend(month=month, fields="ts_code,name,broker,reason")
        if df is None or len(df) == 0:
            last = (_bj - datetime.timedelta(days=30)).strftime("%Y%m")
            df = pro.broker_recommend(month=last, fields="ts_code,name,broker,reason")
        if df is not None and len(df) > 0:
            rc = df.groupby("ts_code").agg(
                name=("name", "first"),
                broker_count=("broker", "count"),
                brokers=("broker", lambda x: "、".join(x.head(3)))
            ).reset_index()
            for _, row in rc.nlargest(8, "broker_count").iterrows():
                result.append({
                    "code":         row["ts_code"],
                    "name":         row["name"],
                    "broker_count": int(row["broker_count"]),
                    "brokers":      row["brokers"],
                })
            print(f"  券商金股：{len(result)}只")
    except Exception as e:
        print(f"  券商金股失败: {e}")
    return result[:6]


# ══════════════════════════════════════════════════════════
#  模块：周报数据（周末模式）
# ══════════════════════════════════════════════════════════

def get_weekly_index_perf(week_dates: list) -> list:
    """本周主要指数涨跌幅。week_dates为本周交易日升序列表。"""
    if not week_dates:
        return []
    start_d, end_d = week_dates[0], week_dates[-1]
    indices = [
        ("000001.SH", "上证指数"),
        ("399001.SZ", "深证成指"),
        ("399006.SZ", "创业板指"),
        ("000688.SH", "科创50"),
        ("000300.SH", "沪深300"),
        ("000905.SH", "中证500"),
    ]
    out = []
    for code, name in indices:
        try:
            df = pro.index_daily(ts_code=code, start_date=start_d, end_date=end_d,
                                 fields="trade_date,close,pct_chg")
            if df is None or len(df) == 0:
                continue
            df = df.sort_values("trade_date")
            open_close = safe_float(df.iloc[0]["close"]) - safe_float(df.iloc[0]["pct_chg"]) * 0  # 占位
            first_close = safe_float(df.iloc[0]["close"])
            last_close  = safe_float(df.iloc[-1]["close"])
            # 周涨跌幅 = 各日pct_chg复合
            cum = 1.0
            for _, r in df.iterrows():
                cum *= (1 + safe_float(r["pct_chg"]) / 100)
            week_chg = round((cum - 1) * 100, 2)
            out.append({
                "code": code, "name": name,
                "close": round(last_close, 2),
                "week_chg": week_chg,
                "days": len(df),
            })
        except Exception as e:
            print(f"  指数{code}失败: {e}")
    print(f"  本周指数：{len(out)}个")
    return out


def get_weekly_sector_flow(week_dates: list) -> dict:
    """本周行业资金累计净流入（亿元）。返回 {流入TOP, 流出TOP}。"""
    if not week_dates:
        return {"inflow": [], "outflow": []}
    agg = {}
    for d in week_dates:
        try:
            df = pro.moneyflow_ind_ths(trade_date=d)
            if df is None or len(df) == 0:
                continue
            for _, row in df.iterrows():
                ind = row.get("industry", "")
                if not ind:
                    continue
                # moneyflow_ind_ths net_amount 单位已是亿元
                agg[ind] = agg.get(ind, 0.0) + safe_float(row.get("net_amount", 0))
        except Exception as e:
            print(f"  {d}行业资金失败: {e}")
    items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
    inflow  = [{"sector": s, "net_yi": round(v, 2)} for s, v in items[:8]  if v > 0]
    outflow = [{"sector": s, "net_yi": round(v, 2)} for s, v in items[-8:] if v < 0]
    outflow.reverse()  # 流出最大在前
    print(f"  本周行业资金：流入{len(inflow)} / 流出{len(outflow)}")
    return {"inflow": inflow, "outflow": outflow}


def get_weekly_top_stocks(week_dates: list, names_map: dict) -> list:
    """本周个股累计涨跌幅TOP（按区间首日开盘到末日收盘）。过滤ST/北交所。"""
    if len(week_dates) < 2:
        return []
    start_d, end_d = week_dates[0], week_dates[-1]
    try:
        df_start = pro.daily(trade_date=start_d, fields="ts_code,pre_close,open")
        df_end   = pro.daily(trade_date=end_d,   fields="ts_code,close")
        if df_start is None or df_end is None or df_start.empty or df_end.empty:
            return []
        df = df_start.merge(df_end, on="ts_code", suffixes=("_s", "_e"))
        df["pre_close"] = pd.to_numeric(df["pre_close"], errors="coerce")
        df["close"]     = pd.to_numeric(df["close"],     errors="coerce")
        df = df.dropna(subset=["pre_close", "close"])
        df = df[df["pre_close"] > 0]
        df["week_pct"] = (df["close"] / df["pre_close"] - 1) * 100
        # 过滤北交所
        df = df[~df["ts_code"].str.endswith(".BJ")]
        df = df.sort_values("week_pct", ascending=False)
        out = []
        for _, r in df.head(40).iterrows():
            code = r["ts_code"]
            name, industry = names_map.get(code, ("", ""))
            if not name:
                continue
            if name.startswith("ST") or name.startswith("*ST") or "退" in name:
                continue
            out.append({
                "code": code, "name": name, "industry": industry,
                "week_pct": round(r["week_pct"], 2),
            })
            if len(out) >= 15:
                break
        print(f"  本周强势个股：{len(out)}只")
        return out
    except Exception as e:
        print(f"  本周强势个股失败: {e}")
        return []


def get_weekly_news() -> list:
    """
    本周(周一→今天)CLS新闻汇总，按关键词筛出重大政策/事件/影响大盘的新闻。
    返回 [{date, time, title}]，已去重。
    """
    keywords = [
        # 货币/财政/监管
        "央行", "降准", "降息", "MLF", "LPR", "逆回购", "国常会", "国务院", "政治局",
        "证监会", "国资委", "财政部", "发改委", "工信部",
        # 大盘催化
        "A股", "沪深", "万亿", "新高", "暴跌", "暴涨", "熔断",
        # 产业政策
        "AI", "人工智能", "半导体", "芯片", "新能源", "光伏", "储能", "机器人",
        "军工", "国防", "稀土", "数据要素", "算力",
        # 国际/宏观
        "关税", "贸易", "美联储", "加息", "降息", "特朗普", "拜登",
        # 重大事件
        "IPO", "退市", "重组", "停牌", "复牌", "破发",
    ]
    monday = (_bj - datetime.timedelta(days=_bj.weekday())).strftime("%Y-%m-%d")
    today_str = _bj.strftime("%Y-%m-%d")
    print(f"【本周新闻】{monday} → {today_str}")
    try:
        df = pro.news(src="cls",
                      start_date=monday + " 00:00:00",
                      end_date=today_str + " 23:59:59",
                      fields="datetime,title")
        if df is None or len(df) == 0:
            return []
        seen, out = set(), []
        for _, row in df.iterrows():
            title = str(row.get("title", "")).strip()
            if len(title) < 8 or title in seen:
                continue
            if not any(k in title for k in keywords):
                continue
            seen.add(title)
            dt = str(row.get("datetime", ""))
            out.append({
                "date": dt[:10],
                "time": dt[11:16],
                "title": title[:120],
            })
        # 按时间倒序展示，最多40条
        out.sort(key=lambda x: (x["date"], x["time"]), reverse=True)
        print(f"  本周重要新闻：{len(out)}条（截断展示40条）")
        return out[:40]
    except Exception as e:
        print(f"  本周新闻失败: {e}")
        return []


def quant_and_tech_filter(price_df, basic_df, names, factor_df) -> list:
    """量化筛选 + 技术因子评分"""
    print("【筛选】量化+技术精筛...")
    if price_df.empty or basic_df.empty:
        print("  数据不足，跳过")
        return []
    try:
        df = pd.merge(basic_df, price_df[["ts_code","pct_chg","vol","amount"]],
                      on="ts_code", how="inner", suffixes=("","_p"))
        for col in ["circ_mv","turnover_rate","volume_ratio","pe","pct_chg"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[
            (df["circ_mv"]       >= 500000)  &
            (df["circ_mv"]       <= 5000000) &
            (df["pct_chg"]       >= 1.5)    &
            (df["pct_chg"]       <= 9.5)    &
            (df["turnover_rate"] >= 1.0)    &
            (df["turnover_rate"] <= 15.0)   &
            (df["volume_ratio"]  >= 1.2)    &
            (df["pe"]            > 0)
        ].nlargest(40, "volume_ratio")

        # 构建因子索引
        factor_map = {}
        if not factor_df.empty:
            for _, row in factor_df.iterrows():
                factor_map[row["ts_code"]] = row

        result = []
        for _, row in df.iterrows():
            code     = row["ts_code"]
            name_inf = names.get(code, ("未知", ""))
            stock = {
                "code":          code,
                "name":          name_inf[0],
                "industry":      name_inf[1],
                "change_pct":    round(safe_float(row.get("pct_chg")), 2),
                "market_cap_yi": round(safe_float(row.get("circ_mv")) / 10000, 1),
                "turnover_rate": round(safe_float(row.get("turnover_rate")), 2),
                "volume_ratio":  round(safe_float(row.get("volume_ratio")), 2),
                "pe":            round(safe_float(row.get("pe")), 1),
                "pb":            round(safe_float(row.get("pb")), 2),
            }

            # 技术评分
            frow  = factor_map.get(code)
            score = 0
            tech  = {}
            if frow is not None:
                close = safe_float(frow.get("close"))
                ma5   = safe_float(frow.get("ma5"))
                ma10  = safe_float(frow.get("ma10"))
                ma20  = safe_float(frow.get("ma20"))
                dif   = safe_float(frow.get("dif"))
                dea   = safe_float(frow.get("dea"))
                kdj_k = safe_float(frow.get("kdj_k"))
                kdj_d = safe_float(frow.get("kdj_d"))
                rsi6  = safe_float(frow.get("rsi_6"))
                b_up  = safe_float(frow.get("boll_upper"))
                b_low = safe_float(frow.get("boll_lower"))
                vr    = safe_float(frow.get("volume_ratio"))

                ma_bull   = (close > ma5 > ma10 > ma20) if all([close,ma5,ma10,ma20]) else False
                macd_bull = (dif > dea > 0)             if all([dif,dea])             else False
                kdj_bull  = (kdj_k > kdj_d)             if all([kdj_k,kdj_d])         else False
                vol_exp   = vr > 1.1                    if vr                          else False
                rsi_ok    = (40 < rsi6 < 75)            if rsi6                        else False
                bb_range  = b_up - b_low                if (b_up and b_low and b_up > b_low) else 0
                bb_pos    = (close - b_low) / bb_range  if bb_range > 0               else 0.5

                if ma_bull:                   score += 3
                if macd_bull:                 score += 3
                if vol_exp:                   score += 2
                if kdj_bull:                  score += 1
                if rsi_ok:                    score += 1
                if 0.2 < bb_pos < 0.8:        score += 1
                if stock["change_pct"] >= 3:  score += 1

                tech = {
                    "ma_bullish":    ma_bull,
                    "macd_bullish":  macd_bull,
                    "kdj_bullish":   kdj_bull,
                    "vol_expanding": vol_exp,
                    "rsi_6":         round(rsi6, 1) if rsi6 else None,
                    "bb_position":   round(bb_pos, 2),
                }
            else:
                if stock["change_pct"] >= 3: score += 1

            stock["tech"]       = tech
            stock["tech_score"] = score
            if score >= 5:
                result.append(stock)

        result.sort(key=lambda x: x["tech_score"], reverse=True)
        print(f"  精筛结果：{len(result[:15])}只")
        return result[:15]
    except Exception as e:
        print(f"  筛选失败: {e}")
        return []


# ══════════════════════════════════════════════════════════
#  B轨「潜伏标的」：政策×行业×资金×筹码×形态 综合打分
# ══════════════════════════════════════════════════════════

# 政策关键词清单（来源：政府工作报告、二十届三中全会、十四五规划、国常会议题、
# 战略性新兴产业目录）。可随时增删，不需改其他代码。
POLICY_KEYWORDS = [
    # 新质生产力 / AI / 算力
    "新质生产力", "人工智能", "大模型", "算力", "数据中心", "数据要素", "大数据",
    # 半导体
    "半导体", "集成电路", "芯片", "EDA", "第三代半导体", "先进封装", "光刻",
    # 机器人 / 智能驾驶
    "机器人", "人形机器人", "工业机器人", "具身智能", "自动驾驶", "智能驾驶",
    # 低空经济 / 商业航天
    "低空经济", "通用航空", "eVTOL", "无人机", "商业航天", "卫星互联网", "北斗",
    # 新能源
    "新能源", "储能", "固态电池", "钠离子电池", "光伏", "风电", "氢能", "燃料电池",
    # 军工
    "军工", "国防", "核工业", "海工装备", "航天装备", "导弹", "雷达",
    # 前沿科技
    "可控核聚变", "量子科技", "量子通信", "量子计算", "6G", "脑机接口",
    # 医药
    "创新药", "合成生物", "基因治疗", "医疗器械", "中医药", "脑科学",
    # 高端制造
    "高端制造", "工业母机", "数控机床", "工业软件", "智能制造", "数字孪生",
    # 新材料
    "新材料", "稀土", "钨", "钼", "锑", "高温合金", "碳纤维", "石墨烯",
    # 国企改革
    "国企改革", "中特估", "央企",
]

# 公认的"机构席位"关键词，用于识别十大流通股东里的机构持仓
INSTITUTIONAL_KEYWORDS = ["基金", "社保", "保险", "QFII", "证券", "信托", "养老",
                          "资管", "汇金", "中央汇金", "证金"]

# SQLite缓存文件路径
CACHE_DB = os.environ.get("CACHE_DB_PATH", "/tmp/scanner_cache.db")


# ───────── SQLite 缓存层 ─────────
def _init_cache_db():
    """初始化缓存数据库（首次运行时创建表）"""
    conn = sqlite3.connect(CACHE_DB)
    c = conn.cursor()
    # 日线缓存：(代码, 日期) 唯一
    c.execute("""CREATE TABLE IF NOT EXISTS daily_cache (
        ts_code TEXT, trade_date TEXT, close REAL, vol REAL, amount REAL,
        pct_chg REAL, high REAL, low REAL,
        PRIMARY KEY (ts_code, trade_date)
    )""")
    # 季度数据缓存：股东户数 / 财务 / 十大流通股东 都用这个
    c.execute("""CREATE TABLE IF NOT EXISTS quarterly_cache (
        ts_code TEXT, dtype TEXT, end_date TEXT, payload TEXT,
        fetched_at TEXT,
        PRIMARY KEY (ts_code, dtype, end_date)
    )""")
    # 大股东增减持缓存（90天滚动）
    c.execute("""CREATE TABLE IF NOT EXISTS holder_trade_cache (
        ts_code TEXT PRIMARY KEY, net_change REAL, fetched_at TEXT
    )""")
    # 元数据：记录"上次冷启动"等
    c.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT
    )""")
    conn.commit()
    return conn


def _get_cached_daily(conn, ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从SQLite读历史日线"""
    q = """SELECT ts_code,trade_date,close,vol,amount,pct_chg,high,low
           FROM daily_cache WHERE ts_code=? AND trade_date>=? AND trade_date<=?
           ORDER BY trade_date"""
    return pd.read_sql_query(q, conn, params=(ts_code, start_date, end_date))


def _save_daily(conn, df: pd.DataFrame):
    """写入日线数据"""
    if df is None or df.empty:
        return
    rows = [(r["ts_code"], r["trade_date"],
             safe_float(r.get("close")), safe_float(r.get("vol")),
             safe_float(r.get("amount")), safe_float(r.get("pct_chg")),
             safe_float(r.get("high")), safe_float(r.get("low")))
            for _, r in df.iterrows()]
    conn.executemany("""INSERT OR REPLACE INTO daily_cache
        (ts_code,trade_date,close,vol,amount,pct_chg,high,low)
        VALUES (?,?,?,?,?,?,?,?)""", rows)
    conn.commit()


def _ensure_daily_history(conn, candidates: list, end_date: str, lookback_days=250):
    """
    保证候选池每只股票都有 lookback_days 的历史日线缓存。
    冷启动慢（按日逐天调daily取全市场），增量快（只补缺失日）。
    """
    start_dt = datetime.datetime.strptime(end_date, "%Y%m%d") - datetime.timedelta(days=int(lookback_days*1.6))
    start_date = start_dt.strftime("%Y%m%d")

    # 找出缓存里已有的最新交易日
    c = conn.cursor()
    c.execute("SELECT MAX(trade_date) FROM daily_cache")
    row = c.fetchone()
    cached_max = row[0] if row and row[0] else None

    # 决定需要补的日期范围
    if cached_max and cached_max >= start_date:
        fetch_start = (datetime.datetime.strptime(cached_max, "%Y%m%d") + datetime.timedelta(days=1)).strftime("%Y%m%d")
    else:
        fetch_start = start_date

    if fetch_start > end_date:
        print(f"  日线缓存已就绪（最新{cached_max}）")
        return

    # 拿交易日历，逐日按 trade_date 拉全市场（高效，每次1个API调用就有5000+条）
    try:
        cal = pro.trade_cal(exchange="", start_date=fetch_start, end_date=end_date, is_open="1")
        trade_dates = sorted(cal["cal_date"].tolist()) if cal is not None and len(cal) > 0 else []
    except Exception as e:
        print(f"  trade_cal失败: {e}")
        return

    # 冷启动保护：单次最多拉60个交易日，剩下交给后续运行慢慢补
    # （这样首次执行不会超时，几次后历史就齐了）
    if len(trade_dates) > 60:
        print(f"  冷启动：检测到{len(trade_dates)}个交易日待拉取，本次只拉最近60天，剩余下次补")
        trade_dates = trade_dates[-60:]

    print(f"  日线缓存增量拉取：{trade_dates[0]} → {end_date}（共{len(trade_dates)}个交易日）")
    for i, d in enumerate(trade_dates):
        try:
            df = pro.daily(trade_date=d,
                           fields="ts_code,trade_date,close,vol,amount,pct_chg,high,low")
            if df is not None and len(df) > 0:
                _save_daily(conn, df)
            if (i+1) % 20 == 0:
                print(f"    进度 {i+1}/{len(trade_dates)}")
            time.sleep(0.3)  # 控速，避免触发频次限制
        except Exception as e:
            print(f"    {d} 失败: {e}")
            time.sleep(2)


def _get_cached_quarterly(conn, ts_code: str, dtype: str, max_age_days=80):
    """读取季度数据缓存。返回最新payload(JSON字符串)或None。"""
    c = conn.cursor()
    c.execute("""SELECT end_date, payload, fetched_at FROM quarterly_cache
                 WHERE ts_code=? AND dtype=? ORDER BY end_date DESC LIMIT 4""",
              (ts_code, dtype))
    rows = c.fetchall()
    if not rows:
        return None
    # 看最新一条是否还新鲜
    latest = rows[0]
    fetched = datetime.datetime.strptime(latest[2][:10], "%Y-%m-%d") if latest[2] else datetime.datetime(2000,1,1)
    if (datetime.datetime.utcnow() - fetched).days > max_age_days:
        return None
    return rows  # 返回最多4个季度


def _save_quarterly(conn, ts_code: str, dtype: str, end_date: str, payload: str):
    conn.execute("""INSERT OR REPLACE INTO quarterly_cache
        (ts_code,dtype,end_date,payload,fetched_at) VALUES (?,?,?,?,?)""",
        (ts_code, dtype, end_date, payload,
         datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()


# ───────── B轨候选池准入筛选 ─────────
def _eligible_candidates(price_df, basic_df, names_map) -> pd.DataFrame:
    """
    宽松准入：剔除绝对不要的（ST/北交所/次新/市值过小过大/流动性差）
    其他全进打分模型，保证有票出。
    """
    if price_df.empty or basic_df.empty:
        return pd.DataFrame()
    df = pd.merge(basic_df, price_df[["ts_code","pct_chg","vol","amount","high","low"]],
                  on="ts_code", how="inner", suffixes=("","_p"))
    for col in ["circ_mv","turnover_rate","volume_ratio","pe","pct_chg","amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 市值 30-800 亿（circ_mv 单位万元：300000 - 8000000）
    df = df[(df["circ_mv"] >= 300000) & (df["circ_mv"] <= 8000000)]
    # 北交所排除
    df = df[~df["ts_code"].str.endswith(".BJ")]
    # 当日成交额 ≥ 5000 万（流动性兜底）
    df = df[df["amount"].fillna(0) >= 5000]

    # 通过 names_map 排除 ST/退市/次新
    def _name_ok(code):
        name = names_map.get(code, ("", ""))[0]
        if not name:
            return False
        if name.startswith("ST") or name.startswith("*ST") or "退" in name:
            return False
        return True
    df = df[df["ts_code"].apply(_name_ok)]
    return df.reset_index(drop=True)


# ───────── B轨各项评分函数 ─────────
def _score_policy(industry: str, name: str) -> tuple:
    """政策赛道命中：满分6分。返回 (得分, 命中的关键词)"""
    text = (industry or "") + " " + (name or "")
    hits = [k for k in POLICY_KEYWORDS if k in text]
    if not hits:
        return 0, []
    # 命中越多分越高，但单次最多6分
    return min(6, 3 + len(hits) * 2), hits


def _score_sector_momentum(industry: str, sector_30d_rank: dict) -> int:
    """行业景气度：满分4。所属行业近30日资金流入排名前30%"""
    if not industry or industry not in sector_30d_rank:
        return 0
    rank, total = sector_30d_rank[industry]
    pct = rank / max(total, 1)
    if pct <= 0.1:  return 4
    if pct <= 0.2:  return 3
    if pct <= 0.3:  return 2
    if pct <= 0.5:  return 1
    return 0


def _score_market_cap(circ_mv_wan: float) -> int:
    """市值弹性：满分2。50-200亿最优"""
    yi = circ_mv_wan / 10000
    if 50 <= yi <= 200:  return 2
    if 30 <= yi < 50 or 200 < yi <= 500:  return 1
    return 0


def _score_form(hist_df: pd.DataFrame) -> tuple:
    """
    形态评分：底部充分(4) + 均线粘合(4) + 放量初现(3) = 满分11
    输入 hist_df: 最近250天日线（升序）
    返回 (得分细项 dict, 总分)
    """
    parts = {"bottom": 0, "ma_glue": 0, "vol_emerge": 0}
    if hist_df is None or len(hist_df) < 60:
        return parts, 0

    # 1. 底部充分（4分）：250日内最高/最低 < 1.5
    h_max = hist_df["high"].max()
    l_min = hist_df["low"][hist_df["low"] > 0].min()
    if l_min and l_min > 0:
        amp = h_max / l_min
        if amp < 1.3:    parts["bottom"] = 4
        elif amp < 1.5:  parts["bottom"] = 3
        elif amp < 1.8:  parts["bottom"] = 2
        elif amp < 2.2:  parts["bottom"] = 1

    # 2. 均线粘合（4分）：MA5/10/20/60 的极差/当前价 < 5%
    closes = hist_df["close"].astype(float).tolist()
    if len(closes) >= 60:
        ma5  = sum(closes[-5:])  / 5
        ma10 = sum(closes[-10:]) / 10
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        cur  = closes[-1]
        if cur > 0:
            mas = [ma5, ma10, ma20, ma60]
            spread = (max(mas) - min(mas)) / cur
            if spread < 0.02:   parts["ma_glue"] = 4
            elif spread < 0.04: parts["ma_glue"] = 3
            elif spread < 0.06: parts["ma_glue"] = 2
            elif spread < 0.10: parts["ma_glue"] = 1

    # 3. 放量初现（3分）：近5日均量 / 近60日均量
    vols = hist_df["vol"].astype(float).tolist()
    if len(vols) >= 60:
        v5  = sum(vols[-5:])  / 5
        v60 = sum(vols[-60:]) / 60
        if v60 > 0:
            ratio = v5 / v60
            if 1.5 <= ratio <= 3.0:  parts["vol_emerge"] = 3  # 温和放量
            elif 1.2 <= ratio < 1.5: parts["vol_emerge"] = 2
            elif 3.0 < ratio <= 5.0: parts["vol_emerge"] = 1  # 偏大但未天量
            # >5 不给分（天量风险）

    return parts, sum(parts.values())


def _get_chip_concentration_score(conn, ts_code: str) -> tuple:
    """
    筹码集中（股东户数）：满分5。连续两季下降满分，一季下降部分分。
    返回 (得分, 描述)
    """
    cached = _get_cached_quarterly(conn, ts_code, "holdernumber", max_age_days=80)
    if cached is None:
        try:
            df = pro.stk_holdernumber(ts_code=ts_code,
                                      start_date=(_bj - datetime.timedelta(days=400)).strftime("%Y%m%d"),
                                      end_date=TODAY,
                                      fields="ts_code,end_date,holder_num")
            if df is None or df.empty:
                return 0, ""
            df = df.sort_values("end_date", ascending=False).head(4)
            for _, r in df.iterrows():
                _save_quarterly(conn, ts_code, "holdernumber", r["end_date"], str(int(r["holder_num"])))
            cached = [(r["end_date"], str(int(r["holder_num"])), "") for _, r in df.iterrows()]
        except Exception as e:
            return 0, ""

    if not cached or len(cached) < 2:
        return 0, ""
    try:
        nums = [int(c[1]) for c in cached if c[1]]
    except Exception:
        return 0, ""
    if len(nums) < 2:
        return 0, ""

    # cached是按end_date降序的
    if len(nums) >= 3 and nums[0] < nums[1] < nums[2]:
        chg = (nums[0] - nums[2]) / max(nums[2], 1) * 100
        return 5, f"户数连续2季下降{chg:.0f}%"
    if nums[0] < nums[1]:
        chg = (nums[0] - nums[1]) / max(nums[1], 1) * 100
        return 3, f"户数下降{chg:.0f}%"
    return 0, ""


def _get_institution_score(conn, ts_code: str) -> tuple:
    """
    机构持仓：满分4。十大流通股东里有≥2个机构席位满分，1个机构2分。
    返回 (得分, 描述)
    """
    cached = _get_cached_quarterly(conn, ts_code, "top10float", max_age_days=80)
    if cached is None:
        try:
            df = pro.top10_floatholders(ts_code=ts_code,
                                        start_date=(_bj - datetime.timedelta(days=180)).strftime("%Y%m%d"),
                                        end_date=TODAY,
                                        fields="ts_code,end_date,holder_name,hold_amount")
            if df is None or df.empty:
                return 0, ""
            latest = df["end_date"].max()
            df_latest = df[df["end_date"] == latest]
            holders = "|".join(df_latest["holder_name"].tolist())
            _save_quarterly(conn, ts_code, "top10float", latest, holders)
            cached = [(latest, holders, "")]
        except Exception:
            return 0, ""

    if not cached:
        return 0, ""
    holders_str = cached[0][1] if cached[0][1] else ""
    holders = holders_str.split("|")
    inst_count = sum(1 for h in holders if any(k in h for k in INSTITUTIONAL_KEYWORDS))
    if inst_count >= 3:  return 4, f"前十有{inst_count}个机构"
    if inst_count == 2:  return 3, f"前十有2个机构"
    if inst_count == 1:  return 2, f"前十有1个机构"
    return 0, ""


def _get_holder_trade_score(conn, ts_code: str) -> tuple:
    """
    大股东增减持：满分3。近90日净增持满分，净减持0分。
    缓存7天。
    """
    c = conn.cursor()
    c.execute("SELECT net_change, fetched_at FROM holder_trade_cache WHERE ts_code=?", (ts_code,))
    row = c.fetchone()
    net_chg = None
    if row:
        try:
            fetched = datetime.datetime.strptime(row[1][:10], "%Y-%m-%d")
            if (datetime.datetime.utcnow() - fetched).days < 7:
                net_chg = row[0]
        except Exception:
            net_chg = None

    if net_chg is None:
        try:
            start_d = (_bj - datetime.timedelta(days=90)).strftime("%Y%m%d")
            df = pro.stk_holdertrade(ts_code=ts_code, start_date=start_d, end_date=TODAY)
            if df is not None and not df.empty:
                # in_de: IN=增持, DE=减持。change_vol是股数
                signs = df["in_de"].map({"IN": 1, "DE": -1}).fillna(0)
                net = (signs * df["change_vol"].astype(float).fillna(0)).sum()
                net_chg = float(net)
            else:
                net_chg = 0.0
            conn.execute("""INSERT OR REPLACE INTO holder_trade_cache
                (ts_code, net_change, fetched_at) VALUES (?,?,?)""",
                (ts_code, net_chg,
                 datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
        except Exception:
            return 0, ""

    if net_chg is None or net_chg == 0:
        return 0, ""
    if net_chg > 0:
        return 3, f"大股东近90日净增持{net_chg/1e6:.1f}百万股"
    return 0, "大股东净减持（已扣减）"


def _get_earnings_score(conn, ts_code: str) -> tuple:
    """
    业绩改善：满分5。最近一季净利润同比 >50%(5), >30%(4), >0(2), 扭亏(4), 负(0)
    """
    cached = _get_cached_quarterly(conn, ts_code, "fina", max_age_days=80)
    if cached is None:
        try:
            df = pro.fina_indicator(ts_code=ts_code,
                                    start_date=(_bj - datetime.timedelta(days=400)).strftime("%Y%m%d"),
                                    end_date=TODAY,
                                    fields="ts_code,end_date,netprofit_yoy,q_netprofit_yoy")
            if df is None or df.empty:
                return 0, ""
            df = df.sort_values("end_date", ascending=False).head(1)
            r = df.iloc[0]
            yoy = safe_float(r.get("q_netprofit_yoy")) or safe_float(r.get("netprofit_yoy"))
            _save_quarterly(conn, ts_code, "fina", r["end_date"], f"{yoy:.2f}")
            cached = [(r["end_date"], f"{yoy:.2f}", "")]
        except Exception:
            return 0, ""

    if not cached:
        return 0, ""
    try:
        yoy = float(cached[0][1])
    except Exception:
        return 0, ""
    if yoy >= 50:    return 5, f"净利润同比+{yoy:.0f}%"
    if yoy >= 30:    return 4, f"净利润同比+{yoy:.0f}%"
    if yoy >= 0:     return 2, f"净利润同比+{yoy:.0f}%"
    return 0, f"净利润同比{yoy:.0f}%"


def _score_overheat(hist_df: pd.DataFrame) -> tuple:
    """
    防过热：满分3。近20日涨幅 <20%(3), 20-40%(1), >40%(0)
    """
    if hist_df is None or len(hist_df) < 20:
        return 3, ""
    closes = hist_df["close"].astype(float).tolist()
    if len(closes) < 20 or closes[-20] <= 0:
        return 3, ""
    chg20 = (closes[-1] / closes[-20] - 1) * 100
    if chg20 < 20:  return 3, f"20日{chg20:+.0f}%"
    if chg20 < 40:  return 1, f"20日{chg20:+.0f}%（偏热）"
    return 0, f"20日{chg20:+.0f}%（过热）"


# ───────── B轨主入口 ─────────
def find_potential_stocks(price_df, basic_df, names_map,
                          sector_30d_rank: dict, top_n=15) -> list:
    """
    B轨「潜伏标的」综合打分。
    满分约 40 分（政策6 + 行业4 + 市值2 + 形态11 + 筹码5 + 机构4 + 增持3 + 业绩5 + 防过热3 - 不一定全部满）
    返回 TOP_n 列表（按总分降序）。
    """
    print("【B轨】潜伏标的扫描...")
    df = _eligible_candidates(price_df, basic_df, names_map)
    if df.empty:
        print("  无符合准入条件的标的")
        return []
    print(f"  准入候选：{len(df)}只")

    conn = _init_cache_db()

    # 先用「政策+行业+市值+防过热」快速预筛，把候选池压到 100 只再做重的形态/筹码/财务
    pre_scored = []
    for _, row in df.iterrows():
        code = row["ts_code"]
        name, industry = names_map.get(code, ("", ""))
        s_pol, hits = _score_policy(industry, name)
        s_sec       = _score_sector_momentum(industry, sector_30d_rank)
        s_mv        = _score_market_cap(safe_float(row["circ_mv"]))
        # 没有政策也没有行业景气度，直接不进重算
        if s_pol == 0 and s_sec == 0:
            continue
        pre_scored.append({
            "code": code, "name": name, "industry": industry,
            "circ_mv": safe_float(row["circ_mv"]),
            "_pre": s_pol + s_sec + s_mv,
            "s_pol": s_pol, "s_sec": s_sec, "s_mv": s_mv,
            "policy_hits": hits,
        })
    pre_scored.sort(key=lambda x: x["_pre"], reverse=True)
    candidates = pre_scored[:120]   # 限制到120只做重计算（控制API调用）
    print(f"  政策/行业预筛后：{len(candidates)}只进入深度评分")

    # 保证日线缓存就绪（一次性）
    _ensure_daily_history(conn, candidates, end_date=TODAY, lookback_days=250)

    # 历史日线起止
    start_d = (_bj - datetime.timedelta(days=400)).strftime("%Y%m%d")

    results = []
    for i, stk in enumerate(candidates):
        try:
            hist = _get_cached_daily(conn, stk["code"], start_d, TODAY)

            # 形态
            form_parts, s_form_total = _score_form(hist)
            # 防过热
            s_over, over_desc        = _score_overheat(hist)
            # 筹码
            s_chip, chip_desc        = _get_chip_concentration_score(conn, stk["code"])
            # 机构
            s_inst, inst_desc        = _get_institution_score(conn, stk["code"])
            # 增减持
            s_hldr, hldr_desc        = _get_holder_trade_score(conn, stk["code"])
            # 业绩
            s_earn, earn_desc        = _get_earnings_score(conn, stk["code"])

            total = (stk["s_pol"] + stk["s_sec"] + stk["s_mv"]
                     + s_form_total + s_chip + s_inst + s_hldr + s_earn + s_over)

            results.append({
                "code":     stk["code"],
                "name":     stk["name"],
                "industry": stk["industry"],
                "circ_mv_yi": round(stk["circ_mv"] / 10000, 1),
                "total":    total,
                "scores": {
                    "政策": stk["s_pol"],
                    "行业资金": stk["s_sec"],
                    "市值":   stk["s_mv"],
                    "形态":   s_form_total,
                    "筹码":   s_chip,
                    "机构":   s_inst,
                    "增持":   s_hldr,
                    "业绩":   s_earn,
                    "防过热": s_over,
                },
                "form_detail": form_parts,
                "notes": {
                    "policy":   "、".join(stk["policy_hits"][:3]) if stk["policy_hits"] else "",
                    "chip":     chip_desc,
                    "inst":     inst_desc,
                    "holder":   hldr_desc,
                    "earnings": earn_desc,
                    "overheat": over_desc,
                },
            })
            if (i+1) % 25 == 0:
                print(f"    评分进度 {i+1}/{len(candidates)}")
        except Exception as e:
            print(f"    {stk['code']} 评分失败: {e}")
            continue

    results.sort(key=lambda x: x["total"], reverse=True)
    print(f"  B轨潜伏标的：返回TOP{top_n}（最高分{results[0]['total'] if results else 0}）")
    conn.close()
    return results[:top_n]


def get_sector_30d_rank() -> dict:
    """
    返回 {行业名: (排名, 总数)}，基于近30个交易日 moneyflow_ind_ths 累计净流入。
    用于B轨"行业景气度"评分。
    """
    print("【行业30日资金】累计...")
    end_d = TODAY
    start_d = (_bj - datetime.timedelta(days=45)).strftime("%Y%m%d")
    try:
        cal = pro.trade_cal(exchange="", start_date=start_d, end_date=end_d, is_open="1")
        trade_dates = sorted(cal["cal_date"].tolist())[-30:] if cal is not None else []
    except Exception as e:
        print(f"  trade_cal失败: {e}")
        return {}

    agg = {}
    for d in trade_dates:
        try:
            df = pro.moneyflow_ind_ths(trade_date=d)
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                ind = r.get("industry", "")
                if ind:
                    agg[ind] = agg.get(ind, 0.0) + safe_float(r.get("net_amount", 0))
            time.sleep(0.2)
        except Exception:
            continue

    if not agg:
        return {}
    items = sorted(agg.items(), key=lambda x: x[1], reverse=True)
    total = len(items)
    rank_map = {ind: (i+1, total) for i, (ind, _) in enumerate(items)}
    print(f"  行业30日资金：{total}个板块完成排名")
    return rank_map


# ───────── B轨 HTML 展示 ─────────
def build_potential_html_section(potentials: list) -> str:
    """生成B轨结果的HTML片段，嵌入收盘报告/周报。"""
    if not potentials:
        return """<div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">
          B轨「潜伏标的」 <span style="color:#aaa;font-size:11px;font-weight:400">· 政策×资金×筹码×形态综合评分</span>
        </div>
        <div style="padding:18px;text-align:center;color:#aaa;font-size:12px">暂无符合政策赛道的标的</div>
      </div>"""

    rows = []
    for s in potentials:
        sc = s["scores"]
        notes_chips = []
        if s["notes"]["policy"]:   notes_chips.append(f"<span style='color:#6c5ce7'>政策:{s['notes']['policy']}</span>")
        if s["notes"]["earnings"]: notes_chips.append(f"<span style='color:#d63031'>{s['notes']['earnings']}</span>")
        if s["notes"]["chip"]:     notes_chips.append(f"<span style='color:#0984e3'>{s['notes']['chip']}</span>")
        if s["notes"]["inst"]:     notes_chips.append(f"<span style='color:#00b894'>{s['notes']['inst']}</span>")
        if s["notes"]["holder"]:   notes_chips.append(f"<span style='color:#fdcb6e'>{s['notes']['holder']}</span>")
        notes_html = " · ".join(notes_chips) or "<span style='color:#aaa'>—</span>"

        score_breakdown = " ".join([
            f"<span style='color:#888'>政{sc['政策']}</span>",
            f"<span style='color:#888'>业{sc['业绩']}</span>",
            f"<span style='color:#888'>筹{sc['筹码']}</span>",
            f"<span style='color:#888'>机{sc['机构']}</span>",
            f"<span style='color:#888'>形{sc['形态']}</span>",
            f"<span style='color:#888'>资{sc['行业资金']}</span>",
        ])

        rows.append(f"""
        <tr>
          <td style='padding:6px 8px;font-weight:500'>{s['name']}</td>
          <td style='padding:6px 8px;color:#666;font-size:11px'>{s['industry']}</td>
          <td style='padding:6px 8px;color:#888;font-size:11px'>{s['code']}</td>
          <td style='padding:6px 8px;text-align:right;font-size:11px'>{s['circ_mv_yi']}亿</td>
          <td style='padding:6px 8px;text-align:right;font-weight:600;color:#6c5ce7'>{s['total']}</td>
          <td style='padding:6px 8px;font-size:10px;line-height:1.6'>{score_breakdown}<br/>{notes_html}</td>
        </tr>""")

    return f"""<div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
      <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:4px">
        B轨「潜伏标的」 TOP{len(potentials)}
      </div>
      <div style="font-size:11px;color:#888;margin-bottom:10px">
        政策×行业资金×市值×形态×筹码×机构×增持×业绩 综合评分（满分约40）· 找还没启动但条件齐备的标的
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="background:#f8f9fa;color:#888">
          <th style="padding:5px 8px;text-align:left;font-weight:400">名称</th>
          <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
          <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
          <th style="padding:5px 8px;text-align:right;font-weight:400">流通市值</th>
          <th style="padding:5px 8px;text-align:right;font-weight:400">总分</th>
          <th style="padding:5px 8px;text-align:left;font-weight:400">细分</th>
        </tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>"""


def ai_potential_review(potentials: list) -> str:
    """对TOP10潜伏标的做一段AI解读，引用真实分项。"""
    if not potentials:
        return "本轮B轨暂无符合政策赛道的潜伏标的，建议下一轮观察。"
    lines = []
    for s in potentials[:10]:
        sc = s["scores"]
        notes = " | ".join([v for v in [
            s["notes"]["policy"] and f"政策赛道:{s['notes']['policy']}",
            s["notes"]["earnings"],
            s["notes"]["chip"], s["notes"]["inst"], s["notes"]["holder"],
        ] if v])
        lines.append(
            f"{s['name']}({s['code']}, {s['industry']}, {s['circ_mv_yi']}亿) "
            f"总分{s['total']}（政策{sc['政策']}/业绩{sc['业绩']}/筹码{sc['筹码']}/机构{sc['机构']}/形态{sc['形态']}/资金{sc['行业资金']}）{notes}"
        )
    data_block = "\n".join(lines)
    prompt = f"""以下是「B轨潜伏标的」综合评分系统输出的TOP10（按总分降序）。
评分维度：政策赛道命中(6) + 行业资金景气度(4) + 市值弹性(2) + 形态底部+均线粘合+温和放量(11) + 筹码集中(5) + 机构持仓(4) + 大股东增持(3) + 业绩改善(5) + 防过热(3)。

{data_block}

请基于上方真实数据写一段简短复盘（300-500字），要求：
1. 指出本轮潜伏标的最集中的政策赛道（出现最多的关键词方向）
2. 挑出2-3只综合质量最高的标的，逐只点评（政策/业绩/筹码三个角度），不超过3行
3. 整体提示：哪些维度的得分整体偏低（说明市场欠缺什么）
4. 不构成投资建议，不预测涨幅，不引用列表外的股票。
"""
    return ask_deepseek(prompt, max_tokens=1500)


# ══════════════════════════════════════════════════════════
#  模块四：财报分析（财报季专用）
# ══════════════════════════════════════════════════════════

def get_financial_data() -> dict:
    """获取最新财报数据，分析业绩扭转和超预期"""
    print("【财报】获取最新财务数据...")
    result = {
        "turnaround":    [],   # 业绩扭转（亏转盈/增速拐点）
        "beat":          [],   # 超预期（实际>预期）
        "miss":          [],   # 不及预期
        "high_growth":   [],   # 高增长（净利润>50%）
        "deteriorating": [],   # 恶化信号
    }
    try:
        # 最新一期财报（按最新报告期）
        end   = TODAY
        start = (_bj - datetime.timedelta(days=90)).strftime("%Y%m%d")

        # 利润表
        income = pro.income(
            start_date=start, end_date=end,
            fields="ts_code,ann_date,end_date,revenue,n_income,n_income_attr_p"
        )
        # 财务指标
        fina = pro.fina_indicator(
            start_date=start, end_date=end,
            fields="ts_code,ann_date,end_date,grossprofit_margin,netprofit_margin,roe,debt_to_assets,yoy_net_profit,yoy_sales"
        )
        # 业绩快报
        express = pro.express(
            start_date=start, end_date=end,
            fields="ts_code,ann_date,end_date,revenue,operate_profit,total_profit,n_income,yoy_net_profit"
        )
        # 业绩预告
        forecast = pro.forecast(
            start_date=start, end_date=end,
            fields="ts_code,ann_date,type,p_change_min,p_change_max"
        )

        # 分析业绩快报中的高增长
        if express is not None and len(express) > 0:
            express["yoy_net_profit"] = pd.to_numeric(express["yoy_net_profit"], errors="coerce")
            for _, row in express.iterrows():
                yoy = safe_float(row.get("yoy_net_profit"))
                if yoy >= 50:
                    result["high_growth"].append({
                        "code":     row.get("ts_code", ""),
                        "yoy":      round(yoy, 1),
                        "ann_date": row.get("ann_date", ""),
                    })
                elif yoy <= -30:
                    result["deteriorating"].append({
                        "code":     row.get("ts_code", ""),
                        "yoy":      round(yoy, 1),
                        "ann_date": row.get("ann_date", ""),
                    })

        # 分析业绩预告中的扭转信号
        if forecast is not None and len(forecast) > 0:
            forecast["p_change_max"] = pd.to_numeric(forecast["p_change_max"], errors="coerce")
            turnaround_types = ["扭亏", "略增", "续盈", "预增"]
            for _, row in forecast.iterrows():
                ftype = str(row.get("type", ""))
                pct   = safe_float(row.get("p_change_max"))
                if ftype in turnaround_types and pct >= 50:
                    result["turnaround"].append({
                        "code":    row.get("ts_code", ""),
                        "type":    ftype,
                        "pct_max": round(pct, 0),
                    })

        # 分析财务指标中的毛利率趋势
        if fina is not None and len(fina) > 0:
            fina["grossprofit_margin"] = pd.to_numeric(fina["grossprofit_margin"], errors="coerce")
            fina["yoy_net_profit"]     = pd.to_numeric(fina["yoy_net_profit"],     errors="coerce")
            for _, row in fina.iterrows():
                gpm = safe_float(row.get("grossprofit_margin"))
                yoy = safe_float(row.get("yoy_net_profit"))
                if gpm > 40 and yoy > 30:
                    result["beat"].append({
                        "code": row.get("ts_code", ""),
                        "gpm":  round(gpm, 1),
                        "yoy":  round(yoy, 1),
                    })

        # 去重
        for key in result:
            seen = set()
            deduped = []
            for item in result[key]:
                if item["code"] not in seen:
                    seen.add(item["code"])
                    deduped.append(item)
            result[key] = deduped[:10]

        print(f"  财报分析完成：高增长{len(result['high_growth'])}只，"
              f"扭转{len(result['turnaround'])}只，恶化{len(result['deteriorating'])}只")
    except Exception as e:
        print(f"  财报数据失败: {e}")
    return result


def get_industry_financial_trend() -> list:
    """行业财务趋势：哪些行业的整体盈利在改善"""
    print("【财报】行业财务趋势...")
    result = []
    try:
        # 用行业分类统计最新财务指标
        fina = pro.fina_indicator(
            start_date=(_bj - datetime.timedelta(days=90)).strftime("%Y%m%d"),
            end_date=TODAY,
            fields="ts_code,ann_date,grossprofit_margin,roe,yoy_net_profit"
        )
        names = pro.stock_basic(exchange="", list_status="L",
                                fields="ts_code,name,industry")
        if fina is not None and names is not None:
            merged = pd.merge(fina, names, on="ts_code", how="left")
            merged["yoy_net_profit"] = pd.to_numeric(merged["yoy_net_profit"], errors="coerce")
            industry_stats = merged.groupby("industry")["yoy_net_profit"].agg(
                median="median", count="count"
            ).reset_index()
            industry_stats = industry_stats[industry_stats["count"] >= 5]
            for _, row in industry_stats.nlargest(8, "median").iterrows():
                result.append({
                    "industry": row["industry"],
                    "median_yoy": round(safe_float(row["median"]), 1),
                    "count": int(row["count"]),
                })
        print(f"  行业趋势：{len(result)}个行业")
    except Exception as e:
        print(f"  行业财务趋势失败: {e}")
    return result[:8]


# ══════════════════════════════════════════════════════════
#  模块五：AI分析
# ══════════════════════════════════════════════════════════

def detect_market_driver(northbound, sector_flow, policy_news,
                          market_data, financial=None) -> str:
    """
    判断当前市场主要驱动逻辑
    市场看重什么，我们就重点分析什么
    """
    signals = []

    # 政策信号强度
    policy_count = len(policy_news)
    if policy_count >= 5:
        signals.append("政策驱动")

    # 北向资金信号
    north_total = safe_float(northbound.get("total", 0))
    if abs(north_total) >= 10:
        signals.append("外资驱动" if north_total > 0 else "外资撤离")

    # 市场情绪
    if market_data:
        limit_up = market_data.get("limit_up", 0)
        sentiment = market_data.get("sentiment", "")
        if limit_up >= 50:
            signals.append("情绪驱动")
        if "偏多" in sentiment:
            signals.append("多头市场")

    # 财报季信号
    if financial and IS_EARNINGS:
        high_growth = len(financial.get("high_growth", []))
        if high_growth >= 5:
            signals.append("业绩驱动")

    if not signals:
        signals = ["震荡观望"]

    return "、".join(signals)


def ai_morning_report(news_data, announcements, yest_capital) -> str:
    """
    上午快报AI分析
    数据来源：今日新闻（实时）+ 今日公告（实时）+ 昨日资金（T-1背景）
    """
    # 今日新闻
    news_text = "\n".join(
        f"[{n['time']}] [{n['src']}] {n['title']}"
        for n in news_data
    ) if news_data else "今日新闻接口暂无返回（可能数据源未入库或接口异常，非市场无新闻）"

    # 今日公告
    ann_text = "\n".join(
        f"- [{a['code']}] {a['title']}"
        for a in announcements
    ) if announcements else "今日暂无重大公告"

    # 昨日资金背景
    yc = yest_capital
    dt_names  = "、".join(f"{s['name']}（{s.get('industry','')}）" for s in yc.get("dragon_tiger",[])[:5]) or "暂无"
    mf_text   = "、".join(
        f"{s.get('name','')}({s['code']}，{s.get('industry','')}，+{s['net_yi']}亿)" for s in yc.get("top_moneyflow",[])[:5]
    ) or "暂无"
    ydate_str = (_bj - datetime.timedelta(days=1)).strftime("%m月%d日")

    prompt = f"""今天是{TODAY_CN}，上午收盘后。

【规则】
1. 只使用下方列表中实际提供的数据进行分析
2. 任何列表为空的数据项，直接写"暂无"，不得补充任何内容
3. 不得引用列表以外的任何事件、新闻或信息

【今日新闻标题列表（{TODAY_CN}，共{len(news_data)}条）】
{news_text}

【今日重大公告列表】
{ann_text}

【昨日资金数据（仅作参考，标注为昨日）】
昨日龙虎榜：{dt_names}
昨日主力净流入：{mf_text}

注意：今日涨停/连板/游资/热榜数据收盘后才有，在18:00收盘报告里。

请输出上午快报：

**一、今日新闻中的重要信息**
只列出上方新闻列表中实际出现的重要条目，列表为空则写"今日暂无"

**二、今日公告中的重要信号**
只分析上方公告列表中的内容，列表为空则写"今日暂无"

**三、基于昨日资金的参考判断**
昨日龙虎榜数据对今日的参考意义

**四、今日操作建议**
无充分数据支撑时直接建议观望为主，等待18:00收盘报告"""

    print("  AI上午快报分析...")
    return ask_deepseek(prompt, max_tokens=1500)


def build_morning_html(title, ai_report, news_data, announcements, yest_capital) -> str:
    yc       = yest_capital

    # 昨日龙虎榜
    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']} <span style='color:#aaa;font-size:10px'>{s.get('industry','')}</span></td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td></tr>"
        for s in yc.get("dragon_tiger", [])[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 昨日主力资金
    mf_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s.get('name','')} <span style='color:#aaa;font-size:10px'>{s.get('industry','')}</span></td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_yi']}亿</td></tr>"
        for s in yc.get("top_moneyflow", [])[:6]
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 今日新闻
    news_items = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px;white-space:nowrap'>{n['time']}</td>"
        f"<td style='padding:5px 8px;font-size:12px'>{n['title']}</td>"
        f"</tr>"
        for n in news_data[:12]
    ) or "<tr><td colspan='2' style='padding:10px;text-align:center;color:#aaa'>今日新闻接口暂无返回（可能数据源未入库或接口异常）</td></tr>"

    # 今日公告
    ann_items = "".join(
        f"<li style='padding:3px 0;font-size:12px;color:#2d3436'>"
        f"<span style='color:#888;font-size:11px'>[{a['code']}]</span> {a['title']}</li>"
        for a in announcements[:10]
    ) or "<li style='color:#aaa;font-size:12px'>今日暂无重大公告</li>"

    ydate_str = (_bj - datetime.timedelta(days=1)).strftime("%m月%d日")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:720px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,#0984e3,#00cec9);padding:20px 28px;color:#fff">
    <div style="font-size:17px;font-weight:600">{title}</div>
    <div style="font-size:11px;opacity:0.85;margin-top:3px">
      {TODAY_CN} · 上午快报 · v7.0 · 数据：今日新闻+公告（实时）+ 昨日资金（参考）
    </div>
  </div>

  <div style="padding:20px 28px">

    <!-- 说明提示 -->
    <div style="background:#fffbf0;border:0.5px solid #fdcb6e;border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#636e72">
      <strong>上午快报说明：</strong>涨停板、连板天梯、游资、热榜数据收盘后才入库，将在今日 <strong>18:00收盘报告</strong> 中提供。
      本报告数据来源：今日新闻快讯（实时）+ 今日重大公告 + 昨日资金背景。
    </div>

    <!-- AI分析 -->
    <div style="background:#f0f7ff;border-left:4px solid #0984e3;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#0984e3;margin-bottom:8px">上午快报</div>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    <!-- 今日新闻 -->
    <div style="margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">
        今日财经新闻快讯（{TODAY_CN}）
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="background:#f8f9fa;color:#888">
          <th style="padding:5px 8px;text-align:left;font-weight:400;width:50px">时间</th>
          <th style="padding:5px 8px;text-align:left;font-weight:400">标题</th>
        </tr></thead>
        <tbody>{news_items}</tbody>
      </table>
    </div>

    <!-- 今日公告 -->
    <div style="margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">今日重大公告</div>
      <ul style="margin:0;padding-left:16px">{ann_items}</ul>
    </div>

    <!-- 昨日资金背景 -->
    <div style="font-size:12px;color:#888;margin-bottom:8px">
      昨日（{ydate_str}）资金背景参考
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">

      <div style="background:#f0fff4;border-radius:8px;padding:12px;border:0.5px solid #55efc4">
        <div style="font-size:10px;color:#888;margin-bottom:4px">昨日龙虎榜机构席位</div>
        <table style="width:100%;font-size:11px">{dt_rows}</table>
      </div>

      <div style="background:#f0f7ff;border-radius:8px;padding:12px;border:0.5px solid #74b9ff">
        <div style="font-size:10px;color:#888;margin-bottom:4px">昨日主力净流入TOP6</div>
        <table style="width:100%;font-size:11px">{mf_rows}</table>
      </div>

    </div>

  </div>
  <div style="padding:10px 28px;background:#f8f9fa;color:#aaa;font-size:10px;text-align:center;border-top:1px solid #eee">
    本报告由AI自动生成，数据源：Tushare Pro，不构成投资建议。投资有风险，决策需谨慎。
  </div>
</div></body></html>"""

def ai_closing_report(policy_news, stocks, market_sentiment,
                       northbound, moneyflow, dragon_tiger,
                       block_trade, sector_flow, broker_rec,
                       financial=None) -> str:
    """收盘深度报告AI分析"""
    # 新闻直接透传原文，不加任何"政策"提示，避免AI联想历史信息
    if policy_news:
        news_lines = "\n".join(
            f"{i+1}. {n}" for i, n in enumerate(policy_news[:20])
        )
        policy_text = f"今日新闻列表（{len(policy_news)}条）：\n{news_lines}\n\n要求：只分析以上编号列表中实际出现的新闻标题，不得引用列表以外的任何信息。"
    else:
        policy_text = "今日新闻：无（本项留空，不得填入任何内容）"

    ms = market_sentiment
    sentiment_text = (
        f"市场情绪：{ms.get('sentiment','—')}，"
        f"涨{ms.get('up',0)}/跌{ms.get('down',0)}，"
        f"涨停{ms.get('limit_up',0)}/跌停{ms.get('limit_down',0)}"
    ) if ms.get("up", 0) > 0 else "今日收盘数据未入库"

    mf_text = "\n".join(
        f"- {s.get('name','')}（{s['code']}，{s.get('industry','')}）：大单净流入{s['net_flow_yi']}亿"
        for s in moneyflow[:8]
    ) or "暂无"

    dt_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['signal']}"
        for s in dragon_tiger
    ) or "今日暂无"

    block_text = "\n".join(
        f"- {s['name']}：折价{s['discount_rate']}%，成交{s['amount_wan']}万"
        for s in block_trade
    ) or "今日暂无"

    sector_text = "\n".join(
        f"- {s['sector']}：净流入{s['net_flow_yi']}亿"
        for s in sector_flow
    ) or "暂无"

    broker_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['broker_count']}家推荐（{s['brokers']}）"
        for s in broker_rec
    ) or "暂无"

    stocks_text = "\n".join(
        f"- {s['name']}（{s['code']}）[{s['industry']}]："
        f"收涨{s['change_pct']}%，市值{s['market_cap_yi']}亿，"
        f"PE{s['pe']}，换手{s['turnover_rate']}%，量比{s['volume_ratio']}，"
        f"技术评分{s['tech_score']}/12"
        for s in stocks
    ) or "今日暂无符合条件标的"

    fin_text = ""
    if financial and IS_EARNINGS:
        hg = financial.get("high_growth", [])
        tv = financial.get("turnaround", [])
        dt = financial.get("deteriorating", [])
        fin_text = f"""
【财报信号（财报季）】
高增长（净利润+50%以上）：{', '.join([s['code'] for s in hg[:5]])} 共{len(hg)}只
业绩扭转（预增/扭亏）：{', '.join([s['code'] for s in tv[:5]])} 共{len(tv)}只
业绩恶化（下滑30%以上）：{', '.join([s['code'] for s in dt[:5]])} 共{len(dt)}只"""

    driver = detect_market_driver(northbound, sector_flow, policy_news, ms, financial)

    missing = []
    if ms.get("up", 0) == 0:              missing.append("市场行情")
    if not moneyflow:                      missing.append("主力资金")
    sep = " / "
    warning = (f"⚠️ 数据缺失：{sep.join(missing)}，对应维度不得编造。\n\n"
               if missing else "")

    prompt = f"""今天是{TODAY_CN}，收盘后复盘。

{warning}
【铁律——违反以下规则即为无效分析】
1. 政策分析只能引用上方"今日财经新闻"列表中实际存在的条目，列表为空则写"今日无政策催化"
2. 数据缺失的维度直接标注"数据缺失"，不得用任何历史数据或常识补充
3. 推荐标的必须有本报告中实际出现的数据支撑（技术评分/资金流），无则不推荐

【当前市场主要驱动逻辑】
{driver}

【市场情绪】
{sentiment_text}

【行业资金流向】
{sector_text}

【主力大单净流入】
{mf_text}

【龙虎榜机构席位】
{dt_text}

【大宗交易折价】
{block_text}
{fin_text}

【技术面精筛标的（评分≥5/12）】
{stocks_text}

【今日新闻（只分析列表中实际存在的条目）】
{policy_text}

请输出收盘深度报告：

**一、今日市场驱动逻辑判断**
基于上方数据判断今日市场由什么驱动（资金/情绪/新闻事件）
不得引用新闻列表以外的任何事件

**二、核心数据解读**
- 市场情绪（涨跌停数据）
- 行业资金流向
- 主力资金（数据缺失直接标注"缺失"）
- 今日新闻中实际出现的重要事项（列表为空则写"今日无"）

**三、明日重点关注标的（2-3只）**
只能从【技术面精筛标的】中选取，必须说明数据来源
不得基于新闻列表以外的信息推荐标的
不得推荐券商金股、大盘权重股等未经本系统技术精筛的标的

**四、明日操作策略**
首选标的、止损原则、需要回避的方向"""

    print("  AI收盘深度分析...")
    return ask_deepseek(prompt, max_tokens=2500)


def ai_financial_report(financial_data, industry_trend, policy_news,
                         names, broker_rec) -> str:
    """财报专项报告AI分析"""
    def get_name(code):
        return names.get(code, (code, ""))[0]

    hg_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：净利润同比+{s['yoy']}%（{s['ann_date']}披露）"
        for s in financial_data.get("high_growth", [])[:8]
    ) or "本期暂无"

    tv_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：{s['type']}，预增上限{s['pct_max']}%"
        for s in financial_data.get("turnaround", [])[:8]
    ) or "本期暂无"

    dt_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：净利润同比{s['yoy']}%"
        for s in financial_data.get("deteriorating", [])[:8]
    ) or "本期暂无"

    ind_text = "\n".join(
        f"- {s['industry']}：净利润中位数同比+{s['median_yoy']}%（样本{s['count']}家）"
        for s in industry_trend
    ) or "暂无行业数据"

    broker_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['broker_count']}家券商推荐"
        for s in broker_rec[:6]
    ) or "暂无"

    policy_text = "\n".join(policy_news[:8]) if policy_news else "暂无"

    prompt = f"""今天是{TODAY_CN}，财报季专项分析。

请从"市场会怎么看这些数据"的角度分析，只使用以下真实数据。

【高增长标的（净利润增速50%以上）】
{hg_text}

【业绩扭转标的（扭亏/预增）】
{tv_text}

【业绩恶化标的（下滑30%以上，需回避）】
{dt_text}

【行业盈利趋势】
{ind_text}

【相关政策背景（今日）】
{policy_text}

请输出财报专项分析报告：

**一、本期财报季市场关注焦点**
市场最看重哪些维度？

**二、业绩扭转机会**
预期差机会：财务改善逻辑 + 国家战略关联度 + 估值判断

**三、行业财务趋势**
哪些行业整体盈利在系统性改善？对应哪些A股主线？

**四、需要回避的财务风险**
恶化标的背后的行业逻辑

**五、本周选股建议**
结合财务+政策，给出2-3只值得深度研究的标的
（仅限净利润高增长/业绩扭转等本报告中出现的财务信号标的，不得推荐券商金股或大盘权重股）

基于真实数据，不编造数字。"""

    print("  AI财报专项分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ══════════════════════════════════════════════════════════
#  周报 AI + HTML
# ══════════════════════════════════════════════════════════

def ai_weekly_review(week_dates, idx_perf, sector_flow_week,
                     top_stocks, weekly_news, stocks_friday) -> str:
    """周度复盘：基于本周真实数据生成AI综述。"""
    if not week_dates:
        return "本周无交易日数据，无法生成周度复盘。"

    period = f"{week_dates[0]} → {week_dates[-1]}（共{len(week_dates)}个交易日）"

    idx_text = "\n".join(
        f"- {x['name']}（{x['code']}）：周涨跌 {x['week_chg']:+.2f}%，收 {x['close']}"
        for x in idx_perf
    ) or "暂无"

    inflow_text = "\n".join(
        f"- {x['sector']}：+{x['net_yi']}亿" for x in sector_flow_week["inflow"]
    ) or "暂无"
    outflow_text = "\n".join(
        f"- {x['sector']}：{x['net_yi']}亿" for x in sector_flow_week["outflow"]
    ) or "暂无"

    top_text = "\n".join(
        f"- {x['name']}（{x['code']}，{x['industry']}）：+{x['week_pct']}%"
        for x in top_stocks[:10]
    ) or "暂无"

    news_text = "\n".join(
        f"{i+1}. [{n['date']} {n['time']}] {n['title']}"
        for i, n in enumerate(weekly_news[:25])
    ) or "本周无重大政策/事件新闻"

    pick_text = "\n".join(
        f"- {s.get('name','')}（{s.get('code','')}，{s.get('industry','')}）评分{s.get('score','')}/12"
        for s in (stocks_friday or [])[:10]
    ) or "周五技术面精筛无符合条件标的"

    prompt = f"""本周A股市场周度复盘（{period}）。
基于以下真实数据，撰写一份结构化周报。要求：只引用下方数据，不得编造、不得引用列表外的事件；用平实中文，不用比喻。

【本周指数走势】
{idx_text}

【本周行业资金流入TOP】
{inflow_text}

【本周行业资金流出TOP】
{outflow_text}

【本周强势个股TOP10】
{top_text}

【本周重大政策/事件/新闻（编号列表）】
{news_text}

【周五技术面精筛标的】
{pick_text}

请按以下五部分输出：

**一、本周市场总览**
用2-3句话点出本周大盘强弱、风格分化（大小盘/成长价值/行业）、量能特征。引用指数涨跌幅。

**二、本周资金主线**
基于行业资金流入/流出数据，指出本周资金最集中的方向、被抛弃的方向，以及对应的逻辑判断。

**三、本周重大政策/事件回顾**
从上述编号新闻中挑选3-5条对大盘或行业影响最大的，逐条说明其市场含义。不得引用列表外的新闻。

**四、下周关注方向**
结合本周资金主线 + 政策催化 + 技术面精筛，给出2-3个值得跟踪的方向（板块层面，不是个股推荐）。

**五、下周可重点跟踪的个股**
仅从【周五技术面精筛标的】中选取，必须说明数据依据。若为空则写"周五技术面无符合条件标的，下周观察精筛结果"。不得推荐券商金股、大盘权重股。
"""
    print("  AI周度复盘...")
    return ask_deepseek(prompt, max_tokens=3000)


def build_weekly_html(title, ai_report, week_dates, idx_perf,
                      sector_flow_week, top_stocks, weekly_news, stocks_friday) -> str:
    period = f"{week_dates[0]} → {week_dates[-1]}" if week_dates else "—"

    idx_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{x['name']}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{x['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:{'#d63031' if x['week_chg']>=0 else '#00b894'};font-weight:500'>"
        f"{x['week_chg']:+.2f}%</td></tr>"
        for x in idx_perf
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    inflow_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{x['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{x['net_yi']}亿</td></tr>"
        for x in sector_flow_week["inflow"]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    outflow_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{x['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#00b894;font-weight:500'>{x['net_yi']}亿</td></tr>"
        for x in sector_flow_week["outflow"]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    top_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{x['name']}</td>"
        f"<td style='padding:5px 8px;color:#666;font-size:11px'>{x['industry']}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{x['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{x['week_pct']}%</td></tr>"
        for x in top_stocks[:15]
    ) or "<tr><td colspan='4' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    news_rows = "".join(
        f"<tr><td style='padding:5px 8px;color:#888;font-size:11px;white-space:nowrap'>{n['date']} {n['time']}</td>"
        f"<td style='padding:5px 8px'>{n['title']}</td></tr>"
        for n in weekly_news[:40]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>本周无重大政策/事件新闻</td></tr>"

    # 周五技术面精筛
    pick_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s.get('name','')}</td>"
        f"<td style='padding:5px 8px;color:#666;font-size:11px'>{s.get('industry','')}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s.get('code','')}</td>"
        f"<td style='padding:5px 8px;text-align:right'>{s.get('pct_chg','')}%</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#6c5ce7;font-weight:500'>{s.get('score','')}/12</td></tr>"
        for s in (stocks_friday or [])[:10]
    ) or "<tr><td colspan='5' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>周五技术面无符合条件标的</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#f5f5f7;margin:0;padding:20px;color:#2d3436">
<div style="max-width:780px;margin:auto">

  <div style="background:linear-gradient(135deg,#6c5ce7 0%,#a29bfe 100%);color:#fff;padding:20px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:18px;font-weight:600">{title}</div>
    <div style="font-size:11px;opacity:0.85;margin-top:3px">{period} · 周度复盘 · v7.0 · Tushare Pro 6000积分</div>
  </div>

  <div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:500;color:#6c5ce7;margin-bottom:10px">AI 周度复盘</div>
    <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
  </div>

  <div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:10px">本周指数走势</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="background:#f8f9fa;color:#888">
        <th style="padding:5px 8px;text-align:left;font-weight:400">指数</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">周涨跌</th>
      </tr></thead>
      <tbody>{idx_rows}</tbody>
    </table>
  </div>

  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
    <div style="background:#fff;padding:18px;border-radius:12px">
      <div style="font-size:13px;font-weight:500;color:#d63031;margin-bottom:10px">本周资金流入板块</div>
      <table style="width:100%;font-size:12px">{inflow_rows}</table>
    </div>
    <div style="background:#fff;padding:18px;border-radius:12px">
      <div style="font-size:13px;font-weight:500;color:#00b894;margin-bottom:10px">本周资金流出板块</div>
      <table style="width:100%;font-size:12px">{outflow_rows}</table>
    </div>
  </div>

  <div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:10px">本周强势个股 TOP15</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="background:#f8f9fa;color:#888">
        <th style="padding:5px 8px;text-align:left;font-weight:400">名称</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">周涨幅</th>
      </tr></thead>
      <tbody>{top_rows}</tbody>
    </table>
  </div>

  <div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:10px">本周重大政策 / 事件 / 新闻回顾</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <tbody>{news_rows}</tbody>
    </table>
  </div>

  <div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:10px">周五技术面精筛标的（评分≥5/12）</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="background:#f8f9fa;color:#888">
        <th style="padding:5px 8px;text-align:left;font-weight:400">名称</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">涨幅</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">评分</th>
      </tr></thead>
      <tbody>{pick_rows}</tbody>
    </table>
  </div>

  <div style="text-align:center;color:#aaa;font-size:11px;margin-top:14px">
    本报告由AI自动生成，数据源：Tushare Pro 6000积分，不构成投资建议。投资有风险，决策需谨慎。
  </div>

</div></body></html>"""


def build_closing_html(title, ai_report, stocks, market_sentiment,
                        northbound, moneyflow, dragon_tiger,
                        block_trade, sector_flow, broker_rec) -> str:
    ms = market_sentiment
    sc = {"强势偏多":"#00b894","温和偏多":"#55efc4","中性震荡":"#888",
          "温和偏空":"#e17055","弱势偏空":"#d63031"}.get(ms.get("sentiment",""), "#888")

    stock_rows = ""
    for s in stocks[:12]:
        tech = s.get("tech", {})
        ma_t   = "✓" if tech.get("ma_bullish")   else "—"
        macd_t = "✓" if tech.get("macd_bullish")  else "—"
        kdj_t  = "✓" if tech.get("kdj_bullish")   else "—"
        vol_t  = "✓" if tech.get("vol_expanding") else "—"
        stock_rows += (
            f"<tr>"
            f"<td style='padding:5px 8px'>{s['name']}</td>"
            f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
            f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s.get('industry','')}</td>"
            f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['change_pct']}%</td>"
            f"<td style='padding:5px 8px;text-align:right'>{s['market_cap_yi']}亿</td>"
            f"<td style='padding:5px 8px;text-align:right'>{s['pe']}</td>"
            f"<td style='padding:5px 8px;text-align:right'>{s['turnover_rate']}%</td>"
            f"<td style='padding:5px 8px;text-align:center'>{ma_t}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{macd_t}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{kdj_t}</td>"
            f"<td style='padding:5px 8px;text-align:center'>{vol_t}</td>"
            f"<td style='padding:5px 8px;text-align:center;font-weight:500;color:#6c5ce7'>{s.get('tech_score','—')}</td>"
            f"</tr>"
        )

    mf_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s.get('name','')}</td>"
        f"<td style='padding:5px 8px;color:#666;font-size:11px'>{s.get('industry','')}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in moneyflow[:10]
    ) or "<tr><td colspan='4' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;color:#666;font-size:11px'>{s.get('industry','')}</td></tr>"
        for s in dragon_tiger[:5]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    sector_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in sector_flow[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:760px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,#6c5ce7,#a29bfe);padding:20px 28px;color:#fff">
    <div style="font-size:17px;font-weight:600">{title}</div>
    <div style="font-size:11px;opacity:0.85;margin-top:3px">{TODAY_CN} · 收盘深度复盘 · v7.0 · Tushare Pro 6000积分</div>
  </div>

  <div style="padding:20px 28px">

    <!-- AI深度报告 -->
    <div style="background:#f4f0ff;border-left:4px solid #6c5ce7;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#6c5ce7;margin-bottom:8px">收盘深度复盘</div>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    <!-- 市场情绪 -->
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:16px">
      <div style="background:#f8f9fa;border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#888">市场情绪</div>
        <div style="font-size:13px;font-weight:500;color:{sc}">{ms.get('sentiment','—')}</div>
      </div>
      <div style="background:#f0fff4;border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#888">上涨</div>
        <div style="font-size:16px;font-weight:500;color:#00b894">{ms.get('up',0)}</div>
      </div>
      <div style="background:#fff5f5;border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#888">下跌</div>
        <div style="font-size:16px;font-weight:500;color:#d63031">{ms.get('down',0)}</div>
      </div>
      <div style="background:#fff5f5;border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#888">涨停</div>
        <div style="font-size:16px;font-weight:500;color:#d63031">{ms.get('limit_up',0)}</div>
      </div>
      <div style="background:#f0fff4;border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:10px;color:#888">跌停</div>
        <div style="font-size:16px;font-weight:500;color:#00b894">{ms.get('limit_down',0)}</div>
      </div>
    </div>

    <!-- 聪明钱 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:20px">
      <div style="background:#f0fff4;border-radius:8px;padding:12px;border:0.5px solid #55efc4">
        <div style="font-size:10px;color:#888;margin-bottom:4px">龙虎榜机构</div>
        <table style="width:100%;font-size:11px">{dt_rows}</table>
      </div>
      <div style="background:#f0f7ff;border-radius:8px;padding:12px;border:0.5px solid #74b9ff">
        <div style="font-size:10px;color:#888;margin-bottom:4px">行业资金TOP6</div>
        <table style="width:100%;font-size:11px">{sector_rows}</table>
      </div>
    </div>

    <!-- 主力资金 -->
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">主力大单净流入 TOP10</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
      <thead><tr style="background:#f8f9fa;color:#888">
        <th style="padding:5px 8px;text-align:left;font-weight:400">名称</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">净流入</th>
      </tr></thead>
      <tbody>{mf_rows}</tbody>
    </table>

    <!-- 技术面精筛 -->
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">技术面精筛标的（评分≥5/12）</div>
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:11px;min-width:600px">
      <thead><tr style="background:#f8f9fa;color:#888">
        <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">涨幅</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">市值</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">PE</th>
        <th style="padding:5px 8px;text-align:right;font-weight:400">换手</th>
        <th style="padding:5px 8px;text-align:center;font-weight:400">均线</th>
        <th style="padding:5px 8px;text-align:center;font-weight:400">MACD</th>
        <th style="padding:5px 8px;text-align:center;font-weight:400">KDJ</th>
        <th style="padding:5px 8px;text-align:center;font-weight:400">量能</th>
        <th style="padding:5px 8px;text-align:center;font-weight:400">评分</th>
      </tr></thead>
      <tbody>{stock_rows if stock_rows else "<tr><td colspan='12' style='padding:12px;text-align:center;color:#aaa'>今日暂无符合条件标的</td></tr>"}</tbody>
    </table>
    </div>
    <div style="font-size:10px;color:#aaa;margin-top:4px">✓=达标 —=未达标 | 满分12分</div>

  </div>
  <div style="padding:10px 28px;background:#f8f9fa;color:#aaa;font-size:10px;text-align:center;border-top:1px solid #eee">
    本报告由AI自动生成，数据源：Tushare Pro 6000积分，不构成投资建议。投资有风险，决策需谨慎。
  </div>
</div></body></html>"""


def build_financial_html(title, ai_report, financial_data, industry_trend, names) -> str:
    def get_name(code):
        return names.get(code, (code, ""))[0]

    hg_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{get_name(s['code'])}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['yoy']}%</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['ann_date']}</td></tr>"
        for s in financial_data.get("high_growth", [])[:8]
    ) or "<tr><td colspan='4' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    tv_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{get_name(s['code'])}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:center;color:#6c5ce7'>{s['type']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['pct_max']}%</td></tr>"
        for s in financial_data.get("turnaround", [])[:8]
    ) or "<tr><td colspan='4' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{get_name(s['code'])}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#00b894;font-weight:500'>{s['yoy']}%</td></tr>"
        for s in financial_data.get("deteriorating", [])[:6]
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    ind_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['industry']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['median_yoy']}%</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#888;font-size:11px'>{s['count']}家</td></tr>"
        for s in industry_trend[:8]
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:720px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,#e17055,#d63031);padding:20px 28px;color:#fff">
    <div style="font-size:17px;font-weight:600">{title}</div>
    <div style="font-size:11px;opacity:0.85;margin-top:3px">{TODAY_CN} · 财报季专项分析 · v7.0</div>
  </div>

  <div style="padding:20px 28px">

    <div style="background:#fff5f5;border-left:4px solid #e17055;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#e17055;margin-bottom:8px">财报季深度分析</div>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
      <div>
        <div style="font-size:13px;font-weight:500;margin-bottom:8px;color:#d63031">高增长标的（净利润+50%）</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">增速</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">披露日</th>
          </tr>
          {hg_rows}
        </table>
      </div>
      <div>
        <div style="font-size:13px;font-weight:500;margin-bottom:8px;color:#6c5ce7">业绩扭转标的</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
            <th style="padding:5px 8px;text-align:center;font-weight:400">类型</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">预增上限</th>
          </tr>
          {tv_rows}
        </table>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
      <div>
        <div style="font-size:13px;font-weight:500;margin-bottom:8px;color:#00b894">业绩恶化（需回避）</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">增速</th>
          </tr>
          {dt_rows}
        </table>
      </div>
      <div>
        <div style="font-size:13px;font-weight:500;margin-bottom:8px;color:#0984e3">行业盈利趋势</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">行业</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">中位增速</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">样本</th>
          </tr>
          {ind_rows}
        </table>
      </div>
    </div>

  </div>
  <div style="padding:10px 28px;background:#f8f9fa;color:#aaa;font-size:10px;text-align:center;border-top:1px solid #eee">
    本报告由AI自动生成，财务数据来源：Tushare Pro，不构成投资建议。
  </div>
</div></body></html>"""


def get_smtp_config(email: str):
    domain = email.split("@")[-1].lower()
    return {
        "qq.com":      ("smtp.qq.com",        465, True),
        "foxmail.com": ("smtp.qq.com",        465, True),
        "163.com":     ("smtp.163.com",       465, True),
        "126.com":     ("smtp.126.com",       465, True),
        "gmail.com":   ("smtp.gmail.com",     587, False),
        "outlook.com": ("smtp.office365.com", 587, False),
        "me.com":      ("smtp.mail.me.com",   587, False),
        "icloud.com":  ("smtp.mail.me.com",   587, False),
    }.get(domain, ("smtp.qq.com", 465, True))


def md_to_html(text: str) -> str:
    import re as _re
    text = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = _re.sub(r"#{1,4}\s*(.+)", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")


def send_email(subject: str, html: str):
    smtp_host, smtp_port, use_ssl = get_smtp_config(EMAIL_SENDER)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = ", ".join(EMAIL_RECEIVERS)
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        server = (smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
                  if use_ssl else smtplib.SMTP(smtp_host, smtp_port, timeout=15))
        if not use_ssl:
            server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVERS, msg.as_string())
        server.quit()
        print(f"  邮件发送成功：{', '.join(EMAIL_RECEIVERS)}")
    except Exception as e:
        print(f"  邮件发送失败: {e}")
        raise


def save_report(html: str, suffix: str):
    os.makedirs("reports", exist_ok=True)
    path = f"reports/{TODAY}_{suffix}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  报告已保存：{path}")


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def main():
    # 所有模式共用：政策新闻
    policy_news    = fetch_policy_news()
    announcements  = fetch_announcements()

    if MODE == "morning":
        # ══ 上午快报（基于测试确认的可用接口）══
        # 盘中可用：news✓ anns✓ 昨日资金✓
        # 收盘后才入库：涨停/连板/游资/热榜 → 已移到18:00收盘报告
        print("\n=== 上午快报模式 ===")
        news_data     = get_morning_news()
        announcements = get_morning_announcements()
        yest_capital  = get_yesterday_capital()

        ai_report = ai_morning_report(news_data, announcements, yest_capital)
        subject   = f"【A股上午】{TODAY_CN} · 上午快报"
        html      = build_morning_html(subject, ai_report, news_data, announcements, yest_capital)
        send_email(subject, html)
        save_report(html, "上午快报")

    elif MODE == "weekly":
        # ══ 周末周报 ══
        print("\n=== 周末周报模式 ===")
        week_dates = get_trade_dates_in_week()
        if not week_dates:
            print("  本周无交易日，跳过")
            return
        friday = week_dates[-1]
        names       = get_stock_names()

        # 1. 指数走势
        idx_perf        = get_weekly_index_perf(week_dates)
        # 2. 行业资金累计
        sector_flow_week= get_weekly_sector_flow(week_dates)
        # 3. 强势个股
        top_stocks      = get_weekly_top_stocks(week_dates, names)
        # 4. 周五技术面精筛
        price_df, basic_df = get_daily_data(friday)
        factor_df          = get_stk_factor(friday)
        stocks_friday      = quant_and_tech_filter(price_df, basic_df, names, factor_df)
        # 5. 本周重大新闻
        weekly_news        = get_weekly_news()

        # 6. B轨：潜伏标的（用周五数据跑）
        try:
            sector_30d = get_sector_30d_rank()
            potentials = find_potential_stocks(price_df, basic_df, names, sector_30d, top_n=15)
            potential_ai = ai_potential_review(potentials) if potentials else ""
        except Exception as e:
            print(f"  B轨执行失败: {e}")
            potentials, potential_ai = [], ""

        # 7. AI 周度复盘
        ai_report = ai_weekly_review(
            week_dates, idx_perf, sector_flow_week,
            top_stocks, weekly_news, stocks_friday
        )
        subject = f"【A股周报】{TODAY_CN} · 本周复盘 ({week_dates[0]}→{week_dates[-1]})"
        html    = build_weekly_html(
            subject, ai_report, week_dates, idx_perf,
            sector_flow_week, top_stocks, weekly_news, stocks_friday
        )
        # 周报HTML的footer是: <div style="text-align:center;color:#aaa
        if potentials:
            b_html = build_potential_html_section(potentials)
            if potential_ai:
                b_html += f"""<div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
                <div style="font-size:13px;font-weight:500;color:#6c5ce7;margin-bottom:10px">B轨 AI 解读</div>
                <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(potential_ai)}</div></div>"""
            html = html.replace('<div style="text-align:center;color:#aaa',
                                b_html + '<div style="text-align:center;color:#aaa', 1)

        send_email(subject, html)
        save_report(html, "周报")

    elif MODE == "financial" and IS_EARNINGS and WEEKDAY == 4:
        # ══ 财报专项（周五，财报季内）══
        print("\n=== 财报专项报告模式 ===")
        financial_data   = get_financial_data()
        industry_trend   = get_industry_financial_trend()
        names            = get_stock_names()
        broker_rec       = []  # 券商金股已移除
        ai_report        = ai_financial_report(
            financial_data, industry_trend, policy_news, names, broker_rec
        )
        subject = f"【A股财报】{TODAY_CN} · 财报季专项分析"
        html    = build_financial_html(subject, ai_report, financial_data, industry_trend, names)
        send_email(subject, html)
        save_report(html, "财报专项")

    else:
        # ══ 收盘深度报告 ══
        print("\n=== 收盘深度报告模式 ===")
        price_df, basic_df = get_daily_data()
        names       = get_stock_names()
        factor_df   = get_stk_factor()
        northbound  = {"total": 0, "top_stocks": []}  # 北向实时/净流入数据自2024年起已停止披露，模块移除
        moneyflow   = get_moneyflow()
        for _m in moneyflow:                       # 补名称+行业（names: code -> (name, industry)）
            _info = names.get(_m["code"], ("", ""))
            _m["name"], _m["industry"] = _info[0], _info[1]
        dragon_tiger= get_dragon_tiger()
        for _d in dragon_tiger:                     # 补行业
            _d["industry"] = names.get(_d["code"], ("", ""))[1]
        block_trade = get_block_trade()
        sector_flow = get_sector_flow()
        broker_rec  = []  # 券商金股与本系统中小盘动量逻辑不符，且为月度静态共识，移除

        # 市场情绪（从收盘数据计算）
        market_sentiment = {"up":0,"down":0,"flat":0,"limit_up":0,"limit_down":0,"sentiment":"数据未入库"}
        if not price_df.empty:
            pct = pd.to_numeric(price_df["pct_chg"], errors="coerce")
            market_sentiment = {
                "up":         int((pct > 0).sum()),
                "down":       int((pct < 0).sum()),
                "flat":       int((pct == 0).sum()),
                "limit_up":   int((pct >= 9.9).sum()),
                "limit_down": int((pct <= -9.9).sum()),
                "sentiment":  "",
            }
            ratio = market_sentiment["up"] / max(market_sentiment["up"]+market_sentiment["down"],1)
            market_sentiment["sentiment"] = (
                "强势偏多" if ratio>0.65 else "温和偏多" if ratio>0.55 else
                "中性震荡" if ratio>0.45 else "温和偏空" if ratio>0.35 else "弱势偏空"
            )

        # 财报模块（财报季内加入收盘报告）
        financial = None
        if IS_EARNINGS:
            financial = get_financial_data()

        stocks = quant_and_tech_filter(price_df, basic_df, names, factor_df)

        # B轨：潜伏标的（政策×资金×筹码×形态 综合评分）
        try:
            sector_30d = get_sector_30d_rank()
            potentials = find_potential_stocks(price_df, basic_df, names, sector_30d, top_n=15)
            potential_ai = ai_potential_review(potentials) if potentials else ""
        except Exception as e:
            print(f"  B轨执行失败: {e}")
            potentials, potential_ai = [], ""

        ai_report = ai_closing_report(
            policy_news, stocks, market_sentiment,
            northbound, moneyflow, dragon_tiger,
            block_trade, sector_flow, broker_rec, financial
        )

        prefix  = "【A股周报】" if IS_WEEKEND else "【A股收盘】"
        subject = f"{prefix} {TODAY_CN} · 收盘深度复盘"
        html    = build_closing_html(
            subject, ai_report, stocks, market_sentiment,
            northbound, moneyflow, dragon_tiger,
            block_trade, sector_flow, broker_rec
        )

        # 把B轨HTML和AI解读注入到footer之前
        if potentials:
            b_html = build_potential_html_section(potentials)
            if potential_ai:
                b_html += f"""<div style="background:#fff;padding:18px;border-radius:12px;margin-bottom:16px">
                <div style="font-size:13px;font-weight:500;color:#6c5ce7;margin-bottom:10px">B轨 AI 解读</div>
                <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(potential_ai)}</div></div>"""
            # 收盘报告footer是 <div style="padding:10px 28px;background:#f8f9fa
            html = html.replace('<div style="padding:10px 28px;background:#f8f9fa',
                                b_html + '<div style="padding:10px 28px;background:#f8f9fa', 1)

        send_email(subject, html)
        save_report(html, "收盘复盘")

        # 财报专项（周五+财报季，收盘报告后额外发一封）
        if IS_EARNINGS and WEEKDAY == 4 and financial:
            print("\n=== 额外发送财报专项报告 ===")
            industry_trend = get_industry_financial_trend()
            fin_ai = ai_financial_report(
                financial, industry_trend, policy_news, names, broker_rec
            )
            fin_subject = f"【A股财报】{TODAY_CN} · 财报季专项分析"
            fin_html    = build_financial_html(
                fin_subject, fin_ai, financial, industry_trend, names
            )
            send_email(fin_subject, fin_html)
            save_report(fin_html, "财报专项")

    print(f"\n✓ {MODE}模式完成")


if __name__ == "__main__":
    main()
