#include <OneWire.h>
#include <DallasTemperature.h>

const char* DEVICE_ID = "arduino-01";
const uint8_t TEMP_PIN = 2;
const uint8_t POWER_PIN = 3;
const uint8_t DOOR_PIN = 4;
const unsigned long INTERVAL_MS = 2000;

OneWire oneWire(TEMP_PIN);
DallasTemperature sensors(&oneWire);
unsigned long lastSent = 0;

void setup() {
  Serial.begin(9600);
  pinMode(POWER_PIN, INPUT);
  pinMode(DOOR_PIN, INPUT_PULLUP);
  sensors.begin();
}

void loop() {
  unsigned long now = millis();
  if (now - lastSent < INTERVAL_MS) {
    return;
  }
  lastSent = now;
  sensors.requestTemperatures();
  float temp = sensors.getTempCByIndex(0);
  bool powerOn = digitalRead(POWER_PIN) == HIGH;
  bool doorOpen = digitalRead(DOOR_PIN) == HIGH;
  Serial.print("{\"device_id\":\"");
  Serial.print(DEVICE_ID);
  Serial.print("\",\"temp_c\":");
  if (temp == DEVICE_DISCONNECTED_C) {
    Serial.print("null");
  } else {
    Serial.print(temp, 2);
  }
  Serial.print(",\"power_on\":");
  Serial.print(powerOn ? "true" : "false");
  Serial.print(",\"door_open\":");
  Serial.print(doorOpen ? "true" : "false");
  Serial.print(",\"uptime_ms\":");
  Serial.print(now);
  Serial.println("}");
}
