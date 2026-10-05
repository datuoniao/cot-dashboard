# -*- coding: utf-8 -*-
"""
合并持仓与价格, 计算净持仓与 COT Index, 生成前端数据源 data/cot_data.json

对齐规则:
  CFTC 报告日为周二收盘; 价格取【<= 报告日的最近一个交易日】收盘, 避免三天错位。

COT Index:
  投机类净持仓在【滚动 3 年(156 周)】窗口内的 min-max 归一化到 0-100。
"""
import bisect
import json
import os
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")

INDEX_WINDOW = 156  # 3 年

# 品种展示配置
DISPLAY = {
    "gold":     {"name": "黄金",           "price_name": "伦敦金现",       "primary": "m_money"},
    "silver":   {"name": "白银",           "price_name": "伦敦银现",       "primary": "m_money"},
    "crude":    {"name": "原油",           "price_name": "NYMEX原油连续",  "primary": "m_money"},
    "soybean":  {"name": "美豆",           "price_name": "CBOT大豆连续",   "primary": "m_money"},
    "sugar":    {"name": "原糖",           "price_name": "ICE原糖期货主力", "primary": "m_money"},
    "us10y":    {"name": "美国十年期国债",  "price_name": "10年期美债收益率", "primary": "lev_money"},
    "us30y":    {"name": "美国30年期国债",  "price_name": "30年期美债收益率", "primary": "lev_money"},
}

GROUP_LABELS = {
    "disaggregated": [
        ("prod_merc",  "生产商/贸易商", "hedge"),
        ("swap",       "掉期商",        "hedge"),
        ("m_money",    "管理基金",      "spec"),
        ("other_rept", "其他报告头寸",  "other"),
    ],
    "tff": [
        ("dealer",     "交易商/中介",   "hedge"),
        ("asset_mgr",  "资产管理机构",  "other"),
        ("lev_money",  "杠杆基金",      "spec"),
        ("other_rept", "其他报告头寸",  "other"),
    ],
}


def rolling_index(values, window=INDEX_WINDOW):
    """滚动窗口 min-max 归一化到 0-100"""
    out = []
    for i in range(len(values)):
        seg = [v for v in values[max(0, i - window + 1): i + 1] if v is not None]
        if not seg:
            out.append(None)
            continue
        lo, hi = min(seg), max(seg)
        out.append(50.0 if hi == lo else round((values[i] - lo) / (hi - lo) * 100, 1))
    return out


def align_price(series_map: dict, dates: list) -> list:
    """把 {date: value} 价格序列按 CFTC 报告日对齐: 取 <= 报告日的最近一个交易日收盘。"""
    if not series_map:
        return [None] * len(dates)
    pdates = sorted(series_map)
    out = []
    for d in dates:
        j = bisect.bisect_right(pdates, d) - 1
        out.append(series_map[pdates[j]] if j >= 0 else None)
    return out


def main():
    positions = json.load(open(os.path.join(DATA, "positions.json"), encoding="utf-8"))
    prices = json.load(open(os.path.join(DATA, "prices.json"), encoding="utf-8"))

    instruments = {}
    for key, pos in positions["instruments"].items():
        series = pos["series"]
        if not series:
            continue
        disp = DISPLAY.get(key, {})
        price_info = prices["instruments"].get(key, {})
        price_series = price_info.get("series", {})
        alt_info = price_info.get("alt") or {}
        alt_series = alt_info.get("series") or {}

        dates = [r["date"] for r in series]
        oi = [r["oi"] for r in series]
        price_vals = align_price(price_series, dates)

        groups_meta = GROUP_LABELS[pos["report_type"]]
        out_groups = {}
        for gkey, glabel, grole in groups_meta:
            longs, shorts, nets = [], [], []
            for r in series:
                g = r["groups"].get(gkey) or {}
                lo, sh = g.get("long"), g.get("short")
                longs.append(lo)
                shorts.append(sh)
                nets.append(None if (lo is None or sh is None) else lo - sh)
            out_groups[gkey] = {
                "label": glabel, "role": grole,
                "long": longs, "short": shorts, "net": nets,
                "net_index": rolling_index(nets),
            }

        primary = disp.get("primary", "m_money")
        instruments[key] = {
            "name": disp.get("name", key),
            "contract_code": pos["contract_code"],
            "report_type": pos["report_type"],
            "primary_group": primary,
            "price": {
                "name": disp.get("price_name") or price_info.get("name"),
                "symbol": price_info.get("symbol"),
                "unit": price_info.get("unit"),
                "note": price_info.get("note"),
            },
            "group_order": [g[0] for g in groups_meta],
            "groups": out_groups,
            "dates": dates,
            "oi": oi,
            "price_values": price_vals,
        }
        # 第二价格口径 (国债期货绝对价格), 仅国债品种有
        if alt_series:
            instruments[key]["price_alt"] = {
                "name": alt_info.get("name"),
                "symbol": alt_info.get("symbol"),
                "unit": alt_info.get("unit"),
                "note": alt_info.get("note"),
            }
            instruments[key]["price_alt_values"] = align_price(alt_series, dates)

    result = {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "latest_report_date": positions.get("latest_report_date"),
        "price_fetched_at": prices.get("fetched_at"),
        "instruments": instruments,
    }
    out_path = os.path.join(DATA, "cot_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))

    # 同步输出 JS 版本, 使 index.html 可直接双击(file://)打开, 不受 fetch CORS 限制
    js_path = os.path.join(DATA, "cot_data.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.COT_DATA=")
        json.dump(result, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";")

    size = os.path.getsize(out_path)
    print(f"[build_data] {len(instruments)} 品种, 最新报告周={result['latest_report_date']}")
    for k, v in instruments.items():
        n = len(v["dates"])
        miss = sum(1 for p in v["price_values"] if p is None)
        prim = v["groups"][v["primary_group"]]
        line = (f"  {k:8s} {n:4d}周 {v['dates'][0]}..{v['dates'][-1]}  "
                f"缺价={miss}  {v['primary_group']}净={prim['net'][-1]} "
                f"index={prim['net_index'][-1]}  价={v['price_values'][-1]}")
        if v.get("price_alt_values"):
            amiss = sum(1 for p in v["price_alt_values"] if p is None)
            line += f"  期货价={v['price_alt_values'][-1]}({v['price_alt']['unit']},缺{amiss})"
        print(line)
    print(f"[build_data] -> {out_path} ({size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
