import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.SystemClock;
import android.util.Log;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.Set;

/**
 * The PC's mouse and keyboard, delivered to whatever is on the tablet's
 * screen. Runs on the tablet as the shell user over ADB:
 *
 *   app_process -cp /data/local/tmp/tabs9-remote.dex / Remote
 *
 * and listens on the abstract Unix socket "tabs9-remote" (the host reaches
 * it with "adb forward tcp:PORT localabstract:tabs9-remote"). One client at
 * a time sends fixed 12-byte little-endian records:
 *
 *   u8 type, u8 flags, u16 code, i32 a, i32 b
 *
 *   type 1 MOVE    a=x b=y      absolute display pixels; the pointer hovers
 *                               there, or drags if a button is held
 *   type 2 BUTTON  code=evdev BTN_* (0x110 left, 0x111 right, 0x112 middle),
 *                  flags=1 press / 0 release, a=x b=y
 *   type 3 SCROLL  a=vertical, b=horizontal, in thousandths of a wheel notch
 *                  (Android's AXIS_VSCROLL/AXIS_HSCROLL sign: positive = up/left)
 *   type 4 KEY     code=evdev key code, flags=1 press / 0 release
 *   type 5 RESET   release every button and key
 *
 * Events go through InputManager.injectInputEvent like UI Automator's, so
 * they reach system UI and desktop-mode windows alike. Keys are injected
 * with Android's virtual keyboard device: the key layout is the US one that
 * Generic.kcm defines, whatever the PC's layout (the evdev code names the
 * physical key, the character comes from the tablet's map).
 *
 * The evdev -> KEYCODE table is Android 16's Generic.kl, generated at
 * build time (see build-and-push.sh); modifier state is tracked here so
 * Shift/Ctrl/Alt/Meta combinations arrive with the right meta flags.
 */
public class Remote {
    static final String TAG = "UScreenRemote";

    /** Everything goes to logcat: adb-shell stdout has nobody reading it. */
    private static void log(String message) {
        Log.i(TAG, message);
        System.out.println(message);
    }

    static final int TYPE_MOVE = 1, TYPE_BUTTON = 2, TYPE_SCROLL = 3, TYPE_KEY = 4, TYPE_RESET = 5;
    static final int BTN_LEFT = 0x110, BTN_RIGHT = 0x111, BTN_MIDDLE = 0x112;

    // Generic.kl as (evdev, keycode) pairs.
    static final int[] KEY_TABLE = {1,111, 2,8, 3,9, 4,10, 5,11, 6,12, 7,13, 8,14, 9,15, 10,16, 11,7, 12,69, 13,70, 14,67, 15,61, 16,45, 17,51, 18,33, 19,46, 20,48, 21,53, 22,49, 23,37, 24,43, 25,44, 26,71, 27,72, 28,66, 29,113, 30,29, 31,47, 32,32, 33,34, 34,35, 35,36, 36,38, 37,39, 38,40, 39,74, 40,75, 41,68, 42,59, 43,73, 44,54, 45,52, 46,31, 47,50, 48,30, 49,42, 50,41, 51,55, 52,56, 53,76, 54,60, 55,155, 56,57, 57,62, 58,115, 59,131, 60,132, 61,133, 62,134, 63,135, 64,136, 65,137, 66,138, 67,139, 68,140, 69,143, 70,116, 71,151, 72,152, 73,153, 74,156, 75,148, 76,149, 77,150, 78,157, 79,145, 80,146, 81,147, 82,144, 83,158, 85,211, 86,73, 87,141, 88,142, 89,217, 92,214, 93,215, 94,213, 95,159, 96,160, 97,114, 98,154, 99,120, 100,58, 102,122, 103,19, 104,92, 105,21, 106,22, 107,123, 108,20, 109,93, 110,124, 111,112, 113,164, 114,25, 115,24, 116,26, 117,161, 119,121, 120,312, 121,159, 122,218, 123,212, 124,216, 125,117, 126,118, 127,82, 128,86, 133,278, 135,279, 137,277, 139,82, 140,210, 142,223, 143,224, 150,64, 152,324, 155,65, 156,174, 158,4, 159,125, 160,128, 161,129, 162,129, 163,87, 164,85, 165,88, 166,86, 167,130, 168,89, 169,5, 171,209, 172,3, 173,285, 177,92, 178,93, 179,162, 180,163, 181,320, 183,326, 184,327, 185,328, 186,329, 187,330, 188,331, 189,332, 190,333, 191,334, 192,335, 193,336, 194,337, 200,126, 201,127, 204,83, 206,321, 207,126, 208,90, 210,323, 212,27, 213,209, 215,65, 217,84, 224,220, 225,221, 226,79, 228,307, 229,305, 230,306, 248,91, 256,188, 257,189, 258,190, 259,191, 260,192, 261,193, 262,194, 263,195, 264,196, 265,197, 266,198, 267,199, 268,200, 269,201, 270,202, 271,203, 288,188, 289,189, 290,190, 291,191, 292,192, 293,193, 294,194, 295,195, 296,196, 297,197, 298,198, 299,199, 300,200, 301,201, 302,202, 303,203, 304,96, 305,97, 306,98, 307,99, 308,100, 309,101, 310,102, 311,103, 312,104, 313,105, 314,109, 315,108, 316,110, 317,106, 318,107, 329,310, 331,308, 332,309, 353,23, 362,172, 366,173, 368,204, 370,175, 372,325, 377,170, 397,208, 398,183, 399,184, 400,185, 401,186, 402,166, 403,167, 405,229, 418,168, 419,169, 429,207, 464,119, 465,111, 466,131, 467,132, 468,133, 469,134, 470,135, 471,136, 472,137, 473,138, 474,139, 475,140, 476,141, 477,142, 478,8, 479,9, 480,32, 481,33, 482,34, 483,47, 484,30, 522,17, 523,18, 528,80, 580,187, 582,231, 583,219, 585,317, 586,319, 656,313, 657,314, 658,315, 659,316};
    static final int[] KEYCODES = new int[768];
    static {
        for (int i = 0; i < KEY_TABLE.length; i += 2) {
            if (KEY_TABLE[i] < KEYCODES.length) KEYCODES[KEY_TABLE[i]] = KEY_TABLE[i + 1];
        }
    }

    private static Object inputManager;
    private static Method inject;

    private static void send(android.view.InputEvent ev) throws Exception {
        inject.invoke(inputManager, ev, 0 /* INJECT_INPUT_EVENT_MODE_ASYNC */);
    }

    // -- pointer state ---------------------------------------------------------
    private static float x = 100, y = 100;
    private static int buttons = 0;
    private static long downTime = 0;
    private static boolean hovering = false;

    private static MotionEvent mouse(int action, int actionButton, float vscroll, float hscroll) throws Exception {
        MotionEvent.PointerProperties[] props = {new MotionEvent.PointerProperties()};
        props[0].id = 0;
        props[0].toolType = MotionEvent.TOOL_TYPE_MOUSE;
        MotionEvent.PointerCoords[] coords = {new MotionEvent.PointerCoords()};
        coords[0].x = x;
        coords[0].y = y;
        coords[0].pressure = buttons != 0 ? 1f : 0f;
        coords[0].size = 1f;
        if (vscroll != 0) coords[0].setAxisValue(MotionEvent.AXIS_VSCROLL, vscroll);
        if (hscroll != 0) coords[0].setAxisValue(MotionEvent.AXIS_HSCROLL, hscroll);
        long now = SystemClock.uptimeMillis();
        MotionEvent ev = MotionEvent.obtain(buttons != 0 ? downTime : now, now, action, 1, props, coords,
                metaState, buttons, 1f, 1f, 0, 0, InputDevice.SOURCE_MOUSE, 0);
        if (actionButton != 0) {
            MotionEvent.class.getMethod("setActionButton", int.class).invoke(ev, actionButton);
        }
        return ev;
    }

    private static void move(int nx, int ny) throws Exception {
        x = nx;
        y = ny;
        if (buttons != 0) {
            send(mouse(MotionEvent.ACTION_MOVE, 0, 0, 0));
        } else {
            if (!hovering) {
                send(mouse(MotionEvent.ACTION_HOVER_ENTER, 0, 0, 0));
                hovering = true;
            }
            send(mouse(MotionEvent.ACTION_HOVER_MOVE, 0, 0, 0));
        }
    }

    private static int androidButton(int evdev) {
        switch (evdev) {
            case BTN_LEFT: return MotionEvent.BUTTON_PRIMARY;
            case BTN_RIGHT: return MotionEvent.BUTTON_SECONDARY;
            case BTN_MIDDLE: return MotionEvent.BUTTON_TERTIARY;
            case 0x113: return MotionEvent.BUTTON_BACK;
            case 0x114: return MotionEvent.BUTTON_FORWARD;
            default: return 0;
        }
    }

    private static void button(int evdev, boolean press, int nx, int ny) throws Exception {
        int bit = androidButton(evdev);
        if (bit == 0) return;
        x = nx;
        y = ny;
        if (press) {
            if ((buttons & bit) != 0) return;
            if (buttons == 0) {
                if (hovering) {
                    send(mouse(MotionEvent.ACTION_HOVER_EXIT, 0, 0, 0));
                    hovering = false;
                }
                downTime = SystemClock.uptimeMillis();
                buttons = bit;
                send(mouse(MotionEvent.ACTION_DOWN, 0, 0, 0));
            } else {
                buttons |= bit;
                send(mouse(MotionEvent.ACTION_MOVE, 0, 0, 0));
            }
            send(mouse(MotionEvent.ACTION_BUTTON_PRESS, bit, 0, 0));
        } else {
            if ((buttons & bit) == 0) return;
            buttons &= ~bit;
            // The release event still names the button being released.
            buttons |= bit;
            send(mouse(MotionEvent.ACTION_BUTTON_RELEASE, bit, 0, 0));
            buttons &= ~bit;
            if (buttons == 0) {
                buttons = 0;
                send(mouse(MotionEvent.ACTION_UP, 0, 0, 0));
            } else {
                send(mouse(MotionEvent.ACTION_MOVE, 0, 0, 0));
            }
        }
    }

    private static void scroll(int v, int h) throws Exception {
        send(mouse(MotionEvent.ACTION_SCROLL, 0, v / 1000f, h / 1000f));
    }

    // -- keyboard state --------------------------------------------------------
    private static int metaState = 0;
    private static final Set<Integer> keysDown = new HashSet<>();

    private static int modifierBit(int keyCode) {
        switch (keyCode) {
            case KeyEvent.KEYCODE_SHIFT_LEFT: return KeyEvent.META_SHIFT_LEFT_ON | KeyEvent.META_SHIFT_ON;
            case KeyEvent.KEYCODE_SHIFT_RIGHT: return KeyEvent.META_SHIFT_RIGHT_ON | KeyEvent.META_SHIFT_ON;
            case KeyEvent.KEYCODE_CTRL_LEFT: return KeyEvent.META_CTRL_LEFT_ON | KeyEvent.META_CTRL_ON;
            case KeyEvent.KEYCODE_CTRL_RIGHT: return KeyEvent.META_CTRL_RIGHT_ON | KeyEvent.META_CTRL_ON;
            case KeyEvent.KEYCODE_ALT_LEFT: return KeyEvent.META_ALT_LEFT_ON | KeyEvent.META_ALT_ON;
            case KeyEvent.KEYCODE_ALT_RIGHT: return KeyEvent.META_ALT_RIGHT_ON | KeyEvent.META_ALT_ON;
            case KeyEvent.KEYCODE_META_LEFT: return KeyEvent.META_META_LEFT_ON | KeyEvent.META_META_ON;
            case KeyEvent.KEYCODE_META_RIGHT: return KeyEvent.META_META_RIGHT_ON | KeyEvent.META_META_ON;
            default: return 0;
        }
    }

    private static void key(int evdev, boolean press) throws Exception {
        int keyCode = evdev >= 0 && evdev < KEYCODES.length ? KEYCODES[evdev] : 0;
        if (keyCode == 0) return;
        int modifier = modifierBit(keyCode);
        if (press) {
            if (!keysDown.add(keyCode)) return;
            metaState |= modifier;
            if (keyCode == KeyEvent.KEYCODE_CAPS_LOCK) metaState ^= KeyEvent.META_CAPS_LOCK_ON;
            if (keyCode == KeyEvent.KEYCODE_NUM_LOCK) metaState ^= KeyEvent.META_NUM_LOCK_ON;
        } else {
            if (!keysDown.remove(keyCode)) return;
        }
        long now = SystemClock.uptimeMillis();
        KeyEvent ev = new KeyEvent(now, now, press ? KeyEvent.ACTION_DOWN : KeyEvent.ACTION_UP, keyCode, 0,
                metaState, KeyCharacterMap.VIRTUAL_KEYBOARD, evdev, 0, InputDevice.SOURCE_KEYBOARD);
        send(ev);
        if (!press) metaState &= ~modifier;
    }

    private static void reset() throws Exception {
        for (int keyCode : new HashSet<>(keysDown)) {
            long now = SystemClock.uptimeMillis();
            keysDown.remove(keyCode);
            send(new KeyEvent(now, now, KeyEvent.ACTION_UP, keyCode, 0, metaState,
                    KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0, InputDevice.SOURCE_KEYBOARD));
            metaState &= ~modifierBit(keyCode);
        }
        metaState &= KeyEvent.META_CAPS_LOCK_ON | KeyEvent.META_NUM_LOCK_ON;
        for (int evdev : new int[] {BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, 0x113, 0x114}) {
            if ((buttons & androidButton(evdev)) != 0) button(evdev, false, (int) x, (int) y);
        }
        if (hovering) {
            send(mouse(MotionEvent.ACTION_HOVER_EXIT, 0, 0, 0));
            hovering = false;
        }
    }

    // -- socket loop -----------------------------------------------------------
    private static int readInt(DataInputStream in) throws IOException {
        int b0 = in.readUnsignedByte(), b1 = in.readUnsignedByte(), b2 = in.readUnsignedByte(), b3 = in.readUnsignedByte();
        return b0 | (b1 << 8) | (b2 << 16) | (b3 << 24);
    }

    /** First two bytes of every connection: proof that this is the receiver. */
    static final int HELLO_0 = 0x54, HELLO_1 = 0x39;   // 'T', '9'

    private static void serve(LocalSocket client) throws Exception {
        // ADB's forward accepts the local TCP connection whether or not the
        // abstract socket exists on the device, so the host cannot tell a
        // running receiver from a missing one until something answers.
        client.getOutputStream().write(new byte[] {(byte) HELLO_0, (byte) HELLO_1});
        client.getOutputStream().flush();
        DataInputStream in = new DataInputStream(client.getInputStream());
        while (true) {
            int type, flags, code, a, b;
            try {
                type = in.readUnsignedByte();
                flags = in.readUnsignedByte();
                code = in.readUnsignedByte() | (in.readUnsignedByte() << 8);
                a = readInt(in);
                b = readInt(in);
            } catch (EOFException end) {
                return;
            }
            switch (type) {
                case TYPE_MOVE: move(a, b); break;
                case TYPE_BUTTON: button(code, flags != 0, a, b); break;
                case TYPE_SCROLL: scroll(a, b); break;
                case TYPE_KEY: key(code, flags != 0); break;
                case TYPE_RESET: reset(); break;
                default: break;
            }
        }
    }

    public static void main(String[] args) throws Exception {
        Class<?> im;
        try {
            im = Class.forName("android.hardware.input.InputManagerGlobal");
            inputManager = im.getMethod("getInstance").invoke(null);
        } catch (Exception e) {
            im = Class.forName("android.hardware.input.InputManager");
            inputManager = im.getMethod("getInstance").invoke(null);
        }
        inject = im.getMethod("injectInputEvent", android.view.InputEvent.class, int.class);
        String name = args.length > 0 ? args[0] : "tabs9-remote";
        LocalServerSocket server = new LocalServerSocket(name);
        log("listening on localabstract:" + name);
        while (true) {
            LocalSocket client = server.accept();
            log("client connected");
            try {
                serve(client);
            } catch (Exception e) {
                log("client error: " + e);
            } finally {
                try { reset(); } catch (Exception ignored) {}
                try { client.close(); } catch (IOException ignored) {}
                log("client gone; everything released");
            }
        }
    }
}
