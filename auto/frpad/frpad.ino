// frpad: ESP32-S3 as a wired Switch controller (HORIPAD S compatible), driven over the COM port.
//
// Board: ESP32-S3 DevKitC-1 (two USB-C ports)
//   "USB" port -> Switch dock     (the controller)
//   "COM" port -> Mac             (commands from Python, and flashing)
// Arduino IDE settings: Board "ESP32S3 Dev Module", USB Mode "USB-OTG (TinyUSB)",
//   USB CDC On Boot "Disabled", Upload Mode "UART0 / Hardware CDC".
//
// Serial protocol (115200 baud, one command per line, every command answers "OK" or "ERR"):
//   ?                          -> "OK FRPAD 1"
//   S <btn> <hat> <lx> <ly> <rx> <ry>   set and hold a state (btn hex bitmask, hat 0-8, sticks 0-255)
//   P <btn> <hat> <ms>         press for ms, then release (timed here, so it is exact)
//   R                          release everything
// Safety: a held state with no command for 10 s is released (host crashed / unplugged).

#include "USB.h"
#include "USBHID.h"

static const uint8_t report_desc[] = {
  0x05, 0x01, 0x09, 0x05, 0xA1, 0x01,                          // Usage Page (Desktop), Usage (Gamepad), Collection
  0x15, 0x00, 0x25, 0x01, 0x35, 0x00, 0x45, 0x01,
  0x75, 0x01, 0x95, 0x10, 0x05, 0x09, 0x19, 0x01, 0x29, 0x10, 0x81, 0x02,   // 16 buttons
  0x05, 0x01, 0x25, 0x07, 0x46, 0x3B, 0x01, 0x75, 0x04, 0x95, 0x01,
  0x65, 0x14, 0x09, 0x39, 0x81, 0x42,                          // hat switch (4 bits)
  0x65, 0x00, 0x95, 0x01, 0x81, 0x01,                          // 4 bits padding
  0x26, 0xFF, 0x00, 0x46, 0xFF, 0x00, 0x09, 0x30, 0x09, 0x31, 0x09, 0x32, 0x09, 0x35,
  0x75, 0x08, 0x95, 0x04, 0x81, 0x02,                          // X Y Z Rz sticks
  0x06, 0x00, 0xFF, 0x09, 0x20, 0x95, 0x01, 0x81, 0x02,        // vendor byte
  0x0A, 0x21, 0x26, 0x95, 0x08, 0x91, 0x02,                    // 8-byte output report
  0xC0
};

struct __attribute__((packed)) PadReport {
  uint16_t buttons;
  uint8_t hat;
  uint8_t lx, ly, rx, ry;
  uint8_t vendor;
};

class SwitchPad : public USBHIDDevice {
public:
  USBHID hid;
  SwitchPad() { hid.addDevice(this, sizeof(report_desc)); }
  uint16_t _onGetDescriptor(uint8_t *buf) override {
    memcpy(buf, report_desc, sizeof(report_desc));
    return sizeof(report_desc);
  }
  bool send(const PadReport &r) { return hid.SendReport(0, &r, sizeof(r)); }
};

SwitchPad pad;
static PadReport cur = {0, 8, 128, 128, 128, 128, 0};
static const PadReport neutral = {0, 8, 128, 128, 128, 128, 0};
static uint32_t release_at = 0, last_cmd = 0, last_send = 0;
static bool dirty = true;
static char line[96];
static uint8_t len = 0;

static void usb_event(void *arg, esp_event_base_t base, int32_t id, void *data) {
  const char *names[] = {"started", "stopped", "suspend", "resume"};
  Serial0.printf("# usb %s\n", (id >= 0 && id < 4) ? names[id] : "?");
}

static void led(uint8_t r, uint8_t g, uint8_t b) {
#ifdef RGB_BUILTIN
  rgbLedWrite(RGB_BUILTIN, r, g, b);
#endif
}

static bool is_neutral() { return memcmp(&cur, &neutral, sizeof(cur)) == 0; }

static void handle(char *s) {
  last_cmd = millis();
  unsigned btn = 0, hat = 8, a = 128, b = 128, c = 128, d = 128, ms = 0;
  switch (s[0]) {
    case '?':
      Serial0.println("OK FRPAD 1");
      return;
    case 'R':
      cur = neutral; release_at = 0; dirty = true;
      break;
    case 'S':
      if (sscanf(s + 1, "%x %u %u %u %u %u", &btn, &hat, &a, &b, &c, &d) < 2) { Serial0.println("ERR"); return; }
      cur.buttons = btn; cur.hat = hat > 8 ? 8 : hat; cur.lx = a; cur.ly = b; cur.rx = c; cur.ry = d;
      release_at = 0; dirty = true;
      break;
    case 'P':
      if (sscanf(s + 1, "%x %u %u", &btn, &hat, &ms) != 3) { Serial0.println("ERR"); return; }
      cur = neutral; cur.buttons = btn; cur.hat = hat > 8 ? 8 : hat;
      release_at = millis() + (ms ? ms : 1); dirty = true;
      break;
    default:
      Serial0.println("ERR");
      return;
  }
  Serial0.println("OK");
}

void setup() {
  Serial0.begin(115200);
  Serial0.println("# frpad boot");
  USB.onEvent(usb_event);
  USB.VID(0x0F0D);                 // HORI: the Switch accepts this pad without the Pro Controller handshake
  USB.PID(0x00C1);                 // HORIPAD S
  USB.productName("HORIPAD S");
  USB.manufacturerName("HORI CO.,LTD.");
  USB.usbClass(0); USB.usbSubClass(0); USB.usbProtocol(0);
  pad.hid.begin();
  bool ok = USB.begin();
  Serial0.printf("# usb begin %s\n", ok ? "ok" : "FAILED");
  led(0, 0, 16);
}

void loop() {
  while (Serial0.available()) {
    char ch = Serial0.read();
    if (ch == '\r') continue;
    if (ch == '\n') { line[len] = 0; if (len) handle(line); len = 0; }
    else if (len < sizeof(line) - 1) line[len++] = ch;
  }
  uint32_t now = millis();
#ifdef FRPAD_DEBUG
  static uint32_t last_dbg = 0;
  if (now - last_dbg > 2000) {
    last_dbg = now;
    Serial0.printf("# t=%lu mounted=%d ready=%d\n", (unsigned long)now, (int)(bool)USB, (int)pad.hid.ready());
  }
#endif
  if (release_at && (int32_t)(now - release_at) >= 0) { cur = neutral; release_at = 0; dirty = true; }
  if (!release_at && !is_neutral() && now - last_cmd > 10000) { cur = neutral; dirty = true; }   // watchdog
  if ((dirty || now - last_send >= 8) && pad.hid.ready()) {
    if (pad.send(cur)) {
      if (dirty) led(is_neutral() ? 0 : 16, is_neutral() ? 16 : 8, 0);
      dirty = false; last_send = now;
    }
  }
}
