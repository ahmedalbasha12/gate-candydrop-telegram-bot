#!/usr/bin/env python3
"""Single-file Gate CandyDrop Telegram monitoring bot."""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

API = "https://api.gateio.ws/api/v4"
PROFILE_DIR = Path(__file__).resolve().parent / ".gate-browser-profile"
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
)
NUM = r"([0-9][0-9,]*(?:\.[0-9]+)?)"


@dataclass
class Report:
    activity_id: str = ""
    currency: str = ""
    status: str = ""
    url: str = ""
    pool_tokens: float | None = None
    total_candies: float | None = None
    token_per_candy: float | None = None
    token_price_usdt: float | None = None
    gross_usdt_per_candy: float | None = None
    estimated_fee_usdt: float | None = None
    net_usdt_per_candy: float | None = None
    fixed_reward_tokens: float | None = None
    fixed_reward_usdt: float | None = None
    spots_left: int | None = None
    spots_total: int | None = None
    first_trade_min_usdt: float | None = None
    raw_title: str = ""
    checked_at: str = ""
    reward_pools: list[dict[str, Any]] | None = None
    fee_rate: float = 0.0015


def http_json(path: str, params: dict[str, Any] | None = None) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{API}{path}" + (f"?{query}" if query else "")
    timestamp = str(int(time.time()))
    headers = {
        "Accept": "application/json",
        "User-Agent": MOBILE_UA,
        # Gate started requiring this APIv4 header on public CandyDrop routes.
        # It is Unix time in seconds; no API key/signature is needed for reads.
        "Timestamp": timestamp,
    }
    api_key = os.getenv("GATE_API_KEY", "").strip()
    api_secret = os.getenv("GATE_API_SECRET", "").strip()
    if api_key and api_secret:
        body_hash = hashlib.sha512(b"").hexdigest()
        sign_text = f"GET\n/api/v4{path}\n{query}\n{body_hash}\n{timestamp}"
        headers["KEY"] = api_key
        headers["SIGN"] = hmac.new(api_secret.encode(), sign_text.encode(), hashlib.sha512).hexdigest()
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            body = ""
        raise RuntimeError(f"Gate API HTTP {exc.code}: {body or exc.reason} | {url}") from exc


def objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from objects(child)


def first_key(data: Any, names: set[str]) -> Any:
    wanted = {x.lower().replace("_", "") for x in names}
    for obj in objects(data):
        for key, value in obj.items():
            if key.lower().replace("_", "") in wanted and value not in (None, ""):
                return value
    return None


def number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(NUM, value)
        if match:
            return float(match.group(1).replace(",", ""))
    return None


def activity_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("list", "items", "data", "activities", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = activity_items(value)
                if nested:
                    return nested
    return []


def parse_slots(text: str) -> tuple[int | None, int | None]:
    patterns = [
        rf"First\s+{NUM}\s+get rewards?\.?\s*Only\s+{NUM}\s+spot\(s\)\s+left",
        rf"Only\s+{NUM}\s+spot\(s\)\s+left",
        rf"(?:متبق(?:ي|ية)|باقي)\s*{NUM}\s*(?:مكان|أماكن)",
    ]
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.I)
        if match:
            nums = [int(float(x.replace(",", ""))) for x in match.groups()]
            return (nums[-1], nums[0] if index == 0 else None)
    return None, None


def parse_rule_text(text: str, currency: str) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "fixed": None, "minimum": None, "pool": None, "candies": None,
        "estimated": None, "spots_left": None, "spots_total": None,
    }
    fixed = re.search(rf"fixed\s+(?:reward\s+of\s+)?{NUM}\s+{re.escape(currency)}\b", text, re.I)
    if not fixed:
        fixed = re.search(rf"Claim\s+{NUM}\s+{re.escape(currency)}\b", text, re.I)
    minimum = re.search(rf"(?:trade|volume)[^\n]{{0,80}}[≥>]\s*{NUM}\s*USDT", text, re.I)
    pool = re.search(rf"(?:prize\s+pool|pool)\s*:?\s*{NUM}\s+{re.escape(currency)}\b", text, re.I)
    candy = re.search(rf"{NUM}\s*(?:Candy|Candies)\s*\(\s*[≈~]?\s*{NUM}\s+{re.escape(currency)}", text, re.I)
    result["fixed"] = number(fixed.group(1)) if fixed else None
    result["minimum"] = number(minimum.group(1)) if minimum else None
    result["pool"] = number(pool.group(1)) if pool else None
    if candy:
        count, reward = number(candy.group(1)), number(candy.group(2))
        result["candies"] = count
        result["estimated"] = reward / count if count else None
    left, total = parse_slots(text)
    result["spots_left"], result["spots_total"] = left, total
    return result


def reward_kind(title: str) -> str:
    value = title.lower()
    if "vip" in value: return "vip"
    if "first" in value and "spot" in value: return "first_spot"
    if "invite" in value or "friend" in value or "referral" in value: return "invite"
    if "future" in value or "contract" in value: return "futures"
    if "deposit" in value: return "deposit"
    if "convert" in value or "swap" in value: return "convert"
    if "bot" in value: return "trading_bot"
    if "earn" in value: return "earn"
    if "spot" in value or "trading" in value: return "spot"
    return "other"


def parse_reward_pools(text: str, currency: str, price: float | None, fee_rate: float) -> list[dict[str, Any]]:
    """Parse every reward tab/section from the rendered detail-page text."""
    heading = re.compile(
        rf"(?im)^(?P<title>[^\n]*(?:Prize\s+Pool|to\s+Share)[^\n]*?)(?::|\s)\s*{NUM}\s+{re.escape(currency)}\b"
    )
    matches = list(heading.finditer(text))
    pools: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for index, match in enumerate(matches):
        title = re.sub(r"\s+", " ", match.group("title")).strip(" #: ")
        amount = number(match.group(2))
        if title.lower() == "total prize pool":
            continue
        if amount is None or (title.lower(), amount) in seen:
            continue
        seen.add((title.lower(), amount))
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.start() + 2500)
        section = text[match.start():end]
        kind = reward_kind(title)
        cap_match = re.search(rf"Individual\s+Cap\s*\n?\s*{NUM}\s+{re.escape(currency)}", section, re.I)
        claim_match = re.search(rf"Claim\s+{NUM}\s+{re.escape(currency)}", section, re.I)
        threshold_values = [number(x) for x in re.findall(rf"[≥>]\s*{NUM}\s*USDT", section, re.I)]
        tier_candies = [number(x) for x in re.findall(rf"(?im)^\s*{NUM}\s*Cand(?:y|ies)\s*$", section)]
        tier_thresholds = [number(x) for x in re.findall(rf"(?im)^\s*{NUM}\s*USDT\s*$", section)]
        tier_schedule = []
        if tier_candies and len(tier_candies) == len(tier_thresholds):
            tier_schedule = [
                {"minimum_usdt": threshold, "candies": candies}
                for candies, threshold in zip(tier_candies, tier_thresholds)
                if candies is not None and threshold is not None
            ]
        candy_values: list[dict[str, float]] = []
        for count_s, reward_s in re.findall(rf"{NUM}\s*(?:Candy|Candies)\s*[≈~]\s*{NUM}\s+{re.escape(currency)}", section, re.I):
            count, reward = number(count_s), number(reward_s)
            if count and reward is not None:
                candy_values.append({"candies": count, "reward_tokens": reward, "tokens_per_candy": reward / count})
        left, total = parse_slots(section)
        fixed = number(claim_match.group(1)) if claim_match else None
        per_candy = candy_values[0]["tokens_per_candy"] if candy_values else None
        minimum = min((x for x in threshold_values if x is not None), default=None)
        fees = minimum * fee_rate if minimum is not None and kind not in {"invite", "deposit"} else None
        gross = per_candy * price if per_candy is not None and price is not None else None
        pools.append({
            "kind": kind, "title": title, "pool_tokens": amount,
            "individual_cap_tokens": number(cap_match.group(1)) if cap_match else None,
            "fixed_reward_tokens": fixed, "fixed_reward_usdt": fixed * price if fixed is not None and price is not None else None,
            "minimum_usdt": minimum, "thresholds_usdt": threshold_values,
            "tier_schedule": tier_schedule,
            "candy_rewards": candy_values, "tokens_per_candy": per_candy,
            "gross_usdt_per_candy": gross, "estimated_fee_usdt": fees,
            "net_usdt_per_candy": gross - fees if gross is not None and fees is not None else gross,
            "spots_left": left, "spots_total": total,
        })
    return pools


def enrich_reward_pools(pools: list[dict[str, Any]], detail: dict[str, Any], price: float | None, fee_rate: float) -> None:
    """Add exact tier rewards carried in Next.js data but sometimes hidden in UI."""
    structured = detail.get("prize_pool_list") if isinstance(detail, dict) else None
    if not isinstance(structured, list):
        return
    used: set[int] = set()
    for pool in pools:
        candidate = None
        for index, item in enumerate(structured):
            if index in used or not isinstance(item, dict):
                continue
            if number(item.get("prize_all")) == pool.get("pool_tokens"):
                candidate = item; used.add(index); break
        if not candidate:
            continue
        if pool.get("kind") == "first_spot":
            continue
        prize_rule = str(candidate.get("prize_rule") or "")
        max_match = re.search(r"up to\s+(\d+)\s+cand(?:y|ies)", prize_rule, re.I)
        if max_match:
            pool["max_candies"] = int(max_match.group(1))
        rule_rows: list[dict[str, Any]] = []
        for task in candidate.get("task_list") or []:
            for rule in task.get("rule_info") or []:
                candies = number(rule.get("rewards"))
                threshold = number(rule.get("min"))
                reward_tokens = number(rule.get("currency_by_candy"))
                minimum_unit = str(rule.get("currency") or "USDT")
                if candies is None or threshold is None:
                    continue
                reward_usdt = reward_tokens * price if reward_tokens is not None and price is not None else None
                fees = threshold * fee_rate if minimum_unit.upper() == "USDT" and pool.get("kind") not in {"invite", "deposit"} else 0.0
                rule_rows.append({
                    "minimum_usdt": threshold, "minimum_unit": minimum_unit, "candies": candies,
                    "reward_tokens": reward_tokens,
                    "tokens_per_candy": reward_tokens / candies if reward_tokens is not None and candies else None,
                    "reward_usdt": reward_usdt, "estimated_fee_usdt": fees,
                    "net_usdt": reward_usdt - fees if reward_usdt is not None else None,
                })
        if len(rule_rows) > 1 and pool.get("kind") in {"spot", "vip", "futures", "convert", "trading_bot"}:
            rule_rows.sort(key=lambda row: row["minimum_usdt"])
            cumulative_candies = 0.0
            cumulative_tokens = 0.0
            for row in rule_rows:
                row["tier_candies"] = row["candies"]
                row["tier_reward_tokens"] = row["reward_tokens"]
                cumulative_candies += row["candies"]
                cumulative_tokens += row["reward_tokens"] or 0.0
                row["candies"] = cumulative_candies
                row["reward_tokens"] = cumulative_tokens
                row["tokens_per_candy"] = cumulative_tokens / cumulative_candies if cumulative_candies else None
                row["reward_usdt"] = cumulative_tokens * price if price is not None else None
                row["net_usdt"] = row["reward_usdt"] - row["estimated_fee_usdt"] if row["reward_usdt"] is not None else None
        if pool.get("kind") == "invite" and len(rule_rows) == 1 and pool.get("max_candies", 0) > 1:
            base = rule_rows[0]
            rule_rows = []
            for count in range(1, int(pool["max_candies"]) + 1):
                reward_tokens = (base.get("reward_tokens") or 0.0) * count
                rule_rows.append({
                    **base, "minimum_usdt": float(count), "minimum_unit": "referral(s)",
                    "candies": float(count), "tier_candies": 1.0,
                    "reward_tokens": reward_tokens,
                    "tokens_per_candy": base.get("tokens_per_candy"),
                    "reward_usdt": reward_tokens * price if price is not None else None,
                    "estimated_fee_usdt": 0.0,
                    "net_usdt": reward_tokens * price if price is not None else None,
                })
        individual_cap = number(pool.get("individual_cap_tokens"))
        for row in rule_rows:
            raw_tokens = row.get("reward_tokens")
            if raw_tokens is None:
                continue
            row["uncapped_reward_tokens"] = raw_tokens
            payable_tokens = min(raw_tokens, individual_cap) if individual_cap is not None else raw_tokens
            row["reward_tokens"] = payable_tokens
            row["cap_applied"] = individual_cap is not None and raw_tokens > individual_cap
            row["reward_usdt"] = payable_tokens * price if price is not None else None
            row["net_usdt"] = row["reward_usdt"] - row["estimated_fee_usdt"] if row["reward_usdt"] is not None else None
        if rule_rows:
            pool["tier_schedule"] = rule_rows
            if individual_cap is not None:
                pool["maximum_user_reward_tokens"] = individual_cap
                pool["maximum_user_reward_usdt"] = individual_cap * price if price is not None else None
            first = rule_rows[0]
            if first.get("tokens_per_candy"):
                pool["tokens_per_candy"] = first["tokens_per_candy"]
                pool["gross_usdt_per_candy"] = (
                    first["tokens_per_candy"] * price if price is not None else None
                )
                pool["net_usdt_per_candy"] = (
                    first["net_usdt"] / first["candies"] if first.get("net_usdt") is not None and first.get("candies") else None
                )


def mobile_text(url: str, timeout_ms: int = 30000) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium") from exc
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-minimized"],
            viewport={"width": 390, "height": 844}, locale="en-US",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        if "Access Denied" in page.locator("body").inner_text(timeout=timeout_ms):
            context.close()
            raise RuntimeError("Gate rejected the browser session. Close Chrome and scan again to create a new session.")
        text = page.locator("body").inner_text(timeout=timeout_ms)
        context.close()
        return text


def website_response(url: str, params: dict[str, Any] | None = None, timeout: int = 30):
    """Request Gate with a real Chrome TLS fingerprint (Akamai rejects requests)."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError as exc:
        raise RuntimeError("curl_cffi is not installed. Run: pip install -r requirements.txt") from exc
    response = curl_requests.get(
        url, params=params, impersonate="chrome136", timeout=timeout,
        headers={"Accept": "application/json,text/html", "Referer": "https://www.gate.com/candy-drop"},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Gate website HTTP {response.status_code}: {response.text[:300]}")
    return response


def detail_page_requests(url: str) -> tuple[str, dict[str, Any]]:
    """Fetch rendered text plus structured Next.js event data without a browser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise RuntimeError("beautifulsoup4 is not installed. Run: pip install -r requirements.txt") from exc
    raw = website_response(url).text
    soup = BeautifulSoup(raw, "html.parser")
    text = soup.get_text("\n", strip=True)
    detail_data: dict[str, Any] = {}
    next_script = soup.find("script", id="__NEXT_DATA__")
    if next_script and next_script.string:
        try:
            next_payload = json.loads(next_script.string)
            detail_data = next_payload["props"]["pageProps"]["detailData"]["data"]
        except (ValueError, KeyError, TypeError):
            detail_data = {}
    slot_pairs = re.findall(r'"fixed_pool_total"\s*:\s*(\d+)\s*,\s*"fixed_pool_left"\s*:\s*(\d+)', raw)
    if slot_pairs:
        # The first fixed pool corresponds to the first-trade section.
        total, left = slot_pairs[0]
        text += f"\nFirst {total} get rewards. Only {left} spot(s) left!"
    return text, detail_data


def detail_text_requests(url: str) -> str:
    return detail_page_requests(url)[0]


def discover_activities_requests() -> list[dict[str, Any]]:
    """Read only currently ongoing events from Gate's website API."""
    rows = discover_activity_states_requests()
    return [row for row in rows if row["activity_status"] == "ongoing"]


def discover_activity_states_requests() -> list[dict[str, Any]]:
    """Read recent events including ended ones and their distribution status."""
    payload = website_response(
        "https://www.gate.com/apiw/v2/launch/candydrop/activity-list",
        {"page": 1, "pageSize": 30, "sub_website_id": 0},
    ).json()
    try:
        rows = payload["data"]["list"]["list"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Gate activity-list format: {str(payload)[:500]}") from exc
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        event_id = int(row.get("id") or 0)
        if not event_id or event_id in seen:
            continue
        seen.add(event_id)
        currency = str(row.get("currency") or "").upper()
        result.append({
            "activity_id": "", "currency": currency,
            "status": str(row.get("activity_status") or "").lower(),
            "activity_status": str(row.get("activity_status") or "").lower(),
            "lottery_drawing_status": int(row.get("lottery_drawing_status") or 0),
            "title": currency, "url": f"https://www.gate.com/candy-drop/detail/{currency}-{event_id}",
            "slug": f"{currency}-{event_id}", "list_price": number(row.get("exchange_rate")),
            "participants": row.get("participants"), "rule_names": row.get("rule_name") or [],
        })
    return result


def distribution_state_from_detail(url: str) -> dict[str, Any]:
    """Read an ended event directly, even after it leaves Gate's recent list."""
    _, detail_data = detail_page_requests(url)
    info = detail_data.get("activity_info") or {}
    currency = str(info.get("currency") or "").upper()
    return {
        "url": url,
        "currency": currency,
        "activity_status": "ended" if int(info.get("status") or 0) == 3 else "unknown",
        "lottery_drawing_status": int(info.get("lottery_drawing_status") or 0),
    }


def discover_activities_mobile(timeout_ms: int = 45000) -> list[dict[str, Any]]:
    """Discover current CandyDrop cards from Gate's public mobile website."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install chromium") from exc
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), channel="chrome", headless=False,
            args=["--disable-blink-features=AutomationControlled", "--start-minimized"],
            viewport={"width": 1280, "height": 900}, locale="en-US",
        )
        context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = context.new_page()
        page.goto("https://www.gate.com/candy-drop", wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(5000)
        body_text = page.locator("body").inner_text(timeout=timeout_ms)
        if "Access Denied" in body_text:
            context.close()
            raise RuntimeError("Gate returned Access Denied. Close all Chrome windows and scan again.")
        # The list page is the source of truth: select Ongoing so ended/upcoming
        # events never enter the scan.
        try:
            ongoing = page.get_by_text(re.compile(r"^Ongoing(?:\s*\(\d+\))?$", re.I)).first
            ongoing.click(timeout=8000)
            page.wait_for_timeout(2500)
        except Exception:
            # On some layouts Ongoing is already the selected default tab.
            pass
        for _ in range(4):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(700)
        links = page.locator('a[href*="/candy-drop/detail/"]:visible')
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index in range(links.count()):
            node = links.nth(index)
            href = node.get_attribute("href") or ""
            if not href or href in seen:
                continue
            seen.add(href)
            url = urllib.parse.urljoin("https://www.gate.com", href)
            slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
            currency = slug.split("-")[0].upper() if slug else ""
            try:
                title = node.inner_text(timeout=1500).strip()
            except Exception:
                title = ""
            try:
                card_text = node.evaluate("""el => {
                    let p = el;
                    for (let i = 0; i < 7 && p; i++, p = p.parentElement) {
                        const t = (p.innerText || '').trim();
                        if (t.includes('USDT') && t.includes('Join')) return t;
                    }
                    return '';
                }""")
            except Exception:
                card_text = ""
            token_total = None
            usd_total = None
            token_match = re.search(rf"{NUM}\s+{re.escape(currency)}\b", card_text, re.I)
            usd_match = re.search(rf"[≈~]\s*{NUM}\s*USDT", card_text, re.I)
            if token_match: token_total = number(token_match.group(1))
            if usd_match: usd_total = number(usd_match.group(1))
            list_price = usd_total / token_total if token_total and usd_total is not None else None
            display_title = title if title and title.lower() not in {"join", "join now", "participate"} else f"CandyDrop {currency}"
            items.append({"activity_id": "", "currency": currency, "status": "ongoing",
                          "title": display_title, "url": url, "slug": slug,
                          "list_price": list_price})
        context.close()
        if not items:
            raise RuntimeError("No CandyDrop cards were found on Gate's public page; the layout may have changed.")
        return items


def ticker_price(currency: str) -> float | None:
    if currency.upper() in {"USDT", "USDC", "USD"}:
        return 1.0
    try:
        payload = http_json("/spot/tickers", {"currency_pair": f"{currency.upper()}_USDT"})
        item = payload[0] if isinstance(payload, list) and payload else payload
        return number(item.get("last")) if isinstance(item, dict) else None
    except Exception:
        return None


def build_report(item: dict[str, Any], use_browser: bool, fee_rate: float) -> Report:
    activity_id = str(first_key(item, {"activity_id", "id", "activityId"}) or "")
    currency = str(first_key(item, {"currency", "coin", "reward_currency", "rewardCoin"}) or "").upper()
    status = str(first_key(item, {"status", "activity_status"}) or "")
    title = str(first_key(item, {"title", "name", "activity_name"}) or "")
    slug = str(first_key(item, {"slug", "activity_code", "activityCode"}) or "")
    url = str(first_key(item, {"url", "detail_url", "jump_url"}) or "")
    if url.startswith("/"):
        url = "https://www.gate.com" + url
    if not url and slug:
        url = f"https://www.gate.com/candy-drop/detail/{slug}"
    rules: Any = {}
    # Web-discovered records deliberately avoid the authenticated CandyDrop API.
    if status not in {"web", "direct", "ongoing"}:
        try:
            rules = http_json("/launch/candydrop/activity-rules", {"activity_id": activity_id or None, "currency": None if activity_id else currency})
        except Exception as exc:
            rules = {"api_error": str(exc)}
    combined = json.dumps(rules, ensure_ascii=False)
    structured_detail: dict[str, Any] = {}
    if url:
        try:
            detail_text, structured_detail = detail_page_requests(url)
            combined += "\n" + detail_text
        except Exception as exc:
            if use_browser:
                try:
                    combined += "\n" + mobile_text(url)
                except Exception as browser_exc:
                    combined += f"\n request_error={exc}\n browser_error={browser_exc}"
            else:
                combined += f"\n request_error={exc}"
    parsed = parse_rule_text(combined, currency) if currency else {}
    pool = number(first_key(rules, {"prize_pool", "reward_pool", "pool_amount", "total_reward"})) or parsed.get("pool")
    total_candies = number(first_key(rules, {"total_candies", "candy_total", "total_candy"}))
    estimated = number(first_key(rules, {"estimated_reward", "reward_per_candy", "per_candy_reward"})) or parsed.get("estimated")
    if estimated is None and pool is not None and total_candies:
        estimated = pool / total_candies
    fixed = number(first_key(rules, {"fixed_reward", "fixed_amount"})) or parsed.get("fixed")
    minimum = number(first_key(rules, {"min_trade_amount", "trade_threshold", "min_volume"})) or parsed.get("minimum")
    left, total = parse_slots(combined)
    price = number(item.get("list_price")) or (ticker_price(currency) if currency else None)
    reward_pools = parse_reward_pools(combined, currency, price, fee_rate) if currency else []
    enrich_reward_pools(reward_pools, structured_detail, price, fee_rate)
    first_pool = next((x for x in reward_pools if x["kind"] == "first_spot"), None)
    candy_pool = next((x for x in reward_pools if x.get("tokens_per_candy") is not None), None)
    if first_pool:
        if first_pool.get("spots_left") is None and left is not None:
            first_pool["spots_left"] = left
            first_pool["spots_total"] = total
        fixed = first_pool.get("fixed_reward_tokens") or fixed
        minimum = first_pool.get("minimum_usdt") or minimum
        left = first_pool.get("spots_left") if first_pool.get("spots_left") is not None else left
        total = first_pool.get("spots_total") or total
    if candy_pool:
        estimated = candy_pool.get("tokens_per_candy") or estimated
    gross = estimated * price if estimated is not None and price is not None else None
    # Gate defines task volume as buy + sell, so the threshold already includes
    # both charged legs; multiplying by two again would double-count fees.
    fees = minimum * fee_rate if minimum is not None else None
    net = gross - fees if gross is not None and fees is not None else gross
    return Report(
        activity_id=activity_id, currency=currency, status=status, url=url,
        pool_tokens=pool, total_candies=total_candies, token_per_candy=estimated,
        token_price_usdt=price, gross_usdt_per_candy=gross,
        estimated_fee_usdt=fees, net_usdt_per_candy=net,
        fixed_reward_tokens=fixed,
        fixed_reward_usdt=fixed * price if fixed is not None and price is not None else None,
        spots_left=left, spots_total=total, first_trade_min_usdt=minimum,
        raw_title=title, checked_at=datetime.now(timezone.utc).isoformat(), reward_pools=reward_pools,
        fee_rate=fee_rate,
    )


def fetch_activities(statuses: list[str]) -> list[dict[str, Any]]:
    """Fetch activities while tolerating Gate deployments that reject status.

    Some Gate regions currently return HTTP 400 for the documented lowercase
    status values. Prefer one unfiltered request and filter its latest results
    locally; fall back to the documented filtered calls on older deployments.
    """
    wanted = {x.strip().lower() for x in statuses if x.strip()}
    last_error: Exception | None = None

    # Newer/region-specific deployments: status omitted.
    for params in ({"limit": 30, "offset": 0}, {}):
        try:
            page = activity_items(http_json("/launch/candydrop/activity-list", params))
            if page:
                filtered: list[dict[str, Any]] = []
                for item in page:
                    raw_status = str(first_key(item, {"status", "activity_status"}) or "").lower()
                    # Keep unknown statuses: Gate occasionally returns numeric or localized values.
                    if not wanted or not raw_status or raw_status in wanted:
                        filtered.append(item)
                return filtered or page
        except Exception as exc:
            last_error = exc

    # Older deployments: documented status filter. Try lowercase then uppercase.
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for status in statuses:
        for status_value in (status.lower(), status.upper()):
            try:
                page = activity_items(http_json("/launch/candydrop/activity-list", {
                    "status": status_value, "limit": 30, "offset": 0,
                }))
            except Exception as exc:
                last_error = exc
                continue
            for item in page:
                identity = str(first_key(item, {"activity_id", "id", "activityId"}) or json.dumps(item, sort_keys=True))
                if identity not in seen:
                    seen.add(identity); result.append(item)
            if page:
                break
    if not result and last_error:
        raise RuntimeError(f"Could not fetch the CandyDrop list after trying fallback formats: {last_error}")
    return result


def save(reports: list[Report], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [asdict(x) for x in reports]
    (out_dir / "latest.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    if rows:
        with (out_dir / "latest.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


def print_reports(reports: list[Report]) -> None:
    print(f"\n{'Coin':<10} {'Candy':>13} {'$/Candy':>12} {'Net $':>12} {'Fixed $':>12} {'Spots':>12}")
    print("-" * 76)
    for r in reports:
        fmt = lambda x: "-" if x is None else f"{x:,.6g}"
        spots = "-" if r.spots_left is None else f"{r.spots_left:,}" + (f"/{r.spots_total:,}" if r.spots_total else "")
        print(f"{r.currency:<10} {fmt(r.token_per_candy):>13} {fmt(r.gross_usdt_per_candy):>12} {fmt(r.net_usdt_per_candy):>12} {fmt(r.fixed_reward_usdt):>12} {spots:>12}")



import asyncio
import html
import json
import logging
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
STATE_FILE = BASE / "telegram_state.json"
DATA_DIR = BASE / "gate-monitor-data"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USERS = {
    int(x) for x in os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",") if x.strip().isdigit()
}
DEFAULT_INTERVAL = max(30, int(os.getenv("GATE_SCAN_INTERVAL", "60")))
DEFAULT_FEE = float(os.getenv("GATE_FEE_RATE", "0.0015"))
USE_BROWSER = os.getenv("GATE_USE_BROWSER", "1").lower() not in {"0", "false", "no"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gate-candydrop-bot")
SCAN_LOCK = asyncio.Lock()


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("chats", {})
    data.setdefault("events", {})
    return data


def write_state(state: dict[str, Any]) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not ALLOWED_USERS or user.id in ALLOWED_USERS))


def keyboard(monitoring: bool = True) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Scan Now", callback_data="scan"),
         InlineKeyboardButton("📋 Events", callback_data="events")],
        [InlineKeyboardButton("⏱ Status", callback_data="status"),
         InlineKeyboardButton("⏸ Pause Alerts" if monitoring else "▶️ Enable Alerts",
                              callback_data="stop" if monitoring else "start")],
        [InlineKeyboardButton("♻️ Refresh", callback_data="refresh")],
    ])


def fmt(value: float | None, digits: int = 6) -> str:
    if value is None:
        return "N/A"
    if digits == 0:
        return f"{value:,.0f}"
    return f"{value:,.{digits}f}".rstrip("0").rstrip(".")


def event_text(r: Report, compact: bool = False, pool_kind: str | None = None) -> str:
    coin = html.escape(r.currency or "Unknown")
    title = html.escape(r.raw_title or f"CandyDrop {coin}")
    lines = [
        f"🍬 <b>{title}</b>",
        f"🧾 Fee Rate Used: <b>{r.fee_rate * 100:g}%</b> of counted trading volume",
    ]
    if compact:
        return "\n".join(lines)
    kind_names = {
        "first_spot": "First Spot Trade", "spot": "Spot Trading Distribution",
        "invite": "Referral / Invite Friends", "futures": "Futures Trading",
        "deposit": "Deposit", "convert": "Convert",
        "trading_bot": "Trading Bot", "earn": "Simple Earn", "vip": "VIP Spot",
        "other": "Other Reward",
    }
    visible_pools = [
        pool for pool in (r.reward_pools or [])
        if pool_kind is None or pool.get("kind") == pool_kind
    ]
    for index, pool in enumerate(visible_pools, 1):
        lines += [
            "",
            "━━━━━━━━━━━━━━━━━━",
            f"<blockquote><b>{index}) {kind_names.get(pool.get('kind'), 'Reward')}</b></blockquote>",
            "",
        ]

        details = [f"🏦 <b>Prize Pool</b>: {fmt(pool.get('pool_tokens'))} {coin}"]
        if pool.get("maximum_user_reward_usdt") is not None:
            details.append(
                f"💰 <b>Maximum per User</b>\n"
                f"{fmt(pool.get('maximum_user_reward_tokens'))} {coin} ≈ "
                f"{fmt(pool.get('maximum_user_reward_usdt'), 4)} USDT"
            )
        if pool.get("fixed_reward_tokens") is not None:
            details.append(
                f"🎁 <b>Fixed Reward</b>\n"
                f"{fmt(pool.get('fixed_reward_tokens'))} {coin} ≈ "
                f"{fmt(pool.get('fixed_reward_usdt'), 4)} USDT"
            )
        if pool.get("minimum_usdt") is not None:
            details.append(f"📊 <b>Minimum Volume</b>: {fmt(pool.get('minimum_usdt'), 2)} USDT")
        if pool.get("max_candies") is not None:
            details.append(f"🍬 <b>Maximum Candies</b>: {fmt(pool.get('max_candies'), 0)}")
        if pool.get("tokens_per_candy") is not None:
            details.append(
                f"🍭 <b>Per Candy</b>\n"
                f"{fmt(pool.get('tokens_per_candy'))} {coin} ≈ "
                f"{fmt(pool.get('gross_usdt_per_candy'), 4)} USDT"
            )
        if pool.get("spots_left") is not None:
            details.append(f"🎟 <b>Spots Left</b>\n{pool['spots_left']:,}")
        lines.append("\n\n".join(details))

        calculations = []
        if pool.get("fixed_reward_usdt") is not None:
            fees = pool.get("estimated_fee_usdt") or 0.0
            net = pool["fixed_reward_usdt"] - fees
            calculations.append(
                f"<b>Volume:</b> {fmt(pool.get('minimum_usdt'), 0)} USDT | "
                f"<b>Fees:</b> {fmt(fees, 4)} USDT | "
                f"<b>Net:</b> {fmt(net, 4)} USDT"
            )
        tiers = pool.get("tier_schedule") or []
        if pool.get("kind") == "invite" and tiers:
            grouped_tiers = []
            for tier in tiers:
                requirement = int(tier.get("minimum_usdt") or 0)
                net_value = tier.get("net_usdt")
                if (
                    grouped_tiers
                    and round(grouped_tiers[-1].get("net_usdt") or 0, 8) == round(net_value or 0, 8)
                    and requirement == grouped_tiers[-1]["range_end"] + 1
                ):
                    grouped_tiers[-1]["range_end"] = requirement
                else:
                    grouped_tiers.append({**tier, "range_start": requirement, "range_end": requirement})
            tiers = grouped_tiers
        for tier in tiers:
            requirement = tier.get("minimum_usdt")
            if not requirement:
                continue
            unit = html.escape(str(tier.get("minimum_unit") or "USDT"))
            if pool.get("kind") == "invite":
                range_start = tier.get("range_start", int(requirement))
                range_end = tier.get("range_end", int(requirement))
                count_label = str(range_start) if range_start == range_end else f"{range_start} ~ {range_end}"
                referral_word = "referral" if range_start == range_end == 1 else "referrals"
                calculations.append(
                    f"<b>{count_label} {referral_word}</b> → "
                    f"Net <b>{fmt(tier.get('net_usdt'), 4)} USDT</b>"
                )
            else:
                calculations.append(
                    f"<b>Volume:</b> {fmt(requirement, 0)} {unit} | "
                    f"<b>Candies:</b> {fmt(tier.get('candies'), 0)} | "
                    f"<b>Fees:</b> {fmt(tier.get('estimated_fee_usdt'), 4)} USDT | "
                    f"<b>Net:</b> {fmt(tier.get('net_usdt'), 4)} USDT"
                )
        if calculations:
            lines += [
                "",
                f"<blockquote><b>{'Profit' if pool.get('kind') == 'invite' else 'Profit Scenarios'}</b></blockquote>",
                "\n\n".join(f"• {calculation}" for calculation in calculations),
            ]
    return "\n".join(lines)


def event_keyboard(r: Report) -> InlineKeyboardMarkup | None:
    if not r.url:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open Event on Gate", url=r.url)]])


async def scan() -> list[Report]:
    def blocking() -> list[Report]:
        state = load_state()
        fee = float(state.get("fee_rate", DEFAULT_FEE))
        # Gate's website API is the source; curl_cffi supplies Chrome's TLS
        # fingerprint without opening a browser. Browser remains fallback only.
        try:
            items = discover_activities_requests()
        except Exception:
            if not USE_BROWSER:
                raise
            log.exception("HTTP discovery failed; using browser fallback")
            items = discover_activities_mobile()
        reports = [build_report(x, USE_BROWSER, fee) for x in items]
        save(reports, DATA_DIR)
        return reports
    async with SCAN_LOCK:
        return await asyncio.to_thread(blocking)


async def send_reports(chat_id: int, reports: list[Report], context: ContextTypes.DEFAULT_TYPE) -> None:
    if not reports:
        await context.bot.send_message(chat_id, "No ongoing events are available right now.", reply_markup=keyboard())
        return
    await context.bot.send_message(chat_id, f"Found <b>{len(reports)}</b> ongoing event(s):", parse_mode=ParseMode.HTML)
    for report in reports[:20]:
        await context.bot.send_message(
            chat_id, event_text(report), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=event_keyboard(report),
        )
    if len(reports) > 20:
        await context.bot.send_message(chat_id, f"{len(reports) - 20} additional events were hidden.")
    await context.bot.send_message(chat_id, "Control Panel:", reply_markup=keyboard())


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await update.effective_message.reply_text("This is a private bot.")
        return
    state = load_state(); chat_id = str(update.effective_chat.id)
    state["chats"].setdefault(chat_id, {"enabled": True})
    write_state(state)
    text = (
        "🤖 <b>Gate CandyDrop Monitor</b>\n\n"
        "I monitor new events, first-trade rewards, Candy values, and remaining spots.\n"
        "Calculations are estimates. The bot never registers or trades automatically."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard(True))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    await update.effective_message.reply_text(
        "/start Control panel\n/scan Scan now\n/events Show saved events\n"
        "/check URL Inspect one event URL\n"
        "/status Monitor status\n/interval 5m Change notification interval\n"
        "/fee 0.0015 Set the trading fee rate (0.15%)\n"
        "/stop Pause alerts\n/resume Enable alerts"
    )


async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    msg = await update.effective_message.reply_text("⏳ Scanning Gate...")
    try:
        reports = await scan()
        await msg.edit_text("✅ Scan completed.")
        await send_reports(update.effective_chat.id, reports, context)
    except Exception as exc:
        log.exception("scan failed")
        await msg.edit_text(f"❌ Scan failed: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)


async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Inspect one Gate CandyDrop detail URL without using the activity API."""
    if not allowed(update): return
    if not context.args:
        await update.effective_message.reply_text(
            "Send the event URL after the command, for example:\n"
            "/check https://www.gate.com/candy-drop/detail/AEON-351"
        )
        return
    url = context.args[0].strip()
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"gate.com", "www.gate.com"} or "/candy-drop/detail/" not in parsed.path:
        await update.effective_message.reply_text("The URL must be an official gate.com CandyDrop detail URL.")
        return
    slug = parsed.path.rstrip("/").split("/")[-1]
    currency = slug.split("-")[0].upper()
    msg = await update.effective_message.reply_text("⏳ Inspecting the event page...")
    try:
        state = load_state(); fee = float(state.get("fee_rate", DEFAULT_FEE))
        item = {"activity_id": "", "currency": currency, "status": "direct",
                "title": f"CandyDrop {currency}", "url": url, "slug": slug}
        report = await asyncio.to_thread(build_report, item, True, fee)
        await msg.edit_text("✅ Event inspection completed.")
        await context.bot.send_message(
            update.effective_chat.id, event_text(report), parse_mode=ParseMode.HTML,
            disable_web_page_preview=True, reply_markup=event_keyboard(report),
        )
    except Exception as exc:
        log.exception("direct check failed")
        await msg.edit_text(f"❌ Event inspection failed: {html.escape(str(exc))}", parse_mode=ParseMode.HTML)


async def events_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    try:
        rows = json.loads((DATA_DIR / "latest.json").read_text(encoding="utf-8"))
        reports = [Report(**{k: v for k, v in row.items() if k in Report.__dataclass_fields__}) for row in rows]
    except (OSError, ValueError, TypeError):
        return await run_scan(update, context)
    await send_reports(update.effective_chat.id, reports, context)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    state = load_state(); cfg = state["chats"].get(str(update.effective_chat.id), {"enabled": True})
    enabled = bool(cfg.get("enabled", True))
    interval = int(state.get("scan_interval", DEFAULT_INTERVAL))
    await update.effective_message.reply_text(
        f"Status: {'🟢 Running' if enabled else '🔴 Paused'}\n"
        f"Scan interval: every {format_interval(interval)}\n"
        f"Browser fallback: {'Enabled' if USE_BROWSER else 'Disabled'}\n"
        f"Trading fee rate: {float(state.get('fee_rate', DEFAULT_FEE)) * 100:g}% of counted volume",
        reply_markup=keyboard(enabled),
    )


async def toggle(update: Update, enabled: bool) -> None:
    if not allowed(update): return
    state = load_state(); state["chats"][str(update.effective_chat.id)] = {"enabled": enabled}; write_state(state)
    await update.effective_message.reply_text(
        "Alerts enabled ✅" if enabled else "Alerts paused ⏸",
        reply_markup=keyboard(enabled),
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle(update, False)


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await toggle(update, True)


def parse_interval(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", value.lower())
    if not match:
        raise ValueError
    amount = int(match.group(1))
    multiplier = {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    seconds = amount * multiplier
    if not 30 <= seconds <= 86400:
        raise ValueError
    return seconds


def format_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def schedule_monitor(application: Application, interval: int, first: int = 1) -> None:
    for job in application.job_queue.get_jobs_by_name("gate_monitor"):
        job.schedule_removal()
    application.job_queue.run_repeating(
        monitor_job, interval=interval, first=first, name="gate_monitor",
    )


async def interval_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    if not context.args:
        await update.effective_message.reply_text(
            "Examples: /interval 30s, /interval 5m, /interval 1h\n"
            "Allowed range: 30 seconds to 24 hours."
        )
        return
    try:
        interval = parse_interval(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(
            "Invalid interval. Use 30s–86400s, 1m–1440m, or 1h–24h."
        )
        return
    state = load_state(); state["scan_interval"] = interval; write_state(state)
    schedule_monitor(context.application, interval)
    await update.effective_message.reply_text(
        f"Notification interval changed to every {format_interval(interval)} ✅"
    )


async def fee_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    if not context.args:
        await update.effective_message.reply_text("Example: /fee 0.001  (0.1% of counted trading volume)")
        return
    try:
        fee = float(context.args[0])
        if not 0 <= fee <= 0.02: raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a number between 0 and 0.02.")
        return
    state = load_state(); state["fee_rate"] = fee; write_state(state)
    await update.effective_message.reply_text(f"Trading fee rate set to {fee * 100:g}% ✅")


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update): return
    query = update.callback_query; await query.answer()
    action = query.data
    if action in {"scan", "refresh"}: await run_scan(update, context)
    elif action == "events": await events_cmd(update, context)
    elif action == "status": await status_cmd(update, context)
    elif action == "stop": await toggle(update, False)
    elif action == "start": await toggle(update, True)


def fingerprint(r: Report) -> dict[str, Any]:
    return {"spots_left": r.spots_left, "fixed_reward_tokens": r.fixed_reward_tokens,
            "token_per_candy": r.token_per_candy, "status": r.status,
            "currency": r.currency, "url": r.url}


def event_state_key(report: Report) -> str:
    """Use the permanent Gate detail URL; API ids can be absent or change shape."""
    if report.url:
        return report.url.rstrip("/")
    if report.activity_id:
        return f"activity:{report.activity_id}"
    return f"currency:{report.currency}"


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        reports = await scan()
        recent_states = await asyncio.to_thread(discover_activity_states_requests)
        state = load_state(); previous = state.get("events", {})
        current: dict[str, Any] = dict(previous)
        changes: list[tuple[str, Report, str | None]] = []
        distribution_changes: list[dict[str, Any]] = []
        for report in reports:
            key = event_state_key(report)
            old = previous.get(key)
            current[key] = {**(old or {}), **fingerprint(report)}
            if old is None:
                changes.append(("🆕 New Event", report, None))
            elif (
                old.get("spots_left") is not None
                and report.spots_left is not None
                and old.get("spots_left") != report.spots_left
            ):
                changes.append((f"🎟 Spots Changed: {old.get('spots_left')} → {report.spots_left}", report, "first_spot"))
            elif old.get("fixed_reward_tokens") != report.fixed_reward_tokens:
                changes.append(("🎁 First Spot Reward Changed", report, "first_spot"))

        # Gate status mapping verified against ended events:
        # lottery_drawing_status 1 = awaiting distribution, 2 = distributed.
        # Keep probing previously-ended pending events directly: after enough new
        # events arrive, Gate can remove an old event from its first API page.
        tracked: dict[str, dict[str, Any]] = {event["url"]: event for event in recent_states}
        for key, old in previous.items():
            if (
                isinstance(key, str)
                and key.startswith("https://www.gate.com/candy-drop/detail/")
                and old.get("status") == "ended"
                and int(old.get("lottery_drawing_status") or 0) != 2
                and key not in tracked
            ):
                tracked[key] = {
                    "url": key, "currency": old.get("currency", ""),
                    "activity_status": "ended",
                    "lottery_drawing_status": int(old.get("lottery_drawing_status") or 0),
                    "needs_detail_check": True,
                }

        for key, event in tracked.items():
            old = previous.get(key) or {}
            # Preserve the detailed scan fingerprint (especially spots_left).
            # The activity-list response does not include those values.
            stored = current.get(key) or old
            if event.get("needs_detail_check"):
                event = await asyncio.to_thread(distribution_state_from_detail, key)
            drawing_status = int(event.get("lottery_drawing_status") or 0)
            current[key] = {
                **stored,
                "currency": event.get("currency") or old.get("currency", ""), "url": key,
                "status": event.get("activity_status", "unknown"),
                "lottery_drawing_status": drawing_status,
            }
            # A recorded pending event changing 1 -> 2 is the normal case.  The
            # second condition handles an update/restart that last saw the event
            # as ongoing, so a just-completed distribution is not missed.
            was_pending = int(old.get("lottery_drawing_status") or 0) == 1
            was_active = old.get("status") in {"ongoing", "active"}
            if (
                event.get("activity_status") == "ended"
                and drawing_status == 2
                and not old.get("distribution_notified", False)
                and (was_pending or was_active)
            ):
                current[key]["distribution_notified"] = True
                distribution_changes.append(current[key])
        # Persist detection state *before* Telegram delivery. A blocked/deleted
        # chat must not make successful chats receive the same event forever.
        state["events"] = current
        write_state(state)
        for chat_id, cfg in state.get("chats", {}).items():
            if not cfg.get("enabled", True): continue
            for heading, report, pool_kind in changes:
                try:
                    await context.bot.send_message(
                        int(chat_id), f"<b>{heading}</b>\n\n{event_text(report, pool_kind=pool_kind)}",
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                        reply_markup=event_keyboard(report),
                    )
                except Exception:
                    log.exception("could not send event alert to chat %s", chat_id)
            for event in distribution_changes:
                try:
                    await context.bot.send_message(
                        int(chat_id),
                        f"<blockquote><b>💸 Rewards Distributed</b></blockquote>\n\n"
                        f"🍬 <b>{html.escape(event['currency'])}</b> CandyDrop rewards have been distributed.\n"
                        f"✅ Event status: <b>Ended</b>\n"
                        f"🎁 Distribution status: <b>Completed</b>",
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔗 Open Event on Gate", url=event["url"])
                        ]]),
                    )
                except Exception:
                    log.exception("could not send distribution alert to chat %s", chat_id)
    except Exception:
        log.exception("background monitor failed")


async def configure_bot(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand("start", "Open the control panel"),
        BotCommand("scan", "Scan all ongoing events now"),
        BotCommand("events", "Show the latest saved events"),
        BotCommand("check", "Inspect one Gate event URL"),
        BotCommand("status", "Show monitoring settings"),
        BotCommand("interval", "Change the notification interval"),
        BotCommand("fee", "Set the trading fee rate"),
        BotCommand("stop", "Pause automatic alerts"),
        BotCommand("resume", "Enable automatic alerts"),
        BotCommand("help", "Show command help"),
    ])


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in the environment or .env file before starting the bot.")
    app = Application.builder().token(BOT_TOKEN).post_init(configure_bot).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("scan", run_scan))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("events", events_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("interval", interval_cmd))
    app.add_handler(CommandHandler("fee", fee_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("resume", resume_cmd))
    app.add_handler(CallbackQueryHandler(buttons))
    interval = int(load_state().get("scan_interval", DEFAULT_INTERVAL))
    schedule_monitor(app, interval, first=10)
    log.info("Bot started; interval=%s seconds", interval)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
