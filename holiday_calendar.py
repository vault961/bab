"""KASI public holidays. Unknown status must never be treated as a workday."""
import json
import os
import time
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree as ET

KST = timezone(timedelta(hours=9))
ENDPOINT = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
CACHE_PATH = Path(__file__).resolve().with_name("holiday_cache.json")
_cache = None
_retry_after = {}
_lock = threading.RLock()


def _fetch_month(year: int, month: int) -> dict[str, str]:
    key = os.environ.get("KASI_SERVICE_KEY", "").strip()
    if not key:
        raise ValueError("missing service key")
    holidays = {}
    page = 1
    received = 0
    while True:
        query = urlencode({"ServiceKey": key, "solYear": year,
                           "solMonth": f"{month:02d}", "numOfRows": 100,
                           "pageNo": page})
        with urlopen(ENDPOINT + "?" + query, timeout=10) as response:
            root = ET.fromstring(response.read())
        if root.findtext("./header/resultCode") != "00":
            raise ValueError("API returned failure")
        body = root.find("body")
        if body is None:
            raise ValueError("missing response body")
        total = int(body.findtext("totalCount", "-1"))
        items = body.findall("./items/item")
        if total < 0 or (not items and received < total):
            raise ValueError("incomplete response")
        for item in items:
            raw_date = item.findtext("locdate", "")
            parsed = datetime.strptime(raw_date, "%Y%m%d").date()
            flag = item.findtext("isHoliday")
            if parsed.year != year or parsed.month != month or flag not in {"Y", "N"}:
                raise ValueError("invalid holiday record")
            if flag == "Y":
                holidays[parsed.isoformat()] = item.findtext("dateName", "공휴일")
        received += len(items)
        if received >= total:
            return holidays
        page += 1


def check_holiday(target: date) -> tuple[bool | None, str]:
    """Return the API holiday status, including for weekend dates."""
    with _lock:
        return _check_holiday(target)


def _check_holiday(target: date) -> tuple[bool | None, str]:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, ValueError):
            _cache = {}
    month_key = target.strftime("%Y-%m")
    today = datetime.now(KST).date().isoformat()
    entry = _cache.get(month_key)
    if not (isinstance(entry, dict) and entry.get("checked_on") == today
            and isinstance(entry.get("holidays"), dict)):
        if time.monotonic() < _retry_after.get(month_key, 0):
            return None, "공휴일 조회 재시도 대기"
        try:
            entry = {"checked_on": today, "holidays": _fetch_month(target.year, target.month)}
        except Exception:
            # Never log exception text: transport exceptions may contain the service key URL.
            _retry_after[month_key] = time.monotonic() + 300
            print("[holiday] 공휴일 조회 실패: 자동 투표 보류, 5분 후 재시도")
            return None, "공휴일 조회 실패"
        _cache[month_key] = entry
        try:
            temporary = CACHE_PATH.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(_cache, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            temporary.replace(CACHE_PATH)
        except OSError:
            print("[holiday] 파일 캐시 저장 실패: 메모리 캐시 사용")
    name = entry["holidays"].get(target.isoformat())
    return (True, name) if name else (False, "공휴일 아님")


def check_workday(target: date) -> tuple[bool | None, str]:
    """Return True for weekdays without public holidays, None on lookup failure."""
    if target.weekday() >= 5:
        return False, "주말"
    holiday, reason = check_holiday(target)
    if holiday is None:
        return None, reason
    return (False, reason) if holiday else (True, "공휴일이 아닌 평일")
