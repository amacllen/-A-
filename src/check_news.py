import os
import datetime
import tushare as ts

ts.set_token(os.environ["TUSHARE_TOKEN"])
pro = ts.pro_api()

bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
today = bj.strftime("%Y-%m-%d")

print(f"北京时间：{bj.strftime('%Y-%m-%d %H:%M')}")
print(f"查询日期：{today}")
print()

# 查看cls新闻的实际日期
df = pro.news(src="cls",
              start_date=today + " 00:00:00",
              end_date=today + " 23:59:59",
              fields="datetime,title")

if df is not None and len(df) > 0:
    print(f"返回{len(df)}条，前20条时间和标题：")
    for _, row in df.head(20).iterrows():
        print(f"  [{row.get('datetime','')}] {str(row.get('title',''))[:60]}")
else:
    print("无数据")
