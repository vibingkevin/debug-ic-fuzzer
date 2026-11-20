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
5. start_commit must build successfully with both baseline and SUT.
6. Base PRs on the latest main and validate locally. Run one PR at a time, and wait for all gates to pass before opening the next.
7. Keep configuration changes minimal; add workarounds only for problems reproduced in the selected project.
8. Cache reusable downloads, excluding IC/build/run state and lock files. Use stable project/platform/config keys with platform restore prefixes; save only on success.
9. Run commands directly in Actions logs; do not capture console output just to upload it. Keep structured results and required diagnostics.
10. Keep project badges at the top of README and append each new badge after the last; their order drives the twice-daily replay rotation. Enable rotation one project per PR with the scheduled input and run-name marker; keep schedules only in the rotation workflow.
