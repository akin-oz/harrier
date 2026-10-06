"""The learned fit score (spec 077).

Four stages with typed handoffs: `features.validate` decides whether a job
can be judged at all, `features.extract` turns it into a fixed, named vector,
`model` scores that vector in plain Python from a JSON file, and
`model.fit_score_for` writes the result as the integer, signals and version
every reader already uses, falling back to the rules whenever the model
cannot judge.

Training is offline and lives apart: `labels` and `export` read the tracker
inside the container, `train` fits on the host from the export alone.
"""
