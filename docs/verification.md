# 템플릿 검증 기록

## Cloudflare Tunnel 배포 환경 검증 (2026-10-25)

계정·도메인·터널 연결 전 배포 파일을 준비했다. `compose.cloudflare.yaml`은 기존 DSM 배포 파일과 합치지 않는 독립 구성이다. 테스트용 자격 증명과 `oms-cloudflare-verify` 프로젝트에서 검사하고 실제 Cloudflare tunnel run은 시작하지 않았다.

| 항목 | 결과 |
| --- | --- |
| Compose 구문 | `.env.cloudflare.example`으로 구문 검사 통과. 공개 ports 없음, 앱·DB·connector 네트워크와 HTTPS 프록시 설정 확인 |
| cloudflared 이미지 | 공식 `2026.10.3` ARM64, UID/GID 65532, run/token-file/protocol/metrics 옵션 확인 |
| 비밀 파일 권한 | 네트워크 차단·비루트·읽기 전용 컨테이너에서 테스트 파일 읽기 성공. 무효 토큰 오류를 예상대로 반환했고 실제 연결은 수행하지 않음 |
| connector readiness | daemon 없는 네트워크 차단 컨테이너에서 /ready 접속 거부를 실패 코드로 반환하는 것 확인 |
| 앱·DB 실행 | 별도 PostgreSQL DB에 마이그레이션 적용 후 앱과 DB healthy. 실제 PortBindings가 비어 있음을 확인 |
| 네트워크 분리 | `tunnel` 네트워크에서 공개 Host와 X-Forwarded-Proto=https로 `app:8000` 로그인 화면 200, DB service 이름 해석 불가 확인 |
| origin HTTP smoke | HTTPS 리다이렉트·정적 자산·DB health·로그인 화면·비밀번호 거부·관리자·CSRF·로그아웃 통과 |
| 제외 규칙 | `secrets/`와 `.env.cloudflare` Git 제외 및 앱 빌드 context 제외 규칙 추가 |

HTTP 검사는 이전에 검증한 SSO 앱 이미지를 사용했다. 앱 코드는 변경하지 않았으며, 이번 범위는 배포 구성과 문서다. 실제 connector 연결·DNS·Cloudflare edge TLS·브라우저 실계정 SSO는 검증하지 않았다. origin HTTP 검사의 인증 세션은 서버에서 생성한 테스트 fixture이며 실제 Entra 인증 성공을 의미하지 않는다. 검증용 컨테이너와 네트워크는 정리하고 DB/media 볼륨은 유지한다.

실제 배포 단계와 외부 점검은 [Cloudflare 배포 문서](deployment-cloudflare.md)를 따른다.

## Entra SSO 구현 검증 (2026-10-25)

`feat/entra-sso`에서 아직 배포하지 않은 신규 서비스 기준으로 구현했다. 기존 `oms-dev` 컨테이너·DB는 변경하지 않고 별도 `oms-sso-test` 프로젝트·DB·볼륨·포트 18001을 사용했다. 테스트 tenant/client/secret은 가짜 값이며 회사 계정이나 실제 테넌트 설정을 변경하지 않았다.

| 항목 | 결과 |
| --- | --- |
| ARM64 이미지 | Python 3.12 / Django 5.2.18 / Authlib 1.9.1 / joserfc 1.7.7 / requests 2.34.2, 빌드 및 정적 파일 수집 성공 |
| PostgreSQL | 초기 마이그레이션부터 `0004`까지 적용 성공 |
| 전체 테스트 | 기존 업무·권한 32개 + SSO 27개, 총 59개 통과; PostgreSQL에서 생략 없이 실행 |
| SSO 통신 테스트 | HTTPS 전송만 대체하고 실제 Authlib PKCE·RSA 서명·issuer/audience/nonce/만료 검증 실행 |
| 동시성 | 기존 제출·결재 2개 + 동시 첫 로그인 사용자 중복 방지·callback 1회 소비 2개 통과 |
| Gunicorn HTTP | 로그인 화면·비밀번호 로그인 거부·관리자·정적 자산·DB health·CSRF·POST 로그아웃·세션 만료 확인 |
| 업무 HTTP | 서버 생성 테스트 세션으로 계획·기준 초과 대기·결재·실적·중복 결재 차단·대시보드·로그아웃 통과 |
| Django 검사 | system check, migration 누락 검사, diff 공백 검사 통과 |

SSO 테스트는 잘못된 서명·issuer·audience·nonce·만료·tenant·Object ID·앱 역할, 필수 claim/ID token 누락, state의 다른 브라우저 사용·만료·재사용, 서명 키 교체, 이메일 변경·누락, 여러 탭의 nonce/복귀 주소 분리와 미완료 흐름 수 제한을 검증한다. 계정 기본 권한·로컬 비밀번호 불가·업무 설정 대기 URL/POST 차단·관리자 부여·세션 버전 차단·절대 만료·업무 활성화와 재활성화 시 기존 세션 거부·로그 가림·보안 쿠키도 포함한다.

HTTP smoke의 인증 세션은 DB 접근이 가능한 테스트 스크립트가 생성한다. 이는 Microsoft 로그인 성공의 증거가 아니며, 웹 우회 로그인 경로는 추가하지 않았다. 로그인 protocol 검증은 가짜 HTTPS 공급자와 서명 토큰을 사용하는 자동 테스트에서 수행한다. 실계정·MFA·앱 할당/해제·실제 secret 교체·Microsoft 로그아웃 및 NAS TLS/프록시는 배포 시 검증해야 한다.

테스트 재현은 개발 Compose에서 `python manage.py test`를 실행하면 된다. 운영 형태 검증은 아래 과거 Docker 절차를 사용하되 `prepare_verification.py`가 생성한 **가짜 Entra 설정으로는 회사 로그인이 불가능**하다. `smoke_workflow.py`는 DEBUG 개발 서버 전용이고 `smoke_container.py`는 신뢰 프록시 헤더를 사용하는 운영 형태 서버 전용이다.

아래 기록은 SSO 도입 전 당시 검증이다. 비밀번호 로그인·수동 계정 생성 관련 설명은 현재 인증 동작에 적용되지 않는다. 현재 설정 절차는 [SSO 운영 문서](entra-sso.md)를 따른다.

## 핵심 업무 기능 추가 검증 (2026-10-25)

`compose.dev.yaml`로 운영 환경과 분리한 `oms-dev` 프로젝트를 사용했다. Python 패키지 설치·Django 명령·마이그레이션·테스트·HTTP 검증은 모두 Docker 컨테이너 안에서 수행했다. PostgreSQL 개발 데이터는 별도 볼륨에 보존한다.

| 항목 | 결과 |
|---|---|
| 컨테이너 이미지 빌드 | ARM64 Python 3.12 / Django 5.2.18, 정적 파일 수집 성공 |
| DB | PostgreSQL 17, 초기 및 추가 마이그레이션 적용 성공 |
| Django 검사 | `check`, `makemigrations --check --dry-run` 통과 |
| 자동 테스트 | 기존 6개 + 업무 흐름 24개 + PostgreSQL 동시성 2개, 총 32개 통과 |
| 실제 HTTP | 로그인, 정적 파일, health, 16시간 확정, 기준 초과 대기, 결재함, 실적 입력, 승인, 중복 결재 차단, 대시보드, CSRF 차단, 로그아웃 확인 |

자동 테스트는 임시·반려·취소의 누적 제외, 수정 시 자기 시간 제외, 회수·재제출, 적용일별 정책·판단 보존, 자기 결재 및 일반 관리자 결재 차단, 부서장 본인의 지정 승인자, 부서 이동 후 기존 결재 유지와 타 부서 조회 차단, 월 경계 배분, 휴일·수동 실적 계산, 미계획·초과 수행 사유, 수정 이력, 중복 날짜, 소급 승인 표시, 기초정보 권한·비밀번호 검증·권한 상승 차단, 구버전 실적 보존을 포함한다.

동시성 테스트는 각각 별도 PostgreSQL 연결 두 개를 동시에 시작한다. 기존 15시간에서 1시간 신청 두 건을 동시 제출하면 하나만 무승인 확정되고 나머지는 승인 대기가 된다. 같은 계획의 동시 결재는 한 건만 처리되고 다른 요청은 오래된 버전으로 거절된다. SQLite 결과로 대체하지 않았다.

```sh
docker compose -p oms-dev -f compose.dev.yaml up -d --build --wait
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py migrate --noinput
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py check
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py makemigrations --check --dry-run
docker compose -p oms-dev -f compose.dev.yaml exec app python manage.py test --noinput
docker compose -p oms-dev -f compose.dev.yaml exec app python scripts/smoke_workflow.py
```

HTTP 검증 스크립트는 개발 설정에서만 실행된다. 매번 고유한 임시 계정·부서·미래 적용 정책을 만들고 `finally`에서 자신이 생성한 업무 데이터와 함께 정리한다. 기존 사용자와 업무 데이터는 유지한다. 초기 관리자 비밀번호는 문서·소스에 저장하지 않는다.

NAS 실기기, 운영 TLS·리버스 프록시, 백업·복구 및 부하 검증은 여전히 배포 환경에서 필요하다. 아래 2026-10-23 기록은 핵심 업무 기능을 추가하기 전의 초기 템플릿 검증이다.

검증일: 2026-10-23

## 완료

- 로컬 프로젝트 가상환경에 의존성 설치: Django 5.2.18, psycopg 3.3.6, Gunicorn 23.0.0, WhiteNoise 6.12.0.
- 로컬 Python 3.14.3 및 SQLite에서 초기 마이그레이션 적용 성공.
- Django system check: 오류 없음.
- makemigrations --check --dry-run: 모델 변경 누락 없음.
- 테스트 6개 통과: 역할별 조회, 로그인 요구, 부서 간 정보 차단, 일반 관리자의 Django Admin 접근 차단, 계획 시간 검증, 타 사용자 계획 연결 차단.
- 운영 설정으로 collectstatic 및 압축·해시 manifest 생성 성공.
- 운영 보안 검사: HSTS 미설정 경고 1개. 적용 판단은 배포 문서 참고.
- Compose 설정 구문 검증 성공 (`--env-file .env.example config --no-env-resolution --quiet`).

## 미검증

- 초기 검증 당시 Docker 엔진이 실행 중이지 않았던 항목은 아래 추가 검증에서 확인했다.
- NAS 실기기, DSM 리버스 프록시, 실제 TLS 인증서, 부하·성능, 재생성 후 데이터 보존, 백업·복구는 배포 환경에서 검증해야 한다.
- SQLite 테스트는 PostgreSQL 동시성 검증을 대신하지 않는다. 결재·누적 시간 제어 로직은 아직 구현하지 않았다.

## 참고

초기 venv 생성의 ensurepip 오류는 전역 pip의 `--python` 옵션으로 프로젝트 가상환경에 설치하여 우회했다. 마이그레이션 생성 중 로컬 black 실행 오류가 있었으나 마이그레이션 파일은 생성되었고 적용 및 변경 누락 검사에 통과했다.

## Docker 추가 검증 (2026-10-23)

Windows Docker Desktop의 Linux x86_64 엔진에서 ARM64 에뮬레이션으로 검증했다. 운영 설정과 분리한 `oms-verify` 프로젝트, `.env.verify`, 별도 PostgreSQL·media 볼륨을 사용했다. `.env.verify`의 비밀값은 무작위 생성하며 Git과 이미지 빌드에서 제외한다.

| 항목 | 결과 |
|---|---|
| ARM64 이미지 빌드 | `oms:verify`, linux/arm64 성공 |
| 런타임 | Python 3.12, Django 5.2.18, PostgreSQL 17.11, aarch64 |
| 초기 DB 마이그레이션 | 실제 PostgreSQL에서 전체 적용 성공 |
| 기존 권한·모델 테스트 | PostgreSQL 테스트 DB에서 6개 통과 |
| 앱·DB healthcheck | 모두 healthy |
| HTTP smoke 검증 | 18개 통과 |
| 호스트 포트 연결 | 127.0.0.1:18000/health/ 응답 200 |
| 메모리 관측 | 앱 약 131.5MiB, DB 약 108.5MiB (단일 관측값) |

HTTP 검증은 실제 Gunicorn 서버에서 로그인 화면, 세션 로그인, 인증 후 대시보드, 관리자 화면, POST 로그아웃, 세션 무효화, CSRF 누락 시 403, Secure·HttpOnly 세션 쿠키, 해시 정적 파일 3개, DB 상태, HTTP→HTTPS 리다이렉트를 확인했다. UID 10001 비관리자 실행과 media 볼륨 쓰기도 확인했다. 임시 테스트 계정과 파일은 검증 후 제거했다.

실제 TLS 대신 신뢰하는 프록시의 X-Forwarded-Proto 헤더를 모사했다. 이는 DSM·인증서 검증을 대신하지 않는다. 메모리 관측값은 에뮬레이션 환경의 유휴 상태에 가까운 값으로 DS423 용량 산정 근거가 아니다. 테스트 명령에서만 SSL 리다이렉트를 껐으며, 실행 앱의 운영 설정에서는 켠 상태로 HTTP 검증했다.

### 재현 절차 (개발 PC, 프로젝트 루트)

Docker 엔진과 ARM64 에뮬레이션, `!override`를 지원하는 최신 Docker Compose가 필요하다. `.env.verify`는 검증용이며 NAS 운영에 사용하지 않는다.

```sh
python scripts/prepare_verification.py
docker buildx build --platform linux/arm64 --load -t oms:verify .
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml up -d --wait db
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml run --rm app python manage.py migrate --noinput
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml run --rm -e DJANGO_SECURE_SSL_REDIRECT=0 app python manage.py test
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml up -d --wait app
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml exec -T app python scripts/smoke_container.py
docker compose -p oms-verify --env-file .env.verify -f compose.yaml -f compose.verify.yaml stop
```

검증 후 컨테이너는 중지하고 이미지·볼륨은 재검증용으로 보존한다. 브라우저 미리보기용 HTTP 설정은 제공하지 않으며 운영처럼 HTTPS 프록시를 전제로 한다.
