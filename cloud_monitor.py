#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
青春安理活动报名监控 —— 云端自包含版本。

特点：
1. 抓取与发信都在同一个脚本内完成，不依赖本机文件、不依赖 WorkBuddy 客户端。
2. 所有凭证从环境变量读取（适合放在 GitHub Actions Secrets 等云端密文配置中）：
   - SCHOOL_CODE   学校代码，安徽理工大学为 10361
   - STU_ID        学号
   - STU_PASSWORD  第二课堂密码
   - SMTP_USER     发信邮箱，如 xxx@qq.com
   - SMTP_CODE     邮箱 SMTP 授权码（不是登录密码）
   - SMTP_HOST     SMTP 服务器，默认 smtp.qq.com
   - SMTP_PORT     SMTP 端口，默认 465（SSL）
   - TO_EMAIL      收件邮箱
3. 严格只读：只调用登录、活动列表、活动详情三类读取接口，绝不调用任何报名类写入接口。
4. --dry-run：只抓取并打印邮件正文，不真正发送，用于本地验证。

用法：
   python cloud_monitor.py            # 正常抓取并发邮件
   python cloud_monitor.py --dry-run  # 只验证，不发邮件
"""
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

BASE = "https://win.9xueqi.com/api/app/client/v1/"
SCHOOL_CODE = os.environ.get("SCHOOL_CODE", "10361")
STU_ID = os.environ.get("STU_ID", "")
STU_PASSWORD = os.environ.get("STU_PASSWORD", "")
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_CODE = os.environ.get("SMTP_CODE", "")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
TO_EMAIL = os.environ.get("TO_EMAIL", "")

# 用户条件：由账号实际信息确定，云端无法再调用用户信息接口时可先用环境变量覆盖
MY_GRADE = os.environ.get("MY_GRADE", "2026")
MY_COLLEGE = os.environ.get("MY_COLLEGE", "数学与大数据学院")
MY_MAJOR = os.environ.get("MY_MAJOR", "信息与计算科学")
MY_CLASS = os.environ.get("MY_CLASS", "信计26-4")

DRY_RUN = "--dry-run" in sys.argv
CST = timezone(timedelta(hours=8))

session = requests.Session()
session.headers.update({"Content-Type": "application/x-www-form-urlencoded"})


def now_cst():
    return datetime.now(CST)


def _get(path, params=None, token=None):
    headers = {"Authorization": token} if token else {}
    payload = json.dumps(params, separators=(",", ":"), ensure_ascii=False) if params is not None else None
    q = {"params": payload} if payload is not None else None
    r = session.get(BASE + path, params=q, headers=headers, timeout=30)
    return r.json()


def login():
    if not STU_ID or not STU_PASSWORD:
        raise RuntimeError("缺少环境变量 STU_ID 或 STU_PASSWORD")
    payload = {
        "schoolCode": SCHOOL_CODE,
        "code": STU_ID,
        "password": STU_PASSWORD,
        "openId": "",
    }
    r = session.post(
        BASE + "token",
        data={"params": json.dumps(payload, separators=(",", ":"), ensure_ascii=False)},
        timeout=30,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("登录失败: %s" % d.get("msg"))
    return d["data"]["access_token"]


def fetch_all_activities(token, max_pages=20, page_size=50):
    out, page = [], 1
    total_pages = 1
    while page <= max_pages:
        d = _get("activity/list", {
            "pageNum": page,
            "pageSize": page_size,
            "name": "",
            "classifyId": "",
            "sortType": 1,
        }, token)
        if d.get("code") != 0:
            raise RuntimeError("活动列表读取失败: %s" % d.get("msg"))
        data = d["data"]
        lst = data.get("list", []) or []
        out.extend(lst)
        total = data.get("total", 0)
        ps = data.get("pageSize", page_size) or page_size
        total_pages = max(1, (int(total) + int(ps) - 1) // int(ps))
        if page >= total_pages or not lst:
            break
        page += 1
    seen, uniq = set(), []
    for a in out:
        if a.get("id") not in seen:
            seen.add(a.get("id"))
            uniq.append(a)
    return uniq


def grade_ok(detail):
    grades = detail.get("gradeList") or []
    if not grades:
        return True, "不限"
    names = [str(g.get("name", "")) for g in grades]
    allowed = [n for n in names if str(MY_GRADE) in n or ("级" not in n)]
    return (len(allowed) > 0), "/".join(names)


def college_ok(detail):
    colleges = detail.get("collegeList") or []
    orgs = detail.get("organizationList") or []
    labels = [c.get("name", "") for c in colleges] + [o.get("name", "") for o in orgs]
    if not labels:
        return True, "不限"
    for s in labels:
        if s and (s in MY_COLLEGE or MY_COLLEGE in s or s in MY_MAJOR or s in MY_MAJOR or s in MY_CLASS or "校" in s):
            return True, "/".join(labels)
    return False, "/".join(labels)


def fmt_ts(ms):
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, CST).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ms)


def check_activity(token, activity):
    aid = activity.get("id")
    d = _get("activity/detail/non-member", {"id": aid}, token)
    if d.get("code") != 0:
        return None
    x = d["data"]
    status = x.get("activityStatus")
    joined = x.get("joinMemberCount", 0)
    limit = x.get("peopleLimit", 0)
    now_ms = int(time.time() * 1000)

    in_signup = (status == 5)
    has_slot = (limit > joined)
    not_closed = (x.get("enrollEndTime") or 0) > now_ms
    g_ok, g_label = grade_ok(x)
    c_ok, c_label = college_ok(x)

    return {
        "id": aid,
        "name": x.get("name", ""),
        "joined": joined,
        "limit": limit,
        "remaining": max(limit - joined, 0),
        "enroll_end": fmt_ts(x.get("enrollEndTime")),
        "classify": x.get("classifyName", ""),
        "organizer": x.get("organizationName", ""),
        "address": x.get("address", ""),
        "grade_limit": g_label,
        "college_limit": c_label,
        "eligible": bool(in_signup and has_slot and not_closed and g_ok and c_ok),
        "url": "https://win.9xueqi.com/#/ActivityShow/%s" % aid,
    }


def build_email(eligible, checked_at):
    lines = []
    lines.append("# 青春安理 · 可报名活动汇总")
    lines.append("")
    lines.append("检查时间：%s（北京时间）" % checked_at)
    lines.append("")
    lines.append("当前共 **%d** 个符合条件、正在报名且有名额的活动。" % len(eligible))
    lines.append("")
    if not eligible:
        lines.append("本次检查未发现符合条件且可报名的活动。")
    else:
        for i, a in enumerate(eligible, 1):
            lines.append("## %d. %s" % (i, a["name"]))
            lines.append("")
            lines.append("- 剩余名额：**%s / %s**" % (a["remaining"], a["limit"]))
            lines.append("- 报名截止：%s" % (a["enroll_end"] or "未标注"))
            lines.append("- 分类：%s" % (a["classify"] or "未标注"))
            lines.append("- 组织方：%s" % (a["organizer"] or "未标注"))
            lines.append("- 地点：%s" % (a["address"] or "未标注"))
            lines.append("- 链接：%s" % a["url"])
            lines.append("")
    lines.append("---")
    lines.append("本邮件由自动监控发出，仅读取信息，不会替你报名。")
    return "\n".join(lines)


def send_email(subject, markdown_body):
    if not SMTP_USER or not SMTP_CODE or not TO_EMAIL:
        raise RuntimeError("缺少 SMTP 环境变量（SMTP_USER / SMTP_CODE / TO_EMAIL）")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = TO_EMAIL
    # 邮件客户端对 Markdown 支持不一，同时提供纯文本版本
    msg.attach(MIMEText(markdown_body, "plain", "utf-8"))

    if SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30)
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.starttls()
    try:
        server.login(SMTP_USER, SMTP_CODE)
        server.sendmail(SMTP_USER, [TO_EMAIL], msg.as_string())
    finally:
        server.quit()


def main():
    token = login()
    activities = fetch_all_activities(token)
    results = []
    for a in activities:
        try:
            r = check_activity(token, a)
            if r:
                results.append(r)
        except Exception as e:
            print("WARN check %s: %s" % (a.get("id"), e), file=sys.stderr)
        time.sleep(0.15)

    eligible = [r for r in results if r["eligible"]]
    eligible.sort(key=lambda x: (x["enroll_end"], -x["remaining"]))
    checked_at = now_cst().strftime("%Y-%m-%d %H:%M:%S")
    body = build_email(eligible, checked_at)
    subject = "【青春安理】可报名活动汇总（%s）" % checked_at

    print("检查时间:", checked_at)
    print("扫描活动数:", len(results))
    print("符合条件且可报名:", len(eligible))
    print("-" * 50)
    print(body)
    print("-" * 50)

    if DRY_RUN:
        print("[dry-run] 未发送邮件。")
        return

    send_email(subject, body)
    print("[ok] 邮件已发送至", TO_EMAIL)


if __name__ == "__main__":
    main()
