"""
Daily email reporter for AlphaTrader.
- Sends a daily portfolio + AI signal report via Gmail SMTP
- Uses IMAP IDLE for real-time push notification when user replies
  (server notifies immediately; no polling delay)
"""
import smtplib
import imaplib
import email as email_lib
import logging
import re
import socket
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.header import decode_header

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
REPORT_SUBJECT_PREFIX = "AlphaTrader Daily Report"


# ---------------------------------------------------------------------------
# HTML report generation
# ---------------------------------------------------------------------------

def _color(val: float, positive_good=True) -> str:
    if val > 0:
        return "#27ae60" if positive_good else "#e74c3c"
    elif val < 0:
        return "#e74c3c" if positive_good else "#27ae60"
    return "#7f8c8d"


def _signal_badge(signal: str) -> str:
    colors = {"BUY": "#27ae60", "SELL": "#e74c3c", "HOLD": "#f39c12"}
    c = colors.get(signal.upper(), "#7f8c8d")
    return f'<span style="background:{c};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;font-weight:bold;">{signal}</span>'


def _pct(val) -> str:
    if val is None:
        return "N/A"
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.2f}%"


def generate_report_html(
    date_str: str,
    alpaca_account: dict,
    positions: list,
    signals: list,
    macro_scenarios: list,
    planned_trades: list,
) -> str:
    """Generate a full HTML daily report email."""

    # ── Account summary ──────────────────────────────────────────────────────
    equity = alpaca_account.get("equity", 0)
    cash = alpaca_account.get("cash", 0)
    day_pnl = alpaca_account.get("unrealized_pl", 0)
    day_pnl_pct = (day_pnl / (equity - day_pnl) * 100) if (equity - day_pnl) != 0 else 0
    pnl_color = _color(day_pnl)

    # ── Positions table rows ─────────────────────────────────────────────────
    pos_rows = ""
    if positions:
        for p in positions:
            sym = p.get("symbol", "")
            qty = p.get("qty", p.get("quantity", 0))
            avg = p.get("avg_entry_price", p.get("avg_cost", 0))
            cur = p.get("current_price", 0)
            pnl = p.get("unrealized_pl", p.get("unrealized_pnl", 0))
            pnl_pct = p.get("unrealized_plpc", 0)
            if isinstance(pnl_pct, float) and abs(pnl_pct) < 1:
                pnl_pct *= 100  # convert from decimal if needed
            mv = float(qty) * float(cur) if qty and cur else 0
            c = _color(float(pnl) if pnl else 0)
            pos_rows += f"""
            <tr>
              <td style="padding:8px;font-weight:600;">{sym}</td>
              <td style="padding:8px;">{qty}</td>
              <td style="padding:8px;">${float(avg):.2f}</td>
              <td style="padding:8px;">${float(cur):.2f}</td>
              <td style="padding:8px;">${mv:.2f}</td>
              <td style="padding:8px;color:{c};font-weight:600;">{_pct(float(pnl_pct) if pnl_pct else 0)} (${float(pnl):.2f})</td>
            </tr>"""
    else:
        pos_rows = '<tr><td colspan="6" style="padding:8px;color:#999;text-align:center;">暂无持仓</td></tr>'

    # ── AI signals table rows ────────────────────────────────────────────────
    sig_rows = ""
    if signals:
        for s in signals[:10]:
            sym = s.get("symbol", "")
            sig = s.get("signal", "HOLD")
            conf = s.get("confidence", 0)
            reason = s.get("reasoning", "")[:120] + ("..." if len(s.get("reasoning", "")) > 120 else "")
            ts = s.get("timestamp", "")[:16] if s.get("timestamp") else ""
            sig_rows += f"""
            <tr>
              <td style="padding:8px;font-weight:600;">{sym}</td>
              <td style="padding:8px;">{_signal_badge(sig)}</td>
              <td style="padding:8px;">{int(conf * 100)}%</td>
              <td style="padding:8px;color:#555;font-size:12px;">{reason}</td>
              <td style="padding:8px;color:#999;font-size:11px;">{ts}</td>
            </tr>"""
    else:
        sig_rows = '<tr><td colspan="5" style="padding:8px;color:#999;text-align:center;">今日暂无新信号</td></tr>'

    # ── Macro alerts ─────────────────────────────────────────────────────────
    macro_html = ""
    if macro_scenarios:
        for m in macro_scenarios:
            sev = m.get("severity", "LOW")
            sev_colors = {"CRITICAL": "#e74c3c", "HIGH": "#e67e22", "MEDIUM": "#f39c12", "LOW": "#27ae60"}
            sc = sev_colors.get(sev, "#7f8c8d")
            macro_html += f"""
            <div style="border-left:4px solid {sc};padding:8px 12px;margin:6px 0;background:#fafafa;">
              <strong style="color:{sc};">[{sev}]</strong> {m.get('name', '')}
              <br><small style="color:#555;">受益: {', '.join(m.get('beneficiaries', [])[:5])}</small>
            </div>"""
    else:
        macro_html = '<p style="color:#999;">无活跃宏观事件</p>'

    # ── Planned trades ───────────────────────────────────────────────────────
    plan_rows = ""
    if planned_trades:
        for t in planned_trades:
            sym = t.get("symbol", "")
            action = t.get("action", "")
            reason = t.get("reason", "")[:100]
            conf = t.get("confidence", 0)
            c = "#27ae60" if action == "BUY" else "#e74c3c"
            plan_rows += f"""
            <tr>
              <td style="padding:8px;font-weight:600;">{sym}</td>
              <td style="padding:8px;"><span style="color:{c};font-weight:bold;">{action}</span></td>
              <td style="padding:8px;">{int(conf*100)}%</td>
              <td style="padding:8px;color:#555;font-size:12px;">{reason}</td>
            </tr>"""
    else:
        plan_rows = '<tr><td colspan="4" style="padding:8px;color:#999;text-align:center;">暂无计划交易</td></tr>'

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f6f8;font-family:'Helvetica Neue',Arial,sans-serif;color:#2c3e50;">
<div style="max-width:680px;margin:24px auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">

  <!-- Header -->
  <div style="background:linear-gradient(135deg,#1a1a2e 0%,#16213e 100%);padding:28px 32px;color:#fff;">
    <div style="font-size:22px;font-weight:700;letter-spacing:1px;">📈 AlphaTrader Daily Report</div>
    <div style="margin-top:6px;color:#a0b4c8;font-size:14px;">{date_str} · 美东时间收盘后汇报</div>
  </div>

  <!-- Account Summary -->
  <div style="background:#f8f9fa;padding:20px 32px;border-bottom:1px solid #eee;">
    <div style="font-size:13px;color:#7f8c8d;margin-bottom:6px;">ALPACA 账户总览</div>
    <div style="display:flex;gap:32px;flex-wrap:wrap;">
      <div><div style="font-size:12px;color:#999;">总资产</div><div style="font-size:22px;font-weight:700;">${equity:.2f}</div></div>
      <div><div style="font-size:12px;color:#999;">现金</div><div style="font-size:18px;font-weight:600;">${cash:.2f}</div></div>
      <div><div style="font-size:12px;color:#999;">今日盈亏</div>
        <div style="font-size:18px;font-weight:600;color:{pnl_color};">{'+' if day_pnl >= 0 else ''}${day_pnl:.2f} ({_pct(day_pnl_pct)})</div>
      </div>
    </div>
  </div>

  <div style="padding:24px 32px;">

    <!-- Positions -->
    <h3 style="margin:0 0 12px;font-size:15px;color:#2c3e50;border-bottom:2px solid #3498db;padding-bottom:6px;">📦 当前持仓</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#ecf0f1;">
          <th style="padding:8px;text-align:left;">标的</th>
          <th style="padding:8px;text-align:left;">数量</th>
          <th style="padding:8px;text-align:left;">成本价</th>
          <th style="padding:8px;text-align:left;">现价</th>
          <th style="padding:8px;text-align:left;">市值</th>
          <th style="padding:8px;text-align:left;">持仓盈亏</th>
        </tr>
      </thead>
      <tbody>{pos_rows}</tbody>
    </table>

    <!-- AI Signals -->
    <h3 style="margin:24px 0 12px;font-size:15px;color:#2c3e50;border-bottom:2px solid #9b59b6;padding-bottom:6px;">🤖 AI 信号（今日最新）</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#ecf0f1;">
          <th style="padding:8px;text-align:left;">标的</th>
          <th style="padding:8px;text-align:left;">信号</th>
          <th style="padding:8px;text-align:left;">置信度</th>
          <th style="padding:8px;text-align:left;">AI 分析摘要</th>
          <th style="padding:8px;text-align:left;">时间</th>
        </tr>
      </thead>
      <tbody>{sig_rows}</tbody>
    </table>

    <!-- Macro Alerts -->
    <h3 style="margin:24px 0 12px;font-size:15px;color:#2c3e50;border-bottom:2px solid #e74c3c;padding-bottom:6px;">🌍 宏观预警</h3>
    {macro_html}

    <!-- Tomorrow's Plan -->
    <h3 style="margin:24px 0 12px;font-size:15px;color:#2c3e50;border-bottom:2px solid #27ae60;padding-bottom:6px;">📋 明日计划交易</h3>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <thead>
        <tr style="background:#ecf0f1;">
          <th style="padding:8px;text-align:left;">标的</th>
          <th style="padding:8px;text-align:left;">操作</th>
          <th style="padding:8px;text-align:left;">置信度</th>
          <th style="padding:8px;text-align:left;">理由</th>
        </tr>
      </thead>
      <tbody>{plan_rows}</tbody>
    </table>

    <!-- Reply Instructions -->
    <div style="margin-top:28px;padding:16px;background:#eaf4fb;border-radius:6px;border:1px solid #bee3f8;">
      <div style="font-size:13px;font-weight:600;color:#2980b9;margin-bottom:6px;">💬 定制化指令</div>
      <div style="font-size:13px;color:#555;line-height:1.7;">
        直接回复此邮件即可向 AI 发出指令，例如：<br>
        • "把 LMT 的仓位调小一点"<br>
        • "不要买能源股"<br>
        • "加入茅台 600519.SH 到分析列表"<br>
        • "置信度阈值提高到 80%"<br><br>
        AI 会在 30 分钟内读取并自动执行。
      </div>
    </div>

  </div>

  <!-- Footer -->
  <div style="background:#f8f9fa;padding:16px 32px;text-align:center;color:#aaa;font-size:11px;border-top:1px solid #eee;">
    AlphaTrader Pro · 本报告由 AI 自动生成，不构成投资建议 · 请独立判断风险
  </div>

</div>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def send_email(sender: str, app_password: str, recipient: str, subject: str, html: str) -> bool:
    """Send an HTML email via Gmail SMTP."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"AlphaTrader <{sender}>"
        msg["To"] = recipient
        msg["X-Mailer"] = "AlphaTrader-Pro"
        msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, app_password)
            server.sendmail(sender, recipient, msg.as_string())
        logger.info(f"[Email] Report sent to {recipient}")
        return True
    except Exception as e:
        logger.error(f"[Email] Failed to send: {e}")
        return False


# ---------------------------------------------------------------------------
# IMAP reply checker
# ---------------------------------------------------------------------------

def _decode_str(s) -> str:
    """Decode encoded email header string."""
    parts = decode_header(s)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += part
    return result


def _extract_body(msg) -> str:
    """Extract plain text body from an email message."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = part.get("Content-Disposition", "")
            if ct == "text/plain" and "attachment" not in cd:
                charset = part.get_content_charset() or "utf-8"
                body += part.get_payload(decode=True).decode(charset, errors="replace")
    else:
        charset = msg.get_content_charset() or "utf-8"
        body = msg.get_payload(decode=True).decode(charset, errors="replace")

    # Strip quoted reply (lines starting with >)
    lines = [l for l in body.splitlines() if not l.startswith(">")]
    body = "\n".join(lines).strip()
    return body


def _fetch_new_replies(mail: imaplib.IMAP4_SSL) -> list[dict]:
    """
    After IDLE notifies of new mail, fetch any unread AlphaTrader reply messages.
    Returns list of {subject, body, date} dicts.
    """
    replies = []
    try:
        # Search for unseen replies with AlphaTrader in subject
        status, data = mail.search(None, '(UNSEEN SUBJECT "AlphaTrader")')
        if status != "OK" or not data[0]:
            return []
        for uid in data[0].split():
            status, msg_data = mail.fetch(uid, "(RFC822)")
            if status != "OK":
                continue
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)
            subject = _decode_str(msg.get("Subject", ""))
            # Only process replies (Re:), not reports we sent
            if not re.search(r"\bRe\b", subject, re.IGNORECASE):
                # Mark as seen so we don't reprocess
                mail.store(uid, "+FLAGS", "\\Seen")
                continue
            body = _extract_body(msg)
            if body:
                replies.append({
                    "subject": subject,
                    "body": body,
                    "date": msg.get("Date", ""),
                })
            mail.store(uid, "+FLAGS", "\\Seen")
    except Exception as e:
        logger.error(f"[Email] Error fetching replies: {e}")
    return replies


def idle_wait(mail: imaplib.IMAP4_SSL, timeout: int = 840) -> bool:
    """
    Send IMAP IDLE command and block until the server signals new mail
    or the timeout expires (default 14 min — Gmail drops idle at 15 min).
    Returns True if new mail detected, False on timeout/error.
    """
    try:
        # Send IDLE command
        tag = mail._new_tag()
        if isinstance(tag, bytes):
            tag = tag.decode()
        mail.send(f"{tag} IDLE\r\n".encode())

        # Read the continuation response: "+ idling" or similar
        mail.readline()

        # Wait for server push
        mail.socket().settimeout(timeout)
        new_mail = False
        try:
            while True:
                line = mail.readline()
                if not line:
                    break
                # Server sends "* N EXISTS" or "* N RECENT" on new mail
                if b"EXISTS" in line or b"RECENT" in line:
                    new_mail = True
                    break
        except socket.timeout:
            pass  # Normal — just means no new mail in this window

        # Exit IDLE
        try:
            mail.send(b"DONE\r\n")
            mail.readline()
        except Exception:
            pass
        mail.socket().settimeout(None)
        return new_mail
    except Exception as e:
        logger.error(f"[Email] IDLE error: {e}")
        return False


def connect_imap(sender: str, app_password: str) -> imaplib.IMAP4_SSL | None:
    """Open a persistent IMAP connection for IDLE."""
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(sender, app_password)
        mail.select("INBOX")
        return mail
    except Exception as e:
        logger.error(f"[Email] IMAP connect failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Reply → AI instruction processor
# ---------------------------------------------------------------------------

async def process_reply_with_ai(reply_body: str, db, settings: dict) -> dict:
    """
    Feed user's email reply to DeepSeek AI and apply suggested changes.
    Returns dict of changes applied.
    """
    try:
        import deepseek_ai as _ai_mod

        prompt = f"""You are the control system for an automated stock trading platform called AlphaTrader.
The user has replied to their daily report with the following instruction:

---
{reply_body}
---

Current settings:
- Auto trade enabled: {settings.get('auto_trade_enabled', 'true')}
- Min confidence threshold: {settings.get('auto_trade_min_confidence', '0.70')}
- Risk per trade: {settings.get('risk_per_trade_pct', '2.0')}%
- Current watchlist: {settings.get('watchlist', '[]')}

Based on the user's instruction, return a JSON object with ONLY the fields that should change:
{{
  "auto_trade_enabled": "true" or "false" (if user wants to pause/resume trading),
  "auto_trade_min_confidence": "0.80" (if user wants to change confidence threshold),
  "risk_per_trade_pct": "3.0" (if user wants to change risk per trade),
  "watchlist_add": ["SYMBOL1", "SYMBOL2"] (symbols to add),
  "watchlist_remove": ["SYMBOL3"] (symbols to remove),
  "reply_message": "Human-readable summary of what changes you made"
}}
If the message is just a greeting or acknowledgement with no action required, return:
{{"reply_message": "收到，无需操作。"}}
Only include fields that actually need to change. Return valid JSON only."""

        messages = [{"role": "user", "content": prompt}]
        response = _ai_mod._call_ollama(messages)
        if not response:
            return {"error": "AI did not respond"}

        # Extract JSON from response — use raw_decode to handle trailing text
        import json
        json_match = re.search(r"\{", response)
        if not json_match:
            return {"reply_message": "收到，无需操作（AI未返回JSON）"}
        try:
            changes, _ = json.JSONDecoder().raw_decode(response, json_match.start())
        except Exception:
            # Fallback: strip to last closing brace
            snippet = response[json_match.start():]
            end = snippet.rfind("}") + 1
            changes = json.loads(snippet[:end])
        changes_applied = {}

        # Apply changes to DB settings
        from database import Settings
        user_id = 1  # default user

        simple_keys = ["auto_trade_enabled", "auto_trade_min_confidence", "risk_per_trade_pct"]
        for key in simple_keys:
            if key in changes:
                val = str(changes[key])
                s = db.query(Settings).filter_by(user_id=user_id, key=key).first()
                if s:
                    s.value = val
                else:
                    db.add(Settings(user_id=user_id, key=key, value=val))
                changes_applied[key] = val

        # Handle watchlist changes
        wl_setting = db.query(Settings).filter_by(user_id=user_id, key="watchlist").first()
        import json as json2
        current_wl = json2.loads(wl_setting.value) if wl_setting else []

        if "watchlist_add" in changes:
            for sym in changes["watchlist_add"]:
                if sym.upper() not in [s.upper() for s in current_wl]:
                    current_wl.append(sym.upper())
            changes_applied["watchlist_add"] = changes["watchlist_add"]

        if "watchlist_remove" in changes:
            current_wl = [s for s in current_wl if s.upper() not in [r.upper() for r in changes["watchlist_remove"]]]
            changes_applied["watchlist_remove"] = changes["watchlist_remove"]

        if "watchlist_add" in changes or "watchlist_remove" in changes:
            if wl_setting:
                wl_setting.value = json2.dumps(current_wl)
            else:
                db.add(Settings(user_id=user_id, key="watchlist", value=json2.dumps(current_wl)))

        db.commit()

        changes_applied["reply_message"] = changes.get("reply_message", "Changes applied.")
        logger.info(f"[Email] Applied user instruction changes: {changes_applied}")
        return changes_applied

    except Exception as e:
        logger.error(f"[Email] Error processing reply: {e}")
        return {"error": str(e)}
