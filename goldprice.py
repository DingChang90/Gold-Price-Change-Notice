# -*- coding: utf-8 -*-
"""
cron: */10 * * * *
new Env('金价查询-多条件全能生产版');
"""

import requests
import logging
import sys
import os
import json
from datetime import datetime
import time

# 日志初始化（增加时间戳）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ===================== 【参数配置】 =====================
# 核心抄底预期金额：低于或等于这个价格（元/克）时触发推送
EXPECTED_PRICE = 850.00
# 盘中剧烈波动触发阈值：比早上9:20价格「高出」或「低于」这个金额时再次通知
MOVE_THRESHOLD = 20.00
# 状态缓存文件路径
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_status_v3.json")
# 企业微信生产应用消息配置
CORP_ID = ""
CORP_SECRET = ""
AGENT_ID = ""
TO_USER = "@all"
# 接口请求重试次数
REQ_RETRY = 2
# =======================================================

# 全局缓存企业微信Token (2小时有效期)
WX_ACCESS_TOKEN = ""
WX_TOKEN_EXPIRE = 0

def get_wx_token():
    """获取企业微信Token，增加缓存"""
    global WX_ACCESS_TOKEN, WX_TOKEN_EXPIRE
    now_ts = int(time.time())
    # Token 未过期直接返回
    if WX_ACCESS_TOKEN and now_ts < WX_TOKEN_EXPIRE:
        return WX_ACCESS_TOKEN

    url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={CORP_ID}&corpsecret={CORP_SECRET}"
    try:
        res = requests.get(url, timeout=10).json()
        if res.get("errcode") == 0:
            WX_ACCESS_TOKEN = res.get("access_token")
            WX_TOKEN_EXPIRE = now_ts + 7200  # 2小时有效期
            return WX_ACCESS_TOKEN
    except Exception as e:
        logger.info(f"❌ 获取微信 Token 异常: {str(e)}")
    return None

def send_wx_msg(title, content):
    token = get_wx_token()
    if not token:
        return
    url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    payload = {
        "touser": TO_USER,
        "msgtype": "textcard",
        "agentid": AGENT_ID,
        "textcard": {
            "title": title,
            "description": content,
            "url": "https://www.huilvbiao.com/gold",
            "btntxt": "查看详情"
        },
        "safe": 0
    }
    try:
        res = requests.post(url, json=payload, timeout=10).json()
        if res.get("errcode") == 0:
            logger.info("✅ 企业微信应用卡片消息推送成功！")
        else:
            logger.info(f"❌ 企业微信网关返回错误: {res.get('errmsg')}")
    except Exception as e:
        logger.info(f"❌ 推送异常: {str(e)}")

def get_realtime_gold_price():
    """获取实时金价，增加重试、异常拦截"""
    cmb_url = "https://fx.cmbchina.com/api/v1/fx/rate"
    sina_url = "http://hq.sinajs.cn/list=fx_sxauusd"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.sina.com.cn/"
    }
    usd_rate, gold_usd_per_oz = None, None

    # 重试机制
    for _ in range(REQ_RETRY):
        try:
            cmb_res = requests.get(cmb_url, headers=headers, timeout=10).json()
            for item in cmb_res.get("body", []):
                if item.get("ccyNbr") == "美元":
                    usd_rate = float(item.get("rthOfr")) / 100.0
                    break
            sina_res = requests.get(sina_url, headers=headers, timeout=10)
            if sina_res.status_code == 200 and '"' in sina_res.text:
                raw_data = sina_res.text.split('"')[1]
                fields = raw_data.split(',')
                if len(fields) > 1:
                    gold_usd_per_oz = float(fields[1])

            if usd_rate and gold_usd_per_oz and usd_rate > 0 and gold_usd_per_oz > 0:
                return round((gold_usd_per_oz * usd_rate) / 31.1034768, 2)
        except Exception as e:
            logger.info(f"⚠️ 金价接口请求异常: {str(e)}，正在重试...")
            time.sleep(1)
    logger.info("⚠️ 金价计算失败，多次重试无效")
    return None

def check_push_conditions(current_price):
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    current_hour = now.hour
    current_min = now.minute

    # 状态机：基准价格改为 9:20 价格
    status = {
        "date": today,
        "price_at_920": None,
        "pushed_920": False,
        "pushed_2130": False,
        "pushed_expected_price": False,
        "pushed_rise_fluct": False,    # 暴涨独立锁
        "pushed_fall_fluct": False     # 暴跌独立锁
    }

    # 读取状态文件，精准捕获异常
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, "r", encoding="utf-8") as f:
                saved_status = json.load(f)
            if saved_status.get("date") == today:
                status = saved_status
            else:
                logger.info("📅 检测到新的一天，自动重置金价监控状态机。")
        except (json.JSONDecodeError, OSError, KeyError) as e:
            logger.info(f"⚠️ 状态文件异常，重置当日状态: {str(e)}")

    def save_status():
        try:
            with open(STATUS_FILE, "w", encoding="utf-8") as f:
                json.dump(status, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.info(f"⚠️ 缓存写入失败: {str(e)}")

    # ----- 条件1：9:20 早盘通知（区间 9:20 ~ 9:29）-----
    if current_hour == 9 and 20 <= current_min <= 29 and not status["pushed_920"]:
        status["price_at_920"] = current_price
        status["pushed_920"] = True
        save_status()
        title = "☀️ 早盘金价例行通知"
        content = (
            f"今日上午 09:20 盘面已同步。\n"
            f"当前价格：{current_price} 元/克\n"
            f"提示：今日盘中若上下波动超过 {MOVE_THRESHOLD} 元将自动发出二次变盘预警。"
        )
        send_wx_msg(title, content)
        return

    # ----- 条件2：21:30 盘后收盘总结（区间 21:30 ~ 21:39）-----
    if current_hour == 21 and 30 <= current_min <= 39 and not status["pushed_2130"]:
        status["pushed_2130"] = True
        save_status()
        title = "🌙 晚间金价收盘总结"
        price_920 = status.get("price_at_920")

        if price_920 is not None:
            diff = round(current_price - price_920, 2)
            direction = "📈 上涨" if diff > 0 else "📉 下跌" if diff < 0 else "↔️ 平盘"
            diff_str = f"+{diff}" if diff > 0 else f"{diff}"
            content = (
                f"今日晚间 21:30 盘后总结已生成。\n"
                f"早上 09:20 价格：{price_920} 元/克\n"
                f"晚间 21:30 价格：{current_price} 元/克\n"
                f"较早盘走势：{direction} 了 {diff_str.replace('-', '')} 元/克"
            )
        else:
            content = (
                f"今日晚间 21:30 盘后总结已生成。\n"
                f"当前价格：{current_price} 元/克\n"
                f"注：今日 09:20 脚本未运行，缺失早盘对比基准。"
            )
        send_wx_msg(title, content)
        return

    # ----- 条件3：盘中波动预警（对比基准 = 09:20 价格）-----
    if status["price_at_920"] is not None:
        price_diff = current_price - status["price_at_920"]
        # 暴涨预警
        if price_diff >= MOVE_THRESHOLD and not status["pushed_rise_fluct"]:
            status["pushed_rise_fluct"] = True
            save_status()
            title = "⚡ 🚀 盘中金价突发暴涨预警！"
            content = (
                f"注意！盘中金价出现异动拉升！\n"
                f"当前价格：{current_price} 元/克\n"
                f"09:20基准：{status['price_at_920']} 元/克\n"
                f"日内涨幅：已猛涨 +{round(price_diff, 2)} 元/克！"
            )
            send_wx_msg(title, content)
        # 暴跌预警
        elif price_diff <= -MOVE_THRESHOLD and not status["pushed_fall_fluct"]:
            status["pushed_fall_fluct"] = True
            save_status()
            title = "⚡ 💥 盘中金价突发暴跌预警！"
            content = (
                f"注意！盘中金价出现剧烈跳水！\n"
                f"当前价格：{current_price} 元/克\n"
                f"09:20基准：{status['price_at_920']} 元/克\n"
                f"日内跌幅：已暴跌 {round(price_diff, 2)} 元/克！"
            )
            send_wx_msg(title, content)

    # ----- 条件4：抄底提醒（独立触发）-----
    if current_price <= EXPECTED_PRICE and not status["pushed_expected_price"]:
        status["pushed_expected_price"] = True
        save_status()
        title = "🪙 黄金绝对抄底时机提示"
        content = (
            f"实时金价已跌至预设终极抄底线以下！\n"
            f"当前价格：{current_price} 元/克\n"
            f"终极目标：≤ {EXPECTED_PRICE} 元/克"
        )
        send_wx_msg(title, content)

    logger.info("🧘 未触发新拦截线，或今日对应卡片已发送过，保持静默。")

def main():
    logger.info("===== 开始执行国内实时金价多条件监控任务 =====")
    price = get_realtime_gold_price()
    if price is None:
        logger.info("❌ 数据解析链路异常，任务结束")
        sys.exit()
    logger.info(f"当前实时金价: {price} 元/克")
    check_push_conditions(price)
    logger.info("===== 任务执行完毕 =====")

if __name__ == '__main__':
    main()
