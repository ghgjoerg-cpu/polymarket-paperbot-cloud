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
PAPER_PORTFOLIO_JSON = DOCS_DIR / "paper_portfolio.json"
PAPER_TRADES_JSONL = DOCS_DIR / "paper_trades.jsonl"

GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/events/keyset"
BOOK_URL = "https://clob.polymarket.com/book"
ODDS_SPORTS_URL = "https://api.the-odds-api.com/v4/sports"
ODDS_ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
POLYMARKET_SPORTS_URL = "https://gamma-api.polymarket.com/sports"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

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
MAX_FOOTBALL_ODDS_KEYS = 8
HISTORY_MAX_LINES = 5000
PAPER_BANKROLL_START = 1000.0
PAPER_STAKE_FRACTION = 0.0025
PAPER_MIN_STAKE = 2.50
PAPER_MAX_STAKE = 2.50

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



def load_paper_portfolio() -> dict[str, Any]:
    if PAPER_PORTFOLIO_JSON.exists():
        try:
            payload = json.loads(PAPER_PORTFOLIO_JSON.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload.setdefault("bankroll_start", PAPER_BANKROLL_START)
                payload.setdefault("cash", PAPER_BANKROLL_START)
                payload.setdefault("realized_pnl", 0.0)
                payload.setdefault("open_positions", [])
                payload.setdefault("closed_positions", [])
                payload.setdefault("last_updated_utc", now_utc())
                return payload
        except Exception:
            pass

    return {
        "bankroll_start": PAPER_BANKROLL_START,
        "cash": PAPER_BANKROLL_START,
        "realized_pnl": 0.0,
        "open_positions": [],
        "closed_positions": [],
        "last_updated_utc": now_utc(),
    }


def paper_position_key(signal: dict[str, Any]) -> str:
    token = safe_text(signal.get("polymarket_token_id")).strip()
    if token:
        return token

    return "|".join([
        safe_text(signal.get("sport_name")),
        safe_text(signal.get("polymarket_market_id")),
        safe_text(signal.get("selection")),
    ])


def paper_stake_for_portfolio(portfolio: dict[str, Any]) -> float:
    bankroll = as_float(portfolio.get("bankroll_start")) or PAPER_BANKROLL_START
    stake = bankroll * PAPER_STAKE_FRACTION
    stake = max(PAPER_MIN_STAKE, stake)
    stake = min(PAPER_MAX_STAKE, stake)
    return round(stake, 2)


def update_paper_portfolio_from_buys(
    portfolio: dict[str, Any],
    paper_buys: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    open_positions = [
        item for item in portfolio.get("open_positions", [])
        if isinstance(item, dict)
    ]
    closed_positions = [
        item for item in portfolio.get("closed_positions", [])
        if isinstance(item, dict)
    ]

    existing_open_keys = {
        safe_text(item.get("position_key"))
        for item in open_positions
    }
    existing_closed_keys = {
        safe_text(item.get("position_key"))
        for item in closed_positions
    }

    new_trades: list[dict[str, Any]] = []
    cash = as_float(portfolio.get("cash"))
    if cash is None:
        cash = PAPER_BANKROLL_START

    for signal in paper_buys:
        key = paper_position_key(signal)
        if not key:
            continue

        if key in existing_open_keys or key in existing_closed_keys:
            continue

        entry_price = as_float(signal.get("best_ask"))
        if entry_price is None or entry_price <= 0 or entry_price >= 1:
            continue

        stake = paper_stake_for_portfolio(portfolio)
        if cash < stake:
            continue

        shares = stake / entry_price
        max_payout = shares
        max_profit = max_payout - stake

        position = {
            "position_key": key,
            "status": "OPEN",
            "opened_at_utc": now_utc(),
            "sport_name": signal.get("sport_name"),
            "event_title": signal.get("event_title"),
            "kickoff_utc": signal.get("kickoff_utc"),
            "selection": signal.get("selection"),
            "opponent": signal.get("opponent"),
            "entry_price": entry_price,
            "stake": stake,
            "shares": shares,
            "max_payout": max_payout,
            "max_profit": max_profit,
            "max_loss": stake,
            "fair_probability_at_entry": signal.get("fair_probability"),
            "net_edge_at_entry": signal.get("net_edge"),
            "spread_at_entry": signal.get("spread"),
            "bookmaker_count": signal.get("bookmaker_count"),
            "polymarket_event_id": signal.get("polymarket_event_id"),
            "polymarket_market_id": signal.get("polymarket_market_id"),
            "polymarket_token_id": signal.get("polymarket_token_id"),
            "odds_api_sport_key": signal.get("odds_api_sport_key"),
            "odds_api_event_id": signal.get("odds_api_event_id"),
        }

        open_positions.append(position)
        existing_open_keys.add(key)
        cash -= stake

        trade = {
            "trade_type": "PAPER_BUY",
            **position,
        }
        new_trades.append(trade)

    open_risk = sum(as_float(item.get("stake")) or 0.0 for item in open_positions)
    open_max_profit = sum(as_float(item.get("max_profit")) or 0.0 for item in open_positions)
    realized_pnl = as_float(portfolio.get("realized_pnl")) or 0.0
    estimated_equity = cash + open_risk + realized_pnl

    portfolio.update({
        "cash": round(cash, 4),
        "realized_pnl": round(realized_pnl, 4),
        "open_positions": open_positions,
        "closed_positions": closed_positions,
        "open_position_count": len(open_positions),
        "closed_position_count": len(closed_positions),
        "open_risk": round(open_risk, 4),
        "open_max_profit": round(open_max_profit, 4),
        "estimated_equity": round(estimated_equity, 4),
        "last_updated_utc": now_utc(),
    })

    PAPER_PORTFOLIO_JSON.write_text(
        json.dumps(portfolio, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if new_trades:
        with PAPER_TRADES_JSONL.open("a", encoding="utf-8") as file:
            for trade in new_trades:
                file.write(json.dumps(trade, ensure_ascii=False) + "\n")

    return portfolio, new_trades


def fmt_money(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return "–"
    return f"${number:.2f}"


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
        "portfolio": report.get("portfolio", {}),
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
    portfolio = report.get("portfolio") if isinstance(report.get("portfolio"), dict) else {}
    open_positions = portfolio.get("open_positions") if isinstance(portfolio.get("open_positions"), list) else []
    new_trades = report.get("new_paper_trades") if isinstance(report.get("new_paper_trades"), list) else []

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


    position_cards = ""
    for pos in open_positions[:10]:
        position_cards += f"""
        <div class="signal">
          <div class="row">
            <span class="badge buy">OPEN</span>
            <span class="muted">{esc(pos.get('sport_name'))}</span>
          </div>
          <h3>{esc(pos.get('event_title'))}</h3>
          <div class="pick">{esc(pos.get('selection'))}</div>
          <div class="mini-grid">
            <div><span>Entry</span><b>{esc(pct(pos.get('entry_price')))}</b></div>
            <div><span>Stake</span><b>{esc(fmt_money(pos.get('stake')))}</b></div>
            <div><span>Shares</span><b>{esc(round(as_float(pos.get('shares')) or 0, 4))}</b></div>
            <div><span>Max Profit</span><b>{esc(fmt_money(pos.get('max_profit')))}</b></div>
          </div>
          <p>Eröffnet: {esc(pos.get('opened_at_utc'))}</p>
        </div>
        """

    if not position_cards:
        position_cards = '<div class="empty">Noch keine offenen Paper-Positionen.</div>'

    new_trade_cards = ""
    for trade in new_trades[:8]:
        new_trade_cards += f"""
        <div class="signal">
          <div class="row">
            <span class="badge buy">NEU</span>
            <span class="muted">{esc(trade.get('sport_name'))}</span>
          </div>
          <h3>{esc(trade.get('event_title'))}</h3>
          <div class="pick">{esc(trade.get('selection'))}</div>
          <div class="mini-grid">
            <div><span>Entry</span><b>{esc(pct(trade.get('entry_price')))}</b></div>
            <div><span>Stake</span><b>{esc(fmt_money(trade.get('stake')))}</b></div>
            <div><span>Netto</span><b>{esc(pct(trade.get('net_edge_at_entry')))}</b></div>
            <div><span>Bookies</span><b>{esc(trade.get('bookmaker_count'))}</b></div>
          </div>
        </div>
        """

    if not new_trade_cards:
        new_trade_cards = '<div class="empty">In diesem Lauf kein neuer Paper-Trade.</div>'


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
      <h2>Paper-Portfolio</h2>
      <div class="big-grid">
        <div class="metric"><span>Cash</span><b>{esc(fmt_money(portfolio.get('cash')))}</b></div>
        <div class="metric"><span>Offen</span><b>{esc(portfolio.get('open_position_count') or 0)}</b></div>
        <div class="metric"><span>Risiko</span><b>{esc(fmt_money(portfolio.get('open_risk')))}</b></div>
        <div class="metric"><span>Equity</span><b>{esc(fmt_money(portfolio.get('estimated_equity')))}</b></div>
      </div>
    </section>

    <section>
      <h2>Neue Paper-Trades</h2>
      <div class="signals">{new_trade_cards}</div>
    </section>

    <section>
      <h2>Offene Paper-Positionen</h2>
      <div class="signals">{position_cards}</div>
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



def fetch_polymarket_sports_metadata(session: requests.Session) -> list[dict[str, Any]]:
    try:
        payload, _headers = request_json(session, POLYMARKET_SPORTS_URL)
    except Exception as exc:
        print(f"   Fußball: /sports konnte nicht geladen werden: {exc}")
        return []

    if not isinstance(payload, list):
        return []

    return [item for item in payload if isinstance(item, dict)]


def parse_tag_ids(value: Any) -> list[int]:
    if value is None:
        return []

    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = re.split(r"[,;\s]+", safe_text(value))

    ids: list[int] = []
    for item in raw_items:
        try:
            number = int(str(item).strip())
        except Exception:
            continue
        if number > 0:
            ids.append(number)

    return ids


def football_tag_ids_from_sports(session: requests.Session) -> list[int]:
    sports = fetch_polymarket_sports_metadata(session)
    tag_ids: list[int] = []

    for item in sports:
        text = " ".join([
            safe_text(item.get("sport")),
            safe_text(item.get("title")),
            safe_text(item.get("slug")),
            safe_text(item.get("name")),
            safe_text(item.get("series")),
            safe_text(item.get("tags")),
        ]).lower()

        if ("soccer" in text or "football" in text or "fifa" in text or "world cup" in text) and "american" not in text:
            tag_ids.extend(parse_tag_ids(item.get("tags")))

    # Fallback-Kandidaten; sie werden nur genutzt, wenn Polymarket dazu aktive Fußball-Events liefert.
    tag_ids.extend([1, 100381, 101, 102, 103, 100383, 100384])

    result: list[int] = []
    seen: set[int] = set()
    for tag_id in tag_ids:
        if tag_id not in seen:
            seen.add(tag_id)
            result.append(tag_id)

    return result[:12]


def football_keywords(text: str) -> bool:
    low = text.lower()
    words = [
        "soccer",
        "football",
        "world cup",
        "fifa",
        "fifwc",
        "uefa",
        "champions league",
        "europa league",
        "premier league",
        "la liga",
        "bundesliga",
        "serie a",
        "ligue 1",
        "mls",
        "copa",
    ]
    return any(word in low for word in words)


def fetch_events_for_tag(session: requests.Session, tag_id: int, max_pages: int = 3) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()

    for _page in range(max_pages):
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
            continue

        if not isinstance(payload, dict):
            break

        page_events = [item for item in payload.get("events", []) if isinstance(item, dict)]
        events.extend(page_events)

        next_cursor = str(payload.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            break

        seen_cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(REQUEST_PAUSE_SECONDS)

    return events


def fetch_active_events_fallback(session: requests.Session, max_pages: int = 6) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    limit = 200

    for page in range(max_pages):
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": page * limit,
            "order": "volume_24hr",
            "ascending": "false",
        }

        try:
            payload, _headers = request_json(session, GAMMA_EVENTS_URL, params=params)
        except Exception:
            break

        if not isinstance(payload, list):
            break

        page_events = [item for item in payload if isinstance(item, dict)]
        events.extend(page_events)

        if len(page_events) < limit:
            break

        time.sleep(REQUEST_PAUSE_SECONDS)

    return [
        event for event in events
        if football_keywords(" ".join([
            event_title(event),
            safe_text(event.get("slug")),
            safe_text(event.get("description")),
        ]))
    ]


def fetch_football_events(session: requests.Session) -> tuple[list[dict[str, Any]], list[int]]:
    tag_ids = football_tag_ids_from_sports(session)
    events: list[dict[str, Any]] = []

    for tag_id in tag_ids:
        tag_events = fetch_events_for_tag(session, tag_id, max_pages=2)
        if tag_events:
            print(f"   Fußball Tag {tag_id}: {len(tag_events)} Events")
        events.extend(tag_events)
        if len(events) >= 800:
            break

    if not events:
        print("   Fußball: Tag-Suche leer, nutze Fallback über aktive Events.")
        events = fetch_active_events_fallback(session)

    dedup: dict[str, dict[str, Any]] = {}
    for event in events:
        text = " ".join([
            event_title(event),
            safe_text(event.get("slug")),
            safe_text(event.get("description")),
        ])
        if football_keywords(text):
            identifier = event_id(event)
            if identifier:
                dedup[identifier] = event

    return list(dedup.values()), tag_ids


def parse_vs_teams(title: str) -> tuple[str, str] | None:
    text = safe_text(title)
    if ":" in text:
        text = text.split(":")[-1]

    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    parts = re.split(r"\s+vs\.?\s+|\s+v\.?\s+", text, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return None

    home = parts[0].strip(" -–—")
    away = parts[1].strip(" -–—")

    if not home or not away:
        return None

    return home, away


def clean_selection_label(text: str, home: str, away: str) -> str:
    value = safe_text(text).strip()
    if not value:
        return ""

    if similarity(value, home) >= 0.90:
        return home
    if similarity(value, away) >= 0.90:
        return away
    if normalize_name(value) in {"draw", "tie"}:
        return "Draw"

    value = re.sub(r"\([^)]*\)", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" :-–—")
    return value


def football_selection_from_market(event: dict[str, Any], market: dict[str, Any], home: str, away: str) -> str | None:
    candidates = [
        safe_text(market.get("groupItemTitle")),
        safe_text(market.get("question")),
        safe_text(market.get("title")),
        safe_text(market.get("slug")),
        safe_text(market.get("description")),
    ]

    for candidate in candidates:
        if not candidate:
            continue

        label = clean_selection_label(candidate, home, away)
        low = normalize_name(label)

        if low in {"draw", "tie"} or " draw " in f" {low} ":
            return "Draw"

        if similarity(label, home) >= 0.74:
            return home

        if similarity(label, away) >= 0.74:
            return away

    return None


def is_football_moneyline_leg(event: dict[str, Any], market: dict[str, Any]) -> bool:
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

    outcomes = [safe_text(item).strip().lower() for item in parse_json_list(market.get("outcomes"))]
    token_ids = [safe_text(item).strip() for item in parse_json_list(market.get("clobTokenIds"))]

    if len(outcomes) != 2 or len(token_ids) != 2:
        return False

    return set(outcomes) == {"yes", "no"}


def build_football_1x2_selections(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selections: list[dict[str, Any]] = []

    for event in events:
        teams = parse_vs_teams(event_title(event))
        if teams is None:
            continue

        home, away = teams
        legs: dict[str, dict[str, Any]] = {}

        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue

            if not is_football_moneyline_leg(event, market):
                continue

            kickoff = get_kickoff(event, market)
            if kickoff is None or kickoff <= datetime.now(timezone.utc):
                continue

            selection = football_selection_from_market(event, market, home, away)
            if selection is None:
                continue

            token_ids = [safe_text(item).strip() for item in parse_json_list(market.get("clobTokenIds"))]
            if not token_ids:
                continue

            legs[selection] = {
                "sport_name": "Fußball",
                "tag_id": "dynamic",
                "event_title": event_title(event),
                "kickoff_utc": kickoff.isoformat(),
                "selection": selection,
                "opponent": away if selection == home else home,
                "home_team": home,
                "away_team": away,
                "outcome_index": 0,
                "token_id": token_ids[0],
                "polymarket_event_id": event_id(event),
                "polymarket_market_id": market_id(market),
            }

        if len(legs) < 2:
            continue

        for selection in [home, "Draw", away]:
            if selection in legs:
                selections.append(legs[selection])

    return selections


def football_odds_keys(sports: list[dict[str, Any]]) -> list[str]:
    candidates: list[tuple[int, str]] = []
    priority = [
        "fifa_world_cup",
        "world_cup",
        "uefa_champs_league",
        "uefa_europa_league",
        "usa_mls",
        "england_premier_league",
        "spain_la_liga",
        "germany_bundesliga",
        "italy_serie_a",
        "france_ligue_one",
        "brazil",
        "argentina",
    ]

    for sport in sports:
        key = safe_text(sport.get("key"))
        if not key:
            continue

        text = " ".join([
            key,
            safe_text(sport.get("group")),
            safe_text(sport.get("title")),
            safe_text(sport.get("description")),
        ]).lower()

        if "soccer" not in text:
            continue

        # Outright tournament-winner markets do not support h2h.
        if "_winner" in key.lower() or "outright" in text:
            continue

        if sport.get("active") is False:
            continue

        rank = 999
        for index, word in enumerate(priority):
            if word in text:
                rank = index
                break

        candidates.append((rank, key))

    candidates.sort(key=lambda item: (item[0], item[1]))
    keys = [key for _rank, key in candidates]
    return sorted(set(keys), key=keys.index)[:MAX_FOOTBALL_ODDS_KEYS]


def match_football_selection_to_odds_event(selection: dict[str, Any], odds_events: list[dict[str, Any]]) -> dict[str, Any] | None:
    kickoff = parse_datetime(selection.get("kickoff_utc"))
    if kickoff is None:
        return None

    home = safe_text(selection.get("home_team"))
    away = safe_text(selection.get("away_team"))

    best: dict[str, Any] | None = None
    best_score = 0.0

    for odds_event in odds_events:
        commence = parse_datetime(odds_event.get("commence_time"))
        if commence is None:
            continue

        hours = abs((commence - kickoff).total_seconds()) / 3600
        if hours > MAX_MATCH_HOURS:
            continue

        odds_home = safe_text(odds_event.get("home_team"))
        odds_away = safe_text(odds_event.get("away_team"))

        direct = (similarity(home, odds_home) + similarity(away, odds_away)) / 2
        swapped = (similarity(home, odds_away) + similarity(away, odds_home)) / 2
        name_score = max(direct, swapped)
        time_score = max(0.0, 1.0 - (hours / MAX_MATCH_HOURS))
        total_score = 0.85 * name_score + 0.15 * time_score

        if total_score > best_score:
            best_score = total_score
            best = {"odds_event": odds_event, "match_score": total_score, "name_score": name_score, "time_diff_hours": hours}

    if best is None or best_score < MIN_MATCH_SCORE:
        return None

    return best


def is_draw_name(name: str) -> bool:
    low = normalize_name(name)
    return low in {"draw", "tie"} or " draw " in f" {low} "


def football_bookmaker_fair_probability(odds_event: dict[str, Any], selection: dict[str, Any]) -> tuple[float | None, int, list[dict[str, Any]]]:
    selected_label = safe_text(selection.get("selection"))
    home = safe_text(selection.get("home_team"))
    away = safe_text(selection.get("away_team"))

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

        outcomes = [item for item in h2h_market.get("outcomes", []) if isinstance(item, dict)]
        if len(outcomes) < 2:
            continue

        probs: dict[str, float] = {}
        for outcome in outcomes:
            name = safe_text(outcome.get("name"))
            prob = decimal_to_probability(outcome.get("price"))
            if prob is None:
                continue
            if is_draw_name(name):
                probs["Draw"] = prob
            elif similarity(name, home) >= 0.70:
                probs[home] = prob
            elif similarity(name, away) >= 0.70:
                probs[away] = prob

        if selected_label not in probs:
            continue

        total = sum(probs.values())
        if total <= 0:
            continue

        fair = probs[selected_label] / total
        fair_values.append(fair)
        details.append({
            "bookmaker": bookmaker.get("key"),
            "title": bookmaker.get("title"),
            "selection": selected_label,
            "raw_probability": probs[selected_label],
            "fair_probability": fair,
            "outcome_count": len(probs),
        })

    if not fair_values:
        return None, 0, details

    return statistics.median(fair_values), len(fair_values), details


def signal_from_football_selection(selection: dict[str, Any], match: dict[str, Any]) -> dict[str, Any]:
    odds_event = match["odds_event"]
    orderbook = selection["orderbook"]
    fair, bookmaker_count, bookmaker_details = football_bookmaker_fair_probability(odds_event, selection=selection)

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
        "sport_name": "Fußball",
        "decision": decision,
        "event_title": selection.get("event_title"),
        "kickoff_utc": selection.get("kickoff_utc"),
        "selection": selection.get("selection"),
        "opponent": selection.get("opponent"),
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


def scan_football_cloud(session: requests.Session, api_key: str, sports: list[dict[str, Any]]) -> dict[str, Any]:
    print()
    print("Bereich: Fußball")
    events, tag_ids = fetch_football_events(session)
    selections = build_football_1x2_selections(events)
    market_count = len({item["polymarket_market_id"] for item in selections})
    print(f"   Events: {len(events)}")
    print(f"   1X2/Moneyline-Märkte: {market_count}")
    print(f"   Outcome-Auswahlen: {len(selections)}")

    keys = football_odds_keys(sports)
    print(f"   Odds-Keys: {', '.join(keys) if keys else 'keine'}")

    odds_events: list[dict[str, Any]] = []
    quota: dict[str, Any] = {"calls": []}
    if keys:
        odds_events, quota = fetch_odds_for_keys(session, api_key, "Fußball", keys)

    enriched: list[dict[str, Any]] = []
    for index, item in enumerate(selections, start=1):
        orderbook = fetch_orderbook(session, item["token_id"])
        if not orderbook.get("error"):
            enriched.append({**item, "orderbook": orderbook})
        if index % 75 == 0:
            print(f"   {index}/{len(selections)} Fußball-Orderbücher geprüft")
        time.sleep(REQUEST_PAUSE_SECONDS)

    signals: list[dict[str, Any]] = []
    for item in enriched:
        if not odds_events:
            continue
        match = match_football_selection_to_odds_event(item, odds_events)
        if match is None:
            continue
        signals.append(signal_from_football_selection(item, match))

    buys = [item for item in signals if item.get("decision") == "PAPER_BUY"]
    print(f"   Signale: {len(signals)} | PAPER_BUY: {len(buys)}")

    return {
        "signals": signals,
        "summary": {
            "name": "Fußball",
            "polymarket_events": len(events),
            "football_tag_ids": tag_ids[:12],
            "match_winner_markets": market_count,
            "outcome_selections": len(selections),
            "orderbooks_loaded": len(enriched),
            "odds_keys": keys,
            "odds_events": len(odds_events),
            "signals": len(signals),
            "paper_buys": len(buys),
        },
        "quota": quota,
    }


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

    try:
        football_result = scan_football_cloud(session, api_key, sports)
        football_signals = football_result.get("signals") or []
        all_signals.extend(football_signals)
        if isinstance(football_result.get("summary"), dict):
            per_target.append(football_result["summary"])
        football_quota = football_result.get("quota") or {}
        if football_quota.get("last_remaining") is not None:
            quota_remaining = safe_text(football_quota.get("last_remaining"))
        if football_quota.get("last_used") is not None:
            quota_used = safe_text(football_quota.get("last_used"))
    except Exception as exc:
        safe_error = safe_text(exc).replace(api_key, "***")
        errors.append({"target": "Fußball", "error": safe_error})
        print(f"   Fußball FEHLER: {safe_error}")

    all_signals.sort(
        key=lambda item: as_float(item.get("net_edge")) if as_float(item.get("net_edge")) is not None else -999,
        reverse=True,
    )

    paper_buys = [item for item in all_signals if item.get("decision") == "PAPER_BUY"]

    portfolio, new_paper_trades = update_paper_portfolio_from_buys(
        load_paper_portfolio(),
        paper_buys,
    )

    report = {
        "generated_at_utc": now_utc(),
        "paper_buy_count": len(paper_buys),
        "signal_count": len(all_signals),
        "quota_remaining": quota_remaining,
        "quota_used": quota_used,
        "per_target": per_target,
        "paper_buys": paper_buys,
        "new_paper_trades": new_paper_trades,
        "portfolio": portfolio,
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
        f"QUOTA={report.get('quota_remaining') or '?'}\n"
        f"OPEN_POSITIONS={portfolio.get('open_position_count', 0)}\n"
        f"OPEN_RISK={portfolio.get('open_risk', 0)}\n",
        encoding="utf-8",
    )

    return report


def main() -> int:
    print("=" * 78)
    print("SCHRITT 28B: CLOUD PAPER-PORTFOLIO HOTFIX")
    print("=" * 78)
    print("Cloud-Scanner für Fußball, Tennis, WNBA und UFC mit Paper-Portfolio.")
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
