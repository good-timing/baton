## Summary

<!-- One or two sentences: what changed and why. -->

## Scope

- [ ] Bug fix
- [ ] New feature (opt-in / backwards-compatible)
- [ ] Breaking change (requires deprecation cycle per CONTRIBUTING.md)
- [ ] Spec change (also updates `docs/SPEC.md` + `docs/SPEC.md §13` changelog)
- [ ] Documentation only
- [ ] Refactor / no behavior change

## Linked issue

<!-- Fixes #NNN / Refs #NNN — or "none" if this is a small standalone change. -->

## Test plan

<!-- How did you verify this works? Include the commands you ran (`make ci` is the canonical gate). -->

```sh
# e.g.:
make ci
```

## Spec-first checklist

- [ ] If this changes the wire format, `docs/SPEC.md` and `docs/SPEC.md §13` are updated in this PR.
- [ ] If this changes a load-bearing decision, `docs/CHARTER.md` reflects the new posture.
- [ ] Public API additions are exported from `src/baton/__init__.py` (or the appropriate `integrations/<name>/__init__.py`).
- [ ] Failing test was written first; now passes.

## Reviewer notes

<!-- Anything reviewers should focus on. Trade-offs, alternatives you considered and rejected, things you weren't sure about. -->
