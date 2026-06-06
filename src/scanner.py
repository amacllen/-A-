"""
A股智能选股系统 v6.0
数据源：Tushare Pro（5000积分全接口）+ 财联社新闻
盘中版（10:30）：当日行情异动筛选
收盘版（18:00）：完整技术面 + 全维度资金数据
AI引擎：DeepSeek   推送：邮件（支持多收件人）
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
_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
TODAY    = _bj.strftime("%Y%m%d")
TODAY_CN = _bj.strftime("%Y年%m月%d日")
WEEKDAY  = _bj.weekday()
NOW_HOUR = datetime.datetime.utcnow().hour
NOW_MIN  = datetime.datetime.utcnow().minute

# 盘中：UTC 01:30-05:00 = 北京 09:30-13:00
IS_INTRADAY = (1 <= NOW_HOUR < 5) or (NOW_HOUR == 1 and NOW_MIN >= 30)
IS_WEEKEND  = WEEKDAY >= 5
MODE        = "盘中" if IS_INTRADAY else "收盘复盘"

POLICY_KEYWORDS = [
    "政策","国务院","发改委","工信部","财政部","战略","支持",
    "算力","半导体","新能源","军工","生物","机器人","低空",
    "储能","补贴","专项债","产业基金","规划","攻关","突破",
    "卡脖子","自主可控","国产替代","先进制造","数字经济",
    "人形机器人","具身智能","大模型","芯片","光伏","氢能",
]

print(f"\n{'='*55}")
print(f"A股智能选股系统 v6.0 — {MODE}模式")
print(f"运行时间：{_bj.strftime('%Y-%m-%d %H:%M')} 北京时间")
print(f"{'='*55}\n")


def safe_float(val, default=0.0):
    try:
        return float(val)
    except:
        return default


# ══════════════════════════════════════════════════════════
#  模块一：政策情报（新闻源）
# ══════════════════════════════════════════════════════════

def fetch_policy_news() -> list:
    """多源政策新闻抓取"""
    print("【政策情报】多源抓取...")
    result = []

    # 1. 财联社（多函数名容错）
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
                    print(f"  财联社({fname})：{len([r for r in result if '财联社' in r])}条")
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"  财联社失败: {e}")

    # 2. 新华社RSS（境外实例，GitHub可访问）
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

    # 3. RSSHub政府部委（境外实例）
    rsshub_bases = [
        "https://rsshub.app",
        "https://rsshub.rssforever.com",
        "https://hub.slarker.me",
    ]
    gov_routes = [
        ("/gov/govscn",   "国务院"),
        ("/ndrc/xwdt",    "发改委"),
        ("/miit/xwdt",    "工信部"),
        ("/csrc/news",    "证监会"),
        ("/pbc/zhengcefabu", "央行"),
    ]
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/xml,text/xml"}
    for route, name in gov_routes:
        for base in rsshub_bases:
            try:
                resp = requests.get(f"{base}{route}", timeout=6, headers=headers)
                if resp.status_code != 200:
                    continue
                root = ElementTree.fromstring(resp.content)
                cnt = 0
                for item in root.iter("item"):
                    title = item.findtext("title", "")
                    if any(k in title for k in POLICY_KEYWORDS):
                        result.append(f"[{name}] {title[:150]}")
                        cnt += 1
                if cnt:
                    print(f"  {name}：{cnt}条")
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

    print(f"  政策新闻合计（去重）：{len(deduped)}条")
    return deduped[:25]


# ══════════════════════════════════════════════════════════
#  模块二：Tushare 全量行情数据
# ══════════════════════════════════════════════════════════

def get_daily_basic() -> pd.DataFrame:
    """
    收盘基础指标（5000积分）
    包含：换手率、量比、市值、PE、PB、涨跌幅等
    每日15:00-16:00入库
    """
    print("【行情】每日基础指标...")
    try:
        df = pro.daily_basic(
            trade_date=TODAY,
            fields="ts_code,close,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv,pct_chg"
        )
        if df is None or len(df) == 0:
            print("  今日数据未入库（可能收盘前运行）")
            return pd.DataFrame()
        print(f"  获取到 {len(df)} 只股票基础指标")
        return df
    except Exception as e:
        print(f"  daily_basic失败: {e}")
        return pd.DataFrame()


def get_daily_price() -> pd.DataFrame:
    """当日收盘行情（涨跌幅、成交量）"""
    print("【行情】当日价格行情...")
    try:
        df = pro.daily(trade_date=TODAY,
                       fields="ts_code,open,high,low,close,pre_close,change,pct_chg,vol,amount")
        if df is None or len(df) == 0:
            return pd.DataFrame()
        print(f"  获取到 {len(df)} 只股票价格数据")
        return df
    except Exception as e:
        print(f"  daily失败: {e}")
        return pd.DataFrame()


def get_stock_names() -> dict:
    """获取股票名称映射"""
    try:
        df = pro.stock_basic(exchange="", list_status="L",
                             fields="ts_code,name,industry")
        if df is not None and len(df) > 0:
            return dict(zip(df["ts_code"], zip(df["name"], df["industry"])))
    except Exception as e:
        print(f"  股票名称获取失败: {e}")
    return {}


def quant_filter_closing(daily_basic: pd.DataFrame,
                          daily_price: pd.DataFrame,
                          names: dict) -> list:
    """
    收盘版量化筛选：
    - 市值 50亿～500亿（流通市值）
    - 涨幅 1.5%～9.5%
    - 换手率 1%～15%
    - 量比 > 1.2
    - PE > 0（排除亏损股）
    """
    print("【筛选】收盘量化精筛...")
    if daily_basic.empty or daily_price.empty:
        print("  数据不足，跳过筛选")
        return []
    try:
        # 合并
        df = pd.merge(daily_basic, daily_price[["ts_code","pct_chg","vol","amount"]],
                      on="ts_code", how="inner", suffixes=("","_p"))
        # 用daily_basic的pct_chg（更准确）
        df["pct"] = pd.to_numeric(df.get("pct_chg",""), errors="coerce")
        df["mv"]  = pd.to_numeric(df.get("circ_mv",""), errors="coerce")  # 万元
        df["tr"]  = pd.to_numeric(df.get("turnover_rate",""), errors="coerce")
        df["vr"]  = pd.to_numeric(df.get("volume_ratio",""), errors="coerce")
        df["pe"]  = pd.to_numeric(df.get("pe",""), errors="coerce")

        filtered = df[
            (df["mv"]  >= 50000)    &   # 流通市值 >= 50亿（万元单位）
            (df["mv"]  <= 500000)   &   # 流通市值 <= 500亿
            (df["pct"] >= 1.5)      &   # 涨幅 >= 1.5%
            (df["pct"] <= 9.5)      &   # 排除涨停
            (df["tr"]  >= 1.0)      &   # 换手率 >= 1%
            (df["tr"]  <= 15.0)     &   # 排除过热
            (df["vr"]  >= 1.2)      &   # 量比 >= 1.2
            (df["pe"]  > 0)             # 排除亏损股
        ].nlargest(40, "vr")

        result = []
        for _, row in filtered.iterrows():
            code = row["ts_code"]
            name_info = names.get(code, ("未知", ""))
            result.append({
                "code":          code,
                "name":          name_info[0],
                "industry":      name_info[1],
                "price":         safe_float(row.get("close")),
                "change_pct":    round(safe_float(row.get("pct")), 2),
                "market_cap_yi": round(safe_float(row.get("mv")) / 10000, 1),
                "turnover_rate": round(safe_float(row.get("tr")), 2),
                "volume_ratio":  round(safe_float(row.get("vr")), 2),
                "pe":            round(safe_float(row.get("pe")), 1),
                "pb":            round(safe_float(row.get("pb")), 2),
            })
        print(f"  量化精筛结果：{len(result)} 只")
        return result[:20]
    except Exception as e:
        print(f"  量化筛选失败: {e}")
        return []


def get_stk_factor_batch(trade_date: str) -> pd.DataFrame:
    """
    批量获取全市场技术因子（6000积分）
    stk_factor 一次调取全市场当日技术指标
    比逐只计算快100倍，且数据更准确
    包含：MACD、KDJ、RSI、布林带、均线等
    """
    print("【技术因子】批量获取全市场技术指标...")
    try:
        df = pro.stk_factor(
            trade_date=trade_date,
            fields=(
                "ts_code,trade_date,"
                "close,ma5,ma10,ma20,ma60,"
                "dif,dea,macd,"
                "kdj_k,kdj_d,kdj_j,"
                "rsi_6,rsi_12,"
                "boll_upper,boll_mid,boll_lower,"
                "cci,turnover_rate,volume_ratio"
            )
        )
        if df is not None and len(df) > 0:
            print(f"  技术因子：获取到 {len(df)} 只股票数据")
            return df
        print("  技术因子：今日数据未入库")
        return pd.DataFrame()
    except Exception as e:
        print(f"  stk_factor失败: {e}")
        return pd.DataFrame()


def tech_filter(candidates: list, factor_df: pd.DataFrame = None) -> list:
    """
    用stk_factor批量技术因子精筛
    不需要逐只调接口，直接从factor_df里查找
    """
    print(f"  技术面精筛 {len(candidates)} 只候选标的...")

    # 构建因子索引
    factor_map = {}
    if factor_df is not None and len(factor_df) > 0:
        for _, row in factor_df.iterrows():
            factor_map[row["ts_code"]] = row

    qualified = []
    for stock in candidates:
        code = stock["code"]
        row  = factor_map.get(code)

        if row is None:
            # 没有因子数据，基础评分
            stock["tech"] = {}
            stock["tech_score"] = 1 if stock["change_pct"] >= 3 else 0
            continue

        close = safe_float(row.get("close"))
        ma5   = safe_float(row.get("ma5"))
        ma10  = safe_float(row.get("ma10"))
        ma20  = safe_float(row.get("ma20"))
        ma60  = safe_float(row.get("ma60"))
        dif   = safe_float(row.get("dif"))
        dea   = safe_float(row.get("dea"))
        macd  = safe_float(row.get("macd"))
        rsi6  = safe_float(row.get("rsi_6"))
        kdj_k = safe_float(row.get("kdj_k"))
        kdj_d = safe_float(row.get("kdj_d"))
        b_up  = safe_float(row.get("boll_upper"))
        b_mid = safe_float(row.get("boll_mid"))
        b_low = safe_float(row.get("boll_lower"))
        vr    = safe_float(row.get("volume_ratio"))

        # 均线多头排列
        ma_bull = (close > ma5 > ma10 > ma20) if all([close,ma5,ma10,ma20]) else False
        # MACD多头（DIF>DEA且MACD柱放大）
        macd_bull = (dif > dea > 0) if all([dif,dea]) else False
        # 量能放大（量比>1.1）
        vol_expand = vr > 1.1 if vr else False
        # 布林带位置（0=下轨，1=上轨）
        bb_range = b_up - b_low if (b_up and b_low and b_up > b_low) else 0
        bb_pos = (close - b_low) / bb_range if bb_range > 0 else 0.5
        # KDJ金叉信号
        kdj_bull = (kdj_k > kdj_d) if (kdj_k and kdj_d) else False
        # RSI健康区间（40-75）
        rsi_ok = (40 < rsi6 < 75) if rsi6 else False

        # 综合评分（满分12分）
        score = 0
        if ma_bull:                     score += 3
        if macd_bull:                   score += 3
        if vol_expand:                  score += 2
        if kdj_bull:                    score += 1
        if rsi_ok:                      score += 1
        if 0.2 < bb_pos < 0.8:         score += 1
        if stock["change_pct"] >= 3:   score += 1

        stock["tech"] = {
            "ma_bullish":    ma_bull,
            "macd_bullish":  macd_bull,
            "vol_expanding": vol_expand,
            "kdj_bullish":   kdj_bull,
            "rsi_6":         round(rsi6, 1) if rsi6 else None,
            "bb_position":   round(bb_pos, 2),
            "ma5": round(ma5,2), "ma10": round(ma10,2),
            "ma20": round(ma20,2), "ma60": round(ma60,2) if ma60 else None,
            "dif": round(dif,4), "dea": round(dea,4),
        }
        stock["tech_score"] = score

        if score >= 5:
            qualified.append(stock)

    qualified.sort(key=lambda x: x["tech_score"], reverse=True)
    print(f"  技术面精筛：{len(qualified)} 只入围（评分≥5/12）")
    return qualified[:15]


# ══════════════════════════════════════════════════════════
#  模块三：Tushare 资金数据（全解锁）
# ══════════════════════════════════════════════════════════

def get_market_sentiment(daily_price: pd.DataFrame) -> dict:
    """从daily数据计算市场情绪（不需要额外接口）"""
    result = {"up": 0, "down": 0, "flat": 0, "limit_up": 0, "limit_down": 0, "sentiment": "中性"}
    try:
        if daily_price.empty:
            return result
        pct = pd.to_numeric(daily_price["pct_chg"], errors="coerce")
        result["up"]         = int((pct > 0).sum())
        result["down"]       = int((pct < 0).sum())
        result["flat"]       = int((pct == 0).sum())
        result["limit_up"]   = int((pct >= 9.9).sum())
        result["limit_down"] = int((pct <= -9.9).sum())
        ratio = result["up"] / max(result["up"] + result["down"], 1)
        result["sentiment"] = (
            "强势偏多" if ratio > 0.65 else
            "温和偏多" if ratio > 0.55 else
            "中性震荡" if ratio > 0.45 else
            "温和偏空" if ratio > 0.35 else "弱势偏空"
        )
        print(f"  市场情绪：{result['sentiment']}，涨{result['up']}跌{result['down']}，涨停{result['limit_up']}")
    except Exception as e:
        print(f"  市场情绪计算失败: {e}")
    return result


def get_northbound() -> dict:
    """北向资金"""
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
            result["top_stocks"] = df_top[["name","net_amount"]].head(10).to_dict("records")
        print(f"  北向资金：{result['total']}亿（沪股通{result['sh']}亿，深股通{result['sz']}亿）")
    except Exception as e:
        print(f"  北向资金失败: {e}")
    return result


def get_moneyflow_rank() -> list:
    """
    个股资金流向（2000积分解锁，5000积分高频）
    大单净流入排行：特大单+大单净额
    """
    print("【资金】主力资金净流入排行...")
    result = []
    try:
        df = pro.moneyflow(trade_date=TODAY,
                           fields="ts_code,buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount")
        if df is None or len(df) == 0:
            print("  今日资金流向数据未入库")
            return result
        # 大单+特大单净流入
        df["big_net"] = (
            pd.to_numeric(df["buy_elg_amount"], errors="coerce").fillna(0) +
            pd.to_numeric(df["buy_lg_amount"],  errors="coerce").fillna(0) -
            pd.to_numeric(df["sell_elg_amount"], errors="coerce").fillna(0) -
            pd.to_numeric(df["sell_lg_amount"],  errors="coerce").fillna(0)
        )
        top = df.nlargest(15, "big_net")
        for _, row in top.iterrows():
            net = safe_float(row.get("big_net", 0))
            if net > 0:
                result.append({
                    "code":        row["ts_code"],
                    "net_flow_yi": round(net / 1e8, 2),
                    "net_mf_yi":   round(safe_float(row.get("net_mf_amount", 0)) / 1e8, 2),
                })
        print(f"  主力净流入标的：{len(result)}只")
    except Exception as e:
        print(f"  moneyflow失败: {e}")
    return result[:10]


def get_dragon_tiger() -> list:
    """龙虎榜机构席位（2000积分）"""
    print("【资金】龙虎榜...")
    result = []
    try:
        df_list = pro.top_list(trade_date=TODAY)
        df_inst = pro.top_inst(trade_date=TODAY)
        if df_inst is not None and len(df_inst) > 0:
            for code in df_inst["ts_code"].unique()[:10]:
                name = "未知"
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
    """大宗交易折价（120积分）"""
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


def get_margin_data() -> list:
    """融资余额变化（2000积分）"""
    print("【资金】融资融券...")
    result = []
    try:
        df = pro.margin_detail(trade_date=TODAY)
        if df is not None and len(df) > 0:
            df["rzmre"] = pd.to_numeric(df["rzmre"], errors="coerce")
            df["rzye"]  = pd.to_numeric(df["rzye"],  errors="coerce")
            top = df.nlargest(10, "rzmre")
            for _, row in top.iterrows():
                result.append({
                    "code":      row.get("ts_code", ""),
                    "rzmre_wan": round(safe_float(row.get("rzmre")) / 1e4, 0),
                    "rzye_yi":   round(safe_float(row.get("rzye"))  / 1e8, 2),
                })
        print(f"  融资买入：{len(result)}条")
    except Exception as e:
        print(f"  融资融券失败: {e}")
    return result[:8]


def get_inst_survey() -> list:
    """机构调研（5000积分）"""
    print("【资金】机构调研...")
    result = []
    try:
        end   = TODAY
        start = (_bj - datetime.timedelta(days=7)).strftime("%Y%m%d")
        df = pro.stk_surv(start_date=start, end_date=end)
        if df is not None and len(df) > 0:
            df["fund_nums"] = pd.to_numeric(df["fund_nums"] if "fund_nums" in df.columns else 0, errors="coerce").fillna(0)
            for _, row in df.nlargest(10, "fund_nums").iterrows():
                result.append({
                    "name":      row.get("name", ""),
                    "code":      row.get("ts_code", ""),
                    "fund_nums": int(row.get("fund_nums", 0)),
                    "date":      row.get("surv_date", ""),
                })
        print(f"  机构调研：{len(result)}条（近7日）")
    except Exception as e:
        print(f"  机构调研失败: {e}")
    return result[:8]


def get_sector_moneyflow() -> list:
    """
    行业资金流向（5000积分，同花顺行业）
    接口：moneyflow_ind_ths
    """
    print("【资金】行业资金流向...")
    result = []
    try:
        df = pro.moneyflow_ind_ths(trade_date=TODAY)
        if df is None or len(df) == 0:
            print("  行业资金流向暂无数据")
            return result
        df["net_amount"] = pd.to_numeric(df.get("net_amount", 0), errors="coerce")
        for _, row in df.nlargest(8, "net_amount").iterrows():
            net = safe_float(row.get("net_amount", 0))
            if net > 0:
                result.append({
                    "sector":      row.get("industry", ""),
                    "net_flow_yi": round(net / 1e8, 2),
                    "close":       safe_float(row.get("close", 0)),
                })
        print(f"  行业资金流向：{len(result)}个板块")
    except Exception as e:
        print(f"  行业资金流向失败: {e}")
    return result[:8]


def get_forecast() -> list:
    """
    业绩预测（盈利预期差）- 6000积分
    获取近期分析师上调盈利预测的标的
    预期差 = 市场低估但分析师预期高增长的标的
    """
    print("【预期差】分析师盈利预测...")
    result = []
    try:
        # 获取近期业绩预告（上调评级）
        # 先查今日，再查近7天
        df = None
        try:
            df = pro.forecast(
                ann_date=TODAY,
                fields="ts_code,ann_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max"
            )
        except Exception:
            pass
        if df is None or len(df) == 0:
            start = (_bj - datetime.timedelta(days=7)).strftime("%Y%m%d")
            try:
                df = pro.forecast(
                    start_date=start,
                    end_date=TODAY,
                    fields="ts_code,ann_date,type,p_change_min,p_change_max,net_profit_min,net_profit_max"
                )
            except Exception:
                pass
        if df is not None and len(df) > 0:
            # 筛选业绩大幅增长的（净利润增速>50%）
            df["p_change_max"] = pd.to_numeric(df.get("p_change_max", 0), errors="coerce")
            high_growth = df[df["p_change_max"] >= 50].nlargest(10, "p_change_max")
            for _, row in high_growth.iterrows():
                result.append({
                    "code":       row.get("ts_code", ""),
                    "type":       row.get("type", ""),
                    "pct_max":    round(safe_float(row.get("p_change_max")), 0),
                    "pct_min":    round(safe_float(row.get("p_change_min")), 0),
                    "ann_date":   row.get("ann_date", ""),
                    "summary":    str(row.get("summary", ""))[:80],
                })
            print(f"  高增长业绩预告：{len(result)}条")
        else:
            print("  今日暂无业绩预告数据")
    except Exception as e:
        print(f"  业绩预测失败: {e}")
    return result[:8]


def get_broker_recommend() -> list:
    """
    券商金股（6000积分）
    每月券商重点推荐的标的，机构背书的买入信号
    """
    print("【券商】金股推荐...")
    result = []
    try:
        # 获取本月券商金股
        month = _bj.strftime("%Y%m")
        df = pro.broker_recommend(month=month,
                                  fields="ts_code,name,broker,reason")
        if df is None or len(df) == 0:
            # 尝试上月
            last_month = (_bj - datetime.timedelta(days=30)).strftime("%Y%m")
            df = pro.broker_recommend(month=last_month,
                                      fields="ts_code,name,broker,reason")
        if df is not None and len(df) > 0:
            # 按被推荐次数统计（被多家券商推荐的更有价值）
            recommend_count = df.groupby("ts_code").agg(
                name=("name", "first"),
                broker_count=("broker", "count"),
                brokers=("broker", lambda x: "、".join(x.head(3)))
            ).reset_index()
            for _, row in recommend_count.nlargest(8, "broker_count").iterrows():
                result.append({
                    "code":         row["ts_code"],
                    "name":         row["name"],
                    "broker_count": int(row["broker_count"]),
                    "brokers":      row["brokers"],
                })
            print(f"  券商金股：{len(result)}只（本月/上月）")
        else:
            print("  券商金股暂无数据")
    except Exception as e:
        print(f"  券商金股失败: {e}")
    return result[:6]


# ══════════════════════════════════════════════════════════
#  模块四：AI 分析
# ══════════════════════════════════════════════════════════

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
            print(f"  DeepSeek失败（第{attempt+1}次）: {e}")
            time.sleep(3)
    return "AI分析暂时不可用。"


def build_tech_summary(stock: dict) -> str:
    tech = stock.get("tech", {})
    if not tech:
        return "技术数据不足"
    parts = []
    parts.append("均线多头✓" if tech.get("ma_bullish") else "均线未多头")
    parts.append("MACD多头✓" if tech.get("macd_bullish") else "MACD偏弱")
    parts.append("量能放大✓" if tech.get("vol_expanding") else "量能一般")
    bp = tech.get("bb_position", 0.5)
    parts.append("超卖区" if bp < 0.3 else ("超买区" if bp > 0.8 else "布林中位"))
    parts.append(f"近20日{'+' if tech.get('ret_20d',0)>0 else ''}{tech.get('ret_20d',0)}%")
    return "、".join(parts) + f"（评分{stock.get('tech_score',0)}/10）"


def ai_closing_analysis(policy_news, stocks, market_sentiment,
                         northbound, moneyflow_rank, dragon_tiger,
                         block_trade, margin, inst_survey, sector_flow,
                         forecast=None, broker_rec=None) -> str:
    policy_text = "\n".join(policy_news) if policy_news else "今日暂无政策信号"

    # 数据完整性检查
    has_stocks  = len(stocks) > 0
    has_north   = northbound.get("total", 0) != 0
    has_mf      = len(moneyflow_rank) > 0
    has_dt      = len(dragon_tiger) > 0
    has_sector  = len(sector_flow) > 0
    data_score  = sum([has_stocks, has_north, has_mf, has_dt, has_sector])

    if data_score == 0:
        return "⚠️ 今日数据均未获取，可能是收盘前运行或接口暂时异常。建议等待18:00后自动报告。"

    missing = []
    if not has_stocks:  missing.append("技术面候选标的")
    if not has_north:   missing.append("北向资金")
    if not has_mf:      missing.append("主力资金流向")
    if not has_dt:      missing.append("龙虎榜")
    if not has_sector:  missing.append("行业资金流向")
    sep = " / "
    warning = (f"⚠️ 数据缺失：{sep.join(missing)}。缺失维度不得编造，直接标注【数据缺失】。\n\n" if missing else "")

    ms = market_sentiment
    sentiment_text = (
        f"今日市场情绪：{ms.get('sentiment','未知')}，"
        f"涨{ms.get('up',0)}家/跌{ms.get('down',0)}家，"
        f"涨停{ms.get('limit_up',0)}家/跌停{ms.get('limit_down',0)}家"
    )

    north_text = f"北向资金今日净流入：{northbound.get('total',0)}亿（沪{northbound.get('sh',0)}亿+深{northbound.get('sz',0)}亿）"
    if northbound.get("top_stocks"):
        north_text += "\n北向重仓：" + "、".join(
            f"{s.get('name','')}({round(safe_float(s.get('net_amount',0))/1e8,1)}亿)"
            for s in northbound["top_stocks"][:5]
        )

    mf_text = "\n".join(
        f"- {s.get('name', s['code'])}（{s['code']}）：大单净流入{s['net_flow_yi']}亿"
        for s in moneyflow_rank[:8]
    ) or "暂无数据"

    dt_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['signal']}"
        for s in dragon_tiger
    ) or "今日暂无机构龙虎榜"

    block_text = "\n".join(
        f"- {s['name']}：成交{s['amount_wan']}万，折价{s['discount_rate']}%"
        for s in block_trade
    ) or "今日暂无折价大宗"

    sector_text = "\n".join(
        f"- {s['sector']}：净流入{s['net_flow_yi']}亿"
        for s in sector_flow
    ) or "暂无数据"

    inst_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['fund_nums']}家机构调研（{s['date']}）"
        for s in inst_survey
    ) or "近7日暂无机构调研数据"

    stocks_text = "\n".join(
        f"- {s['name']}（{s['code']}）[{s['industry']}]：收涨{s['change_pct']}%，"
        f"市值{s['market_cap_yi']}亿，换手{s['turnover_rate']}%，量比{s['volume_ratio']}，"
        f"PE{s['pe']}，技术面：{build_tech_summary(s)}"
        for s in stocks
    ) or "今日暂无符合条件标的"

    # 业绩预测文字
    forecast_text = "\n".join(
        f"- {s['code']}：净利润预增{s['pct_min']}%～{s['pct_max']}%（{s['ann_date']}披露）"
        for s in (forecast or [])
    ) or "今日暂无高增长业绩预告"

    # 券商金股文字
    broker_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['broker_count']}家券商推荐（{s['brokers']}）"
        for s in (broker_rec or [])
    ) or "暂无本月券商金股数据"

    prompt = f"""今天是{TODAY_CN}，收盘后复盘。

{warning}
【政策信号（多部委来源）】
{policy_text}

【市场整体情绪】
{sentiment_text}

【行业资金流向TOP8（同花顺行业）】
{sector_text}

【北向资金（收盘完整数据）】
{north_text}

【主力大单净流入排行（特大单+大单）】
{mf_text}

【龙虎榜机构席位】
{dt_text}

【大宗交易折价（主力建仓信号）】
{block_text}

【机构调研热点（近7日）】
{inst_text}

【业绩预测（净利润预增≥50%，预期差机会）】
{forecast_text}

【券商金股（本月机构重点推荐）】
{broker_text}

【技术面精筛标的（均线+MACD+KDJ+RSI+布林，评分≥5/12）】
{stocks_text}

请输出收盘复盘报告：

**一、今日市场总结**
- 市场情绪和资金主线判断（基于真实数据）
- 行业板块轮动方向
- 明日市场情绪预判

**二、明日重点关注标的**
综合技术面+资金面+政策面，筛选3-5只，每只给出：
- 入选维度（说明技术面/资金面/政策面哪些支撑）
- 技术结构评价（均线/MACD/量能/布林位置）
- 关键价位（支撑位/压力位）
- 综合评分（1-10分，严格按数据支撑度）
- 明日操作建议

**三、明日操作策略**
- 首选标的（1只，最高确定性）
- 备选标的（1-2只）
- 止损设定原则
- 需要回避的方向

直接给结论，不得编造数据缺失的维度。"""

    print("  AI收盘复盘分析...")
    return ask_deepseek(prompt, max_tokens=2500)


def ai_intraday_analysis(policy_news, market_sentiment, sector_flow, northbound) -> str:
    policy_text   = "\n".join(policy_news) if policy_news else "今日暂无政策信号"
    ms            = market_sentiment
    sentiment_text = (
        f"市场情绪：{ms.get('sentiment','未知')}，"
        f"涨{ms.get('up',0)}家/跌{ms.get('down',0)}家，"
        f"涨停{ms.get('limit_up',0)}家"
    ) if ms.get("up", 0) > 0 else "盘中实时情绪数据待更新"
    sector_text = "\n".join(
        f"- {s['sector']}：净流入{s['net_flow_yi']}亿"
        for s in sector_flow
    ) or "暂无板块数据"
    north_text = f"北向资金：{northbound.get('total',0)}亿"

    prompt = f"""今天是{TODAY_CN}，现在是盘中时段。

【政策信号】
{policy_text}

【市场情绪】
{sentiment_text}

【行业资金流向】
{sector_text}

【北向资金】
{north_text}

请输出盘中分析报告：

**一、今日市场情绪判断**
- 整体情绪（强势/震荡/弱势）及判断依据
- 今日资金追涨的板块方向
- 北向资金态度

**二、今日操作建议**
- 可关注的板块方向（不超过2个）
- 需要回避的操作
- 下午需要重点观察的变化

**三、明日预判**
- 基于今日政策和资金走向，预判明日市场方向

语言简洁，直接给结论。"""

    print("  AI盘中分析...")
    return ask_deepseek(prompt, max_tokens=1500)


def ai_deep_analysis(stocks: list) -> str:
    if not stocks:
        return ""
    names = "、".join(f"{s['name']}（{s['code']}）" for s in stocks[:5])
    prompt = f"""请对以下A股标的做"打仗视角"深度周度分析：{names}

每只股票分析：
1. 国家战略层：是否在国家要打仗的名单？政策落地哪个阶段？
2. 行业竞争层：核心壁垒在哪个环节？公司在该环节的位置？
3. 公司质地层：近3年业绩趋势？是否切入核心供应链？
4. 技术面：当前形态适合建仓还是等待回调？
5. 综合评分（0-10）及一句话投资逻辑

最后给出本周最值得建仓的排序（第一到第三名）。"""
    print("  AI周末深度分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ══════════════════════════════════════════════════════════
#  模块五：邮件发送
# ══════════════════════════════════════════════════════════

def get_smtp_config(email: str):
    domain = email.split("@")[-1].lower()
    return {
        "qq.com":      ("smtp.qq.com",          465, True),
        "foxmail.com": ("smtp.qq.com",          465, True),
        "163.com":     ("smtp.163.com",         465, True),
        "126.com":     ("smtp.126.com",         465, True),
        "gmail.com":   ("smtp.gmail.com",       587, False),
        "outlook.com": ("smtp.office365.com",   587, False),
        "me.com":      ("smtp.mail.me.com",     587, False),
        "icloud.com":  ("smtp.mail.me.com",     587, False),
    }.get(domain, ("smtp.qq.com", 465, True))


def md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"#{1,4}\s*(.+)", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")


def build_html(title, mode, ai_report, deep_report, stocks,
               market_sentiment, northbound, moneyflow_rank,
               dragon_tiger, block_trade, sector_flow,
               forecast=None, broker_rec=None) -> str:
    mode_color = "#0984e3" if mode == "盘中" else "#6c5ce7"

    # 技术面表格
    stock_rows = ""
    for s in stocks[:12]:
        tech = s.get("tech", {})
        ma_t   = "✓" if tech.get("ma_bullish")   else "—"
        macd_t = "✓" if tech.get("macd_bullish")  else "—"
        vol_t  = "✓" if tech.get("vol_expanding") else "—"
        score  = s.get("tech_score", "—")
        stock_rows += (
            f"<tr>"
            f"<td style='padding:6px 8px'>{s['name']}</td>"
            f"<td style='padding:6px 8px;color:#888;font-size:11px'>{s['code']}</td>"
            f"<td style='padding:6px 8px;color:#888;font-size:11px'>{s.get('industry','')}</td>"
            f"<td style='padding:6px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['change_pct']}%</td>"
            f"<td style='padding:6px 8px;text-align:right'>{s['market_cap_yi']}亿</td>"
            f"<td style='padding:6px 8px;text-align:right'>{s['turnover_rate']}%</td>"
            f"<td style='padding:6px 8px;text-align:right'>{s['volume_ratio']}</td>"
            f"<td style='padding:6px 8px;text-align:right'>{s.get('pe','—')}</td>"
            f"<td style='padding:6px 8px;text-align:center'>{ma_t}</td>"
            f"<td style='padding:6px 8px;text-align:center'>{macd_t}</td>"
            f"<td style='padding:6px 8px;text-align:center'>{vol_t}</td>"
            f"<td style='padding:6px 8px;text-align:center;font-weight:500;color:{mode_color}'>{score}</td>"
            f"</tr>"
        )

    # 市场情绪卡片
    ms = market_sentiment
    sc = {"强势偏多":"#00b894","温和偏多":"#55efc4","中性震荡":"#888",
          "温和偏空":"#e17055","弱势偏空":"#d63031"}.get(ms.get("sentiment",""), "#888")
    sentiment_cards = f"""
    <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:16px">
      <div style="background:var(--color-background-secondary);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:11px;color:var(--color-text-secondary)">市场情绪</div>
        <div style="font-size:14px;font-weight:500;color:{sc}">{ms.get('sentiment','—')}</div>
      </div>
      <div style="background:var(--color-background-secondary);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:11px;color:var(--color-text-secondary)">上涨</div>
        <div style="font-size:16px;font-weight:500;color:#00b894">{ms.get('up',0)}</div>
      </div>
      <div style="background:var(--color-background-secondary);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:11px;color:var(--color-text-secondary)">下跌</div>
        <div style="font-size:16px;font-weight:500;color:#d63031">{ms.get('down',0)}</div>
      </div>
      <div style="background:var(--color-background-secondary);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:11px;color:var(--color-text-secondary)">涨停</div>
        <div style="font-size:16px;font-weight:500;color:#d63031">{ms.get('limit_up',0)}</div>
      </div>
      <div style="background:var(--color-background-secondary);border-radius:8px;padding:10px;text-align:center">
        <div style="font-size:11px;color:var(--color-text-secondary)">跌停</div>
        <div style="font-size:16px;font-weight:500;color:#00b894">{ms.get('limit_down',0)}</div>
      </div>
    </div>"""

    # 北向资金
    north_color = "#d63031" if northbound.get("total",0) > 0 else "#00b894"
    north_val   = f"{'+' if northbound.get('total',0)>0 else ''}{northbound.get('total',0)}亿"
    north_top   = "、".join(s.get("name","") for s in northbound.get("top_stocks",[])[:5]) or "暂无"

    # 主力资金流向
    mf_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s.get('name',s['code'])}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in moneyflow_rank[:6]
    ) or "<tr><td colspan='2' style='padding:8px;color:#aaa;text-align:center;font-size:12px'>暂无数据</td></tr>"

    # 龙虎榜
    dt_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['name']}</td>"
        f"<td style='padding:5px 8px;color:#6c5ce7;font-size:11px'>{s['signal']}</td></tr>"
        for s in dragon_tiger[:5]
    ) or "<tr><td colspan='2' style='padding:8px;color:#aaa;text-align:center;font-size:12px'>暂无数据</td></tr>"

    # 板块资金
    sector_rows = "".join(
        f"<tr><td style='padding:5px 8px'>{s['sector']}</td>"
        f"<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+{s['net_flow_yi']}亿</td></tr>"
        for s in sector_flow[:6]
    ) or "<tr><td colspan='2' style='padding:8px;color:#aaa;text-align:center;font-size:12px'>暂无数据</td></tr>"

    deep_section = f"""
    <div style="background:#f4f0ff;border-left:4px solid #6c5ce7;padding:16px 20px;margin:20px 0;border-radius:0 8px 8px 0">
      <h2 style="color:#6c5ce7;margin:0 0 10px;font-size:15px">📊 本周深度分析（打仗视角）</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(deep_report)}</div>
    </div>""" if deep_report else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:760px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,{mode_color},{mode_color}cc);padding:22px 28px;color:#fff">
    <div style="font-size:18px;font-weight:600">{title}</div>
    <div style="font-size:12px;opacity:0.85;margin-top:4px">
      {TODAY_CN} · v6.0 · 数据源：Tushare Pro 6000积分全接口
    </div>
  </div>

  <div style="padding:22px 28px">

    <div style="background:#f8faff;border-left:4px solid {mode_color};padding:16px 20px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <h2 style="color:{mode_color};margin:0 0 10px;font-size:15px">{'📊 盘中实时报告' if mode=='盘中' else '📊 收盘复盘报告'}</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    {deep_section}

    <h2 style="font-size:14px;color:#2d3436;margin:0 0 10px">📊 今日市场情绪</h2>
    {sentiment_cards}

    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px">
      <div style="background:#fff5f5;border-radius:8px;padding:12px 14px;border:0.5px solid #fab1a0">
        <div style="font-size:11px;color:#888;margin-bottom:4px">北向资金今日</div>
        <div style="font-size:20px;font-weight:600;color:{north_color}">{north_val}</div>
        <div style="font-size:10px;color:#aaa;margin-top:4px">{north_top}</div>
      </div>
      <div style="background:#f0fff4;border-radius:8px;padding:12px 14px;border:0.5px solid #55efc4">
        <div style="font-size:11px;color:#888;margin-bottom:6px">龙虎榜机构席位</div>
        <table style="width:100%;font-size:12px">{dt_rows}</table>
      </div>
      <div style="background:#f0f7ff;border-radius:8px;padding:12px 14px;border:0.5px solid #74b9ff">
        <div style="font-size:11px;color:#888;margin-bottom:6px">行业资金流向TOP6</div>
        <table style="width:100%;font-size:12px">{sector_rows}</table>
      </div>
    </div>

    <!-- 业绩预测 & 券商金股 -->
    '''
    extra_sections = ""
    if forecast:
        fc_list = forecast[:5] if forecast else []
        fc_rows = "".join(
            "<tr><td style='padding:5px 8px'>" + str(item.get("name", item.get("code",""))) + "</td>"
            "<td style='padding:5px 8px;text-align:right;color:#d63031;font-weight:500'>+" + str(item.get("pct_max","")) + "%</td>"
            "<td style='padding:5px 8px;color:#888;font-size:11px'>" + str(item.get("ann_date","")) + "</td></tr>"
            for item in fc_list
        )
        extra_sections += (
            "<div style='margin-bottom:16px'>"
            "<h2 style='font-size:14px;color:#2d3436;margin:0 0 8px'>业绩预增标的（净利润增速50%以上）</h2>"
            "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
            "<thead><tr style='background:#f8f9fa;color:#636e72'>"
            "<th style='padding:6px 8px;text-align:left;font-weight:400'>股票</th>"
            "<th style='padding:6px 8px;text-align:right;font-weight:400'>预增上限</th>"
            "<th style='padding:6px 8px;text-align:right;font-weight:400'>披露日期</th>"
            f"</tr></thead><tbody>{fc_rows}</tbody></table></div>"
        )
    if broker_rec:
        br_list = broker_rec[:5] if broker_rec else []
        br_rows = "".join(
            "<tr><td style='padding:5px 8px'>" + str(item.get("name","")) + "</td>"
            "<td style='padding:5px 8px;text-align:center;color:#6c5ce7;font-weight:500'>" + str(item.get("broker_count","")) + "家</td>"
            "<td style='padding:5px 8px;color:#888;font-size:11px'>" + str(item.get("brokers","")) + "</td></tr>"
            for item in br_list
        )
        extra_sections += (
            "<div style='margin-bottom:16px'>"
            "<h2 style='font-size:14px;color:#2d3436;margin:0 0 8px'>券商金股（本月机构重点推荐）</h2>"
            "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
            "<thead><tr style='background:#f8f9fa;color:#636e72'>"
            "<th style='padding:6px 8px;text-align:left;font-weight:400'>股票</th>"
            "<th style='padding:6px 8px;text-align:center;font-weight:400'>推荐家数</th>"
            "<th style='padding:6px 8px;text-align:left;font-weight:400'>推荐券商</th>"
            f"</tr></thead><tbody>{br_rows}</tbody></table></div>"
        )

    <h2 style="font-size:14px;color:#2d3436;margin:0 0 8px">💰 主力大单净流入 TOP10</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:6px 8px;text-align:left;font-weight:400">股票</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">净流入</th>
      </tr></thead>
      <tbody>{mf_rows}</tbody>
    </table>

    {extra_sections}

    <h2 style="font-size:14px;color:#2d3436;margin:0 0 8px">📈 技术面精筛标的</h2>
    <div style="overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;font-size:11px;min-width:600px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:6px 8px;text-align:left;font-weight:400">股票</th>
        <th style="padding:6px 8px;text-align:left;font-weight:400">代码</th>
        <th style="padding:6px 8px;text-align:left;font-weight:400">行业</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">涨幅</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">市值</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">换手</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">量比</th>
        <th style="padding:6px 8px;text-align:right;font-weight:400">PE</th>
        <th style="padding:6px 8px;text-align:center;font-weight:400">均线</th>
        <th style="padding:6px 8px;text-align:center;font-weight:400">MACD</th>
        <th style="padding:6px 8px;text-align:center;font-weight:400">量能</th>
        <th style="padding:6px 8px;text-align:center;font-weight:400">评分</th>
      </tr></thead>
      <tbody>{stock_rows if stock_rows else "<tr><td colspan='12' style='padding:12px;text-align:center;color:#aaa'>今日暂无符合条件标的</td></tr>"}</tbody>
    </table>
    </div>
    <div style="font-size:10px;color:#aaa;margin-top:4px">✓=达标 —=未达标 | 评分满分10分（均线3+MACD3+量能2+布林1+涨幅1）</div>

  </div>

  <div style="padding:12px 28px;background:#f8f9fa;color:#aaa;font-size:11px;text-align:center;border-top:1px solid #eee">
    本报告由 AI 自动生成，数据源：Tushare Pro，不构成投资建议。投资有风险，决策需谨慎。
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
        print(f"  邮件发送成功！收件人：{', '.join(EMAIL_RECEIVERS)}")
    except Exception as e:
        print(f"  邮件发送失败: {e}")
        raise


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def main():
    # ── 政策新闻（所有模式共用）──
    policy_news = fetch_policy_news()

    # ── 行情数据（Tushare）──
    daily_basic = get_daily_basic()
    daily_price = get_daily_price()
    names       = get_stock_names()

    # ── 市场情绪（从行情数据计算）──
    market_sentiment = get_market_sentiment(daily_price)

    # ── 资金数据（Tushare全接口）──
    northbound    = get_northbound()
    moneyflow     = get_moneyflow_rank()
    dragon_tiger  = get_dragon_tiger()
    block_trade   = get_block_trade()
    margin        = get_margin_data()
    inst_survey   = get_inst_survey()
    sector_flow   = get_sector_moneyflow()

    if IS_INTRADAY:
        # ══ 盘中模式：轻量分析 ══
        print("\n【盘中模式】政策+情绪+资金方向")
        ai_report   = ai_intraday_analysis(policy_news, market_sentiment, sector_flow, northbound)
        deep_report = ""
        stocks      = []
        prefix      = "【A股盘中】"
        subject     = f"{prefix} {TODAY_CN} · 实时异动报告"
    else:
        # ══ 收盘复盘模式：完整分析 ══
        print("\n【收盘复盘模式】全维度精筛")
        # 批量技术因子（6000积分，一次获取全市场）
        factor_df   = get_stk_factor_batch(TODAY)
        # 业绩预测 & 券商金股（6000积分）
        forecast    = get_forecast()
        broker_rec  = get_broker_recommend()

        candidates  = quant_filter_closing(daily_basic, daily_price, names)
        stocks      = tech_filter(candidates, factor_df)
        ai_report   = ai_closing_analysis(
            policy_news, stocks, market_sentiment,
            northbound, moneyflow, dragon_tiger,
            block_trade, margin, inst_survey, sector_flow,
            forecast, broker_rec
        )
        deep_report = ""
        if IS_WEEKEND and stocks:
            deep_report = ai_deep_analysis(stocks[:5])
        prefix  = "【A股周报】" if IS_WEEKEND else "【A股收盘】"
        subject = f"{prefix} {TODAY_CN} · 复盘选股报告"

    # ── 发送报告 ──
    html = build_html(
        subject, MODE, ai_report, deep_report, stocks,
        market_sentiment, northbound, moneyflow,
        dragon_tiger, block_trade, sector_flow,
        forecast if not IS_INTRADAY else None,
        broker_rec if not IS_INTRADAY else None
    )
    print("\n发送邮件...")
    send_email(subject, html)

    os.makedirs("reports", exist_ok=True)
    path = f"reports/{TODAY}_{MODE}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ 完成，报告已保存：{path}")


if __name__ == "__main__":
    main()
