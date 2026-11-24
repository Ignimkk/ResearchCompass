"""예제 프로젝트 "QA 평가 오답 줄이기" (계획서 7절 검증 시나리오의 앞부분).

`rcompass init --example`이 이 모듈로 예제를 심습니다. 화면을 하나씩 따라가며 앱의 흐름을 익히는 용도입니다.

- 성공 기준 2개(오답 수는 기준선 B0에서 측정, 평균 응답 시간은 미측정)
- 문제 3개(해결 1, 지금 조사 1, 나중에 검토 1)
- 외부 참조 데이터 버전 1건, 실험당 실행 3개
- E1 기각, E2 채택(기준선 B1), 그래프 PNG 2개와 표 CSV·설정 파일 첨부

``assets/``의 PNG는 같은 수치로 그린 그림을 헤드리스 Chrome으로 저장한 것입니다.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from rcompass.models import DecisionType, Direction, FileKind, ProblemStatus, Project
from rcompass.services.datasets import add_dataset_version
from rcompass.services.decisions import decide
from rcompass.services.experiments import create_experiment, update_inputs
from rcompass.services.files import StoredBlob, add_file, discard_if_unreferenced, store_stream
from rcompass.services.problems import add_problem, change_status, set_focus
from rcompass.services.projects import CriterionInput, ProjectInput, create_project
from rcompass.services.runs import get_run, parse_results_table, record_results

ASSETS = Path(__file__).parent / "assets"
EXAMPLE_TITLE = "QA 평가 오답 줄이기 (예제)"


def _table(values: list[int]) -> str:
    lines = ["label\twrong_count"] + [f"seed={i}\t{v}" for i, v in enumerate(values, start=1)]
    return "\n".join(lines)


def seed_example(session: Session, data_dir: Path, *, max_bytes: int) -> Project:
    """예제 프로젝트를 만듭니다. 첨부 파일 내용은 먼저 저장하고, 실패하면 쓰지 않은 내용을 지웁니다. 호출한 쪽이 커밋합니다."""
    blobs: dict[str, StoredBlob] = {}
    try:
        for name in ("wrong_types_E1.png", "wrong_types_E2.png", "wrong_types.csv", "summarize.yaml"):
            with (ASSETS / name).open("rb") as handle:
                blobs[name] = store_stream(data_dir, handle, max_bytes)
        return _seed(session, blobs)
    except BaseException:
        session.rollback()
        for blob in blobs.values():
            discard_if_unreferenced(session, data_dir, blob)
        raise


def _seed(session: Session, blobs: dict[str, StoredBlob]) -> Project:
    project = create_project(session, ProjectInput(
        title=EXAMPLE_TITLE,
        question="동일 평가 100문항에서 오답을 18개 → 12개 이하로 줄일 수 있는가?",
        baseline_label="현행 프롬프트 v1",
        criteria=[
            CriterionInput(name="오답 수", metric_key="wrong_count", direction=Direction.LOWER, target=12,
                           unit="개", min_runs=3, baseline_values=[17, 18, 19]),
            CriterionInput(name="평균 응답 시간", metric_key="avg_latency_s", direction=Direction.LOWER,
                           target=2.0, unit="초"),
        ],
        next_action="E2 설정으로 100문항 평균 응답 시간을 측정한다.",
    ))
    dataset = add_dataset_version(
        session, project, dataset_name="eval-100", version_label="v1.0",
        external_uri="D:/data/eval100_v1.jsonl", source="내부 QA 수집", collected_at=date(2026, 11, 2),
        notes="예제용 외부 참조입니다. 이 경로의 파일은 실제로 없습니다.",
    )
    version_id = dataset.version.id

    p1 = add_problem(session, project, title="오답 유형 분석", why="개선 방향을 정하려고")
    p2 = add_problem(session, project, title="장문 지문 오답", why="오답 18개 중 7개가 장문 지문")
    add_problem(session, project, title="모델 교체", why="프롬프트만으로 부족할 수 있음",
                status=ProblemStatus.PARKED, revisit_condition="프롬프트 개선이 한계에 도달하면")
    change_status(session, project, p1, ProblemStatus.RESOLVED)
    set_focus(session, project, p2)

    # E1: few-shot 예시 → 기각
    e1 = create_experiment(
        session, project, problem_short_id=p2.short_id, title="few-shot 예시 3개 추가",
        hypothesis="장문 지문 풀이 예시 3개를 넣으면 장문 오답이 줄어든다",
        decision_rule="평균 오답 ≤ 17이면 채택, 평균 ≥ 18이면 기각 (n ≥ 3)",
    )
    update_inputs(session, e1, dataset_version_ids=[version_id], code_ref="3f2a9b1",
                  config_text="prompts/fewshot3.yaml")
    record_results(session, project, e1, parse_results_table(_table([18, 19, 20])), dataset_version_id=version_id)
    add_file(session, project, blobs["wrong_types_E1.png"], original_name="오답유형_E1.png", kind=FileKind.FIGURE,
             caption="E1 R1의 오답 유형: 장문은 그대로이고 형식 오류가 늘었다", experiment=e1,
             run=get_run(session, e1, "R1"), derived_from_version_ids=[version_id])
    decide(session, project, e1, DecisionType.REJECT, reason="평균 오답 +1, 형식 오류 2건 증가")

    # E2: 문단 요약 → 채택(B1)
    e2 = create_experiment(
        session, project, problem_short_id=p2.short_id, title="문단 요약 후 답변",
        hypothesis="지문을 문단별로 요약한 뒤 답하면 장문 오답이 줄어든다",
        decision_rule="평균 오답 ≤ 17이고 다른 유형 악화 없음 → 채택 (n ≥ 3)",
    )
    update_inputs(session, e2, dataset_version_ids=[version_id], code_ref="a1b2c3d",
                  config_text="configs/summarize.yaml")
    record_results(session, project, e2, parse_results_table(_table([11, 12, 10])), dataset_version_id=version_id)
    add_file(session, project, blobs["summarize.yaml"], original_name="summarize.yaml", kind=FileKind.CONFIG,
             caption="E2 공통 설정", experiment=e2)
    add_file(session, project, blobs["wrong_types_E2.png"], original_name="오답유형_E2.png", kind=FileKind.FIGURE,
             caption="E2 R1의 오답 유형: 장문 오답 7 → 2", experiment=e2,
             run=get_run(session, e2, "R1"), derived_from_version_ids=[version_id])
    add_file(session, project, blobs["wrong_types.csv"], original_name="오답유형.csv", kind=FileKind.TABLE,
             caption="B0·E1·E2의 R1 오답 유형 집계", experiment=e2, derived_from_version_ids=[version_id])
    decide(session, project, e2, DecisionType.ADOPT, reason="목표 달성, 다른 유형 악화 없음",
           baseline_label="문단 요약 v1")
    return project
