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
        'FIGR': 'Figure Technology', 'MU': '美光科技', 'CF': 'Fundrise Innovation Fund',
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

        # 尝试多个域名，避免限流
        domains = [
            'query2.finance.yahoo.com',
            'query1.finance.yahoo.com',
        ]

        last_error = None
        for attempt in range(2):  # 仅试 query2 / query1 两个域名各一次，避免持续限流时空等
            domain = domains[attempt % len(domains)]
            url = f"https://{domain}/v8/finance/chart/{symbol}"
            try:
                r = self.session.get(url, params=params, timeout=10)
                if r.status_code == 429:
                    # 系统性防护：命中限流即触发全局冷却，停止对 Yahoo 的后续重试
                    _trigger_yahoo_cooldown()
                    last_error = f"Yahoo Finance ({domain}) 限流 429（已触发全局冷却，转 Nasdaq 兜底）"
                    break
                if r.status_code != 200:
                    # 非 200（含 5xx/404 等）一律视为 Yahoo 不可用，触发冷却并放弃本次重试
                    _trigger_yahoo_cooldown()
                    last_error = f"Yahoo Finance ({domain}) 返回 {r.status_code}（已触发全局冷却）"
                    break

                d = r.json()
                result = d.get('chart', {}).get('result')
                if not result:
                    err = d.get('chart', {}).get('error', {})
                    last_error = f"Yahoo Finance 错误: {err.get('description', '未知')}"
                    continue

                data = result[0]
                timestamps = data.get('timestamp', [])
                quote = data.get('indicators', {}).get('quote', [{}])[0]
                ohlcv = data.get('indicators', {}).get('adjclose', [{}])[0] if 'adjclose' in data.get('indicators', {}) else {}

                opens = quote.get('open', [])
                highs = quote.get('high', [])
                lows = quote.get('low', [])
                closes = quote.get('close', [])
                volumes = quote.get('volume', [])
                adj_closes = ohlcv.get('adjclose', []) if ohlcv else closes

                df = pd.DataFrame({
                    'date': [datetime.fromtimestamp(t).strftime('%Y-%m-%d') for t in timestamps],
                    'open': opens,
                    'high': highs,
                    'low': lows,
                    'close': adj_closes,  # 使用前复权收盘价
                    'volume': volumes,
                })

                # 去除空行
                df = df.dropna(subset=['close'])
                df = df[df['close'] > 0]
                df = df.reset_index(drop=True)

                # 用原始open/high/low，但如果没有则用close
                df['open'] = df['open'].fillna(df['close'])
                df['high'] = df['high'].fillna(df['close'])
                df['low'] = df['low'].fillna(df['close'])
                df['volume'] = df['volume'].fillna(0).astype(float)

                # 如果有前复权价，调整 open/high/low 的比例
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
                # 截取请求的天数
                if len(df) > days:
                    df = df.tail(days).reset_index(drop=True)
                return df

            except Exception as e:
                # 连接超时/异常也视为 Yahoo 不可用，触发全局冷却后转 Nasdaq 兜底
                _trigger_yahoo_cooldown()
                last_error = str(e)
                break

        # Yahoo 全部失败 → 尝试 Nasdaq 兜底（免费、无需 API Key，覆盖主流美股）
        try:
            df = self._fetch_us_stock_nasdaq(symbol, days)
            if df is not None and len(df) > 0:
                return df
        except Exception as ne:
            last_error = f"{last_error}；Nasdaq 兜底也失败: {ne}"

        raise ValueError(f"Yahoo Finance 所有域名均失败: {last_error}")

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
        通过新浪财经接口获取A股数据。
        若按启发式前缀（sh/sz）取不到数据，自动切换交易所前缀重试一次，
        以应对部分 ETF/代码前缀与常规规则不符的情况（系统性兜底，无需逐只硬编码）。
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
            raise ValueError("新浪财经未返回数据，请检查股票代码")
        return df.tail(days).reset_index(drop=True)

    def fetch(self, symbol: str, days: int = 300) -> pd.DataFrame:
        """
        统一获取接口，自动判断美股/A股

        Args:
            symbol: 股票代码
                - 美股: AAPL, MSFT, ^GSPC(指数)
                - A股: 600519, 000001, sh600519
            days: 获取最近N个交易日
        Returns:
            pd.DataFrame with columns: date, open, high, low, close, volume
        """
        symbol = symbol.strip()

        if self._is_us_index(symbol) or not self._is_cn_stock(symbol):
            return self.fetch_us_stock(symbol, days)
        else:
            return self.fetch_cn_stock(symbol, days)

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
                results.insert(0, {'code': kw_lower, 'name': '自定义A股', 'market': 'A股'})

        # 美股代码可能包含字母、横线或点号（如 BRK-B, BRK.B）
        # 标准化：点号转为横线
        normalized = keyword.replace('.', '-')
        has_existing = any(r['code'] == normalized for r in results)
        if not has_existing:
            # 只匹配纯ASCII字母+横线/点号（排除中文）
            cleaned = keyword.replace('-', '').replace('.', '')
            if cleaned.isascii() and cleaned.isalpha() and len(keyword) <= 8:
                results.insert(0, {'code': normalized, 'name': '自定义美股', 'market': '美股'})

        return results[:20]  # 最多返回20条

    def fetch_profile(self, symbol: str) -> "str | None":
        """
        获取公司主营业务简介（最简短描述）。

        数据源（best-effort，任一成功即返回）：
          - 美股/ETF/指数/A股：Yahoo quoteSummary 的 assetProfile.longBusinessSummary
          - 美股/ETF 兜底：Nasdaq /info 的 profile.Description
        受系统性限流保护：Yahoo 冷却期内直接返回 None，不做请求。
        任何失败均返回 None（前端隐藏该区域）。
        """
        if _yahoo_in_cooldown():
            return None

        sym = symbol.strip()
        # 构造 Yahoo 符号
        if self._is_cn_stock(sym):
            ysym = f"{sym}.SS" if sym.startswith('6') else f"{sym}.SZ"  # A股
        elif sym.startswith('^'):
            ysym = sym  # 指数直接用
        else:
            ysym = sym.replace('.', '-').upper()

        # 1) Yahoo quoteSummary（主源）
        try:
            url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ysym}"
            r = self.session.get(url, params={'modules': 'assetProfile'}, timeout=10)
            if r.status_code == 429:
                _trigger_yahoo_cooldown()
            elif r.status_code == 200:
                d = r.json()
                result = d.get('quoteSummary', {}).get('result')
                if result:
                    s = result[0].get('assetProfile', {}).get('longBusinessSummary')
                    if s:
                        return s.strip()
        except Exception:
            pass

        # 2) Nasdaq /info 兜底（仅美股/ETF，A股与指数无）
        if (not self._is_cn_stock(sym)) and (not sym.startswith('^')):
            try:
                nh = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'application/json',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Origin': 'https://www.nasdaq.com',
                    'Referer': 'https://www.nasdaq.com/',
                }
                r2 = self.session.get(
                    f"https://api.nasdaq.com/api/quote/{ysym}/info", headers=nh, timeout=12)
                if r2.status_code == 200:
                    d2 = r2.json()
                    prof = (d2.get('data') or {}).get('summary', {}).get('profile', {})
                    desc = prof.get('Description') or prof.get('description')
                    if desc:
                        return desc.strip()
            except Exception:
                pass

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
