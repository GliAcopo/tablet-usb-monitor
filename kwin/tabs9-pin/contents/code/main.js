// KWin 6 script.  The host creates the tablet output through the portal, so
// its name always starts with "Virtual-".  KWin's virtual desktops are global
// to the workspace; the closest thing to "no desktops on that screen" is to
// keep every window that sits there on all desktops, and to release that
// again when the window comes back to a physical screen (including when the
// tablet output disappears and KWin moves its windows to the laptop).
const OUTPUT_PREFIX = "Virtual-";
const pinnedByUs = new Set();

function onTablet(window) {
    return window.output && window.output.name && window.output.name.startsWith(OUTPUT_PREFIX);
}

function sync(window) {
    if (window.specialWindow || window.deleted) {
        return;
    }
    if (onTablet(window)) {
        if (!window.onAllDesktops) {
            window.onAllDesktops = true;
            pinnedByUs.add(window);
        }
    } else if (pinnedByUs.has(window)) {
        pinnedByUs.delete(window);
        if (window.onAllDesktops) {
            window.onAllDesktops = false;
        }
    }
}

function track(window) {
    sync(window);
    window.outputChanged.connect(() => sync(window));
    window.closed.connect(() => pinnedByUs.delete(window));
}

workspace.windowList().forEach(track);
workspace.windowAdded.connect(track);
