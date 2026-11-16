#include "alert_display.h"
#include <Arduino.h>
#include <WiFi.h>
#include <Arduino_GFX_Library.h>
#include <XPowersLib.h>
#include <Wire.h>

// T-Watch S3 confirmed by the physical packaging.
// Pinout and power rails: TTGO_TWatch_Library/t-watch-s3/src/LilyGoLib.cpp
// Display ST7789V 240x240: DC38 CS12 SCK18 MOSI13 BL45, no reset pin.

namespace {
constexpr uint32_t ALERT_MS = 8000;
constexpr uint16_t WHITE_COLOR = 0xFFFF;
constexpr uint16_t IDLE_COLOR = 0x1126;
constexpr uint16_t ALERT_COLOR = 0xD800;
Arduino_ESP32SPI bus(38, 12, 18, 13, GFX_NOT_DEFINED);
Arduino_ST7789 screen(&bus, GFX_NOT_DEFINED, 0, true, 240, 240);
XPowersAXP2101 pmu;
bool powerReady = false;
bool ready = false;
bool active = false;
uint32_t started = 0;
uint32_t refreshed = 0;

void line(const char *text, int y, uint8_t size)
{
    screen.setTextSize(size);
    screen.setTextColor(WHITE_COLOR);
    screen.setTextWrap(false);
    int x = (240 - strlen(text) * 6 * size) / 2;
    screen.setCursor(x < 4 ? 4 : x, y);
    screen.print(text);
}

void drawIdle()
{
    if (!ready) return;
    screen.fillScreen(IDLE_COLOR);
    line("SISTEMA EPI", 25, 2);
    line("AGUARDANDO", 85, 2);
    line("COMANDO", 110, 2);
    line(WiFi.status() == WL_CONNECTED ? "WI-FI CONECTADO" : "CONECTANDO WI-FI", 165, 1);
    if (WiFi.status() == WL_CONNECTED) line(WiFi.localIP().toString().c_str(), 185, 1);
    line("T-WATCH S3", 215, 1);
}
}

void configureWatchPower()
{
    powerReady = pmu.init(Wire, 10, 11);
    if (!powerReady) {
        Serial.println("Falha ao detectar AXP2101 do T-Watch S3.");
        return;
    }
    // Only configure the display and haptic rails; leave charging unchanged.
    pmu.setALDO2Voltage(3300);
    pmu.enableALDO2();
    pmu.setBLDO2Voltage(3300);
    pmu.enableBLDO2();
    Serial.println("AXP2101: alimentacao do display e DRV2605 habilitada.");
}

void configureAlertDisplay()
{
    pinMode(45, OUTPUT);
    digitalWrite(45, LOW);
    ready = powerReady && screen.begin(20000000);
    if (!ready) {
        Serial.println("Falha ao inicializar ST7789V/alimentacao.");
        return;
    }
    drawIdle();
    digitalWrite(45, HIGH);
    Serial.println("ST7789V 240x240 inicializado; backlight GPIO45. Confirmacao visual necessaria.");
}

void showVisualAlert(bool hapticAccepted)
{
    if (!ready) return;
    active = true;
    started = millis();
    screen.fillScreen(ALERT_COLOR);
    screen.drawRect(5, 5, 230, 230, WHITE_COLOR);
    line("!", 20, 6);
    line("ALERTA", 90, 4);
    line("COMANDO RECEBIDO", 150, 1);
    line(hapticAccepted ? "VIBRACAO SOLICITADA" : "FALHA NO VIBRADOR", 175, 1);
    line("Confira o sistema", 210, 1);
}

// Quebra a mensagem em linhas de ate maxChars caracteres, sem cortar palavras.
int wrappedLines(const String &text, int y, uint8_t size, int maxChars, int maxLines)
{
    String remaining = text;
    remaining.trim();
    int lines = 0;
    while (remaining.length() > 0 && lines < maxLines) {
        int cut = remaining.length();
        if (cut > maxChars) {
            cut = remaining.lastIndexOf(' ', maxChars);
            if (cut <= 0) cut = maxChars;
        }
        String current = remaining.substring(0, cut);
        current.trim();
        line(current.c_str(), y + lines * (8 * size + 6), size);
        remaining = remaining.substring(cut);
        remaining.trim();
        lines++;
    }
    return lines;
}

void showVisualAlert(bool hapticAccepted, const String &message, const String &detail)
{
    if (message.length() == 0) {
        showVisualAlert(hapticAccepted);
        return;
    }
    if (!ready) return;
    active = true;
    started = millis();
    screen.fillScreen(ALERT_COLOR);
    screen.drawRect(5, 5, 230, 230, WHITE_COLOR);
    line("!", 12, 5);
    // Tamanho 2 = 12 px por caractere: ate 18 caracteres por linha.
    int used = wrappedLines(message, 70, 2, 18, 4);
    int y = 70 + used * 22 + 8;
    if (detail.length() > 0) {
        wrappedLines(detail, y, 1, 36, 3);
    }
    line(hapticAccepted ? "VIBRACAO SOLICITADA" : "FALHA NO VIBRADOR", 218, 1);
}

// ------------------------------------------------------------
// Estado continuo: enquanto o detector informar IRREGULAR, a tela
// fica vermelha mostrando o que falta e ha quanto tempo.
// ------------------------------------------------------------
namespace {
constexpr uint16_t REGULAR_COLOR = 0x0560;
constexpr uint32_t REGULAR_MS = 4000;
bool irregular = false;
bool regularActive = false;
uint32_t irregularSince = 0;
uint32_t regularShownAt = 0;
uint32_t lastTick = 0;
bool borderOn = false;
String irregularMessage;
String irregularMissing;

void drawIrregularTimer()
{
    uint32_t seconds = (millis() - irregularSince) / 1000;
    char text[24];
    snprintf(text, sizeof(text), "HA %02lu:%02lu", (unsigned long)(seconds / 60), (unsigned long)(seconds % 60));
    screen.fillRect(8, 206, 224, 24, ALERT_COLOR);
    line(text, 210, 2);
    // Borda piscando para chamar atencao.
    borderOn = !borderOn;
    uint16_t color = borderOn ? WHITE_COLOR : ALERT_COLOR;
    screen.drawRect(2, 2, 236, 236, color);
    screen.drawRect(3, 3, 234, 234, color);
    screen.drawRect(4, 4, 232, 232, color);
}

void drawIrregular()
{
    screen.fillScreen(ALERT_COLOR);
    line("!", 8, 4);
    int used = wrappedLines(irregularMessage, 46, 2, 18, 3);
    int y = 46 + used * 22 + 6;
    line("FALTANDO:", y, 2);
    String missing = irregularMissing;
    missing.toUpperCase();
    wrappedLines(missing, y + 26, missing.length() > 26 ? 2 : 3, missing.length() > 26 ? 18 : 12, 2);
    drawIrregularTimer();
}
}

void showIrregular(const String &message, const String &missing)
{
    if (!ready) return;
    bool changed = !irregular || message != irregularMessage || missing != irregularMissing;
    if (!irregular) irregularSince = millis();
    irregular = true;
    regularActive = false;
    active = false;
    irregularMessage = message.length() ? message : String("Anomalia identificada: falta de EPI");
    irregularMissing = missing.length() ? missing : String("EPI");
    if (changed) drawIrregular();
}

void showRegular()
{
    irregular = false;
    if (!ready) return;
    regularActive = true;
    regularShownAt = millis();
    screen.fillScreen(REGULAR_COLOR);
    line("OK", 40, 6);
    line("EPIS EM DIA", 120, 2);
    line("SITUACAO REGULAR", 160, 2);
}

void clearStatus()
{
    irregular = false;
    regularActive = false;
    if (ready) drawIdle();
}

bool isIrregular() { return irregular; }

void updateAlertDisplay()
{
    if (!ready) return;
    uint32_t now = millis();
    if (irregular) {
        if (active && static_cast<uint32_t>(now - started) >= ALERT_MS) {
            active = false;
            drawIrregular();
        }
        if (!active && static_cast<uint32_t>(now - lastTick) >= 1000) {
            lastTick = now;
            drawIrregularTimer();
        }
        return;
    }
    if (regularActive) {
        if (static_cast<uint32_t>(now - regularShownAt) >= REGULAR_MS) {
            regularActive = false;
            drawIdle();
            refreshed = now;
        }
        return;
    }
    if (active && static_cast<uint32_t>(now - started) >= ALERT_MS) {
        active = false;
        drawIdle();
        refreshed = now;
    } else if (!active && static_cast<uint32_t>(now - refreshed) >= 5000) {
        drawIdle();
        refreshed = now;
    }
}

bool isAlertDisplayReady() { return ready; }
bool isVisualAlertActive() { return active; }
