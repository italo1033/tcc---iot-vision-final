$ErrorActionPreference = "Stop"

Set-Location -Path (Join-Path $PSScriptRoot "..")

if (-not (Test-Path -LiteralPath ".env")) {
    Write-Error "Arquivo .env nao encontrado. Copie .env.example para .env e ajuste as variaveis."
}

Get-Content -LiteralPath ".env" | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) {
        return
    }

    $parts = $line -split "=", 2
    if ($parts.Count -eq 2) {
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($value.Length -ge 2) {
            $first = $value.Substring(0, 1)
            $last = $value.Substring($value.Length - 1, 1)
            if (($first -eq "'" -and $last -eq "'") -or ($first -eq '"' -and $last -eq '"')) {
                $value = $value.Substring(1, $value.Length - 2)
            }
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

if ([string]::IsNullOrWhiteSpace($env:ESP32_WINDOWS_PORT)) {
    Write-Error "ESP32_WINDOWS_PORT nao foi definido no .env. Exemplo: ESP32_WINDOWS_PORT=COM3"
}

Write-Host "Portas seriais detectadas:"
Get-CimInstance Win32_SerialPort |
    Select-Object DeviceID, Name, PNPDeviceID |
    Format-Table -AutoSize

Write-Host "Testando acesso a $env:ESP32_WINDOWS_PORT..."
$serialPort = $null
try {
    $serialPort = New-Object System.IO.Ports.SerialPort $env:ESP32_WINDOWS_PORT, 115200, None, 8, One
    $serialPort.ReadTimeout = 500
    $serialPort.WriteTimeout = 500
    $serialPort.Open()
    Write-Host "Porta $env:ESP32_WINDOWS_PORT abriu corretamente no teste."
} catch {
    Write-Host ""
    Write-Host "Nao foi possivel abrir $env:ESP32_WINDOWS_PORT antes do upload."
    Write-Host "Possiveis causas:"
    Write-Host "- Monitor serial, Arduino IDE, PlatformIO, PuTTY ou outro app esta usando a porta."
    Write-Host "- A placa precisa entrar em modo BOOT/Download."
    Write-Host "- O dispositivo USB travou; desconecte e conecte o cabo novamente."
    Write-Host "- O cabo ou hub USB falhou; teste outra porta USB/cabo de dados."
    Write-Host ""
    Write-Host "Tente: feche monitores seriais, desconecte/reconecte a placa, segure BOOT, aperte RESET, solte BOOT e rode de novo."
    Write-Error $_
} finally {
    if ($serialPort -and $serialPort.IsOpen) {
        $serialPort.Close()
    }
}

$pio = Get-Command pio -ErrorAction SilentlyContinue
if (-not $pio) {
    Write-Error "PlatformIO CLI nao encontrado no Windows. Instale com: py -m pip install platformio"
}

Write-Host ""
Write-Host "Preparando upload em $env:ESP32_WINDOWS_PORT."
Write-Host "Se aparecer 'Connecting...', coloque a placa em BOOT:"
Write-Host "1. Mantenha BOOT pressionado."
Write-Host "2. Aperte e solte RESET."
Write-Host "3. Solte BOOT quando o upload iniciar."
Write-Host ""

pio run -e twatch-s3 -t upload --upload-port $env:ESP32_WINDOWS_PORT
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Upload falhou. Se o erro citar 'No serial data received', a placa nao respondeu ao bootloader."
    Write-Host "1. Feche qualquer monitor serial."
    Write-Host "2. Remova e conecte o cabo USB novamente."
    Write-Host "3. Mantenha BOOT pressionado, aperte e solte RESET, e solte BOOT apenas quando o upload comecar."
    Write-Host "4. Rode este script novamente."
    Write-Host "5. Se a porta COM mudar em modo BOOT, atualize ESP32_WINDOWS_PORT no .env."
    exit $LASTEXITCODE
}
