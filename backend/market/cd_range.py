"""
CD desk range: HIGH / LOW / MID from the last N closed TF-minute bars of the
active monthly SENSEX futures contract.

The contract is the nearest BSEFO SENSEX future whose expiry date is still
today or later. The expiry session stays on that contract. The next session
uses the next month. Spot bars are not used.

The forming candle is never included. A live wick does not move HIGH or LOW.
Bars are session-aligned from 09:15 IST. The first entry is 09:15 + N × TF.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from market.dada_range import (
    IST,
    OhlcBar,
    _minute_key_to_dt,
    aggregate_1m_to_tf,
    closed_tf_bars,
    fetch_session_1m_bars,
)

BSEFO_SEGMENT = 12
CANDLES = 4
MIN_GAP = 50.0
TF_MINUTES = 15
CACHE_SECONDS = 2.0
CONTRACT_CACHE_SECONDS = 60.0
CONTRACT_MISS_SECONDS = 15.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"at": 0.0, "day": "", "payload": None}
_CONTRACT_CACHE: dict[str, Any] = {"at": 0.0, "day": "", "contract": None}
_MD_CLIENT: Callable[[], Any] | None = None


def set_cd_md_client(getter: Callable[[], Any] | None) -> None:
    """XTS market-data client used to fetch Sensex minute bars. Set from the API route."""
    global _MD_CLIENT
    _MD_CLIENT = getter


def _xts_client() -> Any:
    if _MD_CLIENT is None:
        raise RuntimeError("XTS history is not configured")
    client = _MD_CLIENT()
    if client is None:
        raise RuntimeError("XTS history is not configured")
    return client


def parse_range_params(tf: str, candles: str, min_gap: str) -> dict[str, Any] | None:
    """Saved CD range inputs. Whole minutes and candles, gap from 0 to 5000."""
    tf_text = str(tf or "").strip()
    candle_text = str(candles or "").strip()
    gap_text = str(min_gap or "").strip()
    if not re.fullmatch(r"\d+", tf_text) or not re.fullmatch(r"\d+", candle_text):
        return None
    if not re.fullmatch(r"\d+(\.\d+)?", gap_text):
        return None
    tf_i = int(tf_text)
    n = int(candle_text)
    gap = float(gap_text)
    if not (1 <= tf_i <= 60 and 1 <= n <= 20 and 0 <= gap <= 5000):
        return None
    return {"tf_minutes": tf_i, "candles": n, "min_gap": gap}


def first_entry_label(candles: int, tf_minutes: int) -> str:
    total = 9 * 60 + 15 + int(candles) * int(tf_minutes)
    days, mins = divmod(total, 24 * 60)
    hh, mm = divmod(mins, 60)
    clock = f"{hh:02d}:{mm:02d}"
    return f"{clock} +{days}d" if days else clock


def _label(minute_key: int) -> str:
    text = str(int(minute_key))
    if len(text) != 12:
        return text
    return f"{text[8:10]}:{text[10:12]}"


def _today_bars(bars: list[OhlcBar], now: datetime) -> list[OhlcBar]:
    day = now.astimezone(IST).date()
    out: list[OhlcBar] = []
    for bar in bars or []:
        dt = _minute_key_to_dt(bar.minute_key)
        if dt is not None and dt.date() == day:
            out.append(bar)
    return out


def build_cd_range(
    bars_1m: list[OhlcBar],
    now: datetime | None = None,
    *,
    candles: int = CANDLES,
    min_gap: float = MIN_GAP,
    tf_minutes: int = TF_MINUTES,
) -> dict[str, Any]:
    """Last `candles` fully closed TF bars. The live bar is left out."""
    now = (now or datetime.now(IST)).astimezone(IST)
    tf = max(1, int(tf_minutes))
    n = max(1, int(candles))
    closed = closed_tf_bars(aggregate_1m_to_tf(_today_bars(bars_1m, now), tf), tf, now)
    window = closed[-n:] if len(closed) >= n else []
    ready = len(window) == n
    high = max((bar.high for bar in window), default=0.0) if ready else 0.0
    low = min((bar.low for bar in window), default=0.0) if ready else 0.0
    gap = high - low if ready else 0.0
    tradable = ready and gap + 1e-9 >= float(min_gap)
    first_entry = first_entry_label(n, tf)
    return {
        "ok": True,
        "ready": ready,
        "tradable": tradable,
        "high": round(high, 2) if ready else None,
        "low": round(low, 2) if ready else None,
        "mid": round((high + low) / 2, 2) if ready else None,
        "gap": round(gap, 2) if ready else None,
        "closedCount": len(closed),
        "candles": n,
        "tfMinutes": tf,
        "minGap": float(min_gap),
        "firstEntry": first_entry,
        "sessionDate": now.date().isoformat(),
        "firstBar": _label(window[0].minute_key) if ready else None,
        "lastBar": _label(window[-1].minute_key) if ready else None,
        "error": None if ready else f"{n} closed {tf}-minute bars are required. The first entry is {first_entry}.",
    }


def _parse_expiry_date(raw: str) -> date | None:
    text = str(raw or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def sensex_futures_from_master(master_text: str) -> list[dict[str, Any]]:
    """Monthly BSEFO SENSEX futures. SENSEX50 and options are left out."""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in str(master_text or "").splitlines():
        if "|1|SENSEX|" not in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 17:
            continue
        segment = parts[0].upper()
        if segment not in ("BSEFO", "12"):
            continue
        if parts[2] not in ("1", "FUTIDX", "FUTURE", "FUTURES"):
            continue
        if parts[3].upper() != "SENSEX" or parts[5].upper() != "IF":
            continue
        expiry = _parse_expiry_date(parts[16])
        if expiry is None or not parts[1].isdigit():
            continue
        instrument_id = int(parts[1])
        if instrument_id in seen:
            continue
        seen.add(instrument_id)
        found.append(
            {
                "segment": BSEFO_SEGMENT,
                "instrumentId": instrument_id,
                "symbol": parts[4] or f"SENSEX {expiry.isoformat()}",
                "expiry": expiry.isoformat(),
            }
        )
    return found


def pick_active_sensex_future(master_text: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Nearest SENSEX future that has not passed its expiry date. Expiry day is still active."""
    now = (now or datetime.now(IST)).astimezone(IST)
    today = now.date()
    live = []
    for row in sensex_futures_from_master(master_text):
        expiry = _parse_expiry_date(str(row.get("expiry") or ""))
        if expiry is None or expiry < today:
            continue
        live.append(row)
    if not live:
        return None
    live.sort(key=lambda row: (str(row["expiry"]), int(row["instrumentId"])))
    return live[0]


def _master_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "cache"


def load_bsefo_master_text() -> str:
    cache_dir = _master_dir()
    if not cache_dir.exists():
        return ""
    files = sorted(cache_dir.glob("instruments_master_*BSEFO*.txt"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in files[:4]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "|1|SENSEX|" in text:
            return text
    return ""


def active_sensex_future(now: datetime | None = None, master_text: str | None = None) -> dict[str, Any] | None:
    """Cached for the session date. A miss is retried sooner than a hit."""
    now = (now or datetime.now(IST)).astimezone(IST)
    day = now.date().isoformat()
    if master_text is None:
        with _LOCK:
            cached = _CONTRACT_CACHE.get("contract")
            age = time.monotonic() - float(_CONTRACT_CACHE.get("at") or 0)
            limit = CONTRACT_CACHE_SECONDS if isinstance(cached, dict) else CONTRACT_MISS_SECONDS
            if _CONTRACT_CACHE.get("day") == day and age < limit:
                return cached if isinstance(cached, dict) else None
        master_text = load_bsefo_master_text()
    contract = pick_active_sensex_future(master_text or "", now)
    if contract is None and not str(master_text or "").strip():
        return None
    with _LOCK:
        _CONTRACT_CACHE["at"] = time.monotonic()
        _CONTRACT_CACHE["day"] = day
        _CONTRACT_CACHE["contract"] = contract
    return contract


def _contract_fields(contract: dict[str, Any] | None) -> dict[str, Any]:
    if not contract:
        return {
            "contract": None,
            "contractExpiry": None,
            "exchangeSegment": None,
            "exchangeInstrumentID": None,
        }
    return {
        "contract": contract.get("symbol"),
        "contractExpiry": contract.get("expiry"),
        "exchangeSegment": contract.get("segment"),
        "exchangeInstrumentID": contract.get("instrumentId"),
    }


def _fetch_contract_bars(contract: dict[str, Any]) -> list[OhlcBar]:
    return fetch_session_1m_bars(
        _xts_client(),
        exchange_segment=int(contract["segment"]),
        exchange_instrument_id=int(contract["instrumentId"]),
    )


def get_range(
    *,
    now: datetime | None = None,
    bars: list[OhlcBar] | None = None,
    tf_minutes: int = TF_MINUTES,
    candles: int = CANDLES,
    min_gap: float = MIN_GAP,
    contract: dict[str, Any] | None = None,
    master_text: str | None = None,
) -> dict[str, Any]:
    """Today's rolling range from the active monthly SENSEX future. Pass `bars` to skip the broker fetch."""
    now = (now or datetime.now(IST)).astimezone(IST)
    tf = int(tf_minutes)
    n = int(candles)
    gap = float(min_gap)
    if bars is not None:
        payload = build_cd_range(bars, now, tf_minutes=tf, candles=n, min_gap=gap)
        payload.update(_contract_fields(contract))
        return payload
    if contract is None:
        contract = active_sensex_future(now, master_text)
    fields = _contract_fields(contract)
    if now.weekday() >= 5:
        payload = build_cd_range([], now, tf_minutes=tf, candles=n, min_gap=gap)
        payload["error"] = "Cash market is closed."
        payload.update(fields)
        return payload
    if not contract:
        return {
            "ok": False,
            "ready": False,
            "tradable": False,
            "high": None,
            "low": None,
            "mid": None,
            "gap": None,
            "closedCount": 0,
            "sessionDate": now.date().isoformat(),
            "firstBar": None,
            "lastBar": None,
            "error": "Current month SENSEX futures contract was not found.",
            **fields,
        }
    day = now.date().isoformat()
    cache_key = f"{day}|{tf}|{n}|{gap}|{contract.get('instrumentId')}"
    with _LOCK:
        cached = _CACHE.get("payload")
        age = time.monotonic() - float(_CACHE.get("at") or 0)
        if _CACHE.get("day") == cache_key and isinstance(cached, dict) and age < CACHE_SECONDS:
            return cached
    try:
        fetched = _fetch_contract_bars(contract)
    except Exception as exc:
        return {
            "ok": False,
            "ready": False,
            "tradable": False,
            "high": None,
            "low": None,
            "mid": None,
            "gap": None,
            "closedCount": 0,
            "sessionDate": day,
            "firstBar": None,
            "lastBar": None,
            "error": str(exc) or "SENSEX futures range is not in yet.",
            **fields,
        }
    payload = build_cd_range(fetched, now, tf_minutes=tf, candles=n, min_gap=gap)
    payload.update(fields)
    with _LOCK:
        _CACHE["at"] = time.monotonic()
        _CACHE["day"] = cache_key
        _CACHE["payload"] = payload
    return payload
