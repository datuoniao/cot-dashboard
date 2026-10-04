# -*- coding: utf-8 -*-
"""
CFTC Commitments of Traders - 持仓采集

数据源: CFTC 官方历史 ZIP 包 (传统站点, 沙箱内可达; Socrata JSON API 被 403 拦截)

口径: 【期货 + 期权 合并】(Futures-and-Options-Combined)。合并报告中 _All 后缀列
      即 "全部头寸" = 期货 + 期权; 而 fut_* 期货only 包的 _All 仅含期货。
      两者列名完全一致, 因此切换口径只需换 ZIP 地址, 解析逻辑不变。

  - 分项报告 Disaggregated (期货+期权)
      * 年度包: com_disagg_txt_{YYYY}.zip  内 c_year.txt
      * 汇总包: com_disagg_txt_hist_2006_2016.zip  内 C_Disagg06_16.txt
  - 金融期货 TFF (期货+期权)
      * 年度包: com_fin_txt_{YYYY}.zip  内 FinComYY.txt
      * 汇总包: fin_com_txt_2006_2016.zip  内 C_TFF_2006_2016.txt

关键点:
  * 不同年份文件表头有差异 (Report_Date_as_YYYY-MM-DD / Report_Date_as_MM_DD_YYYY,
    值格式甚至与列名不符), 因此统一用 As_of_Date_In_Form_YYMMDD (6位 YYMMDD) 作为日期主键。
  * 字段可能带引号或前后空格, 使用 csv 模块解析 (所有行列数一致, 无逗号错位风险)。
  * Ultra UST Bond (020604) 2010-01 才上市, 故其历史自 2010-03 起, 周数少于其他品种。

输出: data/positions.json
"""
import csv
import io
import json
import os
import sys
import time
import zipfile
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "cache")
DATA = os.path.join(BASE, "data")
os.makedirs(CACHE, exist_ok=True)
os.makedirs(DATA, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HOST = "https://www.cftc.gov/files/dea/history/"

ANNUAL_START = 2010           # 年度包最早年份
HIST_END = 2016               # 汇总包覆盖到 2016

# 报告类型 -> (年度包URL模板, 年度包内文件, 汇总包URL, 汇总包内文件)
# 注意: com_* = 期货+期权合并口径 (历史: com_disagg_txt_hist / fin_com_txt)
REPORTS = {
    "disaggregated": (
        HOST + "com_disagg_txt_{y}.zip", "c_year.txt",
        HOST + "com_disagg_txt_hist_2006_2016.zip", "C_Disagg06_16.txt",
    ),
    "tff": (
        HOST + "com_fin_txt_{y}.zip", "FinComYY.txt",
        HOST + "fin_com_txt_2006_2016.zip", "C_TFF_2006_2016.txt",
    ),
}

# 缓存文件名前缀 (与旧的期货only 缓存 disagg_*/tff_* 区分开)
CACHE_NAME = {"disaggregated": "comdisagg", "tff": "comfin"}

# 品种 -> (报告类型, 合约代码)
TARGETS = {
    "gold":     ("disaggregated", "088691"),   # 黄金 COMEX
    "silver":   ("disaggregated", "084691"),   # 白银 COMEX
    "crude":    ("disaggregated", "067651"),   # WTI 原油 NYMEX
    "soybean":  ("disaggregated", "005602"),   # 美豆 CBOT
    "us10y":    ("tff",           "043602"),   # 10 年期美债期货 CBOT
    "us30y":    ("tff",           "020601"),   # 30 年期美债期货 CBOT
    "us_ultra": ("tff",           "020604"),   # 超长期美债期货 CBOT (2010 上市)
}

# 各报告类型关注的交易员类别 -> 字段基名 (不含 _Positions_<side>_All)
# 注意 CFTC 的历史怪癖: Swap 空头列名为双下划线 Swap__Positions_Short_All
GROUP_FIELDS = {
    "disaggregated": {
        "prod_merc":  "Prod_Merc",
        "swap":       "Swap",
        "m_money":    "M_Money",
        "other_rept": "Other_Rept",
    },
    "tff": {
        "dealer":     "Dealer",
        "asset_mgr":  "Asset_Mgr",
        "lev_money":  "Lev_Money",
        "other_rept": "Other_Rept",
    },
}

CODE2KEY = {code: key for key, (_, code) in TARGETS.items()}


def download(url: str, filename: str, force: bool = False) -> str | None:
    """下载(或复用缓存) -> 本地路径; 404 或失败返回 None。"""
    path = os.path.join(CACHE, filename)
    if not force and os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as resp:
                blob = resp.read()
            if len(blob) < 100:
                return None
            with open(path, "wb") as f:
                f.write(blob)
            return path
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(1 + attempt)
        except Exception:
            time.sleep(1 + attempt)
    return None


def _int(v):
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _field(row, base, side):
    """按候选列名取值, 兼容单/双下划线两种历史写法。"""
    for key in (f"{base}_Positions_{side}_All", f"{base}__Positions_{side}_All"):
        v = row.get(key)
        if v not in (None, ""):
            return v
    return None


def _date_from_asof(v):
    """As_of_Date_In_Form_YYMMDD -> YYYY-MM-DD"""
    s = "".join(ch for ch in str(v) if ch.isdigit())
    if len(s) < 6:
        return None
    s = s[-6:]
    yy, mm, dd = int(s[:2]), s[2:4], s[4:6]
    return f"20{yy:02d}-{mm}-{dd}"


def parse_zip(path: str, report_type: str) -> list[tuple[str, dict]]:
    _, _, hist_url, hist_inner = REPORTS[report_type]
    groups = GROUP_FIELDS[report_type]
    codes = {c for _k, (rt, c) in TARGETS.items() if rt == report_type}
    out = []
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        # 优先按已知名取; 否则取第一个 .txt
        inner = None
        for candidate in names:
            if candidate.lower().endswith(".txt"):
                inner = candidate
                break
        if inner is None:
            return out
        with zf.open(inner) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8-sig", errors="replace")
            reader = csv.DictReader(text)
            for row in reader:
                code = (row.get("CFTC_Contract_Market_Code") or "").strip()
                if code not in codes:
                    continue
                date = _date_from_asof(row.get("As_of_Date_In_Form_YYMMDD"))
                if not date:
                    continue
                rec = {"date": date, "oi": _int(row.get("Open_Interest_All")),
                       "groups": {}}
                for g, base in groups.items():
                    rec["groups"][g] = {
                        "long":  _int(_field(row, base, "Long")),
                        "short": _int(_field(row, base, "Short")),
                    }
                out.append((code, rec))
    return out


def main():
    current_year = datetime.now().year
    tasks = []  # (report_type, url, filename, force)

    # 汇总包覆盖 2006-2016
    tasks.append(("disaggregated", REPORTS["disaggregated"][2], "comdisagg_hist.zip", False))
    tasks.append(("tff",           REPORTS["tff"][2],           "comfin_hist.zip",    False))
    # 年度包 (当年文件每周刷新 -> force)
    for y in range(ANNUAL_START, current_year + 1):
        for rt in REPORTS:
            tpl, _inner, _hu, _hi = REPORTS[rt]
            fn = f"{CACHE_NAME[rt]}_{y}.zip"
            tasks.append((rt, tpl.format(y=y), fn, y == current_year))

    collected = {c: {} for c in CODE2KEY}  # code -> {date: rec}
    missing = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futures = [(rt, fn, ex.submit(download, url, fn, force)) for rt, url, fn, force in tasks]
        for rt, fn, fut in futures:
            path = fut.result()
            if not path:
                missing.append(fn)
                continue
            try:
                for code, rec in parse_zip(path, rt):
                    collected[code][rec["date"]] = rec
            except Exception as e:
                print(f"  ! 解析失败 {fn}: {e}", file=sys.stderr)

    instruments = {}
    for key, (rt, code) in TARGETS.items():
        series = [collected[code][d] for d in sorted(collected[code])]
        instruments[key] = {
            "contract_code": code,
            "report_type": rt,
            "groups": list(GROUP_FIELDS[rt].keys()),
            "series": series,
        }
        if series:
            print(f"  [{key:8s}] {code}  周数={len(series):4d}  "
                  f"{series[0]['date']} .. {series[-1]['date']}")
        else:
            print(f"  [{key:8s}] {code}  无数据!", file=sys.stderr)

    all_dates = [inst["series"][-1]["date"] for inst in instruments.values() if inst["series"]]
    result = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latest_report_date": max(all_dates) if all_dates else None,
        "missing_files": missing,
        "instruments": instruments,
    }
    out_path = os.path.join(DATA, "positions.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[fetch_positions] 口径=期货+期权(Futures-and-Options-Combined) "
          f"最新报告周={result['latest_report_date']} -> {out_path}")
    if missing:
        print(f"[fetch_positions] 缺失 {len(missing)} 个文件: {missing[:6]}", file=sys.stderr)


if __name__ == "__main__":
    main()
