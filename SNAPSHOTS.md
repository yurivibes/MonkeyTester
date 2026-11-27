# Snapshots and layout checks

Two test steps check fonts, styles and asset placement on real devices. Both work on every
screen size because they measure in points (iOS) or dp (Android), never raw pixels.

- `DIFF: SNAPSHOT` compares the screen with a baseline image kept in the test case.
- `DIFF: ASSERT_LAYOUT` checks where tagged elements are relative to each other or to the screen.

Both are actions of the **DIFF plugin** (MonkeyTester-Plugin-Diff, a separate service added on
Settings → Plugins like any test plugin; its `/health` name is `DIFF`). The plugin only compares
and keeps nothing between jobs; the runner captures. Before each job it fills the action's
runner-supplied params (PLUGIN_API.md, "Runner-supplied params"): the screenshot (uploaded to the
plugin as a PNG), the controller's `/screen` (with `scale`), `/elements` (for `ASSERT_LAYOUT`, and
for `SNAPSHOT` only with a `tag:` mask), the platform, record mode, the case's baselines (as a
`snapshots/…` zip) and the raw text after the action. Without the
plugin the step fails with `DIFF plugin is not configured: add MonkeyTester-Plugin-Diff on
Settings → Plugins`; there is no fallback in the runner.

The old bare `SNAPSHOT:` / `ASSERT_LAYOUT:` lines (also with an `IF IOS:` / `IF ANDROID:` prefix)
are parse errors that name the replacement, e.g. `SNAPSHOT: steps were removed; write
"DIFF: SNAPSHOT eula mask=status_bar" (line 3)`. The arguments are unchanged: put `DIFF: ` in
front and drop the colon after the step name.

Both need a Mobile Controller whose `GET /devices/<id>/screen` returns `scale` and which serves
`GET /devices/<id>/elements` (MonkeyTester-Plugin-Mobile-Controller API.md). With an older controller the
steps fail and name the missing piece.

## SNAPSHOT

```
DIFF: SNAPSHOT <name> [mask=<mask>[,<mask>...]] [tolerance=<percent>]
```

Baselines live in the case folder, one per screen class:

```
mt-tests/<case>/
  INSTRUCTION.txt
  eula_fresh_install.txt
  snapshots/
    ios/375x667@2x/eula.png          iPhone SE
    ios/393x852@3x/eula.png          iPhone 14 Pro / 15 Pro / 16
    android/411x910@2.625x/eula.png  moto g power
```

- **Screen class** = screen size in pt/dp, plus the scale: pixels / scale, scale printed with `%g`.
  Phones with the same screen share a baseline. The plugin computes the class from the screenshot
  and the scale the controller reports, so a test never names a device.
- **Images are stored at 1x.** The capture is box-averaged from device pixels to pt/dp, which
  keeps files small and evens out anti-aliasing. Comparison happens at 1x.
- **Comparison:** a pixel differs when any channel differs by more than 24. The step fails when
  more than `tolerance` percent (default `0.1`) of the unmasked pixels differ.
- **Masks** exclude areas that change between runs:
  - `status_bar`: the system status bar (iOS 20 pt on home-button phones, 59 pt on taller ones;
    Android 52 dp).
  - `nav_bar`: the home indicator (34 pt, taller iPhones only) or the Android navigation bar (48 dp).
  - `tag:<testTag>`: an element's bounds from the controller, e.g. a live temperature reading.
  - `rect:x,y,w,h`: a rectangle in pt/dp.

### Outcomes

| Status | Step | When |
|--------|------|------|
| `match` | pass | within tolerance |
| `mismatch` | fail | too many pixels differ, or the baseline has a different size |
| `missing` | fail | no baseline for this screen class. The message lists the classes that have one. |
| `recorded` | pass | record mode, not compared |
| `error` | fail | bad arguments, no scale from the controller, capture failed |

Each step writes `outputs/snapshots/<name>.actual.png` (`outputs/<case>/snapshots/` in a
multi-case run) and, when compared, `<name>.baseline.png` and `<name>.diff.png`: the plugin
returns the 1x capture and the diff as base64 PNGs in its job result, the runner writes them and
copies the baseline from the case. In the diff, differing pixels are red, masked areas
are blue and everything else is dimmed. The result step carries
`snapshot: {name, status, screen_class, baseline_path, diff_percent, tolerance, message, actual,
baseline, diff}`. `test.html` shows the three images under the step.

### New baselines

A new screen, a new device class or an intended change all fail first. Then:

1. On the run page, **New baselines (N)** downloads a zip of every `missing`, `mismatch` and
   `recorded` capture, laid out as `mt-tests/<case>/snapshots/<platform>/<class>/<name>.png`.
   `<case>` comes from the upload's `case` field (CI sends the folder name). Without it the zip
   uses `CASE`.
2. Unzip it into the app repo on a branch, check the images, commit and open a PR. The viewer
   never writes to a repo.

**Record mode** (`--record-snapshots`, or **Record snapshot baselines** on the new test page)
saves every `DIFF: SNAPSHOT` as a candidate without comparing (the runner sends it to the plugin
as the `source: record` param). The run's CI status is `recording`, and
the run list shows **Recording**. A record run never counts as passed. CI never turns it on.

### Ownership

A screen's baseline belongs to one case. In TemperatureSensor-App, `eula` owns the EULA and Home
baselines. Other cases snapshot only the screens they are about, so one redesign means one
re-baseline.

### Uploads

`ci/mt-submit/submit.py` sends every `snapshots/*/*/*.png` of a case as a `snapshots` part
whose filename is the path relative to the case (`snapshots/ios/375x667@2x/eula.png`), plus
`case=<folder name>`. The viewer accepts only paths that match
`snapshots/<android|ios>/<W>x<H>@<scale>x/<name>.png`, PNG content and at most 8 MB per file
(1000 files), and stages them next to the test files.

## ASSERT_LAYOUT

Tags are Compose `Modifier.testTag` values. On Android the app must set
`testTagsAsResourceId = true` on a root semantics node so uiautomator sees them. On iOS, Compose
Multiplatform exposes them as the accessibility identifier. Tags must be unique on screen.

```
DIFF: ASSERT_LAYOUT <tag> visible
DIFF: ASSERT_LAYOUT <tag> inside screen
DIFF: ASSERT_LAYOUT <tag> inside <tag2>
DIFF: ASSERT_LAYOUT <tag> centered-x [tol=N]                 also centered-y
DIFF: ASSERT_LAYOUT <tag> below <tag2> [gap>=N] [gap<=N]     also above, left-of, right-of
DIFF: ASSERT_LAYOUT <tag> no-overlap <tag2>
DIFF: ASSERT_LAYOUT <tag> aligned-left <tag2> [tol=N]        also -right -top -bottom -center-x -center-y
```

`N` is pt/dp, or a percentage of the screen width (x checks) or height (y checks) with a
trailing `%`. The default tolerance is 2. A failure names the boxes it compared, and a missing
tag lists the tags that are on screen. The result step carries `layout: {passed, message}`
(from the plugin's job result).

Example for the EULA screen:

```
DIFF: ASSERT_LAYOUT eula_title centered-x
DIFF: ASSERT_LAYOUT eula_agree below eula_terms gap>=8
DIFF: ASSERT_LAYOUT eula_agree inside screen
DIFF: SNAPSHOT eula mask=status_bar
```
