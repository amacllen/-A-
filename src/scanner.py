"""
A股智能选股系统 - 主扫描脚本
每日自动运行：政策信号 + 量化筛选 + 技术面初筛 + AI深度分析
AI引擎：DeepSeek   推送方式：邮件
"""

import os
import re
import datetime
import time
import smtplib
import akshare as ak
import pandas as pd
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from openai import OpenAI  # DeepSeek 兼容 OpenAI SDK

# ─── 初始化 ────────────────────────────────────────────────
deepseek = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url="https://api.deepseek.com"
)

EMAIL_SENDER   = os.environ["EMAIL_SENDER"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_RECEIVER = os.environ["EMAIL_RECEIVER"]

def get_smtp_config(email: str):
    domain = email.split("@")[-1].lower()
    configs = {
        "qq.com":      ("smtp.qq.com",   465, True),
        "foxmail.com": ("smtp.qq.com",   465, True),
        "163.com":     ("smtp.163.com",  465, True),
        "126.com":     ("smtp.126.com",  465, True),
        "gmail.com":   ("smtp.gmail.com", 587, False),
        "outlook.com": ("smtp.office365.com", 587, False),
        "me.com":      ("smtp.mail.me.com",  587, False),
        "icloud.com":  ("smtp.mail.me.com",  587, False),
    }
    return configs.get(domain, ("smtp.qq.com", 465, True))

TODAY   = datetime.date.today().strftime("%Y年%m月%d日")
WEEKDAY = datetime.date.today().weekday()


# ─── AI 调用封装 ───────────────────────────────────────────
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
    return "AI分析暂时不可用，请稍后手动查看。"


# ─── 第一步：政策情报 ──────────────────────────────────────
def fetch_policy_news() -> list:
    news_list = []
    keywords = [
        "政策","国务院","发改委","工信部","财政部","支持","战略",
        "算力","半导体","新能源","军工","生物","机器人",
        "低空","储能","补贴","专项债","产业基金","规划","攻关"
    ]
    try:
        df = ak.stock_telegraph_cls()
        for _, row in df.head(40).iterrows():
            content = str(row.get("content", ""))
            if any(k in content for k in keywords):
                news_list.append(content[:200])
    except Exception as e:
        print(f"财联社新闻获取失败: {e}")
    print(f"获取到 {len(news_list)} 条政策相关新闻")
    return news_list[:15]


# ─── 第二步：量化筛选 ──────────────────────────────────────
def run_quant_filter() -> list:
    try:
        print("正在拉取 A 股实时行情...")
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
        ]
        df = df.nlargest(30, "量比")

        candidates = []
        for _, row in df.iterrows():
            candidates.append({
                "code":          row.get("代码", ""),
                "name":          row.get("名称", ""),
                "price":         row.get("最新价", 0),
                "change_pct":    round(float(row.get("涨跌幅", 0)), 2),
                "market_cap_yi": round(float(row.get("总市值", 0)) / 1e8, 1),
                "turnover_rate": round(float(row.get("换手率", 0)), 2),
                "volume_ratio":  round(float(row.get("量比", 0)), 2),
            })
        print(f"量化初筛完成，候选标的：{len(candidates)} 只")
        return candidates[:20]
    except Exception as e:
        print(f"量化筛选失败: {e}")
        return []


# ─── 第三步：资金流向过滤 ──────────────────────────────────
def filter_by_capital_flow(candidates: list) -> list:
    filtered = []
    for stock in candidates:
        try:
            code   = stock["code"]
            market = "sh" if code.startswith("6") else "sz"
            df_flow = ak.stock_individual_fund_flow(stock=code, market=market)
            if df_flow is not None and len(df_flow) >= 3:
                net = pd.to_numeric(
                    df_flow.head(3).get("主力净流入-净额", pd.Series([0])),
                    errors="coerce"
                ).sum()
                stock["net_flow_3d_wan"] = round(net / 1e4, 0)
                if net > 0:
                    filtered.append(stock)
            time.sleep(0.4)
        except Exception:
            filtered.append(stock)
    print(f"资金流向筛选后：{len(filtered)} 只")
    return filtered


# ─── 第四步：AI 每日分析 ───────────────────────────────────
def ai_daily_analysis(policy_news: list, candidates: list) -> str:
    policy_text = (
        "\n".join(f"- {n}" for n in policy_news)
        if policy_news else "今日暂无明显政策信号"
    )
    stock_text = "\n".join(
        f"- {s['name']}（{s['code']}）：涨幅 {s['change_pct']}%，"
        f"市值 {s['market_cap_yi']} 亿，换手率 {s['turnover_rate']}%，量比 {s['volume_ratio']}"
        for s in candidates
    ) or "今日暂无符合条件的候选标的"

    prompt = f"""今天是{TODAY}，请对以下 A 股信息做投资分析。

【今日政策与新闻信号】
{policy_text}

【量化筛选候选标的（已过滤市值50-500亿、主力资金净流入）】
{stock_text}

请按以下框架输出分析报告：

**一、政策信号解读**
1. 今日政策信号强度（强/中/弱）及核心理由
2. 涉及哪些国家战略方向？对应 A 股哪些主力板块？
3. 当前国家重点"要打仗"的产业是哪几个？

**二、候选标的初步筛选**
从候选标的中结合政策方向挑出 3-5 只重点关注，每只给出：
- 所属板块与国家战略关联度
- 上涨是业绩驱动还是题材炒作的初步判断
- 技术面简评（量比/换手率角度）
- 综合关注度评分（1-10 分）

**三、今日操作建议**
- 值得今日重点跟踪的标的（不超过 3 只）
- 本周需持续关注的行业方向
- 需警惕的风险点

语言简洁专业，直接给出判断。"""

    print("正在调用 DeepSeek 做每日分析...")
    return ask_deepseek(prompt, max_tokens=2000)


# ─── 第五步：周末深度分析 ──────────────────────────────────
def ai_deep_analysis(top_stocks: list) -> str:
    if not top_stocks:
        return ""
    names = "、".join(s["name"] for s in top_stocks[:5])
    prompt = f"""请对以下 A 股标的做"打仗视角"深度周度分析：{names}

对每只股票逐一分析：
1. 国家战略层：是否在国家要去打仗的名单里？政策落地处于哪个阶段（定调/资金进场/业绩兑现/泡沫）？
2. 行业竞争层：核心壁垒在哪个环节？公司在该环节处于什么位置？
3. 公司质地层：近 3 年业绩趋势？是否已切入核心供应链（英伟达/华为/比亚迪等）？
4. 估值与风险：对标海外同类是否合理？最大不确定性是什么？
5. 综合评分（0-10）及一句话投资逻辑

最后给出本周最值得重点关注的排序（第一到第三）。"""

    print("正在调用 DeepSeek 做周度深度分析...")
    return ask_deepseek(prompt, max_tokens=3000)


# ─── 第六步：邮件发送 ──────────────────────────────────────
def md_to_html(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    return text.replace("\n", "<br>")

def build_html(title: str, daily: str, deep: str, candidates: list) -> str:
    table_rows = "".join(
        f"<tr>"
        f"<td style='padding:8px 10px'>{s['name']}</td>"
        f"<td style='padding:8px 10px;color:#888'>{s['code']}</td>"
        f"<td style='padding:8px 10px;text-align:right;color:#d63031;font-weight:500'>+{s['change_pct']}%</td>"
        f"<td style='padding:8px 10px;text-align:right'>{s['market_cap_yi']} 亿</td>"
        f"<td style='padding:8px 10px;text-align:right'>{s['turnover_rate']}%</td>"
        f"<td style='padding:8px 10px;text-align:right'>{s['volume_ratio']}</td>"
        f"</tr>"
        for s in candidates[:10]
    )
    deep_section = f"""
    <div style="background:#f4f0ff;border-left:4px solid #6c5ce7;padding:16px 20px;margin:20px 0;border-radius:0 8px 8px 0">
      <h2 style="color:#6c5ce7;margin:0 0 12px;font-size:15px">📊 本周深度分析（打仗视角）</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:14px">{md_to_html(deep)}</div>
    </div>""" if deep else ""

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif">
<div style="max-width:680px;margin:20px auto;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08)">
  <div style="background:linear-gradient(135deg,#0984e3,#6c5ce7);padding:24px 28px;color:#fff">
    <div style="font-size:19px;font-weight:600">{title}</div>
    <div style="font-size:12px;opacity:0.8;margin-top:4px">{TODAY} · AI 自动生成 · 仅供参考</div>
  </div>
  <div style="padding:24px 28px">
    <div style="background:#f0f7ff;border-left:4px solid #0984e3;padding:16px 20px;border-radius:0 8px 8px 0">
      <h2 style="color:#0984e3;margin:0 0 12px;font-size:15px">📋 每日分析报告</h2>
      <div style="color:#2d3436;line-height:1.9;font-size:14px">{md_to_html(daily)}</div>
    </div>
    {deep_section}
    <h2 style="color:#2d3436;font-size:15px;margin:24px 0 10px">📈 今日候选标的</h2>
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f8f9fa;color:#636e72;border-bottom:1px solid #eee">
        <th style="padding:8px 10px;text-align:left;font-weight:500">股票</th>
        <th style="padding:8px 10px;text-align:left;font-weight:500">代码</th>
        <th style="padding:8px 10px;text-align:right;font-weight:500">涨幅</th>
        <th style="padding:8px 10px;text-align:right;font-weight:500">市值</th>
        <th style="padding:8px 10px;text-align:right;font-weight:500">换手率</th>
        <th style="padding:8px 10px;text-align:right;font-weight:500">量比</th>
      </tr></thead>
      <tbody>{table_rows}</tbody>
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


# ─── 主流程 ────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"A股智能选股系统启动 - {TODAY}")
    print(f"{'='*50}\n")

    is_weekend = WEEKDAY >= 5

    print("Step 1: 抓取政策新闻...")
    policy_news = fetch_policy_news()

    print("\nStep 2: 量化条件筛选...")
    candidates = run_quant_filter()

    print("\nStep 3: 资金流向过滤...")
    filtered = filter_by_capital_flow(candidates)

    print("\nStep 4: AI 每日分析...")
    daily_report = ai_daily_analysis(policy_news, filtered)

    deep_report = ""
    if is_weekend and filtered:
        print("\nStep 5: 周末深度分析...")
        deep_report = ai_deep_analysis(filtered[:5])

    prefix  = "【A股周报】" if is_weekend else "【A股日报】"
    subject = f"{prefix} {TODAY} · AI选股报告"
    html    = build_html(subject, daily_report, deep_report, filtered)

    print("\nStep 6: 发送邮件...")
    send_email(subject, html)

    os.makedirs("reports", exist_ok=True)
    path = f"reports/report_{datetime.date.today().strftime('%Y%m%d')}.html"
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"报告已保存：{path}")


if __name__ == "__main__":
    main()
