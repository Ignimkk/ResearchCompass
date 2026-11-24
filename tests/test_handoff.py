"""8단계: Claude Code 인계 Markdown과 결과 블록 (계획서 5절, 7절 시나리오 7)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rcompass.models import (
    BaselineChange,
    Decision,
    DecisionType,
    MetricResult,
    ProblemStatus,
    Project,
    Provenance,
    Run,
)
from rcompass.services import ValidationError
from rcompass.services.datasets import add_dataset_version
from rcompass.services.decisions import decide
from rcompass.services.experiments import create_experiment, create_measurement, update_inputs
from rcompass.services.handoff import HandoffOptions, build_handoff
from rcompass.services.problems import add_problem, get_problem, set_focus
from rcompass.services.progress import CriterionState, evaluate_project
from rcompass.services.result_block import confirm_results, parse_result_block, prepare_preview
from rcompass.services.runs import parse_results_table, record_results


@pytest.fixture
def scenario(session: Session, project: Project):
    """시나리오 2~4: P1 › P2, 보류 P3, E1 기각, E2 채택(B1), 집중 P2, 다음 행동."""
    add_problem(session, project, title="오답 유형 분석", why="개선 방향 결정")
    add_problem(session, project, title="장문 지문 오답", why="오답 18 중 7이 장문", parent_short_id="P1")
    add_problem(session, project, title="모델 교체", why="프롬프트 개선 한계 시", status=ProblemStatus.PARKED,
                revisit_condition="프롬프트 개선이 한계에 도달하면")
    version = add_dataset_version(session, project, dataset_name="eval-100", version_label="v1.0",
                                  external_uri="D:/data/eval100_v1.jsonl", source="내부 QA 수집",
                                  collected_at=date(2026, 11, 2)).version

    def trial(title: str, values: list[int], code: str):
        experiment = create_experiment(session, project, problem_short_id="P2", title=title, hypothesis="h",
                                       decision_rule="평균 ≤ 17이면 채택")
        update_inputs(session, experiment, dataset_version_ids=[version.id], code_ref=code,
                      config_text="configs/summarize.yaml")
        rows = "\n".join(f"seed={i}\t{v}" for i, v in enumerate(values, 1))
        record_results(session, project, experiment, parse_results_table(f"label\twrong_count\n{rows}"),
                       dataset_version_id=version.id)
        return experiment

    e1 = trial("few-shot 예시 3개", [18, 19, 20], "3f2a9e1")
    decide(session, project, e1, DecisionType.REJECT, reason="평균 오답 +1, 형식 오류 2건 증가")
    e2 = trial("문단 요약 후 답변", [11, 12, 10], "a1b2c3d")
    adopt = decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성, 다른 유형 악화 없음",
                   baseline_label="문단 요약 v1")
    set_focus(session, project, get_problem(session, project.id, "P2"))
    project.next_action = "E2 설정으로 100문항 평균 응답 시간을 측정한다"
    session.commit()
    return {"version": version, "e1": e1, "e2": e2, "adopt": adopt}


EXPECTED = """# 연구 인계: QA 평가 오답 줄이기 (2026-11-19 기준)
> Claude Code에게: 연구 기록 앱에서 내보낸 현재 상태입니다. 기각된 접근은 이유가 해소되지 않는 한 다시 제안하지 마세요. 코드와 Git은 요청 없이 바꾸지 마세요.

## 연구 질문
동일 평가 100문항에서 오답을 18→12개 이하로 줄일 수 있는가?

## 성공 기준
| 기준 | 목표 | 현재 기준선 B1 | 상태 |
|---|---|---|---|
| 오답 수 (`wrong_count`, 평균, n≥3) | ≤ 12 | 11.0 ± 1.0 (n=3) | 달성 |
| 평균 응답 시간 (`avg_latency_s`, 평균) | ≤ 2 | — | 미측정 |

## 현재 기준선
B1 문단 요약 v1 (E2에서 {adopted} 채택) · 코드 a1b2c3d · 설정 configs/summarize.yaml

## 지금 집중할 문제
원래 질문 › P1 오답 유형 분석 › P2 장문 지문 오답
이유: 오답 18 중 7이 장문

## 시도한 접근
- E1 few-shot 예시 3개 — **기각**: 평균 오답 +1, 형식 오류 2건 증가 · 오답 수 18.0 ± 1.0 (n=3) → 19.0 ± 1.0 (n=3)
- E2 문단 요약 후 답변 — **채택**: 목표 달성, 다른 유형 악화 없음 · 오답 수 18.0 ± 1.0 (n=3) → 11.0 ± 1.0 (n=3)

## 관련 자료
- DS1 eval-100 v1.0 (내부 QA 수집, 2026-11-02) — D:/data/eval100_v1.jsonl

## 다음 행동
E2 설정으로 100문항 평균 응답 시간을 측정한다

## 결과 보고 형식
대상 실험: E3 응답 시간 측정 (기준선 측정: 방식은 바꾸지 않고 재기만 합니다)
실험이 끝나면 아래 블록만 출력하세요. 연구자가 확인한 뒤 기록합니다. 재지 않은 지표는 비워 두세요.
```rcompass-result
experiment: E3
dataset: DS1@v1.0
code: a1b2c3d
config: configs/summarize.yaml
runs:
  - {label: seed=1, metrics: {avg_latency_s: }}
  - {label: seed=2, metrics: {avg_latency_s: }}
  - {label: seed=3, metrics: {avg_latency_s: }}
files: [<생성 파일 경로>]
note: <한 줄 해석>
```
"""


def test_handoff_matches_plan_structure(session: Session, project: Project, scenario) -> None:
    """계획서 5절 인계 예시와 같은 구조(스냅샷)."""
    create_measurement(session, project, title="응답 시간 측정")
    text = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19)))
    expected = EXPECTED.replace("{adopted}", scenario["adopt"].created_at.date().isoformat())
    assert text == expected
    assert len(text.splitlines()) <= 60
    # 템플릿 블록을 그대로 붙여넣어도 해석됩니다(값이 비어 실행이 없다는 오류만).
    block = text.split("## 결과 보고 형식")[1]
    with pytest.raises(ValidationError) as error:
        parse_result_block(block)
    assert "기록할 지표 값이 없습니다" in error.value.errors["block"]


def test_approaches_outside_focus_scope_are_only_counted(session: Session, project: Project, scenario) -> None:
    add_problem(session, project, title="다른 갈래", why="w", parent_short_id="P3")
    other = create_experiment(session, project, problem_short_id="P4", title="숨길 실험", hypothesis="h",
                              decision_rule="r")
    record_results(session, project, other, parse_results_table("label\twrong_count\nx\t30"))
    decide(session, project, other, DecisionType.REJECT, reason="나쁨")
    text = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19)))
    assert "숨길 실험" not in text
    assert "- 그 밖의 문제에서 판정한 실험 1개(기각 1)는 앱에서 확인하세요." in text


def test_user_text_cannot_break_markdown(session: Session, project: Project) -> None:
    project.title = "제목 ```에서 탈출"
    project.next_action = "줄\n바꿈 | 표"
    text = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19)))
    assert text.count("```") == 2  # 결과 블록 펜스만
    assert "줄 바꿈 | 표" in text


# ---------------------------------------------------------------- 결과 블록 해석

BLOCK = """Claude Code 출력 앞부분 설명
```rcompass-result
experiment: E3
dataset: DS1@v1.0
code: 9e8d7c6
config: configs/summarize.yaml
runs:
  - {label: seed=1, metrics: {avg_latency_s: 1.8}}
  - {label: seed=2, metrics: {avg_latency_s: 1.9, wrong_count: }}
  - label: seed=3
    metrics:
      avg_latency_s: 1.7
files: [outputs/latency.png]
note: 2초 안쪽
```
뒷부분 설명"""


def test_parse_block_from_full_output() -> None:
    block = parse_result_block(BLOCK)
    assert (block.experiment, block.dataset, block.code, block.config) == ("E3", "DS1@v1.0", "9e8d7c6",
                                                                            "configs/summarize.yaml")
    assert [(r.label, r.values) for r in block.runs] == [
        ("seed=1", {"avg_latency_s": 1.8}), ("seed=2", {"avg_latency_s": 1.9}), ("seed=3", {"avg_latency_s": 1.7})]
    assert block.files == ["outputs/latency.png"] and block.note == "2초 안쪽"
    assert block.to_table() == "label\tavg_latency_s\nseed=1\t1.8\nseed=2\t1.9\nseed=3\t1.7"


def test_values_are_read_as_text_not_yaml_types() -> None:
    block = parse_result_block("experiment: e3\ncode: 1e10\nruns:\n  - {label: no, metrics: {acc: 0.5}}")
    assert block.experiment == "E3" and block.code == "1e10" and block.runs[0].label == "no"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("experiment: E3\nruns:\n  - {label: seed=1, metrics: {acc: abc}}", "3번째 줄: acc의 값 'abc'은(는) 숫자가 아닙니다."),
        ("experiment: E3\nrunz: []", "2번째 줄: 'runz'는 알 수 없는 항목입니다"),
        ("experiment: E3\nruns:\n  - {metrics: {acc: 1}}", "3번째 줄: 실행 이름(label)이 비어 있습니다"),
        ("runs:\n  - {label: a, metrics: {acc: 1}}\n  - {label: a, metrics: {acc: 2}}", "3번째 줄: 실행 이름 'a'이(가) 두 번"),
        ("runs:\n  - {label: a, metrics: {9bad: 1}}", "2번째 줄: '9bad'는 지표 이름으로 쓸 수 없습니다"),
        ("runs:\n  - {label: a, metrics: {acc: inf}}", "기록할 수 없습니다"),
        ("experiment: E3\nruns: [\n", "블록 형식을 읽을 수 없습니다"),
        ("앞\n```rcompass-result\nruns: []\n", "2번째 줄: 블록이 ```로 닫히지 않았습니다"),
        ("experiment: E3", "'runs:' 아래에 실행별 결과를 적어야 합니다"),
        ("- a\n- b", "1번째 줄: 'experiment:', 'runs:' 같은 항목으로 시작해야 합니다"),
        ("", "rcompass-result 블록을 붙여넣어"),
    ],
)
def test_block_errors_point_to_lines(text: str, message: str) -> None:
    with pytest.raises(ValidationError) as error:
        parse_result_block(text)
    assert message in error.value.errors["block"]


def test_line_numbers_count_from_pasted_text() -> None:
    with pytest.raises(ValidationError) as error:
        parse_result_block("설명 줄\n\n```rcompass-result\nexperiment: E3\nruns:\n  - {label: a, metrics: {acc: x}}\n```")
    assert error.value.errors["block"].startswith("6번째 줄:")


def test_aliases_are_rejected() -> None:
    text = "runs:\n  - &r {label: a, metrics: {acc: 1}}\n  - *r"
    with pytest.raises(ValidationError, match="별칭"):
        parse_result_block(text)


def test_size_limit() -> None:
    with pytest.raises(ValidationError, match="너무 깁니다"):
        parse_result_block("note: " + "x" * 70_000)


# ---------------------------------------------------------------- 미리보기와 확인


def test_preview_writes_nothing_and_checks_experiment(session: Session, project: Project, scenario) -> None:
    e3 = create_measurement(session, project, title="응답 시간 측정")
    session.commit()
    counts = (session.scalar(select(func.count()).select_from(Run)),
              session.scalar(select(func.count()).select_from(MetricResult)))
    preview = prepare_preview(session, project, e3, BLOCK)
    assert preview.dataset.version.id == scenario["version"].id
    assert counts == (session.scalar(select(func.count()).select_from(Run)),
                      session.scalar(select(func.count()).select_from(MetricResult)))
    with pytest.raises(ValidationError, match="E3 화면에서 붙여넣으세요"):
        prepare_preview(session, project, scenario["e1"], BLOCK)
    with pytest.raises(ValidationError, match="'DS9@v1'을\\(를\\) 찾을 수 없습니다"):
        prepare_preview(session, project, e3, BLOCK.replace("DS1@v1.0", "DS9@v1"))


def test_scenario_7_measurement_fills_unmeasured_criterion(session: Session, project: Project, scenario) -> None:
    """시나리오 7: E3 결과 블록 → 미리보기에서 값 수정 → 확인. 실행 3개가 Run으로 나뉘고 C2가 미측정에서 바뀜."""
    e3 = create_measurement(session, project, title="응답 시간 측정")
    preview = prepare_preview(session, project, e3, BLOCK)
    edited = preview.table.replace("seed=3\t1.7", "seed=3\t2.6")  # 연구자가 로그를 보고 고친 값
    outcome = confirm_results(session, project, e3, table_text=edited, dataset_version_id=preview.dataset.version.id,
                              code=preview.block.code, config=preview.block.config)
    assert [r.label for r in outcome.created] == ["seed=1", "seed=2", "seed=3"]
    assert outcome.snapshot_baseline.short_id == "B1"
    provenances = set(session.scalars(select(MetricResult.provenance).join(Run).where(Run.experiment_id == e3.id)))
    assert provenances == {Provenance.CLAUDE_CONFIRMED}
    assert e3.code_ref == "9e8d7c6" and e3.config_text == "configs/summarize.yaml"
    c1, c2 = evaluate_project(session, project)
    assert c1.state is CriterionState.MET  # B1의 오답 수 근거는 그대로
    assert c2.state is CriterionState.NOT_MET and c2.display_value == "2.1 ± 0.4 (n=3)"
    change = session.scalars(select(BaselineChange).order_by(BaselineChange.id.desc())).first()
    assert change.kind.value == "snapshot_update" and change.to_baseline_id == project.current_baseline_id


def test_confirm_on_trial_with_different_code_keeps_override(session: Session, project: Project, scenario) -> None:
    e4 = create_experiment(session, project, problem_short_id="P2", title="새 시도", hypothesis="h", decision_rule="r")
    update_inputs(session, e4, dataset_version_ids=[scenario["version"].id], code_ref="aaaa111", config_text="c.yaml")
    block = BLOCK.replace("experiment: E3", f"experiment: {e4.short_id}")
    preview = prepare_preview(session, project, e4, block)
    assert any("코드 버전" in w for w in preview.warnings)
    outcome = confirm_results(session, project, e4, table_text=preview.table, dataset_version_id=None,
                              code=preview.block.code, config="c.yaml")
    assert e4.code_ref == "aaaa111"
    assert {r.code_ref_override for r in outcome.created} == {"9e8d7c6"}
    assert {r.config_override for r in outcome.created} == {None}
    assert outcome.snapshot_baseline is None  # 일반 실험은 기준선을 바꾸지 않습니다


def test_confirm_respects_baseline_locks(session: Session, project: Project, scenario) -> None:
    e2 = scenario["e2"]
    with pytest.raises(ValidationError, match="기준선 B1의 근거"):
        confirm_results(session, project, e2, table_text="label\twrong_count\nseed=1\t99", dataset_version_id=None,
                        code=None, config=None)


def test_measurement_on_old_baseline_does_not_extend(session: Session, project: Project, scenario) -> None:
    e3 = create_measurement(session, project)
    assert e3.title == "기준선 측정: B1 문단 요약 v1"
    e5 = create_experiment(session, project, problem_short_id="P2", title="더 나은", hypothesis="h", decision_rule="r")
    update_inputs(session, e5, dataset_version_ids=[scenario["version"].id], code_ref="b", config_text="c")
    record_results(session, project, e5, parse_results_table("label\twrong_count\na\t9\nb\t9\nc\t9"))
    decide(session, project, e5, DecisionType.ADOPT, reason="ok")
    outcome = confirm_results(session, project, e3, table_text="label\tavg_latency_s\na\t1.5", dataset_version_id=None,
                              code=None, config=None)
    assert outcome.snapshot_baseline is None and "현재 기준선이 아니어서" in outcome.warnings[0]
    assert session.scalar(select(func.count()).select_from(Decision)) == 3


def test_measurement_requires_current_baseline_problem(session: Session, project: Project) -> None:
    with pytest.raises(ValidationError):
        create_measurement(session, project, problem_short_id="P9")
    measured = create_measurement(session, project)
    assert measured.short_id == "E1" and measured.based_on_baseline_id == project.current_baseline_id



# ---------------------------------------------------------------- 리뷰 뒤 회귀 테스트


def test_measurement_cannot_change_metrics_the_baseline_already_has(session: Session, project: Project,
                                                                    scenario) -> None:
    e3 = create_measurement(session, project)
    with pytest.raises(ValidationError, match="B1은\(는\) wrong_count를 이미 근거로"):
        confirm_results(session, project, e3, table_text="label\twrong_count\na\t5", dataset_version_id=None,
                        code=None, config=None)
    confirm_results(session, project, e3, table_text="label\tavg_latency_s\na\t1.5", dataset_version_id=None,
                    code=None, config=None)
    with pytest.raises(ValidationError, match="avg_latency_s를 이미 근거로"):  # 한 번 채우면 그 뒤로는 고정
        confirm_results(session, project, e3, table_text="label\tavg_latency_s\nb\t1.9", dataset_version_id=None,
                        code=None, config=None)
    c1, c2 = evaluate_project(session, project)
    assert c1.display_value == "11.0 ± 1.0 (n=3)" and c2.display_value == "1.5"


def test_b0_measurement_still_widens_any_metric(session: Session, project: Project) -> None:
    from rcompass.models import Experiment

    e0 = session.scalars(select(Experiment).where(Experiment.short_id == "E0")).one()
    outcome = record_results(session, project, e0, parse_results_table("label\twrong_count\nseed=4\t20"))
    assert outcome.snapshot_baseline.short_id == "B0"


def test_template_placeholders_and_values_survive_yaml(session: Session, project: Project) -> None:
    """빈 프로젝트의 템플릿(안내 문구)과 ': ', ' #'이 든 값도 그대로 붙여넣으면 해석됩니다."""
    add_problem(session, project, title="p", why="w")
    e1 = create_experiment(session, project, problem_short_id="P1", title="t", hypothesis="h", decision_rule="r",
                           code_ref="abc  dirty", config_text="model: gpt # main")
    text = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19), experiment=e1))
    block = text.split("## 결과 보고 형식")[1].replace("wrong_count: ,", "wrong_count: 11,")
    assert 'config: "model: gpt # main"' in block and 'code: "abc  dirty"' in block
    parsed = parse_result_block(block)
    assert parsed.config == "model: gpt # main" and parsed.dataset is None
    outcome = confirm_results(session, project, e1, table_text=parsed.to_table(), dataset_version_id=None,
                              code=parsed.code, config=parsed.config)
    assert {(r.code_ref_override, r.config_override) for r in outcome.created} == {(None, None)}

    empty = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19)))
    assert "dataset: <데이터 버전 (예 DS1@v1.0)>" in empty
    with pytest.raises(ValidationError, match="기록할 지표 값이 없습니다"):
        parse_result_block(empty.split("## 결과 보고 형식")[1])


def test_long_or_multiline_config_becomes_a_placeholder(session: Session, project: Project) -> None:
    add_problem(session, project, title="p", why="w")
    e1 = create_experiment(session, project, problem_short_id="P1", title="t", hypothesis="h", decision_rule="r",
                           config_text="lr: 0.1\nbatch: 32")
    text = build_handoff(session, project, HandoffOptions(today=date(2026, 11, 19), experiment=e1))
    assert "config: <실험에 기록한 값과 같으면 그대로 두세요>" in text
    block = text.split("## 결과 보고 형식")[1].replace("wrong_count: ,", "wrong_count: 11,")
    assert parse_result_block(block).config is None


@pytest.mark.parametrize("value", ["1_000", "0.9_5"])
def test_underscore_numbers_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="숫자가 아닙니다"):
        parse_result_block(f"runs:\n  - {{label: a, metrics: {{acc: {value}}}}}")
    with pytest.raises(ValidationError, match="숫자가 아닙니다"):
        parse_results_table(f"label\tacc\na\t{value}")
