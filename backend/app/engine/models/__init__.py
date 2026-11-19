"""Statistical models (docs/specs/statistical-models.md): the catalogue every component reads
(`catalog.py`, its snapshot `catalog.json`) and the check of the table a model's runner
writes (`results.py`).

A model is not an engine operation: it runs as a Kubernetes Job in the image of its group,
and what comes back is a table of the statistics' ten columns. Nothing here fits a model.
"""
