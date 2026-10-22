# JTT RC3 showcase / 시연 자료와 제작 기준

The [README GIF](../media/jtt-rc3-showcase.gif), [75-second video](../media/jtt-rc3-intro.mp4) and [thumbnail](../media/jtt-rc3-thumbnail.png) use actual RC3 curses/PTY output, rendered with synthetic demo data and edited timing. They are not native Windows Terminal recordings or performance benchmarks. Native Windows Terminal visual QA remains **PENDING**.

[English subtitles](../media/jtt-rc3-intro.en.srt) · [한국어 자막](../media/jtt-rc3-intro.ko.srt) · [Text alternatives and verification / 텍스트 설명·검증](JTT_MEDIA.md) · [SHA-256 manifest](../media/jtt-rc3-manifest.json)

README GIF·75초 영상·썸네일은 실제 RC3 curses/PTY 출력에 합성 데이터를 사용한 편집 시연입니다. Windows Terminal 창 녹화나 성능 측정이 아니며 네이티브 화면 검증은 아직 대기 상태입니다. 아래는 재현과 후속 촬영을 위한 제작 기준이며, 실제 검증 범위는 위 미디어 설명과 검증 목록을 따릅니다.

Source: immutable public `jtt-v0.3.0-rc3`, commit `2a4480178b500fbd3044d9e0bb152a98dbc12a63`.
Do not replace Release assets or modify the tag.

## Capture boundary / 촬영 경계

Use a separate test account or isolated HOME, JTT_HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME and XDG_STATE_HOME.
Choose a new owner-only temporary root. Use the verified official RC3 launcher with those paths on every reopen.
Do not run the default launcher against real state. The test HOME must have a synthetic prompt, history disabled and no copied user dotfiles.
Review system login profiles too: HOME isolation is not an OS sandbox.

For a future native capture, use a dedicated Windows Terminal/WSL2 surface, 160×44 cells, dark theme and a readable Korean font. The current edited media instead renders captured PTY cells.
Check 80×24 navigation separately; do not equate PTY rendering with native Windows display QA. No existing user terminals, Agent, SSH, tmux, Clipboard or credentials.
Type commands manually or through a test-owned PTY driver; never paste personal terminal output.
Do not capture shell environment listings, home paths, real usernames or hostnames.
Only stop the test session identities explicitly created for capture; UI detach is not cleanup.

## Reproducible screen content / 재현용 화면 내용

Terminal A:

```sh
printf '\033[36mJTT demo / 문서 작업\033[0m\n'
printf 'README draft\nQuick Start checked\n'
```

Terminal B:

```sh
printf 'Local test terminal\nNo network or Agent calls\n'
printf 'Ready / 준비 완료\n'
```

Use synthetic names: Window 1, Docs, Review; Work Kit: Demo kit; roles: Writer, Reviewer.
For a directory chooser, create only test folders beneath the isolated root.
No downloaded project, external service or AI program is needed.

## Final README GIF timeline: 18 seconds

| Time | Actual action | Caption |
| --- | --- | --- |
| 0–2s | Empty JTT → Ctrl+G n | A real local terminal / 로컬 터미널 |
| 2–8s | Type Terminal A command; show output in RIGHT | Type directly / 직접 입력 |
| 8–11s | Terminal B input | Independent sessions / 독립 세션 |
| 11–13s | Observe A unchanged | Switch cards / 카드 전환 |
| 13–15s | Labeled editorial detach transition | Detach ≠ stop / 분리 ≠ 종료 |
| 15–18s | Reconnected test sessions | Same sessions / 동일 세션 |

Cut typing pauses, not failures. Label time cuts; do not imply instant creation or reboot persistence.
If 18 seconds cannot communicate reconnect honestly, leave reconnect for the long video.
Export a readable GIF with a static poster and text alternative; do not commit large raw recordings.

## Final product video timeline: 75 seconds

| Time | Scene |
| --- | --- |
| 0–5s | Empty JTT; create Terminal A |
| 5–19s | Terminal A input and output, with labeled same-frame RIGHT detail |
| 19–33s | Terminal B input and independent output |
| 33–38s | Return to Terminal A |
| 38–44s | Empty Window 2; no automatic shell |
| 44–57s | Archive and Work Kit template preview; no execution |
| 57–60s | Labeled editorial detach transition |
| 60–66s | Reconnect to the same test sessions |
| 66–75s | Actual 80×24 CENTER / RIGHT navigation and Korean output |

Exact bilingual captions and source-frame selections are in the [render timeline](../media/showcase-source/timeline.json). Timing is edited across isolated takes, including a final two-second output hold; it is not a real-time performance measurement.

Do not present the template as a running Agent team. Do not imply all TUI protocols are supported.
Use Korean UI labels as recorded, with English/Korean subtitles rather than a fictional translated interface.

## Verification contract / 검증 원칙

- Actual released RC3, exact version recorded; not mockup or legacy Tower.
- Synthetic data label visible; 3-panel design unaltered.
- Same session IDs/PIDs before/after reconnect checked outside public footage.
- No private path, secret, environment dump or real work visible in any frame.
- Key overlays match default Ctrl+G / Ctrl+] behavior.
- New Window and Work Kit preview create zero shells.
- Report native Windows Terminal readability separately; it remains pending until actually checked. Do not imply the rendered PTY media closes that gate.
- Test-only terminals explicitly stopped; no other sessions touched.
- Record capture tool/version, dimensions, frame rate, SHA-256 and source commit in the media PR.
- Review GIF size, alt text and video captions; submit media in a separate Draft PR, without replacing the released RC3 assets.

## Legacy preservation

The complete legacy portions of the previous READMEs are retained in
[Tower English](TOWER_LEGACY.md) and [Tower 한국어](TOWER_LEGACY.ko.md), with relative links adjusted.
The existing `media/tower-readme-loop.gif` stays with Tower and is not relabeled as JTT.
Technical contracts stay in [terminal support](JTT_TERMINAL_SUPPORT.md) and [updating](JTT_UPDATING.md).
