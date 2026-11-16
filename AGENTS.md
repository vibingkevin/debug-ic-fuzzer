# Development guidelines

- Keep the implementation simple, don't add features not explicitly asked for, reuse existing libraries.
- Use English for everything in this repo.
- For markdown there's no line length limit. Don't do new lines inside a paragraph
- In GitHub Actions, cache reusable Gradle user-home caches, wrapper distributions, Kotlin/Native dependency downloads, and applicable package downloads. Exclude Native IC state (including `.auto-cache` and `kotlin-native-ic-cache`), project build/run state, and lock files; restore and save separately so failed replays can save downloaded dependencies, and use fresh keys with restore prefixes so later runs can extend partial caches.
- Run workflow commands directly so their output appears in the Actions log. Do not pipe, tee, or redirect console output into files just to upload it again as artifacts; keep structured results and diagnostic files required by the tool.
