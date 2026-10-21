# JTT 설치·업데이트·롤백

JTT와 기존 `tower` 명령은 서로 다른 제품과 런처입니다. 이 문서는 독립 실행형
`jtt`에만 적용됩니다. 기존 Tower 업데이트는 [`UPDATING.md`](UPDATING.md)를
참고하세요.

## 개발 런처

소스 체크아웃에서 체크아웃 전용 환경을 준비한 뒤 개발 명령을 명시적으로
설치합니다.

```sh
./scripts/jtt-setup-dev
./scripts/jtt-install-dev
jtt --version
```

설치기는 `~/.local/bin/jtt`를 해당 체크아웃과 `.venv-jtt`에 고정합니다. PATH에
다른 `jtt` 명령이 있으면 덮어쓰지 않으며 `.bashrc`, `.zshrc` 등 셸 시작 파일도
수정하지 않습니다. 필요하면 현재 셸에서 `export PATH="$HOME/.local/bin:$PATH"`를
실행하세요. 버전과 doctor 결과에는 `DEV`가 표시됩니다. 소스 체크아웃에서는
Release 확인이나 자동 적용을 거부합니다. `scripts/jtt-dev`와
`scripts/jtt-setup-dev`도 계속 사용할 수 있습니다.

## 공식 설치

현재 JTT Release는 **HOLD** 상태입니다. 개발 CI는 Release, 설치 또는 런처 전환을
수행하지 않습니다. JTT 버전이 공개되면 정확한 태그의 깨끗한 체크아웃에서
설치기를 실행합니다.

```sh
git clone --branch <공개된-JTT-태그> --depth 1 https://github.com/ju0o/tmux-agent-tower.git jtt-release
cd jtt-release
./scripts/jtt-install-release
```

JTT 태그는 제품 접두어를 사용합니다. Stable은 `jtt-v주버전.부버전.수정버전`,
RC는 `jtt-v주버전.부버전.수정버전-rc번호` 형식입니다. 예를 들어 태그의
버전 부분 `v0.3.0-rc2`는 Python 패키지 버전 `0.3.0rc2`와 대응합니다.
기존 Tower 태그 `v0.3.0-rc2`는 JTT Release 후보가 아닙니다. 현재 저장소는
두 명령을 `tmux-agent-tower` 배포 패키지 하나로 빌드하므로 제품별 숫자 버전이
겹칠 수 있습니다. JTT의 전체 식별은 `jtt-` 태그 접두어, 일치하는 패키지 버전,
JTT console entry point, 비공개 설치 기록의 `product: jtt` 조합입니다.
설치기와 업데이트기는 버전이 더 높아도 기존 Tower 태그를 거부합니다.

부트스트랩은 태그 체크아웃이 깨끗한지, GitHub에 실제 공개된 Release와 일치하는지,
태그가 가리키는 커밋이 같은지 확인한 다음 사용자 확인을 받습니다. 설치는
`${XDG_DATA_HOME:-~/.local/share}/jtt/` 아래 새 버전 경로에 진행합니다.
`~/.local/bin/jtt` 또는 PATH에 다른 명령이 있으면 설치를 거부합니다. `tower`나
셸 프로필은 변경하지 않습니다. 사용자 bin 경로가 PATH에 없으면 현재 셸용 안내만
출력하고 영구 셸 설정은 직접 수정하도록 둡니다.

## 명령

| 명령 | 동작 |
| --- | --- |
| `jtt` | 작업공간을 엽니다. 시작 시 업데이트를 확인하거나 터미널을 자동 생성하지 않습니다. |
| `jtt --version` | 패키지 버전과 `DEV`, `OFFICIAL`, `UNMANAGED` 설치 구분을 표시합니다. |
| `jtt doctor` | 로컬 환경을 읽기 전용으로 진단합니다. PTY·서비스를 만들거나 GitHub에 접속하지 않습니다. |
| `jtt update --check` | 공식 GitHub Releases API에서 현재 채널의 버전을 확인합니다. |
| `jtt update --dry-run` | 검증된 Release와 설치·런처 전환 계획을 표시합니다. 파일은 바꾸지 않습니다. |
| `jtt update` | 대화형 `yes` 확인 뒤 새 버전을 준비하고 JTT 런처를 전환합니다. |
| `jtt rollback --dry-run` | 보존된 이전 버전을 보여주며 파일은 바꾸지 않습니다. |
| `jtt rollback` | 대화형 확인 뒤 보존된 이전 Release로 복원합니다. |

`--channel stable` 또는 `--channel rc`로 배포 흐름을 명시할 수 있습니다. 기본
`auto`는 설치 버전의 흐름을 따릅니다. `jtt-v...` 네임스페이스의 공개 JTT Release만
대상입니다. Draft, 기존 Tower 태그, 임의 브랜치, 공개 Release가 없는 태그,
잘못된 버전·채널 조합, 검증 커밋으로 해석되지 않는 태그는 거부합니다. 프로그램
소스는 검증된 Release 커밋에서 가져옵니다. 확인된 업데이트를 준비할 때 Python
런타임 의존성은 설정된 pip 패키지 인덱스에서 받을 수 있습니다. 설치가 실패하면
현재 런처를 유지합니다. 오프라인 또는 다운로드 실패에서도 버전을 전환하지
않습니다.

개발 체크아웃은 이 업데이트 경로를 사용할 수 없습니다. 관리되지 않는 PATH
런처는 교체하지 않습니다. 새 버전은 별도 경로에 설치한 뒤 패키지 버전, JTT
진입점, import 경로를 확인하고 원자적으로 런처를 바꿉니다. 이전 버전과 런처
백업은 자동 삭제하지 않습니다. `jtt rollback`은 로컬에 남은 이전 sidecar를
사용하므로 네트워크에 접속하지 않습니다.

업데이트와 롤백은 JTT_HOME, 저장 작업공간, 세션 메타데이터, Supervisor, PTY,
Agent 프로그램, SSH/tmux 상태 또는 기존 `tower` 설치를 변경하지 않습니다.
현재 실행 중인 JTT를 자동 재시작하지 않으며, 새 버전은 다음 실행부터 사용됩니다.

## JTT doctor

`jtt doctor`는 실제 package/executable 경로, Python·pyte 준비 상태, Linux/WSL
환경, JTT_HOME 권한, 작업공간 읽기 가능 여부, Supervisor registry, 예상 Unix
socket 경로 길이, `jtt`와 `tower` PATH 충돌 여부를 표시합니다. 기존 JTT 메타데이터는
읽을 수 있지만 없는 디렉터리를 생성하거나 Supervisor socket에 접속하지 않습니다.
실행 중 상태는 로컬 registry의 PID/시작 시각 확인 결과이며 실제 TUI 조작 검증은
아닙니다.

## Release 상태

Draft PR과 실제 Windows Terminal/WSL2 사용자 경험을 검수할 때까지 Release HOLD를
유지합니다. 가짜 GitHub Release 응답을 이용한 코드 테스트는 공개 Release나 실제
사용자 설치가 아닙니다.
