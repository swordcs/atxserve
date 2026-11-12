# Jinja: 30 Parent Features and 100 Derived Subtasks

This package contains the public requirements for the 30 original Jinja features and 100 derived subtasks used in our experiments, together with their parent-child mappings. It contains no features from other repositories, reference implementations, gold patches, or hidden test code. Original English requirement bodies are preserved.

## Definitions

- **Parent feature:** A complete original CooperBench feature requirement describing expected behavior, interfaces, edge cases, and compatibility requirements. A feature may contain several independently verifiable behaviors.
- **Derived subtask / acceptance policy:** A limited development behavior selected from a parent feature, such as a parameter's semantics, an input boundary, error handling, or a compatibility rule. Synchronous/asynchronous variants and equivalent acceptance checks are merged into one behavioral subtask where applicable.
- The 100 subtasks are derived from these 30 parent features. They are not 100 independent original features. Subtasks may share APIs or implementation, and one implementation may satisfy multiple subtasks.
- Each complete subtask input includes its parent feature as background. The delivery scope is defined by `CURRENT ASSIGNED POLICY`; the background does not instruct the agent to implement the entire parent feature again.
- The unique parent key is `source_task_id + original_feature_id`. Features with the same number in different source tasks are distinct features.

## Workload Scope

| Source task / feature pool | Main source file | Parent features | Selected subtasks |
|---|---|---:|---:|
| task1465 / grouping_filters | src/jinja2/filters.py | 10 | 31 |
| task1559 / internationalization | src/jinja2/ext.py | 10 | 33 |
| task1621 / template_loaders | src/jinja2/loaders.py | 10 | 36 |
| Total | Jinja | 30 | 100 |

The three source pools were adapted to the common source baseline `896a62135bcc151f2997e028c5125bec2cb2431f`. The 100 subtasks were selected using source-method order and round-robin selection across parent features. Each parent contributes between one and four selected subtasks.

The construction records describe 298 candidate test methods, 244 methods that fail on the baseline and pass with the composite reference implementation, 187 merged behavioral tasks, and a fixed selection of 100 subtasks corresponding to 150 acceptance selectors. The test code for those selectors is not included in this export.

## File Index

| File or directory | Contents |
|---|---|
| [Parent feature definitions](02_Jinja_30_Parent_Feature_Definitions.md) | Complete original requirements for all 30 parent features |
| [Subtask definitions](03_100_Subtask_Definitions.md) | All 100 complete subtask requirements, grouped by parent feature |
| [Grouped JSONL](jinja_30features_100subtasks_grouped.jsonl) | 100 records, one per subtask, containing the full parent definition and subtask requirement; records from the same parent are adjacent |
| [Parent-child mapping CSV](parent_child_mapping_100.csv) | One row per subtask with its parent, assigned policy, and group |
| [Parent summary CSV](parent_children_summary_30.csv) | Selected subtask counts and IDs for each parent feature |
| [Parent CSV](jinja_features_30.csv) / [Parent JSON](jinja_features_30.json) | Complete parent definitions and associations |
| [Subtask CSV](subtasks_100.csv) / [Subtask JSON](subtasks_100.json) | Complete subtask inputs, acceptance policies, provenance, and relative file references |
| original_features/ | 30 individual original feature definition files |
| subtasks/ | 100 individual subtask requirement files |
| [Export provenance](provenance/export_info.json) | Frozen manifest location, counts, grouping, and export conventions |
| [Export validation](provenance/validation.json) | Validation information from the original export |

CSV files use UTF-8 with a byte-order mark. Requirement cells contain embedded newlines; use a standard CSV parser or Excel import. Requirements in JSON files are plain text. This package is intended for reading and data processing and is not a replacement launcher configuration for the original executor.

## Provenance

Subtasks come from the frozen formal-experiment inputs in `experiments/comparison_v2/code/inputs/manifest.json` and its `requirements/` directory. Parent definitions come from the 30 public `feature.md` files referenced by that manifest.

The original export copied requirements and organized indexes without regenerating requirements, running models, or executing evaluations. This English edition translates the documentation and wrapper headings, renames the two compiled Markdown files, and updates their references. Existing English requirement bodies and the original experiment data are preserved.
