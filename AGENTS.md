# Development guidelines

- Keep the implementation simple, don't add features not explicitly asked for, reuse existing libraries.
- Use English for everything in this repo.
- For markdown there's no line length limit. Don't do new lines inside a paragraph
- In GitHub Actions, cache reusable Gradle user-home caches, wrapper distributions, Kotlin/Native dependency downloads, and applicable package downloads. Exclude Native IC state (including `.auto-cache` and `kotlin-native-ic-cache`), project build/run state, and lock files; restore and save separately so failed replays can save downloaded dependencies, and use fresh keys with restore prefixes so later runs can extend partial caches.
- Run workflow commands directly so their output appears in the Actions log. Do not pipe, tee, or redirect console output into files just to upload it again as artifacts; keep structured results and diagnostic files required by the tool.

## Project replay rules

1. Changes to a project's configuration, workflow, init script, or compiler configuration must trigger that project's workflow.
2. Prefer an iOS device target (iosArm64), and verify that the selected task exists throughout the commit range.
3. Run the full range in one job when it can finish within GitHub Actions' six-hour limit; do not add pagination, a matrix, or a guard for that range. Only paginate when necessary, using measured timings to choose the largest practical page size rounded to a multiple of five, with room for setup and artifact upload. Do not add redundant max-parallel or timeout-minutes settings.
4. Select the longest practical range supported by Kotlin 2.4.21. If dependency versions prevent many commits from building, first try a minimal version override in the init script; only then shrink the range toward the middle if necessary.
5. Verify that start_commit builds successfully with both baseline and SUT. Two failed builds do not count as a successful commit. Print the number of successfully built commits at the end of the replay. The replay script must return failure when every selected commit is both_failed; do not add a separate GitHub Actions build-result check.
6. Prefer end_commit: latest. Pin an earlier end only when the latest commit cannot build with Kotlin 2.4.21 after the necessary dependency overrides.
7. Base each PR on the latest main and validate the selected project locally on Studio before opening or updating it. GitHub Actions has a five-job macOS runner limit; execute only one PR at a time and wait for all gates to pass before opening the next PR.
8. Keep configuration changes minimal. Add dependency overrides, linker options, and other workarounds only for problems reproduced in the selected project; record the evidence and remove any option that is not needed.
