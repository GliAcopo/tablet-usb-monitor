import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * A real mouse and keyboard on the tablet, through /dev/uhid.
 *
 * Injected MotionEvents move nothing the user can see: Android only draws a
 * pointer for a device its InputReader knows about, so a remote-controlled
 * tablet had an invisible cursor. A UHID device *is* such a device — the
 * kernel builds an input device from the HID report descriptor below and
 * Android treats it exactly like a mouse plugged into the USB port, cursor,
 * pointer acceleration, DeX and all. The shell user may open /dev/uhid
 * (group uhid, which adb shell is in), so no root is needed.
 *
 * Only the two writes that matter are implemented: UHID_CREATE2 to make the
 * device and UHID_INPUT2 to send a report. The kernel zero-fills the rest of
 * its event struct, so a short write is a complete event.
 */
final class Uhid implements AutoCloseable {
    private static final int UHID_DESTROY = 1;
    private static final int UHID_CREATE2 = 11;
    private static final int UHID_INPUT2 = 12;
    static final String DEVICE = "/dev/uhid";

    /** 5 buttons, relative X/Y, a wheel and a horizontal wheel: report is 5 bytes. */
    static final byte[] MOUSE_DESCRIPTOR = {
        0x05, 0x01,        // Usage Page (Generic Desktop)
        0x09, 0x02,        // Usage (Mouse)
        (byte) 0xA1, 0x01, // Collection (Application)
        0x09, 0x01,        //   Usage (Pointer)
        (byte) 0xA1, 0x00, //   Collection (Physical)
        0x05, 0x09,        //     Usage Page (Button)
        0x19, 0x01,        //     Usage Minimum (1)
        0x29, 0x05,        //     Usage Maximum (5)
        0x15, 0x00, 0x25, 0x01,
        (byte) 0x95, 0x05, 0x75, 0x01,
        (byte) 0x81, 0x02, //     Input (Data, Variable, Absolute)
        (byte) 0x95, 0x01, 0x75, 0x03,
        (byte) 0x81, 0x01, //     Input (Constant): padding to a byte
        0x05, 0x01,        //     Usage Page (Generic Desktop)
        0x09, 0x30,        //     Usage (X)
        0x09, 0x31,        //     Usage (Y)
        0x15, (byte) 0x81, 0x25, 0x7F,
        0x75, 0x08, (byte) 0x95, 0x02,
        (byte) 0x81, 0x06, //     Input (Data, Variable, Relative)
        0x09, 0x38,        //     Usage (Wheel)
        0x15, (byte) 0x81, 0x25, 0x7F, 0x75, 0x08, (byte) 0x95, 0x01, (byte) 0x81, 0x06,
        0x05, 0x0C,        //     Usage Page (Consumer)
        0x0A, 0x38, 0x02,  //     Usage (AC Pan): the horizontal wheel
        0x15, (byte) 0x81, 0x25, 0x7F, 0x75, 0x08, (byte) 0x95, 0x01, (byte) 0x81, 0x06,
        (byte) 0xC0,       //   End Collection
        (byte) 0xC0        // End Collection
    };

    /** Boot-protocol keyboard: modifier byte, reserved, six key usages. */
    static final byte[] KEYBOARD_DESCRIPTOR = {
        0x05, 0x01, 0x09, 0x06, (byte) 0xA1, 0x01,
        0x05, 0x07,                                   //   Usage Page (Keyboard)
        0x19, (byte) 0xE0, 0x29, (byte) 0xE7,         //   Usage Min/Max (modifiers)
        0x15, 0x00, 0x25, 0x01, 0x75, 0x01, (byte) 0x95, 0x08,
        (byte) 0x81, 0x02,                            //   Input (modifier bits)
        (byte) 0x95, 0x01, 0x75, 0x08, (byte) 0x81, 0x01,   //   Input (reserved byte)
        0x05, 0x08, 0x19, 0x01, 0x29, 0x05,           //   Usage Page (LEDs)
        (byte) 0x95, 0x05, 0x75, 0x01, (byte) 0x91, 0x02,
        (byte) 0x95, 0x01, 0x75, 0x03, (byte) 0x91, 0x01,
        0x05, 0x07, 0x19, 0x00, 0x29, 0x65,           //   Usage Page (Keyboard)
        0x15, 0x00, 0x25, 0x65, 0x75, 0x08, (byte) 0x95, 0x06,
        (byte) 0x81, 0x00,                            //   Input (six key slots)
        (byte) 0xC0
    };

    private final FileOutputStream out;
    private final String name;

    private Uhid(String name, byte[] descriptor, int product) throws IOException {
        this.name = name;
        this.out = new FileOutputStream(DEVICE);
        ByteBuffer event = ByteBuffer.allocate(4 + 128 + 64 + 64 + 2 + 2 + 4 + 4 + 4 + 4
                                               + descriptor.length).order(ByteOrder.nativeOrder());
        event.putInt(UHID_CREATE2);
        byte[] nameBytes = name.getBytes();
        byte[] field = new byte[128];
        System.arraycopy(nameBytes, 0, field, 0, Math.min(nameBytes.length, 127));
        event.put(field);
        event.put(new byte[64]);          // phys
        event.put(new byte[64]);          // uniq
        event.putShort((short) descriptor.length);
        event.putShort((short) 0x03);     // BUS_USB
        event.putInt(0x18D1);             // vendor
        event.putInt(product);
        event.putInt(0);                  // version
        event.putInt(0);                  // country
        event.put(descriptor);
        out.write(event.array());
        out.flush();
    }

    /** The two devices, or null when /dev/uhid cannot be used on this tablet. */
    static Uhid mouse() throws IOException {
        return new Uhid("tabs9 remote mouse", MOUSE_DESCRIPTOR, 0x5301);
    }

    static Uhid keyboard() throws IOException {
        return new Uhid("tabs9 remote keyboard", KEYBOARD_DESCRIPTOR, 0x5302);
    }

    static boolean available() {
        return new File(DEVICE).canWrite();
    }

    void report(byte[] data) throws IOException {
        ByteBuffer event = ByteBuffer.allocate(4 + 2 + data.length).order(ByteOrder.nativeOrder());
        event.putInt(UHID_INPUT2);
        event.putShort((short) data.length);
        event.put(data);
        out.write(event.array());
        out.flush();
    }

    @Override
    public void close() {
        try {
            ByteBuffer event = ByteBuffer.allocate(4).order(ByteOrder.nativeOrder());
            event.putInt(UHID_DESTROY);
            out.write(event.array());
            out.flush();
        } catch (IOException ignored) {
        }
        try {
            out.close();
        } catch (IOException ignored) {
        }
    }

    @Override
    public String toString() {
        return name;
    }
}
