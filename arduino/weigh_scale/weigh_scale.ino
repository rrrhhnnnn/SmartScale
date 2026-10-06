/*
 * Smart Weighing Scale - firmware v2
 * Load cell -> HX711 -> Arduino Uno -> I2C LCD  (+ USB serial to the PC app)
 *
 * Wiring (unchanged from your build):
 *   HX711 DT  -> D7        HX711 VCC -> 5V      Load cell Red   -> E+
 *   HX711 SCK -> D2        HX711 GND -> GND     Load cell Black -> E-
 *   LCD  SDA  -> A4/SDA                         Load cell Green -> A+
 *   LCD  SCL  -> A5/SCL                         Load cell White -> A-
 *
 * Serial protocol @ 9600 (line based, '\n' terminated)
 *   OUT  W,<seq>,<grams>,<millis>    live weight sample  (~3/sec)
 *   OUT  ACK,TARE                    tare completed
 *   OUT  ACK,CAL,<factor>            calibration factor applied
 *   OUT  RAW,<value>                 offset-corrected raw count (calibration wizard)
 *   OUT  READY                       boot complete
 *   IN   T                           tare
 *   IN   C:<factor>                  set calibration factor live (no re-flash!)
 *   IN   R                           report raw count
 */

#include <HX711.h>
#include <LiquidCrystal_I2C.h>

HX711 scale;
LiquidCrystal_I2C lcd(0x27, 16, 2);   // change to 0x3F if the LCD stays blank

#define DT_PIN  7
#define SCK_PIN 2

float calibration_factor = 215748.0;
unsigned long seq = 1;
unsigned long lastLcd = 0;

void setup() {
  Serial.begin(9600);

  scale.begin(DT_PIN, SCK_PIN);
  delay(2000);                 // let the HX711 settle before taring
  scale.tare();
  scale.set_scale(calibration_factor);

  lcd.init();
  lcd.backlight();
  lcd.setCursor(0, 0);
  lcd.print(" Smart Scale ");
  lcd.setCursor(0, 1);
  lcd.print(" Starting... ");
  delay(1500);
  lcd.clear();

  Serial.println("READY");
}

void loop() {
  handleCommands();

  // 3 samples ~= 300 ms at the HX711's default 10 SPS.
  float kg    = scale.get_units(3);
  float grams = kg * 1000.0;

  // NOTE: negatives are deliberately NOT clamped to zero.
  // The PC app needs to see drift and removals to work correctly.
  Serial.print(F("W,"));
  Serial.print(seq++);      Serial.print(',');
  Serial.print(grams, 1);   Serial.print(',');
  Serial.println(millis());

  // Refresh the LCD at most ~3x/sec: I2C writes are slow and constant
  // redraws make the display flicker.
  if (millis() - lastLcd > 300) {
    lastLcd = millis();
    lcd.setCursor(0, 0);
    lcd.print(F("Weight:         "));
    lcd.setCursor(0, 1);
    lcd.print(kg, 3);
    lcd.print(F(" kg     "));
  }
}

void handleCommands() {
  if (!Serial.available()) return;

  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "T") {
    scale.tare();
    Serial.println(F("ACK,TARE"));
  }
  else if (cmd.startsWith("C:")) {
    float f = cmd.substring(2).toFloat();
    if (f > 0) {
      calibration_factor = f;
      scale.set_scale(calibration_factor);
      Serial.print(F("ACK,CAL,"));
      Serial.println(calibration_factor, 2);
    }
  }
  else if (cmd == "R") {
    Serial.print(F("RAW,"));
    Serial.println(scale.get_value(10), 0);   // raw minus tare offset
  }
}
