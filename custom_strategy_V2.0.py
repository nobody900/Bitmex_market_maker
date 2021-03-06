# -*- coding:utf-8 -*-
import sys
from os.path import getmtime
import logging
import requests
from time import sleep
import datetime
import schedule
import re
import numpy as np
import atexit
import signal

"""
custom_strategy2.0版本
模式种类：
0：Orange_Mode 标准模式，买（3小时内）min5，卖（3小时内）max5
1：Down_Mode 下降模式，买（1小时内）min5，卖MA15
2：Up_Mode 上涨模式，买（1小时内）min5，卖（3小时内）max5
"""
from market_maker.market_maker import OrderManager, XBt_to_XBT, ExchangeInterface
from market_maker.utils import log, constants, errors, math
from telegram_msg import tg_send_message, tg_send_railgun_message, tg_send_important_message, tg_get_updates, tg_get_railgun_updates, tg_get_important_updates

# Used for reloading the bot - saves modified times of key files
import os

LOOP_INTERVAL = 1
STOP_SIZE = 200
START_SIZE_MAGNIFICATION = 1000
BUY_STOP_PRICE = 8900
SELL_STOP_PRICE = 8200
#BASE_URL = "https://testnet.bitmex.com/api/v1/"
#API_KEY = "9NRBliZDL4IaNK8ocye2MUtv"
#API_SECRET = "T-RMbdjYP24sAUvxpbzYrDCGX3JLwXl9PPB7caSXJt1gia7p"
BASE_URL = "https://www.bitmex.com/api/v1/"
#railgun
API_KEY = "xQk9myeZDheUomfcnAIt2sHd"
API_SECRET = "aj-RGh53UjxJWay1NXNPX8y0zdWdABEyX4MeuRaCdBHuQKHm"
#index
ACCOUNT_NAME = "railgun"
#API_KEY = "vZxXmRdnSx8_mHt1jTWECfBi"
#API_SECRET = "dBvP3q6-ar7byQbMan_AalfKWNjVsXGdnp5mhNcWSYe75kuE"

if(ACCOUNT_NAME == "index"):
    tg_send_message_alias = tg_send_message
    tg_get__updates_alias = tg_get_updates
elif(ACCOUNT_NAME == "railgun"):
    tg_send_message_alias = tg_send_railgun_message
    tg_get__updates_alias = tg_get_railgun_updates

#
# Helpers
#
logger = logging.getLogger('root')

class CustomOrderManager(OrderManager):
    def __init__(self):
        self.exchange = ExchangeInterface(base_url=BASE_URL,
                                    apiKey=API_KEY, apiSecret=API_SECRET,
                                    orderIDPrefix="mm_bitmex_", postOnly=False,
                                    timeout=7)
        # Once exchange is created, register exit handler that will always cancel orders
        # on any error.
        atexit.register(self.exit)
        signal.signal(signal.SIGTERM, self.exit)

        logger.info("Using symbol %s." % self.exchange.symbol)

        self.start_time = datetime.datetime.now()
        self.instrument = self.exchange.get_instrument()
        self.starting_qty = self.exchange.get_delta()
        self.running_qty = self.starting_qty
        self.reset()

    def reset(self):
        self.exchange.cancel_all_orders()
        self.sanity_check()
        self.print_status()
        self.position_grade = 0
        self.last_running_qty = 0
        self.reset = True                   #设置初始化标记, 买卖单都变化
        self.stop_order_price = 0        #止损触发价格
        self.stop_market_maker_flag = False     #暂停所有交易, 取消平仓及止损以外所有挂单
        self.cancel_all_orders_flag = False     #取消所有挂单, 并暂停交易
        self.clear_position_flag = False        #清空所有仓位, 并暂停交易
        self.countdown = False                  #延迟挂单计数器, 仓位成交后必须等待60秒后挂新单
        self.delay_order_check = False          #控制是否延迟挂单
        self.restart_flag = False               #防止挂单后延迟生效而产生的重新挂单
        self.buy_only_flag = True              #仅挂买单, 由telegram控制
        self.sell_only_flag = False             #仅挂卖单, 由telegram控制
        self.fast_back_countdown = False        #市场成交后急速回升,120秒内挂MA15平均值单
        self.fast_back_countdown_num = 0
        self.fast_back_over_flag = False
        self.change_order_flag = False          #防止双重下单, 设置为True仅更改订单
        self.stop_price_flag = False            #触发止损后, 挂单标志
        self.last_maxMA15_defference = 0        #wave_coefficient减小但大于0, 仍然上涨的状态下不降低平仓高度
        self.last_minMA15_defference = 0
        self.mode_number = 0
        self.order_counter = 0                  #仓位成交时间计时器, 超过1小时降低平仓价格
        self.last_buy_orders = []
        self.last_sell_orders = []
        self.MA15_list_difference = []

        #持仓方向通过self.running_qty来判断, 大于0为多仓, 小于0为空仓
        schedule.every().day.at("00:00").do(self.write_mybalance) #每天00:00执行一次
        schedule.every(5).seconds.do(self.check_tg_message) #每5秒执行一次检查来自telegram的消息
        schedule.every(5).seconds.do(self.check_double_order) #每5秒执行一次检测是否有重复挂单,发现立即删除
        schedule.every(5).seconds.do(self.set_BXBT_list_60min)   #每5秒执行一次记录最新价, 程序初始化通过BXBT指数来计算MA, 之后改用最新价

        self.BXBT_list_60min = []
        trade_list = self.exchange.bitmex.get_last_trade('.BXBT', 200)
        for trade in trade_list[0:60]:
            print('time: %s price: %s' % (trade['timestamp'], trade['price']))
            for i in range(0, 12):
                self.BXBT_list_60min.append(trade['price'])
        for i in range(0, 180):
            for j in range(0,12):
                self.MA15_list_difference.append(trade_list[i]['price'] - (trade_list[i]['price'] + trade_list[i+1]['price'] + trade_list[i+2]['price'] + trade_list[i+3]['price'] + trade_list[i+4]['price'] + trade_list[i+5]['price'] + trade_list[i+6]['price'] + trade_list[i+7]['price'] + trade_list[i+8]['price'] + trade_list[i+9]['price'] + trade_list[i+10]['price'] + trade_list[i+11]['price'] + trade_list[i+12]['price'] + trade_list[i+13]['price'] + trade_list[i+14]['price'])/15)

        # Create orders and converge.
        with open(r'/root/mybalance2.txt', 'r') as f:
            lines = f.readlines()
            m1 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-1])
            self.yesterday_balance = float(m1.group(3))
            m2 = re.match(r'(\d{4}-\d{2}-\d{2})\s(\d{2}\:\d{2}\:\d{2})\s+([0-9\.]+)', lines[-2])
            self.before_yesterday_balance = float(m2.group(3))
        self.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION    #新算法, 每次初始交易重新设定ORDER_START_SIZE
        print('ORDER_START_SIZE: %s' % self.ORDER_START_SIZE)
        self.place_orders()

    def write_mybalance(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        with open(r'/root/mybalance2.txt', 'a') as f:
            f.write(now.strftime('%Y-%m-%d %H:%M:%S') + '   ' + mybalance + '\n')
        message = 'BitMEX今日交易统计' + ACCOUNT_NAME + '\n' + \
                '时间：' + now.strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
                '保证金余额：' + mybalance + '\n' + \
                '合约数量：' + str(self.running_qty) + '\n' + \
                '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
                '风险等级：' + str(self.position_grade) + '\n' + \
                '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
                '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
                '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
                '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_important_message(message)
        self.before_yesterday_balance = self.yesterday_balance
        self.yesterday_balance = float(mybalance)

    def get_5th_max_MA15_defference(self, getmessage = 5):
        """取得排名第五大的最大值"""
        max_MA15_defference_list = []
        if (getmessage == -2):
            range_num = 6
            list_num = 60
        else:
            range_num = 18
            list_num = 120
        for i in range(0,range_num):
            max_MA15_defference_list.append(max(self.MA15_list_difference[i*list_num:i*list_num+list_num]))
        max1 = max2 = max3 = max4 = max5 = max6 = max7 = max8 = 0
        for i in range(0,range_num):
            if(max_MA15_defference_list[i] > max1):
                max8 = max7
                max7 = max6
                max6 = max5
                max5 = max4
                max4 = max3
                max3 = max2
                max2 = max1
                max1 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max2):
                max8 = max7
                max7 = max6
                max6 = max5
                max5 = max4
                max4 = max3
                max3 = max2
                max2 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max3):
                max8 = max7
                max7 = max6
                max6 = max5
                max5 = max4
                max4 = max3
                max3 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max4):
                max8 = max7
                max7 = max6
                max6 = max5
                max5 = max4
                max4 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max5):
                max8 = max7
                max7 = max6
                max6 = max5
                max5 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max6):
                max8 = max7
                max7 = max6
                max6 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max7):
                max8 = max7
                max7 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] > max8):
                max8 = max_MA15_defference_list[i]
        if(max2 == 0):
            max8 = max7 = max6 = max5 = max4 = max3 = max2 = max1
        elif(max3 == 0):
            max8 = max7 = max6 = max5 = max4 = max3 = max2
        elif(max4 == 0):
            max8 = max7 = max6 = max5 = max4 = max3
        elif(max5 == 0):
            max8 = max7 = max6 = max5 = max4
        elif(max6 == 0):
            max8 = max7 = max6 = max5
        elif(max7 == 0):
            max8 = max7 = max6
        elif(max8 == 0):
            max8 = max7
        print('max1 = %.2f max2 = %.2f max3 = %.2f max4 = %.2f max5 = %.2f max6 = %.2f max7 = %.2f max8 = %.2f' % (max1, max2, max3, max4, max5, max6, max7, max8))
        if(getmessage == 0):
            return ('max1 = %.2f\n max2 = %.2f\n max3 = %.2f\n max4 = %.2f\n max5 = %.2f\n max6 = %.2f\n max7 = %.2f\n max8 = %.2f\n' % (max1, max2, max3, max4, max5, max6, max7, max8))
        elif(getmessage == 1):
            return max1
        elif(getmessage == 2):
            return max2
        elif(getmessage == 3):
            return max3
        elif(getmessage == 4):
            return max4
        elif(getmessage == 5):
            return max5
        elif(getmessage == 6):
            return max6
        elif(getmessage == 7):
            return max7
        elif(getmessage == 8):
            return max8
        elif(getmessage == -1 or getmessage == -2):
            return max1, max2, max3, max4, max5, max6, max7, max8
        else:
            return max5

    def get_5th_min_MA15_defference(self, getmessage = 5):
        """取得排名第五小的最小值"""
        max_MA15_defference_list = []
        if(getmessage == -2):
            range_num = 6
            list_num = 60
        else:
            range_num = 18
            list_num = 120
        for i in range(0,range_num):
            max_MA15_defference_list.append(min(self.MA15_list_difference[i*list_num:i*list_num +list_num]))
        min1 = min2 = min3 = min4 = min5 = min6 = min7 = min8 = 0
        for i in range(0,range_num):
            if(max_MA15_defference_list[i] < min1):
                min8 = min7
                min7 = min6
                min6 = min5
                min5 = min4
                min4 = min3
                min3 = min2
                min2 = min1
                min1 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min2):
                min8 = min7
                min7 = min6
                min6 = min5
                min5 = min4
                min4 = min3
                min3 = min2
                min2 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min3):
                min8 = min7
                min7 = min6
                min6 = min5
                min5 = min4
                min4 = min3
                min3 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min4):
                min8 = min7
                min7 = min6
                min6 = min5
                min5 = min4
                min4 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min5):
                min8 = min7
                min7 = min6
                min6 = min5
                min5 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min6):
                min8 = min7
                min7 = min6
                min6 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min7):
                min8 = min7
                min7 = max_MA15_defference_list[i]
            elif(max_MA15_defference_list[i] < min8):
                min8 = max_MA15_defference_list[i]
        if(min2 == 0):
            min8 = min7 = min6 = min5 = min4 = min3 = min2 = min1
        elif(min3 == 0):
            min8 = min7 = min6 = min5 = min4 = min3 = min2
        elif(min4 == 0):
            min8 = min7 = min6 = min5 = min4 = min3
        elif(min5 == 0):
            min8 = min7 = min6 = min5 = min4
        elif(min6 == 0):
            min8 = min7 = min6 = min5
        elif(min7 == 0):
            min8 = min7 = min6
        elif(min8 == 0):
            min8 = min7
        print('min1 = %.2f min2 = %.2f min3 = %.2f min4 = %.2f min5 = %.2f min6 = %.2f min7 = %.2f min8 = %.2f' % (min1, min2, min3, min4, min5, min6, min7, min8))
        if(getmessage == 0):
            return ('min1 = %.2f\n min2 = %.2f\n min3 = %.2f\n min4 = %.2f\n min5 = %.2f\n min6 = %.2f\n min7 = %.2f\n min8 = %.2f' % (min1, min2, min3, min4, min5, min6, min7, min8))
        elif(getmessage == 1):
            return min1
        elif(getmessage == 2):
            return min2
        elif(getmessage == 3):
            return min3
        elif(getmessage == 4):
            return min4
        elif(getmessage == 5):
            return min5
        elif(getmessage == 6):
            return min6
        elif(getmessage == 7):
            return min7
        elif(getmessage == 8):
            return min8
        elif(getmessage == -1 or getmessage == -2):
            return min1, min2, min3, min4, min5, min6, min7, min8
        else:
            return min5

    def get_avg_MA15_defference_postive(self):
        """获取所有正数偏差值的平均值的1/2"""
        new_list = [i for i in self.MA15_list_difference if i > 1]
        return (np.mean(new_list) / 2)

    def get_avg_MA15_defference_negative(self):
        """获取所有负数偏差值的平均值"""
        new_list = [i for i in self.MA15_list_difference if i < -1]
        return abs(np.mean(new_list) / 2)

    def set_BXBT_list_60min(self):
        self.BXBT_list_60min.pop()
        self.BXBT_list_60min.insert(0, self.get_ticker()['last'])
        price_defference = self.BXBT_list_60min[0] - self.get_BXBT_MA15()
        self.MA15_list_difference.pop()
        self.MA15_list_difference.insert(0, price_defference)

    def get_BXBT_MA30(self):
        return np.mean(self.BXBT_list_60min[0:360])

    def get_BXBT_MA7(self):
        return np.mean(self.BXBT_list_60min[0:84])

    def get_BXBT_MA10(self):
        return np.mean(self.BXBT_list_60min[0:120])

    def get_BXBT_MA15(self):
        return np.mean(self.BXBT_list_60min[0:180])

    def get_BXBT_MA60(self):
        return np.mean(self.BXBT_list_60min)

    def get_avg_more_MA15(self):
        """获取一小时内正数偏差值的平均值"""
        new_list = [i for i in self.MA15_list_difference[0:720] if i > 0]
        return abs(np.mean(new_list)), len(new_list)

    def get_avg_less_MA15(self):
        """获取一小时内负数偏差值的平均值"""
        new_list = [i for i in self.MA15_list_difference[0:720] if i < 0]
        return abs(np.mean(new_list)), len(new_list)

    def select_mode(self):
        """模式选择
        0：Orange_Mode 标准模式，买（3小时内）min5，卖（3小时内）max5
        1：Down_Mode 下降模式，买（1小时内）min5，卖MA15
        2：Up_Mode 上涨模式，买（1小时内）min5，卖（3小时内）max5
        """
        avg_more_MA15, avg_more_length = self.get_avg_more_MA15()
        avg_less_MA15, avg_less_length = self.get_avg_less_MA15()
        print('avg_more_MA15 = %s, avg_less_MA15 = %s, avg_more_length = %s, avg_less_length = %s' % (avg_more_MA15, avg_less_MA15, avg_more_length, avg_less_length))
        if (avg_more_length > 2*avg_less_length):
            return 2
        elif (avg_less_length > 2*avg_more_length):
            return 1
        else:
            return 0    

    def get_wave_coefficient(self):
        """求波动系数, 当前市场波动系数, 超过一定值取消挂单"""
        if (np.mean(self.BXBT_list_60min[0:120]) > self.BXBT_list_60min[0]):      #10分钟内平均指数大于最新指数,下跌,返回负值
            return (min(self.BXBT_list_60min[0:120]) - max(self.BXBT_list_60min[0:120]))
        elif (np.mean(self.BXBT_list_60min[0:120]) < self.BXBT_list_60min[0]):     #10分钟内平均指数小于最新指数,上涨,返回正值
            return (max(self.BXBT_list_60min[0:120]) - min(self.BXBT_list_60min[0:120]))
        else:
            return 0

    def get_buy_stopsize(self):
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        if(avgCostPrice == None):
            return 200
        if(avgCostPrice >= 8800):
            return 100
        elif(avgCostPrice <= 8200):
            return 300
        else:
            return (300 - (avgCostPrice - 8200) / 3)

    def check_last_price_upordown(self):
        ret = 0
        if(self.running_qty < 0):
            if self.BXBT_list_60min[0] >= self.BXBT_list_60min[1]:
                ret = ret + 1
            if self.BXBT_list_60min[1] >= self.BXBT_list_60min[2]:
                ret = ret + 1
            if self.BXBT_list_60min[2] >= self.BXBT_list_60min[3]:
                ret = ret + 1
            if self.BXBT_list_60min[3] >= self.BXBT_list_60min[4]:
                ret = ret + 1
            if self.BXBT_list_60min[4] >= self.BXBT_list_60min[5]:
                ret = ret + 1
        elif(self.running_qty > 0):
            if self.BXBT_list_60min[0] <= self.BXBT_list_60min[1]:
                ret = ret + 1
            if self.BXBT_list_60min[1] <= self.BXBT_list_60min[2]:
                ret = ret + 1
            if self.BXBT_list_60min[2] <= self.BXBT_list_60min[3]:
                ret = ret + 1
            if self.BXBT_list_60min[3] <= self.BXBT_list_60min[4]:
                ret = ret + 1
            if self.BXBT_list_60min[4] <= self.BXBT_list_60min[5]:
                ret = ret + 1
        else:
            if(self.last_running_qty < 0):
                if self.BXBT_list_60min[0] <= self.BXBT_list_60min[1]:
                    ret = ret + 1
                if self.BXBT_list_60min[1] <= self.BXBT_list_60min[2]:
                    ret = ret + 1
                if self.BXBT_list_60min[2] <= self.BXBT_list_60min[3]:
                    ret = ret + 1
                if self.BXBT_list_60min[3] <= self.BXBT_list_60min[4]:
                    ret = ret + 1
                if self.BXBT_list_60min[4] <= self.BXBT_list_60min[5]:
                    ret = ret + 1
            elif(self.last_running_qty > 0):
                if self.BXBT_list_60min[0] >= self.BXBT_list_60min[1]:
                    ret = ret + 1
                if self.BXBT_list_60min[1] >= self.BXBT_list_60min[2]:
                    ret = ret + 1
                if self.BXBT_list_60min[2] >= self.BXBT_list_60min[3]:
                    ret = ret + 1
                if self.BXBT_list_60min[3] >= self.BXBT_list_60min[4]:
                    ret = ret + 1
                if self.BXBT_list_60min[4] >= self.BXBT_list_60min[5]:
                    ret = ret + 1
            else:
                return False
        if ret >= 4:
            return True
        else:
            return False

    def check_tg_message(self):
        """检查是否有来自telegram的消息,并处理"""
        tg_message = tg_get__updates_alias()
        if (tg_message == None):
            return
        elif (tg_message == '/new'):
            self.send_tg_message()
        elif (tg_message == '/order'):
            self.send_tg_order_message()
        elif (tg_message == '/select_mode'):
            if(self.mode_number == 0):
                tg_send_message_alias('标准模式')
            elif(self.mode_number == 1):
                tg_send_message_alias('下降模式')
            elif(self.mode_number == 2):
                tg_send_message_alias('上涨模式')
        elif (tg_message == '/get_maxmin'):
            tg_send_message_alias(self.get_5th_max_MA15_defference(getmessage = 0) + self.get_5th_min_MA15_defference(getmessage = 0))
        elif (tg_message == '/wave_coefficient'):
            wave_coefficient = self.get_wave_coefficient()
            tg_send_message_alias('wave_coefficient is %.2f now' % wave_coefficient)
        elif (tg_message == '/bxbt_ma7'):
            BXBT_MA7 = self.get_BXBT_MA7()
            tg_send_message_alias('BXBT_MA7 is %.2f now' % BXBT_MA7)
        elif (tg_message == '/bxbt_ma10'):
            BXBT_MA10 = self.get_BXBT_MA10()
            tg_send_message_alias('BXBT_MA10 is %.2f now' % BXBT_MA10)
        elif (tg_message == '/bxbt_ma15'):
            BXBT_MA15 = self.get_BXBT_MA15()
            tg_send_message_alias('BXBT_MA15 is %.2f now' % BXBT_MA15)
        elif (tg_message == '/check_important'):
            ret = self.check_tg_important_message()
            if (ret != None):
                tg_send_message_alias(ret)
            else:
                tg_send_message_alias('未执行命令')
        else:
            return

    def check_tg_important_message(self):
        tg_important_message = tg_get_important_updates()
        if (tg_important_message == None):
            return None
        elif (tg_important_message == '/stop_market_maker2'):
            self.stop_market_maker_flag = True
            return '执行stop_market_maker2'
        elif (tg_important_message == '/start_market_maker2'):
            self.stop_market_maker_flag = False
            self.cancel_all_orders_flag = False
            self.clear_position_flag = False
            self.stop_price_flag = False
            self.stop_order_price = 0
            self.reset = True
            return '执行start_market_maker2'
        elif (tg_important_message == '/cancel_all_orders2'):
            self.cancel_all_orders_flag = True
            self.stop_market_maker_flag = True
            self.clear_position_flag = False
            return '执行cancel_all_orders2'
        elif (tg_important_message == '/clear_position2'):
            self.clear_position_flag = True
            self.stop_market_maker_flag = True
            self.cancel_all_orders_flag = False
            return '执行clear_position2'
        elif (tg_important_message == '/buy_only2'):
            self.buy_only_flag = True
            self.sell_only_flag = False
            return '执行buy_only2'
        elif (tg_important_message == '/sell_only2'):
            self.buy_only_flag = False
            self.sell_only_flag = True
            return '执行sell_only2'
        elif (tg_important_message == '/cancel_buysell_only2'):
            self.buy_only_flag = False
            self.sell_only_flag = False
            return '执行cancel_buysell_only2'
        else:
            return None

    def get_position_grade(self):
        """获取仓位等级"""
        self.position_grade = abs(self.running_qty) // (self.ORDER_START_SIZE//4)
        if self.position_grade > 6:
            self.position_grade = 6
        return self.position_grade

    def get_price_offset2(self, index):
        """根据index依次设置每一个价格，这里为差价依次增大"""
        #L = [2, 5, 9, 15, 24, 40, 70, 100]
        L = [10, 20, 105]
        if abs(index) > 3:
            logger.error("index cannot over 3")
            self.exit()

        BXBT_MA15 = self.get_BXBT_MA15()
        BXBT_MA60 = self.get_BXBT_MA60()
        wave_coefficient = self.get_wave_coefficient()
        if(self.mode_number == 0):      #标准模式，买（3小时内）min5，卖（3小时内）max5，超过1小时卖MA15
            if(BXBT_MA15 > BXBT_MA60 and self.buy_only_flag == True):
                BXBT_MA15 = BXBT_MA60
            max_MA15_defference = self.get_5th_max_MA15_defference(getmessage = 5)
            min_MA15_defference = self.get_5th_min_MA15_defference(getmessage = 5)
        elif(self.mode_number == 1):    #下降模式，买MA15-100，卖MA15
            max_MA15_defference = 20
            min_MA15_defference = -100
        elif(self.mode_number == 2):    #上涨模式，买（1小时内）min5，卖（3小时内）max1
            min1, min2, min3, min4, min5, min6, min7, min8 = self.get_5th_min_MA15_defference(getmessage = -2)
            max_MA15_defference = 100
            min_MA15_defference = min5
        else:
            print('Error mode_number')
            self.exit()
        """
        max1, max2, max3, max4, max5, max6, max7, max8 = self.get_5th_max_MA15_defference(getmessage = -1)
        min1, min2, min3, min4, min5, min6, min7, min8 = self.get_5th_min_MA15_defference(getmessage = -1)
        BXBT_MA60 = self.get_BXBT_MA60()

        if(wave_coefficient > max1):
            max_MA15_defference = max1
        elif(wave_coefficient > max2):
            max_MA15_defference = max2
        elif(wave_coefficient > max3):
            max_MA15_defference = max3
        elif(wave_coefficient > max4):
            max_MA15_defference = max4
        else:
            max_MA15_defference = max5
        if(wave_coefficient < min1):
            min_MA15_defference = min1
        elif(wave_coefficient < min2):
            min_MA15_defference = min2
        elif(wave_coefficient < min3):
            min_MA15_defference = min3
        elif(wave_coefficient < min4):
            min_MA15_defference = min4
        else:
            min_MA15_defference = min5

        if(max1 > 1.8 * max_MA15_defference):
            max_MA15_defference = max1 / 1.8
        if(min1 < 1.8 * min_MA15_defference):
            min_MA15_defference = min1/1.8
        if(BXBT_MA15 > BXBT_MA60):
            min_MA15_defference = 2 * min_MA15_defference
        else:
            max_MA15_defference = 2 * max_MA15_defference
        """
        if index > 0:
            L[0] = max_MA15_defference
            L[1] = 2 * L[0]
            if(BXBT_MA15 + L[index - 1] < self.start_position_sell):
                return math.toNearest(self.start_position_sell, self.instrument['tickSize'])
            else:
                return math.toNearest(BXBT_MA15 + L[index - 1], self.instrument['tickSize'])
        elif index < 0:
            L[0] = abs(min_MA15_defference)
            L[1] = 2 * L[0]
            if(BXBT_MA15 - L[abs(index) - 1] > self.start_position_buy):
                return math.toNearest(self.start_position_buy, self.instrument['tickSize'])
            else:
                return math.toNearest(BXBT_MA15 - L[abs(index) - 1], self.instrument['tickSize'])
        else:
            logger.error("offset2_index(%s) cannot 0" % index)
            self.exit()

    def get_price_offset3(self, index):
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        if(avgCostPrice == None):
            return None
        BXBT_MA15 = self.get_BXBT_MA15()
        if(self.mode_number == 0):      #标准模式，买（3小时内）min5，卖（3小时内）max5，超过1小时卖MA15
            max_MA15_defference = self.get_5th_max_MA15_defference(getmessage = 5)
            min_MA15_defference = self.get_5th_min_MA15_defference(getmessage = 5)
            if(self.order_counter > 3600):
                max_MA15_defference = 0
                min_MA15_defference = 0
        elif(self.mode_number == 1):    #下降模式，买MA15-100，卖MA15
            max_MA15_defference = 0
            min_MA15_defference = self.get_5th_min_MA15_defference(getmessage = 1)
        elif(self.mode_number == 2):    #上涨模式，买（1小时内）min5，卖（3小时内）max1
            max_MA15_defference = self.get_5th_max_MA15_defference(getmessage = 1)
            min_MA15_defference = 0
        else:
            print('Error mode_number')
            self.exit()
        """
        if(self.order_counter > 3600):
            max_MA15_defference = 0
            min_MA15_defference = 0
        else:
            wave_coefficient = self.get_wave_coefficient()
            max1, max2, max3, max4, max5, max6, max7, max8 = self.get_5th_max_MA15_defference(getmessage = -1)
            min1, min2, min3, min4, min5, min6, min7, min8 = self.get_5th_min_MA15_defference(getmessage = -1)
            if(wave_coefficient > max1):
                max_MA15_defference = max1
            elif(wave_coefficient > max2):
                max_MA15_defference = max2
            elif(wave_coefficient > max3):
                max_MA15_defference = max3
            elif(wave_coefficient > max4):
                max_MA15_defference = max4
            else:
                max_MA15_defference = max5
            if(wave_coefficient > 0 and self.last_maxMA15_defference > max_MA15_defference):
                max_MA15_defference = self.last_maxMA15_defference
            self.last_maxMA15_defference = max_MA15_defference
            if(wave_coefficient < min1):
                min_MA15_defference = min1
            elif(wave_coefficient < min2):
                min_MA15_defference = min2
            elif(wave_coefficient < min3):
                min_MA15_defference = min3
            elif(wave_coefficient < min4):
                min_MA15_defference = min4
            else:
                min_MA15_defference = min5
            if(wave_coefficient < 0 and self.last_minMA15_defference < min_MA15_defference):
                min_MA15_defference = self.last_minMA15_defference
            self.last_minMA15_defference = min_MA15_defference
            min_MA15_defference = abs(min_MA15_defference)
        """
        if(self.order_counter < 60*20):
            add_price = 30
        elif(self.order_counter < 60*50):
            add_price = 30 - (self.order_counter-60*20)//60
        else:
            add_price = 0
        if index > 0:
            if(self.mode_number == 0):
                return math.toNearest(avgCostPrice + add_price, self.instrument['tickSize'])
            else:
                if((BXBT_MA15 + (self.get_avg_MA15_defference_postive() if self.buy_only_flag == False else max_MA15_defference)) < avgCostPrice+1):
                    return math.toNearest(avgCostPrice + index, self.instrument['tickSize'])
                elif(self.fast_back_over_flag == False):
                    return math.toNearest(BXBT_MA15 + self.get_avg_MA15_defference_postive(), self.instrument['tickSize'])
                else:
                    return math.toNearest(BXBT_MA15 + (self.get_avg_MA15_defference_postive() if self.buy_only_flag == False else max_MA15_defference), self.instrument['tickSize'])
        elif index < 0:
            if(self.mode_number == 0):
                return math.toNearest(avgCostPrice - add_price, self.instrument['tickSize'])
            else:
                if(avgCostPrice-1 < (BXBT_MA15 - (self.get_avg_MA15_defference_negative() if self.sell_only_flag == False else min_MA15_defference))):
                    return math.toNearest(avgCostPrice - abs(index), self.instrument['tickSize'])
                elif(self.fast_back_over_flag == False):
                    return math.toNearest(BXBT_MA15 - self.get_avg_MA15_defference_postive(), self.instrument['tickSize'])
                else:
                    return math.toNearest(BXBT_MA15 - (self.get_avg_MA15_defference_negative() if self.sell_only_flag == False else min_MA15_defference), self.instrument['tickSize'])
        else:
            logger.error("offset3_index(%s) cannot 0" % index)
            self.exit()


    def place_orders(self):
        """Create order items for use in convergence."""
        buy_orders = []
        sell_orders = []
        buy_stop_order = {}
        sell_stop_order = {}
        order_status = 0
        """order_status参数说明
            0: running_qty为0, 维持原样
            1: self.running_qty > 0, 买卖都变化, 买单按照offset2, 卖单按照offset3
            2: 买单维持不变, 卖单按照offset3
            3: self.running_qty < 0, 买卖都变化, 买单按照offset3, 卖单按照offset2
            4: 卖单维持不变, 买单按照offset3
            5: 追加指定订单
            6: 取消指定订单
            7: self.running_qty > 0, 买单按照offset2, 卖单不变
            8: self.running_qty < 0, 买单不变, 卖单按照offset2
            9: self.running_qty > 0, 买单维持不变, 卖单变化, 不追加
            10: self.running_qty < 0, 卖单维持不变, 买单变化, 不追加
            11: 订单变化, 不可追加
        """
        # Create orders from the outside in. This is intentional - let's say the inner order gets taken;
        # then we match orders from the outside in, ensuring the fewest number of orders are amended and only
        # a new order is created in the inside. If we did it inside-out, all orders would be amended
        # down and a new order would be created at the outside.
        position_grade = self.get_position_grade()
        avgCostPrice = self.exchange.get_position()['avgCostPrice']
        print ('position_grade: %s ' % position_grade)
        print ('running_qty: %s ' % self.running_qty)
        print ('ORDER_START_SIZE: %s ' % self.ORDER_START_SIZE)
        print ('wave_coefficient: %s ' % self.get_wave_coefficient())
        self.mode_number = self.select_mode()
        print ('select_mode: %s ' % self.mode_number)
        
        schedule.run_pending()

        if(self.countdown == True):                #设置倒数计时, 60秒后delay_order_check设为True, 可以重新挂非清仓方向的价格
            self.cycleclock = self.cycleclock - 1
            if(self.cycleclock <= 0):
                if(self.check_last_price_upordown() == True):
                    self.cycleclock = 5
                else:
                    self.countdown = False
                    self.delay_order_check = True

        if(self.fast_back_countdown == True):
            self.fast_back_countdown_num = self.fast_back_countdown_num - 1
            if(self.fast_back_countdown_num <= 0):
                self.fast_back_countdown_num = 0
                self.fast_back_over_flag = True
                self.fast_back_countdown == False

        if(self.get_ticker()['last'] > BUY_STOP_PRICE and self.buy_only_flag == True):
            self.sell_only_flag = True
            self.buy_only_flag = False
        elif(self.get_ticker()['last'] < SELL_STOP_PRICE and self.sell_only_flag == True):
            self.buy_only_flag = True
            self.sell_only_flag = False

        if(self.get_5th_max_MA15_defference(getmessage = 1) > 100 and self.stop_market_maker_flag == False):
            self.stop_market_maker_flag = True
            self.cancel_all_orders_flag = True
            tg_send_important_message('上涨差值超过100，暂停交易')

        if(self.get_ticker()['last'] < (self.stop_order_price+50) and self.running_qty == 0 and self.stop_market_maker_flag == False):
            self.stop_market_maker_flag = True
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
            buy_orders.append({'price': self.stop_order_price - 200, 'orderQty': self.ORDER_START_SIZE, 'side': "Buy"})
            tg_send_important_message('触发止损，暂停交易')
            self.stop_price_flag = True

        elif(self.stop_market_maker_flag == True and self.cancel_all_orders_flag == True):
            if (len(self.exchange.get_orders()) != 0):
                self.exchange.cancel_all_orders()
            logger.info("Cancel all orders")
        elif(self.stop_market_maker_flag == True and self.clear_position_flag == True):
            if(self.running_qty != 0):
                self.clear_position(buy_orders, sell_orders)
            else:
                if (len(self.exchange.get_orders()) != 0):
                    self.exchange.cancel_all_orders()
                logger.info("Market_maker has stopped. No orders, no positions now")
        elif(self.stop_market_maker_flag == True):
            if(self.running_qty > 0):
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - self.get_buy_stopsize(), self.instrument['tickSize']), "Sell", abs(self.running_qty))
                order_status = 4
            elif(self.running_qty < 0):
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy", abs(self.running_qty))
                order_status = 2
            elif(self.running_qty == 0 and self.last_running_qty == 0):
                if(self.stop_price_flag == True):
                    pass
                elif (len(self.exchange.get_orders()) != 0):
                    self.exchange.cancel_all_orders()
                logger.info("Market_maker has stopped. No orders, no positions now")

        elif(self.running_qty == 0 and self.restart_flag == False):
            """
            if(self.check_last_price_upordown() == True):
                self.restart_flag = True
                self.countdown_restart = 5
                return
            """
            self.ORDER_START_SIZE = self.start_XBt // 1000000 * START_SIZE_MAGNIFICATION    #新算法, 每次初始交易重新设定ORDER_START_SIZE
            order_status = 0
            self.order_counter = 0
            if not(self.sell_only_flag == True):
                buy_orders.append(self.prepare_order(-1, order_status))
            if not(self.buy_only_flag == True):
                sell_orders.append(self.prepare_order(1, order_status))
            self.countdown = False
            self.restart_flag = True
            self.countdown_restart = 30
            self.fast_back_over_flag = False
            if(self.change_order_flag == True):
                order_status = 11
            self.change_order_flag = True
            if(self.last_running_qty > 0):
                tg_send_message_alias('最后成交价格：%s' % self.last_sell_orders[0]['price'])
            elif(self.last_running_qty < 0):
                tg_send_message_alias('最后成交价格：%s' % self.last_buy_orders[0]['price'])

        elif(self.running_qty == 0 and self.restart_flag == True):
            self.countdown_restart = self.countdown_restart - 1
            if(self.countdown_restart <= 0):
                self.restart_flag = False
            return

        #elif(self.running_qty != 0 and self.running_qty != self.last_running_qty):                #(只有1级仓位, 不追加仓位)仓位变动后开始倒计时60秒, 60秒后delay_order_check为True, 可以重新挂非清仓方向的价格
        elif(self.running_qty != 0 and self.last_running_qty == 0):
            if(self.running_qty > 0):
                order_status = 2
                sell_orders.append(self.prepare_order(1, order_status))
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - self.get_buy_stopsize(), self.instrument['tickSize']), "Sell", abs(self.running_qty))
            elif(self.running_qty < 0):
                order_status = 4
                buy_orders.append(self.prepare_order(-1, order_status))
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy", abs(self.running_qty))
            #self.cycleclock = 60
            #self.countdown = True
            self.restart_flag = False
            #self.delay_order_check = False
            self.change_order_flag = False
            if(self.fast_back_over_flag == False and self.fast_back_countdown_num == 0):
                self.fast_back_countdown_num = 120
                self.fast_back_countdown = True

        elif(self.running_qty != 0 and self.running_qty == self.last_running_qty and self.delay_order_check == True and False):                 #可以重新挂非清仓方向的价格, 目前只有一级仓位，所以不需要
            i = abs(self.running_qty) // (self.ORDER_START_SIZE) + 1
            if(self.running_qty > 0):
                order_status = 7
                if(i <= 1):
                    buy_orders.append(self.prepare_order(-i, order_status))
            if(self.running_qty < 0):
                order_status = 8
                if(i <= 1):
                    sell_orders.append(self.prepare_order(i, order_status))
            self.cycleclock = 30
            self.countdown = True
            self.delay_order_check = False

        else:
            self.order_counter = self.order_counter + 1
            if(self.running_qty > 0):
                if(self.running_qty != self.ORDER_START_SIZE):      #部分成交, 不操作等待全部成交
                    return
                if(self.reset == True):
                    order_status = 0
                else:
                    order_status = 9
                sell_orders.append(self.prepare_order(1, order_status))
                if avgCostPrice != None:
                    sell_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice - self.get_buy_stopsize(), self.instrument['tickSize']), "Sell", abs(self.running_qty))
            elif(self.running_qty < 0):
                if(abs(self.running_qty) != self.ORDER_START_SIZE):      #部分成交, 不操作等待全部成交
                    return
                if(self.reset == True):
                    order_status = 0
                else:
                    order_status = 10
                buy_orders.append(self.prepare_order(-1, order_status))
                if avgCostPrice != None:
                    buy_stop_order = self.prepare_stop_order(math.toNearest(avgCostPrice + STOP_SIZE, self.instrument['tickSize']), "Buy", abs(self.running_qty))

        if(self.last_running_qty != self.running_qty):
            self.send_tg_message()
        self.last_running_qty = self.running_qty
        self.reset = False
        buy_orders = list(filter(None.__ne__, buy_orders))      #去除None
        sell_orders = list(filter(None.__ne__, sell_orders))    #去除None
        print('BXBT_MA15: %s' % self.get_BXBT_MA15())
        print(buy_orders)
        print(sell_orders)
        if((self.last_buy_orders == buy_orders and self.last_sell_orders == sell_orders) or (buy_orders == [] and sell_orders == [])):
            print('order no change, return')
            return
        else:
            self.last_buy_orders = buy_orders
            self.last_sell_orders = sell_orders
        self.converge_stop_order(buy_stop_order, sell_stop_order)
        return self.converge_orders(buy_orders, sell_orders, order_status)


    def clear_position(self, buy_orders, sell_orders):
        """清空所有仓位"""
        if (self.running_qty > 0):
            sell_orders.append({'price': self.start_position_buy - 1, 'orderQty': self.running_qty, 'side': "Sell"})
        elif (self.running_qty < 0):
            buy_orders.append({'price': self.start_position_sell + 1, 'orderQty': abs(self.running_qty), 'side': "Buy"})

    def prepare_order(self, index, order_status):
        """Create an order object."""
        if(self.running_qty > 0 and index > 0):
            quantity = self.running_qty
            price = self.get_price_offset3(index)
        elif(self.running_qty < 0 and index < 0):
            quantity = abs(self.running_qty)
            price = self.get_price_offset3(index)
        else:
            quantity = self.ORDER_START_SIZE
            price = self.get_price_offset2(index)
        if (price == None):
            return None
        else:
            return {'price': price, 'orderQty': quantity, 'side': "Buy" if index < 0 else "Sell"}

    def prepare_stop_order(self, price, side, orderqty):
        if((price < self.get_ticker()['last']) and (side == 'Buy')):
            price = self.get_ticker()['last'] + 0.5
        elif((price > self.get_ticker()['last']) and (side == 'Sell')):
            price = self.get_ticker()['last'] - 0.5
        self.stop_order_price = price
        return {'stopPx': price, 'orderQty': orderqty, 'side': side}

    def check_double_order(self):
        """检测是否有重复挂单, 发现价格一样的重复挂单删除"""
        to_cancel = []
        def get_price(order):
            if(order['ordType'] == 'Stop'):
                return float(order['stopPx'])
            else:
                return float(order['price'])
        existing_orders = sorted(self.exchange.get_orders(), key=get_price, reverse=True)   #对订单进行排序
        if(len(existing_orders) == 0):
            return
        order_target = {'price' : 0, 'ordType' : '', 'side' : '', 'stopPx' : 0}
        for order in existing_orders:
            if (order['ordType'] == 'Limit' and order_target['price'] == order['price'] and order_target['ordType'] == order['ordType'] and order_target['side'] == order['side']):
                to_cancel.append(order)
            elif(order['ordType'] == 'Stop' and order_target['stopPx'] == order['stopPx'] and order_target['ordType'] == order['ordType'] and order_target['side'] == order['side']):
                to_cancel.append(order)
            order_target = order
        if len(to_cancel) > 0:
            logger.info("Canceling stop %d orders:" % (len(to_cancel)))
            self.exchange.cancel_bulk_orders(to_cancel)

    def converge_stop_order(self, buy_stop_order, sell_stop_order):
        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()
        for order in existing_orders:
            if order['ordType'] != 'Stop':
                continue
            try:
                if(order['side'] == 'Buy'):
                    if(len(buy_stop_order) == 0):
                        to_cancel.append(order)
                        continue
                    else:
                        desired_order = buy_stop_order
                        buys_matched += 1
                elif (order['side'] == 'Sell'):
                    if(len(sell_stop_order) == 0):
                        to_cancel.append(order)
                        continue
                    else:
                        desired_order = sell_stop_order
                        sells_matched += 1
                else:
                    continue
                if desired_order['orderQty'] != order['leavesQty'] or (desired_order['stopPx'] != order['stopPx']):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'], 'stopPx': desired_order['stopPx'], 'side': order['side']})
            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                to_cancel.append(order)
        if(len(buy_stop_order) > 0 and buys_matched < 1):
            self.exchange.bitmex.buy_stop(buy_stop_order['orderQty'], buy_stop_order['stopPx'])
        if(len(sell_stop_order) > 0 and sells_matched < 1):
            self.exchange.bitmex.sell_stop(sell_stop_order['orderQty'], sell_stop_order['stopPx'])

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending stop %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['stopPx'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['stopPx'],
                    tickLog, (amended_order['stopPx'] - reference_order['stopPx'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                errorObj = e.response.json()
                if errorObj['error']['message'] == 'Invalid ordStatus':
                    logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                    sleep(0.5)
                    return self.place_orders()
                else:
                    logger.error("Unknown error on amend: %s. Exiting" % errorObj)
                    sys.exit(1)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling stop %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['stopPx']))
            self.exchange.cancel_bulk_orders(to_cancel)


    def converge_orders(self, buy_orders, sell_orders, order_status):
        """Converge the orders we currently have in the book with what we want to be in the book.
           This involves amending any open orders and creating new ones if any have filled completely.
           We start from the closest orders outward."""

        tickLog = self.exchange.get_instrument()['tickLog']
        to_amend = []
        to_create = []
        to_cancel = []
        buys_matched = 0
        sells_matched = 0
        existing_orders = self.exchange.get_orders()

        # Check all existing orders and match them up with what we want to place.
        # If there's an open one, we might be able to amend it to fit what we want.
        for order in existing_orders:
            if order['ordType'] != 'Limit':
                continue
            try:
                if (order['side'] == 'Buy' and (order_status == 0 or order_status == 11 or order_status == 4 or order_status == 3 or order_status == 1 or order_status == 7 or order_status == 10)):
                    desired_order = buy_orders[buys_matched]
                    buys_matched += 1
                elif (order['side'] == 'Sell' and (order_status == 0 or order_status == 11 or order_status == 2 or order_status == 1 or order_status == 3 or order_status == 8 or order_status == 9)):
                    desired_order = sell_orders[sells_matched]
                    sells_matched += 1
                elif (order['price'] == buy_orders[buys_matched]['price'] and order_status == 6):
                    to_cancel.append(order)
                    buys_matched += 1
                    continue
                elif (order['price'] == sell_orders[sells_matched]['price'] and order_status == 6):
                    to_cancel.append(order)
                    sells_matched += 1
                    continue
                else:
                    continue

                # Found an existing order. Do we need to amend it?
                if desired_order['orderQty'] != order['leavesQty'] or (
                        # If price has changed, and the change is more than our RELIST_INTERVAL, amend.
                        desired_order['price'] != order['price'] and
                        abs((desired_order['price'] / order['price']) - 1) > 0):
                    to_amend.append({'orderID': order['orderID'], 'orderQty': order['cumQty'] + desired_order['orderQty'],
                                     'price': desired_order['price'], 'side': order['side']})
                # Found an stop existing order. Do we need to amend it?

            except IndexError:
                # Will throw if there isn't a desired order to match. In that case, cancel it.
                if ((order_status == 2 and order['side'] == 'Sell') or (order_status == 1 and self.running_qty > 0) or (order_status == 4 and order['side'] == 'Buy') or (order_status == 3 and self.running_qty < 0) or (order_status == 7 and order['side'] == 'Buy') or (order_status == 8 and order['side'] == 'Sell') or (order_status == 9 and order['side'] == 'Sell') or (order_status == 10 and order['side'] == 'Buy')):
                    to_cancel.append(order)

        if (order_status == 0 or order_status == 4 or order_status == 3 or order_status == 1 or order_status == 5 or order_status == 7):
            while buys_matched < len(buy_orders):
                to_create.append(buy_orders[buys_matched])
                buys_matched += 1
        if (order_status == 0 or order_status == 2 or order_status == 1 or order_status == 3 or order_status == 5 or order_status == 8):
            while sells_matched < len(sell_orders):
                to_create.append(sell_orders[sells_matched])
                sells_matched += 1

        if len(to_amend) > 0:
            for amended_order in reversed(to_amend):
                reference_order = [o for o in existing_orders if o['orderID'] == amended_order['orderID']][0]
                logger.info("Amending %4s: %d @ %.*f to %d @ %.*f (%+.*f)" % (
                    amended_order['side'],
                    reference_order['leavesQty'], tickLog, reference_order['price'],
                    (amended_order['orderQty'] - reference_order['cumQty']), tickLog, amended_order['price'],
                    tickLog, (amended_order['price'] - reference_order['price'])
                ))
            # This can fail if an order has closed in the time we were processing.
            # The API will send us `invalid ordStatus`, which means that the order's status (Filled/Canceled)
            # made it not amendable.
            # If that happens, we need to catch it and re-tick.
            try:
                self.exchange.amend_bulk_orders(to_amend)
            except requests.exceptions.HTTPError as e:
                logger.warn("Amending failed. Waiting for order data to converge and retrying.")
                sleep(5)
                return self.place_orders()

        if len(to_create) > 0:
            logger.info("Creating %d orders:" % (len(to_create)))
            for order in reversed(to_create):
                logger.info("%4s %d @ %.*f" % (order['side'], order['orderQty'], tickLog, order['price']))
            self.exchange.create_bulk_orders(to_create)

        # Could happen if we exceed a delta limit
        if len(to_cancel) > 0:
            logger.info("Canceling %d orders:" % (len(to_cancel)))
            for order in reversed(to_cancel):
                logger.info("%4s %d @ %.*f" % (order['side'], order['leavesQty'], tickLog, order['price']))
            self.exchange.cancel_bulk_orders(to_cancel)

    def send_tg_message(self):
        now = datetime.datetime.now()
        mybalance = '%.6f' % XBt_to_XBT(self.start_XBt)
        message = 'BitMEX交易状态' + ACCOUNT_NAME + '\n' + ('暂停交易\n' if self.stop_market_maker_flag == True else '') + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '保证金余额：' + mybalance + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice']) + '\n' + \
            '今日盈利：' + '%.6f' % (float(mybalance) - self.yesterday_balance) + '\n' + \
            '作日盈利：' + '%.6f' % (self.yesterday_balance - self.before_yesterday_balance)
        tg_send_message_alias(message)
        if self.position_grade > 4:
            tg_send_important_message(message)

    def send_tg_order_message(self):
        def get_price(order):
            if(order['ordType'] == 'Stop'):
                return float(order['stopPx'])
            else:
                return float(order['price'])

        message = 'BitMEX委托状态' + ACCOUNT_NAME + '\n'
        existing_orders = sorted(self.exchange.get_orders(), key=get_price, reverse=True)
        for order in existing_orders:
            if (order['ordType'] == 'Stop'):
                message = message + '%s %d @ %s %s\n' % (order['side'], order['leavesQty'], order['stopPx'], order['ordType'])
            else:
                message = message + '%s %d @ %s %s\n' % (order['side'], order['leavesQty'], order['price'], order['ordType'])
        tg_send_message_alias(message)

    def run_loop(self):
        while True:
            sys.stdout.write("-----\n")
            sys.stdout.flush()

            self.check_file_change()
            sleep(LOOP_INTERVAL)

            # This will restart on very short downtime, but if it's longer,
            # the MM will crash entirely as it is unable to connect to the WS on boot.
            if not self.check_connection():
                logger.error("Realtime data connection unexpectedly closed, restarting.")
                self.restart()

            self.sanity_check()  # Ensures health of mm - several cut-out points here
            self.print_status()  # Print skew, delta, etc
            self.place_orders()  # Creates desired orders and converges to existing orders

    def exit(self):
        logger.info("Shutting down. All open orders will be cancelled.")
        now = datetime.datetime.now()
        message = 'BitMEX交易机器人2异常退出\n' + \
            '时间：' + now.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + '\n' + \
            '合约数量：' + str(self.running_qty) + '\n' + \
            '开仓价格：' + str(self.exchange.get_position()['avgCostPrice']) + '\n' + \
            '风险等级：' + str(self.position_grade) + '\n' + \
            '最新价格：' + str(self.get_ticker()['last']) + '\n' + \
            '指数价格：' + str(self.exchange.get_portfolio()['XBTUSD']['markPrice'])
        tg_send_important_message(message)
        try:
            self.exchange.cancel_all_orders()
            self.exchange.bitmex.exit()
        except errors.AuthenticationError as e:
            logger.info("Was not authenticated; could not cancel orders.")
        except Exception as e:
            logger.info("Unable to cancel orders: %s" % e)

        sys.exit()


def run() -> None:
    order_manager = CustomOrderManager()

    # Try/except just keeps ctrl-c from printing an ugly stacktrace
    try:
        order_manager.run_loop()
    except (KeyboardInterrupt, SystemExit):
        sys.exit()
