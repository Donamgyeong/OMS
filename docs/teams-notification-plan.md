# Teams 결재 알림 계획 및 구현

작성일: 2026-10-26. Teams-managed bot을 이용해 결재 요청과 처리 결과를 대상자의 Teams 개인 채팅으로 전달한다. API 등록과 실제 발송은 아직 수행하지 않았다.

## 1. 적용 범위

| 사건 | 수신자 | 종류 |
| --- | --- | --- |
| 제출·재제출 후 승인 대기 | 계획에 저장된 지정 승인자 | requested |
| 승인 완료 | 신청자 | approved |
| 반려 | 신청자 | rejected |

임시 저장·무승인 확정·회수·취소·독촉은 Teams 발송 대상에서 제외한다. 기존 OMS 내부 알림은 유지한다. Teams 카드에는 신청자·업무명·근무 예정일·계획 시간·처리자·계획 버전을 담고 OMS 상세 화면 링크를 제공한다. 사유와 결재 의견 전문은 포함하지 않는다. 승인·반려는 OMS에서 수행하고 기존 로그인 및 권한 검사를 적용한다.

## 2. 구조와 구현 결정

`core/services.py`의 결재 이벤트 생성 시 같은 트랜잭션에 `TeamsDelivery`를 생성한다. 결재 상태 변경이 롤백되면 발송 대기도 롤백된다. 별도 worker가 커밋된 대기 건을 읽어 발송한다. 사용자 요청 처리 중 Microsoft API를 호출하지 않는다.

초기 계획의 별도 SDK 봇 서버 대신 기존 Django에 수신 endpoint를 추가하고 공식 Bot Connector REST API를 사용한다. 별도 웹 서버와 신규 SDK 의존성을 추가하지 않으며 기존 requests·joserfc를 사용한다. Microsoft 문서는 SDK 없이 HTTPS·JSON으로 연동하는 방법을 지원한다. REST 인증 규약은 아래 공식 문서를 기준으로 한다. ARM64와 Python 3.12 검증은 개발 컨테이너에서 수행한다.

기능은 `TEAMS_ENABLED=0`이 기본값이다. 비활성 중에는 대기를 생성하지 않고 endpoint는 404를 반환한다. 활성화 이전의 결재 이력은 자동 재발송하지 않는다.

## 3. 사용자 연결과 봇 수신

- endpoint: `POST /api/teams/messages/`
- 허용 범위: 회사 단일 테넌트, Teams personal 대화.
- Microsoft 고정 공개키로 RS256 서명, issuer, audience(bot client ID), exp, nbf, serviceurl 및 키의 채널 endorsement를 검증한다.
- 인증된 activity의 serviceUrl과 토큰의 serviceurl이 같아야 하며, HTTPS·허용 호스트만 저장한다. redirect를 따라가지 않는다.
- `tenant ID + Entra object ID + bot ID`에 대화 ID·service URL·설치 활성 상태를 저장한다. 이름이나 이메일로 연결하지 않는다.
- 개인 앱 설치/대화 이벤트 또는 사용자 메시지에서 연결하고 제거 이벤트에서 비활성화한다. 수신 이벤트는 멱등 upsert를 사용한다.
- 발송할 때 기존 `EntraIdentity`의 tenant/object ID로 대화를 찾는다. 따라서 앱을 OMS 최초 로그인 전에 설치해도 연결 정보를 보관할 수 있다.
- 해당 endpoint는 브라우저 세션/CSRF 대신 Bot Connector 인증을 사용한다. 업무 화면 접근 제어는 그대로 유지한다.

## 4. 발송 대기와 장애 대응

`TeamsDelivery`는 결재 이벤트·수신자·종류에 유일 제약을 두고 당시 메시지 데이터를 보존한다. 상태는 pending / processing / sent / blocked / failed / skipped다. 조회는 Django 관리자에서 슈퍼유저에게 허용하고 blocked/failed 건은 감사 로그를 남기며 수동 재시도한다.

worker는 PostgreSQL `select_for_update(skip_locked=True)`로 작업을 선점하고 트랜잭션 밖에서 발송한다. 선점 시간은 120초이며 임대 토큰으로 만료된 worker의 결과 덮어쓰기를 막는다. 재시작 후 만료된 작업을 회수하고, 선점 시도는 최대 6회로 제한한다. HTTP 호출은 연결 5초·응답 15초 timeout을 사용한다.

- 429·408·5xx·네트워크 장애: Retry-After 또는 지수 간격으로 재시도.
- 사용자 연결 누락·앱 미설치·401/403 등: blocked로 보관하고 설정 수정 후 수동 재시도. 401에서는 캐시된 access token도 폐기한다.
- 반복 실패/프로세스 중단: 최대 시도 이후 failed.
- 발송 직전 요청이 이미 회수·처리·재제출되었으면 해당 요청 알림을 skipped로 종료.
- 비활성/업무 설정 미완료 계정은 발송하지 않는다.
- 오류 본문·access token·secret은 발송 기록에 저장하지 않는다. 발송 성공은 API 수락을 뜻하며 사용자 읽음/배너 표시를 뜻하지 않는다.

Teams 발송과 DB 결과 저장은 하나의 트랜잭션으로 묶을 수 없다. Microsoft 수락 후 응답 유실 또는 DB 장애가 발생하면 재시도로 중복 메시지가 생길 수 있다. 선점과 유일 제약은 일반적인 중복 처리 방지이며 exactly-once 보장이 아니다. 발송 직전 확인 이후 결재 상태가 바뀌는 짧은 경쟁 구간도 있어 카드에 버전을 표시하고 실제 처리는 OMS에서 검증한다. 설치 이벤트의 순서 역전은 사용자 메시지 또는 재설치로 연결을 복구한다.

## 5. API 설정 및 배포 절차

다음 작업은 실제 등록 권한과 자격 증명을 준비한 뒤 운영 담당자가 수행한다.

1. OMS SSO 앱과 별도의 Teams-managed bot을 등록한다. 회사 tenant ID와 bot client ID/secret을 확보한다. Teams app ID, bot client ID, Teams 사용자 ID는 서로 다른 값이다.
2. 메시징 endpoint를 `https://<OMS 도메인>/api/teams/messages/`로 설정한다. personal 범위 봇을 포함한 Teams 앱 패키지를 만들고 테스트 사용자에게 설치한다. 이 저장소는 아직 발급되지 않은 ID로 앱 패키지를 생성하지 않는다.
3. 조직의 사용자 지정 앱 허용 및 배포 정책을 확인한다. 초기 수동 설치 후 관리자 정책으로 대상자에게 설치한다. Graph 자동 설치 권한은 이번 범위에 추가하지 않는다.
4. 기존 Cloudflare Tunnel 경로로 endpoint를 공개한다. 봇 endpoint에 브라우저 로그인/JavaScript challenge를 요구하지 않는다. 봇 JWT 검증은 반드시 유지한다.
5. `.env`에 아래 값을 설정한다. secret은 Git에 넣지 않는다.

```dotenv
TEAMS_ENABLED=1
TEAMS_TENANT_ID=<OMS와 같은 tenant UUID>
TEAMS_CLIENT_ID=<봇 client UUID>
TEAMS_CLIENT_SECRET=<봇 secret>
OMS_PUBLIC_URL=https://oms.example.com
```

공개 URL은 허용된 호스트의 HTTPS origin이어야 한다. 허용 발송 호스트 기본값은 `smba.trafficmanager.net,smba.infra.teams.microsoft.com`이다. 실제 서명된 설치 이벤트에서 다른 Microsoft 호스트가 관찰되면 출처 확인 후 `TEAMS_SERVICE_HOSTS`로 명시적으로 추가한다. 임의 도메인이나 wildcard는 사용하지 않는다. 현재 구현은 Microsoft public cloud 기준이다.

운영 Cloudflare 구성의 예시:

```sh
docker compose -p oms -f compose.cloudflare.yaml run --rm app python manage.py migrate
docker compose -p oms -f compose.cloudflare.yaml run --rm app python manage.py check --deploy --fail-level ERROR
docker compose -p oms -f compose.cloudflare.yaml --profile teams up -d app teams-worker
```

일반 `compose.yaml`에도 동일한 `teams` profile을 제공한다. worker는 앱과 같은 이미지·DB·환경 설정을 사용하며 외부 포트를 열지 않는다. 개발 환경에서 한 번 처리하려면 환경 변수를 설정한 후 `docker compose -f compose.dev.yaml run --rm app python manage.py send_teams_notifications --once`를 사용한다.

배포 시 기존 백업/명시적 migration 절차를 따른다. 발송 중단은 먼저 worker를 멈추고 `TEAMS_ENABLED=0`을 적용해 앱을 재생성한다. 재활성화하면 기존 pending 건은 다시 처리되므로 관리자에서 대상/날짜를 확인한다. sent 건은 수동 재시도 대상에서 제외한다.

## 6. 검증 및 운영 전 확인

자동 검증은 API 자격 증명 없이 모의 HTTP 응답과 실제 RSA 서명 토큰을 사용한다. 요청/승인/반려 수신자, 재제출 구분, DB 롤백, 기본 비활성, HTTP timeout, 429 재시도, 최대 시도, 임대 복구, 오래된 worker 결과 차단, 요청 회수 시 생략, 잘못된 서명/issuer/audience/만료/테넌트/service URL/공유 대화 거부와 PostgreSQL worker 동시 선점을 검증한다.

운영 전에는 실제 Teams 설치 이벤트에 object ID·personal scope·대화 정보가 전달되는지, 승인자/신청자 개인 채팅에 올바르게 표시되는지, 모바일·데스크톱 링크의 OMS SSO 복귀, 앱 제거 후 실패와 재설치 복구, Cloudflare 경유 수신, NAS 메모리 사용량을 확인한다. API 자격 증명 미설정 상태에서는 실제 전송과 NAS 동작을 검증했다고 간주하지 않는다.

2026-10-26 검증 결과: Python 3.12 / Django 5.2 / PostgreSQL 17 / ARM64 개발 컨테이너에서 전체 87개 테스트 통과(Teams 관련 28개 포함). ARM64 이미지 빌드, 모델 migration 변경 누락 검사, 개발·일반 운영·Cloudflare 운영 Compose 구문 검사 통과. 개발 DB에 `0005_teamsconversation_teamsdelivery`까지 적용했다. 실제 Teams API와 운영 NAS는 미검증이다.

## 7. 참고

- [Teams-managed bot 등록](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/getting-started/running-in-teams/overview)
- [Proactive messaging와 설치 전제](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
- [Bot Connector REST 인증](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication)
- [Bot Connector REST API](https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-api-reference)
- [Teams-managed bot OAuth 제약](https://learn.microsoft.com/en-us/microsoftteams/platform/teams-sdk/in-depth-guides/user-authentication)
