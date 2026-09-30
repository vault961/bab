import json
import os
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from holiday_calendar import check_workday, check_holiday
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError


ENV_PATH = Path(__file__).resolve().with_name(".env")
load_dotenv(dotenv_path=ENV_PATH)


def _read_slack_token(name: str, prefix: str) -> str:
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        raise SystemExit(f"환경 변수 {name} 을 설정하세요. ({ENV_PATH} 확인)")
    s = raw.strip()
    try:
        s.encode("latin-1")
    except UnicodeEncodeError:
        raise SystemExit(
            f"{name} 에 한글이나 특수문자가 섞였습니다. "
            "Slack API 페이지에서 복사한 토큰만 넣고, '여기에…' 같은 예시 문구는 지우세요."
        )
    if not s.startswith(prefix):
        raise SystemExit(f"{name} 은(는) {prefix} 로 시작해야 합니다.")
    return s


SLACK_BOT_TOKEN = _read_slack_token("SLACK_BOT_TOKEN", "xoxb-")
SLACK_APP_TOKEN = _read_slack_token("SLACK_APP_TOKEN", "xapp-")

app = App(token=SLACK_BOT_TOKEN, token_verification_enabled=False)

DEFAULT_RECOMMENDATION_COUNT = 5
LUNCH_TITLE = "*오늘의 추천 식당* 🍽️"
OTHER_MEAL_OPTION = "따로 먹을게요"
VOTE_BAR_WIDTH = 14
KST = timezone(timedelta(hours=9), name="Asia/Seoul")
SCHEDULER_INTERVAL_SECONDS = 5
AUTO_VOTE_LEAD_MINUTES = 30


def _restaurants_json_path() -> str:
    env = os.environ.get("BAB_RESTAURANTS_JSON")
    if env and env.strip():
        return env.strip()
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "restaurants.json")


def _schedule_json_path() -> Path:
    env = os.environ.get("BAB_POLL_SCHEDULE_JSON")
    if env and env.strip():
        return Path(env.strip())
    return Path(__file__).resolve().with_name("poll_schedule.json")


def _poll_state_json_path() -> Path:
    env = os.environ.get("BAB_POLL_STATE_JSON")
    if env and env.strip():
        return Path(env.strip())
    return _schedule_json_path().with_name("poll_state.json")


def _parse_clock(value: object, field_name: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    for pattern in ("%H%M", "%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, pattern)
            return parsed.hour, parsed.minute, parsed.second
        except ValueError:
            pass
    raise ValueError(
        f"{field_name}은 HHMM, HH:MM 또는 HH:MM:SS 형식이어야 합니다: {text!r}"
    )


def load_schedule_config(force: bool = False) -> dict:
    global SCHEDULE_CACHE, SCHEDULE_CACHE_MTIME_NS

    path = _schedule_json_path()
    stat = path.stat()
    if (
        not force
        and SCHEDULE_CACHE is not None
        and SCHEDULE_CACHE_MTIME_NS == stat.st_mtime_ns
    ):
        return SCHEDULE_CACHE

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError("투표 설정의 최상위 값은 JSON 객체여야 합니다.")

    raw_channels = raw.get("channels", {})
    if not isinstance(raw_channels, dict):
        raise ValueError("channels는 채널 ID를 키로 사용하는 JSON 객체여야 합니다.")

    channels: dict[str, dict] = {}
    for channel_id, value in raw_channels.items():
        if not isinstance(value, dict):
            raise ValueError(f"채널 {channel_id} 설정은 JSON 객체여야 합니다.")
        channel_id = str(channel_id).strip()
        if not channel_id:
            raise ValueError("빈 채널 ID는 사용할 수 없습니다.")

        meal_clock = _parse_clock(value.get("meal_time"), f"{channel_id}.meal_time")
        count = value.get("recommendation_count", DEFAULT_RECOMMENDATION_COUNT)
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"{channel_id}.recommendation_count는 1 이상의 정수여야 합니다.")

        channels[channel_id] = {
            "enabled": bool(value.get("enabled", True)),
            "meal_clock": meal_clock,
            "recommendation_count": count,
        }

    SCHEDULE_CACHE = {"channels": channels}
    SCHEDULE_CACHE_MTIME_NS = stat.st_mtime_ns
    print(f"[scheduler] 설정을 읽었습니다: {path} ({len(channels)}개 채널)")
    return SCHEDULE_CACHE


def _clock_to_text(clock: tuple[int, int, int]) -> str:
    hour, minute, second = clock
    return f"{hour:02d}:{minute:02d}:{second:02d}" if second else f"{hour:02d}:{minute:02d}"


def _write_channel_schedule(channel_id: str, values: dict) -> None:
    global SCHEDULE_CACHE, SCHEDULE_CACHE_MTIME_NS

    path = _schedule_json_path()
    with CONFIG_LOCK:
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("투표 설정의 최상위 값은 JSON 객체여야 합니다.")
        channels = raw.get("channels", {})
        if not isinstance(channels, dict):
            raise ValueError("channels는 채널 ID를 키로 사용하는 JSON 객체여야 합니다.")
        channels[channel_id] = values

        output = {"channels": channels}
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
            f.write("\n")
        temporary.replace(path)
        SCHEDULE_CACHE = None
        SCHEDULE_CACHE_MTIME_NS = None


def _schedule_help_text(schedule: dict | None = None) -> str:
    lines = [
        "*밥봇 명령어*",
        "• `!밥` — 모든 명령어 안내",
        "• `!밥 투표` — 즉시 투표 시작",
        "• `!밥 투표 3` — 지정한 수만큼 후보를 뽑아 즉시 투표 시작",
        "• `!밥 시간 1200` — 식사시간 설정 (한국 시간)",
        "• `!밥 자동` — 공휴일을 제외한 평일 식사 30분 전 자동 투표 켜기/끄기",
        "• `!밥 후보` — 전체 식당 후보 확인",
    ]
    if schedule is None:
        lines.append("\n현재 채널에는 자동 투표 설정이 없습니다.")
    else:
        enabled_text = "켜짐" if schedule["enabled"] else "꺼짐"
        lines.extend(
            [
                "\n*현재 채널 설정*",
                f"• 식사시간: `{_clock_to_text(schedule['meal_clock'])}`",
                f"• 평일 자동 투표: *{enabled_text}*",
            ]
        )
    return "\n".join(lines)


def _handle_schedule_command(client, channel_id: str, user_id: str | None, text: str) -> None:
    parts = text.split()
    allowlist = _shutdown_allowlist()
    if allowlist and (not user_id or user_id not in allowlist):
        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text="투표 설정을 변경할 권한이 없습니다.",
        )
        return

    config = load_schedule_config(force=True)
    current = config["channels"].get(channel_id)

    if len(parts) == 3 and parts[1] == "시간":
        meal_clock = _parse_clock(parts[2], "식사 시간")
        values = {
            "enabled": current["enabled"] if current else True,
            "meal_time": _clock_to_text(meal_clock),
            "recommendation_count": (
                current["recommendation_count"]
                if current
                else DEFAULT_RECOMMENDATION_COUNT
            ),
        }
    elif len(parts) == 2 and parts[1] == "자동":
        if current is None:
            client.chat_postMessage(
                channel=channel_id,
                text="먼저 `!밥 시간 1200`처럼 식사시간을 설정해 주세요.",
            )
            return
        values = {
            "enabled": not current["enabled"],
            "meal_time": _clock_to_text(current["meal_clock"]),
            "recommendation_count": current["recommendation_count"],
        }
    else:
        client.chat_postMessage(channel=channel_id, text=_schedule_help_text(current))
        return

    _write_channel_schedule(channel_id, values)
    enabled_text = "켜짐" if values["enabled"] else "꺼짐"
    client.chat_postMessage(
        channel=channel_id,
        text=(
            "자동 투표 설정을 저장했습니다.\n"
            f"• 식사시간: `{values['meal_time']}`\n"
            f"• 투표 시작: 평일 식사 30분 전\n"
            f"• 평일 자동 투표: *{enabled_text}*"
        ),
    )


def _serialize_poll(poll_id: str, poll: dict) -> dict:
    return {
        "poll_id": poll_id,
        "choices": poll["choices"],
        "votes": {
            name: sorted(user_ids) for name, user_ids in poll["votes"].items()
        },
        "member_cap": poll.get("member_cap"),
        "channel_id": poll.get("channel_id"),
        "message_ts": poll.get("message_ts"),
        "end_at": poll.get("end_at"),
        "scheduled_date": poll.get("scheduled_date"),
        "ended": bool(poll.get("ended", False)),
    }


def save_poll_state() -> None:
    path = _poll_state_json_path()
    with POLLS_LOCK:
        data = {
            "version": 1,
            "polls": [_serialize_poll(poll_id, poll) for poll_id, poll in POLLS.items()],
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    temporary.replace(path)


def load_poll_state() -> None:
    global POLL_STATE_LOADED
    with POLLS_LOCK:
        if POLL_STATE_LOADED:
            return
        POLL_STATE_LOADED = True

    path = _poll_state_json_path()
    if not path.exists():
        return
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        rows = data.get("polls", []) if isinstance(data, dict) else []
        restored: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            poll_id = str(row.get("poll_id") or "")
            choices = row.get("choices")
            votes = row.get("votes")
            if not poll_id or not isinstance(choices, list) or not isinstance(votes, dict):
                continue
            restored[poll_id] = {
                "choices": [str(choice) for choice in choices],
                "votes": {
                    str(name): {str(user_id) for user_id in user_ids}
                    for name, user_ids in votes.items()
                    if isinstance(user_ids, list)
                },
                "member_cap": row.get("member_cap"),
                "channel_id": row.get("channel_id"),
                "message_ts": row.get("message_ts"),
                "end_at": row.get("end_at"),
                "scheduled_date": row.get("scheduled_date"),
                "ended": bool(row.get("ended", False)),
            }
        with POLLS_LOCK:
            POLLS.update(restored)
        print(f"[scheduler] 저장된 투표 {len(restored)}개를 복원했습니다: {path}")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        print(f"[scheduler][error] 투표 상태를 복원하지 못했습니다: {e}")


def load_restaurants() -> list[str]:
    path = _restaurants_json_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except OSError as e:
        raise SystemExit(f"레스토랑 파일을 열 수 없습니다: {path}\n{e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"JSON 형식 오류: {path}\n{e}")
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and "restaurants" in data:
        items = data["restaurants"]
    else:
        raise SystemExit(
            f"{path} 은 문자열 배열이거나 "
            '{{"restaurants": ["이름", ...]}} 형식이어야 합니다.'
        )
    out = [str(x).strip() for x in items if str(x).strip()]
    if not out:
        raise SystemExit("레스토랑 목록이 비어 있습니다.")
    return out


RESTAURANTS = load_restaurants()

POLLS: dict[str, dict] = {}
POLLS_LOCK = threading.RLock()
SCHEDULER_STOP_EVENT = threading.Event()
SCHEDULER_THREAD: threading.Thread | None = None
SCHEDULE_CACHE: dict | None = None
SCHEDULE_CACHE_MTIME_NS: int | None = None
POLL_STATE_LOADED = False
CONFIG_LOCK = threading.RLock()
EXCLUDED_MEMBER_CAP_STATUS_EMOJIS = {
    "🌴",
    "🏠",
    "🏡",
    ":palm_tree:",
    ":house:",
    ":house_with_garden:",
}


def _shutdown_allowlist() -> set[str]:
    raw = os.environ.get("BAB_SHUTDOWN_USER_IDS", "")
    return {x.strip() for x in raw.split(",") if x.strip()}


def _can_shutdown(user_id: str) -> bool:
    return user_id in _shutdown_allowlist()


def _schedule_process_exit(client, channel_id: str) -> None:
    try:
        client.chat_postMessage(
            channel=channel_id, text="봇 프로세스를 종료합니다."
        )
    except SlackApiError:
        pass

    def _exit_delayed() -> None:
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_exit_delayed, daemon=True).start()


UserInfo = dict[str, object]

USER_INFO_CACHE: dict[str, UserInfo] = {}


def _log_user_info(user_id: str, user_info: UserInfo, cached: bool = False) -> None:
    source = "cached" if cached else "api"
    print(
        f"[member-check][{source}] user={user_id} "
        f"name={user_info.get('name')!r} real_name={user_info.get('real_name')!r} "
        f"display_name={user_info.get('display_name')!r} "
        f"status_emoji={user_info.get('status_emoji')!r} "
        f"is_bot={user_info.get('is_bot')} is_app_user={user_info.get('is_app_user')} "
        f"is_workflow_bot={user_info.get('is_workflow_bot')} deleted={user_info.get('deleted')} "
        f"human={user_info.get('is_human')} "
        f"counts_toward_member_cap={user_info.get('counts_toward_member_cap')}"
    )


def _build_user_info(user: dict[str, object]) -> UserInfo:
    profile = user.get("profile") or {}
    if not isinstance(profile, dict):
        profile = {}

    is_bot = bool(user.get("is_bot", False))
    is_app_user = bool(user.get("is_app_user", False))
    is_workflow_bot = bool(user.get("is_workflow_bot", False))
    deleted = bool(user.get("deleted", False))
    status_emoji = profile.get("status_emoji")
    is_human = not (is_bot or is_app_user or is_workflow_bot or deleted)

    return {
        "name": user.get("name"),
        "real_name": user.get("real_name"),
        "display_name": profile.get("display_name"),
        "status_emoji": status_emoji,
        "is_bot": is_bot,
        "is_app_user": is_app_user,
        "is_workflow_bot": is_workflow_bot,
        "deleted": deleted,
        "is_human": is_human,
        "counts_toward_member_cap": (
            is_human and not _status_excluded_from_member_cap(status_emoji)
        ),
    }


def _unknown_user_info() -> UserInfo:
    return {
        "name": None,
        "real_name": None,
        "display_name": None,
        "status_emoji": None,
        "is_bot": False,
        "is_app_user": False,
        "is_workflow_bot": False,
        "deleted": False,
        "is_human": True,
        "counts_toward_member_cap": True,
    }


def _get_user_info(client, user_id: str, refresh: bool = False) -> UserInfo:
    cached = USER_INFO_CACHE.get(user_id)
    if cached is not None and not refresh:
        _log_user_info(user_id, cached, cached=True)
        return cached

    try:
        response = client.users_info(user=user_id)
        if response.get("ok"):
            user = response.get("user") or {}
            if isinstance(user, dict):
                user_info = _build_user_info(user)
                USER_INFO_CACHE[user_id] = user_info
                _log_user_info(user_id, user_info, cached=False)
                return user_info
    except SlackApiError as e:
        print(f"[member-check][error] user={user_id} exception={e}")

    user_info = _unknown_user_info()
    _log_user_info(user_id, user_info, cached=False)
    return user_info


def get_channel_member_count(client, channel_id: str) -> int | None:
    try:
        total_humans = 0
        cursor = None

        while True:
            r = client.conversations_members(
                channel=channel_id, cursor=cursor, limit=200
            )
            if not r.get("ok"):
                return None

            member_ids = r.get("members") or []
            for user_id in member_ids:
                if _counts_toward_member_cap(client, user_id):
                    total_humans += 1

            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

        return total_humans

    except SlackApiError as e:
        print(f"[member-check][error] Error fetching members: {e}")
        return None


def _status_excluded_from_member_cap(status_emoji: object) -> bool:
    if not status_emoji:
        return False
    return str(status_emoji).strip() in EXCLUDED_MEMBER_CAP_STATUS_EMOJIS


def _counts_toward_member_cap(client, user_id: str) -> bool:
    user_info = _get_user_info(client, user_id, refresh=True)
    return bool(user_info.get("counts_toward_member_cap", True))


def _extract_korean_name(display_name: str | None) -> str | None:
    """display_name에서 한글 이름만 추출합니다."""
    if not display_name:
        return None
    korean_part = display_name.split("/")[0].strip()
    return korean_part if korean_part else None


def _get_user_korean_name(client, user_id: str) -> str:
    """user_id에 해당하는 사용자의 한글 이름 반환"""
    user_info = _get_user_info(client, user_id)
    display_name = user_info.get("display_name")
    korean_name = _extract_korean_name(str(display_name)) if display_name else None
    if korean_name:
        return korean_name

    for field in ("real_name", "name"):
        value = user_info.get(field)
        if value:
            return str(value)

    return user_id


def render_bar(count: int, total: int, width: int = VOTE_BAR_WIDTH) -> str:
    if total <= 0:
        return "░" * width
    filled = min(width, int(round(width * count / total)))
    return "█" * filled + "░" * (width - filled)


def parse_count(text: str) -> tuple[int | None, str | None]:
    parts = text.split()
    if len(parts) >= 2 and parts[0].startswith("!"):
        try:
            return int(parts[1]), None
        except ValueError:
            return None, "숫자만 입력해 주세요. 예: `!밥 3`"

    if len(parts) == 1:
        try:
            return int(parts[0]), None
        except ValueError:
            return DEFAULT_RECOMMENDATION_COUNT, None

    return DEFAULT_RECOMMENDATION_COUNT, None


def fallback_text(chosen: list[str], votes: dict[str, set[str]], client=None) -> str:
    lines = [LUNCH_TITLE]
    for i, name in enumerate(chosen):
        voter_user_ids = votes.get(name, set())
        c = len(voter_user_ids)
        label = name if name == OTHER_MEAL_OPTION else f"{i + 1}. {name}"
        line = f"{label} ({c}표)"
        if name == OTHER_MEAL_OPTION and client is not None and voter_user_ids:
            voter_names = _voter_names_text(client, voter_user_ids)
            if voter_names:
                line += f" · {voter_names}"
        lines.append(line)
    return "\n".join(lines)


def candidate_list_text() -> str:
    lines = [f"*현재 추천 가능한 식당* ({len(RESTAURANTS)}곳)"]
    lines.extend(f"{i + 1}. {name}" for i, name in enumerate(RESTAURANTS))
    lines.append("\n투표를 시작하려면 `!밥 투표` 또는 `!밥 투표 숫자`를 입력해 주세요.")
    return "\n".join(lines)


def _vote_progress(count: int, max_count: int, member_cap: int | None) -> tuple[int, str]:
    if member_cap is not None:
        bar_total = max(member_cap, 1)
        if member_cap <= 0:
            return bar_total, f"{count}/{member_cap}명"

        pct = 100.0 * count / member_cap if count else 0.0
        return bar_total, f"{pct:.0f}% · {count}/{member_cap}명"

    relative = (count / max_count) if max_count > 0 else 0.0
    bar_total = max(max_count, 1)
    return bar_total, f"{100.0 * relative:.0f}% · {count}표"


def _voter_names_text(client, user_ids: set[str]) -> str:
    names = sorted(_get_user_korean_name(client, uid) for uid in user_ids)
    return ", ".join(names)


def _all_voter_user_ids(votes: dict[str, set[str]]) -> set[str]:
    user_ids: set[str] = set()
    for restaurant_votes in votes.values():
        user_ids.update(restaurant_votes)
    return user_ids


def _is_poll_complete(poll: dict) -> bool:
    member_cap = poll.get("member_cap")
    if not isinstance(member_cap, int) or member_cap <= 0:
        return False
    return len(_all_voter_user_ids(poll["votes"])) >= member_cap


def _winning_choice(poll: dict) -> str:
    candidates = [
        choice for choice in poll["choices"] if choice != OTHER_MEAL_OPTION
    ]
    if not candidates:
        raise ValueError("선정할 식당 후보가 없습니다.")
    return max(candidates, key=lambda choice: len(poll["votes"].get(choice, set())))


def build_blocks(
    client,
    chosen: list[str],
    poll_id: str,
    votes: dict[str, set[str]],
    acting_user_id: str | None,
    member_cap: int | None,
    voting_open: bool = True,
) -> list[dict]:
    cap = member_cap if member_cap is not None and member_cap >= 0 else None
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": LUNCH_TITLE},
        }
    ]
    max_cnt = max((len(votes.get(n, set())) for n in chosen), default=0)
    for i, name in enumerate(chosen):
        voter_user_ids = votes.get(name, set())
        cnt = len(voter_user_ids)
        voted = bool(acting_user_id and acting_user_id in voter_user_ids)
        bar_total, suffix = _vote_progress(cnt, max_cnt, cap)
        bar = render_bar(cnt, bar_total)
        value = json.dumps({"p": poll_id, "r": name}, ensure_ascii=False)

        label = name if name == OTHER_MEAL_OPTION else f"{i + 1}. {name}"
        block_text = f"*{label}* · *{cnt}표*\n`{bar}` {suffix}"
        if name == OTHER_MEAL_OPTION and voter_user_ids:
            voter_names = _voter_names_text(client, voter_user_ids)
            if voter_names:
                block_text += f"\n*선택자:* {voter_names}"

        section = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": block_text,
            },
        }
        if voting_open:
            section["accessory"] = {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": "취소" if voted else "투표",
                    "emoji": True,
                },
                "action_id": "vote_toggle",
                "value": value,
            }
        blocks.append(section)
    all_voter_text = _voter_names_text(client, _all_voter_user_ids(votes))
    if all_voter_text:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"*투표 참여자:* {all_voter_text}",
                    }
                ],
            }
        )
    return blocks


def _save_poll_state_safely() -> None:
    try:
        save_poll_state()
    except OSError as e:
        print(f"[scheduler][error] 투표 상태를 저장하지 못했습니다: {e}")


def finalize_poll(client, poll_id: str, reason: str) -> bool:
    with POLLS_LOCK:
        poll = POLLS.get(poll_id)
        if not poll or poll.get("ended"):
            return False
        poll["ended"] = True
        winner = _winning_choice(poll)
        channel_id = poll.get("channel_id") or poll_id.split(":", 1)[0]
        message_ts = poll.get("message_ts") or poll_id.split(":", 1)[-1]
        choices = poll["choices"]
        votes = poll["votes"]
        member_cap = poll.get("member_cap")

    _save_poll_state_safely()

    blocks = build_blocks(
        client,
        choices,
        poll_id,
        votes,
        None,
        member_cap,
        voting_open=False,
    )
    try:
        client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=blocks,
            text=fallback_text(choices, votes, client),
        )
    except SlackApiError as e:
        print(f"[scheduler][error] 종료된 투표 메시지를 갱신하지 못했습니다: {e}")

    prefix = "모든 참여자의 투표가 완료되었습니다." if reason == "completed" else "투표 시간이 종료되었습니다."
    result_text = f"{prefix}\n*선정 메뉴: {winner}* 🎉"
    client.chat_postMessage(
        channel=channel_id,
        text=result_text,
        blocks=[
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": result_text},
            }
        ],
    )
    print(f"[scheduler] poll={poll_id} reason={reason} winner={winner}")
    return True


def _schedule_window(now: datetime, schedule: dict) -> tuple[datetime, datetime]:
    meal_hour, meal_minute, meal_second = schedule["meal_clock"]
    meal_at = now.replace(
        hour=meal_hour,
        minute=meal_minute,
        second=meal_second,
        microsecond=0,
    )
    start_at = meal_at - timedelta(minutes=AUTO_VOTE_LEAD_MINUTES)
    return start_at, meal_at


def _active_window(now: datetime, schedule: dict) -> tuple[datetime, datetime] | None:
    today_window = _schedule_window(now, schedule)
    if today_window[0] <= now < today_window[1]:
        return today_window
    tomorrow_window = _schedule_window(now + timedelta(days=1), schedule)
    if tomorrow_window[0] <= now < tomorrow_window[1]:
        return tomorrow_window
    return None


def _has_scheduled_poll(channel_id: str, scheduled_date: str) -> bool:
    with POLLS_LOCK:
        return any(
            poll.get("channel_id") == channel_id
            and poll.get("scheduled_date") == scheduled_date
            for poll in POLLS.values()
        )


def _run_scheduler_cycle(client, now: datetime | None = None) -> None:
    config = load_schedule_config()
    current = now.astimezone(KST) if now is not None else datetime.now(KST)

    for channel_id, schedule in config["channels"].items():
        if not schedule["enabled"]:
            continue
        window = _active_window(current, schedule)
        if window is None:
            continue
        start_at, end_at = window
        if end_at.weekday() >= 5:
            continue
        scheduled_date = end_at.date().isoformat()
        if _has_scheduled_poll(channel_id, scheduled_date):
            continue
        is_workday, _ = check_workday(end_at.date())
        if is_workday is not True:
            continue
        _create_poll(
            client,
            channel_id,
            schedule["recommendation_count"],
            end_at=end_at,
            scheduled_date=scheduled_date,
        )

    with POLLS_LOCK:
        active = [
            (poll_id, poll.get("end_at"))
            for poll_id, poll in POLLS.items()
            if not poll.get("ended") and poll.get("end_at")
        ]

    for poll_id, end_at_text in active:
        try:
            end_at = datetime.fromisoformat(str(end_at_text)).astimezone(KST)
        except ValueError:
            print(f"[scheduler][error] 잘못된 종료 시각입니다: poll={poll_id} value={end_at_text}")
            continue
        if current >= end_at:
            finalize_poll(client, poll_id, "deadline")


def _scheduler_loop(client) -> None:
    while not SCHEDULER_STOP_EVENT.is_set():
        try:
            _run_scheduler_cycle(client)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            print(f"[scheduler][error] {e}")
        except SlackApiError as e:
            print(f"[scheduler][slack-error] {e}")
        SCHEDULER_STOP_EVENT.wait(SCHEDULER_INTERVAL_SECONDS)


def start_poll_scheduler(client=None) -> None:
    global SCHEDULER_THREAD
    with POLLS_LOCK:
        if SCHEDULER_THREAD is not None and SCHEDULER_THREAD.is_alive():
            return
        load_poll_state()
        SCHEDULER_STOP_EVENT.clear()
        SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop,
            args=(client or app.client,),
            name="bab-poll-scheduler",
            daemon=True,
        )
        SCHEDULER_THREAD.start()


def stop_poll_scheduler() -> None:
    SCHEDULER_STOP_EVENT.set()


@app.message("^!종료\\s*$")
def shutdown_bang(message, client):
    uid = message.get("user")
    channel_id = message["channel"]
    if not uid:
        return
    if not _shutdown_allowlist():
        client.chat_postEphemeral(
            channel=channel_id,
            user=uid,
            text="종료 기능을 쓰려면 서버 환경 변수 BAB_SHUTDOWN_USER_IDS 에 "
            "허용할 Slack 사용자 ID(U로 시작)를 넣으세요.",
        )
        return
    if not _can_shutdown(uid):
        client.chat_postEphemeral(
            channel=channel_id,
            user=uid,
            text="종료 권한이 없습니다.",
        )
        return
    _schedule_process_exit(client, channel_id)


@app.message(r"^!테스트(?:\s|$)")
def holiday_test_by_bang(message, client):
    parts = message.get("text", "").split()
    target = datetime.now(KST).date()
    if len(parts) > 1:
        value = parts[1]
        try:
            if len(parts) != 2 or len(value) != 8 or not value.isascii() or not value.isdigit():
                raise ValueError("invalid date")
            target = datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            client.chat_postMessage(
                channel=message["channel"],
                text="올바른 날짜를 입력해 주세요. 사용법: `!테스트` 또는 `!테스트 20261003`",
            )
            return
    holiday, reason = check_holiday(target)
    weekday = "월화수목금토일"[target.weekday()]
    heading = f"*{target.isoformat()} ({weekday}) · 한국 시간 기준*"
    if holiday is None:
        result = f"공휴일 여부를 확인하지 못했습니다. {reason}. 잠시 후 다시 시도해 주세요."
    else:
        holiday_text = f"공휴일 ({reason})" if holiday else "공휴일 아님"
        workday = target.weekday() < 5 and not holiday
        result = (
            f"• 공휴일 API 결과: {holiday_text}\n"
            f"• 주말 여부: {'주말' if target.weekday() >= 5 else '평일'}\n"
            f"• 근무 평일 판정: {'예' if workday else '아니오'} (주말·공휴일 기준)"
        )
    client.chat_postMessage(channel=message["channel"], text=f"{heading}\n{result}")


@app.message("^!밥")
def lunch_recommend_by_bang(message, client):
    fake_body = {
        "channel_id": message["channel"],
        "user": message.get("user"),
        "text": message.get("text"),
        "ts": message.get("ts"),
    }
    lunch_logic(fake_body, client)


@app.action("vote_toggle")
def handle_vote(ack, body, client):
    ack()
    channel_id = body["channel"]["id"]
    ts = body["message"]["ts"]
    user_id = body["user"]["id"]
    action = body["actions"][0]
    try:
        payload = json.loads(action["value"])
        poll_id = payload["p"]
        restaurant = payload["r"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return

    with POLLS_LOCK:
        poll = POLLS.get(poll_id)
        if not poll:
            return
        if poll.get("ended"):
            client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text="이미 종료된 투표입니다.",
            )
            return
        poll_channel_id = poll.get("channel_id") or channel_id
        if poll_channel_id != channel_id:
            return

        votes = poll["votes"]
        if restaurant not in votes:
            return

        bucket = votes[restaurant]
        if user_id in bucket:
            bucket.remove(user_id)
        else:
            bucket.add(user_id)

        chosen = poll["choices"]
        cap = poll.get("member_cap")

    if cap is None:
        cap = get_channel_member_count(client, channel_id)
        with POLLS_LOCK:
            poll["member_cap"] = cap

    _save_poll_state_safely()

    # 마감 스레드가 먼저 poll을 종료했다면 열린 버튼으로 되돌리지 않습니다.
    with POLLS_LOCK:
        if poll.get("ended"):
            return
        blocks = build_blocks(client, chosen, poll_id, votes, None, cap)
        client.chat_update(
            channel=channel_id,
            ts=ts,
            blocks=blocks,
            text=fallback_text(chosen, votes, client),
        )


@app.event("message")
def handle_message_events(body, logger):
    logger.info(body)


def _create_poll(
    client,
    channel_id: str,
    recommendation_count: int,
    end_at: datetime | None = None,
    scheduled_date: str | None = None,
) -> str:
    if end_at is None:
        current = datetime.now(KST)
        schedule = load_schedule_config()["channels"].get(channel_id)
        if schedule is None:
            client.chat_postMessage(
                channel=channel_id,
                text="먼저 `!밥 시간 1200`처럼 식사시간을 설정해 주세요.",
            )
            return ""
        _, end_at = _schedule_window(current, schedule)
        if current >= end_at:
            client.chat_postMessage(
                channel=channel_id,
                text="오늘 식사시간이 이미 지났습니다. `!밥 시간 HHMM`으로 이후 시각을 설정해 주세요.",
            )
            return ""
    if scheduled_date is None:
        scheduled_date = datetime.now(KST).date().isoformat()
    count = max(1, min(recommendation_count, len(RESTAURANTS)))
    chosen = random.sample(RESTAURANTS, count)
    choices = chosen + [OTHER_MEAL_OPTION]

    loading = client.chat_postMessage(
        channel=channel_id,
        text=f"{LUNCH_TITLE} …",
    )
    ts = loading["ts"]
    poll_id = f"{channel_id}:{ts}"
    empty_votes = {name: set() for name in choices}
    member_cap = get_channel_member_count(client, channel_id)
    with POLLS_LOCK:
        POLLS[poll_id] = {
            "choices": choices,
            "votes": empty_votes,
            "member_cap": member_cap,
            "channel_id": channel_id,
            "message_ts": ts,
            "end_at": end_at.isoformat() if end_at is not None else None,
            "scheduled_date": scheduled_date,
            "ended": False,
        }
    _save_poll_state_safely()

    blocks = build_blocks(client, choices, poll_id, empty_votes, None, member_cap)
    client.chat_update(
        channel=channel_id,
        ts=ts,
        blocks=blocks,
        text=fallback_text(choices, empty_votes, client),
    )
    if end_at is not None:
        client.chat_postMessage(
            channel=channel_id,
            text=f"투표가 시작되었습니다. 마감 시각은 {end_at.strftime('%H:%M')}입니다.",
        )
    return poll_id


def lunch_logic(body, client):
    channel_id = body["channel_id"]
    text = body.get("text", "").strip()

    if text == "!밥":
        try:
            schedule = load_schedule_config(force=True)["channels"].get(channel_id)
            client.chat_postMessage(
                channel=channel_id,
                text=_schedule_help_text(schedule),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            client.chat_postMessage(channel=channel_id, text=f"설정을 읽지 못했습니다: {e}")
        return

    if text.startswith("!밥 시간") or text.startswith("!밥 자동"):
        try:
            _handle_schedule_command(
                client,
                channel_id,
                body.get("user"),
                text,
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            client.chat_postMessage(
                channel=channel_id,
                text=f"설정을 저장하지 못했습니다: {e}",
            )
        return

    if text == "!밥 후보":
        client.chat_postMessage(channel=channel_id, text=candidate_list_text())
        return

    parts = text.split()
    if len(parts) >= 2 and parts[1] == "투표":
        if len(parts) == 2:
            n = DEFAULT_RECOMMENDATION_COUNT
        elif len(parts) == 3:
            try:
                n = int(parts[2])
            except ValueError:
                client.chat_postMessage(
                    channel=channel_id,
                    text="후보 수는 숫자로 입력해 주세요. 예: `!밥 투표 3`",
                )
                return
        else:
            client.chat_postMessage(channel=channel_id, text=_schedule_help_text())
            return

        if n < 1:
            client.chat_postMessage(
                channel=channel_id, text="1 이상의 숫자를 입력해 주세요."
            )
            return
        _create_poll(client, channel_id, n)
        return

    try:
        schedule = load_schedule_config(force=True)["channels"].get(channel_id)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        schedule = None
    client.chat_postMessage(channel=channel_id, text=_schedule_help_text(schedule))


if __name__ == "__main__":
    start_poll_scheduler()
    handler = SocketModeHandler(app, SLACK_APP_TOKEN)
    try:
        handler.start()
    finally:
        stop_poll_scheduler()
