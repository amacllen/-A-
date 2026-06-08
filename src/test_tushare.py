"""
测试Tushare盘中接口是否正常
"""
import os
import datetime
import tushare as ts
import pandas as pd

TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if not TOKEN:
    print("ERROR: 没有TUSHARE_TOKEN环境变量")
    exit(1)

ts.set_token(TOKEN)
pro = ts.pro_api()

bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
TODAY = bj.strftime("%Y%m%d")
print(f"北京时间：{bj.strftime('%Y-%m-%d %H:%M')}")
print(f"TODAY = {TODAY}")
print()

tests = [
    ("涨停板 limit_list_d",    lambda: pro.limit_list_d(trade_date=TODAY, limit_type="U")),
    ("连板天梯 limit_step",     lambda: pro.limit_step(trade_date=TODAY)),
    ("最强板块 limit_top_sector", lambda: pro.limit_top_sector(trade_date=TODAY)),
    ("THS热榜 ths_hot",        lambda: pro.ths_hot(trade_date=TODAY)),
    ("游资明细 hm_detail",      lambda: pro.hm_detail(trade_date=TODAY)),
    ("板块资金 moneyflow_sector_ths", lambda: pro.moneyflow_sector_ths(trade_date=TODAY)),
    ("新闻快讯 news(cls)",      lambda: pro.news(src="cls",
                                                start_date=bj.strftime("%Y-%m-%d")+" 00:00:00",
                                                end_date=bj.strftime("%Y-%m-%d")+" 23:59:59")),
]

for name, func in tests:
    try:
        df = func()
        if df is not None and len(df) > 0:
            print(f"✓ {name}：{len(df)}行，字段：{df.columns.tolist()[:5]}")
        else:
            print(f"✗ {name}：返回空数据")
    except Exception as e:
        print(f"✗ {name}：失败 → {e}")
