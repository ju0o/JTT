# JTT RC3 — demo, transcript and verification

[한국어 설명](#한국어-설명) · [Quick Start](JTT_QUICK_START.md) · [빠른 시작](JTT_QUICK_START.ko.md)

## Watch

- [18-second README GIF](../media/jtt-rc3-showcase.gif)
- [75-second MP4](../media/jtt-rc3-intro.mp4) · [thumbnail](../media/jtt-rc3-thumbnail.png)
- [English subtitles](../media/jtt-rc3-intro.en.srt) · [한국어 자막](../media/jtt-rc3-intro.ko.srt)
- [Checksums and dimensions](../media/jtt-rc3-manifest.json)

This is **real RC3 curses/PTY output with synthetic demo data**, rasterized and
edited for documentation. It is not a native Windows Terminal screen recording,
Agent run, performance benchmark or promise of complete xterm compatibility.
The video combines separate isolated takes. Detail insets crop the same recorded
frame; the detach title is an explicitly labeled editorial transition, not
application UI. Timing is shortened or extended for reading. There is no audio.

## Accessible text alternative

JTT is a local workspace for independent terminal sessions, unlike legacy
Tower's monitoring of existing tmux panes. The left panel lists logical Windows;
the center displays terminal cards; the right shows the selected interactive shell.
Ctrl+G n creates a terminal. The recording shows harmless text being typed into
RIGHT and the shell producing output. Another independent shell is created and
selected. Ctrl+G h returns to the previous card without stopping either shell.

A separate take shows a newly created Window with zero terminals: a Window is
only a logical group. Archive separates saved session references from Work Kit
templates. A preconfigured two-card Work Kit is previewed; the screen explicitly
says it is not running. No Kit command or Agent starts.

Back in the shell take, Ctrl+G d exits the UI. An editorial title explains that
the two shells remain alive. The UI reopens and reconnects to the same session IDs,
generations, shell PIDs and Supervisor identities. This is UI reconnection, not
reboot recovery. The final narrow-screen take demonstrates CENTER navigation
and RIGHT input at 80×24. Ctrl+] returns to navigation.

## What was verified

| Check | Result and boundary |
| --- | --- |
| Source | Public RC3 wheel, package 0.3.0rc3, pyte 0.8.2; wheel digest matches published asset |
| 160×44 | Actual shell creation, typed input/output, card selection, reconnect; captured cells and rendered frames reviewed |
| 80×24 | Actual Tab → CENTER → Enter → RIGHT → Enter input, Ctrl+] navigation, independent outputs, Korean output and reconnect |
| Identity | Same six fields before/after detach: session ID, generation, shell PID/start, Supervisor PID/start |
| Work Kit | Preview did not add any shell; template is preconfigured synthetic data |
| Cleanup | Only test-owned identities explicitly stopped; their shells/Supervisors confirmed gone |
| Privacy | Accepted takes scanned for private paths, host markers, IPs and credential-like keywords; rendered representative/contact-sheet frames reviewed |
| Media | Decoded GIF/MP4 duration, dimensions, subtitles, checksums and file sizes verified |
| Native Windows Terminal | **PENDING**; PTY correctness and raster readability do not prove native font/input/display behavior |

Isolation used a fresh venv, HOME, JTT_HOME, XDG paths, synthetic Bash prompt,
disabled history and suppressed login MOTD. No existing user session, dotfile,
Agent, tmux, SSH, Clipboard or credential was used. A rejected preliminary take
showed login MOTD and was excluded entirely; only accepted, scanned captures are
included. Privacy checks are bounded evidence, not a claim that keyword scanning
alone detects every possible secret.

The release wheel is unchanged. Direct installation in the capture venv is
reported by JTT as UNMANAGED, not a bootstrap-managed OFFICIAL installation.
The source is the exact public wheel, not a modified product build.

## 한국어 설명

이 자료는 **실제 RC3 curses/PTY 출력과 합성 데이터**를 편집·렌더링한 시연입니다.
Windows Terminal 창 녹화, 실제 Agent 실행, 성능 측정 또는 전체 xterm 호환성
검증이 아닙니다. 서로 다른 격리 촬영을 이어 붙였고, 확대 영역은 같은 프레임의
일부입니다. UI 분리 안내는 앱 화면이 아닌 명시적인 편집 전환입니다. 무음 영상이며
읽기 쉽게 시간 흐름을 줄이거나 늘렸습니다.

JTT는 독립 터미널을 만드는 로컬 작업공간이며 기존 tmux Pane을 관찰하는 legacy
Tower와 다릅니다. LEFT에는 논리 Window, CENTER에는 터미널 카드, RIGHT에는
선택된 실제 셸이 보입니다. Ctrl+G n으로 터미널을 만들고 RIGHT에 무해한 명령을
입력해 출력을 확인합니다. 두 번째 셸도 독립적으로 실행되며 Ctrl+G h로 카드를
전환해도 두 셸은 유지됩니다.

별도 촬영에서는 새 Window에 터미널이 0개임을 보여줍니다. 보관함은 저장된 세션
참조와 Work Kit 템플릿을 구분합니다. 미리 준비한 두 카드 템플릿을 선택해도
실행하지 않으며, 화면에 미리보기임을 명시합니다.

셸 촬영으로 돌아와 Ctrl+G d로 UI만 분리합니다. 편집 안내 뒤 JTT를 다시 열면
동일한 세션 ID·generation·셸 PID·Supervisor 신원으로 재연결합니다. 재부팅 복원을
뜻하지 않습니다. 마지막 80×24 촬영은 CENTER 이동과 RIGHT 입력을 보여줍니다.
Ctrl+]는 탐색 복귀입니다.

실제 PTY 동작과 렌더링된 화면은 자체 검수했지만 **네이티브 Windows Terminal 검증은
PENDING**입니다. 사용자 환경에 접근하지 않았으며 촬영 전용 프로세스만 정리했습니다.
태그·Release 자산·제품 기능 코드는 변경하지 않았습니다.

## Reproduction and provenance

[Media-only renderer, timeline and accepted capture cells](../media/showcase-source/README.md)
are separate from JTT's runtime. Source identity:

- Tag: `jtt-v0.3.0-rc3`
- Commit: `2a4480178b500fbd3044d9e0bb152a98dbc12a63`
- Wheel SHA-256: `bd153158e32573850ca447f0f8ae80f62dee20a81df74569260419cf32409ab5`

Publication in a Draft PR is not merge approval. Existing RC3 assets remain immutable.
