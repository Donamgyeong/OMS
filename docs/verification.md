# 템플릿 검증 기록

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
