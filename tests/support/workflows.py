from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias


YamlScalar: TypeAlias = str | int | bool | None
YamlValue: TypeAlias = YamlScalar | list["YamlValue"] | dict[str, "YamlValue"]
YamlMapping: TypeAlias = dict[str, YamlValue]


@dataclass(frozen=True)
class WorkflowStep:
    workflow: "Workflow"
    job_id: str
    index: int
    data: Mapping[str, YamlValue]

    @property
    def name(self) -> str:
        return _string_value(self.data.get("name"), default=f"step {self.index + 1}")

    @property
    def uses(self) -> str:
        return _string_value(self.data.get("uses"))

    @property
    def run(self) -> str:
        return _string_value(self.data.get("run"))

    @property
    def with_values(self) -> Mapping[str, YamlValue]:
        return _mapping_value(self.data.get("with"))


@dataclass(frozen=True)
class Workflow:
    path: Path
    data: Mapping[str, YamlValue]

    @property
    def jobs(self) -> Mapping[str, YamlValue]:
        return _mapping_value(self.data.get("jobs"))

    def job(self, job_id: str) -> Mapping[str, YamlValue]:
        return _mapping_value(self.jobs.get(job_id))

    def steps(self, job_id: str) -> tuple[WorkflowStep, ...]:
        raw_steps = _sequence_value(self.job(job_id).get("steps"))
        steps: list[WorkflowStep] = []
        for index, raw_step in enumerate(raw_steps):
            step = _mapping_value(raw_step)
            if step:
                steps.append(WorkflowStep(self, job_id, index, step))
        return tuple(steps)

    def step_named(self, job_id: str, name: str) -> WorkflowStep | None:
        for step in self.steps(job_id):
            if step.name == name:
                return step
        return None


class _YamlParser:
    def __init__(self, text: str) -> None:
        self._lines = text.splitlines()
        self._index = 0

    def parse(self) -> YamlValue:
        next_line = self._peek_content()
        if next_line is None:
            return {}
        _, indent, text = next_line
        value = self._parse_node(indent)
        trailing = self._peek_content()
        if trailing is not None:
            line_number, _, trailing_text = trailing
            raise ValueError(
                f"unexpected trailing YAML content on line {line_number}: {trailing_text}"
            )
        return value

    def _parse_node(self, indent: int) -> YamlValue:
        next_line = self._peek_content()
        if next_line is None:
            return {}
        _, line_indent, text = next_line
        if line_indent < indent:
            return {}
        if text.startswith("- "):
            return self._parse_sequence(line_indent)
        return self._parse_mapping(line_indent)

    def _parse_mapping(self, indent: int) -> YamlMapping:
        mapping: YamlMapping = {}
        while True:
            next_line = self._peek_content()
            if next_line is None:
                break
            line_number, line_indent, text = next_line
            if line_indent < indent or text.startswith("- "):
                break
            if line_indent > indent:
                raise ValueError(f"unexpected nested YAML content on line {line_number}: {text}")
            key, value_text = _split_key_value(text, line_number)
            self._index = line_number
            mapping[key] = self._parse_value_after_key(indent, value_text)
        return mapping

    def _parse_sequence(self, indent: int) -> list[YamlValue]:
        values: list[YamlValue] = []
        while True:
            next_line = self._peek_content()
            if next_line is None:
                break
            line_number, line_indent, text = next_line
            if line_indent < indent or not text.startswith("- "):
                break
            if line_indent > indent:
                raise ValueError(f"unexpected nested YAML content on line {line_number}: {text}")
            item_text = text[2:].strip()
            self._index = line_number
            if not item_text:
                nested_line = self._peek_content()
                if nested_line is None or nested_line[1] <= indent:
                    values.append({})
                else:
                    values.append(self._parse_node(nested_line[1]))
                continue
            if _looks_like_mapping_item(item_text):
                values.append(self._parse_sequence_mapping_item(indent, item_text, line_number))
            else:
                values.append(_parse_scalar(item_text))
        return values

    def _parse_sequence_mapping_item(
        self,
        sequence_indent: int,
        item_text: str,
        line_number: int,
    ) -> YamlMapping:
        key, value_text = _split_key_value(item_text, line_number)
        mapping: YamlMapping = {key: self._parse_value_after_key(sequence_indent, value_text)}
        next_line = self._peek_content()
        if next_line is not None and next_line[1] > sequence_indent:
            nested = self._parse_mapping(next_line[1])
            mapping.update(nested)
        return mapping

    def _parse_value_after_key(self, indent: int, value_text: str) -> YamlValue:
        value_text = value_text.strip()
        if not value_text:
            next_line = self._peek_content()
            if next_line is None or next_line[1] <= indent:
                return {}
            return self._parse_node(next_line[1])
        if value_text in {"|", "|-", "|+", ">", ">-", ">+"}:
            return self._parse_block_scalar(indent)
        return _parse_scalar(value_text)

    def _parse_block_scalar(self, parent_indent: int) -> str:
        start = self._index
        end = start
        block_indents: list[int] = []
        for raw_index in range(start, len(self._lines)):
            raw_line = self._lines[raw_index]
            if not raw_line.strip():
                end = raw_index + 1
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent <= parent_indent:
                break
            block_indents.append(indent)
            end = raw_index + 1
        if not block_indents:
            self._index = end
            return ""
        trim_indent = min(block_indents)
        block_lines = [
            line[trim_indent:] if len(line) >= trim_indent else ""
            for line in self._lines[start:end]
        ]
        self._index = end
        return "\n".join(block_lines).rstrip("\n")

    def _peek_content(self) -> tuple[int, int, str] | None:
        for raw_index in range(self._index, len(self._lines)):
            raw_line = self._lines[raw_index]
            stripped = raw_line.strip()
            if not stripped or stripped == "---" or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            return raw_index + 1, indent, raw_line[indent:]
        return None


def load_workflow(path: str | Path) -> Workflow:
    workflow_path = Path(path)
    data = _YamlParser(workflow_path.read_text(encoding="utf-8")).parse()
    if not isinstance(data, dict):
        raise ValueError(f"{workflow_path} did not parse to a YAML mapping")
    return Workflow(path=workflow_path, data=data)


def _mapping_value(value: YamlValue | None) -> Mapping[str, YamlValue]:
    if isinstance(value, dict):
        return value
    return {}


def _sequence_value(value: YamlValue | None) -> Sequence[YamlValue]:
    if isinstance(value, list):
        return value
    return ()


def _string_value(value: YamlValue | None, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return default


def _split_key_value(text: str, line_number: int) -> tuple[str, str]:
    quote: str | None = None
    for index, character in enumerate(text):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == ":" and quote is None:
            key = _parse_key(text[:index].strip())
            return key, text[index + 1 :]
    raise ValueError(f"expected YAML key/value pair on line {line_number}: {text}")


def _parse_key(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _looks_like_mapping_item(value: str) -> bool:
    try:
        _split_key_value(value, 0)
    except ValueError:
        return False
    return True


def _parse_scalar(value: str) -> YamlScalar | list[YamlValue]:
    value = _strip_inline_comment(value).strip()
    if not value:
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part) for part in _split_inline_list(inner)]
    if value == "true":
        return True
    if value == "false":
        return False
    if value in {"null", "~"}:
        return None
    if value.isdecimal():
        return int(value)
    return value


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == "#" and quote is None and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _split_inline_list(value: str) -> list[str]:
    parts: list[str] = []
    quote: str | None = None
    start = 0
    for index, character in enumerate(value):
        if character in {'"', "'"}:
            if quote == character:
                quote = None
            elif quote is None:
                quote = character
        if character == "," and quote is None:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return parts
