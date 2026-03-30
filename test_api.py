#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
最终版：全量K线统计历史涨跌家数 + 与东方财富实时数据对比验证
"""
import requests
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

EM_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_SESSION = requests.Session()
EM_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
})

def get_stock_list():
    """获取全部A股（沪深主板+创业板+科创板+北交所）"""
    all_stocks = []
    # 主 fs: m:0+t:6(深主板) + m:0+t:80(创业板) + m:1+t:2(沪主板) + m:1+t:23(科创板)
    for page in range(1, 60):
        url = f"https://push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=100&np=1&fltt=2&invt=2&fid=f12&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14"
        r = EM_SESSION.get(url, timeout=10)
        d = r.json()
        diff = d['data']['diff']
        if not diff: break
        all_stocks.extend(diff)
        if len(all_stocks) >= d['data']['total']: break
    return all_stocks

def date_offset(date_str, days):
    dt = datetime.strptime(date_str, "%Y%m%d")
    return (dt + timedelta(days=days)).strftime("%Y%m%d")

def get_stock_change(code, date_str):
    market = "1" if code.startswith(("6", "9")) else "0"
    secid = f"{market}.{code}"
    beg = date_offset(date_str, -7)
    params = {
        "secid": secid,
        "fields1": "f1",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101", "fqt": "0",
        "beg": beg, "end": date_str,
    }
    try:
        r = EM_SESSION.get(EM_KLINE_URL, params=params, timeout=5)
        d = r.json()
        data = d.get('data')
        if not data or not data.get('klines'):
            return None
        klines = data['klines']
        target_line = None
        prev_close = None
        for kline in klines:
            parts = kline.split(',')
            date = parts[0].replace('-', '')
            if date == date_str:
                target_line = parts
            elif date < date_str:
                vol = int(parts[5])
                if vol > 0:
                    prev_close = float(parts[2])
        if not target_line or prev_close is None:
            return None
        close_p = float(target_line[2])
        volume = int(target_line[5])
        if volume == 0:
            return None
        if close_p > prev_close: return 1
        elif close_p < prev_close: return -1
        else: return 0
    except:
        return None

def count_up_down(stocks, date_str, max_workers=50):
    up = down = flat = no_data = 0
    processed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(get_stock_change, s['f12'], date_str): s['f12'] for s in stocks}
        for future in as_completed(futures):
            result = future.result()
            processed += 1
            if result == 1: up += 1
            elif result == -1: down += 1
            elif result == 0: flat += 1
            else: no_data += 1
            if processed % 1000 == 0:
                print(f"  {processed}/{len(stocks)} up:{up} down:{down} flat:{flat} none:{no_data}")
    return up, down, flat, no_data

if __name__ == "__main__":
    print("获取A股列表...")
    stocks = get_stock_list()
    print(f"共 {len(stocks)} 只股票\n")
    
    # 先验证3月27日（有东方财富实时数据对比）
    print("统计 20260327...")
    t0 = time.time()
    up, down, flat, no_data = count_up_down(stocks, "20260327", max_workers=50)
    t1 = time.time()
    total = up + down + flat
    print(f"  K线统计: 上涨={up} 下跌={down} 平盘={flat} 停牌={no_data}")
    
    # 东方财富实时数据: 上证1834/469 + 深证2369/503 = 4203/972
    # 但注意：深证成指包含创业板，所以是深市全部
    # f104/f105在东方财富中是：上涨/下跌家数（该指数成分股）
    # 上证指数成分股=沪市全部，深证成指成分股=深市500只（非全部深市）
    # 创业板指成分股=创业板100只
    print(f"  东方财富(上证+深证成指): up={1834+2369} down={469+503}")
    print(f"  耗时{t1-t0:.1f}s\n")
    
    # 统计25、26号
    for date in ['20260325', '20260326']:
        print(f"统计 {date}...")
        t0 = time.time()
        up, down, flat, no_data = count_up_down(stocks, date, max_workers=50)
        t1 = time.time()
        print(f"  上涨={up} 下跌={down} 平盘={flat} 停牌={no_data}")
        print(f"  耗时{t1-t0:.1f}s\n")
