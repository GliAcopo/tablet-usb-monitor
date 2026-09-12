// One-shot companion to main.js, run by `tabs9 pin-desktop off` before the
// pinning script is unloaded.  main.js keeps its "pinned by us" set only in
// memory, so a script that is simply unloaded would leave every tablet window
// on all desktops.  This releases the windows that sit on the tablet output;
// windows the user pinned themselves *on the tablet* are released too — that
// is the documented cost of turning the feature off.
const OUTPUT_PREFIX = "Virtual-";
workspace.windowList().forEach(function (window) {
    if (window.specialWindow || window.deleted) {
        return;
    }
    if (window.output && window.output.name && window.output.name.startsWith(OUTPUT_PREFIX)
            && window.onAllDesktops) {
        window.onAllDesktops = false;
    }
});
