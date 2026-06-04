# -*- coding: utf-8 -*-
"""
LOF基金溢价率监控 + 企业微信推送 + Lark 推送
用于 GitHub Actions 定时运行
"""

import sys
import requests
import re
import time
import os
import csv
import json
from datetime import datetime

# Windows 终端默认 GBK 编码无法输出 emoji，统一切换到 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def load_dotenv(path=".env"):
    """从本地 .env 文件加载环境变量（不覆盖已有环境变量）"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# ─── 基金数据抓取 ─────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
    "Referer": "https://fund.eastmoney.com/",
}

def fetch_premium():
    """抓取溢价率"""
    url = "https://palmmicro.com/woody/res/lofcn.php?sort=premium"
    print("获取溢价率（主列表页）...")
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.encoding = "utf-8"
        html = r.text

        m = re.search(r'id="estimationtable".*?<tbody>(.*?)</tbody>', html, re.S)
        if not m:
            print("  未找到 estimationtable")
            return {}, []

        tbody = m.group(1)
        result = {}
        fund_list = []

        for row_m in re.finditer(r'<tr>(.*?)</tr>', tbody, re.S):
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row_m.group(1), re.S)
            if len(cells) < 6:
                continue

            code_m = re.search(r'>(S[HZ]\d{6})<', cells[0])
            if not code_m:
                continue
            full_code = code_m.group(1)
            code6 = full_code[2:]

            name_m = re.search(r'<td[^>]*title="([^"]+)"', row_m.group(1))
            name = name_m.group(1) if name_m else '未知'

            est_m = re.search(r'>([\d.]+)<', cells[1])
            est = float(est_m.group(1)) if est_m else None

            date_m = re.search(r'(\d{4}-\d{2}-\d{2})', cells[2])
            est_date = date_m.group(1) if date_m else None

            prem_m = re.search(r'>([-\d.]+)', cells[3])
            premium = float(prem_m.group(1)) if prem_m else None

            ref_premium = None
            if cells[5].strip():
                ref_m = re.search(r'>([-\d.]+)', cells[5])
                ref_premium = float(ref_m.group(1)) if ref_m else None

            result[full_code] = {
                "est": est,
                "est_date": est_date,
                "premium": premium,
                "ref_premium": ref_premium,
                "name": name,
            }
            fund_list.append((full_code, code6, name))

        print(f"  完成：{len(result)} 只")
        return result, fund_list
    except Exception as e:
        print(f"  溢价获取失败: {e}")
        return {}, []


def fetch_prices(fund_list):
    print("获取实时行情...")
    codes = ",".join(
        ("sh" if f[0].startswith("SH") else "sz") + f[1] for f in fund_list
    )
    try:
        r = requests.get(
            f"https://hq.sinajs.cn/list={codes}",
            headers={**HEADERS, "Referer": "https://finance.sina.com.cn"},
            timeout=15
        )
        r.encoding = "gbk"
        result = {}
        for line in r.text.splitlines():
            m = re.match(r'var hq_str_(s[hz])(\d{6})="([^"]+)"', line)
            if not m:
                continue
            full_code = m.group(1).upper() + m.group(2)
            parts = m.group(3).split(",")
            if len(parts) < 4:
                continue
            try:
                price = float(parts[3])
                prev = float(parts[2]) if parts[2] else 0
                change = round((price - prev) / prev * 100, 2) if prev else 0
                result[full_code] = {"price": price, "change": change}
            except:
                pass
        print(f"  完成：{len(result)} 只")
        return result
    except Exception as e:
        print(f"  行情获取失败: {e}")
        return {}


# ─── 限购抓取（完整）────────────────────────────────────────────────────────────

def parse_money_str(s):
    s = s.replace(",", "").strip()
    m = re.match(r'([\d.]+)\s*万元?', s)
    if m: return float(m.group(1)) * 10000
    m = re.match(r'([\d.]+)\s*亿元?', s)
    if m: return float(m.group(1)) * 1e8
    m = re.match(r'([\d.]+)\s*元?', s)
    if m: return float(m.group(1))
    return None

def fetch_quota_batch(codes6_batch):
    fcodes = ",".join(codes6_batch)
    url = (
        f"https://fundmobapi.eastmoney.com/FundMNewApi/FundMNFInfo"
        f"?pageIndex=1&pageSize={len(codes6_batch)}&plat=Android"
        f"&appType=ttjj&product=EFund&Version=1&Fcodes={fcodes}"
    )
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
        if not data.get("Datas"):
            return {}
        result = {}
        for item in data["Datas"]:
            code = item.get("FCODE", "")
            sgzt = str(item.get("SGZT", "0"))
            sgsxe = float(item.get("SGSXE") or 0)
            sgba = float(item.get("SGBA") or 0)
            if sgzt == "1":
                status, status_text = "closed", "暂停申购"
            elif sgzt == "3":
                status, status_text = "closed", "封闭期"
            elif sgzt == "2":
                status, status_text = "limited", "限制大额"
            elif sgsxe > 0:
                status, status_text = "limited", "限额申购"
            else:
                status, status_text = "open", "正常申购"
            result[code] = {
                "status": status, "status_text": status_text,
                "quota": sgsxe if sgsxe > 0 else None,
                "big_quota": sgba if sgba > 0 else None,
            }
        return result
    except:
        return {}

def fetch_quota_page(code6):
    try:
        r = requests.get(f"https://fund.eastmoney.com/{code6}.html", headers=HEADERS, timeout=10)
        r.encoding = "utf-8"
        html = r.text
        raw_cells = re.findall(r'class="staticCell"[^>]*>(.*?)</span>\s*(?=<span|<div|$)', html, re.S)
        cells = [re.sub(r'<[^>]+>', '', c) for c in raw_cells]
        cell_text = " ".join(c.strip() for c in cells)
        status, status_text, quota = "unknown", "未知", None
        if "暂停申购" in cell_text or "暂停大额" in cell_text:
            status, status_text = "closed", "暂停申购"
        elif "封闭期" in cell_text:
            status, status_text = "closed", "封闭期"
        elif "限大额" in cell_text or "限制大额" in cell_text:
            status, status_text = "limited", "限制大额"
        elif "开放申购" in cell_text or "正常申购" in cell_text:
            status, status_text = "open", "正常申购"
        for target in [cell_text, html]:
            for pat in [r'单日累计购买上限\s*([\d.,]+\s*[万亿]?元?)',
                        r'单笔限购[：:]\s*([\d.,]+\s*[万亿]?元?)',
                        r'每日累计限购[：:]\s*([\d.,]+\s*[万亿]?元?)']:
                m = re.search(pat, target)
                if m:
                    quota = parse_money_str(m.group(1))
                    break
            if quota:
                break
        if quota and status not in ("closed",):
            status = "limited"
            status_text = "限额申购"
        return {"status": status, "status_text": status_text, "quota": quota, "big_quota": None}
    except:
        return {"status": "error", "status_text": "查询失败", "quota": None, "big_quota": None}

def fetch_quota(fund_list):
    print("获取限购状态...")
    all_codes = [f[1] for f in fund_list]
    result = {}
    for i in range(0, len(all_codes), 20):
        result.update(fetch_quota_batch(all_codes[i:i+20]))
        time.sleep(0.5)
    failed = [f[1] for f in fund_list if f[1] not in result]
    for code6 in failed:
        result[code6] = fetch_quota_page(code6)
        time.sleep(0.3)
    print("  完成")
    return result


# ─── 合并数据 ────────────────────────────────────────────────────────────────

def merge(premium_map, price_map, quota_map, fund_list):
    rows = []
    for full_code, code6, name in fund_list:
        p = price_map.get(full_code, {})
        e = premium_map.get(full_code, {})
        q = quota_map.get(code6, {"status": "error", "status_text": "查询失败", "quota": None})

        price = p.get("price")
        change = p.get("change")
        est = e.get("est")
        premium = e.get("premium")
        if premium is None and price and est:
            premium = round((price - est) / est * 100, 2)

        rows.append({
            "full_code": full_code, "code6": code6, "name": name,
            "price": price, "change": change, "est": est, "premium": premium,
            "est_date": e.get("est_date"), "ref_premium": e.get("ref_premium"),
            "status": q["status"], "status_text": q["status_text"],
            "quota": q["quota"],
        })

    rows.sort(key=lambda x: (x["premium"] or -999), reverse=True)
    return rows


# ─── 推送：企业微信 + Lark ───────────────────────────────────────────────────

def send_wecom_bot(title, content, key):
    """企业微信群机器人推送"""
    if not key:
        print("⚠️ 未设置 WECHAT_WORK_KEY，跳过企业微信推送")
        return

    url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}"
    msg = {
        "msgtype": "text",
        "text": {
            "content": f"{title}\n\n{content}"
        }
    }
    try:
        r = requests.post(url, json=msg, timeout=10)
        if r.json().get("errcode") == 0:
            print("✅ 企业微信推送成功")
        else:
            print(f"⚠️ 企业微信推送失败: {r.json()}")
    except Exception as e:
        print(f"❌ 企业微信推送异常: {e}")


def send_lark(title, content, app_id, app_secret, chat_id):
    """Lark 国际版机器人推送"""
    if not (app_id and app_secret and chat_id):
        print("⚠️ Lark 推送信息不完整，跳过")
        return

    try:
        token_res = requests.post(
            "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10
        )
        access_token = token_res.json().get("tenant_access_token", "")
        if not access_token:
            print("⚠️ Lark token 获取失败")
            return

        msg = f"{title}\n\n{content}"
        res = requests.post(
            "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"receive_id": chat_id, "msg_type": "text", "content": json.dumps({"text": msg})},
            timeout=10
        )
        if res.json().get("code") == 0:
            print("✅ Lark 推送成功")
        else:
            print(f"⚠️ Lark 推送失败: {res.json()}")
    except Exception as e:
        print(f"❌ Lark 推送异常: {e}")


# ─── 历史记录 CSV ─────────────────────────────────────────────────────────────

def save_history_csv(rows, now_str, filepath="history.csv"):
    file_exists = os.path.exists(filepath)
    with open(filepath, "a", newline="", encoding="utf-8-sig") as f:
        fieldnames = ["时间"] + [r["full_code"] for r in rows]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        row = {"时间": now_str}
        for r in rows:
            row[r["full_code"]] = r["premium"] if r["premium"] is not None else ""
        writer.writerow(row)
    print(f"历史记录已追加到 {filepath}")


# ─── 主程序 ──────────────────────────────────────────────────────────────────

def main():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"=== LOF溢价监控 {now_str} ===")

    premium_map, fund_list = fetch_premium()
    if not fund_list:
        print("未获取到基金列表，退出")
        return

    time.sleep(0.5)
    price_map = fetch_prices(fund_list)

    time.sleep(0.5)
    quota_map = fetch_quota(fund_list)

    rows = merge(premium_map, price_map, quota_map, fund_list)

    save_history_csv(rows, now_str)

    # 构建推送内容（简化版）
    content = "\n".join([f"{r['name']} {r['full_code']} 溢价 {r['premium']}%" for r in rows[:10]])
    title = f"LOF溢价提醒 {now_str}"

    print(content)

    # 企业微信推送
    wecom_key = os.environ.get("WECHAT_WORK_KEY", "").strip()
    send_wecom_bot(title, content, wecom_key)

    # Lark 推送
    lark_app_id     = os.environ.get("FEISHU_APP_ID", "")
    lark_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    lark_chat_id    = os.environ.get("FEISHU_CHAT_ID", "")
    send_lark(title, content, lark_app_id, lark_app_secret, lark_chat_id)


if __name__ == "__main__":
    main()
