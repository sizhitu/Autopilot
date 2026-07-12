"""
真实数据源模块
=================================
支持：
  - 美股：Yahoo Finance v8 API（免费、无需API Key）
  - A股：新浪财经 K线接口（免费、无需API Key）
  - 指数：Yahoo Finance（^GSPC=标普500, ^DJI=道琼斯, ^IXIC=纳斯达克）
         新浪（sh000001=上证指数, sz399001=深证成指, sz399006=创业板指）
"""

import requests
import json
import re
import time
import random
from datetime import datetime, timedelta
import pandas as pd
import numpy as np


# ================================================================
#  系统性限流防护（熔断）
#  Yahoo 一旦被限流（429），进入冷却期；冷却期内所有美股请求直接走
#  Nasdaq 兜底，避免逐个代码反复空等 429 把限流拖得更久。
#  这是“系统性”处理，不需要为每只股票单独写逻辑。
# ================================================================
_YAHOO_COOLDOWN_UNTIL = 0.0
_YAHOO_COOLDOWN_SEC = 120  # 冷却时长（秒），每次命中 429 均顺延


def _yahoo_in_cooldown() -> bool:
    return time.time() < _YAHOO_COOLDOWN_UNTIL


def _trigger_yahoo_cooldown(seconds: int = _YAHOO_COOLDOWN_SEC) -> None:
    global _YAHOO_COOLDOWN_UNTIL
    _YAHOO_COOLDOWN_UNTIL = max(_YAHOO_COOLDOWN_UNTIL, time.time() + seconds)


def _clear_yahoo_cooldown() -> None:
    global _YAHOO_COOLDOWN_UNTIL
    _YAHOO_COOLDOWN_UNTIL = 0.0



# ================================================================
#  自选股人工维护元数据（行业分类 + 简短主营业务）
#  作用：美股/ETF/指数在沙箱环境常被 Yahoo 限流、且免费接口
#        普遍拿不到行业与简介，故对自选持仓做人工维护，保证
#        详情页与看板无论沙箱还是本机都能稳定展示。
#        不在本表中的代码（临时搜索）仍走动态数据源兜底。
# ================================================================
STOCK_META = {
    # 美股
    'OUST': {'industry': '激光雷达', 'desc': '研发制造高分辨率数字激光雷达，用于自动驾驶、工业与机器人感知。'},
    'FLY':  {'industry': '商业航天', 'desc': '商业航天公司，研制 Alpha 火箭与蓝幽灵月球着陆器，提供发射与太空运输服务。'},
    'SPCX': {'industry': '商业航天', 'desc': '私营航天与卫星互联网公司，运营猎鹰火箭与星链(Starlink)星座。'},
    'FIGR': {'industry': '金融科技', 'desc': '区块链金融科技公司，聚焦房屋净值贷款与数字资产借贷(Figure Markets)。'},
    'MU':   {'industry': '半导体·存储', 'desc': '全球领先 DRAM 与 NAND 闪存制造商，主营存储芯片。'},
    'VCX':  {'industry': '私募创投', 'desc': 'Fundrise 旗下创投基金，投资房地产科技与初创企业（注：本基金为非上市私募，行情取自对应代码）。'},
    'ETN':  {'industry': '电气设备', 'desc': '全球电力管理与流体动力设备供应商，服务数据中心、电网与工业。'},
    'GEV':  {'industry': '电力设备', 'desc': '通用电气分拆的能源业务，主营发电设备、风电与电网技术。'},
    'HIMS': {'industry': '数字医疗', 'desc': '远程医疗与直接面向消费者的健康品牌，主营处方药与保健订阅。'},
    'APP':  {'industry': '移动广告', 'desc': '移动广告变现与营销平台，AXON AI 引擎驱动广告投放。'},
    'ICE':  {'industry': '交易所', 'desc': '运营纽交所(NYSE)及多家清算所，主营交易所与数据服务。'},
    'SMH':  {'industry': '半导体ETF', 'desc': '跟踪 ICE 半导体指数，集中持有全球半导体龙头（设计/设备/制造）。'},
    'VGT':  {'industry': '信息技术ETF', 'desc': '跟踪 MSCI 美国可投资市场信息技术指数，覆盖软硬件与互联网。'},
    'JEPI': {'industry': '高股息策略ETF', 'desc': '通过期权叠加策略获取标普500成分股股息与收益。'},
    'GOOG': {'industry': '互联网·广告', 'desc': '谷歌母公司，主营搜索广告、YouTube、云与安卓生态。'},
    'LITE': {'industry': '光通信', 'desc': '光学元件与激光器供应商，服务光通信与工业市场。'},
    'ASTS': {'industry': '卫星通信', 'desc': '构建天基蜂窝宽带网络，直连普通手机的低轨卫星星座。'},
    'FCX':  {'industry': '有色金属·矿业', 'desc': '全球最大上市铜矿商之一，主营铜、金、钼开采。'},
    'ASM':  {'industry': '半导体设备', 'desc': '半导体沉积设备供应商，服务晶圆制造前端工艺。'},
    'EUV':  {'industry': '半导体设备', 'desc': '极紫外(EUV)光刻相关半导体设备标的。'},
    'WTI':  {'industry': '大宗商品·原油', 'desc': '跟踪美国 WTI 原油价格的商品标的（期货/ETF）。'},
    'AVGO': {'industry': '半导体·软件', 'desc': '通信与 AI 定制芯片(ASIC)及基础设施软件(VMware)供应商。'},
    'NVDA': {'industry': '半导体·AI芯片', 'desc': 'GPU 与加速计算龙头，主营数据中心 AI 芯片、游戏显卡与网络。'},
    'INTC': {'industry': '半导体·CPU', 'desc': '全球主要 CPU 与晶圆代工供应商，覆盖 PC、数据中心与代工。'},
    # A股 ETF（基金，东财 F10 无 ORG_PROFILE，故人工维护兜底）
    '159880': {'industry': '有色金属ETF', 'desc': '跟踪有色金属指数的场内基金，覆盖铜、铝、黄金等工业金属与贵金属。'},
    '518850': {'industry': '黄金ETF', 'desc': '跟踪国内黄金现货价格，配置黄金资产的场内交易型基金。'},
    '560710': {'industry': '船舶ETF', 'desc': '跟踪船舶制造产业指数，覆盖造船、海工与航运装备产业链。'},
    '159985': {'industry': '农产品ETF', 'desc': '跟踪大商所豆粕期货价格指数，挂钩农产品粕类商品的基金。'},
    '516150': {'industry': '纳指ETF', 'desc': '跟踪美国纳斯达克100指数，配置美股科技龙头的 QDII-ETF。'},
    '562500': {'industry': '机器人ETF', 'desc': '跟踪机器人产业指数，覆盖工业机器人、服务机器人及核心零部件。'},
    '513310': {'industry': '半导体ETF', 'desc': '跟踪中韩半导体指数，配置中韩两国半导体龙头公司。'},
    # 指数
    '000001': {'industry': '宽基指数', 'desc': '上交所编制的 A 股大盘基准指数，反映沪市整体表现。'},
    '399300': {'industry': '宽基指数', 'desc': '沪深两市市值与流动性居前的 300 只股票，A 股核心宽基指数。'},
}


# ================================================================
#  抗限流工具：多 UA 轮换 + K 线短时效缓存
# ================================================================
# 多 User-Agent 轮换，降低单一 UA 指纹被限流的概率
_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
]
def _rotate_ua() -> str:
    return random.choice(_USER_AGENTS)


# Stooq 指数代码映射（美股个股用 <代码>.us，指数单独映射）
_STOOQ_INDEX = {'^GSPC': '.inx', '^IXIC': '.ixic', '^DJI': '.dji', '^VIX': '^vix'}


# K线缓存：同一标的短时间内（看板刷新 + 详情页）只真实抓取一次，显著降低限流命中率
_KLINE_CACHE = {}
_KLINE_CACHE_TS = {}
_KLINE_TTL = 120  # 秒


def _kline_cache_get(key):
    ts = _KLINE_CACHE_TS.get(key)
    if ts and (time.time() - ts) < _KLINE_TTL:
        return _KLINE_CACHE.get(key)
    return None


def _kline_cache_set(key, df):
    _KLINE_CACHE[key] = df
    _KLINE_CACHE_TS[key] = time.time()


class DataFetcher:
    """统一数据获取接口"""

    # 美股常见代码映射
    US_STOCKS = {
        'AAPL': '苹果', 'MSFT': '微软', 'GOOGL': '谷歌', 'AMZN': '亚马逊',
        'NVDA': '英伟达', 'META': 'Meta', 'TSLA': '特斯拉', 'BRK-B': '伯克希尔',
        'JPM': '摩根大通', 'V': 'Visa', 'WMT': '沃尔玛', 'MA': '万事达',
        'JNJ': '强生', 'PG': '宝洁', 'UNH': '联合健康', 'HD': '家得宝',
        'DIS': '迪士尼', 'BABA': '阿里巴巴', 'NKE': '耐克', 'KO': '可口可乐',
        'PEP': '百事可乐', 'PFE': '辉瑞', 'MRK': '默克', 'ABBV': '艾伯维',
        'CVX': '雪佛龙', 'XOM': '埃克森美孚', 'BA': '波音', 'INTC': '英特尔',
        'CSCO': '思科', 'ORCL': '甲骨文', 'ADBE': 'Adobe', 'NFLX': '奈飞',
        'PYPL': 'PayPal', 'CRM': 'Salesforce', 'AMD': 'AMD', 'AVGO': '博通',
        'COST': '好市多', 'TMO': '赛默飞', 'ABT': '雅培', 'MCD': '麦当劳',
        'SBUX': '星巴克', 'GS': '高盛', 'MS': '摩根士丹利', 'BAC': '美国银行',
        'WFC': '富国银行', 'C': '花旗', 'GE': '通用电气', 'F': '福特',
        'GM': '通用汽车', 'IBM': 'IBM', 'QCOM': '高通', 'TXN': '德州仪器',
        'SHOP': 'Shopify', 'SQ': 'Block', 'ROKU': 'Roku', 'ZM': 'Zoom',
        'PLTR': 'Palantir', 'COIN': 'Coinbase', 'UBER': '优步', 'LYFT': 'Lyft',
        'ABNB': '爱彼迎', 'SNAP': 'Snap', 'PINS': 'Pinterest', 'SPOT': 'Spotify',
        'T': 'AT&T', 'VZ': 'Verizon', 'TMUS': 'T-Mobile', 'DISH': 'Dish',
        'X': '美国钢铁', 'NUE': '纽柯钢铁', 'CAT': '卡特彼勒', 'DE': '迪尔',
        'DUK': '杜克能源', 'SO': '南方电力', 'NEE': 'NextEra能源',
        'BIDU': '百度', 'JD': '京东', 'PDD': '拼多多', 'BILI': '哔哩哔哩',
        'NTES': '网易', 'TME': '腾讯音乐', 'WB': '微博', 'IQ': '爱奇艺',
        'NIO': '蔚来', 'XPEV': '小鹏', 'LI': '理想', 'BYDDY': '比亚迪ADR',
        'TCEHY': '腾讯ADR', 'BABA': '阿里巴巴', 'TCBJY': '工商银行ADR',
        # --- 用户关注 ---
        'OUST': 'Ouster', 'FLY': 'Firefly Aerospace', 'SPCX': 'SpaceX',
        'FIGR': 'Figure Technology', 'MU': '美光科技', 'VCX': 'Fundrise Innovation Fund',
        'ETN': '伊顿', 'GEV': 'GE Vernova', 'HIMS': 'Hims & Hers', 'APP': 'AppLovin',
        # --- 新增关注 ---
        'ICE': '洲际交易所', 'SMH': '半导体指数ETF', 'VGT': '领航信息技术',
        'JEPI': '摩根JEPi', 'GOOG': '谷歌C', 'LITE': 'Lumentum',
        'ASTS': 'AST SpaceMobile', 'FCX': 'Freeport-McMoRan', 'ASM': 'ASM International',
        'EUV': 'EUV', 'WTI': '原油WTI',
    }

    # A股常见代码映射（注：000001 作为上证指数在 CN_INDICES / 指数代码表中处理，不在此作个股）
    CN_STOCKS = {
        '600519': '贵州茅台', '000858': '五粮液',
        '601318': '中国平安', '600036': '招商银行', '000651': '格力电器',
        '601166': '兴业银行', '600276': '恒瑞医药', '000333': '美的集团',
        '600030': '中信证券', '601398': '工商银行', '600000': '浦发银行',
        '002594': '比亚迪', '300750': '宁德时代', '600887': '伊利股份',
        '601012': '隆基绿能', '002475': '立讯精密', '600031': '三一重工',
        # --- 用户关注 ---
        '600111': '北方稀土', '601899': '紫金矿业',
        # ETF
        '159880': '有色ETF鹏华', '518850': '黄金ETF华夏',
        '560710': '船舶ETF富国', '159985': '豆粕ETF',
        '516150': '纳指ETF嘉实', '562500': '机器人ETF华夏', '513310': '中韩半导体ETF华泰柏瑞',
    }

    # 指数代码
    US_INDICES = {'^GSPC': '标普500', '^DJI': '道琼斯', '^IXIC': '纳斯达克', '^VIX': '波动率指数'}
    CN_INDICES = {'sh000001': '上证指数', 'sz399001': '深证成指', 'sz399006': '创业板指', 'sz399300': '沪深300'}

    # A股指数数字代码 → 新浪代码（用户输入 000001 / 399300 等时正确路由到指数而非个股）
    CN_INDEX_CODES = {
        '000001': 'sh000001',   # 上证指数
        '000300': 'sh000300',   # 沪深300（沪）
        '399300': 'sz399300',   # 沪深300（深）
        '399001': 'sz399001',   # 深证成指
        '399006': 'sz399006',   # 创业板指
        '399005': 'sz399005',   # 中小板指
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            # 携带 consent cookie，规避 Yahoo 因未同意隐私政策而返回的 429
            'Cookie': 'A1=d=v=2&l9=449117&l1d=731550;',
        })

    def _is_cn_stock(self, symbol: str) -> bool:
        """判断是否为A股代码"""
        s = symbol.strip().lower()
        # 纯数字 → A股
        if s.isdigit() and len(s) == 6:
            return True
        # 带前缀 sh/sz
        if s.startswith(('sh', 'sz', 'bj')):
            return True
        return False

    def _normalize_cn_symbol(self, symbol: str) -> str:
        """标准化A股代码为新浪格式"""
        s = symbol.strip().lower()
        # 先匹配已知指数数字代码（如 000001→sh000001, 399300→sz399300）
        if s in self.CN_INDEX_CODES:
            return self.CN_INDEX_CODES[s]
        if s.isdigit() and len(s) == 6:
            if s.startswith(('60', '68', '11', '51', '50')):
                return f'sh{s}'
            else:
                return f'sz{s}'
        return s

    def _is_us_index(self, symbol: str) -> bool:
        return symbol.strip().upper().startswith('^')

    def fetch_us_stock(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        通过 Yahoo Finance v8 API 获取美股/指数数据

        Args:
            symbol: 股票代码（会自动标准化，如 BRK.B → BRK-B）
            days: 期望获取的交易日天数
        """
        # 标准化代码：Yahoo 用横线不用点号
        symbol = symbol.strip().upper().replace('.', '-')

        # 系统性限流防护：Yahoo 处于冷却期时，直接走 Nasdaq 兜底，
        # 不做任何 Yahoo 请求，从根本上避免“逐只代码空等 429”。
        if _yahoo_in_cooldown():
            try:
                df = self._fetch_us_stock_nasdaq(symbol, days)
                if df is not None and len(df) > 0:
                    return df
            except Exception:
                pass  # 冷却可能即将结束，下方仍尝试一次 Yahoo

        # days 是交易日，转换为日历天（交易日约占日历天的 5/7）
        # 额外多取 40 天日历时间，确保足够
        calendar_days = int(days * 7 / 5) + 40

        end_ts = int(time.time())
        start_ts = end_ts - calendar_days * 86400

        params = {
            'period1': start_ts,
            'period2': end_ts,
            'interval': '1d',
        }

        # 尝试多个域名；仅在两个域名都失败时触发全局冷却并降级，
        # 避免单域名“瞬时限流”即放弃（提升抗限流健壮性）。
        domains = ['query2.finance.yahoo.com', 'query1.finance.yahoo.com']
        last_error = None
        parsed = None
        for domain in domains:
            url = f"https://{domain}/v8/finance/chart/{symbol}"
            try:
                r = self.session.get(url, params=params, timeout=10)
                if r.status_code == 429:
                    last_error = f"Yahoo({domain}) 429"
                    continue
                if r.status_code != 200:
                    last_error = f"Yahoo({domain}) {r.status_code}"
                    continue
                d = r.json()
                result = d.get('chart', {}).get('result')
                if not result:
                    last_error = f"Yahoo 错误: {d.get('chart', {}).get('error', {}).get('description', '未知')}"
                    continue

                # —— 解析（两域名共用）——
                data = result[0]
                timestamps = data.get('timestamp', [])
                quote = data.get('indicators', {}).get('quote', [{}])[0]
                ohlcv = data.get('indicators', {}).get('adjclose', [{}])[0] if 'adjclose' in data.get('indicators', {}) else {}
                opens = quote.get('open', []); highs = quote.get('high', [])
                lows = quote.get('low', []); closes = quote.get('close', [])
                volumes = quote.get('volume', [])
                adj_closes = ohlcv.get('adjclose', []) if ohlcv else closes

                df = pd.DataFrame({
                    'date': [datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in timestamps],
                    'open': opens, 'high': highs, 'low': lows,
                    'close': adj_closes,  # 使用前复权收盘价
                    'volume': volumes,
                })
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0].reset_index(drop=True)
                df['open'] = df['open'].fillna(df['close'])
                df['high'] = df['high'].fillna(df['close'])
                df['low'] = df['low'].fillna(df['close'])
                df['volume'] = df['volume'].fillna(0).astype(float)
                if ohlcv and len(adj_closes) == len(closes):
                    for i in range(len(df)):
                        if not np.isnan(closes[i]) and closes[i] > 0:
                            ratio = adj_closes[i] / closes[i]
                            if not np.isnan(df.loc[i, 'open']):
                                df.loc[i, 'open'] = df.loc[i, 'open'] * ratio
                            if not np.isnan(df.loc[i, 'high']):
                                df.loc[i, 'high'] = df.loc[i, 'high'] * ratio
                            if not np.isnan(df.loc[i, 'low']):
                                df.loc[i, 'low'] = df.loc[i, 'low'] * ratio
                df['date'] = pd.to_datetime(df['date'])
                if len(df) > days:
                    df = df.tail(days).reset_index(drop=True)
                parsed = df
                break
            except Exception as e:
                last_error = str(e)
                continue

        if parsed is not None and len(parsed) > 0:
            return parsed

        # Yahoo 两域名均失败 → 触发全局冷却，依次用 Nasdaq / Stooq 兜底
        _trigger_yahoo_cooldown()
        try:
            df = self._fetch_us_stock_nasdaq(symbol, days)
            if df is not None and len(df) > 0:
                return df
        except Exception as ne:
            last_error = f"{last_error}；Nasdaq 兜底失败: {ne}"
        try:
            df = self._fetch_stooq(symbol, days)
            if df is not None and len(df) > 0:
                return df
        except Exception as se:
            last_error = f"{last_error}；Stooq 兜底失败: {se}"

        raise ValueError(f"Yahoo/Nasdaq/Stooq 均失败: {last_error}")

    def _fetch_us_stock_nasdaq(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        Nasdaq 免费历史接口兜底（覆盖主流美股 / ETF / 基金 / 指数，无需 API Key）。
        注意：免费接口返回的交易日数量有限，不足以支撑完整 300 天策略，
        仅用于在 Yahoo 被限流时仍能显示最新价与近期走势。
        部分 ETF / 基金 / 指数在 Nasdaq 按不同 assetclass 归类，故依次尝试
        stocks → etf → fund → index，任一成功即返回（系统性兜底，无需逐只硬编码）。
        """
        from datetime import datetime as _dt, timedelta as _td
        end = _dt.now()
        start = end - _td(days=max(days, 365))
        url = f"https://api.nasdaq.com/api/quote/{symbol}/historical"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Origin': 'https://www.nasdaq.com',
            'Referer': 'https://www.nasdaq.com/',
        }

        def _num(x):
            return float(str(x).replace('$', '').replace(',', '').strip() or 0)

        last_err = None
        for assetclass in ('stocks', 'etf', 'fund', 'index'):
            params = {
                'assetclass': assetclass,
                'fromdate': start.strftime('%Y-%m-%d'),
                'todate': end.strftime('%Y-%m-%d'),
            }
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=15)
                if r.status_code != 200:
                    last_err = f"Nasdaq({assetclass}) 返回 {r.status_code}"
                    continue
                d = r.json()
                if d.get('status', {}).get('rCode') != 200:
                    msg = d.get('status', {}).get('bCodeMessage', [{}])[0].get('errorMessage', '未知错误')
                    last_err = f"Nasdaq({assetclass}): {msg}"
                    continue
                rows = d.get('data', {}).get('tradesTable', {}).get('rows', [])
                if not rows:
                    last_err = f"Nasdaq({assetclass}) 未返回历史数据"
                    continue
                recs = []
                for row in rows:
                    recs.append({
                        'date': _dt.strptime(row['date'], '%m/%d/%Y'),
                        'open': _num(row['open']),
                        'high': _num(row['high']),
                        'low': _num(row['low']),
                        'close': _num(row['close']),
                        'volume': _num(row['volume']),
                    })
                df = pd.DataFrame(recs).sort_values('date').reset_index(drop=True)
                return df
            except Exception as e:
                last_err = f"Nasdaq({assetclass}): {e}"
                continue
        raise ValueError(f"{last_err}")

    def _fetch_stooq(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        Stooq 免费 CSV 历史接口兜底（无需 API Key、限流极宽松，覆盖美股/ETF/指数）。
        作为 Yahoo + Nasdaq 之后的第三层兜底，显著提升抗限流能力。
        """
        import csv as _csv, io as _io
        ysym = symbol.replace('.', '-').upper()
        st = self._STOOQ_INDEX.get(ysym)
        if st is None:
            st = (ysym + '.us') if not ysym.startswith('^') else ysym.lower()
        url = f"https://stooq.com/q/d/l/?s={st}&i=d"
        r = self.session.get(url, timeout=10, headers={'User-Agent': _rotate_ua()})
        if r.status_code != 200 or r.text.lstrip().lower().startswith('<!doctype'):
            raise ValueError(f"Stooq 未返回 CSV（{r.status_code}）")
        rows = list(_csv.reader(_io.StringIO(r.text.strip())))
        if len(rows) < 2 or rows[0][0].strip().lower() != 'date':
            raise ValueError("Stooq 返回格式异常")
        recs = []
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                recs.append({
                    'date': datetime.strptime(row[0].strip(), '%Y-%m-%d'),
                    'open': float(row[1]), 'high': float(row[2]),
                    'low': float(row[3]), 'close': float(row[4]),
                    'volume': float(row[5] or 0),
                })
            except Exception:
                continue
        if not recs:
            raise ValueError("Stooq 无有效数据行")
        df = pd.DataFrame(recs).sort_values('date').reset_index(drop=True)
        if len(df) > days:
            df = df.tail(days).reset_index(drop=True)
        return df

    def _fetch_em_kline(self, sina_symbol: str, days: int = 300) -> pd.DataFrame:
        """
        东方财富 K 线（前复权），A 股权威免费源，作为新浪之后的兜底。
        """
        code = sina_symbol[2:] if sina_symbol[:2] in ('sh', 'sz', 'bj') else sina_symbol
        secid = ('1.' if sina_symbol[:2] == 'sh' else '0.') + code
        url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        params = {'fields1': 'f1,f2,f3,f4,f5,f6',
                  'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
                  'klt': '101', 'fqt': '1', 'secid': secid,
                  'end': '20500101', 'lmt': str(min(days + 60, 800))}
        r = self.session.get(url, params=params, timeout=10,
                             headers={'User-Agent': _rotate_ua(),
                                      'Referer': 'https://quote.eastmoney.com/'})
        if r.status_code != 200:
            raise ValueError(f"东财 {r.status_code}")
        d = r.json()
        kl = (d.get('data') or {}).get('klines') or []
        if not kl:
            raise ValueError("东财无K线数据")
        recs = []
        for line in kl:
            p = line.split(',')
            if len(p) < 7:
                continue
            try:
                recs.append({'date': datetime.strptime(p[0], '%Y-%m-%d'),
                             'open': float(p[1]), 'close': float(p[2]),
                             'high': float(p[3]), 'low': float(p[4]),
                             'volume': float(p[5])})
            except Exception:
                continue
        if not recs:
            raise ValueError("东财K线解析为空")
        df = pd.DataFrame(recs)
        if len(df) > days:
            df = df.tail(days).reset_index(drop=True)
        return df

    def _swap_exchange(self, sina_symbol: str) -> str:
        """sh<->sz 前缀互换（用于取数失败时自动换市场重试）"""
        if sina_symbol.startswith('sh'):
            return 'sz' + sina_symbol[2:]
        if sina_symbol.startswith('sz'):
            return 'sh' + sina_symbol[2:]
        return sina_symbol

    def _fetch_sina_kline(self, sina_symbol: str, days: int) -> pd.DataFrame:
        """向新浪请求单只 K 线并解析为 DataFrame；无数据/解析失败返回空 DataFrame。"""
        datalen = min(days + 50, 1023)  # 新浪最大1023
        url = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var/CN_MarketDataService.getKLineData"
        params = {'symbol': sina_symbol, 'scale': 240, 'ma': 'no', 'datalen': datalen}
        r = self.session.get(url, params=params, timeout=15,
                             headers={'Referer': 'https://finance.sina.com.cn'})
        if r.status_code != 200:
            return pd.DataFrame()
        text = r.text
        m = re.search(r'=\((\[.*\])\)', text, re.DOTALL)
        if not m:
            m = re.search(r'(\[.*\])', text, re.DOTALL)
        if not m:
            return pd.DataFrame()
        try:
            data = json.loads(m.group(1))
        except Exception:
            return pd.DataFrame()
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data).rename(columns={'day': 'date'})
        df['date'] = pd.to_datetime(df['date'])
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df = df.dropna(subset=['close'])
        return df

    def fetch_cn_stock(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        通过新浪财经接口获取A股数据（主源）。
        新浪失败则自动切换交易所前缀重试，仍失败则用东方财富 K 线（前复权）兜底，
        形成「新浪 → 东方财富」双层权威源，显著降低单一源限流导致取不到行情的概率。
        """
        sina_symbol = self._normalize_cn_symbol(symbol)
        df = self._fetch_sina_kline(sina_symbol, days)
        if len(df) == 0:
            alt = self._swap_exchange(sina_symbol)
            if alt != sina_symbol:
                df2 = self._fetch_sina_kline(alt, days)
                if len(df2) > 0:
                    df = df2
        if len(df) == 0:
            # 东方财富 K 线兜底（前复权，权威性高）
            try:
                df = self._fetch_em_kline(sina_symbol, days)
            except Exception:
                pass
        if len(df) == 0:
            # 再试一次东方财富（换市场前缀）
            try:
                df = self._fetch_em_kline(self._swap_exchange(sina_symbol), days)
            except Exception:
                pass
        if len(df) == 0:
            raise ValueError("新浪/东方财富均未返回数据，请检查股票代码")
        return df.tail(days).reset_index(drop=True)

    def fetch(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        统一获取接口，自动判断美股/A股。
        带短时效（120s）内存缓存：同一标的短时间内（如看板刷新 + 详情页）只真实抓取一次，
        显著降低重复请求导致的限流命中率。

        Args:
            symbol: 股票代码
                - 美股: AAPL, MSFT, ^GSPC(指数)
                - A股: 600519, 000001, sh600519
            days: 获取最近N个交易日
        Returns:
            pd.DataFrame with columns: date, open, high, low, close, volume
        """
        symbol = symbol.strip()
        key = (symbol, days)
        cached = _kline_cache_get(key)
        if cached is not None:
            return cached.copy()

        if self._is_us_index(symbol) or not self._is_cn_stock(symbol):
            df = self.fetch_us_stock(symbol, days)
        else:
            df = self.fetch_cn_stock(symbol, days)

        _kline_cache_set(key, df)
        return df

    def search(self, keyword: str) -> list:
        """搜索股票代码，精确匹配优先"""
        keyword = keyword.strip().upper()
        exact = []     # 精确匹配
        partial = []   # 模糊匹配

        # 指数
        for code, name in {**self.US_INDICES, **self.CN_INDICES}.items():
            if code.upper() == keyword:
                exact.append({'code': code, 'name': name, 'market': '美股' if code.startswith('^') else 'A股'})
            elif keyword in code.upper() or keyword in name:
                partial.append({'code': code, 'name': name, 'market': '美股' if code.startswith('^') else 'A股'})

        # 美股
        for code, name in self.US_STOCKS.items():
            if code == keyword:
                exact.append({'code': code, 'name': name, 'market': '美股'})
            elif keyword in code or keyword in name.upper():
                partial.append({'code': code, 'name': name, 'market': '美股'})

        # A股
        for code, name in self.CN_STOCKS.items():
            if code == keyword:
                exact.append({'code': code, 'name': name, 'market': 'A股'})
            elif keyword in code or keyword in name:
                partial.append({'code': code, 'name': name, 'market': 'A股'})

        results = exact + partial

        # 如果keyword是纯数字或带sh/sz前缀，直接作为A股代码
        kw_lower = keyword.lower()
        if kw_lower.startswith(('sh', 'sz', 'bj')) or (keyword.isdigit() and len(keyword) == 6):
            if kw_lower not in [r['code'] for r in results]:
                # 反查真实公司简称（东方财富/新浪），失败再回退"自定义A股"
                cn_code = kw_lower[2:] if kw_lower[:2] in ('sh', 'sz', 'bj') else kw_lower
                cn_name = self.lookup_cn_name(cn_code) or '自定义A股'
                results.insert(0, {'code': kw_lower, 'name': cn_name, 'market': 'A股'})

        # 美股代码可能包含字母、横线或点号（如 BRK-B, BRK.B）
        # 标准化：点号转为横线
        normalized = keyword.replace('.', '-')
        has_existing = any(r['code'] == normalized for r in results)
        if not has_existing:
            # 只匹配纯ASCII字母+横线/点号（排除中文）
            cleaned = keyword.replace('-', '').replace('.', '')
            if cleaned.isascii() and cleaned.isalpha() and len(keyword) <= 8:
                us_name = self.lookup_us_name(normalized) or '自定义美股'
                results.insert(0, {'code': normalized, 'name': us_name, 'market': '美股'})

        return results[:20]  # 最多返回20条

    # ================================================================
    #  公司名称反查（搜索兜底：避免展示"自定义股票"）
    # ================================================================
    def lookup_cn_name(self, code: str) -> "str | None":
        """通过东方财富 F10 / 新浪实时行情反查 A股 6位代码对应的公司简称。"""
        code = code.strip().lower()
        if code[:2] in ('sh', 'sz', 'bj'):   # 兼容 sh600519 / sz000822 形式
            code = code[2:]
        if not (code.isdigit() and len(code) == 6):
            return None
        # 1) 东方财富 F10（权威简称）
        try:
            em = ('SH' if code.startswith('6') else 'SZ') + code
            r = self.session.get(
                f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={em}",
                timeout=12,
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://emweb.securities.eastmoney.com/'})
            if r.status_code == 200:
                d = r.json()
                jb = (d.get('jbzl') or [{}])[0]
                abbr = jb.get('SECURITY_NAME_ABBR') or jb.get('STR_NAMEA')
                if abbr:
                    return abbr.strip()
        except Exception:
            pass
        # 2) 新浪实时行情兜底（首字段即公司名）
        try:
            sina = self._normalize_cn_symbol(code)
            r2 = self.session.get(
                f"https://hq.sinajs.cn/list={sina}",
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://finance.sina.com.cn'},
                timeout=10)
            r2.encoding = 'gbk'
            m = re.search(r'"([^",]+),', r2.text)
            if m and m.group(1) and m.group(1) != code:
                return m.group(1).strip()
        except Exception:
            pass
        return None

    def lookup_us_name(self, symbol: str) -> "str | None":
        """通过 Yahoo v8 chart 的 meta 反查美股/ETF/指数代码对应的公司名（best-effort）。"""
        if _yahoo_in_cooldown():
            return None
        ysym = symbol.replace('.', '-').upper()
        try:
            r = self.session.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}",
                params={'interval': '1d', 'range': '1d'}, timeout=10)
            if r.status_code == 429:
                _trigger_yahoo_cooldown()
                return None
            if r.status_code != 200:
                return None
            meta = r.json().get('chart', {}).get('result', [{}])[0].get('meta', {})
            return meta.get('shortName') or meta.get('longName')
        except Exception:
            return None

    def lookup_name(self, symbol: str) -> "str | None":
        """统一名称反查：A股走东方财富/新浪，其余走 Yahoo。"""
        sym = symbol.strip()
        if self._is_cn_stock(sym):
            return self.lookup_cn_name(sym)
        return self.lookup_us_name(sym)

    # ================================================================
    #  公司主营业务（多数据源兜底）
    # ================================================================
    def _fetch_em_profile(self, em_code: str) -> "str | None":
        """东方财富 F10 公司概况：优先公司简介(ORG_PROFILE)，其次经营范围(BUSINESS_SCOPE)。"""
        try:
            r = self.session.get(
                f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={em_code}",
                timeout=12,
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://emweb.securities.eastmoney.com/'})
            if r.status_code != 200:
                return None
            d = r.json()
            jb = (d.get('jbzl') or [{}])[0]
            prof = jb.get('ORG_PROFILE') or jb.get('BUSINESS_SCOPE')
            return prof.strip() if prof else None
        except Exception:
            return None

    def _fetch_yahoo_profile(self, ysym: str) -> "str | None":
        try:
            r = self.session.get(
                f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ysym}",
                params={'modules': 'assetProfile'}, timeout=10)
            if r.status_code == 429:
                _trigger_yahoo_cooldown()
                return None
            if r.status_code != 200:
                return None
            result = r.json().get('quoteSummary', {}).get('result')
            if not result:
                return None
            s = result[0].get('assetProfile', {}).get('longBusinessSummary')
            return s.strip() if s else None
        except Exception:
            return None

    def _fetch_nasdaq_profile(self, ysym: str) -> "str | None":
        try:
            nh = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'application/json',
                'Accept-Language': 'en-US,en;q=0.9',
                'Origin': 'https://www.nasdaq.com',
                'Referer': 'https://www.nasdaq.com/',
            }
            r = self.session.get(
                f"https://api.nasdaq.com/api/quote/{ysym}/info", headers=nh, timeout=12)
            if r.status_code != 200:
                return None
            d = r.json()
            prof = (d.get('data') or {}).get('summary', {}).get('profile', {})
            desc = prof.get('Description') or prof.get('description')
            return desc.strip() if desc else None
        except Exception:
            return None

    def fetch_profile(self, symbol: str) -> "str | None":
        """
        获取公司主营业务简介（最简短描述）。多数据源兜底，任一成功即返回：

          A股  ：东方财富 F10(公司简介/经营范围) → Yahoo .SS/.SZ assetProfile(受冷却保护)
          美股/ETF：Yahoo quoteSummary assetProfile → Nasdaq /info Description
          指数  ：Yahoo quoteSummary（无则 None）

        说明：东方财富/新浪为独立源，不受 Yahoo 限流冷却影响；只有真正请求 Yahoo 时才检查冷却。
        任何失败均返回 None（前端隐藏该区域）。
        """
        sym = symbol.strip()

        # 优先使用自选股人工维护的简短简介（含指数，避免指数代码被当成个股走东财）
        meta = STOCK_META.get(sym.upper()) or STOCK_META.get(sym)
        if meta and meta.get('desc'):
            return meta['desc']

        # —— A股：东方财富为主，Yahoo 兜底 ——
        if self._is_cn_stock(sym):
            code = sym.lower()
            if code[:2] in ('sh', 'sz', 'bj'):
                code = code[2:]
            em_code = ('SH' if code.startswith('6') else 'SZ') + code
            s = self._fetch_em_profile(em_code)
            if s:
                return s
            if not _yahoo_in_cooldown():
                y = f"{code}.SS" if code.startswith('6') else f"{code}.SZ"
                s = self._fetch_yahoo_profile(y)
                if s:
                    return s
            return None

        # —— 美股/ETF/指数：人工维护映射为主（沙箱/本机均可靠），Yahoo 兜底 ——
        # 优先使用自选股人工维护的简短简介，保证详情页稳定展示；
        # 不在表中的临时代码再走 Yahoo assetProfile（本机可用，沙箱常限流）。
        meta = STOCK_META.get(sym.upper()) or STOCK_META.get(sym)
        if meta and meta.get('desc'):
            return meta['desc']

        ysym = sym if sym.startswith('^') else sym.replace('.', '-').upper()
        if not _yahoo_in_cooldown():
            s = self._fetch_yahoo_profile(ysym)
            if s:
                return s
        if not sym.startswith('^'):
            s = self._fetch_nasdaq_profile(ysym)
            if s:
                return s
        return None

    # ================================================================
    #  行业分类（多数据源兜底）
    # ================================================================
    def _fetch_em_industry(self, em_code: str) -> "str | None":
        """东方财富 F10 证监会行业分类（INDUSTRYCSRC1，如 '制造业-有色金属冶炼和压延加工业'）。"""
        try:
            r = self.session.get(
                f"https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={em_code}",
                timeout=12,
                headers={'User-Agent': 'Mozilla/5.0',
                         'Referer': 'https://emweb.securities.eastmoney.com/'})
            if r.status_code != 200:
                return None
            d = r.json()
            jb = (d.get('jbzl') or [{}])[0]
            ind = jb.get('INDUSTRYCSRC1')
            return ind.strip() if ind else None
        except Exception:
            return None

    def _fetch_yahoo_industry(self, ysym: str) -> "str | None":
        """Yahoo assetProfile 的 sector / industry（本机可用，沙箱常限流）。"""
        if _yahoo_in_cooldown():
            return None
        try:
            r = self.session.get(
                f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ysym}",
                params={'modules': 'assetProfile'}, timeout=10)
            if r.status_code == 429:
                _trigger_yahoo_cooldown()
                return None
            if r.status_code != 200:
                return None
            result = r.json().get('quoteSummary', {}).get('result')
            if not result:
                return None
            ap = result[0].get('assetProfile', {})
            s = ap.get('sector') or ap.get('industry')
            return s.strip() if s else None
        except Exception:
            return None

    def fetch_industry(self, symbol: str) -> "str | None":
        """
        获取行业分类（最简短标签）。多数据源兜底，任一成功即返回：

          A股  ：东方财富 F10(INDUSTRYCSRC1) → 人工维护映射
          美股/ETF/指数：人工维护映射（最可靠）→ Yahoo assetProfile(sector)

        说明：人工维护映射对本仓持有标的在沙箱/本机均稳定可用；
              临时搜索且不在表中的代码走动态源（沙箱下美股可能为空）。
        """
        sym = symbol.strip()
        # 1) 自选股人工维护映射（优先，最可靠）
        meta = STOCK_META.get(sym.upper()) or STOCK_META.get(sym)
        if meta and meta.get('industry'):
            return meta['industry']
        # 2) A股：东方财富行业
        if self._is_cn_stock(sym):
            code = sym.lower()
            if code[:2] in ('sh', 'sz', 'bj'):
                code = code[2:]
            em_code = ('SH' if code.startswith('6') else 'SZ') + code
            s = self._fetch_em_industry(em_code)
            if s:
                return s
        # 3) 美股/ETF/指数：Yahoo sector（本机可用）
        ysym = sym if sym.startswith('^') else sym.replace('.', '-').upper()
        s = self._fetch_yahoo_industry(ysym)
        if s:
            return s
        return None


# ================================================================
#  测试
# ================================================================
if __name__ == "__main__":
    fetcher = DataFetcher()

    print("=== 搜索测试 ===")
    for r in fetcher.search('茅台'):
        print(f"  {r['code']:10s} {r['name']:10s} {r['market']}")
    for r in fetcher.search('AAPL'):
        print(f"  {r['code']:10s} {r['name']:10s} {r['market']}")

    print("\n=== 美股 AAPL ===")
    df = fetcher.fetch('AAPL', 300)
    print(f"rows={len(df)}, last={df.iloc[-1]['date'].strftime('%Y-%m-%d')} close={df.iloc[-1]['close']:.2f}")

    print("\n=== A股 600519 贵州茅台 ===")
    df2 = fetcher.fetch('600519', 300)
    print(f"rows={len(df2)}, last={df2.iloc[-1]['date'].strftime('%Y-%m-%d')} close={df2.iloc[-1]['close']:.2f}")

    print("\n=== 指数 ^GSPC 标普500 ===")
    df3 = fetcher.fetch('^GSPC', 300)
    print(f"rows={len(df3)}, last={df3.iloc[-1]['date'].strftime('%Y-%m-%d')} close={df3.iloc[-1]['close']:.2f}")
