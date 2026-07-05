from __future__ import annotations

import csv
import difflib
import html
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
DOCS_DIR = ROOT / "docs"
DOCS_DIR.mkdir(exist_ok=True)

REPORT_JSON = DOCS_DIR / "report.json"
INDEX_HTML = DOCS_DIR / "index.html"
HISTORY_JSONL = DOCS_DIR / "history.jsonl"
LAST_RUN_TXT = DOCS_DIR / "last_run.txt"

GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/events/keyset"
BOOK_URL = "https://clob.polymarket.com/book"
ODDS_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
ODDS_ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/odds"

PAGE_SIZE = 500
REQUEST_PAUSE_SECONDS = 0.05

REGIONS = "eu"
MARKETS = "h2h"
ODDS_FORMAT = "decimal"
DATE_FORMAT = "iso"

MAX_MATCH_HOURS = 96
MIN_MATCH_SCORE = 0.76
MIN_BOOKMAKERS = 3
MAX_SPREAD = 0.10
MAX_ENTRY_PRICE = 0.90
MIN_ASK_SIZE = 5.0
MIN_NET_EDGE = 0.03
CONSERVATIVE_HAIRCUT = 0.015
DAYS_AHEAD = 14
HISTORY_MAX_LINES = 5000

TARGETS = [
    {
        "name": "Tennis",
        "tag_id": 864,
        "max_pages": 4,
        "odds_keywords": ["tennis"],
    },
    {
        "name": "WNBA",
        "tag_id": 100254,
        "max_pages": 3,
        "odds_keywords": ["basketball_wnba", "wnba"],
    },
    {
        "name": "UFC",
        "tag_id": 279,
        "max_pages": 3,
        "odds_keywords": ["mma", "ufc"],
    },
]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_text(value: Any) -> str:
    return "" if value is None else str(value)


def as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(number):
        return None

    return number


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    return []


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        result = datetime.fromisoformat(text)
    except ValueError:
        return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def normalize_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", safe_text(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(
        r"\b(fc|sc|club|team|gaming|esports|e sports|women|men)\b",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def similarity(a: str, b: str) -> float:
    na = normalize_name(a)
    nb = normalize_name(b)
    if not na or not nb:
        return 0.0

    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    tokens_a = set(na.split())
    tokens_b = set(nb.split())

    if not tokens_a or not tokens_b:
        return seq

    overlap = len(tokens_a & tokens_b) / max(len(tokens_a), len(tokens_b))
    return max(seq, overlap)


def request_json(
    session: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
) -> tuple[Any, dict[str, str]]:
    response = session.get(url, params=params, timeout=45)
    headers = {key.lower(): value for key, value in response.headers.items()}

    if response.status_code >= 400:
        try:
            body = response.json()
        except Exception:
            body = response.text[:500]

        safe_params = dict(params or {})
        if "apiKey" in safe_params:
            safe_params["apiKey"] = "***"

        raise RuntimeError(
            f"HTTP {response.status_code} bei {url} "
            f"mit Parametern {safe_params}. Antwort: {body}"
        )

    return response.json(), headers


def event_id(event: dict[str, Any]) -> str:
    return str(event.get("id") or event.get("slug") or "")


def market_id(market: dict[str, Any]) -> str:
    return str(
        market.get("id")
        or market.get("conditionId")
        or market.get("slug")
        or ""
    )


def event_title(event: dict[str, Any]) -> str:
    return safe_text(event.get("title") or event.get("slug") or event_id(event))


def get_kickoff(
    event: dict[str, Any],
    market: dict[str, Any],
) -> datetime | None:
    candidates = (
        market.get("gameStartTime"),
        market.get("eventStartTime"),
        event.get("gameStartTime"),
        event.get("eventStartTime"),
        market.get("endDate"),
        event.get("endDate"),
    )

    for value in candidates:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed

    return None


def is_match_winner_market(event: dict[str, Any], market: dict[str, Any]) -> bool:
    if str(market.get("sportsMarketType") or "").strip().lower() != "moneyline":
        return False

    if market.get("active") is False:
        return False
    if market.get("closed") is True:
        return False
    if market.get("archived") is True:
        return False
    if market.get("enableOrderBook") is False:
        return False
    if market.get("acceptingOrders") is False:
        return False

    if not market_id(market):
        return False

    outcomes = [safe_text(item).strip() for item in parse_json_list(market.get("outcomes"))]
    token_ids = [safe_text(item).strip() for item in parse_json_list(market.get("clobTokenIds"))]

    if len(outcomes) != 2 or len(token_ids) != 2:
        return False

    low_outcomes = [item.lower() for item in outcomes]
    if set(low_outcomes) == {"yes", "no"}:
        return False

    banned = [
        "over",
        "under",
        "draw",
        "completed match",
        "set ",
        "handicap",
        "spread",
        "total",
    ]

    if any(any(word in item.lower() for word in banned) for item in outcomes):
        return False

    question = safe_text(market.get("question")).lower()
    if any(word in question for word in ["completed match", "o/u", "total sets", "set 1", "set 2"]):
        return False

    kickoff = get_kickoff(event, market)
    if kickoff is None or kickoff <= datetime.now(timezone.utc):
        return False

    return True


def fetch_polymarket_events_for_target(
    session: requests.Session,
    target: dict[str, Any],
) -> list[dict[str, Any]]:
    tag_id = int(target["tag_id"])
    max_pages = int(target.get("max_pages") or 3)

    events: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for page in range(max_pages):
        params: dict[str, Any] = {
            "limit": PAGE_SIZE,
            "tag_id": tag_id,
            "related_tags": "true",
            "closed": "false",
            "order": "createdAt",
            "ascending": "false",
        }

        if cursor:
            params["after_cursor"] = cursor

        try:
            payload, _headers = request_json(session, GAMMA_KEYSET_URL, params=params)
        except Exception:
            params.pop("order", None)
            params.pop("ascending", None)
            payload, _headers = request_json(session, GAMMA_KEYSET_URL, params=params)

        if not isinstance(payload, dict):
            raise RuntimeError("Unerwartete Polymarket-Antwort.")

        page_events = [
            item for item in payload.get("events", [])
            if isinstance(item, dict)
        ]
        events.extend(page_events)

        print(f"   {target['name']} Seite {page + 1}: {len(page_events)} Events")

        next_cursor = str(payload.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_PAUSE_SECONDS)

    dedup: dict[str, dict[str, Any]] = {}
    for event in events:
        identifier = event_id(event)
        if identifier:
            dedup[identifier] = event

    return list(dedup.values())


def build_match_winner_selections(
    target: dict[str, Any],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []

    for event in events:
        markets = [
            market
            for market in (event.get("markets") or [])
            if isinstance(market, dict)
            and is_match_winner_market(event, market)
        ]

        if len(markets) != 1:
            continue

        market = markets[0]
        outcomes = [safe_text(item).strip() for item in parse_json_list(market.get("outcomes"))]
        token_ids = [safe_text(item).strip() for item in parse_json_list(market.get("clobTokenIds"))]
        kickoff = get_kickoff(event, market)

        if len(outcomes) != 2 or len(token_ids) != 2 or kickoff is None:
            continue

        for index, (outcome, token_id) in enumerate(zip(outcomes, token_ids)):
            opponent = outcomes[1 - index]
            selections.append({
                "sport_name": target["name"],
                "tag_id": target["tag_id"],
                "event_title": event_title(event),
                "kickoff_utc": kickoff.isoformat(),
                "selection": outcome,
                "opponent": opponent,
                "outcome_index": index,
                "token_id": token_id,
                "polymarket_event_id": event_id(event),
                "polymarket_market_id": market_id(market),
            })

    return selections


def best_level(
    levels: Any,
    side: str,
) -> tuple[float | None, float | None]:
    if not isinstance(levels, list):
        return None, None

    parsed: list[tuple[float, float]] = []
    for level in levels:
        if not isinstance(level, dict):
            continue

        price = as_float(level.get("price"))
        size = as_float(level.get("size"))

        if price is None or size is None:
            continue

        parsed.append((price, size))

    if not parsed:
        return None, None

    if side == "bid":
        return max(parsed, key=lambda item: item[0])

    return min(parsed, key=lambda item: item[0])


def fetch_orderbook(
    session: requests.Session,
    token_id: str,
) -> dict[str, Any]:
    try:
        payload, _headers = request_json(
            session,
            BOOK_URL,
            params={"token_id": token_id},
        )
    except Exception as exc:
        return {
            "token_id": token_id,
            "error": str(exc),
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
            "spread": None,
        }

    if not isinstance(payload, dict):
        return {
            "token_id": token_id,
            "error": "Unerwartetes Orderbuchformat",
            "best_bid": None,
            "best_bid_size": None,
            "best_ask": None,
            "best_ask_size": None,
            "spread": None,
        }

    bid, bid_size = best_level(payload.get("bids"), "bid")
    ask, ask_size = best_level(payload.get("asks"), "ask")
    spread = None
    if bid is not None and ask is not None:
        spread = ask - bid

    return {
        "token_id": token_id,
        "error": "",
        "best_bid": bid,
        "best_bid_size": bid_size,
        "best_ask": ask,
        "best_ask_size": ask_size,
        "spread": spread,
    }


def fetch_odds_sports(session: requests.Session, api_key: str) -> list[dict[str, Any]]:
    payload, _headers = request_json(
        session,
        ODDS_SPORTS_URL,
        params={"apiKey": api_key},
    )

    sports = payload if isinstance(payload, list) else []
    return [item for item in sports if isinstance(item, dict)]


def target_sport_keys(
    sports: list[dict[str, Any]],
    target: dict[str, Any],
) -> list[str]:
    keywords = [safe_text(item).lower() for item in target.get("odds_keywords", [])]
    keys: list[str] = []

    for sport in sports:
        key = safe_text(sport.get("key"))
        text = " ".join([
            key,
            safe_text(sport.get("group")),
            safe_text(sport.get("title")),
            safe_text(sport.get("description")),
        ]).lower()

        if any(keyword and keyword in text for keyword in keywords):
            if key:
                keys.append(key)

    return sorted(set(keys))


def fetch_odds_for_keys(
    session: requests.Session,
    api_key: str,
    target_name: str,
    sport_keys: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    all_events: list[dict[str, Any]] = []
    quota: dict[str, Any] = {
        "last_remaining": None,
        "last_used": None,
        "last_cost": None,
        "calls": [],
    }

    now = datetime.now(timezone.utc)
    latest_allowed = now + timedelta(days=DAYS_AHEAD)

    for index, sport_key in enumerate(sport_keys, start=1):
        url = ODDS_ODDS_URL.format(sport_key=sport_key)
        params = {
            "apiKey": api_key,
            "regions": REGIONS,
            "markets": MARKETS,
            "oddsFormat": ODDS_FORMAT,
            "dateFormat": DATE_FORMAT,
        }

        try:
            payload, headers = request_json(session, url, params=params)
        except Exception as exc:
            safe_error = str(exc).replace(api_key, "***")
            print(f"   {target_name} {index}/{len(sport_keys)} {sport_key}: FEHLER {safe_error}")
            quota["calls"].append({
                "sport_name": target_name,
                "sport_key": sport_key,
                "error": safe_error,
            })
            continue

        raw_events = payload if isinstance(payload, list) else []
        kept_events: list[dict[str, Any]] = []

        for event in raw_events:
            if not isinstance(event, dict):
                continue

            commence = parse_datetime(event.get("commence_time"))
            if commence is not None and commence > latest_allowed:
                continue

            event["sport_key"] = sport_key
            event["target_name"] = target_name
            kept_events.append(event)
            all_events.append(event)

        remaining = headers.get("x-requests-remaining")
        used = headers.get("x-requests-used")
        last = headers.get("x-requests-last")

        quota["last_remaining"] = remaining
        quota["last_used"] = used
        quota["last_cost"] = last
        quota["calls"].append({
            "sport_name": target_name,
            "sport_key": sport_key,
            "raw_event_count": len(raw_events),
            "kept_event_count": len(kept_events),
            "remaining": remaining,
            "used": used,
            "last_cost": last,
        })

        print(
            f"   {target_name} {index}/{len(sport_keys)} {sport_key}: "
            f"{len(kept_events)} Events behalten "
            f"({len(raw_events)} roh) | Quota verbleibend: {remaining or '?'}"
        )

        time.sleep(REQUEST_PAUSE_SECONDS)

    return all_events, quota


def decimal_to_probability(price: Any) -> float | None:
    number = as_float(price)
    if number is None or number <= 1.0:
        return None
    return 1.0 / number


def match_selection_to_odds_event(
    selection: dict[str, Any],
    odds_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    kickoff = parse_datetime(selection.get("kickoff_utc"))
    if kickoff is None:
        return None

    selected = safe_text(selection.get("selection"))
    opponent = safe_text(selection.get("opponent"))

    best: dict[str, Any] | None = None
    best_score = 0.0

    for odds_event in odds_events:
        commence = parse_datetime(odds_event.get("commence_time"))
        if commence is None:
            continue

        hours = abs((commence - kickoff).total_seconds()) / 3600
        if hours > MAX_MATCH_HOURS:
            continue

        home = safe_text(odds_event.get("home_team"))
        away = safe_text(odds_event.get("away_team"))

        direct = (similarity(selected, home) + similarity(opponent, away)) / 2
        swapped = (similarity(selected, away) + similarity(opponent, home)) / 2
        name_score = max(direct, swapped)
        time_score = max(0.0, 1.0 - (hours / MAX_MATCH_HOURS))
        total_score = 0.85 * name_score + 0.15 * time_score

        if total_score > best_score:
            best_score = total_score
            best = {
                "odds_event": odds_event,
                "match_score": total_score,
                "name_score": name_score,
                "time_diff_hours": hours,
            }

    if best is None or best_score < MIN_MATCH_SCORE:
        return None

    return best


def bookmaker_fair_probability(
    odds_event: dict[str, Any],
    selection: str,
    opponent: str,
) -> tuple[float | None, int, list[dict[str, Any]]]:
    fair_values: list[float] = []
    details: list[dict[str, Any]] = []

    for bookmaker in odds_event.get("bookmakers", []):
        if not isinstance(bookmaker, dict):
            continue

        h2h_market = None
        for market in bookmaker.get("markets", []):
            if isinstance(market, dict) and str(market.get("key")) == "h2h":
                h2h_market = market
                break

        if not h2h_market:
            continue

        outcomes = [
            item for item in h2h_market.get("outcomes", [])
            if isinstance(item, dict)
        ]
        if len(outcomes) < 2:
            continue

        selected_outcome = None
        opponent_outcome = None
        selected_score = 0.0
        opponent_score = 0.0

        for outcome in outcomes:
            name = safe_text(outcome.get("name"))
            s_score = similarity(name, selection)
            o_score = similarity(name, opponent)

            if s_score > selected_score:
                selected_score = s_score
                selected_outcome = outcome

            if o_score > opponent_score:
                opponent_score = o_score
                opponent_outcome = outcome

        if (
            selected_outcome is None
            or opponent_outcome is None
            or selected_outcome is opponent_outcome
            or selected_score < 0.70
            or opponent_score < 0.70
        ):
            continue

        p_selected = decimal_to_probability(selected_outcome.get("price"))
        p_opponent = decimal_to_probability(opponent_outcome.get("price"))

        if p_selected is None or p_opponent is None:
            continue

        total = p_selected + p_opponent
        if total <= 0:
            continue

        fair = p_selected / total
        fair_values.append(fair)
        details.append({
            "bookmaker": bookmaker.get("key"),
            "title": bookmaker.get("title"),
            "selection_price": selected_outcome.get("price"),
            "opponent_price": opponent_outcome.get("price"),
            "fair_probability": fair,
        })

    if not fair_values:
        return None, 0, details

    return statistics.median(fair_values), len(fair_values), details


def signal_from_selection(
    selection: dict[str, Any],
    match: dict[str, Any],
) -> dict[str, Any]:
    odds_event = match["odds_event"]
    orderbook = selection["orderbook"]

    selected = safe_text(selection.get("selection"))
    opponent = safe_text(selection.get("opponent"))

    fair, bookmaker_count, bookmaker_details = bookmaker_fair_probability(
        odds_event,
        selection=selected,
        opponent=opponent,
    )

    ask = as_float(orderbook.get("best_ask"))
    bid = as_float(orderbook.get("best_bid"))
    ask_size = as_float(orderbook.get("best_ask_size"))
    spread = as_float(orderbook.get("spread"))

    if spread is None and ask is not None and bid is not None:
        spread = ask - bid

    reasons: list[str] = []

    if ask is None:
        reasons.append("kein Ask")
    if fair is None:
        reasons.append("keine Buchmacher-Fair-Probability")
    if bookmaker_count < MIN_BOOKMAKERS:
        reasons.append("zu wenige Buchmacher")
    if ask_size is None or ask_size < MIN_ASK_SIZE:
        reasons.append("zu wenig Ask-Liquidität")
    if spread is None or spread > MAX_SPREAD:
        reasons.append("Spread zu groß")
    if ask is not None and ask > MAX_ENTRY_PRICE:
        reasons.append("Entry-Preis zu hoch")

    conservative = None
    gross_edge = None
    net_edge = None
    expected_roi = None

    if fair is not None and ask is not None:
        conservative = max(0.0, fair - CONSERVATIVE_HAIRCUT)
        gross_edge = fair - ask
        net_edge = conservative - ask
        expected_roi = net_edge / ask if ask > 0 else None

        if net_edge < MIN_NET_EDGE:
            reasons.append("Netto-Edge zu klein")

    decision = "PAPER_BUY" if not reasons else "SKIP"

    return {
        "observed_at_utc": now_utc(),
        "sport_name": selection.get("sport_name"),
        "decision": decision,
        "event_title": selection.get("event_title"),
        "kickoff_utc": selection.get("kickoff_utc"),
        "selection": selected,
        "opponent": opponent,
        "outcome_index": selection.get("outcome_index"),
        "best_bid": bid,
        "best_ask": ask,
        "best_ask_size": ask_size,
        "spread": spread,
        "fair_probability": fair,
        "conservative_probability": conservative,
        "gross_edge": gross_edge,
        "net_edge": net_edge,
        "expected_roi": expected_roi,
        "bookmaker_count": bookmaker_count,
        "match_score": match.get("match_score"),
        "time_diff_hours": match.get("time_diff_hours"),
        "odds_api_sport_key": odds_event.get("sport_key"),
        "odds_api_event_id": odds_event.get("id"),
        "reasons": "; ".join(reasons),
        "polymarket_event_id": selection.get("polymarket_event_id"),
        "polymarket_market_id": selection.get("polymarket_market_id"),
        "polymarket_token_id": selection.get("token_id"),
        "bookmaker_details": bookmaker_details[:12],
    }


def pct(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return "–"
    return f"{number:.2%}"


def esc(value: Any) -> str:
    return html.escape(safe_text(value), quote=True)


def update_history(report: dict[str, Any]) -> None:
    old_lines: list[str] = []
    if HISTORY_JSONL.exists():
        old_lines = HISTORY_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()

    old_lines.append(json.dumps({
        "generated_at_utc": report.get("generated_at_utc"),
        "paper_buy_count": report.get("paper_buy_count"),
        "signal_count": report.get("signal_count"),
        "quota_remaining": report.get("quota_remaining"),
        "paper_buys": report.get("paper_buys", []),
        "best_signals": report.get("best_signals", [])[:8],
    }, ensure_ascii=False))

    old_lines = old_lines[-HISTORY_MAX_LINES:]
    HISTORY_JSONL.write_text("\n".join(old_lines) + "\n", encoding="utf-8")


def read_history_summary(max_rows: int = 20) -> list[dict[str, Any]]:
    if not HISTORY_JSONL.exists():
        return []

    rows: list[dict[str, Any]] = []
    for line in HISTORY_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()[-max_rows:]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows[::-1]


def build_html(report: dict[str, Any]) -> str:
    total_buys = as_int(report.get("paper_buy_count"), 0)
    errors = report.get("errors") if isinstance(report.get("errors"), list) else []
    error_count = len(errors)

    if error_count > 0:
        state = "bad"
        status = "FEHLER PRÜFEN"
    elif total_buys > 0:
        state = "warn"
        status = "PAPER_BUY GEFUNDEN"
    else:
        state = "good"
        status = "ALLES RUHIG"

    targets = report.get("per_target") if isinstance(report.get("per_target"), list) else []
    signals = report.get("best_signals") if isinstance(report.get("best_signals"), list) else []
    buys = report.get("paper_buys") if isinstance(report.get("paper_buys"), list) else []
    history = read_history_summary(16)

    target_cards = ""
    for target in targets:
        target_cards += f"""
        <div class="card">
          <h3>{esc(target.get('name'))}</h3>
          <div class="mini-grid">
            <div><span>Märkte</span><b>{esc(target.get('match_winner_markets'))}</b></div>
            <div><span>Signale</span><b>{esc(target.get('signals'))}</b></div>
            <div><span>Buy</span><b>{esc(target.get('paper_buys'))}</b></div>
            <div><span>Odds</span><b>{esc(target.get('odds_events'))}</b></div>
          </div>
        </div>
        """

    def signal_card(signal: dict[str, Any]) -> str:
        decision = safe_text(signal.get("decision") or "SKIP")
        cls = "buy" if decision == "PAPER_BUY" else "skip"
        return f"""
        <div class="signal">
          <div class="row">
            <span class="badge {cls}">{esc(decision)}</span>
            <span class="muted">{esc(signal.get('sport_name'))}</span>
          </div>
          <h3>{esc(signal.get('event_title'))}</h3>
          <div class="pick">{esc(signal.get('selection'))}</div>
          <div class="mini-grid">
            <div><span>Ask</span><b>{esc(pct(signal.get('best_ask')))}</b></div>
            <div><span>Fair</span><b>{esc(pct(signal.get('fair_probability')))}</b></div>
            <div><span>Netto</span><b>{esc(pct(signal.get('net_edge')))}</b></div>
            <div><span>Spread</span><b>{esc(pct(signal.get('spread')))}</b></div>
          </div>
          <p>{esc(signal.get('reasons') or '-')}</p>
        </div>
        """

    buy_cards = "\n".join(signal_card(signal) for signal in buys)
    if not buy_cards:
        buy_cards = '<div class="empty">Kein PAPER_BUY gefunden.</div>'

    signal_cards = "\n".join(signal_card(signal) for signal in signals[:14])
    if not signal_cards:
        signal_cards = '<div class="empty">Noch keine Signale.</div>'

    history_rows = ""
    for item in history:
        cls = "warn" if as_int(item.get("paper_buy_count"), 0) > 0 else "good"
        history_rows += f"""
        <tr>
          <td>{esc(item.get('generated_at_utc'))}</td>
          <td class="{cls}">{esc(item.get('paper_buy_count'))}</td>
          <td>{esc(item.get('signal_count'))}</td>
          <td>{esc(item.get('quota_remaining'))}</td>
        </tr>
        """

    if not history_rows:
        history_rows = '<tr><td colspan="4">Noch keine Historie.</td></tr>'

    error_box = ""
    if errors:
        error_box = "<section><h2>Fehler</h2><pre>" + esc(json.dumps(errors, ensure_ascii=False, indent=2)) + "</pre></section>"

    return f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="120">
  <title>Paper Bot Cloud</title>
  <style>
    :root {{
      --bg:#0b1020; --panel:#151d37; --panel2:#1d284a; --text:#f5f7ff;
      --muted:#aeb9d8; --line:rgba(255,255,255,.12);
      --good:#1fc77a; --warn:#ffcc33; --bad:#ff5c7a;
    }}
    * {{ box-sizing:border-box; }}
    body {{
      margin:0; padding:16px; font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
      background:radial-gradient(circle at top,#22356f,var(--bg) 48%);
      color:var(--text);
    }}
    .wrap {{ max-width:960px; margin:0 auto; }}
    .hero {{
      background:linear-gradient(145deg,rgba(255,255,255,.11),rgba(255,255,255,.04));
      border:1px solid var(--line); border-radius:28px; padding:24px; margin-bottom:16px;
      box-shadow:0 18px 46px rgba(0,0,0,.34);
    }}
    .status {{
      display:inline-flex; padding:10px 14px; border-radius:999px; font-weight:900;
      letter-spacing:.02em;
    }}
    .status.good {{ color:var(--good); background:rgba(31,199,122,.16); }}
    .status.warn {{ color:var(--warn); background:rgba(255,204,51,.16); }}
    .status.bad {{ color:var(--bad); background:rgba(255,92,122,.16); }}
    h1 {{ font-size:clamp(34px,9vw,66px); margin:16px 0 8px; letter-spacing:-.06em; line-height:.92; }}
    h2 {{ margin:0 0 14px; font-size:22px; letter-spacing:-.03em; }}
    h3 {{ margin:4px 0; font-size:16px; }}
    .sub {{ color:var(--muted); line-height:1.45; font-size:14px; }}
    .big-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; margin-top:18px; }}
    .metric, .card, .signal {{
      background:rgba(255,255,255,.06); border:1px solid var(--line); border-radius:20px; padding:14px;
    }}
    .metric span, .mini-grid span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:5px; }}
    .metric b {{ font-size:28px; letter-spacing:-.04em; }}
    section {{
      background:rgba(21,29,55,.88); border:1px solid var(--line); border-radius:24px;
      padding:18px; margin-bottom:16px;
    }}
    .cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }}
    .signals {{ display:grid; grid-template-columns:repeat(2,1fr); gap:10px; }}
    .mini-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:8px; margin-top:10px; }}
    .mini-grid b {{ font-size:15px; }}
    .row {{ display:flex; justify-content:space-between; gap:8px; align-items:center; }}
    .badge {{ padding:6px 9px; border-radius:999px; font-size:12px; font-weight:900; }}
    .badge.buy {{ color:var(--warn); background:rgba(255,204,51,.17); }}
    .badge.skip {{ color:var(--muted); background:rgba(255,255,255,.08); }}
    .muted, .pick, p {{ color:var(--muted); }}
    .pick {{ margin-top:4px; }}
    p {{ margin:10px 0 0; font-size:12px; line-height:1.35; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    td, th {{ padding:9px 6px; border-bottom:1px solid var(--line); text-align:left; }}
    th {{ color:var(--muted); font-weight:700; }}
    .good {{ color:var(--good); font-weight:800; }}
    .warn {{ color:var(--warn); font-weight:800; }}
    .bad {{ color:var(--bad); font-weight:800; }}
    .empty {{ color:var(--muted); border:1px dashed var(--line); border-radius:16px; padding:14px; }}
    pre {{ white-space:pre-wrap; overflow:auto; color:var(--muted); }}
    footer {{ text-align:center; color:var(--muted); font-size:12px; padding:20px 0; }}
    @media(max-width:760px) {{
      body {{ padding:10px; }}
      .hero {{ padding:20px; border-radius:24px; }}
      .big-grid, .cards, .signals {{ grid-template-columns:1fr; }}
      .mini-grid {{ grid-template-columns:repeat(2,1fr); }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="hero">
      <div class="status {state}">{esc(status)}</div>
      <h1>Paper Bot Cloud</h1>
      <div class="sub">
        Letzter Lauf: {esc(report.get('generated_at_utc'))}<br>
        Läuft über GitHub Actions. Seite aktualisiert sich automatisch alle 120 Sekunden.<br>
        Nur Paper-Trading. Keine echten Orders.
      </div>
      <div class="big-grid">
        <div class="metric"><span>PAPER_BUY</span><b>{esc(total_buys)}</b></div>
        <div class="metric"><span>Signale</span><b>{esc(report.get('signal_count'))}</b></div>
        <div class="metric"><span>Fehler</span><b>{esc(error_count)}</b></div>
        <div class="metric"><span>Quota</span><b>{esc(report.get('quota_remaining') or '–')}</b></div>
      </div>
    </div>

    <section>
      <h2>Bereiche</h2>
      <div class="cards">{target_cards}</div>
    </section>

    <section>
      <h2>PAPER_BUY</h2>
      <div class="signals">{buy_cards}</div>
    </section>

    <section>
      <h2>Beste aktuelle Kanten</h2>
      <div class="signals">{signal_cards}</div>
    </section>

    <section>
      <h2>Historie</h2>
      <table>
        <thead><tr><th>Zeit UTC</th><th>Buy</th><th>Signale</th><th>Quota</th></tr></thead>
        <tbody>{history_rows}</tbody>
      </table>
    </section>

    {error_box}

    <footer>
      Wenn PAPER_BUY > 0 angezeigt wird: Bericht kopieren und prüfen. Keine echten Orders.
    </footer>
  </div>
</body>
</html>
"""


def run() -> dict[str, Any]:
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ODDS_API_KEY fehlt. In GitHub als Repository Secret speichern.")

    session = requests.Session()
    session.headers.update({"User-Agent": "polymarket-paper-cloud/0.26"})

    print("1) Odds-API-Sportliste laden")
    sports = fetch_odds_sports(session, api_key)

    all_signals: list[dict[str, Any]] = []
    per_target: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    quota_remaining: str | None = None
    quota_used: str | None = None

    for target in TARGETS:
        name = target["name"]
        print()
        print(f"Bereich: {name}")

        try:
            events = fetch_polymarket_events_for_target(session, target)
            selections = build_match_winner_selections(target, events)
            market_count = len({item["polymarket_market_id"] for item in selections})
            print(f"   Events: {len(events)}")
            print(f"   Match-Winner-Märkte: {market_count}")
            print(f"   Outcome-Auswahlen: {len(selections)}")

            keys = target_sport_keys(sports, target)
            print(f"   Odds-Keys: {', '.join(keys) if keys else 'keine'}")

            odds_events: list[dict[str, Any]] = []
            quota: dict[str, Any] = {"calls": []}
            if keys:
                odds_events, quota = fetch_odds_for_keys(session, api_key, name, keys)
                if quota.get("last_remaining") is not None:
                    quota_remaining = safe_text(quota.get("last_remaining"))
                if quota.get("last_used") is not None:
                    quota_used = safe_text(quota.get("last_used"))

            enriched: list[dict[str, Any]] = []
            for index, item in enumerate(selections, start=1):
                orderbook = fetch_orderbook(session, item["token_id"])
                if not orderbook.get("error"):
                    enriched.append({**item, "orderbook": orderbook})
                if index % 75 == 0:
                    print(f"   {index}/{len(selections)} Orderbücher geprüft")
                time.sleep(REQUEST_PAUSE_SECONDS)

            target_signals: list[dict[str, Any]] = []
            for item in enriched:
                if not odds_events:
                    continue

                match = match_selection_to_odds_event(item, odds_events)
                if match is None:
                    continue

                target_signals.append(signal_from_selection(item, match))

            all_signals.extend(target_signals)
            buys = [item for item in target_signals if item.get("decision") == "PAPER_BUY"]

            per_target.append({
                "name": name,
                "polymarket_events": len(events),
                "match_winner_markets": market_count,
                "outcome_selections": len(selections),
                "orderbooks_loaded": len(enriched),
                "odds_keys": keys,
                "odds_events": len(odds_events),
                "signals": len(target_signals),
                "paper_buys": len(buys),
            })

            print(f"   Signale: {len(target_signals)} | PAPER_BUY: {len(buys)}")

        except Exception as exc:
            safe_error = safe_text(exc).replace(api_key, "***")
            errors.append({"target": name, "error": safe_error})
            print(f"   FEHLER: {safe_error}")

    all_signals.sort(
        key=lambda item: as_float(item.get("net_edge")) if as_float(item.get("net_edge")) is not None else -999,
        reverse=True,
    )

    paper_buys = [item for item in all_signals if item.get("decision") == "PAPER_BUY"]

    report = {
        "generated_at_utc": now_utc(),
        "paper_buy_count": len(paper_buys),
        "signal_count": len(all_signals),
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
        "per_target": per_target,
        "paper_buys": paper_buys,
        "best_signals": all_signals[:30],
        "signals": all_signals,
        "errors": errors,
        "settings": {
            "min_net_edge": MIN_NET_EDGE,
            "max_spread": MAX_SPREAD,
            "max_entry_price": MAX_ENTRY_PRICE,
            "min_ask_size": MIN_ASK_SIZE,
            "min_bookmakers": MIN_BOOKMAKERS,
        },
    }

    REPORT_JSON.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    update_history(report)
    INDEX_HTML.write_text(build_html(report), encoding="utf-8")
    LAST_RUN_TXT.write_text(
        f"{report['generated_at_utc']}\n"
        f"PAPER_BUY={report['paper_buy_count']}\n"
        f"SIGNALS={report['signal_count']}\n"
        f"QUOTA={report.get('quota_remaining') or '?'}\n",
        encoding="utf-8",
    )

    return report


def main() -> int:
    print("=" * 78)
    print("SCHRITT 26: GITHUB CLOUD PAPER BOT")
    print("=" * 78)
    print("Cloud-Scanner für Tennis, WNBA und UFC.")
    print("Nur Paper-Trading. Keine echten Orders.")
    print("=" * 78)

    report = run()

    print()
    print("=" * 78)
    print("ERGEBNIS")
    print("=" * 78)
    print(f"Zeit UTC:   {report['generated_at_utc']}")
    print(f"Signale:    {report['signal_count']}")
    print(f"PAPER_BUY:  {report['paper_buy_count']}")
    print(f"Quota:      {report.get('quota_remaining') or '?'}")
    print(f"Fehler:     {len(report.get('errors') or [])}")
    print("=" * 78)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        raise SystemExit(1)
