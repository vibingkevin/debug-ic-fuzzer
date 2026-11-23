# Development guidelines

- Use English thruout this repo
- For markdown there's no line length limit. Don't do new lines inside a paragraph
- Keep the implementation simple
    - don't add features not explicitly asked for
    - reuse existing libraries
    - any configuration need to solve a problem that has actually accured
- Don't reference local information like local hostname in public communications. Those doesn't mean anything to others.

## Project replay rules

1. Changes to project configs, workflows, init scripts, or compiler configs must trigger the affected project's workflow.
2. Prefer iOS device targets (iosArm64). If the build task changes across the commit range, use multiple project configuration files.
3. Do not paginate ranges that fit in one six-hour job. Otherwise, use measured timings to choose the largest practical page size, rounded to a multiple of five.
4. Maximize the range supported by Kotlin 2.4.21 and prefer end_commit: latest. Try minimal dependency overrides before shrinking the range toward the middle.
5. Choose the first-parent commit that first upgrades the project to Kotlin 2.4.x as start_commit. Use a later start only after a minimal init-script or version override cannot make that boundary build with Kotlin 2.4.21; record the blocking error and the omitted commits.
6. start_commit must build successfully with both baseline and SUT.
7. Base PRs on the latest main and validate locally. Run one PR at a time, and wait for all gates to pass before opening the next. Every PR must run and pass all six IC sample checks.
8. Keep configuration changes minimal; add workarounds only for problems reproduced in the selected project.
9. Cache reusable downloads, excluding IC/build/run state and lock files. Use stable project/platform/config keys with platform restore prefixes; save only on success.
10. Run commands directly in Actions logs; do not capture console output just to upload it. Keep structured results and required diagnostics.
11. Keep project badges at the top of README and append each new badge after the last; their order drives the twice-daily replay rotation. Enable rotation one project per PR with the scheduled input and run-name marker; keep schedules only in the rotation workflow.
12. Use the highest JDK version supported by the project and its build tools across the selected commit range.

## Pull request checks

After opening a pull request, wait for its gates with `gh pr checks <PR_NUMBER> --repo vibingkevin/debug-ic-fuzzer --watch --fail-fast --interval 60`. Investigate and fix any failed check before reporting the pull request as ready.
