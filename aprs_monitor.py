#!/usr/bin/env python3
"""
APRS 设备电池电压监控：BD6IMR-3
5 节串联铅酸电池，低于阈值时推送 Telegram 警告
"""

import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

import requests

# ── 配置 ──────────────────────────────────────────────────────────────────────
APRS_CALLSIGN    = "BD6IMR-3"
APRS_API_URL     = "https://api.aprs.fi/api/get"
APRS_API_KEY     = os.environ.get("APRS_API_KEY", "")
VOLTAGE_WARN     = 60.0   # V，低于此值推送警告（12V/节×5）
VOLTAGE_CRITICAL = 57.5   # V，低于此值推送严重警告（11.5V/节×5）
STATE_FILE       = Path("aprs_state.json")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ── 状态持久化 ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_voltage": None, "last_alert_level": "ok", "last_packet_time": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] 未配置 Telegram 凭据，跳过推送。")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
        print("[OK] Telegram 消息发送成功")
        return True
    except Exception as e:
        print(f"[ERROR] Telegram 发送失败: {e}")
        return False

# ── APRS 数据获取 ─────────────────────────────────────────────────────────────

def fetch_aprs_data() -> dict | None:
    if not APRS_API_KEY:
        print("[APRS] 未配置 APRS_API_KEY，跳过。")
        return None
    params = {
        "name": APRS_CALLSIGN,
        "what": "loc",
        "apikey": APRS_API_KEY,
        "format": "json",
    }
    try:
        r = requests.get(APRS_API_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"[APRS] API 请求失败: {e}")
        return None

    if data.get("result") != "ok" or not data.get("entries"):
        print(f"[APRS] API 返回异常: result={data.get('result')} found={data.get('found', 0)}")
        return None

    return data["entries"][0]


def parse_voltage(comment: str) -> float | None:
    m = re.search(r'pow[:\s]*([\d.]+)\s*[Vv]', comment, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def voltage_bar(voltage: float) -> str:
    full = 63.0
    pct = max(0.0, min(1.0, (voltage - VOLTAGE_CRITICAL) / (full - VOLTAGE_CRITICAL)))
    filled = round(pct * 10)
    return f"[{'█' * filled}{'░' * (10 - filled)}] {pct*100:.0f}%"

# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def main():
    print(f"[APRS] 开始检查 {APRS_CALLSIGN} 电压")

    entry = fetch_aprs_data()
    if entry is None:
        return

    comment     = entry.get("comment", "")
    packet_time = entry.get("lasttime", "")
    print(f"[APRS] comment: {comment!r}  packet_time: {packet_time}")

    voltage = parse_voltage(comment)
    if voltage is None:
        print(f"[APRS] 未找到 pow:xxV，原文: {comment!r}")
        send_telegram(
            f"⚠️ <b>APRS 电压解析失败</b>\n\n"
            f"设备: <code>{APRS_CALLSIGN}</code>\n"
            f"原始 comment: <code>{comment}</code>\n\n"
            f"请检查设备信标格式是否变化。"
        )
        return

    print(f"[APRS] 当前电压: {voltage}V")

    if voltage < VOLTAGE_CRITICAL:
        alert_level = "critical"
    elif voltage < VOLTAGE_WARN:
        alert_level = "warn"
    else:
        alert_level = "ok"

    state      = load_state()
    last_level = state.get("last_alert_level", "ok")
    last_pkt   = state.get("last_packet_time")

    # 同一个数据包且仍在告警中 → 跳过，避免重复轰炸
    if packet_time and packet_time == last_pkt and alert_level != "ok":
        print("[APRS] 数据包未更新，跳过重复推送。")
        return

    # 持续正常 → 静默
    if alert_level == "ok" and last_level == "ok":
        print(f"[APRS] 电压正常 ({voltage}V ≥ {VOLTAGE_WARN}V)，无需推送。")
        state.update({"last_voltage": voltage, "last_alert_level": "ok",
                      "last_packet_time": packet_time})
        save_state(state)
        return

    # 格式化时间
    try:
        dt = datetime.fromtimestamp(int(packet_time), tz=timezone.utc)
        dt_str = dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        dt_str = str(packet_time)

    bar = voltage_bar(voltage)

    if alert_level == "critical":
        msg = (
            f"🔴 <b>电池严重低电！请立即充电！</b>\n\n"
            f"设备: <code>{APRS_CALLSIGN}</code>\n"
            f"电压: <b>{voltage}V</b>（危险阈值 {VOLTAGE_CRITICAL}V）\n"
            f"电量: {bar}\n"
            f"时间: {dt_str}\n\n"
            f"⚡ 5节铅酸已严重亏电，请立即骑行前往充电！"
        )
    elif alert_level == "warn":
        msg = (
            f"🟡 <b>电池电压偏低，请尽快充电</b>\n\n"
            f"设备: <code>{APRS_CALLSIGN}</code>\n"
            f"电压: <b>{voltage}V</b>（警告阈值 {VOLTAGE_WARN}V）\n"
            f"电量: {bar}\n"
            f"时间: {dt_str}\n\n"
            f"🔋 建议尽快充电，避免深度放电损坏电池。"
        )
    else:  # 告警恢复
        msg = (
            f"🟢 <b>电池电压已恢复正常</b>\n\n"
            f"设备: <code>{APRS_CALLSIGN}</code>\n"
            f"电压: <b>{voltage}V</b>（安全 ≥ {VOLTAGE_WARN}V）\n"
            f"电量: {bar}\n"
            f"时间: {dt_str}"
        )

    send_telegram(msg)
    state.update({"last_voltage": voltage, "last_alert_level": alert_level,
                  "last_packet_time": packet_time})
    save_state(state)
    print(f"[APRS] 推送完成: {alert_level} / {voltage}V")


if __name__ == "__main__":
    main()
