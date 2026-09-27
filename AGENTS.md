# Notes for coding agents

## Committing, pushing and publishing: autonomous

The maintainer has authorized agents working in this repository to do all of
the following **without asking first**:

- commit finished work and push it to `origin/main`;
- publish a GitHub release when the Android app's version changes (or when
  the maintainer asks for one).

Still ask before anything else outward-facing or hard to undo: deleting or
re-tagging an existing release, force-pushing, rewriting published history,
changing repository settings or visibility.

### Commit rules

- Author e-mail is the GitHub noreply address already set in the repo-local
  `git config user.email`; never put a real e-mail in a commit.
- Run `python3 -m unittest discover -s tests` first; do not push red tests.
- End the message with the agent's `Co-Authored-By:` line.

### Release procedure

`./tabs9 setup` downloads the release tagged `v<versionName>` (from
`android/app/build.gradle.kts`) and checks the APK against the first
64-hex-digit SHA-256 in the release notes. A version bump pushed without its
release breaks setup on fresh clones, so publish in the same session:

1. Bump `versionCode` and `versionName` in `android/app/build.gradle.kts`,
   commit and push; the working tree must be clean.
2. `scripts/build-android.sh`: runs the JVM tests, builds the debug APK into
   `.local/artifacts/tab-s9-usb-display-debug.apk` and prints its SHA-256.
   The build is reproducible, so the same commit gives the same hash.
3. Install it on an attached tablet and check it streams, when one is attached.
4. `gh release create v<versionName> .local/artifacts/tab-s9-usb-display-debug.apk
   --target <pushed commit> --title "tabs9 <version>: <headline>" --notes-file <notes>`.
   Notes follow the earlier releases: what changed since the last one, **which
   hardware it was tested on** (only what was actually run), the APK name,
   versionName/versionCode, and `SHA-256: \`<hash>\`` (setup parses it).
5. Verify: `python3 -c "import sys; sys.path.insert(0,'scripts'); import setup;
   print(setup.release_apk('v<versionName>'))"` returns the asset and the same
   hash, and a downloaded copy of the asset has that hash.

## Other standing rules

- Never print or commit device serial numbers, tokens, pairing secrets or
  desktop screenshots; `.local/` is ignored and holds all of them.
- The tablet's own prompts (package installer) may be answered over adb;
  never automate KDE's consent dialogs on the computer.
- Do not disturb the computer's screen while testing (no windows, pointer
  moves or clipboard changes there). If a test must touch it, send a desktop
  notification and a sound first.
- State tested hardware explicitly in the README and release notes.
