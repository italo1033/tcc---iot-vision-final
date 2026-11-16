#pragma once

#include <Arduino.h>

void configureWatchPower();
void configureAlertDisplay();
void showVisualAlert(bool hapticAccepted);
void showVisualAlert(bool hapticAccepted, const String &message, const String &detail);
void updateAlertDisplay();
void showIrregular(const String &message, const String &missing);
void showRegular();
void clearStatus();
bool isIrregular();
bool isAlertDisplayReady();
bool isVisualAlertActive();
