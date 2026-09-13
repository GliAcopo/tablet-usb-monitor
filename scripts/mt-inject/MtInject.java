import android.os.SystemClock;
import android.view.InputDevice;
import android.view.MotionEvent;
import java.lang.reflect.Method;

/**
 * Test-only multitouch source for the tablet, run over ADB as the shell user:
 *
 *   app_process -cp /data/local/tmp/mtinject.dex / MtInject pinch x0 y0 x1 y1 dx dy steps
 *   app_process -cp /data/local/tmp/mtinject.dex / MtInject swipe fingers x0 y0 dx dy steps [spacing]
 *
 * It delivers a multi-pointer gesture through InputManager.injectInputEvent
 * (the same route uiautomator uses), so the app receives ordinary
 * MotionEvents with stable pointer ids: ACTION_DOWN, ACTION_POINTER_DOWN,
 * ACTION_MOVE x steps, ACTION_POINTER_UP, ACTION_UP. Coordinates are display
 * pixels in the current orientation; dx/dy are per step.  ``swipe`` lands
 * ``fingers`` contacts 150 px apart, ``spacing`` ms (default 12) after one
 * another, and moves them together: the host's three/four-finger gestures.
 */
public class MtInject {
    private static Object inputManager;
    private static Method inject;

    private static void send(MotionEvent ev) throws Exception {
        ev.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        inject.invoke(inputManager, ev, 0 /* INJECT_INPUT_EVENT_MODE_ASYNC */);
        ev.recycle();
    }

    private static MotionEvent event(int action, long down, float[] xs, float[] ys, int count) {
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[count];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[count];
        for (int i = 0; i < count; i++) {
            props[i] = new MotionEvent.PointerProperties();
            props[i].id = i;
            props[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            coords[i] = new MotionEvent.PointerCoords();
            coords[i].x = xs[i];
            coords[i].y = ys[i];
            coords[i].pressure = 1f;
            coords[i].size = 1f;
        }
        return MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, count, props, coords,
                0, 0, 1f, 1f, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
    }

    public static void main(String[] args) throws Exception {
        // Android 14+ moved the singleton to InputManagerGlobal; older builds
        // keep InputManager.getInstance().
        Class<?> im;
        try {
            im = Class.forName("android.hardware.input.InputManagerGlobal");
            inputManager = im.getMethod("getInstance").invoke(null);
        } catch (Exception e) {
            im = Class.forName("android.hardware.input.InputManager");
            inputManager = im.getMethod("getInstance").invoke(null);
        }
        inject = im.getMethod("injectInputEvent", android.view.InputEvent.class, int.class);

        if (args[0].equals("swipe")) {
            swipe(Integer.parseInt(args[1]), Float.parseFloat(args[2]), Float.parseFloat(args[3]),
                    Float.parseFloat(args[4]), Float.parseFloat(args[5]), Integer.parseInt(args[6]),
                    args.length > 7 ? Long.parseLong(args[7]) : 12);
            return;
        }
        float x0 = Float.parseFloat(args[1]), y0 = Float.parseFloat(args[2]);
        float x1 = Float.parseFloat(args[3]), y1 = Float.parseFloat(args[4]);
        float dx = Float.parseFloat(args[5]), dy = Float.parseFloat(args[6]);
        int steps = Integer.parseInt(args[7]);
        long down = SystemClock.uptimeMillis();
        float[] xs = {x0, x1}, ys = {y0, y1};

        send(event(MotionEvent.ACTION_DOWN, down, xs, ys, 1));
        Thread.sleep(16);
        send(event(MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                down, xs, ys, 2));
        for (int i = 1; i <= steps; i++) {
            Thread.sleep(16);
            xs[0] = x0 + dx * i; ys[0] = y0 + dy * i;
            xs[1] = x1 - dx * i; ys[1] = y1 - dy * i;
            send(event(MotionEvent.ACTION_MOVE, down, xs, ys, 2));
        }
        Thread.sleep(16);
        send(event(MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                down, xs, ys, 2));
        Thread.sleep(16);
        send(event(MotionEvent.ACTION_UP, down, xs, ys, 1));
        Thread.sleep(50);
        System.out.println("delivered pinch with 2 pointers, " + steps + " moves");
    }

    private static void swipe(int fingers, float x0, float y0, float dx, float dy, int steps,
                              long spacingMs) throws Exception {
        long down = SystemClock.uptimeMillis();
        float[] xs = new float[fingers], ys = new float[fingers];
        for (int i = 0; i < fingers; i++) {
            xs[i] = x0 + 150 * i;
            ys[i] = y0;
        }
        send(event(MotionEvent.ACTION_DOWN, down, xs, ys, 1));
        for (int i = 1; i < fingers; i++) {
            Thread.sleep(spacingMs);
            send(event(MotionEvent.ACTION_POINTER_DOWN | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                    down, xs, ys, i + 1));
        }
        for (int s = 1; s <= steps; s++) {
            Thread.sleep(16);
            for (int i = 0; i < fingers; i++) {
                xs[i] += dx;
                ys[i] += dy;
            }
            send(event(MotionEvent.ACTION_MOVE, down, xs, ys, fingers));
        }
        // Lift in reverse landing order: the last index up is the primary.
        for (int i = fingers - 1; i > 0; i--) {
            Thread.sleep(16);
            send(event(MotionEvent.ACTION_POINTER_UP | (i << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                    down, xs, ys, i + 1));
        }
        Thread.sleep(16);
        send(event(MotionEvent.ACTION_UP, down, xs, ys, 1));
        Thread.sleep(50);
        System.out.println("delivered swipe with " + fingers + " pointers, " + steps + " moves");
    }
}
