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
if NOW_H == 3 and NOW_M >= 30:
    MODE = "morning"
elif NOW_H == 10 and NOW_M < 20:
    MODE = "closing"
elif NOW_H == 10 and NOW_M >= 20:
    MODE = "financial"
else:
    # 手动触发时根据北京时间判断
    bj_h = _bj.hour
    if bj_h < 14:
        MODE = "morning"
    elif bj_h < 18:
        MODE = "closing"
    else:
        MODE = "closing"

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

    # 2. Tushare国家政策库（大模型语料专题，每日更新）
    try:
        df_policy = pro.ncov_num(trade_date=today_ts)  # 尝试当日政策
    except Exception:
        df_policy = None

    # 跳过额外政策接口，news已经涵盖

    # 3. 新华社RSS（严格今日过滤）
    for url in [
        "http://www.xinhuanet.com/politics/news_politics.xml",
        "http://www.xinhuanet.com/fortune/news_fortune.xml",
    ]:
        try:
            resp = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title    = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                # 严格验证是今天：格式 Mon, 08 Jun 2026 或含今日年月日
                day_str  = _bj.strftime("%d %b %Y")   # "08 Jun 2026"
                if pub_date and today_str not in pub_date and day_str not in pub_date:
                    continue
                if any(k in title for k in POLICY_KEYWORDS):
                    result.append(f"[新华社] {title[:150]}")
        except Exception as e:
            print(f"  新华社RSS失败: {e}")

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
    today_str = _bj.strftime("%Y-%m-%d")
    for src in ["cls", "sina"]:
        try:
            df = pro.news(
                src=src,
                start_date=today_str + " 00:00:00",
                end_date=today_str + " 23:59:59",
                fields="datetime,title"
            )
            if df is None or len(df) == 0:
                print(f"  {src}新闻：今日暂无")
                continue
            for _, row in df.iterrows():
                pub_time = str(row.get("datetime", ""))
                # 严格验证是今天
                if today_str not in pub_time:
                    continue
                title = str(row.get("title", "")).strip()
                if len(title) < 5:
                    continue
                time_str = pub_time[11:16] if len(pub_time) > 10 else ""
                result.append({
                    "time":  time_str,
                    "title": title[:150],
                    "src":   src.upper(),
                })
            if result:
                print(f"  {src.upper()}新闻：今日{len(result)}条")
                break
        except Exception as e:
            print(f"  {src}新闻失败: {e}")

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

    try:
        # 昨日北向
        df = pro.moneyflow_hsgt(start_date=ydate, end_date=ydate)
        if df is not None and len(df) > 0:
            result["north_total"] = round(safe_float(df.iloc[0].get("north_money")) / 1e8, 2)
        df_top = pro.hsgt_top10(trade_date=ydate, market_type="N")
        if df_top is not None and len(df_top) > 0:
            result["north_top"] = df_top[["name","net_amount"]].head(5).to_dict("records")
        print(f"  昨日北向：{result['north_total']}亿")
    except Exception as e:
        print(f"  昨日北向失败: {e}")

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
                result["dragon_tiger"].append({"code": code, "name": name})
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
                    result["top_moneyflow"].append({
                        "code":    row["ts_code"],
                        "net_yi":  round(net / 1e8, 2),
                    })
        print(f"  昨日主力净流入：{len(result['top_moneyflow'])}只")
    except Exception as e:
        print(f"  昨日主力资金失败: {e}")

    return result



# ══════════════════════════════════════════════════════════
#  模块三：收盘Tushare全量数据
# ══════════════════════════════════════════════════════════

def get_daily_data() -> tuple:
    """收盘行情 + 基础指标"""
    print("【行情】收盘数据...")
    try:
        price = pro.daily(
            trade_date=TODAY,
            fields="ts_code,open,high,low,close,pre_close,change,pct_chg,vol,amount"
        )
        basic = pro.daily_basic(
            trade_date=TODAY,
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


def get_stk_factor() -> pd.DataFrame:
    """批量技术因子（6000积分）"""
    print("【技术因子】批量获取...")
    try:
        df = pro.stk_factor(
            trade_date=TODAY,
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
                    "net_flow_yi": round(net / 1e8, 2),
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
                        "net_flow_yi": round(net / 1e8, 2),
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
            (df["circ_mv"]       >= 50000)  &
            (df["circ_mv"]       <= 500000) &
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
    ) if news_data else "今日暂无政策相关新闻"

    # 今日公告
    ann_text = "\n".join(
        f"- [{a['code']}] {a['title']}"
        for a in announcements
    ) if announcements else "今日暂无重大公告"

    # 昨日资金背景
    yc = yest_capital
    north = yc.get("north_total", 0)
    north_top = "、".join(s.get("name","") for s in yc.get("north_top",[])[:4]) or "暂无"
    dt_names  = "、".join(f"{s['name']}" for s in yc.get("dragon_tiger",[])[:5]) or "暂无"
    mf_text   = "、".join(
        f"{s['code']}(+{s['net_yi']}亿)" for s in yc.get("top_moneyflow",[])[:5]
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
昨日北向：{north}亿，昨日重仓：{north_top}
昨日龙虎榜：{dt_names}
昨日主力净流入：{mf_text}

注意：今日涨停/连板/游资/热榜数据收盘后才有，在18:00收盘报告里。

请输出上午快报：

**一、今日新闻中的重要信息**
只列出上方新闻列表中实际出现的重要条目，列表为空则写"今日暂无"

**二、今日公告中的重要信号**
只分析上方公告列表中的内容，列表为空则写"今日暂无"

**三、基于昨日资金的参考判断**
昨日北向和龙虎榜数据对今日的参考意义

**四、今日操作建议**
无充分数据支撑时直接建议观望为主，等待18:00收盘报告"""

    print("  AI上午快报分析...")
    return ask_deepseek(prompt, max_tokens=1500)


def build_morning_html(title, ai_report, news_data, announcements, yest_capital) -> str:
    yc       = yest_capital
    north    = yc.get("north_total", 0)
    north_color = "#d63031" if north > 0 else "#00b894"
    north_val   = f"{'+' if north>0 else ''}{north}亿"
    north_top   = "、".join(s.get("name","") for s in yc.get("north_top",[])[:4]) or "暂无"

    # 昨日龙虎榜
    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td></tr>"
        for s in yc.get("dragon_tiger", [])[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 昨日主力资金
    mf_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_yi']}亿</td></tr>"
        for s in yc.get("top_moneyflow", [])[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 今日新闻
    news_items = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px;white-space:nowrap'>{n['time']}</td>"
        f"<td style='padding:5px 8px;font-size:12px'>{n['title']}</td>"
        f"</tr>"
        for n in news_data[:12]
    ) or "<tr><td colspan='2' style='padding:10px;text-align:center;color:#aaa'>今日暂无政策相关新闻</td></tr>"

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
        今日政策新闻快讯（{TODAY_CN}）
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
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">

      <div style="background:#fff5f5;border-radius:8px;padding:12px;border:0.5px solid #fab1a0">
        <div style="font-size:10px;color:#888;margin-bottom:4px">昨日北向资金</div>
        <div style="font-size:18px;font-weight:600;color:{north_color}">{north_val}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">{north_top}</div>
      </div>

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

    north_text = f"北向净流入：{northbound.get('total',0)}亿"
    if northbound.get("top_stocks"):
        north_text += "，重仓：" + "、".join(
            f"{s.get('name','')}({round(safe_float(s.get('net_amount',0))/1e8,1)}亿)"
            for s in northbound["top_stocks"][:5]
        )

    mf_text = "\n".join(
        f"- {s['code']}：大单净流入{s['net_flow_yi']}亿"
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
    if northbound.get("total", 0) == 0:   missing.append("北向资金")
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

【北向资金】
{north_text}

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
- 北向/主力资金（数据缺失直接标注"缺失"）
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

    north_color = "#d63031" if northbound.get("total",0) > 0 else "#00b894"
    north_val   = f"{'+' if northbound.get('total',0)>0 else ''}{northbound.get('total',0)}亿"
    north_top   = "、".join(s.get("name","") for s in northbound.get("top_stocks",[])[:4]) or "暂无"

    mf_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in moneyflow[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;color:#6c5ce7;font-size:11px'>{s['signal']}</td></tr>"
        for s in dragon_tiger[:5]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    sector_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in sector_flow[:6]
    ) or "<tr><td colspan='2' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

    broker_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;text-align:center;color:#6c5ce7;font-weight:500'>{s['broker_count']}家</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['brokers']}</td></tr>"
        for s in broker_rec[:5]
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa;font-size:11px'>暂无</td></tr>"

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
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-bottom:20px">
      <div style="background:#fff5f5;border-radius:8px;padding:12px;border:0.5px solid #fab1a0">
        <div style="font-size:10px;color:#888;margin-bottom:4px">北向资金</div>
        <div style="font-size:18px;font-weight:600;color:{north_color}">{north_val}</div>
        <div style="font-size:10px;color:#aaa;margin-top:3px">{north_top}</div>
      </div>
      <div style="background:#f0fff4;border-radius:8px;padding:12px;border:0.5px solid #55efc4">
        <div style="font-size:10px;color:#888;margin-bottom:4px">龙虎榜机构</div>
        <table style="width:100%;font-size:11px">{dt_rows}</table>
      </div>
      <div style="background:#f0f7ff;border-radius:8px;padding:12px;border:0.5px solid #74b9ff">
        <div style="font-size:10px;color:#888;margin-bottom:4px">行业资金TOP6</div>
        <table style="width:100%;font-size:11px">{sector_rows}</table>
      </div>
      <div style="background:#f9f0ff;border-radius:8px;padding:12px;border:0.5px solid #a29bfe">
        <div style="font-size:10px;color:#888;margin-bottom:4px">券商金股</div>
        <table style="width:100%;font-size:11px">{broker_rows}</table>
      </div>
    </div>

    <!-- 主力资金 -->
    <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:8px">主力大单净流入 TOP10</div>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-bottom:20px">
      <thead><tr style="background:#f8f9fa;color:#888">
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

    elif MODE == "financial" and IS_EARNINGS and WEEKDAY == 4:
        # ══ 财报专项（周五，财报季内）══
        print("\n=== 财报专项报告模式 ===")
        financial_data   = get_financial_data()
        industry_trend   = get_industry_financial_trend()
        names            = get_stock_names()
        broker_rec       = get_broker_recommend()
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
        northbound  = get_northbound()
        moneyflow   = get_moneyflow()
        dragon_tiger= get_dragon_tiger()
        block_trade = get_block_trade()
        sector_flow = get_sector_flow()
        broker_rec  = get_broker_recommend()

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
