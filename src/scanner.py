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
    "政策","国务院","发改委","工信部","财政部","战略","支持",
    "算力","半导体","新能源","军工","生物","机器人","低空",
    "储能","补贴","专项债","产业基金","规划","攻关","突破",
    "卡脖子","自主可控","国产替代","先进制造","数字经济",
    "人形机器人","具身智能","大模型","芯片","光伏","氢能",
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
    print("【政策新闻】多源抓取...")
    result = []

    # 1. 财联社
    try:
        import akshare as ak
        for fname in ["stock_telegraph_cls", "stock_cls_telegraph", "news_cls_telegraph"]:
            func = getattr(ak, fname, None)
            if not func:
                continue
            try:
                df = func()
                if df is not None and len(df) > 0:
                    for _, row in df.head(60).iterrows():
                        text = str(row.get("content", row.get("title", "")))
                        if any(k in text for k in POLICY_KEYWORDS):
                            result.append(f"[财联社] {text[:200]}")
                    if [r for r in result if "财联社" in r]:
                        break
            except Exception:
                continue
    except Exception as e:
        print(f"  财联社失败: {e}")

    # 2. 新华社RSS
    for url in [
        "http://www.xinhuanet.com/politics/news_politics.xml",
        "http://www.xinhuanet.com/fortune/news_fortune.xml",
    ]:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "")
                if any(k in title for k in POLICY_KEYWORDS):
                    result.append(f"[新华社] {title[:150]}")
        except Exception as e:
            print(f"  新华社失败: {e}")

    # 3. RSSHub政府部委
    for base in ["https://rsshub.app", "https://rsshub.rssforever.com"]:
        for route, name in [
            ("/gov/govscn", "国务院"),
            ("/ndrc/xwdt",  "发改委"),
            ("/miit/xwdt",  "工信部"),
            ("/csrc/news",  "证监会"),
        ]:
            try:
                resp = requests.get(f"{base}{route}", timeout=6,
                                    headers={"User-Agent": "Mozilla/5.0"})
                if resp.status_code != 200:
                    continue
                root = ElementTree.fromstring(resp.content)
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    if any(k in title for k in POLICY_KEYWORDS):
                        result.append(f"[{name}] {title[:150]}")
                break
            except Exception:
                continue
        time.sleep(0.2)

    # 去重
    seen, deduped = set(), []
    for item in result:
        key = item[6:35]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    print(f"  政策新闻：{len(deduped)}条")
    return deduped[:25]


def fetch_announcements() -> list:
    """重大公告：业绩快报、重大合同、股权变动等"""
    print("【公告】重大公告抓取...")
    result = []
    try:
        # Tushare公告接口
        df = pro.anns(
            ts_code="",
            ann_date=TODAY,
            start_date=TODAY,
            end_date=TODAY,
        )
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
#  模块二：上午行情（Tushare Pro 6000积分全盘中接口）
# ══════════════════════════════════════════════════════════

def get_limit_up_data() -> dict:
    """
    涨跌停和炸板数据（盘中实时）
    包含：涨停全名单、跌停全名单、炸板股
    这是最直接的市场强弱信号
    """
    print("【涨跌停】今日涨停板数据...")
    result = {
        "limit_up_list": [],    # 涨停股
        "limit_down_list": [],  # 跌停股
        "broken_list": [],      # 炸板股（曾经涨停但打开）
        "limit_up_count": 0,
        "limit_down_count": 0,
        "broken_count": 0,
    }
    try:
        df = pro.limit_list_d(
            trade_date=TODAY,
            limit_type="U",     # U=涨停
            fields="ts_code,name,close,pct_chg,amp,fc_ratio,fl_ratio,fd_amount,first_time,last_time,open_times,strth,limit"
        )
        if df is not None and len(df) > 0:
            result["limit_up_count"] = len(df)
            for _, row in df.head(20).iterrows():
                result["limit_up_list"].append({
                    "code":       row.get("ts_code", ""),
                    "name":       row.get("name", ""),
                    "open_times": int(safe_float(row.get("open_times", 0))),  # 开板次数，0=一字板
                    "first_time": str(row.get("first_time", "")),             # 首次涨停时间
                    "fc_ratio":   round(safe_float(row.get("fc_ratio", 0)), 2), # 封单比
                    "strth":      round(safe_float(row.get("strth", 0)), 2),    # 涨停强度
                })
            print(f"  涨停：{result['limit_up_count']}只，其中一字板：{sum(1 for s in result['limit_up_list'] if s['open_times']==0)}只")
        # 跌停
        df_d = pro.limit_list_d(trade_date=TODAY, limit_type="D",
                                fields="ts_code,name,close,pct_chg,open_times,first_time")
        if df_d is not None and len(df_d) > 0:
            result["limit_down_count"] = len(df_d)
            for _, row in df_d.head(10).iterrows():
                result["limit_down_list"].append({
                    "code": row.get("ts_code", ""),
                    "name": row.get("name", ""),
                    "open_times": int(safe_float(row.get("open_times", 0))),
                })
            print(f"  跌停：{result['limit_down_count']}只")
        # 炸板（曾涨停但打开）
        df_z = pro.limit_list_d(trade_date=TODAY, limit_type="Z",
                                fields="ts_code,name,close,pct_chg,open_times,first_time")
        if df_z is not None and len(df_z) > 0:
            result["broken_count"] = len(df_z)
            for _, row in df_z.head(10).iterrows():
                result["broken_list"].append({
                    "code": row.get("ts_code", ""),
                    "name": row.get("name", ""),
                    "open_times": int(safe_float(row.get("open_times", 0))),
                })
            print(f"  炸板：{result['broken_count']}只")
    except Exception as e:
        print(f"  涨跌停数据失败: {e}")
    return result


def get_limit_ladder() -> list:
    """
    涨停连板天梯（今日连板股梯队）
    是判断市场情绪和主线强弱的核心指标
    连板股越多越高，说明市场赚钱效应越强
    """
    print("【连板】涨停天梯...")
    result = []
    try:
        df = pro.limit_step(trade_date=TODAY,
                            fields="ts_code,name,step,close,pct_chg,open_times")
        if df is not None and len(df) > 0:
            df["step"] = pd.to_numeric(df["step"], errors="coerce")
            # 按连板数分组统计
            step_groups = df.groupby("step").agg(
                count=("ts_code", "count"),
                stocks=("name", lambda x: "、".join(x.head(3)))
            ).reset_index()
            for _, row in step_groups.sort_values("step", ascending=False).iterrows():
                s = int(safe_float(row["step"]))
                if s >= 2:  # 只看2板及以上
                    result.append({
                        "step":   s,
                        "count":  int(row["count"]),
                        "stocks": row["stocks"],
                    })
            print(f"  连板天梯：最高{result[0]['step'] if result else 0}板")
        else:
            print("  连板天梯：暂无数据")
    except Exception as e:
        print(f"  连板天梯失败: {e}")
    return result[:8]


def get_strongest_sector() -> list:
    """
    涨停最强板块统计
    今日涨停股票最集中的板块=今日主线
    """
    print("【主线】涨停最强板块...")
    result = []
    try:
        df = pro.limit_top_sector(trade_date=TODAY,
                                  fields="sector,count,rate,limit_up_count")
        if df is not None and len(df) > 0:
            df["count"] = pd.to_numeric(df["count"], errors="coerce")
            for _, row in df.nlargest(8, "count").iterrows():
                result.append({
                    "sector": row.get("sector", ""),
                    "count":  int(safe_float(row.get("count", 0))),
                    "rate":   round(safe_float(row.get("rate", 0)), 1),
                })
            print(f"  最强板块：{result[0]['sector'] if result else '暂无'}")
        else:
            print("  最强板块：暂无数据")
    except Exception as e:
        print(f"  最强板块失败: {e}")
    return result[:6]


def get_ths_hot() -> list:
    """
    同花顺热榜（实时）
    反映散户关注度，热度上升的标的往往有资金跟进
    """
    print("【热榜】同花顺热榜...")
    result = []
    try:
        df = pro.ths_hot(trade_date=TODAY,
                         fields="ts_code,name,hot,rank,pct_chg")
        if df is not None and len(df) > 0:
            df["rank"] = pd.to_numeric(df["rank"], errors="coerce")
            for _, row in df.nsmallest(10, "rank").iterrows():
                result.append({
                    "code":     row.get("ts_code", ""),
                    "name":     row.get("name", ""),
                    "rank":     int(safe_float(row.get("rank", 0))),
                    "pct_chg":  round(safe_float(row.get("pct_chg", 0)), 2),
                })
            print(f"  THS热榜：{len(result)}只")
        else:
            print("  THS热榜：暂无数据")
    except Exception as e:
        print(f"  THS热榜失败: {e}")
    return result[:10]


def get_ths_sector_flow() -> list:
    """
    同花顺板块实时资金流向
    比收盘后的行业资金更及时
    """
    print("【板块资金】THS板块实时资金流向...")
    result = []
    try:
        df = pro.moneyflow_sector_ths(trade_date=TODAY,
                                      fields="ts_code,name,pct_chg,net_amount,buy_lg_amount,sell_lg_amount")
        if df is None or len(df) == 0:
            # 备用：行业资金流向
            df = pro.moneyflow_ind_ths(trade_date=TODAY)
        if df is not None and len(df) > 0:
            net_col  = next((c for c in df.columns if "net" in c.lower() or "净" in c), None)
            name_col = next((c for c in df.columns if "name" in c.lower() or "名称" in c), None)
            pct_col  = next((c for c in df.columns if "pct" in c.lower() or "涨跌" in c), None)
            if net_col and name_col:
                df[net_col] = pd.to_numeric(df[net_col], errors="coerce")
                for _, row in df.nlargest(8, net_col).iterrows():
                    net = safe_float(row.get(net_col, 0))
                    if net > 0:
                        result.append({
                            "name":        row.get(name_col, ""),
                            "net_flow_yi": round(net / 1e8, 2),
                            "pct_chg":     round(safe_float(row.get(pct_col, 0)) if pct_col else 0, 2),
                        })
            print(f"  板块资金：{len(result)}个")
        else:
            print("  板块资金：暂无数据")
    except Exception as e:
        print(f"  板块资金失败: {e}")
    return result[:8]


def get_game_funds() -> list:
    """
    游资交易每日明细
    游资是A股最活跃的短线资金，看游资动向能判断今日热点
    """
    print("【游资】今日游资动向...")
    result = []
    try:
        df = pro.hm_detail(trade_date=TODAY,
                           fields="ts_code,name,hm_name,buy_amount,sell_amount,net_amount")
        if df is not None and len(df) > 0:
            df["net_amount"] = pd.to_numeric(df["net_amount"], errors="coerce")
            # 按个股汇总游资净买入
            stock_sum = df.groupby(["ts_code","name"]).agg(
                net_total=("net_amount", "sum"),
                hm_names=("hm_name", lambda x: "、".join(x.head(2)))
            ).reset_index()
            for _, row in stock_sum.nlargest(8, "net_total").iterrows():
                net = safe_float(row.get("net_total", 0))
                if net > 0:
                    result.append({
                        "code":     row.get("ts_code", ""),
                        "name":     row.get("name", ""),
                        "net_wan":  round(net / 1e4, 0),
                        "hm_names": row.get("hm_names", ""),
                    })
            print(f"  游资净买入：{len(result)}只")
        else:
            print("  游资数据：暂无")
    except Exception as e:
        print(f"  游资数据失败: {e}")
    return result[:8]


def get_today_news() -> list:
    """
    Tushare新闻快讯（实时）
    比RSS更稳定，直接调用官方接口
    """
    print("【新闻】Tushare实时新闻快讯...")
    result = []
    try:
        df = pro.news(src="cls", start_date=TODAY+"09:00:00", end_date=TODAY+"12:00:00",
                      fields="title,content,pub_time")
        if df is None or len(df) == 0:
            # 备用：sina新闻
            df = pro.news(src="sina", start_date=TODAY+"09:00:00", end_date=TODAY+"12:00:00",
                          fields="title,content,pub_time")
        if df is not None and len(df) > 0:
            for _, row in df.head(50).iterrows():
                text = str(row.get("title", "")) + str(row.get("content", ""))[:100]
                if any(k in text for k in POLICY_KEYWORDS):
                    result.append({
                        "title":    str(row.get("title", ""))[:150],
                        "pub_time": str(row.get("pub_time", "")),
                    })
            print(f"  Tushare新闻：{len(result)}条政策相关")
        else:
            print("  Tushare新闻：暂无")
    except Exception as e:
        print(f"  Tushare新闻失败: {e}")
    return result[:12]


def get_today_announcements() -> list:
    """今日重大公告（Tushare官方接口）"""
    print("【公告】今日重大公告...")
    result = []
    important = ["业绩快报","业绩预告","重大合同","股权激励","收购","重组","增持","回购","分红","中标"]
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
            print("  重大公告：暂无")
    except Exception as e:
        print(f"  公告失败: {e}")
    return result[:15]


def get_market_overview() -> dict:
    """
    市场整体情绪（从涨跌停数据推算）
    盘中通过涨跌停数量判断市场情绪
    """
    result = {
        "sentiment": "数据获取中",
        "up": 0, "down": 0,
        "limit_up": 0, "limit_down": 0,
    }
    try:
        # 用指数实时日线判断大盘情绪
        df = pro.rt_k(ts_code="000001.SH,399001.SZ,399006.SZ",
                      fields="ts_code,name,close,pre_close,vol")
        if df is not None and len(df) > 0:
            for _, row in df.iterrows():
                close     = safe_float(row.get("close", 0))
                pre_close = safe_float(row.get("pre_close", 1))
                pct = (close - pre_close) / pre_close * 100 if pre_close > 0 else 0
                result[row.get("ts_code", "")] = round(pct, 2)
    except Exception as e:
        print(f"  大盘指数实时失败（需单独开权限）: {e}")
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
        if price is not None and basic is not None:
            print(f"  收盘数据：{len(price)}只")
        return price or pd.DataFrame(), basic or pd.DataFrame()
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


def ai_morning_report(policy_news, announcements, limit_data,
                       ladder, strongest_sector, ths_hot,
                       sector_flow, game_funds, news_data) -> str:
    """上午快报AI分析 - 使用Tushare全盘中接口"""

    # 涨跌停摘要
    lu  = limit_data.get("limit_up_count", 0)
    ld  = limit_data.get("limit_down_count", 0)
    brk = limit_data.get("broken_count", 0)
    one_word = sum(1 for s in limit_data.get("limit_up_list",[]) if s.get("open_times",1)==0)
    limit_text = (
        f"涨停{lu}只（一字板{one_word}只）、跌停{ld}只、炸板{brk}只\n"
        "涨停代表标的：" + "、".join(
            f"{s['name']}({'一字' if s['open_times']==0 else str(s['open_times'])+'次开板'})"
            for s in limit_data.get("limit_up_list",[])[:8]
        )
    ) if lu > 0 else "今日暂无涨停数据"

    # 连板天梯
    if ladder:
        ladder_text = "\n".join(
            f"- {row['step']}连板：{row['count']}只（{row['stocks']}）"
            for row in ladder
        )
        max_step = ladder[0]["step"] if ladder else 0
        ladder_text = f"最高{max_step}连板\n" + ladder_text
    else:
        ladder_text = "今日暂无连板数据"

    # 最强板块
    sector_text = "\n".join(
        f"- {s['sector']}：{s['count']}只涨停，占比{s['rate']}%"
        for s in strongest_sector
    ) or "暂无板块数据"

    # 板块资金
    flow_text = "\n".join(
        f"- {s['name']}：主力净流入{s['net_flow_yi']}亿，涨幅{s['pct_chg']}%"
        for s in sector_flow
    ) or "暂无板块资金数据"

    # 游资动向
    game_text = "\n".join(
        f"- {s['name']}（{s['code']}）：游资净买入{s['net_wan']}万（{s['hm_names']}）"
        for s in game_funds
    ) or "今日暂无游资数据"

    # THS热榜
    hot_text = "、".join(
        f"{s['name']}({'+' if s['pct_chg']>0 else ''}{s['pct_chg']}%)"
        for s in ths_hot[:8]
    ) or "暂无热榜数据"

    # 今日新闻
    news_text = "\n".join(
        f"- [{n['pub_time'][11:16] if len(n['pub_time'])>10 else ''}] {n['title']}"
        for n in news_data[:8]
    ) or "暂无"

    # 重大公告
    ann_text = "\n".join(
        f"- [{a['code']}] {a['title']}"
        for a in announcements[:10]
    ) or "今日暂无重大公告"

    # 政策
    policy_text = "\n".join(policy_news[:8]) if policy_news else "暂无"

    # 市场情绪判断
    if lu == 0 and ld == 0:
        sentiment_judge = "数据待更新"
    elif lu >= 50 and ld <= 10:
        sentiment_judge = "强势偏多（涨停家数多，情绪亢奋）"
    elif lu >= 20 and ld <= 20:
        sentiment_judge = "温和偏多"
    elif lu <= 10 and ld >= 30:
        sentiment_judge = "弱势偏空（跌停家数多，情绪低迷）"
    elif brk >= lu * 0.5:
        sentiment_judge = "情绪转弱（炸板多，资金不稳）"
    else:
        sentiment_judge = "中性震荡"

    prompt = f"""今天是{TODAY_CN}，上午收盘后。请基于以下Tushare盘中真实数据做上午市场快报。

【市场情绪判断】
{sentiment_judge}
{limit_text}

【连板天梯（反映赚钱效应强弱）】
{ladder_text}

【今日最强板块（涨停股集中的板块=今日主线）】
{sector_text}

【板块实时资金流向（THS）】
{flow_text}

【游资今日动向（短线热点风向标）】
{game_text}

【同花顺热榜TOP10（散户关注热度）】
{hot_text}

【今日重要新闻快讯（上午）】
{news_text}

【今日重大公告】
{ann_text}

【今日政策信号】
{policy_text}

请输出上午市场快报，把情况和行情讲清楚：

**一、今日上午市场总体判断**
基于涨停家数、连板天梯、炸板情况，判断今日市场情绪和赚钱效应

**二、今日主线方向**
最强板块是什么？背后的逻辑是政策驱动、业绩驱动还是游资炒作？
主线的持续性如何判断？

**三、重点关注标的**
结合游资动向、THS热榜、涨停标的，挑出2-3只今日最值得跟踪的标的，说明理由

**四、今日公告和新闻中的重要信号**
有哪些公告或新闻可能对今下午或明日股价产生影响？

**五、下午操作建议**
- 可以关注的方向
- 需要回避的操作
- 下午重点观察的变化"""

    print("  AI上午快报分析...")
    return ask_deepseek(prompt, max_tokens=2000)


def ai_closing_report(policy_news, stocks, market_sentiment,
                       northbound, moneyflow, dragon_tiger,
                       block_trade, sector_flow, broker_rec,
                       financial=None) -> str:
    """收盘深度报告AI分析"""
    policy_text = "\n".join(policy_news[:12]) if policy_news else "今日暂无政策信号"

    ms = market_sentiment
    sentiment_text = (
        f"市场情绪：{ms.get('sentiment','—')}，"
        f"涨{ms.get('up',0)}/跌{ms.get('down',0)}，"
        f"涨停{ms.get('limit_up',0)}/跌停{ms.get('limit_down',0)}"
    ) if ms.get("up", 0) > 0 else "数据未入库"

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

    # 财报摘要
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

    # 判断当前市场驱动逻辑
    driver = detect_market_driver(northbound, sector_flow, policy_news,
                                   ms, financial)

    # 数据完整性
    missing = []
    if ms.get("up", 0) == 0:   missing.append("市场行情")
    if northbound.get("total", 0) == 0: missing.append("北向资金")
    if not moneyflow:          missing.append("主力资金")
    warning = f"⚠️ 数据缺失：{' / '.join(missing)}，对应维度不得编造。\n\n" if missing else ""

    prompt = f"""今天是{TODAY_CN}，收盘后复盘。

{warning}
【当前市场主要驱动逻辑】
{driver}
（请在分析中重点聚焦这个驱动逻辑，这是当前市场最看重的维度）

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

【大宗交易折价（建仓信号）】
{block_text}

【券商本月金股】
{broker_text}
{fin_text}

【技术面精筛标的（评分≥5/12）】
{stocks_text}

【今日政策信号】
{policy_text}

请输出收盘深度报告：

**一、今日市场驱动逻辑判断**
当前市场最看重什么维度？为什么？这个判断如何影响选股方向？

**二、今日核心数据解读**
基于真实数据，解读资金、北向、板块的实际信号（数据缺失直接标注）

**三、明日重点关注标的（2-3只）**
每只必须同时满足：技术面 + 资金面 + 政策/基本面 至少两个维度支撑
给出：入选理由、关键价位、操作建议、综合评分（1-10）

**四、明日操作策略**
首选标的、止损原则、需要回避的方向

直接给结论，数据缺失的维度直接说"数据缺失"，不编造。"""

    print("  AI收盘深度分析...")
    return ask_deepseek(prompt, max_tokens=2500)


def ai_financial_report(financial_data, industry_trend, policy_news,
                         names, broker_rec) -> str:
    """财报专项报告AI分析"""

    def get_name(code):
        info = names.get(code, (code, ""))
        return info[0]

    hg_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：净利润同比+{s['yoy']}%（{s['ann_date']}披露）"
        for s in financial_data.get("high_growth", [])[:8]
    ) or "本期暂无"

    tv_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：{s['type']}，预增上限{s['pct_max']}%"
        for s in financial_data.get("turnaround", [])[:8]
    ) or "本期暂无"

    dt_text = "\n".join(
        f"- {get_name(s['code'])}（{s['code']}）：净利润同比{s['yoy']}%（恶化）"
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

请基于以下真实财务数据，从"市场会怎么看这些数据"的角度进行分析。

【高增长标的（净利润增速50%以上）】
{hg_text}

【业绩扭转标的（扭亏/预增）】
{tv_text}

【业绩恶化标的（下滑30%以上，需回避）】
{dt_text}

【行业盈利趋势（哪些行业整体在改善）】
{ind_text}

【券商本月重点推荐】
{broker_text}

【相关政策背景】
{policy_text}

请输出财报专项分析报告：

**一、本期财报季市场关注焦点**
市场在这个财报季最看重哪些维度？（毛利率？现金流？还是业绩拐点？）

**二、值得重点关注的业绩扭转机会**
从扭转标的中，筛选出股价尚未充分反映业绩改善的（预期差机会）
每只给出：财务改善的具体逻辑 + 对应的国家战略关联度 + 估值是否合理

**三、行业层面的财务趋势**
哪些行业的整体盈利在系统性改善？原因是什么？对应哪些A股主线？

**四、需要回避的财务风险**
业绩恶化标的中，哪些可能引发股价大跌？背后的行业逻辑是什么？

**五、本周财报季综合选股建议**
结合财务数据+政策方向+券商推荐，给出2-3只值得深度研究的标的

要求：基于真实数据分析，不编造数字，数据不足的维度直接说明。"""

    print("  AI财报专项分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ══════════════════════════════════════════════════════════
#  模块六：邮件发送
# ══════════════════════════════════════════════════════════

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
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"#{1,4}\s*(.+)", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")


def build_morning_html(title, ai_report, limit_data, ladder, strongest_sector,
                        ths_hot, sector_flow, game_funds, announcements) -> str:
    lu  = limit_data.get("limit_up_count", 0)
    ld  = limit_data.get("limit_down_count", 0)
    brk = limit_data.get("broken_count", 0)
    one_word = sum(1 for s in limit_data.get("limit_up_list",[]) if s.get("open_times",1)==0)

    # 涨停股列表
    lu_rows = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:5px 8px;text-align:center;color:{'#d63031' if s['open_times']==0 else '#e17055'}'>"
        f"{'一字' if s['open_times']==0 else str(s['open_times'])+'次开板'}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#888;font-size:11px'>{s.get('first_time','')}</td>"
        f"</tr>"
        for s in limit_data.get("limit_up_list",[])[:10]
    ) or "<tr><td colspan='4' style='padding:8px;text-align:center;color:#aaa'>暂无涨停数据</td></tr>"

    # 连板天梯
    ladder_rows = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px;font-weight:500;color:#d63031'>{row['step']}连板</td>"
        f"<td style='padding:5px 8px;text-align:center'>{row['count']}只</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{row['stocks']}</td>"
        f"</tr>"
        for row in ladder
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无连板数据</td></tr>"

    # 最强板块
    sector_rows = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px;font-weight:500'>{s['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:center;color:#d63031'>{s['count']}只涨停</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#888;font-size:11px'>{s['rate']}%</td>"
        f"</tr>"
        for s in strongest_sector
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 游资动向
    game_rows = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_wan']}万</td>"
        f"<td style='padding:5px 8px;color:#888;font-size:11px'>{s['hm_names']}</td>"
        f"</tr>"
        for s in game_funds
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无游资数据</td></tr>"

    # 板块资金
    flow_rows = "".join(
        f"<tr>"
        f"<td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#888;font-size:11px'>{s['pct_chg']}%</td>"
        f"</tr>"
        for s in sector_flow
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # THS热榜
    hot_rows = "".join(
        f"<tr>"
        f"<td style='padding:4px 8px;font-size:11px;color:#888'>{s['rank']}</td>"
        f"<td style='padding:4px 8px'>{s['name']}</td>"
        f"<td style='padding:4px 8px;text-align:right;color:{'#d63031' if s['pct_chg']>0 else '#00b894'};font-size:11px'>"
        f"{'+' if s['pct_chg']>0 else ''}{s['pct_chg']}%</td>"
        f"</tr>"
        for s in ths_hot
    ) or "<tr><td colspan='3' style='padding:8px;text-align:center;color:#aaa'>暂无</td></tr>"

    # 公告
    ann_items = "".join(
        f"<li style='padding:3px 0;font-size:12px'>[{a['code']}] {a['title']}</li>"
        for a in announcements[:8]
    ) or "<li style='color:#aaa;font-size:12px'>今日暂无重大公告</li>"

    # 情绪色
    if lu >= 50:   sc = "#d63031"
    elif lu >= 20: sc = "#e17055"
    elif lu >= 5:  sc = "#888"
    else:          sc = "#00b894"

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:760px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,#0984e3,#00cec9);padding:20px 28px;color:#fff">
    <div style="font-size:17px;font-weight:600">{title}</div>
    <div style="font-size:11px;opacity:0.85;margin-top:3px">{TODAY_CN} · 上午收盘快报 · v7.0 · Tushare Pro 6000积分</div>
  </div>

  <div style="padding:20px 28px">

    <!-- 核心数据卡片 -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:20px">
      <div style="background:#fff5f5;border-radius:8px;padding:12px;text-align:center;border:0.5px solid #fab1a0">
        <div style="font-size:10px;color:#888">涨停</div>
        <div style="font-size:24px;font-weight:600;color:{sc}">{lu}</div>
        <div style="font-size:10px;color:#aaa">一字板{one_word}只</div>
      </div>
      <div style="background:#f0fff4;border-radius:8px;padding:12px;text-align:center;border:0.5px solid #55efc4">
        <div style="font-size:10px;color:#888">跌停</div>
        <div style="font-size:24px;font-weight:600;color:#00b894">{ld}</div>
        <div style="font-size:10px;color:#aaa">炸板{brk}只</div>
      </div>
      <div style="background:#f8f9fa;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:10px;color:#888">最高连板</div>
        <div style="font-size:24px;font-weight:600;color:#6c5ce7">{ladder[0]['step'] if ladder else 0}</div>
        <div style="font-size:10px;color:#aaa">板</div>
      </div>
      <div style="background:#f8f9fa;border-radius:8px;padding:12px;text-align:center">
        <div style="font-size:10px;color:#888">情绪</div>
        <div style="font-size:13px;font-weight:500;color:{sc}">{'亢奋' if lu>=50 else '偏强' if lu>=20 else '一般' if lu>=5 else '低迷'}</div>
      </div>
    </div>

    <!-- AI快报 -->
    <div style="background:#f0f7ff;border-left:4px solid #0984e3;padding:14px 18px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <div style="font-size:13px;font-weight:500;color:#0984e3;margin-bottom:8px">上午快报</div>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    <!-- 四列数据 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">今日涨停股</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">代码</th>
            <th style="padding:5px 8px;text-align:center;font-weight:400">状态</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">首封时间</th>
          </tr>
          {lu_rows}
        </table>
      </div>

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">连板天梯（赚钱效应）</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">连板</th>
            <th style="padding:5px 8px;text-align:center;font-weight:400">数量</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">代表股</th>
          </tr>
          {ladder_rows}
        </table>
      </div>

    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">今日最强板块（主线）</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">板块</th>
            <th style="padding:5px 8px;text-align:center;font-weight:400">涨停数</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">占比</th>
          </tr>
          {sector_rows}
        </table>
      </div>

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">游资今日动向</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">净买入</th>
            <th style="padding:5px 8px;text-align:left;font-weight:400">游资</th>
          </tr>
          {game_rows}
        </table>
      </div>

    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">板块资金流向（THS）</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:5px 8px;text-align:left;font-weight:400">板块</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">净流入</th>
            <th style="padding:5px 8px;text-align:right;font-weight:400">涨幅</th>
          </tr>
          {flow_rows}
        </table>
      </div>

      <div>
        <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">同花顺热榜</div>
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <tr style="background:#f8f9fa;color:#888">
            <th style="padding:4px 8px;text-align:left;font-weight:400">排名</th>
            <th style="padding:4px 8px;text-align:left;font-weight:400">股票</th>
            <th style="padding:4px 8px;text-align:right;font-weight:400">涨幅</th>
          </tr>
          {hot_rows}
        </table>
      </div>

    </div>

    <!-- 重大公告 -->
    <div style="margin-top:16px">
      <div style="font-size:13px;font-weight:500;color:#2d3436;margin-bottom:6px">今日重大公告</div>
      <ul style="margin:0;padding-left:16px">{ann_items}</ul>
    </div>

  </div>
  <div style="padding:10px 28px;background:#f8f9fa;color:#aaa;font-size:10px;text-align:center;border-top:1px solid #eee">
    本报告由AI自动生成，数据源：Tushare Pro 6000积分（涨停/连板/游资/热榜/板块资金），不构成投资建议。
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
        # ══ 上午快报（Tushare全盘中接口）══
        print("\n=== 上午快报模式 ===")
        limit_data       = get_limit_up_data()        # 涨跌停全名单
        ladder           = get_limit_ladder()          # 连板天梯
        strongest_sector = get_strongest_sector()      # 最强板块
        ths_hot          = get_ths_hot()               # 同花顺热榜
        sector_flow      = get_ths_sector_flow()       # 板块资金
        game_funds       = get_game_funds()            # 游资动向
        news_data        = get_today_news()            # Tushare新闻
        announcements    = get_today_announcements()   # 重大公告

        ai_report = ai_morning_report(
            policy_news, announcements, limit_data,
            ladder, strongest_sector, ths_hot,
            sector_flow, game_funds, news_data
        )

        subject = f"【A股上午】{TODAY_CN} · 上午收盘快报"
        html    = build_morning_html(
            subject, ai_report, limit_data, ladder,
            strongest_sector, ths_hot, sector_flow,
            game_funds, announcements
        )
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
