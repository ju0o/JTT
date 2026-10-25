# JTT 정식 출시 WBS · E2E / Release Gate v1.0

- 작성 기준: 2026-10-25 (KST)
- 저장소: https://github.com/ju0o/JTT
- 기준 main: `9e952f94fb4e1f883a8157851b03c41fd0f70de5` (PR #38 병합, Linux main push CI PASS)
- 배포 패키지 후보: `ju0-terminal-tower 0.3.1rc0`, 설치된 Python 네임스페이스 `ju0_terminal_tower`, 실행 명령 `jtt`; 개발 중 내부 소스 경로 `src/tmux_agent_tower`
- 과거 공식 RC3: 기존 `tmux-agent-tower 0.3.0rc3` / `jtt-v0.3.0-rc3`; 변경·삭제·자동 이전 금지
- 문서 상태: **실행 계획 및 검수 계약**. 작성만으로 PR Draft 해제, 병합, 패키지 게시, 기존 설치 업데이트, 배포·정책 변경을 승인하지 않는다.

## 0. 성공의 정의 (Definition of Done)

**목표:** 독립된 로컬 PTY를 Windows 네이티브(WSL 불필요), Linux에서 생성·탐색·입력·관찰·재연결하고, 저장된 참조 및 비실행 템플릿을 혼동하지 않고 사용하며, 분리·종료·실패 상황에서도 허가되지 않은 세션을 변경하거나 데이터·입력 권한을 손상하지 않는 정식 배포 후보를 만든다.

정식 출시 GO의 필수 조건:

1. **지원 범위 확정:** OS/터미널/파이썬/설치 방식·지원하지 않는 기능·업그레이드 경계가 문서에 일치한다. 특히 Windows 10의 지원 또는 미지원은 실제 OS 시험과 라이프사이클 위험을 고려해 Founder가 명시 결정한다.
2. **기능:** 아래 WBS 필수 항목 및 E2E 테스트 100% PASS. 핵심 동작을 `jtt` CLI 한 경로에서 재현한다.
3. **안전:** 미인증/다른 사용자 SID, stale ID·generation·PID·시작 시간, lease 없는 입력·리사이즈, stop 오발동, 무단 클립보드·JTT_HOME 수정, 기존 Tower/tmux/SSH/Agent 오염 **0건**.
4. **품질:** 지원 OS별 깨끗한 환경 CI, 설치 패키지 E2E, 실제 Windows Terminal 사용자 검수, Linux 실기기 검수 PASS. Flaky 테스트가 반복되면 원인/수정/추적 없는 예외 승인 금지.
5. **배포:** wheel+sdist/entrypoint/의존성·라이선스·메타데이터·SHA-256 일치, 설치·업데이트·롤백·제거·복구 시나리오 검증, 독립 환경 설치 및 충돌 예방 확인.
6. **보안/복구:** 백업·변경 전후 상태와 identity 확인 및 비정상 종료 복구 절차. P0/P1 0건; P2는 명시된 제한·우회책·Founder 수용이 있어야만 허용.
7. **최종 권한:** Founder의 **RC Gate GO**와 별도의 **Release/Tag/게시 GO** 없이 공개 게시하지 않는다.

**범위 구분:** Work Kit = 이름/설명/1–4개 카드 템플릿의 미리보기·편집·저장만. **실행 자동화 아님.** Saved Workspace = 살아 있는 실제 PTY 참조 재연결만. **재부팅 후 종료된 프로세스 부활 아님.** 자동 Agent 실행/모델 API/SSH 원격 세션 자동 생성, 모든 xterm·Agent TUI 완전 지원 등은 이번 출시 계약에 포함하지 않는다. Founder가 이를 필수로 선택하면 WBS·일정을 먼저 변경한다.

## 1. 소유권·변경 권한·증거 계약

- **Founder:** 지원 범위·핵심 UX·PR 병합·Ruleset·RC·Tag·Release·TestPyPI/PyPI·실제 사용자 상태 접근을 단계별로 별도 승인한다. 실기기 Human Gate를 직접 수행한다.
- **Supervisor/PM:** 우선순위, PR 독립 검수, CI/보안 증거 대조, WBS 상태와 Gate GO/HOLD 판정; 승인 없이 병합/게시/사용자 파일 변경하지 않는다.
- **Astra(MainPC 개발자):** 별도 브랜치·Draft PR, 격리 `JTT_HOME`/venv, 테스트·로그·수정 및 결함 증거 제출; Founder 승인 없이 Merge/Release/설치 런처·PATH/실사용 세션 변경 금지.
- **증거 최소 묶음:** WBS ID, HEAD SHA, Tree SHA, Base SHA, PR URL, 변경 파일, 재현 단계, 실패 원인, 로컬/Hosted 테스트 명령·결과·Run URL, OS 빌드/Python/Windows Terminal 버전, 격리 JTT_HOME 경로, 원시 로그(민감정보 제거), 작업 전후 상태, 인적 검수 결과, 명확한 PASS/PARTIAL/FAIL.
- **상태 정의:** TODO → IN PROGRESS → CODE PASS → HOSTED PASS → HUMAN PASS → GO/COMPLETE; 실패나 증거 미비는 HOLD. '코드 PASS' ≠ '실제 UX PASS' ≠ '정식 배포 GO'.
- **서로 다른 관리 범위:** 각 WBS는 작은 Draft PR 또는 검증 리포트로 전달한다. 제품 안전성 수정과 CI 정책·배포 설정은 별도 PR.

## 2. 기준선과 미병합 의존 관계

| PR | 현황(기준일) | 다음 승인/검증 | 통과 기준 |
|---|---|---|---|
| #38 | `9e952f94…`로 main 병합. main Linux CI PASS | 완료 | 중복 작업 금지 |
| #36 | Draft, HEAD `1068abcc363963931272f27f55a2353edd25721f`; Founder Windows UX 4/4 PASS | Founder **Ready 전환만 별도 승인** → 새로운 main 후보 Linux/Windows CI → 별도 병합 승인 | HEAD 고정·merge result 부모가 최신 main·검증 체크 둘 다 PASS·충돌 없음 |
| #37 | Draft, HEAD `cdd1f4e502b3b9332495154833d18238d67d1586`, #36 브랜치 기반 | #36 통합 후 main 기준으로 재정렬; Hosted Windows 장경로 명령 실행 결함 분석·수정 | 실제 명령 실행과 출력 + 프롬프트, 복사 UI, Windows 전체 CI PASS |
| #26 | Draft 미병합 | 독립 HOLD | 이 Release WBS가 무단 병합 권한을 주지 않음 |

PR #36은 새 main 정책에 맞춰 Ready 검사 완료 후 **결과만 보고**, Founder가 실제 Merge를 추가 승인할 때만 병합한다. PR #37은 Hosted Windows 장경로 실패를 단순 timeout 연장이나 결함 마스킹으로 처리하지 않는다.

## 3. 단계별 WBS (의존/담당/Exit Gate)

| WBS ID | 단계 / 구체 작업 | 선행 | 담당 | 완료 증거 / Exit Gate | 현재 |
|---|---|---|---|---|---|
| WBS-R00 | 제품 지원 명세 동결: Windows 10/11, Linux(native), WSL2 별도 범위, shell/Terminal/파이썬 하한, Work Kit·Saved Workspace 계약, 제외 기능·P0/P1 정의 | 없음 | Founder+Supervisor | 버전별 지원표와 비지원 사항 서면 승인 | TODO |
| WBS-R01 | PR #36 병합 준비·통합: Ready 전환, 최신 main synthetic merge 검증, Linux 3.9/3.11/3.13+Windows 후보 게이트, 별도 Founder Merge 승인 | R00(기본 원칙) | Astra+Supervisor+Founder | HEAD/Tree/부모·CI·merge SHA·main CI 증빙 | 기술/UX PASS, 병합 PENDING |
| WBS-R02 | PR #37 경로/복사 신뢰성: native Hosted 명령 실행/화면/긴 경로; `cd`, `pwd`, Unicode, 선택·전체 화면 복사 명확화; #36 병합 후 base 정렬 | R01 | Astra+Supervisor | 80×24, 160×44 호스트 Windows backend+UI 전체 PASS; Founder 실제 Shift-드래그/복사 PASS | Windows CI FAIL/HOLD |
| WBS-R03 | 정보구조·핵심 UI 정리: LEFT 평면 Window, CENTER 사용 영역, RIGHT 입력, 보관함 Saved/Kit 탭, 스크롤·키보드·다국어·좁은 창, 최초 사용 안내 | R02 | Astra+Founder | 80×24/120×35/160×44 스크린샷·녹화와 모든 탐색 E2E PASS, Founder UX 승인 | TODO |
| WBS-R04 | PTY/입출력 및 호환: 키·IME/한글/조합 문자, 붙여넣기, 마우스, ANSI/VT, 색/Unicode, 비정상 폭, 출력량/갭, 성능·모니터링 | R01,R02 | Astra | OS별 PTY/Renderer unit+integration, 2~4 세션 반복 입력·출력, 30분 부하 및 입력 손실 0건 | TODO |
| WBS-R05 | 세션 수명주기/복구: create→input→detach→reconnect→stop, 저장 참조, 선택 신원 검증, UI crash, worker crash, 재부팅 후 stale 처리, 읽기 전용 Mini LIVE | R04 | Astra | 기존 활성 셸 무단 종료 0, stale 참조를 활성으로 표시 0, 저장 복구 E2E PASS | TODO |
| WBS-R06 | 소유권·보안·데이터: Windows SID/ACL/named pipe, Linux UID/socket, Job Object/process group, 단일 lease, stale PID/replay, clipboard, 안전한 상태 파일·동시 쓰기, 악의적 기록 | R04,R05 | Astra+Supervisor | 다른 사용자/동시 관찰·입력/stop/데이터 손상·클립보드 보안 테스트 PASS, P0=0 | TODO |
| WBS-R07 | Doctor/진단·환경: `jtt doctor`, `--version`, 누락 의존성/비대화형/Windows 네이티브/WSL 감지, 불변 경로, 에러 메시지와 읽기 전용 확인 | R02,R06 | Astra | Windows/Linux doctor가 POSIX import 오류 없이 작동; 상태·프로세스 생성/변경 없음 | TODO |
| WBS-R08 | 패키징/설치/업데이트/롤백: wheel+sdist, pipx/uv/venv 각 OS, 사용자 설치 충돌, Python 네임스페이스/only `jtt`, RC3 병행, 업데이트 소유권·롤백, 라이선스 | R07 | Astra+Supervisor | 깨끗한 PC/venv 설치·삭제·충돌·활성 세션 보존·실패 롤백 E2E PASS | TODO |
| WBS-R09 | 지원 OS 매트릭스: Windows 10/11 native Terminal+PowerShell, Linux native Ubuntu 두 LTS, WSL2에서 CLI+PTY 통합, OS별 최소 Python·의존성 확인 | R03~R08 | Astra+Founder | 각 공식 지원 OS의 실제 검수·Hosted CI PASS, 제외한 OS는 문서에 표시 | TODO |
| WBS-R10 | CI/E2E 자동화: README 명령 경계, fixture/테스트 격리, HEAD 정확성, Draft/Ready 게이트, crash/fault injection, 성능 기준선·반복 실행 | R04~R09 | Astra+Supervisor | 전 OS 자동 E2E 필수 케이스 100% PASS, flaky 원인 미해결 0 | TODO |
| WBS-R11 | 실제 사용자 Human Gate: 주요 작업을 새 설치에서 처음부터 끝까지 실행; 키/마우스/한글/복사/저장/Agent CLI/재연결/종료 | R09,R10 | Founder | Founder 체크리스트+영상/로그로 모든 P0/P1 시나리오 PASS | TODO |
| WBS-R12 | 문서·권한·가드: Quick Start ko/en, 설치/지원/문제해결, Security, 데이터 정책, 진단, 라이선스, CI required checks, 릴리스/롤백 안내 | R08~R11 | Astra+Supervisor | 문서대로 설치·복구 재현, Gate 체크 필수화 검증, Founder Ruleset 별도 승인 | TODO |
| WBS-R13 | RC Freeze 및 출고 리허설: exact SHA, dependency lock/빌드 재현, 안전·보안 감사, main+후보 CI, SBOM/저작권, manifest/checksum, staging dry-run | R10~R12 | Astra+Supervisor | RC 기능 동결, P0/P1=0, 모든 핵심 E2E PASS, Founder RC GO | TODO |
| WBS-R14 | 실제 게시: 특정 SHA에서 서명/체크섬/배포파일 확인, TestPyPI(별도 승인 시), GitHub Release, PyPI, 출시 후 깨끗한 새 사용자 설치 smoke | R13 | Founder 승인하에 Astra | Founder **각 게시 대상별** GO, Release/tag/PyPI 식별자 일치, 공개 설치 재현 | HOLD |
| WBS-R15 | 출시 후 유지보수: 장애 접수·우선순위·재현, 지원 OS smoke, 핫픽스/rollback 트리거, 버전별 호환성·보안 공지 | R14 | Founder+Supervisor+Astra | 지원/복구 runbook, 품질 지표, 핫픽스 Gate 마련 | TODO |

## 4. E2E 자동 테스트 스위트 (필수 케이스 / 증거)

모든 E2E는 **일회용 `JTT_HOME`**, 전용 venv, 임시 폴더, 고정 임시 사용자 프로필에서 실행한다. 실사용 `jtt`/`tower`/PATH/tmux/SSH/Agent/Clipboard에는 접근하지 않는다. 복사 관련 테스트는 클립보드 API를 mock하고 **실제 클립보드 쓰기는 별도 Founder Human Gate**에서만 승인한다. 상태 변이를 수행하는 E2E는 그날의 HEAD에서 재실행한다.

| ID | 실행 시나리오 | 자동 검증 / 실패 조건 |
|---|---|---|
| E2E-001 | 순수 새 환경 설치 | wheel/sdist 각각 설치, `jtt --version`, `jtt doctor`, 실행·제거. `tower` 미설치 및 중복 명령 없음 |
| E2E-002 | 프로그램 첫 실행 | UI 진입만으로 셸/PTY·외부 Agent·SSH 생성 0, 기존 홈 무변경 |
| E2E-003 | Window 생성·선택 | LEFT 논리 Window CRUD와 포커스, 생성 시 PTY 0; 카드 매핑 정합 |
| E2E-004 | 터미널 2~4개 | 명시적 생성, PowerShell/bash 입력·출력/한글/Unicode, 카드 전환 뒤 입력이 올바른 세션에만 도착 |
| E2E-005 | 붙여넣기와 출력 복사 | Textual Paste, 큰 입력 경계, Ctrl+C 중단, Ctrl+G c 화면 전체 복사, stale 화면 차단; 호스트 선택 영역은 Human Gate |
| E2E-006 | 현재 경로 표시 | 실제 `cd` 후 `pwd`/`Get-Location`과 prompt 일치, 긴 경로 80/160열 축약, 경로 오류 후 셸 계속 입력 가능 |
| E2E-007 | Detach·Reconnect | UI 종료 후 같은 ID/generation/PID·시작시간 유지, 실제 셸 상태 유지, 여러 번 재연결 |
| E2E-008 | 저장된 작업공간 | 저장·재불러오기·세션 참조 재연결, stale 참조 오판 금지, JSON 손상 시 fail-closed |
| E2E-009 | Work Kit | 미리보기/편집/삭제, 절대 명령 실행·PTY 자동 생성·비밀정보 기록 없음 |
| E2E-010 | Mini LIVE | 관찰만 가능, 입력/resize/lease/영구 화면 저장 불가, 세션 stop 시 stale 즉시 제거 |
| E2E-011 | 권한·입력 lease | 단일 소유자 입력, 다중 observer 읽기, lease 박탈/만료/네트워크? **로컬 IPC 단절** 시 차단 및 재획득 검증 |
| E2E-012 | stale 신원과 stop | PID 재사용, generation·시작시간 불일치, stop 모달 취소, 타 세션 프로세스 무종료 |
| E2E-013 | 무결성·동시 쓰기 | JTT_HOME ACL/owner mode, JSON 원자적 저장, 동시 프로세스 편집 충돌 처리, 민감정보 로그 유출 0 |
| E2E-014 | UI·worker 비정상 종료 | UI 강제 종료 후 셸 보존(설계 경계), worker/shell 종료 후 안전한 unavailable 표시·회복 가이드 |
| E2E-015 | 재부팅·종료된 셸 | 이전 세션 부활을 주장하지 않고 stale 표시, 저장된 작업공간 참조를 조용히 새 셸로 바꾸지 않음 |
| E2E-016 | OS 권한 분리 | 서로 다른 Windows 사용자 SID/Unix UID가 다른 세션·IPC·상태 변경 불가 |
| E2E-017 | 리사이즈·렌더링 | 80×24/120×35/160×44, 스크롤, 한글 전각/결합문자, ANSI, 화면 재동기화·중단 시 lease 안전 |
| E2E-018 | 많은 출력·속도 | bounded buffer gap 시 키 차단, output cursor 독립 확인, 비정상 프레임·slow host 복구, p50/p95 기록 |
| E2E-019 | 설치 충돌·업데이트 | 기존 RC3, pipx/uv, 버전 충돌, 부분 설치 실패, 살아있는 PTY에 무단 업데이트·종료 0 |
| E2E-020 | rollback·제거 | 버전/데이터·세션 신원 보존, 실패 롤백, 등록되지 않은 파일 삭제 금지 |
| E2E-021 | OS startup/doctor | Windows 10/11 native (no WSL), Linux native, WSL2 지원 범위 확인, 읽기 전용 Doctor, missing deps/TTY 오류 |
| E2E-022 | 실제 Agent CLI 사용 | 별도 opt-in/폐기 가능한 repo에서 `codex`/`claude`/`opencode` 등 설치된 도구 1종 이상 interactive 실행·전환·detach (사용자 승인·계정/키·API 요금 분리); 미지원 프로토콜은 제한 문서화 |
| E2E-023 | 릴리스 후보 설치 | exact SHA→wheel/sdist→새 격리 환경→독립 실행→해시 검증; PyPI/Tag 게시 없이 dry-run |
| E2E-024 | 배포된 설치 smoke | 실제 승인된 public Release/PyPI에서 설치→doctor→핵심 PTY→제거, 버전/태그/SHA 일치 |

자동 테스트에서 OS 자체 화면 선택, 호스트 단축키, 실제 사용자 체감·IME는 완전 대체 불가. 반드시 다음 Human Gate와 결합한다.

## 5. 실기기 Human Gate 시나리오

| ID | 환경 | Founder 수행(실사용 프로필/기존 세션은 무변경, 새 격리 설치) | PASS |
|---|---|---|---|
| HG-W11-01 | Windows 11 / Windows Terminal | PowerShell 입력, `pwd`, `cd`, 경로 변경·폭 축약, 한글, 붙여넣기, Shift+드래그/선택 복사, Ctrl+Shift+C와 Ctrl+G c 차이 | 정확한 텍스트/현재 경로, 복사 대상·단축키 의도 일치 |
| HG-W11-02 | Windows 11 | 두~네 터미널 전환, Mini LIVE, Work Kit/저장작업 혼동 없음, 30~60분 실작업 | 체감 지연/입력 누락·세션 혼선 0 |
| HG-W11-03 | Windows 11 | Detach·앱 재실행·기존 셸 재연결, stop 취소/확인, 오류/백업 가이드 | 허가 없는 종료·중복 셸 0 |
| HG-W10-01 | 실제 Windows 10 장치/VM (지원 확정 시) | 동일 핵심 입력/ConPTY/Terminal/doctor/재연결/설치 | OS 지원표와 동일한 보증 수준; 미검증이면 Windows 10 지원 표기 금지 |
| HG-LNX-01 | 실제 Ubuntu 22.04/24.04 native | bash/zsh(지원 명시 범위), curses UI, detach/reconnect, tmux 없는 동작, 재설치 | 기본 기능 PASS 및 기존 Tower/tmux 무변경 |
| HG-WSL-01 | WSL2 Ubuntu(명시 지원 시) | TTY/PTY, 저장경로, Windows Terminal 호스트 키/Unicode, restart 후 stale, Doctor | 명시된 WSL 범위 PASS |
| HG-INST-01 | Windows+Linux 각 1종 | 신규 설치→첫 사용→업그레이드/롤백 dry-run→제거, 기존 런처 충돌 확인 | PATH/원래 홈/기존 JTT 타 프로필 변경 0 |

검수 기록: 장치/OS 빌드, Terminal/PowerShell/Python 버전, 설치 방식, HEAD·Tree·패키지 SHA, 체크리스트, 오류 화면 캡처(민감정보 마스킹), 체감 반응과 결함 ID. Founder 승인 없이 SSH·Agent 계정·실제 클립보드 자동 조작·OS 사용자 설정 변경 금지.

## 6. OS / 설치 매트릭스 (지원 계약 승인 시 구체화)

| 플랫폼 | 최소 통과 환경(제안) | 특수 확인 | 미달 시 정책 |
|---|---|---|---|
| Windows 11 | 실제 Windows Terminal + Windows PowerShell 5.1 + native Python, Windows UI | ConPTY, SID/ACL, UTF-8, copy/paste, 리사이즈, 패키징, Win Doctor | 출시 HOLD |
| Windows 10 | 실기기/재현 가능한 VM(공식 지원 결정 시) | ConPTY/배포, OS 지원종료 위험·실제 사용 조건 | Founder가 지원 범위에서 제외하거나 검증 완료 전 출시 HOLD |
| Linux | Ubuntu 22.04/24.04 native + 지정 Python 지원 버전 | curses, PTY, IPC, owner-only state, 설치·update/rollback | 해당 버전 지원 보류/출시 HOLD |
| WSL2 | Windows 11 WSL2 Ubuntu 22.04/24.04 | Linux PTY 경로와 Windows Terminal 호스트 키, 종료/재시작 | 별도 지원 표기로 PASS 또는 제외 |
| Python 버전 | 현재 `requires-python>=3.9`; CI Linux 3.9/3.11/3.13 | Windows pywinpty/Textual 실제 지원 범위 별도 결정 | 메타데이터에서 플랫폼별 현실과 문서 일치시켜야 함 |
| macOS/기타 distro | **현재 정식 출시 필수 아님** | 향후 별도 시험 | 미지원 명시, 미검증 환경 홍보 금지 |

## 7. 릴리스 품질 기준과 실패 처리

- **P0/blocker:** 잘못된 세션 입력·종료, 권한 경계 침범, 상태 데이터 파괴, 기존 사용자 PATH/설치 자동 변경, 잘못된 패키지 게시, 배포 증거 불일치, 사용자 파일 무단 삭제. → 즉시 Release HOLD, 회귀 테스트 추가, 원인 규명.
- **P1/high:** 기본 터미널 입력/붙여넣기·선택·경로·재연결 불가, 공식 지원 OS 설치 불가, 읽기 전용 Doctor 위반, Windows Hosted 핵심 E2E 실패, 반복 flakiness. → Release HOLD.
- **P2/medium:** 경미한 레이아웃/문구/성능 문제·미지원 TUI 프로토콜 등. Founder가 영향·대안·제한 문서를 검토해 개별 수용 가능. 데이터/안전성 위험이면 P0/P1로 상향.
- **성능:** 임의의 단일 p50로 완료 선언 금지. 실제 Win11 + 표준 VM에서 동일 명령·2/4 세션 기준 p50/p95, 생성·재연결·화면 반영, 입력 손실, CPU/메모리, 30~60분 지속성을 기록한다. 릴리스 후보 성능 예산은 Founder가 데이터 기반 승인하며 유의미한 악화/사용 불가 입력 지연은 P1.
- **Flaky:** ‘재시도해서 우연히 PASS’ 인정 금지. 재현률·원인·수정·원시 실패 로그와 안정화 근거 보고. 운영 CI 실행 횟수는 최소화하되 최종 후보의 깨끗한 OS 검증은 생략 불가.

## 8. 출시 게이트 / 출고 순서

| Gate | 입력 증거 | GO 권한 | NO-GO 조건 |
|---|---|---|---|
| G0 범위 승인 | 지원·비지원, WBS, 성능·결함 등급 | Founder | 지원 환경 미정 |
| G1 통합 승인 | PR #36 Ready CI, 별도 병합 승인, main Linux PASS | Founder | HEAD/부모 불일치, Windows/Linux 실패 |
| G2 기능 안전 승인 | #37 Windows 원인 해결, 제품 WBS R02~R07, P0/P1=0 | Supervisor 판정 + Founder | 테스트 flakiness, 미해결 보안·입력 결함 |
| G3 E2E 승인 | OS 전용 매트릭스, 자동 001~023, Human Gate 전 항목 | Founder | 공식 지원 OS 미검증 또는 장경로/복사 불량 |
| G4 RC Freeze | exact commit+tree, 전 버전 CI, 빌드/설치 dry-run, 문서/라이선스/SHA/복구 계약 | Founder | 산출물 불일치, RC3 오염, 패키지 충돌 |
| G5 게시 권한 | 별도 구체 Tag/Release/TestPyPI/PyPI 권한 | Founder 각 대상별 승인 | 승인 미비 또는 검증 artifact 불일치 |
| G6 출시 후 검증 | 공개 설치 smoke E2E-024, 문서 링크, 로깅/지원, rollback | Founder 최종 일반 사용 GO | 신규 설치 실패, 긴급 P0/P1 |

**순서:** #38 완료 → #36 Ready 검증→Founder 병합 승인→main CI → #37 hosted 실패 해결→관련 PR 승인/병합 → 핵심 UX·세션·보안·패키지 → OS 매트릭스 → E2E+Human Gate → RC 동결 → 별도 게시 승인 → 공개 배포 smoke.

**금지:** PR #26 Merge/Draft 해제, 승인되지 않은 다른 PR Merge, 공식 RC3 변경/덮어쓰기, 임의 Tag/Release/테스트·본 PyPI, 로컬 사용자 JTT_HOME/PTY/tmux/SSH/Agent/Clipboard/launcher/PATH 수정, 무단 Ruleset·Actions·결제 변경.

## 9. 각 WBS 결과 보고 양식

```text
[JTT WBS-Rxx 결과]
STATUS: PASS / PARTIAL / FAIL / HOLD
Scope + 비지원 경계:
PR / Base / HEAD / Tree / 변경 파일:
자동 테스트: OS/Python/명령/검사수/CI run URL:
E2E: 대상 ID, 재현 단계/실제 결과/원시 증거:
Human Gate: 필요한가? 실제 수행 결과와 Founder 승인:
보안/회귀: lease·identity·JTT_HOME·clipboard·기존 설치 무변경:
미해결 결함(P0/P1/P2) / Risk Register:
다음 Gate 및 승인 요구(실행 승인과 판단 분리):
```

## 10. WBS↔E2E 추적성(검수 누락 방지)

| WBS | 필수 E2E | 추가 게이트 |
|---|---|---|
| R00 지원 계약 | 021,022 | Founder OS/지원·제외 기능 승인(G0) |
| R01 #36 | 004,005,011,018 | 새 main 후보 두 Ready 상태 체크·별도 Merge 승인(G1) |
| R02 #37 | 005,006,017 | Hosted Windows 장경로 80/160열 안정적 PASS, HG-W11-01 |
| R03 UI·정보구조 | 002,003,008,009,010,017 | HG-W11-02, HG-LNX-01 |
| R04 PTY/렌더러 | 004,005,006,017,018,022 | Host 키/붙여넣기·Unicode·성능 Gate |
| R05 세션·복구 | 007,008,010,012,014,015 | HG-W11-03, 상태 참조 안전성 |
| R06 소유권·보안 | 002,011,012,013,016,020 | 다른 SID/UID 및 P0 보안 회귀 |
| R07 Doctor | 001,021 | Windows PowerShell/Linux 격리 read-only 검증 |
| R08 패키징 | 001,019,020,023 | HG-INST-01, RC3 보존 |
| R09 지원 OS | 004,007,016,017,021 | HG-W10-01(지원 시), HG-LNX-01, HG-WSL-01(지원 시) |
| R10 자동 E2E | 001~023 | 의도된 matrix 전 항목 PASS, CI run 링크 |
| R11 Human Gate | 004~010,017,021,022 | HG-W11/W10/LNX/WSL/INST 해당 행 |
| R12 문서·Ruleset | 001,008,009,019,021,023 | Quick Start 실제 재현·별도 Ruleset 승인 |
| R13 RC Freeze | 001~023 | G4, 체크섬·Manifest·라이선스 증거 |
| R14 공개 게시 | 024 | G5+G6, 별도 실제 게시 승인 |
| R15 유지보수 | 007,014,019,020,024 재수행 | 핫픽스·롤백 리허설 및 대응 책임 |

## 11. 고위험 차단 목록(Risk Register 최초값)

| ID | 확인된/잠재 위험 | 담당·최초 차단 게이트 | 해소 증거 |
|---|---|---|---|
| RISK-01 | PR #37 Hosted Windows: 장경로 명령이 echo만 나타나고 기대 실행 결과/새 프롬프트 미관찰 | Astra / G2 | 원인 분리, 결정적 테스트, Hosted Windows 최종 PASS |
| RISK-02 | PR #36 현재 main 정책에서 Ready 후보 검증 미완료 | Astra+Founder / G1 | 신 SHA merge ref 확인, Linux/Windows 두 candidate 상태 PASS, 별도 병합 승인 |
| RISK-03 | Windows 10 실제 지원 여부 및 Windows별 최소 Python/ConPTY 의존성 미확정 | Founder / G0,G3 | 실기기 E2E 또는 공식 미지원 명시 |
| RISK-04 | 실제 Windows Terminal Shift+드래그 / Ctrl+Shift+C 선택 영역 복사 미승인 | Founder / G2,G3 | HG-W11-01 실제 클립보드 확인·안전성 승인 |
| RISK-05 | Native Windows Doctor 및 패키지 매니저 관리 권한·충돌·롤백 | Astra / G3,G4 | 설치 owner 구분, 기존 런처/세션 보존, 설치/롤백 E2E |
| RISK-06 | 장시간 세션/worker crash, stale 화면·잘못된 stop/입력 | Astra+Supervisor / G2,G3 | fault injection, 30~60분 soak, P0/P1=0 |
| RISK-07 | 기존 RC3와 새 배포 네임스페이스/상태 이전 오해 | Founder+Astra / G4 | 병행 격리, 이전 제한 문서, RC3 원본 불변 |
| RISK-08 | CI Draft NOT RUN과 Ready 필수 상태 혼동, Ruleset 미적용 | Founder+Supervisor / G4 | 별도 required check 정책 승인·우회 실패 시험 |
| RISK-09 | 외부 Agent API/SSH/clipboard 등 시스템 상호작용을 JTT 내장 기능으로 과장 | Supervisor / G0,G4 | 지원 계약·마케팅/FAQ의 정확한 범위 |

## 12. 바로 착수할 첫 작업 — WBS-R01의 제한된 준비 단계

1. 현재 `main=9e952f94fb4e1f883a8157851b03c41fd0f70de5`, PR #36 HEAD `1068abcc363963931272f27f55a2353edd25721f`, PR #37 base=#36인지 재확인.
2. PR #36 synthetic merge ref 부모·Tree 및 `ready_for_review` 후 GitHub Actions의 두 `candidate verification status` 체크 요구사항을 재확인.
3. Founder에게 **PR #36 Draft→Ready 전환 승인만** 명확히 요청. 승인 전 어떤 PR 상태도 변경하지 않는다.
4. 승인 후에는 CI를 실행·검증하고 결과만 제출. **별도 실제 병합 승인**이 있기 전에는 Merge하지 않는다.
5. PR #37 Hosted Windows 장경로 실패는 새로운 CI 정책으로 마스킹하지 말고 R02 위험 항목으로 이어받는다.

---

**유지관리:** 각 PR merge 후 이 WBS의 상태, 신뢰 기준 main SHA, 실패·보류 항목, 실제 검수 근거를 갱신한다. 모든 Release GO는 증거와 Founder의 별도 명시적 결정이 있어야 한다.
