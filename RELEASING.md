# Releasing

## First public release

1. Merge the release-readiness PRs and make the repository public.
2. Confirm required CI checks pass on Intel and Apple Silicon.
3. Require approval on the `release` environment and protect the `v*` tag pattern with a repository ruleset restricted to release maintainers.
4. Change `Unreleased` in `CHANGELOG.md` to the release version and date.
5. Set the same version in `setup.cfg`.
6. Create and push an annotated tag: `git tag -a v0.1.0 -m "Konvu Telemetry v0.1.0" && git push origin v0.1.0`.
7. Verify the GitHub release contains the wheel, source archive, and `SHA256SUMS`.
8. Verify each release artifact with `gh attestation verify <artifact> --repo KonvuInc/konvu-telemetry`.
9. Update the Homebrew formula so its source URL and SHA-256 match the source archive.
10. Test `brew install konvuinc/tap/konvu-telemetry` and `konvu-telemetry setup` on clean Intel and Apple Silicon accounts.

## Formula requirements

The formula should use the tagged source archive, depend on a Homebrew Python version supported by `python_requires`, install the package in an isolated virtual environment, and start an isolated server in its test block to verify `/healthz`. Do not install or start the LaunchAgent from the formula; the user controls that through `konvu-telemetry setup`.

## Rollback

Delete a broken release only before announcing it, fix forward with a new version, and never move an existing tag. Users must run `konvu-telemetry uninstall` before downgrading or removing the formula so the installed binary can clean up its service and integrations.
