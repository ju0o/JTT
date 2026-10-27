# JTT Release WBS — 실행 현황 overlay (2026-10-28 KST)

**문서 후보 / RELEASE HOLD.** 이 문서는 [Release WBS 원문](JTT_RELEASE_WBS.md)의 역사적 스냅샷을 보완한다. 원문과 [R00 지원 정책](JTT_RELEASE_SUPPORT_POLICY.md)을 함께 읽는다. 이 문서 자체는 실제 테스트, PR 병합, 배포 또는 사용자 환경 접근을 승인하지 않는다.

## 상태 및 증거 경계

다음 상태는 2026-10-28까지 Founder와 Supervisor가 전달한 판단과 별도 작업의 정적 QA 보고를 기록한다. 이 문서 작성 과정에서 과거 CI, 사용자 실기기 결과 또는 사용자 환경을 다시 검증하지 않았다.

| 항목 | 기록 상태 | 증거와 제한 |
|---|---|---|
| R00 / G0 | **SUPPORT CONTRACT APPROVED** | 지원 목표 및 설치 정책 승인. 플랫폼별 실측 PASS나 Release GO가 아니다. |
| R01 | **COMPLETE** | Founder 보고: PR #36 병합 후 main `15a663cf787a2d865b6ebc69626304a50bed25e9`, Linux CI `40211502618` PASS. 이번 문서 작업에서 재실행하지 않았다. |
| R02 | **HOLD** | 원본 worker의 최초 프롬프트·장경로와 실제 실행 환경 검증은 미완료다. 원본 worker와 계측 worker의 결과를 구분한다. |
| R03 | **fixture 초안 정적 QA PASS / WBS TODO** | 세 viewport 크기와 논리 Window·카드·RIGHT 매핑 등 UI 테스트 초안. 실제 수집·실행 및 제품 UX PASS는 아니다. |
| R04 | **fixture 초안 정적 QA PASS / WBS TODO** | viewport와 실제 PTY를 분리한 초안. 소유 세션 broker, 실제 크기·입력·정리 검증은 미완료다. |
| 정상 Stop | **CHANGES** | generation 확인과 삭제 사이 경합 및 부분 정리 실패 시 증거 보존을 별도 추적한다. 시작 실패 보존 결과와 혼동하지 않는다. |
| OPS-INC-01 | **OPEN / 영향 UNKNOWN** | Windows 프로세스 열거 사고의 영향은 확정되지 않았다. 기존 사용자 프로세스를 재조사하지 않는다. |
| G2 / Release | **HOLD** | 필요한 코드·E2E·Human Gate 증거 및 별도 승인 전 통과·게시를 주장하지 않는다. |

R03 UI 초안 patch SHA-256은 `47067595310f1c3197b5b0bac88a7e298eb5312c9979dbb594ed876a6d4c6189`, R04 PTY 초안 patch SHA-256은 `6b29fc9654cd0d5edb9b8d9d826261942e5bb1a65cdbc59dcfb9ee1e0bdabb6d`다. 두 초안은 별도 로컬 작업에서 독립 정적 QA를 받았으며 이 문서 PR에 포함되지 않는다. R04는 현재 자동 수집 밖의 부분 초안이며, E2E-005/006의 일부 범위와 E2E-022 실제 Agent CLI 등은 실행·완료되지 않았다. 초안 기준 HEAD는 `b2406f8c593b3793f0f52d6f3a473c9c977ab8cf`이므로 향후 통합 전 현재 main과 별도 대조한다.

## 지원 셀과 E2E·Human Gate

R00 지원 목표는 Windows 11 x64 native, Ubuntu 22.04·24.04 native, WSL2 Ubuntu 22.04·24.04와 Python 3.11·3.12·3.13의 **15개 설치·실행 환경 셀**이다. 지원 계약 승인은 이 15개 셀이 검증됐다는 뜻이 아니다. 이 문서 작업에서 각 셀의 실제 설치·실행 여부는 재검증하지 않았으며, 전체 셀의 통과 증거도 확보되지 않았다. Windows 10은 최초 지원 범위에서 제외하고 HG-W10-01은 N/A로 기록한다. 공개 RC3의 기존 지원 계약은 변경하지 않는다.

R03의 원문 필수 E2E는 **002·003·008·009·010·017**, R04는 **004·005·006·017·018·022**다. 80×24·120×35·160×44는 열×행이며 UI viewport와 실제 PTY 크기는 서로 다른 증거다. 기존 사용자 UX 보고와 별도로, 이번 후보의 전체 E2E 및 해당 Human Gate는 이 문서 작업에서 실제 수행하거나 통과를 확인하지 않았다. 실제 Agent CLI E2E-022는 별도 승인 전 NOT RUN이다.

fake registry에 대한 호출 0건은 실제 OS 프로세스 생성 0건의 증거가 아니다. 자동 테스트, 플랫폼별 실제 PTY 및 Founder Human Gate는 서로 대체하지 않는다.

## 후속 경계

R03/R04 fixture 초안의 정적 QA는 실제 E2E 완료나 사용자 UX 승인이 아니다. 실제 프로세스·PTY·tmux·Agent 시험, Hosted CI, Push, Merge, Ruleset, Tag, Release 및 패키지 게시에는 각각 해당 범위의 별도 승인이 필요하다. 정상 Stop 안전성과 R02 검증 환경은 각각 독립 차단 항목으로 유지한다.
