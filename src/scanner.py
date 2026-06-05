
"""
A股智能选股系统 v3.0
盘中版（10:30）：实时异动筛选
收盘复盘版（15:30）：K线技术分析 + 完整资金数据
AI引擎：DeepSeek   推送：邮件
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

import akshare as ak
import tushare as ts
import pandas as pd
import numpy as np
from openai import OpenAI

# ─── 初始化 ────────────────────────────────────────────────
deepseek = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)
ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

EMAIL_SENDER   = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVER = os.environ["EMAIL_RECEIVER"]

# 使用北京时间（UTC+8）
_beijing_now = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
TODAY    = _beijing_now.strftime("%Y%m%d")
TODAY_CN = _beijing_now.strftime("%Y年%m月%d日")
WEEKDAY  = _beijing_now.weekday()
# GitHub Actions 服务器使用 UTC 时间
# 北京时间 = UTC + 8
# 盘中模式：北京时间 09:30-13:00 = UTC 01:30-05:00
# 收盘复盘：北京时间 15:00后    = UTC 07:00后
NOW_HOUR    = datetime.datetime.utcnow().hour
NOW_MINUTE  = datetime.datetime.utcnow().minute
IS_INTRADAY = (1 <= NOW_HOUR < 5) or (NOW_HOUR == 1 and NOW_MINUTE >= 30)
IS_WEEKEND  = WEEKDAY >= 5
MODE        = "盘中" if IS_INTRADAY else "收盘复盘"

POLICY_KEYWORDS = [
    "政策","国务院","发改委","工信部","财政部","战略","支持",
    "算力","半导体","新能源","军工","生物","机器人","低空",
    "储能","补贴","专项债","产业基金","规划","攻关","突破",
    "卡脖子","自主可控","国产替代","先进制造","数字经济"
]

print(f"\n{'='*55}")
print(f"A股智能选股系统 v3.0 — {MODE}模式")
print(f"运行时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
print(f"{'='*55}\n")


# ══════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════

def safe_float(val, default=0.0):
    try:
        return float(val)
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
            print(f"DeepSeek调用失败（第{attempt+1}次）: {e}")
            time.sleep(3)
    return "AI分析暂时不可用。"


# ══════════════════════════════════════════════════════════
#  模块一：政策情报（共用）
# ══════════════════════════════════════════════════════════

def fetch_policy_news() -> list:
    print("【政策情报】抓取多源新闻...")
    result = []

    # 财联社
    try:
        df = ak.stock_telegraph_cls()
        for _, row in df.head(50).iterrows():
            content = str(row.get("content", ""))
            if any(k in content for k in POLICY_KEYWORDS):
                result.append(f"[财联社] {content[:180]}")
    except Exception as e:
        print(f"  财联社失败: {e}")

    # 新华社RSS
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
                    result.append(f"[新华社] {title[:120]}")
        except Exception as e:
            print(f"  新华社失败: {e}")

    # 政府网站
    for src in [
        {"name": "国务院", "url": "https://www.gov.cn/govweb/zhengce/zuixin/"},
        {"name": "发改委", "url": "https://www.ndrc.gov.cn/xwdt/xwfb/"},
        {"name": "工信部", "url": "https://www.miit.gov.cn/jgsj/index.html"},
    ]:
        try:
            resp = requests.get(src["url"], timeout=8,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.encoding = "utf-8"
            titles = re.findall(r'<a[^>]*href[^>]*>([^<]{10,60})</a>', resp.text)
            for t in titles[:30]:
                t = t.strip()
                if any(k in t for k in POLICY_KEYWORDS):
                    result.append(f"[{src['name']}] {t[:100]}")
        except Exception as e:
            print(f"  {src['name']}失败: {e}")
        time.sleep(0.3)

    print(f"  共获取 {len(result)} 条政策新闻")
    return result[:20]


# ══════════════════════════════════════════════════════════
#  模块二A：盘中实时筛选
# ══════════════════════════════════════════════════════════

def intraday_quant_filter() -> list:
    """盘中：量价异动筛选（涨幅+量比+换手率）"""
    print("【盘中筛选】实时量价异动...")
    try:
        df = ak.stock_zh_a_spot_em()
        for col in ["总市值","涨跌幅","换手率","量比"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[
            (df["总市值"] >= 5e9)  &
            (df["总市值"] <= 5e10) &
            (df["涨跌幅"] >= 2.0)  &
            (df["涨跌幅"] <= 9.5)  &
            (df["换手率"] >= 1.0)  &
            (df["量比"]   >= 1.5)
        ].nlargest(30, "量比")

        result = []
        for _, row in df.iterrows():
            result.append({
                "code":          row.get("代码", ""),
                "name":          row.get("名称", ""),
                "price":         safe_float(row.get("最新价")),
                "change_pct":    round(safe_float(row.get("涨跌幅")), 2),
                "market_cap_yi": round(safe_float(row.get("总市值")) / 1e8, 1),
                "turnover_rate": round(safe_float(row.get("换手率")), 2),
                "volume_ratio":  round(safe_float(row.get("量比")), 2),
            })
        print(f"  量价异动标的：{len(result)} 只")
        return result[:20]
    except Exception as e:
        print(f"  盘中筛选失败: {e}")
        return []


def intraday_capital_flow() -> list:
    """盘中：实时主力资金净流入排行"""
    print("【盘中资金】实时主力流向...")
    result = []
    try:
        df = ak.stock_fund_flow_rank(indicator="今日")
        df["主力净流入-净额"] = pd.to_numeric(df["主力净流入-净额"], errors="coerce")
        df = df.nlargest(15, "主力净流入-净额")
        for _, row in df.iterrows():
            net = safe_float(row.get("主力净流入-净额"))
            if net > 0:
                result.append({
                    "name":        row.get("名称", ""),
                    "code":        row.get("代码", ""),
                    "net_flow_yi": round(net / 1e8, 2),
                    "change_pct":  row.get("今日涨跌幅", 0),
                })
        print(f"  主力净流入标的：{len(result)} 只")
    except Exception as e:
        print(f"  盘中资金失败: {e}")
    return result[:10]


# ══════════════════════════════════════════════════════════
#  模块二B：收盘技术面分析
# ══════════════════════════════════════════════════════════

def calc_technical_indicators(code: str) -> dict:
    """计算单只股票的技术指标：均线、MACD、布林带"""
    result = {}
    try:
        # 拉取近90日K线
        df = ak.stock_zh_a_hist(
            symbol=code, period="daily",
            start_date=(datetime.date.today() - datetime.timedelta(days=90)).strftime("%Y%m%d"),
            end_date=TODAY, adjust="qfq"
        )
        if df is None or len(df) < 30:
            return result

        close = df["收盘"].astype(float)
        vol   = df["成交量"].astype(float)

        # 均线
        ma5  = close.rolling(5).mean().iloc[-1]
        ma10 = close.rolling(10).mean().iloc[-1]
        ma20 = close.rolling(20).mean().iloc[-1]
        ma60 = close.rolling(60).mean().iloc[-1] if len(close) >= 60 else None
        last_close = close.iloc[-1]

        # 均线多头排列判断
        ma_bullish = (last_close > ma5 > ma10 > ma20)

        # MACD（EMA12/26/9）
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        dif   = ema12 - ema26
        dea   = dif.ewm(span=9).mean()
        macd_bar = (dif - dea) * 2
        macd_bullish = (dif.iloc[-1] > dea.iloc[-1]) and (dif.iloc[-1] > 0)

        # 成交量趋势（近5日均量 vs 近20日均量）
        vol_ratio_5_20 = vol.rolling(5).mean().iloc[-1] / vol.rolling(20).mean().iloc[-1]
        vol_expanding  = vol_ratio_5_20 > 1.1  # 近期量能放大

        # 布林带
        bb_mid  = close.rolling(20).mean()
        bb_std  = close.rolling(20).std()
        bb_up   = (bb_mid + 2 * bb_std).iloc[-1]
        bb_low  = (bb_mid - 2 * bb_std).iloc[-1]
        bb_pos  = (last_close - bb_low) / (bb_up - bb_low) if (bb_up - bb_low) > 0 else 0.5

        result = {
            "ma5":          round(ma5, 2),
            "ma10":         round(ma10, 2),
            "ma20":         round(ma20, 2),
            "ma60":         round(ma60, 2) if ma60 else None,
            "ma_bullish":   ma_bullish,
            "dif":          round(dif.iloc[-1], 4),
            "dea":          round(dea.iloc[-1], 4),
            "macd_bullish": macd_bullish,
            "vol_expanding":vol_expanding,
            "bb_position":  round(bb_pos, 2),  # 0=下轨 1=上轨
        }
    except Exception as e:
        print(f"    技术指标计算失败({code}): {e}")
    return result


def closing_quant_filter() -> list:
    """收盘：先用基础行情初筛，再计算技术指标精筛"""
    print("【收盘筛选】技术面精筛...")
    try:
        # 第一步：基础条件初筛
        df = ak.stock_zh_a_spot_em()
        for col in ["总市值","涨跌幅","换手率","量比"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[
            (df["总市值"] >= 5e9)   &
            (df["总市值"] <= 5e10)  &
            (df["涨跌幅"] >= 1.5)   &   # 收盘版降低门槛到1.5%
            (df["涨跌幅"] <= 9.5)   &
            (df["换手率"] >= 1.0)   &
            (df["换手率"] <= 15.0)  &   # 排除过热换手
            (df["量比"]   >= 1.2)        # 收盘版降低量比门槛
        ].nlargest(40, "量比")           # 先取40只做技术分析

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                "code":          str(row.get("代码", "")),
                "name":          row.get("名称", ""),
                "price":         safe_float(row.get("最新价")),
                "change_pct":    round(safe_float(row.get("涨跌幅")), 2),
                "market_cap_yi": round(safe_float(row.get("总市值")) / 1e8, 1),
                "turnover_rate": round(safe_float(row.get("换手率")), 2),
                "volume_ratio":  round(safe_float(row.get("量比")), 2),
            })

        print(f"  基础初筛：{len(candidates)} 只，开始计算技术指标...")

        # 第二步：计算技术指标，精筛
        qualified = []
        for stock in candidates:
            tech = calc_technical_indicators(stock["code"])
            stock["tech"] = tech
            time.sleep(0.3)  # 避免请求过快

            # 技术面评分
            score = 0
            if tech.get("ma_bullish"):    score += 3   # 均线多头排列
            if tech.get("macd_bullish"):  score += 3   # MACD多头
            if tech.get("vol_expanding"): score += 2   # 量能放大
            bb_pos = tech.get("bb_position", 0.5)
            if 0.3 < bb_pos < 0.8:        score += 1   # 布林带中位
            if stock["change_pct"] >= 3:  score += 1   # 涨幅有力度

            stock["tech_score"] = score
            if score >= 5:  # 技术面评分≥5才入围
                qualified.append(stock)

        qualified.sort(key=lambda x: x["tech_score"], reverse=True)
        print(f"  技术面精筛后：{len(qualified)} 只")
        return qualified[:15]

    except Exception as e:
        print(f"  收盘筛选失败: {e}")
        return []


# ══════════════════════════════════════════════════════════
#  模块三：Tushare Pro 资金数据（收盘后更完整）
# ══════════════════════════════════════════════════════════

def get_northbound_flow() -> dict:
    print("【资金】北向资金...")
    result = {"total": 0, "top_stocks": []}
    try:
        df = pro.moneyflow_hsgt(start_date=TODAY, end_date=TODAY)
        print(f"  北向原始数据行数：{len(df) if df is not None else 'None'}")
        if df is not None and len(df) > 0:
            print(f"  北向字段：{df.columns.tolist()}")
            print(f"  北向首行：{df.iloc[0].to_dict()}")
            result["total"] = round(safe_float(df.iloc[0].get("north_money")) / 1e8, 2)
        df_top = pro.hsgt_top10(trade_date=TODAY, market_type="N")
        print(f"  北向TOP10行数：{len(df_top) if df_top is not None else 'None'}")
        if df_top is not None and len(df_top) > 0:
            result["top_stocks"] = df_top[["name","net_amount"]].head(10).to_dict("records")
        print(f"  北向资金：{result['total']} 亿")
    except Exception as e:
        print(f"  北向资金失败: {e}")
    return result


def get_dragon_tiger() -> list:
    print("【资金】龙虎榜机构席位...")
    result = []
    try:
        df      = pro.top_list(trade_date=TODAY)
        df_inst = pro.top_inst(trade_date=TODAY)
        print(f"  龙虎榜行数：{len(df) if df is not None else 'None'}")
        print(f"  机构席位行数：{len(df_inst) if df_inst is not None else 'None'}")
        if df_inst is not None and len(df_inst) > 0:
            for code in df_inst["ts_code"].unique()[:10]:
                name_row = df[df["ts_code"] == code] if df is not None else pd.DataFrame()
                name = name_row["name"].values[0] if len(name_row) > 0 else code
                result.append({"code": code, "name": name, "signal": "机构席位买入"})
        print(f"  龙虎榜机构：{len(result)} 只")
    except Exception as e:
        print(f"  龙虎榜失败: {e}")
    return result[:8]


def get_block_trade() -> list:
    print("【资金】大宗交易折价...")
    result = []
    try:
        df = pro.block_trade(trade_date=TODAY)
        print(f"  大宗交易行数：{len(df) if df is not None else 'None'}")
        if df is not None and len(df) > 0:
            print(f"  大宗字段：{df.columns.tolist()}")
            df["discount_rate"] = pd.to_numeric(df["discount_rate"], errors="coerce")
            for _, row in df[df["discount_rate"] < -2].nsmallest(8, "discount_rate").iterrows():
                result.append({
                    "name":          row.get("name", ""),
                    "code":          row.get("ts_code", ""),
                    "amount_wan":    round(safe_float(row.get("amount")) / 1e4, 0),
                    "discount_rate": round(safe_float(row.get("discount_rate")), 2),
                })
        print(f"  大宗折价：{len(result)} 条")
    except Exception as e:
        print(f"  大宗交易失败: {e}")
    return result


def get_capital_flow_rank() -> list:
    print("【资金】主力资金净流入排行...")
    result = []
    try:
        indicator = "今日" if IS_INTRADAY else "今日"
        df = ak.stock_fund_flow_rank(indicator=indicator)
        df["主力净流入-净额"] = pd.to_numeric(df["主力净流入-净额"], errors="coerce")
        for _, row in df.nlargest(15, "主力净流入-净额").iterrows():
            net = safe_float(row.get("主力净流入-净额"))
            if net > 0:
                result.append({
                    "name":        row.get("名称", ""),
                    "code":        row.get("代码", ""),
                    "net_flow_yi": round(net / 1e8, 2),
                    "change_pct":  row.get("今日涨跌幅", 0),
                })
        print(f"  主力净流入：{len(result)} 条")
    except Exception as e:
        print(f"  主力资金失败: {e}")
    return result[:10]


# ══════════════════════════════════════════════════════════
#  模块四：AI 分析
# ══════════════════════════════════════════════════════════

def build_tech_summary(stock: dict) -> str:
    """把技术指标转成可读文字"""
    tech = stock.get("tech", {})
    if not tech:
        return "技术数据不足"
    parts = []
    parts.append("均线多头✓" if tech.get("ma_bullish") else "均线未多头")
    parts.append("MACD多头✓" if tech.get("macd_bullish") else "MACD偏弱")
    parts.append("量能放大✓" if tech.get("vol_expanding") else "量能一般")
    bb = tech.get("bb_position", 0.5)
    if bb < 0.3:
        parts.append("布林下轨（超卖区）")
    elif bb > 0.8:
        parts.append("布林上轨（注意压力）")
    else:
        parts.append("布林中位（健康）")
    return "、".join(parts) + f"（技术评分{stock.get('tech_score',0)}/10）"


def ai_intraday_analysis(policy_news, stocks, capital_flow, northbound) -> str:
    """盘中版AI分析"""
    policy_text  = "\n".join(policy_news) if policy_news else "今日暂无明显政策信号"
    north_text   = f"北向资金实时净流入：{northbound.get('total', 0)} 亿"
    stocks_text  = "\n".join(
        f"- {s['name']}（{s['code']}）：涨幅{s['change_pct']}%，"
        f"量比{s['volume_ratio']}，换手{s['turnover_rate']}%，市值{s['market_cap_yi']}亿"
        for s in stocks
    ) or "暂无符合条件的盘中异动标的"
    flow_text = "\n".join(
        f"- {s['name']}：主力净流入{s['net_flow_yi']}亿，涨幅{s['change_pct']}%"
        for s in capital_flow
    ) or "暂无数据"

    prompt = f"""今天是{TODAY_CN}，现在是盘中时段，请基于以下实时数据做A股盘中分析。

【政策信号】
{policy_text}

【北向资金】
{north_text}

【盘中量价异动标的（量比>1.5，涨幅2-9.5%）】
{stocks_text}

【主力资金净流入排行】
{flow_text}

请输出盘中分析报告：

**一、今日市场情绪判断**
- 整体情绪：强势/震荡/弱势
- 主线方向：今日资金在追什么板块
- 北向资金态度

**二、盘中重点异动标的**
从以上数据综合筛选3-5只，每只给出：
- 异动原因（政策催化/资金推动/技术突破）
- 量价结构评价
- 建议操作（可介入/观察等回调/回避）
- 风险提示

**三、今日操作建议**
- 可以关注介入的标的（不超过2只）
- 回避的方向
- 下午需要重点观察的变化

语言简洁，直接给结论。"""

    print("AI盘中分析...")
    return ask_deepseek(prompt, max_tokens=2000)


def ai_closing_analysis(policy_news, stocks, northbound,
                        dragon_tiger, block_trade, capital_flow) -> str:
    """收盘复盘版AI分析"""
    policy_text = "\n".join(policy_news) if policy_news else "今日暂无明显政策信号"

    # 数据完整性检查
    has_tech    = len(stocks) > 0
    has_north   = northbound.get("total", 0) != 0
    has_dt      = len(dragon_tiger) > 0
    has_flow    = len(capital_flow) > 0
    has_block   = len(block_trade) > 0
    data_score  = sum([has_tech, has_north, has_dt, has_flow, has_block])

    # 数据缺失时直接返回，不让AI乱编
    if data_score == 0:
        return """⚠️ 数据缺失提醒

今日技术面、资金面数据均未获取到，原因可能是：
1. 当前时间不在有效数据窗口内（建议在18:00-20:00之间运行）
2. 数据接口今日异常

本次报告无法生成有效的选股建议，请等待明日正常时间窗口的自动报告。

不建议基于本次报告做任何投资操作。"""

    north_text = f"北向资金今日净流入：{northbound.get('total', 0)} 亿\n"
    if northbound.get("top_stocks"):
        north_text += "北向重仓：" + "、".join(
            f"{s.get('name','')}({round(safe_float(s.get('net_amount',0))/1e8,1)}亿)"
            for s in northbound["top_stocks"][:5]
        )
    elif not has_north:
        north_text = "北向资金：今日数据未获取"

    dt_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['signal']}"
        for s in dragon_tiger
    ) or "今日龙虎榜数据未获取"

    block_text = "\n".join(
        f"- {s['name']}：成交{s['amount_wan']}万，折价{s['discount_rate']}%"
        for s in block_trade
    ) or "今日大宗交易数据未获取"

    flow_text = "\n".join(
        f"- {s['name']}：主力净流入{s['net_flow_yi']}亿，涨幅{s['change_pct']}%"
        for s in capital_flow
    ) or "今日主力资金数据未获取"

    stocks_text = "\n".join(
        f"- {s['name']}（{s['code']}）：收涨{s['change_pct']}%，"
        f"换手{s['turnover_rate']}%，量比{s['volume_ratio']}，"
        f"技术面：{build_tech_summary(s)}"
        for s in stocks
    ) or "今日技术面筛选无符合条件标的"

    # 数据完整性警告
    missing = []
    if not has_tech:   missing.append("技术面筛选标的")
    if not has_north:  missing.append("北向资金")
    if not has_dt:     missing.append("龙虎榜")
    if not has_flow:   missing.append("主力资金流向")
    if not has_block:  missing.append("大宗交易")
    sep = " / "
    warning = (f"⚠️ 以下数据今日未获取：{sep.join(missing)}\n缺失数据对应维度不得推断或假设，直接标注【数据缺失】。\n\n" if missing else "")

    prompt = f"""今天是{TODAY_CN}，收盘后复盘。

{warning}【重要规则——必须严格遵守】
1. 对于数据缺失的维度，必须直接写"数据缺失，无法评估"，禁止假设、推断或编造任何技术形态
2. 如果技术面和资金面数据同时缺失，该标的综合评分不得超过3分
3. 评分必须真实反映数据支撑程度，不得因为政策面好就给高分
4. 没有技术面确认的标的，操作建议只能写"等待技术面确认后再介入"

【政策信号】
{policy_text}

【北向资金】
{north_text}

【龙虎榜机构席位】
{dt_text}

【大宗交易折价】
{block_text}

【主力资金净流入排行】
{flow_text}

【技术面精筛标的】
{stocks_text}

请输出收盘复盘报告：

**一、今日市场总结**
- 今日板块轮动情况（仅基于真实数据）
- 资金主线方向（如数据缺失直接说明）
- 明日市场情绪预判

**二、明日重点关注标的**
只推荐有真实数据支撑的标的，每只注明：
- 有数据支撑的维度（技术面/资金面/政策面）
- 数据缺失的维度（直接标注"缺失"）
- 技术结构评价（无数据则写"数据缺失，待确认"）
- 关键价位（有数据则给出，无数据则写"待确认"）
- 综合评分（严格按数据支撑度打分）

**三、明日操作策略**
- 数据支撑充分的标的才列为首选
- 数据不足的只能列为"待观察"
- 明确说明本次报告的数据完整度

直接给判断，不编造任何数据。"""

    print("AI收盘复盘分析...")
    return ask_deepseek(prompt, max_tokens=2500)
def ai_deep_analysis(stocks: list) -> str:
    """周末深度分析"""
    if not stocks:
        return ""
    names = "、".join(s["name"] for s in stocks[:5])
    prompt = f"""请对以下A股标的做"打仗视角"深度周度分析：{names}

每只股票分析：
1. 国家战略层：是否在国家要打仗的名单？政策落地哪个阶段？
2. 行业竞争层：核心壁垒在哪个环节？公司在该环节的位置？
3. 公司质地层：近3年业绩趋势？是否切入核心供应链？
4. 技术面：当前技术形态适合建仓还是等待？
5. 综合评分（0-10）及一句话投资逻辑

最后给出本周最值得建仓的排序（第一到第三名）。"""
    print("AI周末深度分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ══════════════════════════════════════════════════════════
#  模块五：邮件发送
# ══════════════════════════════════════════════════════════

def get_smtp_config(email: str):
    domain = email.split("@")[-1].lower()
    return {
        "qq.com":      ("smtp.qq.com",            465, True),
        "foxmail.com": ("smtp.qq.com",            465, True),
        "163.com":     ("smtp.163.com",           465, True),
        "126.com":     ("smtp.126.com",           465, True),
        "gmail.com":   ("smtp.gmail.com",         587, False),
        "outlook.com": ("smtp.office365.com",     587, False),
        "me.com":      ("smtp.mail.me.com",       587, False),
        "icloud.com":  ("smtp.mail.me.com",       587, False),
    }.get(domain, ("smtp.qq.com", 465, True))


def md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"#{1,4}\s*(.+)", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")


def build_html(title, mode, ai_report, deep_report,
               stocks, northbound, dragon_tiger,
               capital_flow, block_trade) -> str:

    mode_color = "#0984e3" if mode == "盘中" else "#6c5ce7"
    mode_label = "📊 盘中实时报告" if mode == "盘中" else "📊 收盘复盘报告"

    stock_rows = ""
    for s in stocks[:12]:
        tech = s.get("tech", {})
        ma_tag   = "✓" if tech.get("ma_bullish")   else "—"
        macd_tag = "✓" if tech.get("macd_bullish")  else "—"
        vol_tag  = "✓" if tech.get("vol_expanding") else "—"
        score    = s.get("tech_score", "—")
        stock_rows += (
            f"<tr>"
            f"<td style='padding:7px 10px'>{s['name']}</td>"
            f"<td style='padding:7px 10px;color:#888'>{s['code']}</td>"
            f"<td style='padding:7px 10px;text-align:right;color:#d63031;font-weight:500'>+{s['change_pct']}%</td>"
            f"<td style='padding:7px 10px;text-align:right'>{s['market_cap_yi']}亿</td>"
            f"<td style='padding:7px 10px;text-align:right'>{s['turnover_rate']}%</td>"
            f"<td style='padding:7px 10px;text-align:right'>{s['volume_ratio']}</td>"
            f"<td style='padding:7px 10px;text-align:center'>{ma_tag}</td>"
            f"<td style='padding:7px 10px;text-align:center'>{macd_tag}</td>"
            f"<td style='padding:7px 10px;text-align:center'>{vol_tag}</td>"
            f"<td style='padding:7px 10px;text-align:center;font-weight:500;color:{mode_color}'>{score}</td>"
            f"</tr>"
        )

    north_color = "#d63031" if northbound.get("total", 0) > 0 else "#00b894"
    north_val   = f"{'+' if northbound.get('total',0)>0 else ''}{northbound.get('total',0)}亿"
    north_top   = "、".join(s.get("name","") for s in northbound.get("top_stocks",[])[:5]) or "暂无数据"

    dt_rows = "".join(
        f"<tr><td style='padding:6px 8px'>{s['name']}</td>"
        f"<td style='padding:6px 8px;color:#888;font-size:11px'>{s['code']}</td>"
        f"<td style='padding:6px 8px;color:#6c5ce7;font-size:11px'>{s['signal']}</td></tr>"
        for s in dragon_tiger[:5]
    ) or "<tr><td colspan='3' style='padding:10px;color:#aaa;text-align:center;font-size:12px'>今日暂无数据</td></tr>"

    flow_rows = "".join(
        f"<tr><td style='padding:6px 8px'>{s['name']}</td>"
        f"<td style='padding:6px 8px;color:#d63031;text-align:right;font-weight:500'>+{s['net_flow_yi']}亿</td>"
        f"<td style='padding:6px 8px;color:#888;text-align:right;font-size:11px'>{s['change_pct']}%</td></tr>"
        for s in capital_flow[:6]
    ) or "<tr><td colspan='3' style='padding:10px;color:#aaa;text-align:center;font-size:12px'>今日暂无数据</td></tr>"

    block_rows = "".join(
        f"<tr><td style='padding:6px 8px'>{s['name']}</td>"
        f"<td style='padding:6px 8px;text-align:right'>{s['amount_wan']}万</td>"
        f"<td style='padding:6px 8px;color:#00b894;text-align:right;font-weight:500'>{s['discount_rate']}%</td></tr>"
        for s in block_trade[:5]
    ) or "<tr><td colspan='3' style='padding:10px;color:#aaa;text-align:center;font-size:12px'>今日暂无折价大宗</td></tr>"

    deep_section = f"""
    <div style="background:#f4f0ff;border-left:4px solid #6c5ce7;padding:16px 20px;margin:20px 0;border-radius:0 8px 8px 0">
      <h2 style="color:#6c5ce7;margin:0 0 12px;font-size:15px">📊 本周深度分析（打仗视角）</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(deep_report)}</div>
    </div>""" if deep_report else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:720px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,{mode_color},{mode_color}cc);padding:22px 28px;color:#fff">
    <div style="font-size:18px;font-weight:600">{title}</div>
    <div style="font-size:12px;opacity:0.85;margin-top:4px">
      {TODAY_CN} · {mode_label} · 数据源：财联社+新华社+发改委+AKShare+Tushare Pro
    </div>
  </div>

  <div style="padding:22px 28px">

    <div style="background:#f8faff;border-left:4px solid {mode_color};padding:16px 20px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <h2 style="color:{mode_color};margin:0 0 10px;font-size:15px">{mode_label}</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(ai_report)}</div>
    </div>

    {deep_section}

    <h2 style="font-size:14px;color:#2d3436;margin:20px 0 10px">🧠 聪明钱动向</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:20px">

      <div style="background:#fff5f5;border-radius:8px;padding:12px 14px;border:0.5px solid #fab1a0">
        <div style="font-size:11px;color:#888;margin-bottom:4px">北向资金今日</div>
        <div style="font-size:20px;font-weight:600;color:{north_color}">{north_val}</div>
        <div style="font-size:10px;color:#aaa;margin-top:4px">{north_top}</div>
      </div>

      <div style="background:#f0fff4;border-radius:8px;padding:12px 14px;border:0.5px solid #55efc4">
        <div style="font-size:11px;color:#888;margin-bottom:6px">龙虎榜机构</div>
        <table style="width:100%;font-size:11px">{dt_rows}</table>
      </div>

      <div style="background:#fff9f0;border-radius:8px;padding:12px 14px;border:0.5px solid #ffd08a">
        <div style="font-size:11px;color:#888;margin-bottom:6px">大宗折价成交</div>
        <table style="width:100%;font-size:11px">
          <tr style="color:#aaa"><td>股票</td><td style="text-align:right">金额</td><td style="text-align:right">折价</td></tr>
          {block_rows}
        </table>
      </div>

    </div>

    <h2 style="font-size:14px;color:#2d3436;margin:0 0 8px">💰 主力资金净流入 TOP10</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:7px 10px;text-align:left;font-weight:500">股票</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">净流入</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">涨幅</th>
      </tr></thead>
      <tbody>{flow_rows}</tbody>
    </table>

    <h2 style="font-size:14px;color:#2d3436;margin:0 0 8px">📈 技术面筛选标的</h2>
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:7px 8px;text-align:left;font-weight:500">股票</th>
        <th style="padding:7px 8px;text-align:left;font-weight:500">代码</th>
        <th style="padding:7px 8px;text-align:right;font-weight:500">涨幅</th>
        <th style="padding:7px 8px;text-align:right;font-weight:500">市值</th>
        <th style="padding:7px 8px;text-align:right;font-weight:500">换手</th>
        <th style="padding:7px 8px;text-align:right;font-weight:500">量比</th>
        <th style="padding:7px 8px;text-align:center;font-weight:500">均线</th>
        <th style="padding:7px 8px;text-align:center;font-weight:500">MACD</th>
        <th style="padding:7px 8px;text-align:center;font-weight:500">量能</th>
        <th style="padding:7px 8px;text-align:center;font-weight:500">评分</th>
      </tr></thead>
      <tbody>{stock_rows}</tbody>
    </table>
    <div style="font-size:11px;color:#aaa;margin-top:6px">✓=达标 —=未达标 &nbsp;|&nbsp; 评分满分10分（均线3+MACD3+量能2+布林1+涨幅1）</div>

  </div>

  <div style="padding:12px 28px;background:#f8f9fa;color:#aaa;font-size:11px;text-align:center;border-top:1px solid #eee">
    本报告由 AI 自动生成，不构成投资建议。投资有风险，决策需谨慎。
  </div>
</div></body></html>"""


def send_email(subject: str, html: str):
    smtp_host, smtp_port, use_ssl = get_smtp_config(EMAIL_SENDER)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        server = (smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
                  if use_ssl else smtplib.SMTP(smtp_host, smtp_port, timeout=15))
        if not use_ssl:
            server.starttls()
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        server.sendmail(EMAIL_SENDER, EMAIL_RECEIVER, msg.as_string())
        server.quit()
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败: {e}")
        raise


# ══════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════

def main():
    # ── 共用：政策情报 ──
    policy_news  = fetch_policy_news()
    northbound   = get_northbound_flow()
    dragon_tiger = get_dragon_tiger()
    block_trade  = get_block_trade()
    capital_flow = get_capital_flow_rank()

    if IS_INTRADAY:
        # ══ 盘中模式 ══
        print("\n【盘中模式】实时异动筛选")
        stocks     = intraday_quant_filter()
        intra_flow = intraday_capital_flow()
        ai_report  = ai_intraday_analysis(policy_news, stocks, intra_flow, northbound)
        deep_report = ""
        prefix  = "【A股盘中】"
        subject = f"{prefix} {TODAY_CN} · 实时异动报告"

    else:
        # ══ 收盘复盘模式 ══
        print("\n【收盘复盘模式】技术面精筛")
        stocks      = closing_quant_filter()
        ai_report   = ai_closing_analysis(
            policy_news, stocks, northbound,
            dragon_tiger, block_trade, capital_flow
        )
        deep_report = ""
        if IS_WEEKEND and stocks:
            deep_report = ai_deep_analysis(stocks[:5])

        prefix  = "【A股周报】" if IS_WEEKEND else "【A股收盘】"
        subject = f"{prefix} {TODAY_CN} · 复盘选股报告"

    # ── 发送邮件 ──
    html = build_html(
        subject, MODE, ai_report, deep_report,
        stocks, northbound, dragon_tiger, capital_flow, block_trade
    )
    print("\n发送邮件...")
    send_email(subject, html)

    os.makedirs("reports", exist_ok=True)
    path = f"reports/{datetime.date.today().strftime('%Y%m%d')}_{MODE}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✓ 完成，报告已保存：{path}")


if __name__ == "__main__":
    main()
