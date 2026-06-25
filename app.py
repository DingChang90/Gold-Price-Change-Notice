import os
import re
from datetime import datetime
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from apscheduler.schedulers.background import BackgroundScheduler
import requests

app = Flask(__name__)
CORS(app)  # 允许前端 H5 跨域请求

# --- 后端防刷：限流器配置 ---
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["150 per day", "20 per minute"],
    storage_uri="memory://"
)

# --- 数据库配置（SQLite单文件） ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{os.path.join(BASE_DIR, "gold_subscriber.db")}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# --- 数据库模型 ---
class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    push_type = db.Column(db.String(20), nullable=False)  # 'server_chan', 'dingtalk', 'feishu'
    token = db.Column(db.String(255), nullable=False)      
    trigger_mode = db.Column(db.Integer, nullable=False)  # 1:固定时间, 2:固定价格, 3:价格变化
    cron_weeks = db.Column(db.String(50))  
    cron_time = db.Column(db.String(10))   
    price_condition = db.Column(db.String(10))  
    price_value = db.Column(db.Float, default=0.0)
    last_pushed_date = db.Column(db.String(10))  

class SystemConfig(db.Model):
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(50))


# --- 🧠 优化后的 Server酱 Token 全系列通用校验器 ---
def check_server_chan_token_valid(token):
    token = token.strip()
    # 完美兼容旧版 SCU、新版 sctp 以及您使用的 SCT 核心格式 (不限大小写，支持字母数字组合)
    pattern = r'^(SCU|sctp|SCT)\d+[A-Za-z0-9]+$'
    if re.match(pattern, token, re.IGNORECASE):
        return True
    return False


# --- 金价获取函数 ---
def get_current_gold_price():
    REQ_RETRY = 2
    cmb_url = "https://fx.cmbchina.com/api/v1/fx/rate"
    sina_url = "http://hq.sinajs.cn/list=fx_sxauusd"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn/"
    }
    usd_rate, gold_usd = None, None
    for _ in range(REQ_RETRY):
        try:
            cmb_res = requests.get(cmb_url, headers=headers, timeout=10).json()
            for item in cmb_res.get("body", []):
                if item.get("ccyNbr") == "美元":
                    usd_rate = float(item.get("rthOfr")) / 100
                    break
            sina_res = requests.get(sina_url, headers=headers, timeout=10)
            raw = sina_res.text.split('"')[1]
            fields = raw.split(',')
            gold_usd = float(fields[1])
            if usd_rate and gold_usd > 0:
                return round((gold_usd * usd_rate) / 31.1034768, 2)
        except Exception as e:
            print(f"[-] 金价接口请求重试中... 原因: {e}")
            time.sleep(1)
    return None

# --- 扩展：钉钉与飞书机器人 Token 通用提取清洗 ---
def clean_webhook_token(input_str, keyword="hook/"):
    input_str = input_str.strip()
    # 如果用户录入了完整 URL 链接，精准抠出最后的 token/uuid 部分
    if "access_token=" in input_str:
        match = re.search(r'access_token=([^&]+)', input_str)
        if match: return match.group(1).strip()
    if keyword in input_str:
        return input_str.split(keyword)[-1].strip()
    return input_str

# --- 消息推送核心函数（追加飞书支持） ---
def send_notification(push_type, token, content):
    try:
        if push_type == 'server_chan':
            url = f"https://sctapi.ftqq.com/{token}.send"
            requests.post(url, data={'title': '🔔 金价资产多重策略提醒', 'desp': content}, timeout=5)
        elif push_type == 'dingtalk':
            url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
            headers = {"Content-Type": "application/json"}
            data = {"msgtype": "text", "text": {"content": f"【金价策略提醒】\n{content}"}}
            requests.post(url, json=data, headers=headers, timeout=5)
        elif push_type == 'feishu':
            # 飞书自定义机器人标准的 Webhook 调用规范
            url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
            headers = {"Content-Type": "application/json"}
            data = {"msg_type": "text", "content": {"text": f"🔔【金价策略提醒】\n{content}"}}
            requests.post(url, json=data, headers=headers, timeout=5)
        print(f"[+] 推送成功 [{push_type}]: {content}")
    except Exception as e:
        print(f"[-] 推送失败: {e}")


# --- 核心自动化轮询逻辑 ---
def check_subscriptions():
    with app.app_context():
        current_price = get_current_gold_price()
        if current_price is None:
            return

        now = datetime.now()
        today_str = now.strftime('%Y-%m-%d')
        current_time_str = now.strftime('%H:%M')
        current_weekday = str(now.isoweekday())
        
        base_price_record = SystemConfig.query.filter_by(key='base_price_920').first()
        base_price = float(base_price_record.value) if base_price_record else None

        subs = Subscription.query.all()
        for sub in subs:
            triggered = False
            msg = ""

            if sub.trigger_mode == 1:
                weeks = sub.cron_weeks.split(',')
                if current_weekday in weeks and current_time_str == sub.cron_time:
                    triggered = True
                    msg = f"自选定时推送。当前国内黄金结算换算价为：{current_price} 元/克。"
            
            elif sub.trigger_mode == 2:
                if sub.last_pushed_date == today_str:
                    continue
                if sub.price_condition == 'above' and current_price >= sub.price_value:
                    triggered = True
                    msg = f"实时金价警报：当前价格 {current_price} 元/克，已【高于/等于】您设定的阈值 {sub.price_value} 元/克。"
                elif sub.price_condition == 'below' and current_price <= sub.price_value:
                    triggered = True
                    msg = f"实时金价警报：当前价格 {current_price} 元/克，已【低于/等于】您设定的阈值 {sub.price_value} 元/克。"

            elif sub.trigger_mode == 3 and base_price is not None:
                if sub.last_pushed_date == today_str:
                    continue
                diff = current_price - base_price
                if abs(diff) >= sub.price_value:
                    triggered = True
                    direction = "上涨" if diff > 0 else "下跌"
                    msg = f"盘面剧烈波动警报：当前实时换算价 {current_price} 元/克，对比今日09:20基准价({base_price} 元/克)，盘面已大方向【{direction}】了 {round(abs(diff), 2)} 元，超过最大限制阈值 {sub.price_value} 元。"

            if triggered:
                send_notification(sub.push_type, sub.token, msg)
                if sub.trigger_mode in [2, 3]:
                    sub.last_pushed_date = today_str
                    db.session.commit()

def record_920_price():
    with app.app_context():
        current_price = get_current_gold_price()
        if current_price:
            record = SystemConfig.query.filter_by(key='base_price_920').first()
            if not record:
                record = SystemConfig(key='base_price_920', value=str(current_price))
                db.session.add(record)
            else:
                record.value = str(current_price)
            db.session.commit()


# --- 🌐 API 路由接口 ---

@app.route('/api/query_rules', methods=['POST'])
@limiter.limit("20 per minute")
def query_rules():
    data = request.json or {}
    push_type = data.get('push_type')
    raw_token = data.get('token', '')

    if push_type == 'dingtalk':
        token = clean_webhook_token(raw_token, "access_token=")
    elif push_type == 'feishu':
        token = clean_webhook_token(raw_token, "hook/")
    else:
        token = raw_token.strip()

    if not token:
        return jsonify({"code": 400, "msg": "凭证参数不能为空"}), 400

    if push_type == 'server_chan' and not check_server_chan_token_valid(token):
        return jsonify({"code": 400, "msg": "查询拒绝：您输入的 Server 酱密钥格式不合法。"}), 400

    subs = Subscription.query.filter_by(push_type=push_type, token=token).all()
    rules_list = []
    for sub in subs:
        weeks = [int(w) for w in sub.cron_weeks.split(',')] if sub.cron_weeks else []
        rules_list.append({
            "trigger_mode": sub.trigger_mode,
            "cron_weeks": weeks,
            "cron_time": sub.cron_time,
            "price_condition": sub.price_condition,
            "price_value": sub.price_value
        })

    return jsonify({"code": 200, "rules": rules_list})


@app.route('/api/subscribe_batch', methods=['POST'])
@limiter.limit("10 per minute")
def subscribe_batch():
    data = request.json or {}
    push_type = data.get('push_type')
    raw_token = data.get('token')
    rules = data.get('rules', [])
    
    if not raw_token:
        return jsonify({"code": 400, "msg": "凭证不能为空"}), 400

    if push_type == 'dingtalk':
        token = clean_webhook_token(raw_token, "access_token=")
    elif push_type == 'feishu':
        token = clean_webhook_token(raw_token, "hook/")
    else:
        token = raw_token.strip()

    if push_type == 'server_chan' and not check_server_chan_token_valid(token):
        return jsonify({"code": 400, "msg": "保存拒绝：您输入的 Server 酱密钥格式不符合规范。"}), 400

    try:
        Subscription.query.filter_by(push_type=push_type, token=token).delete()
        
        for r in rules:
            trigger_mode = int(r.get('trigger_mode'))
            weeks_str = ",".join(map(str, r.get('cron_weeks', []))) if r.get('cron_weeks') else ""
            
            new_sub = Subscription(
                push_type=push_type,
                token=token,
                trigger_mode=trigger_mode,
                cron_weeks=weeks_str,
                cron_time=r.get('cron_time'),
                price_condition=r.get('price_condition') if trigger_mode == 2 else None,
                price_value=float(r.get('price_value', 0) or 0.0)
            )
            db.session.add(new_sub)
            
        db.session.commit()
        return jsonify({"code": 200, "msg": "数据已全量覆写，排重保存成功！"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"code": 500, "msg": f"保存失败: {str(e)}"}), 500


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_subscriptions, 'cron', second='0')
    scheduler.add_job(record_920_price, 'cron', hour=9, minute=20)
    scheduler.start()
    
    print("[*] 自动化定时监控引擎及 09:20 基准线捕获器已全部全线启动...")
    app.run(host='0.0.0.0', port=5001)
