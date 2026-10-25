# Microsoft Entra ID SSO 설정 및 운영

OMS는 Microsoft 회사 계정으로만 로그인한다. Authlib 1.9.1의 OIDC Authorization Code Flow와 S256 PKCE를 사용한다. 로그인·계정 생성·권한 검증은 구현되어 있으며 실제 회사 테넌트의 앱 등록과 실계정 검증은 배포 담당자가 수행해야 한다.

## 1. 앱 등록

개발용과 운영용 앱을 각각 등록한다.

1. Entra 관리 센터 → App registrations → New registration에서 ‘이 조직 디렉터리의 계정만’을 선택한다.
2. Authentication → Web에 정확한 callback 주소를 등록한다.
   - 개발: `http://localhost:18000/accounts/entra/callback/`
   - 운영: `https://<OMS 도메인>/accounts/entra/callback/`
3. Application (client) ID와 Directory (tenant) ID를 기록한다.
4. Certificates & secrets에서 client secret을 생성한다. **Secret ID가 아니라 Value**를 안전하게 보관하고 만료 담당자와 교체 일정을 정한다. 현재 구현은 client secret 방식이다.
5. App roles에서 역할을 생성한다. Value는 정확히 `OMS.Access`, Allowed member types는 Users/Groups, Enabled로 설정한다.
6. Enterprise applications에서 해당 앱의 Assignment required를 Yes로 설정한다.
7. 사내 직원용 보안 그룹 `OMS-Users`를 만들고 Users and groups에서 `OMS.Access` 역할로 할당한다. 중첩 그룹에 의존하지 않고 직접 구성원을 등록한다. 최초 관리자도 이 그룹에 포함한다. 초기에는 게스트를 할당하지 않는다.
8. 요청 scope는 `openid profile email`뿐이다. 로그인만을 위해 `User.Read`, `Directory.Read.All` 등 Graph 권한을 추가하지 않는다. 기본 등록에 불필요한 Graph 권한이 있다면 제거하고, 테넌트 정책과 앱 할당에 필요한 관리자 동의를 처리한다.
9. 기존 MFA·Security Defaults·조건부 액세스를 확인한다. 새 MFA 정책은 Report-only 결과와 파일럿을 확인한 뒤 적용한다. OMS가 MFA를 대신 구현하지는 않는다.

OMS는 ID token의 서명·issuer·audience·만료·nonce와 로그인 state를 검증하고, 회사 tenant와 `OMS.Access` 역할을 추가 확인한다. Entra 관리자 권한은 OMS 업무 관리자 권한을 부여하지 않는다.

## 2. 환경변수와 실행

운영 `.env`에는 다음 네 값을 모두 설정한다. 값 일부만 설정하거나 UUID·callback 주소가 잘못되면 설정 오류로 실행을 중단한다. 운영 callback은 HTTPS와 정확한 `/accounts/entra/callback/` 경로를 사용하며 호스트가 `DJANGO_ALLOWED_HOSTS`에 있어야 한다. 개발 HTTP 예외는 DEBUG 환경의 localhost/127.0.0.1에만 적용된다.

```dotenv
ENTRA_TENANT_ID=<회사 tenant UUID>
ENTRA_CLIENT_ID=<앱 client UUID>
ENTRA_CLIENT_SECRET=<secret value>
ENTRA_REDIRECT_URI=https://<OMS 도메인>/accounts/entra/callback/
```

`compose.dev.yaml`은 이 네 환경변수를 호스트 셸 또는 Compose의 `.env`에서 앱 컨테이너로 전달한다. 개발 주소의 localhost/127.0.0.1은 앱 등록·설정·브라우저에서 일치시킨다. 호스트 Python은 `.env`를 자동으로 읽지 않는다.

네 값이 모두 비어 있으면 로컬 서버는 로그인 준비 화면을 제공하고 실제 로그인은 503으로 거부한다. 운영 시작 스크립트는 `check --deploy --fail-level ERROR`를 실행하므로 미설정 상태에서는 Gunicorn을 시작하지 않는다. 이미지 빌드의 collectstatic과 인증 없이 수행하는 테스트에는 실제 자격 증명이 필요 없다.

## 3. 최초 관리자와 직원 업무 설정

1. 마이그레이션을 적용하고 앱을 시작한다.
2. 최초 관리자가 Microsoft 회사 계정으로 로그인하여 ‘업무 설정 대기’ 화면까지 진입한다.
3. Entra 관리 센터의 해당 **사용자 Object ID**를 확인한다. 앱 Object ID나 client ID를 사용하지 않는다.
4. 신뢰하는 NAS 셸에서 실행한다.

```sh
docker compose exec app python manage.py bootstrap_entra_admin <사용자-Object-ID> --superuser
```

현재 설정된 tenant의 활성 SSO 계정만 지정할 수 있다. `--superuser`는 Django Admin 접근까지 부여하고, 생략하면 OMS 업무 관리자만 부여한다. 명령은 업무 설정 완료 상태로 바꾸지만 직원의 부서나 근무 기준은 자동 추정하지 않는다. 대기 화면에서 ‘설정 완료 확인’을 누르면 관리 기능을 이용할 수 있다.

5. `/setup/`에서 부서를 만들고, 직원들에게 첫 로그인하도록 안내한다. 허용된 직원은 기본 직원 역할·사용 불가능한 로컬 비밀번호로 자동 생성된다. 사번·부서·권한·근무 기준을 설정하고 부서장/지정 승인자를 연결한다.
6. 직원·부서장 계정의 ‘업무 설정 완료’를 체크하려면 사번·활성 부서·유효한 승인자가 필요하다. 부서장 본인 신청은 상위 부서장 또는 별도 지정 승인자를 사용한다. 관리 전용 계정은 부서 없이 관리 작업을 시작할 수 있지만 근무 입력에는 활성 부서가 필요하다.
7. 정책과 휴일을 등록한 뒤 직원의 업무 이용을 확인한다. 설정 대기 계정은 업무 URL·POST·Django Admin 접근이 서버에서 차단된다.

사용자는 웹에서 수동 생성하거나 이메일로 다른 계정에 연결하지 않는다. 이름과 이메일은 최초 생성 때 표시 정보로만 저장하고 이후 로그인에서 업무 관리자 변경 값을 덮어쓰지 않는다. 식별은 `(tenant ID, object ID)`로 한다. 이메일 변경이나 같은 이메일의 새 디렉터리 객체는 기존 계정을 자동 승계하지 않는다.

## 4. 세션·차단·로그아웃

- 서버 DB 세션의 절대 수명은 로그인부터 8시간이다. 화면 조회로 연장되지 않는다. 재로그인은 Entra를 거치지만 Microsoft 세션/MFA 정책에 따라 암호나 MFA 입력이 생략될 수 있다.
- 로그인 요청은 10분 동안 유효하고 한 번만 소비된다. 브라우저 세션당 미완료 요청은 최대 5개로 유지한다. PKCE verifier와 nonce는 서버 세션에만 저장한다. 토큰과 refresh token은 세션/DB에 저장하지 않는다.
- `/accounts/logout/`은 CSRF가 있는 POST만 받는다. 기본값은 OMS 세션만 종료하므로 Microsoft 브라우저 세션은 유지될 수 있다.
- Microsoft 로그아웃도 필요하면 `ENTRA_LOGOUT_MICROSOFT=1`과 `ENTRA_POST_LOGOUT_REDIRECT_URI=https://<OMS 도메인>/accounts/login/`을 설정한다. 해당 복귀 주소를 앱의 Web redirect URI에도 등록하고 다른 Microsoft 앱과 재로그인 경험을 실제로 확인한다. OMS 세션을 먼저 삭제하고 Microsoft로 이동한다.
- Entra의 로그인 차단/앱 할당 해제는 이미 발급된 OMS 세션을 즉시 끝내지 않는다. 퇴사 시 Entra 차단과 아래 명령을 함께 수행한다.

```sh
docker compose exec app python manage.py revoke_entra_sessions <사용자-Object-ID> --deactivate
```

`--deactivate`는 새 OMS 로그인도 차단한다. 옵션 없이 실행하면 기존 세션만 무효화하고 재로그인은 허용한다. 세션 버전을 증가시켜 해당 사용자의 모든 기존 세션을 **다음 요청부터** 거부한다. 이미 처리 중인 요청을 취소하지는 않는다. 세션 행을 물리적으로 검색·삭제하는 대신 버전을 비교한다. 만료된 DB 세션은 주기적으로 `python manage.py clearsessions`로 정리한다.

기초정보 화면과 Django Admin에서 재직 활성 상태를 변경해도 세션 버전을 올린다. 재활성화 후에도 이전 세션이 살아나지 않으며 재로그인이 필요하다. 부서·권한은 요청마다 DB에서 조회하므로 변경 이후 요청에 반영된다.

## 5. 네트워크·감사·복구

사용자 브라우저는 Microsoft와 OMS 주소 양쪽에 접근해야 하며 OMS 서버는 Microsoft metadata/token/JWKS에 HTTPS 연결할 수 있어야 한다. 각 외부 요청의 기본 timeout은 10초다. 사내망/VPN의 callback은 사용자 브라우저가 방문하므로 이 흐름을 위해 NAS를 인터넷에 공개할 필요는 없다.

DSM 프록시의 Host·`X-Forwarded-Proto`, 시간 동기화와 인증서를 확인한다. 프록시에서도 callback의 query string을 로그에 남기지 않도록 구성한다. 앱 Gunicorn 접근 로그는 query string을 제외하며 개발 서버 로그에서도 callback query를 가린다. 앱에는 인증 성공/실패 분류·계정 생성·관리자 지정·세션 차단과 사용자 ID를 기록하며 토큰·인증 코드·secret·공급자 상세 오류를 기록하지 않는다. 운영 로그의 접근 권한·보관 기간·외부 수집은 배포 담당자가 정한다.

일반 로그인과 Django Admin 모두 비밀번호 인증을 제공하지 않는다. `createsuperuser`로 만든 로컬 계정도 웹 로그인할 수 없다. 장애 복구는 NAS SSH/Container Manager에서 앱 설정과 배포 상태를 복구하고, 필요 시 위 명령으로 기존 SSO 사용자의 관리자 권한을 다시 부여한다. secret 만료·오류는 올바른 secret으로 교체한 뒤 앱 컨테이너를 재생성한다. 코드가 임의로 로컬 로그인을 열거나 비활성 계정을 재활성화하지 않는다.

자동 입퇴사·부서·상사 동기화, SCIM, Graph 연동과 Entra front-channel/single logout은 이번 구현에 포함하지 않는다. 실제 테넌트에서 MFA, 사용자 할당/해제, 게스트 제외, secret 교체, Microsoft 로그아웃 및 NAS HTTPS를 최종 검증한다.

## 공식 참고

- [앱 역할과 토큰](https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps)
- [ID token claims](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
- [Authlib Django OIDC](https://docs.authlib.org/en/v1.6.10/client/django.html)
- [애플리케이션 세션과 Entra 토큰](https://learn.microsoft.com/en-us/entra/identity/devices/concept-tokens-microsoft-entra-id)
