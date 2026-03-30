#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
涨停日报自动生成脚本
数据源：同花顺涨停池 + 东方财富批量行情
输出：桌面 涨停日报/YYYYMMDD.html
"""
import requests
import time
import re
import json
import sqlite3
import subprocess


from datetime import datetime, timedelta
import os
from collections import defaultdict

# 配置
OUTPUT_DIR = r"C:\Users\yanghui\Desktop\涨停日报"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, f"涨停日报_{datetime.now().strftime('%Y%m%d')}.html")

# GitHub Pages 发布（仓库根目录）
GITHUB_PAGES_DIR = os.path.dirname(os.path.abspath(__file__))
GITHUB_REPO_URL = "https://github.com/yanghuiysz/limitup-report.git"

TODAY = datetime.now().strftime("%Y%m%d")
TODAY_HYPHEN = datetime.now().strftime("%Y-%m-%d")

# 请求头
THS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://data.10jqka.com.cn/",
    "Accept": "application/json, text/plain, */*",
}

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

THS_SESSION = requests.Session()
THS_SESSION.headers.update(THS_HEADERS)

EM_SESSION = requests.Session()
EM_SESSION.headers.update(EM_HEADERS)


# ==================== SQLite 缓存模块 ====================

DB_PATH = os.path.join(OUTPUT_DIR, "cache.db")


def _get_conn():
    """获取数据库连接，自动建表"""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 每日市场统计（涨停/跌停/上涨/下跌家数）
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_market_stats (
            date        TEXT PRIMARY KEY,
            limit_up    INTEGER,
            limit_down  INTEGER,
            up          INTEGER,
            down        INTEGER,
            flat        INTEGER,
            updated_at  TEXT
        )
    """)

    # 涨停个股明细
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_limit_stocks (
            date            TEXT,
            code            TEXT,
            name            TEXT,
            em_amount       REAL,
            turnover_rate   REAL,
            first_limit_up_time INTEGER,
            limit_up_type   TEXT,
            reason_type     TEXT,
            change_rate     REAL,
            PRIMARY KEY (date, code)
        )
    """)

    conn.commit()
    return conn


# ---------- 市场统计缓存 ----------

def cache_get_market_stats(date_str):
    """从缓存读取市场统计，返回 dict 或 None"""
    try:
        conn = _get_conn()
        row = conn.execute(
            "SELECT * FROM daily_market_stats WHERE date=?", (date_str,)
        ).fetchone()
        conn.close()
        if row:
            return {
                'limit_up':   row['limit_up'],
                'limit_down': row['limit_down'],
                'up':         row['up'],
                'down':       row['down'],
                'flat':       row['flat'],
            }
    except Exception as e:
        print(f"  [缓存] 读取市场统计失败: {e}")
    return None


def cache_set_market_stats(date_str, stats):
    """写入/更新市场统计缓存"""
    try:
        conn = _get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO daily_market_stats
                (date, limit_up, limit_down, up, down, flat, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            date_str,
            stats.get('limit_up'),
            stats.get('limit_down'),
            stats.get('up'),
            stats.get('down'),
            stats.get('flat'),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  [缓存] 写入市场统计失败: {e}")


# ---------- 涨停个股缓存 ----------

def cache_get_limit_stocks(date_str):
    """从缓存读取涨停个股列表，返回 list[dict] 或 None"""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM daily_limit_stocks WHERE date=? ORDER BY em_amount DESC",
            (date_str,)
        ).fetchall()
        conn.close()
        if rows:
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"  [缓存] 读取涨停个股失败: {e}")
    return None


def cache_set_limit_stocks(date_str, stocks):
    """写入/更新涨停个股缓存"""
    try:
        conn = _get_conn()
        # 先清除该日旧数据
        conn.execute("DELETE FROM daily_limit_stocks WHERE date=?", (date_str,))
        for s in stocks:
            conn.execute("""
                INSERT INTO daily_limit_stocks
                    (date, code, name, em_amount, turnover_rate,
                     first_limit_up_time, limit_up_type, reason_type, change_rate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date_str,
                s.get('code', ''),
                s.get('name', ''),
                s.get('em_amount', 0),
                s.get('turnover_rate', 0),
                s.get('first_limit_up_time', 0),
                s.get('limit_up_type', ''),
                s.get('reason_type', ''),
                s.get('change_rate', 0),
            ))
        conn.commit()
        conn.close()
        print(f"  [缓存] 已保存 {len(stocks)} 条涨停记录 ({date_str})")
    except Exception as e:
        print(f"  [缓存] 写入涨停个股失败: {e}")


# ==================== 历史涨跌家数统计（东方财富全量K线方案） ====================

def _date_offset(date_str, days):
    """日期偏移，date_str格式'YYYYMMDD'"""
    dt = datetime.strptime(date_str, "%Y%m%d")
    return (dt + timedelta(days=int(days))).strftime("%Y%m%d")


def _get_stock_change_kline(code, date_str):
    """通过东方财富历史K线判断单只股票涨跌
    Returns: 1=涨, -1=跌, 0=平, None=无数据/停牌
    """
    market = "1" if code.startswith(("6", "9")) else "0"
    secid = f"{market}.{code}"
    beg = _date_offset(date_str, -7)  # 多取几天确保覆盖前一个交易日
    params = {
        "secid": secid,
        "fields1": "f1",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",
        "fqt": "0",
        "beg": beg,
        "end": date_str,
    }
    try:
        r = EM_SESSION.get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                           params=params, timeout=5)
        data = r.json().get('data')
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
        if close_p > prev_close:
            return 1
        elif close_p < prev_close:
            return -1
        else:
            return 0
    except:
        return None


def get_historical_up_down(date_str, stock_list=None, max_workers=50):
    """通过东方财富全量K线统计指定日期的涨跌家数
    
    Args:
        date_str: 日期字符串，格式'YYYYMMDD'
        stock_list: 可选，股票代码列表。为None则自动获取全量A股。
        max_workers: 并发线程数
    
    Returns:
        dict: {'up': 上涨家数, 'down': 下跌家数, 'flat': 平盘家数, 'suspended': 停牌家数}
        或 None 表示获取失败
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    # 获取股票列表
    if stock_list is None:
        all_stocks = []
        for page in range(1, 60):
            url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=100"
                   f"&np=1&fltt=2&invt=2&fid=f12"
                   f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12,f14")
            try:
                r = EM_SESSION.get(url, timeout=10)
                d = r.json()
                diff = d['data']['diff']
                if not diff:
                    break
                all_stocks.extend(diff)
                if len(all_stocks) >= d['data']['total']:
                    break
            except:
                break
        codes = [s['f12'] for s in all_stocks]
    else:
        codes = stock_list
    
    if not codes:
        return None
    
    up = down = flat = suspended = 0
    processed = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_get_stock_change_kline, code, date_str): code
                   for code in codes}
        for future in as_completed(futures):
            result = future.result()
            processed += 1
            if result == 1:
                up += 1
            elif result == -1:
                down += 1
            elif result == 0:
                flat += 1
            else:
                suspended += 1
            if processed % 1000 == 0:
                print(f"  涨跌统计进度: {processed}/{len(codes)} 涨:{up} 跌:{down}")
    
    print(f"  涨跌统计完成: 上涨={up} 下跌={down} 平盘={flat} 停牌={suspended}")
    return {'up': up, 'down': down, 'flat': flat, 'suspended': suspended}


# ==================== 数据获取 ====================

def get_ths_limitup_stats(date_str=None):
    """从同花顺涨停池接口获取涨停/跌停统计（支持历史日期）
    
    Args:
        date_str: 日期字符串，格式'YYYYMMDD'，如'20260327'。默认今天。
    
    Returns:
        dict: {
            'limit_up': 涨停家数,
            'limit_down': 跌停家数,
            'limit_up_open': 涨停开板数,
        }
    """
    if date_str is None:
        date_str = TODAY
    
    url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    params = {
        "page": 1,
        "limit": 1,
        "field": "199112,10,9001",
        "filter": "HS,GEM2STAR",
        "order_field": "330323",
        "order_type": "0",
        "date": date_str,
        "_": int(time.time() * 1000)
    }
    try:
        response = THS_SESSION.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if data.get('status_code') != 0:
            return None
        
        result_data = data.get('data', {})
        luc = result_data.get('limit_up_count', {}).get('today', {})
        ldc = result_data.get('limit_down_count', {}).get('today', {})
        
        return {
            'limit_up': luc.get('num', 0) or 0,
            'limit_down': ldc.get('num', 0) or 0,
            'limit_up_open': luc.get('open_num', 0) or 0,
        }
    except Exception as e:
        print(f"  获取涨停统计失败({date_str}): {str(e)}")
        return None


def get_ths_limitup_all(date_str=None):
    """同花顺涨停池接口，翻页获取全部涨停个股（支持历史日期）"""
    if date_str is None:
        date_str = TODAY
    
    base_url = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
    fields = "199112,10,9001,330323,330324,330325,1968584,3475914,9002,330335,1"

    all_stocks = []
    page = 1
    total_pages = 1

    while page <= total_pages:
        params = {
            "page": page,
            "limit": 200,
            "field": fields,
            "filter": "HS,GEM2STAR",
            "order_field": "330323",
            "order_type": "0",
            "date": date_str,
            "_": int(time.time() * 1000)
        }

        try:
            response = THS_SESSION.get(base_url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            if data.get('status_code') != 0:
                print(f"API错误: {data.get('status_code')}")
                break

            result = data.get('data', {})
            page_info = result.get('page', {})
            total = page_info.get('total', 0)
            stocks = result.get('info', [])
            total_pages = (total + 199) // 200

            for s in stocks:
                # 历史日期的成交额需要后续通过 get_historical_amount_batch 填充
                if 'em_amount' not in s:
                    s['em_amount'] = 0

            all_stocks.extend(stocks)
            print(f"  第{page}/{total_pages}页，获取{len(stocks)}只")

            page += 1
            time.sleep(0.5)

        except Exception as e:
            print(f"获取第{page}页失败: {str(e)}")
            break

    return all_stocks


def get_em_amount_batch(stocks):
    """从东方财富批量获取成交额"""
    if not stocks:
        return

    secids = []
    code_to_stock = {}
    for s in stocks:
        code = s.get('code', '')
        market = "1" if code.startswith(("6", "9")) else "0"
        secid = f"{market}.{code}"
        secids.append(secid)
        code_to_stock[code] = s

    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f2,f3,f4,f6,f12,f14",
        "secids": ",".join(secids),
        "fltt": "2",
    }

    try:
        response = EM_SESSION.get(url, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        if data.get('rc') == 0 and 'data' in data:
            diff_list = data['data'].get('diff', [])
            for item in diff_list:
                code = item.get('f12', '')
                amount = item.get('f6', 0)
                change_pct = item.get('f3', 0)
                if code in code_to_stock:
                    code_to_stock[code]['em_amount'] = amount or 0
                    if not code_to_stock[code].get('change_rate'):
                        code_to_stock[code]['change_rate'] = change_pct / 100 if change_pct else 0
            print(f"  成功获取 {len(diff_list)}/{len(stocks)} 只个股行情")
        else:
            print(f"  批量接口返回异常: rc={data.get('rc')}")
    except Exception as e:
            print(f"  批量获取成交额失败: {str(e)}")


def get_historical_amount_batch(stocks, date_str):
    """通过东方财富K线接口批量获取历史个股成交额

    Args:
        stocks: 涨停个股列表
        date_str: 历史日期 YYYYMMDD
    """
    if not stocks:
        return

    # 构建 secids
    secids = []
    code_to_stock = {}
    for s in stocks:
        code = s.get('code', '')
        market = "1" if code.startswith(("6", "9")) else "0"
        secid = f"{market}.{code}"
        secids.append(secid)
        code_to_stock[code] = s

    # 分批请求（每批50只）
    batch_size = 50
    total = len(secids)
    success = 0

    for i in range(0, total, batch_size):
        batch = secids[i:i + batch_size]
        secid_str = ",".join(batch)

        # beg/end 日期（多取几天确保覆盖）
        beg = _date_offset(date_str, -5)

        params = {
            "secid": secid_str,
            "fields1": "f1",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "klt": "101",
            "fqt": "0",
            "beg": beg,
            "end": date_str,
        }

        try:
            # 注意：push2his的批量接口用 ; 分隔多个secid
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            r = EM_SESSION.get(url, params=params, timeout=15)
            data = r.json()

            if data.get('rc') == 0 and data.get('data'):
                d = data['data']
                klines = d.get('klines', [])
                # 找到目标日期的K线
                target_kline = None
                for kline in klines:
                    parts = kline.split(',')
                    k_date = parts[0].replace('-', '')
                    if k_date == date_str:
                        target_kline = parts
                        break

                if target_kline:
                    # f56=成交额(元), 但这里是单股
                    # 这个接口只能单股查，批量需要逐个请求
                    pass

        except Exception as e:
            pass

    # push2his不支持真正的批量，改用逐个请求但用线程池加速
    import concurrent.futures

    def fetch_one(code):
        s = code_to_stock.get(code)
        if not s:
            return code, 0
        market = "1" if code.startswith(("6", "9")) else "0"
        secid = f"{market}.{code}"
        beg = _date_offset(date_str, -5)
        try:
            r = EM_SESSION.get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "secid": secid,
                    "fields1": "f1",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57",
                    "klt": "101", "fqt": "0",
                    "beg": beg, "end": date_str,
                },
                timeout=5
            )
            d = r.json().get('data')
            if d and d.get('klines'):
                for kline in d['klines']:
                    parts = kline.split(',')
                    k_date = parts[0].replace('-', '')
                    if k_date == date_str:
                        # parts: 日期,开盘,收盘,最高,最低,成交量,成交额
                        return code, float(parts[6]) if len(parts) > 6 else 0
            return code, 0
        except:
            return code, 0

    print(f"  批量获取历史成交额（{len(code_to_stock)}只）...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_one, code): code for code in code_to_stock}
        done_count = 0
        for future in concurrent.futures.as_completed(futures):
            code, amount = future.result()
            if code in code_to_stock and amount > 0:
                code_to_stock[code]['em_amount'] = amount
                done_count += 1
                if done_count <= 5 or done_count == len(code_to_stock):
                    print(f"    {code}: {amount/1e8:.2f}亿")

    print(f"  成功获取 {done_count}/{len(code_to_stock)} 只成交额")


def get_market_stats():
    """从东方财富获取全市场涨跌统计（上涨/下跌/涨停/跌停家数）
    
    注意：f104(上涨家数)/f105(下跌家数)仅支持实时数据，收盘后仍有效。
    f106(涨停)/f152(跌停)也是实时的。
    """
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "fields": "f104,f105,f106,f152",
        "secids": "1.000001",
        "fltt": "2",
    }
    try:
        response = EM_SESSION.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get('rc') == 0 and 'data' in data:
            diff = data['data'].get('diff', [])
            if diff:
                item = diff[0]
                result = {
                    'up': item.get('f104', 0) or 0,
                    'down': item.get('f105', 0) or 0,
                    'limit_up': item.get('f106', 0) or 0,
                    'limit_down': item.get('f152', 0) or 0,
                }
                # 补充同花顺涨停/跌停数据（更准确，区分涨停开板）
                ths_stats = get_ths_limitup_stats()
                if ths_stats:
                    result['limit_up'] = ths_stats['limit_up']
                    result['limit_down'] = ths_stats['limit_down']
                return result
    except Exception as e:
        print(f"  获取市场统计失败: {str(e)}")
    return None


# ==================== 涨停原因概念合并 ====================

CONCEPT_MERGE = {
    "锂电池": ["盐湖提锂", "固态电池", "锂矿", "锂电", "磷酸铁锂", "碳酸锂", "六氟磷酸锂",
               "三元锂电", "锂电回收", "钴镍", "电池材料", "钠离子电池", "锂盐", "锂辉石",
               "锂产品", "锂资源", "电解液", "隔膜", "铜箔", "铝箔", "负极材料", "正极材料",
               "锂电设备", "电池结构件", "固态电解质", "锂枝晶", "半固态电池", "麒麟电池",
               "锰铁锂", "富锂锰基", "钴酸锂", "氢氧化锂", "锂云母"],
    "充电桩": ["充电桩", "充电设备", "充电站", "换电", "超级充电", "液冷超充", "充电模块"],
    "新能源车": ["新能源车", "新能源汽车", "整车", "汽车零部件", "特斯拉", "比亚迪",
                 "理想汽车", "小米汽车", "华为汽车", "问界", "智界", "蔚来", "小鹏",
                 "智能驾驶", "自动驾驶", "车联网", "汽车电子", "线控底盘", "空气悬挂",
                 "一体化压铸", "轻量化", "毫米波雷达", "激光雷达", "车载镜头", "HUD",
                 "智能座舱", "线束", "汽车热管理", "汽车芯片", "车规芯片", "胎压监测",
                 "电机电控", "混动", "氢燃料电池", "燃料电池", "重卡", "商用车", "乘用车"],
    "光伏": ["光伏", "太阳能", "光伏组件", "光伏玻璃", "光伏胶膜", "光伏硅片",
             "光伏支架", "HJT电池", "TOPCon", "钙钛矿", "异质结", "BC电池",
             "硅料", "多晶硅", "单晶硅", "切片", "电池片", "背板", "接线盒", "逆变器"],
    "储能": ["储能", "储能电池", "储能系统", "储能 PCS", "储能电站", "压缩空气储能",
             "熔盐储能", "钒液流电池", "全钒液流电池", "飞轮储能", "抽水蓄能",
             "液流电池", "锌离子电池", "铁锂电池"],
    "风电": ["风电", "风力发电", "海上风电", "风机", "风电塔筒", "风电叶片", "风电轴承",
             "风电铸件", "风电电缆"],
    "氢能源": ["氢能源", "氢能", "制氢", "储氢", "加氢站", "氢燃料电池", "质子交换膜",
               "电解槽", "绿氢", "氢气", "氢能装备"],
    "AI应用": ["人工智能", "AI", "AIGC", "ChatGPT", "大模型", "大语言模型", "LLM",
               "Chatbot", "对话AI", "生成式AI", "智能体", "Agent", "AI Agent", "AI助手",
               "AI聊天", "文心一言", "通义千问", "Kimi", "豆包", "Sora", "Midjourney",
               "AI搜索", "AI教育", "AI医疗", "AI制药", "AI设计", "AI办公", "AI客服",
               "AI投资", "AI金融", "AI法律", "AI营销", "AI文案", "AI视频"],
    "算力": ["算力", "AI算力", "GPU", "CPU", "芯片", "服务器", "光模块",
             "数据中心", "IDC", "云计算", "液冷服务器", "算力租赁", "绿色算力",
             "参股算力", "智算中心", "超算", "国产芯片", "AI芯片", "存储芯片",
             "模拟芯片", "FPGA", "ASIC", "半导体", "先进封装", "Chiplet", "EDA",
             "光刻", "光刻胶", "CMP", "电子气体", "溅射靶材", "硅片", "晶圆",
             "封测", "晶圆代工", "存储", "DRAM", "NAND"],
    "通信/5G": ["5G", "6G", "通信", "光纤", "光缆", "光纤光缆", "通信设备", "基站",
                "射频", "天线", "滤波器", "物联网", "卫星通信", "卫星导航", "北斗",
                "华为产业链", "华为概念", "鸿蒙", "欧拉", "昇腾", "鲲鹏", "5.5G"],
    "数据要素": ["数据要素", "数据确权", "数据安全", "数据交易", "大数据", "数字经济",
                 "智慧城市", "智慧政务", "智慧交通", "智慧医疗", "智慧教育", "数字孪生",
                 "区块链", "Web3", "数字货币", "数字人民币", "DCEP", "跨境支付"],
    "机器人": ["机器人", "人形机器人", "工业机器人", "服务机器人", "协作机器人", "减速器",
               "伺服电机", "控制器", "传感器", "机器视觉", "工控", "激光雷达", "力传感器",
               "谐波减速器", "RV减速器", "执行器", "灵巧手", "无人机", "eVTOL", "飞行汽车"],
    "创新药": ["创新药", "CXO", "CRO", "CDMO", "生物药", "单抗", "双抗", "ADC",
               "GLP-1", "减肥药", "mRNA", "基因治疗", "细胞治疗", "CAR-T", "核酸药物",
               "小分子", "靶向药", "化学药", "仿制药", "原料药", "中间体",
               "疫苗", "血液制品", "中药", "中药配方颗粒"],
    "医疗器械": ["医疗器械", "高值耗材", "低值耗材", "医疗影像", "医疗设备", "监护仪",
                 "呼吸机", "内窥镜", "骨科植入", "心血管介入", "眼科", "口腔", "IVD",
                 "体外诊断", "基因测序", "分子诊断", "POCT", "CGM"],
    "消费电子": ["消费电子", "苹果产业链", "苹果概念", "iPhone", "iPad", "MR", "VR",
                 "AR", "XR", "头显", "TWS耳机", "智能手表", "智能手环", "可穿戴",
                 "面板", "OLED", "MiniLED", "MicroLED", "LCD", "柔性屏", "折叠屏",
                 "触摸屏", "盖板玻璃", "光学镜头", "CIS"],
    "食品饮料": ["白酒", "啤酒", "红酒", "乳制品", "奶粉", "调味品", "酱油", "醋",
                 "休闲食品", "速冻食品", "预制菜", "饮料", "茶饮", "咖啡", "火锅",
                 "餐饮", "烘焙", "卤味", "肉制品", "保健品", "益生菌"],
    "家电": ["家电", "白色家电", "黑色家电", "小家电", "厨电", "智能家电", "家电出海",
             "空调", "冰箱", "洗衣机", "电视", "扫地机器人", "集成灶", "净水器",
             "微波炉", "电饭煲", "空气炸锅"],
    "有色金属": ["锂矿", "铜", "铝", "锌", "铅", "镍", "钴", "锡", "钨", "钼",
                 "稀土", "黄金", "白银", "铂", "钯", "锑", "铀", "钛", "锰",
                 "铬", "镁", "硅", "锗", "铟", "镓", "碳酸锂", "氢氧化锂",
                 "氧化铝", "电解铝", "铜箔", "铅锌矿", "铜矿", "金矿"],
    "化工": ["化工", "纯碱", "PVC", "MDI", "TDI", "钛白粉", "化肥", "农药",
             "草甘膦", "磷化工", "氟化工", "氯碱", "烧碱", "炭黑", "炭黑龙头",
             "染料", "涂料", "聚氨酯", "环氧丙烷", "丙烯酸", "有机硅", "甲酸",
             "溴素", "溴素涨价", "己二酸", "DMC", "BDO", "PTA", "涤纶",
             "锦纶", "氨纶", "粘胶", "碳纤维", "玻璃纤维", "玻纤"],
    "煤炭": ["煤炭", "动力煤", "焦煤", "焦炭", "煤化工", "煤制烯烃", "煤制乙二醇"],
    "钢铁": ["钢铁", "特钢", "不锈钢", "板材", "线材", "螺纹钢", "钢管", "钢构"],
    "房地产": ["房地产", "地产", "地产销售大增", "物业管理", "物业服务", "城中村",
               "保障房", "廉租房", "REITs", "棚改", "旧改", "城中村改造", "保交楼",
               "二手房", "新房", "土地"],
    "军工": ["军工", "国防军工", "军民融合", "导弹", "战斗机", "军用飞机", "直升机",
             "航空发动机", "燃气轮机", "军工电子", "军工信息化", "雷达", "红外",
             "军工新材料", "高温合金", "钛合金", "航天材料",
             "商业航天", "航天", "卫星", "火箭", "飞船", "空间站"],
    "电力": ["电力", "绿色电力", "绿电", "绿电储备", "火电", "水电", "核电",
             "光伏发电", "风力发电", "抽水蓄能", "特高压", "电网", "智能电网",
             "配电", "变电", "输配电", "电力设备", "电气设备", "变压器", "开关",
             "继电器", "断路器", "电缆", "电线", "电气"],
    "基建": ["基建", "建筑", "建材", "水泥", "混凝土", "工程机械", "挖掘机",
             "起重机", "搅拌站", "管材", "管道", "钢结构", "装配式建筑",
             "地下管网", "水利", "水利建设", "交建", "路桥", "隧道"],
    "金融": ["券商", "证券", "银行", "保险", "信托", "多元金融", "金融科技",
             "数字货币", "区块链金融", "AMC", "互金", "金融街"],
    "乡村振兴": ["乡村振兴", "农业", "种业", "种子", "化肥", "农药", "农机",
                 "粮食安全", "猪", "猪肉", "养殖", "饲料", "渔业", "禽类",
                 "农产品", "大豆", "玉米", "棉花", "糖"],
    "体育": ["体育", "体育产业", "足球", "足球概念", "健身器材", "电竞", "奥运会",
             "亚运会", "冬奥会", "体育用品"],
    "旅游酒店": ["旅游", "酒店", "民宿", "免税", "免税店", "景区", "在线旅游",
                 "航空", "机场", "高铁", "铁路", "公路", "港口", "航运"],
    "环保": ["环保", "污水处理", "垃圾处理", "固废处理", "大气治理", "水治理",
             "环境治理", "节能", "碳中和", "碳交易", "碳汇", "清洁能源", "再生资源",
             "动力电池回收", "电池回收"],
    "传媒游戏": ["传媒", "游戏", "短视频", "直播", "影视", "影视动漫", "出版",
                 "教育", "在线教育", "职业教育", "知识付费", "数字阅读",
                 "NFT", "数字藏品", "元宇宙", "虚拟人"],
    "纺织服装": ["纺织", "服装", "鞋帽", "棉花", "棉纺", "印染", "皮革", "箱包"],
    "造纸": ["造纸", "纸", "包装", "包装印刷", "纸包装", "塑料包装"],
}


def _build_concept_index():
    """构建概念合并索引：长关键词优先匹配"""
    index = []
    for concept, keywords in CONCEPT_MERGE.items():
        for kw in keywords:
            index.append((kw, concept))
    index.sort(key=lambda x: len(x[0]), reverse=True)
    return index


_CONCEPT_INDEX = _build_concept_index()


def merge_reason_tag(tag):
    """将细碎标签合并为大概念，未匹配则保留原名"""
    tag_lower = tag.lower()
    for kw, concept in _CONCEPT_INDEX:
        if kw.lower() in tag_lower:
            return concept
    return tag


def classify_by_reason(stocks):
    """按涨停原因（合并后的大概念）分类，保留每只股的原始原因标签"""
    reason_map = defaultdict(list)
    for s in stocks:
        raw_reasons = s.get('reason_type', '') or ''
        if not raw_reasons:
            reason_map['暂无数据'].append(s)
            continue

        # 收集该股票所有合并后的概念（去重）
        concepts = set()
        for tag in raw_reasons.split("+"):
            tag = tag.strip()
            if tag:
                concepts.add(merge_reason_tag(tag))

        for concept in concepts:
            reason_map[concept].append(s)

    sorted_reasons = sorted(reason_map.items(), key=lambda x: len(x[1]), reverse=True)
    result = []
    for reason, r_stocks in sorted_reasons:
        total_amount = sum([s.get('em_amount', 0) for s in r_stocks]) / 1e8
        result.append((reason, r_stocks, len(r_stocks), total_amount))
    return result


# ==================== 工具函数 ====================

def fmt_amount(val):
    if val is None or val == 0:
        return "-"
    try:
        val = float(val)
        if val >= 1e8:
            return f"{val/1e8:.2f}亿"
        elif val >= 1e4:
            return f"{val/1e4:.2f}万"
        else:
            return f"{val:.0f}元"
    except:
        return str(val)


def limit_up_type_badge(lt_type):
    color_map = {
        "换手板": ("#ff4d4f", "#fff1f0"),
        "封板": ("#1890ff", "#e6f7ff"),
        "连板": ("#ff7a00", "#fff7e6"),
        "一字板": ("#722ed1", "#f9f0ff"),
        "T字板": ("#13c2c2", "#e6fffb"),
        "开板": ("#52c41a", "#f6ffed"),
    }
    for k, (text_color, bg_color) in color_map.items():
        if k in str(lt_type):
            return f'<span style="color:{text_color};background:{bg_color};padding:1px 6px;border-radius:3px;font-size:11px;font-weight:600">{lt_type}</span>'
    return f'<span style="color:#666;background:#f5f5f5;padding:1px 6px;border-radius:3px;font-size:11px">{lt_type or "-"}</span>'


def get_market(code):
    return "SH" if code.startswith(("6", "9")) else "SZ"


def format_timestamp(timestamp):
    if not timestamp:
        return "未知"
    try:
        ts = int(timestamp)
        dt = datetime.fromtimestamp(ts)
        return dt.strftime('%H:%M')
    except:
        return "未知"






# ==================== HTML报告生成 ====================

def generate_html_report(data):
    stocks = data.get('stocks', [])
    total = data.get('total', len(stocks))
    NOW_STR = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 统计
    total_amount = sum([s.get('em_amount', 0) for s in stocks]) / 1e8
    mkt_stats = data.get('market_stats') or {}

    # 概念分类
    reason_groups = classify_by_reason(stocks)

    slot_colors = ["#e60012", "#ff7a00", "#1890ff", "#722ed1", "#52c41a",
                   "#13c2c2", "#eb2f96", "#fa8c16", "#2f54eb", "#a0d911"]

    # ---- 概念分类卡片（含涨停原因列，可排序） ----
    reason_section_html = ""
    table_idx = 0
    for gi, (reason, r_stocks, r_count, r_amount) in enumerate(reason_groups):
        color = slot_colors[gi % len(slot_colors)]
        r_stocks_sorted = sorted(r_stocks, key=lambda x: x.get('em_amount', 0), reverse=True)

        r_rows = ""
        for i, s in enumerate(r_stocks_sorted):
            code = s.get('code', '')
            name = s.get('name', '')
            amount = s.get('em_amount', 0) or 0
            turnover = s.get('turnover_rate', 0)
            first_time = format_timestamp(s.get('first_limit_up_time'))
            first_time_ts = s.get('first_limit_up_time') or 0
            limit_type = s.get('limit_up_type', '')
            raw_reason = s.get('reason_type', '') or '-'

            row_bg = "#fff" if i % 2 == 0 else "#fafafa"
            mkt = get_market(code)
            em_link = f"https://quote.eastmoney.com/concept/{mkt.lower()}{code}.html"

            r_rows += f"""
            <tr style="background:{row_bg}" data-amount="{amount}" data-time="{first_time_ts}">
                <td style="text-align:center;color:#888;font-size:13px">{i+1}</td>
                <td>
                    <div style="font-weight:600;font-size:14px">
                        <a href="{em_link}" target="_blank" style="color:#1a1a1a;text-decoration:none">{name}</a>
                    </div>
                    <div style="font-size:12px;color:#999;margin-top:2px">{mkt}{code}</div>
                </td>
                <td class="col-amount" style="font-weight:700;color:#e60012;font-size:15px">{fmt_amount(amount)}</td>
                <td style="color:#666">{turnover:.2f}%</td>
                <td class="col-time" style="color:#1890ff;font-weight:600">{first_time}</td>
                <td>{limit_up_type_badge(limit_type)}</td>
                <td style="font-size:12px;color:#555;max-width:200px">{raw_reason}</td>
            </tr>"""

        reason_section_html += f"""
    <div class="card" style="margin-bottom:16px">
        <div class="card-head" style="background:{color}12;border-left:4px solid {color}">
            <span style="color:{color};font-weight:700;font-size:15px">{reason}</span>
            <span style="margin-left:auto;color:#888;font-size:12px">
                {r_count} 只 | 成交额 {r_amount:.2f}亿
            </span>
        </div>
        <div style="overflow-x:auto">
        <table class="sortable-table" style="width:100%;border-collapse:collapse">
            <thead>
                <tr style="background:#fafafa">
                    <th style="text-align:center;padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">序号</th>
                    <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">名称/代码</th>
                    <th class="sort-th" data-key="amount" data-dir="desc" style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0;cursor:pointer;user-select:none">成交额 <span class="sort-arrow">&#9660;</span></th>
                    <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">换手率</th>
                    <th class="sort-th" data-key="time" data-dir="asc" style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0;cursor:pointer;user-select:none">涨停时间 <span class="sort-arrow"></span></th>
                    <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">类型</th>
                    <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">涨停原因</th>
                </tr>
            </thead>
            <tbody>
{r_rows}
            </tbody>
        </table>
        </div>
    </div>"""

    # ---- 热门标签云 ----
    top_reasons = reason_groups[:15]
    tag_cloud = ""
    for i, (reason, _, count, _) in enumerate(top_reasons):
        size = max(12, min(22, 12 + count * 2))
        color = slot_colors[i % len(slot_colors)]
        tag_cloud += f'<span class="hot-tag" style="font-size:{size}px;color:{color};border-color:{color}40;background:{color}08">{reason} ({count})</span>'

    # ---- 组装HTML ----
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>涨停日报 - {datetime.now().strftime('%Y-%m-%d')}</title>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif; background:#f0f2f5; color:#333; }}
.wrap {{ max-width:1400px; margin:0 auto; padding:16px; }}

.header {{ background:linear-gradient(135deg,#e60012,#ff6b35); color:white; padding:20px 28px; border-radius:12px; margin-bottom:16px; box-shadow:0 4px 16px rgba(230,0,18,.3); }}
.header h1 {{ font-size:22px; font-weight:700; }}
.header .sub {{ font-size:13px; opacity:.85; margin-top:6px; }}

.stats {{ display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
.stat-card {{ background:white; border-radius:10px; padding:14px 20px; flex:1; min-width:120px; box-shadow:0 2px 8px rgba(0,0,0,.06); text-align:center; }}
.stat-card .num {{ font-size:28px; font-weight:700; color:#e60012; }}
.stat-card .label {{ font-size:13px; color:#888; margin-top:4px; }}

.card {{ background:white; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,.06); overflow:hidden; }}
.card-head {{ padding:14px 20px; border-bottom:1px solid #f0f0f0; font-size:15px; font-weight:600; display:flex; align-items:center; gap:8px; }}

table {{ width:100%; border-collapse:collapse; }}
thead tr {{ background:#fafafa; }}
th {{ background:#fafafa; padding:10px 14px; text-align:left; font-size:12px; color:#888; font-weight:600; border-bottom:2px solid #f0f0f0; white-space:nowrap; }}
td {{ padding:10px 14px; font-size:13px; border-bottom:1px solid #f5f5f5; vertical-align:middle; }}
tr:hover td {{ background:#fffef0 !important; transition:background .15s; }}

.hot-tag {{ display:inline-block; border:1px solid #d9d9d9; padding:4px 10px; border-radius:4px; margin:3px 4px 3px 0; font-weight:600; cursor:default; transition:transform .15s; }}
.hot-tag:hover {{ transform:translateY(-2px); }}

.tag-cloud-wrap {{ background:white; border-radius:12px; padding:20px 24px; margin-bottom:16px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}

.section-title {{ font-size:16px; font-weight:600; margin-bottom:12px; color:#333; }}

.note {{ text-align:center; padding:12px; color:#999; font-size:12px; border-top:1px solid #f5f5f5; }}

.sort-th:hover {{ color:#e60012 !important; }}
.sort-arrow {{ font-size:10px; margin-left:2px; opacity:.3; }}
.sort-th[data-dir="desc"] .sort-arrow::after {{ content:"\\25BC"; opacity:1; color:#e60012; }}
.sort-th[data-dir="asc"] .sort-arrow::after {{ content:"\\25B2"; opacity:1; color:#e60012; }}
.sort-th:not([data-dir]) .sort-arrow::after {{ content:"\\25B2\\25BC"; opacity:.2; }}

@media(max-width:768px) {{
    .stats {{ gap:8px; }}
    th, td {{ padding:8px 10px; font-size:12px; }}
    .stat-card .num {{ font-size:22px; }}
}}
</style>
</head>
<body>
<div class="wrap">
    <div class="header">
        <h1>涨停日报</h1>
        <div class="sub">数据来源：同花顺涨停池 + 东方财富行情 &nbsp;|&nbsp; 更新时间：{NOW_STR}</div>
    </div>

    <div class="stats">
        <div class="stat-card"><div class="num" style="color:#e60012">{mkt_stats.get('up', '-')}</div><div class="label">上涨家数</div></div>
        <div class="stat-card"><div class="num" style="color:#52c41a">{mkt_stats.get('down', '-')}</div><div class="label">下跌家数</div></div>
        <div class="stat-card"><div class="num" style="color:#ff4d4f">{mkt_stats.get('limit_up', '-')}</div><div class="label">涨停家数</div></div>
        <div class="stat-card"><div class="num" style="color:#52c41a">{mkt_stats.get('limit_down', '-')}</div><div class="label">跌停家数</div></div>
        <div class="stat-card"><div class="num">{total}</div><div class="label">涨停总数</div></div>
        <div class="stat-card"><div class="num" style="color:#52c41a;font-size:20px">{total_amount:.1f}<span style="font-size:13px">亿</span></div><div class="label">总成交额</div></div>
    </div>


    <!-- 热门涨停原因标签云 -->
    <div style="margin-bottom:16px">
        <h2 class="section-title">热门涨停概念</h2>
        <div class="tag-cloud-wrap">{tag_cloud}</div>
    </div>

    <!-- 按涨停原因分类 -->
    <div style="margin-bottom:20px">
        <h2 class="section-title">按概念分类详情</h2>
{reason_section_html}
    </div>

    <div class="note">
        数据来源：同花顺涨停池 + 东方财富批量行情 | 生成时间：{NOW_STR}
    </div>
</div>
<script>
document.querySelectorAll('.sort-th').forEach(function(th) {{
    th.addEventListener('click', function() {{
        var key = this.dataset.key;
        var tbody = this.closest('table').querySelector('tbody');
        var rows = Array.from(tbody.querySelectorAll('tr'));

        // toggle direction
        var dir = this.dataset.dir === 'desc' ? 'asc' : 'desc';
        this.dataset.dir = dir;
        // reset other sort-th in same table
        this.closest('thead').querySelectorAll('.sort-th').forEach(function(h) {{
            if (h !== th) h.dataset.dir = '';
        }});

        rows.sort(function(a, b) {{
            var va, vb;
            if (key === 'amount') {{
                va = parseFloat(a.dataset.amount) || 0;
                vb = parseFloat(b.dataset.amount) || 0;
            }} else if (key === 'time') {{
                va = parseInt(a.dataset.time) || 0;
                vb = parseInt(b.dataset.time) || 0;
            }}
            return dir === 'desc' ? vb - va : va - vb;
        }});

        rows.forEach(function(row, i) {{
            row.querySelector('td').textContent = i + 1;
            var bg = i % 2 === 0 ? '#fff' : '#fafafa';
            row.style.background = bg;
            tbody.appendChild(row);
        }});
    }});
}});
</script>
</body>
</html>"""
    return html


def generate_multi_day_report(all_days_data):
    """生成多天对比报告，整合到一个HTML页面，tab切换 + 折线图"""
    NOW_STR = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    slot_colors = ["#e60012", "#ff7a00", "#1890ff", "#722ed1", "#52c41a",
                   "#13c2c2", "#eb2f96", "#fa8c16", "#2f54eb", "#a0d911"]
    dates_list = [d['date'] for d in all_days_data]

    tab_buttons = ""
    tab_panels = ""
    
    # 收集折线图数据
    chart_dates = []
    chart_limit_up = []
    chart_limit_down = []
    chart_up = []
    chart_down = []
    
    for idx, day_data in enumerate(all_days_data):
        day_date = day_data['date']
        stocks = day_data['stocks']
        total = len(stocks)
        total_amount = sum([s.get('em_amount', 0) for s in stocks]) / 1e8
        reason_groups = classify_by_reason(stocks)
        active = "active" if idx == len(all_days_data) - 1 else ""
        
        # 市场统计
        mkt = day_data.get('market_stats') or {}
        limit_up_count = mkt.get('limit_up', total)
        limit_down_count = mkt.get('limit_down', 0)
        up_count = mkt.get('up')
        down_count = mkt.get('down')
        
        # 折线图数据
        # 将 YYYYMMDD 转为 MM-DD 格式用于图表显示
        display_date = day_date[5:] if len(day_date) == 8 else day_date
        chart_dates.append(display_date)
        chart_limit_up.append(limit_up_count)
        chart_limit_down.append(limit_down_count)
        chart_up.append(up_count)
        chart_down.append(down_count)

        # 概念分类卡片
        reason_section_html = ""
        for gi, (reason, r_stocks, r_count, r_amount) in enumerate(reason_groups):
            color = slot_colors[gi % len(slot_colors)]
            r_stocks_sorted = sorted(r_stocks, key=lambda x: x.get('em_amount', 0), reverse=True)

            r_rows = ""
            for i, s in enumerate(r_stocks_sorted):
                code = s.get('code', '')
                name = s.get('name', '')
                amount = s.get('em_amount', 0) or 0
                turnover = s.get('turnover_rate', 0)
                first_time = format_timestamp(s.get('first_limit_up_time'))
                first_time_ts = s.get('first_limit_up_time') or 0
                limit_type = s.get('limit_up_type', '')
                raw_reason = s.get('reason_type', '') or '-'

                row_bg = "#fff" if i % 2 == 0 else "#fafafa"
                mkt_code = get_market(code)
                em_link = f"https://quote.eastmoney.com/concept/{mkt_code.lower()}{code}.html"

                r_rows += f"""
                <tr style="background:{row_bg}" data-amount="{amount}" data-time="{first_time_ts}">
                    <td style="text-align:center;color:#888;font-size:13px">{i+1}</td>
                    <td>
                        <div style="font-weight:600;font-size:14px">
                            <a href="{em_link}" target="_blank" style="color:#1a1a1a;text-decoration:none">{name}</a>
                        </div>
                        <div style="font-size:12px;color:#999;margin-top:2px">{mkt_code}{code}</div>
                    </td>
                    <td class="col-amount" style="font-weight:700;color:#e60012;font-size:15px">{fmt_amount(amount)}</td>
                    <td style="color:#666">{turnover:.2f}%</td>
                    <td class="col-time" style="color:#1890ff;font-weight:600">{first_time}</td>
                    <td>{limit_up_type_badge(limit_type)}</td>
                    <td style="font-size:12px;color:#555;max-width:200px">{raw_reason}</td>
                </tr>"""

            reason_section_html += f"""
        <div class="card" style="margin-bottom:16px">
            <div class="card-head" style="background:{color}12;border-left:4px solid {color}">
                <span style="color:{color};font-weight:700;font-size:15px">{reason}</span>
                <span style="margin-left:auto;color:#888;font-size:12px">{r_count} 只 | 成交额 {r_amount:.2f}亿</span>
            </div>
            <div style="overflow-x:auto">
            <table class="sortable-table" style="width:100%;border-collapse:collapse">
                <thead>
                    <tr style="background:#fafafa">
                        <th style="text-align:center;padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">序号</th>
                        <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">名称/代码</th>
                        <th class="sort-th" data-key="amount" data-dir="desc" style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0;cursor:pointer;user-select:none">成交额 <span class="sort-arrow"></span></th>
                        <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">换手率</th>
                        <th class="sort-th" data-key="time" data-dir="asc" style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0;cursor:pointer;user-select:none">涨停时间 <span class="sort-arrow"></span></th>
                        <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">类型</th>
                        <th style="padding:10px 14px;font-size:12px;color:#888;font-weight:600;border-bottom:2px solid #f0f0f0">涨停原因</th>
                    </tr>
                </thead>
                <tbody>
    {r_rows}
                </tbody>
            </table>
            </div>
        </div>"""

        # 热门标签云
        top_reasons = reason_groups[:10]
        tag_cloud = ""
        for i, (reason, _, count, _) in enumerate(top_reasons):
            size = max(12, min(20, 12 + count))
            color = slot_colors[i % len(slot_colors)]
            tag_cloud += f'<span class="hot-tag" style="font-size:{size}px;color:{color};border-color:{color}40;background:{color}08">{reason} ({count})</span>'

        # 涨跌家数显示（有数据显示数据，无数据提示历史不可用）
        up_display = str(up_count) if up_count is not None else '-'
        down_display = str(down_count) if down_count is not None else '-'
        up_note = '' if up_count is not None else '<div style="font-size:10px;color:#bbb;margin-top:2px">仅当日</div>'
        down_note = '' if down_count is not None else '<div style="font-size:10px;color:#bbb;margin-top:2px">仅当日</div>'

        # tab button (用 data-date 属性，避免 event.target 在 span 上失效)
        tab_buttons += f'<button class="tab-btn {active}" data-date="{day_date}" onclick="switchTab(this)">{day_date} <span style="font-size:11px;opacity:.7">{total}只</span></button>'

        tab_panels += f"""
    <div class="tab-panel" id="panel-{day_date}" style="display:{'block' if active else 'none'}">
        <div class="stats">
            <div class="stat-card"><div class="num" style="color:#e60012">{up_display}</div><div class="label">上涨家数</div>{up_note}</div>
            <div class="stat-card"><div class="num" style="color:#52c41a">{down_display}</div><div class="label">下跌家数</div>{down_note}</div>
            <div class="stat-card"><div class="num" style="color:#ff4d4f">{limit_up_count}</div><div class="label">涨停家数</div></div>
            <div class="stat-card"><div class="num" style="color:#13c2c2">{limit_down_count}</div><div class="label">跌停家数</div></div>
            <div class="stat-card"><div class="num">{total}</div><div class="label">涨停总数</div></div>
            <div class="stat-card"><div class="num" style="color:#52c41a;font-size:20px">{total_amount:.1f}<span style="font-size:13px">亿</span></div><div class="label">总成交额</div></div>
        </div>
        <div style="margin-bottom:16px">
            <h2 class="section-title">热门涨停概念</h2>
            <div class="tag-cloud-wrap">{tag_cloud}</div>
        </div>
        <div style="margin-bottom:20px">
            <h2 class="section-title">按概念分类详情</h2>
            {reason_section_html}
        </div>
    </div>"""

    # ============ 构建折线图JSON数据 ============
    import json as json_mod
    chart_json = json_mod.dumps({
        'dates': chart_dates,
        'limit_up': chart_limit_up,
        'limit_down': chart_limit_down,
        'up': chart_up,
        'down': chart_down,
    })

    # ============ 组装完整HTML ============
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>涨停日报对比 - {' / '.join(dates_list)}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif; background:#f0f2f5; color:#333; }}
.wrap {{ max-width:1400px; margin:0 auto; padding:16px; }}

.header {{ background:linear-gradient(135deg,#e60012,#ff6b35); color:white; padding:20px 28px; border-radius:12px; margin-bottom:16px; box-shadow:0 4px 16px rgba(230,0,18,.3); }}
.header h1 {{ font-size:22px; font-weight:700; }}
.header .sub {{ font-size:13px; opacity:.85; margin-top:6px; }}

/* Tab */
.tab-bar {{ display:flex; gap:4px; margin-bottom:0; flex-wrap:wrap; }}
.tab-btn {{ padding:10px 20px; border:1px solid #d9d9d9; background:white; border-radius:8px 8px 0 0; cursor:pointer; font-size:14px; font-weight:600; color:#666; transition:all .2s; border-bottom:none; }}
.tab-btn:hover {{ color:#e60012; border-color:#e60012; }}
.tab-btn.active {{ background:#e60012; color:white; border-color:#e60012; }}
.tab-content {{ background:white; border-radius:0 12px 12px 12px; padding:20px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}

.stats {{ display:flex; gap:12px; margin-bottom:16px; flex-wrap:wrap; }}
.stat-card {{ background:#f9f9f9; border-radius:10px; padding:14px 20px; flex:1; min-width:100px; box-shadow:0 1px 4px rgba(0,0,0,.04); text-align:center; }}
.stat-card .num {{ font-size:28px; font-weight:700; color:#e60012; }}
.stat-card .label {{ font-size:13px; color:#888; margin-top:4px; }}

.card {{ background:white; border-radius:12px; box-shadow:0 2px 8px rgba(0,0,0,.06); overflow:hidden; }}
.card-head {{ padding:14px 20px; border-bottom:1px solid #f0f0f0; font-size:15px; font-weight:600; display:flex; align-items:center; gap:8px; }}

table {{ width:100%; border-collapse:collapse; }}
thead tr {{ background:#fafafa; }}
th {{ background:#fafafa; padding:10px 14px; text-align:left; font-size:12px; color:#888; font-weight:600; border-bottom:2px solid #f0f0f0; white-space:nowrap; }}
td {{ padding:10px 14px; font-size:13px; border-bottom:1px solid #f5f5f5; vertical-align:middle; }}
tr:hover td {{ background:#fffef0 !important; transition:background .15s; }}

.hot-tag {{ display:inline-block; border:1px solid #d9d9d9; padding:4px 10px; border-radius:4px; margin:3px 4px 3px 0; font-weight:600; cursor:default; transition:transform .15s; }}
.hot-tag:hover {{ transform:translateY(-2px); }}
.tag-cloud-wrap {{ background:#f9f9f9; border-radius:12px; padding:20px 24px; margin-bottom:16px; }}
.section-title {{ font-size:16px; font-weight:600; margin-bottom:12px; color:#333; }}

.sort-th:hover {{ color:#e60012 !important; }}
.sort-arrow {{ font-size:10px; margin-left:2px; opacity:.3; }}
.sort-th[data-dir="desc"] .sort-arrow::after {{ content:"\\25BC"; opacity:1; color:#e60012; }}
.sort-th[data-dir="asc"] .sort-arrow::after {{ content:"\\25B2"; opacity:1; color:#e60012; }}

.chart-wrap {{ background:white; border-radius:12px; padding:20px 24px; box-shadow:0 2px 8px rgba(0,0,0,.06); }}

.note {{ text-align:center; padding:12px; color:#999; font-size:12px; border-top:1px solid #f5f5f5; margin-top:20px; }}

@media(max-width:768px) {{
    .stats {{ gap:8px; }}
    th, td {{ padding:8px 10px; font-size:12px; }}
    .stat-card .num {{ font-size:22px; }}
    .stat-card {{ min-width:80px; padding:10px 12px; }}
    #limitChart, #updownChart {{ height:260px; }}
}}
</style>
</head>
<body>
<div class="wrap">
    <div class="header">
        <h1>涨停日报对比</h1>
        <div class="sub">数据来源：同花顺涨停池 + 东方财富行情 &nbsp;|&nbsp; 生成时间：{NOW_STR}</div>
    </div>

    <!-- 趋势折线图 - 两张并列 -->
    <div style="display:flex; gap:16px; margin-bottom:16px; flex-wrap:wrap;">
        <div class="chart-wrap" style="flex:1; min-width:300px;">
            <h2 class="section-title">涨跌停家数趋势</h2>
            <div id="limitChart" style="width:100%; height:320px;"></div>
        </div>
        <div class="chart-wrap" style="flex:1; min-width:300px;">
            <h2 class="section-title">上涨/下跌家数趋势</h2>
            <div id="updownChart" style="width:100%; height:320px;"></div>
        </div>
    </div>

    <div class="tab-bar">{tab_buttons}</div>
    <div class="tab-content">{tab_panels}</div>

    <div class="note">数据来源：同花顺涨停池 + 东方财富批量行情 | 生成时间：{NOW_STR}</div>
</div>
<script>
// 图表数据
var chartData = {chart_json};

// 涨跌停趋势图
var limitDom = document.getElementById('limitChart');
var limitChart = echarts.init(limitDom);
limitChart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross' }} }},
    legend: {{ data: ['涨停家数', '跌停家数'], bottom: 0 }},
    grid: {{ left: '3%', right: '4%', bottom: '12%', top: '8%', containLabel: true }},
    xAxis: {{
        type: 'category',
        data: chartData.dates,
        axisLabel: {{ fontSize: 13, fontWeight: 'bold' }}
    }},
    yAxis: {{ type: 'value', name: '家数', axisLabel: {{ fontSize: 12 }} }},
    series: [
        {{
            name: '涨停家数',
            type: 'line',
            data: chartData.limit_up,
            smooth: true,
            symbol: 'circle',
            symbolSize: 10,
            lineStyle: {{ width: 3, color: '#e60012' }},
            itemStyle: {{ color: '#e60012' }},
            areaStyle: {{
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    {{ offset: 0, color: 'rgba(230,0,18,0.25)' }},
                    {{ offset: 1, color: 'rgba(230,0,18,0.02)' }}
                ])
            }}
        }},
        {{
            name: '跌停家数',
            type: 'line',
            data: chartData.limit_down,
            smooth: true,
            symbol: 'circle',
            symbolSize: 10,
            lineStyle: {{ width: 3, color: '#13c2c2' }},
            itemStyle: {{ color: '#13c2c2' }},
            areaStyle: {{
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    {{ offset: 0, color: 'rgba(19,194,194,0.2)' }},
                    {{ offset: 1, color: 'rgba(19,194,194,0.02)' }}
                ])
            }}
        }}
    ]
}});

// 涨跌幅趋势图
var updownDom = document.getElementById('updownChart');
var updownChart = echarts.init(updownDom);
updownChart.setOption({{
    tooltip: {{ trigger: 'axis', axisPointer: {{ type: 'cross' }} }},
    legend: {{ data: ['上涨家数', '下跌家数'], bottom: 0 }},
    grid: {{ left: '3%', right: '4%', bottom: '12%', top: '8%', containLabel: true }},
    xAxis: {{
        type: 'category',
        data: chartData.dates,
        axisLabel: {{ fontSize: 13, fontWeight: 'bold' }}
    }},
    yAxis: {{ type: 'value', name: '家数', axisLabel: {{ fontSize: 12 }} }},
    series: [
        {{
            name: '上涨家数',
            type: 'line',
            data: chartData.up,
            smooth: true,
            symbol: 'diamond',
            symbolSize: 10,
            lineStyle: {{ width: 3, color: '#e60012' }},
            itemStyle: {{ color: '#e60012', borderColor: '#fff', borderWidth: 2 }},
            areaStyle: {{
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    {{ offset: 0, color: 'rgba(230,0,18,0.18)' }},
                    {{ offset: 1, color: 'rgba(230,0,18,0.02)' }}
                ])
            }}
        }},
        {{
            name: '下跌家数',
            type: 'line',
            data: chartData.down,
            smooth: true,
            symbol: 'diamond',
            symbolSize: 10,
            lineStyle: {{ width: 3, color: '#52c41a' }},
            itemStyle: {{ color: '#52c41a', borderColor: '#fff', borderWidth: 2 }},
            areaStyle: {{
                color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                    {{ offset: 0, color: 'rgba(82,196,26,0.15)' }},
                    {{ offset: 1, color: 'rgba(82,196,26,0.02)' }}
                ])
            }}
        }}
    ]
}});

window.addEventListener('resize', function() {{
    limitChart.resize();
    updownChart.resize();
}});

// Tab切换
function switchTab(btn) {{
    var date = btn.dataset.date;
    document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('.tab-panel').forEach(function(p) {{ p.style.display = 'none'; }});
    btn.classList.add('active');
    document.getElementById('panel-' + date).style.display = 'block';
}}

// 表格排序
document.querySelectorAll('.sort-th').forEach(function(th) {{
    th.addEventListener('click', function() {{
        var key = this.dataset.key;
        var tbody = this.closest('table').querySelector('tbody');
        var rows = Array.from(tbody.querySelectorAll('tr'));
        var dir = this.dataset.dir === 'desc' ? 'asc' : 'desc';
        this.dataset.dir = dir;
        this.closest('thead').querySelectorAll('.sort-th').forEach(function(h) {{
            if (h !== th) h.dataset.dir = '';
        }});
        rows.sort(function(a, b) {{
            var va, vb;
            if (key === 'amount') {{ va = parseFloat(a.dataset.amount)||0; vb = parseFloat(b.dataset.amount)||0; }}
            else if (key === 'time') {{ va = parseInt(a.dataset.time)||0; vb = parseInt(b.dataset.time)||0; }}
            return dir === 'desc' ? vb - va : va - vb;
        }});
        rows.forEach(function(row, i) {{
            row.querySelector('td').textContent = i + 1;
            row.style.background = i % 2 === 0 ? '#fff' : '#fafafa';
            tbody.appendChild(row);
        }});
    }});
}});
</script>
</body>
</html>"""
    return html


# ==================== 多天对比报告生成 ====================

def _fetch_stock_codes():
    """获取全量A股代码列表"""
    stock_codes = []
    for page in range(1, 60):
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=100"
               f"&np=1&fltt=2&invt=2&fid=f12"
               f"&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23&fields=f12")
        try:
            r = EM_SESSION.get(url, timeout=10)
            d = r.json()
            diff = d['data']['diff']
            if not diff:
                break
            stock_codes.extend([s['f12'] for s in diff])
            if len(stock_codes) >= d['data']['total']:
                break
        except:
            break
    return stock_codes


def generate_multi_day_report_file(dates_list):
    """生成多天对比报告并保存文件（带SQLite缓存）
    
    Args:
        dates_list: 日期列表，格式 ['YYYYMMDD', ...]，如 ['20260325', '20260326', '20260327']
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    all_days_data = []
    
    # 判断哪些日期需要计算历史涨跌家数
    dates_need_kline = [d for d in dates_list
                        if d != TODAY and cache_get_market_stats(d) is None]
    
    # 只在确实需要时才获取全量股票列表
    stock_codes = []
    if dates_need_kline:
        print(f"  以下日期需要K线统计涨跌家数: {dates_need_kline}")
        print("  预获取A股股票列表...")
        stock_codes = _fetch_stock_codes()
        print(f"  共 {len(stock_codes)} 只股票")
    
    for i, date_str in enumerate(dates_list):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{len(dates_list)}] 处理日期: {date_str}")
        print(f"{'='*60}")
        
        # ---- 1. 涨停个股数据（先查缓存）----
        cached_stocks = cache_get_limit_stocks(date_str)
        if cached_stocks and date_str != TODAY:
            print(f"  [1/4] 涨停个股: 命中缓存 ({len(cached_stocks)} 只)")
            stocks = cached_stocks
        else:
            print("  [1/4] 获取涨停池数据...")
            stocks = get_ths_limitup_all(date_str)
            if not stocks:
                print(f"  未获取到涨停数据，跳过")
                continue
            print(f"  共 {len(stocks)} 只涨停股票")
            
            # 补充成交额
            if date_str == TODAY:
                print("  [2/4] 补充成交额数据...")
                get_em_amount_batch(stocks)
            else:
                print("  [2/4] 获取历史成交额...")
                get_historical_amount_batch(stocks, date_str)
            
            # 写入缓存（当天数据不缓存，避免数据不完整）
            if date_str != TODAY:
                cache_set_limit_stocks(date_str, stocks)
        
        # ---- 3. 市场统计（先查缓存）----
        cached_mkt = cache_get_market_stats(date_str)
        if cached_mkt and date_str != TODAY:
            print(f"  [3/4] 市场统计: 命中缓存 "
                  f"涨停:{cached_mkt.get('limit_up')} 跌停:{cached_mkt.get('limit_down')} "
                  f"上涨:{cached_mkt.get('up')} 下跌:{cached_mkt.get('down')}")
            market_stats = cached_mkt
        else:
            market_stats = {}
            
            # 涨停/跌停统计（同花顺）
            print("  [3/4] 获取涨停/跌停统计...")
            ths_stats = get_ths_limitup_stats(date_str)
            if ths_stats:
                market_stats['limit_up'] = ths_stats['limit_up']
                market_stats['limit_down'] = ths_stats['limit_down']
                print(f"  涨停:{ths_stats['limit_up']} 跌停:{ths_stats['limit_down']}")
            
            # 上涨/下跌家数
            print("  [4/4] 统计涨跌家数...")
            if date_str == TODAY:
                today_stats = get_market_stats()
                if today_stats:
                    market_stats['up'] = today_stats.get('up')
                    market_stats['down'] = today_stats.get('down')
                    print(f"  上涨:{market_stats['up']} 下跌:{market_stats['down']} (实时接口)")
            else:
                up_down = get_historical_up_down(date_str, stock_list=stock_codes)
                if up_down:
                    market_stats['up'] = up_down['up']
                    market_stats['down'] = up_down['down']
                    market_stats['flat'] = up_down.get('flat', 0)
                    print(f"  上涨:{up_down['up']} 下跌:{up_down['down']} (历史K线)")
            
            # 写入缓存
            if date_str != TODAY and market_stats:
                cache_set_market_stats(date_str, market_stats)
        
        # 按成交额排序
        stocks.sort(key=lambda x: x.get('em_amount', 0), reverse=True)
        
        all_days_data.append({
            'date': date_str,
            'stocks': stocks,
            'market_stats': market_stats,
        })
    
    if not all_days_data:
        print("\n未获取到任何日期的涨停数据")
        return
    
    # 生成HTML
    print(f"\n{'='*60}")
    print("生成多天对比报告...")
    print(f"{'='*60}")
    
    html = generate_multi_day_report(all_days_data)
    
    # 文件名
    first_date = all_days_data[0]['date']
    last_date = all_days_data[-1]['date']
    filename = f"涨停日报_对比_{first_date}-{last_date}.html"
    filepath = os.path.join(OUTPUT_DIR, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)
    
    print(f"报告已保存: {filepath}")
    
    # 发布到 GitHub Pages
    print("\n[发布] 正在推送到 GitHub Pages...")
    publish_to_github_pages([filepath])

    return filepath


# ==================== GitHub Pages 发布 ====================

def publish_to_github_pages(html_files):
    """将HTML报告发布到 GitHub Pages
    
    Args:
        html_files: 要发布的HTML文件路径列表
    """
    if not html_files:
        return
    
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 复制HTML文件到仓库根目录
    for src in html_files:
        if os.path.exists(src):
            import shutil
            shutil.copy2(src, os.path.join(repo_dir, os.path.basename(src)))
            print(f"  [发布] 复制: {os.path.basename(src)}")
    
    # 生成 index.html（指向最新的对比报告）
    latest = max(html_files, key=lambda f: os.path.basename(f))
    latest_name = os.path.basename(latest)
    index_html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="0;url={latest_name}">
<title>A股涨停日报</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; background: #1a1a2e; color: #eee; }}
a {{ color: #e60012; font-size: 1.2em; }}
</style>
</head>
<body>
<p>正在跳转到最新报告，如果没有自动跳转请 <a href="{latest_name}">点击这里</a></p>
</body>
</html>'''
    with open(os.path.join(repo_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(index_html)
    
    # git add 仅HTML文件 + commit + push
    try:
        # 先清理旧的报告HTML（只保留最新的，避免仓库越来越大）
        html_files_in_repo = [f for f in os.listdir(repo_dir) 
                             if f.endswith('.html') and f != 'index.html']
        for old_html in html_files_in_repo:
            if old_html != latest_name and old_html != 'index.html':
                os.remove(os.path.join(repo_dir, old_html))
                subprocess.run(['git', 'rm', old_html], cwd=repo_dir, capture_output=True, text=True)
        
        subprocess.run(['git', 'add', 'index.html', latest_name], cwd=repo_dir, capture_output=True, text=True)
        subprocess.run(['git', 'commit', '-m', f'更新涨停日报 {datetime.now().strftime("%Y-%m-%d")}'], 
                      cwd=repo_dir, capture_output=True, text=True)
        result = subprocess.run(['git', 'push', 'origin', 'master'], cwd=repo_dir, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            print(f"  [发布] 已推送到 GitHub Pages")
            print(f"  [访问] https://yanghuiysz.github.io/limitup-report/")
        else:
            print(f"  [发布] 推送失败: {result.stderr[:200]}")
    except Exception as e:
        print(f"  [发布] git操作异常: {e}")


# ==================== 主函数 ====================

def main():
    print("=" * 60)
    print("涨停日报自动生成脚本")
    print("=" * 60)
    print(f"运行时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. 获取同花顺涨停池数据
    print("[1/3] 正在获取同花顺涨停池数据...")
    ths_stocks = get_ths_limitup_all()

    if not ths_stocks:
        print("未获取到涨停数据")
        return

    print(f"  共获取 {len(ths_stocks)} 只涨停股票")

    # 2. 补充东方财富成交额
    print("[2/3] 正在补充东方财富成交额数据...")
    get_em_amount_batch(ths_stocks)

    # 3. 获取全市场涨跌统计
    print("[3/3] 正在获取全市场涨跌统计...")
    market_stats = get_market_stats()
    if market_stats:
        print(f"  上涨:{market_stats['up']} 下跌:{market_stats['down']} 涨停:{market_stats['limit_up']} 跌停:{market_stats['limit_down']}")
    else:
        print("  获取市场统计失败，将跳过")

    # 按成交额排序
    ths_stocks.sort(key=lambda x: x.get('em_amount', 0), reverse=True)

    # 4. 生成报告
    print("[4/4] 正在生成HTML报告...")
    data = {
        'date': TODAY_HYPHEN,
        'update_time': datetime.now().strftime('%H:%M'),
        'total': len(ths_stocks),
        'stocks': ths_stocks,
        'market_stats': market_stats,
    }

    html = generate_html_report(data)

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"报告已保存: {OUTPUT_FILE}")

    # 5. 当天收盘后（15:00后）写入缓存，供以后多天对比使用
    now_hour = datetime.now().hour
    if now_hour >= 15:
        print("[5/5] 收盘后写入今日缓存...")
        cache_set_limit_stocks(TODAY, ths_stocks)
        if market_stats:
            cache_set_market_stats(TODAY, market_stats)
        print("  [缓存] 今日数据已保存")

    # 打印摘要
    print("\n" + "=" * 60)
    print("TOP 10 涨停个股摘要")
    print("=" * 60)
    for i, item in enumerate(ths_stocks[:10], 1):
        print(f"{i:2d}. {item.get('name', ''):8s} ({item.get('code', '')})  "
              f"成交额:{item.get('em_amount', 0)/100000000:8.2f}亿  "
              f"换手:{item.get('turnover_rate', 0):5.2f}%  "
              f"涨停时间:{format_timestamp(item.get('first_limit_up_time'))}  "
              f"原因:{(item.get('reason_type', '') or '')[:30]}")

    reason_groups = classify_by_reason(ths_stocks)
    print("\n" + "=" * 60)
    print("热门涨停概念 TOP 5")
    print("=" * 60)
    for i, (reason, _, count, amount) in enumerate(reason_groups[:5], 1):
        print(f"{i}. {reason}  ({count}只, {amount:.2f}亿)")


    print("\n" + "=" * 60)
    print("完成!")
    print("=" * 60)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--multi':
        # 多天对比模式: python limitup_scheduled.py --multi 20260325 20260326 20260327
        dates = sys.argv[2:] if len(sys.argv) > 2 else ['20260325', '20260326', '20260327']
        filepath = generate_multi_day_report_file(dates)
        if filepath:
            print(f"\n完成! 报告: {filepath}")
    else:
        main()
