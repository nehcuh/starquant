#!/usr/bin/env python
# -*- coding: utf-8 -*-
from queue import Queue, Empty
from threading import Thread
from nanomsg import Socket, PAIR, SUB, PUB, PUSH,SUB_SUBSCRIBE, AF_SP,SOL_SOCKET,RCVTIMEO
from datetime import datetime, timedelta
import os,sys
import yaml
from collections import defaultdict
from copy import copy
import traceback
import importlib
from typing import Any, Callable
from pathlib import Path
from time import sleep
from ..api.ctp_constant import THOST_FTDC_PT_Net
from ..common.datastruct import *
from ..common.utility import *
from source.data.data_board import BarGenerator
from ..data import database_manager
from ..engine.iengine import BaseEngine


class RecorderEngine(BaseEngine):
    """
    market data recorder 
    """
    config_filename = "config_server.yaml"
    setting_filename = "data_recorder_setting.json"

# init    
    def __init__(self,configfile:str = '',gateway:str = "CTP.MD"):
        super(RecorderEngine, self).__init__()
        """
        two sockets to send and recv msg
        """
        self.__active = False
        self.id = os.getpid()
        self.engine_type = EngineType.LIVE     
        self._recv_sock = Socket(SUB)
        self._send_sock = Socket(PUSH)

        if configfile:            
            self.config_filename = configfile
        if gateway:
            self.gateway = gateway
        filepath = Path.cwd().joinpath("etc/" + self.config_filename)
        with open(filepath, encoding='utf8') as fd:
            self._config = yaml.load(fd)

        self.tick_recordings = {}
        self.bar_recordings = {}
        self.bar_generators = {}
        self.contracts = {}

        self.init_engine()

# init functions 
    def init_engine(self):
        self.init_nng()
        self.load_contract()
        self.load_setting()
        self.register_event()
        self.init_subcribe()
        self.put_event()
        self.start()

    def init_nng(self):
        self._recv_sock.set_string_option(SUB, SUB_SUBSCRIBE, '')  # receive msg start with all
        self._recv_sock.set_int_option(SOL_SOCKET,RCVTIMEO,100)
        self._recv_sock.connect(self._config['serverpub_url'])
        self._send_sock.connect(self._config['serverpull_url'])

    def load_contract(self):
        contractfile = Path.cwd().joinpath("etc/ctpcontract.yaml")
        with open(contractfile, encoding='utf8') as fc: 
            contracts = yaml.load(fc)
        print(len(contracts))
        for sym, data in contracts.items():
            contract = ContractData(
                symbol=data["symbol"],
                exchange=Exchange(data["exchange"]),
                name=data["name"],
                product=PRODUCT_CTP2VT[str(data["product"])],
                size=data["size"],
                pricetick=data["pricetick"],
                net_position = True if str(data["positiontype"]) == THOST_FTDC_PT_Net else False,
                long_margin_ratio = data["long_margin_ratio"],
                short_margin_ratio = data["short_margin_ratio"],
                full_symbol = data["full_symbol"]
            )            
            # For option only
            if contract.product == Product.OPTION:
                contract.option_underlying = data["option_underlying"],
                contract.option_type = OPTIONTYPE_CTP2VT.get(str(data["option_type"]), None),
                contract.option_strike = data["option_strike"],
                contract.option_expiry = datetime.strptime(str(data["option_expiry"]), "%Y%m%d"),
            self.contracts[contract.full_symbol] = contract

    def load_setting(self):
        """"""
        setting = load_json(self.setting_filename)
        self.tick_recordings = setting.get("tick", {})
        self.bar_recordings = setting.get("bar", {})

    def save_setting(self):
        """"""
        setting = {
            "tick": self.tick_recordings,
            "bar": self.bar_recordings
        }
        save_json(self.setting_filename, setting)

    def register_event(self):
        """"""
        self.event_engine.register(EventType.TICK, self.process_tick_event)
        self.event_engine.register(EventType.CONTRACT, self.process_contract_event)        
        self.event_engine.register(EventType.RECORDER_CONTROL,self.process_recordercontrol_event)
        self.event_engine.register(EventType.HEADER,self.process_general_event)

    def init_subcribe(self):
        for sym in self.tick_recordings.keys():
            self.subscribe(sym)
            sleep(1)
        for sym in self.bar_recordings.keys():
            self.subscribe(sym)
            sleep(1)    

# event handler
    def process_general_event(self, event):
        pass


    def process_tick_event(self, event: Event):
        """"""
        tick = event.data

        if tick.full_symbol in self.tick_recordings:
            self.record_tick(tick)

        if tick.full_symbol in self.bar_recordings:
            bg = self.get_bar_generator(tick.full_symbol)
            bg.update_tick(tick)


    def process_contract_event(self, event: Event):
        """"""
        contract = event.data
        self.contracts[contract.full_symbol] = contract

    def process_recordercontrol_event(self,event:Event):
        msgtype = event.msg_type
        deslist = ['@*',str(self.id),'@'+str(self.id)]
        if (event.destination not in deslist ) :
            return
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_STATUS):
            m = Event(type=EventType.RECORDER_CONTROL,
                des='@0',
                src=str(self.id),
                data=str(self.__active),
                msgtype=MSG_TYPE.MSG_TYPE_RECORDER_STATUS
                )
            self._send_sock.send(m.serialize())
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_ADD_TICK):
            full_symbol = event.data
            self.add_tick_recording(full_symbol)
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_ADD_BAR):
            full_symbol = event.data
            self.add_bar_recording(full_symbol)
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_REMOVE_TICK):
            full_symbol = event.data
            self.remove_tick_recording(full_symbol)
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_REMOVE_BAR):
            full_symbol = event.data
            self.remove_bar_recording(full_symbol)
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_START):
            if not self.__active:
                self.start()
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_STOP):
            if self.__active:
                self.stop()
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_RELOAD):
            pass
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_RESET):
            pass
        elif (msgtype == MSG_TYPE.MSG_TYPE_RECORDER_GET_DATA):
            self.put_event()

        
    def put_event(self):
        """"""
        tick_symbols = list(self.tick_recordings.keys())
        tick_symbols.sort()

        bar_symbols = list(self.bar_recordings.keys())
        bar_symbols.sort()
        data = {
            "tick": tick_symbols,
            "bar": bar_symbols
        }
        msg = json.dumps(data)
        m = Event(type=EventType.RECORDER_CONTROL,data=msg,des='@0',src=str(self.id),msgtype=MSG_TYPE.MSG_TYPE_RECORDER_RTN_DATA)
        self._send_sock.send(m.serialize())

    def add_bar_recording(self, full_symbol: str):
        """"""
        if full_symbol in self.bar_recordings:
            self.write_log(f"已在K线记录列表中：{full_symbol}")
            return

        contract = self.contracts.get(full_symbol,None)
        if not contract:
            self.write_log(f"找不到合约：{full_symbol}")
            return

        self.bar_recordings[full_symbol] = {
            "symbol": contract.symbol,
            "exchange": contract.exchange.value,
            "gateway_name": self.gateway
        }

        self.subscribe(full_symbol)
        self.save_setting()
        self.put_event()

        self.write_log(f"添加K线记录成功：{full_symbol}")

    def add_tick_recording(self, full_symbol: str):
        """"""
        if full_symbol in self.tick_recordings:
            self.write_log(f"已在Tick记录列表中：{full_symbol}")
            return

        contract = self.contracts.get(full_symbol,None)
        if not contract:
            self.write_log(f"找不到合约：{full_symbol}")
            return

        self.tick_recordings[full_symbol] = {
            "symbol": contract.symbol,
            "exchange": contract.exchange.value,
            "gateway_name": self.gateway
        }

        self.subscribe(full_symbol)
        self.save_setting()
        self.put_event()

        self.write_log(f"添加Tick记录成功：{full_symbol}")

    def remove_bar_recording(self, full_symbol: str):
        """"""
        if full_symbol not in self.bar_recordings:
            self.write_log(f"不在K线记录列表中：{full_symbol}")
            return

        self.bar_recordings.pop(full_symbol)
        self.save_setting()
        self.put_event()

        self.write_log(f"移除K线记录成功：{full_symbol}")

    def remove_tick_recording(self, full_symbol: str):
        """"""
        if full_symbol not in self.tick_recordings:
            self.write_log(f"不在Tick记录列表中：{full_symbol}")
            return

        self.tick_recordings.pop(full_symbol)
        self.save_setting()
        self.put_event()

        self.write_log(f"移除Tick记录成功：{full_symbol}")

    def record_tick(self, tick: TickData):
        """"""
        database_manager.save_tick_data([tick])

    def record_bar(self, bar: BarData):
        """"""
        database_manager.save_bar_data([bar])

    def get_bar_generator(self, full_symbol: str):
        """"""
        bg = self.bar_generators.get(full_symbol, None)

        if not bg:
            bg = BarGenerator(self.record_bar)
            self.bar_generators[full_symbol] = bg

        return bg

    def subscribe(self, full_symbol: str):
        contract = self.contracts.get(full_symbol,None)
        if contract:
            m = Event(type=EventType.SUBSCRIBE,msgtype=MSG_TYPE.MSG_TYPE_SUBSCRIBE_MARKET_DATA)
            m.destination = self.gateway
            m.source = str(self.id)
            req = SubscribeRequest()
            if self.gateway == 'CTP.MD':
                req.sym_type = SYMBOL_TYPE.CTP                
                req.content = contract.symbol
            else:
                req.sym_type = SYMBOL_TYPE.FULL                
                req.content = full_symbol                
            m.data = req
            self._send_sock.send(m.serialize())
        else:
            self.write_log(f"行情订阅失败，找不到合约{full_symbol}")
















  
# start and stop    
    def start(self, timer=True):
        """
        start the dispatcher thread and begin to recv msg through nng
        """
        self.event_engine.start()
        print('tradeclient started ,pid = %d ' % os.getpid())
        self.__active = True
        while self.__active:
            try:
                msgin = self._recv_sock.recv(flags=0)
                msgin = msgin.decode("utf-8")
                if msgin is not None and msgin.index('|') > 0:
                    if msgin[0] == '@':
                        print('recorder(pid = %d) rec @ msg:'%(self.id), msgin,'at ', datetime.now())
                    if msgin[-1] == '\0':
                        msgin = msgin[:-1]
                    if msgin[-1] == '\x00':
                        msgin = msgin[:-1]
                    m = Event()
                    m.deserialize(msgin)
                    self.event_engine.put(m)
            except Exception as e:
                pass
                #print("TradeEngineError {0}".format(str(e.args[0])).encode("utf-8"))
 
    def stop(self):
        """
        stop 
        """
        self.__active = False
        self.event_engine.stop()

    def put(self, event):
        """
        send event msg,TODO:check the event
        """
        # 
        self._send_sock.send(event.serialize(),flags=1)


    def write_log(self, msg: str):
        """
        Create engine log event.
        """

        # log = LogData(msg=msg, gateway_name="CtaStrategy")
        # event = Event(type=EVENT_CTA_LOG, data=log)
        # self.event_engine.put(event)  
        print(msg)      

    # -------------------------------- end of public functions -----------------------------#