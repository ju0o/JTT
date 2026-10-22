# JTT RC3 showcase production plan / 시연 촬영 계획

Status: **storyboard ready; GIF/video not recorded**. This is not native Windows Terminal visual QA.
Target the immutable public `jtt-v0.3.0-rc3`, commit `2a4480178b500fbd3044d9e0bb152a98dbc12a63`.
Do not replace Release assets or modify the tag.

## Capture boundary / 촬영 경계

Use a separate test account or isolated HOME, JTT_HOME, XDG_CONFIG_HOME, XDG_CACHE_HOME and XDG_STATE_HOME.
Choose a new owner-only temporary root. Use the verified official RC3 launcher with those paths on every reopen.
Do not run the default launcher against real state. The test HOME must have a synthetic prompt, history disabled and no copied user dotfiles.
Review system login profiles too: HOME isolation is not an OS sandbox.

Use a fresh Windows Terminal/WSL2 tab, 160×44 cells, dark theme, readable Korean font.
Record a short 80×24 navigation segment separately. No existing user terminals, Agent, SSH, tmux, Clipboard or credentials.
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

## README GIF: 18 seconds

| Time | Actual action | Caption |
| --- | --- | --- |
| 0–3s | Empty JTT → Ctrl+G n | A real local terminal / 로컬 터미널 |
| 3–7s | Type Terminal A command; show output in RIGHT | Type directly / 직접 입력 |
| 7–11s | Ctrl+G n; Terminal B output | Independent sessions / 독립 세션 |
| 11–14s | Ctrl+G h; observe A unchanged | Switch cards / 카드 전환 |
| 14–18s | Ctrl+G d; reopen same test state | Detach ≠ stop / 분리 ≠ 종료 |

Cut typing pauses, not failures. Label time cuts; do not imply instant creation or reboot persistence.
If 18 seconds cannot communicate reconnect honestly, leave reconnect for the long video.
Export a readable GIF with a static poster and text alternative; do not commit large raw recordings.

## Product video: 75 seconds

| Time | Scene |
| --- | --- |
| 0–8s | JTT title; LEFT / CENTER / RIGHT on actual RC3 |
| 8–23s | Create Terminal A; input and Unicode output; Ctrl+] navigation |
| 23–35s | Second terminal; card switch; independent outputs |
| 35–45s | New empty Window; return to Window 1; no automatic shell |
| 45–58s | Archive tabs: Saved work references vs Work Kit preview; no execution |
| 58–68s | Detach and reopen; same test sessions visible |
| 68–75s | RC3 prerelease, Linux/WSL2, official release URL and limitations |

Do not present the template as a running Agent team. Do not imply all TUI protocols are supported.
Use Korean UI labels as recorded, with English/Korean subtitles rather than a fictional translated interface.

## Acceptance before adding media

- Actual released RC3, exact version recorded; not mockup or legacy Tower.
- Synthetic data label visible; 3-panel design unaltered.
- Same session IDs/PIDs before/after reconnect checked outside public footage.
- No private path, secret, environment dump or real work visible in any frame.
- Key overlays match default Ctrl+G / Ctrl+] behavior.
- New Window and Work Kit preview create zero shells.
- Native display readability checked by a human in Windows Terminal.
- Test-only terminals explicitly stopped; no other sessions touched.
- Record capture tool/version, dimensions, frame rate, SHA-256 and source commit in the media PR.
- Review GIF size, alt text and video captions; attach in a separate approved documentation/media change if not ready for this PR.

## Legacy preservation

The complete legacy portions of the previous READMEs are retained in
[Tower English](TOWER_LEGACY.md) and [Tower 한국어](TOWER_LEGACY.ko.md), with relative links adjusted.
The existing `media/tower-readme-loop.gif` stays with Tower and is not relabeled as JTT.
Technical contracts stay in [terminal support](JTT_TERMINAL_SUPPORT.md) and [updating](JTT_UPDATING.md).
