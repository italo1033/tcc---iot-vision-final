#include <Arduino.h>
#include "alert_display.h"
#include <WiFi.h>
#include <ArduinoOTA.h>
#include <WebServer.h>
#include <Wire.h>
#include <Adafruit_DRV2605.h>

#ifndef WIFI_SSID
#error "WIFI_SSID nao definido. Copie .env.example para .env e preencha os valores."
#endif

#ifndef WIFI_PASSWORD
#error "WIFI_PASSWORD nao definido. Copie .env.example para .env e preencha os valores."
#endif

#ifndef OTA_HOSTNAME
#error "OTA_HOSTNAME nao definido. Copie .env.example para .env e preencha os valores."
#endif

#ifndef OTA_PASSWORD
#error "OTA_PASSWORD nao definido. Copie .env.example para .env e preencha os valores."
#endif

#ifndef WEB_AP_MODE
#define WEB_AP_MODE 1
#endif

#ifndef WEB_AP_SSID
#define WEB_AP_SSID "TWATCH-EPI"
#endif

#ifndef WEB_AP_PASSWORD
#define WEB_AP_PASSWORD "12345678"
#endif

namespace {
constexpr unsigned long WIFI_RETRY_INTERVAL_MS = 10000;
constexpr unsigned long WIFI_CONNECT_PROGRESS_MS = 500;
constexpr int HAPTIC_SDA_PIN = 10;
constexpr int HAPTIC_SCL_PIN = 11;
constexpr uint8_t HAPTIC_DEFAULT_EFFECT = 1;
constexpr uint8_t HAPTIC_STRONG_CLICK = 1;
constexpr uint8_t HAPTIC_DOUBLE_CLICK = 10;
constexpr uint8_t HAPTIC_BUZZ = 47;
constexpr uint8_t DRV2605_I2C_ADDRESS = 0x5A;
constexpr int WIFI_SCAN_LIMIT = 20;

unsigned long lastWiFiAttemptMs = 0;
unsigned long lastWiFiProgressMs = 0;
bool otaConfigured = false;
bool webServerStarted = false;
bool hapticReady = false;
bool hapticAddressSeen = false;
wl_status_t lastWiFiStatus = WL_IDLE_STATUS;

// Estado continuo enviado pelo detector (POST /status).
// Enquanto IRREGULAR, o relogio vibra a cada statusVibrationIntervalMs.
// Se o computador parar de mandar atualizacoes por statusTtlMs, o alerta e
// encerrado (evita alarme preso caso o detector seja fechado).
bool statusIrregular = false;
unsigned long lastStatusUpdateMs = 0;
unsigned long lastPeriodicVibrationMs = 0;
unsigned long statusVibrationIntervalMs = 10000;
unsigned long statusTtlMs = 30000;
WebServer server(80);
Adafruit_DRV2605 drv;

bool i2cAddressResponds(uint8_t address)
{
    Wire.beginTransmission(address);
    return Wire.endTransmission() == 0;
}

const char *authModeToText(wifi_auth_mode_t authMode)
{
    switch (authMode) {
        case WIFI_AUTH_OPEN:
            return "OPEN";
        case WIFI_AUTH_WEP:
            return "WEP";
        case WIFI_AUTH_WPA_PSK:
            return "WPA";
        case WIFI_AUTH_WPA2_PSK:
            return "WPA2";
        case WIFI_AUTH_WPA_WPA2_PSK:
            return "WPA/WPA2";
        case WIFI_AUTH_WPA2_ENTERPRISE:
            return "WPA2-ENTERPRISE";
        case WIFI_AUTH_WPA3_PSK:
            return "WPA3";
        case WIFI_AUTH_WPA2_WPA3_PSK:
            return "WPA2/WPA3";
        default:
            return "UNKNOWN";
    }
}

void printWiFiScan()
{
    Serial.println("Procurando redes Wi-Fi proximas...");
    int networkCount = WiFi.scanNetworks();
    if (networkCount <= 0) {
        Serial.println("Nenhuma rede encontrada no scan.");
        return;
    }

    Serial.print("Redes encontradas: ");
    Serial.println(networkCount);
    for (int i = 0; i < networkCount && i < WIFI_SCAN_LIMIT; i++) {
        Serial.print(" - SSID: ");
        Serial.print(WiFi.SSID(i));
        Serial.print(" | RSSI: ");
        Serial.print(WiFi.RSSI(i));
        Serial.print(" dBm | Canal: ");
        Serial.print(WiFi.channel(i));
        Serial.print(" | Seguranca: ");
        Serial.println(authModeToText(WiFi.encryptionType(i)));
    }
    WiFi.scanDelete();
}

void configureWiFiEvents()
{
    WiFi.onEvent([](WiFiEvent_t event, WiFiEventInfo_t info) {
        if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
            Serial.print("\nWi-Fi desconectou. Motivo: ");
            Serial.println(info.wifi_sta_disconnected.reason);
            Serial.println("Dica: motivo 2/201 costuma indicar senha ou autenticacao; redes WPA2-ENTERPRISE precisam usuario/senha, nao so senha.");
        }
    });
}

const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>LILYGO Vibrar</title>
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      font-family: Arial, sans-serif;
      background: #101418;
      color: #f5f7fa;
    }
    main {
      width: min(92vw, 420px);
      padding: 32px;
      text-align: center;
      background: #1b222b;
      border: 1px solid #2f3a46;
      border-radius: 8px;
      box-shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
    }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { margin: 0 0 28px; color: #aeb8c4; line-height: 1.5; }
    button {
      border: 0;
      border-radius: 8px;
      padding: 16px 24px;
      min-width: 190px;
      font-size: 18px;
      font-weight: 700;
      color: #071014;
      background: #3ddc97;
      cursor: pointer;
      transition: transform 160ms ease, background 160ms ease;
    }
    button:hover { background: #5ee6aa; transform: translateY(-2px); }
    button:active { transform: translateY(0) scale(0.98); }
    button.is-vibrating { animation: vibrate 0.35s linear; }
    #status { min-height: 22px; margin-top: 18px; color: #d7dee8; font-size: 14px; }
    @keyframes vibrate {
      0% { transform: translateX(0); }
      15% { transform: translateX(-4px) rotate(-1deg); }
      30% { transform: translateX(4px) rotate(1deg); }
      45% { transform: translateX(-4px) rotate(-1deg); }
      60% { transform: translateX(4px) rotate(1deg); }
      75% { transform: translateX(-2px) rotate(-0.5deg); }
      100% { transform: translateX(0); }
    }
  </style>
</head>
<body>
  <main>
    <h1>Vibrar relogio</h1>
    <p>Clique para enviar o comando ao firmware da ESP32-S3.</p>
    <button id="vibrateButton" type="button">Vibrar agora</button>
    <div id="status"></div>
  </main>
  <script>
    const button = document.getElementById("vibrateButton");
    const statusEl = document.getElementById("status");

    button.addEventListener("click", async () => {
      button.classList.remove("is-vibrating");
      void button.offsetWidth;
      button.classList.add("is-vibrating");
      statusEl.textContent = "Enviando comando...";

      try {
        const response = await fetch("/vibrate", { method: "POST" });
        const data = await response.json();
        statusEl.textContent = data.message || (response.ok ? "Comando enviado." : "Falha no DRV2605.");
        if ("vibrate" in navigator) navigator.vibrate(120);
      } catch (error) {
        statusEl.textContent = "Falha ao enviar comando.";
      }
    });
  </script>
</body>
</html>
)HTML";

void printWiFiStatus()
{
    Serial.print("\nWi-Fi conectado. IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("Abra no navegador: http://");
    Serial.println(WiFi.localIP());
    Serial.print("Hostname OTA: ");
    Serial.println(OTA_HOSTNAME);
}

void configureAccessPoint()
{
    if (!WEB_AP_MODE) {
        return;
    }

    bool started = WiFi.softAP(WEB_AP_SSID, WEB_AP_PASSWORD);
    if (!started) {
        Serial.println("Falha ao iniciar Access Point da ESP32.");
        return;
    }

    Serial.print("Access Point ativo: ");
    Serial.println(WEB_AP_SSID);
    Serial.println("Senha AP configurada no .env.");
    Serial.print("Abra direto pela rede da placa: http://");
    Serial.println(WiFi.softAPIP());
}

void connectWiFi()
{
    lastWiFiAttemptMs = millis();
    lastWiFiProgressMs = millis();

    if (WiFi.status() == WL_CONNECTED) {
        return;
    }

    Serial.print("Conectando ao Wi-Fi ");
    Serial.print(WIFI_SSID);
    Serial.print(" ");

    WiFi.mode(WEB_AP_MODE ? WIFI_AP_STA : WIFI_STA);
    WiFi.setSleep(false);
    WiFi.setHostname(OTA_HOSTNAME);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void configureOTA()
{
    ArduinoOTA.setHostname(OTA_HOSTNAME);
    ArduinoOTA.setPassword(OTA_PASSWORD);

    ArduinoOTA.onStart([]() {
        const char *type = ArduinoOTA.getCommand() == U_FLASH ? "firmware" : "filesystem";
        Serial.println("\nInicio da atualizacao OTA.");
        Serial.print("Tipo: ");
        Serial.println(type);
    });

    ArduinoOTA.onEnd([]() {
        Serial.println("\nAtualizacao OTA finalizada.");
    });

    ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
        if (total == 0) {
            return;
        }

        static int lastPercent = -1;
        int percent = static_cast<int>((progress * 100U) / total);
        if (percent != lastPercent) {
            lastPercent = percent;
            Serial.printf("Progresso OTA: %d%%\n", percent);
        }
    });

    ArduinoOTA.onError([](ota_error_t error) {
        Serial.printf("Erro OTA [%u]: ", error);
        switch (error) {
            case OTA_AUTH_ERROR:
                Serial.println("falha de autenticacao.");
                break;
            case OTA_BEGIN_ERROR:
                Serial.println("falha ao iniciar.");
                break;
            case OTA_CONNECT_ERROR:
                Serial.println("falha de conexao.");
                break;
            case OTA_RECEIVE_ERROR:
                Serial.println("falha ao receber dados.");
                break;
            case OTA_END_ERROR:
                Serial.println("falha ao finalizar.");
                break;
            default:
                Serial.println("erro desconhecido.");
                break;
        }
    });

    ArduinoOTA.begin();
    otaConfigured = true;
    Serial.println("OTA pronto.");
}

void configureHaptic()
{
    Wire.begin(HAPTIC_SDA_PIN, HAPTIC_SCL_PIN);
    hapticAddressSeen = i2cAddressResponds(DRV2605_I2C_ADDRESS);
    Serial.print("Scan DRV2605 no endereco 0x5A: ");
    Serial.println(hapticAddressSeen ? "encontrado" : "nao respondeu");

    if (!drv.begin(&Wire)) {
        hapticReady = false;
        Serial.println("DRV2605 nao encontrado em SDA=IO10 SCL=IO11.");
        return;
    }

    drv.selectLibrary(1);
    drv.useERM();
    drv.setMode(DRV2605_MODE_INTTRIG);
    hapticReady = true;
    Serial.println("DRV2605 haptic pronto em SDA=IO10 SCL=IO11.");

    drv.setWaveform(0, HAPTIC_STRONG_CLICK);
    drv.setWaveform(1, HAPTIC_DOUBLE_CLICK);
    drv.setWaveform(2, HAPTIC_BUZZ);
    drv.setWaveform(3, 0);
    drv.go();
    Serial.println("Teste haptico inicial enviado ao DRV2605.");
}

void softClick()
{
    if (!hapticReady) return;
    drv.setWaveform(0, HAPTIC_STRONG_CLICK);
    drv.setWaveform(1, 0);
    drv.go();
}

bool startVibration(uint8_t effect = HAPTIC_DEFAULT_EFFECT)
{
    if (!hapticReady) {
        Serial.println("Comando de vibracao recebido, mas DRV2605 nao esta pronto.");
        return false;
    }

    if (effect == 0) {
        effect = HAPTIC_DEFAULT_EFFECT;
    }

    drv.setWaveform(0, effect);
    drv.setWaveform(1, HAPTIC_DOUBLE_CLICK);
    drv.setWaveform(2, HAPTIC_BUZZ);
    drv.setWaveform(3, 0);
    drv.go();
    Serial.print("Vibracao DRV2605 acionada. Efeito inicial: ");
    Serial.println(effect);
    return true;
}

void configureWebServer()
{
    server.on("/", HTTP_GET, []() {
        Serial.print("Pagina web acessada por ");
        Serial.println(server.client().remoteIP());
        server.send_P(200, "text/html; charset=utf-8", INDEX_HTML);
    });

    server.on("/health", HTTP_GET, []() {
        String json = "{\"ok\":true,\"sta_ip\":\"";
        json += WiFi.localIP().toString();
        json += "\",\"ap_ip\":\"";
        json += WiFi.softAPIP().toString();
        json += "\",\"wifi_connected\":";
        json += (WiFi.status() == WL_CONNECTED ? "true" : "false");
        json += ",\"haptic_ready\":";
        json += (hapticReady ? "true" : "false");
        json += ",\"drv2605_address_0x5a_seen\":";
        json += (hapticAddressSeen ? "true" : "false");
        json += ",\"display_ready\":";
        json += (isAlertDisplayReady() ? "true" : "false");
        json += ",\"visual_alert_active\":";
        json += (isVisualAlertActive() ? "true" : "false");
        json += ",\"irregular\":";
        json += (statusIrregular ? "true" : "false");
        json += ",\"firmware\":\"twatch-s3-visual-v4\"}";
        server.send(200, "application/json", json);
    });

    server.on("/vibrate", HTTP_POST, []() {
        Serial.print("Comando /vibrate recebido de ");
        Serial.println(server.client().remoteIP());
        uint8_t effect = HAPTIC_DEFAULT_EFFECT;
        if (server.hasArg("effect")) {
            effect = static_cast<uint8_t>(server.arg("effect").toInt());
        }

        // Mensagem opcional enviada pelo detector (ex.: "Anomalia identificada: falta de EPI").
        String message = server.hasArg("message") ? server.arg("message") : String();
        String detail = server.hasArg("detail") ? server.arg("detail") : String();
        if (message.length() > 120) message = message.substring(0, 120);
        if (detail.length() > 100) detail = detail.substring(0, 100);
        if (message.length() > 0) {
            Serial.print("Mensagem do alerta: ");
            Serial.println(message);
        }

        bool hapticAccepted = startVibration(effect);
        showVisualAlert(hapticAccepted, message, detail);
        if (!hapticAccepted) {
            server.send(503, "application/json", "{\"ok\":false,\"configured\":false,\"message\":\"DRV2605 nao encontrado em SDA=IO10 SCL=IO11.\"}");
            return;
        }

        String json = "{\"ok\":true,\"configured\":true,\"message\":\"Haptic DRV2605 acionado.\",\"effect\":";
        json += String(effect);
        json += ",\"display_ready\":";
        json += (isAlertDisplayReady() ? "true" : "false");
        json += ",\"visual_alert_active\":";
        json += (isVisualAlertActive() ? "true" : "false");
        json += ",\"firmware\":\"twatch-s3-visual-v4\"}";
        server.send(200, "application/json", json);
    });

    server.on("/status", HTTP_POST, []() {
        String state = server.hasArg("state") ? server.arg("state") : String("idle");
        state.toLowerCase();
        unsigned long now = millis();
        if (server.hasArg("interval")) {
            long value = server.arg("interval").toInt();
            if (value >= 3 && value <= 300) statusVibrationIntervalMs = value * 1000UL;
        }
        if (server.hasArg("ttl")) {
            long value = server.arg("ttl").toInt();
            if (value >= 5 && value <= 600) statusTtlMs = value * 1000UL;
        }
        lastStatusUpdateMs = now;

        if (state == "irregular") {
            String message = server.hasArg("message") ? server.arg("message") : String();
            String missing = server.hasArg("missing") ? server.arg("missing") : String();
            if (message.length() > 120) message = message.substring(0, 120);
            if (missing.length() > 60) missing = missing.substring(0, 60);
            bool wasIrregular = statusIrregular;
            statusIrregular = true;
            showIrregular(message, missing);
            if (!wasIrregular) {
                Serial.print("Status IRREGULAR. Faltando: ");
                Serial.println(missing);
                startVibration(HAPTIC_DEFAULT_EFFECT);
                lastPeriodicVibrationMs = now;
            }
        } else if (state == "regular") {
            if (statusIrregular) {
                Serial.println("Status REGULAR.");
                showRegular();
                softClick();
            }
            statusIrregular = false;
        } else {
            if (statusIrregular) clearStatus();
            statusIrregular = false;
        }

        String json = "{\"ok\":true,\"irregular\":";
        json += (statusIrregular ? "true" : "false");
        json += ",\"haptic_ready\":";
        json += (hapticReady ? "true" : "false");
        json += ",\"firmware\":\"twatch-s3-visual-v4\"}";
        server.send(200, "application/json", json);
    });

    server.onNotFound([]() {
        server.send(404, "application/json", "{\"ok\":false,\"message\":\"Rota nao encontrada.\"}");
    });

    server.begin();
    webServerStarted = true;
    Serial.println("Servidor web pronto na porta 80.");
    if (WEB_AP_MODE) {
        Serial.print("URL AP: http://");
        Serial.println(WiFi.softAPIP());
    }
}

void maintainWiFiConnection()
{
    wl_status_t currentStatus = WiFi.status();
    unsigned long now = millis();

    if (currentStatus == WL_CONNECTED) {
        if (lastWiFiStatus != WL_CONNECTED) {
            printWiFiStatus();
            if (!otaConfigured) {
                configureOTA();
            }
            if (!webServerStarted) {
                configureWebServer();
            }
        }

        lastWiFiStatus = currentStatus;
        return;
    }

    if (lastWiFiStatus == WL_CONNECTED) {
        Serial.println("Wi-Fi desconectado. Tentando reconectar sem bloquear o sistema.");
    }

    if (now - lastWiFiProgressMs >= WIFI_CONNECT_PROGRESS_MS) {
        Serial.print(".");
        lastWiFiProgressMs = now;
    }

    if (now - lastWiFiAttemptMs >= WIFI_RETRY_INTERVAL_MS) {
        Serial.println("\nNova tentativa de conexao Wi-Fi.");
        WiFi.disconnect();
        connectWiFi();
    }

    lastWiFiStatus = currentStatus;
}
}

void setup()
{
    Serial.begin(115200);
    delay(1000);

    Serial.println();
    Serial.println("LILYGO T-Watch S3 - alerta visual + vibracao + OTA");

    configureWatchPower();
    configureHaptic();
    configureAlertDisplay();

    WiFi.mode(WEB_AP_MODE ? WIFI_AP_STA : WIFI_STA);
    WiFi.setSleep(false);
    configureWiFiEvents();
    printWiFiScan();
    configureAccessPoint();
    configureWebServer();
    connectWiFi();
}

void loop()
{
    if (otaConfigured) {
        ArduinoOTA.handle();
    }
    if (webServerStarted) {
        server.handleClient();
    }

    maintainWiFiConnection();
    updateAlertDisplay();

    if (statusIrregular) {
        unsigned long now = millis();
        if (now - lastStatusUpdateMs >= statusTtlMs) {
            Serial.println("Sem atualizacao do detector; encerrando alerta continuo.");
            statusIrregular = false;
            clearStatus();
        } else if (now - lastPeriodicVibrationMs >= statusVibrationIntervalMs) {
            lastPeriodicVibrationMs = now;
            startVibration(HAPTIC_DEFAULT_EFFECT);
        }
    }
    delay(10);
}
