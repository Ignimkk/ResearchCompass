"""Claude Code 결과 블록(rcompass-result) 해석, 미리보기, 확인 후 기록 (계획서 5절 "결과 회수").

흐름
1. 연구자가 Claude Code가 출력한 블록을 실험 화면에 붙여넣습니다.
2. 앱은 블록을 해석해 **미리보기(초안)**를 보여줍니다. 이때는 DB에 아무것도 쓰지 않습니다.
3. 연구자가 값을 고친 뒤 "확인하고 기록"을 누르면, 실행마다 Run으로 나눠 provenance=claude_confirmed로 기록합니다.

블록은 YAML이지만 값의 형 변환(예: ``no``→False, ``1e3``→1000.0)을 피하려고 PyYAML의 compose로 노드만 읽고,
값은 모두 글자 그대로 다룹니다. 그래서 오류마다 붙여넣은 텍스트 기준 줄 번호를 알려줄 수 있습니다.
파일 경로는 첨부 제안으로만 보여주고, 실제 파일은 연구자가 직접 올려야 연결됩니다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import yaml
from sqlalchemy.orm import Session

from rcompass.models import Experiment, Project, Provenance, Run
from rcompass.services import ValidationError
from rcompass.services.datasets import VersionView, list_versions
from rcompass.services.experiments import INPUTS_LOCKED, input_versions, update_inputs
from rcompass.services.handoff import RESULT_FENCE
from rcompass.services.projects import MAX_RUNS, METRIC_KEY_PATTERN
from rcompass.services.runs import RecordOutcome, parse_results_table, record_results

MAX_BLOCK_CHARS = 64 * 1024
KEYS = ("experiment", "dataset", "code", "config", "runs", "files", "note")
FENCE_OPEN = re.compile(rf"^\s*(```|~~~)\s*{RESULT_FENCE}\s*$")
PLACEHOLDER = re.compile(r"^<.*>$")


@dataclass
class BlockRun:
    line: int
    label: str
    values: dict[str, float]


@dataclass
class ResultBlock:
    experiment: str | None = None
    experiment_line: int = 0
    dataset: str | None = None
    dataset_line: int = 0
    code: str | None = None
    config: str | None = None
    runs: list[BlockRun] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def metrics(self) -> list[str]:
        """블록에 나온 지표 이름(처음 나온 순서)."""
        return list(dict.fromkeys(key for run in self.runs for key in run.values))

    def to_table(self) -> str:
        """미리보기에서 고칠 수 있는 '실행 × 지표' 표(탭 구분). 값이 없는 칸은 '-'입니다."""
        metrics = self.metrics
        rows = ["\t".join(["label", *metrics])]
        for run in self.runs:
            cells = [_number(run.values[key]) if key in run.values else "-" for key in metrics]
            rows.append("\t".join([run.label, *cells]))
        return "\n".join(rows)


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def _number(value: float) -> str:
    text = repr(float(value))
    return text[:-2] if text.endswith(".0") else text


# ---------------------------------------------------------------- 해석


def extract_block(text: str) -> tuple[str, int]:
    """붙여넣은 글에서 ```rcompass-result 블록을 꺼냅니다. (블록 본문, 본문 첫 줄의 줄 번호)

    펜스가 없으면 글 전체를 블록으로 봅니다. 그래서 Claude Code 출력 전체를 붙여넣어도 되고 블록만 붙여넣어도 됩니다.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = FENCE_OPEN.match(line)
        if match:
            fence = match.group(1)
            for end in range(index + 1, len(lines)):
                if lines[end].strip() == fence:
                    return "\n".join(lines[index + 1:end]), index + 2
            raise ValidationError({"block": f"{index + 1}번째 줄: 블록이 {fence}로 닫히지 않았습니다."})
    return text, 1


def _scalar(node: yaml.Node, line: int, name: str, problems: list[str]) -> str | None:
    if not isinstance(node, yaml.ScalarNode):
        problems.append(f"{line}번째 줄: {name}에는 값 하나를 적어야 합니다.")
        return None
    value = " ".join(str(node.value).split())
    if not value or PLACEHOLDER.match(value):
        return None  # 비었거나 형식 안내(<...>)를 그대로 둔 값
    return value


def parse_result_block(text: str) -> ResultBlock:
    """블록을 해석합니다. 문제가 있으면 줄 번호를 붙여 모두 모아 ValidationError(block=...)로 알립니다."""
    if not (text or "").strip():
        raise ValidationError({"block": "Claude Code가 출력한 rcompass-result 블록을 붙여넣어 주세요."})
    if len(text) > MAX_BLOCK_CHARS:
        raise ValidationError({"block": "블록이 너무 깁니다. rcompass-result 블록만 붙여넣어 주세요."})
    body, offset = extract_block(text)
    try:
        root = yaml.compose(body, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        where = f"{offset + mark.line}번째 줄: " if mark else ""
        raise ValidationError({"block": f"{where}블록 형식을 읽을 수 없습니다({error.problem or '형식 오류'})."}) from None
    except yaml.YAMLError:
        raise ValidationError({"block": "블록 형식을 읽을 수 없습니다."}) from None
    if not isinstance(root, yaml.MappingNode):
        raise ValidationError({"block": f"{offset}번째 줄: 'experiment:', 'runs:' 같은 항목으로 시작해야 합니다."})

    seen: set[int] = set()

    def line_of(node: yaml.Node) -> int:
        if id(node) in seen:  # 별칭(&, *)으로 같은 노드를 다시 쓰면 거부합니다(지수적 팽창 방지).
            raise ValidationError({"block": f"{offset + node.start_mark.line}번째 줄: 별칭(&, *)은 쓸 수 없습니다."})
        seen.add(id(node))
        return offset + node.start_mark.line

    block = ResultBlock()
    problems: list[str] = []
    present: set[str] = set()
    for key_node, value_node in root.value:
        line = line_of(key_node)
        key = str(key_node.value).strip() if isinstance(key_node, yaml.ScalarNode) else ""
        if key not in KEYS:
            problems.append(f"{line}번째 줄: '{key}'는 알 수 없는 항목입니다. 쓸 수 있는 항목: {', '.join(KEYS)}")
            continue
        if key in present:
            problems.append(f"{line}번째 줄: '{key}'가 두 번 있습니다.")
            continue
        present.add(key)
        value_line = line_of(value_node)
        if key == "runs":
            block.runs = _parse_runs(value_node, value_line, line_of, problems)
        elif key == "files":
            block.files = _parse_files(value_node, value_line, line_of, problems)
        else:
            value = _scalar(value_node, value_line, key, problems)
            if key == "experiment":
                block.experiment, block.experiment_line = (value.upper() if value else None), value_line
            elif key == "dataset":
                block.dataset, block.dataset_line = value, value_line
            else:
                setattr(block, key, value)
    if "runs" not in present:
        problems.append("'runs:' 아래에 실행별 결과를 적어야 합니다.")
    elif not block.runs and not problems:
        problems.append("기록할 실행이 없습니다.")
    if problems:
        raise ValidationError({"block": "\n".join(problems)})
    return block


def _parse_runs(node: yaml.Node, line: int, line_of, problems: list[str]) -> list[BlockRun]:
    if not isinstance(node, yaml.SequenceNode):
        problems.append(f"{line}번째 줄: runs는 '- {{label: ..., metrics: {{...}}}}' 목록이어야 합니다.")
        return []
    if len(node.value) > MAX_RUNS:
        problems.append(f"{line}번째 줄: 실행은 한 번에 {MAX_RUNS}개까지 기록할 수 있습니다.")
        return []
    runs: list[BlockRun] = []
    labels: set[str] = set()
    for item in node.value:
        item_line = line_of(item)
        if not isinstance(item, yaml.MappingNode):
            problems.append(f"{item_line}번째 줄: 실행은 {{label: ..., metrics: {{...}}}} 형태여야 합니다.")
            continue
        label, values, has_metrics = None, {}, False
        for key_node, value_node in item.value:
            key_line = line_of(key_node)
            key = str(key_node.value).strip() if isinstance(key_node, yaml.ScalarNode) else ""
            value_line = line_of(value_node)
            if key == "label":
                label = _scalar(value_node, value_line, "label", problems)
            elif key == "metrics":
                has_metrics = True
                values = _parse_metrics(value_node, value_line, line_of, problems)
            else:
                problems.append(f"{key_line}번째 줄: 실행에는 label과 metrics만 적을 수 있습니다('{key}').")
        if not label:
            problems.append(f"{item_line}번째 줄: 실행 이름(label)이 비어 있습니다. 예: seed=1")
            continue
        label = label.replace("\t", " ")
        if label in labels:
            problems.append(f"{item_line}번째 줄: 실행 이름 '{label}'이(가) 두 번 있습니다.")
        labels.add(label)
        if not has_metrics or not values:
            problems.append(f"{item_line}번째 줄: {label}에 기록할 지표 값이 없습니다.")
            continue
        runs.append(BlockRun(item_line, label, values))
    return runs


def _parse_metrics(node: yaml.Node, line: int, line_of, problems: list[str]) -> dict[str, float]:
    if not isinstance(node, yaml.MappingNode):
        problems.append(f"{line}번째 줄: metrics는 {{지표: 값}} 형태여야 합니다.")
        return {}
    values: dict[str, float] = {}
    for key_node, value_node in node.value:
        key_line = line_of(key_node)
        key = str(key_node.value).strip() if isinstance(key_node, yaml.ScalarNode) else ""
        value_line = line_of(value_node)
        if not METRIC_KEY_PATTERN.match(key):
            problems.append(f"{key_line}번째 줄: '{key}'는 지표 이름으로 쓸 수 없습니다(영문으로 시작, 영문·숫자·_ . -).")
            continue
        raw = _scalar(value_node, value_line, key, problems)
        if raw is None or raw == "-":
            continue  # 재지 않은 지표
        try:
            if "_" in raw:  # 1_000 같은 자릿수 구분은 오타일 수 있어 거부합니다.
                raise ValueError(raw)
            value = float(raw)
        except ValueError:
            problems.append(f"{value_line}번째 줄: {key}의 값 '{raw}'은(는) 숫자가 아닙니다.")
            continue
        if not math.isfinite(value):
            problems.append(f"{value_line}번째 줄: {key}의 값 '{raw}'은(는) 기록할 수 없습니다.")
            continue
        if key in values:
            problems.append(f"{key_line}번째 줄: {key}가 두 번 있습니다.")
        values[key] = value
    return values


def _parse_files(node: yaml.Node, line: int, line_of, problems: list[str]) -> list[str]:
    items = node.value if isinstance(node, yaml.SequenceNode) else [node]
    files = []
    for item in items:
        item_line = line_of(item) if item is not node else line
        value = _scalar(item, item_line, "files", problems)
        if value:
            files.append(value)
    return files


# ---------------------------------------------------------------- 미리보기


def resolve_dataset(session: Session, project: Project, ref: str) -> VersionView | None:
    """'DS1@v1.0', 'eval-100@v1.0', 버전이 하나뿐인 'DS1' 같은 표기를 데이터 버전으로 찾습니다."""
    name, _, label = ref.partition("@")
    name, label = name.strip(), label.strip()
    matches = [v for v in list_versions(session, project.id)
               if name in (v.dataset.short_id, v.dataset.name) and (not label or v.version.version_label == label)]
    return matches[0] if len(matches) == 1 else None


@dataclass
class Preview:
    block: ResultBlock
    table: str
    dataset: VersionView | None
    warnings: list[str] = field(default_factory=list)


def prepare_preview(session: Session, project: Project, experiment: Experiment, text: str) -> Preview:
    """블록을 해석하고 이 실험·프로젝트와 맞는지 확인합니다. DB에는 아무것도 쓰지 않습니다."""
    block = parse_result_block(text)
    problems: list[str] = []
    if block.experiment and block.experiment != experiment.short_id:
        problems.append(f"{block.experiment_line}번째 줄: 이 블록은 {block.experiment}의 결과인데 지금은 "
                        f"{experiment.short_id} 화면입니다. {block.experiment} 화면에서 붙여넣으세요.")
    dataset = None
    if block.dataset:
        dataset = resolve_dataset(session, project, block.dataset)
        if dataset is None:
            problems.append(f"{block.dataset_line}번째 줄: 데이터 버전 '{block.dataset}'을(를) 찾을 수 없습니다. "
                            "자료 화면에 등록한 뒤 'DS1@v1.0'처럼 적어 주세요.")
    if problems:
        raise ValidationError({"block": "\n".join(problems)})
    preview = Preview(block=block, table=block.to_table(), dataset=dataset)
    if not block.experiment:
        preview.warnings.append(f"블록에 experiment가 없어 지금 화면의 {experiment.short_id}에 기록합니다.")
    if block.code and experiment.code_ref and block.code != _flat(experiment.code_ref):
        preview.warnings.append(f"블록의 코드 버전({block.code})이 실험의 코드 버전({experiment.code_ref})과 달라, "
                                "이 실행들의 코드 버전으로 따로 기록합니다.")
    if block.config and experiment.config_text and block.config != _flat(experiment.config_text):
        preview.warnings.append("블록의 설정이 실험의 설정과 달라, 이 실행들의 설정으로 따로 기록합니다.")
    return preview


# ---------------------------------------------------------------- 확인하고 기록


def confirm_results(
    session: Session,
    project: Project,
    experiment: Experiment,
    *,
    table_text: str,
    dataset_version_id: int | None,
    code: str | None,
    config: str | None,
) -> RecordOutcome:
    """미리보기에서 연구자가 확인(필요하면 수정)한 값을 기록합니다. 호출한 쪽이 커밋합니다.

    - 실행마다 Run으로 나눠 provenance=claude_confirmed로 기록합니다.
    - 실험에 코드 버전·설정·데이터 버전이 비어 있으면 블록 값으로 채우고(채택했던 실험은 제외),
      이미 있는데 다르면 이번에 기록한 실행의 코드·설정으로 따로 남깁니다.
    """
    code = _flat(code) or None
    config = (config or "").strip() or None
    outcome = record_results(session, project, experiment, parse_results_table(table_text),
                             dataset_version_id=dataset_version_id, provenance=Provenance.CLAUDE_CONFIRMED)
    touched: list[Run] = outcome.created + outcome.updated
    can_fill = experiment.outcome not in INPUTS_LOCKED
    fill_code = can_fill and code and not experiment.code_ref
    fill_config = can_fill and config and not experiment.config_text
    fill_dataset = can_fill and dataset_version_id is not None and not input_versions(session, experiment)
    if fill_code or fill_config or fill_dataset:
        update_inputs(
            session, experiment,
            dataset_version_ids=[dataset_version_id] if fill_dataset else
            [v.version.id for v in input_versions(session, experiment)],
            code_ref=code if fill_code else experiment.code_ref,
            config_text=config if fill_config else experiment.config_text,
        )
    for run in touched:
        if code and code != _flat(experiment.code_ref):
            run.code_ref_override = code
        if config and _flat(config) != _flat(experiment.config_text):
            run.config_override = config
    session.flush()
    return outcome

