"""
A股智能选股系统 v2.0
数据源：AKShare + Tushare Pro + 多政策网站
AI引擎：DeepSeek
推送：邮件
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
from openai import OpenAI

# ─── 初始化 ────────────────────────────────────────────────
deepseek = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)

# Tushare Pro
ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

EMAIL_SENDER   = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVER = os.environ["EMAIL_RECEIVER"]

TODAY      = datetime.date.today().strftime("%Y%m%d")
TODAY_CN   = datetime.date.today().strftime("%Y年%m月%d日")
WEEKDAY    = datetime.date.today().weekday()
IS_WEEKEND = WEEKDAY >= 5

POLICY_KEYWORDS = [
    "政策", "国务院", "发改委", "工信部", "财政部", "战略", "支持",
    "算力", "半导体", "新能源", "军工", "生物", "机器人", "低空",
    "储能", "补贴", "专项债", "产业基金", "规划", "攻关", "突破",
    "卡脖子", "自主可控", "国产替代", "先进制造", "数字经济"
]


# ══════════════════════════════════════════════════════════
#  第一模块：政策情报（多源）
# ══════════════════════════════════════════════════════════

def fetch_cls_news() -> list:
    """财联社快讯"""
    result = []
    try:
        df = ak.stock_telegraph_cls()
        for _, row in df.head(50).iterrows():
            content = str(row.get("content", ""))
            if any(k in content for k in POLICY_KEYWORDS):
                result.append(f"[财联社] {content[:180]}")
    except Exception as e:
        print(f"财联社获取失败: {e}")
    return result[:8]


def fetch_xinhua_news() -> list:
    """新华社RSS"""
    result = []
    feeds = [
        "http://www.xinhuanet.com/politics/news_politics.xml",
        "http://www.xinhuanet.com/fortune/news_fortune.xml",
    ]
    for url in feeds:
        try:
            resp = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
            root = ElementTree.fromstring(resp.content)
            for item in root.iter("item"):
                title = item.findtext("title", "")
                desc  = item.findtext("description", "")
                text  = title + desc
                if any(k in text for k in POLICY_KEYWORDS):
                    result.append(f"[新华社] {title[:120]}")
        except Exception as e:
            print(f"新华社RSS获取失败: {e}")
    return result[:6]


def fetch_gov_news() -> list:
    """国务院、发改委、工信部网站最新政策标题"""
    result = []
    sources = [
        {
            "name": "国务院",
            "url": "https://www.gov.cn/govweb/zhengce/zuixin/",
        },
        {
            "name": "发改委",
            "url": "https://www.ndrc.gov.cn/xwdt/xwfb/",
        },
        {
            "name": "工信部",
            "url": "https://www.miit.gov.cn/jgsj/index.html",
        },
    ]
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}
    for src in sources:
        try:
            resp = requests.get(src["url"], timeout=8, headers=headers)
            resp.encoding = "utf-8"
            # 简单提取页面中的标题文字
            text = resp.text
            # 用正则提取 <a> 标签内的中文标题
            titles = re.findall(r'<a[^>]*href[^>]*>([^<]{10,60})</a>', text)
            for title in titles[:30]:
                title = title.strip()
                if any(k in title for k in POLICY_KEYWORDS):
                    result.append(f"[{src['name']}] {title[:100]}")
        except Exception as e:
            print(f"{src['name']}获取失败: {e}")
        time.sleep(0.5)
    return result[:8]


def fetch_csrc_news() -> list:
    """证监会最新公告（AKShare）"""
    result = []
    try:
        df = ak.stock_notice_report(symbol="全部")
        for _, row in df.head(20).iterrows():
            title = str(row.get("公告标题", ""))
            if any(k in title for k in POLICY_KEYWORDS):
                result.append(f"[证监会] {title[:100]}")
    except Exception as e:
        print(f"证监会公告获取失败: {e}")
    return result[:4]


def fetch_all_policy_news() -> list:
    """汇总所有政策来源"""
    print("正在抓取多源政策新闻...")
    all_news = []
    all_news.extend(fetch_cls_news())
    all_news.extend(fetch_xinhua_news())
    all_news.extend(fetch_gov_news())
    all_news.extend(fetch_csrc_news())
    print(f"共获取政策新闻 {len(all_news)} 条")
    return all_news


# ══════════════════════════════════════════════════════════
#  第二模块：量化筛选（AKShare 基础面）
# ══════════════════════════════════════════════════════════

def run_quant_filter() -> list:
    """市值+涨幅+换手率+量比初筛"""
    try:
        print("正在拉取 A 股实时行情...")
        df = ak.stock_zh_a_spot_em()
        for col in ["总市值", "涨跌幅", "换手率", "量比"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[
            (df["总市值"] >= 5e9)  &
            (df["总市值"] <= 5e10) &
            (df["涨跌幅"] >= 2.0)  &
            (df["涨跌幅"] <= 9.5)  &
            (df["换手率"] >= 1.0)  &
            (df["量比"]   >= 1.5)
        ]
        df = df.nlargest(30, "量比")

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                "code":          row.get("代码", ""),
                "name":          row.get("名称", ""),
                "price":         round(float(row.get("最新价", 0)), 2),
                "change_pct":    round(float(row.get("涨跌幅", 0)), 2),
                "market_cap_yi": round(float(row.get("总市值", 0)) / 1e8, 1),
                "turnover_rate": round(float(row.get("换手率", 0)), 2),
                "volume_ratio":  round(float(row.get("量比", 0)), 2),
            })
        print(f"量化初筛：{len(candidates)} 只")
        return candidates[:25]
    except Exception as e:
        print(f"量化筛选失败: {e}")
        return []


# ══════════════════════════════════════════════════════════
#  第三模块：Tushare Pro 深度数据
# ══════════════════════════════════════════════════════════

def get_northbound_flow() -> dict:
    """北向资金今日净流入（外资动向）"""
    result = {"total": 0, "sh": 0, "sz": 0, "top_stocks": []}
    try:
        df = pro.moneyflow_hsgt(start_date=TODAY, end_date=TODAY)
        if df is not None and len(df) > 0:
            row = df.iloc[0]
            result["sh"]    = round(float(row.get("north_money", 0) or 0) / 1e8, 2)
            result["total"] = result["sh"]
        # 北向资金流入前10只股票
        df_top = pro.hsgt_top10(trade_date=TODAY, market_type="N")
        if df_top is not None and len(df_top) > 0:
            result["top_stocks"] = df_top[["name", "net_amount"]].head(10).to_dict("records")
        print(f"北向资金：{result['total']} 亿")
    except Exception as e:
        print(f"北向资金获取失败: {e}")
    return result


def get_dragon_tiger_list() -> list:
    """龙虎榜——机构席位买入的股票"""
    result = []
    try:
        df = pro.top_list(trade_date=TODAY)
        if df is None or len(df) == 0:
            return result
        # 筛选有机构买入的条目
        df_detail = pro.top_inst(trade_date=TODAY)
        if df_detail is not None and len(df_detail) > 0:
            inst_codes = df_detail["ts_code"].unique().tolist()
            for code in inst_codes[:10]:
                name_row = df[df["ts_code"] == code]
                name = name_row["name"].values[0] if len(name_row) > 0 else code
                result.append({"code": code, "name": name, "signal": "机构席位买入"})
        print(f"龙虎榜机构标的：{len(result)} 只")
    except Exception as e:
        print(f"龙虎榜获取失败: {e}")
    return result[:8]


def get_margin_data() -> list:
    """融资余额增加最多的股票（杠杆资金加仓信号）"""
    result = []
    try:
        df = pro.margin_detail(trade_date=TODAY)
        if df is None or len(df) == 0:
            return result
        df["rzye"] = pd.to_numeric(df["rzye"], errors="coerce")
        df["rzmre"] = pd.to_numeric(df["rzmre"], errors="coerce")
        df = df.nlargest(10, "rzmre")  # 按融资买入额排序
        for _, row in df.iterrows():
            result.append({
                "code": row.get("ts_code", ""),
                "rzye_yi": round(float(row.get("rzye", 0)) / 1e8, 2),
                "rzmre_wan": round(float(row.get("rzmre", 0)) / 1e4, 0),
            })
        print(f"融资数据：获取 {len(result)} 条")
    except Exception as e:
        print(f"融资数据获取失败: {e}")
    return result[:8]


def get_block_trade() -> list:
    """大宗交易（折价成交=大资金低价建仓信号）"""
    result = []
    try:
        df = pro.block_trade(trade_date=TODAY)
        if df is None or len(df) == 0:
            return result
        df["discount_rate"] = pd.to_numeric(df["discount_rate"], errors="coerce")
        # 筛选折价成交的（折价率 < -2%，说明大资金主动压价拿货）
        df_discount = df[df["discount_rate"] < -2].nsmallest(8, "discount_rate")
        for _, row in df_discount.iterrows():
            result.append({
                "name":          row.get("name", ""),
                "code":          row.get("ts_code", ""),
                "amount_wan":    round(float(row.get("amount", 0)) / 1e4, 0),
                "discount_rate": round(float(row.get("discount_rate", 0)), 2),
            })
        print(f"大宗交易折价：{len(result)} 条")
    except Exception as e:
        print(f"大宗交易获取失败: {e}")
    return result


def get_institution_survey() -> list:
    """近5日机构调研记录（机构在看什么）"""
    result = []
    try:
        end   = datetime.date.today()
        start = end - datetime.timedelta(days=5)
        df = pro.stk_surv(
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d")
        )
        if df is None or len(df) == 0:
            return result
        # 按调研机构数量排序
        df["fund_nums"] = pd.to_numeric(df.get("fund_nums", 0), errors="coerce").fillna(0)
        df = df.nlargest(10, "fund_nums")
        for _, row in df.iterrows():
            result.append({
                "name":      row.get("name", ""),
                "code":      row.get("ts_code", ""),
                "fund_nums": int(row.get("fund_nums", 0)),
                "date":      row.get("surv_date", ""),
            })
        print(f"机构调研：{len(result)} 条")
    except Exception as e:
        print(f"机构调研获取失败: {e}")
    return result[:8]


def get_capital_flow_rank() -> list:
    """主力资金净流入排行（AKShare）"""
    result = []
    try:
        df = ak.stock_fund_flow_rank(indicator="今日")
        if df is None or len(df) == 0:
            return result
        df["主力净流入-净额"] = pd.to_numeric(df["主力净流入-净额"], errors="coerce")
        df = df.nlargest(15, "主力净流入-净额")
        for _, row in df.iterrows():
            result.append({
                "name":         row.get("名称", ""),
                "code":         row.get("代码", ""),
                "net_flow_yi":  round(float(row.get("主力净流入-净额", 0)) / 1e8, 2),
                "change_pct":   row.get("今日涨跌幅", 0),
            })
        print(f"主力资金流入排行：{len(result)} 条")
    except Exception as e:
        print(f"主力资金流入排行获取失败: {e}")
    return result[:10]


# ══════════════════════════════════════════════════════════
#  第四模块：AI 分析
# ══════════════════════════════════════════════════════════

def ask_deepseek(prompt: str, max_tokens: int = 2000) -> str:
    for attempt in range(2):
        try:
            resp = deepseek.chat.completions.create(
                model="deepseek-chat",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.choices[0].message.content
        except Exception as e:
            print(f"DeepSeek 调用失败（第{attempt+1}次）: {e}")
            time.sleep(3)
    return "AI分析暂时不可用。"


def ai_daily_analysis(policy_news, quant_stocks, northbound,
                      dragon_tiger, margin, block_trade,
                      inst_survey, capital_flow) -> str:

    policy_text = "\n".join(policy_news) if policy_news else "今日暂无明显政策信号"

    north_text = (
        f"北向资金今日净流入：{northbound['total']} 亿元\n"
        + ("北向重仓股：" + "、".join(
            f"{s.get('name','')}({round(float(s.get('net_amount',0))/1e8,1)}亿)"
            for s in northbound.get("top_stocks", [])[:5]
        ) if northbound.get("top_stocks") else "")
    )

    dt_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['signal']}"
        for s in dragon_tiger
    ) or "今日暂无机构龙虎榜数据"

    flow_text = "\n".join(
        f"- {s['name']}（{s['code']}）：主力净流入 {s['net_flow_yi']} 亿，涨幅 {s['change_pct']}%"
        for s in capital_flow
    ) or "暂无数据"

    survey_text = "\n".join(
        f"- {s['name']}（{s['code']}）：{s['fund_nums']} 家机构调研（{s['date']}）"
        for s in inst_survey
    ) or "近5日暂无机构调研数据"

    block_text = "\n".join(
        f"- {s['name']}（{s['code']}）：成交 {s['amount_wan']} 万元，折价 {s['discount_rate']}%"
        for s in block_trade
    ) or "今日暂无折价大宗交易"

    quant_text = "\n".join(
        f"- {s['name']}（{s['code']}）：涨幅{s['change_pct']}%，市值{s['market_cap_yi']}亿，换手{s['turnover_rate']}%，量比{s['volume_ratio']}"
        for s in quant_stocks
    ) or "今日暂无符合条件的候选标的"

    prompt = f"""今天是{TODAY_CN}，请基于以下多维度数据做A股投资分析。

【一、政策与新闻信号（多源）】
{policy_text}

【二、北向资金动向】
{north_text}

【三、龙虎榜机构席位】
{dt_text}

【四、主力资金净流入排行】
{flow_text}

【五、机构调研热点（近5日）】
{survey_text}

【六、大宗交易折价成交（大资金建仓信号）】
{block_text}

【七、量化技术面候选标的】
{quant_text}

请按以下框架输出今日分析报告：

**一、政策信号解读**
- 今日政策信号强度（强/中/弱）
- 国家战略重点方向及对应板块
- 最值得关注的1-2条政策信号

**二、聪明钱动向分析**
- 北向资金今日态度（流入/流出/观望）
- 机构龙虎榜有无明显埋伏迹象
- 大宗折价是否出现主力建仓信号

**三、综合候选标的（最重要）**
综合以上所有维度，挑出3-5只最值得关注的标的，每只给出：
- 入选理由（政策/资金/技术哪个维度支撑）
- 所属赛道与国家战略关联度
- 风险提示
- 综合评分（1-10分）

**四、今日操作建议**
- 重点跟踪标的（不超过3只）
- 本周行业配置方向
- 需要警惕的风险

语言简洁，直接给出判断，不要废话。"""

    print("正在调用 DeepSeek 做综合分析...")
    return ask_deepseek(prompt, max_tokens=2500)


def ai_deep_analysis(top_stocks: list) -> str:
    if not top_stocks:
        return ""
    names = "、".join(s["name"] for s in top_stocks[:5])
    prompt = f"""请对以下A股标的做"打仗视角"深度周度分析：{names}

对每只股票：
1. 国家战略层：是否在国家要打仗的名单？政策落地哪个阶段（定调/资金进场/业绩兑现/泡沫）？
2. 行业竞争层：核心壁垒在哪个环节？公司在该环节的位置？
3. 公司质地层：近3年业绩趋势？是否切入核心供应链？
4. 估值与风险：对标海外是否合理？最大不确定性？
5. 综合评分（0-10）及一句话投资逻辑

最后给出本周最值得建仓的排序（第一到第三名）。"""
    print("正在调用 DeepSeek 做周度深度分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ══════════════════════════════════════════════════════════
#  第五模块：邮件发送
# ══════════════════════════════════════════════════════════

def get_smtp_config(email: str):
    domain = email.split("@")[-1].lower()
    configs = {
        "qq.com":      ("smtp.qq.com",   465, True),
        "foxmail.com": ("smtp.qq.com",   465, True),
        "163.com":     ("smtp.163.com",  465, True),
        "126.com":     ("smtp.126.com",  465, True),
        "gmail.com":   ("smtp.gmail.com", 587, False),
        "outlook.com": ("smtp.office365.com", 587, False),
        "me.com":      ("smtp.mail.me.com",   587, False),
        "icloud.com":  ("smtp.mail.me.com",   587, False),
    }
    return configs.get(domain, ("smtp.qq.com", 465, True))


def md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")


def build_html(title, daily, deep, quant_stocks,
               northbound, dragon_tiger, capital_flow) -> str:

    quant_rows = "".join(
        f"<tr>"
        f"<td style='padding:7px 10px'>{s['name']}</td>"
        f"<td style='padding:7px 10px;color:#888'>{s['code']}</td>"
        f"<td style='padding:7px 10px;text-align:right;color:#d63031;font-weight:500'>+{s['change_pct']}%</td>"
        f"<td style='padding:7px 10px;text-align:right'>{s['market_cap_yi']}亿</td>"
        f"<td style='padding:7px 10px;text-align:right'>{s['turnover_rate']}%</td>"
        f"<td style='padding:7px 10px;text-align:right'>{s['volume_ratio']}</td>"
        f"</tr>"
        for s in quant_stocks[:10]
    )

    north_color = "#d63031" if northbound.get("total", 0) > 0 else "#00b894"
    north_sign  = "+" if northbound.get("total", 0) > 0 else ""
    north_top   = "、".join(
        s.get("name", "") for s in northbound.get("top_stocks", [])[:5]
    ) or "暂无数据"

    dt_rows = "".join(
        f"<tr><td style='padding:6px 10px'>{s['name']}</td>"
        f"<td style='padding:6px 10px;color:#888'>{s['code']}</td>"
        f"<td style='padding:6px 10px;color:#6c5ce7'>{s['signal']}</td></tr>"
        for s in dragon_tiger[:6]
    ) or "<tr><td colspan='3' style='padding:10px;color:#aaa;text-align:center'>今日暂无数据</td></tr>"

    flow_rows = "".join(
        f"<tr><td style='padding:6px 10px'>{s['name']}</td>"
        f"<td style='padding:6px 10px;color:#d63031;text-align:right'>+{s['net_flow_yi']}亿</td>"
        f"<td style='padding:6px 10px;color:#888;text-align:right'>{s['change_pct']}%</td></tr>"
        for s in capital_flow[:6]
    ) or "<tr><td colspan='3' style='padding:10px;color:#aaa;text-align:center'>今日暂无数据</td></tr>"

    deep_section = f"""
    <div style="background:#f4f0ff;border-left:4px solid #6c5ce7;padding:16px 20px;margin:20px 0;border-radius:0 8px 8px 0">
      <h2 style="color:#6c5ce7;margin:0 0 12px;font-size:15px">📊 本周深度分析（打仗视角）</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(deep)}</div>
    </div>""" if deep else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:700px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">

  <div style="background:linear-gradient(135deg,#0984e3,#6c5ce7);padding:24px 28px;color:#fff">
    <div style="font-size:19px;font-weight:600">{title}</div>
    <div style="font-size:12px;opacity:0.8;margin-top:4px">{TODAY_CN} · 数据源：财联社+新华社+发改委+Tushare Pro</div>
  </div>

  <div style="padding:24px 28px">

    <!-- AI 综合分析 -->
    <div style="background:#f0f7ff;border-left:4px solid #0984e3;padding:16px 20px;border-radius:0 8px 8px 0;margin-bottom:20px">
      <h2 style="color:#0984e3;margin:0 0 12px;font-size:15px">📋 AI 综合分析报告</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:13px">{md_to_html(daily)}</div>
    </div>

    {deep_section}

    <!-- 聪明钱数据 -->
    <h2 style="font-size:15px;color:#2d3436;margin:20px 0 12px">🧠 聪明钱动向</h2>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">

      <div style="background:#fff5f5;border-radius:8px;padding:14px 16px;border:0.5px solid #fab1a0">
        <div style="font-size:11px;color:#888;margin-bottom:4px">北向资金今日净流入</div>
        <div style="font-size:22px;font-weight:600;color:{north_color}">{north_sign}{northbound.get('total', 0)}亿</div>
        <div style="font-size:11px;color:#888;margin-top:6px">重仓：{north_top}</div>
      </div>

      <div style="background:#f0fff4;border-radius:8px;padding:14px 16px;border:0.5px solid #55efc4">
        <div style="font-size:11px;color:#888;margin-bottom:6px">龙虎榜机构席位</div>
        <table style="width:100%;font-size:12px">
          <tr style="color:#888"><th style="text-align:left;font-weight:400">股票</th><th style="text-align:left;font-weight:400">代码</th><th style="text-align:left;font-weight:400">信号</th></tr>
          {dt_rows}
        </table>
      </div>
    </div>

    <!-- 主力资金流入排行 -->
    <h2 style="font-size:15px;color:#2d3436;margin:0 0 10px">💰 主力资金净流入 TOP10</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:20px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:7px 10px;text-align:left;font-weight:500">股票</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">净流入</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">涨幅</th>
      </tr></thead>
      <tbody>{flow_rows}</tbody>
    </table>

    <!-- 量化候选 -->
    <h2 style="font-size:15px;color:#2d3436;margin:0 0 10px">📈 量化技术面候选</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f8f9fa;color:#636e72">
        <th style="padding:7px 10px;text-align:left;font-weight:500">股票</th>
        <th style="padding:7px 10px;text-align:left;font-weight:500">代码</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">涨幅</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">市值</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">换手率</th>
        <th style="padding:7px 10px;text-align:right;font-weight:500">量比</th>
      </tr></thead>
      <tbody>{quant_rows}</tbody>
    </table>

  </div>

  <div style="padding:14px 28px;background:#f8f9fa;color:#aaa;font-size:11px;text-align:center;border-top:1px solid #eee">
    本报告由 AI 自动生成，不构成投资建议。投资有风险，决策需谨慎。
  </div>
</div></body></html>"""


def send_email(subject: str, body_html: str):
    smtp_host, smtp_port, use_ssl = get_smtp_config(EMAIL_SENDER)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = EMAIL_RECEIVER
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
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
    print(f"\n{'='*55}")
    print(f"A股智能选股系统 v2.0 启动 - {TODAY_CN}")
    print(f"{'='*55}\n")

    # ── 数据采集 ──
    print("【模块一】政策情报（多源）")
    policy_news = fetch_all_policy_news()

    print("\n【模块二】量化基础筛选")
    quant_stocks = run_quant_filter()

    print("\n【模块三】Tushare Pro 深度数据")
    northbound   = get_northbound_flow()
    dragon_tiger = get_dragon_tiger_list()
    margin       = get_margin_data()
    block_trade  = get_block_trade()
    inst_survey  = get_institution_survey()
    capital_flow = get_capital_flow_rank()

    # ── AI 分析 ──
    print("\n【模块四】AI 综合分析")
    daily_report = ai_daily_analysis(
        policy_news, quant_stocks, northbound,
        dragon_tiger, margin, block_trade,
        inst_survey, capital_flow
    )

    deep_report = ""
    if IS_WEEKEND and quant_stocks:
        print("\n【模块四+】周末深度分析")
        deep_report = ai_deep_analysis(quant_stocks[:5])

    # ── 发送报告 ──
    prefix  = "【A股周报】" if IS_WEEKEND else "【A股日报】"
    subject = f"{prefix} {TODAY_CN} · AI多维选股报告"
    html    = build_html(
        subject, daily_report, deep_report,
        quant_stocks, northbound, dragon_tiger, capital_flow
    )

    print("\n【模块五】发送邮件")
    send_email(subject, html)

    os.makedirs("reports", exist_ok=True)
    path = f"reports/report_{datetime.date.today().strftime('%Y%m%d')}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✓ 完成，报告已保存：{path}")


if __name__ == "__main__":
    main()
