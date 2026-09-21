# Releasing

## First public release

1. Merge the release-readiness PRs and make the repository public.
2. Confirm required CI checks pass on Intel and Apple Silicon.
3. Change `Unreleased` in `CHANGELOG.md` to the release version and date.
4. Set the same version in `setup.cfg`.
5. Create and push an annotated tag: `git tag -a v0.1.0 -m "Konvu Telemetry v0.1.0" && git push origin v0.1.0`.
6. Verify the GitHub release contains the wheel, source archive, and `SHA256SUMS`.
7. Create the public `KonvuInc/homebrew-tap` repository with a formula whose source URL and SHA-256 match the source archive.
8. Test `brew install konvuinc/tap/konvu-telemetry` and `konvu-telemetry setup` on clean Intel and Apple Silicon accounts.
9. Remove the pre-release qualifier from the README install section.

## Formula requirements

The formula should use the tagged source archive, depend on a Homebrew Python version supported by `python_requires`, install the package in an isolated virtual environment, and run `konvu-telemetry --help` in its test block. Do not install or start the LaunchAgent from the formula; the user controls that through `konvu-telemetry setup`.

## Rollback

Delete a broken release only before announcing it, fix forward with a new version, and never move an existing tag. Users can stop integrations with `konvu-telemetry uninstall` before downgrading the formula.
