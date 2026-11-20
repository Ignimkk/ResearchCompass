# 구조도

이 폴더에는 [Archify](https://github.com/tt-a1i/archify)로 만든 연구 나침반 구조도가 들어 있습니다. 각 그림은 원본 JSON(`*.json`)과 브라우저로 바로 여는 HTML(`*.html`)로 이루어집니다.

## 그림 목록

| 그림 | 근거 | 기준 | 상태 |
|---|---|---|---|
| [v0.1 구조 (계획 단계 예상)](planned-v0.1.html) | 계획서만. 코드 근거 없음 | [구현 계획서](../implementation-plan.md) 6절, 2026-11-20 | 현재 유효 |

### v0.1 구조 (계획 단계 예상)

**이 그림에서 볼 것**: 연구자는 브라우저로 **웹 화면**에 접속합니다. 화면의 모든 동작은 **서비스 계층**을 거칩니다. 서비스 계층은 결정, 기준선, 실행 집계, 계보, 인계, 백업을 맡습니다. 기록은 **저장소 밖의 데이터 디렉터리**에 남는데, 구조화된 기록은 SQLite에, 그래프와 설정 같은 첨부 파일은 sha256 주소의 파일 저장소에 저장됩니다. 대용량 외부 데이터는 복사하지 않고 위치와 지문만 적어 둡니다. 터미널의 **rcompass CLI**도 같은 서비스 계층을 쓰므로, 웹 화면과 CLI의 결과가 달라지지 않습니다. Claude Code와는 직접 연결되지 않습니다. 연구자가 인계 Markdown을 붙여넣고, 돌아온 결과 블록을 웹 화면에서 확인한 뒤 기록합니다.

**주의**: 대부분 아직 구현 전입니다. 현재 구현된 것은 0단계 골격(웹 빈 화면, `rcompass serve`, SQLite 연결)뿐입니다. 구현 단계가 끝나면 푸시된 커밋에 고정한 코드 근거 구조도를 추가하고, 이 그림과의 차이를 `compare`로 남깁니다.

## Archify 확인 기록 (v3.2.0, 2026-11-20)

계획서 1절에서 "확인하지 못한 것"으로 남겨 둔 항목을 설치한 스킬의 스키마로 확인했습니다.

- **노드 필드**: Architecture 컴포넌트는 `id`, `type`, `label`, `sublabel`, `tag`, `icon`, `brand`, `sources`, `row`/`col`/`pos`/`size`만 받습니다(`additionalProperties: false`). **설명문 필드는 없습니다.** 그래서 쉬운 한국어 설명은 그림의 `cards`와 이 README에 둡니다.
- **경계 종류**: `region`, `security-group` 두 가지뿐입니다. "계획 단계" 표시는 경계가 아니라 제목과 카드로 합니다.
- **코드 근거**: `meta.repository`는 `url`과 40자 `revision`이 필수이고, `provider`(github, gitee, gitlab)와 `link_mode`(`web`, `local-only`)를 선택할 수 있습니다. `sources`는 노드당 1~3개이며 각각 `path`, `line`, `end_line`, `label`을 가집니다.
- **한국어**: `meta.locale: "ko"`가 화면 UI(범례, 버튼)를 한국어로 바꿉니다. 그림 내용은 작성한 언어 그대로 표시됩니다.

## 스킬 설치와 재생성

스킬 패키지는 저장소에 포함하지 않습니다(약 18MB). 설치 출처와 해시는 루트의 `skills-lock.json`에 기록됩니다.

```bash
npx -y skills@latest add https://archify.si --skill archify --agent claude-code --copy --yes
node .claude/skills/archify/bin/archify.mjs doctor
node .claude/skills/archify/bin/archify.mjs finalize architecture docs/architecture/planned-v0.1.json docs/architecture/planned-v0.1.html --quality showcase --json
```
