Import("env")

import os
import re
import sys

REQUIRED_VARIABLES = (
    "WIFI_SSID",
    "WIFI_PASSWORD",
    "OTA_HOSTNAME",
    "OTA_PASSWORD",
)

OPTIONAL_VARIABLES = (
    "WEB_AP_MODE",
    "WEB_AP_SSID",
    "WEB_AP_PASSWORD",
)


def parse_dotenv(path):
    values = {}
    if not os.path.exists(path):
        return values

    with open(path, "r", encoding="utf-8") as dotenv:
        for raw_line in dotenv:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue

            name, value = line.split("=", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue

            values[name] = normalize_env_value(value)

    return values


def fail(message):
    print(f"\nErro de configuracao: {message}", file=sys.stderr)
    print("Crie um arquivo .env a partir de .env.example ou exporte as variaveis antes do pio run.", file=sys.stderr)
    env.Exit(1)


def validate_macro_name(name):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        fail(f"nome de macro invalido: {name}")


def normalize_env_value(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def quote_for_cpp(value):
    if hasattr(env, "StringifyMacro"):
        return env.StringifyMacro(value)

    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'\\"{escaped}\\"'


dotenv_values = parse_dotenv(".env")

for name in REQUIRED_VARIABLES + OPTIONAL_VARIABLES:
    if os.environ.get(name):
        os.environ[name] = normalize_env_value(os.environ[name])
    elif name in dotenv_values:
        os.environ[name] = dotenv_values[name]

missing = [name for name in REQUIRED_VARIABLES if not os.environ.get(name)]
if missing:
    fail("variaveis ausentes ou vazias: " + ", ".join(missing))

for name in REQUIRED_VARIABLES:
    validate_macro_name(name)
    env.Append(CPPDEFINES=[(name, quote_for_cpp(os.environ[name]))])

for name in OPTIONAL_VARIABLES:
    value = os.environ.get(name)
    if value is None or value == "":
        continue

    if name == "WEB_AP_MODE":
        try:
            int(value)
        except ValueError:
            fail(f"{name} deve ser um numero inteiro.")

    validate_macro_name(name)
    if name in ("WEB_AP_SSID", "WEB_AP_PASSWORD"):
        env.Append(CPPDEFINES=[(name, quote_for_cpp(value))])
    else:
        env.Append(CPPDEFINES=[(name, value)])

ota_host = os.environ.get("ESP32_OTA_HOST", "")
if env.subst("$UPLOAD_PROTOCOL") == "espota" and not ota_host:
    fail("ESP32_OTA_HOST e obrigatorio para upload OTA.")
