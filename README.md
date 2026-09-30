# 밥봇 🍚

> 인류는 아직 점심 메뉴도 자동으로 정하지 못했습니다.
> 그래서 봇한테 시켰습니다.

Slack 채널에서 점심 후보를 뽑고 투표하는 작은 봇입니다.

대단한 인공지능은 없고 `restaurants.json`에서 무작위로 뽑습니다.
취향 분석도 없습니다. 어제 먹은 메뉴가 또 나오면 운명입니다.

## 하는 일

- 평일 식사시간 30분 전에 자동으로 투표를 엽니다.
- 식사시간이 되면 투표를 닫고 가장 많은 표를 받은 메뉴를 알려줍니다.
- 주말과 공휴일에는 쉽니다. 봇도 쉽니다.
- `따로 먹을게요`를 고를 수 있습니다. 민주주의입니다.
- 동점이면 후보 목록에서 위에 있는 메뉴가 이깁니다. 완벽하게 공정하지는 않습니다.

## 필요한 것

- Python 3.12 권장
- Slack Bot Token
- Slack App Token
- 한국천문연구원 특일 정보 API 서비스 키
- 점심을 먹겠다는 의지

## 설치

```powershell
py -3.12 -m pip install -r requirements.txt
```

`.env.example`을 참고해서 프로젝트 폴더에 `.env`를 만듭니다.

```env
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_APP_TOKEN=xapp-your-app-token
KASI_SERVICE_KEY=your-public-data-service-key
```

실제 토큰이 들어 있는 `.env`는 Git에 올리지 마세요.
점심 메뉴는 공개돼도 되지만 토큰은 안 됩니다.

## 설정

`poll_schedule.example.json`을 복사해서 `poll_schedule.json`을 만듭니다.

```json
{
  "channels": {
    "C0123456789": {
      "enabled": true,
      "meal_time": "12:00",
      "recommendation_count": 5
    }
  }
}
```

식당 목록은 `restaurants.json`에 적습니다.

```json
{
  "restaurants": [
    "김치찌개",
    "돈까스",
    "오늘은 굶기"
  ]
}
```

마지막 후보는 권장하지 않습니다.

## 실행

콘솔에서 실행:

```powershell
py -3.12 bab.py
```

Windows 트레이 앱으로 실행:

```powershell
pyw.exe bab_app.pyw
```

트레이 앱은 창을 최소화하면 알림 영역으로 숨습니다.
아이콘을 더블 클릭하면 다시 나타납니다. 사라진 게 아니라 숨어 있는 겁니다.

## 명령어

| 명령어 | 설명 |
|---|---|
| `!밥` | 도움말과 현재 채널 설정을 봅니다. |
| `!밥 투표` | 지금 바로 투표를 시작합니다. |
| `!밥 투표 3` | 후보 3개로 투표를 시작합니다. |
| `!밥 시간 1200` | 식사시간을 12:00으로 설정합니다. |
| `!밥 자동` | 평일 자동 투표를 켜거나 끕니다. |
| `!밥 후보` | 등록된 식당 후보를 전부 봅니다. |
| `!테스트` | 오늘이 휴일인지 확인합니다. |

더 자세한 예약 설정은 [POLL_SCHEDULE.md](POLL_SCHEDULE.md)를 참고하세요.

## 자동 생성되는 파일

아래 파일은 실행 중 알아서 생기며 Git에 올리지 않습니다.

- `.env` — 진짜 토큰이 들어 있습니다. 매우 중요합니다.
- `poll_state.json` — 진행 중인 투표 상태입니다.
- `holiday_cache.json` — 공휴일 조회 캐시입니다.
- `__pycache__/` — 파이썬이 뭔가 열심히 했다는 흔적입니다.

## 결과 예시

```text
투표 시간이 종료되었습니다.
선정 메뉴: 김치찌개 🎉
```

맛이 없으면 봇에게 항의할 수 있습니다.
봇은 듣지 않습니다.
