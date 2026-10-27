# 회사 휴일 캘린더 연동

## 설정

Exchange Online 사서함의 회사 휴일 전용 캘린더를 사용한다. 일반 회의와 휴가는 넣지 않는다. Outlook 구독형 국가 휴일 캘린더는 실제 Graph 조회 가능 여부를 먼저 확인한다.

서버용 Entra 애플리케이션에 읽기 전용 Graph application 권한을 부여한다. `calendarView`의 최소 권한 `Calendars.ReadBasic`부터 실제 테넌트에서 검증한다. 관리자 동의 및 Exchange의 대상 사서함 접근 제한을 구성한다. 로그인용 OIDC scope는 변경하지 않는다.

다음 환경변수를 설정한 뒤 앱 컨테이너를 재생성한다. 비밀값은 저장소에 커밋하지 않는다.

- `HOLIDAY_TENANT_ID`: 테넌트 UUID
- `HOLIDAY_CLIENT_ID`: 연동용 앱 ID
- `HOLIDAY_CLIENT_SECRET`: 연동용 앱 비밀값
- `HOLIDAY_MAILBOX`: 원본 사서함 ID 또는 UPN
- `HOLIDAY_CALENDAR_ID`: 원본 캘린더 ID

`python manage.py migrate` 후 관리자의 기초정보 화면에서 **MS 캘린더 동기화**를 누르거나 `python manage.py sync_holidays`를 실행한다.

## 매일 자동 실행

기본 Compose는 `holidays` 프로필의 전용 워커를 제공한다. 연결 설정 후 다음 명령으로 켜면 서울 시간 매일 03시 이후 하루 한 번 성공할 때까지 실행하고, 실패하면 5분 후 재시도한다.

```sh
docker compose --profile holidays up -d holiday-worker
```

SSH 배포 스크립트 사용 시 첫 활성화에 `python3 scripts/deploy_nas.py --host oms-nas --password --holidays`를 사용한다. 이후 일반 배포는 실행 중인 워커를 자동으로 중지·교체·재시작한다. Cloudflare 배포를 수동 관리한다면 위 Compose 명령에 `-f compose.cloudflare.yaml`을 추가한다.

외부 스케줄러를 쓰려면 워커 대신 Synology 작업 스케줄러 등 배포 환경의 스케줄러에서 매일 03:00(Asia/Seoul)에 다음 명령을 실행하도록 등록한다. 프로젝트 경로와 Compose 프로젝트 옵션은 실제 배포에 맞춘다.

```sh
docker compose exec -T app python manage.py sync_holidays
```

동기화 명령은 실패 시 0이 아닌 종료 코드를 반환한다. 네트워크/429/서버 오류를 최대 3회 시도하며, 실행 잠금은 비정상 종료 시 2시간 후 만료한다. 마지막 성공·오류·휴일 수는 기초정보 화면에서 확인한다. 실제 운영 스케줄러와 테넌트 연결은 배포 환경에서 설정해야 한다.

## 반영 규칙

전년도 1월 1일부터 다음 연도 12월 31일까지 수집한다. 서울 시간의 종일 일정만 반영하며, 반복 일정의 발생분·예외와 다일 일정(종료일 제외)을 처리한다. 취소 일정은 제외한다. 전체 페이지 수집과 데이터 검증이 끝나기 전에는 기존 휴일을 변경하지 않는다.

같은 날짜의 여러 원본은 별도로 보존하고 날짜별 휴일은 하나로 집계한다. 수동 등록 휴일은 우선하고, 동기화 휴일을 화면에서 수정하면 수동 휴일로 보존한다. 원본 캘린더를 교체하면 조회 기간 내 종전 연동 원본을 대체한다. 수동 휴일과 조회 범위 밖 휴일은 삭제하지 않는다.

기존 실적의 시간과 감사 이력은 자동 변경하지 않는다. 과거 휴일을 바꾼 경우 해당 날짜의 실적을 확인하고, 필요한 기록만 기존 실적 수정 화면에서 변경 사유를 작성해 저장한다.

공식 API: [calendarView](https://learn.microsoft.com/en-us/graph/api/calendar-list-calendarview?view=graph-rest-1.0).
