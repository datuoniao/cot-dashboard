# -*- coding: utf-8 -*-
"""
价格数据采集 (全程纯 HTTP, 可被计划任务独立调度)

  gold    : 东方财富 现货黄金/美元   122.XAU      ~1992 至今
  silver  : 东方财富 现货白银/美元   122.XAG      ~1992 至今
  crude   : 东方财富 NYMEX原油连续   102.CL00Y    ~1986 至今
  soybean : 东方财富 CBOT大豆连续    103.ZS00Y    ~2006 至今 (美分/蒲式耳)
  us10y   : 美国财政部 每日国债收益率曲线 CSV (取 "10 Yr" 列, 单位 %)
  us30y   : 同上 CSV, 取 "30 Yr" 列
  us_ultra: 同上 CSV, 取 "30 Yr" 列 (财政部无超长期收益率, 用 30Y 作同久期代理)

国债的【第二价格口径】(ALT): 国债期货本身的绝对价格 (点), 与收益率口径互为镜像
  us10y    -> 东方财富 103.TY00Y  CBOT 10 年期美债期货当月连续
  us30y    -> 东方财富 103.US00Y  CBOT 30 年期美债期货当月连续
  us_ultra -> 东方财富 103.UL00Y  CBOT 超长期美债期货当月连续
  注: 东财外盘代码不用 CME 的 ZN/ZB/UB 命名, 而用 TY/US/UL (均属市场 103 = CBOT),
      报价为十进制点值 (104.375 即 104'12, 0.375*32=12)。

注: 国债主口径为【收益率】; 收益率上行对应债券价格下行, 页面已注明。
输出: data/prices.json
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# 东方财富对 urllib 会 RemoteDisconnected, 必须走 curl
CURL = shutil.which("curl") or r"C:\Windows\System32\curl.exe"

EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid={sid}&fields1=f1,f13,f14&fields2=f51,f53"
            "&klt=101&fqt=1&beg=0&end=20500101&lmt=100000")
UST_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/"
           "interest-rates/daily-treasury-rates.csv/{y}/all"
           "?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")

START_YEAR = 2006

# 品种 -> (显示名, 源, 符号, 单位, 说明)
META = {
    "gold":     ("黄金",          "eastmoney", "122.XAU",    "美元/盎司",   "伦敦金现"),
    "silver":   ("白银",          "eastmoney", "122.XAG",    "美元/盎司",   "伦敦银现"),
    "crude":    ("原油",          "eastmoney", "102.CL00Y",  "美元/桶",     "NYMEX 原油连续"),
    "soybean":  ("美豆",          "eastmoney", "103.ZS00Y",  "美分/蒲式耳", "CBOT 大豆连续"),
    "us10y":    ("美国十年期国债", "ust", "10Y_YIELD", "%", "10年期美债收益率(与期货价格反向)"),
    "us30y":    ("美国30年期国债", "ust", "30Y_YIELD", "%", "30年期美债收益率(与期货价格反向)"),
    "us_ultra": ("美国超长期国债", "ust", "30Y_YIELD", "%", "30年期美债收益率(代理, 与期货价格反向)"),
}

# 美国财政部 CSV 中对应的列名 (财政部不发布 "超长期" 收益率, 用 30Y 作同久期代理)
UST_COLUMNS = {"us10y": "10 Yr", "us30y": "30 Yr", "us_ultra": "30 Yr"}

# 国债的第二个价格口径: 期货绝对价格 (点)
# 品种 -> (显示名, 源, 符号, 单位, 说明)
ALT = {
    "us10y":    ("10年期美债期货", "eastmoney", "103.TY00Y", "点", "CBOT 10年期美债期货当月连续"),
    "us30y":    ("30年期美债期货", "eastmoney", "103.US00Y", "点", "CBOT 30年期美债期货当月连续"),
    "us_ultra": ("超长期美债期货", "eastmoney", "103.UL00Y", "点", "CBOT 超长期美债期货当月连续"),
}


def http_get(url: str, timeout: int = 60) -> bytes | None:
    for attempt in range(3):
        try:
            p = subprocess.run(
                [CURL, "-sS", "-L", "--max-time", str(timeout),
                 "-H", f"User-Agent: {UA}", url],
                capture_output=True,
            )
            if p.returncode == 0 and p.stdout:
                return p.stdout
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


# 东财对外盘连续合约有较严的频率限制, 连续快速请求会被断开 (curl 56 / http 000),
# 因此每个东财请求之间留出间隔, 由 http_get 内部再做 3 次重试。
EM_DELAY = 1.2


def fetch_eastmoney(sid: str) -> dict:
    """返回 {date: close}"""
    blob = http_get(EM_KLINE.format(sid=sid))
    time.sleep(EM_DELAY)
    if not blob:
        return {}
    try:
        d = json.loads(blob.decode("utf-8", "replace"))
    except Exception:
        return {}
    klines = (d.get("data") or {}).get("klines") or []
    out = {}
    for line in klines:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        try:
            out[parts[0]] = float(parts[1])
        except ValueError:
            continue
    return out


def fetch_ust_year(year: int, columns) -> dict:
    """美国财政部某年每日收益率 -> {列名: {date: yield}}"""
    url = UST_CSV.format(y=year)
    blob = http_get(url)
    if not blob:
        return {}
    text = blob.decode("utf-8-sig", "replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return {}
    header = [h.strip() for h in header]
    idx = {c: (header.index(c) if c in header else -1) for c in columns}
    out = {c: {} for c in columns}
    for row in reader:
        if not row:
            continue
        raw = row[0].strip()
        try:
            m, d, y = raw.split("/")
            iso = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except (ValueError, IndexError):
            continue
        for c, j in idx.items():
            if 0 <= j < len(row):
                try:
                    out[c][iso] = float(row[j])
                except ValueError:
                    pass
    return out


def main():
    cur = datetime.now().year
    result = {"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "instruments": {}}

    # 财政部 CSV 按年抓取一次并同时取多列, 避免每个国债品种重复请求
    ust_cols = sorted({UST_COLUMNS[k] for k, v in META.items() if v[1] == "ust"})
    ust = {c: {} for c in ust_cols}
    for y in range(START_YEAR, cur + 1):
        for c, vals in fetch_ust_year(y, ust_cols).items():
            ust[c].update(vals)

    for key, (name, source, symbol, unit, note) in META.items():
        if source == "eastmoney":
            series = fetch_eastmoney(symbol)
        else:
            series = ust.get(UST_COLUMNS.get(key, ""), {})
        dates = sorted(series)
        result["instruments"][key] = {
            "name": name, "source": source, "symbol": symbol,
            "unit": unit, "note": note,
            "series": {d: series[d] for d in dates},
        }
        if dates:
            print(f"  [{key:8s}] {symbol:10s} 日数={len(dates):5d}  "
                  f"{dates[0]} .. {dates[-1]}  末值={series[dates[-1]]}")
        else:
            print(f"  [{key:8s}] {symbol:10s}  无数据!", file=sys.stderr)

    # 第二价格口径 (国债期货绝对价格)
    for key, (name, source, symbol, unit, note) in ALT.items():
        if key not in result["instruments"]:
            continue
        series = fetch_eastmoney(symbol) if source == "eastmoney" else {}
        dates = sorted(series)
        result["instruments"][key]["alt"] = {
            "name": name, "source": source, "symbol": symbol,
            "unit": unit, "note": note,
            "series": {d: series[d] for d in dates},
        }
        if dates:
            print(f"  [{key:8s}] {symbol:10s} 日数={len(dates):5d}  "
                  f"{dates[0]} .. {dates[-1]}  末值={series[dates[-1]]}  <- {name}")
        else:
            print(f"  [{key:8s}] {symbol:10s}  无数据! ({name})", file=sys.stderr)

    # 防护: 若某个数据源【整体】不可用(如东财限流/网络中断), 直接中止而不是写出空序列,
    # 否则会用空价格覆盖上一份可用数据, 且看板不会报错。个别品种缺失仍照常继续。
    for src, label in (("eastmoney", "东方财富"), ("ust", "美国财政部")):
        keys = [k for k, v in META.items() if v[1] == src]
        if keys and all(not result["instruments"][k]["series"] for k in keys):
            print(f"[fetch_prices] 严重错误: {label} 行情整体获取失败(疑似限流或网络不可达), "
                  f"本次不写出 prices.json, 保留上一份可用数据。", file=sys.stderr)
            sys.exit(1)

    out_path = os.path.join(DATA, "prices.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[fetch_prices] -> {out_path}")


if __name__ == "__main__":
    main()
