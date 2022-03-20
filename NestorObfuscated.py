import asyncio
import json
import math
import os
import threading
import time
from datetime import datetime

import ccxt
import hikari
import pandas as pd
from flask import Flask
from hikari.impl.rest import RESTClientImpl as Discord


# ----------------------------------------------------------------------------------------------------------------------
# MAIN CLASS
# ----------------------------------------------------------------------------------------------------------------------
# Main class, memory centralization, handle technical stuff
class Kernel:
    """
    Kernel class
    Centralize the memory
    Boot handling
    Launch all threads
    Plays the role of a configuration file
    """
    # -- Public Memory --
    # discord
    bot_token = "token"
    server_id = 0
    category_id = 0
    general_channel_id = 0
    log_channel_id = 0
    dash_channel_id = 0
    technical_channel_id = 0
    # trusted users
    trust = (0, 0)
    discord_client = None
    prefix = "!"
    # for binance
    ratelimit = 1000
    # for ftx
    latency = 1
    # for client
    limiter = None
    testnet = False
    # pairs
    pairs = {}
    # threads
    threads = {}
    # trading timeframe
    interval = "1d"
    # files
    data_file_path = "data.json"
    # for flash file
    parent_path = ""
    # terminal colors
    colors = {"LIGHT_RED": '\33[91m',
              "LIGHT_YELLOW": '\33[93m',
              "LIGHT_GREEN": '\33[92m',
              "LIGHT_BLUE": '\33[94m',
              "LIGHT_PURPLE": '\33[95m',
              "LIGHT_CYAN": '\33[96m',
              "LIGHT_WHITE": '\33[97m',
              "RED": '\33[31m',
              "YELLOW": '\33[33m',
              "GREEN": '\33[32m',
              "BLUE": '\33[34m',
              "PURPLE": '\33[35m',
              "CYAN": '\33[36m',
              "WHITE": '\33[37m',
              "END": '\33[0m'}

    # class
    class Limiter:
        """
        Limiter class
        Handle binance or others exchanges ratelimit
        ( two different methods )
        Use the current value of used weight returned by binance
        Or add an artificial latency between each request
        """

        def __init__(self, max_weight=None, latency=None):
            """
            Constructor
            :param max_weight: max weight for the binance ratelimit
            :param latency: artificial latency between each request for other exchanges
            """
            self.max_weight = max_weight
            self.latency = latency
            # store requests to perform
            self.data = {}
            # store responses
            self.response = {0.0: None}
            # store used keys
            self.keys = []
            # launch the flusher to centralize request sending for all threads
            Kernel.threads["limiter_flusher"] = threading.Thread(target=self.flush, name="flusher").start()

        def perform(self, client, function, *args, **kwargs):
            """
            Place a request in the queue
            :param client:  ccxt client that will be used for the request
            :param function: function to perform
            :param args: args for the function
            :param kwargs: kwargs for the function
            :return: return the response of the request
            """
            # make a key
            key = time.time()
            # check if the key is already used
            while key in self.keys:
                key = time.time()
            # add the request to the queue
            self.data[key] = {"client": client, "function": function, "args": args, "kwargs": kwargs}
            # add the key to the used keys
            self.keys.append(key)
            # wait for the response
            while key not in self.response:
                time.sleep(1)
            # send the response
            response = self.response[key]
            self.response.pop(key)
            return response

        def flush(self):
            """
            Flush the queue
            """

            # infinite loop ( in a dedicated thread )
            while True:
                time.sleep(1)
                # handle all requests in the queue
                for key in self.keys:
                    if self.latency is not None:
                        time.sleep(self.latency)
                    # get request data
                    client = self.data[key]["client"]
                    function = self.data[key]["function"]
                    args = self.data[key]["args"]
                    kwargs = self.data[key]["kwargs"]

                    # ask for the current binance weight value ( if binance )
                    try:
                        if self.max_weight is not None:
                            client.fetch_ticker("BTC/USDT")
                            current_weight_consumption = int(client.last_response_headers['x-mbx-used-weight-1m'])
                            # wait if ratelimit is reached
                            if current_weight_consumption >= self.max_weight:
                                now = datetime.now().strftime('%M')
                                while now == datetime.now().strftime('%M'):
                                    time.sleep(1)
                        # handle the request
                        self.response[key] = function(client, *args, **kwargs)
                        self.keys.remove(key)
                        self.data.pop(key)
                    except Exception as e:
                        # retry the request and log the error
                        log("RED[Limiter] Ccxt api error >")
                        print(e)
                        print(function.__name__, str(*args), str(**kwargs))
                        print("retry in 60s")
                        time.sleep(60)

    # constructor
    def __init__(self):
        """
        Constructor
        """
        self._boot()

    # booting function
    def _boot(self):
        """
        Boot the bot
        """
        log("GREEN[Kernel] Booting...")
        # getting data
        try:
            with open(self.data_file_path, 'r') as file:
                data = json.loads(file.read())
        # handle datafile related errors
        except FileNotFoundError:
            log("RED[ERROR] Cannot find data file")
            cin = input("Create a new data file? (y/n)")
            if cin == "n":
                log("PURPLEExiting...")
                exit()
            elif cin == "y":
                load_template(self.data_file_path)
                log("PURPLEFile created, exiting...")
                exit()

        # discord first method
        Kernel._start_link()

        # FTX
        if "API" in data:
            # create a rate limiter to be able to handle ftx rate limit with multiple clients
            Kernel.limiter = Kernel.Limiter(latency=Kernel.latency)

            # data parsing
            api_key = data["API"]["key"]
            api_secret = data["API"]["secret"]
            for key in data.keys():
                if key != "API":
                    self._add(token=key, stablecoin=data[key]["stablecoin"], levels=data[key]["levels"],
                              api_key=api_key, api_secret=api_secret, subaccount=data[key]["subaccount"])
        # Binance
        else:
            # create a rate limiter to be able to handle binance rate limit with multiple clients
            Kernel.limiter = Kernel.Limiter(max_weight=Kernel.ratelimit)

            # data parsing
            for key in data.keys():
                self._add(token=key, stablecoin=data[key]["stablecoin"], levels=data[key]["levels"],
                          api_key=data[key]["key"], api_secret=data[key]["secret"])

        # discord first method
        self._finish_link()

        # run the technical handler
        Kernel.threads["main"] = threading.current_thread()
        self._technical_handler()

    # discord bot
    @staticmethod
    def _start_link():
        """
        instantiate discord client ( bot & REST app )
        """
        Kernel.discord_client = DiscordClient()

    @staticmethod
    def _finish_link():
        """
        Launch the discord gateway bot and the dashboard
        """
        # start the bot
        Kernel.discord_client.run()

        # Launch dash thread
        Kernel.threads["dash"] = threading.Thread(target=Dash.main, name="dash").start()

    # add a pair
    def _add(self, token, stablecoin, levels, api_key, api_secret, subaccount=None):
        """
        Add a pair to the bot
        :param token: the token of the pair token/stablecoin
        :param stablecoin: the stablecoin of the pair token/stablecoin
        :param levels: list of levels as parsed from the datafile
        :param api_key: api key
        :param api_secret: api secret
        :param subaccount: subaccount name if FTX
        :return:
        """
        log(f"LIGHT_CYAN[Kernel] Adding {token}")
        # instantiate a ccxt client
        client = Client(Kernel.limiter, api_key, api_secret, subaccount=subaccount, testnet=Kernel.testnet)
        # instantiate a pair
        pair = Pair(token, stablecoin, levels, client)
        self.pairs[token] = pair

    # run all threads
    @staticmethod
    def run_all():
        """
        Launch all threads
        """
        for pair in Kernel.pairs.values():
            pair.run()
        Kernel.discord_client.update_general_status()
        log(f"GREEN[Kernel] run_all() called")

    # stop all threads
    @staticmethod
    def stop_all():
        """
        Stop all threads
        """
        for pair in Kernel.pairs.values():
            pair.stop()
        Kernel.discord_client.update_general_status()
        log(f"GREEN[Kernel] stop_all() called")

    # run in main thread, stuff like auto-updater will be added in the future
    @staticmethod
    def _technical_handler():
        """
        Technical handler
        ( to keep main thread running )
        """
        log(f"GREEN[Kernel] technical handler called, boot finished")
        finish()


# Ccxt wrapped client
class Client:
    """
    Client wrapped from ccxt
    """

    def __init__(self, limiter, api_key=None, api_secret=None, subaccount=None, testnet=False):
        """
        Constructor
        :param limiter: rate limiter class ( instanced by the Kernel )
        :param api_key: api key if using authenticated client
        :param api_secret: api key if using authenticated client
        :param subaccount: api key if using authenticated FTX client
        :param testnet: to use testnet ( not for FTX and with authenticated client only)
        """

        # check if we use an authenticated client
        if api_key is not None and api_secret is not None:
            # check if we use FTX or binance client
            if subaccount is not None:
                # for main subaccount
                if subaccount == "Main Account":
                    subaccount = None
                self.exchange = ccxt.ftx({
                    'apiKey': api_key,
                    'secret': api_secret,
                    'enableRateLimit': True,
                    'headers': {'FTX-SUBACCOUNT': subaccount}
                })
                self.x = ccxt.ftx
            else:
                self.exchange = ccxt.binance({
                    'apiKey': api_key,
                    'secret': api_secret,
                    'enableRateLimit': True,
                    'options': {'adjustForTimeDifference': True}
                })
                self.x = ccxt.binance
                # toggle testnet
                if testnet:
                    self.exchange.set_sandbox_mode(True)
            log(f"CYAN[Client] Enabling authenticated client for {self.exchange.name}, testnet={testnet}")
        # unauthenticated client
        else:
            self.exchange = ccxt.binance()
            self.x = ccxt.binance
            log(f"CYAN[Client] Enabling unauthenticated client for {self.exchange.name}")

        self.exchange.load_markets()
        # Rate limiter
        self.limiter = limiter

    def _perform(self, function, *args, **kwargs):
        """
        Give a request to be placed in the queue to the limiter
        :param function: function to be performed
        :param args: args for the function
        :param kwargs: kwargs for the function
        """
        log(f"LIGHT_PURPLE[Client] Scheduling {function.__name__} args:{args}, kwargs:{kwargs}")
        return self.limiter.perform(self.exchange, function, *args, **kwargs)

    def get_bid(self, market):
        """
        Get the bid price for a market
        :param market: example 'BTC/USDT'
        :return: bid price
        """
        return self._perform(self.x.fetch_ticker, market)['bid']

    def get_ask(self, market):
        """
        Get the ask price for a market
        :param market: example 'BTC/USDT'
        :return: ask price
        """
        return self._perform(self.x.fetch_ticker, market)['ask']

    def post_market_order(self, market, side, amount):
        """
        Post a market order
        :param market: example 'BTC/USDT'
        :param side: "buy" or "sell"
        :param amount: value returned by get_buy_size() or get_sell_size() depending on the side
        :return: response returned ( order data)
        """
        if amount == 0:
            return False
        return self._perform(self.x.create_order, symbol=market, type='market', side=side, amount=amount)

    def post_limit_order(self, market, side, amount, price):
        """
        Post a limit order
        :param market: example 'BTC/USDT'
        :param side: "buy" or "sell"
        :param amount: value returned by get_buy_size() or get_sell_size() depending on the side
        :param price: trigger price of the order
        :return: response returned ( order data)
        """
        if amount == 0:
            return False
        return self._perform(self.x.create_limit_order, symbol=market, side=side, amount=amount, price=float(price))

    def post_stop(self, market, amount, price, side='sell'):
        """
        Post a limit order
        :param market: example 'BTC/USDT'
        :param amount: value returned by get_buy_size() or get_sell_size() depending on the side
        :param price: trigger price of the order
        :param side: "buy" or "sell"
        :return: response returned ( order data)
        """
        if amount == 0:
            return False
        return self._perform(self.x.create_order, symbol=market, type='stop', side=side, amount=amount,
                             params={"stopPrice": price})

    def post_take_profit(self, market, amount, price, side='sell'):
        """
        Post a limit order
        :param market: example 'BTC/USDT'
        :param amount: value returned by get_buy_size() or get_sell_size() depending on the side
        :param price: trigger price of the order
        :param side: "buy" or "sell"
        :return: response returned ( order data)
        """
        if amount == 0:
            return False
        return self._perform(self.x.create_order, symbol=market, type='takeProfit', side=side, amount=amount,
                             params={"triggerPrice": price})

    def cancel_order(self, order, market):
        """
        Cancel an order
        :param order: order object returned by get_order method or by a response. Can also be an order id as a string
        :param market: example 'BTC/USDT'
        :return: the response returned
        """
        if type(order) is str:
            order = self.get_order(order_id=order, market=market)

        order_type = order['info']['type']
        if isinstance(self.exchange, ccxt.ftx):
            if order_type == "stop" or order_type == "take_profit":
                return self._perform(self.x.cancel_order, order["info"]["orderId"], market,
                                     {'method': 'privateDeleteConditionalOrdersOrderId'})
            else:
                return self._perform(self.x.cancel_order, order["info"]["orderId"], market,
                                     {'method': 'privateDeleteOrdersOrderId'})
        else:
            return self._perform(self.x.cancel_order, order["info"]["orderId"], market)

    def get_free_balance(self, token):
        """
        Get the free balance of a token
        :param token: BTC, ETH, USDT, etc.
        :return: the free balance
        """
        self._perform(self.x.load_markets)
        balances = self._perform(self.x.fetch_balance)["free"]
        if not token.upper() in balances:
            return 0
        return balances[token.upper()]

    def get_balance(self, token):
        """
        Get the total balance of a token ( free + locked in orders)
        :param token: BTC, ETH, USDT, etc.
        :return: the total balance
        """
        self._perform(self.x.load_markets)
        balances = self._perform(self.x.fetch_balance)["total"]
        if not token.upper() in balances:
            return 0
        return balances[token.upper()]

    def get_klines(self, market, interval, limit=100):
        """
        Get the klines of a market
        :param market: example 'BTC/USDT'
        :param interval: example '1m' or '1d'
        :param limit: max number of klines to return
        :return:
        """
        klines = self._perform(self.x.fetch_ohlcv, market, interval, limit=limit)
        dataframe = pd.DataFrame(klines)
        # parse dataframe
        dataframe.rename(columns={0: 'timestamp', 1: 'open', 2: 'high', 3: 'low', 4: 'close'}, inplace=True)
        dataframe.pop(5)
        dataframe.drop(index=dataframe.index[-1], axis=0, inplace=True)
        return dataframe

    def get_market(self, market):
        """
        Get some market data
        :param market: example 'BTC/USDT'
        :return: response returned
        """
        return self._perform(self.x.market, market)

    def get_precision(self, market):
        """
        Get the precision of a market ( number of decimals that you can increment for a price )
        :param market: example 'BTC/USDT'
        :return: precision
        """
        market = self.get_market(market)
        return market["precision"]["amount"], market["limits"]["amount"]["min"]

    def get_buy_size(self, coin, pair, amount, price=None, free_currency_balance=None):
        """
        Calculate the size of a buy order, parse the size to make it corresponding with the market precision and minimum
        :param coin: example 'USDT'
        :param pair: example 'BTC/USDT'
        :param amount: in percentage
        :param price: if it's for a limit order
        :param free_currency_balance: if you want to use it instead of your token balance
        :return: the size of your order

        if you enter an amount of 100, it will take 100% of your wallet or 100% of the free currency balance
        if it's not None
        """

        # get free balance
        if free_currency_balance is None:
            free_currency_balance = self.get_free_balance(coin)

        # apply percentage
        amount = (amount * float(free_currency_balance)) / 100

        # get precision & limit
        precision, limit = self.get_precision(pair)

        digits = int(math.sqrt((int(math.log10(precision)) + 1) ** 2)) + 1

        # get price
        if price is None:
            price = self.get_ask(pair)

        amount /= price

        # apply precision
        amount = truncate(amount, digits)

        # apply limit
        print(amount, limit)
        if amount < limit:
            log(f"RED[Client] Amount is below limit for {pair}")
            return 0

        return amount

    def get_sell_size(self, token, pair, amount, free_token_balance=None):
        """
        Calculate the size of a sell order, parse the size to make it corresponding with the market
        precision and minimum
        :param token: example 'BTC'
        :param pair: example 'BTC/USDT'
        :param amount: in percentage
        :param free_token_balance: if you want to use it instead of your token balance
        :return: the size of your order

        if you enter an amount of 100, it will take 100% of your wallet or 100% of the free currency balance
        if it's not None
        even for limit orders, you don't have to enter a price
        """

        # get free balance
        if free_token_balance is None:
            free_token_balance = self.get_free_balance(token)

        # apply percentage
        amount = (amount * float(free_token_balance)) / 100

        # get precision & limit
        precision, limit = self.get_precision(pair)

        digits = int(math.sqrt((int(math.log10(precision)) + 1) ** 2)) + 1

        # apply precision
        amount = truncate(amount, digits)

        # apply limit
        if amount < limit:
            log(f"RED[Client] Amount is below limit for {pair}")
            return 0

        return amount

    def get_order(self, order_id, market):
        """
        Get an order
        :param order_id: as a string
        :param market: example 'BTC/USDT'
        :return: order data ( response returned )
        """
        # special method for FTX
        if isinstance(self.exchange, ccxt.ftx):
            return self._perform(self.x.fetch_order, order_id, market, {'method': 'privateGetOrdersOrderId'})
        else:
            return self._perform(self.x.fetch_order, order_id, market)

    def get_all_orders(self, market=None, open_only=True):
        """
        Get all orders
        :param market: example 'BTC/USDT'
        :param open_only: if you want to get only open orders
        :return: order data ( response returned )
        """
        if open_only:
            return self._perform(self.x.fetch_open_orders, symbol=market)
        else:
            return self._perform(self.x.fetch_orders, symbol=market)

    def get_order_status(self, order_id, market):
        """
        Get the status of an order
        :param order_id: as a string
        :param market: example 'BTC/USDT'
        :return: one of the 3 status: 'open', 'closed', 'canceled'
        """
        order = self.get_order(order_id, market)

        if order["info"]["remaining"] == 0:
            return "filled"
        elif order["info"]["status"] == "open":
            return "open"
        elif order["info"]["status"] == "canceled":
            return "canceled"


# ----------------------------------------------------------------------------------------------------------------------
# UTILS CLASS
# ----------------------------------------------------------------------------------------------------------------------
# Used by live engine to store pairs data
class Pair:
    """
    Pair class that store all data and method related to a market pair
    """

    def __init__(self, token, stablecoin, levels, client):
        """
        :param token: example 'BTC'
        :param stablecoin: example 'USDT'
        :param levels: as parsed from the data file by the kernel
        :param client: ccxt client instanced by the kernel
        """
        # Used by main
        self.token = token
        self.stablecoin = stablecoin
        self.market = token + "/" + stablecoin

        # Levels parsing
        self.levels = LevelContainer(levels)

        # Status
        self.thread_status = ThreadStatus.STOPPED
        self.trade_status = TradeStatus.UNKNOWN

        # Other
        self.channel = self._get_channel()

        # API
        self.client = client

        # Create all buttons objects
        def make_actions_rows():
            action_row = Discord.build_action_row(Kernel.discord_client.rest_app.get_client())
            button = action_row.add_button(hikari.ButtonStyle.DANGER, "stop/" + token)
            button.set_label("Stop")
            button.add_to_container()
            button = action_row.add_button(hikari.ButtonStyle.SUCCESS, "start/" + token)
            button.set_label("Start")
            button.add_to_container()
            return action_row

        self.action_row = make_actions_rows()

        # Update the status
        self.update_status()

    def _get_channel(self):
        """
        Try to recover or create the discord channel related to this pair
        :return: channel object
        """
        # INI
        app = Kernel.discord_client.rest_app
        category, guild = Kernel.category_id, Kernel.server_id

        # Fetch required data
        channels = app.perform(Discord.fetch_guild_channels, guild)

        # Build the channel name
        name = "💲│" + self.token.lower()
        channel = None

        for chan in channels:
            if chan.type == hikari.ChannelType.GUILD_TEXT:
                if chan.name == name and chan.parent_id == category:
                    channel = chan

        if channel is None:
            channel = app.perform(Discord.create_guild_text_channel, guild, name, category=category)

        return channel

    def send(self, content, component=None):
        """
        Send a message to the discord channel related to this pair
        :param content: as a string
        :param component: discord component ( buttons, embeds, ... )
        """
        if component is None:
            Kernel.discord_client.rest_app.perform(Discord.create_message, self.channel, content, )
        else:
            Kernel.discord_client.rest_app.perform(
                Discord.create_message, self.channel, content, component=component)

    def update_status(self):
        """
        Update the status of the pair, it means the message in the channel
        """
        # Get the thread status
        if self.thread_status == ThreadStatus.RUNNING:
            color = RGB.GREEN
            thread_status = "Running :green_circle:"

        else:
            color = RGB.RED
            thread_status = "Stopped :red_circle:"

        # make the embed component
        embed = hikari.Embed()
        embed.title = self.token + " panel"
        embed.add_field("Thread:", thread_status)
        embed.add_field("Trading status:", self.trade_status)
        embed.set_author(name="Algo Trader",
                         icon="https://cdn.discordapp.com/attachments/738091114166222929/922434138068361246/IMG_0985"
                              ".jpg")
        embed.color = hikari.Color.from_rgb(color[0], color[1], color[2])
        # clear the channel and send the embed
        Kernel.discord_client.clear(self.channel)
        self.send(embed, component=self.action_row)
        log(f"PURPLE[{self.token}] Updated pair status")

    def run(self, update=False):
        """
        Run main function of the pair ( start the bot on this pair )
        :param update: to update the discord status ( false when this method is called by the run_all method )
        """
        if self.thread_status == ThreadStatus.RUNNING:
            return
        # run main method in a dedicated thread
        t = threading.Thread(target=self.main, args=[update], name=self.token)
        t.start()
        # fill the threads list
        Kernel.threads[self.token] = t
        log(f"[Pair] run() called on {self.token}")

    def stop(self):
        """
        Schedule the stop of the pair
        """

        if self.thread_status == ThreadStatus.STOPPED:
            return
        # schedule the stop
        self.thread_status = ThreadStatus.STOPPED
        Kernel.discord_client.update_general_status()
        log(f"[Pair] stop() called on {self.token}")

    # Obfuscated, was the main function containing all the algorithm
    def main(self, update_at_start=False):
        """
        Obfuscated
        """
        pass


# This class run in a separated thread to handle the dashboard and to monitor all threads and other stuff
class Dash:
    """
    This class is used to handle the dashboard to monitor all threads and other stuff
    """

    # function run in a dedicated thread
    @staticmethod
    def main():
        """
        This function is used to run the dashboard in a dedicated thread
        :return:
        """
        while True:
            Kernel.discord_client.update_dashboard()
            time.sleep(500)


# Used by live engine to interact with discord api
class DiscordClient:
    """
    This class is used to interact with discord api by using a gateway bot or the Rest App
    """

    def __init__(self):
        """
        Constructor
        """
        self.rest_app = DiscordRestApp(Kernel.bot_token)

        # Create all buttons objects
        def make_actions_rows():
            """
            Create all buttons
            :return: the action row
            """
            action_row = Discord.build_action_row(self.rest_app.get_client())
            button = action_row.add_button(hikari.ButtonStyle.DANGER, "stop_all")
            button.set_label("Stop all")
            button.add_to_container()
            button = action_row.add_button(hikari.ButtonStyle.SUCCESS, "start_all")
            button.set_label("Start all")
            button.add_to_container()
            return action_row

        self.general_action_row = make_actions_rows()
        log(f"GREEN[DiscordClient] client initialized")

    def update_general_status(self):
        """
        Update the general status posted in the general channel to monitor all pairs
        """
        log(f"LIGHT_CYAN[DiscordClient] update_general_status()")
        # clear the channel
        self.clear(Kernel.general_channel_id)

        # build field data
        off, on = "", ""

        for pair in Kernel.pairs.values():
            if pair.thread_status == ThreadStatus.STOPPED:
                off += pair.token + "   :red_circle:" + "\n"
            else:
                on += pair.token + "   :green_circle:" + "\n"

        if off == "":
            off = "No threads"
        if on == "":
            on = "No threads"

        # make the embed
        embed = hikari.Embed()
        embed.title = "General Status"
        embed.add_field("Running threads", on, inline=True)
        embed.add_field("Sleeping threads", off, inline=True)
        embed.set_author(name="Algo Trader",
                         icon="https://cdn.discordapp.com/attachments/738091114166222929/922434138068361246/IMG_0985"
                              ".jpg")
        embed.color = hikari.Color.from_rgb(3, 165, 252)

        # post the embed
        self.rest_app.perform(Discord.create_message, Kernel.general_channel_id, embed,
                              component=self.general_action_row)
        log(f"LIGHT_CYAN[DiscordClient] update_general_status() completed")

    def update_dashboard(self):
        """
        Update the dashboard to monitor threads, wallet and other stuff
        """
        log(f"LIGHT_CYAN[DiscordClient] update_dashboard()")
        self.clear(Kernel.dash_channel_id)

        # handle crashed threads
        threads = threading.enumerate()
        crashed = ""
        for pair in Kernel.pairs.values():
            token = pair.token
            if pair.thread_status == ThreadStatus.RUNNING:
                is_alive = False
                for thread in threads:
                    if thread.name == token:
                        is_alive = True
                        break
                if not is_alive:
                    crashed += f"💥 {token}\n"
                    self.log(RGB.RED, "Critical Error", f"Thread {token} crashed")

        if crashed == "":
            crashed = "No threads"

        # handle balance display
        balance = ""
        for pair in Kernel.pairs.values():
            free_stablecoin = pair.client.get_balance(token=pair.stablecoin)
            free_token = pair.client.get_balance(token=pair.token)
            token_eq = pair.client.get_ask(market=pair.market) * free_token
            total = free_stablecoin + token_eq

            balance += f"{pair.token} thread: **{round(free_stablecoin, 2)}** *{pair.stablecoin.lower()}" \
                       f"*\xa0\xa0\xa0**{round(free_token, 2)}** *" \
                       f"{pair.token.lower()}*\xa0\xa0\xa0**{round(total, 2)}** *total supply*\n "
        if balance == "":
            balance = "No threads"

        embed = hikari.Embed()
        embed.title = "Dashboard"
        embed.add_field("Crashed threads", crashed, inline=False)
        embed.add_field("Funds", balance, inline=False)
        embed.set_author(name="Algo Trader",
                         icon="https://cdn.discordapp.com/attachments/738091114166222929/922434138068361246/IMG_0985"
                              ".jpg")
        embed.color = hikari.Color.from_rgb(102, 0, 204)

        self.rest_app.perform(Discord.create_message, Kernel.dash_channel_id, embed)

        log(f"LIGHT_CYAN[DiscordClient] update_dashboard() completed")

    def clear(self, channel_id):
        """
        Clear the channel
        :param channel_id: id of the channel
        """
        messages = self.rest_app.perform(Discord.fetch_messages, channel_id)
        self.rest_app.perform(Discord.delete_messages, channel_id, messages)

    def log(self, color, title, content):
        """
        Log a message to the discord log channel ( this method is used to monitor the trading ! the other log function
        is used to monitor the bot on the technical side )
        :param color: color of the embed of the DiscordColors class
        :param title: title of the embed
        :param content: content of the embed
        """
        embed = hikari.Embed()
        embed.add_field(title, content)
        embed.color = hikari.Color.from_rgb(color[0], color[1], color[2])

        self.rest_app.perform(Discord.create_message, Kernel.log_channel_id, embed)

    def send(self, content):
        """
        Send a message to the technical channel ( used by log function below not log method above )
        :param content: content of the log
        """
        self.rest_app.perform(Discord.create_message, Kernel.technical_channel_id, content)

    def perform_command(self, message):
        """
        Perform a command
        :param message: the message object from discord
        """

        # ping
        def ping():
            """
            Ping the bot ( only the discord bot thread )
            """
            self.rest_app.perform(Discord.create_message, message.channel_id, "Pong!")

        # force dash & general update
        def force():
            """
            Force an update of the dashboard and the general status
            """
            self.update_dashboard()
            self.update_general_status()
            self.rest_app.perform(Discord.create_message, message.channel_id, "Performing update..")

        # threads
        def threads():
            """
            Display the list of all threads
            """
            self.rest_app.perform(Discord.create_message, message.channel_id, threading.enumerate())

        # balance
        def balance():
            """
            Display the balance of a token
            """
            # try to handle command syntax mistake
            try:
                split = message.content.split(" ")
                token = split[1].upper()
                if len(split) > 2:
                    account = split[2].upper()
                else:
                    account = token

                # try to handle command performing errors
                try:
                    client = Kernel.pairs[account].client
                    bal = client.get_balance(token=token)
                    self.rest_app.perform(Discord.create_message, message.channel_id,
                                          f"{token} balance: {bal}")
                except Exception as e:
                    self.rest_app.perform(Discord.create_message, message.channel_id,
                                          f"Error, in the command: {e}")
                    return

            except Exception as e:
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"Error, wrong usage of the command: {e}\nUsage: "
                                      f"{Kernel.prefix}balance <token> <token that represent the subacount>")
                return

        # price
        def price():
            """
            Display the price of a token ( ask ) but there's not a notable difference between the ask and the bid
            """

            # try to handle command syntax mistake
            try:
                split = message.content.split(" ")
                market = split[1].upper()

                # try to handle command performing errors
                try:
                    client = Kernel.pairs[0].client
                    ask = client.get_ask(market=market)
                    self.rest_app.perform(Discord.create_message, message.channel_id, f"{market} price: {ask}")
                except Exception as e:
                    self.rest_app.perform(Discord.create_message, message.channel_id,
                                          f"Error market probably doesn't exist: {e}")
                    return
            except Exception as e:
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"Error, wrong usage of the command: {e}\n Usage: {Kernel.prefix}price <market>")
                return

        # post
        def post():
            """
            Post an order
            """

            # try to handle command syntax mistake
            try:
                # content parsing
                split = message.content.split(" ")
                order_type = split[1]
                side = split[2]
                token = split[3]
                amount = split[4]
                if len(split) == 6:
                    p = split[5]
                else:
                    p = None
                token = token.upper()

                # try to handle command performing errors
                try:
                    client = Kernel.pairs[token].client
                    coin = Kernel.pairs[token].stablecoin
                    market = Kernel.pairs[token].market
                    # buy side
                    if side == "buy":
                        if order_type == "limit":
                            if p is None:
                                self.rest_app.perform(Discord.create_message, message.channel_id,
                                                      f"Error no price provided")
                                return
                            amount = client.get_buy_size(coin=coin, pair=market, amount=100, price=p,
                                                         free_currency_balance=amount)
                            order = client.post_limit_order(side="buy", market=token + "/" + coin, amount=amount,
                                                            price=p)
                            self.rest_app.perform(Discord.create_message, message.channel_id,
                                                  f"Posted buy limit order for {amount} {token} at {p}\n"
                                                  f"{order}")
                        elif order_type == "market":
                            amount = client.get_buy_size(coin=coin, pair=market, amount=100, price=p,
                                                         free_currency_balance=amount)
                            order = client.post_market_order(side="buy", market=token + "/" + coin, amount=amount)
                            self.rest_app.perform(Discord.create_message, message.channel_id,
                                                  f"Posted buy market order for {amount} {token}\n "
                                                  f"{order}")
                    # sell side
                    elif side == "sell":
                        if order_type == "limit":
                            if p is None:
                                self.rest_app.perform(Discord.create_message, message.channel_id,
                                                      f"Error no price provided")
                                return
                            amount = client.get_sell_size(token=token, pair=market, amount=100,
                                                          free_token_balance=amount)
                            order = client.post_limit_order(side="sell", market=token + "/" + coin, amount=amount,
                                                            price=p)
                            self.rest_app.perform(Discord.create_message, message.channel_id,
                                                  f"Posted sell limit order for {amount} {token} at {p}\n"
                                                  f"{order}")
                        elif order_type == "market":
                            amount = client.get_sell_size(token=token, pair=market, amount=100,
                                                          free_token_balance=amount)
                            order = client.post_market_order(side="sell", market=token + "/" + coin, amount=amount)
                            self.rest_app.perform(Discord.create_message, message.channel_id,
                                                  f"Posted sell market order for {amount} {token}\n "
                                                  f"{order}")
                except Exception as e:
                    self.rest_app.perform(Discord.create_message, message.channel_id,
                                          f"Error, in the command: {e}")
                    return

            except Exception as e:
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"Error, wrong usage of the command: {e}\n Usage: {Kernel.prefix}helppost")
                return

        # get orders
        def orders():
            """
            Get all open orders
            """
            # try to handle errors
            try:
                split = message.content.split(" ")
                token = split[1].upper()
                client = Kernel.pairs[token].client
                o = client.get_all_orders(market=token + "/" + Kernel.pairs[token].stablecoin, open_only=True)
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"{str(o)}")
            except Exception as e:
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"Error, wrong usage of the command: {e}\n Usage: {Kernel.prefix}orders <token>")
                return

        # cancel order
        def cancel():
            """
            Cancel an order
            :return:
            """

            # try to handle errors
            try:
                split = message.content.split(" ")
                order_id = split[1]
                token = split[2].upper()
                market = token + "/" + Kernel.pairs[token].stablecoin
                client = Kernel.pairs[token].client
                order = client.cancel_order(order=order_id, market=market)
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"canceled {order}")
            except Exception as e:
                self.rest_app.perform(Discord.create_message, message.channel_id,
                                      f"Error, wrong usage of the command: {e}"
                                      f"\n Usage: {Kernel.prefix}cancel <order_id>, <token>")
                return

        # help for post
        def help_post():
            """
            Help for post function
            :return:
            """
            self.rest_app.perform(Discord.create_message, message.channel_id,
                                  f"Usage: !post <type> <side> <token> <amount> [price]\n")

        # all commands
        commands = {"ping": ping,
                    "force": force,
                    "threads": threads,
                    "bal": balance,
                    "price": price,
                    "post": post,
                    "orders": orders,
                    "cancel": cancel,
                    "helppost": help_post}

        # command performer
        for item in commands:
            if Kernel.prefix + item in message.content:
                commands[item]()
                return

    # Run a gateway bot to receive interactions
    def run(self):
        """
        Run the gateway bot
        """
        bot = hikari.GatewayBot(Kernel.bot_token)

        # To receive messages ( so to handle commands )
        @bot.listen()
        async def on_message(event: hikari.MessageCreateEvent) -> None:
            """Listen for messages being created."""
            if not event.is_human or event.author_id not in Kernel.trust:
                # Do not respond to bots or webhooks or unauthorized users !
                return
            threading.Thread(target=self.perform_command, args=[event.message]).start()

        # To receive interactions ( so to handle buttons )
        @bot.listen()
        async def on_interaction(event: hikari.InteractionCreateEvent) -> None:
            itype = event.interaction.type
            if itype != hikari.InteractionType.MESSAGE_COMPONENT:
                return
            custom_id = event.interaction.custom_id
            user_id = event.interaction.member.id
            if user_id != 392376378730872832 and user_id != 255776481785937922:
                return

            # General panel
            if custom_id == "stop_all":
                Kernel.threads["*stop_all"] = threading.Thread(target=Kernel.stop_all, name="*stop_all").start()
                await event.interaction.create_initial_response(hikari.ResponseType.MESSAGE_UPDATE)
                return
            elif custom_id == "start_all":
                Kernel.threads["*start_all"] = threading.Thread(target=Kernel.run_all, name="*run_all").start()
                await event.interaction.create_initial_response(hikari.ResponseType.MESSAGE_UPDATE)
                return

            token = str(custom_id).split("/")[1]
            side = str(custom_id).split("/")[0]

            if token in Kernel.pairs:
                if side == "stop":
                    Kernel.threads["*stop"] = threading.Thread(target=Kernel.pairs[token].stop, name="*stop").start()
                elif side == "start":
                    Kernel.threads["*run"] = threading.Thread(target=Kernel.pairs[token].run, args=(True,), name="*run"
                                                              ).start()

            await event.interaction.create_initial_response(hikari.ResponseType.MESSAGE_UPDATE)

        def _run():
            bot.run(enable_signal_handlers=False)

        Kernel.threads["discord"] = threading.Thread(target=_run, name="discord").start()
        time.sleep(2)


# Discord api v8 rest wrapper using hikari ( component of DiscordClient )
class DiscordRestApp:
    """
    Discord rest api wrapper
    """

    def __init__(self, token):
        """
        Initialize the rest app
        :param token:
        """
        self.token = token
        self.rest = hikari.RESTApp()
        self.client = None
        log(f"GREEN[RestApp] initialized")

    def perform(self, function, *args, **kwargs):
        """
        Perform a request to the discord rest api
        :param function: function
        :param args: args
        :param kwargs: kwargs
        :return:
        """
        async def _run():
            async with self.rest.acquire(self.token, hikari.TokenType.BOT) as client:
                return await function(client, *args, **kwargs)

        return asyncio.run(_run())

    def get_client(self):
        """
        Get the client
        :return: the client
        """
        return self.rest.acquire(self.token, hikari.TokenType.BOT)


# To handle levels
class LevelContainer:
    """
    To handle price levels
    """
    class Level:
        """
        To handle a level
        """
        def __init__(self, level):
            """
            Initialize the level
            :param level: (lead, low, buy_amount)
            """
            self.lead = level[0]
            self.low = level[1]
            self.amt = int(level[2].replace("%", ""))

    def __init__(self, levels):
        """
        Initialize the level container
        :param levels:
        """
        self.levels = []
        # parsing
        for level in levels:
            self.levels.append(LevelContainer.Level(level))
        # sorting
        self.levels.sort(key=lambda x: x.lead, reverse=True)


# To handle the FlashFile
class FlashFile:
    """
    To handle the flash file, it will be used for the trading strategy to know when a buy signal got handled
    """
    def __init__(self, pair):
        """
        Initialize the flash file
        :param pair: a pair
        """
        self.pair = pair
        self.path = f"{Kernel.parent_path}FlashFile{pair.token}.json"
        self.data = {"buy_signal_handled": False, "sell_signal_handled": False}
        self.load()

    def load(self):
        """
        Load data from the flash file
        :return:
        """
        # check if file exists
        if not os.path.exists(self.path):
            # create a blank file
            with open(self.path, "w") as file:
                file.write(json.dumps(self.data))
        # load file if it exists
        else:
            with open(self.path, "r") as file:
                self.data = json.loads(file.read())

    def save(self):
        """
        Save data to the flash file
        :return:
        """
        with open(self.path, "w") as file:
            file.write(json.dumps(self.data))
        log(f"LIGHT_PURPLE[FlashFile] saving {self.path}")
        with open(self.path, "w") as file:
            file.write(json.dumps(self.data))

    def is_handled(self, side):
        """
        Check if a signal got handled
        :param side: buy/sell
        :return:
        """
        return bool(self.data[f"{side}_signal_handled"])

    def handle(self, side):
        """
        Handle a signal
        :param side: buy/sell
        :return:
        """
        self.data[f"{side}_signal_handled"] = True
        self.save()
        log(f"LIGHT_PURPLE[FlashFile] set to {side} handled")
        self.data[f"{side}_signal_handled"] = True
        self.save()

    def reset(self, side):
        """
        Reset a signal
        :param side: buy/sell
        :return:
        """
        log(f"LIGHT_PURPLE[FlashFile] set to {side} unhandled")
        self.data[f"{side}_signal_handled"] = False
        self.save()


# ----------------------------------------------------------------------------------------------------------------------
# UTILS FUNCTIONS
# ----------------------------------------------------------------------------------------------------------------------
# Used by the wrapped client to get buy & sell size
def truncate(number, decimals=0):
    """
    Returns a value truncated to a specific number of decimal places.
    :param number: the number
    :param decimals: the number of decimals
    """
    if not isinstance(decimals, int):
        raise TypeError("decimal places must be an integer.")
    elif decimals < 0:
        raise ValueError("decimal places has to be 0 or more.")
    elif decimals == 0:
        return math.trunc(number)

    factor = 10.0 ** decimals
    return math.trunc(number * factor) / factor


# Used to log
def log(message: str):
    """
    Log a message to the console and to discord
    :param message: the message
    """
    to_discord = message
    for color in Kernel.colors:
        to_discord = to_discord.replace(color, " ")
        message = message.replace(color, Kernel.colors[color])
    current_time = datetime.now().strftime("[%D-%M %H:%M:%S] ")
    print(current_time + message, Kernel.colors["END"])
    if Kernel.discord_client is not None:
        Kernel.discord_client.send(f"```\n{current_time + to_discord}\n```")


# Obfuscated, was used to calculate some indicators
def technical_indicators():
    """
    Obfuscated
    """
    pass


# Used to keep main thread alive
def finish():
    """
    Keep main thread alive
    :return:
    """
    app = Flask(__name__)

    @app.route('/')
    def index():
        return "Hello I'm robot"

    if __name__ == '__main__':
        app.run(debug=True, host='0.0.0.0', port=80)

    while True:
        time.sleep(1000)


# Template file used to load the bot
def load_template(path, exchange=ccxt.binance):
    """
    Load a template file
    :param path: path for the file
    :param exchange: binance/ftx..
    :return:
    """
    if exchange == ccxt.binance:
        json_data = {
            "<Token1>": {"key": "<API_KEY1>", "secret": "<API_SECRET1>", "stablecoin": "<stablecoin (usd or usdt)>"
                , "levels": [[60000, 55000, "x%"], [50000, 45000, "x%"], [40000, 35000, "x%"], [30000, 25000, "x%"]
                    , [20000, 15000, "x%"], [10000, 5000, "x%"]]},
            "<Token2>": {"key": "<API_KEY2>", "secret": "<API_SECRET2>", "stablecoin": "<stablecoin (usd or usdt)>"
                , "levels": [[60000, 55000, "x%"], [50000, 45000, "x%"], [40000, 35000, "x%"], [30000, 25000, "x%"]
                    , [20000, 15000, "x%"], [10000, 5000, "x%"]]}}
    else:
        json_data = {
            "API": {"key": "<API_KEY1>", "secret": "<API_SECRET1>"},
            "<Token1>": {"subaccount": "<subaccount name (none for main)>", "stablecoin": "<stablecoin (usd or usdt)>"
                , "levels": [[60000, 55000, "x%"], [50000, 45000, "x%"], [40000, 35000, "x%"], [30000, 25000, "x%"]
                    , [20000, 15000, "x%"], [10000, 5000, "x%"]]},
            "<Token2>": {"subaccount": "<subaccount name (none for main)>", "stablecoin": "<stablecoin (usd or usdt)>"
                , "levels": [[60000, 55000, "x%"], [50000, 45000, "x%"], [40000, 35000, "x%"], [30000, 25000, "x%"]
                    , [20000, 15000, "x%"], [10000, 5000, "x%"]]}}

    json_data = json.dumps(json_data, indent=2)
    with open(path, "w") as file:
        file.write(json_data)


# ----------------------------------------------------------------------------------------------------------------------
# CONST CLASS
# ----------------------------------------------------------------------------------------------------------------------
# Discord colors
class DiscordColors:
    """
    Discord colors
    """
    GREEN = 3066993
    RED = 15158332
    GOLD = 15844367
    BLUE = 3447003


# RGB colors
class RGB:
    """
    RGB colors
    """
    RED = (255, 0, 0)
    GREEN = (0, 255, 0)
    BLUE = (0, 0, 255)
    ORANGE = (255, 165, 0)
    YELLOW = (255, 255, 0)
    PURPLE = (255, 0, 255)
    CYAN = (0, 255, 255)
    WHITE = (255, 255, 255)
    BLACK = (0, 0, 0)


# Status used for each token by the live engine
class TradeStatus:
    """
    Trade status
    """
    LF_BUY_SIGNAL = "Waiting for buy signal"
    LF_SELL_SIGNAL = "Buying & waiting for sell signal"
    UNKNOWN = "Waiting for updates.."
    NO_FUNDS = "Not enough funds"
    NEK = "Not enough klines"


# Status of threads
class ThreadStatus:
    """
    Thread status
    """
    STOPPED = "down"
    RUNNING = "alive"
