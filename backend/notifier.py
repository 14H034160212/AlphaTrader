"""
Push Notification System — Email + Slack
=========================================
Sends real-time alerts to Email and/or Slack for:
  - Auto trade executed (BUY/SELL)
  - Blog intelligence alert (e.g. Anthropic COBOL post → IBM sell)
  - Macro scenario activated (2028 GIC, Fed pivot, etc.)
  - Extreme social sentiment detected

Configuration (store in DB settings via API or set_setting):
  notify_email_sender    = "yourbot@gmail.com"
  notify_email_password  = "xxxx xxxx xxxx xxxx"  (Gmail App Password)
  notify_email_recipient = "you@gmail.com"
  notify_slack_webhook   = "https://hooks.slack.com/services/T.../B.../"
  notify_enabled         = "true"

Gmail App Password setup:
  1. Go to myaccount.google.com → Security → 2-Step Verification (must be on)
  2. Search "App passwords" → Create one for "SerenityAlphaTrader"
  3. Copy the 16-char password → paste into notify_email_password
"""
import sys
sys.path.append("/home/qbao775/.local/lib/python3.8/site-packages")

import logging
import smtplib
import json
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ── Credential helpers ────────────────────────────────────────────────────────

def _get_config(db) -> dict:
    """
    Read notification config from DB settings.
    Reads from the first user that has notify_enabled=true,
    or falls back to user_id=1 (trader).
    """
    try:
        from database import get_setting, Settings, User
        # Find first user with notifications enabled
        setting = db.query(Settings).filter(
            Settings.key == "notify_enabled", Settings.value == "true"
        ).first()
        user_id = setting.user_id if setting else 1

        return {
            "enabled": get_setting(db, "notify_enabled", user_id, "false") == "true",
            "email_sender": get_setting(db, "notify_email_sender", user_id, ""),
            "email_password": get_setting(db, "notify_email_password", user_id, ""),
            "email_recipient": get_setting(db, "notify_email_recipient", user_id, ""),
            "slack_webhook": get_setting(db, "notify_slack_webhook", user_id, ""),
        }
    except Exception as e:
        logger.debug(f"[Notifier] Could not read config: {e}")
        return {"enabled": False}


# ── Email sender ──────────────────────────────────────────────────────────────

def _send_email(sender: str, password: str, recipient: str, subject: str, body: str) -> bool:
    """Send an email via Gmail SMTP with TLS."""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"SerenityAlphaTrader <{sender}>"
        msg["To"] = recipient

        # Plain text version
        text_part = MIMEText(body, "plain", "utf-8")
        # HTML version (with basic formatting)
        html_body = body.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        html_part = MIMEText(
            f"<html><body style='font-family:monospace;font-size:14px;'>{html_body}</body></html>",
            "html", "utf-8"
        )
        msg.attach(text_part)
        msg.attach(html_part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())

        logger.info(f"[Notifier] Email sent: {subject}")
        return True
    except Exception as e:
        logger.error(f"[Notifier] Email failed: {e}")
        return False


# ── Slack sender ──────────────────────────────────────────────────────────────

def _send_slack(webhook_url: str, text: str, blocks: Optional[list] = None) -> bool:
    """Send a message to Slack via Incoming Webhook."""
    try:
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        resp = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        if resp.status_code == 200:
            logger.info(f"[Notifier] Slack sent: {text[:60]}...")
            return True
        else:
            logger.error(f"[Notifier] Slack failed: HTTP {resp.status_code} — {resp.text}")
            return False
    except Exception as e:
        logger.error(f"[Notifier] Slack error: {e}")
        return False


def _notify(db, subject: str, body: str, slack_text: str = "", slack_blocks: list = None):
    """Send to all configured channels (email + slack)."""
    cfg = _get_config(db)
    if not cfg.get("enabled"):
        return

    slack_msg = slack_text or body

    if cfg.get("email_sender") and cfg.get("email_recipient") and cfg.get("email_password"):
        _send_email(cfg["email_sender"], cfg["email_password"], cfg["email_recipient"], subject, body)

    if cfg.get("slack_webhook"):
        _send_slack(cfg["slack_webhook"], slack_msg, slack_blocks)


# ── Public notification functions ─────────────────────────────────────────────

def notify_trade(db, symbol: str, side: str, quantity: float, price: float,
                 total: float, reasoning: str, trigger: str = "auto"):
    """Notify when an auto trade is executed."""
    emoji = "📈" if side == "BUY" else "📉"
    trigger_label = {"auto": "AI Auto-Trade", "blog_alert": "Blog Alert", "pre_event": "Pre-Event"}.get(trigger, trigger)
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{emoji} SerenityAlphaTrader: {side} {symbol} @ ${price:.2f}"

    body = f"""
{emoji} AUTO TRADE EXECUTED — {trigger_label}
{'='*50}
Time:      {now}
Symbol:    {symbol}
Action:    {side}
Quantity:  {quantity} shares
Price:     ${price:.2f}
Total:     ${total:.2f}

AI Reasoning:
{reasoning[:600]}
{'='*50}
SerenityAlphaTrader Autonomous Trading Platform
""".strip()

    slack_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {side} {symbol} — {trigger_label}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Symbol:* {symbol}"},
            {"type": "mrkdwn", "text": f"*Action:* {side}"},
            {"type": "mrkdwn", "text": f"*Quantity:* {quantity} shares"},
            {"type": "mrkdwn", "text": f"*Price:* ${price:.2f}"},
            {"type": "mrkdwn", "text": f"*Total:* ${total:.2f}"},
            {"type": "mrkdwn", "text": f"*Time:* {now}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*AI Reasoning:*\n{reasoning[:400]}"}},
        {"type": "divider"},
    ]

    _notify(db, subject, body, slack_text=f"{emoji} {side} {symbol} × {quantity} @ ${price:.2f} | {trigger_label}", slack_blocks=slack_blocks)


def notify_blog_alert(db, source: str, title: str, link: str,
                      severity: str, sell_stocks: list, watch_stocks: list, reason: str):
    """Notify when a high-impact official blog post is detected."""
    emoji = {"CRITICAL": "🚨", "HIGH": "⚠️", "MEDIUM": "📌"}.get(severity, "📌")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{emoji} [{severity}] Blog Alert: {source} — {title[:60]}"

    body = f"""
{emoji} OFFICIAL BLOG INTELLIGENCE ALERT [{severity}]
{'='*50}
Time:     {now}
Source:   {source}
Title:    {title}
Link:     {link}

Impact:
{reason}

SELL / AVOID: {', '.join(sell_stocks) if sell_stocks else 'None'}
CONSIDER:     {', '.join(watch_stocks) if watch_stocks else 'None'}
{'='*50}
SerenityAlphaTrader Blog Monitor — First-party source detected
""".strip()

    sell_str = " | ".join(f"`{s}`" for s in sell_stocks) if sell_stocks else "None"
    watch_str = " | ".join(f"`{s}`" for s in watch_stocks) if watch_stocks else "None"

    slack_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Blog Alert [{severity}]: {source}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n<{link}|Read article>"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Reason:*\n{reason[:200]}"},
            {"type": "mrkdwn", "text": f"*Time:* {now}"},
        ]},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*📉 SELL / AVOID:* {sell_str}"},
            {"type": "mrkdwn", "text": f"*📈 CONSIDER:* {watch_str}"},
        ]},
        {"type": "divider"},
    ]

    _notify(db, subject, body,
            slack_text=f"{emoji} [{severity}] {source}: \"{title[:80]}\" | SELL: {', '.join(sell_stocks)}",
            slack_blocks=slack_blocks)


def notify_macro_scenario(db, scenario_name: str, severity: str,
                           description: str, stocks_to_avoid: list,
                           beneficiaries: list, evidence_count: int):
    """Notify when a macro scenario (2028 GIC, Fed pivot, etc.) is activated."""
    emoji = {"CRITICAL": "🚨", "HIGH": "⚠️", "BULLISH": "📈"}.get(severity, "📌")
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{emoji} [{severity}] Macro Alert: {scenario_name}"

    body = f"""
{emoji} MACRO SCENARIO ALERT [{severity}]
{'='*50}
Time:      {now}
Scenario:  {scenario_name}
Evidence:  {evidence_count} news articles matched

Description:
{description}

AVOID / SELL: {', '.join(stocks_to_avoid) if stocks_to_avoid else 'None'}
CONSIDER:     {', '.join(beneficiaries) if beneficiaries else 'None'}
{'='*50}
SerenityAlphaTrader Macro Intelligence System
""".strip()

    avoid_str = " | ".join(f"`{s}`" for s in stocks_to_avoid) if stocks_to_avoid else "None"
    benefit_str = " | ".join(f"`{s}`" for s in beneficiaries) if beneficiaries else "None"

    slack_blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Macro Scenario [{severity}]: {scenario_name}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": description}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Evidence:* {evidence_count} articles"},
            {"type": "mrkdwn", "text": f"*Time:* {now}"},
        ]},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*📉 AVOID / SELL:* {avoid_str}"},
            {"type": "mrkdwn", "text": f"*📈 CONSIDER:* {benefit_str}"},
        ]},
        {"type": "divider"},
    ]

    _notify(db, subject, body,
            slack_text=f"{emoji} MACRO [{severity}]: {scenario_name} | AVOID: {', '.join(stocks_to_avoid)}",
            slack_blocks=slack_blocks)


def notify_sentiment_alert(db, symbol: str, sentiment: str, score: float,
                            bullish: int, bearish: int, total: int):
    """Notify when extreme social sentiment is detected on a stock."""
    emoji = "🐂" if sentiment == "BULLISH" else "🐻"
    direction = "极端看涨" if sentiment == "BULLISH" else "极端看跌"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    subject = f"{emoji} {symbol} 社交情绪: {sentiment} ({score:+.2f})"

    body = f"""
{emoji} 极端社交情绪预警
{'='*50}
Time:      {now}
Symbol:    {symbol}
Sentiment: {sentiment} ({direction})
Score:     {score:+.2f}  (range: -1.0 to +1.0)
Bullish:   {bullish} messages ↑
Bearish:   {bearish} messages ↓
Total:     {total} messages on StockTwits

Note: Extreme retail sentiment can signal:
  BULLISH: crowded trade (potential reversal risk)
  BEARISH: capitulation (potential contrarian buy)
{'='*50}
SerenityAlphaTrader Social Sentiment Monitor
""".strip()

    slack_text = f"{emoji} {symbol}: {sentiment} ({score:+.2f}) — {bullish}↑ {bearish}↓ on StockTwits"

    _notify(db, subject, body, slack_text=slack_text)


def notify_daily_summary(db, session_type: str, portfolio: dict,
                          trades_today: list, blog_alerts: list,
                          macro_alerts: list, sentiment_alerts: list):
    """
    Send one daily digest email.
    session_type: "pre_market" | "post_market"
    portfolio: dict from engine.get_portfolio_summary()
    trades_today: list of {symbol, side, quantity, price, total, reasoning, trigger}
    blog_alerts: list of blog alert dicts from blog_monitor
    macro_alerts: list of active macro scenario dicts
    sentiment_alerts: list of {symbol, sentiment, score}
    """
    now_utc = datetime.utcnow()
    # NZT = UTC+13
    now_nzt = now_utc.replace(hour=(now_utc.hour + 13) % 24)
    nzt_str = now_nzt.strftime("%Y-%m-%d %H:%M NZT")

    if session_type == "pre_market":
        title = "📊 SerenityAlphaTrader 开市前日报"
        subtitle = "美股今日开市，以下是市场概况与今日计划"
    else:
        title = "📋 SerenityAlphaTrader 收市后日报"
        subtitle = "美股今日收市，以下是今日交易汇总"

    subject = f"{title} — {now_utc.strftime('%Y-%m-%d')}"

    # ── Portfolio Summary ─────────────────────────────────────────────────────
    equity = portfolio.get("total_equity", 0)
    cash = portfolio.get("cash", 0)
    ret = portfolio.get("total_return", 0)
    ret_pct = portfolio.get("total_return_pct", 0)
    ret_sign = "+" if ret >= 0 else ""
    positions = portfolio.get("positions", [])

    port_lines = [
        f"总资产:    ${equity:,.2f}",
        f"可用现金:  ${cash:,.2f}",
        f"总盈亏:    {ret_sign}${ret:,.2f}  ({ret_sign}{ret_pct:.2f}%)",
    ]
    if positions:
        port_lines.append("\n持仓:")
        for p in sorted(positions, key=lambda x: x.get("market_value", 0), reverse=True)[:8]:
            pnl = p.get("unrealized_pnl", 0)
            pnl_s = f'+${pnl:.2f}' if pnl >= 0 else f'-${abs(pnl):.2f}'
            port_lines.append(
                f"  {p['symbol']:6s} × {p['quantity']:.4f} @ ${p['current_price']:.2f}  ({pnl_s})"
            )
    else:
        port_lines.append("  无持仓")

    # ── Today's Trades ────────────────────────────────────────────────────────
    if trades_today:
        trade_lines = [f"\n{'='*50}", "📈 今日交易记录:"]
        for t in trades_today:
            emoji = "📈" if t["side"] == "BUY" else "📉"
            trigger_label = {"auto": "AI自动", "blog_alert": "博客预警", "pre_event": "事件预判"}.get(t.get("trigger", "auto"), "AI自动")
            trade_lines.append(
                f"  {emoji} {t['side']} {t['symbol']} × {t['quantity']:.4f} "
                f"@ ${t['price']:.2f}  (合计 ${t['total']:.2f})  [{trigger_label}]"
            )
            if t.get("reasoning"):
                trade_lines.append(f"     理由: {t['reasoning'][:150]}")
    else:
        trade_lines = ["\n今日暂无交易执行。"]

    # ── Blog Alerts ───────────────────────────────────────────────────────────
    if blog_alerts:
        alert_lines = [f"\n{'='*50}", "🚨 官方博客威胁预警:"]
        for a in blog_alerts[:5]:
            sell = [s for imp in a["impacts"] for s in imp["stocks_to_avoid"]]
            alert_lines.append(
                f"  [{a['max_severity']}] {a['source_name']}: \"{a['title'][:80]}\""
            )
            if sell:
                alert_lines.append(f"     受影响股票 (建议卖出): {', '.join(sell)}")
    else:
        alert_lines = []

    # ── Macro Alerts ──────────────────────────────────────────────────────────
    if macro_alerts:
        macro_lines = [f"\n{'='*50}", "🌐 宏观场景预警:"]
        for m in macro_alerts:
            macro_lines.append(f"  [{m['severity']}] {m['name']}")
            if m["stocks_to_avoid"]:
                macro_lines.append(f"     建议回避: {', '.join(m['stocks_to_avoid'])}")
    else:
        macro_lines = []

    # ── Sentiment Highlights ──────────────────────────────────────────────────
    if sentiment_alerts:
        sent_lines = [f"\n{'='*50}", "📱 StockTwits 情绪异常:"]
        for s in sentiment_alerts[:5]:
            # scan_sentiment_alerts returns dicts with 'sentiment_label'/'sentiment_score' keys
            label = s.get("sentiment", s.get("sentiment_label", "NEUTRAL"))
            score = s.get("score", s.get("sentiment_score", 0))
            symbol = s.get("symbol", "?")
            emoji = "🐂" if label == "BULLISH" else "🐻"
            sent_lines.append(f"  {emoji} {symbol}: {label} ({score:+.2f})")
    else:
        sent_lines = []

    # ── Assemble ──────────────────────────────────────────────────────────────
    body = f"""
{title}
{nzt_str} | {subtitle}
{'='*50}

💼 账户概况:
{'  ' + chr(10) + '  '.join(port_lines)}

{'  ' + chr(10).join(trade_lines)}
{'  ' + chr(10).join(alert_lines)}
{'  ' + chr(10).join(macro_lines)}
{'  ' + chr(10).join(sent_lines)}

{'='*50}
SerenityAlphaTrader 自主AI交易平台
下一封: {'收市后日报 (NZT 10:15 AM)' if session_type == 'pre_market' else '明日开市前日报 (NZT 03:25 AM)'}
""".strip()

    _notify(db, subject, body, slack_text=f"{title} | 资产: ${equity:,.2f} | 今日交易: {len(trades_today)}笔")
