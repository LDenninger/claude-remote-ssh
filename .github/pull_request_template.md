## What and why

<!-- One or two sentences. Link the issue this closes: Closes #123 -->

## Type

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor or internal change
- [ ] Documentation or CI

## Testing

<!-- Which tests cover this change, and the platforms you ran them on. -->

- [ ] `python3 -m pytest -q` passes
- [ ] `python3 -m pytest tests/integration -q` passes (Linux)
- [ ] `claude plugin validate . --strict` passes
- [ ] A test fails without this change

## Overhead

<!-- Hook time per call, tool or skill description size, polling cost. Write "none" if unchanged. -->

## Checklist

- [ ] `CHANGELOG.md` has an entry under the unreleased version
- [ ] README and skills match the new behaviour
- [ ] No breaking change, or the breaking change is described above
