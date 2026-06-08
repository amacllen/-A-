import os
import datetime
import tushare as ts

ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
today_ts = bj.strftime("%Y%m%d")
print(f"北京时间：{bj.strftime('%Y-%m-%d %H:%M')}")
print(f"今日(YYYYMMDD)：{today_ts}")
print()

# 找最近一个交易日
ago = (bj - datetime.timedelta(days=15)).strftime("%Y%m%d")
cal = pro.trade_cal(exchange="", start_date=ago, end_date=today_ts, is_open="1")
trade_days = sorted(cal["cal_date"].tolist())
last_trade = trade_days[-1] if trade_days else today_ts
prev_trade = trade_days[-2] if len(trade_days) >= 2 else last_trade
print(f"最近交易日：{last_trade}    上一交易日：{prev_trade}")
print("=" * 60)

# ── 1. 行业资金（同花顺）：确认 net_amount 的真实单位 ──
print("\n【1】moneyflow_ind_ths 行业资金 —— 确认 net_amount 单位")
try:
    df = pro.moneyflow_ind_ths(trade_date=last_trade)
    if df is None or len(df) == 0:
        print(f"  {last_trade} 无数据（可能未入库）")
    else:
        print(f"  返回 {len(df)} 个板块，字段：{list(df.columns)}")
        df["net_amount"] = df["net_amount"].astype(float)
        top = df.nlargest(5, "net_amount")
        print("  净流入 TOP5（net_amount 为接口原始值，未做任何换算）：")
        for _, r in top.iterrows():
            raw = r["net_amount"]
            print(f"    {str(r.get('industry','')):8s}  原始值={raw:>18,.2f}   "
                  f"÷1e8={raw/1e8:>10.4f}   ÷1e4={raw/1e4:>10.2f}")
        print("  → 对照行情软件该板块当日净流入，看哪一列(÷1e8 还是 ÷1e4 还是原始值)接近真实'亿'数")
except Exception as e:
    print(f"  失败：{e}")

# ── 2. 个股主力资金：确认今日是否入库（时间问题验证）──
print("\n【2】moneyflow 个股主力资金 —— 验证入库时间")
for label, d in [("今日", today_ts), ("最近交易日", last_trade), ("上一交易日", prev_trade)]:
    try:
        df = pro.moneyflow(trade_date=d, fields="ts_code,net_mf_amount,buy_lg_amount")
        n = 0 if df is None else len(df)
        print(f"  {label}({d})：{n} 条" + ("  ← 空，说明此刻尚未入库" if n == 0 else "  ← 已入库"))
        if n > 0:
            r = df.iloc[0]
            print(f"      样本 {r['ts_code']}  net_mf_amount原始值={float(r['net_mf_amount']):,.2f}")
    except Exception as e:
        print(f"  {label}({d})：失败 {e}")
