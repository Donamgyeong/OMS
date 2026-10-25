# OMS Microsoft Entra ID SSO 통합 계획

작성일: 2026-10-25. **아직 배포하지 않은 신규 서비스**를 전제로 한다. 기존 운영 계정 이전이나 비밀번호 로그인 병행 없이 처음부터 Entra SSO를 기본 인증으로 구현한다. 테넌트 설정과 운영 환경은 아직 확인하지 않았다.

구현 상태: SSO·JIT·업무 설정 대기·관리자 초기화·세션 차단을 구현했다. 실제 설정과 구현된 동작은 [SSO 운영 문서](entra-sso.md)를 기준으로 한다. 아래는 도입 설계이며 실제 테넌트·NAS 검증은 남아 있다.

## 1. 목표와 권장안

회사 Microsoft 365 계정으로 OMS에 로그인한다. **단일 테넌트 OIDC + Authorization Code Flow(PKCE)**를 사용하고, 인증은 Entra ID, 업무 권한과 결재 구조는 OMS에서 관리한다.

Microsoft 365 Business Premium에는 Entra ID P1이 포함되므로, 해당 라이선스가 할당된 사용자를 대상으로 SSO·그룹 기반 앱 할당·조건부 액세스를 구성하는 방향이다. 위험 기반 조건부 액세스 등 P2 기능은 이번 범위에 포함하지 않는다. 실제 대상 사용자별 라이선스 할당은 관리자와 확인한다.

## 2. 현재 구조와 유지할 사항

- Django 5.2, `core.User(AbstractUser)`, Django 서버 세션과 기본 아이디/비밀번호 로그인을 사용한다.
- 사용자에는 사번·부서·직원/부서장/관리자 역할·지정 승인자·근무 기준이 저장된다.
- 업무 데이터는 계속 OMS User의 PK를 참조한다. Entra는 외부 인증 식별자로 연결하며 업무 모델을 디렉터리 객체에 직접 종속시키지 않는다.
- 기존 운영 계정 사전 연결·이메일 대조·과거 데이터 이전 작업은 필요하지 않다. 개발 데이터는 운영 데이터로 간주하지 않으며, 초기화는 별도 명시적 작업으로 다룬다.
- 일반 관리자는 전체 조회와 기초정보 관리, Django Admin은 슈퍼유저 전용이라는 기존 경계를 유지한다.
- Synology의 HTTPS 리버스 프록시와 사내망/VPN 접근 구성을 유지한다.

## 3. 로그인과 첫 사용자 생성 설계

1. 사용자가 OMS의 ‘Microsoft 회사 계정으로 로그인’을 누른다.
2. 회사 tenant 전용 Entra 인증 엔드포인트로 이동한다.
3. Entra에서 로그인 및 기존 정책에 따른 MFA를 수행한다.
4. OMS callback에서 라이브러리로 인증 응답을 처리한다. 서명·issuer·audience·만료·nonce를 검증하고 state 및 PKCE를 확인한다.
5. 회사 tenant와 앱 접근 역할 `OMS.Access`를 검증한다. 검증된 `(tid, oid)`로 사용자 연결을 찾고 없으면 사용자와 연결을 트랜잭션으로 함께 생성한다.
6. 기존 사용자라면 OMS 활성 상태를 검사한다. 비활성 사용자를 자동 재활성화하지 않는다.
7. 신규 사용자는 업무 설정 대기 화면으로 이동한다. 관리자가 부서·사번·근무 기준·승인자를 확인한 뒤 업무 이용을 활성화한다.
8. 업무 설정이 완료된 사용자는 OMS에 저장된 부서·역할·지정 승인자에 따라 화면을 제공한다.

외부 계정 연결용 모델을 추가한다. 예: `EntraIdentity(user, tenant_id, object_id, linked_at)`이며 `(tenant_id, object_id)`에 유일 제약을 두고, 초기에는 사용자당 하나의 Entra 연결만 허용한다. 연결 변경은 관리자가 수행하고 감사 이력을 남긴다.

**이메일·UPN·표시 이름을 계정의 영구 키나 자동 연결 근거로 사용하지 않는다.** 허용된 직원의 첫 로그인 시 `(tid, oid)`를 기준으로 OMS 계정을 자동 생성한다(JIT). 내부 username은 이메일과 무관한 고유 값으로 생성하고 비밀번호는 `set_unusable_password()`로 설정한다. 동시 첫 로그인에도 계정이 중복 생성되지 않도록 유일 제약과 충돌 처리를 구현한다.

신규 계정의 기본 역할은 일반 직원이며, `is_staff=False`, `is_superuser=False`로 시작한다. 업무 설정 대기와 퇴사 차단은 별도로 구분한다. 예를 들어 `onboarding_completed=False`인 활성 계정은 대기 화면과 로그아웃만 허용하고, `is_active=False`인 계정은 로그인 자체를 거부한다. 업무 설정 여부는 화면뿐 아니라 업무 URL·변경 요청 전체에서 검사한다.

Entra 사용자나 관리자가 OMS의 관리자·슈퍼유저로 자동 승격되는 경로를 만들지 않는다. 최초 운영 관리자는 첫 SSO 로그인 후 서버 관리 명령으로 특정 `(tid, oid)` 계정을 지정해 부여한다. 이 과정에는 웹에서 접근 가능한 최초 관리자 등록 기능을 만들지 않는다. 이메일과 이름의 자동 갱신 범위는 구현 시 정하되 업무 권한은 덮어쓰지 않는다.

## 4. Entra 관리자 설정

- 개발/운영 앱을 분리하여 등록하고 지원 계정은 ‘이 조직 디렉터리의 계정만’으로 설정한다.
- 플랫폼은 Web으로 설정한다. Redirect URI 예시는 운영 `https://<OMS 도메인>/accounts/entra/callback/`, 개발 `http://localhost:18000/accounts/entra/callback/`이며 실제 도메인 확정 후 정확히 등록한다.
- Enterprise Application에서 사용자 할당 필요를 켜고 전용 보안 그룹 `OMS-Users`를 할당한다. 중첩 그룹에 의존하지 않고 직접 구성원을 관리한다.
- 앱 역할 `OMS.Access`를 정의하고 `OMS-Users` 그룹에 할당한다. OMS도 검증된 ID token의 `roles`를 검사해 해당 역할이 없는 계정의 로그인과 자동 가입을 거부한다. Entra 관리자라는 이유만으로 가입을 허용하지 않는다.
- 초기에는 사내 직원만 `OMS-Users`에 넣고 외부 게스트는 할당하지 않는다. 같은 tenant ID만으로 직원과 게스트가 구분된다고 가정하지 않는다.
- 최초 권한 범위는 `openid profile email`로 계획한다. 로그인만을 위해 Microsoft Graph의 조직 전체 읽기 권한을 추가하지 않는다. 앱 할당 설정에 필요한 관리자 동의는 관리자가 처리한다.
- 서버 자격 증명은 배포 방식에 맞춰 인증서 또는 만료가 관리되는 client secret을 선택한다. NAS 초기 운영에서 secret을 사용하면 접근 제한된 환경변수로 주입하고 담당자·만료 알림·교체 절차를 기록한다.
- 기존 MFA·Security Defaults·조건부 액세스 구성을 먼저 조사한다. MFA 정책 변경이 필요하면 별도 신규 정책의 Report-only 결과를 확인하고 파일럿에 적용한 후 확대한다. 기존 보호 정책을 임의로 해제하지 않는다.
- Intune 준수 장치 제한은 실제 직원 장치와 모바일 이용 가능 여부를 확인한 뒤 후속 적용한다.

## 5. Django 구현 작업

| 위치 | 작업 |
| --- | --- |
| `requirements.txt` | 유지보수 중인 OIDC/MSAL 계열 라이브러리를 비교하고 Django 5.2·Python 3.12·ARM64 호환성을 확인해 버전 고정 |
| `core/models.py`, migrations | 외부 ID 연결·업무 설정 완료 상태·필요한 감사 이력 추가 |
| 별도 인증 모듈 | 로그인 시작·callback·역할 검증·JIT 생성·오류 처리 구현 |
| `config/urls.py` | Entra 로그인·callback·로그아웃 경로 추가 |
| `config/settings.py` | tenant/client/redirect/자격 증명·SSO 기본 인증·세션 정책 설정과 운영 필수값 검증 |
| `templates/registration/login.html` | Microsoft 로그인 버튼과 미할당·업무 설정 대기·장애 안내 추가, 일반 비밀번호 폼 제거 |
| 사용자 관리 | SSO로 생성된 사용자에게 부서·권한·승인자 설정, 기존 비밀번호 입력·재설정 경로 정리 |
| 관리 명령·업무 접근 제어 | 최초 관리자 부여, 사용자 세션 종료, 업무 설정 대기 계정의 업무 URL 접근 차단 |
| `.env.example`, 운영 문서 | 비밀값 없는 설정 예시, 앱 등록·교체·복구 절차 추가 |
| 인증 테스트 | 응답 검증 실패·JIT 중복 방지·업무 설정 대기·권한 유지·세션 종료·비밀번호 우회 방지 검증 |

콜백의 외부 통신에는 timeout을 설정하고 인증 실패 시 세션을 발급하지 않는다. `next`는 같은 서비스의 허용 경로만 사용한다. ID/access token, 인증 코드, secret을 로그에 기록하지 않는다. 로그인만 필요한 1차에서는 장기 refresh token 보관을 피한다.

로그아웃은 POST와 CSRF 보호로 OMS 세션을 먼저 삭제하고, Microsoft 세션 종료 연동 여부를 사용자 경험과 함께 검증한다. 다른 Microsoft 365 앱의 로그인 상태에 미치는 영향을 확인하고 실제 동작을 안내한다.

## 6. 세션과 퇴사자 처리

**Entra 계정 차단이나 앱 할당 해제가 이미 발급된 Django 세션을 즉시 없애지는 않는다.**

- 초기 권장값은 OMS 세션의 절대 수명 8시간이며, 재로그인 시 Entra 정책을 다시 평가한다. 일반 요청으로 절대 만료가 계속 연장되지 않도록 한다.
- 퇴사·긴급 차단 시 Entra 로그인 차단/앱 할당 해제와 OMS `is_active=False`를 함께 수행하고, 해당 사용자의 OMS 세션을 무효화한다.
- 비활성화된 사용자의 기존 요청과 로그인 거부를 검증한다. 사용자별 세션 삭제를 위한 관리 명령 또는 관리 기능을 제공한다.
- 자동 입퇴사·부서·상사 동기화는 후속 Graph/프로비저닝 과제로 분리한다. 자동화 전까지 두 시스템의 차단 책임자와 처리 절차를 운영 문서에 명시한다.

## 7. NAS 네트워크와 비상 접근

- 사용자 브라우저가 Microsoft 로그인과 OMS HTTPS 도메인 양쪽에 접근할 수 있어야 한다. callback은 브라우저가 방문하므로 이 흐름만을 위해 NAS를 인터넷에 공개할 필요는 없다.
- OMS 서버는 인증 메타데이터·서명 키·토큰 교환을 위해 Microsoft 관련 엔드포인트에 HTTPS로 접근할 수 있어야 한다. NAS의 DNS·시간 동기화·인증서 검증도 확인한다.
- DSM 프록시의 Host 및 `X-Forwarded-Proto`, Secure 쿠키, callback URL을 실제 HTTPS 환경에서 검증한다.
- 개발・파일럿・운영 모두 일반 사용자는 SSO만 사용한다. UI·백엔드・Django Admin에서 일반 비밀번호 로그인을 허용하지 않고 Django Admin에도 SSO 인증을 연결한다.
- 초기 비상 복구는 NAS SSH/컨테이너 관리 명령으로 설정·관리자 상태를 복구하는 방식으로 한다. 일반 웹 비밀번호 로그인과 별도 비상 로그인 화면은 만들지 않는다. NAS 운영 접근 권한과 복구 절차를 사전에 검증한다.

## 8. 순서와 예상 공수

| 단계 | 산출물·완료 기준 | 예상 |
| --- | --- | --- |
| 1. 준비 | 운영 URL·tenant·대상 그룹·앱 역할·MFA 현황 확정 | 0.5~1일 |
| 2. 개발용 연동 | 개발 앱 등록, 연결 모델, 로그인/callback, JIT 생성, 기본 검증 통과 | 1~2일 |
| 3. 운영 통제 | 업무 설정 대기 제어, 최초 관리자 명령, Admin SSO, 세션 차단, 감사 로그, 배포 문서 | 1~2일 |
| 4. 파일럿 | 직원·부서장·관리자 및 NAS HTTPS 실제 검증 | 0.5~1일 + 관찰 2~3영업일 |
| 5. 최초 배포 | 운영 앱 설정·관리자 초기화·조직 및 정책 등록·직원 업무 설정·복구 확인 | 0.5일 |

개발·검증 공수는 약 3.5~6.5인일의 초기 추정이다. 계정 이전 작업은 제외하며 JIT와 업무 설정 대기 흐름을 포함한다. 관리자 설정 대기, 조직·사용자 업무 정보 등록, 파일럿 관찰 기간은 별도다.

## 9. 수용 기준과 최초 배포·복구

- 허용된 직원의 첫 로그인에서 OMS 사용자 하나만 생성되고, 재로그인 시 동일 사용자로 연결된다.
- 다른 tenant, 앱 역할 누락·미할당·비활성 사용자, 변조·만료·재사용 응답, state/nonce 불일치는 거부한다.
- 신규 사용자는 업무 설정 전 업무 데이터 조회·입력·결재가 불가능하며 설정 완료 후 일반 직원 범위에서 이용한다.
- 이메일 변경이나 이메일 중복으로 사용자 연결이 바뀌지 않는다. 외부 연결 중복도 DB에서 거부한다.
- 직원/부서장/관리자 권한, 부서 조회 범위, 지정 승인과 자기 결재 차단이 유지된다.
- MFA·취소·Entra 장애·secret 만료·여러 탭 로그인·세션 만료·로그아웃을 검증한다.
- 퇴사자 세션 차단, 일반 사용자의 비밀번호 우회 차단, 최초 관리자 부여와 SSH/컨테이너 복구를 실제로 검증한다.
- 기존 전체 테스트와 신규 인증 테스트, Django 배포 점검, NAS ARM64 컨테이너 및 브라우저 HTTPS 검증을 통과한다.
- 최초 배포는 운영 앱 설정 → 마이그레이션 → 최초 관리자 SSO 로그인 및 명령으로 권한 부여 → 부서·정책 등록 → 파일럿 직원 가입 및 업무 설정 → 대상 그룹 확대 순서로 진행한다.
- 파일럿 시작부터 DB 백업과 복구를 검증한다. 장애 시 호환되는 이전 SSO 이미지/설정으로 복구하고 사용자와 이력은 삭제하지 않는다. 최초 릴리스에 복구할 이전 버전이 없으면 서비스를 점검 상태로 두고 수정 배포한다. 자동으로 비밀번호 로그인을 열지 않는다.

## 10. 구현 시작 전 확정할 값

운영 HTTPS 주소, 회사 Tenant ID, Entra 앱 등록/정책 담당자, 대상 직원 그룹과 라이선스 할당, 최초 OMS 관리자, 부서·승인자 관리 담당자, 외부 게스트 허용 여부(초기 권장: 제외), NAS 복구 담당자와 접근 경로.

## 공식 근거

- [Entra 라이선스 및 Business Premium 포함 기능](https://learn.microsoft.com/en-us/entra/fundamentals/licensing)
- [OIDC](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc), [Authorization Code Flow와 PKCE](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow)
- [ID token claims: oid, tid와 변경 가능한 이메일](https://learn.microsoft.com/en-us/entra/identity-platform/id-token-claims-reference)
- [사용자·그룹 앱 할당 및 중첩 그룹 제한](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/assign-user-or-group-access-portal)
- [앱 역할 정의·그룹 할당·ID token의 roles 검증](https://learn.microsoft.com/en-us/entra/identity-platform/howto-add-app-roles-in-apps)
- [조건부 액세스 Report-only](https://learn.microsoft.com/en-us/entra/identity/conditional-access/concept-conditional-access-report-only)
- [Entra 토큰과 애플리케이션 세션의 차이](https://learn.microsoft.com/en-us/entra/identity/devices/concept-tokens-microsoft-entra-id)
