# Project replay conventions

The rules for adding and maintaining a project replay. The first seven are the
working agreement; the notes under each one record the failure it prevents.

## 1. A project's replay runs when its config, workflow or init script changes

`pull_request.paths` lists the workflow file, the project config, every init
script the config references, and the shared compiler/entry files it depends on.
A workflow without paths only ever runs by hand, so a config change goes
unverified. Reviewing a project means reviewing its trigger too.

## 2. Target the iOS device when the project ships one

Prefer `linkDebugFrameworkIosArm64` over the simulator variants. The device
target is what the project actually ships and what the IC differences are
reported against. Some projects only have a simulator target; take the device
one whenever it exists.

## 3. Pages finish inside 6h, are as large as possible, and round to 5 commits

`--commit-count` is a page in commits, and the `start_at` matrix step must match
it. Measure one page, then scale to just under the 6h ceiling — aim for 4-5h of
work with a margin. Round the page size to a multiple of 5 (45, 50, 80, 85) so
the matrix and the range arithmetic stay readable.

Do not copy another project's page size: Confetti measured 45, Animeko 80,
KotlinConf 65-80, NYTimes 1000+ commits per page.

The guard page sits past the end of the range so the empty-range check fires.

## 4. Take the longest range 2.4.21 still builds, shrinking inward if needed

Start as early as the pinned compiler still supports. When a dependency version
blocks the earlier commits, override it in an init script referenced from the
config (SKIE, Koin compiler plugin, Gradle wrapper) and retry. If the range still
cannot build after the override, give up the unusable commits and move the start
point forward — but keep the range as long as the compiler allows.

## 5. `start_commit` must build

Being on the first-parent line is necessary but not sufficient: the CLI rejects
off-branch endpoints with exit 2, and an unbuildable start produces a page of
`Both Failed`. Probe the first commit before opening the PR.

## 6. `end_commit` is `latest` unless the newest commit does not build on 2.4.21

Pinning the end freezes the range; `latest` keeps the replay honest as the
project moves. Only pin when the newest revisions need a compiler newer than
2.4.21, and note the reason in the config.

## 7. One PR verifies one project: local pass, all-green GHA, Feishu, merge, then next

Open a PR for a single project and wait for it to be green before starting the
next. Batch changes start several replays at once and saturate the five macOS
runners (PR #33: eight workflows, 69 failures). Send the link to Han Lin only
once every check is green, so each message is a merge-ready PR.

## Supporting notes

- The per-commit worktree checkout must be forced (`git checkout --force
  --detach`, fixed in PR #41). A tracked file left modified by an interrupted
  build otherwise aborts a whole page with exit 2.
- Fix the compiler download step to a single attempt; no retry loop.
- Rebase onto the latest `main` before merging: every project PR appends its
  badge to the same README line, so any two open PRs conflict there.
- Do not edit a branch another session is also touching; force pushes overwrite
  each other.
- A failed page is not always a bug. A `production_diff` or a sut build error is
  a finding; record it and keep the workflow intact instead of papering over it.
- Explain every dependency, argument and step in the PR body with the evidence
  that shows it is needed; do not copy a template.
