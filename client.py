import argparse
import time
from decimal import Decimal
from operator import itemgetter

import requests
from prometheus_client import Gauge, start_http_server
from tabulate import tabulate

ACTIVE_SYMBOL_STATUS = 'TRADING'
PROMETHEUS_PORT = 8080


def get_args():
    """
    Handle arguments to run full script or just some actions and chose behaviour of script
    """

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--action',
        help="Action",
        choices=[
            'get-top-symbols',
            'get-notional-value',
            'prometheus',
            'full'
        ],
        default='full'
    )

    parser.add_argument('--top-symbols-count', help='Start date for import', type=int, default=5)
    parser.add_argument('--top-bids-count', help='Start date for import', type=int, default=200)
    parser.add_argument(
        '--only-trading',
        help=f'Use only symbols in status {ACTIVE_SYMBOL_STATUS}',
        type=bool,
        action=argparse.BooleanOptionalAction,
        default=False
    )
    parser.add_argument('--symbol', help='Chose symbol for top top-symbols-count action', type=str, default='BTC')
    parser.add_argument(
        '--field',
        help='Chose field for top-symbols-count action',
        type=str,
        default='volume',
        choices=['volume', 'count']
    )

    return parser.parse_args()


class APIError(Exception):
    """
    Binance API requests specific exception
    """
    pass


class BinanceClient:
    API_URL = 'https://api.binance.com/api'

    def __init__(self, top_symbols: int = 5, top_bids: int = 200, only_trading: bool = False):
        self.top_symbols = top_symbols
        self.top_bids = top_bids
        self.only_trading = only_trading
        self.prom_spread = Gauge('absolute_spread_value',
                                 'Absolute Value of Price Spread', ['symbol'])

        self.prom_delta = Gauge('absolute_delta_spread_value',
                                'Absolute Delta Value of Price Spread', ['symbol'])

    def _request(self, uri: str, params: dict = None) -> dict:
        resp = requests.get(self.API_URL + uri, params=params)
        if not resp.ok:
            raise APIError(resp.text)
        data = resp.json()
        return data

    def get_top_symbols(self, asset: str, field: str, output: bool = False) -> dict[str, Decimal]:
        """
        1. Print the top 5 symbols with
         quote asset BTC and the highest volume over the last 24 hours in descending order.
        2. Print the top 5 symbols with
         quote asset USDT and the highest number of trades over the last 24 hours in descending order.

        There are no information about status of a symbol in test questions, but we can see that some top symbols can be
        in 'BREAK' status of trading like LUNABTC and I made additional option to filter only 'active' symbols
        use ONLY_TRADING_SYMBOLS constant to enable this behaviour

        """
        data = self._request("/v3/ticker/24hr")
        # float due to limitation can have some nuances: 0.1-0.01 = 0.09000000000000001
        # so we will go with Decimal class for floating point arithmetic
        symbols = {i['symbol']: Decimal(i[field]) for i in data if i['symbol'].endswith(asset)}

        if self.only_trading:
            symbols_info = self._request('/v3/exchangeInfo')['symbols']
            symbols_status = {i['symbol']: i['status'] for i in symbols_info}
            symbols = {k: v for k, v in symbols.items() if symbols_status[k] == ACTIVE_SYMBOL_STATUS}

        # Since 3.7 dicts supports order, so we will use dict for convenience
        symbols = dict(sorted(symbols.items(), key=itemgetter(1), reverse=True)[:self.top_symbols])

        if output:
            print(f'\nTop symbols for {asset} by {field}:')
            print(tabulate(
                symbols.items(),
                headers=['Symbol', field.capitalize()],
                floatfmt=".8f")
            )
        return symbols

    def get_notional_value(self, asset: str, field: str, output: bool = False) -> dict[str, dict[str, Decimal]]:
        """
        3. Using the symbols from Q1,
           what is the total notional value of the top 200 bids and asks currently on each order book?

        """

        symbols = self.get_top_symbols(asset, field)
        notional = {}
        for symbol in symbols:
            data = self._request(
                "/v3/depth",
                params={'symbol': symbol, 'limit': 500}
            )
            notional[symbol] = {}
            for col in ["bids", "asks"]:
                vals = sorted(
                    [(Decimal(i), Decimal(j)) for i, j in data[col]],
                    key=itemgetter(0),
                    reverse=True
                )[:self.top_bids]
                notional[symbol][col] = sum([(i[0] * i[1]) for i in vals])

        if output:
            print(f'\nNotional values for {asset} by {field}:')
            print(tabulate(
                [(k, v['bids'], v['asks']) for k, v in notional.items()],
                headers=['Symbol', 'Bids', 'Asks'],
                floatfmt=".8f"),
            )

        return notional

    def get_price_spread(self, asset: str, field: str, output: bool = False) -> dict[str, Decimal]:
        """
        4. What is the price spread for each of the symbols from Q2?
        """

        symbols = self.get_top_symbols(asset, field)
        prices = self._request(
            '/v3/ticker/bookTicker',
            params={'symbols': '["{}"]'.format('","'.join([i for i in symbols]))}
        )
        spread = {}
        for price in prices:
            spread[price['symbol']] = Decimal(price['askPrice']) - Decimal(price['bidPrice'])

        if output:
            print(f'\nPrice spread for {asset} for top by {field}:')
            print(tabulate(
                spread.items(),
                headers=['Symbol', field.capitalize()],
                floatfmt=".8f")
            )

        return spread

    def get_spread_delta(self,
                         old_spread: dict[str, Decimal],
                         asset: str,
                         field: str,
                         output: bool = False) -> (dict[str, float], dict[str, float]):
        """
        5. Every 10 seconds print the result of Q4 and the absolute delta from the previous value for each symbol.
        """
        spread = self.get_price_spread(asset, field)
        delta = {}
        result = []
        for key in old_spread:
            delta[key] = abs(old_spread[key] - spread[key])
            self.prom_delta.labels(key).set(float(delta[key]))
            self.prom_spread.labels(key).set(float(old_spread[key]))
            result.append([key, old_spread[key], delta[key]])

        if output:
            print(f"\nAbsolute Delta for {asset} by {field}:")
            print(tabulate(
                result,
                headers=['Symbol', 'Old spread', 'Delta'],
                floatfmt=".8f"),
            )

        return spread, delta


if __name__ == '__main__':
    args = get_args()

    client = BinanceClient(args.top_symbols_count, args.top_bids_count, args.only_trading)

    # We will use new, fancy structural pattern matching here introduced in python3.10 - PEP 636
    # to get only partial data
    match args.action:
        case 'get-top-symbols':
            client.get_top_symbols(args.symbol, args.field, True)
        case 'get-notional-value':
            client.get_notional_value(args.symbol, args.field, True)
        case 'prometheus':
            spread = client.get_price_spread('USDT', 'count', True)
            start_http_server(PROMETHEUS_PORT)
            while True:
                time.sleep(10)
                spread, _ = client.get_spread_delta(spread, 'USDT', 'count', True)
        case 'full':
            client.get_top_symbols('BTC', 'volume', True)
            client.get_top_symbols('USDT', 'count', True)
            client.get_notional_value('BTC', 'volume', True)
            spread = client.get_price_spread('USDT', 'count', True)
            start_http_server(PROMETHEUS_PORT)
            while True:
                time.sleep(10)
                spread, _ = client.get_spread_delta(spread, 'USDT', 'count', True)
