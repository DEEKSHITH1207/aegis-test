"""Language-model analysis of a run's findings.

``prompts`` is pure: it turns findings into the text a model reads, and a
model's reply into checked dataclasses. ``client`` is the only module that
talks to the model provider. Persistence stays in ``apps.testing.services``.
"""
